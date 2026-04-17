"""市场新闻摘要 — 科技 / 政治政策 / PLTR / UNH

用于每日开盘/收盘邮件。数据源：Alpaca News API（Benzinga 等）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from config import settings

ET = ZoneInfo("America/New_York")

# 科技板块
TECH_SYMBOLS = ["NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA", "AMD", "AVGO"]

# 政策敏感标的（UNH=医保政策, PLTR=国防/政府合同, XOM=能源政策, LMT=国防）
POLICY_SENSITIVE_SYMBOLS = ["UNH", "PLTR", "LMT", "XOM", "SPY"]


def _fetch(symbols: list[str], hours: int = 24, limit: int = 15) -> list:
    """获取最近 N 小时的新闻"""
    try:
        client = NewsClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
        req = NewsRequest(
            symbols=",".join(symbols) if isinstance(symbols, list) else symbols,
            start=datetime.now(timezone.utc) - timedelta(hours=hours),
            limit=limit,
        )
        result = client.get_news(req)
        if hasattr(result, "data"):
            return result.data.get("news", [])
        return []
    except Exception:
        return []


def _format_section(title: str, articles: list, max_items: int = 5) -> str:
    """格式化新闻章节为 Markdown"""
    lines = [f"### {title}"]
    lines.append("")
    if not articles:
        lines.append("_No recent news._")
        return "\n".join(lines)

    # 去重（按 headline）
    seen = set()
    unique = []
    for a in articles:
        hl = a.headline if hasattr(a, "headline") else a.get("headline", "")
        if hl and hl not in seen:
            seen.add(hl)
            unique.append(a)

    for art in unique[:max_items]:
        headline = art.headline if hasattr(art, "headline") else art.get("headline", "")
        url = art.url if hasattr(art, "url") else art.get("url", "")
        source = art.source if hasattr(art, "source") else art.get("source", "")
        summary = art.summary if hasattr(art, "summary") else art.get("summary", "")
        syms = art.symbols if hasattr(art, "symbols") else art.get("symbols", [])
        ts = art.created_at if hasattr(art, "created_at") else art.get("created_at", "")

        # 时间
        if hasattr(ts, "strftime"):
            time_str = ts.astimezone(ET).strftime("%m/%d %H:%M ET")
        else:
            time_str = str(ts)[:16]

        # 相关标的
        syms_str = ", ".join(syms[:5]) if syms else ""

        # 摘要（截断）
        summary_short = (summary[:200] + "...") if summary and len(summary) > 200 else (summary or "")

        lines.append(f"**[{headline}]({url})**")
        lines.append(f"_{source} · {time_str} · {syms_str}_")
        if summary_short.strip():
            lines.append(f"> {summary_short.strip()}")
        lines.append("")

    return "\n".join(lines)


def build_market_news() -> str:
    """生成完整的市场新闻摘要 Markdown"""
    lines = ["## Market News Summary (last 24h)", ""]

    # 1. 科技板块
    tech = _fetch(TECH_SYMBOLS, hours=24, limit=20)
    lines.append(_format_section("🖥️ Tech Sector", tech, max_items=5))
    lines.append("")

    # 2. 政策 & 宏观
    policy = _fetch(POLICY_SENSITIVE_SYMBOLS, hours=48, limit=20)
    lines.append(_format_section("🏛️ Policy & Macro (Healthcare / Defense / Energy)", policy, max_items=5))
    lines.append("")

    # 3. PLTR 专项
    pltr = _fetch(["PLTR"], hours=72, limit=10)
    lines.append(_format_section("🛡️ PLTR (Palantir)", pltr, max_items=3))
    lines.append("")

    # 4. UNH 专项
    unh = _fetch(["UNH"], hours=72, limit=10)
    lines.append(_format_section("🏥 UNH (UnitedHealth)", unh, max_items=3))
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(build_market_news())
