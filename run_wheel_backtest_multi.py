"""多标的 + 长窗口 Wheel 回测 — 2020-2025 on TSLA, NVDA, MSFT, SPY, AAPL"""
from __future__ import annotations
import sys
import os
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import settings
from backtest.wheel_backtest import run_wheel_backtest
from backtest.wheel_variants import make_variants


# 近年财报日（按需扩展）— 仅供 earnings filter 使用
EARNINGS_DB = {
    "TSLA": ["2020-01-29","2020-04-29","2020-07-22","2020-10-21",
             "2021-01-27","2021-04-26","2021-07-26","2021-10-20",
             "2022-01-26","2022-04-20","2022-07-20","2022-10-19",
             "2023-01-25","2023-04-19","2023-07-19","2023-10-18",
             "2024-01-24","2024-04-23","2024-07-23","2024-10-23",
             "2025-01-29","2025-04-22","2025-07-23","2025-10-22","2026-01-28"],
    "NVDA": ["2020-02-13","2020-05-21","2020-08-19","2020-11-18",
             "2021-02-24","2021-05-26","2021-08-18","2021-11-17",
             "2022-02-16","2022-05-25","2022-08-24","2022-11-16",
             "2023-02-22","2023-05-24","2023-08-23","2023-11-21",
             "2024-02-21","2024-05-22","2024-08-28","2024-11-20",
             "2025-02-26","2025-05-28","2025-08-27","2025-11-19","2026-02-25"],
    "MSFT": ["2020-01-29","2020-04-29","2020-07-22","2020-10-27",
             "2021-01-26","2021-04-27","2021-07-27","2021-10-26",
             "2022-01-25","2022-04-26","2022-07-26","2022-10-25",
             "2023-01-24","2023-04-25","2023-07-25","2023-10-24",
             "2024-01-30","2024-04-25","2024-07-30","2024-10-30",
             "2025-01-29","2025-04-30","2025-07-30","2025-10-29","2026-01-28"],
    "AAPL": ["2020-01-28","2020-04-30","2020-07-30","2020-10-29",
             "2021-01-27","2021-04-28","2021-07-27","2021-10-28",
             "2022-01-27","2022-04-28","2022-07-28","2022-10-27",
             "2023-02-02","2023-05-04","2023-08-03","2023-11-02",
             "2024-02-01","2024-05-02","2024-08-01","2024-10-31",
             "2025-01-30","2025-05-01","2025-07-31","2025-10-30","2026-01-29"],
    "SPY":  [],  # SPY 无财报
}


def fetch_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    client = StockHistoricalDataClient(
        api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
    )
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.fromisoformat(start), end=datetime.fromisoformat(end),
    )
    resp = client.get_stock_bars(req)
    df = pd.DataFrame([{
        "date": b.timestamp.date(),
        "open": float(b.open), "high": float(b.high),
        "low": float(b.low), "close": float(b.close),
        "volume": float(b.volume),
    } for b in resp[symbol]])
    return df.set_index("date").sort_index()


def run_one_symbol(symbol: str, start: str, end: str) -> dict:
    print(f"\n{'='*60}\n  {symbol}  {start} → {end}\n{'='*60}")
    df = fetch_bars(symbol, start, end)
    print(f"  {len(df)} 条日线, ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    variants = make_variants(earnings_dates=EARNINGS_DB.get(symbol, []))
    results = []
    for cfg in variants:
        r = run_wheel_backtest(df, cfg, initial_capital=100_000.0)
        results.append(r)
        print(f"  {cfg.name:32s} 年化 {r.annualized_return:+7.1%} | "
              f"胜率 {r.win_rate:5.0%} | MaxDD {r.max_drawdown:5.1%} | "
              f"Sharpe {r.sharpe:+5.2f}")
    return {"symbol": symbol, "results": results}


def cross_symbol_summary(all_data: list) -> str:
    """跨标的对比矩阵：每个改进在不同标的上的年化收益"""
    lines = ["# 多标的 × 多变体 Wheel 回测总览", ""]

    # 矩阵：行=变体，列=标的，值=年化收益
    variant_names = [r.config_name for r in all_data[0]["results"]]
    symbols = [d["symbol"] for d in all_data]

    lines.append("## 年化收益矩阵")
    lines.append("")
    header = "| 变体 | " + " | ".join(symbols) + " | 平均 |"
    sep = "|------|" + "|".join(["------"] * (len(symbols) + 1)) + "|"
    lines.append(header)
    lines.append(sep)

    for v_name in variant_names:
        row = [v_name]
        vals = []
        for d in all_data:
            r = next(r for r in d["results"] if r.config_name == v_name)
            vals.append(r.annualized_return)
            row.append(f"{r.annualized_return:+.1%}")
        row.append(f"**{sum(vals)/len(vals):+.1%}**")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 相对 baseline 改善矩阵
    lines.append("## 相对 Baseline 的年化改善")
    lines.append("")
    header = "| 改进 | " + " | ".join(symbols) + " | 平均 | 胜场次 |"
    sep = "|------|" + "|".join(["------"] * (len(symbols) + 2)) + "|"
    lines.append(header)
    lines.append(sep)

    baseline_name = variant_names[0]  # 01_baseline
    for v_name in variant_names[1:]:
        row = [v_name]
        deltas = []
        wins = 0
        for d in all_data:
            base = next(r for r in d["results"] if r.config_name == baseline_name)
            r = next(r for r in d["results"] if r.config_name == v_name)
            delta = r.annualized_return - base.annualized_return
            deltas.append(delta)
            if delta > 0.005:
                wins += 1
            row.append(f"{delta:+.1%}")
        avg = sum(deltas) / len(deltas)
        row.append(f"**{avg:+.1%}**")
        row.append(f"{wins}/{len(symbols)}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 最大回撤矩阵
    lines.append("## 最大回撤矩阵")
    lines.append("")
    header = "| 变体 | " + " | ".join(symbols) + " | 平均 |"
    lines.append(header)
    lines.append(sep)
    for v_name in variant_names:
        row = [v_name]
        vals = []
        for d in all_data:
            r = next(r for r in d["results"] if r.config_name == v_name)
            vals.append(r.max_drawdown)
            row.append(f"{r.max_drawdown:.1%}")
        row.append(f"**{sum(vals)/len(vals):.1%}**")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    return "\n".join(lines)


def main():
    symbols = ["TSLA", "NVDA", "MSFT", "AAPL", "SPY"]
    start = "2020-06-01"
    end = "2025-04-01"

    all_data = []
    for sym in symbols:
        try:
            all_data.append(run_one_symbol(sym, start, end))
        except Exception as e:
            print(f"  {sym} 失败: {e}")

    report = cross_symbol_summary(all_data)
    print("\n" + report)

    os.makedirs("reports", exist_ok=True)
    out = f"reports/wheel_backtest_multi_{start}_{end}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告保存至 {out}")


if __name__ == "__main__":
    main()
