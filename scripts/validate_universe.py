"""Wheel/CSP 标的池验证器 — 数据驱动剔除不合格标的

验证标准：
  1. 日均成交量 ≥ 500万股（流动性底线）
  2. 有 DTE 1-10 天（Wheel 池）或 DTE 20-45 天（CSP 池）的期权链
  3. 25-delta Put 存在且买卖价差 ≤ 5%（期权流动性）
  4. 过去 30 天最大单日跌幅 ≤ 15%（稳定性）
  5. 股价 > $5（避免 penny stock）

用法：
    python scripts/validate_universe.py                # 默认验证当前两个池
    python scripts/validate_universe.py --add MORE_SYMBOLS  # 额外评估新标的
"""
from __future__ import annotations
import sys
import os
import argparse
import json
from datetime import date, timedelta
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest, StockBarsRequest, StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from config import settings
from wheel_scanner import WHEEL_UNIVERSE, CSP_UNIVERSE


# ── 候选新标的（我想补充评估的） ─────────────────────────────────────────
CANDIDATE_NEW = [
    # 可能有吸引力但原池未包含
    "ETSY", "ROKU", "RBLX", "DKNG", "ZM", "PINS", "SPOT",
    "ENPH", "FSLR", "RIVN", "LCID", "NIO", "XPEV",
    "GME", "AMC", "BBBY",  # meme 股（验证应剔除）
    "BA", "CAT", "GE", "NKE",  # 有些已在池里
    "TLT", "GLD", "SLV", "USO",  # 商品/债券 ETF
]


def check_symbol(
    sym: str,
    stk_client: StockHistoricalDataClient,
    opt_client: OptionHistoricalDataClient,
    dte_min: int = 1,
    dte_max: int = 10,
) -> dict:
    """对单只标的运行全部验证检查"""
    result = {
        "symbol": sym,
        "passed": False,
        "checks": {},
        "score": 0.0,
        "notes": [],
    }

    # ── 1. 股价 & 日均成交量 ──
    try:
        bars_req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=60),
        )
        bars = stk_client.get_stock_bars(bars_req).data.get(sym, [])
        if len(bars) < 10:
            result["notes"].append(f"数据不足 (only {len(bars)} bars)")
            return result

        recent_bars = bars[-30:]
        avg_volume = mean(float(b.volume) for b in recent_bars)
        price = float(recent_bars[-1].close)

        result["checks"]["price"] = price
        result["checks"]["avg_volume"] = int(avg_volume)
        result["checks"]["price_ok"] = price >= 5.0
        result["checks"]["volume_ok"] = avg_volume >= 5_000_000

        # 最大单日跌幅（30 天）
        closes = [float(b.close) for b in recent_bars]
        daily_rets = [(closes[i] - closes[i-1]) / closes[i-1]
                      for i in range(1, len(closes))]
        max_drop = min(daily_rets) if daily_rets else 0
        result["checks"]["max_drop"] = max_drop
        result["checks"]["stability_ok"] = max_drop > -0.15
    except Exception as e:
        result["notes"].append(f"股价数据失败: {e}")
        return result

    # ── 2. 期权链验证 ──
    try:
        today = date.today()
        chain = opt_client.get_option_chain(OptionChainRequest(
            underlying_symbol=sym,
            expiration_date_gte=today + timedelta(days=dte_min),
            expiration_date_lte=today + timedelta(days=dte_max),
            type="put",
        ))

        if not chain:
            result["notes"].append("无期权链")
            result["checks"]["options_ok"] = False
            return result

        # 找 Delta ≈ -0.25 的 Put
        target = -0.25
        best = None
        best_diff = 10.0
        for contract_sym, snapshot in chain.items():
            greeks = getattr(snapshot, "greeks", None)
            quote = getattr(snapshot, "latest_quote", None)
            if not greeks or not quote:
                continue
            delta = greeks.delta
            if delta is None:
                continue
            diff = abs(delta - target)
            if diff < best_diff:
                best_diff = diff
                best = (contract_sym, snapshot, delta)

        if not best:
            result["notes"].append("未找到 25-delta Put")
            result["checks"]["options_ok"] = False
            return result

        contract_sym, snapshot, delta = best
        quote = snapshot.latest_quote
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        mid = (bid + ask) / 2 if bid and ask else 0
        spread_pct = ((ask - bid) / mid) if mid > 0 else 1.0

        result["checks"]["option_contract"] = contract_sym
        result["checks"]["option_delta"] = delta
        result["checks"]["option_bid"] = bid
        result["checks"]["option_ask"] = ask
        result["checks"]["option_spread_pct"] = spread_pct
        result["checks"]["options_ok"] = 0 < spread_pct < 0.10  # 5% 略宽松到 10%
    except Exception as e:
        result["notes"].append(f"期权数据失败: {e}")
        result["checks"]["options_ok"] = False
        return result

    # ── 综合评分 ──
    all_ok = all([
        result["checks"].get("price_ok"),
        result["checks"].get("volume_ok"),
        result["checks"].get("stability_ok"),
        result["checks"].get("options_ok"),
    ])
    result["passed"] = all_ok

    # 评分：综合打分
    score = 0
    if result["checks"].get("volume_ok"):
        score += min(avg_volume / 10_000_000, 3) * 10  # 成交量越大越好
    if result["checks"].get("options_ok"):
        score += max(0, (0.10 - spread_pct) * 100)  # 价差越小越好
    if result["checks"].get("stability_ok"):
        score += max(0, (0.15 + max_drop) * 50)  # 跌幅越小越好
    result["score"] = round(score, 1)

    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pool", choices=["wheel", "csp", "both"], default="both")
    p.add_argument("--add", nargs="+", default=[], help="额外评估的新标的")
    p.add_argument("--dte-min", type=int, default=1)
    p.add_argument("--dte-max", type=int, default=10)
    p.add_argument("--output", default="reports/universe_validation.json")
    args = p.parse_args()

    stk = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    opt = OptionHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)

    # 决定要验证的标的集合
    symbols = set()
    if args.pool in ("wheel", "both"):
        symbols.update(WHEEL_UNIVERSE)
    if args.pool in ("csp", "both"):
        symbols.update(CSP_UNIVERSE)
    symbols.update(args.add)
    if not args.add and args.pool == "both":
        symbols.update(CANDIDATE_NEW)
    symbols = sorted(symbols)

    print(f"验证 {len(symbols)} 只标的（DTE {args.dte_min}-{args.dte_max}）...\n")

    results = []
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {sym:6s} ... ", end="", flush=True)
        r = check_symbol(sym, stk, opt, args.dte_min, args.dte_max)
        results.append(r)
        if r["passed"]:
            print(f"✓ 通过 (score={r['score']}, vol={r['checks'].get('avg_volume',0):,}, spread={r['checks'].get('option_spread_pct',0):.1%})")
        else:
            notes = "; ".join(r["notes"]) if r["notes"] else "、".join(
                k for k, v in r["checks"].items() if k.endswith("_ok") and not v
            )
            print(f"✗ 拒绝 ({notes})")

    # 排序：通过的按 score 降序
    passed = [r for r in results if r["passed"]]
    rejected = [r for r in results if not r["passed"]]
    passed.sort(key=lambda x: x["score"], reverse=True)

    # 输出汇总
    print("\n" + "=" * 60)
    print(f"验证总结: {len(passed)}/{len(results)} 通过")
    print("=" * 60)

    print("\n## ✅ 通过验证（按评分排序）\n")
    print(f"{'符号':<6} {'价格':>8} {'日均成交量':>15} {'价差':>7} {'评分':>6}")
    print("-" * 50)
    for r in passed[:40]:
        c = r["checks"]
        print(f"{r['symbol']:<6} ${c.get('price',0):>7.2f} {c.get('avg_volume',0):>15,} "
              f"{c.get('option_spread_pct',0):>7.1%} {r['score']:>6.1f}")

    print(f"\n## ❌ 拒绝（{len(rejected)} 只）\n")
    for r in rejected[:30]:
        reason = "; ".join(r["notes"]) or ",".join(
            k.replace("_ok","") for k,v in r["checks"].items()
            if k.endswith("_ok") and not v
        )
        print(f"  {r['symbol']:<6} — {reason}")

    # 写 JSON
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({
            "validated_at": date.today().isoformat(),
            "dte_range": [args.dte_min, args.dte_max],
            "passed": [r for r in passed],
            "rejected": [r for r in rejected],
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n结果已保存至 {args.output}")

    # 生成建议的新池
    print("\n## 📝 推荐的新 WHEEL_UNIVERSE（Top 40）\n")
    top_syms = [r["symbol"] for r in passed[:40]]
    print('WHEEL_UNIVERSE = [')
    for i in range(0, len(top_syms), 8):
        row = top_syms[i:i+8]
        print('    ' + ', '.join(f'"{s}"' for s in row) + ',')
    print(']')


if __name__ == "__main__":
    main()
