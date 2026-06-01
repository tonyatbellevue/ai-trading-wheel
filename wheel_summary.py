"""Wheel 策略每日摘要 — 生成 Markdown 格式报告供 GitHub Issue 使用

由 GitHub Actions 在市场开盘和收盘时调用。
输出到 stdout（适合 GitHub Actions 的 $GITHUB_STEP_SUMMARY）
同时写入 summary.md 供创建 Issue 使用。
"""
import sys
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, StockLatestQuoteRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

from config import settings
from core.alpaca_client import AlpacaClients
from execution.option_order_manager import OptionOrderManager
from strategy.wheel_strategy import WheelStrategy, WheelPhase, _parse_symbol

ET = ZoneInfo("America/New_York")


def build_summary(event: str = "update") -> str:
    """生成 Markdown 格式的 Wheel 策略摘要

    event: "open" | "close" | "trade" | "update"
    """
    trading  = AlpacaClients.trading()
    opt_data = OptionHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    stk_data = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    opt_mgr  = OptionOrderManager()
    sym      = settings.WHEEL_SYMBOL

    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    lines = []

    # ── 头部 ─────────────────────────────────────────────────────────────
    title_map = {
        "open":   "Market Open Summary",
        "close":  "Market Close Summary",
        "trade":  "Trade Executed",
        "update": "Wheel Status Update",
    }
    lines.append(f"# {sym} Wheel — {title_map.get(event, 'Update')}")
    lines.append(f"**{now_et}**")
    lines.append("")

    # ── 阶段 ─────────────────────────────────────────────────────────────
    strategy = WheelStrategy(sym)
    phase, obj = strategy.get_phase()
    phase_desc = {
        WheelPhase.IDLE:       "IDLE — waiting to sell new Put",
        WheelPhase.SHORT_PUT:  "SHORT PUT — cash-secured put active",
        WheelPhase.LONG_STOCK: "LONG STOCK — ready to sell Covered Call",
        WheelPhase.SHORT_CALL: "SHORT CALL — covered call active",
    }
    lines.append(f"## Phase: `{phase.name}`")
    lines.append(phase_desc[phase])
    lines.append("")

    # ── 每轮评估决策 ──────────────────────────────────────────────────────
    try:
        from strategy.wheel_evaluator import evaluate_and_maybe_plan
        from strategy.wheel_switch import load_state
        decision = evaluate_and_maybe_plan(
            current_symbol=sym,
            current_phase_is_idle=(phase == WheelPhase.IDLE),
        )
        state = load_state()
        plan = state.get("plan")
        lines.append("## Cycle Evaluation")
        lines.append("")
        lines.append(f"- Health Verdict: **{decision.get('health_verdict','?')}**")
        lines.append(f"- Action: `{decision.get('action','keep')}`")
        lines.append(f"- {decision.get('message','')}")
        if plan:
            lines.append(f"- 📅 **Pending Switch**: {plan['from_symbol']} → **{plan['to_symbol']}** @ ≥{plan['trigger_date']}")
            lines.append(f"  - Reason: _{plan.get('reason','')}_")
        lines.append("")
    except Exception as e:
        lines.append(f"## Cycle Evaluation\n\n⚠️ Evaluation failed: {e}\n")

    # ── 账户 ─────────────────────────────────────────────────────────────
    acct = trading.get_account()
    eq  = float(acct.equity)
    le  = float(acct.last_equity)
    cash = float(acct.cash)
    bp  = float(acct.buying_power)
    pnl_day = eq - le
    lines.append("## Account")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Equity | ${eq:,.2f} |")
    lines.append(f"| Cash | ${cash:,.2f} |")
    lines.append(f"| Buying Power | ${bp:,.2f} |")
    lines.append(f"| Daily P&L | ${pnl_day:+,.2f} |")
    lines.append("")

    # ── 股价 ─────────────────────────────────────────────────────────────
    # 盘后/周末 ask 经常返回 $0，单用 bid 会拿到 stale 残留价导致 ITM 误判。
    # 三层保护：①两侧报价都>0 ②价差<5% ③不满足时降级到最近日线收盘价。
    try:
        q = stk_data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym))
        bid = float(q[sym].bid_price or 0)
        ask = float(q[sym].ask_price or 0)
        stock_price = None
        if bid > 0 and ask > 0 and ask >= bid:
            if (ask - bid) / bid < 0.05:
                stock_price = round((bid + ask) / 2, 2)
        if stock_price is None:
            # Fallback: most recent daily close (works pre/post market + weekends)
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from datetime import timedelta
            bars = stk_data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                start=datetime.now() - timedelta(days=10),
            )).df
            if not bars.empty:
                stock_price = round(float(bars['close'].iloc[-1]), 2)
    except Exception:
        stock_price = None

    if stock_price:
        lines.append(f"## {sym} Current Price: ${stock_price:.2f}")
        lines.append("")

    # ── 期权持仓 ──────────────────────────────────────────────────────────
    # v12 修 (6/2): 用全账户的 option positions，不只 primary wheel symbol。
    # 否则邮件会漏报 secondary wheel 的持仓（如 GM wheel 时漏掉 MSFT）。
    all_account_positions = AlpacaClients.trading().get_all_positions()
    opt_positions = [
        p for p in all_account_positions
        if p.symbol and len(p.symbol) > 10  # OCC option symbol format
    ]
    if opt_positions:
        lines.append("## Option Positions")
        lines.append("")
        lines.append("| Contract | Type | Strike | Expiry | DTE | Qty | Cost | Current | P&L | Delta |")
        lines.append("|----------|------|--------|--------|-----|-----|------|---------|-----|-------|")
        for p in opt_positions:
            info = _parse_symbol(p.symbol)
            if not info:
                continue
            dte = (info["expiry"] - date.today()).days
            cost = float(p.avg_entry_price)
            cur  = float(p.current_price)
            pl   = float(p.unrealized_pl)
            typ  = "PUT" if info["type"] == "P" else "CALL"
            # get delta
            delta_str = "—"
            try:
                snap = opt_data.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=p.symbol)
                )[p.symbol]
                if snap.greeks and snap.greeks.delta is not None:
                    delta_str = f"{snap.greeks.delta:+.2f}"
            except Exception:
                pass
            lines.append(
                f"| `{p.symbol}` | {typ} | ${info['strike']:.2f} | {info['expiry']} | "
                f"{dte}d | {p.qty} | ${cost:.2f} | ${cur:.2f} | **${pl:+,.2f}** | {delta_str} |"
            )
        lines.append("")
        # 安全垫
        for p in opt_positions:
            info = _parse_symbol(p.symbol)
            if not info or stock_price is None:
                continue
            if info["type"] == "P":
                cushion = stock_price - info["strike"]
                status = "OTM ✓" if cushion > 0 else "ITM ⚠️"
                lines.append(f"- **{p.symbol}**: {status} | Cushion: ${cushion:+.2f}")
            else:
                cushion = info["strike"] - stock_price
                status = "OTM ✓" if cushion > 0 else "ITM ⚠️"
                lines.append(f"- **{p.symbol}**: {status} | Cushion: ${cushion:+.2f}")
        lines.append("")

    # ── 股票持仓 ──────────────────────────────────────────────────────────
    try:
        stock_pos = trading.get_open_position(sym)
        lines.append("## Stock Position")
        lines.append("")
        lines.append(f"- Shares: {stock_pos.qty}")
        lines.append(f"- Avg Cost: ${float(stock_pos.avg_entry_price):.2f}")
        lines.append(f"- Current: ${float(stock_pos.current_price):.2f}")
        lines.append(f"- Unrealized P&L: ${float(stock_pos.unrealized_pl):+,.2f}")
        lines.append("")
    except Exception:
        pass

    # ── 挂单 ─────────────────────────────────────────────────────────────
    open_orders = opt_mgr.get_open_option_orders(sym)
    if open_orders:
        lines.append("## Open Orders")
        lines.append("")
        lines.append("| Contract | Side | Qty | Limit | Status |")
        lines.append("|----------|------|-----|-------|--------|")
        for o in open_orders:
            side = str(o.side).split(".")[-1]
            lines.append(
                f"| `{o.symbol}` | {side} | {o.qty} | ${float(o.limit_price or 0):.2f} | {o.status} |"
            )
        lines.append("")

    # ── 历史收益汇总 (所有 wheel 标的, 不只当前) ────────────────────────────
    # OCC option symbol pattern: 6-char ticker (variable) + 6 digit yymmdd +
    # C/P + 8 digit strike. Detect by the 9th-from-end char being C/P and
    # last 8 chars being digits — works for any underlying in the universe.
    closed = trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200))
    sto_total = 0.0
    btc_total = 0.0
    trades = []
    for o in closed:
        if len(o.symbol) < 18:
            continue
        if o.symbol[-9] not in ("C", "P") or not o.symbol[-8:].isdigit():
            continue
        if o.filled_qty and float(o.filled_qty) > 0:
            price = float(o.filled_avg_price or 0)
            qty   = float(o.filled_qty)
            notional = price * qty * 100
            if str(o.side).endswith("SELL"):
                sto_total += notional
            else:
                btc_total += notional
            trades.append(o)

    lines.append("## Cumulative Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total Premium Collected (STO) | **${sto_total:,.2f}** |")
    lines.append(f"| Total Buy-to-Close Paid (BTC) | ${btc_total:,.2f} |")
    lines.append(f"| Realized Option P&L | **${sto_total - btc_total:+,.2f}** |")
    lines.append(f"| Option Trades | {len(trades)} |")
    lines.append("")

    # ── 所有 wheel 交易历史 (跨标的) ─────────────────────────────────────
    if trades:
        lines.append(f"## All Wheel Option Trades ({len(trades)})")
        lines.append("")
        lines.append("| Time (ET) | Contract | Side | Price | Qty |")
        lines.append("|-----------|----------|------|-------|-----|")
        for o in trades:
            t = o.filled_at.astimezone(ET).strftime("%m/%d %H:%M") if o.filled_at else "—"
            side = str(o.side).split(".")[-1]
            lines.append(
                f"| {t} | `{o.symbol}` | {side} | ${float(o.filled_avg_price or 0):.2f} | {o.filled_qty} |"
            )
        lines.append("")

    # ── 本周表现分析（仅收盘带，且仅周五）───────────────────────────────────
    if event == "close":
        try:
            from metrics.baseline_tracker import record_snapshot, format_weekly_report
            # 每日收盘记录一次快照
            record_snapshot()
            # 周五收盘附上周报和深度复盘
            if datetime.now(ET).weekday() == 4:
                lines.append(format_weekly_report())
                # 新增：本周复盘报告
                try:
                    from metrics.weekly_review import build_weekly_review
                    lines.append(build_weekly_review())
                except Exception as e:
                    lines.append(f"## Weekly Review\n\n❌ Review failed: {e}\n")
        except Exception as e:
            lines.append(f"## Weekly Stats\n\n❌ Tracker failed: {e}\n")

        # ── Assignment 复盘（有新事件才出现）──────────────────────────────
        try:
            from metrics.assignment_postmortem import (
                detect_and_analyze, get_pending_summaries,
            )
            detect_and_analyze(since_days=14)
            pending = get_pending_summaries(max_items=3)
            if pending:
                lines.append("# ⚖️ Assignment / Expiration Postmortems")
                lines.append("")
                lines.append("_Self-analysis of every option that got assigned or expired, "
                             "with lessons for next cycle._")
                lines.append("")
                for snippet in pending:
                    lines.append(snippet)
                    lines.append("")
                    lines.append("---")
                    lines.append("")
        except Exception as e:
            lines.append(f"_Postmortem module failed: {e}_\n")

    # ── 市场新闻（开盘和收盘都带）─────────────────────────────────────────
    if event in ("open", "close"):
        try:
            from market_news import build_market_news
            lines.append(build_market_news())
        except Exception as e:
            lines.append(f"## Market News\n\n❌ News fetch failed: {e}\n")

    # ── 健康检查 + 候选扫描（开盘和收盘都带）───────────────────────────────
    if event in ("open", "close"):
        try:
            from wheel_health_check import run_health_check, format_markdown
            health = run_health_check(sym)
            lines.append(format_markdown(health))
        except Exception as e:
            lines.append(f"## Suitability Check\n\n❌ Health check failed: {e}\n")

        try:
            # NOTE: LEAPS Buy Call section was removed — bot doesn't trade
            # LEAPS, the recommendations were informational noise. Keep the
            # function importable in case it's wanted back later.
            from wheel_scanner import (
                scan_wheel_alternatives, scan_csp_candidates,
                format_wheel_alternatives_md, format_csp_md,
            )
            alt = scan_wheel_alternatives(exclude=sym, top_n=2)
            lines.append(format_wheel_alternatives_md(alt))
            csp = scan_csp_candidates(top_n=3)
            lines.append(format_csp_md(csp))
        except Exception as e:
            lines.append(f"## Candidate Scan\n\n❌ Scan failed: {e}\n")

    lines.append("---")
    lines.append("_Automated by GitHub Actions — Wheel Strategy Bot_")

    return "\n".join(lines)


if __name__ == "__main__":
    event = sys.argv[1] if len(sys.argv) > 1 else "update"
    md = build_summary(event)
    print(md)
    # 同时写入文件供工作流使用
    with open("summary.md", "w", encoding="utf-8") as f:
        f.write(md)
