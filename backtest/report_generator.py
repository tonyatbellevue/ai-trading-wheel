"""回测报告生成 — HTML 交互图表 + CSV 交易记录"""
from __future__ import annotations
import os
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from backtest.broker_simulator import SimBroker
from config import settings
from loguru import logger


def generate_report(broker: SimBroker, metrics: dict, output_name: str = "backtest_report"):
    os.makedirs(settings.REPORTS_DIR, exist_ok=True)
    html_path = os.path.join(settings.REPORTS_DIR, f"{output_name}.html")
    csv_path  = os.path.join(settings.REPORTS_DIR, f"{output_name}_trades.csv")

    # ── 交易记录 CSV ─────────────────────────────────────────────────
    trades_df = pd.DataFrame(broker.trades)
    trades_df.to_csv(csv_path, index=False)
    logger.info(f"交易记录已保存: {csv_path}")

    # ── 权益曲线图 ───────────────────────────────────────────────────
    eq = pd.Series(broker.equity_curve)

    fig = make_subplots(rows=2, cols=1,
                        subplot_titles=("权益曲线", "逐笔 PnL"),
                        vertical_spacing=0.12)

    fig.add_trace(go.Scatter(
        y=eq.values, mode="lines", name="权益",
        line=dict(color="#00b4d8", width=2),
    ), row=1, col=1)

    # 基准线
    fig.add_hline(y=broker.initial_capital, line_dash="dash",
                  line_color="gray", annotation_text="初始资金", row=1, col=1)

    # PnL 柱状图
    closed = [t for t in broker.trades if t.get("pnl") is not None]
    if closed:
        pnl_vals  = [t["pnl"] for t in closed]
        pnl_colors = ["#2ecc71" if p > 0 else "#e74c3c" for p in pnl_vals]
        fig.add_trace(go.Bar(
            y=pnl_vals, name="逐笔PnL",
            marker_color=pnl_colors,
        ), row=2, col=1)

    # 绩效表格
    metric_text = "<br>".join([f"<b>{k}</b>: {v}" for k, v in metrics.items()])
    fig.add_annotation(
        text=metric_text,
        xref="paper", yref="paper",
        x=1.01, y=0.95, align="left",
        showarrow=False,
        font=dict(size=11),
        bgcolor="rgba(255,255,255,0.8)",
        bordercolor="gray", borderwidth=1,
    )

    fig.update_layout(
        title="AI 交易机器人 — 回测报告",
        template="plotly_dark",
        height=700,
        showlegend=True,
        margin=dict(r=250),
    )

    fig.write_html(html_path)
    logger.info(f"回测报告已保存: {html_path}")
    return html_path
