"""Text-like files: .txt .md .log .srt .vtt, .json/.jsonl, .html/.htm, .xml.

Also the shared plumbing for the other text-ish handlers (tabular.py uses it):

* ``detect_encoding`` - BOM (UTF-8/16/32) -> strict UTF-8 -> CP949 (a superset of EUC-KR);
  anything else raises ``TextEncodingError``. Output is written in the same encoding (same
  BOM) and newlines are never touched, so the newline style is kept.
* ``Batch`` - collect every text "slot" of one file (paragraphs, cells, JSON values, ...),
  register values of typed fields (an Excel 이름 column, a JSON "patient_name" key) with
  ``Detector.add_known``, run ``Detector.detect_document`` once over all of them so a name
  found in one place is masked everywhere in the file, then write the results back.
* ``deid_text(text, detector, anonymizer)`` - stdin / clipboard entry point used by the CLI.

Markup (HTML/XML) is never re-serialised: the source is split into tags and text with a
tokenizer and only text nodes, comments, CDATA, ``<script>`` bodies and selected attribute
values are replaced (plus ``<style>`` bodies, DOCTYPE/DTD declarations and processing
instructions), so tag names and structure cannot be corrupted. Character references
(``&#54861;``) are decoded before detection and the replacement is mapped back onto the
original characters.
"""
from __future__ import annotations

import codecs
import html as _html
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import entities as E
from ..types import AnonResult, Report, Span

if TYPE_CHECKING:  # pragma: no cover
    from ..anonymizer import Anonymizer
    from ..config import Config
    from ..engine import Detector

EXTENSIONS = frozenset({
    ".txt", ".text", ".md", ".markdown", ".log", ".srt", ".vtt",
    ".json", ".jsonl", ".ndjson", ".html", ".htm", ".xhtml", ".xml",
})
HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml"})
JSON_EXTENSIONS = frozenset({".json"})
JSONL_EXTENSIONS = frozenset({".jsonl", ".ndjson"})
XML_EXTENSIONS = frozenset({".xml"})

# deid_text() splits long input into paragraph chunks of about this many characters. The
# chunks are still detected together (detect_document), so values propagate between them.
CHUNK_CHARS = 8000

MAX_NAME_WORDS = 4   # longer text in a name column is treated as free text

TYPED_SOURCE = "column"
TYPED_SCORE = 1.0


# ---------------------------------------------------------------------------------------------
# Errors and file helpers
# ---------------------------------------------------------------------------------------------


class TextEncodingError(ValueError):
    """The file is not text in a supported encoding."""


def same_file_error() -> ValueError:
    return ValueError("출력 파일이 원본 파일과 같습니다. 원본을 덮어쓰지 않도록 다른 경로를 지정하세요. "
                      "(output path must differ from the input file)")


def check_paths(src: Path, dst: Path) -> None:
    """Refuse to overwrite the original file (the output would replace the only copy)."""
    try:
        same = src.resolve() == dst.resolve() or (dst.exists() and os.path.samefile(src, dst))
    except OSError:
        same = False
    if same:
        raise same_file_error()


def write_atomic(dst: Path, payload: bytes | Callable[[Path], None]) -> None:
    """Write `payload` (bytes, or a function writing to a path) to dst via a temp file.

    A failure half-way never leaves a partial (possibly unprocessed) output behind.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}.deid-tmp")
    try:
        if callable(payload):
            payload(tmp)
        else:
            tmp.write_bytes(payload)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------------------------
# Encoding detection
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TextEncoding:
    """How a file was encoded: a Python codec plus the BOM it started with (if any)."""

    codec: str
    bom: bytes = b""

    @property
    def name(self) -> str:
        return f"{self.codec}+bom" if self.bom else self.codec

    def decode(self, data: bytes) -> str:
        return data[len(self.bom):].decode(self.codec)

    def encode(self, text: str, errors: str = "strict") -> bytes:
        return self.bom + text.encode(self.codec, errors)


_BOMS = (  # UTF-32 first: its LE BOM starts with the UTF-16 LE BOM
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

# C0 controls other than tab/newlines/form feed: real text (even UTF-16 text) has none.
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1a\x1c-\x1f]")

_ENCODING_HELP = ("텍스트 인코딩을 알 수 없습니다. UTF-8, CP949/EUC-KR, UTF-16 파일만 처리합니다. "
                  "메모장 등에서 UTF-8로 다시 저장한 뒤 시도하세요. "
                  "(unknown text encoding; supported: UTF-8, CP949/EUC-KR, UTF-16)")


def detect_encoding(data: bytes) -> TextEncoding:
    """BOM -> UTF-8/16/32; else strict UTF-8; else CP949 (covers EUC-KR); else raise."""
    for bom, codec in _BOMS:
        if data.startswith(bom):
            enc = TextEncoding(codec, bom)
            try:
                enc.decode(data)
            except UnicodeDecodeError as e:
                raise TextEncodingError(_ENCODING_HELP) from e
            return enc
    if b"\x00" in data:  # BOM-less UTF-16 (ASCII-heavy) or a binary file
        sample = data[:8192]
        even, odd = sample[0::2].count(0), sample[1::2].count(0)
        codec = "utf-16-le" if odd and not even else "utf-16-be" if even and not odd else None
        if codec and len(data) % 2 == 0:
            try:
                text = data.decode(codec)
            except UnicodeDecodeError:
                text = None
            if text is not None and not _CONTROL.search(text):
                return TextEncoding(codec)
        raise TextEncodingError("텍스트 파일이 아닌 것 같습니다 (바이너리 데이터 포함). "
                                "(this does not look like a text file: it contains binary data)")
    for codec in ("utf-8", "cp949"):
        try:
            data.decode(codec)
        except UnicodeDecodeError:
            continue
        return TextEncoding(codec)
    raise TextEncodingError(_ENCODING_HELP)


def read_text(path: Path) -> tuple[str, TextEncoding]:
    data = Path(path).read_bytes()
    enc = detect_encoding(data)
    return enc.decode(data), enc


def encode_text(text: str, enc: TextEncoding, report: Report) -> bytes:
    """Encode in the original encoding; unencodable replacement characters become '?'."""
    try:
        return enc.encode(text)
    except UnicodeEncodeError:
        report.warnings.append(
            f"일부 대체 문자를 {enc.codec}로 저장할 수 없어 '?'로 바꿨습니다 "
            f"(some replacement characters cannot be encoded in {enc.codec}; written as '?')")
        return enc.encode(text, errors="replace")


# ---------------------------------------------------------------------------------------------
# Typed values (a whole field is known to be PII: Excel 이름 column, JSON "phone" key ...)
# ---------------------------------------------------------------------------------------------

# Cell/field values that mean "nothing here"; never treated as PII nor registered as known.
NULL_WORDS = frozenset("""
- -- --- — – . .. ... ? x o n/a na n.a. none null nil nan unknown 없음 무 미상 모름 미기재 미입력
해당없음 해당무 해당사항없음 기타 합계 소계 총계 계 total subtotal sum 평균 avg average 본인확인
""".split()) | {"해당 없음", "해당 사항 없음"}

# Relations and roles that appear in name columns ("홍길동(모)", "본인") but are not names.
RELATION_WORDS = frozenset("""
본인 모 부 처 남편 아내 배우자 자녀 자 녀 아들 딸 형 누나 언니 오빠 동생 손자 손녀 며느리 사위 친구
지인 어머니 아버지 엄마 아빠 할머니 할아버지 모친 부친 조모 조부 형제 자매 가족 친척 이웃 보호자
환자 환자분 보호자분 담당 담당자 주치의 간호사 의사 직원 동료 대리인 법정대리인 시설 요양원
self mother father spouse son daughter wife husband friend parent guardian sibling brother sister
relative
""".split())

_PUNCT_ONLY = re.compile(r"[\W_]*")
_PERSON_SPLIT = re.compile(r"[,;/|·・、\n\r\t()\[\]{}<>（）]+|\s+(?:및|그리고|and|&|or)\s+", re.I)
_PHONE_SPLIT = re.compile(r"[,;|\n\r\t/]+|\s+(?:또는|or)\s+", re.I)
_ID_SPLIT = re.compile(r"[,;|\n\r\t]+|\s+/\s+")
_DATE_SPLIT = re.compile(r"[,;|\n\r\t]+")
_EMAIL_SPLIT = re.compile(r"[,;|\s/<>()\[\]]+")
_DIGIT = re.compile(r"[0-9]")
_HANGUL_SPACED = re.compile(r"[가-힣]+(?:\s+[가-힣]+)+")
_KEY_STRIP = re.compile(r"[\s\-.]+")


def is_null_value(text: str) -> bool:
    t = text.strip().casefold()
    return not t or bool(_PUNCT_ONLY.fullmatch(t)) or t in NULL_WORDS


def _honorifics() -> tuple[str, ...]:
    try:
        from ..recognizers.names import HONORIFICS
        hons = tuple(h for h in HONORIFICS if isinstance(h, str) and h)
    except (ImportError, TypeError):  # pragma: no cover - names module always present
        hons = ()
    return tuple(sorted(set(hons) | {"님", "씨", "환자", "보호자", "선생님", "교수님", "원장님"},
                        key=lambda h: (-len(h), h)))


_HONORIFIC_CACHE: tuple[str, ...] | None = None


def _strip_honorific(s: int, e: int, text: str) -> int:
    """New end offset with a trailing honorific ("홍길동 님", "김민준씨") removed."""
    global _HONORIFIC_CACHE
    if _HONORIFIC_CACHE is None:
        _HONORIFIC_CACHE = _honorifics()
    part = text[s:e]
    for h in _HONORIFIC_CACHE:
        if part.endswith(h):
            core = part[: -len(h)].rstrip()
            if len(core) >= 2:
                return s + len(core)
    return e


_STOPWORD_CACHE: frozenset[str] | None = None


def _name_stopwords() -> frozenset[str]:
    """Common words that start with a surname syllable (정상, 이상 ...); never registered as
    known names even when they sit in a name column."""
    global _STOPWORD_CACHE
    if _STOPWORD_CACHE is None:
        try:
            from ..recognizers.names import STOPWORDS
            _STOPWORD_CACHE = frozenset(w for w in STOPWORDS if isinstance(w, str))
        except (ImportError, TypeError):  # pragma: no cover
            _STOPWORD_CACHE = frozenset()
    return _STOPWORD_CACHE


def _name_like(part: str) -> bool:
    t = part.strip()
    return (bool(t) and not is_null_value(t) and t.casefold() not in RELATION_WORDS
            and not _DIGIT.search(t) and "@" not in t and any(ch.isalpha() for ch in t)
            and len(t) <= 60)


def _pieces(text: str, rx: re.Pattern | None, start: int = 0, end: int | None = None
            ) -> list[tuple[int, int]]:
    """(start, end) of the stripped, non-empty pieces of text[start:end] split by rx."""
    end = len(text) if end is None else end
    bounds, pos = [], start
    if rx is not None:
        for m in rx.finditer(text, start, end):
            bounds.append((pos, m.start()))
            pos = m.end()
    bounds.append((pos, end))
    out = []
    for s, e in bounds:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if s < e:
            out.append((s, e))
    return out


def _split_whitespace_ids(text: str, s: int, e: int) -> list[tuple[int, int]]:
    """'010-1234-5678 010-9876-5432' -> two pieces (only when 2+ tokens look like full numbers)."""
    toks = [(m.start(), m.end()) for m in re.finditer(r"\S+", text[s:e])]
    full = [t for t in toks if len(_DIGIT.findall(text[s + t[0]:s + t[1]])) >= 7]
    if len(full) < 2:
        return [(s, e)]
    return [(s + a, s + b) for a, b in toks if _DIGIT.search(text[s + a:s + b])]


def typed_spans(entity: str, text: str) -> list[Span]:
    """Spans for a value that is known to be `entity` because of its column/key name.

    The whole stripped value is the span, except that lists ("홍길동, 김민준",
    "010-1234-5678 / 02-123-4567") are split, relation words ("(모)", "본인") and empty
    markers ("-", "없음") are skipped, and honorifics are trimmed from names.
    """
    if not text or is_null_value(text):
        return []
    ranges: list[tuple[int, int]] = []
    if entity == E.PERSON:
        for s, e in _pieces(text, _PERSON_SPLIT):
            e = _strip_honorific(s, e, text)
            part = text[s:e]
            # A sentence in a name column ("메모: 최수아 님 연락") is left to the detector, which
            # still finds names there by honorific/label and by propagation of known names.
            if _name_like(part) and ":" not in part and len(part.split()) <= MAX_NAME_WORDS:
                ranges.append((s, e))
    elif entity == E.ADDRESS:
        ranges = _pieces(text, None)
    elif entity == E.EMAIL:
        ranges = [r for r in _pieces(text, _EMAIL_SPLIT) if "@" in text[r[0]:r[1]]]
        if not ranges:
            ranges = _pieces(text, None)
    else:
        rx = (_PHONE_SPLIT if entity == E.PHONE else _DATE_SPLIT if entity in (E.BIRTHDATE, E.DATE)
              else _ID_SPLIT)
        for s, e in _pieces(text, rx):
            if is_null_value(text[s:e]):
                continue
            if entity in (E.PHONE, E.RRN, E.FRN):
                ranges.extend(_split_whitespace_ids(text, s, e))
            else:
                ranges.append((s, e))
    return [Span(s, e, entity, text[s:e], TYPED_SCORE, TYPED_SOURCE) for s, e in ranges
            if not is_null_value(text[s:e])]


def _date_key(value: str) -> tuple[int, int, int] | None:
    try:
        from ..recognizers.patterns import parse_date
        y, m, d = parse_date(value)
    except Exception:  # noqa: BLE001 - best effort; an unparsable date simply has no key
        return None
    return (y, m, d) if y and m and d else None


def _value_key(value: str) -> str:
    return _KEY_STRIP.sub("", value).casefold()


# ---------------------------------------------------------------------------------------------
# Batch: detect over all text of one file at once
# ---------------------------------------------------------------------------------------------

Setter = Callable[[AnonResult], None]

# How much of the value an action hides; a typed span yields to a detected span of another
# entity that hides more (an RRN typed into a 연락처 column is redacted, not phone-masked).
ACTION_STRENGTH = {"keep": 0, "mask": 1, "generalize": 2, "pseudo": 3, "redact": 3}
FULL_STRENGTH = 3


@dataclass
class _Slot:
    text: str
    setter: Setter | None
    typed: list[Span] = field(default_factory=list)
    detect: bool = True    # False: typed spans already hide every character


class Batch:
    """Collect text slots of one file, detect them together, then hand results to setters.

    ``add(text, setter, entity=None)``: `setter(result)` is called after ``run()`` only when
    the anonymized text differs from `text`. With `entity` the whole value is treated as that
    entity (see typed_spans) and registered with ``Detector.add_known`` so the same value is
    also found in every other slot of the file.
    """

    def __init__(self, detector: "Detector", anonymizer: "Anonymizer", report: Report) -> None:
        self.detector = detector
        self.anonymizer = anonymizer
        self.report = report
        self.slots: list[_Slot] = []
        self._typed_values: dict[str, str] = {}          # normalized value -> entity
        self._birthdays: set[tuple[int, int, int]] = set()

    def __len__(self) -> int:
        return len(self.slots)

    def add_known(self, entity: str, value: str) -> None:
        value = (value or "").strip()
        if not value or is_null_value(value):
            return
        if not self.detector.cfg.is_enabled(entity):
            return
        if entity == E.PERSON and value in _name_stopwords():
            return
        self.detector.add_known(entity, value)
        if entity == E.PERSON and _HANGUL_SPACED.fullmatch(value):
            self.detector.add_known(entity, "".join(value.split()))   # "김 민준" -> "김민준"
        self._typed_values.setdefault(_value_key(value), entity)
        if entity == E.BIRTHDATE:
            key = _date_key(value)
            if key:
                self._birthdays.add(key)

    def add(self, text: str, setter: Setter | None, entity: str | None = None) -> None:
        if not isinstance(text, str) or not text.strip():
            return
        typed: list[Span] = []
        detect = True
        if entity and self.detector.cfg.is_enabled(entity):
            typed = typed_spans(entity, text)
            for sp in typed:
                self.add_known(entity, sp.text)
            # Nothing left to find when the typed spans cover all non-space text and their
            # action already hides the whole value (no stronger entity could take over).
            if typed and self._strength(entity) >= FULL_STRENGTH:
                inside = sum(1 for sp in typed for ch in sp.text if not ch.isspace())
                detect = inside < sum(1 for ch in text if not ch.isspace())
        self.slots.append(_Slot(text, setter, typed, detect))

    def _upgrade(self, spans: list[Span]) -> list[Span]:
        """A 'keep' span (DATE, URL) whose value is a typed value of another entity becomes
        that entity, e.g. a birth date from the 생년월일 column found as a DATE in a memo."""
        if not self._typed_values and not self._birthdays:
            return spans
        cfg, out, changed = self.detector.cfg, [], False
        for sp in spans:
            new_entity = None
            if cfg.action_for(sp.entity) == "keep":
                ent = self._typed_values.get(_value_key(sp.text))
                if ent and ent != sp.entity and cfg.action_for(ent) != "keep":
                    new_entity = ent
                elif sp.entity == E.DATE and self._birthdays and cfg.action_for(E.BIRTHDATE) != "keep":
                    if _date_key(sp.text) in self._birthdays:
                        new_entity = E.BIRTHDATE
            if new_entity:
                sp = Span(sp.start, sp.end, new_entity, sp.text, sp.score, sp.source)
                changed = True
            out.append(sp)
        return out if changed else spans

    def run(self) -> None:
        if not self.slots:
            return
        texts = list(dict.fromkeys(s.text for s in self.slots if s.detect))
        found = dict(zip(texts, self.detector.detect_document(texts))) if texts else {}
        for slot in self.slots:
            spans = self._upgrade(found.get(slot.text, []))
            if slot.typed:
                spans = merge_typed(slot.typed, spans, self._strength)
            res = self.anonymizer.anonymize(slot.text, spans)
            for r in res.replacements:
                self.report.add(r.entity)
            if res.text != slot.text and slot.setter is not None:
                slot.setter(res)

    def _strength(self, entity: str) -> int:
        return ACTION_STRENGTH.get(self.detector.cfg.action_for(entity), 3)


def _overlaps(a: Span, b: Span) -> bool:
    return a.start < b.end and b.start < a.end


def _covers(outer: Span, inner: Span) -> bool:
    return outer.start <= inner.start and inner.end <= outer.end


def merge_typed(typed: list[Span], detected: list[Span],
                strength: Callable[[str], int] | None = None) -> list[Span]:
    """Typed spans win over overlapping detected spans, unless a detected span of another
    entity covers the whole typed span and its action hides more (all typed spans of one
    value share one entity, so a promoted span replaces every typed span inside it)."""
    chosen: list[Span] = []
    for t in typed:
        pick = t
        if strength is not None:
            for sp in detected:
                if (_covers(sp, t) and sp.entity != t.entity
                        and strength(sp.entity) > strength(t.entity)
                        and all(_covers(sp, o) for o in typed if _overlaps(o, sp))):
                    pick = sp
                    break
        if pick not in chosen:
            chosen.append(pick)
    for sp in detected:
        if sp not in chosen and not any(_overlaps(sp, o) for o in chosen):
            chosen.append(sp)
    return sorted(chosen, key=lambda x: x.start)


def numeric_text(value: Any, entity: str | None) -> str | None:
    """Text to check for a number cell/field, or None to leave it alone.

    Typed: phone numbers lose their leading 0 when stored as numbers (1012345678 ->
    01012345678), RRNs lose leading zeros of 2000s births (zero-filled to 13 digits).
    Untyped: only integers with 5+ digits are worth checking (IDs, phone numbers, RRNs);
    9-10 digit numbers starting with 1 get their mobile-number 0 back.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer() or abs(value) >= 1e15:
            return str(value) if entity else None
        value = int(value)
    if not isinstance(value, int) or (value < 0 and not entity):
        return None
    s = str(abs(value))
    if entity == E.PHONE:
        if 8 <= len(s) <= 10 and not (len(s) == 8 and s[:2] in ("15", "16", "18")):
            return "0" + s
        return s
    if entity in (E.RRN, E.FRN):
        return s.zfill(13) if len(s) <= 13 else s
    if entity:
        return s
    if len(s) < 5:
        return None
    if len(s) in (9, 10) and s[0] == "1":
        return "0" + s
    if len(s) in (11, 12):
        return s.zfill(13)
    return s


# ---------------------------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------------------------


def split_paragraphs(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Split at blank lines (or, for huge paragraphs, at line ends) into ~max_chars chunks.

    Lines are never cut, and "".join(chunks) == text.
    """
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        cur.append(line)
        size += len(line)
        blank = not line.strip()
        if (size >= max_chars and blank) or size >= 2 * max_chars:
            chunks.append("".join(cur))
            cur, size = [], 0
    if cur:
        chunks.append("".join(cur))
    return chunks


def _text_slots(text: str, batch: Batch) -> list[str]:
    chunks = split_paragraphs(text)
    out = list(chunks)

    def setter(i: int) -> Setter:
        return lambda res: out.__setitem__(i, res.text)

    for i, c in enumerate(chunks):
        batch.add(c, setter(i))
    return out


def deid_text(text: str, detector: "Detector", anonymizer: "Anonymizer") -> tuple[str, Report]:
    """De-identify a plain text (stdin, clipboard). Report.source/output stay empty."""
    report = Report()
    batch = Batch(detector, anonymizer, report)
    out = _text_slots(text, batch)
    batch.run()
    return "".join(out), report


# ---------------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------------


class _Obj(list):
    """A JSON object as [[key, value], ...]: keeps key order and duplicate keys."""


def _json_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=lambda pairs: _Obj([list(p) for p in pairs]))


# Generic keys ("name") are PERSON only next to other personal fields or under a person key.
GENERIC_PERSON_KEYS = frozenset({"name", "이름", "fullname", "displayname"})
_PERSON_EVIDENCE_KEYS = frozenset({
    "gender", "sex", "age", "성별", "나이", "연령", "patient", "환자", "birth", "dob",
})
_PERSON_CONTEXT = (
    "patient", "person", "guardian", "doctor", "physician", "nurse", "author", "user", "member",
    "customer", "client", "employee", "staff", "contact", "owner", "subject", "people", "parent",
    "mother", "father", "spouse", "kin", "환자", "보호자", "의사", "작성자", "고객", "회원", "직원",
    "담당", "수진자", "가족", "신청인", "대상자",
)


def _person_context(name: str | None) -> bool:
    if not name:
        return False
    from .tabular import normalize_header
    n = normalize_header(name)
    return any(w in n for w in _PERSON_CONTEXT)


def field_entities(keys: list[str], parent: str | None = None) -> list[str | None]:
    """Entity per key of one JSON object / XML element group (None = free text)."""
    from .tabular import column_entity, normalize_header
    ents = [column_entity(k) if isinstance(k, str) else None for k in keys]
    norm = [normalize_header(k) if isinstance(k, str) else "" for k in keys]
    evidence = _person_context(parent) or any(
        (e and e != E.PERSON) or n in _PERSON_EVIDENCE_KEYS for e, n in zip(ents, norm))
    return [None if (e == E.PERSON and n in GENERIC_PERSON_KEYS and not evidence) else e
            for e, n in zip(ents, norm)]


class _JsonWalker:
    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        self.keys_changed = 0

    def _key_setter(self, pair: list) -> Setter:
        def set_key(res: AnonResult) -> None:
            pair[0] = res.text
            self.keys_changed += 1
        return set_key

    @staticmethod
    def _value_setter(container: list, idx: int) -> Setter:
        return lambda res: container.__setitem__(idx, res.text)

    def walk(self, node: Any, parent_key: str | None = None) -> None:
        if isinstance(node, _Obj):
            ents = field_entities([k for k, _ in node], parent_key)
            for pair, ent in zip(node, ents):
                self.batch.add(pair[0], self._key_setter(pair))
                self.visit(pair, 1, ent, pair[0])
        elif isinstance(node, list):
            for i in range(len(node)):
                self.visit(node, i, None, parent_key)

    def visit(self, container: list, idx: int, entity: str | None, key: str | None) -> None:
        value = container[idx]
        if isinstance(value, _Obj):
            self.walk(value, key)
        elif isinstance(value, list):  # ["010-...", "02-..."] under "phones" keeps the type
            for i in range(len(value)):
                self.visit(value, i, entity, key)
        elif isinstance(value, str):
            self.batch.add(value, self._value_setter(container, idx), entity)
        else:
            cand = numeric_text(value, entity)
            if cand is not None:
                self.batch.add(cand, self._value_setter(container, idx), entity)


@dataclass
class _JsonStyle:
    indent: str | None
    item_sep: str
    key_sep: str
    ensure_ascii: bool
    newline: str


def _json_style(text: str) -> _JsonStyle:
    m = re.search(r"[\[{][ \t]*\r?\n([ \t]*)\S", text)
    indent = m.group(1) if m else None
    key_sep = ": " if re.search(r'"[ \t]*:[ \t]+', text) else ":"
    item_sep = "," if indent is not None else (", " if re.search(r",[ \t]+\S", text) else ",")
    return _JsonStyle(indent, item_sep, key_sep, text.isascii() and "\\u" in text,
                      "\r\n" if "\r\n" in text else "\n")


def _json_dump(node: Any, st: _JsonStyle, level: int = 0) -> str:
    if isinstance(node, _Obj):
        items = [json.dumps(k, ensure_ascii=st.ensure_ascii) + st.key_sep + _json_dump(v, st, level + 1)
                 for k, v in node]
        return _json_container("{", "}", items, st, level)
    if isinstance(node, list):
        return _json_container("[", "]", [_json_dump(v, st, level + 1) for v in node], st, level)
    return json.dumps(node, ensure_ascii=st.ensure_ascii)


def _json_container(op: str, cl: str, items: list[str], st: _JsonStyle, level: int) -> str:
    if not items:
        return op + cl
    if st.indent is None:
        return op + st.item_sep.join(items) + cl
    pad = st.newline + st.indent * (level + 1)
    return op + pad + ("," + pad).join(items) + st.newline + st.indent * level + cl


def _deid_json(text: str, batch: Batch, report: Report, lines: bool) -> Callable[[], str] | None:
    """Register slots for a JSON (or JSON Lines) document; returns a renderer, or None if the
    text is not valid JSON."""
    st = _json_style(text)
    walker = _JsonWalker(batch)
    if lines:
        docs: list[tuple[str, Any, str] | str] = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            if not body.strip():
                docs.append(line)
                continue
            try:
                node = _json_loads(body)
            except ValueError:
                return None
            lead = body[: len(body) - len(body.lstrip())]
            docs.append((lead, node, line[len(lead) + len(body.strip()):]))
        holders = []
        for d in docs:
            if isinstance(d, tuple):
                holder = [d[1]]
                holders.append(holder)
                walker.visit(holder, 0, None, None)
            else:
                holders.append(None)
        line_style = _JsonStyle(None, st.item_sep, st.key_sep, st.ensure_ascii, st.newline)

        def render_lines() -> str:
            out = []
            for d, h in zip(docs, holders):
                out.append(d if h is None else d[0] + _json_dump(h[0], line_style) + d[2])
            _key_warning(walker, report)
            return "".join(out)

        return render_lines

    try:
        node = _json_loads(text)
    except ValueError:
        return None
    lead = text[: len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    holder = [node]
    walker.visit(holder, 0, None, None)

    def render() -> str:
        _key_warning(walker, report)
        return lead + _json_dump(holder[0], st) + tail

    return render


def _key_warning(walker: _JsonWalker, report: Report) -> None:
    if walker.keys_changed:
        report.warnings.append(
            f"JSON 키 {walker.keys_changed}개에 개인정보가 있어 키 이름을 바꿨습니다 "
            f"({walker.keys_changed} JSON key(s) contained personal data and were replaced)")


# ---------------------------------------------------------------------------------------------
# HTML / XML
# ---------------------------------------------------------------------------------------------

_TOKEN_RX = re.compile(r"""
    (?P<comment><!--)
  | (?P<cdata><!\[CDATA\[)
  | (?P<decl><![^>]*>)
  | (?P<pi><\?.*?(?:\?>|\Z))
  | (?P<tag></?(?P<name>[^\W\d][\w.:\-]*)(?P<attrs>(?:[^>"']|"[^"]*"|'[^']*')*)>)
""", re.S | re.X)
_ATTR_RX = re.compile(
    r"""(?P<name>[^\s=/>"']+)(?:\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<uq>[^\s"'=<>`]+)))?""")
_CHARREF = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_UNQUOTED_UNSAFE = re.compile(r"[\s\"'=<>`]")

# HTML attributes that hold structure, not content; everything else (alt, title, value,
# content, placeholder, href, src, data-*, aria-* ...) is checked.
HTML_SKIP_ATTRS = frozenset("""
class id style type rel lang xml:lang dir charset http-equiv width height colspan rowspan align
valign border cellpadding cellspacing bgcolor color target method enctype for role tabindex name
size maxlength minlength min max step pattern autocomplete accept media sizes crossorigin
integrity loading decoding referrerpolicy nonce slot form headers scope span start shape coords
itemprop itemtype itemscope property async defer hidden disabled checked selected readonly
required multiple novalidate draggable spellcheck translate contenteditable inputmode
""".split())
_META_PERSON = frozenset({"author", "creator", "dc.creator", "dcterms.creator", "citation_author",
                          "article:author", "designer", "owner", "twitter:creator"})
_META_EMAIL = frozenset({"reply-to"})


def _decode_refs(raw: str) -> tuple[str, list[int] | None, list[int] | None]:
    """Unescape character references; per decoded char, its raw (start, end) offsets."""
    if "&" not in raw:
        return raw, None, None
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    for m in _CHARREF.finditer(raw):
        for i in range(pos, m.start()):
            out.append(raw[i])
            starts.append(i)
            ends.append(i + 1)
        dec = _html.unescape(m.group())
        if dec == m.group():
            for i in range(m.start(), m.end()):
                out.append(raw[i])
                starts.append(i)
                ends.append(i + 1)
        else:
            for ch in dec:
                out.append(ch)
                starts.append(m.start())
                ends.append(m.end())
        pos = m.end()
    for i in range(pos, len(raw)):
        out.append(raw[i])
        starts.append(i)
        ends.append(i + 1)
    return "".join(out), starts, ends


def _apply_raw(raw: str, starts: list[int] | None, ends: list[int] | None, res: AnonResult,
               escape: Callable[[str], str]) -> str:
    parts: list[str] = []
    pos = 0
    for r in res.replacements:
        if r.replacement == r.original:
            continue
        rs = r.start if starts is None else starts[r.start]
        re_ = r.end if ends is None else ends[r.end - 1]
        rs = max(rs, pos)
        if re_ <= rs:
            continue
        parts += (raw[pos:rs], escape(r.replacement))
        pos = re_
    parts.append(raw[pos:])
    return "".join(parts)


def _escape_text(s: str) -> str:
    return _html.escape(s, quote=False)


def _escape_attr(s: str) -> str:
    return _html.escape(s, quote=True)


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


Pending = tuple[int, str, bool, Callable[[str], str]]   # (piece index, raw, decode, escape)


@dataclass
class _Frame:
    """An open XML element: text found directly inside it waits until we know whether the
    element is a leaf (only leaves are typed by their name: <name>홍길동</name>)."""

    name: str
    pending: list[Pending] = field(default_factory=list)
    has_child: bool = False


@dataclass
class _HtmlTable:
    """An open HTML <table>: cell text waits for </table> so columns can be typed by their
    header (<th>이름</th> ... <td>홍길동</td>) exactly like a spreadsheet."""

    rows: list[list[list[Pending]]] = field(default_factory=list)
    cell: list[Pending] | None = None


class _Markup:
    """Tokenize HTML/XML into raw pieces; text and selected attribute values become slots."""

    def __init__(self, src: str, batch: Batch, is_html: bool) -> None:
        self.src = src
        self.batch = batch
        self.html = is_html
        self.pieces: list[str] = []
        self.frames: list[_Frame] = []
        self.tables: list[_HtmlTable] = []
        self._classify_cache: dict[str, str | None] = {}

    # -- slots --------------------------------------------------------------------------

    def _register(self, idx: int, raw: str, decode: bool, escape: Callable[[str], str],
                  entity: str | None, unquoted: bool = False) -> None:
        if not raw.strip():
            return
        text, starts, ends = _decode_refs(raw) if decode else (raw, None, None)

        def setter(res: AnonResult) -> None:
            new = _apply_raw(raw, starts, ends, res, escape)
            if unquoted and _UNQUOTED_UNSAFE.search(new):
                new = '"' + new.replace('"', "&quot;") + '"'
            self.pieces[idx] = new

        self.batch.add(text, setter, entity)

    def _slot(self, raw: str, decode: bool, escape: Callable[[str], str], entity: str | None,
              unquoted: bool = False) -> None:
        idx = len(self.pieces)
        self.pieces.append(raw)
        self._register(idx, raw, decode, escape, entity, unquoted)

    def _content(self, raw: str, decode: bool, escape: Callable[[str], str]) -> None:
        """Element content (text or CDATA): typed later if the XML element turns out a leaf."""
        if not raw:
            return
        if self.html and self.tables and self.tables[-1].cell is not None:
            idx = len(self.pieces)
            self.pieces.append(raw)
            if raw.strip():
                self.tables[-1].cell.append((idx, raw, decode, escape))
            return
        if self.html or not self.frames:
            self._slot(raw, decode, escape, None)
            return
        idx = len(self.pieces)
        self.pieces.append(raw)
        if raw.strip():
            self.frames[-1].pending.append((idx, raw, decode, escape))

    def _flush(self, frame: _Frame, parent: str | None, typed: bool) -> None:
        entity = self._entity(frame.name, parent) if typed else None
        for idx, raw, decode, escape in frame.pending:
            self._register(idx, raw, decode, escape, entity)
        frame.pending.clear()

    def _entity(self, name: str, parent: str | None) -> str | None:
        """Typed entity for an XML element/attribute name (None = free text)."""
        key = f"{name}\x00{parent or ''}"
        if key not in self._classify_cache:
            self._classify_cache[key] = field_entities([_local(name)], parent and _local(parent))[0]
        return self._classify_cache[key]

    # -- tokenizer ------------------------------------------------------------------------

    def run(self) -> None:
        src, pos = self.src, 0
        while pos < len(src):
            m = _TOKEN_RX.search(src, pos)
            if not m:
                self._content(src[pos:], True, _escape_text)
                break
            self._content(src[pos:m.start()], True, _escape_text)
            if m.group("comment") or m.group("cdata"):
                closer = "-->" if m.group("comment") else "]]>"
                end = src.find(closer, m.end())
                body_end = len(src) if end < 0 else end
                self.pieces.append(m.group())
                if m.group("cdata"):
                    self._content(src[m.end():body_end], False, str)
                else:
                    self._slot(src[m.end():body_end], False, str, None)
                if end >= 0:
                    self.pieces.append(closer)
                pos = len(src) if end < 0 else end + len(closer)
            elif m.group("decl") or m.group("pi"):
                # <!DOCTYPE x [<!ENTITY p "홍길동">]> and <?pi ...?> can carry text too: the
                # body is checked as raw text (no character-reference decoding).
                tok = m.group()
                head = 2
                tail = 2 if m.group("pi") and tok.endswith("?>") else 1 if m.group("decl") else 0
                self.pieces.append(tok[:head])
                self._slot(tok[head:len(tok) - tail], False, str, None)
                self.pieces.append(tok[len(tok) - tail:])
                pos = m.end()
            else:
                pos = self._tag(m)
        while self.frames:  # unclosed elements: their text is still checked, untyped
            self._flush(self.frames.pop(), None, typed=False)
        while self.tables:  # unclosed tables
            self._close_table()

    def _parent_name(self) -> str | None:
        return self.frames[-2].name if len(self.frames) > 1 else None

    def _tag(self, m: re.Match) -> int:
        src = self.src
        name = m.group("name")
        closing = m.group().startswith("</")
        attrs = m.group("attrs")
        self_closing = attrs.rstrip().endswith("/")
        if not self.html and not closing and self.frames:
            top = self.frames[-1]
            top.has_child = True
            self._flush(top, self._parent_name(), typed=False)
        a0 = m.start("attrs")
        self.pieces.append(src[m.start():a0])
        self._attributes(name, attrs)
        self.pieces.append(">")
        lname = name.lower()
        if self.html:
            self._table_tag(lname, closing, self_closing)
        if self.html and not closing and not self_closing and lname in ("script", "style"):
            close = re.compile(rf"</{re.escape(name)}\s*>", re.I).search(src, m.end())
            body_end = close.start() if close else len(src)
            body = src[m.end():body_end]
            # <script> and <style> bodies are checked as raw text (CSS can hold
            # content: "..." strings, scripts hold data).
            self._slot(body, False, str, None)
            if close:
                self.pieces.append(close.group())
                return close.end()
            return len(src)
        if not self.html:
            if closing:
                if any(f.name == name for f in self.frames):
                    while self.frames:
                        frame = self.frames[-1]
                        match = frame.name == name
                        self._flush(frame, self._parent_name(), typed=match and not frame.has_child)
                        self.frames.pop()
                        if match:
                            break
            elif not self_closing:
                self.frames.append(_Frame(name))
        return m.end()

    def _table_tag(self, lname: str, closing: bool, self_closing: bool) -> None:
        if lname == "table":
            if closing:
                if self.tables:
                    self._close_table()
            elif not self_closing:
                self.tables.append(_HtmlTable())
            return
        if not self.tables:
            return
        table = self.tables[-1]
        if lname == "tr":
            table.cell = None
            if not closing:
                table.rows.append([])
        elif lname in ("td", "th"):
            if closing:
                table.cell = None
            else:
                if not table.rows:
                    table.rows.append([])
                table.cell = []
                table.rows[-1].append(table.cell)

    def _close_table(self) -> None:
        table = self.tables.pop()
        from .tabular import ColumnPolicy, build_layout, column_overrides
        texts: dict[int, dict[int, str]] = {}
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row):
                joined = "".join(_decode_refs(raw)[0] if decode else raw
                                 for _, raw, decode, _ in cell).strip()
                if joined:
                    texts.setdefault(r, {})[c] = joined
        layout = build_layout(texts, ColumnPolicy(column_overrides(self.batch.detector.cfg)))
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row):
                kind = layout.kind(r, c)
                entity = None if kind in (None, "skip", "text") else kind
                if entity and len(cell) > 1:   # "<b>홍길동</b> 님": register the names, check pieces
                    for sp in typed_spans(entity, texts.get(r, {}).get(c, "")):
                        self.batch.add_known(entity, sp.text)
                    entity = None
                for idx, raw, decode, escape in cell:
                    self._register(idx, raw, decode, escape, entity)

    def _attributes(self, tag: str, attrs: str) -> None:
        found = list(_ATTR_RX.finditer(attrs))
        meta_entity = None
        if self.html and tag.lower() == "meta":
            for am in found:
                if am.group("name").lower() in ("name", "property"):
                    v = (am.group("dq") or am.group("sq") or am.group("uq") or "").strip().lower()
                    meta_entity = (E.PERSON if v in _META_PERSON else E.EMAIL if v in _META_EMAIL
                                   else meta_entity)
        pos = 0
        for am in found:
            group = next((g for g in ("dq", "sq", "uq") if am.group(g) is not None), None)
            if group is None:
                continue
            aname = am.group("name")
            low = aname.lower()
            if low == "xmlns" or low.startswith(("xmlns:", "xsi:", "xml:")):
                continue
            if self.html:
                if low in HTML_SKIP_ATTRS:
                    continue
                entity = meta_entity if low == "content" else None
            else:
                entity = self._entity(aname, tag)
            vs, ve = am.span(group)
            self.pieces.append(attrs[pos:vs])
            self._slot(attrs[vs:ve], True, _escape_attr, entity, unquoted=group == "uq")
            pos = ve
        self.pieces.append(attrs[pos:])

    def render(self) -> str:
        return "".join(self.pieces)


def _deid_markup(text: str, batch: Batch, is_html: bool) -> Callable[[], str]:
    mk = _Markup(text, batch, is_html)
    mk.run()
    return mk.render


# ---------------------------------------------------------------------------------------------
# File entry point
# ---------------------------------------------------------------------------------------------


def deid_document(text: str, kind: str, detector: "Detector", anonymizer: "Anonymizer",
                  report: Report | None = None) -> tuple[str, Report]:
    """De-identify `text` as kind "text" | "json" | "jsonl" | "html" | "xml"."""
    report = report if report is not None else Report()
    batch = Batch(detector, anonymizer, report)
    render: Callable[[], str] | None = None
    if kind in ("json", "jsonl"):
        render = _deid_json(text, batch, report, lines=kind == "jsonl")
        if render is None:
            report.warnings.append("JSON 형식이 올바르지 않아 일반 텍스트로 처리했습니다 "
                                   "(invalid JSON: processed as plain text)")
            batch = Batch(detector, anonymizer, report)
    elif kind in ("html", "xml"):
        render = _deid_markup(text, batch, is_html=kind == "html")
    if render is None:
        out = _text_slots(text, batch)
        batch.run()
        return "".join(out), report
    batch.run()
    return render(), report


def kind_for(path: Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in JSON_EXTENSIONS:
        return "json"
    if ext in JSONL_EXTENSIONS:
        return "jsonl"
    if ext in HTML_EXTENSIONS:
        return "html"
    if ext in XML_EXTENSIONS:
        return "xml"
    return "text"


def process(src: Path, dst: Path, detector: "Detector", anonymizer: "Anonymizer",
            cfg: "Config") -> Report:
    """De-identify a text-like file into dst (same encoding, BOM and newline style)."""
    src, dst = Path(src), Path(dst)
    check_paths(src, dst)
    data = src.read_bytes()
    enc = detect_encoding(data)
    text = enc.decode(data)
    if src.suffix.lower() == ".rtf" or text.lstrip()[:5] == "{\\rtf":
        # RTF stores Korean as escapes (\'b1\'e8, \u54861) that no recognizer can see.
        raise ValueError("RTF 문서는 지원하지 않습니다. 워드/한글에서 .docx로 저장한 뒤 다시 시도하세요. "
                         "(RTF is not supported: save it as .docx first)")
    report = Report(source=str(src), output=str(dst))
    new, _ = deid_document(text, kind_for(src), detector, anonymizer, report)
    write_atomic(dst, encode_text(new, enc, report))
    return report


__all__ = (
    "EXTENSIONS", "TextEncoding", "TextEncodingError", "detect_encoding", "read_text",
    "encode_text", "write_atomic", "check_paths", "Batch", "typed_spans", "numeric_text",
    "split_paragraphs", "deid_text", "deid_document", "field_entities", "process",
)
