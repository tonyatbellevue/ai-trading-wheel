"""Headless interface, for batch work and for verifying the engine without a GUI.

    python -m app.cli report.pdf                       # list candidates, write nothing
    python -m app.cli report.pdf -o clean.pdf          # redact everything found
    python -m app.cli report.pdf -o clean.pdf --min-confidence 0.9
    python -m app.cli report.pdf -o clean.pdf --ocr --ocr-lang eng+chi_sim
    python -m app.cli --inspect clean.pdf              # what is left in a PDF

Values are masked in the output unless ``--reveal`` is passed, so a terminal
transcript or a redirected log does not become a copy of the PII.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence, Set

from . import APP_NAME, __version__
from .models import CATEGORY_LABELS, DEFAULT_ENABLED, Category, Source
from .redactor import document_metadata, extract_all_text, redact_pdf
from .safety import enable_network_lockdown, mask
from .scanner import PdfOpenError, PdfSession


def _parse_categories(values: Optional[Sequence[str]]) -> Set[Category]:
    if not values:
        return set(DEFAULT_ENABLED)
    if len(values) == 1 and values[0].lower() == "all":
        return set(Category)
    chosen: Set[Category] = set()
    for value in values:
        try:
            chosen.add(Category[value.strip().upper()])
        except KeyError:
            valid = ", ".join(c.name for c in Category)
            raise SystemExit(f"Unknown category '{value}'. Valid: {valid}")
    return chosen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localredact",
        description=f"{APP_NAME} {__version__} - fully offline PDF redaction.",
    )
    parser.add_argument("input", help="PDF to scan")
    parser.add_argument("-o", "--output", help="write the redacted PDF here")
    parser.add_argument(
        "-c", "--categories", nargs="+",
        help="categories to detect, or 'all' (default: the standard PII set)",
    )
    parser.add_argument(
        "-t", "--terms", nargs="+", default=[],
        help="extra literal strings to redact, e.g. your own name",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.0,
        help="only redact detections at or above this confidence (0-1)",
    )
    parser.add_argument("--ocr", action="store_true", help="use local offline OCR")
    parser.add_argument("--ocr-lang", default="eng", help="Tesseract language codes")
    parser.add_argument(
        "--reveal", action="store_true",
        help="print detected values in full instead of masked",
    )
    parser.add_argument(
        "--no-verify", action="store_true", help="skip the post-write verification"
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="report the text and metadata still present in INPUT, then exit",
    )
    return parser


def _inspect(path: str) -> int:
    meta = {k: v for k, v in document_metadata(path).items() if v and k != "format"}
    text = extract_all_text(path)
    print(f"Metadata fields present: {len(meta)}")
    for key, value in sorted(meta.items()):
        print(f"  {key}: {value}")
    print(f"Extractable text: {len(text.strip())} characters")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    enable_network_lockdown()
    args = build_parser().parse_args(argv)

    if args.inspect:
        return _inspect(args.input)

    try:
        session = PdfSession(args.input)
    except PdfOpenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with session:
        result = session.scan(
            categories=_parse_categories(args.categories),
            custom_terms=args.terms,
            use_ocr=args.ocr,
            ocr_languages=args.ocr_lang,
            progress=lambda i, n, m: print(f"\r{m} ({i}/{n})", end="", file=sys.stderr),
        )
        print("", file=sys.stderr)

        for det in result.detections:
            if det.confidence < args.min_confidence:
                det.selected = False
            flag = "REVIEW" if det.needs_review else "      "
            source = "OCR" if det.source is Source.OCR else "txt"
            state = "x" if det.selected else " "
            value = det.text if args.reveal else mask(det.text)
            print(
                f"[{state}] p{det.page + 1:<3} {flag} {source} "
                f"{det.confidence:5.0%}  {CATEGORY_LABELS[det.category]:<26} {value}"
            )

        for note in result.notes:
            print(f"note: {note}", file=sys.stderr)

        selected = [d for d in result.detections if d.selected]
        print(
            f"\n{len(result.detections)} candidate(s), {len(selected)} selected, "
            f"{sum(1 for d in result.detections if d.needs_review)} need review",
            file=sys.stderr,
        )

        if not args.output:
            print("No --output given; nothing was written.", file=sys.stderr)
            return 0
        if not selected:
            print("Nothing selected; nothing was written.", file=sys.stderr)
            return 1

        report = redact_pdf(
            args.input, args.output, result.detections, verify=not args.no_verify
        )

    print(
        f"\nWrote {report.output_path}: {report.applied} area(s) on "
        f"{report.pages_touched} page(s); metadata cleared.",
        file=sys.stderr,
    )
    if args.no_verify:
        return 0
    if report.verification_passed:
        print(
            f"VERIFIED: {report.verified_absent} redacted area(s) contain no "
            "extractable text.",
            file=sys.stderr,
        )
        return 0
    print("VERIFICATION FAILED:", file=sys.stderr)
    for failure in report.verification_failures:
        print(f"  - {failure}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
