"""End-to-end checks of deid.api.deidentify on realistic synthetic Korean/English documents.

Every person, number and address below is invented. Each case lists
  pii      - original strings that must not survive anywhere in the output,
  keep     - clinical content (labs, drugs, doses, diagnoses, codes, visit dates) that must survive,
  persons  - {name: mentions}: every mention of the name must become the same pseudonym token,
  counts   - entity counts the report must contain (a subset of the report).
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from deid import entities as E
from deid.api import deidentify
from deid.config import config_from_dict, default_config
from deid.engine import Detector
from deid.vault import Vault

BENCH = Path(__file__).resolve().parents[1] / "bench"


@dataclass
class Doc:
    id: str
    text: str
    pii: list[str]
    keep: list[str]
    persons: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


DOCS = [
    Doc(
        "progress_note",
        """[경과기록] 2024-03-15 내과 외래
환자명: 김민준 (M/45)   등록번호: 12345678
주민등록번호 850302-1234567 / 연락처 010-1234-5678
주소: 서울특별시 강남구 테헤란로 123, 4층 (역삼동)
S) 김민준 님은 3일 전부터 발열 38.5도, 기침, 누런 가래 호소.
O) BP 120/80 mmHg, HR 88회/분, BT 38.2℃, SpO2 97%
   WBC 12,300/μL, CRP 5.6 mg/dL, Hb 13.2 g/dL, PLT 250 x10^3/μL
A) J18.9 Pneumonia, unspecified
P) Augmentin 625mg 1T tid x 7일, 타이레놀 500mg prn
보호자 이서연(배우자, 010-9876-5432)에게 설명함. 김민준 환자 1주 후 재진 예정.
""",
        pii=["김민준", "12345678", "850302-1234567", "010-1234-5678", "테헤란로 123", "역삼동",
             "이서연", "010-9876-5432"],
        keep=["2024-03-15", "38.5도", "BP 120/80 mmHg", "HR 88회/분", "SpO2 97%", "WBC 12,300/μL",
              "CRP 5.6 mg/dL", "Hb 13.2 g/dL", "J18.9", "Augmentin 625mg 1T tid x 7일",
              "타이레놀 500mg prn", "서울특별시 강남구"],
        persons={"김민준": 3, "이서연": 1},
        counts={E.PERSON: 4, E.MRN: 1, E.RRN: 1, E.PHONE: 2, E.ADDRESS: 1, E.DATE: 1},
    ),
    Doc(
        "referral",
        """진료의뢰서
수신: 한빛대학교병원 호흡기내과 귀중
환자 성명: 박지훈   생년월일: 1978년 5월 12일   성별: 남
주소: 경기도 성남시 분당구 정자일로 95, 101동 1203호
상기 환자는 2023년 11월부터 COPD(J44.9)로 본원에서 치료 중이며 FEV1 58% (pred), FEV1/FVC 0.62로
감소하여 정밀검사 위해 의뢰드립니다. 박지훈 님의 흡연력은 30 pack-years 입니다.
현재 투약: Tiotropium 18mcg qd, Budesonide/Formoterol 160/4.5 2 puffs bid
담당의: 최영희 (내과 전문의)   Tel 031-123-4567   email: yhchoi@samplemed.co.kr
""",
        pii=["박지훈", "1978년 5월 12일", "정자일로 95", "101동 1203호", "최영희", "031-123-4567",
             "yhchoi@samplemed.co.kr"],
        keep=["J44.9", "FEV1 58%", "FEV1/FVC 0.62", "30 pack-years", "Tiotropium 18mcg qd",
              "Budesonide/Formoterol 160/4.5 2 puffs bid", "2023년 11월", "1978년생", "경기도 성남시 분당구"],
        persons={"박지훈": 2, "최영희": 1},
    ),
    Doc(
        "medical_opinion",
        """소 견 서
성명: 정수아      주민등록번호: 920815-2345678
주소: 대구광역시 수성구 범어동 123-45
병명: 1. 요추 추간판 탈출증 (M51.1)  2. 근막통증증후군 (M79.1)
소견: 상기 환자는 2024년 2월 3일 요통 및 좌측 하지 방사통으로 내원하였으며 L4-5 MRI 상
추간판 탈출 소견 보여 4주간의 안정 가료가 필요할 것으로 사료됨.
발행일: 2024.02.20   의료기관: 푸른정형외과의원 (전화 053-765-4321)
의사: 강동원 (면허번호 제12345호)
""",
        pii=["정수아", "920815-2345678", "범어동 123-45", "강동원", "053-765-4321"],
        keep=["M51.1", "M79.1", "요추 추간판 탈출증", "근막통증증후군", "2024년 2월 3일", "L4-5 MRI",
              "4주간", "2024.02.20", "대구광역시 수성구"],
        persons={"정수아": 1, "강동원": 1},
    ),
    Doc(
        "guardian_sms",
        """[Web발신]
[푸른소아청소년과] 윤하준 보호자님, 윤하준 어린이의 예방접종(DTaP 4차) 예약일은 2024년 4월 2일(화) 오전 10:30 입니다.
변경을 원하시면 02-345-6789 로 연락주세요. 수신거부 080-123-4567
문의: 담당 간호사 송미란 010-2468-1357
""",
        pii=["윤하준", "02-345-6789", "송미란", "010-2468-1357"],
        keep=["DTaP 4차", "2024년 4월 2일", "오전 10:30"],
        persons={"윤하준": 2, "송미란": 1},
    ),
    Doc(
        "kakaotalk",
        """[카카오톡] 2024년 3월 15일 금요일
[정하늘] [오후 2:13] 원장님 안녕하세요 저희 아버지 정민호 환자 검사결과 나왔나요?
[정하늘] [오후 2:14] 차량번호 12가 3456 으로 주차 등록 부탁드려요
[원장] [오후 2:20] 네 정민호님 HbA1c 7.2%, 공복혈당 145 mg/dL 입니다. 메트포르민 500mg bid 유지합니다.
[정하늘] [오후 2:21] 진료비는 국민은행 123-456-789012 예금주 정하늘로 보내면 될까요?
[원장] [오후 2:25] 네 정하늘님 확인했습니다. 다음 내원은 2024-04-12 입니다.
""",
        pii=["정하늘", "정민호", "12가 3456", "123-456-789012"],
        keep=["HbA1c 7.2%", "공복혈당 145 mg/dL", "메트포르민 500mg bid", "2024-04-12", "2024년 3월 15일"],
        persons={"정하늘": 5, "정민호": 2},
        counts={E.PERSON: 7, E.PLATE: 1, E.ACCOUNT: 1},
    ),
    Doc(
        "reception_memo",
        """접수 메모 (2024.05.07)
신환 이서진 (F/34) 010 5555 7777, 직장 02-2123-4567
주소 인천 남동구 구월동 1130 래미안 102동 1501호
건강보험증번호 1234567890 / 본인부담 30%
주소 변경 요청 있음. 이서진 씨 다음 주 화요일 내시경 예약 (EGD + CFS), 장정결제 처방 필요.
""",
        pii=["이서진", "010 5555 7777", "02-2123-4567", "구월동 1130", "102동 1501호", "1234567890"],
        keep=["2024.05.07", "30%", "EGD + CFS", "인천 남동구"],
        persons={"이서진": 2},
    ),
    Doc(
        "english_referral",
        """Referral Letter
Dear Dr. Michael Brown,
I am referring Mr. John Smith (DOB: 03/15/1962, MRN: A1234567) for evaluation of atrial
fibrillation (I48.91). He is on apixaban 5 mg bid and metoprolol succinate 25 mg qd.
Echo on 2024-01-10: EF 55%, LA 4.2 cm. TSH 2.1 mIU/L, Cr 1.1 mg/dL, eGFR 68.
Mr. Smith lives at 서울 마포구 월드컵북로 400, 3층.
Please contact me at +82 10-5555-1234 or jane.doe@hospital.example.com.
Sincerely,
Dr. Jane Doe
""",
        pii=["Michael Brown", "John Smith", "03/15/1962", "A1234567", "월드컵북로 400",
             "10-5555-1234", "jane.doe@hospital.example.com", "Jane Doe"],
        keep=["I48.91", "apixaban 5 mg bid", "metoprolol succinate 25 mg qd", "2024-01-10", "EF 55%",
              "LA 4.2 cm", "TSH 2.1 mIU/L", "Cr 1.1 mg/dL", "eGFR 68", "1962년생"],
        persons={"John Smith": 1, "Michael Brown": 1},
    ),
    Doc(
        "discharge_summary_mixed",
        """DISCHARGE SUMMARY / 퇴원요약지
Patient Name: 한지민 (F/67)   Hospital No: 20240123   DOB: 1957-08-21
Admission: 2024-02-01   Discharge: 2024-02-10   Attending: Dr. 오세훈
Dx) NSTEMI (I21.4), DM type 2 (E11.9), HTN (I10)
Hospital course: CAG on 2/2 showed LAD 90% stenosis -> PCI with DES 3.0 x 28 mm.
Peak troponin I 4.52 ng/mL, CK-MB 18.3 ng/mL. LDL 142 mg/dL -> atorvastatin 40mg.
Discharge Meds: aspirin 100mg qd, clopidogrel 75mg qd, atorvastatin 40mg qhs, metformin 1000mg bid
F/U: 2024-02-24 순환기내과 오세훈 교수 외래. 보호자(딸) 문채원 010-3333-4444
""",
        pii=["한지민", "20240123", "1957-08-21", "오세훈", "문채원", "010-3333-4444"],
        keep=["2024-02-01", "2024-02-10", "I21.4", "E11.9", "I10", "LAD 90%", "DES 3.0 x 28 mm",
              "troponin I 4.52 ng/mL", "CK-MB 18.3 ng/mL", "LDL 142 mg/dL", "aspirin 100mg qd",
              "clopidogrel 75mg qd", "atorvastatin 40mg qhs", "metformin 1000mg bid", "2024-02-24",
              "1957년생"],
        persons={"한지민": 1, "오세훈": 2},
    ),
    Doc(
        "lab_report",
        """검 사 결 과 보 고 서
성명: 배수현   등록번호: 00456789   생년월일: 1990.11.03   성별/나이: F/33
검체채취: 2024-03-11 08:40   보고일: 2024-03-11 14:20   의뢰과: 가정의학과
항목            결과      단위        참고치
WBC             6.8      x10^3/uL    4.0-10.0
Hb              11.2 L   g/dL        12.0-16.0
Platelet        312      x10^3/uL    150-450
AST/ALT         23/31    IU/L        0-40
Na/K/Cl         139/4.1/102 mmol/L   135-145/3.5-5.1/98-107
Glucose(F)      98       mg/dL       70-99
HbA1c           5.6      %           4.0-6.0
TSH             1.85     uIU/mL      0.35-4.94
Ferritin        8.2 L    ng/mL       13-150
Total chol      1500-1800 kcal 식이 권장
판독: 철결핍빈혈 의심 (D50.9). 담당의 서지원
""",
        pii=["배수현", "00456789", "1990.11.03", "서지원"],
        keep=["6.8", "4.0-10.0", "11.2 L", "12.0-16.0", "150-450", "23/31", "139/4.1/102",
              "135-145/3.5-5.1/98-107", "70-99", "5.6", "0.35-4.94", "8.2 L", "13-150",
              "1500-1800 kcal", "D50.9", "2024-03-11 08:40", "1990년생"],
        persons={"배수현": 1, "서지원": 1},
    ),
    Doc(
        "prescription",
        """처 방 전   (교부번호: 2024-0315-00123)
환자 성명 홍길동   주민등록번호 900101-1******
의료기관: 행복내과의원   전화 02-555-1234   사업자등록번호 220-81-62517
처방의: 남궁민수
처방 의약품
  아모디핀정 5mg    1정  1일 1회  30일
  리피토정 10mg     1정  1일 1회  30일
  글루코파지정 500mg 1정 1일 2회 30일
결제: 카드번호 4111-1111-1111-1111
""",
        pii=["홍길동", "900101-1******", "02-555-1234", "220-81-62517", "남궁민수", "4111-1111-1111-1111"],
        keep=["아모디핀정 5mg", "리피토정 10mg", "글루코파지정 500mg", "1일 1회", "1일 2회", "30일"],
        persons={"홍길동": 1, "남궁민수": 1},
        counts={E.PERSON: 2, E.RRN: 1, E.PHONE: 1, E.BRN: 1, E.CARD: 1},
    ),
    Doc(
        "consent_form",
        """수술 동의서
환자: 최유진 (여, 58세)   병록번호: 7788990
수술명: 복강경 담낭절제술 (Laparoscopic cholecystectomy)  진단: 담석증 (K80.2)
예정일: 2024년 6월 10일
수술 및 마취의 부작용(출혈, 감염, 담관 손상 1% 미만)에 대해 설명 들었음.
환자 서명: 최유진     보호자: 김태호 (남편)   보호자 연락처: 010-7777-8888
설명 의사: 이도현 교수
""",
        pii=["최유진", "7788990", "김태호", "010-7777-8888", "이도현"],
        keep=["복강경 담낭절제술", "Laparoscopic cholecystectomy", "K80.2", "2024년 6월 10일", "1% 미만"],
        persons={"최유진": 2},
    ),
    Doc(
        "nursing_record",
        """간호기록 2024-07-01
09:00 V/S BP 135/85, HR 76, RR 18, BT 36.8 checked. NRS 3점.
10:30 류승현 환자 수액 N/S 1L 80cc/hr 유지 중. I/O 1200/900 cc.
14:00 보호자 류지아님(딸) 면회, 연락처 010-1122-3344 확인.
16:00 류승현님 오심 호소하여 ondansetron 4mg IV 투여함. 주치의 장서윤 선생님께 보고.
""",
        pii=["류승현", "류지아", "010-1122-3344", "장서윤"],
        keep=["BP 135/85", "HR 76", "RR 18", "BT 36.8", "NRS 3점", "80cc/hr", "1200/900 cc",
              "ondansetron 4mg IV", "2024-07-01"],
        persons={"류승현": 2, "류지아": 1, "장서윤": 1},
    ),
    Doc(
        "foreign_patient",
        """외국인 환자 등록
Name: Nguyen Thi Lan   국적: 베트남   외국인등록번호: 950505-6123456
여권번호: M12345678   연락처: 010-4321-8765   이메일: lan.nguyen95@example.org
주소: 경기 안산시 단원구 원곡로 45 (원곡동)
주호소: 임신 12주, 입덧. hCG 85,000 mIU/mL, Hb 10.9 g/dL. 엽산 0.4mg qd 처방.
통역: Ms. Tran Mai (통역사)
""",
        pii=["Nguyen Thi Lan", "950505-6123456", "M12345678", "010-4321-8765",
             "lan.nguyen95@example.org", "원곡로 45", "Tran Mai"],
        keep=["임신 12주", "hCG 85,000 mIU/mL", "Hb 10.9 g/dL", "엽산 0.4mg qd", "베트남", "경기 안산시 단원구"],
        persons={"Nguyen Thi Lan": 1},
        counts={E.FRN: 1, E.PASSPORT: 1, E.EMAIL: 1, E.ADDRESS: 1},
    ),
    Doc(
        "email",
        """보낸사람: 김하윤 <hayoon.kim@clinic-sample.kr>
받는사람: 박서준 선생님 <sj.park@univ-hosp.example.ac.kr>
제목: 협진 문의 - 등록번호 55667788 환자
박서준 선생님께, 
등록번호 55667788 환자(72세 남) 2024-08-05 시행한 Chest CT에서 RUL 2.3cm 결절 (Lung-RADS 4B) 확인되어
협진 부탁드립니다. CEA 6.2 ng/mL, CYFRA 21-1 3.8 ng/mL.
김하윤 드림 | 내과 | 직통 02-3010-1234 | Fax 02-3010-5678
""",
        pii=["김하윤", "hayoon.kim@clinic-sample.kr", "박서준", "sj.park@univ-hosp.example.ac.kr",
             "55667788", "02-3010-1234", "02-3010-5678"],
        keep=["2024-08-05", "Chest CT", "RUL 2.3cm", "Lung-RADS 4B", "CEA 6.2 ng/mL", "CYFRA 21-1 3.8 ng/mL",
              "72세"],
        persons={"박서준": 2, "김하윤": 2},
    ),
    Doc(
        "er_record",
        """응급실 기록 2024-09-14 23:10
119 구급대 이송. 환자 신원: 조현우 (M/29), 주민번호 950101 1234567
교통사고(운전자), 차량 서울 34나 5678 승용차. 동승자 없음.
GCS 15, BP 100/60, HR 112, SpO2 95%. FAST negative. Hb 12.8, lactate 2.4 mmol/L.
X-ray: Rt. clavicle fx (S42.0). 경찰 담당 박정우 경위 010-9999-0000.
어머니 조미경 씨에게 연락함(자택 031-555-6666).
""",
        pii=["조현우", "950101 1234567", "34나 5678", "박정우", "010-9999-0000", "조미경", "031-555-6666"],
        keep=["2024-09-14 23:10", "GCS 15", "BP 100/60", "HR 112", "SpO2 95%", "lactate 2.4 mmol/L",
              "S42.0", "FAST negative"],
        persons={"조현우": 1, "조미경": 1},
    ),
    Doc(
        "psych_counsel",
        """정신건강의학과 상담기록
내담자: 임채윤 (여, 16세)   차트번호: P-20311
어머니 백은정 씨와 함께 내원. 학업 스트레스, 수면장애 (F51.0) 호소.
PHQ-9 14점, GAD-7 11점. Escitalopram 5mg qd 시작, 2주 후 재평가.
임채윤 학생은 자해 사고는 부인함. 비상연락: 백은정 010-6543-2109
""",
        pii=["임채윤", "P-20311", "백은정", "010-6543-2109"],
        keep=["F51.0", "PHQ-9 14점", "GAD-7 11점", "Escitalopram 5mg qd", "2주 후", "16세"],
        persons={"백은정": 2},
    ),
    Doc(
        "insurance_claim",
        """진단서 발급 신청서
신청인: 서민재 (본인)   운전면허번호: 서울 12-345678-90
건강보험증번호 9876543210   계좌: 신한은행 110-234-567890 (예금주 서민재)
진단명: 전방십자인대 파열 (S83.5), 수술일 2024.04.18, 입원 2024.04.17 ~ 2024.04.22 (6일)
""",
        pii=["서민재", "12-345678-90", "9876543210", "110-234-567890"],
        keep=["S83.5", "2024.04.18", "2024.04.17", "2024.04.22", "6일", "전방십자인대 파열"],
        persons={"서민재": 2},
        counts={E.DRIVER_LICENSE: 1, E.HEALTH_INSURANCE: 1, E.ACCOUNT: 1, E.PERSON: 2},
    ),
]


@pytest.fixture(scope="module")
def detector():
    return Detector(default_config())


def _run(doc: Doc, detector: Detector) -> tuple[str, Vault]:
    vault = Vault()
    out, _report = deidentify(doc.text, vault=vault, detector=detector)
    return out, vault


def test_enough_documents():
    assert len(DOCS) >= 15
    assert len({d.id for d in DOCS}) == len(DOCS)


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.id)
def test_no_pii_survives(doc: Doc, detector: Detector):
    out, _ = _run(doc, detector)
    leaked = [p for p in doc.pii if p in out]
    assert not leaked, f"leaked {leaked}\n{out}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.id)
def test_clinical_content_preserved(doc: Doc, detector: Detector):
    out, _ = _run(doc, detector)
    lost = [k for k in doc.keep if k not in out]
    assert not lost, f"lost {lost}\n{out}"


@pytest.mark.parametrize("doc", [d for d in DOCS if d.persons], ids=lambda d: d.id)
def test_same_person_same_token(doc: Doc, detector: Detector):
    out, vault = _run(doc, detector)
    for name, mentions in doc.persons.items():
        tokens = {t for t, orig in vault.tokens.items() if orig.replace(" ", "") == name.replace(" ", "")}
        assert len(tokens) == 1, f"{name}: tokens {tokens}\n{out}"
        assert out.count(tokens.pop()) == mentions, out


@pytest.mark.parametrize("doc", [d for d in DOCS if d.counts], ids=lambda d: d.id)
def test_report_counts(doc: Doc, detector: Detector):
    _, report = deidentify(doc.text, detector=detector)
    got = {ent: report.counts.get(ent, 0) for ent in doc.counts}
    assert got == doc.counts


def test_report_has_counts_only(detector: Detector):
    doc = DOCS[0]
    _, report = deidentify(doc.text, detector=detector)
    dumped = json.dumps(report.to_dict(), ensure_ascii=False)
    assert not [p for p in doc.pii if p in dumped]


def test_all_pseudo_round_trip_restores_original():
    """With every entity pseudonymized, the vault restores the document exactly."""
    cfg = config_from_dict({"actions": {ent: "pseudo" for ent in E.ALL}})
    det = Detector(cfg)
    for doc in DOCS:
        vault = Vault()
        out, _ = deidentify(doc.text, cfg, vault, detector=det)
        assert vault.restore(out) == doc.text, doc.id


def test_shared_vault_keeps_tokens_across_documents(detector: Detector):
    vault = Vault()
    out1, _ = deidentify("환자명: 김민준 (M/45) 등록번호: 12345678", vault=vault, detector=detector)
    out2, _ = deidentify("김민준 님 재진. 등록번호 12345678 확인. 보호자 이서연 님 동행.",
                         vault=vault, detector=detector)
    assert "[이름1]" in out1 and "[등록번호1]" in out1
    assert out2.startswith("[이름1] 님 재진. 등록번호 [등록번호1] 확인.")
    assert "[이름2]" in out2


def test_spaced_and_compact_korean_name_share_a_token(detector: Detector):
    vault = Vault()
    out, _ = deidentify("성명: 김 민준\n김민준 님 내원", vault=vault, detector=detector)
    assert out == "성명: [이름1]\n[이름1] 님 내원"


def test_detect_document_propagates_across_chunks(detector: Detector):
    chunks = ["이름: 박서준", "박서준이 3일 전부터 기침함.", "BP 120/80, 박서준과 상담"]
    spans = detector.detect_document(chunks)
    assert [[s.text for s in sp if s.entity == E.PERSON] for sp in spans] == [["박서준"]] * 3


def test_dictionary_catches_names_without_any_cue():
    cfg = default_config()
    cfg.names = ["이서준"]
    out, _ = deidentify("엄마 오늘 이서준 소아과 갔다왔어. 이서준 학교에 결석계 내야 함.", cfg)
    assert "이서준" not in out and out.count("[이름1]") == 2


def test_allowlist_keeps_named_institution_contacts():
    cfg = default_config()
    cfg.allowlist = ["02-555-1234", "최영희"]
    out, report = deidentify("담당의: 최영희  병원 대표 02-555-1234  환자 성명: 홍길동", cfg)
    assert "최영희" in out and "02-555-1234" in out and "홍길동" not in out
    assert report.counts == {E.PERSON: 1}


def test_disabled_entity_is_left_alone():
    cfg = default_config()
    cfg.enabled = set(E.ALL) - {E.PHONE}
    out, _ = deidentify("환자명: 홍길동 연락처 010-1234-5678", cfg)
    assert out == "환자명: [이름1] 연락처 010-1234-5678"


def test_custom_pattern_from_config():
    cfg = config_from_dict({"patterns": [{"entity": "STAFF_ID", "regex": r"EMP-\d{5}"}],
                            "labels": {"STAFF_ID": "직원번호"}})
    out, report = deidentify("간호사 사번 EMP-00123 확인", cfg)
    assert out == "간호사 사번 [직원번호] 확인" and report.counts == {"STAFF_ID": 1}


# ---------------------------------------------------------------------- benchmark guard


def _load_bench_module(name: str):
    spec = importlib.util.spec_from_file_location(f"deid_bench_{name}", BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _corpus() -> list[dict]:
    with open(BENCH / "corpus.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_bench_corpus_is_well_formed():
    samples = _corpus()
    assert len(samples) >= 120
    negatives = sum(1 for s in samples if not s["spans"])
    assert 0.15 <= negatives / len(samples) <= 0.3
    templates = {s["id"].split("-")[0] for s in samples}
    assert len(templates) >= 60  # diverse, not copies of one template
    for s in samples:
        for sp in s["spans"]:
            frag = s["text"][sp["start"]:sp["end"]]
            assert frag and frag == frag.strip() and sp["entity"] in E.ALL, (s["id"], sp)


def test_bench_corpus_is_reproducible(tmp_path):
    out = tmp_path / "corpus.jsonl"
    _load_bench_module("make_corpus").main(["--out", str(out)])
    assert out.read_text(encoding="utf-8") == (BENCH / "corpus.jsonl").read_text(encoding="utf-8")


def test_bench_overlap_scores_stay_high(detector: Detector):
    run_bench = _load_bench_module("run_bench")
    stats, _errors = run_bench.evaluate(_corpus(), detector)
    for ent, st in stats.items():
        p = st["ov_pred"] / st["pred"] if st["pred"] else 1.0
        r = st["ov_gold"] / st["gold"] if st["gold"] else 1.0
        assert p >= 0.95 and r >= 0.95, (ent, round(p, 3), round(r, 3))
