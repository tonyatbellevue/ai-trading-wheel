"""胜率基线追踪器 — 每日记录账户表现，累计周度/月度胜率统计

用法：
    from metrics.baseline_tracker import record_snapshot, get_weekly_stats
    record_snapshot()       # 每日收盘后调用
    stats = get_weekly_stats()   # 返回本周/上周对比
"""
from __future__ import annotations
import os
import csv
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from core.alpaca_client import AlpacaClients
from config import settings

METRICS_DIR = Path(settings.BASE_DIR) / "metrics" / "data"
METRICS_DIR.mkdir(parents=True, exist_ok=True)

DAILY_CSV = METRICS_DIR / "daily_snapshot.csv"
TRADES_CSV = METRICS_DIR / "closed_trades.csv"


# ── 每日快照 ────────────────────────────────────────────────────────────
def record_snapshot() -> dict:
    """记录账户每日快照 — 权益、现金、持仓、P&L"""
    trading = AlpacaClients.trading()
    acct = trading.get_account()

    today = date.today().isoformat()
    snapshot = {
        "date": today,
        "symbol": settings.WHEEL_SYMBOL,
        "equity": float(acct.equity),
        "last_equity": float(acct.last_equity),
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
        "daily_pl": float(acct.equity) - float(acct.last_equity),
        "daily_pl_pct": (float(acct.equity) - float(acct.last_equity)) / float(acct.last_equity) if float(acct.last_equity) > 0 else 0,
    }

    # 追加到 CSV
    header_needed = not DAILY_CSV.exists()
    with open(DAILY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=snapshot.keys())
        if header_needed:
            w.writeheader()
        w.writerow(snapshot)

    logger.info(f"已记录每日快照: equity=${snapshot['equity']:.2f}, pl={snapshot['daily_pl']:+.2f}")
    return snapshot


# ── 交易记录 ────────────────────────────────────────────────────────────
def record_closed_trade(symbol: str, side: str, contract: str, entry_premium: float,
                         exit_premium: float, qty: int, pnl: float, is_win: bool,
                         notes: str = "") -> None:
    """记录一笔平仓交易"""
    row = {
        "closed_at": datetime.now().isoformat(),
        "symbol": symbol,
        "side": side,            # STO / BTC / assigned
        "contract": contract,
        "entry_premium": entry_premium,
        "exit_premium": exit_premium,
        "qty": qty,
        "pnl": pnl,
        "is_win": 1 if is_win else 0,
        "notes": notes,
    }

    header_needed = not TRADES_CSV.exists()
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if header_needed:
            w.writeheader()
        w.writerow(row)


# ── 读取历史 ────────────────────────────────────────────────────────────
def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _daterange(days_back: int) -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=days_back)
    return start, end


# ── 周度统计 ────────────────────────────────────────────────────────────
def get_weekly_stats() -> dict:
    """本周 vs 上周对比"""
    snapshots = _read_csv(DAILY_CSV)
    trades = _read_csv(TRADES_CSV)

    today = date.today()
    this_week_start = today - timedelta(days=today.weekday())  # 本周一
    last_week_start = this_week_start - timedelta(days=7)

    def filter_period(rows: list, start: date, end: date, date_key: str) -> list:
        result = []
        for r in rows:
            try:
                d = datetime.fromisoformat(r[date_key].split("T")[0]).date()
                if start <= d < end:
                    result.append(r)
            except Exception:
                continue
        return result

    # 权益变化
    this_week_snaps = filter_period(snapshots, this_week_start, today + timedelta(days=1), "date")
    last_week_snaps = filter_period(snapshots, last_week_start, this_week_start, "date")

    def equity_change(snaps: list) -> tuple[float, float]:
        if not snaps:
            return 0.0, 0.0
        start_eq = float(snaps[0].get("last_equity", snaps[0].get("equity", 0)))
        end_eq = float(snaps[-1]["equity"])
        change = end_eq - start_eq
        pct = change / start_eq if start_eq > 0 else 0
        return change, pct

    this_change, this_pct = equity_change(this_week_snaps)
    last_change, last_pct = equity_change(last_week_snaps)

    # 交易胜率
    this_week_trades = filter_period(trades, this_week_start, today + timedelta(days=1), "closed_at")
    last_week_trades = filter_period(trades, last_week_start, this_week_start, "closed_at")

    def win_rate(ts: list) -> tuple[float, int, int]:
        if not ts:
            return 0.0, 0, 0
        wins = sum(1 for t in ts if int(t.get("is_win", 0)) == 1)
        return wins / len(ts), wins, len(ts)

    this_wr, this_wins, this_total = win_rate(this_week_trades)
    last_wr, last_wins, last_total = win_rate(last_week_trades)

    # 累计 P&L
    def sum_pnl(ts: list) -> float:
        return sum(float(t.get("pnl", 0)) for t in ts)

    this_pnl = sum_pnl(this_week_trades)
    last_pnl = sum_pnl(last_week_trades)

    return {
        "this_week": {
            "start": this_week_start.isoformat(),
            "equity_change": this_change,
            "equity_pct": this_pct,
            "win_rate": this_wr,
            "wins": this_wins,
            "total_trades": this_total,
            "realized_pnl": this_pnl,
        },
        "last_week": {
            "start": last_week_start.isoformat(),
            "equity_change": last_change,
            "equity_pct": last_pct,
            "win_rate": last_wr,
            "wins": last_wins,
            "total_trades": last_total,
            "realized_pnl": last_pnl,
        },
        "delta": {
            "equity_pct": this_pct - last_pct,
            "win_rate": this_wr - last_wr,
            "realized_pnl": this_pnl - last_pnl,
        },
    }


# ── 月度统计 ────────────────────────────────────────────────────────────
def get_monthly_stats() -> dict:
    """最近 30 天累计"""
    snapshots = _read_csv(DAILY_CSV)
    trades = _read_csv(TRADES_CSV)
    if not snapshots:
        return {"total_trades": 0, "win_rate": 0, "equity_pct": 0, "realized_pnl": 0}

    start, end = _daterange(30)
    def in_range(r, key):
        try:
            d = datetime.fromisoformat(r[key].split("T")[0]).date()
            return start <= d <= end
        except Exception:
            return False

    month_snaps = [s for s in snapshots if in_range(s, "date")]
    month_trades = [t for t in trades if in_range(t, "closed_at")]

    if not month_snaps:
        return {"total_trades": len(month_trades), "win_rate": 0, "equity_pct": 0, "realized_pnl": 0}

    start_eq = float(month_snaps[0].get("last_equity", month_snaps[0]["equity"]))
    end_eq = float(month_snaps[-1]["equity"])
    equity_pct = (end_eq - start_eq) / start_eq if start_eq > 0 else 0

    wins = sum(1 for t in month_trades if int(t.get("is_win", 0)) == 1)
    win_rate = wins / len(month_trades) if month_trades else 0
    realized = sum(float(t.get("pnl", 0)) for t in month_trades)

    # 最大回撤
    equities = [float(s["equity"]) for s in month_snaps]
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return {
        "total_trades": len(month_trades),
        "wins": wins,
        "win_rate": win_rate,
        "equity_pct": equity_pct,
        "realized_pnl": realized,
        "max_drawdown": max_dd,
    }


# ── Markdown 格式输出 ──────────────────────────────────────────────────
def format_weekly_report() -> str:
    """生成周报 Markdown（用于周五收盘邮件）"""
    w = get_weekly_stats()
    m = get_monthly_stats()

    this = w["this_week"]
    last = w["last_week"]
    delta = w["delta"]

    def fmt_pct(x): return f"{x:+.2%}"
    def fmt_usd(x): return f"${x:+,.2f}"

    lines = [
        "## 📊 本周表现分析",
        "",
        "| 指标 | 本周 | 上周 | 变化 |",
        "|------|------|------|------|",
        f"| 权益变化 | {fmt_usd(this['equity_change'])} ({fmt_pct(this['equity_pct'])}) | "
            f"{fmt_usd(last['equity_change'])} ({fmt_pct(last['equity_pct'])}) | "
            f"{fmt_pct(delta['equity_pct'])} |",
        f"| 胜率 | {this['win_rate']:.0%} ({this['wins']}/{this['total_trades']}) | "
            f"{last['win_rate']:.0%} ({last['wins']}/{last['total_trades']}) | "
            f"{delta['win_rate']:+.0%} |",
        f"| 已实现 P&L | {fmt_usd(this['realized_pnl'])} | {fmt_usd(last['realized_pnl'])} | "
            f"{fmt_usd(delta['realized_pnl'])} |",
        "",
        "### 📈 最近 30 天",
        "",
        f"- 总交易数: {m['total_trades']}",
        f"- 胜率: {m.get('win_rate', 0):.0%} ({m.get('wins', 0)} 胜)",
        f"- 权益变化: {fmt_pct(m.get('equity_pct', 0))}",
        f"- 已实现 P&L: {fmt_usd(m.get('realized_pnl', 0))}",
        f"- 最大回撤: {m.get('max_drawdown', 0):.1%}",
        "",
    ]

    # 目标达成提醒
    if m.get("win_rate", 0) >= 0.80 and m.get("total_trades", 0) >= 5:
        lines.append("✅ **月度胜率目标达成（≥80%）**")
    elif m.get("total_trades", 0) >= 5:
        gap = 0.80 - m["win_rate"]
        lines.append(f"⚠️ 距离月度胜率目标 80% 还差 {gap:.0%}")

    lines.append("")

    # 集成交易日志分析（过滤器贡献）
    try:
        from metrics.trade_journal import format_journal_md
        lines.append(format_journal_md(days=7))
    except Exception as e:
        lines.append(f"_交易日志加载失败: {e}_\n")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    snap = record_snapshot()
    print(f"\n今日快照: {snap}\n")
    print(format_weekly_report())
