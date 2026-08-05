"""Wheel 期权策略核心 — 状态机 + 期权筛选"""
from __future__ import annotations

import re
from datetime import date, timedelta
from enum import Enum, auto
from typing import Optional

from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from config import settings
from core.alpaca_client import AlpacaClients
from execution.option_order_manager import OptionOrderManager
from strategy.wheel_filters import (
    pre_open_put_checks, pre_open_call_checks, kelly_contracts,
    check_buying_power_sufficient,
)
from strategy.wheel_switch import maybe_switch, get_active_symbol
from strategy.wheel_evaluator import evaluate_and_maybe_plan
from loguru import logger

# 交易日志（best-effort，不影响主流程）
try:
    from metrics import trade_journal as _journal
except Exception:
    _journal = None


def _safe_journal(fn_name: str, **kwargs):
    """安全调用 trade_journal，失败仅 warn 不抛异常"""
    if _journal is None:
        return
    try:
        getattr(_journal, fn_name)(**kwargs)
    except Exception as e:
        logger.warning(f"[Journal] {fn_name} 失败: {e}")


class WheelPhase(Enum):
    IDLE       = auto()   # 无持仓，准备卖 Put
    SHORT_PUT  = auto()   # 已有 Short Put（未成交或持仓）
    LONG_STOCK = auto()   # 被行权持有股票，准备卖 Call
    SHORT_CALL = auto()   # 已有 Short Call（未成交或持仓）


def _parse_symbol(symbol: str) -> dict:
    """解析 OCC 期权代码 → {underlying, expiry, type, strike}
    格式示例：CSCO250516P00060000
    """
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', symbol)
    if not m:
        return {}
    underlying, date_str, opt_type, strike_str = m.groups()
    expiry = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:]))
    strike = int(strike_str) / 1000.0
    return {"underlying": underlying, "expiry": expiry, "type": opt_type, "strike": strike}


def _otm_pct(stock_price: float, strike: float, opt_type: str) -> float:
    """How far OUT OF THE MONEY an option is, as a fraction of strike.
    Positive = OTM (safe), negative = ITM (at risk).

    Direction depends on the contract type — this is the bug this helper
    fixes. The old inline formula `(stock - strike) / strike` is written
    for PUTS only:
      - PUT  is OTM when stock > strike → (stock - strike) / strike
      - CALL is OTM when stock < strike → (strike - stock) / strike

    Applying the put formula to a covered call made a SAFELY out-of-the-money
    call (e.g. MSFT $490 vs $505 strike) report -2.85%, so:
      - v11 "let winners ride" (needs ≥5%) could never fire for calls
      - v7 expiry-day logic saw it as "<1% → 太接近行权价 → 强制BTC" and
        bought back a call that was about to expire worthless for free.
    """
    if strike <= 0:
        return 0.0
    if str(opt_type).upper() == "C":
        return (strike - stock_price) / strike
    return (stock_price - strike) / strike


class WheelStrategy:
    def __init__(self, symbol: str = None):
        self.symbol = symbol or settings.WHEEL_SYMBOL
        self._trading = AlpacaClients.trading()
        self._data = OptionHistoricalDataClient(
            api_key=settings.API_KEY,
            secret_key=settings.SECRET_KEY,
        )
        self._option_mgr = OptionOrderManager()

    def _calc_total_put_collateral(self) -> float:
        """计算账户所有 Short Put 的总抵押（跨标的合计，防爆仓用）"""
        return self._calc_put_collateral_by_sector().get("__total__", 0.0)

    def _calc_put_collateral_by_sector(self) -> dict:
        """Return {sector: collateral_sum} for every short put on the account,
        plus a "__total__" key. Used by the sector-exposure filter.

        v12 修 C: also counts pending SELL Put limit orders. Without this,
        two parallel wheels in the same cron tick could both think there's
        full headroom (wheel 1 places limit order → wheel 2 reads positions
        before fill → underestimates collateral).
        """
        from strategy.sector_map import sector_of
        buckets: dict[str, float] = {}
        total = 0.0
        try:
            # ── 已成交持仓 ──
            all_positions = self._trading.get_all_positions()
            for pos in all_positions:
                info = _parse_symbol(pos.symbol)

                # 期权 short put
                if info and info.get("type") == "P":
                    qty = float(pos.qty)
                    if qty >= 0:  # skip long puts
                        continue
                    collateral = info["strike"] * 100 * abs(qty)
                    sec = sector_of(info["underlying"])
                    buckets[sec] = buckets.get(sec, 0.0) + collateral
                    total += collateral
                    continue

                # v12 修 C2 (5/30): 被行权后的股票持仓也计入同板块暴露。
                # 不然 wheel 2 选新标的时会忽略已被行权的同板块股票，
                # 可能叠加成 sector cap 60% 之上（例：AVGO 股票 $40K
                # + QCOM 新 put $23K = $63K = 62% ai_hardware，越界）。
                if not info:  # 非 option symbol = 股票
                    try:
                        qty = float(pos.qty)
                        if qty < 100:
                            continue  # 不到 100 股不算 wheel 持仓
                        stock_value = float(pos.market_value or 0)
                        if stock_value <= 0:
                            stock_value = float(pos.current_price or 0) * qty
                        sec = sector_of(pos.symbol)
                        buckets[sec] = buckets.get(sec, 0.0) + stock_value
                        total += stock_value
                        logger.debug(
                            f"被行权持股计入 sector: {pos.symbol} x{qty} "
                            f"= ${stock_value:,.0f} ({sec})"
                        )
                    except Exception as e:
                        logger.debug(f"股票 sector 统计失败 {pos.symbol}: {e}")
                    continue

            # ── v12 修 C: 未成交的 SELL Put 限价单也算抵押 ──
            try:
                from alpaca.trading.requests import GetOrdersRequest
                from alpaca.trading.enums import QueryOrderStatus
                open_orders = self._trading.get_orders(
                    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
                )
                for o in open_orders:
                    if not o.symbol or len(o.symbol) < 10:
                        continue
                    info = _parse_symbol(o.symbol)
                    if not info or info.get("type") != "P":
                        continue
                    if "SELL" not in str(o.side).upper():
                        continue
                    qty = float(o.qty or 0)
                    if qty <= 0:
                        continue
                    collateral = info["strike"] * 100 * qty
                    sec = sector_of(info["underlying"])
                    buckets[sec] = buckets.get(sec, 0.0) + collateral
                    total += collateral
                    logger.debug(f"待成交挂单计入抵押: {o.symbol} qty={qty} → ${collateral:,.0f}")
            except Exception as e:
                logger.debug(f"未成交挂单抵押统计失败（忽略）: {e}")

        except Exception as e:
            logger.warning(f"计算 sector 抵押失败: {e}")
        buckets["__total__"] = total
        return buckets

    def _record_quote_ok(self, symbol: str) -> None:
        """P2.4: reset consecutive-skip counter on a good quote."""
        try:
            from metrics.skip_monitor import record_ok
            record_ok(symbol)
        except Exception:
            pass

    def _record_quote_skip(self, symbol: str) -> None:
        """P2.4: increment consecutive-skip counter, alert at threshold."""
        try:
            from metrics.skip_monitor import record_skip
            record_skip(symbol)
        except Exception:
            pass

    def _get_stock_fair_price(self, mark: float) -> Optional[float]:
        """Bug #2 fix: validated stock price for the -35% stop decision.

        Returns a trustworthy price, or None if data is unreliable.

        Design (risk-review concern D): do NOT cross-check against the
        position mark — mark LAGS in fast moves, so a real crash (fresh
        price legitimately far below a stale mark) would be wrongly
        rejected and the stop skipped exactly when needed. Instead anchor
        on the LATEST TRADE (a real transaction = ground truth):
          1. NBBO health: bid>0, ask>0, spread/mid ≤ 5% (a liquid
             wheel-universe stock never has >5% spread; a momentary bad
             print widens spread or goes one-sided)
          2. Cross-check mid vs latest trade ≤ 8% (catches a tight-but-
             wrong quote that survived the spread test — it won't match
             the last real trade). A real crash moves BOTH mid and last
             trade together → passes → stop fires.
          3. Return mid.
        `mark` is kept in the signature for logging only.
        """
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
            dc = StockHistoricalDataClient(
                api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
            )
            q = dc.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=self.symbol)
            )[self.symbol]
            bid = float(q.bid_price or 0)
            ask = float(q.ask_price or 0)
            if bid <= 0 or ask <= 0:
                logger.warning(f"_get_stock_fair_price({self.symbol}): one-sided quote, skip stop")
                return None
            mid = (bid + ask) / 2
            # NBBO health: liquid stock shouldn't have >5% spread.
            if (ask - bid) / mid > 0.05:
                logger.warning(
                    f"_get_stock_fair_price({self.symbol}): spread {(ask-bid)/mid:.1%} >5% "
                    f"— 报价异常, 跳过止损本轮 (mark ${mark:.2f})"
                )
                return None
            # Anchor on latest trade (real transaction), NOT the lagging mark.
            # A real crash moves mid AND last together → passes. A tight bad
            # quote won't match the last real trade → rejected.
            try:
                t = dc.get_stock_latest_trade(
                    StockLatestTradeRequest(symbol_or_symbols=self.symbol)
                )[self.symbol]
                last = float(t.price or 0)
                if last > 0 and abs(mid - last) / last > 0.08:
                    logger.warning(
                        f"_get_stock_fair_price({self.symbol}): mid ${mid:.2f} vs "
                        f"last trade ${last:.2f} 偏差 >8% — 报价可疑(非成交价), 跳过止损本轮"
                    )
                    return None
            except Exception as e:
                logger.debug(f"latest_trade cross-check skipped: {e}")
                # No trade data → fall back to quote-only (NBBO health passed).
            return mid
        except Exception as e:
            logger.warning(f"_get_stock_fair_price({self.symbol}) failed: {e}")
            return None

    def _open_stock_sells(self, symbol: str) -> list:
        """Return OPEN sell orders on the underlying STOCK (exact symbol)."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            orders = self._trading.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            )
            return [o for o in orders
                    if o.symbol == symbol and str(o.side).upper().endswith("SELL")]
        except Exception as e:
            logger.debug(f"查询股票挂单失败: {e}")
            return []

    def _cancel_open_stock_sells(self, symbol: str) -> bool:
        """Cancel any OPEN sell order on the underlying STOCK. Returns True if
        no open stock-sell remains afterward (safe to place a new one).
        Phase 4.1: re-pricing each cycle; without cancelling the prior unfilled
        limit, a second order would oversell. Bug-A fix: verify cancellation
        actually cleared before allowing a new submit."""
        open_sells = self._open_stock_sells(symbol)
        for o in open_sells:
            try:
                self._trading.cancel_order_by_id(o.id)
                logger.info(f"撤销旧止损挂单 {symbol} (order={o.id}) 以便重新定价")
            except Exception as e:
                logger.debug(f"撤单 {o.id} 失败: {e}")
        return len(self._open_stock_sells(symbol)) == 0

    def _try_stock_stop_loss(self, obj, stock_price: float, cost_basis: float,
                             drop_pct: float) -> bool:
        """Phase 4.1 — escalating stock stop-loss with halt-awareness.

        Three states across cron cycles (state persisted in stop_state.json):
          - Cycle 1-2: LIMIT @ stock_price × 0.97 (filters bad data, allows
                        rebound — a transient bad print won't persist 2 cycles)
          - Cycle 3+ : MARKET sell (≥16 min still below -35% = real crash,
                        stop chasing, guarantee exit)

        Step 0 halt check: non-tradable (LULD halt — most likely during a
        crash) → wait; don't queue an order that would jam. Not an attempt.
        Re-prices each cycle: cancels prior unfilled stop first (oversell guard).
        Returns True always (caller returns after a stop decision)."""
        from datetime import datetime, timezone
        from metrics import stop_state

        # Step 0: LULD / tradability check
        try:
            asset = self._trading.get_asset(self.symbol)
            if not getattr(asset, "tradable", True):
                logger.warning(
                    f"⏸ {self.symbol} 当前不可交易(熔断 LULD?), 等待恢复, "
                    f"不挂止损单 (跌幅 {drop_pct:.1%})"
                )
                return True
        except Exception as e:
            logger.debug(f"asset 可交易状态检查失败(继续): {e}")

        # Bug-A: cancel prior stop AND verify cleared; else skip (no stacking).
        if not self._cancel_open_stock_sells(self.symbol):
            logger.warning(
                f"⏸ {self.symbol} 旧止损单未能撤销, 本轮跳过(避免重复挂单致超卖), 等下个 tick"
            )
            return True

        # Bug-B: re-fetch live qty right before submit (old limit may have filled).
        try:
            fresh = self._trading.get_open_position(self.symbol)
            qty_to_sell = int(float(fresh.qty))
        except Exception:
            qty_to_sell = int(float(obj.qty))
        if qty_to_sell < 1:
            logger.info(f"{self.symbol} 已无持仓(止损已成交?), 重置止损状态")
            stop_state.reset(self.symbol)
            return True

        now_iso = datetime.now(timezone.utc).isoformat()
        attempts = stop_state.record_attempt(self.symbol, now_iso)

        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        try:
            if attempts <= 2:
                limit = round(stock_price * 0.97, 2)
                logger.warning(
                    f"🔴 止损 第{attempts}轮(限价): {self.symbol} 成本 ${cost_basis:.2f} → "
                    f"验证价 ${stock_price:.2f} ({drop_pct:.1%}), 限价卖 {qty_to_sell} 股 @ ${limit:.2f}"
                )
                req = LimitOrderRequest(
                    symbol=self.symbol, qty=qty_to_sell, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, limit_price=limit,
                )
                reason = f"stop_limit_attempt{attempts} drop={drop_pct:.1%} limit={limit}"
            else:
                mins = (attempts - 1) * 8
                logger.warning(
                    f"🔴🔴 止损 第{attempts}轮(市价): {self.symbol} 持续崩盘 "
                    f"({drop_pct:.1%}) 约 {mins} 分钟未出, 市价清仓 {qty_to_sell} 股"
                )
                req = MarketOrderRequest(
                    symbol=self.symbol, qty=qty_to_sell, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
                reason = f"stop_market_attempt{attempts} drop={drop_pct:.1%}"
            self._trading.submit_order(req)
            _safe_journal("log_skip", symbol=self.symbol, action="sell_stock",
                          skip_reason=reason)
        except Exception as e:
            logger.error(f"止损卖股失败: {e}")
        return True

    def _theoretical_max_price(self, option_symbol: str) -> Optional[float]:
        """Black-Scholes physical upper bound for an option price. Used as a
        4th-layer sanity check: even a LAST TRADE can be a single anomalous
        print (verified 6/5: GM $78P had a 1-lot $0.52 print while real
        value was ~$0.01). last/mid layers can't catch a poisoned last;
        this physical bound can.

        bound = max(BS(vol=max(rv,100%)) × 3,  intrinsic + max($0.30, K×0.5%))
        The absolute buffer prevents false-rejecting normal near-expiry
        residual value (0DTE OTM puts trade at $0.02-0.10 even when BS≈0).
        Bound is deliberately wide — it only catches grossly impossible
        prices (~5×+), leaving precise pricing to the last/mid layers.
        """
        try:
            info = _parse_symbol(option_symbol)
            if not info:
                return None
            K = info["strike"]
            is_call = info["type"] == "C"
            dte = (info["expiry"] - date.today()).days
            T = max(dte, 0.5) / 365.0
            S = self._get_stock_fair_price(K)
            if S is None or S <= 0:
                return None
            from strategy.wheel_filters import compute_realized_vol
            rv = max(compute_realized_vol(self.symbol) or 0.60, 1.0)
            from backtest.wheel_pricing import bs_price
            theo = bs_price(S, K, T, r=0.04, sigma=rv, is_call=is_call)
            intrinsic = max(S - K, 0) if is_call else max(K - S, 0)
            abs_buffer = max(0.30, K * 0.005)
            return max(theo * 3, intrinsic + abs_buffer)
        except Exception as e:
            logger.debug(f"_theoretical_max_price({option_symbol}) failed: {e}")
            return None

    def _finalize_fair(self, symbol: str, price: float, source: str) -> Optional[float]:
        """Apply the intrinsic-value bound to a candidate price, record stats.
        Returns the price if it passes the physical bound, else None."""
        bound = self._theoretical_max_price(symbol)
        if bound is None:
            # 4th layer disabled this cycle (stock data unavailable for BS).
            # Layers 1-3 still applied, so SL/TP not disabled — just no extra
            # anomaly catch. Log so the silent skip is observable.
            logger.debug(f"fair_price({symbol}) = {source} ${price:.3f} (bound 不可用, 仅前3层)")
            self._record_quote_ok(symbol)
            self._record_quote_stat(source)
            return price
        if price > bound:
            logger.warning(
                f"fair_price({symbol}): {source} ${price:.2f} > 理论上限 ${bound:.2f} "
                f"— 物理上不可能(单笔抽风?), 拒绝"
            )
            self._record_quote_stat("reject_bound")
            self._record_quote_skip(symbol)
            return None
        logger.debug(f"fair_price({symbol}) = {source} ${price:.3f} (≤ bound ${bound:.2f})")
        self._record_quote_ok(symbol)
        self._record_quote_stat(source)
        return price

    def _record_quote_stat(self, outcome: str) -> None:
        try:
            from metrics.quote_stats import record
            record(outcome)
        except Exception:
            pass

    def _get_option_fair_price(self, symbol: str) -> Optional[float]:
        """Return best-available fair price for an option, in priority order:
          1. Last trade if fresh (+ fat-finger clamp)
          2. Mid (bid+ask)/2 if NBBO healthy
          3. ALL candidates pass an intrinsic-value physical bound (layer 4)
          4. None — caller MUST skip decision (don't fall back to mark!)

        Phase 1 fix (6/5 incident): returned None on bid=0 → caller fell
        back to ASK-skewed mark → GM $78P false stop BTC $2.13 vs $0.01.
        Phase 3: added intrinsic-value bound — a last trade can ALSO be a
        single anomalous print (GM had a 1-lot $0.52 print); the bound
        catches what last/mid can't.
        """
        try:
            from alpaca.data.requests import OptionSnapshotRequest
            from datetime import datetime, timezone
            snap = self._data.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=symbol)
            )
            s = snap.get(symbol) if isinstance(snap, dict) else snap[symbol]
            if not s:
                return None

            # Feed-aware freshness thresholds. Alpaca paper/free option data
            # is 15-min delayed → timestamps are always ~900s old. With a
            # real-time 15s/300s threshold the mid/last paths would NEVER pass
            # on paper, disabling all SL/TP. settings auto-relaxes for delayed.
            last_max_age = getattr(settings, "LAST_TRADE_MAX_AGE_S", 1200)
            quote_max_age = getattr(settings, "QUOTE_MAX_AGE_S", 1200)

            # ── Priority 1: Last trade within freshness window (fat-finger clamp) ──
            if s.latest_trade and s.latest_trade.price and s.latest_trade.timestamp:
                age = (datetime.now(timezone.utc) - s.latest_trade.timestamp).total_seconds()
                price = float(s.latest_trade.price)
                if age < last_max_age and price > 0:
                    # Fat-finger sanity: last trade must be within reason vs ask.
                    # Prevents trusting a one-off anomalous print (e.g. someone
                    # market-bought at stale ask, printing 100x mid).
                    if s.latest_quote:
                        ask = float(s.latest_quote.ask_price or 0)
                        if ask > 0 and price > ask * 2:
                            logger.warning(
                                f"fair_price({symbol}): last ${price} > ask ${ask}*2, "
                                f"fat-finger suspected, falling through to mid"
                            )
                        else:
                            return self._finalize_fair(symbol, price, "last")
                    else:
                        return self._finalize_fair(symbol, price, "last")

            # ── Priority 2: Mid quote if NBBO healthy ──
            # Phase 2 (P2.3): tightened spread gate 50% → 25%.
            # 50% was too loose — a $0.01/$0.015 quote (spread/mid=40%) passed
            # but is barely tradeable. 25% matches IBKR/CBOE liquidity norms.
            # Age threshold is feed-aware (quote_max_age) so paper's 15-min
            # delay doesn't permanently disable the mid path.
            if s.latest_quote:
                bid = float(s.latest_quote.bid_price or 0)
                ask = float(s.latest_quote.ask_price or 0)
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    spread_ratio = (ask - bid) / mid
                    q_age = 9999
                    if s.latest_quote.timestamp:
                        q_age = (datetime.now(timezone.utc) - s.latest_quote.timestamp).total_seconds()
                    if spread_ratio <= 0.25 and q_age < quote_max_age:
                        return self._finalize_fair(symbol, mid, "mid")
                    logger.debug(
                        f"fair_price({symbol}): mid rejected — spread {spread_ratio:.0%} "
                        f"(need ≤25%) or age {q_age:.0f}s (need <{quote_max_age}s)"
                    )

            # ── Priority 3: nothing reliable — caller must SKIP ──
            logger.warning(f"fair_price({symbol}): no reliable quote — skip SL/TP decision")
            self._record_quote_stat("reject_none")
            self._record_quote_skip(symbol)
            return None
        except Exception as e:
            logger.debug(f"_get_option_fair_price({symbol}) failed: {e}")
            return None

    def _try_stop_loss(self, pos, loss_multiple: float = 3.0) -> bool:
        """Tail-risk killer: BTC at market when a short option's price has
        risen to `loss_multiple` × entry premium.

        Without this, a single ITM blowup (UNH down 20% on a fraud headline,
        TSLA earnings miss, etc.) can wipe out weeks of profits and force
        assignment at the worst possible price. This caps single-trade loss
        at ~2× premium received: we sold for $X, paid up to $3X to close.

        Triggered BEFORE _try_take_profit in the SHORT_PUT/SHORT_CALL branch
        — if we're in stop-loss territory we obviously aren't in
        take-profit territory.

        Idempotent: skips if a BTC order already exists on this contract.
        """
        try:
            qty = float(pos.qty)
            if qty >= 0:
                return False
            entry = float(pos.avg_entry_price)
            # Phase 1 fix (6/5): NEVER fall back to mark for SL decision.
            # mark is ASK-skewed on illiquid 0DTE strikes — caused $1,122
            # GM false-SL loss. If fair_price unavailable, SKIP this cycle.
            fair = self._get_option_fair_price(pos.symbol)
            if fair is None:
                logger.info(f"⏸ skip SL: {pos.symbol} 无可靠 quote, 等下个 tick")
                return False
            current = fair
            if entry <= 0:
                return False
            # We sold at `entry`, current price is what we'd pay to close.
            # Stop loss only fires when current is well above entry.
            if current < entry * loss_multiple:
                return False

            # Idempotency: don't double-place a BTC.
            try:
                open_orders = self._option_mgr.get_open_option_orders(self.symbol)
                for o in open_orders:
                    if o.symbol == pos.symbol and str(o.side).endswith("BUY"):
                        logger.warning(
                            f"⛔ stop-loss BTC 已挂单: {pos.symbol} (order={o.id})"
                        )
                        return True
            except Exception as e:
                logger.debug(f"开放订单检查失败: {e}")

            close_qty = int(abs(qty))
            # Aggressive limit: 5% above current to ensure fill in fast move.
            # Stop-loss triggers usually coincide with rapid IV expansion,
            # so spreads can blow out — 5% above mid is a sane ceiling.
            limit = round(current * 1.05, 2)
            limit = max(limit, current + 0.02)
            logger.warning(
                f"🛑 STOP-LOSS 触发: {pos.symbol} 入场 ${entry:.2f} → "
                f"现价 ${current:.2f} ({current/entry:.1f}x), "
                f"强制 BTC @ ${limit:.2f}"
            )
            self._option_mgr.buy_to_close(pos.symbol, close_qty, limit)
            return True
        except Exception as e:
            logger.warning(f"stop-loss 尝试失败（继续持有）: {e}")
            return False

    def _try_take_profit(self, pos, stock_price: float = None,
                         profit_threshold: float = 0.65) -> bool:
        """If a short option has decayed to ≤ (1 - threshold) × entry premium,
        buy to close.

        v10 (5/27): raised from 50% → 65% to capture more of each premium.
        Standard wheel convention is 50%, but backtest shows PRODUCTION
        config at 50% gave up ~4% annualized vs holding to 65% before BTC.
        Trade-off: positions held 1-2 days longer, slightly higher gamma
        exposure in the tail, but ~30% more $ captured per trade. Tasty-
        trade's research is Sharpe-optimal at 50% but absolute-return-
        optimal at 65%. We're now in the latter mode.

        Returns True if BTC order is in flight (new or already pending),
        False when we decided not to close / not eligible.

        v7 expiry-aware override: on the last day (DTE ≤ 1), skip BTC based
        on OTM% distance rather than premium price. OTM% is the true risk
        measure — a $0.01 premium can still be dangerous if the underlying
        is only $0.05 above the strike.

        Three-tier logic (DTE ≤ 1):
          - OTM ≥ safe_otm  → skip BTC, let it expire worthless
          - OTM 1–safe_otm% AND premium ≤ $0.15 → skip BTC (grey zone)
          - OTM < 1%        → must BTC (too close to strike)

        safe_otm is vol-adaptive:
          - Realized vol ≥ 4% daily → safe_otm = 5% (high-vol: LITE/SNDK/SMCI…)
          - Realized vol < 4% daily → safe_otm = 3% (normal: TSLA/AVGO/MSFT…)

        Safety:
         - stock_price=None → falls back to old premium-based logic (fail-open)
         - Skips if there's already an open BTC order on this contract
           (idempotent across 8-min cron re-runs).
         - Does NOT log an exit here — let the next run_cycle that sees
           the position actually gone log the exit uniformly (same path
           as assignment/expiration).
        """
        try:
            qty = float(pos.qty)
            if qty >= 0:
                return False    # not a short position
            entry = float(pos.avg_entry_price)
            # Phase 1 fix: same NEVER-fall-back-to-mark policy as SL.
            fair = self._get_option_fair_price(pos.symbol)
            if fair is None:
                logger.info(f"⏸ skip TP: {pos.symbol} 无可靠 quote, 等下个 tick")
                return False
            current = fair
            if entry <= 0:
                return False
            profit_pct = (entry - current) / entry
            if profit_pct < profit_threshold:
                return False

            # Expiry-aware override: OTM%-based skip on last day.
            try:
                info = _parse_symbol(pos.symbol)
                if info:
                    from datetime import date as _date
                    expiry = info["expiry"]
                    dte = (expiry - _date.today()).days
                    strike = info["strike"]

                    # v11 (5/29) — "let winners ride" mid-week:
                    # If profit already ≥ 65% (passed threshold above) AND
                    # we're still 2-5 days from expiry AND OTM cushion ≥ 5%,
                    # SKIP the BTC and let it expire naturally. This captures
                    # the remaining 35% of premium that the 65% BTC gives up.
                    # Backtest on 11 historical trades shows +$264 (+12%) incremental.
                    # Risk: 2-5 days of gamma exposure, but 5% cushion is wide
                    # enough to absorb normal market noise.
                    if (2 <= dte <= 5
                            and stock_price is not None
                            and strike > 0):
                        otm_pct = _otm_pct(stock_price, strike, info["type"])
                        if otm_pct >= 0.05:
                            logger.info(
                                f"⏭️ v11 hold: {pos.symbol} profit {profit_pct:.0%} "
                                f"+ OTM {otm_pct:.1%} ≥ 5% + DTE {dte} → "
                                f"让它自然到期，多拿剩余 ${current*100*int(abs(qty)):.0f}"
                            )
                            return False

                    if dte <= 1:
                        # Determine safe OTM threshold based on realized vol.
                        # compute_realized_vol returns ANNUALIZED RV (e.g. 0.60 = 60%).
                        # Threshold 0.60 annualized ≈ 3.8% daily — matches the
                        # high-vol symbols (LITE/SNDK/SMCI) identified in backtests.
                        # Falls back to 3% OTM if vol unavailable.
                        safe_otm = 0.03
                        if stock_price is not None:
                            try:
                                from strategy.wheel_filters import compute_realized_vol
                                rv = compute_realized_vol(self.symbol)
                                if rv is not None and rv >= 0.60:  # annualized ≥ 60%
                                    safe_otm = 0.05  # high-vol symbol → wider safety margin
                            except Exception:
                                pass  # use default 3%

                        if stock_price is not None and strike > 0:
                            otm_pct = _otm_pct(stock_price, strike, info["type"])
                            if otm_pct >= safe_otm:
                                logger.info(
                                    f"⏭️ skip BTC: {pos.symbol} OTM {otm_pct:.1%} "
                                    f"≥ {safe_otm:.0%} 安全线, DTE={dte} → 让它归零"
                                )
                                return False
                            elif otm_pct >= 0.01 and current <= 0.15:
                                logger.info(
                                    f"⏭️ skip BTC: {pos.symbol} OTM {otm_pct:.1%} "
                                    f"灰色地带但权利金 ${current:.2f} ≤ $0.15, DTE={dte} → 让它归零"
                                )
                                return False
                            elif otm_pct >= 0.01 and current > 0.15:
                                logger.info(
                                    f"🔄 BTC: {pos.symbol} OTM {otm_pct:.1%} 灰色地带"
                                    f"但权利金 ${current:.2f} > $0.15, DTE={dte} → 正常BTC"
                                )
                                # fall through to BTC logic below
                            elif otm_pct < 0.01:
                                logger.warning(
                                    f"⚠️ 强制BTC: {pos.symbol} OTM {otm_pct:.1%} "
                                    f"< 1% 太接近行权价, DTE={dte} → 必须平仓"
                                )
                                # fall through to BTC logic below
                        else:
                            # No stock price available — fall back to old logic
                            if current <= 0.10:
                                logger.info(
                                    f"⏭️ skip BTC: {pos.symbol} 权利金 ${current:.2f} "
                                    f"≤ $0.10 (无股价数据降级), DTE={dte} → 让它归零"
                                )
                                return False
            except Exception as e:
                logger.debug(f"expiry check failed: {e}")
                # fall through — better to BTC than skip on parse failure

            # Idempotency: is a BTC already open on this contract?
            try:
                open_orders = self._option_mgr.get_open_option_orders(self.symbol)
                for o in open_orders:
                    if o.symbol == pos.symbol and str(o.side).endswith("BUY"):
                        logger.info(
                            f"⏳ BTC 已挂单: {pos.symbol} (order={o.id}), 等成交"
                        )
                        return True
            except Exception as e:
                logger.debug(f"开放订单检查失败: {e}")
                # fall through — we'd rather risk a duplicate order than
                # never close, and Alpaca rejects true duplicates by id

            close_qty = int(abs(qty))
            # Pay a little over the bid/current to actually get filled.
            # For very cheap options (< $0.10) a flat +$0.02 wins over
            # percentage (which rounds to same cent).
            limit = max(round(current * 1.05, 2), current + 0.02)
            limit = max(limit, 0.01)
            logger.info(
                f"💰 {profit_threshold:.0%}-profit BTC 触发: {pos.symbol} 入场 ${entry:.2f} → "
                f"现价 ${current:.2f} ({profit_pct:.0%} 已实现), 挂 BTC @ ${limit:.2f}"
            )
            self._option_mgr.buy_to_close(pos.symbol, close_qty, limit)
            return True
        except Exception as e:
            logger.warning(f"BTC 尝试失败（继续持有）: {e}")
            return False

    # ── 阶段检测 ──────────────────────────────────────────────────────────────

    def get_phase(self) -> tuple[WheelPhase, object]:
        """判断当前 Wheel 阶段，返回 (WheelPhase, 相关持仓/订单对象)"""

        # 1. 检查期权持仓（已成交的 short option）
        for pos in self._option_mgr.get_option_positions(self.symbol):
            info = _parse_symbol(pos.symbol)
            if not info:
                continue
            qty = float(pos.qty)
            if info["type"] == "P" and qty < 0:
                logger.info(f"持仓 Short Put: {pos.symbol} qty={qty:.0f}")
                return WheelPhase.SHORT_PUT, pos
            if info["type"] == "C" and qty < 0:
                logger.info(f"持仓 Short Call: {pos.symbol} qty={qty:.0f}")
                return WheelPhase.SHORT_CALL, pos

        # 2. 检查期权未成交订单
        for order in self._option_mgr.get_open_option_orders(self.symbol):
            info = _parse_symbol(order.symbol)
            if not info:
                continue
            if info["type"] == "P":
                logger.info(f"挂单 Short Put: {order.symbol}")
                return WheelPhase.SHORT_PUT, order
            if info["type"] == "C":
                logger.info(f"挂单 Short Call: {order.symbol}")
                return WheelPhase.SHORT_CALL, order

        # 3. 检查股票持仓（被行权后持有股票）
        # Bug #3 fix: 之前只在 qty>=100 时返回 LONG_STOCK, 部分行权 (1-99 股)
        # 会落入 IDLE → 持有裸股却又去卖新 Put (风险叠加), 且这些股票无 CC、
        # 无止损管理。现在任何 ≥1 股都进 LONG_STOCK; CC 逻辑本身已有
        # contracts<1 守卫 (不足 100 股不卖 CC), 所以部分持仓会被"持有等待",
        # 不会卖新 put, 也不会乱卖 CC。
        try:
            pos = self._trading.get_open_position(self.symbol)
            qty = float(pos.qty)
            if qty >= 100:
                logger.info(f"持仓股票: {self.symbol} x{qty:.0f} 股 @ 成本 {pos.avg_entry_price}")
                # Phase 4.4: full lot → clear any partial-holding alert state.
                try:
                    from metrics import partial_alert
                    partial_alert.reset(self.symbol)
                except Exception:
                    pass
                return WheelPhase.LONG_STOCK, pos
            if qty >= 1:
                logger.warning(
                    f"⚠️ 部分持仓: {self.symbol} x{qty:.0f} 股 (<100, 部分行权?) "
                    f"@ 成本 {pos.avg_entry_price} — 持有等待, 不卖新 Put 也不卖 CC。"
                    f"请人工检查是否需补足/清理。"
                )
                # Phase 4.4: alert the operator (throttled 1/day/symbol).
                try:
                    from metrics import partial_alert
                    partial_alert.maybe_alert(self.symbol, qty, float(pos.avg_entry_price))
                except Exception:
                    pass
                return WheelPhase.LONG_STOCK, pos
        except Exception:
            pass  # 无股票持仓

        return WheelPhase.IDLE, None

    # ── 期权链 ─────────────────────────────────────────────────────────────────

    def _fetch_chain(self, contract_type: str) -> dict:
        """获取 WHEEL_MIN_DTE~WHEEL_MAX_DTE 的期权链"""
        today = date.today()
        dte_min = today + timedelta(days=settings.WHEEL_MIN_DTE)
        dte_max = today + timedelta(days=settings.WHEEL_MAX_DTE)
        try:
            req = OptionChainRequest(
                underlying_symbol=self.symbol,
                expiration_date_gte=dte_min,
                expiration_date_lte=dte_max,
                type=contract_type,
            )
            chain = self._data.get_option_chain(req)
            logger.info(f"期权链: {len(chain)} 个 {contract_type.upper()} 合约")
            return chain
        except Exception as e:
            logger.error(f"获取期权链失败: {e}")
            return {}

    def select_put(self) -> Optional[tuple[str, float, float]]:
        """筛选 delta ≈ -target_delta 的 Put，返回 (symbol, mid_price, delta).

        v6: target delta is now adaptive on IV rank — see
        wheel_filters.target_delta_for_iv_rank. High IV → smaller delta
        (further OTM, premium still ample). Low IV → standard 0.25.
        """
        from strategy.wheel_filters import compute_iv_rank, target_delta_for_iv_rank
        rank = compute_iv_rank(self.symbol)
        target_abs = target_delta_for_iv_rank(rank)
        target = -target_abs
        if rank is not None:
            logger.info(f"📊 {self.symbol} IV rank={rank:.0f} → target Δ={target:.2f}")

        chain = self._fetch_chain("put")
        best_sym, best_snap, best_diff = None, None, float("inf")

        for sym, snap in chain.items():
            if not snap.greeks or snap.greeks.delta is None:
                continue
            if not snap.latest_quote:
                continue
            # Bug #4 fix: gate out one-sided quotes (bid=0 or ask=0).
            # Scanner already does this; select_put didn't → bid=0 made
            # mid=ask/2 (underpriced sell). Match scanner's behavior.
            if (snap.latest_quote.bid_price or 0) <= 0 or (snap.latest_quote.ask_price or 0) <= 0:
                continue
            diff = abs(snap.greeks.delta - target)
            if diff < best_diff:
                best_diff = diff
                best_sym = sym
                best_snap = snap

        if best_sym is None:
            logger.warning("未找到合适的 Put 合约")
            return None

        bid = float(best_snap.latest_quote.bid_price or 0)
        ask = float(best_snap.latest_quote.ask_price or 0)
        mid = round((bid + ask) / 2, 2)
        delta = float(best_snap.greeks.delta)
        info = _parse_symbol(best_sym)
        logger.info(
            f"选定 Put: {best_sym} | 执行价={info['strike']:.2f} "
            f"到期={info['expiry']} Δ={delta:.3f} mid={mid:.2f}"
        )
        return best_sym, mid, delta

    def select_call(self, cost_basis: float) -> Optional[tuple[str, float, float]]:
        """筛选 delta ≈ +WHEEL_TARGET_DELTA 且执行价 ≥ cost_basis 的 Call，返回 (symbol, mid, delta)."""
        chain = self._fetch_chain("call")
        target = settings.WHEEL_TARGET_DELTA
        best_sym, best_snap, best_diff = None, None, float("inf")

        for sym, snap in chain.items():
            if not snap.greeks or snap.greeks.delta is None:
                continue
            if not snap.latest_quote:
                continue
            # Bug #4 fix: gate one-sided quotes (bid=0 or ask=0).
            if (snap.latest_quote.bid_price or 0) <= 0 or (snap.latest_quote.ask_price or 0) <= 0:
                continue
            info = _parse_symbol(sym)
            if not info or info["strike"] < cost_basis:
                continue  # 执行价必须 ≥ 成本价，避免锁定亏损
            diff = abs(snap.greeks.delta - target)
            if diff < best_diff:
                best_diff = diff
                best_sym = sym
                best_snap = snap

        if best_sym is None:
            logger.warning(f"未找到执行价 ≥ {cost_basis:.2f} 的 Call 合约")
            return None

        bid = float(best_snap.latest_quote.bid_price or 0)
        ask = float(best_snap.latest_quote.ask_price or 0)
        mid = round((bid + ask) / 2, 2)
        delta = float(best_snap.greeks.delta)
        info = _parse_symbol(best_sym)
        logger.info(
            f"选定 Call: {best_sym} | 执行价={info['strike']:.2f} "
            f"到期={info['expiry']} Δ={delta:.3f} mid={mid:.2f}"
        )
        return best_sym, mid, delta

    # ── 主循环 ─────────────────────────────────────────────────────────────────

    def run_cycle(self):
        """检查当前阶段并执行相应动作（幂等）"""
        # Reconcile exits first — this logs assignment / expiration / BTC-fill
        # events that happened since the last cron tick, so downstream code
        # (evaluator's "was last cycle profitable?", weekly review, etc.)
        # reads a current trade journal.
        try:
            from metrics.position_tracker import reconcile
            events = reconcile()
            if events:
                logger.info(f"[tracker] 探测到 {len(events)} 笔 exit，已写入 journal")
        except Exception as e:
            logger.warning(f"position reconcile failed (continuing): {e}")

        phase, obj = self.get_phase()
        logger.info(f"── Wheel 阶段: {phase.name} ──")

        is_idle = (phase == WheelPhase.IDLE)

        # ── 每轮评估：检查当前标的是否该切换（STOP/CAUTION/更优候选）──
        try:
            decision = evaluate_and_maybe_plan(
                current_symbol=self.symbol,
                current_phase_is_idle=is_idle,
            )
            logger.info(f"📊 评估: {decision['message']}")
            self.last_decision = decision
        except Exception as e:
            logger.warning(f"每轮评估失败（继续执行）: {e}")
            self.last_decision = None

        # ── 切换检查：若计划切换且当前 IDLE，则触发切换并直接返回，等下一 cron ──
        switched = maybe_switch(current_phase_is_idle=is_idle)
        if switched:
            old_sym, new_sym = switched
            logger.warning(f"🔄 已从 {old_sym} 切换到 {new_sym}，本次周期跳过，下一轮用新标的")
            # 更新实例标的以便本轮摘要正确
            self.symbol = new_sym
            return

        if phase == WheelPhase.IDLE:
            # Bug-E fix (Phase 4.1): position is closed (IDLE). Clear any
            # leftover stop-loss escalation state, else a future re-assignment
            # of this symbol that dips to -35% would skip the limit grace
            # cycles and fire MARKET immediately (stale attempts count).
            try:
                from metrics import stop_state
                if stop_state.get_attempts(self.symbol) > 0:
                    logger.info(f"{self.symbol} 已 IDLE(仓位关闭), 重置止损升级状态")
                    stop_state.reset(self.symbol)
            except Exception:
                pass
            # Phase 4.4: IDLE = no stock held → clear partial-holding alert.
            try:
                from metrics import partial_alert
                partial_alert.reset(self.symbol)
            except Exception:
                pass
            # ── 回测验证的过滤器：财报/MA50趋势/极端波动 ──
            ok, reason = pre_open_put_checks(self.symbol)
            if not ok:
                logger.warning(f"⏸️  跳过卖 Put: {reason}")
                # 记录跳过原因，识别哪个过滤器触发
                fname = "earnings" if "财报" in reason or "earnings" in reason.lower() else \
                        "ma_trend" if "MA" in reason or "trend" in reason.lower() else \
                        "vol" if "波动" in reason or "vol" in reason.lower() else "other"
                _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                              skip_reason=reason, filter_name=fname)
                return
            if reason:
                logger.info(f"过滤检查通过 | {reason}")

            result = self.select_put()
            if result:
                sym, mid, delta = result
                if mid <= 0:
                    logger.warning(f"权利金 mid={mid:.2f}，价格异常，跳过")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                                  skip_reason=f"premium_invalid={mid:.2f}")
                    return

                # 最低年化收益率门槛：20%
                # 年化 = (权利金 / 行权价) × (365 / DTE)
                # 低于门槛说明 IV 不足以补偿风险，资金留着等更好机会
                info = _parse_symbol(sym)
                strike = info["strike"] if info else 0
                dte_days = (info["expiry"] - date.today()).days if info and info.get("expiry") else 0
                if strike > 0 and dte_days > 0:
                    annualized = (mid / strike) * (365 / dte_days)
                    if annualized < settings.MIN_PREMIUM_ANNUALIZED:
                        logger.warning(
                            f"⏸️ 跳过卖 Put: 年化收益率 {annualized:.0%} < "
                            f"{settings.MIN_PREMIUM_ANNUALIZED:.0%} 门槛 "
                            f"(权利金 ${mid:.2f} / 行权价 ${strike} / DTE {dte_days})"
                        )
                        _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                                      skip_reason=f"low_premium annualized={annualized:.0%}",
                                      filter_name="min_premium")
                        return
                acct = self._trading.get_account()
                cash = float(acct.cash)
                bp = float(acct.buying_power)
                equity = float(acct.equity)
                # 计算账户所有 Short Put 的总抵押 + 按 sector 拆分（防 sector beta）
                sector_buckets = self._calc_put_collateral_by_sector()
                existing_collateral = sector_buckets.get("__total__", 0.0)
                from strategy.sector_map import sector_of
                this_sector = sector_of(self.symbol)
                same_sector = sector_buckets.get(this_sector, 0.0)
                contracts = kelly_contracts(
                    cash=cash, strike=strike, premium=mid,
                    buying_power=bp, equity=equity,
                    existing_put_collateral=existing_collateral,
                    same_sector_collateral=same_sector,
                )
                if contracts < 1:
                    logger.warning(f"⏸️ 资金不足，跳过开仓 (cash=${cash:,.0f} bp=${bp:,.0f} existing=${existing_collateral:,.0f})")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                                  skip_reason=f"insufficient_funds")
                    return
                # ★ 下单前硬检查（最后一道防线）
                bp_ok, bp_msg = check_buying_power_sufficient(
                    strike=strike, qty=contracts,
                    buying_power=bp, equity=equity,
                )
                if not bp_ok:
                    logger.error(f"🛑 BP 硬检查失败: {bp_msg} — 取消开仓")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                                  skip_reason=bp_msg)
                    return
                logger.info(f"✓ 资金检查通过: 开 {contracts} 张 | {bp_msg}")
                self._option_mgr.sell_to_open(sym, contracts, mid)
                # 记录开仓（delta 供周度校准使用）
                expiry_str = info["expiry"].isoformat() if info and info.get("expiry") else ""
                dte = (info["expiry"] - date.today()).days if info and info.get("expiry") else None
                _safe_journal("log_entry",
                              symbol=self.symbol, action="sell_put",
                              contract=sym, strike=strike, expiry=expiry_str,
                              qty=contracts, premium=mid, delta=delta, dte=dte,
                              filters_passed={"earnings": True, "ma_trend": True, "vol": True, "spy_market": True},
                              notes=f"size={contracts}")

        elif phase == WheelPhase.LONG_STOCK:
            cost_basis = float(obj.avg_entry_price)
            mark = float(obj.current_price)
            # Bug #2 fix: validate stock price before any drop decision.
            # Single ASK-skewed / bad print previously triggered a MARKET
            # dump of the whole position. Use a cross-checked mid; if data
            # is unreliable, skip the drop checks this cycle (hold).
            stock_price = self._get_stock_fair_price(mark)
            if stock_price is None:
                logger.info(
                    f"⏸ {self.symbol} LONG_STOCK: 股价数据不可靠, 跳过跌幅保护本轮 "
                    f"(等下个 tick)"
                )
                return
            drop_pct = (stock_price - cost_basis) / cost_basis

            # ── 跌幅保护 ──────────────────────────────────────────────────
            # 专家建议：股价深跌后停止卖 CC，避免锁死反弹空间。
            # 跌 35%+：限价卖出股票止损（标的出问题，不再等待）
            # 跌 20-35%：暂停 CC，持股等反弹，恢复后继续 Wheel
            if drop_pct <= -0.35:
                # Phase 4.1: escalating stop (limit → limit → market) +
                # halt-awareness. See _try_stock_stop_loss.
                self._try_stock_stop_loss(obj, stock_price, cost_basis, drop_pct)
                return
            else:
                # Recovered above -35% line → clear any escalation state.
                try:
                    from metrics import stop_state
                    if stop_state.get_attempts(self.symbol) > 0:
                        logger.info(f"{self.symbol} 回到 -35% 线上方, 重置止损计数")
                        stop_state.reset(self.symbol)
                except Exception:
                    pass

            if drop_pct <= -0.20:
                logger.warning(
                    f"⏸️ 暂停 CC: {self.symbol} 成本 ${cost_basis:.2f} → "
                    f"现价 ${stock_price:.2f} ({drop_pct:.1%})，跌幅 > 20%，"
                    f"等待股价反弹至成本附近再恢复卖 Call"
                )
                _safe_journal("log_skip", symbol=self.symbol, action="sell_call",
                              skip_reason=f"stock_below_cost_basis drop={drop_pct:.1%}",
                              filter_name="drop_protection")
                return

            # ── 正常流程：财报检查 → 卖 CC ────────────────────────────────
            # Covered Call 只过滤财报（必须继续卖以回收溢价）
            ok, reason = pre_open_call_checks(self.symbol)
            if not ok:
                logger.warning(f"⏸️  跳过卖 Call: {reason}")
                _safe_journal("log_skip", symbol=self.symbol, action="sell_call",
                              skip_reason=reason, filter_name="earnings")
                return
            result = self.select_call(cost_basis)
            if result:
                sym, mid, delta = result
                if mid <= 0:
                    logger.warning(f"权利金 mid={mid:.2f}，价格异常，跳过")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_call",
                                  skip_reason=f"premium_invalid={mid:.2f}")
                    return

                # 最低年化收益率门槛（同 Put）
                cc_info = _parse_symbol(sym)
                cc_strike = cc_info["strike"] if cc_info else 0
                cc_dte = (cc_info["expiry"] - date.today()).days if cc_info and cc_info.get("expiry") else 0
                if cc_strike > 0 and cc_dte > 0:
                    cc_annualized = (mid / cc_strike) * (365 / cc_dte)
                    if cc_annualized < settings.MIN_PREMIUM_ANNUALIZED:
                        logger.warning(
                            f"⏸️ 跳过卖 CC: 年化收益率 {cc_annualized:.0%} < "
                            f"{settings.MIN_PREMIUM_ANNUALIZED:.0%} 门槛 "
                            f"(权利金 ${mid:.2f} / 行权价 ${cc_strike} / DTE {cc_dte})"
                        )
                        _safe_journal("log_skip", symbol=self.symbol, action="sell_call",
                                      skip_reason=f"low_premium annualized={cc_annualized:.0%}",
                                      filter_name="min_premium")
                        return
                # CC 合约数严格由持股数决定（每 100 股支持 1 张 covered call）
                # 卖 CC 不占现金（股票即抵押），不存在爆仓/平仓风险
                contracts = int(float(obj.qty) // 100)
                if contracts < 1:
                    logger.warning(f"持股 {obj.qty} < 100，不够 1 张 CC")
                    return
                self._option_mgr.sell_to_open(sym, contracts, mid)
                # 记录开仓
                info = _parse_symbol(sym)
                strike = info["strike"] if info else 0
                expiry_str = info["expiry"].isoformat() if info and info.get("expiry") else ""
                dte = (info["expiry"] - date.today()).days if info and info.get("expiry") else None
                _safe_journal("log_entry",
                              symbol=self.symbol, action="sell_call",
                              delta=delta,
                              contract=sym, strike=strike, expiry=expiry_str,
                              qty=contracts, premium=mid, dte=dte,
                              filters_passed={"earnings": True},
                              notes=f"cost_basis={cost_basis:.2f}")

        elif phase in (WheelPhase.SHORT_PUT, WheelPhase.SHORT_CALL):
            # 1. STOP-LOSS first — if a position has blown out (price >= 3×
            #    entry), close it before checking take-profit. Tail-risk
            #    killer: caps single-trade loss at ~2× premium received.
            if self._try_stop_loss(obj):
                return
            # 2. TAKE-PROFIT — BTC at 50% profit, industry-standard wheel rule.
            #    Pass stock_price for v7 OTM%-based expiry override.
            #    Look up from already-fetched positions to avoid extra API call.
            stock_price = None
            try:
                all_pos = self._trading.get_all_positions()
                for p in all_pos:
                    if p.symbol == self.symbol:
                        stock_price = float(p.current_price)
                        break
                if stock_price is None:
                    # symbol not in positions (no stock held) — use market quote
                    from alpaca.data.historical import StockHistoricalDataClient
                    from alpaca.data.requests import StockLatestQuoteRequest
                    dc = StockHistoricalDataClient(
                        api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
                    )
                    q = dc.get_stock_latest_quote(
                        StockLatestQuoteRequest(symbol_or_symbols=self.symbol)
                    )
                    # Bug #5 fix: use mid, not raw ask. Raw ask is ASK-skewed
                    # (same class as the GM mark bug). Mid is symmetric.
                    qa = float(q[self.symbol].ask_price or 0)
                    qb = float(q[self.symbol].bid_price or 0)
                    if qa > 0 and qb > 0:
                        stock_price = (qa + qb) / 2
                    else:
                        stock_price = qa or qb or None
            except Exception as e:
                logger.debug(f"stock_price lookup failed, OTM% check degraded: {e}")
            if self._try_take_profit(obj, stock_price=stock_price):
                return
            logger.info("期权已开仓，持仓中，等待到期或行权...")
