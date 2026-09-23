"""Tony-style infographic deck builder (python-pptx, native editable shapes).

Style DNA (from the two reference images in ../references/):
  * Two-tone headline: navy text + ONE orange highlighted keyword.
  * Up to 5 numbered horizontal "bands", each with its own accent color
    (blue -> orange -> purple -> teal -> green), a big numbered circle,
    a gradient down-arrow, rounded card, check-mark bullets,
    detail columns and a star-rating "priority" card.
  * One HERO band (the main point) gets a tinted fill + thick colored border.
  * Right-hand vertical "flow rail": dashed line + numbered circles + short labels.
  * Bottom tagline banner with ONE orange keyword, optional navy focus bar,
    strategy-card row, small grey sources line, "Confidential | For Internal Use Only".

Usage:
    python tony_deck.py spec.json out.pptx
Spec format: see ../SKILL.md and example_spec.json next to this file.
"""
from __future__ import annotations

import json
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

# ── Design tokens ──────────────────────────────────────────────────────────
NAVY = "0B2A6B"
BLUE = "1565E0"
ORANGE = "F26A1B"
PURPLE = "6A2BD9"
TEAL = "0E8F8F"
GREEN = "3E9B2F"
GOLD = "F5A623"
TEXT = "1F2A44"
MUTED = "6B7280"
WHITE = "FFFFFF"
BORDER = "DDE3EE"
DARK_BAR = "1B2438"

BAND_COLORS = [BLUE, ORANGE, PURPLE, TEAL, GREEN]
TINTS = {BLUE: "EEF4FF", ORANGE: "FFF4EC", PURPLE: "F4EFFF", TEAL: "EAF7F6",
         GREEN: "EFF8EC", NAVY: "EEF1F8", GOLD: "FFF8E8"}

FONT_LATIN = "Arial"
FONT_LATIN_HEAVY = "Arial Black"
FONT_EA = "Microsoft YaHei"

SIZES = {"landscape": (13.333, 7.5), "portrait": (7.5, 13.333)}


def rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_.lstrip("#").upper())


def tint(color: str) -> str:
    return TINTS.get(color, "F4F6FA")


# ── Low-level helpers ──────────────────────────────────────────────────────
def _set_fonts(run, bold: bool, heavy: bool = False):
    run.font.name = FONT_LATIN_HEAVY if heavy else FONT_LATIN
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = rpr.makeelement(qn("a:ea"), {})
        rpr.append(ea)
    ea.set("typeface", FONT_EA)
    run.font.bold = bold


def _zero_margins(tf, m=0.0):
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, side, Inches(m))


def add_text(slide, x, y, w, h, lines, size=12, align=PP_ALIGN.LEFT,
             anchor=MSO_ANCHOR.TOP, margin=0.03):
    """lines: str | list of lines; a line is str or list of segments
    (text, color, bold[, size[, heavy]])."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.vertical_anchor = anchor
    _zero_margins(tf, margin)
    lines = lines if isinstance(lines, list) else [lines]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        segs = [(line, TEXT, False)] if isinstance(line, str) else line
        for seg in segs:
            text, color, bold = seg[0], seg[1], seg[2]
            sz = seg[3] if len(seg) > 3 and seg[3] else size
            heavy = seg[4] if len(seg) > 4 else False
            r = p.add_run()
            r.text = text
            r.font.size = Pt(sz)
            r.font.color.rgb = rgb(color)
            _set_fonts(r, bold, heavy)
    return tb


def add_shape(slide, kind, x, y, w, h, fill=None, line=None, line_w=0.75, radius=None):
    s = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill:
        s.fill.solid()
        s.fill.fore_color.rgb = rgb(fill)
    else:
        s.fill.background()
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(line_w)
    else:
        s.line.fill.background()
    if radius is not None and kind == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = radius
    # kill the theme drop shadow -> flat, clean look
    sppr = s._element.spPr
    sppr.append(sppr.makeelement(qn("a:effectLst"), {}))
    style = s._element.find(qn("p:style"))
    if style is not None:
        s._element.remove(style)
    return s


def gradient(shape, top: str, bottom: str):
    shape.fill.gradient()
    shape.fill.gradient_angle = 90  # top -> bottom
    stops = shape.fill.gradient_stops
    stops[0].color.rgb = rgb(top)
    stops[0].position = 0
    stops[1].color.rgb = rgb(bottom)
    stops[1].position = 1.0


def circle_num(slide, x, y, d, n, color, font=None):
    c = add_shape(slide, MSO_SHAPE.OVAL, x, y, d, d, fill=color, line=WHITE, line_w=1.5)
    tf = c.text_frame
    _zero_margins(tf)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = str(n)
    r.font.size = Pt(font or max(10, int(d * 40)))
    r.font.color.rgb = rgb(WHITE)
    _set_fonts(r, True, heavy=True)
    return c


def pill(slide, x, y, w, h, text, color, size=9, text_color=WHITE):
    s = add_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill=color, radius=0.5)
    tf = s.text_frame
    _zero_margins(tf, 0.02)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.color.rgb = rgb(text_color)
    _set_fonts(r, True)
    return s


def text_w(text: str, pt: float) -> float:
    """Rough rendered width in inches (CJK ≈ 1em, Latin ≈ 0.55em)."""
    em = pt / 72
    return sum(em if ord(ch) > 0x2E80 else em * 0.58 for ch in text)


def fit_size(lines, width_in, size, min_size=20):
    """Largest pt <= size at which every line fits on one row of width_in."""
    while size > min_size and max(text_w(ln, size) for ln in lines) * 1.2 > width_in:  # 1.2 = heavy-font fudge
        size -= 1
    return size


def stars_text(n: float, total: int = 5) -> str:
    n = max(0.0, min(float(n), total))
    full = int(n)
    half = n - full >= 0.5
    return "★" * full + ("⯪" if half else "") + "☆" * (total - full - (1 if half else 0))


def highlight_line(text, hl, base, hl_color=ORANGE, size=None, bold=True, heavy=False):
    """Segments for `text` with every occurrence of `hl` in `hl_color`."""
    if not hl or hl not in text:
        return [(text, base, bold, size, heavy)]
    out = []
    pieces = text.split(hl)
    for i, piece in enumerate(pieces):
        if piece:
            out.append((piece, base, bold, size, heavy))
        if i < len(pieces) - 1:
            out.append((hl, hl_color, True, size, heavy))
    return out


# ── Deck ───────────────────────────────────────────────────────────────────
class Deck:
    def __init__(self, orientation: str = "landscape"):
        self.orientation = orientation
        self.portrait = orientation == "portrait"
        self.W, self.H = SIZES[orientation]
        self.prs = Presentation()
        self.prs.slide_width = Inches(self.W)
        self.prs.slide_height = Inches(self.H)
        self.blank = self.prs.slide_layouts[6]

    # page chrome ------------------------------------------------------------
    def new_slide(self):
        s = self.prs.slides.add_slide(self.blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = rgb(WHITE)
        # soft sky-blue corner wash (stands in for the reference's scenery corner)
        add_shape(s, MSO_SHAPE.OVAL, self.W - 3.0, self.H - 1.7, 4.2, 3.0, fill="EDF4FC")
        return s

    def headline(self, s, title, highlight=None, subtitle=None, badge=None, top=0.18):
        w = self.W - (2.5 if badge else 0.6)
        tlines = title.split("\n")
        size = fit_size(tlines, w, 28 if self.portrait else 34)
        th = len(tlines) * size / 72 * 1.25 + 0.1
        add_text(s, 0.3, top, w, th,
                 [highlight_line(ln, highlight, NAVY, size=size, heavy=True) for ln in tlines])
        y = top + th
        if subtitle:
            add_text(s, 0.3, y, w, 0.34, [[("—— ", MUTED, False), (subtitle, TEXT, True),
                                            (" ——", MUTED, False)]], size=12,
                     align=PP_ALIGN.CENTER if self.portrait else PP_ALIGN.LEFT)
            y += 0.4
        if badge:  # top-right rounded info box (key stat / author tag)
            add_shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, self.W - 2.15, top + 0.05, 1.9, 0.68,
                      fill=WHITE, line=BLUE, line_w=1.25, radius=0.3)
            add_text(s, self.W - 2.1, top + 0.07, 1.8, 0.64,
                     [[(ln, BLUE if i == 0 else NAVY, i == 0, 11 if i == 0 else 8)]
                      for i, ln in enumerate(badge.split("\n"))],
                     align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        return y + 0.05

    def flow_rail(self, s, x, y0, y1, labels, colors, header=None):
        rw = 0.95 if self.portrait else 1.2
        cx = x + rw / 2
        if header:
            add_text(s, x - 0.1, y0 - 0.3, rw + 0.2, 0.28, [[(header, NAVY, True, 10)]],
                     align=PP_ALIGN.CENTER)
        n = len(labels)
        step = (y1 - y0) / n
        ln = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(cx), Inches(y0),
                                    Inches(cx), Inches(y1))
        ln.line.color.rgb = rgb("9AA8C0")
        ln.line.width = Pt(1.25)
        ln.line.dash_style = MSO_LINE.DASH
        for i, (lab, col) in enumerate(zip(labels, colors)):
            cy = y0 + step * i + step * 0.5 - 0.38
            circle_num(s, cx - 0.17, cy, 0.34, i + 1, col, font=13)
            add_text(s, x, cy + 0.36, rw, 0.5,
                     [[(t, col if j == 0 else NAVY, True, 9)]
                      for j, t in enumerate(lab.split("\n"))], align=PP_ALIGN.CENTER)
            if i < n - 1:
                tri = add_shape(s, MSO_SHAPE.ISOSCELES_TRIANGLE, cx - 0.06,
                                y0 + step * (i + 1) - 0.06, 0.12, 0.09, fill=colors[i + 1])
                tri.rotation = 180

    def band(self, s, x, y, w, h, n, b, color, hero=False):
        add_shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h,
                  fill=tint(color) if hero else WHITE,
                  line=color if hero else BORDER, line_w=2.5 if hero else 1.0, radius=0.08)
        # number + gradient down-arrow
        d = min(0.55, h * 0.42)
        circle_num(s, x + 0.1, y + 0.08, d, n, color)
        ah = h - d - 0.2
        if ah > 0.15:
            arr = add_shape(s, MSO_SHAPE.DOWN_ARROW, x + 0.1 + d / 2 - 0.13, y + 0.1 + d,
                            0.26, ah, fill=color)
            gradient(arr, tint(color), color)
        # title block
        tx = x + d + 0.22
        tw = b.get("title_w", 1.75 if self.portrait else 2.5)
        tsize = 17 if h > 1.0 else 15
        lines = [[(b["title"], color, True, tsize, True)]]
        th = tsize / 72 * 1.3
        if b.get("subtitle"):
            lines.append([(f"({b['subtitle']})", color, True, 10)])
            th += 0.2
        add_text(s, tx, y + 0.05, tw, th + 0.05, lines)
        cy = y + 0.08 + th
        if b.get("tag"):
            pill(s, tx + 0.02, cy, min(tw - 0.05, text_w(b["tag"], 8) + 0.25), 0.21, b["tag"],
                 color, size=8)
            cy += 0.26
        if b.get("checks") and y + h - cy > 0.15:
            add_text(s, tx, cy, tw, y + h - cy - 0.03,
                     [[("✓ ", color, True, 8.5), (c, TEXT, False, 8.5)] for c in b["checks"]],
                     size=8.5, margin=0.02)
        # detail columns
        cols = b.get("columns", [])
        stars = b.get("stars")
        star_w = (1.3 if self.portrait else 1.7) if stars is not None else 0
        cx0 = tx + tw + 0.1
        cx1 = x + w - star_w - 0.2
        if cols and (cx1 - cx0) / len(cols) < 1.4:  # too narrow -> stack columns in one box
            self._column(s, cx0, y + 0.06, cx1 - cx0, h - 0.12, cols, color)
        elif cols:
            cw = (cx1 - cx0) / len(cols)
            for i, col in enumerate(cols):
                ccx = cx0 + i * cw
                if i:
                    dv = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(ccx - 0.04),
                                                Inches(y + 0.15), Inches(ccx - 0.04),
                                                Inches(y + h - 0.15))
                    dv.line.color.rgb = rgb(BORDER)
                    dv.line.width = Pt(0.75)
                self._column(s, ccx, y + 0.06, cw - 0.1, h - 0.12, col, color)
        # star "priority" card
        if stars is not None:
            sx = x + w - star_w - 0.1
            add_shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, sx, y + 0.08, star_w, h - 0.16,
                      fill=WHITE if hero else tint(color), line=color, line_w=1.0, radius=0.1)
            lines = [[(b.get("star_label", "优先级"), color, True, 10)],
                     [(stars_text(stars), ORANGE if hero else color, False, 15 if h > 1 else 13)]]
            lines += [[(t, color if hero else TEXT, True, 8.5)] for t in b.get("star_notes", [])]
            add_text(s, sx + 0.04, y + 0.1, star_w - 0.08, h - 0.2, lines,
                     align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

    def _column(self, s, x, y, w, h, col, color):
        cols = col if isinstance(col, list) else [col]
        lines = []
        for col in cols:
            lines += self._column_lines(col, color)
        add_text(s, x, y, w, h, lines,
                 align=PP_ALIGN.CENTER if cols[0].get("center") else PP_ALIGN.LEFT,
                 anchor=MSO_ANCHOR.MIDDLE)

    @staticmethod
    def _column_lines(col, color):
        lines = []
        if col.get("header"):
            lines.append([(col["header"], color, True, 11)])
        if col.get("big"):
            lines.append([(col["big"], NAVY, True, 20, True)])
        if col.get("delta"):
            lines.append([(col["delta"], ORANGE, True, 12)])
        if col.get("stars") is not None:
            lines.append([(stars_text(col["stars"]), GOLD, False, 12)])
        icon = col.get("icon", "▸")
        for item in col.get("items", []):
            lines.append([(icon + " ", color, True, 9), (item, TEXT, False, 9)])
        return lines

    def tagline(self, s, y, text, highlight=None, h=0.46):
        add_shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 0.3, y, self.W - 0.6, h, fill=WHITE,
                  line=BORDER, line_w=1.0, radius=0.5)
        add_text(s, 0.45, y, 0.5, h, [[("⟳", BLUE, True, 20)]], anchor=MSO_ANCHOR.MIDDLE)
        add_text(s, 0.95, y, self.W - 1.4, h,
                 [highlight_line(text, highlight, NAVY, size=13 if self.portrait else 17,
                                 heavy=True)], anchor=MSO_ANCHOR.MIDDLE)

    def focus_bar(self, s, y, text, h=0.36):
        add_shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 0.9, y, self.W - 1.8, h, fill=DARK_BAR,
                  radius=0.2)
        add_text(s, 0.9, y, self.W - 1.8, h, [[(text, WHITE, True, 11)]],
                 align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

    def strategy_row(self, s, y, cards, h=1.0):
        n = len(cards)
        gap = 0.12
        w = (self.W - 0.6 - gap * (n - 1)) / n
        for i, c in enumerate(cards):
            color = c.get("color", [ORANGE, GOLD, BLUE, MUTED][i % 4])
            x = 0.3 + i * (w + gap)
            add_shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill=WHITE, line=BORDER,
                      radius=0.12)
            lines = [[(c.get("label", ""), color, True, 11),
                      (f" {c['note']}" if c.get("note") else "", MUTED, False, 9)]]
            if c.get("title"):
                lines.append([(c["title"], NAVY, True, 12)])
            if c.get("sub"):
                lines.append([(c["sub"], TEXT, False, 9)])
            if c.get("stars") is not None:
                lines.append([(stars_text(c["stars"]), color, False, 12)])
            for it in c.get("checks", []):
                lines.append([("☑ ", color, True, 9), (it, TEXT, False, 9)])
            add_text(s, x + 0.08, y + 0.04, w - 0.16, h - 0.08, lines,
                     align=PP_ALIGN.CENTER if not c.get("checks") else PP_ALIGN.LEFT,
                     anchor=MSO_ANCHOR.MIDDLE)

    def footer(self, s, sources=None, confidential=True, slogan=None):
        y = self.H - 0.42
        if sources:
            add_text(s, 0.3, y, self.W - 2.8, 0.38,
                     [[("Sources: ", MUTED, True, 7), (sources, MUTED, False, 7)]], size=7)
        right = []
        if slogan:
            right.append([(slogan, NAVY, True, 9)])
        if confidential:
            right.append([("Confidential | For Internal Use Only", MUTED, False, 7)])
        if right:
            add_text(s, self.W - 2.45, y - 0.15, 2.2, 0.53, right,
                     align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.BOTTOM)

    # slide templates --------------------------------------------------------
    def flow_slide(self, spec):
        """The signature one-page '一张图看懂' layout: numbered color bands + flow rail."""
        s = self.new_slide()
        y = self.headline(s, spec["title"], spec.get("highlight"), spec.get("subtitle"),
                          spec.get("badge"))
        bands = spec["bands"]
        rail = spec.get("rail")
        bottom = self.H - 0.5
        if spec.get("tagline"):
            bottom -= 0.55
        if spec.get("focus_bar"):
            bottom -= 0.44
        if spec.get("strategy"):
            bottom -= 1.12
        rail_w = (1.0 if self.portrait else 1.25) if rail else 0
        x, w = 0.3, self.W - 0.6 - rail_w - (0.08 if rail else 0)
        gap = 0.1
        top = y + (0.3 if rail and rail.get("header") else 0.0)
        bh = (bottom - top - gap * (len(bands) - 1)) / len(bands)
        colors = []
        for i, b in enumerate(bands):
            color = b.get("color", BAND_COLORS[i % len(BAND_COLORS)])
            colors.append(color)
            self.band(s, x, top + i * (bh + gap), w, bh, i + 1, b, color,
                      hero=b.get("hero", False))
        if rail:
            self.flow_rail(s, self.W - 0.3 - rail_w, top, bottom, rail["labels"], colors,
                           rail.get("header"))
        yy = bottom + 0.08
        if spec.get("tagline"):
            self.tagline(s, yy, spec["tagline"], spec.get("tagline_highlight"))
            yy += 0.55
        if spec.get("focus_bar"):
            self.focus_bar(s, yy, spec["focus_bar"])
            yy += 0.44
        if spec.get("strategy"):
            self.strategy_row(s, yy, spec["strategy"])
        self.footer(s, spec.get("sources"), spec.get("confidential", True), spec.get("slogan"))
        return s

    def cover_slide(self, spec):
        s = self.new_slide()
        add_shape(s, MSO_SHAPE.RECTANGLE, 0, 0, 0.18, self.H, fill=BLUE)
        yc = self.H * 0.28
        tlines = spec["title"].split("\n")
        size = fit_size(tlines, self.W - 1.6, 36 if self.portrait else 44, min_size=26)
        th = len(tlines) * size / 72 * 1.25 + 0.1
        add_text(s, 0.8, yc, self.W - 1.6, th,
                 [highlight_line(ln, spec.get("highlight"), NAVY, size=size, heavy=True)
                  for ln in tlines])
        yy = yc + th + 0.1
        if spec.get("subtitle"):
            add_text(s, 0.8, yy, self.W - 1.6, 0.5,
                     [[("—— ", MUTED, False, 16), (spec["subtitle"], TEXT, True, 16),
                       (" ——", MUTED, False, 16)]])
            yy += 0.7
        cx = 0.8
        for i, c in enumerate(spec.get("chips", [])):
            wch = text_w(c, 11) + 0.4
            pill(s, cx, yy, wch, 0.36, c, BAND_COLORS[i % 5], size=11)
            cx += wch + 0.15
        if spec.get("meta"):
            add_text(s, 0.8, self.H - 1.1, self.W - 1.6, 0.4, [[(spec["meta"], MUTED, False, 11)]])
        self.footer(s, None, spec.get("confidential", True), spec.get("slogan"))
        return s

    def cards_slide(self, spec):
        """Grid of rated cards (like the reference's 'Country Opportunity' row)."""
        s = self.new_slide()
        y = self.headline(s, spec["title"], spec.get("highlight"), spec.get("subtitle"),
                          spec.get("badge"))
        cards = spec["cards"]
        per_row = spec.get("per_row", min(len(cards), 2 if self.portrait else 4))
        rows = (len(cards) + per_row - 1) // per_row
        bottom = self.H - 0.55 - (0.55 if spec.get("tagline") else 0)
        gap = 0.15
        w = (self.W - 0.6 - gap * (per_row - 1)) / per_row
        h = min((bottom - y - gap * (rows - 1)) / rows, spec.get("card_h", 2.6))
        used = rows * (h + gap) + (0.55 if spec.get("tagline") else 0)
        y += max(0.0, (self.H - 0.55 - y - used) / 2)  # vertically center the block
        for i, c in enumerate(cards):
            color = c.get("color", BAND_COLORS[i % 5])
            r, k = divmod(i, per_row)
            x = 0.3 + k * (w + gap)
            yy = y + r * (h + gap)
            add_shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, yy, w, h,
                      fill=tint(color) if c.get("hero") else WHITE, line=color,
                      line_w=2.5 if c.get("hero") else 1.0, radius=0.06)
            add_shape(s, MSO_SHAPE.RECTANGLE, x + 0.15, yy + 0.02, w - 0.3, 0.06, fill=color)
            circle_num(s, x + 0.15, yy + 0.2, 0.42, c.get("n", i + 1), color, font=15)
            lines = [[(c["title"], color, True, 15, True)]]
            if c.get("subtitle"):
                lines.append([(c["subtitle"], MUTED, False, 10)])
            add_text(s, x + 0.65, yy + 0.16, w - 0.75, 0.6, lines)
            body = []
            if c.get("big"):
                body.append([(c["big"], NAVY, True, 22, True)])
            if c.get("delta"):
                body.append([(c["delta"], ORANGE, True, 12)])
            if c.get("stars") is not None:
                body.append([(stars_text(c["stars"]), ORANGE if c.get("hero") else GOLD,
                              False, 15)])
            for it in c.get("checks", []):
                body.append([("✓ ", color, True, 10), (it, TEXT, False, 10)])
            add_text(s, x + 0.2, yy + 0.8, w - 0.35, h - 0.9, body, size=10)
        if spec.get("tagline"):
            self.tagline(s, y + rows * (h + gap) + 0.05, spec["tagline"],
                         spec.get("tagline_highlight"))
        self.footer(s, spec.get("sources"), spec.get("confidential", True), spec.get("slogan"))
        return s

    def save(self, path):
        self.prs.save(path)


SLIDE_TYPES = {"flow": "flow_slide", "cover": "cover_slide", "cards": "cards_slide"}


def build(spec: dict, out: str) -> str:
    deck = Deck(spec.get("orientation", "landscape"))
    for sl in spec["slides"]:
        getattr(deck, SLIDE_TYPES[sl.get("type", "flow")])(sl)
    deck.save(out)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python tony_deck.py spec.json out.pptx")
    with open(sys.argv[1], encoding="utf-8") as f:
        print(build(json.load(f), sys.argv[2]))
