"""Test suite for LocalRedact.

Runs with pytest, or standalone:  python tests/test_localredact.py

The redaction tests do not trust the text-extraction API to tell them the truth.
They rebuild the output's raw bytes, every decompressed object and every page
content stream, and search all of them for the secret in ASCII, hex and UTF-16
form - because that is how a PDF actually stores glyphs, and it is the only way
to prove the content was deleted rather than covered.
"""

from __future__ import annotations

import binascii
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import validators as v                                   # noqa: E402
from app.detectors import detect                                  # noqa: E402
from app.extractor import extract_page_text                       # noqa: E402
from app.models import Category, Detection, Source, merge_rects   # noqa: E402
from app.pdfcompat import fitz                                    # noqa: E402
from app.redactor import RedactionError, redact_pdf               # noqa: E402
from app.safety import NetworkBlockedError, SecureTempDir         # noqa: E402
from app.scanner import PdfSession                                # noqa: E402

SAMPLE_LINES = [
    "CONFIDENTIAL PATIENT RECORD",
    "Name: John Anderson",
    "Date of Birth: 14/03/1982",
    "NRIC No: S1234567D",
    "Passport No: E1234567",
    "Address: 21 Orchard Road, #12-05, Singapore 238888",
    "Mobile: +65 9123 4567",
    "Email: john.anderson@example.com",
    "Bank Account No: 123-456-789012",
    "Credit Card: 4111 1111 1111 1111",
    "Attending physician was Dr. Sarah Lim.",
    "Invoice Number 8829371 for Total Amount 1,250.00 USD.",
]

SECRETS = [
    "Anderson", "S1234567D", "john.anderson", "4111 1111", "E1234567",
    "Orchard Road", "238888", "9123 4567", "Sarah Lim",
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def make_sample(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in SAMPLE_LINES:
        page.insert_text((60, y), line, fontsize=11, fontname="helv")
        y += 20
    doc.set_metadata(
        {
            "title": "Patient John Anderson",
            "author": "Dr Sarah Lim",
            "subject": "NRIC S1234567D",
            "keywords": "john.anderson@example.com",
            "creator": "SecretApp",
            "producer": "SecretApp",
        }
    )
    doc.save(str(path))
    doc.close()
    return path


def _variants(value: str):
    raw = value.encode()
    return [
        raw,
        binascii.hexlify(raw),
        binascii.hexlify(raw).upper(),
        binascii.hexlify(value.encode("utf-16-be")),
        binascii.hexlify(value.encode("utf-16-be")).upper(),
    ]


def deep_leaks(path: Path, secrets=SECRETS) -> dict:
    """Every place a PDF can hide a string, searched in every encoding."""
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
    found = {}
    for secret in secrets:
        count = sum(sum(b.count(form) for b in blobs) for form in _variants(secret))
        if count:
            found[secret] = count
    return found


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------
def test_luhn():
    assert v.luhn_valid("4111 1111 1111 1111")
    assert v.luhn_valid("378282246310005")
    assert not v.luhn_valid("4111 1111 1111 1112")
    assert not v.luhn_valid("12345")


def test_nric():
    assert v.nric_valid("S1234567D")
    assert v.nric_valid("F1234567N")
    assert not v.nric_valid("S1234567A")
    assert not v.nric_valid("INVOICE12")


def test_other_national_ids():
    assert v.china_id_valid("11010519491231002X")
    assert not v.china_id_valid("110105194912310021")
    assert not v.china_id_valid("11010519491331002X")   # month 13
    assert v.ssn_plausible("123-45-6789")
    assert not v.ssn_plausible("666-45-6789")
    assert v.iban_valid("GB82 WEST 1234 5698 7654 32")
    assert not v.iban_valid("GB82 WEST 1234 5698 7654 33")
    assert v.hkid_valid("A123456(3)")
    assert not v.hkid_valid("A123456(4)")


def test_dates():
    assert v.parse_date("1985-03-04").isoformat() == "1985-03-04"
    assert v.parse_date("31/02/1990") is None                     # no such day
    assert v.plausible_birth_date("14/03/1982")
    assert not v.plausible_birth_date("2099-01-01")               # future
    assert not v.plausible_birth_date("1850-01-01")               # age > 120


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------
def test_detects_the_standard_set():
    found = {m.category for m in detect("\n".join(SAMPLE_LINES))}
    for expected in (
        Category.NAME, Category.NATIONAL_ID, Category.PASSPORT, Category.ADDRESS,
        Category.PHONE, Category.EMAIL, Category.DATE_OF_BIRTH,
        Category.BANK_ACCOUNT, Category.CREDIT_CARD,
    ):
        assert expected in found, f"missed {expected.value}"


def test_rejects_lookalikes():
    """Long digit runs that fail Luhn are not cards; order numbers are not PII."""
    text = "Invoice 1234567812345670000 ref ABC123. Order 8829371."
    hits = detect(text)
    assert not any(m.category is Category.CREDIT_CARD for m in hits)
    assert not any(m.confidence >= 0.75 for m in hits), hits


def test_checksum_failure_downgrades_but_does_not_discard():
    """An NRIC-shaped string with a bad checksum stays visible, low confidence.

    Dropping it would hide real NRICs that OCR mis-read by one character, so it
    is surfaced for review instead - but it must never reach high confidence.
    """
    hits = [m for m in detect("Serial S1234567A.") if m.text == "S1234567A"]
    assert hits, "an NRIC-shaped value should still be surfaced"
    assert all(m.confidence < 0.75 for m in hits), hits

    good = [m for m in detect("NRIC No: S1234567D") if m.text == "S1234567D"]
    assert good and good[0].confidence >= 0.95, "a valid checksum must score high"


def test_labels_do_not_match_inside_words():
    """Regression: the "ic" label matched inside "Public notice".

    Several labels are two letters ("ic", "hp", "b"), so the label alternation
    must assert a non-letter on both sides. Without it, ordinary English prose
    produced high-confidence national-id hits.
    """
    for phrase in (
        "Public notice.", "The republic of x", "Basic terms apply.",
        "Traffic report 2024", "Hydraulic pressure test",
    ):
        assert detect(phrase) == [], f"false positive on {phrase!r}: {detect(phrase)}"

    # ...while the real labels still work.
    assert any(m.text == "S1234567D" for m in detect("IC No: S1234567D"))
    assert any(m.text == "E1234567" for m in detect("Passport No: E1234567"))
    assert any(m.text == "Sarah Lim" for m in detect("Dr. Sarah Lim"))
    assert any(m.text == "14/03/1982" for m in detect("D.O.B: 14/03/1982"))


def test_clean_document_is_mostly_quiet():
    clean = (
        "QUARTERLY REVENUE SUMMARY\n"
        "Total Revenue 1,250,000. Gross Margin 42%. Order Number 8829371.\n"
        "Section 3 of the Agreement. See Appendix B and Table 4.\n"
    )
    high = [m for m in detect(clean) if m.confidence >= 0.75]
    assert high == [], f"false positives at high confidence: {high}"


def test_custom_terms():
    hits = detect("Project Bluebird is confidential.", custom_terms=["Bluebird"])
    assert any(m.category is Category.CUSTOM and m.text == "Bluebird" for m in hits)


def test_overlaps_resolved_by_priority():
    """A labelled card number yields one CREDIT_CARD, not an overlapping pair."""
    hits = detect("Account No: 4111 1111 1111 1111")
    spans = [(m.start, m.end) for m in hits]
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            assert not (a[0] < b[1] and b[0] < a[1]), "overlapping matches survived"


def test_cjk():
    text = "姓名: 张伟\n身份证号: 11010519491231002X\n地址: 上海市浦东新区世纪大道100号\n"
    cats = {m.category for m in detect(text)}
    assert Category.NAME in cats
    assert Category.NATIONAL_ID in cats
    assert Category.ADDRESS in cats
    # "身份证号" is an ID label, not an address, despite ending in 号.
    assert not any(
        m.category is Category.ADDRESS and "身份证" in m.text for m in detect(text)
    )


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
def test_char_rects_align_with_text(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    pdf = make_sample(tmp / "s.pdf")
    doc = fitz.open(str(pdf))
    try:
        page_text = extract_page_text(doc[0])
        index = page_text.text.index("S1234567D")
        rects = page_text.rects_for(index, index + len("S1234567D"))
        assert len(rects) == 1
        x0, y0, x1, y1 = rects[0]
        assert x1 > x0 and y1 > y0
        # The box must actually contain the value on the page.
        recovered = doc[0].get_text("text", clip=fitz.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1))
        assert "S1234567D" in recovered
    finally:
        doc.close()


def test_merge_rects():
    merged = merge_rects([(0, 0, 10, 10), (9, 0, 20, 10), (50, 0, 60, 10)])
    assert len(merged) == 2
    assert merged[0] == (0, 0, 20, 10)


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------
def test_redaction_removes_content_not_just_covers_it(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = make_sample(tmp / "in.pdf")
    out = tmp / "out.pdf"

    assert deep_leaks(src), "test fixture should contain the secrets"

    with PdfSession(src) as session:
        result = session.scan()
        report = redact_pdf(src, out, result.detections)

    assert report.applied > 0
    assert report.verification_passed, report.verification_failures
    assert deep_leaks(out) == {}, f"content survived redaction: {deep_leaks(out)}"


def test_naive_black_box_would_fail_the_same_check(tmp_path=None):
    """Guards the test itself: drawing a box must NOT pass deep_leaks."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = make_sample(tmp / "in2.pdf")
    naive = tmp / "naive.pdf"
    doc = fitz.open(str(src))
    doc[0].draw_rect(fitz.Rect(50, 60, 400, 300), color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(str(naive))
    doc.close()
    assert deep_leaks(naive), "deep_leaks is not sensitive enough to be meaningful"


def test_metadata_is_scrubbed(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = make_sample(tmp / "in3.pdf")
    out = tmp / "out3.pdf"
    with PdfSession(src) as session:
        result = session.scan()
        redact_pdf(src, out, result.detections)
    doc = fitz.open(str(out))
    try:
        meta = doc.metadata or {}
        for key, value in meta.items():
            if key in ("format", "encryption"):
                continue
            assert not value, f"metadata field {key} survived: {value!r}"
    finally:
        doc.close()


def test_deselected_items_are_kept(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = make_sample(tmp / "in4.pdf")
    out = tmp / "out4.pdf"
    with PdfSession(src) as session:
        result = session.scan()
        for det in result.detections:
            det.selected = det.category is Category.EMAIL
        redact_pdf(src, out, result.detections)
    doc = fitz.open(str(out))
    try:
        text = doc[0].get_text("text")
    finally:
        doc.close()
    assert "john.anderson@example.com" not in text
    assert "S1234567D" in text, "an unticked item must survive"


def test_image_pixels_are_destroyed(tmp_path=None):
    """The scanned-page path: redaction must repaint raster pixels."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = make_sample(tmp / "in5.pdf")
    scanned = tmp / "scanned.pdf"
    out = tmp / "scanned_out.pdf"

    doc = fitz.open(str(src))
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    flat = fitz.open()
    page = flat.new_page(width=doc[0].rect.width, height=doc[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    flat.save(str(scanned))
    flat.close()
    doc.close()

    box = (95.0, 140.0, 260.0, 156.0)
    det = Detection(
        uid=1, page=0, category=Category.NATIONAL_ID, text="S1234567D",
        rects=[box], confidence=0.97, rule="ocr", source=Source.OCR,
        ocr_confidence=0.9, selected=True,
    )
    redact_pdf(scanned, out, [det])

    def colours(path):
        d = fitz.open(str(path))
        pm = d[0].get_pixmap(matrix=fitz.Matrix(3, 3), clip=fitz.Rect(*box), alpha=False)
        seen = {
            pm.pixel(x, y)
            for y in range(0, pm.height, max(1, pm.height // 30))
            for x in range(0, pm.width, max(1, pm.width // 30))
        }
        d.close()
        return seen

    assert len(colours(scanned)) > 2, "fixture should contain visible text"
    assert colours(out) == {(0, 0, 0)}, "image pixels were not repainted"


def test_refuses_to_overwrite_the_original(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = make_sample(tmp / "in6.pdf")
    try:
        redact_pdf(src, src, [])
    except RedactionError:
        return
    raise AssertionError("writing over the input should have been refused")


# --------------------------------------------------------------------------
# safety rails
# --------------------------------------------------------------------------
def test_network_is_blocked():
    from app.safety import enable_network_lockdown
    import socket

    enable_network_lockdown()
    try:
        socket.create_connection(("example.com", 80), timeout=1)
    except NetworkBlockedError:
        pass
    else:
        raise AssertionError("outbound connection was not blocked")

    try:
        socket.getaddrinfo("example.com", 80)
    except NetworkBlockedError:
        return
    raise AssertionError("DNS lookup was not blocked")


def test_temp_dir_is_shredded():
    with SecureTempDir() as scratch:
        root = scratch.path
        temp_file = scratch.new_file(".bin")
        temp_file.write_bytes(b"S1234567D" * 100)
        assert temp_file.exists()
        # Third-party libraries land inside our directory too.
        assert Path(tempfile.gettempdir()) == root
    assert not root.exists(), "scratch directory survived"
    assert not temp_file.exists()


def test_logging_never_carries_document_text(capsys=None):
    """A record marked sensitive must be dropped by the filter."""
    import io
    import logging
    from app.safety import get_logger

    logger = get_logger("localredact.test")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    from app.safety import _NoSensitiveDataFilter

    handler.addFilter(_NoSensitiveDataFilter())
    logger.addHandler(handler)
    try:
        logger.info("secret value S1234567D", extra={"sensitive": True})
        logger.info("found %d candidates", 3)
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert "S1234567D" not in output
    assert "3 candidates" in output


# --------------------------------------------------------------------------
# boundary and integration cases
# --------------------------------------------------------------------------
def test_empty_and_textless_documents(tmp_path=None):
    """A blank page must not crash, and must be reported as unscanned."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    blank = tmp / "blank.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(blank))
    doc.close()

    with PdfSession(blank) as session:
        result = session.scan()
    assert result.detections == []
    assert 0 in result.pages_without_text
    assert any("not scanned" in n or "manually" in n for n in result.notes), result.notes


def test_export_with_nothing_selected_is_a_no_op(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = make_sample(tmp / "in7.pdf")
    out = tmp / "out7.pdf"
    with PdfSession(src) as session:
        result = session.scan()
        for det in result.detections:
            det.selected = False
        report = redact_pdf(src, out, result.detections)
    assert report.applied == 0
    assert report.verification_passed
    # The document survives intact apart from the metadata scrub.
    doc = fitz.open(str(out))
    try:
        assert "S1234567D" in doc[0].get_text("text")
        assert not (doc.metadata or {}).get("author")
    finally:
        doc.close()


def test_partial_selection_does_not_fail_verification(tmp_path=None):
    """The same value twice, only one ticked: the survivor is intentional.

    Regression test - the lexical verifier used to report this as a failure.
    """
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = tmp / "dup.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 100), "Name: John Anderson", fontsize=11, fontname="helv")
    page.insert_text((60, 140), "Signed: John Anderson", fontsize=11, fontname="helv")
    doc.save(str(src))
    doc.close()

    with PdfSession(src) as session:
        result = session.scan()
        hits = [d for d in result.detections if d.text == "John Anderson"]
        assert len(hits) >= 2, [d.text for d in result.detections]
        hits[0].selected = True
        for det in hits[1:]:
            det.selected = False
        report = redact_pdf(src, tmp / "dup_out.pdf", result.detections)
    assert report.verification_passed, report.verification_failures
    assert report.applied == 1


def test_detections_only_on_a_later_page(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = tmp / "multi.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((60, 100), "Cover page. Nothing here.", fontsize=11, fontname="helv")
    doc.new_page().insert_text((60, 100), "Public notice.", fontsize=11, fontname="helv")
    doc.new_page().insert_text((60, 100), "Email: a.person@example.com", fontsize=11, fontname="helv")
    doc.save(str(src))
    doc.close()

    with PdfSession(src) as session:
        result = session.scan()
        assert [d.page for d in result.detections] == [2]
        report = redact_pdf(src, tmp / "multi_out.pdf", result.detections)
    assert report.pages_touched == 1
    assert report.verification_passed
    assert deep_leaks(tmp / "multi_out.pdf", ["a.person@example.com"]) == {}


def test_rotated_page_coordinates(tmp_path=None):
    """A /Rotate 90 page must still redact the right area.

    Text geometry and redaction annotations have to agree about which coordinate
    space they are in; if they did not, the box would land on the wrong part of
    the page and the value would survive.
    """
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = tmp / "rot.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 100), "NRIC No: S1234567D", fontsize=11, fontname="helv")
    page.insert_text((60, 140), "Keep this line intact.", fontsize=11, fontname="helv")
    page.set_rotation(90)
    doc.save(str(src))
    doc.close()

    with PdfSession(src) as session:
        result = session.scan()
        assert any(d.text == "S1234567D" for d in result.detections)
        report = redact_pdf(src, tmp / "rot_out.pdf", result.detections)
    assert report.verification_passed, report.verification_failures
    out_text = fitz.open(str(tmp / "rot_out.pdf"))[0].get_text("text")
    assert "S1234567D" not in out_text
    assert "Keep this line intact." in out_text, "redaction landed on the wrong area"
    assert deep_leaks(tmp / "rot_out.pdf", ["S1234567D"]) == {}


def test_value_split_across_two_lines(tmp_path=None):
    """A labelled value wrapped onto the next line yields one box per line."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    src = tmp / "wrap.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 100), "Address:", fontsize=11, fontname="helv")
    page.insert_text((60, 118), "21 Orchard Road", fontsize=11, fontname="helv")
    doc.save(str(src))
    doc.close()
    with PdfSession(src) as session:
        result = session.scan()
        addresses = [d for d in result.detections if d.category is Category.ADDRESS]
        assert addresses, [d.text for d in result.detections]
        assert all(len(d.rects) >= 1 for d in addresses)
        report = redact_pdf(src, tmp / "wrap_out.pdf", result.detections)
    assert report.verification_passed
    assert "Orchard" not in fitz.open(str(tmp / "wrap_out.pdf"))[0].get_text("text")


# --------------------------------------------------------------------------
# packaging
# --------------------------------------------------------------------------
def test_pyinstaller_spec_paths_resolve():
    """The .spec must reference real files, from any working directory.

    Regression test: PyInstaller resolves relative paths in a spec against the
    SPEC FILE's directory, not the CWD, so a bare "run_app.py" in
    packaging/localredact.spec was looked up as packaging/run_app.py and the
    build failed immediately. Executing the spec with stub builder classes
    catches that here instead of five minutes into a Windows build.
    """
    import os

    root = Path(__file__).resolve().parent.parent
    spec = root / "packaging" / "localredact.spec"
    assert spec.is_file()

    captured = {}

    class _Rec:
        def __init__(self, *args, **kwargs):
            captured.setdefault(type(self).__name__, []).append((args, kwargs))

        def __getattr__(self, name):
            return f"<{name}>"   # a.pure, a.binaries, a.datas ...

    namespace = {
        "SPECPATH": str(spec.parent),
        "Analysis": type("Analysis", (_Rec,), {}),
        "PYZ": type("PYZ", (_Rec,), {}),
        "EXE": type("EXE", (_Rec,), {}),
    }
    previous = os.getcwd()
    os.chdir(tempfile.gettempdir())   # a directory that is NOT the project root
    try:
        exec(compile(spec.read_text(encoding="utf-8"), str(spec), "exec"), namespace)
    finally:
        os.chdir(previous)

    args, kwargs = captured["Analysis"][0]
    entry = args[0][0]
    assert Path(entry).is_file(), f"spec entry script does not exist: {entry}"
    assert Path(entry).name == "run_app.py"

    for source, _dest in kwargs["datas"]:
        assert Path(source).is_file(), f"spec data file missing: {source}"

    exe_kwargs = captured["EXE"][0][1]
    assert exe_kwargs["name"] == "LocalRedact"
    assert exe_kwargs["console"] is False
    assert exe_kwargs["upx"] is False, "UPX raises antivirus false positives"
    assert Path(exe_kwargs["icon"]).is_file(), "exe icon missing"
    assert Path(exe_kwargs["version"]).is_file(), "version resource missing"
    for blocked in ("requests", "urllib3", "smtplib"):
        assert blocked in kwargs["excludes"], f"{blocked} should be excluded"


def test_icon_is_a_valid_multi_size_ico():
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    _sys.path.insert(0, str(root / "packaging"))
    from make_icon import ICO_SIZES, verify_ico

    verify_ico(root / "packaging" / "localredact.ico", ICO_SIZES)
    assert (root / "packaging" / "localredact_256.png").is_file()


# --------------------------------------------------------------------------
# standalone runner
# --------------------------------------------------------------------------
def _run_all() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = []
    for name, func in tests:
        scratch = Path(tempfile.mkdtemp(prefix="lrtest-"))
        try:
            import inspect

            if "tmp_path" in inspect.signature(func).parameters:
                func(scratch)
            else:
                func()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc.__class__.__name__}: {exc}")
            failures.append(name)
        finally:
            import shutil

            shutil.rmtree(scratch, ignore_errors=True)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        print("failed:", ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
