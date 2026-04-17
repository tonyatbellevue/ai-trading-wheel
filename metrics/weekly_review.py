"""周五收盘"本周复盘"自动生成 — 总结本周交易决策与改进建议

生成内容：
  1. 本周总览：开仓/平仓/胜率/P&L
  2. 最佳交易 / 最差交易（带原因分析）
  3. 过滤器表现：哪些救了你 / 哪些误杀了
  4. 持仓状态：当前持仓的下周到期/Delta 概览
  5. 下周关注清单：财报日历、当前 Wheel 标的的关键水位

仅在周五（weekday=4）由 wheel_summary.py 调用。
"""
from __future__ import annotations
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from config import settings

ET = ZoneInfo("America/New_York")


def _read_journal() -> list:
    try:
        from metrics.trade_journal import read_journal
        return read_journal()
    except Exception:
        return []


def _read_snapshots() -> list:
    try:
        from metrics.baseline_tracker import _read_csv, DAILY_CSV
        return _read_csv(DAILY_CSV)
    except Exception:
        return []


def _filter_this_week(rows: list, date_key: str) -> list:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    out = []
    for r in rows:
        try:
            d = datetime.fromisoformat(r[date_key].split("T")[0]).date()
            if monday <= d <= today:
                out.append(r)
        except Exception:
            pass
    return out


def _best_worst_trades(exits: list, top_n: int = 2) -> tuple[list, list]:
    """根据 P&L 排序"""
    sorted_exits = sorted(exits, key=lambda r: float(r.get("pnl") or 0), reverse=True)
    return sorted_exits[:top_n], sorted_exits[-top_n:][::-1] if len(sorted_exits) >= top_n else sorted_exits[:top_n]


def _format_trade(r: dict) -> str:
    """单笔交易格式化"""
    pnl = float(r.get("pnl") or 0)
    return (f"`{r.get('contract','?')}` qty={r.get('qty','?')} "
            f"P&L=${pnl:+,.2f} ({r.get('exit_reason','?')})")


def _improvement_suggestions(journal: list, exits: list) -> list[str]:
    """根据本周数据生成改进建议"""
    suggestions = []

    if not exits:
        suggestions.append("ℹ️ 本周无平仓交易，建议下周观察 Roll 时机")
        return suggestions

    wins = [r for r in exits if float(r.get("pnl") or 0) > 0]
    losses = [r for r in exits if float(r.get("pnl") or 0) < 0]
    win_rate = len(wins) / len(exits) if exits else 0

    if win_rate < 0.7 and len(exits) >= 3:
        suggestions.append(
            f"⚠️ 本周胜率 {win_rate:.0%} 低于 70% 目标，下周建议："
            f"提高 Delta 阈值 (从 0.25 → 0.20) 远离行权价"
        )

    # 检查是否有"被行权"的 Put（assigned）
    assigned = [r for r in exits if r.get("exit_reason") == "assigned"]
    if assigned:
        suggestions.append(
            f"📌 本周被行权 {len(assigned)} 次。建议启用 Roll 机制以提前避免接货"
        )

    # 过滤器跳过太多
    skips = [r for r in journal if r["event_type"] == "skip"]
    if len(skips) > 5:
        suggestions.append(
            f"ℹ️ 本周过滤器跳过 {len(skips)} 次（{len(skips)} > 5），"
            f"可能错失机会，下周复盘 skip_reason"
        )

    if not suggestions:
        suggestions.append("✅ 本周表现稳健，继续当前策略")

    return suggestions


def build_weekly_review() -> str:
    """生成本周复盘 Markdown，供周五收盘邮件使用"""
    today = date.today()
    weekday = today.weekday()  # 0=Mon, 4=Fri
    monday = today - timedelta(days=weekday)

    journal = _filter_this_week(_read_journal(), "event_at")
    snapshots = _filter_this_week(_read_snapshots(), "date")

    entries = [r for r in journal if r["event_type"] == "entry"]
    exits = [r for r in journal if r["event_type"] == "exit"]
    skips = [r for r in journal if r["event_type"] == "skip"]

    lines = [
        "## 🔬 本周复盘报告",
        "",
        f"**周期：** {monday} ~ {today}",
        "",
    ]

    # ── 总览 ──
    if exits:
        wins = sum(1 for r in exits if float(r.get("pnl") or 0) > 0)
        total_pnl = sum(float(r.get("pnl") or 0) for r in exits)
        avg_pnl = total_pnl / len(exits)
        lines += [
            "### 总览",
            "",
            f"| 指标 | 值 |",
            f"|------|-----|",
            f"| 本周开仓 | {len(entries)} 次 |",
            f"| 本周平仓 | {len(exits)} 次 |",
            f"| 胜率 | {wins}/{len(exits)} ({wins/len(exits):.0%}) |",
            f"| 净 P&L | ${total_pnl:+,.2f} |",
            f"| 平均每笔 | ${avg_pnl:+,.2f} |",
            f"| 因过滤器跳过 | {len(skips)} 次 |",
            "",
        ]
    else:
        lines += [
            "### 总览",
            "",
            f"本周无平仓交易（{len(entries)} 个开仓持仓中，{len(skips)} 次过滤跳过）",
            "",
        ]

    # ── 最佳/最差交易 ──
    if len(exits) >= 2:
        best, worst = _best_worst_trades(exits, top_n=2)
        lines += ["### 最佳交易", ""]
        for r in best:
            lines.append(f"- 🏆 {_format_trade(r)}")
        lines += ["", "### 最差交易", ""]
        for r in worst:
            lines.append(f"- ⚠️ {_format_trade(r)}")
        lines.append("")

    # ── 过滤器贡献 ──
    try:
        from metrics.trade_journal import filter_contribution_analysis
        fc = filter_contribution_analysis(days=7)
        has_data = any(v["passed_total"] > 0 or v["skipped"] > 0 for v in fc.values())
        if has_data:
            lines += [
                "### 过滤器表现（本周）",
                "",
                "| 过滤器 | 救了你（跳过） | 通过后胜率 |",
                "|--------|---------------|-----------|",
            ]
            for fname, stats in fc.items():
                wr = f"{stats['win_rate']:.0%} ({stats['wins']}/{stats['closed']})" if stats['closed'] else "-"
                lines.append(f"| {fname} | {stats['skipped']} | {wr} |")
            lines.append("")
    except Exception:
        pass

    # ── 当前持仓快照 ──
    try:
        from core.alpaca_client import AlpacaClients
        positions = AlpacaClients.trading().get_all_positions()
        opt_positions = [p for p in positions if "P" in p.symbol or "C" in p.symbol]
        if opt_positions:
            lines += ["### 当前期权持仓", ""]
            for p in opt_positions[:5]:
                lines.append(f"- `{p.symbol}` qty={p.qty} | 未实现 ${float(p.unrealized_pl):+,.2f}")
            lines.append("")
    except Exception:
        pass

    # ── 改进建议 ──
    suggestions = _improvement_suggestions(journal, exits)
    lines += ["### 下周改进建议", ""]
    for s in suggestions:
        lines.append(f"- {s}")
    lines.append("")

    # ── Shadow Rotation 对照 ──
    try:
        from metrics.shadow_rotation import format_shadow_report
        shadow_md = format_shadow_report()
        if shadow_md:
            lines.append(shadow_md)
    except Exception as e:
        lines.append(f"_Shadow 报告生成失败: {e}_")

    # ── 30 天进化计划进度 ──
    plan_day = (today - date(2026, 4, 16)).days  # Day 0 = 4/16
    plan_week = plan_day // 7 + 1
    if 1 <= plan_week <= 5:
        lines += [
            f"### 30 天进化计划进度",
            "",
            f"当前在 **Week {plan_week}** (Day {plan_day})",
            "",
        ]

    return "\n".join(lines)


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print(build_weekly_review())
