"""Tests for deid.recognizers.patterns. All people and numbers are synthetic."""
from __future__ import annotations

import time

import pytest

from deid import entities as E
from deid.config import CustomPattern, default_config
from deid.recognizers import patterns as P

CFG = default_config()
RECS = P.pattern_recognizers(CFG)


def spans(text, recs=None, cfg=CFG):
    out = []
    for r in recs or RECS:
        for s in r.find(text, cfg):
            assert s.text == text[s.start:s.end]
            assert 0.0 < s.score <= 1.0
            assert s.source == r.name
            out.append(s)
    return out


def values(text, entity):
    return [s.text for s in spans(text) if s.entity == entity]


def score(text, entity, value):
    hits = [s.score for s in spans(text) if s.entity == entity and s.text == value]
    assert hits, f"{entity} {value!r} not found in {text!r}"
    return max(hits)


# --------------------------------------------------------------------------- RRN / FRN

@pytest.mark.parametrize("text,entity,value,expected", [
    ("주민번호 900101-1234568", E.RRN, "900101-1234568", 0.95),     # valid checksum
    ("주민번호: 9001011234567 입니다", E.RRN, "9001011234567", 0.75),  # post-2020 style, no checksum
    ("900101 1234567", E.RRN, "900101 1234567", 0.75),
    ("900101 - 1234568", E.RRN, "900101 - 1234568", 0.95),
    ("000229-3123454", E.RRN, "000229-3123454", 0.95),              # 2000-02-29
    ("850302-2345672를 확인", E.RRN, "850302-2345672", 0.95),         # particle excluded
    ("외국인등록번호 900101-5234561", E.FRN, "900101-5234561", 0.95),
    ("900101-6234567", E.FRN, "900101-6234567", 0.75),
    ("880101-9123451", E.RRN, "880101-9123451", 0.9),               # 1800s, checksum ok
    ("880101-9123450", E.RRN, "880101-9123450", 0.55),              # 1800s, separator only
])
def test_rrn_frn_full(text, entity, value, expected):
    assert score(text, entity, value) == pytest.approx(expected)


@pytest.mark.parametrize("text,entity,value,expected", [
    ("900101-1******", E.RRN, "900101-1******", 0.8),
    ("주민번호 900101-1xxxxxx.", E.RRN, "900101-1xxxxxx", 0.8),
    ("900101-2XXXXXX", E.RRN, "900101-2XXXXXX", 0.8),
    ("900101-1234***", E.RRN, "900101-1234***", 0.8),
    ("900101-5●●●●●●", E.FRN, "900101-5●●●●●●", 0.8),
    ("900101-*******", E.RRN, "900101-*******", 0.7),
    ("9001011******", E.RRN, "9001011******", 0.8),
    ("주민번호 앞자리 900101-1 입니다", E.RRN, "900101-1", 0.7),
])
def test_rrn_masked_forms_still_detected(text, entity, value, expected):
    assert score(text, entity, value) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "900230-1234567",          # Feb 30
    "901301-1234567",          # month 13
    "000229-1234567",          # 1900-02-29 does not exist
    "010229-8234566",          # 2001-02-29 (FRN, 2000s)
    "12900101-1234567",        # inside a longer digit run
    "9001011234567890",        # 16 digits
    "900101-12345678",         # 8-digit tail
    "A9001011234567",          # glued to an alphanumeric code
    "900101-1234567-1",        # part of a longer hyphenated id
    "8801019123450",           # 1800s, no separator, bad checksum
    "900230-1******",          # masked but invalid date
    "900101*******",           # masked gender digit needs a separator
    "검체번호 240315-1",         # date-sequence numbering without RRN context
    "900101-1",
])
def test_rrn_negatives(text):
    assert values(text, E.RRN) == [] and values(text, E.FRN) == []


# --------------------------------------------------------------------------- PHONE

@pytest.mark.parametrize("value", [
    "010-1234-5678", "01012345678", "010 1234 5678", "010.1234.5678",
    "+82 10-1234-5678", "+82-10-1234-5678", "+821012345678", "+82 (0)10-1234-5678",
    "(02) 123-4567", "02-1234-5678", "031-123-4567", "064-123-4567", "070-1234-5678",
    "0505-123-4567", "080-123-4567", "1588-1234", "011-123-4567", "+82 2-123-4567",
])
def test_phone_shapes(value):
    text = f"연락처: {value}로 연락 바랍니다."
    assert values(text, E.PHONE) == [value]


def test_phone_scores_and_context():
    assert score("010-1234-5678", E.PHONE, "010-1234-5678") == pytest.approx(0.9)
    assert score("1588-1234", E.PHONE, "1588-1234") == pytest.approx(0.75)
    assert score("고객센터 1588-1234", E.PHONE, "1588-1234") == pytest.approx(0.9)
    assert score("0212345678", E.PHONE, "0212345678") == pytest.approx(0.55)
    assert score("전화 0212345678", E.PHONE, "0212345678") == pytest.approx(0.85)
    assert values("15881234", E.PHONE) == []
    assert values("문의 15881234", E.PHONE) == ["15881234"]


def test_phone_multiple_and_separated_by_slash():
    text = "HP 010-1234-5678/02-123-4567, 보호자(이서연) 010.9876.5432"
    assert values(text, E.PHONE) == ["010-1234-5678", "02-123-4567", "010.9876.5432"]


@pytest.mark.parametrize("text", [
    "BP 120/80", "HR 88", "WBC 10.5", "Hb 13.2 g/dL", "2024-03-15", "14:30", "J20.9", "E11.9",
    "35,000원", "500mg", "1.2.3", "2019-2023", "lot 1588", "1588",
    "주문번호 9201012345678901", "A01012345678", "ID01012345678X", "식이 1500-1800 kcal",
    "1800-2000", "1600-1700년대", "010-1234-56789", "12-010-1234-5678", "0.01012345678",
    "02-123-45678", "PLT 250 x10^3/uL, Cr 0.92",
])
def test_phone_negatives(text):
    assert values(text, E.PHONE) == []


# --------------------------------------------------------------------------- EMAIL / URL

@pytest.mark.parametrize("text,value", [
    ("이메일 hong.gildong@example.co.kr입니다", "hong.gildong@example.co.kr"),
    ("kim_minjun+test@hospital.org.", "kim_minjun+test@hospital.org"),
    ("<lee.seoyeon@mail.example.com>", "lee.seoyeon@mail.example.com"),
])
def test_email(text, value):
    assert values(text, E.EMAIL) == [value]


@pytest.mark.parametrize("text", ["user@localhost", "@mention", "a@b", "test@.com"])
def test_email_negatives(text):
    assert values(text, E.EMAIL) == []


@pytest.mark.parametrize("text,value", [
    ("https://example.com/path?a=1에서 확인", "https://example.com/path?a=1"),
    ("www.naver.com.", "www.naver.com"),
    ("(https://en.wikipedia.org/wiki/Foo_(bar))", "https://en.wikipedia.org/wiki/Foo_(bar)"),
    ("http://intranet:8080/emr?pid=12345를", "http://intranet:8080/emr?pid=12345"),
    ("링크: https://blog.example.com/홍길동", "https://blog.example.com/홍길동"),
    ("see http://example.org, then", "http://example.org"),
])
def test_url(text, value):
    assert values(text, E.URL) == [value]


@pytest.mark.parametrize("text", ["www.", "https://", "e.g. this", "www.한글"])
def test_url_negatives(text):
    assert values(text, E.URL) == []


# --------------------------------------------------------------------------- CARD

@pytest.mark.parametrize("text,value", [
    ("카드 4111-1111-1111-1111", "4111-1111-1111-1111"),
    ("4111 1111 1111 1111 3개월 할부", "4111 1111 1111 1111"),
    ("4111111111111111", "4111111111111111"),
    ("Amex 3782-822463-10005", "3782-822463-10005"),
    ("5555-5555-5555-4444로 결제", "5555-5555-5555-4444"),
    ("Mastercard 2221000000000009", "2221000000000009"),
])
def test_card(text, value):
    assert values(text, E.CARD) == [value]


@pytest.mark.parametrize("text", [
    "4111-1111-1111-1112",        # Luhn fails
    "900101-1234568",             # RRN
    "4222222222222",              # 13 digits: left to RRN territory
    "12345678901234567890",       # too long
    "x4111111111111111",          # glued to letters
    "2024031512345672",           # Luhn-valid order number, but no card issuer starts with 2024
    "010-1234-5678",
])
def test_card_negatives(text):
    assert values(text, E.CARD) == []


# --------------------------------------------------------------------------- ACCOUNT

@pytest.mark.parametrize("text,value", [
    ("신한은행 110-123-456789 예금주 홍길동", "110-123-456789"),
    ("계좌번호: 123-456789-01-012", "123-456789-01-012"),
    ("카카오뱅크 3333-01-1234567", "3333-01-1234567"),
    ("1002-123-456789 (우리은행)으로 입금", "1002-123-456789"),
    ("계좌 110123456789", "110123456789"),
])
def test_account_with_context(text, value):
    assert values(text, E.ACCOUNT) == [value]


@pytest.mark.parametrize("text", [
    "번호 110-123-456789",                    # no banking context
    "은행 담당자 연락처 010-1234-5678",          # phone, not account
    "은행 대표 02-1234-5678",
    "입금자 주민번호 900101-1234568",           # RRN, not account
    "국민건강보험 1234567890",                  # 국민 here is not the bank
    "계좌 개설일 2024-03-15",
    "은행 제출용 면허 11-12-345678-90",
    "계좌 4111-1111-1111-1111",                 # card
    "계좌 12345",                               # too short
])
def test_account_negatives(text):
    assert values(text, E.ACCOUNT) == []


def test_account_context_only_after_scores_lower():
    # so a label right before the number (e.g. 건강보험증번호) wins the overlap
    assert score("1002-123-456789 (우리은행)으로 입금", E.ACCOUNT, "1002-123-456789") == pytest.approx(0.8)
    assert score("신한은행 110-123-456789", E.ACCOUNT, "110-123-456789") == pytest.approx(0.85)
    text = "건강보험증번호 9876543210   계좌: 신한은행 110-234-567890"
    hi = score(text, E.HEALTH_INSURANCE, "9876543210")
    assert hi > score(text, E.ACCOUNT, "9876543210")


# --------------------------------------------------------------------------- PASSPORT

@pytest.mark.parametrize("text,value,expected", [
    ("여권번호 M12345678", "M12345678", 0.9),
    ("passport no. m12345678", "m12345678", 0.9),
    ("M123A4567", "M123A4567", 0.75),
    ("S12345678을", "S12345678", 0.6),
    ("T987B6543", "T987B6543", 0.75),
])
def test_passport(text, value, expected):
    assert score(text, E.PASSPORT, value) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "m12345678", "A12345678", "M1234567", "M123456789", "XM12345678", "M12345678A", "M123a4567",
])
def test_passport_negatives(text):
    assert values(text, E.PASSPORT) == []


# --------------------------------------------------------------------------- DRIVER_LICENSE

@pytest.mark.parametrize("text,value,expected", [
    ("운전면허번호 11-12-345678-90", "11-12-345678-90", 0.95),
    ("11-12-345678-90", "11-12-345678-90", 0.85),
    ("28-20-123456-01", "28-20-123456-01", 0.85),
    ("서울 12-345678-90", "서울 12-345678-90", 0.9),
    ("면허: 서울12-345678-90", "서울12-345678-90", 0.95),
    ("경기북부 20-123456-11", "경기북부 20-123456-11", 0.9),
    ("면허번호 111234567890", "111234567890", 0.8),
])
def test_driver_license(text, value, expected):
    assert score(text, E.DRIVER_LICENSE, value) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "27-12-345678-90", "10-12-345678-90", "111234567890", "11 12 345678 90", "111-12-345678-90",
])
def test_driver_license_negatives(text):
    assert values(text, E.DRIVER_LICENSE) == []


# --------------------------------------------------------------------------- BRN

@pytest.mark.parametrize("text,value,expected", [
    ("사업자등록번호: 123-45-67891", "123-45-67891", 0.95),
    ("123-45-67891", "123-45-67891", 0.9),
    ("220-81-12341", "220-81-12341", 0.9),
    ("사업자번호 1234567891", "1234567891", 0.85),
])
def test_brn(text, value, expected):
    assert score(text, E.BRN, value) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "123-45-67890", "1234567891", "123-45-678912", "사업자 123-45-67890",
])
def test_brn_negatives(text):
    assert values(text, E.BRN) == []


# --------------------------------------------------------------------------- HEALTH_INSURANCE

@pytest.mark.parametrize("text,value", [
    ("건강보험증번호: 12345678901", "12345678901"),
    ("보험증 번호 5-1234567890", "5-1234567890"),
    ("건강보험 1234567890 (피부양자)", "1234567890"),
    # phone-shaped, but a bare digit run right after its own label
    ("건강보험증번호 0113626399 / 피보험자", "0113626399"),
    ("건강보험증번호: 01012345678", "01012345678"),
])
def test_health_insurance(text, value):
    assert values(text, E.HEALTH_INSURANCE) == [value]


@pytest.mark.parametrize("text", [
    "12345678901", "건강보험 가입자 연락처 01012345678", "건강보험 123456789", "보험 청구액 1234567890",
    "건강보험 문의 010-1234-5678", "보험증번호 문의 01012345678", "건강보험증번호 010-1234-5678",
])
def test_health_insurance_negatives(text):
    assert values(text, E.HEALTH_INSURANCE) == []


# --------------------------------------------------------------------------- MRN

@pytest.mark.parametrize("text,value,expected", [
    ("등록번호: 12345678 홍길동", "12345678", 0.9),
    ("차트번호 A1234567", "A1234567", 0.9),
    ("차트 No. 2024-00123", "2024-00123", 0.9),
    ("MRN#987654", "987654", 0.9),
    ("Chart No.: 7654321", "7654321", 0.9),
    ("PT ID 998877", "998877", 0.9),
    ("환자ID: P-123456", "P-123456", 0.9),
    ("병록번호 00012345번", "00012345", 0.9),
    ("환자번호\n12345678", "12345678", 0.9),
    ("등록번호(MRN): 55501234", "55501234", 0.9),
    ("Patient ID: 20240315", "20240315", 0.9),
    ("ID: 123456", "123456", 0.7),
])
def test_mrn(text, value, expected):
    assert score(text, E.MRN, value) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "주민등록번호: 900101-1234568",
    "외국인 등록번호 900101-5234561",
    "사업자등록번호 123-45-67891",
    "차량등록번호 1234567",
    "등록번호: 12",
    "ID consult 예정",
    "등록번호 미발급",
    "HIV 1234",
])
def test_mrn_negatives(text):
    assert values(text, E.MRN) == []


# --------------------------------------------------------------------------- IP

@pytest.mark.parametrize("text,value,expected", [
    ("IP 192.168.0.1", "192.168.0.1", 0.95),
    ("서버 접속: 10.0.0.12.", "10.0.0.12", 0.95),
    ("from 172.16.254.3 at", "172.16.254.3", 0.85),
    ("2001:db8::1", "2001:db8::1", 0.85),
    ("addr fe80::1ff:fe23:4567:890a,", "fe80::1ff:fe23:4567:890a", 0.85),
    ("IPv6:2001:db8::7334", "2001:db8::7334", 0.85),
])
def test_ip(text, value, expected):
    assert score(text, E.IP, value) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "10.0.0.256", "1.2.3.4.5", "v1.2.3.4", "version 10.2.3.4", "1.2.3.4 항목", "1.2.3", "14:30:15",
    "00:1a:2b:3c:4d:5e", "std::vector", "a::b", "2024.03.15",
])
def test_ip_negatives(text):
    assert values(text, E.IP) == []


# --------------------------------------------------------------------------- PLATE

@pytest.mark.parametrize("text,value", [
    ("12가3456", "12가3456"),
    ("123가4567을 주차", "123가4567"),
    ("서울12가3456", "서울12가3456"),
    ("차량번호 12가 3456", "12가 3456"),
    ("렌트카 45하1234", "45하1234"),
])
def test_plate(text, value):
    assert values(text, E.PLATE) == [value]


@pytest.mark.parametrize("text", ["37도 2000년 이후", "3개월 1234", "12가34567", "1가3456", "120도 1234mg"])
def test_plate_negatives(text):
    assert values(text, E.PLATE) == []


# --------------------------------------------------------------------------- DATE / BIRTHDATE

@pytest.mark.parametrize("text,value", [
    ("2024-03-15 내원", "2024-03-15"),
    ("2024.03.15", "2024.03.15"),
    ("다음 내원 2024. 3. 15.", "2024. 3. 15"),
    ("2024/03/15", "2024/03/15"),
    ("2024년 3월 15일에", "2024년 3월 15일"),
    ("2024년3월15일", "2024년3월15일"),
    ("24.03.15 CT", "24.03.15"),
    ("2024년 3월 초", "2024년 3월"),
    ("24년 3월 15일", "24년 3월 15일"),
    ("2024-02-29", "2024-02-29"),
    ("seen on March 15, 2024.", "March 15, 2024"),
    ("15 Mar 2024", "15 Mar 2024"),
])
def test_date(text, value):
    assert values(text, E.DATE) == [value]
    assert values(text, E.BIRTHDATE) == []


@pytest.mark.parametrize("text", [
    "2024-13-15", "2023-02-29", "24.13.15", "24.02.30", "1.24.03.15", "24.03.15.1", "2019-2023",
    "3/15", "J20.9", "1.2.3", "BP 120/80", "15/03/2024", "20240315", "3월 15일", "2024년", "v24.03.15",
    "12.5.3", "2024-03",
])
def test_date_negatives(text):
    assert values(text, E.DATE) == []


@pytest.mark.parametrize("text,value", [
    ("생년월일: 1985-03-02", "1985-03-02"),
    ("DOB: 03/15/1985", "03/15/1985"),
    ("D.O.B 1985.03.02", "1985.03.02"),
    ("Date of birth 2 Jan 1990", "2 Jan 1990"),
    ("생일 3월 2일", "3월 2일"),
    ("생년월일 19850302", "19850302"),
    ("생년월일 850302", "850302"),
    ("출생: 1985년 3월 2일", "1985년 3월 2일"),
    ("1985.03.02생", "1985.03.02생"),
    ("85.03.02생인 환자", "85.03.02생"),
    ("85년생", "85년생"),
    ("1985년생 남자", "1985년생"),
    ("1985년 3월 2일생", "1985년 3월 2일생"),
])
def test_birthdate(text, value):
    assert values(text, E.BIRTHDATE) == [value]
    assert values(text, E.DATE) == []


def test_birthdate_context_does_not_leak_to_later_dates():
    text = "생년월일: 1985-03-02, 내원일: 2024-03-15"
    assert values(text, E.BIRTHDATE) == ["1985-03-02"]
    assert values(text, E.DATE) == ["2024-03-15"]
    text = "DOB 1985-03-02 2024-03-15"
    assert values(text, E.BIRTHDATE) == ["1985-03-02"]
    assert values(text, E.DATE) == ["2024-03-15"]
    text = "생년월일 및 내원일 2024-03-15"
    assert values(text, E.DATE) == ["2024-03-15"]


@pytest.mark.parametrize("text,value", [
    ("증상 발생일 2024.03.10", "2024.03.10"),     # 발생일 contains 생일 but is not a birthday
    ("2024.03.15생검 시행", "2024.03.15"),       # 생검 (biopsy) is not the birth suffix
])
def test_birth_lookalikes_are_plain_dates(text, value):
    assert values(text, E.DATE) == [value]
    assert values(text, E.BIRTHDATE) == []


# --------------------------------------------------------------------------- clinical text

CLINICAL_NOTE = """\
S) 3일 전부터 기침, 가래, 발열 38.2도. 1일 3회 복용.
O) BP 130/85 mmHg, HR 92회/분, RR 20, BT 37.8℃, SpO2 97% (RA). 키 172cm, 체중 68.5kg, BMI 23.1
Lab: WBC 12,300/μL, Hb 13.2 g/dL, PLT 250 x10^3/μL, CRP 4.5 mg/dL, HbA1c 7.2%, eGFR 85
Cr 0.92 mg/dL, AST/ALT 25/30 IU/L, TSH 2.15 uIU/mL, LDL 135 mg/dL, INR 1.05, PT 12.3 sec
A) J20.9 급성 기관지염, E11.9 제2형 당뇨병, I10 본태성 고혈압. ICD-10 R50.9
P) Amoxicillin 500mg 1T tid x 7일, Metformin 1000mg bid, 본인부담 35,000원. 2주 후 f/u.
검사 결과 3/15 확인. 14:30 내원. 수액 1000cc, 식이 1800kcal, 2019-2023 추적, 1.2.3 버전, 2.1.3.4절
NRS 3/10, 10/160 mg, 37도 2000년 이후, lot 1588, 교부번호 2024031500001, 요양기관 12345678
"""


def test_clinical_vocabulary_produces_no_spans():
    assert spans(CLINICAL_NOTE) == []


def test_mixed_record():
    text = ("환자 홍길동(남/39세) 주민번호 850302-2345672, 등록번호: 12345678\n"
            "연락처 010-1234-5678 / 보호자 김민준 02-123-4567, 이메일 minjun.kim@example.com\n"
            "생년월일 1985.03.02, 내원 2024-03-15, 신한은행 110-123-456789")
    got = {(s.entity, s.text) for s in spans(text)}
    assert got == {
        (E.RRN, "850302-2345672"), (E.MRN, "12345678"), (E.PHONE, "010-1234-5678"),
        (E.PHONE, "02-123-4567"), (E.EMAIL, "minjun.kim@example.com"),
        (E.BIRTHDATE, "1985.03.02"), (E.DATE, "2024-03-15"), (E.ACCOUNT, "110-123-456789"),
    }


# --------------------------------------------------------------------------- assembly / custom

def test_recognizer_protocol_and_entities():
    covered = set()
    for r in RECS:
        assert isinstance(r.name, str) and r.name
        assert isinstance(r.entities, tuple) and r.entities
        assert callable(r.find)
        covered.update(r.entities)
    expected = set(E.ALL) - {E.PERSON, E.ADDRESS, E.OTHER}
    assert covered == expected
    assert len({r.name for r in RECS}) == len(RECS)


def test_pattern_recognizers_respects_enabled():
    cfg = default_config()
    cfg.enabled = {E.PHONE, E.BIRTHDATE}
    names = {r.name for r in P.pattern_recognizers(cfg)}
    assert names == {"kr_phone", "date"}


def test_custom_patterns():
    cfg = default_config()
    cfg.custom_patterns = [
        CustomPattern(entity="STAFF_ID", regex=r"EMP-\d{5}", score=0.8),
        CustomPattern(entity=E.MRN, regex=r"(?<!\d)(\d{6})(?!\d)", context=["환자"]),
    ]
    recs = P.pattern_recognizers(cfg)
    custom = [r for r in recs if r.name.startswith("custom_")]
    assert len(custom) == 2
    text = "담당 EMP-00123, 환자 코드 654321"
    got = {(s.entity, s.text, s.score) for s in spans(text, recs=custom, cfg=cfg)}
    assert got == {("STAFF_ID", "EMP-00123", 0.8), (E.MRN, "654321", 0.9)}
    # context is required for the second pattern
    assert [s.text for s in spans("코드 654321", recs=custom, cfg=cfg)] == []


def test_custom_pattern_disabled_and_invalid():
    cfg = default_config()
    cfg.custom_patterns = [CustomPattern(entity="STAFF_ID", regex=r"EMP-\d+")]
    cfg.enabled = {E.PHONE}
    assert not [r for r in P.pattern_recognizers(cfg) if r.name.startswith("custom_")]
    cfg = default_config()
    cfg.custom_patterns = [CustomPattern(entity="BAD", regex=r"(unclosed")]
    with pytest.raises(ValueError, match="BAD"):
        P.pattern_recognizers(cfg)


def test_performance_100kb():
    para = ("환자 홍길동(900101-1234568) 연락처 010-1234-5678, hong@example.com, BP 120/80, WBC 10.5, "
            "2024-03-15 내원. 신한은행 110-123-456789. 등록번호: 12345678. IP 192.168.0.1 12가3456\n")
    text = (para * (100_000 // len(para) + 1))[:100_000]
    adversarial = ["1234567890" * 10_000, "deadbeef:" * 11_000, "1." * 50_000, "x*" * 50_000,
                   ":" * 100_000, "a@" * 50_000, "https://" + "a" * 100_000]
    for t in [text] + adversarial:
        t0 = time.perf_counter()
        spans(t)
        assert time.perf_counter() - t0 < 1.0


# --------------------------------------------------------------------------- presidio cross-check

# Shared samples: (text, our expected entity or None). Where presidio's korean recognizers disagree
# with us, the reason is recorded in INTENTIONAL_DIFFERENCES ("ours" = only we detect it,
# "presidio" = only presidio does).
CROSS_SAMPLES = [
    ("900101-1234568", E.RRN),
    ("9001011234567", E.RRN),
    ("850302-2345672", E.RRN),
    ("000229-3123454", E.RRN),
    ("040815-4123450", E.RRN),
    ("900101-5234561", E.FRN),
    ("900101-6234567", E.FRN),
    ("900101 1234567", E.RRN),
    ("900231-1234567", None),
    ("010229-4123456", None),
    ("880101-9123451", E.RRN),
    ("900101-1******", E.RRN),
    ("12900101-1234567", None),
    ("9001011234567890", None),
    ("123-45-67891", E.BRN),
    ("220-81-12341", E.BRN),
    ("123-45-67890", None),
    ("1234567891", None),
    ("사업자등록번호 1234567891", E.BRN),
    ("11-12-345678-90", E.DRIVER_LICENSE),
    ("26-12-345678-90", E.DRIVER_LICENSE),
    ("27-12-345678-90", None),
    ("11 12 345678 90", None),
    ("111234567890", None),
    ("운전면허 111234567890", E.DRIVER_LICENSE),
    ("서울 12-345678-90", E.DRIVER_LICENSE),
    ("M12345678", E.PASSPORT),
    ("M123A4567", E.PASSPORT),
    ("m12345678", None),
    ("O12345678", None),
    ("G12345678", E.PASSPORT),
]

INTENTIONAL_DIFFERENCES = {
    "900101 1234567": ("ours", "space-separated RRNs are common in OCR/Excel output"),
    "900231-1234567": ("presidio", "we validate the calendar date (Feb 31 rejected)"),
    "010229-4123456": ("presidio", "leap years are checked with the century from the gender digit"),
    "880101-9123451": ("ours", "gender digits 9/0 (1800s births) are accepted as RRN"),
    "900101-1******": ("ours", "masked RRNs still leak birth date and gender"),
    "1234567891": ("presidio", "bare 10-digit BRNs need context (too many unrelated 10-digit ids)"),
    "11 12 345678 90": ("presidio", "space/no separator driver licences need context"),
    "111234567890": ("presidio", "space/no separator driver licences need context"),
    "서울 12-345678-90": ("ours", "region-name licence form is not in presidio"),
    "m12345678": ("presidio", "lowercase passport prefix only with 여권/passport context"),
    "O12345678": ("presidio", "prefix set follows the task spec {M,S,R,G,D,T}"),
    "G12345678": ("ours", "prefix set follows the task spec {M,S,R,G,D,T}"),
}

_PRESIDIO_TO_DEID = {
    "KR_RRN": E.RRN, "KR_FRN": E.FRN, "KR_BRN": E.BRN,
    "KR_DRIVER_LICENSE": E.DRIVER_LICENSE, "KR_PASSPORT": E.PASSPORT,
}
_CROSS_ENTITIES = set(_PRESIDIO_TO_DEID.values())


def _presidio_hits(text):
    korea = pytest.importorskip("presidio_analyzer.predefined_recognizers.country_specific.korea")
    recs = [korea.KrRrnRecognizer(), korea.KrFrnRecognizer(), korea.KrBrnRecognizer(),
            korea.KrDriverLicenseRecognizer(), korea.KrPassportRecognizer()]
    hits = set()
    for r in recs:
        for res in r.analyze(text, entities=r.supported_entities, nlp_artifacts=None):
            hits.add((_PRESIDIO_TO_DEID[res.entity_type], text[res.start:res.end]))
    return hits


def _our_hits(text):
    return {(s.entity, s.text) for s in spans(text) if s.entity in _CROSS_ENTITIES}


@pytest.mark.parametrize("text,expected", CROSS_SAMPLES)
def test_cross_check_with_presidio(text, expected):
    theirs = _presidio_hits(text)
    ours = _our_hits(text)
    assert {e for e, _ in ours} == ({expected} if expected else set())
    if text in INTENTIONAL_DIFFERENCES:
        side, _reason = INTENTIONAL_DIFFERENCES[text]
        if side == "ours":
            assert ours and not theirs
        else:
            assert theirs and not ours
    else:
        # Same entity; presidio spans never include the context words or region names.
        assert {e for e, _ in ours} == {e for e, _ in theirs}
        assert {v for _, v in ours} == {v for _, v in theirs}


# --------------------------------------------------------------------------- parse_date helper

@pytest.mark.parametrize("value,expected", [
    ("2024-03-15", (2024, 3, 15)),
    ("2024. 3. 15", (2024, 3, 15)),
    ("2024년 3월", (2024, 3, None)),
    ("24.03.15", (2024, 3, 15)),
    ("1985.03.02생", (1985, 3, 2)),
    ("85.03.02생", (1985, 3, 2)),
    ("85년생", (1985, None, None)),
    ("1985년생", (1985, None, None)),
    ("1985년 3월 2일생", (1985, 3, 2)),
    ("850302", (1985, 3, 2)),
    ("19850302", (1985, 3, 2)),
    ("03/15/1985", (1985, 3, 15)),
    ("15/03/1985", (1985, 3, 15)),
    ("03/04/1985", (1985, None, None)),     # month/day order unknown
    ("3월 2일", (None, 3, 2)),
    ("March 15, 2024", (2024, 3, 15)),
    ("2 Jan 1990", (1990, 1, 2)),
    ("not a date", (None, None, None)),
])
def test_parse_date(value, expected):
    assert P.parse_date(value) == expected


def test_parse_date_round_trips_detected_spans():
    text = "생년월일 85.03.02, 내원 2024년 3월 15일, 1990년생"
    for s in spans(text):
        year, _, _ = P.parse_date(s.text)
        assert year in (1985, 2024, 1990)


def test_expand_two_digit_year_pivot():
    assert P._expand_yy(26, today_year=2026) == 2026
    assert P._expand_yy(27, today_year=2026) == 1927
    assert P._expand_yy(0, today_year=2026) == 2000


@pytest.mark.parametrize("text,value", [
    ("의사 면허번호 제12345호", "12345"),
    ("면허번호: 123456", "123456"),
    ("한의사면허 제 4567 호", "4567"),
    ("간호사 면허번호 98765", "98765"),
    ("전문의 자격번호 제1234호", "1234"),
    ("Medical License No. 54321", "54321"),
])
def test_medical_license_positive(text, value):
    spans = [s for s in P.MedicalLicenseRecognizer().find(text, default_config())]
    assert [s.text for s in spans] == [value]
    assert spans[0].entity == E.MEDICAL_LICENSE


@pytest.mark.parametrize("text", [
    "운전면허번호 11-12-345678-90",
    "운전면허 12345",
    "면허 취득 후 3년",
    "사업면허 12345",
])
def test_medical_license_negative(text):
    assert P.MedicalLicenseRecognizer().find(text, default_config()) == []
