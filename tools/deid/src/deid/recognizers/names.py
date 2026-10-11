"""Rule-based person-name recognizers: Korean (surname + trigger) and English (title/label)."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable

from ..entities import PERSON
from ..types import Span
from .base import make_span

if TYPE_CHECKING:
    from ..config import Config

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

SURNAMES2 = frozenset({"남궁", "황보", "제갈", "선우", "독고", "사공", "서문"})
SURNAMES1 = frozenset("""
김 이 박 최 정 강 조 윤 장 임 한 오 서 신 권 황 안 송 전 홍 유 고 문 양 손 배 백 허 남 심
노 하 곽 성 차 주 우 구 나 민 진 지 엄 채 원 천 방 공 현 함 변 염 여 추 도 소 석 선 설 마
길 연 위 표 명 기 반 라 왕 금 옥 육 인 맹 제 모 탁 국 어 은 편 용 예 경 봉 사 부 가 복 태
목 형 피 두 감 호 음 빈 동 온 시 범 좌 팽 승 상 갈 단 견 화 류 당 창
""".split())
SURNAMES = SURNAMES1 | SURNAMES2

_BASE_PARTICLES = (
    "에게서", "한테서", "께서는", "께서도", "에게는", "에게도", "한테는", "에서는", "으로는",
    "이랑", "에게", "한테", "께서", "에서", "으로", "부터", "까지", "처럼", "보다", "하고",
    "와는", "과는", "이나", "이며", "이고", "이다", "이야", "였다", "였고", "예요",
    "입니다", "이에요", "이었다", "이었고",
    "가", "이", "는", "은", "를", "을", "의", "께", "와", "과", "도", "만", "랑", "에", "로",
    "아", "야", "나",
)
# Colloquial "민준이가", "서연이는": 이 + particle after a consonant-final name.
_I_COMBOS = tuple("이" + p for p in ("가", "는", "를", "도", "의", "만", "와", "랑", "한테", "에게", "나"))
PARTICLES = tuple(sorted(set(_BASE_PARTICLES + _I_COMBOS), key=lambda p: (-len(p), p)))

HONORIFICS = tuple(sorted({
    "님", "씨", "군", "양",
    "환자", "환자분", "환자님", "보호자", "보호자분", "보호자님",
    "선생", "선생님", "원장", "원장님", "교수", "교수님", "간호사", "간호사님", "수간호사",
    "전공의", "전문의", "레지던트", "인턴", "약사", "약사님", "치료사",
    "할머니", "할아버지", "할머님", "할아버님", "어머니", "아버지", "어머님", "아버님", "어르신",
    "여사", "여사님",
    "과장", "과장님", "팀장", "팀장님", "실장", "실장님", "부장", "부장님", "차장", "차장님",
    "대리", "대리님", "주임", "주임님", "대표", "대표님", "이사", "이사님", "사장", "사장님",
    "회장", "회장님", "기사", "기사님", "박사", "박사님", "변호사", "변호사님",
}, key=lambda h: (-len(h), h)))

# Labels that introduce a name. Kinds: strong (single space is enough), weak (single space
# needs a 3+ syllable name), colon (needs ':' or a table-like separator).
_STRONG_LABELS = (
    "성명", "환자명", "환자성명", "환자이름", "수진자명", "보호자명", "보호자성명", "예금주",
    "작성자", "신청인", "신청자", "담당의", "주치의", "담당자", "처방의", "진료의", "판독의",
    "집도의", "의뢰인", "의뢰의", "피보험자", "수령인", "수취인", "고객명", "서명자",
    "보낸 사람", "받는 사람", "보내는 사람", "발신인", "수신인", "발신자", "수신자",
    "대표자", "예약자", "예약자명", "환아명", "산모명", "대리인", "법정대리인", "동의자", "보증인",
    "기록자", "검사자", "확인자", "요청자", "받는분", "보내는분", "수령자",
)
_WEAK_LABELS = (
    "환자", "보호자", "수진자", "담당", "의사", "원장", "교수", "간호사", "전공의", "과장",
    "남편", "아내", "부인", "배우자", "아들", "딸", "모친", "부친", "Dr.", "Prof.", "Mr.", "Ms.",
)
_COLON_LABELS = (
    "이름", "성함", "성 명", "이 름", "수신", "참조", "발신", "name", "patient", "patient name", "pt",
)
NAME_LABELS = _STRONG_LABELS + _WEAK_LABELS + _COLON_LABELS

# Common words (mostly clinical) that begin with a surname syllable. A name candidate equal to
# one of these, or one of these plus a particle/suffix, is never reported.
STOPWORDS = frozenset("""
이상 이하 이후 이전 이번 이미 이제 이유 이용 이동 이송 이해 이행 이외 이내 이날 이곳 이쪽 이분
이들 이것 이런 이름 이야기 이웃 이모 이사 이사장 이번주 이월 이틀 이명 이완 이물 이물감 이식
이환 이차 이뇨제 이미지 이용자 이민 이상한 이상함 이비인후과 이혼 이전원 이해도
정상 정도 정기 정보 정확 정밀 정맥 정신 정신과 정형외과 정리 정말 정지 정체 정착 정답 정의 정식
정오 정원 정부 정책 정형 정상인 정기적 정량 정성 정규 정산 정액 정해진 정정 정상적
조사 조절 조영 조영제 조기 조금 조치 조직 조심 조언 조건 조정 조제 조합 조회 조무사 조교
조부모 조부 조모 조카 조상 조현병 조울증 조장 조식 조깅 조용 조영술
신경 신경과 신경외과 신장 신체 신규 신환 신청 신생아 신부 신랑 신고 신속 신호 신뢰 신약
신부전 신증 신원 신분 신상 신중 신발 신문 신경통 신생물 신종 신부님 신경학
장기 장애 장애인 장염 장관 장모 장인 장남 장녀 장점 장소 장비 장교 장로 장치 장례 장갑 장난
장내 장루 장시간 장거리
고열 고혈압 고혈당 고지혈 고지혈증 고령 고령자 고위험 고객 고객님 고모 고수 고인 고도 고도비만
고통 고정 고민 고려 고향 고등 고무 고름 고관절 고막 고용 고생 고유 고장 고체 고등학생
한방 한의원 한의사 한국 한국인 한문 한번 한쪽 한계 한편 한참 한숨 한달 한약 한기 한두
오한 오심 오늘 오전 오후 오른쪽 오래 오류 오염 오진 오직 오일 오십견 오른
유방 유방암 유아 유치원 유형 유지 유무 유사 유발 유전 유산 유두 유착 유행 유의 유일 유리
유도 유입 유효 유출
임신 임신부 임산부 임상 임부 임금 임의 임시 임종 임파선 임플란트
전신 전체 전원 전화 전달 전공의 전문의 전도사 전날 전일 전립선 전후 전혀 전반 전이 전이성
전해질 전방 전염 전산 전자 전기 전부 전반적 전신마취
문진 문제 문의 문서 문자 문장 문화 문턱
홍반 홍역 홍조 홍채 홍보 홍차 홍삼
양성 양측 양호 양상 양치 양약 양수 양말 양쪽 양손 양발 양파 양적 양부 양모
음성 음주 음주자 음식 음료 음악 음부 음낭 음경
안정 안과 안전 안내 안내원 안구 안면 안색 안압 안경 안약 안쪽 안심
서맥 서명 서류 서방 서울 서로 서서히 서식 서랍
손목 손가락 손등 손바닥 손톱 손상 손자 손녀 손님 손발
황달 황인 황반 황색
기침 기사 기사님 기존 기타 기간 기능 기록 기관 기관지 기흉 기저 기자 기장 기술 기억 기준 기분
기본 기대 기도 기부자 기형 기립 기복 기상 기온 기운 기전 기재
김장 김치 김밥
박동 박사 박리 박수 박자
최근 최대 최소 최초 최종 최선 최고 최저 최적 최악
강도 강사 강화 강직 강박 강박증 강의 강조 강제
구토 구강 구내염 구역 구환 구청장 구급 구조 구입 구매 구성 구간 구분 구체 구순
배뇨 배변 배꼽 배액 배양 배우자 배달원 배우 배부 배려 배정
백혈구 백혈병 백내장 백인 백신 백분율 백태
우측 우울 우울증 우선 우려 우유 우리 우상복부 우하복부
좌측 좌약 좌석 좌우 좌상복부 좌하복부
표준 표시 표정 표면 표피 표적 표기
방문 방광 방광염 방법 방사선 방향 방장 방지 방금 방치 방식
소견 소견서 소변 소아 소아과 소화 소화기 소장 소수 소방관 소량 소독 소통 소실 소양감 소양증
소염제 소위 소령 소요
진료 진료과 진단 진단서 진찰 진행 진술 진통 진통제 진정 진정제 진균 진물 진료비 진료실 진료의
상태 상기 상지 상담 상담사 상담원 상처 상복부 상황 상승 상사 상주 상세 상대 상당 상한 상악
상부 상기도 상동 상식 상온
차트 차장 차관 차남 차녀 차이 차도 차량 차단 차후
하지 하복부 하루 하나 하나님 하사 하부 하강 하향 하품
경과 경증 경구 경부 경비 경비원 경찰 경찰관 경계성 경련 경우 경험 경향 경도 경미 경색 경화
남성 남자 남편 남아 남은 남녀 남매 남측
여성 여자 여아 여러 여부 여행 여름 여유 여드름 여사
노인 노약자 노모 노부 노력 노출 노화 노무사 노란
원장 원장님 원인 원래 원감 원사 원칙 원활
선생 선생님 선배 선배님 선장 선임 선종 선천 선택 선별
연고 연구 연구원 연구자 연락 연락처 연령 연속 연하 연장 연기
주사 주치의 주의 주간 주기 주인 주임 주장 주무관 주요 주변 주로 주말 주소 주증상 주호소 주사제
시행 시술 시간 시작 시력 시장 시야 시점 시험
지속 지난 지연 지시 지방 지역 지침 지혈 지인 지금 지원
성인 성별 성분 성장 성격 성기 성향
공황 공복 공주 공무원 공간 공기 공동 공개
사망 사장 사회 사모 사모님 사부 사돈 사용 사항 사례 사진 사이 사지 사구체
부종 부친 부인 부모 부모님 부부 부장 부위 부작용 부전 부정맥 부족 부분 부담 부착 부비동 부교수
가래 가족 가정 가슴 가려움 가능 가끔
모든 모친 모두 모양 모습 모유 모름
동료 동네 동일 동의 동반 동맥 동맥류 동상 동서 동생 동안 동시
명단 명일 명확 명치
피부 피부과 피부염 피로 피검사 피하 피아노 피디
두통 두피 두드러기
감기 감염 감각 감소 감독
호흡 호흡기 호전 호소 호중구 호르몬
빈혈 빈맥 빈도 빈뇨
온몸 온도 온열 범위 승모판 화상 화학 창상 창구
단순 단백 단백뇨 단계 당뇨 당뇨병 당일 당시 당분간
금일 금식 금연 금주 국어 국가 국장 천식 천사 편도 편도염 편두통 편의 편안
설사 설명 설계사 변비 변화 변호사 변경 염좌 염증 맹장 태아 태반 태권도 용종 용량 용법 용왕
육종 육안 인후 인후통 인두 인두염 인지 인턴 도움 도우미 도사 도덕 도련 도령 도지사 석사
반장 반응 반복 봉사자 위암 위장 위장염 위험 위중 위궤 위궤양 위치 위염 마취 마비 마약
길이 민감 엄마 채혈 채취 함장 추가 추적 추후 추정 현재 현병력 현기증 현상
권사 권고 권유 허리 허혈 허약 심장 심부전 심전도 심한 심리 심박 심방 심실 심근
나이 나머지 나중 왕자 왕진 예약 예정 예방 예후 예상
복통 복부 복용 복약 복수 복합 복귀 목사 목표 목록 목욕 형수 형제 견관절 팽만 갈증 갈색
제수 제외 제거 제공 제출 제한
조리사 조산사 조종사 위생사 은행원 사무원 사무직 기술자 노동자 승무원 정비사 도배사 사업가 사업자
마술사 장의사 공인 공무직 경리 감정사 도선사 운전사 운전수
어머니 어머님 여동생 남동생 고모부 이모부 고모님 이모님 형수님 제수씨 사촌 사촌형 사촌동생 사촌언니
사촌오빠 사촌누나 시누이 시동생 시어머니 시아버지 시어머님 시아버님 시부모 시부모님 시댁 조카딸
조카사위 손자녀 손주 사위 오빠 남자친구 여자친구 남친 여친 동거인 동거녀 동거남 외손자 외손녀
모바일 연락망 비상연락 비상연락망 대표번호 공용폰
""".split())

# Endings that make a word a common noun even when the start looks like a name.
_NONNAME_SUFFIXES = (
    "궤양", "종양", "배양", "요양", "영양", "모양",
    "증후군", "위험군", "대조군", "환자군", "정상군", "실험군", "치료군",
)
# Finals of symptom / disease / treatment nouns that no given name ends with
# (피로감, 우울감, 이질감, 통증, 장염, 두통, 출혈, 위암, 상비약, 수술, 요법, 당뇨병).
_NONNAME_FINALS = frozenset("감증염통혈암약술법병")
# Syllables that end particles, nominal endings or noun suffixes; STOPWORD + one of these
# (e.g. "상태가", "이해함", "유형별") is not a name.
_SPLIT_FINALS = frozenset("가이는은를을의께와과도만랑에로아야나들등별적률율량측쪽내외간씩쯤째뿐함됨음임인한된할될해중시하")
# A given name never ends with these syllables ("이수는" -> 이수 + 는).
_BAD_FINALS = frozenset("는를의에께함됨음임했됐됩합습")
# Particle-like finals that are also common at the end of given names (지은, 하은, 영도, 민아).
_AMBIGUOUS_FINALS = frozenset("이은도만가과와을랑아야나")
# Honorifics that are ordinary nouns too ("고령 환자", "상기 보호자"): a 2-syllable name
# followed only by one of these is ignored.
_NOUNISH_HON = frozenset({
    "환자", "환자분", "환자님", "보호자", "보호자분", "보호자님", "어머니", "아버지", "할머니",
    "할아버지", "어머님", "아버님", "할머님", "할아버님", "어르신", "대표", "대표님", "기사",
    "기사님", "이사", "이사님", "사장", "사장님", "회장", "회장님", "박사", "박사님", "여사",
    "여사님", "군", "양",
})
# Determiners that look like surnames ("이 환자", "고 김민준", "전 원장"): no spaced-surname form.
_DETERMINERS = frozenset("이고전현신구모제양주한남여소대총각본차원장")
_HON_MULTI = tuple(h for h in HONORIFICS if len(h) >= 2)

_MASK_CHARS = "OＯ○◯●◎*＊ㅇxX×"
_MASK = "[" + re.escape(_MASK_CHARS) + "]"


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=lambda w: (-len(w), w)))


_PART_ALT = _alt(PARTICLES)
_HON_ALT = _alt(HONORIFICS)
_TAIL_HON_RE = re.compile(rf"(?P<hon>{_HON_ALT})(?:{_PART_ALT}){{0,2}}")
_TAIL_PART_RE = re.compile(rf"(?:{_PART_ALT}){{1,2}}")
# A Hangul word (masking characters allowed after the first syllable), not glued to a
# preceding letter/digit.
_WORD_RE = re.compile(rf"(?<![가-힣ㄱ-ㅣA-Za-z0-9])[가-힣](?:[가-힣]|{_MASK})*")

_SEX = r"(?:남자|여자|남성|여성|[Mm]ale|[Ff]emale|남|여|[MFmf])"
_AGE = r"(?:만[ ]?)?\d{1,3}[ ]?(?:세|살|개월|y/o|yo|yrs?|[Yy])?"
_BORN = r"(?:(?:19|20)?\d{2}[ ]?년[ ]?생|(?:19|20)\d{2}[.\-/][01]?\d[.\-/][0-3]?\d[ ]?생)"
_PAREN_RE = re.compile(
    rf"[ ]?[(（][ ]*(?:{_SEX}[ ]*[/,·][ ]*{_AGE}|{_AGE}[ ]*[/,·][ ]*{_SEX}"
    rf"|[MF][ ]?\d{{1,3}}|\d{{1,3}}[ ]?[MF]|(?:만[ ]?)?\d{{1,3}}[ ]?세|{_SEX}"
    rf"|{_BORN}(?:[ ]*[/,·][ ]*{_SEX})?)[ ]*[)）]"
)
# KakaoTalk exports: "[홍길동] [오후 2:13] ..." (PC) and "2024. 3. 15. 오후 2:13, 홍길동 : ..." (mobile).
_KAKAO_TIME = r"(?:오전|오후|AM|PM)[ ]?\d{1,2}:\d{2}"
_KAKAO_PC_BEFORE = re.compile(r"(?:^|\n)[ \t]*\[\Z")
_KAKAO_PC_AFTER = re.compile(rf"\][ \t]*\[{_KAKAO_TIME}\]")
_KAKAO_MOBILE_BEFORE = re.compile(rf"{_KAKAO_TIME},[ \t]*\Z")
_KAKAO_MOBILE_AFTER = re.compile(r"[ \t]*:[ \t]")
# Self-introduction: "저 신유진입니다", "저는 김민준인데요", "제 이름은 박지훈이에요".
_INTRO_BEFORE = re.compile(r"(?<![가-힣])(?:저는|저|나는|제[ ]?이름은|이름은)[ \t]+\Z")
# A personal mobile number right after the name: "정아린 / 016-9616-2884", "김민준 010-1234-5678".
_MOBILE_AFTER = re.compile(
    r"[ \t]{0,3}(?:[/,|:：][ \t]{0,3})?(?:\+82[ \t-]?)?0?1[016789][-. ]?\d{3,4}[-. ]?\d{4}(?!\d)"
)
# Finals of place / device / time words that also sit next to phone numbers (사무실, 원무과, 공용폰).
_NOT_BEFORE_PHONE = frozenset("실과부팀처소국청관층점터폰일화번")
# Hypocoristic 이 + particle ("정채원이가", "김민준이는"): grammatical only after a name that
# ends in a final consonant; a common noun takes the bare particle ("인후염이", "인후염은").
_HYPOCORISTIC = frozenset(c for c in _I_COMBOS if c != "이나")  # "3개월이나" is not a name
# Verb-stem finals of nouns that end in a derivational 이 (양손잡이, 고집쟁이, 하루살이, 가슴앓이).
_DEVERBAL_FINALS = frozenset("잡쟁박뱅둥깎놀살맞닦앓받막풀벌떨걸갈먹찍꽂붙뚫묶넣깔밟씻팔꾼낚")
_COPULA = frozenset({"입니다", "이에요", "예요", "이예요", "인데요", "인데", "이고요", "이고", "이요", "이야",
                     "이라고", "라고"})


def _label_regex(label: str) -> str:
    rx = re.escape(label).replace(r"\ ", "[ ]{0,2}")
    return f"(?i:{rx})" if label.isascii() else rx


_LABEL_KIND = {}
for _kind, _labels in (("strong", _STRONG_LABELS), ("weak", _WEAK_LABELS), ("colon", _COLON_LABELS)):
    for _lab in _labels:
        # First kind wins: the spaced form "성 명" must not demote the strong label "성명".
        _LABEL_KIND.setdefault(_lab.replace(" ", "").lower(), _kind)
_LABEL_RE = re.compile(
    rf"(?<![가-힣A-Za-z])(?P<label>{'|'.join(_label_regex(x) for x in sorted(NAME_LABELS, key=len, reverse=True))})"
    r"(?:[ ]?[(（][^()（）\n]{1,10}[)）])?"  # relation: "보호자(딸) 문채원", "보호자(배우자): ..."
    r"(?P<sep>[ \t]{0,4}[:：|][ \t]{0,4}|\t[ \t]{0,6}|[ ]{1,6})\Z"
)
# Letter closings written after a full name: "김하윤 드림", "박서준 올림".
_CLOSING_RE = re.compile(r" (?:드림|올림)(?![가-힣A-Za-z0-9])")
# Object-like particles that make "<word> 드림" a verb phrase ("자료를 드림", "안내도 드림").
_CLOSING_BAD_FINALS = frozenset("을를도만와과랑")
# A form field ends after the value: a gap of 2+ spaces, a tab, a separator or the line end.
_FIELD_END_RE = re.compile(r"(?:[ ]{2,}|\t|[ ]?[|/,(（]|[ ]*(?:\n|\Z))")

_SCORE_HON = 0.85
_SCORE_PAREN = 0.9
_SCORE_LABEL = {"strong": 0.9, "weak": 0.85, "colon": 0.9}
_SCORE_SPACED = 0.85
_SCORE_DICT = 0.95


def _is_hangul(c: str) -> bool:
    return "가" <= c <= "힣"


def _has_final_consonant(c: str) -> bool:
    """True for a Hangul syllable with a final consonant (받침): 준, 원 but not 서, 아."""
    return _is_hangul(c) and (ord(c) - 0xAC00) % 28 != 0


def _has_mask(s: str) -> bool:
    return any(c in _MASK_CHARS for c in s)


def strip_particle(word: str) -> str:
    """Drop one trailing particle ("김민준이" -> "김민준"); keeps 3-syllable names like "김지은"."""
    for p in PARTICLES:
        if len(word) - len(p) < 2 or not word.endswith(p):
            continue
        if len(p) == 1 and p in _AMBIGUOUS_FINALS and (
            len(word) == 3 or (len(word) == 4 and word[:2] in SURNAMES2)
        ):
            continue
        return word[: -len(p)]
    return word


def _given_ok(given: str) -> bool:
    if not 1 <= len(given) <= 2:
        return False
    if not all(_is_hangul(c) or c in _MASK_CHARS for c in given):
        return False
    return given[-1] not in _BAD_FINALS


def _name_lengths(word: str) -> list[int]:
    """Possible name lengths at the start of a word, longest first."""
    out = set()
    if len(word) >= 3 and word[:2] in SURNAMES2:
        out.update(k for k in (3, 4) if k <= len(word) and _given_ok(word[2:k]))
    if word[0] in SURNAMES1:
        out.update(k for k in (2, 3) if k <= len(word) and _given_ok(word[1:k]))
    return sorted(out, reverse=True)


def _givens(name: str) -> list[str]:
    out = [name[2:]] if len(name) >= 3 and name[:2] in SURNAMES2 else []
    if name[0] in SURNAMES1:
        out.append(name[1:])
    return out


def _rejected(name: str, word: str, hon: str | None = None, field: bool = False) -> bool:
    """True when the candidate is (or looks like) an ordinary word rather than a name.

    `field`: the value of a strong labelled field ("신청인: 국가은"), where a stopword plus a
    particle-like final ("국가" + "은") is still read as a name.
    """
    flat = name.replace(" ", "")
    if flat in STOPWORDS or word in STOPWORDS or strip_particle(word) in STOPWORDS:
        return True
    if _has_mask(flat):
        return False
    if any(g.startswith(h) for g in _givens(flat) for h in _HON_MULTI):
        return True  # 김선생님, 정교수, 박사장
    if word.endswith(_NONNAME_SUFFIXES) or (len(flat) >= 3 and flat[-1] in _NONNAME_FINALS):
        return True
    if field or (hon is not None and hon[0] in "님씨"):
        return False  # 님/씨 follow names, so "이상은 님" stays a name
    return len(flat) >= 3 and flat[:-1] in STOPWORDS and flat[-1] in _SPLIT_FINALS


def _kakao_sender(text: str, start: int, end: int) -> bool:
    """True when [start, end) is the sender of a KakaoTalk export line."""
    lo = max(0, start - 30)
    return bool(
        (_KAKAO_PC_BEFORE.search(text, lo, start) and _KAKAO_PC_AFTER.match(text, end))
        or (_KAKAO_MOBILE_BEFORE.search(text, lo, start) and _KAKAO_MOBILE_AFTER.match(text, end))
    )


def _label_before(text: str, start: int) -> tuple[str, bool] | None:
    """(kind, table_like) when a name label directly precedes `start` on the same line."""
    m = _LABEL_RE.search(text, max(0, start - 40), start)
    if not m:
        return None
    sep = m.group("sep")
    table_like = any(c in sep for c in ":：|\t") or len(sep) >= 2
    return _LABEL_KIND.get(re.sub(r"\s", "", m.group("label")).lower(), "weak"), table_like


def _label_ok(label: tuple[str, bool], name: str) -> bool:
    kind, table_like = label
    if table_like:
        return True
    if kind == "colon":
        return False
    return kind == "strong" or len(name.replace(" ", "")) >= 3 or _has_mask(name)


def _spaced_honorific(text: str, end: int) -> str | None:
    """Honorific written as the next word ("김민준 님", "이서연 환자는")."""
    if end >= len(text) or text[end] != " ":
        return None
    m = _WORD_RE.match(text, end + 1)
    if not m:
        return None
    hm = _TAIL_HON_RE.fullmatch(m.group())
    return hm.group("hon") if hm else None


def _combine(scores: list[float]) -> float:
    return min(0.95, max(scores) + 0.05 * (len(scores) - 1))


def _dict_spans(text: str, cfg: "Config", hangul: bool, source: str = "dict") -> list[Span]:
    """Exact, boundary-aware matches of cfg.names entries (Hangul or Latin ones)."""
    out: list[Span] = []
    for raw in cfg.names:
        value = raw.strip()
        if not value or any(_is_hangul(c) for c in value) != hangul:
            continue
        for m in re.finditer(re.escape(value), text):
            s, e = m.span()
            if hangul:
                if s > 0 and re.match(r"[가-힣A-Za-z]", text[s - 1]):
                    continue
                rest = re.match(r"[가-힣]*", text[e:]).group()
                if rest and not (_TAIL_HON_RE.fullmatch(rest) or _TAIL_PART_RE.fullmatch(rest)):
                    continue
            elif (s > 0 and text[s - 1].isascii() and text[s - 1].isalnum()) or (
                e < len(text) and text[e].isascii() and text[e].isalnum()
            ):
                continue
            out.append(make_span(text, s, e, PERSON, _SCORE_DICT, source))
    return out


def _finish(text: str, hits: dict[tuple[int, int], list[float]], extra: list[Span],
            cfg: "Config", source: str) -> list[Span]:
    """Build spans, drop allowlisted text and spans nested inside a longer hit."""
    best: dict[tuple[int, int], Span] = {
        (s, e): make_span(text, s, e, PERSON, _combine(sc), source) for (s, e), sc in hits.items()
    }
    for sp in extra:  # dictionary hits win over a rule hit on the same text
        cur = best.get((sp.start, sp.end))
        if cur is None or sp.score > cur.score:
            best[(sp.start, sp.end)] = sp
    spans = list(best.values())
    allow = set(cfg.allowlist)
    spans = [sp for sp in spans if sp.text not in allow and sp.text.strip() not in allow]
    keep = [
        sp for sp in spans
        if not any(o is not sp and o.start <= sp.start and sp.end <= o.end
                   and (o.end - o.start) > (sp.end - sp.start) for o in spans)
    ]
    return sorted(keep, key=lambda sp: (sp.start, sp.end))


class KoreanNameRecognizer:
    """Korean names: surname + 1-2 syllables, confirmed by honorific, label, (sex/age) or dictionary."""

    name = "names"
    entities = (PERSON,)

    def find(self, text: str, cfg: "Config") -> list[Span]:
        hits: dict[tuple[int, int], list[float]] = {}

        def add(s: int, e: int, scores: list[float]) -> None:
            hits.setdefault((s, e), []).extend(scores)

        for m in _WORD_RE.finditer(text):
            self._scan_word(text, m.start(), m.end(), m.group(), add)
        return _finish(text, hits, _dict_spans(text, cfg, hangul=True), cfg, self.name)

    @staticmethod
    def _scan_word(text: str, ws: int, we: int, word: str,
                   add: Callable[[int, int, list[float]], None]) -> None:
        label = _label_before(text, ws)
        if word in SURNAMES:
            KoreanNameRecognizer._scan_spaced(text, ws, we, word, label, add)
            return
        next_hon = _spaced_honorific(text, we)
        paren = _PAREN_RE.match(text, we) is not None
        closing = _CLOSING_RE.match(text, we) is not None
        intro = _INTRO_BEFORE.search(text, max(0, ws - 12), ws) is not None
        mobile = _MOBILE_AFTER.match(text, we) is not None
        field = label is not None and label[0] == "strong" and (
            label[1] or _FIELD_END_RE.match(text, we) is not None)
        for k in _name_lengths(word):
            name, tail = word[:k], word[k:]
            hm = _TAIL_HON_RE.fullmatch(tail) if tail else None
            scores: list[float] = []
            hon = None
            if hm:
                hon = hm.group("hon")
            elif tail in ("", "의") and next_hon:
                hon = next_hon
            if hon and not (len(name) == 2 and hon in _NOUNISH_HON and not _has_mask(name)):
                scores.append(_SCORE_HON)
            if not tail and paren:
                scores.append(_SCORE_PAREN)
            if not tail and closing and len(name) >= 3 and name[-1] not in _CLOSING_BAD_FINALS:
                scores.append(_SCORE_HON)
            intro_end = tail in _COPULA or (not tail and (we == len(text) or text[we] in ",.!?"))
            if intro and len(name) >= 3 and intro_end:  # "저는 김민준입니다", "저 김민준, ..."
                scores.append(_SCORE_HON)
            if (tail in _HYPOCORISTIC and len(name) >= 3 and _has_final_consonant(name[-1])
                    and name[-1] not in _DEVERBAL_FINALS):
                scores.append(_SCORE_HON)
            if not tail and mobile and len(name) >= 3 and name[-1] not in _NOT_BEFORE_PHONE:
                scores.append(_SCORE_HON)
            if not tail and _kakao_sender(text, ws, ws + k):
                scores.append(_SCORE_LABEL["strong"])
            if label and (not tail or hm or _TAIL_PART_RE.fullmatch(tail)) and _label_ok(label, name):
                scores.append(_SCORE_LABEL[label[0]])
            if scores and not _rejected(name, word, hon, field=field and not tail):
                add(ws, ws + k, scores)
                return

    @staticmethod
    def _scan_spaced(text: str, ws: int, we: int, surname: str,
                     label: tuple[str, bool] | None,
                     add: Callable[[int, int, list[float]], None]) -> None:
        """"김 민준 님" / "성명: 김 민준": surname and given name split by one space."""
        if surname in _DETERMINERS or text[we:we + 1] != " ":
            return
        m = _WORD_RE.match(text, we + 1)
        if not m or len(m.group()) < 2 or not all(_is_hangul(c) for c in m.group()[:2]):
            return
        given, tail = m.group()[:2], m.group()[2:]
        g_end = m.start() + 2
        hon = None
        if tail:
            hm = _TAIL_HON_RE.fullmatch(tail)
            hon = hm.group("hon") if hm else None
        else:
            hon = _spaced_honorific(text, g_end)
        by_label = label is not None and label[0] != "weak" and (not tail or _TAIL_PART_RE.fullmatch(tail))
        if not (hon or by_label) or given in STOPWORDS or given[-1] in _BAD_FINALS:
            return
        if any(given.startswith(h) for h in _HON_MULTI) or _rejected(surname + given, surname + given, hon):
            return
        add(ws, g_end, [_SCORE_SPACED] + ([_SCORE_LABEL[label[0]]] if by_label else []))


# ---------------------------------------------------------------------------
# English
# ---------------------------------------------------------------------------

EN_STOPWORDS = frozenset("""
patient patients pt doctor dr hospital clinic clinics department dept center centre medical medicine
university college school visit visits office room ward unit icu er or emergency surgery surgical
general internal family health care service services lab laboratory test tests result results report
note notes name date time age sex male female man woman boy girl baby unknown none nil na the and of in
on at for to with by from ok yes no see follow history chief complaint plan assessment diagnosis
impression korea korean seoul busan street road avenue building floor team group staff nurse resident
intern fellow professor prof mr mrs ms miss sir madam signature signed phone tel fax email address id
number inc corp ltd llc mon tue wed thu fri sat sun monday tuesday wednesday thursday friday saturday
sunday jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july
august september october november december am pm normal negative positive left right bilateral
dob mrn ssn rrn dx ddx hx rx tx sx fx hpi pmh psh fh ros pe cc bp hr rr bt spo2 bmi ecg ekg ct mri
cbc cmp lft tft er icu opd ipd gp md rn np pa phd do od os ou po iv im sc prn qd bid tid qid
""".split())

_EN_TOKEN = r"(?:[A-Z](?:'[A-Z])?[a-z]+(?:[A-Z][a-z]+)?(?:-[A-Z][a-z]+)?|[A-Z]{2,}(?:-[A-Z]{2,})?|[A-Z]\.)"
_EN_TOKEN_RE = re.compile(_EN_TOKEN)
_TITLE_RE = re.compile(
    rf"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Professor|Sir|Dame)\b\.?[ \t]+(?P<n>{_EN_TOKEN}(?:[ \t]+{_EN_TOKEN}){{0,2}})"
)
_EN_LABEL_RE = re.compile(
    r"(?<![A-Za-z])(?:(?i:patient(?:'s)?[ \t]+name|full[ \t]+name|name|patient|pt|guardian|physician"
    r"|doctor|attending|surgeon|nurse|signed[ \t]+by|referred[ \t]+by|prepared[ \t]+by|author"
    r"|emergency[ \t]+contact|contact[ \t]+person)|이름|성명|성함|환자명|보호자)"
    rf"[ \t]*[:：][ \t]*(?P<n>{_EN_TOKEN}(?:,?[ \t]+{_EN_TOKEN}){{0,3}})"
)


def _trim_en(text: str, s: int, e: int) -> tuple[int, int] | None:
    """Cut a captured name at the first stopword; drop trailing initials."""
    toks = []
    for t in _EN_TOKEN_RE.finditer(text, s, e):
        if t.group().rstrip(".").lower() in EN_STOPWORDS:
            break
        toks.append(t)
    while toks and toks[-1].group().endswith("."):
        toks.pop()
    if not toks:
        return None
    return toks[0].start(), toks[-1].end()


class EnglishNameRecognizer:
    """English names after titles (Dr., Mr.) or labels (Name:, Patient:), plus Latin cfg.names."""

    name = "names_en"
    entities = (PERSON,)

    def find(self, text: str, cfg: "Config") -> list[Span]:
        hits: dict[tuple[int, int], list[float]] = {}
        for rx in (_TITLE_RE, _EN_LABEL_RE):
            for m in rx.finditer(text):
                rng = _trim_en(text, m.start("n"), m.end("n"))
                if rng:
                    hits.setdefault(rng, []).append(0.85)
        for k in hits:
            hits[k] = [max(hits[k])]
        return _finish(text, hits, _dict_spans(text, cfg, hangul=False), cfg, self.name)
