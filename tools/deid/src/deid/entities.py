"""Entity names, Korean display labels, and default actions."""
from __future__ import annotations

PERSON = "PERSON"                  # 사람 이름 (한글/영문)
RRN = "RRN"                        # 주민등록번호
FRN = "FRN"                        # 외국인등록번호
PHONE = "PHONE"                    # 전화번호 (휴대폰, 유선, 대표번호)
EMAIL = "EMAIL"
ADDRESS = "ADDRESS"                # 도로명/지번 주소, 아파트 동호수
BIRTHDATE = "BIRTHDATE"            # 생년월일 (문맥으로 판별된 날짜)
DATE = "DATE"                      # 일반 날짜 (진료일 등)
CARD = "CARD"                      # 카드번호 (Luhn)
ACCOUNT = "ACCOUNT"                # 계좌번호 (문맥 필요)
PASSPORT = "PASSPORT"              # 여권번호
DRIVER_LICENSE = "DRIVER_LICENSE"  # 운전면허번호
BRN = "BRN"                        # 사업자등록번호
HEALTH_INSURANCE = "HEALTH_INSURANCE"  # 건강보험증번호 (문맥 필요)
MRN = "MRN"                        # 병원 등록번호/차트번호/환자번호 (문맥 필요)
IP = "IP"
PLATE = "PLATE"                    # 차량번호
URL = "URL"
MEDICAL_LICENSE = "MEDICAL_LICENSE"  # 의사/간호사 등 보건의료인 면허번호 (문맥 필요)
OTHER = "OTHER"                    # LLM이 찾았지만 유형을 특정하지 못한 식별자

ALL = (
    PERSON, RRN, FRN, PHONE, EMAIL, ADDRESS, BIRTHDATE, DATE, CARD, ACCOUNT,
    PASSPORT, DRIVER_LICENSE, BRN, HEALTH_INSURANCE, MRN, IP, PLATE, URL,
    MEDICAL_LICENSE, OTHER,
)

LABELS_KO = {
    PERSON: "이름",
    RRN: "주민번호",
    FRN: "외국인번호",
    PHONE: "전화번호",
    EMAIL: "이메일",
    ADDRESS: "주소",
    BIRTHDATE: "생년월일",
    DATE: "날짜",
    CARD: "카드번호",
    ACCOUNT: "계좌번호",
    PASSPORT: "여권번호",
    DRIVER_LICENSE: "면허번호",
    BRN: "사업자번호",
    HEALTH_INSURANCE: "보험번호",
    MRN: "등록번호",
    IP: "IP",
    PLATE: "차량번호",
    URL: "URL",
    MEDICAL_LICENSE: "면허번호",
    OTHER: "기타",
}

# Actions: "pseudo" -> consistent token like [이름1]
#          "mask"   -> partial mask like 김*수, 010-****-5678, 900101-1******
#          "redact" -> fixed label like [주민번호]
#          "generalize" -> coarser value (address -> 시/도 시/군/구, birthdate -> 1985년생)
#          "keep"   -> leave as is (still counted in the report)
DEFAULT_ACTIONS = {
    PERSON: "pseudo",
    RRN: "redact",
    FRN: "redact",
    PHONE: "mask",
    EMAIL: "pseudo",
    ADDRESS: "generalize",
    BIRTHDATE: "generalize",
    DATE: "keep",
    CARD: "redact",
    ACCOUNT: "redact",
    PASSPORT: "redact",
    DRIVER_LICENSE: "redact",
    BRN: "redact",
    HEALTH_INSURANCE: "redact",
    MRN: "pseudo",
    IP: "redact",
    PLATE: "redact",
    URL: "keep",
    MEDICAL_LICENSE: "redact",
    OTHER: "redact",
}


def label(entity: str) -> str:
    return LABELS_KO.get(entity, entity)
