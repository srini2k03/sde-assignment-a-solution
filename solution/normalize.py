"""Pure value normalisers for lead fields and event timestamps.

Nothing in here touches the database or the filesystem, so every rule from the
spec can be unit-tested in isolation. Every parser returns the canonical value,
or ``None`` when the input is invalid; deciding what "invalid" means for a row
(reject vs. field warning) is the caller's job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

# I use explicit [0-9] instead of \d everywhere: in Python, \d also matches
# non-ASCII digits such as Devanagari or Arabic-Indic numerals.
_NON_DIGIT = re.compile(r"[^0-9]")


# --------------------------------------------------------------------- phone
def normalize_phone(raw: str) -> str | None:
    """Canonicalise an Indian mobile number to ``+91XXXXXXXXXX``.

    Strip every non-digit; drop a leading ``91`` from 12 digits or a leading
    ``0`` from 11 digits; the rest must be 10 digits starting with 6-9.
    """
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# ---------------------------------------------------------------------- date
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ISO_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
# DD/MM/YYYY or DD-MM-YYYY; the back-reference forces one consistent separator.
_DMY_DATE = re.compile(r"([0-9]{2})([/-])([0-9]{2})\2([0-9]{4})")
_DMON_DATE = re.compile(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{4})")


def parse_date(raw: str) -> str | None:
    """Parse one of the four accepted (day-first) formats into ISO ``YYYY-MM-DD``."""
    s = raw.strip()
    if m := _ISO_DATE.fullmatch(s):
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    elif m := _DMY_DATE.fullmatch(s):
        day, month, year = int(m[1]), int(m[3]), int(m[4])
    elif m := _DMON_DATE.fullmatch(s):
        month_no = _MONTHS.get(m[2].lower())
        if month_no is None:
            return None
        day, month, year = int(m[1]), month_no, int(m[3])
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:  # 31/02, month 13, year 0000 ...
        return None


# ------------------------------------------------------------------- decimal
_CURRENCY_PREFIX = re.compile(r"(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TWO_PLACES = Decimal("0.01")


def parse_decimal(raw: str) -> str | None:
    """Parse an amount like ``₹27,554`` / ``Rs. 24787`` / ``INR 18678.3``.

    Returns the value rounded half-up to 2 places as a string (``"27554.00"``).
    Negative numbers (any minus sign) and anything that is not a plain number
    after removing the prefix and commas are invalid.
    """
    s = raw.strip()
    if m := _CURRENCY_PREFIX.match(s):
        s = s[m.end():]
    s = s.replace(",", "")
    if not _PLAIN_NUMBER.fullmatch(s):
        return None
    try:
        return str(Decimal(s).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))
    except InvalidOperation:  # absurdly long numbers exceed Decimal precision
        return None


# ---------------------------------------------------------------------- enum
def parse_enum(raw: str, allowed: list[str]) -> str | None:
    value = raw.strip().lower()
    return value if value in {a.lower() for a in allowed} else None


# ----------------------------------------------------------------- timestamp
@dataclass(frozen=True)
class Timestamp:
    """An event time, kept in two sortable/readable text forms."""

    utc: str  # 2026-10-05T13:20:21.000000Z (fixed width, so text order == time order)
    ist: str  # 2026-10-05T18:50:21+05:30 (whole seconds, as the reports print it)

    @property
    def ist_date(self) -> str:
        return self.ist[:10]


def parse_timestamp(raw: object) -> Timestamp | None:
    """Parse ``...Z``, ``...+05:30`` or naive (= IST) ISO-8601 timestamps."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)  # naive means IST
    try:
        u = dt.astimezone(timezone.utc)
        i = dt.astimezone(IST)
    except OverflowError:  # e.g. 0001-01-01T00:00:00+05:30 has no UTC equivalent
        return None
    # Formatted by hand: strftime("%Y") does not zero-pad years < 1000 on Linux.
    return Timestamp(
        utc=f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:"
            f"{u.second:02d}.{u.microsecond:06d}Z",
        ist=f"{i.year:04d}-{i.month:02d}-{i.day:02d}T{i.hour:02d}:{i.minute:02d}:"
            f"{i.second:02d}+05:30",
    )
