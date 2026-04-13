"""绩效指标计算"""
from __future__ import annotations
import numpy as np
import pandas as pd
from loguru import logger


def calc_metrics(equity_curve: list[float], trades: list[dict],
                 initial_capital: float, risk_free_rate: float = 0.05) -> dict:
    """
    计算完整绩效指标
    equity_curve: 每根 K 线末的总权益序列
    trades: 所有成交记录列表
    """
    eq = pd.Series(equity_curve, dtype=float)
    if len(eq) < 2:
        return {}

    returns = eq.pct_change().dropna()
    total_return = (eq.iloc[-1] - initial_capital) / initial_capital

    # 年化 (假设每年 252 个交易日)
    n_periods   = len(eq)
    years        = n_periods / 252
    cagr         = (eq.iloc[-1] / initial_capital) ** (1 / max(years, 0.01)) - 1

    # 夏普比率
    excess = returns - risk_free_rate / 252
    sharpe = (excess.mean() / (returns.std() + 1e-9)) * np.sqrt(252)

    # 最大回撤
    rolling_max = eq.cummax()
    drawdown    = (eq - rolling_max) / rolling_max
    max_dd      = float(drawdown.min())

    # Calmar
    calmar = cagr / abs(max_dd) if max_dd != 0 else 0

    # 胜率与盈亏比
    closed = [t for t in trades if t.get("pnl") is not None]
    wins   = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] < 0]
    win_rate   = len(wins) / len(closed) if closed else 0
    avg_win    = np.mean(wins) if wins else 0
    avg_loss   = abs(np.mean(losses)) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

    metrics = {
        "初始资金":     initial_capital,
        "最终权益":     float(eq.iloc[-1]),
        "总收益率":     f"{total_return:.2%}",
        "年化收益率":   f"{cagr:.2%}",
        "夏普比率":     round(sharpe, 3),
        "最大回撤":     f"{max_dd:.2%}",
        "Calmar比率":   round(calmar, 3),
        "总交易次数":   len(closed),
        "胜率":        f"{win_rate:.2%}",
        "平均盈利":     round(avg_win, 2),
        "平均亏损":     round(avg_loss, 2),
        "盈亏比":      round(profit_factor, 3),
    }

    logger.info("=" * 50)
    logger.info("  回测绩效报告")
    logger.info("=" * 50)
    for k, v in metrics.items():
        logger.info(f"  {k:12s}: {v}")
    logger.info("=" * 50)
    return metrics
