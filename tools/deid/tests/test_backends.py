"""Tests for the optional NER/LLM backends (no transformers, gliner or Ollama needed)."""
from __future__ import annotations

import json
import re
import socket
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from deid import entities as E
from deid.anonymizer import Anonymizer
from deid.backends import (
    OTHER, build_ner_recognizer, chunk_text, entity_for, merge_spans, resolve_device, trim_bounds,
)
from deid.backends import ollama
from deid.backends.gliner_backend import DEFAULT_LABELS, DEFAULT_MODEL, GlinerRecognizer
from deid.backends.ner_hf import HFNerRecognizer
from deid.backends.ollama import (
    OllamaError, OllamaRecognizer, check_host, find_occurrences, is_deidentified, llm_find_pii,
    llm_review, parse_items,
)
from deid.config import Config, config_from_dict
from deid.engine import Detector
from deid.types import Span

NAMES = ("김민준", "이서연", "박지호", "최하은", "정우진")


def long_note(n: int = 40) -> str:
    """Synthetic multi-visit note with lab values and a name in every sentence."""
    return " ".join(
        f"{i + 1}일차 혈압 120/80 mmHg, 혈당 98 mg/dL, HbA1c 6.1% 정상. 담당 {NAMES[i % 5]} 확인."
        for i in range(n)
    )


def occurrences(text: str, words=NAMES) -> list[tuple[int, int]]:
    return sorted(m.span() for w in words for m in re.finditer(re.escape(w), text))


class FakePipe:
    """Stands in for transformers' aggregated token-classification pipeline."""

    def __init__(self, words: dict[str, tuple[str, float]], partial: bool = False,
                 key: str = "entity_group") -> None:
        self.words, self.partial, self.key, self.calls = words, partial, key, []

    def __call__(self, chunk: str) -> list[dict]:
        self.calls.append(chunk)
        out = []
        for w, (label, score) in self.words.items():
            for m in re.finditer(re.escape(w), chunk):
                out.append({self.key: label, "score": score, "word": w,
                            "start": m.start(), "end": m.end()})
            if self.partial:  # fragments of a word cut by the chunk edges
                for k in range(len(w) - 1, 1, -1):
                    if chunk.endswith(w[:k]):
                        out.append({self.key: label, "score": score - 0.1, "word": w[:k],
                                    "start": len(chunk) - k, "end": len(chunk)})
                    if chunk.startswith(w[-k:]):
                        out.append({self.key: label, "score": score - 0.1, "word": w[-k:],
                                    "start": 0, "end": k})
        return out


class FakeGliner:
    """Stands in for gliner.GLiNER; ignores the threshold so filtering is tested on our side."""

    def __init__(self, words: dict[str, tuple[str, float]]) -> None:
        self.words, self.calls = words, []

    def predict_entities(self, text, labels, threshold=0.5, **kw):
        self.calls.append((text, list(labels), threshold))
        return [{"start": m.start(), "end": m.end(), "text": w, "label": lab, "score": sc}
                for w, (lab, sc) in self.words.items() if lab in labels
                for m in re.finditer(re.escape(w), text)]


def fake_torch(monkeypatch, mps: bool) -> None:
    torch = types.ModuleType("torch")
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    monkeypatch.setitem(sys.modules, "torch", torch)


def spans_of(spans: list[Span]) -> list[tuple[str, str]]:
    return [(s.entity, s.text) for s in spans]


# ====================================================================== shared helpers


def test_chunk_text_short_and_empty():
    assert chunk_text("") == []
    assert chunk_text("홍길동 내원", 400, 80) == [(0, "홍길동 내원")]


@pytest.mark.parametrize("max_chars,overlap", [(80, 30), (120, 0), (400, 80), (37, 10)])
def test_chunk_text_windows_cover_text_and_overlap(max_chars, overlap):
    text = long_note()
    chunks = chunk_text(text, max_chars, overlap)
    assert len(chunks) > 1
    assert chunks[0][0] == 0 and chunks[-1][0] + len(chunks[-1][1]) == len(text)
    for (off, c), (nxt, _) in zip(chunks, chunks[1:]):
        assert text[off:off + len(c)] == c and len(c) <= max_chars
        assert off < nxt <= off + len(c)                      # progress, no gap
        assert off + len(c) - nxt >= overlap - overlap // 2   # at least half the overlap
        assert c[-1].isspace()                                # cut after a space, not mid-word


def test_chunk_text_hard_cut_without_spaces():
    text = "가" * 250
    chunks = chunk_text(text, 100, 20)
    assert [o for o, _ in chunks] == [0, 80, 160]
    assert all(len(c) <= 100 for _, c in chunks)
    assert chunks[-1][0] + len(chunks[-1][1]) == 250


def test_chunk_text_prefers_newline():
    text = "가나다 라마바 사아자\n차카타 파하 " * 20
    for off, c in chunk_text(text, 50, 10)[:-1]:
        assert c.endswith("\n")


@pytest.mark.parametrize("max_chars,overlap", [(1, 0), (100, 50), (100, -1)])
def test_chunk_text_rejects_bad_params(max_chars, overlap):
    with pytest.raises(ValueError):
        chunk_text("abc", max_chars, overlap)


@pytest.mark.parametrize("raw,entity,expected", [
    (" 김민준님, ", E.PERSON, "김민준"),
    ("이서연 보호자", E.PERSON, "이서연"),
    ("박지호에게", E.PERSON, "박지호"),
    ("최하은", E.PERSON, "최하은"),          # 은 is a name syllable here, not a particle
    ("박하은", E.PERSON, "박하은"),
    ("김씨", E.PERSON, "김씨"),              # trimming would leave a single syllable
    ("「홍길동」", E.PERSON, "홍길동"),
    ("(서울시 강남구)", E.ADDRESS, "서울시 강남구"),
    ("서울시 강남구 (역삼동)", E.ADDRESS, "서울시 강남구 (역삼동)"),
    ("010-1234-5678.", E.PHONE, "010-1234-5678"),
    ("홍길동님", E.ADDRESS, "홍길동님"),      # honorifics only trimmed for PERSON
    (" .,", E.PERSON, ""),
])
def test_trim_bounds(raw, entity, expected):
    s, e = trim_bounds(raw, 0, len(raw), entity)
    assert raw[s:e] == expected


def test_merge_spans_joins_overlapping_and_touching_same_entity_only():
    text = "김민준 010-1234-5678"
    sp = [Span(0, 2, E.PERSON, "김민", 0.7, "x"), Span(0, 3, E.PERSON, "김민준", 0.9, "x"),
          Span(2, 3, E.PERSON, "준", 0.8, "x"), Span(4, 17, E.PHONE, text[4:17], 0.9, "x"),
          Span(4, 17, E.PHONE, text[4:17], 0.95, "x"), Span(2, 7, E.ADDRESS, text[2:7], 0.6, "x")]
    out = merge_spans(text, sp)
    assert [(s.entity, s.start, s.end, s.score) for s in out] == [
        (E.PERSON, 0, 3, 0.9), (E.ADDRESS, 2, 7, 0.6), (E.PHONE, 4, 17, 0.95)]


def test_merge_spans_keeps_separate_names():
    text = "김민준 이서연"
    out = merge_spans(text, [Span(0, 3, E.PERSON, "김민준", 0.9, "x"),
                             Span(4, 7, E.PERSON, "이서연", 0.9, "x")])
    assert spans_of(out) == [(E.PERSON, "김민준"), (E.PERSON, "이서연")]


@pytest.mark.parametrize("label,entity", [
    ("person", E.PERSON), ("NAME", E.PERSON), ("이름", E.PERSON), ("phone number", E.PHONE),
    ("Phone-Number", E.PHONE), ("연락처", E.PHONE), ("E-mail", E.EMAIL), ("주소", E.ADDRESS),
    ("resident registration number", E.RRN), ("주민등록번호", E.RRN), ("DOB", E.BIRTHDATE),
    ("chart number", E.MRN), ("Driver's License Number", E.DRIVER_LICENSE), ("MRN", E.MRN),
])
def test_entity_for_aliases(label, entity):
    assert entity_for(label) == entity


def test_entity_for_unknown():
    assert entity_for("nickname") == OTHER
    assert entity_for("nickname", default=None) is None
    assert entity_for("") == OTHER and entity_for(None) == OTHER


def test_resolve_device(monkeypatch):
    assert resolve_device("cpu") == "cpu" and resolve_device("MPS") == "mps"
    assert resolve_device(None) == "cpu" and resolve_device("cuda:0") == "cuda:0"
    fake_torch(monkeypatch, mps=True)
    assert resolve_device("auto") == "mps"
    fake_torch(monkeypatch, mps=False)
    assert resolve_device("auto") == "cpu"
    monkeypatch.setitem(sys.modules, "torch", None)
    assert resolve_device("auto") == "cpu"
    with pytest.raises(ValueError):
        resolve_device("gpu")


# ====================================================================== HF NER

GENERAL = "klue/bert-base-ner"          # hypothetical general NER model id
PII = "kiyuyeon/pii-ner-kcelectra"
NOTE = "환자 김민준(45세) 010-1234-5678 연락. 서울 강남구 거주, 서울대병원 내원. 혈압 130/85."
NOTE_TAGS = {"김민준": ("PER", 0.98), "010-1234-5678": ("PHONE", 0.95),
             "서울 강남구": ("LOC", 0.9), "서울대병원": ("ORG", 0.92)}


def test_hf_general_model_maps_person_phone_and_ignores_places_orgs():
    rec = HFNerRecognizer(GENERAL, pipeline=FakePipe(NOTE_TAGS))
    assert not rec.pii_model
    spans = rec.find(NOTE, Config())
    assert spans_of(spans) == [(E.PERSON, "김민준"), (E.PHONE, "010-1234-5678")]
    for s in spans:
        assert NOTE[s.start:s.end] == s.text and s.source == "ner_hf"


def test_hf_pii_model_maps_location_to_address():
    rec = HFNerRecognizer(PII, pipeline=FakePipe(NOTE_TAGS))
    assert rec.pii_model
    assert (E.ADDRESS, "서울 강남구") in spans_of(rec.find(NOTE, Config()))
    assert (E.ADDRESS, "서울 강남구") not in spans_of(
        HFNerRecognizer(PII, pii_model=False, pipeline=FakePipe(NOTE_TAGS)).find(NOTE))
    assert (E.ADDRESS, "서울 강남구") in spans_of(
        HFNerRecognizer(GENERAL, pii_model=True, pipeline=FakePipe(NOTE_TAGS)).find(NOTE))


def test_hf_label_map_override():
    rec = HFNerRecognizer(PII, label_map={"LOC": "", "org": "hospital", "PS": "ignore",
                                          "ADDR": "address"},
                          pipeline=FakePipe(NOTE_TAGS))
    assert rec.map_label("LOC") is None and rec.map_label("B-PS") is None
    assert rec.map_label("ORG") == "HOSPITAL" and rec.map_label("ADDR") == E.ADDRESS
    assert spans_of(rec.find(NOTE)) == [
        (E.PERSON, "김민준"), (E.PHONE, "010-1234-5678"), ("HOSPITAL", "서울대병원")]


@pytest.mark.parametrize("label,entity", [
    ("PER", E.PERSON), ("B-PER", E.PERSON), ("I-PS", E.PERSON), ("PER-B", E.PERSON),
    ("PS_NAME", E.PERSON), ("PERSON", E.PERSON), ("PATIENT", E.PERSON), ("name", E.PERSON),
    ("TEL", E.PHONE), ("PHONE_NUMBER", E.PHONE), ("phone number", E.PHONE), ("B-EMAIL", E.EMAIL),
    ("E-MAIL", E.EMAIL), ("ADDRESS", E.ADDRESS), ("ADDR", E.ADDRESS), ("RRN", E.RRN),
    ("ORG", None), ("B-OG", None), ("LOC", None), ("I-LC", None), ("DT", None), ("DATE", None),
    ("QT", None), ("O", None), ("LABEL_3", None), ("", None), (None, None),
])
def test_hf_label_variants(label, entity):
    assert HFNerRecognizer(GENERAL, pipeline=FakePipe({})).map_label(label) == entity


def test_hf_score_filter():
    tags = {"김민준": ("PER", 0.59), "010-1234-5678": ("PHONE", 0.61)}
    assert spans_of(HFNerRecognizer(GENERAL, pipeline=FakePipe(tags)).find(NOTE)) == [
        (E.PHONE, "010-1234-5678")]
    assert HFNerRecognizer(GENERAL, min_score=0.9, pipeline=FakePipe(tags)).find(NOTE) == []


def test_hf_trims_model_offsets():
    text = "담당: 이서연님, 보호자 박지호에게 설명."

    def pipe(chunk):
        return [{"entity_group": "PS", "score": 0.9, "start": 3, "end": 9},    # " 이서연님,"
                {"entity_group": "PS", "score": 0.9, "start": 14, "end": 19}]  # "박지호에게"
    assert spans_of(HFNerRecognizer(GENERAL, pipeline=pipe).find(text)) == [
        (E.PERSON, "이서연"), (E.PERSON, "박지호")]


def test_hf_unaggregated_output_and_missing_offsets():
    text = "김민준 내원"
    items = [{"entity": "B-PER", "score": 0.9, "index": 1, "start": 0, "end": 3},
             {"entity_group": "PER", "score": 0.9, "word": "내원"}]  # no offsets -> skipped
    assert spans_of(HFNerRecognizer(GENERAL, pipeline=lambda c: items).find(text)) == [
        (E.PERSON, "김민준")]


def test_hf_long_text_global_offsets_and_seam_dedup():
    text = long_note()
    pipe = FakePipe({n: ("PER", 0.9) for n in NAMES})
    rec = HFNerRecognizer(GENERAL, max_chars=80, overlap=30, pipeline=pipe)
    spans = rec.find(text)
    assert [(s.start, s.end) for s in spans] == occurrences(text)
    assert all(text[s.start:s.end] == s.text and s.entity == E.PERSON for s in spans)
    # The test really exercises seams: some names were seen by two windows.
    seen = sum(len(re.findall("|".join(NAMES), c)) for c in pipe.calls)
    assert len(pipe.calls) > 10 and seen > len(spans)


def test_hf_partial_entity_at_hard_cut_is_merged():
    text = "가" * 70 + "김민준" + "나" * 70  # no spaces: the first window cuts the name
    pipe = FakePipe({"김민준": ("PER", 0.9)}, partial=True)
    spans = HFNerRecognizer(GENERAL, max_chars=72, overlap=20, pipeline=pipe).find(text)
    assert pipe.calls[0].endswith("김민")
    assert [(s.start, s.end, s.text) for s in spans] == [(70, 73, "김민준")]


def test_hf_no_false_positives_on_clinical_text():
    text = "Hb 13.2 g/dL, WBC 7,800/uL, Cr 0.9 mg/dL. 고혈압, 당뇨 정상 범위. 이상 소견 없음."
    rec = HFNerRecognizer(GENERAL, pipeline=FakePipe({"고혈압": ("DT", 0.99), "13.2": ("QT", 0.99)}))
    assert rec.find(text) == []


def test_hf_empty_text_does_not_load_model():
    rec = HFNerRecognizer(GENERAL)
    assert rec.find("") == [] and rec.find("  \n") == []
    assert rec._pipe is None


def test_hf_missing_transformers_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "transformers", None)
    rec = HFNerRecognizer(GENERAL)
    with pytest.raises(RuntimeError, match=re.escape('pip install "deid-kit[ner]"')):
        rec.find("김민준 내원")


def _fake_transformers(monkeypatch, fail_local=False, fail_all=False):
    calls: dict[str, list] = {"tok": [], "model": [], "pipeline": []}

    def loader(kind, result):
        def from_pretrained(name, local_files_only=False):
            calls[kind].append((name, local_files_only))
            if fail_all or (fail_local and local_files_only):
                raise OSError("We couldn't connect to 'https://huggingface.co'")
            return result
        return types.SimpleNamespace(from_pretrained=from_pretrained)

    def pipeline(task, **kw):
        calls["pipeline"].append((task, kw))
        return FakePipe({"김민준": ("PER", 0.9)})

    mod = types.ModuleType("transformers")
    mod.AutoTokenizer = loader("tok", "TOK")
    mod.AutoModelForTokenClassification = loader("model", "MODEL")
    mod.pipeline = pipeline
    monkeypatch.setitem(sys.modules, "transformers", mod)
    return calls


def test_hf_lazy_load_uses_cache_and_auto_device(monkeypatch):
    calls = _fake_transformers(monkeypatch)
    fake_torch(monkeypatch, mps=True)
    rec = HFNerRecognizer(PII, device="auto")
    assert calls["pipeline"] == []  # nothing loaded at construction
    assert spans_of(rec.find("김민준 내원")) == [(E.PERSON, "김민준")]
    rec.find("김민준 재진")
    assert calls["tok"] == [(PII, True)] and calls["model"] == [(PII, True)]
    task, kw = calls["pipeline"][0]
    assert len(calls["pipeline"]) == 1 and task == "token-classification"
    assert kw == {"model": "MODEL", "tokenizer": "TOK", "aggregation_strategy": "simple",
                  "device": "mps"}


def test_hf_downloads_once_when_not_cached(monkeypatch):
    calls = _fake_transformers(monkeypatch, fail_local=True)
    HFNerRecognizer(PII).load()
    assert calls["tok"] == [(PII, True), (PII, False)]
    assert calls["pipeline"][0][1]["device"] == "cpu"


def test_hf_load_failure_explains_offline_cache(monkeypatch):
    _fake_transformers(monkeypatch, fail_all=True)
    with pytest.raises(RuntimeError, match="HF_HUB_OFFLINE"):
        HFNerRecognizer(PII).load()


def test_is_installed_checks_without_importing(monkeypatch):
    from deid.backends import gliner_backend, ner_hf

    monkeypatch.setitem(sys.modules, "transformers", None)
    monkeypatch.setitem(sys.modules, "gliner", None)
    assert ner_hf.is_installed() is False and gliner_backend.is_installed() is False


def test_hf_rejects_bad_device_and_chunking():
    with pytest.raises(ValueError):
        HFNerRecognizer(PII, device="gpu")
    with pytest.raises(ValueError):
        HFNerRecognizer(PII, max_chars=100, overlap=60)


def test_hf_inside_detector_and_anonymizer():
    cfg = Config()
    rec = HFNerRecognizer(GENERAL, pipeline=FakePipe(NOTE_TAGS))
    det = Detector(cfg, recognizers=[rec])
    out = Anonymizer(cfg).anonymize(NOTE, det.detect(NOTE)).text
    assert "김민준" not in out and "[이름1]" in out and "010-****-5678" in out
    assert "서울대병원" in out and "130/85" in out


# ====================================================================== GLiNER

G_TEXT = "환자 이서연 (등록번호 12345678), 연락처 010-9876-5432, 서울시 마포구 거주. 혈당 102."
G_TAGS = {"이서연": ("person", 0.93), "12345678": ("medical record number", 0.8),
          "010-9876-5432": ("phone number", 0.88), "서울시 마포구": ("address", 0.75),
          "102": ("phone number", 0.2)}


def test_gliner_default_labels_mapping_and_threshold():
    model = FakeGliner(G_TAGS)
    rec = GlinerRecognizer(model_obj=model)
    assert rec.model == DEFAULT_MODEL and rec.labels == list(DEFAULT_LABELS)
    spans = rec.find(G_TEXT, Config())
    assert spans_of(spans) == [(E.PERSON, "이서연"), (E.MRN, "12345678"),
                               (E.PHONE, "010-9876-5432"), (E.ADDRESS, "서울시 마포구")]
    assert all(G_TEXT[s.start:s.end] == s.text and s.source == "gliner" for s in spans)
    assert model.calls[0][1:] == (list(DEFAULT_LABELS), 0.5)
    assert spans_of(GlinerRecognizer(threshold=0.85, model_obj=FakeGliner(G_TAGS)).find(G_TEXT)) \
        == [(E.PERSON, "이서연"), (E.PHONE, "010-9876-5432")]


def test_gliner_custom_labels():
    rec = GlinerRecognizer(labels=["person", "staff id", "passport number", "생년월일"],
                           model_obj=FakeGliner({}))
    assert rec.label_map == {"person": E.PERSON, "staff id": "STAFF_ID",
                             "passport number": E.PASSPORT, "생년월일": E.BIRTHDATE}
    rec = GlinerRecognizer(labels={"patient name": "PERSON", "employee number": "staff_id",
                                   "hospital": ""}, model_obj=FakeGliner({}))
    assert rec.label_map == {"patient name": E.PERSON, "employee number": "STAFF_ID"}
    with pytest.raises(ValueError):
        GlinerRecognizer(labels=[], model_obj=FakeGliner({}))
    with pytest.raises(TypeError):
        GlinerRecognizer(labels="person", model_obj=FakeGliner({}))


def test_gliner_label_case_and_unknown_labels():
    class Model:
        def predict_entities(self, text, labels, threshold=0.5):
            return [{"start": 0, "end": 3, "label": "PERSON", "score": 0.9},
                    {"start": 4, "end": 6, "label": "organization", "score": 0.9}]
    assert spans_of(GlinerRecognizer(model_obj=Model()).find("홍길동 내원")) == [
        (E.PERSON, "홍길동")]


def test_gliner_long_text_offsets():
    text = long_note()
    rec = GlinerRecognizer(max_chars=90, overlap=30,
                           model_obj=FakeGliner({n: ("person", 0.9) for n in NAMES}))
    spans = rec.find(text)
    assert [(s.start, s.end) for s in spans] == occurrences(text)
    assert all(text[s.start:s.end] == s.text for s in spans)


def test_gliner_missing_library_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "gliner", None)
    with pytest.raises(RuntimeError, match=re.escape('pip install "deid-kit[gliner]"')):
        GlinerRecognizer().find("홍길동 내원")


def test_gliner_lazy_load_cache_first_and_device(monkeypatch):
    calls = []

    class Loaded(FakeGliner):
        def to(self, device):
            calls.append(("to", device))
            return self

    class GLiNER:
        @staticmethod
        def from_pretrained(name, local_files_only=False):
            calls.append((name, local_files_only))
            if local_files_only:
                raise OSError("not in cache")
            return Loaded({"홍길동": ("person", 0.9)})

    monkeypatch.setitem(sys.modules, "gliner", types.SimpleNamespace(GLiNER=GLiNER))
    fake_torch(monkeypatch, mps=True)
    rec = GlinerRecognizer(device="auto")
    assert spans_of(rec.find("홍길동 내원")) == [(E.PERSON, "홍길동")]
    rec.find("홍길동 재진")
    assert calls == [(DEFAULT_MODEL, True), (DEFAULT_MODEL, False), ("to", "mps")]


# ====================================================================== build_ner_recognizer


@pytest.mark.parametrize("ner", [{}, {"backend": None}, {"backend": ""}, {"backend": "none"}])
def test_build_ner_disabled(ner):
    assert build_ner_recognizer(Config(ner=ner)) is None


def test_build_ner_hf_from_config():
    cfg = config_from_dict({"ner": {"backend": "hf", "model": PII, "device": "auto",
                                    "min_score": 0.7, "max_chars": 300, "overlap": 50,
                                    "label_map": {"ORG": "HOSPITAL"}}})
    rec = build_ner_recognizer(cfg)
    assert isinstance(rec, HFNerRecognizer)
    assert (rec.model, rec.device, rec.min_score, rec.max_chars, rec.overlap) == (
        PII, "auto", 0.7, 300, 50)
    assert rec.map_label("ORG") == "HOSPITAL" and rec.map_label("LOC") == E.ADDRESS
    assert rec._pipe is None  # loading is deferred to the first find()


def test_build_ner_hf_needs_model():
    with pytest.raises(ValueError, match="model"):
        build_ner_recognizer(Config(ner={"backend": "hf"}))


def test_build_ner_gliner_from_config():
    rec = build_ner_recognizer(Config(ner={"backend": "GLiNER", "min_score": 0.4,
                                           "labels": ["person", "email"]}))
    assert isinstance(rec, GlinerRecognizer)
    assert (rec.model, rec.threshold, rec.labels) == (DEFAULT_MODEL, 0.4, ["person", "email"])
    rec = build_ner_recognizer(Config(ner={"backend": "gliner", "threshold": 0.3}))
    assert rec.threshold == 0.3 and rec.labels == list(DEFAULT_LABELS)


def test_build_ner_ollama_from_config():
    cfg = Config(ner={"backend": "ollama"}, llm={"model": "gemma3:4b",
                                                 "host": "http://localhost:11434/"})
    rec = build_ner_recognizer(cfg)
    assert isinstance(rec, OllamaRecognizer)
    assert (rec.model, rec.host) == ("gemma3:4b", "http://localhost:11434")
    with pytest.raises(ValueError, match="non-local"):
        build_ner_recognizer(Config(ner={"backend": "ollama", "model": "gemma3:4b",
                                         "host": "http://192.168.0.7:11434"}))
    with pytest.raises(ValueError, match="model"):
        build_ner_recognizer(Config(ner={"backend": "ollama"}))


def test_build_ner_unknown_backend():
    with pytest.raises(ValueError, match="unknown"):
        build_ner_recognizer(Config(ner={"backend": "spacy"}))


# ====================================================================== Ollama: parsing


@pytest.mark.parametrize("raw", [
    '{"items": [{"text": "홍길동", "entity": "PERSON", "reason": "name"}]}',
    '[{"text": "홍길동", "entity": "PERSON", "reason": "name"}]',
    '```json\n{"items": [{"text": "홍길동", "entity": "PERSON", "reason": "name"}]}\n```',
    'Sure! {"items": [{"text": "홍길동", "entity": "PERSON", "reason": "name"}]} Hope it helps.',
    '<think>names...</think>{"pii": [{"value": "홍길동", "type": "이름", "reason": "name"}]}',
    '{"text": "홍길동", "label": "name", "reason": "name"}',
])
def test_parse_items_shapes(raw):
    assert parse_items(raw) == [{"text": "홍길동", "entity": E.PERSON, "reason": "name"}]


def test_parse_items_odd_values():
    items = parse_items('{"items": ["이서연", {"text": 12345678, "entity": "chart number"}, '
                        '{"text": "\\"박지호\\" ", "entity": "nickname"}, 7, null, {"entity": "x"}]}')
    assert items == [{"text": "이서연", "entity": OTHER, "reason": ""},
                     {"text": "12345678", "entity": E.MRN, "reason": ""},
                     {"text": "박지호", "entity": OTHER, "reason": ""}]
    assert parse_items("{}") == [] and parse_items('{"items": []}') == []
    assert parse_items('{"answer": "none"}') == []


@pytest.mark.parametrize("raw", ["", "   ", "no identifiers found", '{"items": [{"text": "홍'])
def test_parse_items_malformed_raises(raw):
    with pytest.raises(OllamaError):
        parse_items(raw)


@pytest.mark.parametrize("value,expected", [
    ("[이름1]", True), ("[주소]", True), ("[이름1]님", True), ("[이름1] 환자", True),
    ("김*준", True), ("010-****-5678", True), ("900101-1******", True), ("Dr. [이름2]", True),
    ("홍길동", False), ("[이름1] 홍길동", False), ("010-1234-5678", False),
    ("서울특별시 강남구 [주소]", False),
])
def test_is_deidentified(value, expected):
    assert is_deidentified(value) is expected


@pytest.mark.parametrize("text,value,expected", [
    ("이수가 왔다", "이수", [(0, 2)]),
    ("이수술 후 경과", "이수", []),
    ("이수정 님", "이수", []),
    ("이수님께서 전달", "이수", [(0, 2)]),
    ("박김민준 확인", "김민준", []),
    ("김민준이 왔다. (김민준)", "김민준", [(0, 3), (10, 13)]),
    ("김민준호 확인", "김민준", []),
    ("ID 12345, 123456, 12345", "12345", [(3, 8), (18, 23)]),
    ("Johnson met John.", "John", [(12, 16)]),
    ("John이 왔다", "John", [(0, 4)]),
    ("", "홍길동", []),
])
def test_find_occurrences(text, value, expected):
    assert find_occurrences(text, value) == expected


# ====================================================================== Ollama: fake server


class FakeOllama:
    """Minimal Ollama look-alike on 127.0.0.1 that records requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.replies: list = []        # str (model answer) or (status, body)
        self.default = lambda payload: '{"items": []}'
        self.models = ["gemma3:4b", "qwen3:8b"]
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, headers=None):
                data = body if isinstance(body, bytes) else (
                    body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m, "model": m} for m in fake.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                payload = json.loads(body.decode("utf-8"))
                fake.requests.append(("POST", self.path, payload))
                if self.path == "/redir/api/generate":
                    return self._send(307, b"", {"Location": fake.url + "/api/generate"})
                if self.path != "/api/generate":
                    return self._send(404, {"error": "not found"})
                reply = fake.replies.pop(0) if fake.replies else fake.default(payload)
                if isinstance(reply, tuple):
                    return self._send(*reply)
                self._send(200, {"model": payload["model"], "response": reply, "done": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def generate_calls(self) -> list[dict]:
        return [p for m, path, p in self.requests if m == "POST" and path == "/api/generate"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_ollama():
    srv = FakeOllama()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture
def closed_port_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def items_json(*items) -> str:
    return json.dumps({"items": [dict(zip(("text", "entity", "reason"), it)) for it in items]},
                      ensure_ascii=False)


REVIEWED = "[이름1] 환자(45세), 보호자 박지호 010-****-5678. 주소 [주소]. 담당의 김*준. 혈압 130/85."


def test_available_and_list_models(fake_ollama, closed_port_url):
    assert ollama.available(fake_ollama.url) is True
    assert ollama.list_models(fake_ollama.url) == ["gemma3:4b", "qwen3:8b"]
    assert ollama.available(closed_port_url) is False
    with pytest.raises(OllamaError, match="ollama serve"):
        ollama.list_models(closed_port_url)


def test_llm_review_request_and_filtering(fake_ollama):
    fake_ollama.replies.append(items_json(
        ("박지호", "name", "보호자 이름"),           # present -> kept, alias mapped
        ("최하은", "PERSON", "hallucinated"),       # not in the text -> dropped
        ("[이름1]", "PERSON", "placeholder"),       # placeholder -> dropped
        ("김*준", "PERSON", "masked"),              # masked -> dropped
        ("010-****-5678", "PHONE", "masked"),
        ("45세", "age", "quasi-identifier"),        # unknown entity -> OTHER
        ("박지호", "name", "duplicate"),
        ("박", "PERSON", "too short"),
    ))
    items = llm_review(REVIEWED, "gemma3:4b", host=fake_ollama.url)
    assert items == [{"text": "박지호", "entity": E.PERSON, "reason": "보호자 이름"},
                     {"text": "45세", "entity": OTHER, "reason": "quasi-identifier"}]
    (payload,) = fake_ollama.generate_calls
    assert payload["model"] == "gemma3:4b" and payload["stream"] is False
    assert payload["format"] == "json" and payload["options"]["temperature"] == 0
    assert REVIEWED in payload["prompt"] and "[이름1]" in payload["prompt"]
    assert "ALREADY" in payload["prompt"]


def test_llm_review_clean_text(fake_ollama):
    assert llm_review(REVIEWED, "gemma3:4b", host=fake_ollama.url) == []
    assert llm_review("  ", "gemma3:4b", host=fake_ollama.url) == []
    assert len(fake_ollama.generate_calls) == 1  # blank text never reaches the model


def test_llm_review_tolerates_fences_and_bare_lists(fake_ollama):
    fake_ollama.replies += ['```json\n[{"text": "박지호", "entity": "PERSON"}]\n```']
    assert [i["text"] for i in llm_review(REVIEWED, "m", host=fake_ollama.url)] == ["박지호"]


def test_llm_review_malformed_json_raises(fake_ollama):
    fake_ollama.replies.append("I could not find anything, sorry")
    with pytest.raises(OllamaError, match="not valid JSON"):
        llm_review(REVIEWED, "gemma3:4b", host=fake_ollama.url)
    fake_ollama.replies.append((200, b"<html>oops</html>"))
    with pytest.raises(OllamaError, match="non-JSON"):
        llm_review(REVIEWED, "gemma3:4b", host=fake_ollama.url)
    fake_ollama.replies.append((200, {"done": True}))
    with pytest.raises(OllamaError, match="response"):
        llm_review(REVIEWED, "gemma3:4b", host=fake_ollama.url)


def test_llm_review_missing_model_hint(fake_ollama):
    fake_ollama.replies.append((404, {"error": "model \"gemma9\" not found, try pulling it first"}))
    with pytest.raises(OllamaError, match="ollama pull gemma9") as exc:
        llm_review(REVIEWED, "gemma9", host=fake_ollama.url)
    assert exc.value.status == 404


def test_llm_review_unreachable(closed_port_url):
    with pytest.raises(OllamaError, match="cannot reach Ollama"):
        llm_review(REVIEWED, "gemma3:4b", host=closed_port_url, timeout=5)


def test_llm_review_long_text_is_chunked(fake_ollama):
    text = long_note(30)

    def reply(payload):
        chunk = payload["prompt"].rsplit("<text>", 1)[1]
        return items_json(*[(n, "PERSON", "name") for n in NAMES if n in chunk])

    fake_ollama.default = reply
    items = llm_review(text, "gemma3:4b", host=fake_ollama.url, max_chars=500)
    assert len(fake_ollama.generate_calls) > 3
    assert sorted(i["text"] for i in items) == sorted(NAMES)


@pytest.mark.parametrize("host", [
    "http://192.168.0.10:11434", "https://api.example.com", "http://127.0.0.1.evil.com:11434",
    "http://localhost.evil.com", "http://127.0.0.1@evil.com", "http://10.0.0.1",
])
def test_remote_hosts_refused_before_any_request(monkeypatch, host):
    def no_network(*a, **kw):
        raise AssertionError("network used")
    monkeypatch.setattr(ollama, "_open", no_network)
    with pytest.raises(ValueError, match="non-local"):
        llm_review("홍길동", "gemma3:4b", host=host)
    with pytest.raises(ValueError):
        llm_find_pii("홍길동", "gemma3:4b", host=host)
    with pytest.raises(ValueError):
        ollama.available(host)
    with pytest.raises(ValueError):
        OllamaRecognizer("gemma3:4b", host=host)


@pytest.mark.parametrize("host,base", [
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
    ("localhost:11434", "http://localhost:11434"),
    ("http://[::1]:11434", "http://[::1]:11434"),
])
def test_check_host_loopback(host, base):
    assert check_host(host) == base


@pytest.mark.parametrize("host", ["", "ftp://127.0.0.1", "http://127.0.0.1:99999", "http://"])
def test_check_host_invalid(host):
    with pytest.raises(ValueError):
        check_host(host)


def test_check_host_allow_remote():
    assert check_host("http://10.0.0.5:11434/", allow_remote=True) == "http://10.0.0.5:11434"


def test_requests_bypass_proxy_settings(fake_ollama, monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # would fail if used
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    assert ollama.available(fake_ollama.url)
    fake_ollama.replies.append(items_json(("박지호", "PERSON", "")))
    assert llm_review(REVIEWED, "m", host=fake_ollama.url)[0]["text"] == "박지호"


def test_redirects_are_not_followed(fake_ollama):
    with pytest.raises(OllamaError, match="307"):
        llm_review(REVIEWED, "m", host=fake_ollama.url + "/redir")
    assert fake_ollama.generate_calls == []


# ====================================================================== OllamaRecognizer

RAW = ("환자 김민준(45세)은 보호자 이서연과 내원. 김민준 님 연락처 010-1234-5678. "
       "이서연에게 설명함. 이서연호 병동 아님. 혈압 130/85, 이상 없음.")


def test_llm_find_pii_prompt_is_for_raw_text(fake_ollama):
    llm_find_pii(RAW, "qwen3:4b", host=fake_ollama.url)
    (payload,) = fake_ollama.generate_calls
    assert "ALREADY" not in payload["prompt"] and RAW in payload["prompt"]


def test_ollama_recognizer_spans_every_occurrence(fake_ollama):
    fake_ollama.replies.append(items_json(
        ("김민준", "PERSON", ""), ("이서연", "이름", ""), ("010-1234-5678", "phone", ""),
        ("박지호", "PERSON", "not in text"), ("130/85", "OTHER", "")))
    rec = OllamaRecognizer("gemma3:4b", host=fake_ollama.url)
    spans = rec.find(RAW, Config())
    got = [(s.entity, s.text, s.start) for s in spans]
    expected = sorted([(E.PERSON, "김민준", m.start()) for m in re.finditer("김민준", RAW)]
                      + [(E.PERSON, "이서연", i) for i in (RAW.index("이서연과"),
                                                         RAW.index("이서연에게"))]
                      + [(E.PHONE, "010-1234-5678", RAW.index("010")),
                         (OTHER, "130/85", RAW.index("130/85"))], key=lambda t: t[2])
    assert got == expected  # "이서연호" is a different word and is not matched
    assert all(s.score == 0.7 and s.source == "llm" and RAW[s.start:s.end] == s.text
               for s in spans)


def test_ollama_recognizer_in_detector(fake_ollama):
    fake_ollama.replies.append(items_json(("김민준", "PERSON", "")))
    cfg = Config()
    det = Detector(cfg, recognizers=[OllamaRecognizer("gemma3:4b", host=fake_ollama.url)])
    out = Anonymizer(cfg).anonymize(RAW, det.detect(RAW)).text
    assert "김민준" not in out and out.count("[이름1]") == 2


def test_ollama_recognizer_failure_is_loud(closed_port_url):
    rec = OllamaRecognizer("gemma3:4b", host=closed_port_url, timeout=5)
    with pytest.raises(OllamaError):
        rec.find(RAW, Config())
