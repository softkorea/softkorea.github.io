"""Tests for deid.recognizers.validators (checksums, dates, IPs)."""
import pytest

from deid.recognizers import validators as V


@pytest.mark.parametrize("y,m,d,ok", [
    (2024, 2, 29, True),    # leap year
    (2023, 2, 29, False),
    (2000, 2, 29, True),    # divisible by 400
    (1900, 2, 29, False),   # divisible by 100 only
    (2024, 4, 31, False),
    (2024, 12, 31, True),
    (2024, 13, 1, False),
    (2024, 0, 10, False),
    (2024, 1, 0, False),
])
def test_is_valid_date(y, m, d, ok):
    assert V.is_valid_date(y, m, d) is ok


def test_is_valid_yy_date_accepts_either_century():
    assert V.is_valid_yy_date(0, 2, 29)       # 2000-02-29
    assert V.is_valid_yy_date(4, 2, 29)       # 1904 / 2004
    assert not V.is_valid_yy_date(1, 2, 29)
    assert not V.is_valid_yy_date(24, 2, 30)


@pytest.mark.parametrize("g,century", [
    ("1", 1900), ("2", 1900), ("5", 1900), ("6", 1900),
    ("3", 2000), ("4", 2000), ("7", 2000), ("8", 2000),
    ("9", 1800), ("0", 1800), ("x", None),
])
def test_rrn_century(g, century):
    assert V.rrn_century(g) == century


@pytest.mark.parametrize("yymmdd,g,ok", [
    ("900101", "1", True),
    ("000229", "3", True),    # 2000-02-29
    ("000229", "1", False),   # 1900-02-29 does not exist
    ("040229", "4", True),
    ("010229", "8", False),   # 2001 is not a leap year
    ("880229", "9", True),    # 1888 is a leap year
    ("900230", "1", False),
    ("901301", "1", False),
    ("900100", "2", False),
    ("000229", None, True),   # unknown century: valid in 2000
    ("010229", None, False),
    ("90010", "1", False),
    ("9001o1", "1", False),
])
def test_rrn_date_ok(yymmdd, g, ok):
    assert V.rrn_date_ok(yymmdd, g) is ok


def test_rrn_and_frn_checksums():
    # Check digits computed independently (and cross-checked against presidio's implementation).
    assert V.rrn_checksum_ok("9001011234568")
    assert not V.rrn_checksum_ok("9001011234567")
    assert V.rrn_checksum_ok("8503022345672")
    assert V.frn_checksum_ok("9001015234561")
    assert not V.frn_checksum_ok("9001015234569")
    assert V.rrn_checksum_ok("9001015234569")  # same digits, RRN formula differs from FRN
    assert not V.rrn_checksum_ok("900101123456")       # too short
    assert not V.rrn_checksum_ok("90010112345a8")


@pytest.mark.parametrize("num,ok", [
    ("4111111111111111", True),
    ("4111-1111-1111-1111", True),
    ("79927398713", True),
    ("378282246310005", True),
    ("4111111111111112", False),
    ("1234567812345678", False),
    ("0", False),
    ("", False),
])
def test_luhn(num, ok):
    assert V.luhn_ok(num) is ok


@pytest.mark.parametrize("num,ok", [
    ("123-45-67891", True),
    ("1234567891", True),
    ("220-81-12341", True),
    ("104-81-23454", True),
    ("123-45-67890", False),
    ("220-81-12342", False),
    ("123-45-6789", False),
])
def test_brn_checksum(num, ok):
    assert V.brn_checksum_ok(num) is ok


@pytest.mark.parametrize("ip,ok", [
    ("192.168.0.1", True),
    ("0.0.0.0", True),
    ("255.255.255.255", True),
    ("192.168.001.010", True),
    ("256.1.1.1", False),
    ("1.2.3", False),
    ("1.2.3.4.5", False),
    ("1..2.3", False),
    ("1234.1.1.1", False),
])
def test_ipv4_ok(ip, ok):
    assert V.ipv4_ok(ip) is ok


@pytest.mark.parametrize("ip,ok", [
    ("2001:db8::1", True),
    ("2001:0db8:85a3:0000:0000:8a2e:0370:7334", True),
    ("fe80::1ff:fe23:4567:890a", True),
    ("::1", True),
    ("14:30:15", False),                # a time, not an address
    ("00:1a:2b:3c:4d:5e", False),       # MAC address
    ("2001:db8:::1", False),
    ("12345::1", False),
])
def test_ipv6_ok(ip, ok):
    assert V.ipv6_ok(ip) is ok


def test_digits():
    assert V.digits("010-1234 5678") == "01012345678"
    assert V.digits("abc") == ""
