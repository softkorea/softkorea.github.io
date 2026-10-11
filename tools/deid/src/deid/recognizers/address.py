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
    # Phase / block of a complex: "지웰시티 2차", "6차", "F15블록", "3단지"
    re.compile(rf",?[^\S\n]*(?:[가-힣A-Za-z0-9{re.escape(_MASK)}]{{1,15}}[ ]?)?(?:\d{{1,2}}(?:차|단지)|[A-Z]?\d{{1,3}}블록)"
               r"(?![가-힣0-9])"),
    re.compile(rf",?[^\S\n]*(?:{_BLDG_NAME}[ ]?)?(?:\d{{1,4}}동[ ]?)?\d{{1,5}}호"),
    re.compile(rf",?[^\S\n]*(?:{_BLDG_NAME}[ ]?)?\d{{1,4}}동(?![가-힣])"),
    re.compile(r",?[^\S\n]*(?:지하[ ]?|B)?\d{1,3}층"),
    re.compile(rf",?[^\S\n]*{_BLDG_NAME}(?![가-힣A-Za-z0-9])"),
    # Any one-word complex name right after an anchored address ("센텀파크 102동 803호").
    re.compile(rf",?[^\S\n]*[가-힣A-Za-z0-9{re.escape(_MASK)}]{{1,15}}[ ]?(?:\d{{1,4}}동[ ]?)?\d{{1,5}}호"),
)
_APT_RE = re.compile(rf"(?<![가-힣A-Za-z0-9]){_APT_NAME}[ ]?\d{{1,4}}동[ ]?\d{{1,5}}호")

# An apartment unit on its own ("102동 1503호", "101-1203호"): practically never clinical text
# (wards are "71병동", rooms "712호"). The span grows to the left over the complex name, its
# phase/block and any 시/도, 시/군/구, 읍/면/동 words.
_UNIT_RE = re.compile(r"(?<![0-9A-Za-z])(?:\d{1,4}[ ]?동[ ]?\d{1,5}호|\d{2,4}-\d{3,5}호)(?![0-9])(?!선)")
_TOKEN_RE = re.compile(r"[^\s,]+")
_ADMIN_TOKEN = re.compile(rf"(?:{_SIDO_ALT}|{_SGG_ANY}|{_EMD}|{_RI})")
_AUX_TOKEN = re.compile(r"\d{1,2}(?:차|단지)|[A-Z]?\d{1,3}블록|\d{1,5}(?:-\d{1,5})?(?:번지)?")
_COMPLEX_TOKEN = re.compile(rf"[가-힣A-Za-z0-9{re.escape(_MASK)}]{{2,15}}")
_NOT_COMPLEX = frozenset("""
주소 주소는 주소지 현주소 자택 거주지 집 저희 저희집 우리 우리집 본가 친정 시댁 택배 배송지 방문 현재 지금
이사 새 새집 옛 예전 전 원래 거기 여기 그 이 저 같은 동 호 단지 아파트
""".split())
_PARTICLE_FINALS = frozenset("은는을를에의와과랑께서도")
# A unit inside an office / hospital building is a room, not a home: "OO타워 3동 201호".
_NON_HOME_BEFORE = re.compile(
    r"(?:타워|빌딩|센터|프라자|플라자|상가|병원|의원|본관|별관|신관|회관|학교|청사|공장|사옥|오피스)[ ]?\Z")

# Romanized Korean addresses in English letters ("302-1104, 15 Banpo-daero, Seocho-gu, Seoul").
_RO_PROVINCE = (r"(?:Seoul|Busan|Daegu|Incheon|Gwangju|Daejeon|Ulsan|Sejong|Jeju(?:-do)?|"
                r"[A-Z][a-z]+-do|Gyeonggi|Gangwon|Chungbuk|Chungnam|Jeonbuk|Jeonnam|Gyeongbuk|Gyeongnam)"
                r"(?:[ ]?(?:-si|City))?")
_RO_ROAD = r"[A-Z][a-z]+(?:-[a-z]+)*-(?:daero|ro|gil)(?:[ ]?\d{1,3}(?:beon)?-?gil)?"
_RO_ADMIN = r"[A-Z][a-z]+(?:-[a-z]+)*-(?:gu|si|gun|dong|eup|myeon|ri)"
_ROMAN_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?:(?:\d{1,4}-\d{1,5}|(?:Apt|Unit|Rm|Room|#)\.?[ ]?[0-9A-Za-z-]{1,10}),?[ ]+)?"
    rf"(?:\d{{1,5}}(?:-\d{{1,5}})?,?[ ]+{_RO_ROAD}|{_RO_ROAD}[ ]+\d{{1,5}}(?:-\d{{1,5}})?)"
    rf"(?:,?[ ]+{_RO_ADMIN})*(?:,?[ ]+{_RO_PROVINCE})?(?:,?[ ]+\d{{5}})?(?![A-Za-z0-9])"
)
# English street addresses: "455 Harrow Lane, Suite 300, Portland, OR 97205".
_EN_STREET = (r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Boulevard|Blvd|Court|Ct|Way|Place|Pl|"
              r"Terrace|Parkway|Pkwy|Highway|Hwy|Circle|Square|Sq)")
_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_DIGIT = re.compile(r"[0-9]")
_EN_ADDR_RE = re.compile(
    rf"(?<![A-Za-z0-9])\d{{1,5}}[ ]+(?:[A-Z][a-z]+[ ]+){{1,3}}{_EN_STREET}\b\.?"
    r"(?:,?[ ]+(?:Suite|Ste\.?|Apt\.?|Apartment|Unit|Floor|Fl\.?|#)[ ]?[0-9A-Za-z-]{1,8})?"
    r"(?:,[ ]+[A-Z][a-z]+(?:[ ][A-Z][a-z]+)?(?:,?[ ]+[A-Z]{2})?(?:[ ]+\d{5}(?:-\d{4})?)?)?(?![A-Za-z0-9])"
)

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
        ranges = [(sp.start, sp.end, sp.score) for sp in out]
        if "호" in text:
            for m in _UNIT_RE.finditer(text):
                if _NON_HOME_BEFORE.search(text, max(0, m.start() - 12), m.start()):
                    continue
                ranges.append((self._extend_left(text, m.start()), m.end(), 0.85))
        if _HAS_UPPER.search(text) and _HAS_DIGIT.search(text):
            for rx in (_ROMAN_RE, _EN_ADDR_RE):
                for m in rx.finditer(text):
                    ranges.append((m.start(), m.end(), 0.85))
        allow = set(cfg.allowlist)
        spans = [make_span(text, s, e, ADDRESS, sc, self.name) for s, e, sc in _union(ranges)]
        return [sp for sp in spans if sp.text not in allow]

    @staticmethod
    def _extend_left(text: str, start: int) -> int:
        """Grow an apartment unit leftwards over its complex name and admin-area words."""
        line_start = text.rfind("\n", 0, start) + 1
        seg = text[max(line_start, start - 80):start]
        base = start - len(seg)
        toks = list(_TOKEN_RE.finditer(seg))
        complexes = 0
        new_start = start
        for i, t in enumerate(reversed(toks)):
            word = t.group()
            glued = i == 0 and t.end() == len(seg)          # "센텀파크102동"
            if word in _NOT_COMPLEX:
                break
            if _ADMIN_TOKEN.fullmatch(word) and not glued and not (word[-1] == "리" and len(word) < 3):
                new_start = base + t.start()
                continue
            if complexes >= 3 or not _COMPLEX_TOKEN.fullmatch(word):
                break
            if word[-1] in _PARTICLE_FINALS and not glued:
                break
            if not _AUX_TOKEN.fullmatch(word):
                complexes += 1
            new_start = base + t.start()
        return new_start

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


def _union(ranges: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Merge overlapping ranges (keeping the highest score), sorted by start."""
    out: list[list] = []
    for s, e, sc in sorted(ranges):
        if out and s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
            out[-1][2] = max(out[-1][2], sc)
        else:
            out.append([s, e, sc])
    return [tuple(r) for r in out]


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
