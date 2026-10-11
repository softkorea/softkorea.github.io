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
    # PERSON only through query-string values ("?name=..."), never ADDRESS
    expected = set(E.ALL) - {E.ADDRESS}
    assert covered == expected
    assert {r.name for r in RECS if E.PERSON in r.entities} == {"url_param"}
    assert len({r.name for r in RECS}) == len(RECS)


def test_pattern_recognizers_respects_enabled():
    cfg = default_config()
    cfg.enabled = {E.PHONE, E.BIRTHDATE}
    names = {r.name for r in P.pattern_recognizers(cfg)}
    assert names == {"kr_phone", "date", "url_param"}   # url_param: ?tel=..., ?dob=...
    cfg.enabled = {E.CARD}
    assert {r.name for r in P.pattern_recognizers(cfg)} == {"card"}


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


# --------------------------------------------------------------------------- red-team regressions

def _deid(text):
    from deid import deidentify
    return deidentify(text)[0]


@pytest.mark.parametrize("text, expected", [
    ("주민번호: 940305 / 2048117", "940305 / 2048117"),
    ("주민번호 940305·2048117", "940305·2048117"),
    ("주민번호 940305_2048117", "940305_2048117"),
])
def test_tl02_rrn_split_separators_slash_dot_underscore(text, expected):
    assert values(text, E.RRN) == [expected]


@pytest.mark.parametrize("text, back, front", [
    ("발신자 본인 확인 - 주민번호 뒷자리 2048117 (앞자리 940305)", "2048117", "940305"),
    ("주민번호 앞자리 940305 뒷자리 2048117 불러주심", "2048117", "940305"),
    ("피보험자 주민번호 앞 6자리 870204, 뒤 7자리 1538214", "1538214", "870204"),
    ("주민번호 뒷자리 확인 2048117 (앞자리 940305)", "2048117", "940305"),
])
def test_tl02_rrn_split_halves(text, back, front):
    assert values(text, E.RRN) == [back]
    assert values(text, E.BIRTHDATE) == [front]
    out = _deid(text)
    assert back not in out and front not in out


def test_tl02_rrn_halves_need_context():
    assert values("전화번호 뒷자리 5678", E.RRN) == []
    assert values("뒷자리 2048117", E.RRN) == []          # no 주민/등록번호 nearby


@pytest.mark.parametrize("text, digits", [
    ("환자 휴대폰 010 3381 7724 / 자택 031)789-4521", "0317894521"),
    ("집전화 02)555-0198 로 연락 바람", "025550198"),
    ("보호자(딸) 권소희 씨 전화. 010)5873-2216.", "01058732216"),
    ("연락처 (010)5873-2216", "01058732216"),
    ("받는분 신유정 / 연락처 010 - 9912 - 3304", "01099123304"),
    ("연락처 010-9912 -3304", "01099123304"),
    ("폰번호 010ㅡ4417ㅡ2290 이에요", "01044172290"),
    ("번호 010~4417~2290 로 문자주세요", "01044172290"),
    ("연락처 010-66213098", "01066213098"),
    ("연락처 0106621-3098", "01066213098"),
])
def test_tl04_phone_separators(text, digits):
    out = _deid(text)
    assert digits not in "".join(c for c in out if c.isdigit())


@pytest.mark.parametrize("text", ["E 1500-2300 근무", "근무 1600-2400", "야간 당직 1800-0600", "금식 1800-0800 유지"])
def test_over_mask_10_shift_time_ranges_are_not_phones(text):
    assert values(text, E.PHONE) == []


def test_over_mask_10_business_numbers_still_found():
    assert values("1588-1234", E.PHONE) == ["1588-1234"]
    assert values("문의 1600-1020", E.PHONE) == ["1600-1020"]


@pytest.mark.parametrize("text, value", [
    ("Name: 강민서   Sex/Age: F/45   Reg No: 33018274", "33018274"),
    ("Patient: Kim, 32F, Reg. No. 20184473", "20184473"),
    ("Registration No. 20184473", "20184473"),
    ("Unit No: 20184473", "20184473"),
    ("[알림] 탁서윤 님(차트 5523190) 내일 MRI", "5523190"),
    ("엄마 환번 1028473 이래 접수할때 말해", "1028473"),
    ("외래번호 4471902", "4471902"),
    ("입원번호 2026-004512", "2026-004512"),
    ("이름 미상, 임시번호 T-2026-1043", "T-2026-1043"),
    ("1) 나상민 (M/52, #20934411) 흉통", "20934411"),
])
def test_tl12_mrn_context_variants(text, value):
    assert values(text, E.MRN) == [value]


@pytest.mark.parametrize("text", ["차트 2024년 정리", "차트 3건 누락", "차트 정리 완료"])
def test_tl12_bare_chart_word_is_not_an_mrn_label_for_counts(text):
    assert values(text, E.MRN) == []


@pytest.mark.parametrize("text, value", [
    ("f/u list - 강태호 85.3.15생 HTN", "85.3.15생"),
    ("출생 85.3.15 남아", "85.3.15"),
    ("대상자 : 변지섭 (남, 1979.11.23生)", "1979.11.23生"),
    ("1979.11.23 생, 남", "1979.11.23 생"),
    ("생년월일 15-Mar-1985", "15-Mar-1985"),
    ("피보험자(주민번호 앞자리 870204)", "870204"),
    ("주민번호 앞 6자리 870204 확인", "870204"),
])
def test_tl13_birthdate_formats(text, value):
    assert values(text, E.BIRTHDATE) == [value]


def test_tl13_space_before_saeng_needs_a_lone_word():
    assert values("2024.03.05 생검 시행", E.BIRTHDATE) == []


@pytest.mark.parametrize("text", [
    "출생 체중 3.2kg, 2024-03-05 퇴원.",
    "출생 후 2024.03.20 BCG 접종.",
    "생년월일 미상, 2024-03-15 내원.",
    "생일 케이크 섭취 후 2024-03-15 혈당 상승.",
])
def test_over_mask_11_birth_keyword_does_not_rewrite_other_dates(text):
    assert values(text, E.BIRTHDATE) == []
    assert "년생" not in _deid(text)


@pytest.mark.parametrize("text, value", [
    ("생년월일: 2024-03-15 (내원 시 확인)", "2024-03-15"),
    ("생년월일 1985.03.02 / 남", "1985.03.02"),
    ("DOB: 1985-03-02 visit", "1985-03-02"),
])
def test_over_mask_11_labelled_birth_dates_stay_birthdates(text, value):
    assert values(text, E.BIRTHDATE) == [value]


@pytest.mark.parametrize("text, entity, value", [
    ("내담자: 공일공에 삼사오육에 칠팔구공이요.", E.PHONE, "공일공에 삼사오육에 칠팔구공"),
    ("전화는 공일공 칠칠팔이 삼삼일오 입니다", E.PHONE, "공일공 칠칠팔이 삼삼일오"),
    ("번호 공일공-칠칠팔이-삼삼일오", E.PHONE, "공일공-칠칠팔이-삼삼일오"),
    ("상담사: 생년월일 말씀해주시겠어요?\n내담자: 팔칠년 오월 이십일일이요.", E.BIRTHDATE, "팔칠년 오월 이십일일"),
])
def test_tl16_spoken_numbers(text, entity, value):
    assert values(text, entity) == [value]


def test_tl16_spoken_birth_date_needs_birth_context():
    assert values("팔칠년 오월 이십일일에 입사", E.BIRTHDATE) == []


@pytest.mark.parametrize("text, value", [
    ("외국인 환자, 여권번호 C1234567 (사본 수납)", "C1234567"),
    ("여권번호: E12345678", "E12345678"),
    ("Passport No. 123456789", "123456789"),
])
def test_tl17_foreign_passport_after_label(text, value):
    assert values(text, E.PASSPORT) == [value]


def test_tl18_account_variants():
    assert values("환불은 카뱅 3333-01-2345678 홍정아", E.ACCOUNT) == ["3333-01-2345678"]
    assert values("계좌 110 482 339201 신한", E.ACCOUNT) == ["110 482 339201"]
    assert values("검사 110 482 339201", E.ACCOUNT) == []


@pytest.mark.parametrize("text, value", [
    ("제 개인메일 soyul.ahn 골뱅이 naver.com 으로 보내주세요", "soyul.ahn 골뱅이 naver.com"),
    ("hana_choi(at)hanmail.net 으로 보내주세요", "hana_choi(at)hanmail.net"),
    ("hana_choi [at] hanmail [dot] net", "hana_choi [at] hanmail [dot] net"),
    ("이메일 hana_choi @ hanmail.net", "hana_choi @ hanmail.net"),
])
def test_tl19_obfuscated_email(text, value):
    assert values(text, E.EMAIL) == [value]


def test_tl19_plain_at_needs_a_mail_domain():
    assert values("meet me at home.com", E.EMAIL) == []


@pytest.mark.parametrize("text, value", [
    ("Phone: (503) 555-0147", "(503) 555-0147"),
    ("His wife Linda (cell 415-555-0172) accompanies him.", "415-555-0172"),
    ("Her daughter can be reached on 07700 900417.", "07700 900417"),
])
def test_tl20_foreign_phones_with_context(text, value):
    assert values(text, E.PHONE) == [value]


def test_tl20_foreign_phones_need_context():
    assert values("Lot 415-555-0172 shipped", E.PHONE) == []


@pytest.mark.parametrize("text, value", [
    ("증권번호: 2019-A-883412", "2019-A-883412"),
    ("사고접수번호 26-0918-77342", "26-0918-77342"),
    ("직장 : (주)한결물산 / 사번 HG-20114", "HG-20114"),
])
def test_tl21_policy_claim_employee_ids(text, value):
    assert values(text, E.OTHER) == [value]
    assert value not in _deid(text)


@pytest.mark.parametrize("text", ["접수번호 15번", "사번 확인 필요", "증권번호 미기재"])
def test_tl21_reference_ids_need_a_real_value(text):
    assert values(text, E.OTHER) == []


@pytest.mark.parametrize("text", [
    "체온 39도 1400 해열제 투여함.", "BT 38도 0200 재측정.", "38도 0600 오한 호소.", "37도 2000 해열.",
    "40도 1530 경련 발생.", "501호 0830 활력징후 측정함.",
])
def test_over_mask_09_temperature_or_room_plus_time_is_not_a_plate(text):
    assert values(text, E.PLATE) == []
    assert _deid(text) == text


def test_over_mask_09_plates_with_context_still_found():
    assert values("차량번호 12도 1400", E.PLATE) == ["12도 1400"]
    assert values("주차 123가 4567", E.PLATE) == ["123가 4567"]


@pytest.mark.parametrize("text", ["약품 바코드 8801234567890 스캔함.", "검체번호 240315-0012345 접수.",
                                  "880123-4******"])
def test_over_mask_12_rrn_with_impossible_birth_date(text):
    assert values(text, E.RRN) == [] and values(text, E.FRN) == []


def test_over_mask_12_real_rrns_unaffected():
    assert values("주민번호 900101-1234568", E.RRN) == ["900101-1234568"]
    assert values("주민번호 000229-3123454", E.RRN) == ["000229-3123454"]


@pytest.mark.parametrize("text", ["의사 면허 2010년 취득, 가정의학과 전문의 2015년 취득.",
                                  "간호사 면허 2018년 취득 후 내과 병동 근무."])
def test_over_mask_13_licence_year_is_not_a_licence_number(text):
    assert values(text, E.MEDICAL_LICENSE) == []
    assert _deid(text) == text


def test_over_mask_13_licence_numbers_still_found():
    assert values("의사 면허번호 제12345호", E.MEDICAL_LICENSE) == ["12345"]
    assert values("Consultant, GMC 7012345", E.MEDICAL_LICENSE) == ["7012345"]
