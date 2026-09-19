"""Data model shared by the extractor, the detectors, the GUI and the redactor."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points


class Category(str, Enum):
    """PII classes the scanner knows about."""

    NAME = "NAME"
    NATIONAL_ID = "NATIONAL_ID"
    PASSPORT = "PASSPORT"
    ADDRESS = "ADDRESS"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    BANK_ACCOUNT = "BANK_ACCOUNT"
    CREDIT_CARD = "CREDIT_CARD"
    TAX_ID = "TAX_ID"
    VEHICLE = "VEHICLE"
    IP_ADDRESS = "IP_ADDRESS"
    CUSTOM = "CUSTOM"

    @property
    def label(self) -> str:
        return CATEGORY_LABELS[self]


CATEGORY_LABELS: Dict["Category", str] = {
    Category.NAME: "Name / 姓名",
    Category.NATIONAL_ID: "National ID / NRIC / SSN",
    Category.PASSPORT: "Passport number",
    Category.ADDRESS: "Address / 地址",
    Category.PHONE: "Phone number",
    Category.EMAIL: "Email address",
    Category.DATE_OF_BIRTH: "Date of birth",
    Category.BANK_ACCOUNT: "Bank / account number",
    Category.CREDIT_CARD: "Credit card number",
    Category.TAX_ID: "Tax ID",
    Category.VEHICLE: "Vehicle / licence plate",
    Category.IP_ADDRESS: "IP address",
    Category.CUSTOM: "Custom term",
}

# Higher wins when two detections overlap in the page text.
CATEGORY_PRIORITY: Dict["Category", int] = {
    Category.CUSTOM: 100,
    Category.CREDIT_CARD: 90,
    Category.NATIONAL_ID: 85,
    Category.PASSPORT: 80,
    Category.BANK_ACCOUNT: 75,
    Category.TAX_ID: 72,
    Category.EMAIL: 70,
    Category.PHONE: 65,
    Category.DATE_OF_BIRTH: 60,
    Category.ADDRESS: 50,
    Category.VEHICLE: 45,
    Category.IP_ADDRESS: 40,
    Category.NAME: 30,
}

# Categories switched on by default in the GUI. Everything is detected; these
# are the ones pre-ticked for redaction.
DEFAULT_ENABLED: Tuple["Category", ...] = (
    Category.NAME,
    Category.NATIONAL_ID,
    Category.PASSPORT,
    Category.ADDRESS,
    Category.PHONE,
    Category.EMAIL,
    Category.DATE_OF_BIRTH,
    Category.BANK_ACCOUNT,
    Category.CREDIT_CARD,
    Category.TAX_ID,
    Category.CUSTOM,
)

# Below this, a detection is flagged "needs manual review" in the UI.
REVIEW_CONFIDENCE_THRESHOLD = 0.75
# Below this, OCR output is considered unreliable.
OCR_REVIEW_THRESHOLD = 0.80


class Source(str, Enum):
    TEXT = "text"      # extracted from the PDF text layer
    OCR = "ocr"        # recovered from a rasterised page by the local OCR engine


@dataclass
class Match:
    """A raw regex/heuristic hit, expressed as offsets into the page text."""

    start: int
    end: int
    category: Category
    confidence: float
    rule: str
    text: str


@dataclass
class Detection:
    """A candidate redaction, ready for review and for the redaction engine."""

    uid: int
    page: int                     # 0-based
    category: Category
    text: str                     # in-memory only; never logged, never written
    rects: List[Rect]
    confidence: float
    rule: str
    source: Source = Source.TEXT
    ocr_confidence: Optional[float] = None
    selected: bool = True

    @property
    def needs_review(self) -> bool:
        if self.confidence < REVIEW_CONFIDENCE_THRESHOLD:
            return True
        if self.source is Source.OCR:
            if self.ocr_confidence is None:
                return True
            if self.ocr_confidence < OCR_REVIEW_THRESHOLD:
                return True
        return False

    @property
    def confidence_band(self) -> str:
        if self.confidence >= 0.90:
            return "high"
        if self.confidence >= REVIEW_CONFIDENCE_THRESHOLD:
            return "medium"
        return "low"

    def preview(self, reveal: bool) -> str:
        from .safety import mask

        flat = " ".join(self.text.split())
        if reveal:
            return flat
        return mask(flat)


_uid_counter = itertools.count(1)


def next_uid() -> int:
    return next(_uid_counter)


@dataclass
class PageText:
    """Page text plus a character-to-rectangle map.

    ``text[i]`` corresponds to ``char_rects[i]`` and ``line_ids[i]``. A character
    with no geometry (an injected newline) has ``None``. Keeping the two arrays
    aligned is what lets a regex match over the page string be turned back into
    exact page coordinates.
    """

    page: int
    text: str
    char_rects: List[Optional[Rect]]
    line_ids: List[int]
    source: Source = Source.TEXT
    char_conf: Optional[List[float]] = None
    width: float = 0.0
    height: float = 0.0

    def rects_for(self, start: int, end: int) -> List[Rect]:
        """Union the character boxes of ``text[start:end]``, one box per line."""
        by_line: Dict[int, List[Rect]] = {}
        for i in range(max(0, start), min(end, len(self.char_rects))):
            rect = self.char_rects[i]
            if rect is None:
                continue
            by_line.setdefault(self.line_ids[i], []).append(rect)
        out: List[Rect] = []
        for _line, boxes in sorted(by_line.items()):
            x0 = min(b[0] for b in boxes)
            y0 = min(b[1] for b in boxes)
            x1 = max(b[2] for b in boxes)
            y1 = max(b[3] for b in boxes)
            out.append((x0, y0, x1, y1))
        return out

    def confidence_for(self, start: int, end: int) -> Optional[float]:
        if self.char_conf is None:
            return None
        vals = [
            self.char_conf[i]
            for i in range(max(0, start), min(end, len(self.char_conf)))
            if self.char_rects[i] is not None
        ]
        if not vals:
            return None
        return min(vals)


@dataclass
class ScanResult:
    detections: List[Detection] = field(default_factory=list)
    page_count: int = 0
    ocr_pages: List[int] = field(default_factory=list)
    pages_without_text: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def by_page(self, page: int) -> List[Detection]:
        return [d for d in self.detections if d.page == page]

    def counts(self) -> Dict[Category, int]:
        out: Dict[Category, int] = {}
        for det in self.detections:
            out[det.category] = out.get(det.category, 0) + 1
        return out


@dataclass
class RedactionReport:
    """Outcome of an export, including the post-write verification pass."""

    output_path: str
    applied: int
    pages_touched: int
    metadata_cleared: bool
    xmp_removed: bool
    embedded_files_removed: int
    javascript_removed: int
    annotations_removed: int
    verified_absent: int
    verification_failures: List[str] = field(default_factory=list)
    residual_text_pages: List[int] = field(default_factory=list)

    @property
    def verification_passed(self) -> bool:
        return not self.verification_failures


def merge_rects(rects: Sequence[Rect], pad: float = 0.0) -> List[Rect]:
    """Merge rectangles that overlap or touch, optionally padding each first."""
    boxes = [
        (r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad) for r in rects if r is not None
    ]
    if not boxes:
        return []
    boxes.sort()
    merged: List[List[float]] = [list(boxes[0])]
    for box in boxes[1:]:
        last = merged[-1]
        overlap_x = box[0] <= last[2]
        overlap_y = box[1] <= last[3] and box[3] >= last[1]
        if overlap_x and overlap_y:
            last[0] = min(last[0], box[0])
            last[1] = min(last[1], box[1])
            last[2] = max(last[2], box[2])
            last[3] = max(last[3], box[3])
        else:
            merged.append(list(box))
    return [(m[0], m[1], m[2], m[3]) for m in merged]
