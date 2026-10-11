"""Tests for deid.recognizers.names (synthetic names only)."""
from __future__ import annotations

import pytest

from deid.config import default_config
from deid.entities import PERSON
from deid.recognizers.names import (
    HONORIFICS,
    NAME_LABELS,
    PARTICLES,
    STOPWORDS,
    SURNAMES,
    EnglishNameRecognizer,
    KoreanNameRecognizer,
    strip_particle,
)

KO = KoreanNameRecognizer()
EN = EnglishNameRecognizer()


def ko(text, cfg=None):
    return [s.text for s in KO.find(text, cfg or default_config())]


def en(text, cfg=None):
    return [s.text for s in EN.find(text, cfg or default_config())]


CLINICAL_NOTE = """\
[외래 경과기록]
진료과: 내과 / 담당: 원장님 / 작성자: 주치의
보호자: 배우자 / 보호자: 모친
주소(C.C.): 3일 전부터 발생한 고열, 오한, 기침, 가래
현병력: 상기 환자는 고혈압, 당뇨로 본원 정기 추적 중인 분으로, 내원 3일 전부터 38.5도의 고열과 오한이
지속되었다고 함. 이후 기침 및 노란 가래 동반됨. 구토 1회, 설사는 없었음. 최근 해외여행력 없음.
이전 검사상 이상 소견 없었음.
과거력: 고혈압(10년 전 진단), 당뇨(5년 전), 이상지질혈증. 유방암 수술력 없음. 임신 가능성 없음.
위궤양 치료력 있음.
가족력: 부친 고혈압, 모친 당뇨. 형제 없음.
사회력: 흡연 20갑년, 음주 주 2회. 정상 체중.
신체검진: 전신 상태 양호, 의식 명료. 혈압 135/85 mmHg, 맥박 92회/분, 호흡 20회/분, 체온 38.2도.
우측 하폐야 수포음 청진됨. 좌측은 정상. 하지 부종 없음. 황달 없음. 경부 림프절 비대 없음.
신경학적 이상 소견 없음. 서맥 없음. 홍반 없음. 손목 통증 호소.
검사: 백혈구 12,800/uL (정상 4,000-10,000), 호중구 82%, 혈색소 13.2 g/dL, 혈소판 245 K/uL,
CRP 8.4 mg/dL, 크레아티닌 0.9, 공복혈당 142 mg/dL, HbA1c 7.2%. 흉부 X선 우하엽 경화 소견.
신장 기능 정상. 소변검사 정상. 조영 CT 시행 안 함. 음성 소견.
평가: 지역사회획득 폐렴 의증. 고혈압 조절 양호, 당뇨 조절 불량. 고위험군 아님.
계획: 경구 항생제 처방(아목시실린/클라불란산 625mg 1일 3회, 7일), 해열제, 수분 섭취 교육.
3일 후 재방문. 증상 악화 시(호흡곤란, 의식 저하) 응급실 방문 교육함. 담당 간호사에게 투약 교육
의뢰. 원장님 확인 후 보호자에게 설명함. 선생님께서 처방 변경 여부 검토 예정. 환자분 및 보호자
이해함. 고객님 응대. 정형외과 협진 의뢰, 신경과 교수님 회신 대기. 이상 환자 상태 안정적이며 경과
관찰 중. 노인 환자로 낙상 주의. 소아 환자 아님. 임산부 환자 아님. 진료 차트 정리 완료. 연고 도포,
주사 처치. 남성 환자. 여성 보호자 동반. 기존 환자 아님. 의사 소견서 발급. 환자 이송은 불필요.
환자 정보를 확인함. 환자 상태가 호전됨. 강도 높은 운동 제한. 표준 치료 시행. 우측 상지 및 좌측
하지 근력 정상. 배뇨 장애 없음. 김장 후 손목 통증. 박동성 두통 없음. 최대 용량 유지. 최소 3일.
한방 치료 병행 안 함. 문진상 특이사항 없음. 장기 처방. 신경 압박 소견 없음. 조사 결과 정상.
통증 정도는 환자가 느끼는 대로 조절. 이분 어머니께서 동행. 사모님 동행.
"""


# --- shared constants -------------------------------------------------------

def test_constants_shape():
    assert {"김", "이", "박", "최", "정", "남궁", "황보", "제갈", "선우", "독고", "사공", "서문"} <= SURNAMES
    assert len(SURNAMES) >= 110
    assert len(STOPWORDS) >= 150
    assert {"이상", "정상", "조영", "고혈압", "신경", "원장", "선생"} <= STOPWORDS
    assert {"님", "씨", "환자", "선생님", "원장님", "보호자", "어르신"} <= set(HONORIFICS)
    assert {"이름", "성명", "환자명", "보호자", "담당의", "예금주", "성함"} <= set(NAME_LABELS)
    assert {"가", "이", "는", "은", "에게", "께서", "이랑", "한테"} <= set(PARTICLES)
    # longest first, so callers can strip greedily
    assert [len(p) for p in PARTICLES] == sorted((len(p) for p in PARTICLES), reverse=True)
    assert [len(h) for h in HONORIFICS] == sorted((len(h) for h in HONORIFICS), reverse=True)


@pytest.mark.parametrize("word, expected", [
    ("김민준이", "김민준"),
    ("이서연은", "이서연"),
    ("김민준에게", "김민준"),
    ("김민준이랑", "김민준"),
    ("김민준이가", "김민준"),
    ("남궁민수가", "남궁민수"),
    ("이수는", "이수"),
    ("김지은", "김지은"),   # 3 syllables ending in 은: keep (conservative)
    ("이영도", "이영도"),
    ("남궁민은", "남궁민은"),
    ("홍길동", "홍길동"),
    ("이가", "이가"),
])
def test_strip_particle(word, expected):
    assert strip_particle(word) == expected


# --- Korean positives -------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    # clinical (sex/age) convention
    ("홍길동(M/45) 내원", ["홍길동"]),
    ("김영희 (F/62) 두통", ["김영희"]),
    ("이서연(여, 34세)", ["이서연"]),
    ("박지훈(남/7세)", ["박지훈"]),
    ("임서준(45/M)", ["임서준"]),
    ("한지민(F62)", ["한지민"]),
    # labels
    ("성명: 김민준", ["김민준"]),
    ("환자명 : 이서연", ["이서연"]),
    ("환자명:홍길동", ["홍길동"]),
    ("성 명 : 홍길동", ["홍길동"]),
    ("| 성명 | 홍길동 |", ["홍길동"]),
    ("성명\t홍길동", ["홍길동"]),
    ("보호자 박영수(010-1234-5678)", ["박영수"]),
    ("작성자: 이서연", ["이서연"]),
    ("주치의 박지훈", ["박지훈"]),
    ("담당의: 최유나", ["최유나"]),
    ("예금주 홍길동", ["홍길동"]),
    ("남편 김민준", ["김민준"]),
    ("의사 김민준(내과)", ["김민준"]),
    ("Dr. 김민준", ["김민준"]),
    ("Patient: 홍길동", ["홍길동"]),
    ("환자 김민준이 내원함", ["김민준"]),
    ("성명: 이수", ["이수"]),          # 2-syllable name is fine after a strong label
    # honorifics / roles after the name
    ("홍길동 님", ["홍길동"]),
    ("홍길동씨가 왔다", ["홍길동"]),
    ("김민준님이", ["김민준"]),
    ("이서연 환자는", ["이서연"]),
    ("김민준선생님께서", ["김민준"]),
    ("최유나 간호사", ["최유나"]),
    ("박지훈 교수님", ["박지훈"]),
    ("김민준 할머니", ["김민준"]),
    ("김민준의 어머니", ["김민준"]),
    ("이서연 양", ["이서연"]),
    ("김민준군이", ["김민준"]),
    ("이수 씨", ["이수"]),
    # space between surname and given name
    ("김 민준 님", ["김 민준"]),
    ("성명: 김 민준", ["김 민준"]),
    # two-syllable surnames
    ("남궁민수님", ["남궁민수"]),
    ("황보영 씨", ["황보영"]),
    ("제갈민 님", ["제갈민"]),
    ("선우진 환자", ["선우진"]),
    ("독고민 씨", ["독고민"]),
    ("사공지훈 환자는", ["사공지훈"]),
    ("서문현 님", ["서문현"]),
    # names whose first syllables are also common words
    ("이상민 님", ["이상민"]),
    ("정도윤 선생님", ["정도윤"]),
    ("신경민 님", ["신경민"]),
    ("이상은 님", ["이상은"]),
    # partially masked names still leak, so they are reported
    ("김OO 원장", ["김OO"]),
    ("김○○ 선생님", ["김○○"]),
    ("김** 님", ["김**"]),
    ("김ㅇㅇ 님", ["김ㅇㅇ"]),
    ("이*연 환자", ["이*연"]),
    ("환자명: 이*연", ["이*연"]),
    ("김민* 님", ["김민*"]),
    # several names in one text
    ("김민준 환자 보호자 이서연", ["김민준", "이서연"]),
])
def test_korean_positive(text, expected):
    assert ko(text) == expected


def test_span_offsets_and_metadata():
    text = "환자명: 홍길동님께서 내원"
    spans = KO.find(text, default_config())
    assert len(spans) == 1
    sp = spans[0]
    assert (sp.start, sp.end, sp.text) == (5, 8, "홍길동")
    assert text[sp.start:sp.end] == sp.text
    assert sp.entity == PERSON and sp.source == "names"
    assert 0.85 <= sp.score <= 0.95


def test_multiple_triggers_raise_score():
    one = KO.find("김민준 님", default_config())[0].score
    two = KO.find("환자 이서연(여, 34세)", default_config())[0].score
    assert two > one


# --- Korean negatives -------------------------------------------------------

def test_clinical_note_without_names_has_no_person():
    assert KO.find(CLINICAL_NOTE, default_config()) == []
    assert EN.find(CLINICAL_NOTE, default_config()) == []


@pytest.mark.parametrize("text", [
    # roles alone
    "담당 간호사", "원장님", "선생님께서", "교수님", "원장님 확인", "담당: 원장님", "고객님", "사모님",
    "김선생님", "정교수님", "박사장님", "김과장", "이대리", "조교수님", "장모님", "기사님", "하나님께서",
    # clinical vocabulary before honorific-like nouns
    "상기 환자는", "고혈압 환자", "이상 소견 없음", "정상 범위", "조영제 투여", "신경과 교수님", "이후 경과 관찰",
    "고열 환자", "위궤양 환자", "정상군 대비", "고위험군", "신장내과 환자", "피부과 환자", "소아과 환자",
    "조현병 환자", "우울증 환자", "백혈병 환자", "편두통 환자", "인후통 환자", "공황장애 환자", "부종 환자",
    "남아 환자", "여아 환자", "장애인 환자", "임신부 환자", "성인 환자", "고령 환자", "경증 환자",
    "금일 환자", "당일 환자", "동일 환자", "전원 환자", "신환 환자", "모든 환자", "여러 환자", "남은 환자",
    "상기 보호자", "이웃 할머니", "동네 할머니", "음악 선생님", "국어 선생님", "상담 선생님", "이번주 환자",
    "이 환자분은", "이 선생님", "이 고객님", "전 원장님",
    # labels followed by ordinary words
    "환자 이송은 불필요", "환자 정보를 확인", "환자 상태가 호전", "보호자 이해함", "보호자: 배우자",
    "보호자: 모친", "부친 고혈압", "모친 당뇨", "의사 소견서", "의사 진단서", "담당 전공의", "담당 주치의",
    "참조 이미지", "수신 거부", "이름 부르기", "이름: 미상", "성명 및 서명", "환자 유형별", "통증 정도는 환자가",
    # (sex/age) after a common word
    "정상(M/45)", "검사(3세)", "이상(5세)", "소아(5세)", "원장(M/50)",
    # a name-shaped word without any trigger is not reported
    "김민준이 왔다", "김민준 왔다", "이서연과 만남",
    # nouns ending in a derivational 이 are not "name + hypocoristic 이"
    "양손잡이가 많다", "고집쟁이는 아님", "하루살이가 날아다님", "가슴앓이가 심함", "소꿉놀이를 함",
    "3개월이나 지속", "인후염이 심함",
])
def test_korean_negative(text):
    assert ko(text) == []


@pytest.mark.parametrize("text, expected", [
    # hypocoristic 이 + particle after a full name ending in a final consonant
    ("선생님 정채원이가 어제부터 열이 나요", ["정채원"]),
    ("김민준이는 괜찮아요", ["김민준"]),
    ("남궁민준이랑 같이 왔어요", ["남궁민준"]),
    # KakaoTalk export senders (PC and mobile formats)
    ("[신유진] [오후 2:13] 원장님 안녕하세요", ["신유진"]),
    ("2024. 3. 15. 오후 2:13, 정하늘 : 선생님 진료 몇 시까지 하세요?", ["정하늘"]),
    # self-introduction, letter closing, relation in parentheses, birth year marker
    ("원장님 저 신유진입니다.", ["신유진"]),
    ("제 이름은 박지훈이에요", ["박지훈"]),
    ("김하윤 드림 | 내과", ["김하윤"]),
    ("보호자(딸) 문채원 010-3333-4444", ["문채원"]),
    ("설승현 (1946년생) 울산", ["설승현"]),
    ("윤명자 (1983.01.26생)", ["윤명자"]),
    # a personal mobile number right after the name
    ("외래 접수: 정아린 / 016-9616-2884", ["정아린"]),
    ("한혜원, 010.4666.3584", ["한혜원"]),
    # strong labels, including mail headers; a strong labelled field accepts 국가 + 은
    ("성명 홍길동  주민등록번호", ["홍길동"]),
    ("보낸사람: 김하윤 <a@example.com>", ["김하윤"]),
    ("받는 사람: 박서준", ["박서준"]),
    ("대표자 박태윤   전화", ["박태윤"]),
    ("신청인: 국가은   운전면허번호", ["국가은"]),
    ("성명 이하은   주민등록번호 071124-3483431", ["이하은"]),
    ("서하은 드림", ["서하은"]),
])
def test_korean_new_signals(text, expected):
    assert ko(text) == expected


@pytest.mark.parametrize("text", [
    # words next to these signals that are not names
    "[경과기록] [오후 2:13] 내원", "저는 정상입니다", "저는 조리사입니다", "저는 승무원이에요", "공지 드림",
    "자료를 드림", "소식을 드림", "안내도 드림", "성명 이하 생략", "어머니 010-1234-5678",
    "사무실 010-1234-5678", "원무과 010-1234-5678", "모바일 010-1234-5678", "보호자(배우자) 이해함", "보호자(딸) 설명 들음", "남동생 보호자 동행",
    "보호자: 남동생", "여동생 보호자", "고모부 님께서", "시누이 보호자", "어머니 보호자",
    # symptom / disease nouns after the weak label 환자
    "환자 피로감 지속", "환자 우울감 호소", "환자 이질감 호소", "환자 상실감 큼", "환자 공포감 호소",
    "환자 팽만감 호소", "환자 성취감", "환자 신경통 악화", "환자 대장암 수술", "환자 고지혈증",
])
def test_korean_new_signal_negatives(text):
    assert ko(text) == []


# --- dictionary and allowlist -----------------------------------------------

def test_dictionary_names_match_anywhere_with_boundaries():
    cfg = default_config()
    cfg.names = ["이도윤", "Jane Doe"]
    text = "오늘 이도윤이 왔다. 김이도윤 아님. 이도윤님께 연락. 이도윤한 일"
    spans = KO.find(text, cfg)
    assert [(s.text, s.start) for s in spans] == [("이도윤", 3), ("이도윤", text.index("이도윤님"))]
    assert {(s.score, s.source) for s in spans} == {(0.95, "dict")}
    # Latin dictionary entries are handled by the English recognizer
    assert en("Contact Jane Doe today; not Jane Doex.", cfg) == ["Jane Doe"]
    assert ko("Contact Jane Doe today", cfg) == []


def test_dictionary_beats_rules_score():
    cfg = default_config()
    cfg.names = ["홍길동"]
    spans = KO.find("홍길동 님", cfg)
    assert [s.score for s in spans] == [0.95]


def test_allowlist_suppresses():
    cfg = default_config()
    cfg.allowlist = ["홍길동", "John Smith"]
    assert ko("홍길동 님과 김민준 님", cfg) == ["김민준"]
    assert en("Dr. John Smith", cfg) == []


# --- English ----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Dr. John Smith", ["John Smith"]),
    ("seen by Dr Kim today", ["Kim"]),
    ("Mr. Kim", ["Kim"]),
    ("Mrs. O'Neil called", ["O'Neil"]),
    ("Prof. Park Jiwon", ["Park Jiwon"]),
    ("Patient: Jane Doe", ["Jane Doe"]),
    ("Name: Minjun Kim", ["Minjun Kim"]),
    ("Patient Name: KIM, MINJUN", ["KIM, MINJUN"]),
    ("Dr. Kim's office", ["Kim"]),
    ("Dr. Kim Hospital", ["Kim"]),
    ("seen by Dr. J. Smith today", ["J. Smith"]),
    ("성명: Minjun Kim", ["Minjun Kim"]),
    ("Patient: Sophie Miller, DOB 1985-03-02", ["Sophie Miller"]),
    ("Name: KIM, MINJUN, MRN 12345678", ["KIM, MINJUN"]),
])
def test_english_positive(text, expected):
    assert en(text) == expected


@pytest.mark.parametrize("text", [
    "Dr. Visit", "Dr. Monday", "Patient: Male", "Name: N/A", "Patient: Unknown", "The Doctor said",
    "Username: Admin", "Mr. ER", "Follow up with Dr. on Monday", "BP 120/80, HR 72",
])
def test_english_negative(text):
    assert en(text) == []


def test_english_span_metadata():
    text = "Referred by Dr. Jane Doe."
    sp = EN.find(text, default_config())[0]
    assert text[sp.start:sp.end] == "Jane Doe" and sp.entity == PERSON and sp.source == "names_en"
