"""bench/score_values.py: value-level leak score (leak, digit rule, keep, spurious, LLM counters).

All people, numbers and places below are invented.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

from deid.config import default_config
from deid.engine import Detector
from deid.types import Span

BENCH = Path(__file__).resolve().parent.parent / "bench"


def _load():
    spec = importlib.util.spec_from_file_location("bench_score_values", BENCH / "score_values.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = _load()

# Three documents whose fate under the default config does not depend on fine detection details:
#   d1  a labelled name and phone (protected), "청포도" (a plain word, always leaked verbatim),
#       a duplicate gold value (counted once) and a gold value absent from the text (skipped)
#   d2  "최다인 135-792": the name is replaced but the 6 digits stay -> leaked by the digit rule
#   d3  an unlabelled-in-gold phone number is masked -> one spurious region and a damaged keep
DOCS = [
    {"id": "d1", "genre": "test", "lang": "ko",
     "text": "환자명: 김민준\n연락처: 010-2345-6789\n메모: 청포도 사탕을 좋아함\n처방: 아목시실린 500mg tid",
     "pii": [{"value": "김민준", "entity": "PERSON"}, {"value": "010-2345-6789", "entity": "PHONE"},
             {"value": "청포도", "entity": "OTHER"}, {"value": "김민준", "entity": "PERSON"},
             {"value": "홍길순", "entity": "PERSON"}],
     "keep": ["아목시실린 500mg tid", "본문에 없는 문자열"]},
    {"id": "d2", "genre": "test", "lang": "ko",
     "text": "환자명: 최다인\n사물함 번호 최다인 135-792 사용",
     "pii": [{"value": "최다인", "entity": "PERSON"}, {"value": "최다인 135-792", "entity": "OTHER"}],
     "keep": ["사물함 번호"]},
    {"id": "d3", "genre": "test", "lang": "mixed",
     "text": "환자명: 이서준\n보호자 연락처 010-3456-7890\n체온 37.8도",
     "pii": [{"value": "이서준", "entity": "PERSON"}],
     "keep": ["보호자 연락처 010-3456-7890", "체온 37.8도"]},
]


def _write(path: Path, docs: list[dict]) -> Path:
    path.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs), encoding="utf-8")
    return path


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    return _write(tmp_path / "three.jsonl", DOCS)


# ------------------------------------------------------------------------------- metric math


def test_leak_kind_verbatim_and_digit_rule():
    assert sv.leak_kind("청포도", "메모: 청포도 사탕") == "verbatim"
    # re-spaced / re-dotted numbers still leak (>= 6 digits)
    assert sv.leak_kind("010-4827-3391", "call 010 4827 3391 now") == "digits"
    assert sv.leak_kind("010-4827-3391", "010.4827.3391") == "digits"
    assert sv.leak_kind("900101-1234567", "번호 9001011234567") == "digits"
    # a masked run is broken by "*": protected
    assert sv.leak_kind("010-4827-3391", "010-****-3391") is None
    assert sv.leak_kind("900101-1234567", "900101-1******") is None
    # fewer than 6 digits: only the verbatim rule applies
    assert sv.leak_kind("12-345", "12 345") is None
    assert sv.leak_kind("A-10294", "A 10294") is None
    # the digit run must contain the whole digit string
    assert sv.leak_kind("010-4827-3391", "010-4827 / 3391") is None


def test_leak_kind_folds_full_width_digits():
    assert sv.leak_kind("０１０－６６２１－３０９８", "연락 010-6621-3098") == "digits"
    assert sv.leak_kind("010-6621-3098", "연락 ０１０ ６６２１ ３０９８") == "digits"
    assert sv.leak_kind("０１０－６６２１－３０９８", "연락 ０１０-****-３０９８") is None


def test_score_doc_spurious_and_touching_regions():
    doc = {"id": "x", "text": "환자 김민준 내원 2024-03-05 혈압 120/80",
           "pii": [{"value": "김민준", "entity": "PERSON"}], "keep": ["2024-03-05", "120/80"]}
    out = "환자 [이름1] 내원 [날짜] 혈압 120/80"
    res = sv.score_doc(doc, out)
    assert res["evaluated"] == {"PERSON": 1} and res["leaked"] == {}
    assert res["keep_present"] == 2 and res["keep_survived"] == 1
    assert res["keep_damaged"] == ["2024-03-05"]
    assert res["changed_regions"] == 2
    assert [s["input"] for s in res["spurious"]] == ["2024-03-05"]


def test_zero_width_insertion_touching_gold_is_not_spurious():
    spans = [(3, 6)]
    assert sv.touches((3, 3), spans) and sv.touches((6, 6), spans)
    assert not sv.touches((7, 7), spans)
    assert sv.touches((0, 4), spans) and not sv.touches((0, 3), spans)
    doc = {"id": "x", "text": "보호자 김민준", "pii": [{"value": "김민준", "entity": "PERSON"}], "keep": []}
    res = sv.score_doc(doc, "보호자 김민준 [이름1]")  # insertion right after the gold value
    assert res["changed_regions"] == 1 and res["spurious"] == []
    assert res["leaked"] == {"PERSON": 1}


def test_dedupe_and_absent_gold():
    res = sv.score_doc(DOCS[0], DOCS[0]["text"])  # nothing changed: everything leaks
    assert res["evaluated"] == {"PERSON": 1, "PHONE": 1, "OTHER": 1}
    assert res["leaked"] == {"PERSON": 1, "PHONE": 1, "OTHER": 1}
    assert res["absent"] == 1
    assert res["keep_present"] == 1 and res["keep_survived"] == 1
    assert res["changed_regions"] == 0


# ------------------------------------------------------------------------------- end to end


def test_run_three_docs_default_config(corpus: Path, tmp_path: Path):
    out = io.StringIO()
    json_path = tmp_path / "res.json"
    res = sv.run([str(corpus)], json_out=str(json_path), verbose=True, out=out)
    o = res["overall"]
    assert (o["evaluated"], o["leaked"], o["absent_from_input"]) == (6, 2, 1)
    assert o["recall"] == pytest.approx(4 / 6, abs=1e-4)
    ents = res["per_entity"]
    assert ents["PERSON"] == {"evaluated": 3, "leaked": 0, "recall": 1.0}
    assert ents["PHONE"] == {"evaluated": 1, "leaked": 0, "recall": 1.0}
    assert ents["OTHER"] == {"evaluated": 2, "leaked": 2, "recall": 0.0}
    assert res["per_lang"]["ko"]["evaluated"] == 5 and res["per_lang"]["mixed"]["evaluated"] == 1
    assert res["per_corpus"]["three.jsonl"]["leaked"] == 2

    docs = {d["id"]: d for d in res["per_doc"]}
    assert [(lk["value"], lk["how"]) for lk in docs["d1"]["leaks"]] == [("청포도", "verbatim")]
    assert [(lk["value"], lk["how"]) for lk in docs["d2"]["leaks"]] == [("최다인 135-792", "digits")]
    assert docs["d3"]["leaks"] == []

    assert res["keep"] == {"survived": 3, "present": 4, "rate": 0.75}
    assert docs["d3"]["keep_damaged"] == ["보호자 연락처 010-3456-7890"]
    assert res["spurious"]["regions"] == 1
    assert [s["input"] for s in docs["d3"]["spurious"]] == ["3456"]
    assert docs["d1"]["spurious"] == [] and docs["d2"]["spurious"] == []

    assert res["docs"] == {"total": 3, "scored": 3, "failed": 0, "skipped": 0,
                           "interrupted": False, "stopped_after_failures": False}
    assert res["llm_requests"] is None and res["llm"] is None
    assert res["spans_by_entity"]["PERSON"] >= 3
    assert res["timing"]["ms_per_doc"] is not None

    text = out.getvalue()
    assert "66.7%" in text and "75.0% (3/4)" in text
    assert "누출 leaked" in text and "과잉 spurious '3456'" in text and "(digits)" in text
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["overall"] == o and saved["meta"]["backend"] == {}


def test_limit_counts_documents_across_corpora(tmp_path: Path):
    a = _write(tmp_path / "a.jsonl", DOCS[:2])
    b = _write(tmp_path / "b.jsonl", DOCS[2:])
    res = sv.run([str(a), str(b)], limit=1, out=io.StringIO())
    assert res["docs"]["total"] == 1 and [d["id"] for d in res["per_doc"]] == ["d1"]
    res = sv.run([str(a), str(b)], limit=3, out=io.StringIO())
    assert [d["corpus"] for d in res["per_doc"]] == ["a.jsonl", "a.jsonl", "b.jsonl"]
    assert set(res["per_corpus"]) == {"a.jsonl", "b.jsonl"}


# ------------------------------------------------------------------------------- LLM counters


class _FakeLLM:
    """Stands in for an LLM recognizer: counts calls, finds nothing, fails on a marker."""

    name = "fake_llm"
    entities = ("PERSON",)

    def __init__(self, fail_on: str | None = None) -> None:
        self.request_count = 0
        self.failed_requests = 0
        self.fail_on = fail_on

    def find(self, text: str, cfg=None) -> list[Span]:
        self.request_count += 1
        if self.fail_on and self.fail_on in text:
            self.failed_requests += 1
            raise RuntimeError("LLM pass failed (HTTP 502 structured_output_failed)")
        return []


def _with_fake(monkeypatch, fake) -> None:
    cfg = default_config()
    det = Detector(cfg, extra_recognizers=[fake])
    fake.request_count = 0  # the count of the run only
    monkeypatch.setattr(sv, "build_detector", lambda config: (cfg, det, 0.0))


def test_llm_request_count_via_getattr(monkeypatch, corpus: Path):
    assert sv.llm_request_count(Detector(default_config())) is None
    fake = _FakeLLM()
    _with_fake(monkeypatch, fake)
    out = io.StringIO()
    res = sv.run([str(corpus)], out=out)
    assert res["llm_requests"] == fake.request_count >= 3
    assert res["llm"]["request_count"] == fake.request_count
    assert res["overall"]["leaked"] == 2  # a detector that finds nothing changes nothing
    assert f"LLM 요청 requests     {fake.request_count}" in out.getvalue()


def test_failed_document_is_excluded_without_text(monkeypatch, corpus: Path):
    fake = _FakeLLM(fail_on="사물함")
    _with_fake(monkeypatch, fake)
    out = io.StringIO()
    res = sv.run([str(corpus)], verbose=True, out=out)
    assert res["docs"]["failed"] == 1 and res["docs"]["scored"] == 2
    err = next(d for d in res["per_doc"] if d["id"] == "d2")["error"]
    assert err.startswith("RuntimeError: LLM pass failed")
    assert res["overall"]["evaluated"] == 4  # d2's two gold values are left out
    text = out.getvalue()
    assert "실패 failed 1" in text and "최다인" not in text and "사물함" not in text


def test_stops_after_consecutive_failures(monkeypatch, tmp_path: Path):
    fake = _FakeLLM(fail_on="환자명")
    _with_fake(monkeypatch, fake)
    path = _write(tmp_path / "many.jsonl", [dict(DOCS[0], id=f"d{i}") for i in range(6)])
    res = sv.run([str(path)], out=io.StringIO())
    assert res["docs"]["failed"] == sv.STOP_AFTER_FAILURES
    assert res["docs"]["stopped_after_failures"] is True
    assert res["docs"]["skipped"] == 6 - sv.STOP_AFTER_FAILURES


def test_safe_error_blanks_bearer_tokens():
    msg = sv.safe_error(RuntimeError("Authorization: Bearer test-key-not-real rejected\nsecond line"))
    assert "test-key-not-real" not in msg and msg == "RuntimeError: Authorization: Bearer *** rejected"


# ------------------------------------------------------------------------------- CLI


def test_main_always_exits_zero(tmp_path: Path, capsys, corpus: Path):
    assert sv.main([str(corpus)]) == 0
    assert "전체 OVERALL" in capsys.readouterr().out
    assert sv.main([str(tmp_path / "missing.jsonl")]) == 0
    assert "cannot read corpus" in capsys.readouterr().out
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "x", "text": "환자 김민준"\n', encoding="utf-8")
    assert sv.main([str(bad)]) == 0
    msg = capsys.readouterr().out
    assert "bad.jsonl:1" in msg and "김민준" not in msg
    assert sv.main([]) == 0  # usage error: argparse prints, the tool still exits 0
    broken = tmp_path / "broken.toml"
    broken.write_text("[ner]\nbackend = \"no-such-backend\"\n", encoding="utf-8")
    assert sv.main([str(corpus), "--config", str(broken)]) == 0
    assert "config / detector error" in capsys.readouterr().out


def test_bench_corpora_are_consistent():
    for name in ("fresh_a.jsonl", "fresh_b.jsonl"):
        docs = sv.load_corpus(BENCH / name)
        for d in docs:
            assert list(d) == ["id", "genre", "lang", "text", "pii", "keep"]
            assert all(p["value"] in d["text"] for p in d["pii"]), d["id"]
            assert all(k in d["text"] for k in d["keep"]), d["id"]
    a, b = sv.load_corpus(BENCH / "fresh_a.jsonl"), sv.load_corpus(BENCH / "fresh_b.jsonl")
    assert (len(a), len(b)) == (54, 41)
    assert sum(len(d["pii"]) for d in a + b) == 449
