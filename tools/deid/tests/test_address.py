"""Tests for deid.recognizers.address."""
from __future__ import annotations

import pytest

from deid.config import default_config
from deid.entities import ADDRESS
from deid.recognizers.address import AddressRecognizer, generalize_address

REC = AddressRecognizer()


def find(text, cfg=None):
    return [s.text for s in REC.find(text, cfg or default_config())]


@pytest.mark.parametrize("text", [
    "서울특별시 강남구 테헤란로 123",
    "서울 강남구 테헤란로 123, 4층",
    "경기도 성남시 분당구 정자일로 95 (정자동)",
    "부산광역시 해운대구 우동 1234-5",
    "세종특별자치시 한누리대로 2130",
    "강원특별자치도 춘천시 중앙로 1",
    "제주시 연동 123-4",
    "대구 수성구 범어동 123번지",
    "서울시 송파구 올림픽로 300 롯데월드타워 101동 1203호",
    "OO아파트 101동 1203호",
    "강남구 역삼동 123-45",
    "제주특별자치도 제주시 연동 123-4",
    "서울 강남구 테헤란로4길 12",
    "서울 종로구 종로1가 24",
    "경기도 양평군 양서면 목왕리 123",
    "경기도 양평군 양서면 목왕리",
    "경기 광주시 오포읍 문형리 12",
    "충남 천안시 서북구 불당대로 12 (불당동)",
    "인천 남동구 구월동 1130 래미안 101동 1203호",
    "서울 강남구 역삼동 123-45 OO빌라 201호",
    "부산 수영구 광안해변로 219, 지하 1층",
    "래미안아파트 101동 1203호",
    # any one-word complex name after an anchored address
    "부산 해운대구 센텀중앙로 79 센텀파크 102동 803호",
    "서울 강남구 테헤란로 123, 우성 1동 101호",
    "서울 강남구 테헤란로 123 3층 302호",
])
def test_full_address_detected(text):
    assert find(text) == [text]


@pytest.mark.parametrize("text, expected", [
    ("주소: 서울 강남구 테헤란로 123에 거주", "서울 강남구 테헤란로 123"),
    ("현 주소는 대구 수성구 범어동 123번지이며 독거", "대구 수성구 범어동 123번지"),
    ("서울 강남구 테헤란로 123.", "서울 강남구 테헤란로 123"),
    ("(서울 강남구 테헤란로 123)", "서울 강남구 테헤란로 123"),
    ("우리 OO아파트 101동 1203호에 거주", "OO아파트 101동 1203호"),
    ("강남구 역삼동 123-45, 연락처 010-1234-5678", "강남구 역삼동 123-45"),
])
def test_address_inside_sentence(text, expected):
    spans = REC.find(text, default_config())
    assert [s.text for s in spans] == [expected]
    sp = spans[0]
    assert text[sp.start:sp.end] == sp.text and sp.entity == ADDRESS and sp.source == "address"


def test_scores():
    full = REC.find("서울특별시 강남구 테헤란로 123", default_config())[0].score
    partial = REC.find("강남구 역삼동 123-45", default_config())[0].score
    apt = REC.find("OO아파트 101동 1203호", default_config())[0].score
    assert 0.85 <= apt < partial < full <= 0.95


@pytest.mark.parametrize("text", [
    # bare region mentions are not identifying
    "서울에서 왔다", "강남구 보건소", "부산 출장", "서울 강남구에서 왔다", "서울 강남구 역삼동 거주",
    "용인시 처인구 소재 병원", "중구 보건소 2층", "서울대병원 3층",
    # numbers that are doses, counts or durations
    "당시 치료로 3주간", "강남구 보건소로 3번 방문", "부산 출장으로 3일", "강남구 병원으로 2주 후",
    "강남구 역삼동 3주 전", "서울 강남구 2회 방문", "강남구 역삼동 010-1234-5678", "강남구 역삼동 1,234명",
    "연구 대상자 30명", "혈압 135/85", "흉부 X선 2매",
    # hospital rooms are not home addresses
    "본관 3층 301호 병실", "OO타워 3동 201호",
    # a road name and number with no region anchor
    "번영로 12번길 34",
])
def test_not_address(text):
    assert find(text) == []


def test_two_addresses_in_one_text():
    text = "자택: 서울 강남구 테헤란로 123 / 직장: 부산광역시 해운대구 우동 1234-5"
    assert find(text) == ["서울 강남구 테헤란로 123", "부산광역시 해운대구 우동 1234-5"]


def test_allowlist_suppresses():
    cfg = default_config()
    cfg.allowlist = ["서울특별시 강남구 테헤란로 123"]
    assert find("병원 주소 서울특별시 강남구 테헤란로 123", cfg) == []


@pytest.mark.parametrize("text, expected", [
    ("서울 강남구 테헤란로 123", "서울 강남구"),
    ("서울특별시 강남구 테헤란로 123", "서울특별시 강남구"),
    ("경기도 성남시 분당구 정자일로 95 (정자동)", "경기도 성남시 분당구"),
    ("부산광역시 해운대구 우동 1234-5", "부산광역시 해운대구"),
    ("세종특별자치시 한누리대로 2130", "세종특별자치시"),
    ("강원특별자치도 춘천시 중앙로 1", "강원특별자치도 춘천시"),
    ("제주시 연동 123-4", "제주시"),
    ("대구 수성구 범어동 123번지", "대구 수성구"),
    ("강남구 역삼동 123-45", "강남구"),
    ("서울시 송파구 올림픽로 300 롯데월드타워 101동 1203호", "서울시 송파구"),
    ("  서울   강남구  테헤란로 123 ", "서울 강남구"),
    ("OO아파트 101동 1203호", ""),
    ("테헤란로 123", ""),
    ("", ""),
])
def test_generalize_address(text, expected):
    assert generalize_address(text) == expected


def test_generalize_every_detected_address():
    text = "서울 강남구 테헤란로 123 그리고 제주시 연동 123-4 그리고 OO아파트 101동 1203호"
    got = [generalize_address(s.text) for s in REC.find(text, default_config())]
    assert got == ["서울 강남구", "제주시", ""]
