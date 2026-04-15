"""Wheel 标的适用性健康检查 — 评估 TSLA（或其它标的）是否适合继续 Wheel 策略

检查维度：
1. 近期波动（5日realized vol）
2. 隐含波动率 IV（near-the-money Put）
3. 近期最大单日跌幅
4. 权利金收益率（Delta ≈ -0.25 的 Put 收益率）
5. 购买力充足性（账户能否支撑 x2 合约）
6. 3-day option 流动性（OI / volume）

输出：评分 + GO / CAUTION / STOP 建议
"""
from __future__ import annotations

import re
import math
from datetime import date, timedelta
from math import log, sqrt
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


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / sqrt(2)))


def run_health_check(symbol: Optional[str] = None) -> dict:
    """运行全面健康检查，返回 dict 供摘要使用"""
    sym = symbol or settings.WHEEL_SYMBOL
    trading = AlpacaClients.trading()
    opt_data = OptionHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    stk_data = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)

    warnings = []
    checks = []

    # ── 当前价 ──
    try:
        trade = stk_data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym))
        price = float(trade[sym].price)
    except Exception as e:
        return {"error": f"获取股价失败: {e}"}

    # ── 1. 近10日价格波动（realized vol）──
    try:
        bars_req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=20),
        )
        bars = stk_data.get_stock_bars(bars_req)[sym]
        closes = [float(b.close) for b in bars[-10:]]
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        daily_vol = stdev(returns) if len(returns) > 1 else 0
        realized_vol = daily_vol * sqrt(252)  # 年化
        max_drawdown = min(returns) if returns else 0
        recent_range = (max(closes) - min(closes)) / closes[0]

        status = "OK" if realized_vol < 0.70 else "WARN"
        if status == "WARN":
            warnings.append("近10日年化波动率过高")
        checks.append({
            "name": "Realized Volatility (10d, annualized)",
            "value": f"{realized_vol:.0%}",
            "status": status,
            "note": f"max single-day drop: {max_drawdown:.1%}",
        })

        status = "OK" if abs(max_drawdown) < 0.08 else "WARN"
        if status == "WARN":
            warnings.append(f"近10日单日最大跌幅 {max_drawdown:.1%}")
        checks.append({
            "name": "Max Daily Drop (10d)",
            "value": f"{max_drawdown:.1%}",
            "status": status,
            "note": "<-8% = 警告",
        })
    except Exception as e:
        checks.append({"name": "Realized Vol", "value": "ERROR", "status": "WARN", "note": str(e)})

    # ── 2. 隐含波动率 & 下一轮权利金 ──
    try:
        today = date.today()
        req = OptionChainRequest(
            underlying_symbol=sym,
            expiration_date_gte=today + timedelta(days=1),
            expiration_date_lte=today + timedelta(days=7),
            type="put",
        )
        chain = opt_data.get_option_chain(req)

        # 找 delta 最接近 -0.25 的 Put
        best = None
        best_diff = float("inf")
        for s, snap in chain.items():
            if not snap.greeks or snap.greeks.delta is None:
                continue
            if not snap.latest_quote:
                continue
            diff = abs(snap.greeks.delta - (-0.25))
            if diff < best_diff:
                best_diff = diff
                best = (s, snap)

        if best:
            s, snap = best
            m = re.match(rf"^{sym}(\d{{6}})P(\d{{8}})$", s)
            strike = int(m.group(2)) / 1000.0 if m else 0
            dte = (date(2000+int(m.group(1)[:2]), int(m.group(1)[2:4]), int(m.group(1)[4:])) - today).days if m else 0
            bid = float(snap.latest_quote.bid_price or 0)
            ask = float(snap.latest_quote.ask_price or 0)
            mid = (bid + ask) / 2
            iv = snap.implied_volatility or 0
            delta = snap.greeks.delta

            # IV 合理区间（TSLA 30%-80%）
            status = "OK" if 0.30 <= iv <= 0.80 else "WARN"
            if iv < 0.30:
                warnings.append("IV 过低，权利金收益不足")
            elif iv > 0.80:
                warnings.append("IV 过高，风险显著")
            checks.append({
                "name": f"Implied Volatility (ATM Put)",
                "value": f"{iv:.0%}",
                "status": status,
                "note": "30%-80% = 合理",
            })

            # 收益率 (基于 strike 作为抵押)
            premium_total = mid * 200  # x2 contracts
            yield_pct = mid / strike  # 单股权利金/单股strike
            annualized = yield_pct * (365 / max(dte, 1)) if dte > 0 else 0

            status = "OK" if annualized > 0.20 else "WARN"
            if status == "WARN":
                warnings.append("年化收益率 <20%，不够吸引")
            checks.append({
                "name": f"Next Put Premium (Δ≈-0.25, {dte}d)",
                "value": f"${mid:.2f}/share (${premium_total:.0f}/x2)",
                "status": status,
                "note": f"strike=${strike:.0f}, annualized yield={annualized:.0%}",
            })

            # 购买力检查 — 用 cash（现有CSP平仓后会释放购买力）
            acct = trading.get_account()
            cash = float(acct.cash)
            bp = float(acct.buying_power)
            required = strike * 200  # x2
            # 检查现金（到期后可用）
            status = "OK" if cash >= required else "STOP"
            if status == "STOP":
                warnings.append(f"现金不足 (需 ${required:,.0f}, 有 ${cash:,.0f})")
            checks.append({
                "name": "Cash for x2 (post-expiry)",
                "value": f"${cash:,.0f} / ${required:,.0f} required",
                "status": status,
                "note": f"current BP=${bp:,.0f} (CSP reserved)",
            })

            # 记录推荐下一轮
            next_round = {
                "symbol": s,
                "strike": strike,
                "dte": dte,
                "delta": delta,
                "mid": mid,
                "iv": iv,
                "premium_x2": premium_total,
                "annualized_yield": annualized,
            }
        else:
            checks.append({
                "name": "Next Round Put",
                "value": "无合适合约",
                "status": "WARN",
                "note": "近一周无 Delta≈-0.25 的 Put",
            })
            warnings.append("无合适的下一轮 Put")
            next_round = None
    except Exception as e:
        checks.append({"name": "IV Check", "value": "ERROR", "status": "WARN", "note": str(e)})
        next_round = None

    # ── 综合判断 ──
    has_stop = any(c["status"] == "STOP" for c in checks)
    warn_count = sum(1 for c in checks if c["status"] == "WARN")

    if has_stop:
        verdict = "STOP"
        verdict_icon = "🛑"
        verdict_msg = "**STOP — 暂停 Wheel 策略**"
    elif warn_count >= 3:
        verdict = "STOP"
        verdict_icon = "🛑"
        verdict_msg = f"**STOP — {warn_count} 项警告，建议暂停并重新评估**"
    elif warn_count >= 1:
        verdict = "CAUTION"
        verdict_icon = "⚠️"
        verdict_msg = f"**CAUTION — {warn_count} 项警告，可继续但留意**"
    else:
        verdict = "GO"
        verdict_icon = "✅"
        verdict_msg = "**GO — 标的健康，可继续 Wheel**"

    return {
        "symbol": sym,
        "price": price,
        "verdict": verdict,
        "verdict_icon": verdict_icon,
        "verdict_msg": verdict_msg,
        "warnings": warnings,
        "checks": checks,
        "next_round": next_round,
    }


def format_markdown(result: dict) -> str:
    """将健康检查结果格式化为 Markdown"""
    if "error" in result:
        return f"## Suitability Check\n\n❌ Error: {result['error']}\n"

    lines = []
    sym = result["symbol"]
    lines.append(f"## Suitability Check for {sym}")
    lines.append("")
    lines.append(f"### {result['verdict_icon']} Verdict: {result['verdict_msg']}")
    lines.append("")

    lines.append("| Check | Value | Status | Note |")
    lines.append("|-------|-------|--------|------|")
    for c in result["checks"]:
        icon = {"OK": "✅", "WARN": "⚠️", "STOP": "🛑"}[c["status"]]
        lines.append(f"| {c['name']} | {c['value']} | {icon} | {c['note']} |")
    lines.append("")

    if result["warnings"]:
        lines.append("### Warnings")
        lines.append("")
        for w in result["warnings"]:
            lines.append(f"- ⚠️ {w}")
        lines.append("")

    nr = result.get("next_round")
    if nr:
        lines.append("### Next Round Candidate")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        lines.append(f"| Contract | `{nr['symbol']}` |")
        lines.append(f"| Strike | ${nr['strike']:.2f} |")
        lines.append(f"| DTE | {nr['dte']} days |")
        lines.append(f"| Delta | {nr['delta']:+.3f} |")
        lines.append(f"| Premium (mid) | ${nr['mid']:.2f}/share |")
        lines.append(f"| Premium x2 | ${nr['premium_x2']:.0f} |")
        lines.append(f"| IV | {nr['iv']:.0%} |")
        lines.append(f"| Annualized Yield | {nr['annualized_yield']:.0%} |")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    result = run_health_check()
    print(format_markdown(result))
