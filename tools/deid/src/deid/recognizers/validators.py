"""Checksum and format validators for Korean IDs, cards, dates and IP addresses."""
from __future__ import annotations

import calendar
import ipaddress

_RRN_WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
_BRN_WEIGHTS = (1, 3, 7, 1, 3, 7, 1, 3, 5)

# RRN/FRN 7th digit -> century of birth (9/0: born in the 1800s).
_CENTURY = {
    "1": 1900, "2": 1900, "5": 1900, "6": 1900,
    "3": 2000, "4": 2000, "7": 2000, "8": 2000,
    "9": 1800, "0": 1800,
}


def digits(s: str) -> str:
    """Return only the ASCII digits of `s`."""
    return "".join(c for c in s if "0" <= c <= "9")


def is_valid_date(year: int, month: int, day: int) -> bool:
    """True if year/month/day is a real calendar date (leap years honoured)."""
    if not (1 <= month <= 12) or year < 1:
        return False
    return 1 <= day <= calendar.monthrange(year, month)[1]


def is_valid_yy_date(yy: int, month: int, day: int) -> bool:
    """Two-digit year: valid if the date exists in the 1900s or the 2000s."""
    return is_valid_date(1900 + yy, month, day) or is_valid_date(2000 + yy, month, day)


def rrn_century(gender_digit: str) -> int | None:
    """Century implied by the RRN/FRN gender digit, e.g. '1' -> 1900."""
    return _CENTURY.get(gender_digit)


def rrn_date_ok(yymmdd: str, gender_digit: str | None = None) -> bool:
    """Validate the YYMMDD part; unknown gender digit -> any plausible century."""
    if len(yymmdd) != 6 or not yymmdd.isdigit():
        return False
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    century = rrn_century(gender_digit) if gender_digit else None
    if century is None:
        return is_valid_yy_date(yy, mm, dd)
    return is_valid_date(century + yy, mm, dd)


def _rrn_sum(d13: str) -> int:
    return sum(int(d13[i]) * w for i, w in enumerate(_RRN_WEIGHTS))


def rrn_checksum_ok(d13: str) -> bool:
    """Pre-2020-10 RRN check digit: (11 - weighted_sum % 11) % 10."""
    if len(d13) != 13 or not d13.isdigit():
        return False
    return (11 - _rrn_sum(d13) % 11) % 10 == int(d13[12])


def frn_checksum_ok(d13: str) -> bool:
    """Pre-2020-10 foreigner number check digit: (13 - weighted_sum % 11) % 10."""
    if len(d13) != 13 or not d13.isdigit():
        return False
    return (13 - _rrn_sum(d13) % 11) % 10 == int(d13[12])


def luhn_ok(number: str) -> bool:
    """Luhn (mod 10) check used by payment cards. Separators are ignored."""
    ds = digits(number)
    if len(ds) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(ds)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def brn_checksum_ok(number: str) -> bool:
    """Korean business registration number (사업자등록번호) check digit."""
    ds = digits(number)
    if len(ds) != 10:
        return False
    nums = [int(c) for c in ds]
    total = sum(n * w for n, w in zip(nums[:8], _BRN_WEIGHTS[:8]))
    last = nums[8] * _BRN_WEIGHTS[8]
    total += last + last // 10
    return (10 - total % 10) % 10 == nums[9]


def ipv4_ok(s: str) -> bool:
    """Four dot-separated decimal octets 0-255 (leading zeros tolerated)."""
    parts = s.split(".")
    if len(parts) != 4:
        return False
    return all(p.isdigit() and 1 <= len(p) <= 3 and int(p) <= 255 for p in parts)


def ipv6_ok(s: str) -> bool:
    """Full (8 groups) or '::'-compressed IPv6 address."""
    if "::" not in s and s.count(":") != 7:
        return False
    try:
        ipaddress.IPv6Address(s)
    except ValueError:
        return False
    return True
