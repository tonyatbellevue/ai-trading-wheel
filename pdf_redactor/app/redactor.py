"""True PDF redaction: remove the underlying content, then prove it is gone.

A black rectangle drawn over text is not redaction - the glyphs stay in the
content stream and any text extractor recovers them. This module instead uses
MuPDF redaction annotations, which rewrite the page content stream so the
covered glyphs, vector art and image pixels no longer exist in the file.

The export is a four-stage pipeline:

1. **apply** - add a redaction annotation per selected rectangle and apply them
   with ``text=PDF_REDACT_TEXT_REMOVE`` and ``images=PDF_REDACT_IMAGE_PIXELS``,
   so raster content under a box is repainted, not merely hidden.
2. **scrub** - drop metadata, XMP, embedded files, JavaScript, attachments,
   form-field values, link targets, thumbnails and any invisible text layer.
3. **save** - non-incremental, ``garbage=4`` (full object GC + dedup) and
   ``clean=True``, which is what physically drops the superseded objects.
4. **verify** - reopen the written file and confirm that no text can be
   extracted from any redacted rectangle and that the metadata is empty.

Stage 4 is not optional. It is the only honest way to tell the user the
redaction worked, and its result is surfaced in the export summary.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Detection, RedactionReport, Rect, merge_rects
from .pdfcompat import fitz
from .safety import get_logger

log = get_logger()

# Grow each box slightly so glyph side bearings, descenders and antialiased
# edges are fully covered. Too small leaves a readable sliver; too large eats
# neighbouring words.
DEFAULT_PADDING_PT = 1.0

# Inset used when verifying, so a box that merely touches the next character's
# bounding box does not read as a failure.
_VERIFY_INSET_PT = 0.75

BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)


class RedactionError(RuntimeError):
    pass


def _pad(rect: Rect, pad: float) -> Rect:
    return (rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)


def _clip_to_page(rect: Rect, page) -> Optional[Rect]:
    pr = page.rect
    x0 = max(rect[0], pr.x0)
    y0 = max(rect[1], pr.y0)
    x1 = min(rect[2], pr.x1)
    y1 = min(rect[3], pr.y1)
    if x1 - x0 <= 0.1 or y1 - y0 <= 0.1:
        return None
    return (x0, y0, x1, y1)


def _remove_annotations(page, keep_links: bool = False) -> int:
    """Delete annotations, which can carry copies of the redacted text.

    Form-field widgets store their value in the annotation object, and comment
    or popup annotations store free text; neither is part of the page content
    stream, so ``apply_redactions`` does not touch them.
    """
    removed = 0
    annot = page.first_annot
    to_delete = []
    while annot:
        to_delete.append(annot)
        annot = annot.next
    for annot in to_delete:
        try:
            if keep_links and annot.type[0] == fitz.PDF_ANNOT_LINK:
                continue
            page.delete_annot(annot)
            removed += 1
        except Exception:  # pragma: no cover - malformed annotation
            continue
    return removed


def _remove_acroform(doc) -> int:
    """Drop the document-level AcroForm dictionary.

    Widget annotations are removed per page, but the AcroForm entry can retain
    field definitions (including default values) at the catalog level.
    """
    try:
        catalog = doc.pdf_catalog()
        if doc.xref_get_key(catalog, "AcroForm")[0] != "null":
            doc.xref_set_key(catalog, "AcroForm", "null")
            return 1
    except Exception:  # pragma: no cover - not a PDF with a catalog we can edit
        pass
    return 0


def _count_embedded_files(doc) -> int:
    try:
        return doc.embfile_count()
    except Exception:
        return 0


def _has_javascript(doc) -> bool:
    try:
        catalog = doc.pdf_catalog()
        return doc.xref_get_key(catalog, "Names/JavaScript")[0] != "null"
    except Exception:
        return False


def plan_rectangles(
    detections: Sequence[Detection], padding: float = DEFAULT_PADDING_PT
) -> Dict[int, List[Rect]]:
    """Group the selected detections' rectangles by page, padded and merged."""
    by_page: Dict[int, List[Rect]] = {}
    for det in detections:
        if not det.selected:
            continue
        for rect in det.rects:
            by_page.setdefault(det.page, []).append(_pad(rect, padding))
    return {page: merge_rects(rects) for page, rects in by_page.items()}


def redact_pdf(
    input_path: str | os.PathLike,
    output_path: str | os.PathLike,
    detections: Sequence[Detection],
    *,
    padding: float = DEFAULT_PADDING_PT,
    fill: Optional[Tuple[float, float, float]] = BLACK,
    remove_annotations: bool = True,
    remove_hidden_text: bool = True,
    scrub_metadata: bool = True,
    verify: bool = True,
) -> RedactionReport:
    """Redact ``input_path`` into ``output_path`` and verify the result.

    ``fill`` of ``None`` leaves the redacted area blank (white) instead of
    painting a black box; the content is removed either way.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    if input_path.resolve() == output_path.resolve():
        raise RedactionError(
            "Refusing to write the redacted PDF over the original. "
            "Choose a different output file."
        )

    selected = [d for d in detections if d.selected]
    rect_plan = plan_rectangles(selected, padding)

    doc = fitz.open(str(input_path))
    embedded_before = _count_embedded_files(doc)
    javascript_before = 1 if _has_javascript(doc) else 0
    annotations_removed = 0
    applied = 0

    try:
        if doc.is_encrypted and not doc.authenticate(""):
            raise RedactionError(
                "This PDF is password protected. Open it with its password and "
                "save an unprotected copy first."
            )

        for page_index in range(doc.page_count):
            page = doc[page_index]
            rects = rect_plan.get(page_index, [])
            if remove_annotations:
                annotations_removed += _remove_annotations(page)
            if not rects:
                continue
            for rect in rects:
                clipped = _clip_to_page(rect, page)
                if clipped is None:
                    continue
                page.add_redact_annot(fitz.Rect(*clipped), fill=fill)
                applied += 1
            # text=REMOVE deletes the glyphs from the content stream;
            # images=PIXELS repaints the covered area of any raster image, which
            # is what makes this work on scanned pages;
            # graphics=REMOVE_IF_COVERED drops vector art hidden by the box.
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_PIXELS,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )

        if remove_annotations:
            _remove_acroform(doc)

        scrub_ok = False
        if scrub_metadata:
            # redactions=False: we already applied them above with the image
            # mode we want, and letting scrub re-run them would be a no-op at
            # best. Everything else here is document-level leakage.
            try:
                doc.scrub(
                    attached_files=True,
                    clean_pages=True,
                    embedded_files=True,
                    hidden_text=remove_hidden_text,
                    javascript=True,
                    metadata=True,
                    redactions=False,
                    remove_links=True,
                    reset_fields=True,
                    reset_responses=True,
                    thumbnails=True,
                    xml_metadata=True,
                )
                scrub_ok = True
            except Exception as exc:  # pragma: no cover - unusual document
                # Losing scrub() is survivable; losing the redaction is not. Fall
                # through to the explicit metadata clearing below and let the
                # report say that the deep scrub did not run.
                log.warning(
                    "Deep scrub failed (%s); falling back to explicit metadata "
                    "clearing. Embedded files and JavaScript may remain.",
                    exc.__class__.__name__,
                )
            doc.set_metadata({})
            try:
                doc.del_xml_metadata()
            except Exception:  # pragma: no cover - no XMP present
                pass
            # scrub() leaves the producer free-form; set it explicitly so the
            # output does not advertise the original authoring tool.
            doc.set_metadata(
                {
                    "producer": "",
                    "creator": "",
                    "title": "",
                    "author": "",
                    "subject": "",
                    "keywords": "",
                    "creationDate": "",
                    "modDate": "",
                }
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Non-incremental save with full garbage collection is the step that
        # physically removes the superseded content streams from the file.
        doc.save(
            str(output_path),
            garbage=4,
            deflate=True,
            clean=True,
            incremental=False,
            pretty=False,
        )
        pages_touched = len(rect_plan)
    finally:
        doc.close()

    report = RedactionReport(
        output_path=str(output_path),
        applied=applied,
        pages_touched=pages_touched,
        metadata_cleared=scrub_metadata,
        xmp_removed=scrub_metadata,
        embedded_files_removed=embedded_before if scrub_ok else 0,
        javascript_removed=javascript_before if scrub_ok else 0,
        annotations_removed=annotations_removed,
        verified_absent=0,
    )

    if verify:
        _verify(output_path, rect_plan, selected, detections, report, padding)

    log.info(
        "Redaction complete: %d boxes across %d page(s); verification %s",
        report.applied,
        report.pages_touched,
        "passed" if report.verification_passed else "FAILED",
    )
    return report


def _verify(
    output_path: Path,
    rect_plan: Dict[int, List[Rect]],
    selected: Sequence[Detection],
    all_detections: Sequence[Detection],
    report: RedactionReport,
    padding: float,
) -> None:
    """Reopen the written PDF and prove the redacted content is unrecoverable.

    Two independent checks:

    * **geometric** - extracting text clipped to each redaction rectangle must
      return nothing. This is the direct test that the glyphs are gone.
    * **lexical** - any value whose every occurrence was selected must no longer
      appear anywhere in the page's text.
    """
    doc = fitz.open(str(output_path))
    try:
        for page_index, rects in rect_plan.items():
            if page_index >= doc.page_count:
                continue
            page = doc[page_index]
            for rect in rects:
                inset = (
                    rect[0] + _VERIFY_INSET_PT,
                    rect[1] + _VERIFY_INSET_PT,
                    rect[2] - _VERIFY_INSET_PT,
                    rect[3] - _VERIFY_INSET_PT,
                )
                if inset[2] <= inset[0] or inset[3] <= inset[1]:
                    continue
                residual = page.get_text("text", clip=fitz.Rect(*inset)).strip()
                if residual:
                    report.verification_failures.append(
                        f"page {page_index + 1}: text still extractable inside a "
                        f"redaction box ({len(residual)} chars)"
                    )
                    if page_index not in report.residual_text_pages:
                        report.residual_text_pages.append(page_index)
                else:
                    report.verified_absent += 1

        # Lexical check. Only values whose occurrences were *all* selected can be
        # required to vanish; a partially selected value legitimately survives.
        fully_selected: Dict[Tuple[int, str], bool] = {}
        for det in all_detections:
            key = (det.page, " ".join(det.text.split()))
            fully_selected[key] = fully_selected.get(key, True) and det.selected

        by_page_text: Dict[int, str] = {}
        for det in selected:
            if det.page not in by_page_text and det.page < doc.page_count:
                by_page_text[det.page] = doc[det.page].get_text("text")
        for det in selected:
            needle = " ".join(det.text.split())
            if len(needle) < 4:
                continue  # too short to test without false alarms
            if not fully_selected.get((det.page, needle), False):
                continue  # the user kept another copy of this value on purpose
            haystack = " ".join(by_page_text.get(det.page, "").split())
            if needle and needle in haystack:
                report.verification_failures.append(
                    f"page {det.page + 1}: a redacted {det.category.value} value "
                    f"is still present in the output text layer"
                )
                if det.page not in report.residual_text_pages:
                    report.residual_text_pages.append(det.page)

        # Metadata must be empty.
        meta = doc.metadata or {}
        leftovers = [
            key
            for key, value in meta.items()
            if key not in ("format", "encryption") and value
        ]
        if leftovers and report.metadata_cleared:
            report.verification_failures.append(
                "document metadata still contains: " + ", ".join(sorted(leftovers))
            )
    finally:
        doc.close()


def extract_all_text(path: str | os.PathLike) -> str:
    """Full text of a PDF. Used by tests and by the CLI's --verify mode."""
    doc = fitz.open(str(path))
    try:
        return "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()


def document_metadata(path: str | os.PathLike) -> Dict[str, str]:
    doc = fitz.open(str(path))
    try:
        return dict(doc.metadata or {})
    finally:
        doc.close()
