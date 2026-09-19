"""Offline PII detection over a page's plain text.

Everything here is pure Python over a string: no model download, no API call.
Each rule yields a :class:`Match` carrying a confidence in [0, 1]. Confidence is
driven by three things, in decreasing order of trustworthiness:

* a checksum that passed (NRIC, Luhn, IBAN, China ID)  -> 0.95+
* an explicit label next to the value ("Passport No: ...") -> 0.85-0.95
* shape alone ("two capitalised words")                 -> 0.45-0.65

Anything under ``REVIEW_CONFIDENCE_THRESHOLD`` is surfaced to the user as
"needs review" rather than silently redacted or silently dropped.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, List, Optional, Sequence, Set

from . import lexicon as lex
from . import validators as val
from .models import CATEGORY_PRIORITY, Category, Match

# A label, its optional colon, and any whitespace/newline before the value.
_SEP = r"[ \t]*[:：#=]?[ \t]*\n?[ \t]*"
# Strict variant: the delimiter is mandatory. Used where the label words are
# common enough that a bare space would match ordinary prose.
_SEP_STRICT = r"[ \t]*(?:[:：#=][ \t]*\n?[ \t]*|\n[ \t]*)"


def _labels(words: Sequence[str]) -> str:
    """Build an alternation that matches any of ``words`` as a whole label.

    The letter-boundary assertions are essential, not cosmetic. Several labels
    are very short ("ic", "hp", "tel", "a/c"), and without them "ic" matches
    inside "Publ*ic* notice", which turned an ordinary sentence into a
    high-confidence national-id hit. ``\b`` is not usable here because many
    labels end in punctuation ("a/c", "d.o.b", "passport #"), where ``\b``
    asserts the wrong thing; an explicit "not a letter" lookaround is correct
    for every label in the lists.

    Longest-first ordering makes the alternation prefer "account number" over
    "account".
    """
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return r"(?<![A-Za-z])(?:%s)(?![A-Za-z])" % "|".join(escaped)


NAME_LABEL_RE = _labels(lex.NAME_LABELS)
ADDRESS_LABEL_RE = _labels(lex.ADDRESS_LABELS)
PHONE_LABEL_RE = _labels(lex.PHONE_LABELS)
DOB_LABEL_RE = _labels(lex.DOB_LABELS)
ACCOUNT_LABEL_RE = _labels(lex.ACCOUNT_LABELS)
ID_LABEL_RE = _labels(lex.ID_LABELS)
PASSPORT_LABEL_RE = _labels(lex.PASSPORT_LABELS)
TAX_LABEL_RE = _labels(lex.TAX_LABELS)
HONORIFIC_RE = r"(?<![A-Za-z])(?:%s)(?![A-Za-z])" % "|".join(
    sorted(lex.HONORIFICS, key=len, reverse=True)
)
STREET_TYPE_RE = r"(?:%s)" % "|".join(
    sorted(lex.STREET_TYPES, key=len, reverse=True)
)
UNIT_WORD_RE = r"(?:%s)" % "|".join(sorted(lex.UNIT_WORDS, key=len, reverse=True))

# A single name token: capitalised latin, an all-caps token, or CJK characters.
_NAME_TOKEN = r"(?:[A-Z][a-zA-Z'’\-]{1,20}|[A-Z]{2,20}|[一-鿿]{1,4})"

DATE_PATTERN = (
    r"(?:"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}[\s-]*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?[\s,-]*\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{2,4}"
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?"
    r")"
)


class Rule:
    """One detection rule.

    ``group`` selects which regex group becomes the redacted span, so a rule can
    match ``"Passport No: E1234567"`` for context while redacting only the
    number. ``refine`` may lower or raise the confidence, or veto the match
    entirely by returning ``None``.
    """

    __slots__ = ("name", "category", "pattern", "confidence", "group", "refine")

    def __init__(
        self,
        name: str,
        category: Category,
        pattern: str,
        confidence: float,
        group: int = 0,
        refine: Optional[Callable[[str, re.Match], Optional[float]]] = None,
        flags: int = re.IGNORECASE,
    ) -> None:
        self.name = name
        self.category = category
        self.pattern = re.compile(pattern, flags)
        self.confidence = confidence
        self.group = group
        self.refine = refine


# --------------------------------------------------------------------------
# refine callbacks
# --------------------------------------------------------------------------
def _refine_card(text: str, _m: re.Match) -> Optional[float]:
    if not val.luhn_valid(text):
        return None  # a 16-digit invoice number is not a card
    return 0.97 if val.card_brand(text) else 0.90


def _refine_nric(text: str, _m: re.Match) -> Optional[float]:
    return 0.97 if val.nric_valid(text) else 0.55


def _refine_labelled_nric(text: str, _m: re.Match) -> Optional[float]:
    return 0.98 if val.nric_valid(text) else 0.85


def _refine_china_id(text: str, _m: re.Match) -> Optional[float]:
    return 0.97 if val.china_id_valid(text) else None


def _refine_ssn(text: str, _m: re.Match) -> Optional[float]:
    return 0.92 if val.ssn_plausible(text) else None


def _refine_iban(text: str, _m: re.Match) -> Optional[float]:
    return 0.97 if val.iban_valid(text) else None


def _refine_hkid(text: str, _m: re.Match) -> Optional[float]:
    return 0.95 if val.hkid_valid(text) else 0.60


def _refine_mykad(text: str, _m: re.Match) -> Optional[float]:
    return 0.90 if val.mykad_plausible(text) else None


def _refine_dob(text: str, _m: re.Match) -> Optional[float]:
    return 0.95 if val.plausible_birth_date(text) else 0.60


def _refine_ip(text: str, _m: re.Match) -> Optional[float]:
    parts = text.split(".")
    if len(parts) != 4:
        return None
    try:
        if not all(0 <= int(p) <= 255 for p in parts):
            return None
    except ValueError:
        return None
    # Version strings like 1.2.3.4 are common in footers; require a non-trivial
    # octet somewhere to avoid flagging them.
    if all(int(p) < 10 for p in parts):
        return 0.40
    return 0.80


def _refine_generic_account(text: str, _m: re.Match) -> Optional[float]:
    digits = val.digits_only(text)
    if len(digits) < 6:
        return None
    if len(set(digits)) == 1:
        return None  # 000000 style placeholders
    return 0.85


def _looks_like_name(candidate: str) -> bool:
    tokens = [t for t in re.split(r"[\s]+", candidate.strip()) if t]
    if not tokens or len(tokens) > 5:
        return False
    real = [t for t in tokens if t.lower().strip(".") not in lex.NAME_PARTICLES]
    if not real:
        return False
    for tok in real:
        bare = tok.strip(".,'’-")
        if not bare:
            return False
        if bare in lex.STOP_TITLECASE:
            return False
        if bare.title() in lex.STOP_TITLECASE:
            return False
    return True


def _refine_labelled_name(text: str, _m: re.Match) -> Optional[float]:
    if not _looks_like_name(text):
        return None
    return 0.93


def _refine_honorific_name(text: str, _m: re.Match) -> Optional[float]:
    if not _looks_like_name(text):
        return None
    return 0.90


def _refine_bare_name(text: str, _m: re.Match) -> Optional[float]:
    """Capitalised word sequence with no label and no honorific.

    This is the noisiest rule in the set, so it only ever produces
    low-confidence hits that land in the "needs review" bucket.
    """
    if not _looks_like_name(text):
        return None
    tokens = [t for t in text.split() if t]
    if len(tokens) < 2:
        return None
    if any(t.lower().strip(".,") in lex.COMMON_FIRST_NAMES for t in tokens):
        return 0.70
    if all(t.isupper() for t in tokens):
        return 0.45  # ALL CAPS headings are usually not names
    return 0.52


# "号" is both an address suffix (…大道100号) and an id-label suffix (身份证号),
# so the CJK address rule needs an explicit veto list.
_CJK_NOT_ADDRESS = (
    "身份证", "护照", "账号", "卡号", "学号", "编号", "型号", "序号", "单号",
    "工号", "电话", "手机", "税号", "保单", "订单",
)


def _refine_cjk_address(text: str, _m: re.Match) -> Optional[float]:
    stripped = text.strip()
    if len(stripped) < 6:
        return None
    if any(tok in stripped for tok in _CJK_NOT_ADDRESS):
        return None
    return 0.78


def _refine_cjk_name(text: str, _m: re.Match) -> Optional[float]:
    stripped = text.strip()
    if not 2 <= len(stripped) <= 4:
        return None
    return 0.60


# --------------------------------------------------------------------------
# the rule set
# --------------------------------------------------------------------------
RULES: List[Rule] = [
    # ---- email -----------------------------------------------------------
    Rule(
        "email",
        Category.EMAIL,
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b",
        0.97,
    ),
    # ---- credit card -----------------------------------------------------
    Rule(
        "credit_card",
        Category.CREDIT_CARD,
        r"\b(?:\d[ -]?){12,18}\d\b",
        0.90,
        refine=_refine_card,
    ),
    # ---- national id -----------------------------------------------------
    Rule(
        "nric_labelled",
        Category.NATIONAL_ID,
        rf"{ID_LABEL_RE}{_SEP}([STFGM]\d{{7}}[A-Z])\b",
        0.85,
        group=1,
        refine=_refine_labelled_nric,
    ),
    Rule(
        "nric_bare",
        Category.NATIONAL_ID,
        r"\b[STFGM]\d{7}[A-Z]\b",
        0.55,
        refine=_refine_nric,
    ),
    Rule(
        "china_id",
        Category.NATIONAL_ID,
        r"\b\d{17}[\dXx]\b",
        0.97,
        refine=_refine_china_id,
    ),
    Rule(
        "ssn",
        Category.NATIONAL_ID,
        r"\b\d{3}-\d{2}-\d{4}\b",
        0.92,
        refine=_refine_ssn,
    ),
    Rule(
        "ssn_labelled",
        Category.NATIONAL_ID,
        rf"(?:ssn|social security(?:\s+number)?){_SEP}(\d{{3}}[- ]?\d{{2}}[- ]?\d{{4}})\b",
        0.95,
        group=1,
        refine=_refine_ssn,
    ),
    Rule(
        "hkid",
        Category.NATIONAL_ID,
        r"\b[A-Z]{1,2}\d{6}\(\s*[0-9A]\s*\)",
        0.90,
        refine=_refine_hkid,
    ),
    Rule(
        "mykad",
        Category.NATIONAL_ID,
        r"\b\d{6}-\d{2}-\d{4}\b",
        0.90,
        refine=_refine_mykad,
    ),
    Rule(
        "aadhaar",
        Category.NATIONAL_ID,
        rf"(?:aadhaar|aadhar|uid){_SEP}(\d{{4}}\s?\d{{4}}\s?\d{{4}})\b",
        0.92,
        group=1,
    ),
    Rule(
        "id_labelled_generic",
        Category.NATIONAL_ID,
        rf"{ID_LABEL_RE}{_SEP_STRICT}([A-Z0-9][A-Z0-9\-/]{{5,19}})\b",
        0.82,
        group=1,
    ),
    # ---- passport --------------------------------------------------------
    Rule(
        "passport_labelled",
        Category.PASSPORT,
        rf"{PASSPORT_LABEL_RE}{_SEP}([A-Z]{{0,3}}\d{{5,9}}|[A-Z]\d{{7}}|\d{{9}})\b",
        0.93,
        group=1,
    ),
    Rule(
        "passport_mrz",
        Category.PASSPORT,
        r"\bP[<A-Z0-9]{5,}<{2,}[A-Z0-9<]{10,}",
        0.95,
    ),
    # ---- tax id ----------------------------------------------------------
    Rule(
        "tax_labelled",
        Category.TAX_ID,
        rf"{TAX_LABEL_RE}{_SEP_STRICT}([A-Z0-9][A-Z0-9\-]{{5,17}})\b",
        0.88,
        group=1,
    ),
    # ---- bank / account --------------------------------------------------
    Rule(
        "iban",
        Category.BANK_ACCOUNT,
        r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b",
        0.97,
        refine=_refine_iban,
        flags=0,
    ),
    Rule(
        "swift_bic",
        Category.BANK_ACCOUNT,
        rf"(?:swift(?:\s*code)?|bic){_SEP}([A-Z]{{4}}[A-Z]{{2}}[A-Z0-9]{{2}}(?:[A-Z0-9]{{3}})?)\b",
        0.90,
        group=1,
    ),
    Rule(
        "account_labelled",
        Category.BANK_ACCOUNT,
        rf"{ACCOUNT_LABEL_RE}{_SEP}([0-9][0-9\- ]{{5,24}}[0-9])",
        0.88,
        group=1,
        refine=_refine_generic_account,
    ),
    Rule(
        "routing_number",
        Category.BANK_ACCOUNT,
        rf"(?:routing(?:\s*(?:number|no))?|aba|sort\s*code){_SEP}(\d{{6}}|\d{{9}}|\d{{2}}-\d{{2}}-\d{{2}})\b",
        0.90,
        group=1,
    ),
    # ---- phone -----------------------------------------------------------
    Rule(
        "phone_labelled",
        Category.PHONE,
        rf"{PHONE_LABEL_RE}{_SEP}(\+?\d[\d\s().\-]{{6,20}}\d)",
        0.93,
        group=1,
    ),
    Rule(
        "phone_e164",
        Category.PHONE,
        r"\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?[\s.\-]?\d{3,4}[\s.\-]?\d{3,5}",
        0.88,
    ),
    Rule(
        "phone_sg",
        Category.PHONE,
        r"(?<![\d.\-])(?:\+65[\s-]?)?[689]\d{3}[\s-]?\d{4}(?![\d.\-])",
        0.72,
    ),
    Rule(
        "phone_us",
        Category.PHONE,
        r"(?<![\d.\-])\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d.\-])",
        0.78,
    ),
    # ---- date of birth ---------------------------------------------------
    Rule(
        "dob_labelled",
        Category.DATE_OF_BIRTH,
        rf"{DOB_LABEL_RE}{_SEP}({DATE_PATTERN})",
        0.93,
        group=1,
        refine=_refine_dob,
    ),
    # ---- address ---------------------------------------------------------
    Rule(
        "address_labelled",
        Category.ADDRESS,
        rf"{ADDRESS_LABEL_RE}[ \t]*[:：][ \t]*\n?[ \t]*([^\n]{{6,120}})",
        0.88,
        group=1,
    ),
    Rule(
        "address_street",
        Category.ADDRESS,
        rf"\b\d{{1,6}}[A-Za-z]?\s+(?:[A-Za-z][\w'\-]*\s+){{0,4}}{STREET_TYPE_RE}\b\.?"
        rf"(?:\s*,?\s*(?:{UNIT_WORD_RE})\.?\s*[#\w\-]+)?",
        0.80,
    ),
    Rule(
        "address_sg_block_unit",
        Category.ADDRESS,
        r"\b(?:blk|block)\.?\s*\d{1,4}[A-Z]?\b[^\n]{0,60}?#\d{2}\s*-\s*\d{2,4}",
        0.90,
    ),
    Rule(
        "address_unit_only",
        Category.ADDRESS,
        r"#\d{2}\s*-\s*\d{2,4}\b",
        0.72,
    ),
    Rule(
        "postcode_sg",
        Category.ADDRESS,
        r"\bSingapore\s+\d{6}\b",
        0.88,
    ),
    Rule(
        "postcode_uk",
        Category.ADDRESS,
        r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b",
        0.70,
        flags=0,
    ),
    Rule(
        "postcode_us_zip",
        Category.ADDRESS,
        r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
        0.75,
        flags=0,
    ),
    Rule(
        "address_cjk",
        Category.ADDRESS,
        r"[一-鿿]{2,10}(?:省|市|区|县|镇|街道|路|街|巷|号|栋|楼|室|单元)"
        r"(?:[一-鿿\d]{1,12}(?:路|街|巷|号|栋|楼|室|单元|区|镇))*",
        0.78,
        refine=_refine_cjk_address,
        flags=0,
    ),
    # ---- names -----------------------------------------------------------
    Rule(
        "name_labelled",
        Category.NAME,
        rf"\b{NAME_LABEL_RE}{_SEP_STRICT}({_NAME_TOKEN}(?:[ \t]+{_NAME_TOKEN}){{0,4}})",
        0.93,
        group=1,
        refine=_refine_labelled_name,
    ),
    Rule(
        "name_honorific",
        Category.NAME,
        rf"\b{HONORIFIC_RE}\.?[ \t]+({_NAME_TOKEN}(?:[ \t]+{_NAME_TOKEN}){{0,3}})",
        0.90,
        group=1,
        refine=_refine_honorific_name,
        flags=0,
    ),
    Rule(
        "name_cjk_labelled",
        Category.NAME,
        rf"{NAME_LABEL_RE}{_SEP}([一-鿿]{{2,4}})",
        0.90,
        group=1,
        refine=_refine_cjk_name,
    ),
    Rule(
        "name_bare",
        Category.NAME,
        r"\b[A-Z][a-z]{1,15}(?:[ \t]+(?:[a-z]{2,4}[ \t]+)?[A-Z][a-z]{1,15}){1,3}\b",
        0.52,
        refine=_refine_bare_name,
        flags=0,
    ),
    Rule(
        "name_allcaps",
        Category.NAME,
        r"\b[A-Z]{2,15}(?:[ \t]+[A-Z]{2,15}){1,3}\b",
        0.45,
        refine=_refine_bare_name,
        flags=0,
    ),
    # ---- misc ------------------------------------------------------------
    Rule(
        "ip_address",
        Category.IP_ADDRESS,
        r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        0.80,
        refine=_refine_ip,
    ),
    Rule(
        "vehicle_sg",
        Category.VEHICLE,
        r"\b[A-Z]{2,3}\s?\d{1,4}\s?[A-Z]\b",
        0.45,
        flags=0,
    ),
]

RULES_BY_NAME = {r.name: r for r in RULES}


def _iter_rule_matches(
    text: str, rules: Iterable[Rule]
) -> Iterable[Match]:
    for rule in rules:
        for m in rule.pattern.finditer(text):
            try:
                start, end = m.span(rule.group)
            except IndexError:  # pragma: no cover - guards a malformed rule
                continue
            if start < 0 or end <= start:
                continue
            fragment = text[start:end]
            confidence = rule.confidence
            if rule.refine is not None:
                refined = rule.refine(fragment, m)
                if refined is None:
                    continue
                confidence = refined
            # Trailing punctuation should not be part of the redacted span.
            trimmed = fragment.rstrip(" \t.,;:)")
            if trimmed != fragment:
                end -= len(fragment) - len(trimmed)
                fragment = trimmed
            if not fragment.strip():
                continue
            yield Match(
                start=start,
                end=end,
                category=rule.category,
                confidence=round(confidence, 3),
                rule=rule.name,
                text=fragment,
            )


def _custom_matches(text: str, terms: Sequence[str]) -> Iterable[Match]:
    for term in terms:
        term = term.strip()
        if len(term) < 2:
            continue
        for m in re.finditer(re.escape(term), text, re.IGNORECASE):
            yield Match(
                start=m.start(),
                end=m.end(),
                category=Category.CUSTOM,
                confidence=1.0,
                rule="custom_term",
                text=m.group(0),
            )


def _resolve_overlaps(matches: List[Match]) -> List[Match]:
    """Keep the strongest match when spans overlap.

    Ranking is priority first, then confidence, then length. This is what stops
    the generic ``name_bare`` rule from shadowing a labelled passport number and
    stops ``credit_card`` and ``account_labelled`` from double-covering the same
    digits.
    """
    ordered = sorted(
        matches,
        key=lambda m: (
            -CATEGORY_PRIORITY.get(m.category, 0),
            -m.confidence,
            -(m.end - m.start),
            m.start,
        ),
    )
    kept: List[Match] = []
    claimed: List[range] = []
    for m in ordered:
        if any(m.start < c.stop and c.start < m.end for c in claimed):
            continue
        kept.append(m)
        claimed.append(range(m.start, m.end))
    kept.sort(key=lambda m: (m.start, m.end))
    return kept


def detect(
    text: str,
    custom_terms: Sequence[str] = (),
    categories: Optional[Set[Category]] = None,
) -> List[Match]:
    """Run every enabled rule over ``text`` and return non-overlapping matches."""
    if not text:
        return []
    rules = RULES
    if categories is not None:
        rules = [r for r in RULES if r.category in categories]
    matches = list(_iter_rule_matches(text, rules))
    if custom_terms and (categories is None or Category.CUSTOM in categories):
        matches.extend(_custom_matches(text, custom_terms))
    return _resolve_overlaps(matches)
