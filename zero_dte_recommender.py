"""0DTE (Zero Days to Expiration) 期权推荐器

每日开盘时给出 3 个末日期权交易机会：
  1. 🛡️ DEFENSIVE  — SPY/QQQ 卖低 Delta Put（保守收权利金）
  2. ⚖️ BALANCED   — Mag 7 中势头标的卖中 Delta Put（平衡收益/风险）
  3. 🎲 LOTTO      — 强势标的买廉价 OTM Call（小赌大收益）

每个推荐含详细推荐理由，基于：
  - 标的趋势（vs 50d MA）
  - 当日 IV 水平
  - 流动性（bid/ask 价差）
  - 距离行权价的安全垫
"""
from __future__ import annotations
import re
from datetime import date, timedelta
from statistics import mean, stdev
from math import sqrt, log
from typing import Optional

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest, StockBarsRequest, StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame

from config import settings
from loguru import logger

# 当 Alpaca 缺失 0DTE Greeks 时自己算（BS 公式）
from backtest.wheel_pricing import bs_greeks


# 候选标的（高流动性 0DTE 必备）
DEFENSIVE_SYMBOLS = ["SPY", "QQQ"]              # 指数 ETF
BALANCED_SYMBOLS  = ["TSLA", "NVDA", "AAPL", "MSFT", "META", "AMD"]
LOTTO_SYMBOLS     = ["TSLA", "NVDA", "AMD", "PLTR", "COIN", "MSTR"]


# ── 数据辅助 ──────────────────────────────────────────────────────────

def _stock_metrics(sym: str, stk_client) -> Optional[dict]:
    """获取标的近期表现"""
    try:
        quote = stk_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=sym))
        bid = float(quote[sym].bid_price or 0)
        ask = float(quote[sym].ask_price or 0)
        # 盘后 ask=$0 时单用 bid 会拿到 stale 残留报价；价差过宽也不可信。
        price = None
        if bid > 0 and ask > 0 and ask >= bid and (ask - bid) / bid < 0.05:
            price = round((bid + ask) / 2, 2)

        bars = stk_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym, timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=80),
        )).data.get(sym, [])
        # 报价不可信时降级到最近日线收盘价
        if price is None and bars:
            price = round(float(bars[-1].close), 2)
        if len(bars) < 50:
            return None
        closes = [float(b.close) for b in bars]
        ma50 = mean(closes[-50:])
        ma10 = mean(closes[-10:])
        rets = [log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        rv = stdev(rets[-20:]) * sqrt(252)

        # 5 日动能（vs 5 日前价格）
        momentum_5d = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0

        return {
            "price": price,
            "ma50": ma50,
            "ma10": ma10,
            "rv": rv,
            "above_ma50": price > ma50,
            "above_ma10": price > ma10,
            "momentum_5d": momentum_5d,
        }
    except Exception as e:
        logger.debug(f"_stock_metrics({sym}) failed: {e}")
        return None


def _zero_dte_chain(sym: str, opt_client, opt_type: str) -> dict:
    """获取标的当日到期的期权链"""
    try:
        today = date.today()
        chain = opt_client.get_option_chain(OptionChainRequest(
            underlying_symbol=sym,
            expiration_date_gte=today,
            expiration_date_lte=today,
            type=opt_type,
        ))
        return chain or {}
    except Exception as e:
        logger.debug(f"_zero_dte_chain({sym},{opt_type}) failed: {e}")
        return {}


def _short_dte_chain(sym: str, opt_client, opt_type: str, max_dte: int = 3) -> tuple[dict, int]:
    """获取标的最近到期（0~max_dte 天）的期权链

    优先 0DTE，没有则按天搜，返回 (chain, actual_dte)
    """
    today = date.today()
    for offset in range(0, max_dte + 1):
        try:
            target = today + timedelta(days=offset)
            chain = opt_client.get_option_chain(OptionChainRequest(
                underlying_symbol=sym,
                expiration_date_gte=target,
                expiration_date_lte=target,
                type=opt_type,
            ))
            if chain:
                return chain, offset
        except Exception:
            continue
    return {}, -1


def _find_by_delta(chain: dict, target_delta: float,
                    max_spread_pct: float = 0.30,
                    spot_price: Optional[float] = None,
                    underlying: str = "",
                    fallback_iv: float = 0.30,
                    min_premium: float = 0.05) -> Optional[dict]:
    """从期权链中找最接近 target Delta 且流动性合格的合约

    Greeks/IV 缺失时（0DTE 常见）自动用 BS + realized vol 估算。
    """
    is_call = target_delta > 0
    best, best_diff = None, float("inf")

    # T = 早盘 0DTE 约 0.4 天，避免极小值造成 delta 跳变
    T = max(0.4 / 365, 1e-6)
    r = 0.045

    for sym, snap in chain.items():
        if not snap.latest_quote:
            continue
        bid = float(snap.latest_quote.bid_price or 0)
        ask = float(snap.latest_quote.ask_price or 0)
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        if mid < min_premium:  # 太便宜的废纸 skip
            continue
        spread_pct = (ask - bid) / mid

        # 取 Greeks（优先 Alpaca，否则 BS 自算）
        delta = snap.greeks.delta if (snap.greeks and snap.greeks.delta is not None) else None
        theta = snap.greeks.theta if (snap.greeks and snap.greeks.theta) else 0.0
        gamma = snap.greeks.gamma if (snap.greeks and snap.greeks.gamma) else 0.0
        iv = snap.implied_volatility or 0
        iv_used = iv if iv > 0 else fallback_iv  # IV 缺失时用 RV 兜底

        if delta is None:
            # BS 自算：解析 strike
            strike = _parse_strike(sym, underlying) if underlying else None
            if strike is None or spot_price is None or iv_used <= 0:
                continue
            try:
                delta, gamma, theta, _ = bs_greeks(
                    S=spot_price, K=strike, T=T, r=r, sigma=iv_used, is_call=is_call,
                )
            except Exception:
                continue

        # spread 检查（高 spread 但流动性 OK 也接受，因为 0DTE 普遍价差大）
        if spread_pct > max_spread_pct:
            continue

        diff = abs(delta - target_delta)
        if diff < best_diff:
            best_diff = diff
            best = {
                "symbol": sym,
                "bid": bid,
                "ask": ask,
                "mid": round(mid, 2),
                "delta": delta,
                "theta": theta,
                "gamma": gamma,
                "iv": iv_used,
                "spread_pct": spread_pct,
            }
    return best


def _parse_strike(opt_sym: str, sym: str) -> Optional[float]:
    m = re.match(rf"^{sym}(\d{{6}})[CP](\d{{8}})$", opt_sym)
    if not m:
        return None
    return int(m.group(2)) / 1000.0


# ── 三个推荐 ──────────────────────────────────────────────────────────

def _build_defensive_rec(stk, opt) -> Optional[dict]:
    """保守型：SPY/QQQ 卖 0DTE Put @ Delta ≈ -0.10"""
    candidates = []
    for sym in DEFENSIVE_SYMBOLS:
        m = _stock_metrics(sym, stk)
        if not m:
            continue
        chain = _zero_dte_chain(sym, opt, "put")
        if not chain:
            continue
        contract = _find_by_delta(chain, target_delta=-0.10,
                                   max_spread_pct=0.40,
                                   spot_price=m["price"], underlying=sym,
                                   fallback_iv=max(m["rv"], 0.15),
                                   min_premium=0.05)
        if not contract:
            continue
        strike = _parse_strike(contract["symbol"], sym)
        if strike is None:
            continue

        cushion = m["price"] - strike  # 当前价距 strike
        cushion_pct = cushion / m["price"]
        annualized = (contract["mid"] / strike) * 365 if strike > 0 else 0

        candidates.append({
            "symbol": sym, "metrics": m, "contract": contract,
            "strike": strike, "cushion": cushion, "cushion_pct": cushion_pct,
            "annualized": annualized,
        })

    if not candidates:
        return None

    # 选 cushion 最大的（最安全）
    best = max(candidates, key=lambda x: x["cushion_pct"])

    # 推荐理由
    sym = best["symbol"]
    m = best["metrics"]
    c = best["contract"]
    trend = "uptrend ✓" if m["above_ma50"] else "below MA50 ⚠"
    reasons = [
        f"**{sym}** is in {trend} ({m['price']:.2f} vs MA50 {m['ma50']:.2f})",
        f"Strike ${best['strike']:.0f} sits **{best['cushion_pct']:.1%} OTM** — strong safety cushion",
        f"5-day momentum: {m['momentum_5d']:+.1%} · 20-day RV: {m['rv']:.0%}",
        f"Premium ${c['mid']:.2f} → annualized **{best['annualized']:.0%}** if expires worthless",
    ]

    return {
        "tier": "🛡️ DEFENSIVE",
        "tier_color": "green",
        "title": f"Sell {sym} 0DTE Put",
        "contract": c["symbol"],
        "strike": best["strike"],
        "premium": c["mid"],
        "delta": c["delta"],
        "theta": c["theta"],
        "iv": c["iv"],
        "spread_pct": c["spread_pct"],
        "cushion_pct": best["cushion_pct"],
        "reasons": reasons,
        "risk_label": "Low",
        "max_loss": f"${best['strike']*100:,.0f} per contract if assigned (rare at -0.10 Δ)",
    }


def _build_balanced_rec(stk, opt) -> Optional[dict]:
    """中性型：Mag 7 卖 0DTE Put @ Delta ≈ -0.20，挑当日势头最好的"""
    candidates = []
    for sym in BALANCED_SYMBOLS:
        m = _stock_metrics(sym, stk)
        if not m:
            continue
        # 优先 MA50 之上，但不强制（评分会降权）
        chain, dte = _short_dte_chain(sym, opt, "put", max_dte=3)
        if not chain:
            continue
        contract = _find_by_delta(chain, target_delta=-0.20,
                                   max_spread_pct=0.45,
                                   spot_price=m["price"], underlying=sym,
                                   fallback_iv=max(m["rv"], 0.30),
                                   min_premium=0.10)
        if not contract:
            continue
        strike = _parse_strike(contract["symbol"], sym)
        if strike is None or contract["mid"] < 0.30:  # 至少 $30 权利金/合约
            continue

        cushion = m["price"] - strike
        cushion_pct = cushion / m["price"]
        annualized = (contract["mid"] / strike) * 365

        # 综合评分：动能 + 权利金 + cushion + 趋势加分
        trend_bonus = 5 if m["above_ma50"] else -5
        score = (m["momentum_5d"] * 50) + (annualized * 0.3) + (cushion_pct * 30) + trend_bonus

        candidates.append({
            "symbol": sym, "metrics": m, "contract": contract,
            "strike": strike, "cushion": cushion, "cushion_pct": cushion_pct,
            "annualized": annualized, "score": score, "dte": dte,
        })

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x["score"])
    sym = best["symbol"]
    m = best["metrics"]
    c = best["contract"]
    dte = best["dte"]
    dte_label = f"{dte}DTE" if dte > 0 else "0DTE"

    momentum_word = "strong" if m["momentum_5d"] > 0.03 else ("steady" if m["momentum_5d"] > 0 else "weakening")
    trend_status = "above both MA10 and MA50 — clean uptrend" if m["above_ma50"] else f"below MA50 (${m['ma50']:.2f}) — caution"
    reasons = [
        f"**{sym}** showing {momentum_word} momentum ({m['momentum_5d']:+.1%} over 5 days)",
        f"Currently {trend_status}",
        f"Strike ${best['strike']:.0f} = {best['cushion_pct']:.1%} OTM, theta decay ~${abs(c['theta']):.2f}/day",
        f"Premium ${c['mid']:.2f} = **{best['annualized']:.0%}** annualized · IV {c['iv']:.0%}",
    ]

    return {
        "tier": "⚖️ BALANCED",
        "tier_color": "blue",
        "title": f"Sell {sym} {dte_label} Put",
        "contract": c["symbol"],
        "strike": best["strike"],
        "premium": c["mid"],
        "delta": c["delta"],
        "theta": c["theta"],
        "iv": c["iv"],
        "spread_pct": c["spread_pct"],
        "cushion_pct": best["cushion_pct"],
        "reasons": reasons,
        "risk_label": "Medium",
        "max_loss": f"${best['strike']*100:,.0f} per contract if assigned",
    }


def _build_lotto_rec(stk, opt) -> Optional[dict]:
    """彩票型：买高动能标的 0DTE OTM Call (Delta ≈ 0.10)"""
    candidates = []
    for sym in LOTTO_SYMBOLS:
        m = _stock_metrics(sym, stk)
        if not m:
            continue
        # 动能筛选放宽（允许微弱正动能）
        if m["momentum_5d"] < -0.02:
            continue
        chain, dte = _short_dte_chain(sym, opt, "call", max_dte=3)
        if not chain:
            continue
        contract = _find_by_delta(chain, target_delta=0.10,
                                   max_spread_pct=0.50,
                                   spot_price=m["price"], underlying=sym,
                                   fallback_iv=max(m["rv"], 0.40),
                                   min_premium=0.05)
        if not contract:
            continue
        strike = _parse_strike(contract["symbol"], sym)
        if strike is None:
            continue
        # 价格不能太贵
        if contract["mid"] > 2.00:
            continue

        # 杠杆：Delta × Price / Premium
        leverage = (contract["delta"] * m["price"]) / contract["mid"] if contract["mid"] > 0 else 0
        # 突破距离：strike 距当前价多少
        breakout_pct = (strike - m["price"]) / m["price"]

        # 评分：动能 + 杠杆 - 突破距离（太远不容易到）
        score = (m["momentum_5d"] * 100) + (leverage * 5) - (breakout_pct * 100)

        candidates.append({
            "symbol": sym, "metrics": m, "contract": contract,
            "strike": strike, "leverage": leverage, "breakout_pct": breakout_pct,
            "score": score, "dte": dte,
        })

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x["score"])
    sym = best["symbol"]
    m = best["metrics"]
    c = best["contract"]
    dte = best["dte"]
    dte_label = f"{dte}DTE" if dte > 0 else "0DTE"

    reasons = [
        f"**{sym}** is the top momentum play today ({m['momentum_5d']:+.1%} over 5d)",
        f"Strike ${best['strike']:.0f} requires only **{best['breakout_pct']:.1%}** breakout to ITM",
        f"Premium ${c['mid']:.2f} gives **{best['leverage']:.1f}x** leverage if it spikes",
        f"Risk: total premium loss if no move; reward: 3-10x potential intraday",
    ]

    return {
        "tier": "🎲 LOTTO",
        "tier_color": "purple",
        "title": f"Buy {sym} {dte_label} Call",
        "contract": c["symbol"],
        "strike": best["strike"],
        "premium": c["mid"],
        "delta": c["delta"],
        "theta": c["theta"],
        "iv": c["iv"],
        "spread_pct": c["spread_pct"],
        "leverage": best["leverage"],
        "reasons": reasons,
        "risk_label": "High",
        "max_loss": f"${c['mid']*100:.0f} per contract (total premium)",
    }


# ── 公开 API ──────────────────────────────────────────────────────────

def build_zero_dte_recommendations() -> list[dict]:
    """生成 3 个 0DTE 推荐 — 保守/中性/彩票"""
    stk = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    opt = OptionHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)

    recs = []
    for builder in (_build_defensive_rec, _build_balanced_rec, _build_lotto_rec):
        try:
            r = builder(stk, opt)
            if r:
                recs.append(r)
        except Exception as e:
            logger.warning(f"0DTE recommendation builder failed: {e}")
    return recs


def format_zero_dte_md(recs: list[dict]) -> str:
    """格式化为 Markdown 区块"""
    lines = ["## 0DTE Quick Plays (Today's Expiration)", ""]
    if not recs:
        lines.append("_No qualifying 0DTE setups found today (market closed, low liquidity, or no momentum)._")
        return "\n".join(lines)

    lines.append("_Three actionable trades for today's session — pick by your risk appetite_")
    lines.append("")

    for i, r in enumerate(recs, 1):
        lines.append(f"### {r['tier']} · {r['title']}")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        lines.append(f"| Contract | `{r['contract']}` |")
        lines.append(f"| Strike | ${r['strike']:.2f} |")
        lines.append(f"| Premium (mid) | ${r['premium']:.2f} |")
        lines.append(f"| Delta | {r['delta']:+.3f} |")
        lines.append(f"| Theta | ${r['theta']:.2f}/day |")
        lines.append(f"| IV | {r['iv']:.0%} |")
        lines.append(f"| Bid-Ask Spread | {r['spread_pct']:.1%} |")
        lines.append(f"| Risk Level | {r['risk_label']} |")
        lines.append(f"| Max Loss | {r['max_loss']} |")
        lines.append("")
        lines.append("**Why this trade:**")
        lines.append("")
        for reason in r["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    recs = build_zero_dte_recommendations()
    print(format_zero_dte_md(recs))
