"""Regression tests for the completeness critic's engine / recognizer / config gaps.

All people, numbers and addresses below are invented.
"""
from __future__ import annotations

import pytest

from deid import entities as E
from deid.api import deidentify
from deid.config import default_config
from deid.engine import Detector, resolve_overlaps
from deid.types import Span


@pytest.fixture(scope="module")
def det() -> Detector:
    return Detector(default_config())


def run(text: str, det: Detector | None = None, cfg=None) -> str:
    if det is not None:
        return deidentify(text, det.cfg, detector=det)[0]
    return deidentify(text, cfg or default_config())[0]


def found(det: Detector, text: str) -> set[tuple[str, str]]:
    return {(sp.entity, sp.text) for sp in det.detect(text)}


# ----------------------------------------------------------------------------- (1) URLs


def test_gap_url_query_values_are_masked(det):
    url = "https://emr.example/p?name=표다은&rrn=7904052345678&tel=01023456789"
    out = run(url, det)
    for value in ("표다은", "7904052345678", "01023456789"):
        assert value not in out
    assert out.startswith("https://emr.example/p?name=")
    assert "&rrn=" in out and "&tel=" in out


def test_gap_url_hyphenated_phone_inside_url(det):
    out = run("링크 https://emr.example/p?tel=010-2345-6789 확인", det)
    assert "010-2345-6789" not in out
    assert out.startswith("링크 https://emr.example/p?tel=010-")


def test_gap_url_path_name_is_found_by_propagation(det):
    out = run("환자 표다은 님. 결과 https://example.org/환자/표다은/결과", det)
    assert "표다은" not in out
    assert "https://example.org/환자/" in out and "/결과" in out


def test_gap_url_ip_host_follows_ip_action(det):
    out = run("http://192.168.0.12/view?pid=00482917", det)
    assert "192.168.0.12" not in out and "00482917" not in out
    assert out.startswith("http://[IP]/view?pid=")
    # a single-digit-octet host is still an address when it is the host of a link
    assert "1.2.3.4" not in run("http://1.2.3.4/x 접속", det)
    cfg = default_config()
    cfg.actions[E.IP] = "keep"
    kept = run("http://192.168.0.12/view?pid=00482917", cfg=cfg)
    assert "192.168.0.12" in kept and "00482917" not in kept


def test_gap_url_param_key_variants(det):
    text = ("https://h.example/v?patient_name=김민준&PatientID=A00123&dob=19790405"
            "&email=minjun@example.com&jumin=790405-1234567&hp=01098765432")
    got = found(det, text)
    assert (E.PERSON, "김민준") in got
    assert (E.MRN, "A00123") in got
    assert (E.BIRTHDATE, "19790405") in got
    assert (E.EMAIL, "minjun@example.com") in got
    assert any(e == E.RRN and "790405" in t for e, t in got)
    assert (E.PHONE, "01098765432") in got
    out = run(text, det)
    for v in ("김민준", "A00123", "19790405", "minjun@", "1234567", "01098765432"):
        assert v not in out


def test_gap_url_same_result_with_url_disabled():
    text = "https://emr.example/p?name=표다은&rrn=7904052345678&tel=01023456789"
    cfg = default_config()
    cfg.enabled = set(E.ALL) - {E.URL}
    out = run(text, cfg=cfg)
    for value in ("표다은", "7904052345678", "01023456789"):
        assert value not in out


@pytest.mark.parametrize("url", [
    "https://www.naver.com",
    "https://blog.naver.com/someid/223456789012",
    "https://pubmed.ncbi.nlm.nih.gov/12345678/",
    "https://doi.org/10.1056/NEJMoa2034577",
    "https://www.google.com/search?q=당뇨병+치료&hl=ko",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10",
    "https://www.hira.or.kr/re/diag/getDiagList.do?pgmid=HIRAA030009000000&WT.gnb=약제",
    "https://www.mfds.go.kr/brd/m_99/view.do?seq=48123&srchFr=2024-01-01&srchTo=2024-03-31",
    "https://example.com/report_20240315.pdf",
    "https://www.law.go.kr/법령/의료법/제21조",
])
def test_gap_url_benign_links_unchanged(det, url):
    assert run(f"참고: {url} 확인", det) == f"참고: {url} 확인"


def test_gap_url_container_in_resolve_overlaps():
    url = Span(0, 40, E.URL, "x" * 40, 0.9, "url")
    phone = Span(10, 20, E.PHONE, "y" * 10, 0.6, "phone")
    date = Span(25, 35, E.DATE, "z" * 10, 0.85, "date")
    keep = lambda e: e in (E.URL, E.DATE)  # noqa: E731
    out = resolve_overlaps([url, phone, date], keep)
    # the URL opens for the phone; the DATE (also keep) stays as its own span
    assert [(s.entity, s.start) for s in out] == [(E.PHONE, 10), (E.DATE, 25)]
    # without is_keep the old score rule applies
    assert [s.entity for s in resolve_overlaps([url, phone, date])] == [E.URL]
    # a partial overlap is still decided by the score
    partial = Span(35, 45, E.PHONE, "w" * 10, 0.6, "phone")
    assert [s.entity for s in resolve_overlaps([url, partial], keep)] == [E.URL]


def test_gap_url_redact_action_is_not_a_container():
    cfg = default_config()
    cfg.actions[E.URL] = "redact"
    out = run("https://emr.example/p?tel=010-2345-6789", cfg=cfg)
    assert out == "[URL]"


# ----------------------------------------------------------------------------- (2) birth dates


@pytest.mark.parametrize("text, name, dob", [
    ("표다은(79.04.05, F) 내원", "표다은", "79.04.05"),
    ("표다은 (1979.04.05/F) 내원", "표다은", "1979.04.05"),
    ("환자 표다은 (1979-04-05, 여)", "표다은", "1979-04-05"),
    ("표다은 (여, 1979-04-05)", "표다은", "1979-04-05"),
    ("표다은(1979년 4월 5일, 여) 내원", "표다은", "1979년 4월 5일"),
    ("표다은(790405, F)", "표다은", "790405"),
])
def test_gap_birth_chart_header_name_date_sex(det, text, name, dob):
    got = found(det, text)
    assert (E.PERSON, name) in got
    assert (E.BIRTHDATE, dob) in got
    out = run(text, det)
    assert name not in out and dob not in out
    assert "1979년생" in out


@pytest.mark.parametrize("text, dob", [
    ("1979년 4월 5일 출생", "1979년 4월 5일"),
    ("1985-03-02 출생", "1985-03-02"),
    ("1979.04.05 생일", "1979.04.05"),
    ("1979.4.5에 태어남", "1979.4.5"),
    ("생 1979.4.5", "1979.4.5"),
    ("생: 1979.04.05", "1979.04.05"),
    ("b. 1979.4.5", "1979.4.5"),
    ("(1979.04.05, 47세)", "1979.04.05"),
])
def test_gap_birth_unlabelled_forms(det, text, dob):
    assert (E.BIRTHDATE, dob) in found(det, text)


def test_gap_birth_date_in_parentheses_after_a_person(det):
    assert (E.BIRTHDATE, "1979.04.05") in found(det, "환자 표다은(1979.04.05)")
    out = run("환자 표다은 (1979.04.05, #20934411) 내원", det)
    assert "표다은" not in out and "1979.04.05" not in out and "20934411" not in out
    # a visit written in the parentheses, or a future date, stays a DATE
    assert (E.DATE, "2024.03.15") in found(det, "환자 표다은(2024.03.15 내원)")
    assert (E.DATE, "2099.03.15") in found(det, "환자 표다은 (2099.03.15)")


@pytest.mark.parametrize("text, date", [
    ("수술력: 담낭절제술(2015.03.02, 45세)", "2015.03.02"),   # age at the operation
    ("김민준(2024.03.15 내원, 남)", "2024.03.15"),             # an event in the parentheses
    ("검사일(2024.03.15, 남)", "2024.03.15"),                  # a date label before them
    ("85년 생 2024.03.15 CT", "2024.03.15"),                   # 생 belongs to 85년
    ("출생 후 2024.03.20 BCG 접종", "2024.03.20"),
    ("(2099.01.02, F)", "2099.01.02"),                          # future
])
def test_gap_birth_visit_dates_stay_dates(det, text, date):
    assert (E.DATE, date) in found(det, text)


def test_gap_birth_name_before_date_sex_paren_is_person():
    from deid.recognizers.names import KoreanNameRecognizer
    rec, cfg = KoreanNameRecognizer(), default_config()
    for text in ("표다은(79.04.05, F) 내원", "표다은 (1979.04.05/F)", "표다은 (여, 1979-04-05)"):
        assert [sp.text for sp in rec.find(text, cfg)] == ["표다은"], text
    # a clinical word before such parentheses is still not a name
    assert rec.find("진료(2024.03.15, 남)", cfg) == []


# ----------------------------------------------------------------------------- (3) addresses


@pytest.mark.parametrize("text, leaks", [
    ("주소: 정자동 행복아파트 102-1501", ["정자동", "102-1501", "1501"]),
    ("주소: 합정동 123-4", ["합정동", "123-4"]),
    ("거주지: 용봉동 행복빌라 302호", ["용봉동", "302호"]),
    ("주소: 102-1501 행복아파트", ["102-1501", "행복아파트"]),
    ("주소지: 수완동 대방노블랜드 6차 608동 1904호요", ["수완동", "1904호"]),
    ("방문간호 주소는 저희집 101동 1203호에요", ["1203호"]),
])
def test_gap_address_after_label(det, text, leaks):
    out = run(text, det)
    for leak in leaks:
        assert leak not in out, (text, out)
    assert "[주소]" in out


def test_gap_address_label_generalizes_what_parses(det):
    out = run("간호사: 주소는 아직 광주 북구 용봉동 그대로세요?", det)
    assert "용봉동" not in out
    assert "광주 북구 [주소] 그대로세요?" in out
    assert run("주소: 서울 마포구 합정동 123-4", det) == "주소: 서울 마포구 [주소]"


@pytest.mark.parametrize("text", [
    "주소: 3일 전부터 두통", "주소: 운동 시 흉통", "주소 확인 필요", "주소: 수면 중 코골이",
    "주소: 오른쪽 다리 저림", "주소: 기침, 가래 2주", "주소: 체중 감소 5kg", "주소: 경기 2회, 발열",
    "주소 : 안면부 부종", "주소: 안면 마비", "주소: 이상행동 및 불면", "주소: 누우면 어지러움",
    "주소: 허리 통증 3일", "주소: 과거 1호 문의", "주소: 숨참 (계단 2층)", "주소: 5동 앞에서 넘어짐",
    "주소: 2호선 강남역 근처", "주소지 관할 보건소로 이관", "자택 방문 후 2일", "주소: 요로감염 의심",
    "주소(C.C.): 복통 3일", "현주소: 미상", "자택 전화 031-123-4567",
])
def test_gap_address_label_chief_complaint_is_not_an_address(det, text):
    assert not any(e == E.ADDRESS for e, _ in found(det, text)), text


@pytest.mark.parametrize("text, leak", [
    ("용봉동 행복빌라 302호 거주", "302호"),
    ("행복빌 201호로 이사", "201호"),
    ("한강맨션 1102호", "1102호"),
    ("행복아파트 102-1501 거주", "102-1501"),
    ("102-1501 행복아파트로 오세요", "102-1501"),
])
def test_gap_address_home_units_without_dong(det, text, leak):
    assert leak not in run(text, det)


@pytest.mark.parametrize("text", ["OO빌딩 302호 사무실", "본관 302호 병실", "71병동 3호실", "아파트 단지 2곳"])
def test_gap_address_offices_and_rooms_stay(det, text):
    assert not any(e == E.ADDRESS for e, _ in found(det, text)), text


def test_gap_address_postal_codes(det):
    assert "13561" not in run("우편번호 13561", det)
    assert "28591" not in run("송부지 확인 (우편번호 28591)", det)
    assert "06236" not in run("(우) 06236 서울", det)
    out = run("(06236) 서울특별시 강남구 테헤란로 152", det)
    assert out == "서울특별시 강남구 [주소]"
    # an unlabelled five-digit number is not a postal code
    assert run("검체번호 13561 확인", det) == "검체번호 13561 확인"


def test_gap_address_companion_word_is_not_a_dong(det):
    out = run("보호자 진다온 님 동행, 성남시 분당구 정자일로 95 101동 1203호", det)
    assert "동행, 성남시 분당구 [주소]" in out
    assert "1203호" not in out and "진다온" not in out
    out = run("보호자 진다온 님 동행, 경기도 성남시 분당구 정자일로 95", det)
    assert "님 동행, 경기도 성남시 분당구 [주소]" in out


# ----------------------------------------------------------------------------- (5) small issues


@pytest.mark.parametrize("text, initials", [
    ("환자 이니셜 K.M.J.", "K.M.J."),
    ("이니셜: KMJ, 45세 남", "KMJ"),
    ("Patient initials: K.M.", "K.M."),
])
def test_gap_initials_after_label_are_person(det, text, initials):
    got = found(det, text)
    assert (E.PERSON, initials) in got
    assert not any(t == "이니셜" for _, t in got)
    out = run(text, det)
    assert initials not in out
    assert "이니셜" in out or "initials" in out


def test_gap_initials_word_is_a_name_stopword():
    from deid.recognizers.names import STOPWORDS, KoreanNameRecognizer
    assert "이니셜" in STOPWORDS
    assert KoreanNameRecognizer().find("환자 이니셜 확인", default_config()) == []


@pytest.mark.parametrize("value, written, expected", [
    ("한지민", "[이름2]와 왔다", "한지민과 왔다"),
    ("김수아", "[이름2]과 왔다", "김수아와 왔다"),
    ("한지민", "[이름2]는", "한지민은"),
    ("김수아", "[이름2]은", "김수아는"),
    ("한지민", "[이름2]가 왔다", "한지민이 왔다"),
    ("김수아", "[이름2]이 왔다", "김수아가 왔다"),
    ("한지민", "[이름2]를", "한지민을"),
    ("김수아", "[이름2]을", "김수아를"),
    ("한지민", "[이름2]로", "한지민으로"),
    ("김수아", "[이름2]으로", "김수아로"),
    ("이하늘", "[이름2]으로", "이하늘로"),        # ㄹ takes 로
    ("한지민", "[이름2]랑", "한지민이랑"),
    ("김수아", "[이름2]이랑", "김수아랑"),
    ("한지민", "[이름2]야!", "한지민아!"),
    ("김수아", "[이름2]아,", "김수아야,"),
    ("010-1234-5678", "[이름2]로", "010-1234-5678로"),   # 팔 ends in ㄹ
    ("010-1234-5672", "[이름2]은", "010-1234-5672는"),   # 이 ends in a vowel
])
def test_gap_restore_fits_particles(value, written, expected):
    from deid.vault import Vault
    v = Vault()
    v.token_for("PERSON", "김민준", "이름")
    v.token_for("PERSON", value, "이름")
    assert v.restore(written) == expected


def test_gap_restore_particles_only_directly_after_a_token():
    from deid.vault import Vault
    v = Vault()
    v.token_for("PERSON", "한지민", "이름")
    assert v.restore("[이름1] 와 함께") == "한지민 와 함께"       # not directly after
    assert v.restore("[이름1]가요") == "한지민가요"                 # 가요 is a verb
    assert v.restore("[이름1]이에요") == "한지민이에요"
    assert v.restore("사과와 배") == "사과와 배"                    # no token at all
    assert v.restore("[이름1]와", particles=False) == "한지민와"
    v.token_for("PERSON", "John", "이름")
    assert v.restore("[이름2]와") == "John와"                       # unknown sound: left as written
    # tokens kept inside links are not restored, so their particles stay too
    assert v.restore("https://x.example/[이름1]와", links=False) == "https://x.example/[이름1]와"


@pytest.mark.parametrize("text, number", [
    ("사업자등록번호 123-45-67890", "123-45-67890"),
    ("사업자 번호: 1234567890", "1234567890"),
    ("BRN: 123-45-67890", "123-45-67890"),
])
def test_gap_brn_with_label_and_bad_checksum(det, text, number):
    spans = [sp for sp in det.detect(text) if sp.entity == E.BRN]
    assert [sp.text for sp in spans] == [number]
    assert spans[0].score == pytest.approx(0.85)
    assert number not in run(text, det)


def test_gap_brn_bad_checksum_without_label_is_kept(det):
    assert not any(e == E.BRN for e, _ in found(det, "번호 123-45-67890"))


@pytest.mark.parametrize("text, date", [
    ("다음 외래 (2024.03.15 F/U)", "2024.03.15"),
    ("환자 표다은 (2024.03.15, F/U)", "2024.03.15"),
    ("(2024.04.01, 재진 예정, 남)", "2024.04.01"),
])
def test_gap_birth_follow_up_dates_stay_dates(det, text, date):
    assert (E.DATE, date) in found(det, text)


@pytest.mark.parametrize("url", [
    "https://x.example/download?name=검사결과.pdf",
    "https://x.example/p?name=item123&contact=us",
    "https://x.example/p?phone=true&pid=abc&email=none",
])
def test_gap_url_params_need_value_shapes(det, url):
    assert run(url, det) == url


def test_gap_url_param_encoded_and_latin_names(det):
    assert "John+Smith" not in run("https://x.example/p?name=John+Smith", det)
    enc = "%ED%91%9C%EB%8B%A4%EC%9D%80"
    assert enc not in run(f"https://x.example/p?name={enc}", det)


def test_gap_spoken_phone_mixed_digits():
    from deid import deidentify
    # Masked like the numeric form: the middle group is hidden, only the last 4 digits stay.
    for text in ["연락처 공일공-4321-8765 입니다", "공일공 4321 8765로 전화", "영일영에 사삼이일에 8765"]:
        out, _ = deidentify(text)
        assert "4321" not in out and "사삼이일" not in out, out
    out, _ = deidentify("공일공-4321-8765 아니 010)4321-8765요")
    assert "4321" not in out, out
    out, _ = deidentify("공일공 회의는 3시")
    assert out == "공일공 회의는 3시"
