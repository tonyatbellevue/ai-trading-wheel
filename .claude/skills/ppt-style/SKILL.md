---
name: ppt-style
description: Tony's personal PPT / slide style — MUST be used for EVERY deck, slide, PPT, PowerPoint, presentation, 一页纸 / 一张图看懂 infographic, or pitch page, in any language, whatever the topic (trading, networking/HPE Juniper sales, AI industry, reviews). Triggers on "PPT", "slides", "deck", "presentation", "幻灯片", "做个PPT", "做一页", "一张图", "汇报", "pptx". Produces the signature look — navy headline with ONE orange keyword, 5 numbered color bands (blue→orange→purple→teal→green) with number circles + gradient down-arrows, one hero band, star-rating priority cards, a dashed "flow rail" on the right, a tagline banner, and a sources/Confidential footer — as an editable .pptx via scripts/tony_deck.py.
---

# ppt-style — Tony's infographic deck style

**Rule: every PPT/slide deck Tony asks for uses this style.** Don't use a generic template,
the default pptx look, or a different slides theme unless Tony explicitly says so for that deck.
If another skill (e.g. `anthropic-skills:pptx`) is also used for mechanics, this skill still
decides the look.

Reference images (look at them before designing a new layout):
- `references/style-ref-portrait-ai-money-flow.jpg` — "AI的钱，正在从NVDA流向哪里？" (portrait, 中文)
- `references/style-ref-landscape-ip-routing.jpg` — "IP Routing, the New Growth Engine in ASEAN + Taiwan" (16:9, English)

## Style DNA

| Element | Rule |
|---|---|
| **Headline** | Heavy sans (Arial Black / Microsoft YaHei bold), navy `#0B2A6B`. Exactly **one** keyword in orange `#F26A1B` (e.g. "NVDA", "New Growth Engine"). Often a question or a bold claim. |
| **Subtitle** | `—— 一张图看懂 … ——` style: short, bold, flanked by thin dashes. |
| **Structure** | "One page tells the whole story": 3–5 **numbered horizontal bands**, top → bottom = a sequence (money flow, value chain, funnel, phases). |
| **Band colors** | 1 blue `#1565E0` → 2 orange `#F26A1B` → 3 purple `#6A2BD9` → 4 teal `#0E8F8F` → 5 green `#3E9B2F`. Each band uses its color for number circle, arrow, title, pill tag, check marks, star card. |
| **Band anatomy** | Left: big numbered circle + gradient down-arrow. Title (big, colored) + `(status)` + colored pill tag + ✓ bullets. Middle: 2–3 detail columns (代表公司 / 资金逻辑, or stat columns with a big number + orange delta). Right: star "priority" card (优先级 ★★★★☆ + 2 short notes). |
| **Hero band** | Exactly one band is the main message → tinted fill + thick colored border, 5 orange stars. |
| **Flow rail** | Right-hand dashed vertical line with small numbered circles + 1–2 word labels (资金扩散顺序: 已爆发 → 主升浪 → 加速前夜 → 稳步扩散 → 长线布局; or Value Flow: More Compute → More Data → …). |
| **Bottom** | Rounded tagline banner with ⟳ icon and one orange phrase ("钱不会消失，只会提前流向**更高回报**的方向。"). Optional dark navy focus bar (SALES FOCUS: …). Optional strategy row (重仓 / 配置 / 持有 / 观察 cards with stars). |
| **Footer** | Tiny grey `Sources: …` line left; slogan + `Confidential \| For Internal Use Only` right. |
| **Background** | White, flat cards (no drop shadows), light border `#DDE3EE`, soft sky-blue corner wash bottom-right. |
| **Density** | Information-dense but scannable: short phrases, no paragraphs. ≤ 6 words per bullet, ≤ 3 bullets per box. Numbers big, deltas orange (`+25% YoY`). |
| **Language** | Match Tony's language for the deck (中文 or English); bilingual labels OK. |

## How to build (default path: editable .pptx)

```bash
pip install python-pptx          # once per environment
python .claude/skills/ppt-style/scripts/tony_deck.py spec.json out.pptx
```

Write a JSON spec (see `scripts/example_spec.json` — reproduces both references). Top level:
`{"orientation": "landscape" | "portrait", "slides": [ ... ]}` — landscape 16:9 by default;
portrait (7.5×13.3in) for phone / 抖音 / 小红书-style single-page infographics.

Slide types:

- **`cover`** — `title` (use `\n` for 2 lines), `highlight`, `subtitle`, `chips` (colored pills), `meta`, `slogan`.
- **`flow`** (the signature page) — `title`, `highlight`, `subtitle`, `badge` ("Line1\nLine2" top-right box),
  `rail: {header, labels[]}`, `bands[]`, `tagline`, `tagline_highlight`, `focus_bar`, `strategy[]`, `sources`, `slogan`, `confidential`.
  - band: `title`, `subtitle`, `tag`, `checks[]`, `columns[]`, `stars` (0–5, .5 allowed), `star_notes[]`, `star_label`, `hero`, `color` (override).
  - column: `header`, `items[]`, `icon`, `big` (e.g. "US$1.5B"), `delta` ("+15.2% YoY"), `stars`, `center`.
  - strategy card: `label`, `note`, `title`, `sub`, `stars`, `checks[]`, `color`.
- **`cards`** — grid of rated cards (like "Country Opportunity"): `cards[]` each with `title`, `subtitle`,
  `big`, `delta`, `stars`, `checks[]`, `hero`; `per_row`, `card_h`, `tagline`.

For layouts the templates don't cover (charts, tables, timelines), import `tony_deck` and compose
with its helpers (`Deck.new_slide`, `headline`, `band`, `flow_rail`, `tagline`, `pill`,
`circle_num`, `footer`) or add a new `*_slide` method — keep the tokens at the top of the file as
the single source of colors/fonts. For charts, use the same band palette in order.

## Checklist before delivering

1. Render to images and **look** at every slide:
   `soffice --headless --convert-to pdf out.pptx` then rasterize (e.g. `pymupdf`); needs
   `libreoffice-impress`. Fix overflow, overlaps, and clipped text.
2. Exactly one orange keyword in the headline; exactly one hero band per flow slide.
3. Band count 3–5; colors in the fixed order; rail labels match band count.
4. Every number has a source in the footer (or say "illustrative").
5. Deliver the .pptx to Tony (write it inside the working directory / send the file).

If Tony asks for HTML slides or an artifact instead of .pptx, apply the same tokens and layout
rules there.
