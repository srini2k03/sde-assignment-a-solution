import pytest

from solution.normalize import (normalize_phone, parse_date, parse_decimal, parse_enum,
                                parse_timestamp)


# ------------------------------------------------------------------ phone
@pytest.mark.parametrize("raw", [
    "9876543210",
    "+91 98765 43210",
    "09876543210",
    "91-98765-43210",
    "(+91) 9876543210",
    "+919876543210",
    " 98765-43210 ",
    "+91-98765 43210",
])
def test_phone_valid_formats(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",          # 9 digits
    "5876543210",         # bad prefix
    "022-2345-6789",      # landline
    "N/A",
    "+1 415 555 0134",    # 11 digits not starting with 0
    "",
    "919876543210 1",     # 13 digits
    "0091 98765 43210",   # 13 digits
    "910123456789",       # 12 digits starting 91, remainder starts with 0
    "915876543210",       # 12 digits starting 91, remainder starts with 5
    "19876543210",        # 11 digits not starting with 0
    "०९८७६५४३२१०",        # Devanagari digits are not ASCII digits
])
def test_phone_invalid(raw):
    assert normalize_phone(raw) is None


def test_phone_ten_digits_starting_with_91_is_kept_as_is():
    assert normalize_phone("9123456789") == "+919123456789"


def test_phone_valid_remainders_for_each_leading_digit():
    for first in "6789":
        assert normalize_phone(f"91{first}123456789") == f"+91{first}123456789"


# ------------------------------------------------------------------- date
@pytest.mark.parametrize("raw,expected", [
    ("2026-10-05", "2026-10-05"),
    ("05/10/2026", "2026-10-05"),        # day-first
    ("05-10-2026", "2026-10-05"),
    ("05-Oct-2026", "2026-10-05"),
    ("05-oct-2026", "2026-10-05"),
    ("05-OCT-2026", "2026-10-05"),
    ("01-Dec-2026", "2026-12-01"),
    ("29/02/2028", "2028-02-29"),        # leap year
    (" 2026-10-05 ", "2026-10-05"),
])
def test_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "2026-13-01", "29/02/2027", "00/01/2026", "2026-00-10",
    "soon", "TBD", "", "05-Okt-2026", "05/10-2026", "5/10/2026", "2026/10/05",
    "05-October-2026", "10/05/26",
])
def test_date_invalid(raw):
    assert parse_date(raw) is None


# ---------------------------------------------------------------- decimal
@pytest.mark.parametrize("raw,expected", [
    ("12000", "12000.00"),
    ("12000.5", "12000.50"),
    ("₹27,554", "27554.00"),
    ("₹ 27,554", "27554.00"),
    ("Rs27554", "27554.00"),
    ("Rs 27554", "27554.00"),
    ("Rs.27554", "27554.00"),
    ("Rs. 24787", "24787.00"),
    ("rs. 24787", "24787.00"),
    ("RS 1", "1.00"),
    ("INR 18678.3", "18678.30"),
    ("inr18678.3", "18678.30"),
    ("1,23,456.78", "123456.78"),        # Indian grouping
    ("0", "0.00"),
    ("10.005", "10.01"),                 # half-up, not banker's rounding
    ("10.004", "10.00"),
    ("2.675", "2.68"),                   # would be 2.67 with binary floats
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", [
    "N/A", "-", "twelve thousand", "-500", "₹-500", "Rs", "", ".5", "1e5",
    "USD 100", "$100", "12 000", "1.2.3",
])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


# ------------------------------------------------------------------- enum
def test_enum_case_insensitive_and_lowercased():
    values = ["en", "hi", "ta"]
    assert parse_enum("EN", values) == "en"
    assert parse_enum(" hi ", values) == "hi"
    for bad in ("Hindi", "english", "tamil", "e n"):
        assert parse_enum(bad, values) is None


# -------------------------------------------------------------- timestamp
def test_timestamp_utc_z():
    ts = parse_timestamp("2026-10-05T13:20:21Z")
    assert ts.utc == "2026-10-05T13:20:21.000000Z"
    assert ts.ist == "2026-10-05T18:50:21+05:30"


def test_timestamp_offset():
    ts = parse_timestamp("2026-10-01T10:40:19+05:30")
    assert ts.utc == "2026-10-01T05:10:19.000000Z"
    assert ts.ist == "2026-10-01T10:40:19+05:30"


def test_timestamp_naive_means_ist():
    ts = parse_timestamp("2026-10-03 10:01:00")
    assert ts.ist == "2026-10-03T10:01:00+05:30"
    assert ts.utc == "2026-10-03T04:31:00.000000Z"


def test_ist_date_boundary():
    # 20:00Z is 01:30 IST on the next day; 18:29:59Z is still the same IST day.
    assert parse_timestamp("2026-10-05T20:00:00Z").ist_date == "2026-10-06"
    assert parse_timestamp("2026-10-05T18:29:59Z").ist_date == "2026-10-05"
    assert parse_timestamp("2026-10-05T18:30:00Z").ist_date == "2026-10-06"


def test_timestamp_other_offset_converted():
    assert parse_timestamp("2026-10-05T23:00:00-04:00").ist == "2026-10-06T08:30:00+05:30"


@pytest.mark.parametrize("raw", ["yesterday", "", None, 1696000000, "2026-10-05T25:00:00Z"])
def test_timestamp_invalid(raw):
    assert parse_timestamp(raw) is None


def test_timestamp_text_order_is_time_order():
    a = parse_timestamp("2026-10-05T18:00:00Z")          # 23:30 IST
    b = parse_timestamp("2026-10-05T23:45:00+05:30")     # 18:15Z
    assert (a.utc < b.utc) and (a.ist < b.ist)
