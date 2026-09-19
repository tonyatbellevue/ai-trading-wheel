"""End-to-end check that runs on the Windows CI runner.

The engine test suite already covers correctness in depth. This adds the one
thing only a Windows runner can tell us: that the exact dependency set the .exe
is built from still performs a real redaction on Windows, with Windows path
handling and line endings.

    python tests/ci_windows_e2e.py
"""

from __future__ import annotations

import binascii
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdfcompat import fitz          # noqa: E402
from app.redactor import redact_pdf     # noqa: E402
from app.scanner import PdfSession      # noqa: E402

SECRETS = ("Anderson", "S1234567D", "john.anderson", "4111 1111")


def _forms(value: str):
    raw = value.encode()
    return (
        raw,
        binascii.hexlify(raw),
        binascii.hexlify(raw).upper(),
        binascii.hexlify(value.encode("utf-16-be")),
        binascii.hexlify(value.encode("utf-16-be")).upper(),
    )


def _leaks(path: Path):
    blobs = [path.read_bytes()]
    doc = fitz.open(str(path))
    try:
        for index in range(doc.page_count):
            blobs.append(doc[index].read_contents())
        for xref in range(1, doc.xref_length()):
            try:
                stream = doc.xref_stream(xref)
                if stream:
                    blobs.append(stream)
            except Exception:
                pass
            try:
                blobs.append(
                    doc.xref_object(xref, compressed=False).encode("latin-1", "ignore")
                )
            except Exception:
                pass
    finally:
        doc.close()
    return [
        secret
        for secret in SECRETS
        if any(any(b.count(form) for b in blobs) for form in _forms(secret))
    ]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="localredact-ci-"))
    src = tmp / "ci_sample.pdf"
    out = tmp / "ci_out.pdf"

    doc = fitz.open()
    page = doc.new_page()
    for index, line in enumerate(
        [
            "Name: John Anderson",
            "NRIC No: S1234567D",
            "Email: john.anderson@example.com",
            "Credit Card: 4111 1111 1111 1111",
        ]
    ):
        page.insert_text((60, 90 + index * 22), line, fontsize=11, fontname="helv")
    doc.set_metadata({"title": "John Anderson", "author": "Sarah Lim"})
    doc.save(str(src))
    doc.close()

    assert _leaks(src), "fixture should contain the secrets before redaction"

    with PdfSession(src) as session:
        result = session.scan()
        report = redact_pdf(src, out, result.detections)

    if not report.verification_passed:
        print("VERIFICATION FAILED:", report.verification_failures, file=sys.stderr)
        return 1

    leaked = _leaks(out)
    if leaked:
        print(f"CONTENT SURVIVED REDACTION ON WINDOWS: {leaked}", file=sys.stderr)
        return 1

    meta = {k: v for k, v in (fitz.open(str(out)).metadata or {}).items()
            if v and k not in ("format", "encryption")}
    if meta:
        print(f"METADATA SURVIVED: {sorted(meta)}", file=sys.stderr)
        return 1

    print(
        f"Windows end-to-end OK: {report.applied} area(s) redacted, "
        f"{report.verified_absent} verified empty, 0 bytes leaked, metadata clear"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
