"""Wheel/CSP 候选股票扫描器

功能：
1. scan_wheel_candidates() — 扫描支持 3-day 期权的标的，
   找出比 TSLA 更适合做 Wheel 的 TOP 2
2. scan_csp_candidates() — 扫描 1-month 现金担保 Put 最佳标的 TOP 3

评分维度：
- 权利金年化收益率
- 隐含波动率（IV 甜蜜区间 30-70%）
- 近期稳定性（10日最大单日跌幅）
- 购买力适配
"""
from __future__ import annotations

import re
import math
from datetime import date, timedelta
from math import sqrt
from statistics import stdev
from typing import Optional

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame

from config import settings
from core.alpaca_client import AlpacaClients

# 高流动性 + 支持 3-day 期权的标的（每周多次到期）
WHEEL_UNIVERSE = [
    "TSLA", "NVDA", "AAPL", "MSFT", "AMD", "META", "AMZN", "GOOGL",
    "SPY", "QQQ", "IWM", "NFLX", "AVGO", "COIN",
]

# CSP 候选（包括稳健品种）
CSP_UNIVERSE = [
    "TSLA", "NVDA", "AAPL", "MSFT", "AMD", "META", "AMZN", "GOOGL",
    "SPY", "QQQ", "NFLX", "AVGO", "COIN",
    # 稳健品种
    "JPM", "BAC", "V", "MA", "KO", "PG", "WMT", "HD", "DIS",
    "UNH", "JNJ", "LLY", "PFE", "XOM", "CVX",
]


def _get_stock_metrics(sym: str, stk_data) -> Optional[dict]:
    """获取股票近期波动指标"""
    try:
        trade = stk_data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym))
        price = float(trade[sym].price)

        bars = stk_data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=20),
        ))[sym]
        closes = [float(b.close) for b in bars[-10:]]
        if len(closes) < 3:
            return None
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        realized_vol = stdev(returns) * sqrt(252) if len(returns) > 1 else 0
        max_drop = min(returns) if returns else 0
        return {"price": price, "realized_vol": realized_vol, "max_drop": max_drop}
    except Exception:
        return None


def _find_best_put(sym: str, opt_data, dte_min: int, dte_max: int, target_delta: float = -0.25) -> Optional[dict]:
    """找某股票 DTE 范围内 Delta 最接近 target 的 Put"""
    today = date.today()
    try:
        chain = opt_data.get_option_chain(OptionChainRequest(
            underlying_symbol=sym,
            expiration_date_gte=today + timedelta(days=dte_min),
            expiration_date_lte=today + timedelta(days=dte_max),
            type="put",
        ))
    except Exception:
        return None

    best, best_diff = None, float("inf")
    for s, snap in chain.items():
        if not snap.greeks or snap.greeks.delta is None:
            continue
        if not snap.latest_quote:
            continue
        bid = float(snap.latest_quote.bid_price or 0)
        ask = float(snap.latest_quote.ask_price or 0)
        if bid <= 0 or ask <= 0:
            continue
        diff = abs(snap.greeks.delta - target_delta)
        if diff < best_diff:
            best_diff = diff
            best = (s, snap, bid, ask)

    if not best:
        return None

    s, snap, bid, ask = best
    m = re.match(rf"^{sym}(\d{{6}})P(\d{{8}})$", s)
    if not m:
        return None
    date_str, strike_str = m.groups()
    expiry = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:]))
    strike = int(strike_str) / 1000.0
    mid = round((bid + ask) / 2, 2)
    dte = (expiry - today).days

    return {
        "symbol": s,
        "strike": strike,
        "expiry": expiry,
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "delta": snap.greeks.delta,
        "theta": snap.greeks.theta,
        "iv": snap.implied_volatility or 0,
    }


def _score_candidate(stock: dict, put: dict, cash: float, contracts: int = 2) -> dict:
    """综合评分 (0-100)"""
    price = stock["price"]
    iv = put["iv"]
    mid = put["mid"]
    strike = put["strike"]
    dte = put["dte"]
    max_drop = stock["max_drop"]

    # 年化收益率
    yield_pct = mid / strike if strike > 0 else 0
    annualized = yield_pct * (365 / max(dte, 1)) if dte > 0 else 0

    # 评分
    # 1. 收益率分 (目标 20%-150% 年化)
    yield_score = min(100, max(0, (annualized - 0.10) / 0.40 * 100))

    # 2. IV 甜蜜区间 (30%-70%)
    if iv < 0.20:
        iv_score = iv / 0.20 * 50
    elif iv > 1.00:
        iv_score = max(0, 50 - (iv - 1.00) * 100)
    elif 0.30 <= iv <= 0.70:
        iv_score = 100
    elif iv < 0.30:
        iv_score = 50 + (iv - 0.20) / 0.10 * 50
    else:  # 0.70 < iv <= 1.00
        iv_score = 100 - (iv - 0.70) / 0.30 * 50

    # 3. 稳定性 (max_drop 好于 -5% 满分)
    stability_score = max(0, min(100, 100 + max_drop * 1000))  # max_drop=-10% → 0, -5% → 50, 0% → 100

    # 4. 购买力适配
    required = strike * 100 * contracts
    affordable = cash >= required
    affordability_score = 100 if affordable else 0

    # 加权综合分
    total = (
        yield_score * 0.35 +
        iv_score * 0.25 +
        stability_score * 0.20 +
        affordability_score * 0.20
    )

    return {
        "total_score": round(total, 1),
        "yield_score": round(yield_score, 1),
        "iv_score": round(iv_score, 1),
        "stability_score": round(stability_score, 1),
        "affordability_score": affordability_score,
        "annualized_yield": annualized,
        "required_cash": required,
        "affordable": affordable,
        "premium_total": mid * 100 * contracts,
    }


def scan_candidates(universe: list[str], dte_min: int, dte_max: int, cash: float, contracts: int = 2) -> list[dict]:
    """扫描候选股票"""
    stk_data = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    opt_data = OptionHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)

    results = []
    for sym in universe:
        stock = _get_stock_metrics(sym, stk_data)
        if not stock:
            continue
        put = _find_best_put(sym, opt_data, dte_min, dte_max, target_delta=-0.25)
        if not put:
            continue
        score = _score_candidate(stock, put, cash, contracts)
        results.append({
            "symbol": sym,
            "stock": stock,
            "put": put,
            "score": score,
        })

    results.sort(key=lambda r: r["score"]["total_score"], reverse=True)
    return results


def scan_wheel_alternatives(exclude: str = "TSLA", top_n: int = 2) -> list[dict]:
    """扫描比 exclude（通常是 TSLA）更好的 3-day Wheel 候选"""
    trading = AlpacaClients.trading()
    cash = float(trading.get_account().cash)

    # 先评估当前标的，得到基准分数
    current = scan_candidates([exclude], dte_min=1, dte_max=5, cash=cash, contracts=2)
    baseline_score = current[0]["score"]["total_score"] if current else 0

    # 扫描全部（不排除 TSLA 以便对比）
    universe = [s for s in WHEEL_UNIVERSE if s != exclude]
    all_results = scan_candidates(universe, dte_min=1, dte_max=5, cash=cash, contracts=2)

    # 只保留比 baseline 分更高的
    better = [r for r in all_results if r["score"]["total_score"] > baseline_score]
    return {
        "baseline": {"symbol": exclude, "score": baseline_score},
        "better": better[:top_n],
        "all": all_results[:5],  # 额外给 top 5 以防没有 better
    }


def scan_csp_candidates(top_n: int = 3) -> list[dict]:
    """扫描 1-month 现金担保 Put TOP N"""
    trading = AlpacaClients.trading()
    cash = float(trading.get_account().cash)
    results = scan_candidates(CSP_UNIVERSE, dte_min=20, dte_max=40, cash=cash, contracts=1)
    return results[:top_n]


def format_wheel_alternatives_md(data: dict) -> str:
    """格式化 Wheel 替代方案报告"""
    lines = []
    lines.append("## Wheel Alternatives (3-day DTE, better than TSLA)")
    lines.append("")

    baseline = data["baseline"]
    better = data["better"]

    lines.append(f"**TSLA baseline score: {baseline['score']}**")
    lines.append("")

    if not better:
        lines.append("No alternatives scored higher than TSLA today. Top 3 by score:")
        show = data["all"][:3]
    else:
        lines.append(f"Top {len(better)} alternatives scoring above TSLA:")
        show = better

    if show:
        lines.append("")
        lines.append("| Symbol | Price | Strike | DTE | Premium x2 | Annual Yield | IV | Δ | Score |")
        lines.append("|--------|-------|--------|-----|-----------|--------------|-----|---|-------|")
        for r in show:
            s = r["score"]
            p = r["put"]
            st = r["stock"]
            lines.append(
                f"| **{r['symbol']}** | ${st['price']:.2f} | ${p['strike']:.0f} | {p['dte']}d | "
                f"${s['premium_total']:.0f} | {s['annualized_yield']:.0%} | {p['iv']:.0%} | "
                f"{p['delta']:+.2f} | **{s['total_score']}** |"
            )
        lines.append("")

    return "\n".join(lines)


def format_csp_md(candidates: list[dict]) -> str:
    """格式化 CSP 推荐列表"""
    lines = []
    lines.append("## Top Cash-Secured Put Candidates (1-month DTE)")
    lines.append("")

    if not candidates:
        lines.append("_No qualifying candidates found._")
        return "\n".join(lines)

    lines.append("| Symbol | Price | Strike | Expiry | DTE | Premium | Annual Yield | IV | Δ | Cash Req | Score |")
    lines.append("|--------|-------|--------|--------|-----|---------|--------------|-----|---|---------|-------|")
    for r in candidates:
        s = r["score"]
        p = r["put"]
        st = r["stock"]
        aff = "✅" if s["affordable"] else "❌"
        lines.append(
            f"| **{r['symbol']}** | ${st['price']:.2f} | ${p['strike']:.0f} | "
            f"{p['expiry']} | {p['dte']}d | ${p['mid']:.2f} | "
            f"{s['annualized_yield']:.0%} | {p['iv']:.0%} | {p['delta']:+.2f} | "
            f"${s['required_cash']:,.0f} {aff} | **{s['total_score']}** |"
        )
    lines.append("")
    lines.append("_✅ = affordable with current cash | ❌ = insufficient cash_")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("Scanning Wheel alternatives...")
    wheel_data = scan_wheel_alternatives(exclude="TSLA", top_n=2)
    print(format_wheel_alternatives_md(wheel_data))
    print()
    print("Scanning CSP candidates...")
    csp = scan_csp_candidates(top_n=3)
    print(format_csp_md(csp))
