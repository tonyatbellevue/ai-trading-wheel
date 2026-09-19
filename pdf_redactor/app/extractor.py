"""Turn a PDF page into a searchable string with a character-to-geometry map.

The whole design of this tool rests on one invariant: for every character in the
string the detectors see, we know the rectangle it occupies on the page. That is
what lets a regex hit become an exact redaction box instead of a guess.

``page.get_text("rawdict")`` is the only extraction mode that exposes per-glyph
bounding boxes, so it is what we build on.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .pdfcompat import fitz

from .models import PageText, Rect, Source

# Horizontal gap (in points) above which two adjacent spans are treated as
# separated by whitespace even though no space glyph was emitted. Justified text
# and table cells routinely omit the space.
_SPACE_GAP_PT = 1.2


def _normalise(rect) -> Rect:
    x0, y0, x1, y1 = rect
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def extract_page_text(page: "fitz.Page") -> PageText:
    """Extract ``page``'s text layer with per-character rectangles."""
    chars: List[str] = []
    rects: List[Optional[Rect]] = []
    line_ids: List[int] = []

    raw = page.get_text("rawdict")
    line_counter = 0

    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:
            continue  # image block - handled by the OCR path
        for line in block.get("lines", []):
            line_counter += 1
            prev_x1: Optional[float] = None
            wrote_any = False
            for span in line.get("spans", []):
                span_chars = span.get("chars", [])
                if not span_chars:
                    continue
                first_x0 = _normalise(span_chars[0]["bbox"])[0]
                if (
                    prev_x1 is not None
                    and first_x0 - prev_x1 > _SPACE_GAP_PT
                    and chars
                    and not chars[-1].isspace()
                ):
                    chars.append(" ")
                    rects.append(None)
                    line_ids.append(line_counter)
                for ch in span_chars:
                    glyph = ch.get("c", "")
                    if not glyph:
                        continue
                    box = _normalise(ch["bbox"])
                    chars.append(glyph)
                    rects.append(box)
                    line_ids.append(line_counter)
                    prev_x1 = box[2]
                    wrote_any = True
            if wrote_any:
                chars.append("\n")
                rects.append(None)
                line_ids.append(line_counter)
        # Blank line between blocks keeps unrelated paragraphs from merging into
        # one "line" for the multi-line label rules.
        if chars and chars[-1] != "\n":
            chars.append("\n")
            rects.append(None)
            line_ids.append(line_counter)

    return PageText(
        page=page.number,
        text="".join(chars),
        char_rects=rects,
        line_ids=line_ids,
        source=Source.TEXT,
        width=page.rect.width,
        height=page.rect.height,
    )


def page_has_meaningful_text(page_text: PageText, min_chars: int = 24) -> bool:
    """True if the text layer carries enough content to be worth scanning.

    A scanned page often still yields a handful of characters (a stamp, a page
    number), so an emptiness test is not enough; we require a real minimum.
    """
    return len(page_text.text.strip()) >= min_chars


def page_image_coverage(page: "fitz.Page") -> float:
    """Fraction of the page area covered by raster images.

    Used together with the text-length test to decide whether a page is a scan
    and should be offered to the OCR path.
    """
    try:
        page_area = abs(page.rect.width * page.rect.height)
        if page_area <= 0:
            return 0.0
        covered = 0.0
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if not bbox:
                continue
            x0, y0, x1, y1 = _normalise(bbox)
            covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
        return min(1.0, covered / page_area)
    except Exception:
        return 0.0


def looks_scanned(page: "fitz.Page", page_text: PageText) -> bool:
    if page_has_meaningful_text(page_text):
        return False
    return page_image_coverage(page) > 0.3 or not page_text.text.strip()


def render_page(page: "fitz.Page", zoom: float = 2.0) -> Tuple[bytes, float]:
    """Render a page to PNG bytes for the preview canvas.

    Returns the PNG bytes and the zoom actually used, so the GUI can map PDF
    coordinates to canvas pixels.
    """
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    try:
        return pix.tobytes("png"), zoom
    finally:
        # Release the pixmap buffer promptly; a 300 dpi A4 page is ~25 MB and we
        # never want more than one alive at a time.
        del pix
