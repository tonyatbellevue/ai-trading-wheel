"""Dashboard 风格邮件 HTML 渲染器 v2 — 真正的 trading dashboard 美学

改进：
- 顶部 hero card：渐变 + 大标题 + KPI ribbon
- 自动识别"账户"区块为大尺寸 KPI 卡片（带迷你趋势/箭头）
- 表格：sticky header + zebra + hover + 状态色 chip
- Phase 大徽章
- Suitability verdict 巨大彩色 badge
- 持仓 cushion 进度条可视化
- 字体：Inter 系列 + SF Mono 数字
- 圆角更现代、阴影更柔和
- 暗色主题更精致（参考 Bloomberg Terminal + Linear.app）
"""
from __future__ import annotations
import re
from datetime import datetime


# ── 配色系统（白底亮色主题）────────────────────────────────────────────
C = {
    # 背景层级
    "bg_root":       "#f8f9fb",       # 浅灰底
    "bg_panel":      "#ffffff",       # 白色卡片
    "bg_elev":       "#f1f3f6",       # 抬升元素
    "bg_hover":      "#e8ecf1",       # hover

    # 边框
    "border":        "#d8dee6",
    "border_soft":   "#e4e9f0",

    # 文字
    "text":          "#1a1f36",
    "text_dim":      "#5e6785",
    "text_mute":     "#8892a8",

    # 强调
    "accent":        "#2563eb",       # 主蓝
    "accent_glow":   "rgba(37,99,235,0.10)",
    "purple":        "#7c3aed",
    "pink":          "#db2777",
    "cyan":          "#0891b2",

    # 状态
    "green":         "#16a34a",
    "green_bg":      "rgba(22,163,74,0.08)",
    "red":           "#dc2626",
    "red_bg":        "rgba(220,38,38,0.08)",
    "orange":        "#d97706",
    "orange_bg":     "rgba(217,119,6,0.08)",
    "yellow":        "#ca8a04",
}

FONT_TEXT = '"Inter","SF Pro Text",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif'
FONT_MONO = '"SF Mono","JetBrains Mono","Cascadia Code",Consolas,monospace'


# ── 工具函数 ────────────────────────────────────────────────────────────

def _color_for_value(val: str, key: str = "") -> tuple[str, str]:
    """根据值文本推断颜色 + 背景色（用于 chip）

    Returns: (foreground_color, background_color or empty)
    """
    v = val.strip()
    k = key.lower()

    # 状态词
    upper = v.upper()
    if any(w in upper for w in ("STOP", "FAIL", "ITM ⚠", "ITM\u26A0", "DANGER")):
        return C["red"], C["red_bg"]
    if any(w in upper for w in ("GO", "PASS", "OTM ✓", "OK", "SAFE")):
        return C["green"], C["green_bg"]
    if any(w in upper for w in ("CAUTION", "WARN", "PENDING")):
        return C["orange"], C["orange_bg"]

    # 带符号数字
    nums = v.replace(",", "").replace("$", "")
    m = re.match(r"^([+\-])(\$?[\d\.]+)%?$", nums)
    if m:
        sign = m.group(1)
        return (C["green"] if sign == "+" else C["red"]), ""

    # 美元数额
    if v.startswith("$"):
        return C["accent"], ""

    return C["text"], ""


def _phase_meta(phase: str) -> tuple[str, str, str]:
    """phase → (color, bg, icon)"""
    p = phase.upper()
    if "SHORT_PUT" in p:
        return C["accent"], C["accent_glow"], "🎯"
    if "SHORT_CALL" in p:
        return C["purple"], "rgba(163,113,255,0.18)", "🎯"
    if "LONG_STOCK" in p:
        return C["orange"], C["orange_bg"], "📦"
    if "IDLE" in p:
        return C["text_dim"], "rgba(155,168,188,0.13)", "💤"
    return C["text"], "", ""


def _verdict_meta(verdict: str) -> tuple[str, str, str]:
    """verdict → (color, bg, icon)"""
    v = verdict.upper()
    if "STOP" in v:
        return C["red"], C["red_bg"], "🛑"
    if "CAUTION" in v or "WARN" in v:
        return C["orange"], C["orange_bg"], "⚠️"
    if "GO" in v or "PASS" in v or "OK" in v:
        return C["green"], C["green_bg"], "✅"
    return C["text_dim"], "rgba(155,168,188,0.13)", "❓"


def _inline(text: str) -> str:
    """处理 markdown 行内：**bold**, `code`, [link](url), ~~strike~~"""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(
        r"`([^`]+?)`",
        rf'<code style="background:{C["bg_elev"]};color:{C["accent"]};'
        rf'padding:1px 6px;border-radius:4px;font-family:{FONT_MONO};'
        rf'font-size:12px;border:1px solid {C["border_soft"]};">\1</code>',
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        rf'<a href="\2" style="color:{C["accent"]};text-decoration:none;border-bottom:1px dashed {C["accent"]};">\1</a>',
        text,
    )
    return text


def _chip(text: str, color: str, bg: str = "") -> str:
    """彩色徽章"""
    bg = bg or "transparent"
    return (f'<span style="display:inline-block;padding:3px 10px;border-radius:20px;'
            f'background:{bg};color:{color};font-size:12px;font-weight:600;'
            f'letter-spacing:.3px;">{text}</span>')


# ── KPI 卡片渲染（账户区块特殊处理）──────────────────────────────────────

def _is_account_table(header: list, body: list) -> bool:
    """检测是否为账户 KPI 表"""
    if len(header) != 2 or not body:
        return False
    keys = " ".join(r[0].lower() for r in body)
    return any(w in keys for w in ("equity", "cash", "buying power", "p&l", "pnl"))


def _render_account_kpi(body: list) -> str:
    """账户 KPI 大卡片网格 — 突出显示 Equity 和 Daily P&L"""
    cards = []
    for label, value in body:
        color, _ = _color_for_value(value, label)
        is_pnl = "p&l" in label.lower() or "pnl" in label.lower()
        is_equity = "equity" in label.lower()

        # Equity 用最大字号，P&L 用强调色
        size = "28px" if is_equity else ("24px" if is_pnl else "20px")
        weight = "700" if is_equity or is_pnl else "600"

        # P&L 卡片带颜色背景
        bg = C["bg_elev"]
        border_color = C["border"]
        if is_pnl:
            if value.strip().startswith("+"):
                bg = C["green_bg"]
                border_color = "rgba(63,185,80,0.4)"
            elif value.strip().startswith("-"):
                bg = C["red_bg"]
                border_color = "rgba(255,110,110,0.4)"

        cards.append(f"""
<div style="flex:1 1 200px;background:{bg};
            border:1px solid {border_color};border-radius:12px;
            padding:16px 18px;min-width:160px;">
  <div style="color:{C['text_mute']};font-size:11px;font-weight:600;
              text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px;">
    {_inline(label)}
  </div>
  <div style="color:{color};font-size:{size};font-weight:{weight};
              font-family:{FONT_MONO};line-height:1.1;letter-spacing:-0.5px;">
    {_inline(value)}
  </div>
</div>""")
    return f'<div style="display:flex;flex-wrap:wrap;gap:6px;margin:0;">{"".join(cards)}</div>'


def _render_kpi_grid(body: list) -> str:
    """通用 KPI 网格（小尺寸）"""
    cards = []
    for label, value in body:
        color, _ = _color_for_value(value, label)
        cards.append(f"""
<div style="flex:1 1 140px;background:{C['bg_elev']};
            border:1px solid {C['border']};border-radius:10px;
            padding:12px 14px;min-width:130px;">
  <div style="color:{C['text_mute']};font-size:10.5px;font-weight:600;
              text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
    {_inline(label)}
  </div>
  <div style="color:{color};font-size:17px;font-weight:600;
              font-family:{FONT_MONO};">
    {_inline(value)}
  </div>
</div>""")
    return f'<div style="display:flex;flex-wrap:wrap;gap:6px;margin:0;">{"".join(cards)}</div>'


# ── 表格渲染 ────────────────────────────────────────────────────────────

def _render_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header, body = rows[0], rows[1:]

    # 账户 KPI 用大卡
    if _is_account_table(header, body):
        return _render_account_kpi(body)

    # 普通 2 列 KPI 用小卡
    if len(header) == 2 and 2 <= len(body) <= 8:
        return _render_kpi_grid(body)

    # 通用表格
    th_style = (
        f"background:{C['bg_elev']};color:{C['text_mute']};text-align:left;"
        f"padding:11px 14px;font-size:11px;font-weight:700;text-transform:uppercase;"
        f"letter-spacing:1.1px;border-bottom:2px solid {C['border']};"
    )
    td_base = (
        f"padding:11px 14px;border-bottom:1px solid {C['border_soft']};"
        f"font-size:13.5px;font-family:{FONT_MONO};"
    )

    head_html = "".join(f"<th style='{th_style}'>{_inline(c)}</th>" for c in header)

    body_html = ""
    for i, row in enumerate(body):
        cells = []
        for j, cell in enumerate(row):
            key = header[j] if j < len(header) else ""
            color, bg = _color_for_value(cell, key)
            style = td_base + f"color:{color};"
            content = _inline(cell)
            if bg:
                content = f'<span style="background:{bg};padding:2px 8px;border-radius:6px;">{content}</span>'
            cells.append(f"<td style='{style}'>{content}</td>")
        zebra = C['bg_panel'] if i % 2 == 0 else C['bg_root']
        body_html += f"<tr style='background:{zebra};'>{''.join(cells)}</tr>"

    return f"""
<div style="overflow-x:auto;margin:2px 0;border:1px solid {C['border']};
            border-radius:10px;">
<table style="width:100%;border-collapse:collapse;">
<thead><tr>{head_html}</tr></thead>
<tbody>{body_html}</tbody>
</table>
</div>"""


# ── Phase / Verdict 特殊块 ─────────────────────────────────────────────

def _try_phase_badge(lines: list[str]) -> str | None:
    """检测 phase 标题（如 '## Phase: `IDLE`'）→ 大徽章"""
    for line in lines:
        m = re.match(r"^##\s*Phase:\s*`?(\w+)`?", line.strip())
        if m:
            phase = m.group(1)
            color, bg, icon = _phase_meta(phase)
            # 找下一行非空作为描述
            desc = ""
            idx = lines.index(line)
            for j in range(idx + 1, min(idx + 4, len(lines))):
                if lines[j].strip():
                    desc = _inline(lines[j].strip())
                    break
            return f"""
<div style="background:{bg};border:1px solid {color};border-radius:12px;
            padding:10px 14px;margin:0;display:flex;align-items:center;gap:10px;">
  <div style="font-size:36px;line-height:1;">{icon}</div>
  <div>
    <div style="color:{C['text_mute']};font-size:11px;font-weight:600;
                text-transform:uppercase;letter-spacing:1.2px;margin-bottom:4px;">
      Current Wheel Phase
    </div>
    <div style="color:{color};font-size:22px;font-weight:700;font-family:{FONT_MONO};
                letter-spacing:-0.5px;">{phase}</div>
    {f'<div style="color:{C["text_dim"]};font-size:13px;margin-top:4px;">{desc}</div>' if desc else ''}
  </div>
</div>"""
    return None


def _try_verdict_badge(content: str) -> str:
    """识别 ### ✅ Verdict: GO — ... 这种行 → 大彩色 badge"""
    pattern = re.compile(r"^###\s*([^\s]+)\s*Verdict:\s*\*?\*?(\w+)\b[^\n]*", re.MULTILINE)
    def repl(m):
        verdict = m.group(2)
        color, bg, icon = _verdict_meta(verdict)
        msg = m.group(0).split(":", 1)[1].strip()
        msg = re.sub(r"^\*+|\*+$", "", msg)
        return f'@@VERDICT_BADGE@@<div style="background:{bg};border:2px solid {color};border-radius:10px;padding:12px 18px;margin:2px 0;display:flex;align-items:center;gap:14px;"><div style="font-size:28px;">{icon}</div><div style="flex:1;"><div style="color:{C["text_mute"]};font-size:10px;text-transform:uppercase;letter-spacing:1.2px;font-weight:600;">VERDICT</div><div style="color:{color};font-size:18px;font-weight:700;letter-spacing:-0.3px;">{_inline(msg)}</div></div></div>@@VERDICT_BADGE@@'
    return pattern.sub(repl, content)


# ── 段落渲染 ────────────────────────────────────────────────────────────

def _render_block(lines: list[str]) -> str:
    out = []
    table_buf = []

    def flush_table():
        nonlocal table_buf
        if table_buf:
            rows = []
            for line in table_buf:
                cells = [c.strip() for c in line.strip("|").split("|")]
                if all(set(c) <= {"-", ":", " "} for c in cells):
                    continue
                rows.append(cells)
            if rows:
                out.append(_render_table(rows))
            table_buf = []

    for line in lines:
        s = line.rstrip()
        if s.startswith("|") and s.endswith("|") and "|" in s[1:-1]:
            table_buf.append(s)
            continue
        flush_table()

        if not s.strip():
            continue  # 完全跳过空行，不产生任何间距

        # 三级标题
        if s.startswith("### "):
            text = s[4:]
            # 验证 verdict 标题先用通用样式（badge 在外层处理）
            out.append(f"<h3 style='color:{C['text']};font-size:13px;font-weight:700;"
                       f"margin:2px 0 1px 0;text-transform:uppercase;letter-spacing:1px;'>"
                       f"{_inline(text)}</h3>")
            continue

        if s.startswith("- "):
            content = _inline(s[2:])
            out.append(f"<div style='padding:1px 0 1px 14px;color:{C['text']};"
                       f"font-size:13.5px;line-height:1.6;position:relative;'>"
                       f"<span style='position:absolute;left:0;color:{C['accent']};'>•</span>"
                       f"{content}</div>")
            continue

        if s.startswith("> "):
            out.append(
                f"<blockquote style='padding:4px 12px;color:{C['text_dim']};"
                f"margin:0;background:{C['bg_elev']};border-radius:6px;"
                f"font-size:13px;font-style:normal;'>{_inline(s[2:])}</blockquote>"
            )
            continue

        # 段落
        out.append(f"<p style='margin:0;color:{C['text']};font-size:13px;"
                   f"line-height:1.6;'>{_inline(s)}</p>")

    flush_table()
    return "\n".join(out)


# ── 主入口 ──────────────────────────────────────────────────────────────

def render_dashboard(md: str, title_override: str | None = None) -> str:
    lines = md.split("\n")

    # 1. 提取 H1 + subtitle（**...** 那行）
    page_title = title_override or "AI Trading Dashboard"
    page_subtitle = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            page_title = line[2:].strip()
            body_start = i + 1
            for j in range(i + 1, min(i + 4, len(lines))):
                stripped = lines[j].strip()
                if stripped and not stripped.startswith("#"):
                    page_subtitle = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)
                    body_start = j + 1
                    break
            break

    # 2. 按 H2 切分 sections
    sections = []
    current_title = None
    current_lines = []

    def flush():
        if current_title or current_lines:
            sections.append((current_title, current_lines[:]))

    for line in lines[body_start:]:
        if line.startswith("## "):
            flush()
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    flush()

    # 3. 渲染每个 section
    sections_html = []
    for title, content_lines in sections:
        # 特殊处理：phase section
        if title and title.startswith("Phase"):
            badge = _try_phase_badge([f"## {title}"] + content_lines)
            if badge:
                sections_html.append(badge)
                continue

        body = _render_block(content_lines)
        # 检查 verdict badge
        body = _try_verdict_badge(body)

        # 标题色：health/verdict/cycle eval 相关用强调色
        title_color = C["accent"]
        if title and any(w in title.lower() for w in ("alert", "warning", "停")):
            title_color = C["orange"]

        title_html = ""
        if title:
            title_html = (
                f"<h2 style='color:{C['text']};font-size:15px;font-weight:700;"
                f"margin:0 0 8px 0;padding-bottom:6px;"
                f"border-bottom:2px solid {C['border']};"
                f"text-transform:uppercase;letter-spacing:1.5px;'>"
                f"{_inline(title)}</h2>"
            )

        sections_html.append(f"""
<div style="background:{C['bg_panel']};border:1px solid {C['border']};
            border-radius:10px;padding:12px 16px;margin:0;
            box-shadow:0 1px 3px rgba(0,0,0,0.3);">
{title_html}
{body}
</div>""")

    # 4. 顶部时间
    now_str = datetime.now().strftime("%Y-%m-%d · %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{page_title}</title>
</head>
<body style="margin:0;padding:0;background:{C['bg_root']};
            font-family:{FONT_TEXT};color:{C['text']};">
<div style="max-width:920px;margin:0 auto;padding:12px 16px;">

  <!-- HERO HEADER (table layout for max email-client compatibility) -->
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
         bgcolor="#1e3a8a"
         style="background:#1e3a8a;background-color:#1e3a8a;
                border-radius:10px;margin-bottom:6px;border-collapse:separate;">
    <tr>
      <td bgcolor="#1e3a8a" style="background:#1e3a8a;background-color:#1e3a8a;
              padding:16px 22px;border-radius:10px;">
        <div style="color:#a5c8ff;font-size:11px;text-transform:uppercase;
                    letter-spacing:2px;font-weight:700;margin-bottom:6px;">
          ● AI TRADING · LIVE
        </div>
        <div style="color:#ffffff;font-size:24px;font-weight:800;line-height:1.2;
                    letter-spacing:-0.4px;">
          {page_title}
        </div>
        {f'<div style="color:#cfe0ff;font-size:13px;margin-top:4px;font-family:{FONT_MONO};">{page_subtitle}</div>' if page_subtitle else ''}
      </td>
    </tr>
  </table>

  {"".join(sections_html)}

  <!-- FOOTER -->
  <div style="text-align:center;color:{C['text_mute']};font-size:11px;
              padding:24px 0 10px;margin-top:8px;border-top:1px solid {C['border_soft']};">
    <div>Generated {now_str} · Wheel Strategy Bot</div>
    <div style="margin-top:4px;opacity:0.7;">All metrics computed in real-time · Paper trading mode</div>
  </div>
</div>
</body>
</html>"""
