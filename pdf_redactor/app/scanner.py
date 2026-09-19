"""Orchestrates extraction -> (optional offline OCR) -> detection.

``PdfSession`` owns the open document for the lifetime of a review, so the GUI
can render preview pages and re-run scans without reopening the file or writing
anything to disk.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Set

from . import extractor, ocr
from .detectors import detect
from .models import (
    Category,
    DEFAULT_ENABLED,
    Detection,
    PageText,
    ScanResult,
    Source,
    next_uid,
)
from .pdfcompat import fitz
from .safety import get_logger

log = get_logger()

ProgressCallback = Callable[[int, int, str], None]


class PdfOpenError(RuntimeError):
    pass


class PdfSession:
    """An open PDF plus its most recent scan result.

    A PyMuPDF ``Document`` is not safe for concurrent use, and the GUI renders
    preview pages on the main thread while a scan runs on a worker thread. Every
    method that touches the document therefore takes ``self._lock``.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise PdfOpenError(f"File not found: {self.path}")
        try:
            self._doc = fitz.open(str(self.path))
        except Exception as exc:  # pragma: no cover - corrupt input
            raise PdfOpenError(f"Could not open PDF: {exc}") from exc
        # PyMuPDF happily opens .txt, .svg, .epub and images as "documents", so
        # an extension check is not enough - ask the parsed document what it is.
        # Accepting a non-PDF here would mean the redaction engine later fails,
        # or worse, appears to succeed on a file it never really processed.
        if not self._doc.is_pdf:
            self._doc.close()
            raise PdfOpenError(
                f"{self.path.name} is not a PDF. LocalRedact only processes PDF "
                "files; convert it to PDF first."
            )
        if self._doc.is_encrypted and not self._doc.authenticate(""):
            self._doc.close()
            raise PdfOpenError(
                "This PDF is password protected. Save an unprotected copy first."
            )
        self._lock = threading.RLock()
        self.result: Optional[ScanResult] = None
        self._page_text_cache: Dict[int, PageText] = {}

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._doc.close()
            except Exception:
                pass
            self._page_text_cache.clear()

    def __enter__(self) -> "PdfSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- document info -----------------------------------------------------
    @property
    def page_count(self) -> int:
        return self._doc.page_count

    def page_size(self, page_index: int):
        with self._lock:
            rect = self._doc[page_index].rect
            return rect.width, rect.height

    def render(self, page_index: int, zoom: float = 1.5) -> bytes:
        with self._lock:
            png, _ = extractor.render_page(self._doc[page_index], zoom=zoom)
            return png

    def source_metadata(self) -> Dict[str, str]:
        """Metadata present in the *input* file, shown so the user sees what
        will be stripped. Values are displayed in the UI only, never logged."""
        with self._lock:
            return {k: v for k, v in (self._doc.metadata or {}).items() if v}

    # -- scanning ----------------------------------------------------------
    def scan(
        self,
        categories: Optional[Set[Category]] = None,
        custom_terms: Sequence[str] = (),
        use_ocr: bool = False,
        ocr_engine: Optional[str] = None,
        ocr_languages: str = "eng",
        progress: Optional[ProgressCallback] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> ScanResult:
        enabled = set(categories) if categories is not None else set(DEFAULT_ENABLED)
        result = ScanResult(page_count=self.page_count)

        for index in range(self.page_count):
            if cancelled is not None and cancelled():
                result.notes.append("Scan cancelled by user; results are partial.")
                break
            if progress is not None:
                progress(index, self.page_count, f"Reading page {index + 1}")

            with self._lock:
                page = self._doc[index]
                page_text = extractor.extract_page_text(page)
                scanned = extractor.looks_scanned(page, page_text)

            if scanned:
                result.pages_without_text.append(index)
                if use_ocr:
                    if progress is not None:
                        progress(
                            index,
                            self.page_count,
                            f"OCR (offline) on page {index + 1}",
                        )
                    try:
                        with self._lock:
                            page_text = ocr.ocr_page(
                                page, engine=ocr_engine, languages=ocr_languages
                            )
                        result.ocr_pages.append(index)
                    except ocr.OcrUnavailable as exc:
                        result.notes.append(str(exc))
                    except Exception as exc:  # pragma: no cover - engine failure
                        result.notes.append(
                            f"OCR failed on page {index + 1}: {exc.__class__.__name__}"
                        )

            self._page_text_cache[index] = page_text
            matches = detect(page_text.text, custom_terms=custom_terms, categories=enabled)
            for match in matches:
                rects = page_text.rects_for(match.start, match.end)
                if not rects:
                    # No geometry means we could not place a box, so we must not
                    # claim we can redact it. Skipping is the honest outcome.
                    continue
                result.detections.append(
                    Detection(
                        uid=next_uid(),
                        page=index,
                        category=match.category,
                        text=match.text,
                        rects=rects,
                        confidence=match.confidence,
                        rule=match.rule,
                        source=page_text.source,
                        ocr_confidence=(
                            page_text.confidence_for(match.start, match.end)
                            if page_text.source is Source.OCR
                            else None
                        ),
                        selected=True,
                    )
                )

        if progress is not None:
            progress(self.page_count, self.page_count, "Scan complete")

        _add_scan_notes(result, use_ocr)
        self.result = result
        log.info(
            "Scanned %d page(s): %d candidate(s) found",
            result.page_count,
            len(result.detections),
        )
        return result

    def page_text(self, page_index: int) -> Optional[PageText]:
        return self._page_text_cache.get(page_index)


def _add_scan_notes(result: ScanResult, use_ocr: bool) -> None:
    unreadable = [
        p for p in result.pages_without_text if p not in result.ocr_pages
    ]
    if unreadable:
        pages = ", ".join(str(p + 1) for p in unreadable[:12])
        more = "" if len(unreadable) <= 12 else f" (+{len(unreadable) - 12} more)"
        if use_ocr:
            result.notes.append(
                f"Pages {pages}{more} have no text layer and OCR did not produce "
                "text. Any personal data on them was NOT detected - review them "
                "manually before sharing."
            )
        else:
            result.notes.append(
                f"Pages {pages}{more} look scanned and have no text layer. "
                "Enable offline OCR, or review them manually - nothing on these "
                "pages was scanned for personal data."
            )
    if result.ocr_pages:
        pages = ", ".join(str(p + 1) for p in result.ocr_pages[:12])
        more = "" if len(result.ocr_pages) <= 12 else f" (+{len(result.ocr_pages) - 12} more)"
        result.notes.append(
            f"Pages {pages}{more} were read by local OCR. OCR output is "
            "approximate: check every item marked REVIEW on those pages."
        )
    low = [d for d in result.detections if d.needs_review]
    if low:
        result.notes.append(
            f"{len(low)} item(s) are low confidence or OCR-derived and are "
            "marked REVIEW. They are ticked by default - untick any false "
            "positives before exporting."
        )
