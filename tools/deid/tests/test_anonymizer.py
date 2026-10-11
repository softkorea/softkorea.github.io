"""Anonymizer actions, offsets, pseudonym consistency, and the deidentify() API."""
from __future__ import annotations

import datetime
import sys

import pytest

from deid import anonymizer as anon_mod
from deid import engine
from deid.anonymizer import Anonymizer, mask_value, parse_year_month
from deid.api import deidentify
from deid.config import Config
from deid.engine import Detector
from deid.recognizers.base import RegexRecognizer
from deid.types import Span
from deid.vault import Vault


def spans_for(text, *items):
    """items: (entity, value) located left to right in `text`."""
    out, pos = [], 0
    for entity, value in items:
        s = text.index(value, pos)
        out.append(Span(s, s + len(value), entity, value, 0.9, "test"))
        pos = s + len(value)
    return out


def cfg_with(**actions):
    cfg = Config()
    cfg.actions.update(actions)
    return cfg


# ---------------------------------------------------------------------- pseudo


def test_pseudo_tokens_numbered_per_entity_by_first_appearance():
    text = "이서연, 김민준, 이서연 / 등록번호 1234567, 김민준"
    spans = spans_for(text, ("PERSON", "이서연"), ("PERSON", "김민준"), ("PERSON", "이서연"),
                      ("MRN", "1234567"), ("PERSON", "김민준"))
    res = Anonymizer().anonymize(text, spans)
    assert res.text == "[이름1], [이름2], [이름1] / 등록번호 [등록번호1], [이름2]"
    assert [r.action for r in res.replacements] == ["pseudo"] * 5


def test_numbering_follows_text_order_even_if_spans_unsorted():
    text = "김민준 그리고 이서연"
    spans = list(reversed(spans_for(text, ("PERSON", "김민준"), ("PERSON", "이서연"))))
    assert Anonymizer().anonymize(text, spans).text == "[이름1] 그리고 [이름2]"


def test_pseudo_consistent_across_calls_and_shared_vault():
    vault = Vault()
    a, b = Anonymizer(vault=vault), Anonymizer(vault=vault)
    t1, t2 = "김민준 내원", "이서연, 김민준"
    assert a.anonymize(t1, spans_for(t1, ("PERSON", "김민준"))).text == "[이름1] 내원"
    out = b.anonymize(t2, spans_for(t2, ("PERSON", "이서연"), ("PERSON", "김민준"))).text
    assert out == "[이름2], [이름1]"


def test_separate_anonymizers_without_vault_are_independent():
    t = "이서연"
    Anonymizer().anonymize(t, spans_for(t, ("PERSON", "이서연")))
    assert Anonymizer().anonymize("김민준", spans_for("김민준", ("PERSON", "김민준"))).text == "[이름1]"


def test_pseudo_normalizes_numeric_ids_and_emails():
    cfg = cfg_with(PHONE="pseudo")
    text = "010-1234-5678 / 01012345678 / 010 1234 5678 / Minjun@Example.com / minjun@example.com"
    spans = spans_for(text, ("PHONE", "010-1234-5678"), ("PHONE", "01012345678"),
                      ("PHONE", "010 1234 5678"), ("EMAIL", "Minjun@Example.com"),
                      ("EMAIL", "minjun@example.com"))
    out = Anonymizer(cfg).anonymize(text, spans).text
    assert out == "[전화번호1] / [전화번호1] / [전화번호1] / [이메일1] / [이메일1]"


def test_custom_entity_uses_entity_name_as_label():
    cfg = cfg_with(STAFF_ID="pseudo")
    text = "사번 S-1001"
    assert Anonymizer(cfg).anonymize(text, spans_for(text, ("STAFF_ID", "S-1001"))).text == "사번 [STAFF_ID1]"


def test_entities_sharing_a_label_get_distinct_tokens():
    cfg = cfg_with(ACCOUNT="pseudo", MRN="pseudo")
    cfg.labels.update(ACCOUNT="번호", MRN="번호")
    text = "11112222 33334444"
    out = Anonymizer(cfg).anonymize(text, spans_for(text, ("MRN", "11112222"), ("ACCOUNT", "33334444")))
    assert out.text == "[번호1] [번호2]"


# ---------------------------------------------------------------------- mask


@pytest.mark.parametrize("entity,value,expected", [
    ("PERSON", "김민준", "김*준"),
    ("PERSON", "이수", "이*"),
    ("PERSON", "남궁민수", "남**수"),
    ("PERSON", "John Smith", "J*** S****"),
    ("PHONE", "010-1234-5678", "010-****-5678"),
    ("PHONE", "010.1234.5678", "010.****.5678"),
    ("PHONE", "01012345678", "010****5678"),
    ("PHONE", "02-123-4567", "02-***-4567"),
    ("PHONE", "0212345678", "02****5678"),
    ("PHONE", "(031) 123-4567", "(031) ***-4567"),
    ("PHONE", "+82 10-1234-5678", "+82 10-****-5678"),
    ("PHONE", "1588-1234", "1588-****"),
    ("RRN", "900101-1234567", "900101-1******"),
    ("RRN", "9001011234567", "9001011******"),
    ("RRN", "900101-*******", "900101-*******"),   # already masked: shape kept, nothing new shown
    ("FRN", "900101 5234561", "900101 5******"),
    ("FRN", "900101 5234567", "900101 5******"),
    ("CARD", "1234-5678-9012-3456", "****-****-****-3456"),
    ("CARD", "1234567890123456", "************3456"),
    ("EMAIL", "minjun@example.com", "m*****@example.com"),
    ("MRN", "12345678", "1******8"),
    ("ACCOUNT", "123-456-789012", "1**-***-*****2"),
    ("PASSPORT", "M12345678", "M*******8"),
])
def test_mask_shapes(entity, value, expected):
    assert mask_value(entity, value) == expected
    assert len(expected) == len(value)


def test_mask_action_records_replacement():
    cfg = cfg_with(PERSON="mask")
    text = "환자 김민준, 연락처 010-1234-5678"
    res = Anonymizer(cfg).anonymize(text, spans_for(text, ("PERSON", "김민준"), ("PHONE", "010-1234-5678")))
    assert res.text == "환자 김*준, 연락처 010-****-5678"
    assert [r.action for r in res.replacements] == ["mask", "mask"]


# ---------------------------------------------------------------------- redact / keep


def test_redact_and_keep_defaults():
    text = "주민번호 900101-1234567, 진료일 2024-03-02, https://example.com"
    spans = spans_for(text, ("RRN", "900101-1234567"), ("DATE", "2024-03-02"), ("URL", "https://example.com"))
    res = Anonymizer().anonymize(text, spans)
    assert res.text == "주민번호 [주민번호], 진료일 2024-03-02, https://example.com"
    assert [r.action for r in res.replacements] == ["redact", "keep", "keep"]
    assert res.replacements[1].replacement == "2024-03-02"


def test_unknown_action_raises():
    cfg = cfg_with(PERSON="shred")
    with pytest.raises(ValueError, match="shred"):
        Anonymizer(cfg).anonymize("김민준", spans_for("김민준", ("PERSON", "김민준")))


# ---------------------------------------------------------------------- generalize


def test_address_generalize_uses_prefix(monkeypatch):
    monkeypatch.setattr(anon_mod, "_address_prefix", lambda t: "서울특별시 강남구")
    text = "주소: 서울특별시 강남구 테헤란로 123, 101동 202호"
    value = "서울특별시 강남구 테헤란로 123, 101동 202호"
    res = Anonymizer().anonymize(text, spans_for(text, ("ADDRESS", value)))
    assert res.text == "주소: 서울특별시 강남구 [주소]"
    assert res.replacements[0].action == "generalize"


def test_address_generalize_without_prefix(monkeypatch):
    monkeypatch.setattr(anon_mod, "_address_prefix", lambda t: "")
    assert Anonymizer().replacement_for(Span(0, 5, "ADDRESS", "어딘가 1", 0.9, "t")) == ("[주소]", "generalize")


def test_address_module_missing_falls_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "deid.recognizers.address", None)
    assert anon_mod._address_prefix("서울특별시 강남구 테헤란로 1") == ""


@pytest.mark.parametrize("value,expected", [
    ("1985.03.02", "1985년생"),
    ("1985-03-02", "1985년생"),
    ("1985년 3월 2일", "1985년생"),
    ("19850302", "1985년생"),
    ("85.03.02", "1985년생"),
    ("850302", "1985년생"),
    ("85년생", "1985년생"),
    ("05.03.02", "2005년생"),
    ("30.01.01", "1930년생"),   # 30-99 -> 1900s
    ("03/02/1985", "1985년생"),
    ("15/03/1985", "1985년생"),
    ("March 15, 1962", "1962년생"),
    ("15 Mar 1962", "1962년생"),
    ("3월 2일", "[생년월일]"),
    ("12.25", "[생년월일]"),
])
def test_birthdate_generalize(value, expected):
    rep, action = Anonymizer().replacement_for(Span(0, len(value), "BIRTHDATE", value, 0.9, "t"))
    assert (rep, action) == (expected, "generalize")


def test_two_digit_birth_year_is_never_in_the_future():
    this_year = datetime.date.today().year
    yy = (this_year + 1) % 100
    if yy >= anon_mod.YY_PIVOT:
        pytest.skip("next year is already outside the 2000s pivot window")
    value = f"{yy:02d}.01.01"
    assert Anonymizer().replacement_for(Span(0, 8, "BIRTHDATE", value, 0.9, "t"))[0] == f"{1900 + yy}년생"
    # A visit date may lie in the future (next appointment), so DATE keeps the 2000s reading.
    assert parse_year_month(value) == (2000 + yy, 1)


@pytest.mark.parametrize("value,expected", [
    ("2024-03-02", "2024년 3월"),
    ("2024.12.31", "2024년 12월"),
    ("2024/3/2", "2024년 3월"),
    ("2024년 3월 2일", "2024년 3월"),
    ("24.03.02", "2024년 3월"),
    ("20240302", "2024년 3월"),
    ("3월 2일", "[날짜]"),
    ("2024.13.02", "[날짜]"),
    ("March 15, 2024", "2024년 3월"),
    ("15 Mar 2024", "2024년 3월"),
    ("25/12/2024", "2024년 12월"),
])
def test_date_generalize(value, expected):
    cfg = cfg_with(DATE="generalize")
    assert Anonymizer(cfg).replacement_for(Span(0, len(value), "DATE", value, 0.9, "t"))[0] == expected


def test_generalize_other_entities_falls_back_to_redact():
    cfg = cfg_with(RRN="generalize")
    assert Anonymizer(cfg).replacement_for(Span(0, 14, "RRN", "900101-1234567", 0.9, "t")) == ("[주민번호]", "redact")


def test_parse_year_month_rejects_garbage():
    assert parse_year_month("abc") == (None, None)
    assert parse_year_month("3500.01.01") == (None, 1)


# ---------------------------------------------------------------------- anonymize mechanics


def test_replacement_offsets_refer_to_original_text():
    text = "김민준(010-1234-5678) 내원, 이서연 동반"
    spans = spans_for(text, ("PERSON", "김민준"), ("PHONE", "010-1234-5678"), ("PERSON", "이서연"))
    res = Anonymizer().anonymize(text, spans)
    assert res.text == "[이름1](010-****-5678) 내원, [이름2] 동반"
    for r in res.replacements:
        assert text[r.start:r.end] == r.original
    assert [r.start for r in res.replacements] == sorted(r.start for r in res.replacements)


def test_adjacent_spans_are_fine():
    text = "김민준이서연"
    spans = [Span(0, 3, "PERSON", "김민준", 0.9, "t"), Span(3, 6, "PERSON", "이서연", 0.9, "t")]
    assert Anonymizer().anonymize(text, spans).text == "[이름1][이름2]"


def test_overlapping_spans_raise_and_issue_no_tokens():
    text = "김민준 환자"
    spans = [Span(0, 3, "PERSON", "김민준", 0.9, "t"), Span(1, 6, "PERSON", "민준 환자", 0.9, "t")]
    a = Anonymizer()
    with pytest.raises(ValueError, match="overlapping"):
        a.anonymize(text, spans)
    assert len(a.vault) == 0


def test_span_not_matching_text_raises():
    with pytest.raises(ValueError):
        Anonymizer().anonymize("김민준", [Span(0, 3, "PERSON", "홍길동", 0.9, "t")])
    with pytest.raises(ValueError):
        Anonymizer().anonymize("김민", [Span(0, 3, "PERSON", "김민준", 0.9, "t")])


def test_no_spans_returns_text_unchanged():
    res = Anonymizer().anonymize("특이사항 없음", [])
    assert res.text == "특이사항 없음" and res.replacements == []


# ---------------------------------------------------------------------- api.deidentify


def _fake_detector(cfg=None):
    recs = [RegexRecognizer("fake_names", "PERSON", r"([가-힣]{2,4})(?=님|씨)", 0.95),
            RegexRecognizer("fake_phone", "PHONE", r"01[0-9]-[0-9]{4}-[0-9]{4}", 0.9)]
    return Detector(cfg or Config(), recognizers=recs)


def test_deidentify_counts_and_restores(monkeypatch):
    monkeypatch.setattr(engine, "_default_recognizers", lambda cfg: _fake_detector(cfg).recognizers)
    vault = Vault()
    text = "김민준님 010-1234-5678. 김민준이 이서연씨와 왔다."
    out, report = deidentify(text, vault=vault)
    assert out == "[이름1]님 010-****-5678. [이름1]이 [이름2]씨와 왔다."
    assert report.counts == {"PERSON": 3, "PHONE": 1}
    assert "김민준" not in str(report.to_dict())
    assert vault.restore(out) == text.replace("010-1234-5678", "010-****-5678")


def test_deidentify_with_explicit_detector_and_shared_vault():
    det, vault = _fake_detector(), Vault()
    deidentify("이서연님", vault=vault, detector=det)
    out, report = deidentify("김민준님과 이서연님", vault=vault, detector=det)
    assert out == "[이름2]님과 [이름1]님"
    assert report.total == 2


def test_tl03_fullwidth_values_mask_and_generalize():
    from deid.anonymizer import generalize_value, mask_value
    assert mask_value("PHONE", "０１０－６６２１－３０９８") == "０１０－****－３０９８"
    assert generalize_value("BIRTHDATE", "１９８５．０３．１５", "생년월일") == ("1985년생", "generalize")
    assert generalize_value("BIRTHDATE", "1979.11.23生", "생년월일") == ("1979년생", "generalize")
