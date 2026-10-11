#!/usr/bin/env python3
"""Generate bench/corpus.jsonl: a labeled synthetic de-identification benchmark.

Every person, number and address is invented. Samples are rendered from templates whose
entity slots are filled from synthetic pools; offsets are recorded while the text is
assembled, so every gold span is exact. About a fifth of the samples are hard negatives
(lab values, vitals, doses, codes and clinical words with no PII at all).

Template syntax: literal text with slots
  {ENTITY}          gold span of that entity (PERSON, PHONE, RRN, ...)
  {ENTITY:variant}  a specific format of that entity
  {ENTITY#k}        same value for every slot with the same key k in one sample
  {~filler}         non-PII filler text (no span)

Gold conventions: a name span excludes honorifics/particles; an MRN/ID span is the value
only (no label); a birth date written "85년생" / "1985.03.02생" includes the 생.

Usage: python bench/make_corpus.py [--out bench/corpus.jsonl] [--seed 20241011]
"""
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

# --------------------------------------------------------------------------- pools

SURNAMES = list("김이박최정강조윤장임한오서신권황안송류홍전고문양손배백허남심노하곽성차주우구민진지엄채원천방공현"
                "변염여추도소석선설마길연표명기반라왕금옥육인맹제탁국어은편용예경봉") + ["남궁", "황보", "제갈", "선우"]
# Weight the common surnames the way they occur in practice.
SURNAME_WEIGHTS = [20 if s in "김이박" else 6 if s in "최정강조윤장임한오서신권황안송류홍" else 1 for s in SURNAMES]
GIVEN2 = """민준 서연 지훈 하은 도윤 서준 지우 예은 수아 현우 지민 은서 태윤 다은 준서 하린 시우 유나 건우 채원
우진 소윤 지호 예린 승민 가은 민서 정우 수빈 동현 유진 재원 서윤 성민 혜진 영호 미경 정숙 순자 영수 경희
상훈 은정 재석 태호 미란 동훈 보람 하늘 수정 진영 나연 다인 준혁 시은 연우 하준 지아 서현 민재 예준 은우
지안 도현 윤서 주원 아린 승현 다현 지성 혜원 종민 영숙 말순 명자 춘자 광수 병철 상철 기순 옥순""".split()
GIVEN1 = list("솔현준훈민빈진결")
EN_FIRST = "John Emily Michael Sarah David Laura James Anna Robert Grace Daniel Olivia Thomas Sophie".split()
EN_LAST = "Smith Johnson Brown Miller Wilson Taylor Anderson Moore Clark Walker Harris Lewis".split()
ROMAN_KO = ["Kim Minjun", "Lee Seoyeon", "Park Jihoon", "Choi Haeun", "Jung Doyun", "Kang Seojun",
            "Cho Yuna", "Yoon Taeyun", "Lim Chaewon", "Han Jiho"]
EMAIL_DOMAINS = ["example.com", "sample.co.kr", "test-mail.kr", "hospital.example.org", "clinic.example.net",
                 "naver-sample.com", "univ.example.ac.kr"]

ADDRESSES = [  # (시/도, 시/군/구, roads, 읍/면/동)
    ("서울특별시", "강남구", ["테헤란로", "도산대로", "봉은사로"], ["역삼동", "삼성동", "대치동"]),
    ("서울", "마포구", ["월드컵북로", "양화로", "독막로"], ["합정동", "서교동", "망원동"]),
    ("서울시", "송파구", ["올림픽로", "송파대로", "백제고분로"], ["잠실동", "가락동", "문정동"]),
    ("서울특별시", "노원구", ["동일로", "노원로"], ["상계동", "중계동"]),
    ("경기도", "성남시 분당구", ["정자일로", "판교역로", "황새울로"], ["정자동", "서현동", "야탑동"]),
    ("경기", "수원시 영통구", ["광교중앙로", "봉영로"], ["이의동", "매탄동"]),
    ("경기도", "고양시 일산동구", ["중앙로", "일산로"], ["장항동", "마두동"]),
    ("부산광역시", "해운대구", ["센텀중앙로", "해운대해변로"], ["우동", "중동", "좌동"]),
    ("부산", "부산진구", ["중앙대로", "서면로"], ["부전동", "전포동"]),
    ("대구", "수성구", ["달구벌대로", "동대구로"], ["범어동", "만촌동"]),
    ("인천광역시", "남동구", ["구월로", "인하로"], ["구월동", "간석동"]),
    ("광주광역시", "북구", ["설죽로", "우치로"], ["용봉동", "운암동"]),
    ("대전", "유성구", ["대학로", "계룡로"], ["궁동", "봉명동"]),
    ("울산광역시", "남구", ["삼산로", "번영로"], ["삼산동", "달동"]),
    ("세종특별자치시", "", ["한누리대로", "도움로"], ["나성동", "어진동"]),
    ("강원특별자치도", "춘천시", ["중앙로", "후석로"], ["효자동", "석사동"]),
    ("충청남도", "천안시 서북구", ["불당대로", "번영로"], ["불당동", "두정동"]),
    ("전북특별자치도", "전주시 완산구", ["홍산로", "효자로"], ["효자동", "서신동"]),
    ("경상남도", "창원시 성산구", ["중앙대로", "원이대로"], ["상남동", "중앙동"]),
    ("제주특별자치도", "제주시", ["연북로", "노형로"], ["연동", "노형동"]),
]
APT_NAMES = ["래미안", "푸르지오", "힐스테이트", "e편한세상", "자이", "아이파크", "한빛아파트", "우성아파트",
             "센트럴파크", "그린빌라", "현대아파트", "롯데캐슬"]
BANKS = ["국민은행", "신한은행", "우리은행", "하나은행", "농협", "카카오뱅크", "기업은행", "우체국"]
PLATE_LETTERS = "가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주하허호"
PLATE_REGIONS = ["서울", "경기", "부산", "인천", "대구"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]

# Non-PII fillers.
SYMPTOMS = ["발열", "기침", "복통", "두통", "어지럼증", "흉통", "요통", "오심", "호흡곤란", "설사", "인후통", "피로감"]
DX = ["고혈압(I10)", "제2형 당뇨병(E11.9)", "급성 기관지염(J20.9)", "요추 염좌(S33.5)", "위식도역류질환(K21.9)",
      "이상지질혈증(E78.5)", "골다공증(M81.0)", "불면증(G47.0)", "갑상선기능저하증(E03.9)", "만성 신장병(N18.3)",
      "천식(J45.9)", "편두통(G43.9)", "역류성 식도염(K21.0)", "대상포진(B02.9)"]
DX_EN = ["community-acquired pneumonia (J18.9)", "atrial fibrillation (I48.91)", "type 2 diabetes (E11.9)",
         "essential hypertension (I10)", "COPD exacerbation (J44.1)", "acute cholecystitis (K81.0)"]
DRUGS = ["amlodipine 5mg qd", "메트포르민 500mg bid", "atorvastatin 20mg qhs", "아세트아미노펜 650mg tid",
         "esomeprazole 40mg qd", "levothyroxine 50mcg qd", "losartan 50mg qd", "cefixime 100mg bid",
         "prednisolone 5mg qd", "타이레놀 500mg prn", "로수바스타틴 10mg qd", "알렌드로네이트 70mg 주 1회"]
DEPTS = ["내과", "소아청소년과", "정형외과", "가정의학과", "피부과", "이비인후과", "신경과", "산부인과"]
RELS = ["배우자", "딸", "아들", "모", "부", "남편", "아내"]
TIMES = ["오전 9:30", "오전 10:00", "오전 11:20", "오후 2:00", "오후 3:40", "오후 4:15"]


def lab(rng: random.Random) -> str:
    return rng.choice([
        f"HbA1c {rng.randint(55, 95) / 10}%", f"LDL {rng.randint(70, 190)} mg/dL",
        f"Hb {rng.randint(95, 165) / 10} g/dL", f"WBC {rng.randint(35, 150) * 100:,}/μL",
        f"CRP {rng.randint(1, 120) / 10} mg/dL", f"Cr {rng.randint(6, 25) / 10} mg/dL",
        f"AST/ALT {rng.randint(15, 90)}/{rng.randint(10, 120)} IU/L", f"TSH {rng.randint(3, 80) / 10} mIU/L",
        f"공복혈당 {rng.randint(80, 250)} mg/dL", f"eGFR {rng.randint(25, 110)}",
    ])


def vitals(rng: random.Random) -> str:
    return (f"BP {rng.randint(95, 170)}/{rng.randint(55, 105)}, HR {rng.randint(52, 120)}, "
            f"BT {rng.randint(358, 392) / 10}, SpO2 {rng.randint(88, 100)}%")


def sexage(rng: random.Random) -> str:
    age = rng.randint(1, 92)
    return rng.choice([f"M/{age}", f"F/{age}", f"남/{age}", f"여/{age}", f"여, {age}세", f"남, {age}세"])


FILLERS = {
    "sym": lambda r: r.choice(SYMPTOMS), "dx": lambda r: r.choice(DX), "dx_en": lambda r: r.choice(DX_EN),
    "drug": lambda r: r.choice(DRUGS), "dept": lambda r: r.choice(DEPTS), "rel": lambda r: r.choice(RELS),
    "time": lambda r: r.choice(TIMES), "bank": lambda r: r.choice(BANKS), "lab": lab, "vitals": vitals,
    "sexage": sexage, "days": lambda r: str(r.choice([3, 5, 7, 14, 28, 30])),
    "dose": lambda r: f"{r.choice([0.5, 1, 2])}정", "age": lambda r: str(r.randint(2, 90)),
}

# --------------------------------------------------------------------------- value generators


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def gen_person(rng: random.Random, variant: str) -> str:
    if variant == "en":
        return f"{rng.choice(EN_FIRST)} {rng.choice(EN_LAST)}"
    if variant == "rom":
        return rng.choice(ROMAN_KO)
    surname = rng.choices(SURNAMES, SURNAME_WEIGHTS)[0]
    if variant == "short":
        return rng.choice("김이박최정강조윤장한") + rng.choice(GIVEN1)
    return surname + rng.choice(GIVEN2)


def gen_phone(rng: random.Random, variant: str) -> str:
    mid, last = _digits(rng, 4), _digits(rng, 4)
    pre = rng.choice(["010", "010", "010", "011", "016"])
    if variant == "intl":
        return f"+82 10-{mid}-{last}"
    if variant == "land":
        area = rng.choice(["02", "031", "032", "051", "053", "062", "064"])
        mid = _digits(rng, 3 if area != "02" and rng.random() < 0.5 else 4)
        return f"{area}-{mid}-{last}"
    if variant == "biz":
        return f"{rng.choice(['1588', '1577', '1661', '1899'])}-{last}"
    sep = rng.choice(["-", "-", "-", ".", " ", ""])
    return f"{pre}{sep}{mid}{sep}{last}"


def _rrn_check(d12: str) -> str:
    s = sum(int(c) * w for c, w in zip(d12, [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]))
    return str((11 - s % 11) % 10)


def _frn_check(d12: str) -> str:
    s = sum(int(c) * w for c, w in zip(d12, [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]))
    return str(((11 - s % 11) % 10 + 2) % 10)


def _birth(rng: random.Random) -> tuple[int, int, int]:
    return rng.randint(1935, 2019), rng.randint(1, 12), rng.randint(1, 28)


def gen_rrn(rng: random.Random, variant: str, foreign: bool = False) -> str:
    y, m, d = _birth(rng)
    g = (5 if y < 2000 else 7) if foreign else (1 if y < 2000 else 3)
    g += rng.randint(0, 1)
    front = f"{y % 100:02d}{m:02d}{d:02d}"
    body = front + str(g) + _digits(rng, 5)
    tail = (_frn_check if foreign else _rrn_check)(body)
    if variant == "nocheck":  # issued after 2020-10: random last digit
        tail = str((int(tail) + 1) % 10)
    full = body + tail
    if variant == "masked":
        return f"{front}-{g}******"
    if variant == "plain":
        return full
    return f"{front}-{full[6:]}"


def gen_address(rng: random.Random, variant: str) -> str:
    sido, sgg, roads, emds = rng.choice(ADDRESSES)
    head = f"{sido} {sgg}".strip()
    if variant == "lot":
        lot = f"{rng.randint(1, 1500)}" + (f"-{rng.randint(1, 60)}" if rng.random() < 0.6 else "")
        return f"{head} {rng.choice(emds)} {lot}"
    if variant == "partial":
        while not sgg:
            sido, sgg, roads, emds = rng.choice(ADDRESSES)
        return f"{sgg.split()[-1]} {rng.choice(emds)} {rng.randint(1, 999)}-{rng.randint(1, 40)}"
    road = rng.choice(roads)
    if rng.random() < 0.25:
        road += f"{rng.randint(1, 40)}번길"
    out = f"{head} {road} {rng.randint(1, 450)}"
    tail = rng.random()
    if variant == "apt" or tail < 0.3:
        out += f", {rng.choice(APT_NAMES)} {rng.randint(101, 125)}동 {rng.randint(1, 25)}0{rng.randint(1, 4)}호"
    elif tail < 0.45:
        out += f", {rng.randint(2, 12)}층"
    elif tail < 0.55:
        out += f" ({rng.choice(emds)})"
    return out


def gen_email(rng: random.Random, variant: str) -> str:
    local = rng.choice(["minjun", "seoyeon", "jhpark", "hayoon.kim", "doc.lee", "nurse01", "yj_choi",
                        "jsmith", "e.brown", "patient2024", "hk.jung", "sw.han"])
    if rng.random() < 0.5:
        local += str(rng.randint(1, 99))
    return f"{local}@{rng.choice(EMAIL_DOMAINS)}"


def gen_mrn(rng: random.Random, variant: str) -> str:
    if variant == "alnum":
        return rng.choice("ABPH") + _digits(rng, 7)
    return _digits(rng, rng.choice([6, 7, 8, 8, 10]))


def _luhn_complete(body: str) -> str:
    total = 0
    for i, c in enumerate(reversed(body)):
        n = int(c)
        if i % 2 == 0:
            n *= 2
            n -= 9 if n > 9 else 0
        total += n
    return body + str((10 - total % 10) % 10)


def gen_card(rng: random.Random, variant: str) -> str:
    prefix = rng.choice(["4", "51", "53", "9410", "35"])
    ds = _luhn_complete(prefix + _digits(rng, 15 - len(prefix)))
    sep = rng.choice(["-", "-", " "])
    return sep.join(ds[i:i + 4] for i in range(0, 16, 4))


def gen_account(rng: random.Random, variant: str) -> str:
    return rng.choice([
        f"{_digits(rng, 3)}-{_digits(rng, 3)}-{_digits(rng, 6)}",
        f"1002-{_digits(rng, 3)}-{_digits(rng, 6)}",
        f"3333-{_digits(rng, 2)}-{_digits(rng, 7)}",
        f"{_digits(rng, 3)}-{_digits(rng, 6)}-{_digits(rng, 2)}-{_digits(rng, 3)}",
        "1" + _digits(rng, 11),
    ])


def gen_brn(rng: random.Random, variant: str) -> str:
    d9 = str(rng.randint(101, 899)) + _digits(rng, 6)
    s = sum(int(c) * w for c, w in zip(d9, [1, 3, 7, 1, 3, 7, 1, 3, 5])) + int(d9[8]) * 5 // 10
    d10 = d9 + str((10 - s % 10) % 10)
    return f"{d10[:3]}-{d10[3:5]}-{d10[5:]}"


def gen_passport(rng: random.Random, variant: str) -> str:
    if variant == "new":  # 2021+ format: M123A4567
        return f"{rng.choice('MS')}{_digits(rng, 3)}{rng.choice('ABCD')}{_digits(rng, 4)}"
    return rng.choice("MS") + _digits(rng, 8)


def gen_ip(rng: random.Random, variant: str) -> str:
    return f"{rng.choice([10, 172, 192, 203])}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(2, 254)}"


def gen_dl(rng: random.Random, variant: str) -> str:
    if variant == "region":
        return f"{rng.choice(['서울', '경기', '부산', '대구'])} {_digits(rng, 2)}-{_digits(rng, 6)}-{_digits(rng, 2)}"
    return f"{rng.randint(11, 26)}-{_digits(rng, 2)}-{_digits(rng, 6)}-{_digits(rng, 2)}"


def gen_plate(rng: random.Random, variant: str) -> str:
    letter = rng.choice(PLATE_LETTERS)
    num = f"{rng.randint(10, 399)}{letter}{rng.choice(['', ' '])}{rng.randint(1000, 9999)}"
    return f"{rng.choice(PLATE_REGIONS)}{num}" if variant == "region" else num


def gen_birthdate(rng: random.Random, variant: str) -> str:
    y, m, d = _birth(rng)
    if variant == "saeng":
        return rng.choice([f"{y % 100:02d}년생", f"{y}.{m:02d}.{d:02d}생", f"{y}년생"])
    if variant == "en":
        return rng.choice([f"{m:02d}/{d:02d}/{y}", f"{y}-{m:02d}-{d:02d}", f"{EN_MONTHS[m - 1]} {d}, {y}"])
    return rng.choice([f"{y}.{m:02d}.{d:02d}", f"{y}-{m:02d}-{d:02d}", f"{y}년 {m}월 {d}일",
                       f"{y % 100:02d}{m:02d}{d:02d}"])


def gen_date(rng: random.Random, variant: str) -> str:
    y, m, d = rng.randint(2019, 2025), rng.randint(1, 12), rng.randint(1, 28)
    if variant == "en":
        return rng.choice([f"{EN_MONTHS[m - 1]} {d}, {y}", f"{d} {EN_MONTHS[m - 1][:3]} {y}", f"{y}-{m:02d}-{d:02d}"])
    return rng.choice([f"{y}-{m:02d}-{d:02d}", f"{y}.{m:02d}.{d:02d}", f"{y}년 {m}월 {d}일", f"{y}. {m}. {d}."])


GENERATORS = {
    "PERSON": gen_person, "PHONE": gen_phone, "RRN": gen_rrn,
    "FRN": lambda r, v: gen_rrn(r, v, foreign=True), "ADDRESS": gen_address, "EMAIL": gen_email,
    "MRN": gen_mrn, "CARD": gen_card, "ACCOUNT": gen_account, "BRN": gen_brn, "DRIVER_LICENSE": gen_dl,
    "PLATE": gen_plate, "BIRTHDATE": gen_birthdate, "DATE": gen_date,
    "PASSPORT": gen_passport, "IP": gen_ip,
    "HEALTH_INSURANCE": lambda r, v: _digits(r, r.choice([10, 11])),
}

# --------------------------------------------------------------------------- templates

POSITIVE = [
    # clinical notes
    "환자명: {PERSON} ({~sexage})   등록번호: {MRN}\n주소: {ADDRESS}",
    "{PERSON} 님은 {DATE} 내원하여 {~sym} 호소함. 연락처 {PHONE}.",
    "보호자 {PERSON}({~rel}) {PHONE}에게 검사 결과 설명함.",
    "성명 {PERSON}   주민등록번호 {RRN}   생년월일 {BIRTHDATE}",
    "S) {PERSON} 환자 {~sym} 지속. O) {~vitals} A) {~dx} P) {~drug}",
    "주치의 {PERSON} 선생님께 {~lab} 결과 보고함.",
    "{PERSON#a} 씨 {DATE} 수술 예정. {PERSON#a} 씨 보호자 {PERSON} 님께서 동의서 작성함.",
    "입원 {DATE}, 퇴원 {DATE}. 담당의: {PERSON}, {~dx} 진단 하 {~drug} 처방.",
    "환자 {PERSON}({~sexage}) 차트번호 {MRN}, {~lab}, {~lab}",
    "{PERSON} 어르신 {~vitals} 측정함. 보호자 연락처 {PHONE}",
    "외래 접수: {PERSON} / {PHONE} / {ADDRESS}",
    "주민번호 {RRN:masked} 확인 후 {PERSON} 님 본인 인증 완료.",
    "환자번호 {MRN:alnum} {PERSON} 환자 {DATE} {~dept} 협진 의뢰.",
    "{PERSON}({~sexage}) 님 {~dx}로 {~drug} {~days}일분 처방, 다음 내원 {DATE}.",
    "간호기록: {PERSON} 환자 {~vitals}, 보호자 {PERSON}님 상주.",
    # messages and chat
    "[Web발신] {PERSON} 님, {DATE} {~time} {~dept} 진료 예약되었습니다. 문의 {PHONE:biz}",
    "엄마 나 오늘 {PERSON} 선생님한테 진료 받았어 ㅎㅎ 병원 번호 {PHONE:land} 저장해둬",
    "[{PERSON#a}] [{~time}] 원장님 저 {PERSON#a}입니다. 내일 예약 변경 가능할까요? {PHONE}",
    "{DATE} {~time}, {PERSON} : 선생님 오늘 진료 몇 시까지 하세요?",
    "{PERSON} 보호자님, 환불금은 {~bank} {ACCOUNT}로 입금했습니다.",
    "주차 등록 차량번호 {PLATE} 입니다. {PERSON} 드림",
    "{PERSON}님 {~lab} 나왔고 약은 그대로 드시면 됩니다. 궁금하시면 {PHONE}로 연락주세요~",
    "내일 {~time}에 {PERSON#a} 씨 모시고 갈게요. {PERSON#a} 씨 {~sym} 때문에요. 제 번호는 {PHONE}",
    # forms
    "신청인: {PERSON}   운전면허번호: {DRIVER_LICENSE}   연락처: {PHONE}",
    "운전면허 {DRIVER_LICENSE:region} / 성명: {PERSON}",
    "건강보험증번호 {HEALTH_INSURANCE} / 피보험자 {PERSON}",
    "외국인등록번호 {FRN}   여권번호 {PASSPORT}   Name: {PERSON:en}",
    "여권번호: {PASSPORT:new}  국적: 베트남  성명: {PERSON:rom}",
    "사업자등록번호 {BRN}   대표자 {PERSON}   전화 {PHONE:land}",
    "결제 카드 {CARD} (일시불) / 고객명 {PERSON}",
    "이메일: {EMAIL}   휴대폰: {PHONE}",
    "자택 주소 {ADDRESS:lot} / 직장 전화 {PHONE:land}",
    "{PERSON} ({BIRTHDATE:saeng}) {ADDRESS}",
    "주소: {ADDRESS:apt}   보호자: {PERSON} ({~rel})",
    "계좌번호 {ACCOUNT} 예금주 {PERSON}",
    "현 주소는 {ADDRESS:partial}이며 독거 중. 비상연락 {PHONE}",
    "주민등록번호: {RRN:plain}  성명: {PERSON}  {~sexage}",
    "생년월일 {BIRTHDATE} / 연락처 {PHONE} / 이메일 {EMAIL}",
    "환자 성명 {PERSON}   주민등록번호 {RRN:nocheck}",
    # English and mixed
    "Patient: {PERSON:en}, DOB {BIRTHDATE:en}, MRN {MRN}",
    "Dear Dr. {PERSON:en}, I am referring my patient for {~dx_en}.",
    "Mr. {PERSON:en} was seen on {DATE:en} for {~dx_en}. Contact: {PHONE:intl} / {EMAIL}",
    "Referred by Dr. {PERSON:en}. Patient ID: {MRN}.",
    "Name: {PERSON:rom}   Phone: {PHONE}   Address: {ADDRESS}",
    "Please email {EMAIL} or call {PHONE:intl} with the results.",
    "Server access log: login from IP {IP} by {EMAIL}",
    "Attending: Dr. {PERSON}, Hospital No: {MRN}, Adm {DATE}, Dx {~dx}",
    "{PERSON} 환자 (MRN {MRN}) {~vitals}, {~lab}",
    "Discharge on {DATE:en}. Follow up with Ms. {PERSON:en} at {PHONE:land}.",
    # tables and lists
    "이름\t{PERSON}\t연락처\t{PHONE}",
    "| 성명 | {PERSON} | 생년월일 | {BIRTHDATE} |",
    "1. {PERSON} ({~sexage}) - {~dx}\n2. {PERSON} ({~sexage}) - {~dx}",
    # names with weak cues
    "{PERSON#a} 학생 결석 확인서: {PERSON#a} 학생은 {DATE}부터 장염으로 결석함.",
    "{PERSON:short} 씨 {~sym}로 {~dept} 내원.",
    "선생님 {PERSON}{~iga} 어제부터 열이 나요. {~lab}",
]

NEGATIVE = [
    "Hb {~hb} g/dL, WBC {~wbc}/μL, PLT {~plt}K, CRP {~crp} mg/dL로 염증 수치 상승.",
    "BP {~bp} mmHg, HR {~hr}회/분, BT {~bt}℃, SpO2 {~spo2}%.",
    "상기 환자는 {~dx}, {~dx}로 본원 f/u 중이며 이상 소견 없음.",
    "원장님께서 정상 범위라고 설명하심. 다음 검사는 {~n3}개월 후 예정.",
    "1일 {~kcal} kcal 식이, 걷기 30분 주 5회 권장.",
    "Na {~na} mmol/L (135-145), K {~k} (3.5-5.1), Cl {~cl} (98-107).",
    "{~drug}, {~drug} 유지. 부작용 없음.",
    "KCD: {~dx} / 처방일수 {~days}일, 1일 3회 식후 30분.",
    "흉부 X선 2매, 복부 초음파 1회, 혈액검사 {~n12}항목 시행.",
    "체중 {~wt}kg, 신장 {~ht}cm, BMI {~bmi}, 허리둘레 {~waist}cm.",
    "고령 환자로 낙상 고위험군, 보호자 상주 필요함.",
    "선생님께서 처방하신 약은 하루 2번 식후에 드시면 됩니다.",
    "임신 {~ga}주, 추정 체중 {~efw}g, 양수 정상, 태동 양호.",
    "전립선비대증(N40) PSA {~psa} ng/mL, 잔뇨 {~pvr}cc.",
    "ECG: NSR, HR {~hr}, QTc {~qtc}ms, PR {~pr}ms.",
    "PT {~pt} sec, INR {~inr}, aPTT {~aptt} sec.",
    "eGFR {~egfr} mL/min/1.73m2, BUN/Cr {~bun}/{~cr}",
    "시력 {~va}/{~va}, 안압 {~iop}/{~iop} mmHg, 안저 정상.",
    "Patient presented with {~sym_en}. Troponin {~trop} ng/mL, follow up in {~n3} weeks with CBC and CMP.",
    "No acute distress. Lungs clear. Plan: continue {~drug} and recheck {~lab} in {~n3} months.",
    "상담 시 우울감 호소, PHQ-9 {~phq}점, 수면 {~n3}시간.",
    "본관 {~n12}층 {~room}호 병실로 전동, 산소 2L/min 비강캐뉼라.",
    "소아 체중 {~kid}kg → 아세트아미노펜 {~mgkg}mg/kg q6h, 1회 최대 {~cap}mg.",
    "투석 주 3회, 건체중 {~wt}kg, Kt/V {~ktv}, Hb {~hb}.",
    "이상 소견 없음. 정상 소견으로 추적 관찰 예정이며 이후 증상 악화 시 재내원.",
    "{~lab}, {~lab}, {~lab}. 신기능 저하 없음.",
    "수술 후 {~n3}일째 배액량 {~drain}cc, 통증 NRS {~nrs}점.",
    "인슐린 글라진 {~ins}단위 취침 전, 리스프로 {~ins2}-{~ins3}단위 식전.",
    "MMSE {~mmse}/30, CDR 0.5, 기억력 저하 호소하나 일상생활 가능.",
    "Rt. knee ROM 0-{~rom}°, 부종 경미, 2주 후 재평가.",
]
NEG_FILLERS = {
    "hb": lambda r: f"{r.randint(70, 170) / 10}", "wbc": lambda r: f"{r.randint(30, 200) * 100:,}",
    "plt": lambda r: str(r.randint(90, 450)), "crp": lambda r: f"{r.randint(1, 250) / 10}",
    "bp": lambda r: f"{r.randint(90, 180)}/{r.randint(50, 110)}", "hr": lambda r: str(r.randint(48, 130)),
    "bt": lambda r: f"{r.randint(355, 400) / 10}", "spo2": lambda r: str(r.randint(85, 100)),
    "n3": lambda r: str(r.randint(1, 6)), "n12": lambda r: str(r.randint(2, 12)),
    "kcal": lambda r: r.choice(["1500-1800", "1600-1800", "1800-2000", "1200-1500"]),
    "na": lambda r: str(r.randint(128, 148)), "k": lambda r: f"{r.randint(30, 60) / 10}",
    "cl": lambda r: str(r.randint(92, 110)), "wt": lambda r: f"{r.randint(400, 1000) / 10}",
    "ht": lambda r: str(r.randint(145, 190)), "bmi": lambda r: f"{r.randint(170, 350) / 10}",
    "waist": lambda r: str(r.randint(65, 110)), "ga": lambda r: str(r.randint(8, 40)),
    "efw": lambda r: str(r.randint(800, 3800)), "psa": lambda r: f"{r.randint(5, 120) / 10}",
    "pvr": lambda r: str(r.randint(10, 300)), "qtc": lambda r: str(r.randint(380, 520)),
    "pr": lambda r: str(r.randint(120, 220)), "pt": lambda r: f"{r.randint(100, 180) / 10}",
    "inr": lambda r: f"{r.randint(90, 350) / 100}", "aptt": lambda r: f"{r.randint(250, 450) / 10}",
    "egfr": lambda r: str(r.randint(15, 120)), "bun": lambda r: str(r.randint(8, 60)),
    "cr": lambda r: f"{r.randint(5, 40) / 10}", "va": lambda r: r.choice(["0.3", "0.5", "0.8", "1.0", "1.2"]),
    "iop": lambda r: str(r.randint(10, 24)), "sym_en": lambda r: r.choice(["chest pain", "dyspnea", "fever"]),
    "trop": lambda r: f"{r.randint(1, 90) / 100}", "phq": lambda r: str(r.randint(0, 27)),
    "room": lambda r: f"{r.randint(1, 12)}{r.randint(0, 3)}{r.randint(1, 9)}",
    "kid": lambda r: f"{r.randint(30, 400) / 10}", "mgkg": lambda r: r.choice(["10", "12.5", "15"]),
    "cap": lambda r: r.choice(["500", "650", "1000"]), "ktv": lambda r: f"{r.randint(10, 18) / 10}",
    "drain": lambda r: str(r.randint(10, 300)), "nrs": lambda r: str(r.randint(0, 10)),
    "ins": lambda r: str(r.randint(6, 40)), "ins2": lambda r: str(r.randint(2, 6)),
    "ins3": lambda r: str(r.randint(7, 12)), "mmse": lambda r: str(r.randint(15, 30)),
    "rom": lambda r: str(r.randint(90, 140)),
}

def _final_consonant(word: str) -> bool:
    return "가" <= word[-1] <= "힣" and (ord(word[-1]) - 0xAC00) % 28 != 0


# Fillers that depend on the value rendered just before them (Korean particle allomorphs).
AFTER_PREVIOUS = {"iga": lambda prev: "이가" if _final_consonant(prev) else "가"}

_SLOT = re.compile(r"\{(~?)([A-Za-z_]+)(?::([a-z_]+))?(?:#(\w+))?\}")


def render(template: str, rng: random.Random) -> dict:
    """Fill one template; returns {"text", "spans"} with exact offsets."""
    parts: list[str] = []
    spans: list[dict] = []
    memo: dict[str, str] = {}
    pos = last = 0
    for m in _SLOT.finditer(template):
        lit = template[last:m.start()]
        parts.append(lit)
        pos += len(lit)
        last = m.end()
        filler, name, variant, key = m.group(1), m.group(2), m.group(3) or "", m.group(4)
        if filler and name in AFTER_PREVIOUS:
            value = AFTER_PREVIOUS[name](parts[-2] if len(parts) >= 2 else "")
        elif filler:
            value = (FILLERS.get(name) or NEG_FILLERS[name])(rng)
        else:
            value = memo.get(key) if key else None
            if value is None:
                value = GENERATORS[name](rng, variant)
                if key:
                    memo[key] = value
            # Gold spans exclude trailing punctuation, e.g. the last dot of "2024. 3. 15.".
            end = pos + len(value.rstrip("."))
            spans.append({"start": pos, "end": end, "entity": name})
        parts.append(value)
        pos += len(value)
    parts.append(template[last:])
    text = "".join(parts)
    for sp in spans:
        assert sp["end"] <= len(text)
    return {"text": text, "spans": spans}


def build(seed: int, per_positive: int, neg_ratio: float) -> list[dict]:
    """per_positive samples per positive template, plus negatives making up `neg_ratio` of all."""
    rng = random.Random(seed)
    samples = []
    for i, tpl in enumerate(POSITIVE):
        for k in range(per_positive):
            samples.append({"id": f"p{i:02d}-{k}", **render(tpl, rng)})
    n_neg = round(len(samples) * neg_ratio / (1 - neg_ratio))
    for j in range(n_neg):  # cycle through the negative templates
        i = j % len(NEGATIVE)
        samples.append({"id": f"n{i:02d}-{j // len(NEGATIVE)}", **render(NEGATIVE[i], rng)})
    rng.shuffle(samples)
    return samples


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(Path(__file__).with_name("corpus.jsonl")))
    ap.add_argument("--seed", type=int, default=20241011)
    ap.add_argument("--per-positive", type=int, default=3)
    ap.add_argument("--neg-ratio", type=float, default=0.2, help="share of samples without PII")
    args = ap.parse_args(argv)
    samples = build(args.seed, args.per_positive, args.neg_ratio)
    with open(args.out, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    neg = sum(1 for s in samples if not s["spans"])
    print(f"wrote {len(samples)} samples ({neg} without PII) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
