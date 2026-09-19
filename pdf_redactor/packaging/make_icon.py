"""Generate the Windows application icon.

Produces a multi-resolution ``localredact.ico`` plus PNG renders. Every size is
drawn from vector shapes at its own resolution rather than downscaled from one
bitmap, so the 16x16 used in the taskbar and title bar stays crisp instead of
turning to mush.

    python packaging/make_icon.py

Outputs into packaging/:
    localredact.ico        16,24,32,48,64,128,256 - used by PyInstaller
    localredact_256.png    window icon for Tkinter iconphoto
    icon_preview.png       side-by-side sheet of all sizes, for eyeballing

The artwork: a white document on a deep navy rounded square, with one line of
its text replaced by a solid black redaction bar and a second bar mid-strike.
That reads as "document with something removed" even at 16 pixels, which is the
only size guarantee that actually matters.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdfcompat import fitz

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
CANVAS = 256.0

NAVY = (0.086, 0.137, 0.227)      # #16233A background
PAGE = (1.0, 1.0, 1.0)
PAGE_EDGE = (0.804, 0.827, 0.867)
TEXT_LINE = (0.765, 0.792, 0.847)
REDACT = (0.043, 0.055, 0.075)    # near-black bar
ACCENT = (0.839, 0.196, 0.196)    # #D63232 second bar


def _draw(page: "fitz.Page") -> None:
    """Draw the icon at 256x256 user units."""
    shape = page.new_shape()

    # Rounded-square background.
    shape.draw_rect(fitz.Rect(0, 0, CANVAS, CANVAS), radius=0.22)
    shape.finish(color=None, fill=NAVY)

    # Document body, with a folded corner cut away at the top right.
    left, right, top, bottom = 56.0, 200.0, 40.0, 216.0
    fold = 40.0
    shape.draw_polyline(
        [
            fitz.Point(left, top),
            fitz.Point(right - fold, top),
            fitz.Point(right, top + fold),
            fitz.Point(right, bottom),
            fitz.Point(left, bottom),
            fitz.Point(left, top),
        ]
    )
    shape.finish(color=PAGE_EDGE, fill=PAGE, width=2.0)

    # The folded corner itself.
    shape.draw_polyline(
        [
            fitz.Point(right - fold, top),
            fitz.Point(right - fold, top + fold),
            fitz.Point(right, top + fold),
        ]
    )
    shape.finish(color=PAGE_EDGE, fill=PAGE_EDGE, width=2.0)

    # Text lines, then the two redaction bars replacing two of them.
    x0, x1 = left + 18, right - 18
    rows = [96.0, 120.0, 144.0, 168.0, 192.0]
    bar_height = 13.0
    for index, y in enumerate(rows):
        if index == 1:
            shape.draw_rect(fitz.Rect(x0, y - bar_height / 2, x1, y + bar_height / 2))
            shape.finish(color=None, fill=REDACT)
        elif index == 3:
            shape.draw_rect(
                fitz.Rect(x0, y - bar_height / 2, x0 + (x1 - x0) * 0.62,
                          y + bar_height / 2)
            )
            shape.finish(color=None, fill=ACCENT)
        else:
            width = (x1 - x0) if index != 4 else (x1 - x0) * 0.55
            shape.draw_rect(fitz.Rect(x0, y - 4.0, x0 + width, y + 4.0))
            shape.finish(color=None, fill=TEXT_LINE)

    shape.commit()


def render(size: int) -> bytes:
    """Render the icon at ``size`` pixels and return PNG bytes."""
    doc = fitz.open()
    page = doc.new_page(width=CANVAS, height=CANVAS)
    _draw(page)
    zoom = size / CANVAS
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
    try:
        return pix.tobytes("png")
    finally:
        del pix
        doc.close()


def write_ico(path: Path, sizes=ICO_SIZES) -> None:
    """Write a PNG-compressed .ico.

    PNG payloads inside ICO are supported from Windows Vista onward and keep the
    256x256 entry from bloating the file to a megabyte of raw BGRA.
    """
    images = [(size, render(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    entries = bytearray()
    payloads = bytearray()
    offset = len(header) + 16 * len(images)
    for size, png in images:
        # A dimension byte of 0 means 256 in the ICO format.
        dimension = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII",
            dimension, dimension,
            0,      # palette colours (0 = truecolour)
            0,      # reserved
            1,      # colour planes
            32,     # bits per pixel
            len(png),
            offset,
        )
        payloads += png
        offset += len(png)
    path.write_bytes(header + bytes(entries) + bytes(payloads))


def write_preview(path: Path) -> None:
    """A sheet showing every size next to each other, for a visual check."""
    gap, margin = 16, 20
    width = margin * 2 + sum(ICO_SIZES) + gap * (len(ICO_SIZES) - 1)
    height = margin * 2 + max(ICO_SIZES)
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.draw_rect(fitz.Rect(0, 0, width, height), color=None, fill=(0.94, 0.94, 0.95))
    x = margin
    for size in ICO_SIZES:
        y = margin + (max(ICO_SIZES) - size)
        page.insert_image(fitz.Rect(x, y, x + size, y + size), stream=render(size))
        x += size + gap
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pix.save(str(path))
    doc.close()


def verify_ico(path: Path, sizes=ICO_SIZES) -> None:
    """Assert the .ico on disk is a well-formed multi-resolution icon.

    Run in CI instead of a byte-for-byte reproducibility check: what matters is
    that Windows can read every size, not that two PNG encoders agree.
    """
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if (reserved, kind) != (0, 1):
        raise SystemExit(f"{path}: not an icon file (reserved={reserved} type={kind})")
    if count != len(sizes):
        raise SystemExit(f"{path}: expected {len(sizes)} sizes, found {count}")
    found = []
    for index in range(count):
        entry = data[6 + index * 16: 22 + index * 16]
        width, height, _c, _r, _p, bpp, length, offset = struct.unpack(
            "<BBBBHHII", entry
        )
        actual = width or 256
        if actual != (height or 256):
            raise SystemExit(f"{path}: entry {index} is not square")
        if bpp != 32:
            raise SystemExit(f"{path}: entry {actual}px is {bpp}bpp, expected 32")
        if data[offset:offset + 8] != b"\x89PNG\r\n\x1a\n":
            raise SystemExit(f"{path}: entry {actual}px is not a valid PNG")
        if offset + length > len(data):
            raise SystemExit(f"{path}: entry {actual}px is truncated")
        found.append(actual)
    if sorted(found) != sorted(sizes):
        raise SystemExit(f"{path}: sizes {sorted(found)} != {sorted(sizes)}")
    print(f"{path.name} OK: {count} sizes {sorted(found)}, {len(data):,} bytes")


def main() -> int:
    out = Path(__file__).resolve().parent
    if "--check" in sys.argv:
        verify_ico(out / "localredact.ico")
        return 0
    write_ico(out / "localredact.ico")
    (out / "localredact_256.png").write_bytes(render(256))
    (out / "localredact_64.png").write_bytes(render(64))
    write_preview(out / "icon_preview.png")
    ico = out / "localredact.ico"
    print(f"wrote {ico} ({ico.stat().st_size:,} bytes, {len(ICO_SIZES)} sizes)")
    print(f"wrote {out / 'localredact_256.png'}")
    print(f"wrote {out / 'icon_preview.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
