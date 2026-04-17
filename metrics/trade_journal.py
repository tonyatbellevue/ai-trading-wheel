"""交易决策日志 — 记录每笔交易的入场/出场及原因

为什么需要：要提升胜率，必须知道哪些过滤器在赚钱、哪些在亏钱。
本模块记录每笔交易的"为什么"，供周报分析使用。

数据流：
    wheel_strategy.run_cycle()
        ├─ 卖 Put → log_entry(action="sell_put", filters=...)
        ├─ 卖 Call → log_entry(action="sell_call", filters=...)
        ├─ 跳过过滤 → log_skip(reason=...)
        └─ 平仓/到期 → log_exit(pnl=...)

CSV 字段（metrics/data/trade_journal.csv）：
    event_at, event_type, symbol, action, contract, strike, expiry,
    qty, premium, delta, iv, dte,
    cash_before, equity_before,
    filter_earnings, filter_ma_trend, filter_vol, filter_iv_rank,
    market_vix, market_spy_above_200ma,
    skip_reason, pnl, exit_reason, notes
"""
from __future__ import annotations
import os
import csv
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from loguru import logger
from config import settings

JOURNAL_DIR = Path(settings.BASE_DIR) / "metrics" / "data"
JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
JOURNAL_CSV = JOURNAL_DIR / "trade_journal.csv"

FIELDS = [
    "event_at", "event_type", "symbol", "action", "contract",
    "strike", "expiry", "qty", "premium", "delta", "iv", "dte",
    "cash_before", "equity_before",
    "filter_earnings", "filter_ma_trend", "filter_vol", "filter_iv_rank",
    "market_vix", "market_spy_above_200ma",
    "skip_reason", "pnl", "exit_reason", "notes",
]


def _append(row: dict) -> None:
    """追加一行到 journal CSV"""
    # 填充缺失字段
    full = {f: row.get(f, "") for f in FIELDS}
    full["event_at"] = full.get("event_at") or datetime.now().isoformat()

    header_needed = not JOURNAL_CSV.exists()
    with open(JOURNAL_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if header_needed:
            w.writeheader()
        w.writerow(full)


def _market_context() -> dict:
    """获取当前市场上下文（VIX、SPY 是否在 200MA 上）"""
    ctx = {"market_vix": "", "market_spy_above_200ma": ""}
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta

        stk = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
        # VIX
        try:
            vix_quote = stk.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols="^VIX"))
            ctx["market_vix"] = round(float(vix_quote["^VIX"].ask_price or vix_quote["^VIX"].bid_price), 2)
        except Exception:
            # ^VIX 可能不可用，用 VIXY ETF 代替（粗略）
            try:
                vixy = stk.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols="VIXY"))
                ctx["market_vix"] = f"VIXY={float(vixy['VIXY'].ask_price):.2f}"
            except Exception:
                pass

        # SPY 200MA
        try:
            bars = stk.get_stock_bars(StockBarsRequest(
                symbol_or_symbols="SPY",
                timeframe=TimeFrame.Day,
                start=date.today() - timedelta(days=300),
            )).data.get("SPY", [])
            if len(bars) >= 200:
                closes = [float(b.close) for b in bars[-200:]]
                sma200 = sum(closes) / 200
                current = float(bars[-1].close)
                ctx["market_spy_above_200ma"] = "1" if current >= sma200 else "0"
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"市场上下文采集失败: {e}")
    return ctx


def _account_context() -> dict:
    """获取当前账户上下文"""
    try:
        from core.alpaca_client import AlpacaClients
        acct = AlpacaClients.trading().get_account()
        return {
            "cash_before": round(float(acct.cash), 2),
            "equity_before": round(float(acct.equity), 2),
        }
    except Exception:
        return {}


# ── 三个公开 API ─────────────────────────────────────────────────────────

def log_entry(
    symbol: str,
    action: str,                # "sell_put" | "sell_call"
    contract: str,
    strike: float,
    expiry: str,
    qty: int,
    premium: float,
    delta: Optional[float] = None,
    iv: Optional[float] = None,
    dte: Optional[int] = None,
    filters_passed: Optional[dict] = None,
    notes: str = "",
) -> None:
    """记录新开仓"""
    row = {
        "event_type": "entry",
        "symbol": symbol,
        "action": action,
        "contract": contract,
        "strike": strike,
        "expiry": expiry,
        "qty": qty,
        "premium": premium,
        "delta": delta or "",
        "iv": iv or "",
        "dte": dte or "",
        "notes": notes,
    }
    if filters_passed:
        for k, v in filters_passed.items():
            row[f"filter_{k}"] = "PASS" if v else "FAIL"
    row.update(_account_context())
    row.update(_market_context())
    _append(row)
    logger.info(f"[Journal] 记录开仓: {action} {contract} qty={qty} premium=${premium:.2f}")


def log_skip(symbol: str, action: str, skip_reason: str, filter_name: str = "") -> None:
    """记录因过滤器跳过"""
    row = {
        "event_type": "skip",
        "symbol": symbol,
        "action": action,
        "skip_reason": skip_reason,
        "notes": f"filter={filter_name}" if filter_name else "",
    }
    row.update(_account_context())
    row.update(_market_context())
    _append(row)
    logger.info(f"[Journal] 记录跳过: {action} {symbol} 因 {skip_reason}")


def log_exit(
    symbol: str,
    contract: str,
    qty: int,
    pnl: float,
    exit_reason: str,             # "expired_otm" | "assigned" | "closed_btc" | "manual"
    notes: str = "",
) -> None:
    """记录平仓/到期"""
    row = {
        "event_type": "exit",
        "symbol": symbol,
        "contract": contract,
        "qty": qty,
        "pnl": round(pnl, 2),
        "exit_reason": exit_reason,
        "notes": notes,
    }
    row.update(_account_context())
    _append(row)
    logger.info(f"[Journal] 记录平仓: {contract} pnl=${pnl:+.2f} ({exit_reason})")


# ── 分析函数（供周报使用） ─────────────────────────────────────────────

def read_journal() -> list[dict]:
    if not JOURNAL_CSV.exists():
        return []
    with open(JOURNAL_CSV, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def delta_calibration_report(lookback_days: int = 180) -> dict:
    """Compare what the model **assumed** (delta → P(assignment)) with what
    actually happened. If we targeted 0.25-delta puts and our real OTM rate
    over the past N days is 82%, then the market was pricing 32% assignment
    but we only saw 18% — we can afford to sell closer to the money (more
    premium) without increasing real risk. Inverse also holds.

    Returns:
        {
            "lookback_days": int,
            "total_closed": int,
            "by_delta_bucket": {
                "0.10-0.15": {"assumed_wr": 0.85, "actual_wr": 0.XX, "n": N},
                "0.15-0.20": {...},
                "0.20-0.25": {...},
                "0.25-0.30": {...},
            },
            "overall_assumed_wr": float,
            "overall_actual_wr": float,
            "delta_drift": float,         # + means we're safer than delta said
            "recommendation": str,
        }
    """
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=lookback_days)
    rows = read_journal()

    # Pair each entry with its matching exit (by contract symbol)
    entries = {r["contract"]: r for r in rows
               if r["event_type"] == "entry" and r.get("action") == "sell_put"
               and datetime.fromisoformat(r["event_at"]) >= cutoff}
    exits = {r["contract"]: r for r in rows
             if r["event_type"] == "exit" and r["contract"] in entries}

    buckets = {
        "0.10-0.15": {"otm_wins": 0, "total": 0, "assumed_wr": 0.875},
        "0.15-0.20": {"otm_wins": 0, "total": 0, "assumed_wr": 0.825},
        "0.20-0.25": {"otm_wins": 0, "total": 0, "assumed_wr": 0.775},
        "0.25-0.30": {"otm_wins": 0, "total": 0, "assumed_wr": 0.725},
        "0.30+":     {"otm_wins": 0, "total": 0, "assumed_wr": 0.65},
    }

    def bucket_for(delta: float) -> str | None:
        d = abs(delta)
        if 0.10 <= d < 0.15: return "0.10-0.15"
        if 0.15 <= d < 0.20: return "0.15-0.20"
        if 0.20 <= d < 0.25: return "0.20-0.25"
        if 0.25 <= d < 0.30: return "0.25-0.30"
        if d >= 0.30:        return "0.30+"
        return None

    all_wins = 0
    all_total = 0
    for contract, entry in entries.items():
        exit_row = exits.get(contract)
        if not exit_row:
            continue   # still open
        try:
            delta = float(entry.get("delta") or 0)
        except (ValueError, TypeError):
            continue
        b = bucket_for(delta)
        if not b:
            continue
        buckets[b]["total"] += 1
        all_total += 1
        if exit_row.get("exit_reason") in ("expired_otm", "btc_50pct_profit"):
            buckets[b]["otm_wins"] += 1
            all_wins += 1

    # Fill actual win rates
    for b in buckets.values():
        b["actual_wr"] = (b["otm_wins"] / b["total"]) if b["total"] else None

    overall_assumed = 0.75        # implied by target_delta = 0.25
    overall_actual = (all_wins / all_total) if all_total else None
    drift = (overall_actual - overall_assumed) if overall_actual is not None else 0

    if all_total < 10:
        rec = f"样本仅 {all_total} 笔，至少 10 笔后再做推断"
    elif drift > 0.05:
        rec = (f"真实胜率 {overall_actual:.0%} > 假设 {overall_assumed:.0%}；"
               f"delta 可加激进到 0.30 多拿权利金（回测后再上）")
    elif drift < -0.05:
        rec = (f"真实胜率 {overall_actual:.0%} < 假设 {overall_assumed:.0%}；"
               f"delta 降到 0.15 收紧风险")
    else:
        rec = "真实胜率与假设吻合，保持当前 delta 设定"

    return {
        "lookback_days": lookback_days,
        "total_closed": all_total,
        "by_delta_bucket": buckets,
        "overall_assumed_wr": overall_assumed,
        "overall_actual_wr": overall_actual,
        "delta_drift": drift,
        "recommendation": rec,
    }


def format_delta_calibration_md(lookback_days: int = 180) -> str:
    r = delta_calibration_report(lookback_days)
    lines = [f"## 📏 Delta 胜率校准 (最近 {lookback_days} 天)", ""]
    n = r["total_closed"]
    if n == 0:
        lines.append("_没有已平仓的 put 交易，无法校准。_")
        return "\n".join(lines)

    actual = r["overall_actual_wr"]
    assumed = r["overall_assumed_wr"]
    drift = r["delta_drift"]

    lines.append(f"- 样本: **{n}** 笔已平仓 put")
    lines.append(f"- Delta 0.25 假设胜率: **{assumed:.0%}**")
    lines.append(f"- 真实 OTM 胜率:       **{actual:.0%}**" if actual is not None else "- 真实胜率: n/a")
    if actual is not None:
        arrow = "↑" if drift > 0 else ("↓" if drift < 0 else "=")
        lines.append(f"- 偏离:                {arrow} {drift:+.0%}")
    lines.append("")

    lines.append("| Delta 区间 | 假设胜率 | 真实胜率 | 样本 |")
    lines.append("|---|---|---|---|")
    for b_name, b in r["by_delta_bucket"].items():
        actual_wr = b["actual_wr"]
        actual_str = f"{actual_wr:.0%}" if actual_wr is not None else "—"
        lines.append(f"| {b_name} | {b['assumed_wr']:.0%} | {actual_str} | {b['total']} |")
    lines.append("")
    lines.append(f"**建议**: {r['recommendation']}")
    lines.append("")
    return "\n".join(lines)


def filter_contribution_analysis(days: int = 30) -> dict:
    """分析每个过滤器的贡献：跳过的次数 + 通过后的胜率

    返回：
      {
        "earnings": {"skipped": N, "passed_wins": M, "passed_total": K, "win_rate": pct},
        "ma_trend": ...,
        ...
      }
    """
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)
    rows = read_journal()
    rows = [r for r in rows if datetime.fromisoformat(r["event_at"]) >= cutoff]

    filters = ["earnings", "ma_trend", "vol", "iv_rank"]
    result = {}

    for f in filters:
        col = f"filter_{f}"
        skipped = sum(1 for r in rows if r["event_type"] == "skip" and f in r.get("notes", ""))
        passed_entries = [r for r in rows if r["event_type"] == "entry" and r.get(col) == "PASS"]

        # 关联出场
        wins = 0
        total_closed = 0
        for entry in passed_entries:
            for exit_row in rows:
                if exit_row["event_type"] == "exit" and exit_row["contract"] == entry["contract"]:
                    total_closed += 1
                    if float(exit_row.get("pnl") or 0) > 0:
                        wins += 1
                    break

        result[f] = {
            "skipped": skipped,
            "passed_total": len(passed_entries),
            "closed": total_closed,
            "wins": wins,
            "win_rate": (wins / total_closed) if total_closed > 0 else 0.0,
        }
    return result


def format_journal_md(days: int = 7) -> str:
    """格式化最近 N 天交易日志为 Markdown（用于周报）"""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)
    rows = read_journal()
    rows = [r for r in rows if datetime.fromisoformat(r["event_at"]) >= cutoff]

    if not rows:
        return f"## 📓 交易日志（最近 {days} 天）\n\n暂无记录。\n"

    entries = [r for r in rows if r["event_type"] == "entry"]
    exits = [r for r in rows if r["event_type"] == "exit"]
    skips = [r for r in rows if r["event_type"] == "skip"]

    lines = [
        f"## 📓 交易日志（最近 {days} 天）",
        "",
        f"- 开仓: {len(entries)} 次",
        f"- 平仓: {len(exits)} 次",
        f"- 因过滤器跳过: {len(skips)} 次",
        "",
    ]

    if exits:
        wins = sum(1 for r in exits if float(r.get("pnl") or 0) > 0)
        total_pnl = sum(float(r.get("pnl") or 0) for r in exits)
        lines += [
            f"### 平仓表现",
            f"- 胜率: {wins}/{len(exits)} ({wins/len(exits):.0%})",
            f"- 净 P&L: ${total_pnl:+,.2f}",
            "",
        ]

    # 过滤器贡献分析
    fc = filter_contribution_analysis(days=days)
    has_data = any(v["passed_total"] > 0 or v["skipped"] > 0 for v in fc.values())
    if has_data:
        lines += [
            "### 过滤器贡献",
            "",
            "| 过滤器 | 跳过次数 | 通过后开仓 | 已平仓 | 胜率 |",
            "|--------|---------|----------|--------|------|",
        ]
        for fname, stats in fc.items():
            lines.append(
                f"| {fname} | {stats['skipped']} | {stats['passed_total']} | "
                f"{stats['closed']} | {stats['win_rate']:.0%} |"
            )
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print(format_journal_md(days=30))
