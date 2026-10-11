"""Detector: filtering, overlap resolution, propagation. Uses small fake recognizers only."""
from __future__ import annotations

import re
import sys
import time

import pytest

from deid import engine
from deid.config import Config
from deid.engine import PROPAGATE_SCORE, PROPAGATE_SOURCE, Detector, resolve_overlaps
from deid.recognizers.base import RegexRecognizer
from deid.types import Span


class StaticRecognizer:
    """Returns pre-built spans (located by value) regardless of the input text."""

    def __init__(self, items, name="static"):
        self.name = name
        self.items = items  # (entity, value, score, occurrence index)
        self.entities = tuple({e for e, *_ in items})

    def find(self, text, cfg):
        out = []
        for entity, value, score, nth in self.items:
            starts = [m.start() for m in re.finditer(re.escape(value), text)]
            if nth < len(starts):
                s = starts[nth]
                out.append(Span(s, s + len(value), entity, value, score, self.name))
        return out


def honorific_names(score=0.95):
    """Korean name directly followed by 님/씨/환자 (span excludes the honorific)."""
    return RegexRecognizer("fake_names", "PERSON", r"([가-힣]{2,4})(?=님|씨|\s?환자)", score)


def mrn_with_context():
    return RegexRecognizer("fake_mrn", "MRN", r"등록번호\s*:?\s*(?P<pii>[0-9]{4,10})", 0.9)


def texts(spans):
    return [(s.entity, s.text) for s in spans]


def det(*recs, cfg=None):
    return Detector(cfg or Config(), recognizers=list(recs))


# ---------------------------------------------------------------------- filtering


def test_disabled_entity_allowlist_and_min_score_are_filtered():
    rec = StaticRecognizer([
        ("PERSON", "김민준", 0.9, 0),
        ("PHONE", "010-1234-5678", 0.9, 0),
        ("MRN", "1234567", 0.4, 0),
        ("PERSON", "정상", 0.9, 0),
    ])
    cfg = Config(enabled={"PERSON", "MRN"}, allowlist=["정상"])
    text = "김민준 010-1234-5678 등록 1234567 결과 정상"
    assert texts(det(rec, cfg=cfg).detect(text)) == [("PERSON", "김민준")]


def test_allowlist_is_exact_match_only():
    rec = StaticRecognizer([("PERSON", "김민준", 0.9, 0)])
    cfg = Config(allowlist=["김민"])
    assert texts(det(rec, cfg=cfg).detect("김민준 환자")) == [("PERSON", "김민준")]


def test_min_score_boundary_is_inclusive():
    rec = StaticRecognizer([("MRN", "123456", 0.5, 0)])
    assert len(det(rec).detect("ID 123456")) == 1


def test_empty_text_and_no_recognizers():
    assert det(honorific_names()).detect("") == []
    assert det().detect("김민준님") == []
    assert det().detect_document([]) == []


def test_extra_recognizers_are_appended():
    d = Detector(Config(), recognizers=[honorific_names()], extra_recognizers=[mrn_with_context()])
    assert texts(d.detect("김민준님 등록번호 12345678")) == [("PERSON", "김민준"), ("MRN", "12345678")]


def test_mismatched_recognizer_span_fails_closed():
    class Broken:
        name, entities = "broken", ("PERSON",)

        def find(self, text, cfg):
            return [Span(0, 3, "PERSON", "홍길동", 0.9, "broken")]

    with pytest.raises(ValueError, match="broken"):
        det(Broken()).detect("김민준 내원")


# ---------------------------------------------------------------------- overlaps


def S(start, end, entity="X", score=0.9, text=None):
    return Span(start, end, entity, text or "x" * (end - start), score, "t")


def test_overlap_higher_score_wins():
    out = resolve_overlaps([S(0, 10, "ADDRESS", 0.7), S(5, 8, "PERSON", 0.9)])
    assert [(s.start, s.end) for s in out] == [(5, 8)]


def test_overlap_tie_score_longer_wins():
    out = resolve_overlaps([S(2, 5, score=0.8), S(0, 8, score=0.8)])
    assert [(s.start, s.end) for s in out] == [(0, 8)]


def test_overlap_tie_score_and_length_earlier_wins():
    out = resolve_overlaps([S(3, 7), S(1, 5)])
    assert [(s.start, s.end) for s in out] == [(1, 5)]


def test_overlap_result_sorted_and_non_overlapping():
    spans = [S(20, 25), S(0, 4), S(3, 6, score=0.95), S(10, 12), S(11, 30, score=0.5)]
    out = resolve_overlaps(spans)
    assert [(s.start, s.end) for s in out] == [(3, 6), (10, 12), (20, 25)]
    assert all(a.end <= b.start for a, b in zip(out, out[1:]))


def test_touching_spans_do_not_overlap():
    out = resolve_overlaps([S(0, 3), S(3, 6)])
    assert len(out) == 2


def test_detect_resolves_overlaps_between_recognizers():
    rec = StaticRecognizer([("ADDRESS", "강남구 김민준로 12", 0.7, 0), ("PERSON", "김민준", 0.9, 0)])
    assert texts(det(rec).detect("강남구 김민준로 12")) == [("PERSON", "김민준")]


# ---------------------------------------------------------------------- propagation: Korean names


def test_name_found_via_honorific_propagates_with_particles():
    text = "김민준님 내원. 김민준이 왔다. 김민준씨가 말함. 보호자는 (김민준). 김민준, 김민준"
    spans = det(honorific_names()).detect(text)
    assert all(s.text == "김민준" for s in spans)
    assert len(spans) == 6
    prop = [s for s in spans if s.source == PROPAGATE_SOURCE]
    assert len(prop) == 4 and all(s.score == PROPAGATE_SCORE for s in prop)


@pytest.mark.parametrize("text", [
    "김민준님. 김민준호 학생",    # different (longer) name
    "김민준님. 박김민준 씨",       # glued on the left
    "김민준님. 김민준2 계정",      # glued digit
    "김민준님. 김민준호텔",
])
def test_name_not_propagated_inside_longer_word(text):
    spans = det(honorific_names()).detect(text)
    assert [s.start for s in spans] == [0]


def test_longer_known_name_wins_over_prefix():
    text = "김민준님, 김민준호님. 김민준호가 왔고 김민준이 갔다."
    spans = det(honorific_names()).detect(text)
    assert texts(spans) == [("PERSON", "김민준"), ("PERSON", "김민준호"),
                            ("PERSON", "김민준호"), ("PERSON", "김민준")]


def test_name_propagates_next_to_underscore_and_punctuation():
    text = "이서연님\n파일: 이서연_검사결과.xlsx / '이서연'"
    assert len(det(honorific_names()).detect(text)) == 3


def test_two_syllable_names_need_a_clean_suffix():
    d = det(honorific_names())
    assert texts(d.detect("이수씨 내원. 이수가 왔다. 이수님께서 말함.")) == [("PERSON", "이수")] * 3
    starts = [s.start for s in d.detect("이수씨 내원. 이수정 확인. 이수술 예정.")]
    assert starts == [0]


def test_ordinary_clinical_words_untouched_by_propagation():
    text = "김민준님 정상 소견. 고혈압, 이상 없음. Hb 12.3 g/dL, Cr 0.9"
    assert texts(det(honorific_names()).detect(text)) == [("PERSON", "김민준")]


def test_single_character_values_are_not_propagated():
    d = det(StaticRecognizer([("PERSON", "김", 0.9, 0)]))
    assert len(d.detect("김 김치 김")) == 1


# ---------------------------------------------------------------------- propagation: other entities


def test_english_name_uses_ascii_boundaries():
    rec = RegexRecognizer("fake_en", "PERSON", r"Mr\. (?P<pii>[A-Z][a-z]+ [A-Z][a-z]+)", 0.9)
    text = "Mr. John Smith came. John Smith's chart. John Smithson no. John Smith의 기록"
    spans = det(rec).detect(text)
    assert [s.start for s in spans] == [4, text.index("John Smith's"), text.index("John Smith의")]


def test_numeric_ids_propagate_with_digit_boundaries():
    text = "등록번호: 12345678\n재방문 12345678, 912345678, 123456789, 12345678-9, 1.12345678"
    spans = det(mrn_with_context()).detect(text)
    assert [s.start for s in spans] == [6, text.index("12345678,")]


def test_short_numeric_values_are_not_propagated():
    text = "등록번호 2024\n2024년 3월 내원, 수치 2024"
    assert [s.text for s in det(mrn_with_context()).detect(text)] == ["2024"]


def test_phone_propagation():
    rec = StaticRecognizer([("PHONE", "010-1234-5678", 0.9, 0)])
    text = "연락처 010-1234-5678 / 010-1234-5678로 연락 / 010-1234-56789"
    spans = det(rec).detect(text)
    assert [s.start for s in spans] == [4, 20]


def test_email_propagation_is_case_insensitive_with_boundaries():
    rec = StaticRecognizer([("EMAIL", "minjun@example.com", 0.9, 0)])
    text = "minjun@example.com, MinJun@Example.com 및 xminjun@example.com, minjun@example.com.kr"
    spans = det(rec).detect(text)
    assert texts(spans) == [("EMAIL", "minjun@example.com"), ("EMAIL", "MinJun@Example.com")]


def test_non_propagating_entities_stay_local():
    rec = StaticRecognizer([("ADDRESS", "강남구 테헤란로 1", 0.9, 0)])
    assert len(det(rec).detect("강남구 테헤란로 1, 다시 강남구 테헤란로 1")) == 1


def test_propagation_does_not_displace_other_entities():
    rec = StaticRecognizer([("PERSON", "홍길동", 0.95, 0), ("ADDRESS", "홍길동로 12", 0.7, 0)])
    text = "홍길동 환자, 주소 홍길동로 12"
    assert texts(det(rec).detect(text)) == [("PERSON", "홍길동"), ("ADDRESS", "홍길동로 12")]


def test_propagation_extends_a_partial_same_entity_span():
    rec = StaticRecognizer([("PERSON", "김민준", 0.95, 0), ("PERSON", "민준", 0.6, 1)])
    spans = det(rec).detect("김민준님. 오늘 김민준이 왔다")
    assert texts(spans) == [("PERSON", "김민준"), ("PERSON", "김민준")]
    assert spans[1].source == PROPAGATE_SOURCE


def test_detect_does_not_carry_values_between_calls():
    d = det(honorific_names())
    assert len(d.detect("김민준님 내원")) == 1
    assert d.detect("김민준이 왔다") == []


# ---------------------------------------------------------------------- known values and documents


def test_add_known_values_are_found_everywhere():
    d = det()
    d.add_known("PERSON", " 이서연 ")
    d.add_known("MRN", "A1234567")
    d.add_known("PERSON", "이")       # too short: ignored
    spans = d.detect("이서연이 내원, 차트 A1234567 확인. 이 환자")
    assert texts(spans) == [("PERSON", "이서연"), ("MRN", "A1234567")]
    assert d.detect("다른 파일의 이서연")[0].text == "이서연"


def test_add_known_respects_enabled_and_allowlist():
    d = det(cfg=Config(enabled={"MRN"}, allowlist=["홍길동"]))
    d.add_known("PERSON", "김민준")
    d.add_known("MRN", "홍길동")
    assert d.detect("김민준 홍길동") == []


def test_config_names_dictionary_propagates():
    d = det(cfg=Config(names=["홍길동", "Jane Doe"]))
    assert texts(d.detect("홍길동은 Jane Doe와 상담")) == [("PERSON", "홍길동"), ("PERSON", "Jane Doe")]


def test_detect_document_shares_values_across_chunks():
    d = det(honorific_names(), mrn_with_context())
    chunks = ["이름", "김민준 환자", "등록번호 12345678", "김민준이 12345678 재방문", "무관한 셀"]
    out = d.detect_document(chunks)
    assert len(out) == len(chunks)
    assert texts(out[3]) == [("PERSON", "김민준"), ("MRN", "12345678")]
    assert all(s.source == PROPAGATE_SOURCE for s in out[3])
    assert out[0] == [] and out[4] == []


def test_detect_document_spans_index_their_own_chunk():
    d = det(honorific_names())
    chunks = ["김민준님", "보호자 김민준", "", "김민준"]
    for chunk, spans in zip(chunks, d.detect_document(chunks)):
        assert all(chunk[s.start:s.end] == s.text for s in spans)


def test_many_known_values_stay_fast():
    d = det()
    surnames = "김이박최정강조윤장임"
    given = ["민준", "서연", "도윤", "하은", "지호", "수아", "예준", "지우", "시우", "서윤"]
    for i in range(2000):
        d.add_known("PERSON", f"{surnames[i % 10]}{given[(i // 10) % 10]}{chr(0xAC00 + i)}")
    text = "이상 없음, 김치 섭취, 박동 정상, 혈압 120/80, Hb 12.3. " * 2000
    t0 = time.perf_counter()
    d.detect(text)
    assert time.perf_counter() - t0 < 5


# ---------------------------------------------------------------------- defaults


def test_default_recognizers_missing_module_gives_clear_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "deid.recognizers.address", None)
    with pytest.raises(ImportError, match="recognizers=\\[...\\]"):
        Detector(Config())


def test_default_recognizers_used_when_none_given(monkeypatch):
    monkeypatch.setattr(engine, "_default_recognizers", lambda cfg: [honorific_names()])
    assert texts(Detector().detect("김민준님")) == [("PERSON", "김민준")]


# ---------------------------------------------------------------------- red-team regressions


def _deid(text):
    from deid import deidentify
    return deidentify(text)[0]


@pytest.mark.parametrize("text, secret", [
    ("주민번호 ９４０３０５－２０４８１１７", "２０４８１１７"),
    ("주민번호 940305－2048117", "2048117"),
    ("연락처 ０１０－６６２１－３０９８", "６６２１－３０９８"),
    ("등록번호 ２０４１９８７３", "２０４１９８７３"),
    ("생년월일 １９８５．０３．１５", "０３．１５"),
    ("이메일 ｈａｎａ＠ｎａｖｅｒ．ｃｏｍ", "ｈａｎａ"),
    ("아빠 주민번호 940305ㅡ2048117 맞지?", "2048117"),
])
def test_tl03_fullwidth_and_dash_lookalikes_are_detected(text, secret):
    out = _deid(text)
    assert secret not in out
    assert "생년월일" not in text or "1985년생" in out


def test_tl03_spans_index_the_original_text():
    text = "연락처 ０１０－６６２１－３０９８ 입니다"
    spans = Detector().detect(text)
    assert [(s.entity, s.text) for s in spans] == [("PHONE", "０１０－６６２１－３０９８")]
    assert all(text[s.start:s.end] == s.text for s in spans)


def test_mac_nbsp_separator_leak_unicode_spaces_fold_to_space():
    for sp in (" ", " ", "　"):
        for form in ("환자명:{s}최유나 내원", "최유나{s}님 진료 예약", "환자{s}최유나 외래 방문"):
            text = form.format(s=sp)
            spans = [s for s in Detector().detect(text) if s.entity == "PERSON"]
            assert [s.text for s in spans] == ["최유나"], text


def test_security_nfd_hangul_bypass_text_is_detected_and_offsets_map_back():
    import unicodedata
    text = unicodedata.normalize("NFD", "환자 김민서 님, 주소 서울특별시 강남구 테헤란로 123, 연락처 010-2345-6789")
    spans = Detector().detect(text)
    got = {(s.entity, unicodedata.normalize("NFC", s.text)) for s in spans}
    assert ("PERSON", "김민서") in got and ("PHONE", "010-2345-6789") in got
    assert any(s.entity == "ADDRESS" for s in spans)
    assert all(text[s.start:s.end] == s.text for s in spans)
    out = unicodedata.normalize("NFC", _deid(text))
    assert "김민서" not in out and "테헤란로 123" not in out


def test_mac_nfd_hangul_leak_label_and_filename_style_text():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "성명: 정하람")
    assert "정하람" not in unicodedata.normalize("NFC", _deid(nfd))
    probe = unicodedata.normalize("NFD", "환자 정하람 진료기록")
    spans = Detector().detect(probe)
    assert [unicodedata.normalize("NFC", s.text) for s in spans] == ["정하람"]


def test_tl06_given_name_propagation_after_full_name():
    out = _deid("환자 김민준 (M/6) 내원. 민준이가 어제부터 열이 나서 왔어요. 민준이는 약을 잘 먹어요.")
    assert "민준" not in out
    out = _deid("성명: 박하준 (M/4)\n하준이 엄마 말로는 밤새 기침했다고 함")
    assert "하준" not in out
    # the given name alone (no hypocoristic suffix) and inside other words is left alone
    d = det(honorific_names())
    spans = d.detect("김민준님 내원. 민준 왔다. 이민준호 확인.")
    assert [s.text for s in spans] == ["김민준"]


def test_tl06_given_name_propagation_skips_large_name_lists():
    d = det()
    for i in range(engine.MAX_GIVEN_NAME_SOURCES + 1):
        d.add_known("PERSON", f"김{chr(0xAC00 + i)}{chr(0xAC01 + i)}")
    d.add_known("PERSON", "이지원")
    assert d.detect("지원이 필요함") == []


def test_over_mask_14_known_values_matcher_is_fast():
    d = det()
    for i in range(20000):
        d.add_known("PERSON", f"{'김이박최정'[i % 5]}{chr(0xAC00 + i % 9000)}{chr(0xAC00 + i // 9000)}")
        d.add_known("PHONE", f"010-{2000 + i // 10:04d}-{1000 + i % 9000:04d}")
        d.add_known("MRN", str(10000000 + i))
    t0 = time.perf_counter()
    spans = d.detect("혈압 조절 양호, 3개월 후 재검 예정. 010-2000-1001 / 10000001")
    assert time.perf_counter() - t0 < 2
    assert [s.text for s in spans] == ["010-2000-1001", "10000001"]


def test_clear_known_and_per_call_known_values():
    d = det()
    d.add_known("PERSON", "이서연")
    assert [s.text for s in d.detect("이서연 내원")] == ["이서연"]
    d.clear_known()
    assert d.detect("이서연 내원") == []
    assert [s.text for s in d.detect("이서연 내원", known={"PERSON": ["이서연"]})] == ["이서연"]
    out = d.detect_document(["홍길동 내원", "12345678 확인"], known=[("PERSON", "홍길동"), ("MRN", "12345678")])
    assert [[s.entity for s in spans] for spans in out] == [["PERSON"], ["MRN"]]
    assert d.detect("홍길동 내원") == []   # per-call values are not kept


def test_propagation_upgrades_keep_span_to_known_entity():
    rec = RegexRecognizer("fake_date", "DATE", r"\d{4}-\d{2}-\d{2}", 0.85)
    d = det(rec)
    d.add_known("BIRTHDATE", "1985-03-02")
    spans = d.detect("메모: 1985-03-02 생일 축하, 내원 2024-05-01")
    assert [(s.entity, s.text) for s in spans] == [("BIRTHDATE", "1985-03-02"), ("DATE", "2024-05-01")]
    # never the other way round: a known value whose action is "keep" does not take over
    d2 = det(StaticRecognizer([("PHONE", "010-1234-5678", 0.9, 0)]))
    d2.add_known("DATE", "010-1234-5678")
    assert [s.entity for s in d2.detect("010-1234-5678")] == ["PHONE"]
