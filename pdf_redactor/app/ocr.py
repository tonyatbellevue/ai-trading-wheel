"""Strictly offline OCR for scanned pages.

Two engines are supported, both of which run entirely on the local machine:

* **Tesseract** via ``pytesseract``. Preferred, because it reports word-level
  boxes, which keeps redaction rectangles tight. Needs the Tesseract binary
  (bundled beside the .exe, or installed locally).
* **RapidOCR** (``rapidocr-onnxruntime``). Pure pip install with the ONNX models
  shipped inside the wheel, so nothing is ever downloaded. Boxes are line-level,
  so character rectangles are interpolated across the line - slightly wider
  redaction boxes, which errs on the safe side.

Neither engine performs network I/O. ``app.safety`` would raise if one tried.

Everything OCR produces is marked ``Source.OCR`` and carries a per-character
confidence, so the UI can flag it for mandatory human review.
"""

from __future__ import annotations

import io
import os
import sys
from typing import List, Optional, Tuple

from .models import PageText, Rect, Source
from .pdfcompat import fitz
from .safety import get_logger

log = get_logger()

# Rendering DPI for OCR. 300 is the usual accuracy/speed sweet spot; below ~200
# Tesseract's accuracy on 9pt body text falls off sharply.
OCR_DPI = 300
_BASE_DPI = 72.0


class OcrUnavailable(RuntimeError):
    """No offline OCR engine could be initialised."""


def _tesseract_cmd() -> Optional[str]:
    """Locate a Tesseract binary without touching the network.

    Checks, in order: an explicit env var, a copy shipped next to the frozen
    .exe, then the usual Windows install locations and PATH.
    """
    explicit = os.environ.get("LOCALREDACT_TESSERACT")
    if explicit and os.path.isfile(explicit):
        return explicit

    candidates: List[str] = []
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        candidates.append(os.path.join(base, "tesseract", "tesseract.exe"))
        candidates.append(os.path.join(base, "tesseract.exe"))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(here, "tesseract", "tesseract.exe"))
    candidates.extend(
        [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/opt/homebrew/bin/tesseract",
        ]
    )
    for path in candidates:
        if os.path.isfile(path):
            return path

    from shutil import which

    return which("tesseract")


def available_engines() -> List[str]:
    """Names of offline OCR engines usable right now, best first."""
    engines: List[str] = []
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401

        if _tesseract_cmd():
            engines.append("tesseract")
    except ImportError:
        pass
    try:
        import rapidocr_onnxruntime  # noqa: F401

        engines.append("rapidocr")
    except ImportError:
        pass
    return engines


def ocr_status_text() -> str:
    engines = available_engines()
    if not engines:
        return "OCR: not installed (scanned pages cannot be scanned for PII)"
    return "OCR: %s (offline)" % ", ".join(engines)


def _render_for_ocr(page) -> Tuple[bytes, float]:
    zoom = OCR_DPI / _BASE_DPI
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    try:
        return pix.tobytes("png"), zoom
    finally:
        del pix


def _build_page_text(
    page_number: int,
    tokens: List[Tuple[str, Rect, float, int]],
    width: float,
    height: float,
) -> PageText:
    """Assemble a :class:`PageText` from ``(text, rect, confidence, line_id)``.

    Character rectangles are interpolated linearly across each token's box. For
    word-level tokens this is accurate to within a glyph; for line-level tokens
    it is approximate, which only ever makes the redaction box larger.
    """
    chars: List[str] = []
    rects: List[Optional[Rect]] = []
    line_ids: List[int] = []
    confs: List[float] = []

    last_line: Optional[int] = None
    for text, box, conf, line_id in tokens:
        if not text:
            continue
        if last_line is not None and line_id != last_line:
            chars.append("\n")
            rects.append(None)
            line_ids.append(last_line)
            confs.append(1.0)
        elif last_line is not None:
            chars.append(" ")
            rects.append(None)
            line_ids.append(line_id)
            confs.append(1.0)
        last_line = line_id

        x0, y0, x1, y1 = box
        span = max(1, len(text))
        step = (x1 - x0) / span
        for index, glyph in enumerate(text):
            cx0 = x0 + step * index
            chars.append(glyph)
            rects.append((cx0, y0, cx0 + step, y1))
            line_ids.append(line_id)
            confs.append(conf)

    if chars:
        chars.append("\n")
        rects.append(None)
        line_ids.append(last_line or 0)
        confs.append(1.0)

    return PageText(
        page=page_number,
        text="".join(chars),
        char_rects=rects,
        line_ids=line_ids,
        source=Source.OCR,
        char_conf=confs,
        width=width,
        height=height,
    )


def _ocr_tesseract(page, languages: str) -> PageText:
    import pytesseract
    from PIL import Image
    from pytesseract import Output

    cmd = _tesseract_cmd()
    if not cmd:
        raise OcrUnavailable("Tesseract binary not found")
    pytesseract.pytesseract.tesseract_cmd = cmd

    png, zoom = _render_for_ocr(page)
    image = Image.open(io.BytesIO(png))
    try:
        data = pytesseract.image_to_data(
            image, lang=languages, output_type=Output.DICT
        )
    finally:
        image.close()

    tokens: List[Tuple[str, Rect, float, int]] = []
    line_key_to_id: dict = {}
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            conf = 0.0
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        line_id = line_key_to_id.setdefault(key, len(line_key_to_id) + 1)
        left = data["left"][i] / zoom
        top = data["top"][i] / zoom
        right = left + data["width"][i] / zoom
        bottom = top + data["height"][i] / zoom
        tokens.append((text, (left, top, right, bottom), conf / 100.0, line_id))

    return _build_page_text(
        page.number, tokens, page.rect.width, page.rect.height
    )


def _ocr_rapidocr(page) -> PageText:
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    png, zoom = _render_for_ocr(page)
    image = Image.open(io.BytesIO(png)).convert("RGB")
    try:
        array = np.asarray(image)
    finally:
        image.close()

    result, _elapsed = engine(array)
    tokens: List[Tuple[str, Rect, float, int]] = []
    for line_id, entry in enumerate(result or [], start=1):
        box, text, score = entry[0], entry[1], entry[2]
        xs = [float(p[0]) / zoom for p in box]
        ys = [float(p[1]) / zoom for p in box]
        tokens.append(
            (
                str(text),
                (min(xs), min(ys), max(xs), max(ys)),
                float(score),
                line_id,
            )
        )

    return _build_page_text(
        page.number, tokens, page.rect.width, page.rect.height
    )


def ocr_page(page, engine: Optional[str] = None, languages: str = "eng") -> PageText:
    """Run offline OCR on ``page`` and return text with geometry.

    ``engine`` may be ``"tesseract"``, ``"rapidocr"`` or ``None`` (auto).
    """
    engines = available_engines()
    if not engines:
        raise OcrUnavailable(
            "No offline OCR engine is installed. Install pytesseract plus the "
            "Tesseract binary, or 'pip install rapidocr-onnxruntime'."
        )
    chosen = engine or engines[0]
    if chosen not in engines:
        raise OcrUnavailable(f"OCR engine '{chosen}' is not available")

    log.info("Running offline OCR (%s) on page %d", chosen, page.number + 1)
    if chosen == "tesseract":
        return _ocr_tesseract(page, languages)
    return _ocr_rapidocr(page)
