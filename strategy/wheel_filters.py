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
    default_contracts: int = None,
    win_rate_est: float = 0.85,
    kelly_fraction: float = 0.25,
) -> int:
    """Kelly Criterion 动态仓位

    f* = p - q/b, 其中 b = premium/strike（单次收益/风险比）
    实际用 f*/4（分数 Kelly）降低破产风险
    回测证明：平均降低最大回撤 18pp
    """
    default_contracts = default_contracts or settings.WHEEL_CONTRACTS
    if premium <= 0 or strike <= 0 or cash <= 0:
        return default_contracts

    p = win_rate_est
    q = 1 - p
    b = premium / strike  # 每美元抵押能获得的权利金
    kelly = max(p - q / b, 0.0) * kelly_fraction

    allowed_cash = cash * kelly
    max_contracts = int(allowed_cash // (strike * 100))

    # 限制：至少 1，最多默认 × 2
    result = max(1, min(max_contracts, default_contracts * 2))
    # 现金足够性兜底
    max_by_cash = int(cash // (strike * 100))
    result = min(result, max_by_cash)
    return max(result, 0)


def pre_open_put_checks(symbol: str) -> tuple[bool, str]:
    """卖 Put（CSP）前的综合检查 — 组合所有回测验证有效的过滤器

    返回 (是否可开仓, 说明)
    """
    checks = [
        ("earnings", check_earnings(symbol)),
        ("ma_trend", check_ma_trend(symbol)),
        ("rv_cap", check_realized_vol(symbol)),
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
