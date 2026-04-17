"""Wheel 策略改进过滤器 — 基于回测验证有效的改进

回测胜场次（2020-2025 × 5 个标的）：
- MA50 trend filter:   4/5  ✅
- Kelly sizing:        2/5  平均降 MaxDD 18pp  ✅
- Earnings filter:     3/5  ✅
- Low IV skip:         3/5（对稳定股有效）

用法：
    from strategy.wheel_filters import pre_open_checks, kelly_contracts

    ok, reason = pre_open_checks("TSLA", earnings_db=EARNINGS)
    if not ok:
        logger.info(f"跳过开仓: {reason}")
        return
"""
from __future__ import annotations
from datetime import date, timedelta
from math import sqrt, log
from statistics import stdev, mean
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from config import settings


# TSLA/NVDA/MSFT/AAPL 近期财报日 — 按季度更新
EARNINGS_DB = {
    "TSLA": ["2025-04-22", "2025-07-23", "2025-10-22", "2026-01-28", "2026-04-21"],
    "NVDA": ["2025-05-28", "2025-08-27", "2025-11-19", "2026-02-25", "2026-05-27"],
    # MSFT Q3 FY2026 财报：2026-04-29 盘后（日期保险起见两天都列）
    "MSFT": ["2025-04-30", "2025-07-30", "2025-10-29", "2026-01-28",
             "2026-04-29", "2026-04-30", "2026-07-29", "2026-10-28"],
    "AAPL": ["2025-05-01", "2025-07-31", "2025-10-30", "2026-01-29", "2026-05-01"],
    "GOOGL": ["2025-04-24", "2025-07-24", "2025-10-28", "2026-01-27"],
    "META": ["2025-04-30", "2025-07-30", "2025-10-29", "2026-01-28"],
    "AMZN": ["2025-05-01", "2025-07-31", "2025-10-30", "2026-02-04"],
}


def _stk_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
    )


def check_earnings(symbol: str, dte_max: int = None) -> tuple[bool, str]:
    """财报窗口过滤：未来 dte_max 天内有财报则跳过"""
    dte_max = dte_max or settings.WHEEL_MAX_DTE
    earnings = EARNINGS_DB.get(symbol, [])
    today = date.today()
    horizon = today + timedelta(days=dte_max + 1)
    for e_str in earnings:
        e = date.fromisoformat(e_str)
        if today <= e <= horizon:
            return False, f"财报日 {e} 在 {dte_max}d 窗口内"
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


def kelly_contracts(
    cash: float, strike: float, premium: float,
    buying_power: float = None,
    equity: float = None,
    cash_buffer_pct: float = None,
    max_single_position_pct: float = None,
    existing_put_collateral: float = 0.0,
    max_total_exposure_pct: float = None,
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
        max_single_position_pct = getattr(settings, "MAX_SINGLE_POSITION_PCT", 0.70)
    if max_total_exposure_pct is None:
        max_total_exposure_pct = getattr(settings, "MAX_TOTAL_EXPOSURE_PCT", 0.90)
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

    # 取最严格的限制
    effective_available = min(available_bp, single_position_cap, remaining_exposure)

    if effective_available < per_contract_cash:
        logger.warning(
            f"🛑 资金不足开 1 张 (strike ${strike:,.2f}):"
            f"\n   单张需求: ${per_contract_cash:,.0f}"
            f"\n   层1 BP-缓冲: ${available_bp:,.0f}"
            f"\n   层2 单仓上限(权益{max_single_position_pct:.0%}): ${single_position_cap:,.0f}"
            f"\n   层3 总敞口剩余(权益{max_total_exposure_pct:.0%} - 现有持仓 ${existing_put_collateral:,.0f}): ${remaining_exposure:,.0f}"
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


def pre_open_put_checks(symbol: str) -> tuple[bool, str]:
    """卖 Put（CSP）前的综合检查 — 组合所有回测验证有效的过滤器

    返回 (是否可开仓, 说明)

    Order matters: SPY market filter first so we fail fast on systemic
    bear tape before spending quotas on per-symbol API calls.
    """
    checks = [
        ("spy_market", check_spy_trend()),      # new in v4: market-wide gate
        ("earnings",   check_earnings(symbol)),
        ("ma_trend",   check_ma_trend(symbol)),
        ("rv_cap",     check_realized_vol(symbol)),
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
