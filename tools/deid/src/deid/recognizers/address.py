"""Korean postal addresses (road-name and lot-number) and apartment unit numbers."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..entities import ADDRESS
from ..types import Span
from .base import make_span

if TYPE_CHECKING:
    from ..config import Config

SIDO = (
    "서울특별시", "서울시", "서울", "부산광역시", "부산시", "부산", "대구광역시", "대구시", "대구",
    "인천광역시", "인천시", "인천", "광주광역시", "광주시", "광주", "대전광역시", "대전시", "대전",
    "울산광역시", "울산시", "울산", "세종특별자치시", "세종시", "세종", "경기도", "경기",
    "강원특별자치도", "강원도", "강원", "충청북도", "충북", "충청남도", "충남",
    "전북특별자치도", "전라북도", "전북", "전라남도", "전남", "경상북도", "경북", "경상남도", "경남",
    "제주특별자치도", "제주도", "제주",
)

# Real 시/군/구 names; an address may start with one of these without a 시/도.
SIGUNGU = frozenset("""
종로구 중구 용산구 성동구 광진구 동대문구 중랑구 성북구 강북구 도봉구 노원구 은평구 서대문구 마포구
양천구 강서구 구로구 금천구 영등포구 동작구 관악구 서초구 강남구 송파구 강동구
서구 동구 영도구 부산진구 동래구 남구 북구 해운대구 사하구 금정구 연제구 수영구 사상구 기장군
수성구 달서구 달성군 군위군 미추홀구 연수구 남동구 부평구 계양구 강화군 옹진군 광산구 유성구 대덕구 울주군
수원시 성남시 고양시 용인시 부천시 안산시 안양시 남양주시 화성시 평택시 의정부시 시흥시 파주시 김포시
광명시 광주시 군포시 하남시 오산시 이천시 안성시 의왕시 양주시 구리시 포천시 동두천시 과천시 여주시
양평군 가평군 연천군 장안구 권선구 팔달구 영통구 수정구 중원구 분당구 덕양구 일산동구 일산서구
처인구 기흥구 수지구 원미구 소사구 오정구 상록구 단원구 만안구 동안구 만세구 효행구 병점구 동탄구
춘천시 원주시 강릉시 동해시 태백시 속초시 삼척시 홍천군 횡성군 영월군 평창군 정선군 철원군 화천군
양구군 인제군 고성군 양양군
청주시 상당구 서원구 흥덕구 청원구 충주시 제천시 보은군 옥천군 영동군 증평군 진천군 괴산군 음성군 단양군
천안시 동남구 서북구 공주시 보령시 아산시 서산시 논산시 계룡시 당진시 금산군 부여군 서천군 청양군
홍성군 예산군 태안군
전주시 완산구 덕진구 군산시 익산시 정읍시 남원시 김제시 완주군 진안군 무주군 장수군 임실군 순창군
고창군 부안군
목포시 여수시 순천시 나주시 광양시 담양군 곡성군 구례군 고흥군 보성군 화순군 장흥군 강진군 해남군
영암군 무안군 함평군 영광군 장성군 완도군 진도군 신안군
포항시 경주시 김천시 안동시 구미시 영주시 영천시 상주시 문경시 경산시 의성군 청송군 영양군 영덕군
청도군 고령군 성주군 칠곡군 예천군 봉화군 울진군 울릉군
창원시 의창구 성산구 마산합포구 마산회원구 진해구 진주시 통영시 사천시 김해시 밀양시 거제시 양산시
의령군 함안군 창녕군 남해군 하동군 산청군 함양군 거창군 합천군
제주시 서귀포시
""".split())


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=lambda w: (-len(w), w)))


_SP = r"[^\S\n]+"
_SIDO_ALT = _alt(SIDO)
_SGG_KNOWN = f"(?:{_alt(SIGUNGU)})(?![가-힣])"
_SGG_ANY = rf"(?:{_SGG_KNOWN}|[가-힣]{{1,5}}(?:시|군|구)(?![가-힣]))"
_EMD = r"(?:[가-힣]{1,5}\d{0,2}(?:동|읍|면)|[가-힣]{1,4}\d{1,2}가)(?![가-힣])"
_RI = r"[가-힣]{1,5}리(?![가-힣])"
_ROAD = r"[가-힣][가-힣0-9]{0,10}(?:로|길)(?:[ ]?\d{1,4}번?[가-하]?길)?"
_NUM = r"(?:산[ ]?)?\d{1,5}(?:-\d{1,5})?(?:[ ]?번지)?"

_CORE_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])"
    rf"(?:(?P<sido>{_SIDO_ALT}){_SP}(?:(?P<sgg>{_SGG_ANY})(?:{_SP}{_SGG_ANY})?{_SP})?"
    rf"|(?P<sgg_only>{_SGG_KNOWN})(?:{_SP}{_SGG_ANY})?{_SP})"
    rf"(?:{_EMD}{_SP}(?:{_RI}{_SP})?{_NUM}"
    rf"|(?:{_EMD}{_SP})?(?P<road>{_ROAD})[ ]?{_NUM}"
    rf"|[가-힣]{{1,5}}(?:읍|면){_SP}{_RI})"
)

_MASK = "○◯●*＊ＯO"
_RESIDENTIAL = (
    "아파트", "APT", "apt", "빌라", "맨션", "오피스텔", "하이츠", "빌리지", "타운", "주공", "단지",
    "자이", "래미안", "푸르지오", "아이파크", "힐스테이트", "캐슬", "더샵", "e편한세상", "스위첸",
    "데시앙", "센트레빌", "꿈에그린", "위브", "레지던스",
)
_BUILDING = _RESIDENTIAL + ("타워", "빌딩", "주택", "센터", "프라자", "플라자", "상가")
_BLDG_NAME = rf"[가-힣A-Za-z0-9{re.escape(_MASK)}]{{0,15}}?[ ]?(?:{_alt(_BUILDING)})"
_APT_NAME = rf"[가-힣A-Za-z0-9{re.escape(_MASK)}]{{0,15}}?[ ]?(?:{_alt(_RESIDENTIAL)})"

_TAILS = (
    re.compile(r"[ ]?[(（][ ]*[가-힣0-9]{1,10}(?:동|가|리|읍|면)(?:[ ]*,[ ]*[가-힣A-Za-z0-9 ]{1,20})?[ ]*[)）]"),
    re.compile(rf",?[^\S\n]*(?:{_BLDG_NAME}[ ]?)?(?:\d{{1,4}}동[ ]?)?\d{{1,5}}호"),
    re.compile(rf",?[^\S\n]*(?:{_BLDG_NAME}[ ]?)?\d{{1,4}}동(?![가-힣])"),
    re.compile(r",?[^\S\n]*(?:지하[ ]?|B)?\d{1,3}층"),
    re.compile(rf",?[^\S\n]*{_BLDG_NAME}(?![가-힣A-Za-z0-9])"),
    # Any one-word complex name right after an anchored address ("센텀파크 102동 803호").
    re.compile(rf",?[^\S\n]*[가-힣A-Za-z0-9{re.escape(_MASK)}]{{1,15}}[ ]?(?:\d{{1,4}}동[ ]?)?\d{{1,5}}호"),
)
_APT_RE = re.compile(rf"(?<![가-힣A-Za-z0-9]){_APT_NAME}[ ]?\d{{1,4}}동[ ]?\d{{1,5}}호")

# What may directly follow a building/lot number without breaking the address.
_NUM_FOLLOWERS = frozenset({
    "에", "에서", "의", "로", "으로", "이", "가", "은", "는", "을", "를", "와", "과", "도", "까지",
    "부터", "에서는", "에는", "으로는", "이며", "이고", "이다", "입니다", "이에요", "예요", "에서도", "에도",
})


def _number_end_ok(text: str, end: int) -> bool:
    """Reject "치료로 3주" style matches: the number must not run into a unit or more digits."""
    if end >= len(text):
        return True
    c = text[end]
    if c.isdigit() or (c.isascii() and c.isalpha()) or c in "%°":
        return False
    if c in ".,-" and end + 1 < len(text) and text[end + 1].isdigit():
        return False  # 3.5, 1,234, 010-1234-5678
    if "가" <= c <= "힣":
        run = re.match(r"[가-힣]+", text[end:]).group()
        return run in _NUM_FOLLOWERS or run[0] in "호층동"
    return True


class AddressRecognizer:
    """Road-name / lot-number addresses with a 시/도 or 시/군/구 anchor, and apartment units."""

    name = "address"
    entities = (ADDRESS,)

    def find(self, text: str, cfg: "Config") -> list[Span]:
        out: list[Span] = []
        for m in _CORE_RE.finditer(text):
            if m.group("road") and m.group("road").endswith("으로"):
                continue
            if not _number_end_ok(text, m.end()):
                continue
            end = self._extend(text, m.end())
            score = 0.95 if m.group("sido") and m.group("sgg") else 0.9
            out.append(make_span(text, m.start(), end, ADDRESS, score, self.name))
        for m in _APT_RE.finditer(text):
            if any(sp.start <= m.start() < sp.end or m.start() <= sp.start < m.end() for sp in out):
                continue
            out.append(make_span(text, m.start(), m.end(), ADDRESS, 0.85, self.name))
        allow = set(cfg.allowlist)
        return sorted((sp for sp in out if sp.text not in allow), key=lambda sp: sp.start)

    @staticmethod
    def _extend(text: str, end: int) -> int:
        """Absorb floor / building / 동·호 / (법정동) tails after the core address."""
        for _ in range(6):
            for rx in _TAILS:
                t = rx.match(text, end)
                if t and t.end() > end:
                    end = t.end()
                    break
            else:
                break
        return end


_TOKEN_SPLIT = re.compile(r"[\s,]+")
_SIDO_SET = frozenset(SIDO)
_SGG_GENERIC_RE = re.compile(r"[가-힣]{1,5}(?:시|군|구)")


def generalize_address(text: str) -> str:
    """The 시/도 + 시/군/구 prefix ("서울 강남구 테헤란로 123" -> "서울 강남구"), or ""."""
    toks = [t for t in _TOKEN_SPLIT.split(text.strip()) if t]
    parts: list[str] = []
    if toks and toks[0] in _SIDO_SET:
        parts.append(toks.pop(0))
    for tok in toks[:2]:
        # Without a 시/도 the first level must be a known 시/군/구 name.
        if not (tok in SIGUNGU or (parts and _SGG_GENERIC_RE.fullmatch(tok))):
            break
        parts.append(tok)
    return " ".join(parts)
