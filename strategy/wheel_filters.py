"""Wheel 策略改进过滤器 — 基于回测验证有效的改进

回测胜场次（2020-2025 × 5 个标的）：
- MA50 trend filter:   4/5  ✅
- Kelly sizing:        2/5  平均降 MaxDD 18pp  ✅
- Earnings filter:     3/5  ✅
- Low IV skip:         3/5（对稳定股有效）
- VIX regime gate:     v5  ✅ (panic-mode skip)
- IV rank gate:        v6  ✅ (premium-floor skip)

用法：
    from strategy.wheel_filters import pre_open_checks, kelly_contracts

    ok, reason = pre_open_checks("TSLA", earnings_db=EARNINGS)
    if not ok:
        logger.info(f"跳过开仓: {reason}")
        return
"""
from __future__ import annotations
import os
from datetime import date, timedelta
from math import sqrt, log
from statistics import stdev, mean
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from config import settings


# 财报日 DB — 按季度更新。Format: ISO date strings.
#
# Coverage: hand-maintained for the active wheel universe. When a symbol
# isn't in this dict, check_earnings() logs a WARN and falls open (so the
# bot doesn't lock up on first encounter with a new ticker), but a missing
# entry is the #1 cause of "we accidentally sold into earnings". Keep this
# fresh.
#
# To verify: https://finance.yahoo.com/calendar/earnings?symbol=XXX
EARNINGS_DB = {
    "TSLA": ["2025-04-22", "2025-07-23", "2025-10-22", "2026-01-28", "2026-04-21", "2026-07-22"],
    "NVDA": ["2025-05-28", "2025-08-27", "2025-11-19", "2026-02-25", "2026-05-27", "2026-08-26"],
    # MSFT Q3 FY2026 财报：2026-04-29 盘后
    "MSFT": ["2025-04-30", "2025-07-30", "2025-10-29", "2026-01-28",
             "2026-04-29", "2026-04-30", "2026-07-29", "2026-10-28"],
    "AAPL": ["2025-05-01", "2025-07-31", "2025-10-30", "2026-01-29", "2026-05-01", "2026-07-30"],
    "GOOGL": ["2025-04-24", "2025-07-24", "2025-10-28", "2026-01-27", "2026-04-23", "2026-07-23"],
    "META":  ["2025-04-30", "2025-07-30", "2025-10-29", "2026-01-28", "2026-04-29", "2026-07-29"],
    "AMZN":  ["2025-05-01", "2025-07-31", "2025-10-30", "2026-02-04", "2026-04-30", "2026-07-30"],
    # Semis
    "AVGO": ["2025-03-06", "2025-06-05", "2025-09-04", "2025-12-11", "2026-03-05", "2026-06-04"],
    "AMD":  ["2025-05-06", "2025-08-05", "2025-11-04", "2026-02-03", "2026-05-05", "2026-08-04"],
    "QCOM": ["2025-05-01", "2025-07-30", "2025-11-05", "2026-02-04", "2026-04-30", "2026-07-29"],
    "TSM":  ["2025-04-17", "2025-07-17", "2025-10-16", "2026-01-15", "2026-04-16", "2026-07-16"],
    "MU":   ["2025-03-20", "2025-06-25", "2025-09-23", "2025-12-18", "2026-03-19", "2026-06-24"],
    "AMAT": ["2025-05-15", "2025-08-14", "2025-11-13", "2026-02-12", "2026-05-14", "2026-08-13"],
    # Healthcare / defensive
    "UNH":  ["2025-04-17", "2025-07-15", "2025-10-14", "2026-01-22", "2026-04-16", "2026-07-15"],
    "JNJ":  ["2025-04-15", "2025-07-15", "2025-10-14", "2026-01-23", "2026-04-14", "2026-07-14"],
    "COST": ["2025-03-06", "2025-05-29", "2025-09-25", "2025-12-11", "2026-03-05", "2026-05-28"],
    # PLTR
    "PLTR": ["2025-05-05", "2025-08-04", "2025-11-03", "2026-02-02", "2026-05-04", "2026-08-03"],
    # === Memory / storage (added 5/8 with universe expansion) ===
    # Estimates based on standard reporting calendar — VERIFY QUARTERLY.
    "WDC":  ["2025-04-30", "2025-07-30", "2025-10-29", "2026-01-28", "2026-04-29", "2026-07-29"],
    "STX":  ["2025-04-22", "2025-07-22", "2025-10-22", "2026-01-21", "2026-04-22", "2026-07-22"],
    "SNDK": ["2025-04-30", "2025-07-30", "2025-10-29", "2026-01-28", "2026-04-29", "2026-07-29"],
    # === Semi equipment (added 5/8) ===
    "LRCX": ["2025-01-29", "2025-04-23", "2025-07-23", "2025-10-22", "2026-01-28", "2026-04-22", "2026-07-22"],
    "TER":  ["2025-01-29", "2025-04-23", "2025-07-23", "2025-10-22", "2026-01-28", "2026-04-22", "2026-07-22"],
    "ASML": ["2025-01-29", "2025-04-16", "2025-07-16", "2025-10-15", "2026-01-28", "2026-04-15", "2026-07-15"],
    "KLAC": ["2025-01-29", "2025-04-30", "2025-07-30", "2025-10-29", "2026-01-28", "2026-04-29", "2026-07-29"],
    # === Analog / IoT / embedded (added 5/8) ===
    "NXPI": ["2025-02-04", "2025-04-28", "2025-07-21", "2025-10-27", "2026-02-03", "2026-04-27", "2026-07-20"],
    "ADI":  ["2025-02-19", "2025-05-21", "2025-08-20", "2025-11-19", "2026-02-18", "2026-05-20", "2026-08-19"],
    "MCHP": ["2025-02-06", "2025-05-08", "2025-08-07", "2025-11-06", "2026-02-05", "2026-05-07", "2026-08-06"],
    "ON":   ["2025-02-03", "2025-05-05", "2025-08-04", "2025-11-03", "2026-02-02", "2026-05-04", "2026-08-03"],
    # === Networking / interconnect (added 5/8) ===
    "CIEN": ["2025-03-04", "2025-06-04", "2025-09-03", "2025-12-04", "2026-03-03", "2026-06-03", "2026-09-02"],
    "FN":   ["2025-02-03", "2025-05-05", "2025-08-18", "2025-11-03", "2026-02-02", "2026-05-04", "2026-08-17"],
    "ANET": ["2025-02-18", "2025-05-06", "2025-08-05", "2025-11-04", "2026-02-17", "2026-05-05", "2026-08-04"],
    "CSCO": ["2025-02-12", "2025-05-14", "2025-08-13", "2025-11-12", "2026-02-11", "2026-05-13", "2026-08-12"],
    # === AI vision (added 5/8) ===
    "AMBA": ["2025-02-25", "2025-05-27", "2025-08-26", "2025-12-02", "2026-02-24", "2026-05-26", "2026-08-25"],
    # === Software / streaming (added 5/8 — coverage gap from original) ===
    "NFLX": ["2025-01-21", "2025-04-17", "2025-07-17", "2025-10-21", "2026-01-20", "2026-04-16", "2026-07-16"],
    "ORCL": ["2025-03-10", "2025-06-11", "2025-09-09", "2025-12-09", "2026-03-09", "2026-06-10", "2026-09-08"],
    "CRM":  ["2025-02-26", "2025-05-28", "2025-08-27", "2025-12-03", "2026-02-25", "2026-05-27", "2026-08-26"],
    "ADBE": ["2025-03-12", "2025-06-12", "2025-09-11", "2025-12-10", "2026-03-11", "2026-06-11", "2026-09-10"],
    # === CPU (added 5/8 — coverage gap) ===
    "INTC": ["2025-01-30", "2025-04-24", "2025-07-24", "2025-10-23", "2026-01-29", "2026-04-23", "2026-07-23"],
    "ARM":  ["2025-02-05", "2025-05-07", "2025-07-30", "2025-11-05", "2026-02-04", "2026-05-06", "2026-07-29"],
    # === Photonics (added 5/8 — coverage gap) ===
    "MRVL": ["2025-03-05", "2025-05-29", "2025-08-28", "2025-12-03", "2026-03-04", "2026-05-28", "2026-08-27"],
    "LITE": ["2025-02-04", "2025-05-06", "2025-08-12", "2025-11-04", "2026-02-03", "2026-05-05", "2026-08-11"],
    "COHR": ["2025-02-04", "2025-05-06", "2025-08-12", "2025-11-04", "2026-02-03", "2026-05-05", "2026-08-11"],
    "IPGP": ["2025-02-11", "2025-04-29", "2025-07-29", "2025-11-04", "2026-02-10", "2026-04-28", "2026-07-28"],
    # === AI servers / industrial (added 5/8 — coverage gap) ===
    "SMCI": ["2025-02-04", "2025-05-06", "2025-08-05", "2025-11-04", "2026-02-03", "2026-05-05", "2026-08-04"],
    "XYL":  ["2025-02-04", "2025-04-29", "2025-07-29", "2025-10-28", "2026-02-03", "2026-04-28", "2026-07-28"],
    # === ETFs (no earnings — explicit empty list to suppress NO_DATA WARN) ===
    "DRAM": [],
    "SPY":  [],
    "QQQ":  [],
}


def _earnings_db_is_stale() -> bool:
    """Detect when EARNINGS_DB has no future dates anywhere — a sign that
    nobody's updated the file in months. Logs a WARN; doesn't block trading.
    """
    today = date.today()
    for sym, dates in EARNINGS_DB.items():
        if any(date.fromisoformat(d) >= today for d in dates):
            return False
    return True


# Run once on import — alerts the user if the DB has gone fully stale.
if _earnings_db_is_stale():
    logger.warning(
        "⚠️ EARNINGS_DB has zero future dates across all symbols — "
        "the DB is stale. Update strategy/wheel_filters.py:EARNINGS_DB."
    )


def _stk_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
    )


def _fetch_finnhub_earnings(symbol: str) -> list[date]:
    """Optional Finnhub fallback when EARNINGS_DB is missing a symbol.

    Activated only if FINNHUB_API_KEY is set in the environment. Free tier
    gives 60 calls/min which is plenty for this use case. Caches via
    function-level dict so each cycle hits the API at most once per symbol.
    """
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key:
        return []
    if symbol in _fetch_finnhub_earnings._cache:
        return _fetch_finnhub_earnings._cache[symbol]
    try:
        import requests
        today = date.today()
        end = today + timedelta(days=120)
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "from": today.isoformat(),
                "to": end.isoformat(),
                "symbol": symbol,
                "token": api_key,
            },
            timeout=5,
        )
        r.raise_for_status()
        data = r.json().get("earningsCalendar", [])
        dates = [date.fromisoformat(e["date"]) for e in data if "date" in e]
        _fetch_finnhub_earnings._cache[symbol] = dates
        if dates:
            logger.info(f"📅 Finnhub: {symbol} earnings → {[d.isoformat() for d in dates]}")
        return dates
    except Exception as e:
        logger.warning(f"Finnhub earnings lookup failed for {symbol}: {e}")
        _fetch_finnhub_earnings._cache[symbol] = []
        return []
_fetch_finnhub_earnings._cache = {}


def check_earnings(symbol: str, dte_max: int = None) -> tuple[bool, str]:
    """财报窗口过滤：未来 dte_max 天内有财报则跳过

    Sources, in order:
      1. EARNINGS_DB (hand-maintained, fast, no network)
      2. Finnhub API (only if FINNHUB_API_KEY is set, falls back silently)

    When a symbol has no source at all, logs WARN and fails open. Better to
    sometimes accidentally trade through earnings than to lock the bot out
    of an entire ticker because nobody filled in the dates.
    """
    dte_max = dte_max or settings.WHEEL_MAX_DTE
    today = date.today()
    horizon = today + timedelta(days=dte_max + 1)

    # 1. Static DB
    in_db = symbol in EARNINGS_DB
    db_dates = [date.fromisoformat(d) for d in EARNINGS_DB.get(symbol, [])]

    # 2. Finnhub fallback — only if key is set
    api_dates = _fetch_finnhub_earnings(symbol)

    all_dates = sorted(set(db_dates) | set(api_dates))

    # No coverage at all → fall through to "no earnings risk".
    # If symbol has explicit [] in EARNINGS_DB (e.g. ETFs like SPY/QQQ
    # which have no single earnings event), no WARN. Only WARN when the
    # symbol isn't acknowledged in the DB at all.
    if not all_dates:
        if not in_db:
            logger.warning(
                f"⚠️ {symbol} not in EARNINGS_DB and no Finnhub data — "
                f"earnings risk unchecked. Add to wheel_filters.py:EARNINGS_DB "
                f"or set FINNHUB_API_KEY."
            )
            return True, "earnings:NO_DATA"
        # Explicit [] = ETF or no-earnings symbol, silently pass
        return True, "earnings:N/A (ETF or no earnings)"

    # Window check
    for e in all_dates:
        if today <= e <= horizon:
            return False, f"财报日 {e} 在 {dte_max}d 窗口内"
    # Find next earnings for the message
    future = [e for e in all_dates if e >= today]
    if future:
        days_to = (future[0] - today).days
        return True, f"earnings T+{days_to}d ({future[0]})"
    return True, ""


def check_ma_trend(symbol: str, window: int = 50) -> tuple[bool, str]:
    """MA 趋势过滤：股价 < 50日均线时不卖 CSP（避免下跌趋势接飞刀）"""
    try:
        client = _stk_client()
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=window * 2 + 10),
        ))[symbol]
        closes = [float(b.close) for b in bars][-window:]
        if len(closes) < window // 2:
            return True, ""   # 数据不足，放行
        ma = mean(closes)
        latest = closes[-1]
        if latest < ma:
            return False, f"股价 ${latest:.2f} < MA{window} ${ma:.2f}（下跌趋势）"
        return True, f"股价 ${latest:.2f} > MA{window} ${ma:.2f}"
    except Exception as e:
        logger.warning(f"MA 检查失败: {e}")
        return True, ""   # 故障安全：出错时放行


def check_realized_vol(symbol: str, days: int = 20, max_iv: float = 0.90) -> tuple[bool, str]:
    """极端波动过滤：年化实现波动率 > max_iv 时暂停（保护大跌大涨）"""
    try:
        client = _stk_client()
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=days * 2 + 5),
        ))[symbol]
        closes = [float(b.close) for b in bars][-days:]
        if len(closes) < 5:
            return True, ""
        rets = [log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        rv = stdev(rets) * sqrt(252)
        if rv > max_iv:
            return False, f"实现波动率 {rv:.0%} > {max_iv:.0%}（暂停）"
        return True, f"RV={rv:.0%}"
    except Exception as e:
        logger.warning(f"RV 检查失败: {e}")
        return True, ""


def compute_realized_vol(symbol: str, days: int = 20) -> Optional[float]:
    """返回年化实现波动率（浮点数），失败返回 None。

    与 check_realized_vol 使用相同计算逻辑，但直接返回 float 供调用方比较，
    而不是 (bool, str) 元组。

    返回值单位：年化（例如 0.60 = 60% 年化 ≈ 日波动 3.8%）
    """
    try:
        client = _stk_client()
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=days * 2 + 5),
        ))[symbol]
        closes = [float(b.close) for b in bars][-days:]
        if len(closes) < 5:
            return None
        rets = [log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        return stdev(rets) * sqrt(252)   # annualized
    except Exception:
        return None


def kelly_contracts(
    cash: float, strike: float, premium: float,
    buying_power: float = None,
    equity: float = None,
    cash_buffer_pct: float = None,
    max_single_position_pct: float = None,
    existing_put_collateral: float = 0.0,
    max_total_exposure_pct: float = None,
    same_sector_collateral: float = 0.0,
    max_sector_exposure_pct: float = None,
) -> int:
    """Resource-driven max safe contracts for a cash-secured put.

    Name kept for back-compat only — the Kelly formula that once lived here
    was dead code for 3-5 DTE puts (b = premium/strike ≈ 0.005~0.02 << q/p
    ≈ 0.176, always clamped to 0). Current logic uses 4 layers of capital
    protection:

      L1 BP − cash buffer:     don't deploy more than (buying_power − equity × CASH_BUFFER_PCT)
      L2 single-position cap:  one position ≤ equity × MAX_SINGLE_POSITION_PCT
      L3 total-exposure cap:   all short puts combined ≤ equity × MAX_TOTAL_EXOSURE_PCT
      L4 worst-case assertion: existing_collateral + new_collateral + buffer ≤ cash

    Returns 0 when no safe trade is possible.
    """
    if strike <= 0 or cash <= 0:
        logger.warning(f"仓位计算: 输入无效 cash={cash} strike={strike}")
        return 0

    if buying_power is None:
        buying_power = cash
        logger.warning("kelly_contracts 未传 buying_power，使用 cash — 风险：可能高估")

    # 配置读取
    if cash_buffer_pct is None:
        cash_buffer_pct = getattr(settings, "CASH_BUFFER_PCT", 0.10)
    if max_single_position_pct is None:
        max_single_position_pct = getattr(settings, "MAX_SINGLE_POSITION_PCT", 0.40)
    if max_total_exposure_pct is None:
        max_total_exposure_pct = getattr(settings, "MAX_TOTAL_EXPOSURE_PCT", 0.90)
    if max_sector_exposure_pct is None:
        max_sector_exposure_pct = getattr(settings, "MAX_SECTOR_EXPOSURE_PCT", 0.60)
    if equity is None:
        equity = max(cash, buying_power)

    per_contract_cash = strike * 100
    safety_buffer = equity * cash_buffer_pct

    # ── 层 1：BP − 缓冲 ──
    available_bp = buying_power - safety_buffer

    # ── 层 2：单仓上限 ──
    single_position_cap = equity * max_single_position_pct

    # ── 层 3：总敞口剩余空间（扣现有持仓） ──
    total_exposure_cap = equity * max_total_exposure_pct
    remaining_exposure = total_exposure_cap - existing_put_collateral

    # ── 层 5（新）：sector 剩余空间 ──
    sector_cap = equity * max_sector_exposure_pct
    remaining_sector = sector_cap - same_sector_collateral

    # 取最严格的限制
    effective_available = min(available_bp, single_position_cap,
                               remaining_exposure, remaining_sector)

    if effective_available < per_contract_cash:
        logger.warning(
            f"🛑 资金不足开 1 张 (strike ${strike:,.2f}):"
            f"\n   单张需求: ${per_contract_cash:,.0f}"
            f"\n   层1 BP-缓冲: ${available_bp:,.0f}"
            f"\n   层2 单仓上限(权益{max_single_position_pct:.0%}): ${single_position_cap:,.0f}"
            f"\n   层3 总敞口剩余(权益{max_total_exposure_pct:.0%} - 现有持仓 ${existing_put_collateral:,.0f}): ${remaining_exposure:,.0f}"
            f"\n   层5 sector 剩余(权益{max_sector_exposure_pct:.0%} - 同 sector 持仓 ${same_sector_collateral:,.0f}): ${remaining_sector:,.0f}"
            f"\n   → 实际可用: ${effective_available:,.0f}"
        )
        return 0

    # ── 层 4：最坏情况硬检查 ──
    # 假设开 N 张 + 现有仓位都被行权，现金必须能扛住
    max_by_effective = int(effective_available // per_contract_cash)
    for trial in range(max_by_effective, 0, -1):
        worst_case_cash_need = existing_put_collateral + (per_contract_cash * trial)
        if worst_case_cash_need + safety_buffer <= cash:
            logger.info(
                f"✓ 动态仓位: {trial} 张 (strike ${strike:,.2f})"
                f"\n   权益: ${equity:,.0f} | BP: ${buying_power:,.0f} | 现金: ${cash:,.0f}"
                f"\n   现有抵押: ${existing_put_collateral:,.0f} + 新仓 ${per_contract_cash * trial:,.0f}"
                f" = 最坏情况 ${worst_case_cash_need:,.0f}"
                f"\n   缓冲后可用: ${cash - safety_buffer:,.0f} ≥ 最坏情况 ✓"
            )
            return trial

    logger.warning(f"🛑 最坏情况硬检查：任何张数都会超限")
    return 0


def check_buying_power_sufficient(
    strike: float, qty: int, buying_power: float,
    equity: float = None, cash_buffer_pct: float = None,
) -> tuple[bool, str]:
    """硬检查：下单前最后一道防线，确保 BP 足够承担本次新开仓抵押 + 保留缓冲。

    与 kelly_contracts 分离 — 即使 Kelly 算出合约数，这里再检查一次。
    """
    if cash_buffer_pct is None:
        cash_buffer_pct = getattr(settings, "CASH_BUFFER_PCT", 0.10)
    if equity is None:
        equity = buying_power
    safety_buffer = equity * cash_buffer_pct
    required = strike * 100 * qty
    available = buying_power - safety_buffer

    if required > available:
        return False, (
            f"购买力不足: 需抵押 ${required:,.0f} > 可用 ${available:,.0f} "
            f"(BP ${buying_power:,.0f} − {cash_buffer_pct:.0%} 缓冲 ${safety_buffer:,.0f})"
        )
    return True, f"BP OK: 需 ${required:,.0f}, 可用 ${available:,.0f}"


def check_spy_trend(window: int = 200) -> tuple[bool, str]:
    """Market-wide systemic-risk filter: block new CSPs when SPY is below
    its 200-day MA. A broken 200-MA historically precedes every meaningful
    bear phase since 2000; selling puts into that tape is a known way to
    get multiple symbols assigned simultaneously.

    Returns (ok_to_sell, message). Fail-open on API errors.
    """
    try:
        client = _stk_client()
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=int(window * 1.6)),
        ))["SPY"]
        closes = [float(b.close) for b in bars][-window:]
        if len(closes) < window // 2:
            return True, ""    # not enough history, let it pass
        ma = mean(closes)
        spot = closes[-1]
        if spot < ma:
            return False, f"SPY ${spot:.2f} < MA{window} ${ma:.2f} (市场破位)"
        return True, f"SPY=${spot:.2f} > MA{window}=${ma:.2f}"
    except Exception as e:
        logger.warning(f"SPY trend 检查失败: {e}")
        return True, ""


def check_vix_regime(rv_threshold: float = 0.25) -> tuple[bool, str]:
    """Tail-risk gate: skip new CSPs when the broader market is in panic mode.

    Alpaca's data feed doesn't carry the VIX index, so we use SPY's 30-day
    annualized realized volatility as a robust proxy. Historically SPY 30d
    RV tracks VIX with a small offset (VIX ≈ RV plus ~3-5pp IV premium).
    When SPY 30d RV crosses 25%, that maps to VIX in the 28-30 zone — the
    historical "elevated panic" threshold (2008/2020/2022 all started here
    or higher).

    Why this matters: in panic regimes correlations spike to 1, and selling
    puts on multiple symbols means simultaneous ITM, simultaneous assignment,
    no escape. Sitting out for a week of VIX > 25 has historically saved
    much more than the missed premium.

    Returns (ok_to_sell, message). Fail-open on API errors so a data hiccup
    doesn't freeze the whole bot.
    """
    try:
        client = _stk_client()
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=50),
        ))["SPY"]
        closes = [float(b.close) for b in bars][-31:]
        if len(closes) < 21:
            return True, ""    # not enough history, fail open
        # Daily log returns → annualized stdev (sqrt(252) trading days)
        returns = [log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        annual_vol = stdev(returns) * sqrt(252)
        if annual_vol > rv_threshold:
            return False, (f"SPY 30d RV {annual_vol:.0%} > {rv_threshold:.0%} "
                           f"(panic regime, ≈ VIX > 28)")
        return True, f"SPY 30d RV {annual_vol:.0%} (calm)"
    except Exception as e:
        logger.warning(f"VIX regime check failed: {e}")
        return True, ""


def compute_iv_rank(symbol: str, lookback: int = 252, window: int = 20) -> Optional[float]:
    """Compute IV rank as RV percentile. Returns float 0-100, or None on error.

    Shared helper so check_iv_rank (binary gate) and select_put (dynamic
    delta target) can use the same number without recomputing.
    """
    try:
        client = _stk_client()
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=int(lookback * 1.5) + window),
        ))[symbol]
        closes = [float(b.close) for b in bars]
        if len(closes) < window + 30:
            return None
        rets = [log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        rv_series = []
        for i in range(window, len(rets) + 1):
            rv = stdev(rets[i-window:i]) * sqrt(252)
            rv_series.append(rv)
        if len(rv_series) < 30:
            return None
        rv_window = rv_series[-lookback:]
        current_rv = rv_series[-1]
        rank = sum(1 for x in rv_window if x <= current_rv) / len(rv_window) * 100
        return rank
    except Exception as e:
        logger.warning(f"compute_iv_rank failed for {symbol}: {e}")
        return None


def target_delta_for_iv_rank(rank: Optional[float]) -> float:
    """Adaptive delta-target based on IV rank.

    Standard wheel uses fixed delta 0.25. But the same delta means very
    different things at different IV regimes:
      - High IV (rank ≥ 70): premium is fat, can afford further-OTM
        strikes — lower delta = lower assignment probability
      - Low IV (rank < 30): premium thin, need closer-to-ATM to make
        the trade worthwhile (but check_iv_rank already blocks < 30)

    Returns the delta absolute value to target. Caller flips sign for puts.
    Falls back to settings.WHEEL_TARGET_DELTA when rank is unknown.
    """
    if rank is None:
        return settings.WHEEL_TARGET_DELTA
    if rank >= 70:
        return 0.15      # extreme premium — go further OTM
    if rank >= 50:
        return 0.20      # elevated premium — slight pullback from ATM
    return settings.WHEEL_TARGET_DELTA   # 30-50 = standard


def check_iv_rank(symbol: str, lookback: int = 252, window: int = 20,
                  min_rank: float = 30) -> tuple[bool, str]:
    """Premium-floor gate: skip CSPs when current IV rank is too low.

    Uses compute_iv_rank() under the hood. Threshold 30 = current RV is
    in top 70% of past year. Below 30 means thin premium relative to
    historical base — same gamma exposure, smaller paycheck.

    Returns (ok_to_sell, message). Fail-open on errors.
    """
    rank = compute_iv_rank(symbol, lookback=lookback, window=window)
    if rank is None:
        return True, ""    # not enough data / API error — fail open
    if rank < min_rank:
        return False, f"IV rank {rank:.0f} < {min_rank:.0f} (premium too thin)"
    return True, f"IV rank {rank:.0f}"


def pre_open_put_checks(symbol: str) -> tuple[bool, str]:
    """卖 Put（CSP）前的综合检查 — 组合所有回测验证有效的过滤器

    返回 (是否可开仓, 说明)

    Order matters: market-wide gates first (cheapest, fail-fast) so we
    don't waste per-symbol API quota when the whole market is unsafe.
    """
    checks = [
        ("vix_regime", check_vix_regime()),     # v5: market-wide panic gate
        ("spy_market", check_spy_trend()),      # market-wide trend gate
        ("earnings",   check_earnings(symbol)),
        ("ma_trend",   check_ma_trend(symbol)),
        ("rv_cap",     check_realized_vol(symbol)),
        ("iv_rank",    check_iv_rank(symbol)),  # v6: premium-floor gate
    ]
    reasons = []
    for name, (ok, msg) in checks:
        if not ok:
            return False, f"[{name}] {msg}"
        if msg:
            reasons.append(f"{name}:{msg}")
    return True, " | ".join(reasons)


def pre_open_call_checks(symbol: str) -> tuple[bool, str]:
    """卖 Call（CC）前的检查 — 只过滤财报，MA 不过滤（已被行权持股，必须继续卖 call）"""
    ok, msg = check_earnings(symbol)
    if not ok:
        return False, f"[earnings] {msg}"
    return True, ""
