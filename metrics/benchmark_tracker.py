"""Benchmark tracker — compare bot equity curve to SPY and PUTW (CBOE PUT proxy).

The existential question every quant strategy must answer: "Am I actually
adding value vs just buying the index?"

This module reads the bot's daily_snapshot.csv (built by baseline_tracker)
and pulls SPY + PUTW from Alpaca, then computes:
  - Total return (bot vs each benchmark)
  - Annualized Sharpe ratio
  - Max drawdown
  - Alpha / Beta vs SPY

Output is a markdown section that weekly_review can append, or a standalone
report you can run any time.

NOTES on statistical validity:
  - < 30 trading days of data: numbers are noise, treat as directional only
  - 30-90 days: Sharpe / alpha estimates have huge confidence intervals
  - 90+ days: getting meaningful, but still 1-sigma error ~30% on Sharpe
  - 252+ days (1 year): standard "annual return" claim becomes valid
  Always print N alongside the number so the reader can calibrate.

Usage:
    from metrics.benchmark_tracker import build_benchmark_report
    md = build_benchmark_report()
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

from loguru import logger

from config import settings
from core.alpaca_client import AlpacaClients

METRICS_DIR = Path(settings.BASE_DIR) / "metrics" / "data"
DAILY_SNAPSHOT_CSV = METRICS_DIR / "daily_snapshot.csv"

# SPY = S&P 500 ETF, the "did you beat passive holding?" benchmark
# PBP = Invesco S&P 500 BuyWrite — Alpaca DOES have it (PUTW returned 0 bars
#       at testing). By put-call parity, BuyWrite ≈ PutWrite economically:
#       both harvest the IV>RV premium on SPX, just from opposite sides.
BENCHMARKS = ["SPY", "PBP"]

# Trading days per year for annualization
TRADING_DAYS_PER_YEAR = 252

# Risk-free rate for Sharpe (rough approximation, doesn't move the needle
# much for short-horizon comparisons)
RISK_FREE_RATE_ANNUAL = 0.045  # 4.5% — typical short-term US Treasury


# ── Data fetchers ──────────────────────────────────────────────────────────

def _read_bot_equity_history() -> list[tuple[date, float]]:
    """Return [(date, equity)] from daily_snapshot.csv, sorted oldest first.

    Dedupes by date — baseline_tracker.record_snapshot() appends every wheel
    cycle (12+ times per trading day), so the raw CSV has many rows per
    date. We keep the LAST equity value per date, which is the closest to
    end-of-day (the chronologically last cycle of the day).

    Returns empty list if file doesn't exist or has no data — caller should
    handle the empty case (e.g. "not enough data yet").
    """
    if not DAILY_SNAPSHOT_CSV.exists():
        return []
    by_date: dict[date, float] = {}
    try:
        with open(DAILY_SNAPSHOT_CSV, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    d = datetime.strptime(r["date"], "%Y-%m-%d").date()
                    eq = float(r["equity"])
                    # Last write per date wins — CSV is append-only and
                    # written chronologically, so the final row of the day
                    # is the closing equity.
                    by_date[d] = eq
                except (KeyError, ValueError):
                    continue
    except Exception as e:
        logger.warning(f"读取 daily_snapshot.csv 失败: {e}")
        return []
    return sorted(by_date.items())


def _fetch_benchmark_closes(symbol: str, start: date, end: date) -> list[tuple[date, float]]:
    """Pull daily closes for a benchmark symbol from Alpaca.

    Returns [(date, close)] sorted oldest first. Empty list on error.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import Adjustment

        client = StockHistoricalDataClient(
            api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
        )
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,    # split-adjusted
        ))[symbol]
        return [(b.timestamp.date(), float(b.close)) for b in bars]
    except Exception as e:
        logger.warning(f"拉 {symbol} bars 失败: {e}")
        return []


# ── Stat calculators ──────────────────────────────────────────────────────

def _daily_returns(series: list[tuple[date, float]]) -> list[float]:
    """Convert price/equity series to daily simple returns."""
    if len(series) < 2:
        return []
    returns = []
    for i in range(1, len(series)):
        prev = series[i-1][1]
        curr = series[i][1]
        if prev > 0:
            returns.append((curr - prev) / prev)
    return returns


def _annualized_return(returns: list[float]) -> Optional[float]:
    """Geometric annualized return from daily returns. Returns None if empty."""
    if not returns:
        return None
    cumulative = 1.0
    for r in returns:
        cumulative *= (1 + r)
    n_days = len(returns)
    # (1 + r_total) ^ (252/n) - 1
    return cumulative ** (TRADING_DAYS_PER_YEAR / n_days) - 1


def _annualized_volatility(returns: list[float]) -> Optional[float]:
    """Annualized stdev of daily returns."""
    if len(returns) < 2:
        return None
    return stdev(returns) * sqrt(TRADING_DAYS_PER_YEAR)


def _sharpe_ratio(returns: list[float], rf: float = RISK_FREE_RATE_ANNUAL) -> Optional[float]:
    """Annualized Sharpe = (annual return − rf) / annual vol."""
    ann_ret = _annualized_return(returns)
    ann_vol = _annualized_volatility(returns)
    if ann_ret is None or ann_vol is None or ann_vol == 0:
        return None
    return (ann_ret - rf) / ann_vol


def _max_drawdown(series: list[tuple[date, float]]) -> Optional[float]:
    """Max % drawdown from peak-to-trough on the equity series."""
    if len(series) < 2:
        return None
    peak = series[0][1]
    max_dd = 0.0
    for _, val in series:
        if val > peak:
            peak = val
        dd = (val - peak) / peak    # negative number
        if dd < max_dd:
            max_dd = dd
    return max_dd    # negative or zero


def _align_series(s1: list[tuple[date, float]],
                  s2: list[tuple[date, float]]) -> tuple[list[float], list[float]]:
    """Return two return-series aligned by common dates.

    Critical for alpha/beta — we need bot-return[i] and benchmark-return[i]
    to refer to the SAME day. CSV gaps (weekends, holidays) and bot start
    date being later than benchmark mean we have to filter to intersection.
    """
    # Map date → value for fast lookup
    m1 = dict(s1)
    m2 = dict(s2)
    common_dates = sorted(set(m1.keys()) & set(m2.keys()))
    if len(common_dates) < 2:
        return [], []

    aligned1 = [(d, m1[d]) for d in common_dates]
    aligned2 = [(d, m2[d]) for d in common_dates]

    return _daily_returns(aligned1), _daily_returns(aligned2)


def _alpha_beta(bot_returns: list[float],
                bench_returns: list[float]) -> tuple[Optional[float], Optional[float]]:
    """OLS regression bot_ret = alpha + beta * bench_ret + ε.

    Returns (alpha_annualized, beta). Both None if insufficient data.
    """
    if len(bot_returns) != len(bench_returns) or len(bot_returns) < 10:
        return None, None

    n = len(bot_returns)
    bot_mean = mean(bot_returns)
    bench_mean = mean(bench_returns)

    # Covariance / Variance for beta
    cov = sum((bot_returns[i] - bot_mean) * (bench_returns[i] - bench_mean)
              for i in range(n)) / (n - 1)
    var_bench = sum((bench_returns[i] - bench_mean) ** 2 for i in range(n)) / (n - 1)
    if var_bench == 0:
        return None, None
    beta = cov / var_bench

    # Alpha = mean(bot) − beta * mean(bench), annualized
    alpha_daily = bot_mean - beta * bench_mean
    alpha_annualized = alpha_daily * TRADING_DAYS_PER_YEAR

    return alpha_annualized, beta


# ── Report builder ─────────────────────────────────────────────────────────

def _stat_quality_warning(n_days: int) -> str:
    """Honest disclaimer about statistical reliability at this sample size."""
    if n_days < 14:
        return "⚠️ < 14 天数据 — 数字仅供参考，统计不显著"
    if n_days < 30:
        return "⚠️ < 30 天数据 — 趋势性有效，绝对值噪音大"
    if n_days < 90:
        return "📊 30-90 天数据 — Sharpe / alpha 误差 ±30%"
    if n_days < 252:
        return "📊 90-252 天数据 — 接近统计有效"
    return "✅ ≥ 252 天 (1 年) — 统计稳健"


def _fmt_pct(x: Optional[float], digits: int = 2) -> str:
    return f"{x*100:+.{digits}f}%" if x is not None else "n/a"


def _fmt_num(x: Optional[float], digits: int = 2) -> str:
    return f"{x:.{digits}f}" if x is not None else "n/a"


def build_benchmark_report() -> str:
    """Return a markdown section comparing bot to SPY and PUTW."""
    bot_history = _read_bot_equity_history()

    if len(bot_history) < 2:
        return ("## 📊 基准对比\n\n"
                "⚠️ daily_snapshot.csv 数据不足 (< 2 天)，无法对比。\n"
                "确认 baseline_tracker.record_snapshot() 每日有调用。\n")

    start = bot_history[0][0]
    end = bot_history[-1][0]
    n_days = len(bot_history)

    lines = [
        f"## 📊 基准对比 — Bot vs {' vs '.join(BENCHMARKS)}",
        "",
        f"**周期**: {start} ~ {end}  ({n_days} 个数据点)",
        f"**统计可信度**: {_stat_quality_warning(n_days)}",
        "",
    ]

    # Bot's own metrics
    # 5/30: 起始 equity 改用 settings.INITIAL_CAPITAL ($100K) 而非 bot_history[0][1]，
    # 这样把账户开户至今的全部 wheel 操作都算进去（snapshot CSV 是 4/16 才开始记录的，
    # 之前 ~$737 早期累积没体现）。用户偏好"全部都算"。
    from config import settings
    initial_capital = getattr(settings, "INITIAL_CAPITAL", 100000.0)

    bot_returns = _daily_returns(bot_history)
    current_equity = bot_history[-1][1]
    bot_total = (current_equity - initial_capital) / initial_capital
    bot_ann_ret = _annualized_return(bot_returns)
    bot_ann_vol = _annualized_volatility(bot_returns)
    bot_sharpe = _sharpe_ratio(bot_returns)
    bot_dd = _max_drawdown(bot_history)

    lines += [
        "### 你的 Bot",
        "",
        f"- 账户起始 equity (开户): ${initial_capital:,.2f}",
        f"- 快照记录起点: ${bot_history[0][1]:,.2f} ({start})",
        f"- 当前 equity: ${current_equity:,.2f}",
        f"- 总收益（从开户算）: {_fmt_pct(bot_total)}",
        f"- 年化收益 (推算): {_fmt_pct(bot_ann_ret)}",
        f"- 年化波动率: {_fmt_pct(bot_ann_vol)}",
        f"- Sharpe 比率: {_fmt_num(bot_sharpe)}",
        f"- 最大回撤: {_fmt_pct(bot_dd)}",
        "",
    ]

    # Per-benchmark comparison
    for sym in BENCHMARKS:
        bars = _fetch_benchmark_closes(sym, start - timedelta(days=5), end + timedelta(days=2))
        if not bars:
            lines += [f"### {sym}", "", f"⚠️ 无法拉 {sym} 数据", ""]
            continue

        # Total return (close-to-close on aligned period)
        bench_aligned = [(d, c) for d, c in bars if start <= d <= end]
        if len(bench_aligned) < 2:
            lines += [f"### {sym}", "", f"⚠️ {sym} 数据点不足 (need ≥ 2)", ""]
            continue

        bench_total = bench_aligned[-1][1] / bench_aligned[0][1] - 1
        bench_returns = _daily_returns(bench_aligned)
        bench_ann_ret = _annualized_return(bench_returns)
        bench_ann_vol = _annualized_volatility(bench_returns)
        bench_sharpe = _sharpe_ratio(bench_returns)
        bench_dd = _max_drawdown(bench_aligned)

        # Alpha / Beta of bot vs this benchmark
        b_aligned, h_aligned = _align_series(bot_history, bench_aligned)
        alpha, beta = _alpha_beta(b_aligned, h_aligned)

        diff_total = bot_total - bench_total

        lines += [
            f"### {sym}",
            "",
            f"- 总收益: {_fmt_pct(bench_total)}",
            f"- 年化收益: {_fmt_pct(bench_ann_ret)}",
            f"- 年化波动率: {_fmt_pct(bench_ann_vol)}",
            f"- Sharpe 比率: {_fmt_num(bench_sharpe)}",
            f"- 最大回撤: {_fmt_pct(bench_dd)}",
            "",
            f"**Bot vs {sym}**:",
            f"- 累计收益差: {_fmt_pct(diff_total)} (正 = bot 跑赢)",
            f"- Alpha (年化): {_fmt_pct(alpha)} (扣除 beta 暴露后的纯 α)",
            f"- Beta: {_fmt_num(beta)} (1.0 = 跟 {sym} 同步, < 1 = 抗跌)",
            "",
        ]

    # Footer with caveats
    lines += [
        "---",
        "*基准说明*:",
        "- **SPY**: 标普 500 ETF — 被动持股 benchmark",
        "- **PBP**: Invesco S&P 500 BuyWrite ETF — 系统化卖 ATM call",
        "  (Alpaca 不提供 PUTW; PBP 用 BuyWrite 而非 PutWrite, 但通过",
        "  put-call parity 经济等价 — 都是收 IV-RV 溢价)",
        "",
        "*Sharpe 公式*: (年化 return - 4.5% rf) / 年化 vol",
        "*Alpha/Beta*: 至少需 10 个共同交易日才计算 (统计有意义)",
        "",
        f"*生成时间*: {datetime.now().isoformat(timespec='seconds')}",
    ]

    return "\n".join(lines)
