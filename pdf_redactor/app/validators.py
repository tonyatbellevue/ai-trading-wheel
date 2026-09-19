"""Checksum and plausibility validators.

Regexes alone produce far too many false positives on financial documents
(invoice numbers look like account numbers, order ids look like NRICs). Every
validator here turns a shape match into a confidence signal, which is what keeps
the review list short enough for a human to actually read.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

_DIGITS = re.compile(r"\d")


def digits_only(value: str) -> str:
    return "".join(_DIGITS.findall(value))


def luhn_valid(value: str) -> bool:
    """Standard Luhn mod-10 check used by payment cards."""
    digits = digits_only(value)
    if len(digits) < 12 or len(digits) > 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def card_brand(value: str) -> Optional[str]:
    """Best-effort issuer identification, used only to raise confidence."""
    d = digits_only(value)
    if not d:
        return None
    if d.startswith("4") and len(d) in (13, 16, 19):
        return "Visa"
    if len(d) == 16 and 51 <= int(d[:2]) <= 55:
        return "Mastercard"
    if len(d) == 16 and 2221 <= int(d[:4]) <= 2720:
        return "Mastercard"
    if len(d) == 15 and d[:2] in ("34", "37"):
        return "Amex"
    if len(d) == 16 and (d.startswith("6011") or d.startswith("65")):
        return "Discover"
    if len(d) in (16, 19) and d.startswith("62"):
        return "UnionPay"
    if len(d) in (16, 19) and (
        d[:4] in ("3528", "3529")
        or d[:3] in ("353", "354", "355", "356", "357", "358")
    ):
        return "JCB"
    return None


# --- Singapore NRIC / FIN ---------------------------------------------------
# Weights 2,7,6,5,4,3,2 over the 7 digits; the offset depends on the prefix
# letter (T/G/M are the "born or issued from 2000" series).
_NRIC_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)
_NRIC_ST_CHECK = "JZIHGFEDCBA"
_NRIC_FG_CHECK = "XWUTRQPNMLK"
_NRIC_M_CHECK = "XWUTRQPNJLK"


def nric_valid(value: str) -> bool:
    """Validate a Singapore NRIC/FIN checksum letter."""
    v = value.strip().upper().replace(" ", "").replace("-", "")
    if len(v) != 9 or not v[1:8].isdigit():
        return False
    prefix, body, check = v[0], v[1:8], v[8]
    if prefix not in "STFGM":
        return False
    total = sum(int(d) * w for d, w in zip(body, _NRIC_WEIGHTS))
    if prefix in "TG":
        total += 4
    elif prefix == "M":
        total += 3
    remainder = total % 11
    if prefix in "ST":
        expected = _NRIC_ST_CHECK[remainder]
    elif prefix in "FG":
        expected = _NRIC_FG_CHECK[remainder]
    else:  # M series
        expected = _NRIC_M_CHECK[10 - remainder]
    return check == expected


# --- China resident identity card (18 digit, GB 11643-1999) -----------------
_CN_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CN_CHECK = "10X98765432"


def china_id_valid(value: str) -> bool:
    v = value.strip().upper().replace(" ", "")
    if len(v) != 18 or not v[:17].isdigit():
        return False
    # Embedded birth date must be real.
    try:
        _dt.date(int(v[6:10]), int(v[10:12]), int(v[12:14]))
    except ValueError:
        return False
    total = sum(int(d) * w for d, w in zip(v[:17], _CN_WEIGHTS))
    return v[17] == _CN_CHECK[total % 11]


# --- US Social Security Number ---------------------------------------------
def ssn_plausible(value: str) -> bool:
    """SSNs have no checksum; these are the SSA's never-issued ranges."""
    d = digits_only(value)
    if len(d) != 9:
        return False
    area, group, serial = d[:3], d[3:5], d[5:]
    if area in ("000", "666") or area.startswith("9"):
        return False
    if group == "00" or serial == "0000":
        return False
    if d == d[0] * 9:
        return False
    return True


# --- IBAN (ISO 13616) -------------------------------------------------------
def iban_valid(value: str) -> bool:
    v = re.sub(r"[\s-]", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", v):
        return False
    rearranged = v[4:] + v[:4]
    numeric = "".join(
        str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged
    )
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


# --- Malaysia MyKad ---------------------------------------------------------
def mykad_plausible(value: str) -> bool:
    d = digits_only(value)
    if len(d) != 12:
        return False
    yy, mm, dd = int(d[0:2]), int(d[2:4]), int(d[4:6])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return False
    # Place-of-birth code 00 and 17-and-up unassigned ranges are not issued.
    pb = int(d[6:8])
    if pb == 0 or pb in (17, 18, 19, 20, 69, 70, 73, 80, 81, 94, 95, 96, 97):
        return False
    del yy
    return True


# --- Hong Kong ID -----------------------------------------------------------
def hkid_valid(value: str) -> bool:
    v = re.sub(r"[\s()]", "", value.strip().upper())
    m = re.fullmatch(r"([A-Z]{1,2})(\d{6})([0-9A])", v)
    if not m:
        return False
    prefix, body, check = m.groups()
    padded = prefix.rjust(2, " ")
    total = 0
    weight = 9
    for ch in padded:
        total += (36 if ch == " " else ord(ch) - 55) * weight
        weight -= 1
    for ch in body:
        total += int(ch) * weight
        weight -= 1
    remainder = (11 - total % 11) % 11
    expected = "A" if remainder == 10 else str(remainder)
    return check == expected


# --- Dates ------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_date(value: str) -> Optional[_dt.date]:
    """Parse the date shapes the detectors emit. Returns None if implausible.

    Ambiguous numeric dates (03/04/1985) are resolved day-first when the first
    field is > 12, otherwise the year is what matters for a DOB check so the
    ambiguity is harmless.
    """
    v = value.strip()

    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", v)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", v)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = _expand_year(y)
        if a > 12:
            return _safe_date(y, b, a)
        return _safe_date(y, a, b)

    m = re.fullmatch(
        r"(\d{1,2})\s*[-\s.]?\s*([A-Za-z]{3,9})\.?\s*[-,\s]\s*(\d{2,4})", v
    )
    if m:
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            return _safe_date(_expand_year(int(m.group(3))), mon, int(m.group(1)))

    m = re.fullmatch(
        r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{2,4})", v
    )
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            return _safe_date(_expand_year(int(m.group(3))), mon, int(m.group(2)))

    m = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", v)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    return None


def _expand_year(year: int) -> int:
    if year >= 100:
        return year
    # Two-digit years on identity documents are overwhelmingly 19xx/20xx.
    current = _dt.date.today().year % 100
    return 2000 + year if year <= current else 1900 + year


def _safe_date(year: int, month: int, day: int) -> Optional[_dt.date]:
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None


def plausible_birth_date(value: str) -> bool:
    """A birth date must be in the past and imply an age under 120."""
    parsed = parse_date(value)
    if parsed is None:
        return False
    today = _dt.date.today()
    if parsed >= today:
        return False
    return (today.year - parsed.year) <= 120
