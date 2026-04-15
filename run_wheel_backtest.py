"""Wheel 策略回测入口 — 对比各改进变体在 TSLA 历史数据上的效果

用法：
    python run_wheel_backtest.py
    python run_wheel_backtest.py --symbol NVDA --start 2022-01-01 --end 2024-12-31
"""
from __future__ import annotations
import sys
import argparse
from datetime import date, datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import settings
from backtest.wheel_backtest import run_wheel_backtest
from backtest.wheel_variants import make_variants


# TSLA 近年财报日（人工维护，可按需扩展）
TSLA_EARNINGS = [
    "2022-01-26", "2022-04-20", "2022-07-20", "2022-10-19",
    "2023-01-25", "2023-04-19", "2023-07-19", "2023-10-18",
    "2024-01-24", "2024-04-23", "2024-07-23", "2024-10-23",
    "2025-01-29", "2025-04-22", "2025-07-23", "2025-10-22",
    "2026-01-28",
]


def fetch_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """拉取日 K 线"""
    client = StockHistoricalDataClient(
        api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
    )
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
    )
    resp = client.get_stock_bars(req)
    bars = resp[symbol]
    df = pd.DataFrame([{
        "date": b.timestamp.date(),
        "open": float(b.open),
        "high": float(b.high),
        "low":  float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
    } for b in bars])
    df = df.set_index("date").sort_index()
    return df


def format_report(results: list) -> str:
    """将回测结果格式化为对比表"""
    lines = []
    lines.append("# Wheel 策略改进回测结果")
    lines.append("")
    lines.append("| # | 变体 | 总收益 | 年化 | 胜率 | 交易数 | 被行权 | 权利金 | MaxDD | Sharpe |")
    lines.append("|---|------|--------|------|------|--------|--------|--------|-------|--------|")
    for r in results:
        lines.append(
            f"| {r.config_name.split('_')[0]} | {r.config_name} | "
            f"{r.total_return:+.1%} | {r.annualized_return:+.1%} | "
            f"{r.win_rate:.0%} | {r.num_trades} | {r.num_assigned} | "
            f"${r.total_premium:,.0f} | {r.max_drawdown:.1%} | {r.sharpe:.2f} |"
        )
    lines.append("")

    # baseline 对比
    baseline = next((r for r in results if r.config_name.startswith("01_")), None)
    if baseline:
        lines.append("## 相对 Baseline 改善")
        lines.append("")
        lines.append("| 变体 | Δ年化 | Δ胜率 | Δ MaxDD | 建议 |")
        lines.append("|------|-------|-------|---------|------|")
        for r in results:
            if r.config_name == baseline.config_name:
                continue
            d_ann = r.annualized_return - baseline.annualized_return
            d_win = r.win_rate - baseline.win_rate
            d_dd = r.max_drawdown - baseline.max_drawdown
            if d_ann > 0.01 and d_dd <= 0.005:
                verdict = "✅ 采纳"
            elif d_ann > 0 and d_dd < 0:
                verdict = "✅ 采纳（降低回撤）"
            elif abs(d_ann) < 0.005:
                verdict = "➖ 效果不显著"
            else:
                verdict = "❌ 不采纳"
            lines.append(
                f"| {r.config_name} | {d_ann:+.1%} | {d_win:+.1%} | {d_dd:+.1%} | {verdict} |"
            )
    lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="TSLA")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--capital", type=float, default=100_000.0)
    args = p.parse_args()

    print(f"# 拉取 {args.symbol} 日线 {args.start} → {args.end}")
    df = fetch_bars(args.symbol, args.start, args.end)
    print(f"共 {len(df)} 条日线，价格区间 ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    print()

    variants = make_variants(earnings_dates=TSLA_EARNINGS)
    results = []
    for cfg in variants:
        print(f"运行 {cfg.name} ...", end=" ", flush=True)
        r = run_wheel_backtest(df, cfg, initial_capital=args.capital)
        results.append(r)
        print(f"年化 {r.annualized_return:+.1%} | 胜率 {r.win_rate:.0%} | "
              f"交易 {r.num_trades} | MaxDD {r.max_drawdown:.1%}")

    print()
    report = format_report(results)
    print(report)

    # 保存到文件
    out = f"reports/wheel_backtest_{args.symbol}_{args.start}_{args.end}.md"
    import os
    os.makedirs("reports", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告保存至 {out}")


if __name__ == "__main__":
    main()
