"""Detection engine: run recognizers, filter, resolve overlaps, propagate known values.

Text folding (before any recognizer runs):

* Decomposed Hangul (NFD, the macOS "자소 분리" form) and other non-NFC text is composed to
  NFC, keeping a map back to the original offsets.
* Full-width ASCII (U+FF01-U+FF5E: "０１０－１２３４", "ｈａｎａ＠ｎａｖｅｒ．ｃｏｍ") becomes ASCII and
  horizontal Unicode spaces (NBSP U+00A0, U+2000-U+200A, U+202F, U+205F, U+3000) become " ".
* Dash look-alikes between digits ("940305ㅡ2048117", "010–1234–5678") become "-".

Recognizers and propagation work on the folded text; returned spans always index the ORIGINAL
text (span.text == original[start:end]).

Propagation boundary rules (a found or registered value is searched as an exact substring):

* Values starting/ending with a Hangul (or other non-ASCII) letter must not touch another
  letter or digit on the left. On the right the next character must be a non-letter, or the
  value must be followed by a Korean particle/honorific. So with the value "김민준",
  "김민준이 왔다", "김민준씨가", "(김민준)" and "김민준_검사.xlsx" match, while "김민준호" and
  "박김민준" do not ("김민준호" is only found when "김민준호" itself is a known value).
* Two-syllable values ("이수") are stricter: the particles/honorifics after them must be followed
  by a non-letter ("이수가 왔다" matches, "이수정", "이수술" do not).
* ASCII letters use ASCII word boundaries ("John" does not match "Johnson"; a following Hangul
  particle is fine: "John이"). Digits use digit boundaries that also reject "-" or "." glue
  ("12345678" does not match inside "912345678" or "12345678-9").
* Emails match case-insensitively at email-character boundaries.
* Values shorter than 2 characters, and purely numeric values with fewer than 5 digits
  (they collide with years and lab values), are never propagated.
* Given names: for a Korean full name (surname + 2-syllable given name) the given name alone is
  also found when it is used the way Korean speakers address children and colleagues:
  "민준이가", "민준이 엄마", "서연아", "지우야", "하준씨", "수아쌤".

Values are looked up in hash sets at candidate positions (no giant regex), so tens of thousands
of known values (a whole patient list) cost milliseconds.

Overlaps: higher score wins, then the longer span, then the earlier one. A span whose action is
"keep" (URL, DATE by default) is a container: when it fully contains a span of another action
(a phone number, a name or an IP inside a URL) it is dropped and the inner span wins, so the
rest of the link stays as it was. Propagated values open a kept container the same way.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from typing import Callable, Iterable, Mapping, Tuple, Union

from . import entities as E
from .config import Config, default_config
from .recognizers.base import Recognizer
from .types import Span

PROPAGATE_ENTITIES = frozenset({E.PERSON, E.EMAIL, E.PHONE, E.RRN, E.MRN})
# Per-call extra values: {entity: [values]} or [(entity, value), ...].
KnownValues = Union[Mapping[str, Iterable[str]], Iterable[Tuple[str, str]]]
PROPAGATE_SCORE = 0.85
PROPAGATE_SOURCE = "propagate"
MIN_PROPAGATE_LEN = 2
MIN_PROPAGATE_DIGITS = 5
# Given-name propagation is skipped for very large name lists (a whole patient roster): thousands
# of given names collide with ordinary two-syllable words.
MAX_GIVEN_NAME_SOURCES = 300

# Used when recognizers.names (PARTICLES / HONORIFICS) is not available; merged with it otherwise.
_FALLBACK_PARTICLES = (
    "께서", "에게서", "한테서", "에게", "한테", "이랑", "하고", "랑", "와", "과",
    "이", "가", "은", "는", "을", "를", "의", "에", "께", "도", "만",
)
_FALLBACK_HONORIFICS = (
    "님", "씨", "환자분", "환자", "선생님", "선생", "원장님", "원장", "교수님", "교수", "간호사",
    "보호자", "군", "양", "할머니", "할아버지", "어머니", "아버지", "어르신", "과장", "팀장",
    "실장", "대표", "기사",
)

_NUM_PRE = r"(?<![0-9])(?<![0-9][-.])"
_NUM_POST = r"(?![0-9])(?![-.][0-9])"
_ASCII_PRE = r"(?<![A-Za-z0-9])"
_ASCII_POST = r"(?![A-Za-z0-9])"
_WORD_PRE = r"(?<![^\W_])"     # no letter/digit of any script before
_WORD_POST = r"(?![^\W_])"
_EMAIL_PRE = r"(?<![A-Za-z0-9._%+-])"
_EMAIL_POST = r"(?![A-Za-z0-9_%+-])(?!\.[A-Za-z0-9])"
_NUMERIC_VALUE = re.compile(r"[0-9\s\-.+()]+")
_PRE_FOR = {"num": _NUM_PRE, "ascii": _ASCII_PRE, "word": _WORD_PRE, "other": ""}

# ---------------------------------------------------------------------------- text folding

_FOLD_TABLE = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)}
_FOLD_TABLE.update({cp: 0x20 for cp in (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F,
                                         0x3000)})
_DIGIT_DASH = re.compile(r"(?<=[0-9])[ ]?[‐-―−﹣ㅡ][ ]?(?=[0-9])")
_NON_SPACE = re.compile(r"[^ ]")


def _joins(c: str) -> bool:
    """True if `c` composes with (or attaches to) the character before it."""
    return ("ᅠ" <= c <= "ᇿ" or "ힰ" <= c <= "퟿"
            or unicodedata.combining(c) != 0)


def _nfc_with_map(text: str) -> tuple[str, list[int], list[int]] | None:
    """NFC of `text` plus, per output char, the [start, end) of its source cluster."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and _joins(text[j]):
            j += 1
        piece = unicodedata.normalize("NFC", text[i:j])
        out.append(piece)
        starts.extend([i] * len(piece))
        ends.extend([j] * len(piece))
        i = j
    res = "".join(out)
    if res != unicodedata.normalize("NFC", text):  # exotic cross-cluster composition
        return None
    return res, starts, ends


class _Folded:
    """A recognizer-friendly copy of a text and the way back to the original offsets."""

    __slots__ = ("orig", "text", "starts", "ends")

    def __init__(self, orig: str) -> None:
        self.orig = orig
        self.starts: list[int] | None = None
        self.ends: list[int] | None = None
        text = orig
        if not orig.isascii():
            if not unicodedata.is_normalized("NFC", orig):
                mapped = _nfc_with_map(orig)
                if mapped is not None:
                    text, self.starts, self.ends = mapped
            text = _DIGIT_DASH.sub(lambda m: _NON_SPACE.sub("-", m.group()),
                                   text.translate(_FOLD_TABLE))
        self.text = orig if text == orig else text

    def to_orig(self, sp: Span) -> Span:
        if self.starts is None:
            if self.text is self.orig:
                return sp
            return Span(sp.start, sp.end, sp.entity, self.orig[sp.start:sp.end], sp.score,
                        sp.source, sp.group)
        s, e = self.starts[sp.start], self.ends[sp.end - 1]
        return Span(s, e, sp.entity, self.orig[s:e], sp.score, sp.source, sp.group)


def fold_text(text: str) -> str:
    """The folded form recognizers see (NFC, full-width -> ASCII, Unicode spaces -> ' ')."""
    return _Folded(text).text if text else text


# ---------------------------------------------------------------------------- recognizers


def _default_recognizers(cfg: Config) -> list[Recognizer]:
    """Pattern + address + name recognizers, plus the NER backend when cfg.ner["backend"] is set."""
    try:
        from .recognizers.address import AddressRecognizer
        from .recognizers.names import EnglishNameRecognizer, KoreanNameRecognizer
        from .recognizers.patterns import pattern_recognizers
    except ImportError as e:
        raise ImportError(
            f"deid default recognizers are unavailable ({e}); pass recognizers=[...] to Detector"
        ) from e
    recs: list[Recognizer] = list(pattern_recognizers(cfg))
    recs += [AddressRecognizer(), KoreanNameRecognizer(), EnglishNameRecognizer()]
    if (cfg.ner or {}).get("backend"):
        try:
            from .backends import build_ner_recognizer
        except ImportError as e:
            raise ImportError(f"NER backend {cfg.ner['backend']!r} is unavailable ({e})") from e
        ner = build_ner_recognizer(cfg)
        if ner is not None:
            recs.append(ner)
    return recs


_DOB_GAP = re.compile(r"[ ]?[(（][ ]*")


def _birthdate_after_name():
    try:
        from .recognizers.patterns import birthdate_after_name
    except ImportError:
        return None
    return birthdate_after_name


def _names_module():
    try:
        from .recognizers import names
    except ImportError:
        return None
    return names


def _suffix_lists() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(particles, honorifics), longest first: built-ins merged with recognizers.names if present."""
    parts, hons = set(_FALLBACK_PARTICLES), set(_FALLBACK_HONORIFICS)
    names = _names_module()
    for attr, target in (("PARTICLES", parts), ("HONORIFICS", hons)):
        try:
            target.update(v for v in getattr(names, attr, ()) or () if isinstance(v, str) and v)
        except TypeError:  # not an iterable of strings; keep the built-ins
            pass

    def order(xs: set[str]) -> tuple[str, ...]:
        return tuple(sorted(xs, key=lambda s: (-len(s), s)))

    return order(parts), order(hons)


def _char_kind(ch: str) -> str:
    if ch.isascii():
        return "num" if ch.isdigit() else "ascii" if ch.isalpha() else "other"
    return "word" if ch.isalnum() else "other"


def _is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


def _has_final(ch: str) -> bool:
    return _is_hangul(ch) and (ord(ch) - 0xAC00) % 28 != 0


class _SpanSet:
    """Non-overlapping spans kept sorted by start."""

    def __init__(self, spans: Iterable[Span] = ()) -> None:
        self.spans = sorted(spans, key=lambda s: s.start)
        self.starts = [s.start for s in self.spans]

    def overlapping(self, start: int, end: int) -> tuple[int, int]:
        """Index range [i, j) of spans overlapping [start, end)."""
        j = bisect_left(self.starts, end)
        i = j
        while i > 0 and self.spans[i - 1].end > start:
            i -= 1
        return i, j

    def put(self, i: int, j: int, span: Span) -> None:
        self.spans[i:j] = [span]
        self.starts[i:j] = [span.start]


def _open_containers(spans: list[Span], is_keep: Callable[[str], bool]) -> list[Span]:
    """Drop every "keep" span (URL, DATE) that fully contains a span of another action.

    A kept URL is a container, not a value: "https://emr.example/p?tel=010-2345-6789" keeps
    the link text but the phone number inside it is still masked. The rest of the container
    simply stays unchanged, which is what "keep" would have done.
    """
    keeps = [sp for sp in spans if is_keep(sp.entity)]
    if not keeps:
        return spans
    inner = sorted((sp for sp in spans if not is_keep(sp.entity)), key=lambda s: s.start)
    if not inner:
        return spans
    starts = [sp.start for sp in inner]
    opened: set[int] = set()
    for k in keeps:
        i = bisect_left(starts, k.start)
        while i < len(inner) and inner[i].start < k.end:
            x = inner[i]
            if x.end <= k.end:
                # The SAME text read two ways is not a container: the more confident reading
                # wins in resolve_overlaps (a rule DATE 0.85 over an LLM 생년월일/기타 0.7).
                if (x.start, x.end) == (k.start, k.end) and k.score > x.score:
                    i += 1
                    continue
                opened.add(id(k))
                break
            i += 1
    if not opened:
        return spans
    return [sp for sp in spans if id(sp) not in opened]


def resolve_overlaps(spans: Iterable[Span],
                     is_keep: Callable[[str], bool] | None = None) -> list[Span]:
    """Keep non-overlapping spans: higher score wins, then longer, then earlier start.

    `is_keep(entity)`: True for entities whose action is "keep". Such a span loses to any
    other span it fully contains (see _open_containers) - except a span of exactly the same
    range with a lower score; partial overlaps use the scores.
    """
    if is_keep is not None:
        spans = _open_containers(list(spans), is_keep)
    kept = _SpanSet()
    order = sorted(spans, key=lambda s: (-s.score, s.start - s.end, s.start, s.entity, s.source))
    for sp in order:
        i, j = kept.overlapping(sp.start, sp.end)
        if i == j:
            kept.put(i, j, sp)
    return kept.spans


# ---------------------------------------------------------------------------- value matching


class _ValueMatcher:
    """Exact-substring search for a set of values with the propagation boundary rules.

    Equivalent to one alternation regex ``pre(v1|v2|...)post`` with the values longest first,
    but implemented as candidate positions (a tiny char-class regex) plus hash lookups.
    """

    def __init__(self, entity: str, values: Iterable[str], posts: dict) -> None:
        self.entity = entity
        self.icase = entity == E.EMAIL
        self.values = set(values)
        lengths: dict[str, set[int]] = {}
        for v in self.values:
            lengths.setdefault(v[0], set()).add(len(v))
        self.lengths = {c: sorted(ls, reverse=True) for c, ls in lengths.items()}
        self.posts = posts
        by_kind: dict[str, list[str]] = {}
        for c in self.lengths:
            kind = "email" if self.icase else _char_kind(c)
            by_kind.setdefault(kind, []).append(c)
        alts = []
        for kind, chars in sorted(by_kind.items()):
            pre = _EMAIL_PRE if kind == "email" else _PRE_FOR[kind]
            alts.append(f"{pre}[{''.join(re.escape(c) for c in sorted(chars))}]")
        self.rx = re.compile("|".join(alts), re.IGNORECASE if self.icase else 0) if alts else None

    def _post_ok(self, value: str, text: str, end: int) -> bool:
        if self.icase:
            return self.posts["email"].match(text, end) is not None
        kind = _char_kind(value[-1])
        if kind == "word":
            kind = "word_short" if len(value) <= 2 else "word_long"
        rx = self.posts.get(kind)
        return rx is None or rx.match(text, end) is not None

    def find(self, text: str) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        if self.rx is None:
            return out
        last, n = 0, len(text)
        for m in self.rx.finditer(text):
            p = m.start()
            if p < last:
                continue
            first = text[p].lower() if self.icase else text[p]
            for length in self.lengths.get(first, ()):
                e = p + length
                if e > n:
                    continue
                key = text[p:e].lower() if self.icase else text[p:e]
                if key in self.values and self._post_ok(key, text, e):
                    out.append((p, e))
                    last = e
                    break
        return out


# Given name + the forms Koreans use for children / colleagues: 민준이가, 민준이 엄마, 서연아,
# 지우야, 하준씨, 수아쌤. 이/아 only follow a final consonant, 야 only a vowel.
_GIVEN_PARTICLES = ("가", "는", "를", "도", "랑", "랑은", "랑도", "한테", "한테는", "에게", "의", "네",
                    "나", "만", "와", "야", "고", "요", "가요", "는요", "도요", "라고", "라는", "였", "였다",
                    "예요", "에요", "께", "까지", "부터", "처럼", "보다", "하고")
_GIVEN_HON = r"(?:님|씨|쌤|선생님|샘)(?:이|가|은|는|을|를|께|께서|의|도|한테|에게|이랑|랑|과|와)?"
_GIVEN_TAIL_FINAL = re.compile(
    rf"(?:이(?:{'|'.join(sorted(_GIVEN_PARTICLES, key=len, reverse=True))})?|아|{_GIVEN_HON})"
    + _WORD_POST
)
_GIVEN_TAIL_OPEN = re.compile(rf"(?:야|{_GIVEN_HON})" + _WORD_POST)


class _GivenNameMatcher:
    """Find the 2-syllable given name of known full names in its address/hypocoristic forms."""

    def __init__(self, givens: Iterable[str]) -> None:
        self.values = set(givens)
        firsts = sorted({g[0] for g in self.values})
        self.rx = re.compile(f"{_WORD_PRE}[{''.join(map(re.escape, firsts))}]") if firsts else None

    def find(self, text: str) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        if self.rx is None:
            return out
        last = 0
        for m in self.rx.finditer(text):
            p = m.start()
            if p < last:
                continue
            g = text[p:p + 2]
            if len(g) < 2 or g not in self.values:
                continue
            tail = _GIVEN_TAIL_FINAL if _has_final(g[-1]) else _GIVEN_TAIL_OPEN
            if tail.match(text, p + 2):
                out.append((p, p + 2))
                last = p + 2
        return out


def _given_name(value: str, surnames1, surnames2) -> str | None:
    """The 2-syllable given name of a Hangul full name ("김민준" -> "민준"), else None."""
    if not all(_is_hangul(c) for c in value):
        return None
    if len(value) == 3 and value[0] in surnames1:
        return value[1:]
    if len(value) == 4 and value[:2] in surnames2:
        return value[2:]
    return None


# ---------------------------------------------------------------------------- detector


class Detector:
    """Find PII spans in text using a list of recognizers plus value propagation.

    A recognizer may also define ``find_batch(texts, cfg) -> list[list[Span]]`` (the
    OpenAI-compatible LLM backend): detect_document() then calls it ONCE with all chunks of
    the document instead of find() per chunk, so a sequential LLM server gets few requests.
    Recognizers that degrade instead of failing put messages in their ``warnings`` list;
    the detector collects them in ``self.warnings`` (drain_warnings()) for the Report.
    Exceptions from recognizers (e.g. ``LLMError`` with on_error = "fail") propagate.
    """

    def __init__(self, cfg: Config | None = None, recognizers: list[Recognizer] | None = None,
                 extra_recognizers: list[Recognizer] | None = None) -> None:
        self.cfg = cfg if cfg is not None else default_config()
        recs = _default_recognizers(self.cfg) if recognizers is None else list(recognizers)
        self.recognizers: list[Recognizer] = recs + list(extra_recognizers or [])
        # Messages from recognizers that degraded without failing (e.g. the LLM backend with
        # on_error = "rules_only"). Never contain document text; callers drain them
        # (drain_warnings) into their Report.
        self.warnings: list[str] = []
        self._known: dict[str, set[str]] = {}
        self._particles, self._honorifics = _suffix_lists()
        parts = "|".join(map(re.escape, self._particles))
        hons = "|".join(map(re.escape, self._honorifics))
        suffixes = "|".join(map(re.escape, sorted(self._particles + self._honorifics,
                                                  key=lambda s: (-len(s), s))))
        self._ko_post_long = f"(?:{_WORD_POST}|(?=(?:{suffixes})))"
        self._ko_post_short = f"(?=(?:{hons})?(?:{parts}){{0,2}}{_WORD_POST})"
        self._posts = {
            "num": re.compile(_NUM_POST), "ascii": re.compile(_ASCII_POST),
            "word_long": re.compile(self._ko_post_long),
            "word_short": re.compile(self._ko_post_short),
            "email": re.compile(_EMAIL_POST),
        }
        names = _names_module()
        self._surnames1 = frozenset(getattr(names, "SURNAMES1", ()) or ())
        self._surnames2 = frozenset(getattr(names, "SURNAMES2", ()) or ())
        self._given_stop = frozenset(getattr(names, "STOPWORDS", ()) or ())

    # ------------------------------------------------------------------ public API

    def add_known(self, entity: str, value: str) -> None:
        """Register a value that must be found everywhere via propagation."""
        value = fold_text((value or "").strip()).strip()
        if len(value) >= MIN_PROPAGATE_LEN:
            self._known.setdefault(entity, set()).add(value)

    def clear_known(self) -> None:
        """Forget every value registered with add_known()."""
        self._known.clear()

    def drain_warnings(self) -> list[str]:
        """Pending warnings (see `warnings`), removed from the detector."""
        out = list(self.warnings)
        self.warnings.clear()
        return out

    def detect(self, text: str, known: KnownValues | None = None) -> list[Span]:
        """Sorted, non-overlapping spans for one text (propagation within this text)."""
        return self.detect_document([text], known=known)[0]

    def detect_document(self, chunks: list[str], known: KnownValues | None = None) -> list[list[Span]]:
        """Like detect() per chunk, but values found in any chunk propagate to all chunks.

        `known`: extra values for this call only, as {entity: [values]} or [(entity, value)].
        """
        folded = [_Folded(c) if c else None for c in chunks]
        allow = self._allow()
        try:
            # Recognizers with find_batch (the LLM backend) run ONCE for the whole document.
            batched = self._find_batches([f.text for f in folded if f is not None])
            base: list[list[Span]] = []
            k = 0
            for f in folded:
                if f is None:
                    base.append([])
                    continue
                pre = {rid: results[k] for rid, results in batched.items()}
                base.append(self._detect_base(f.text, allow, pre))
                k += 1
        finally:
            self._collect_warnings()
        # Person groups (LLM backend): only groups whose canonical spelling is itself a PERSON
        # span of this document count, and every span of a grouped spelling gets the group -
        # also where the request that saw it could not know the full name.
        groups, persons = _person_groups(base)
        base = [_regroup(spans, groups, persons) for spans in base]
        matchers = self._matchers(self._collect_values(base, self._extra_known(known)))
        out = []
        for f, spans in zip(folded, base):
            if f is None:
                out.append([])
                continue
            spans = self._birth_after_person(f.text,
                                             self._propagate(f.text, spans, matchers, groups))
            out.append([f.to_orig(sp) for sp in spans])
        return out

    # ------------------------------------------------------------------ internals

    def _birth_after_person(self, text: str, spans: list[Span]) -> list[Span]:
        """A DATE in parentheses right after a name is the birth date of the chart header:
        "환자 표다은(1979.04.05)", "표다은 (1979-04-05, #20934411)" -> BIRTHDATE."""
        if len(spans) < 2 or not self.cfg.is_enabled(E.BIRTHDATE):
            return spans
        check = _birthdate_after_name()
        if check is None:
            return spans
        out = None
        for i in range(1, len(spans)):
            sp, prev = spans[i], spans[i - 1]
            if sp.entity != E.DATE or prev.entity != E.PERSON:
                continue
            if not _DOB_GAP.fullmatch(text, prev.end, sp.start) or not check(text, sp.start, sp.end):
                continue
            if out is None:
                out = list(spans)
            out[i] = Span(sp.start, sp.end, E.BIRTHDATE, sp.text, sp.score, sp.source)
        return spans if out is None else out

    @staticmethod
    def _extra_known(known: KnownValues | None) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        if not known:
            return out
        pairs = known.items() if isinstance(known, Mapping) else known
        for ent, vals in pairs:
            if isinstance(vals, str):
                vals = [vals]
            for v in vals:
                v = fold_text((v or "").strip()).strip()
                if len(v) >= MIN_PROPAGATE_LEN:
                    out.setdefault(ent, set()).add(v)
        return out

    def _allow(self) -> set[str]:
        """Allowlist entries as written and in folded form."""
        allow = set(self.cfg.allowlist)
        return allow | {fold_text(a) for a in allow}

    def _find_batches(self, texts: list[str]) -> dict[int, list[list[Span]]]:
        """{id(recognizer): spans per text} for recognizers with find_batch (one call for the
        whole document: the LLM backend packs short texts into few requests). Only used for
        more than one text; a single text goes through find()."""
        out: dict[int, list[list[Span]]] = {}
        if len(texts) < 2:
            return out
        for rec in self.recognizers:
            find_batch = getattr(rec, "find_batch", None)
            if not callable(find_batch):
                continue
            res = list(find_batch(texts, self.cfg))
            if len(res) != len(texts):
                raise ValueError(f"recognizer {getattr(rec, 'name', rec)!r} returned "
                                 f"{len(res)} results for {len(texts)} texts")
            out[id(rec)] = res
        return out

    def _collect_warnings(self) -> None:
        for rec in self.recognizers:
            pending = getattr(rec, "warnings", None)
            if isinstance(pending, list) and pending:
                for w in pending:
                    if isinstance(w, str) and w not in self.warnings:
                        self.warnings.append(w)
                pending.clear()

    def _detect_base(self, text: str, allow: set[str] | None = None,
                     pre: dict[int, list[Span]] | None = None) -> list[Span]:
        if not text:
            return []
        cfg = self.cfg
        if allow is None:
            allow = self._allow()
        found: list[Span] = []
        for rec in self.recognizers:
            spans = pre[id(rec)] if pre and id(rec) in pre else rec.find(text, cfg)
            for sp in spans:
                if text[sp.start:sp.end] != sp.text:
                    raise ValueError(f"recognizer {getattr(rec, 'name', rec)!r} returned a span "
                                     f"that does not match the text at {sp.start}:{sp.end}")
                if cfg.is_enabled(sp.entity) and sp.text not in allow and sp.score >= cfg.min_score:
                    found.append(sp)
        return _carry_groups(resolve_overlaps(found, self._is_keep), found)

    def _is_keep(self, entity: str) -> bool:
        return self.cfg.action_for(entity) == "keep"

    def _collect_values(self, span_lists: list[list[Span]],
                        extra: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
        values = {ent: set(vs) for ent, vs in self._known.items()}
        for ent, vs in (extra or {}).items():
            values.setdefault(ent, set()).update(vs)
        values.setdefault(E.PERSON, set()).update(fold_text(n.strip()) for n in self.cfg.names)
        detected_persons: set[str] = set()
        for spans in span_lists:
            for sp in spans:
                if sp.entity in PROPAGATE_ENTITIES:
                    values.setdefault(sp.entity, set()).add(sp.text.strip())
                    if sp.entity == E.PERSON:
                        detected_persons.add(sp.text.strip())
        allow = self._allow()
        out: dict[str, set[str]] = {}
        for ent, vs in values.items():
            if not self.cfg.is_enabled(ent):
                continue
            keep = {v for v in vs if v not in allow and _propagatable(v)}
            if ent == E.EMAIL:
                keep = {v.lower() for v in keep}
            if keep:
                out[ent] = keep
        if E.PERSON in out:
            sources = set(detected_persons)
            sources.update(fold_text(n.strip()) for n in self.cfg.names)
            bulk = self._known.get(E.PERSON, set()) | (extra or {}).get(E.PERSON, set())
            if len(bulk) <= MAX_GIVEN_NAME_SOURCES:
                sources |= bulk
            givens = set()
            for v in sources & out[E.PERSON]:
                g = _given_name(v, self._surnames1, self._surnames2)
                if g and g not in allow and g not in self._given_stop:
                    givens.add(g)
            if givens:
                out[_GIVEN] = givens
        return out

    def _matchers(self, values: dict[str, set[str]]) -> list[tuple[str, object]]:
        """One value matcher per entity (plus one for given names)."""
        out: list[tuple[str, object]] = []
        for ent in sorted(values):
            if ent == _GIVEN:
                out.append((E.PERSON, _GivenNameMatcher(values[ent])))
            else:
                out.append((ent, _ValueMatcher(ent, values[ent], self._posts)))
        return out

    def _propagate(self, text: str, spans: list[Span], matchers: list[tuple[str, object]],
                   groups: dict[tuple[str, str], str] | None = None) -> list[Span]:
        """Add propagated matches that do not clash with detected spans.

        A match is added where nothing overlaps it, or replaces same-entity spans it fully
        contains (extending a partial detection), or replaces spans whose action is "keep"
        (a DATE that is a known BIRTHDATE). A non-"keep" match inside a "keep" span opens it
        (a known name in a kept URL path is masked; the rest of the URL stays). It never
        displaces another entity otherwise.
        """
        if not text or not matchers:
            return spans
        groups = groups or {}
        cands = [Span(s, e, ent, text[s:e], PROPAGATE_SCORE, PROPAGATE_SOURCE,
                      groups.get((ent, text[s:e])))
                 for ent, matcher in matchers for s, e in matcher.find(text)]
        if not cands:
            return spans
        cfg = self.cfg
        kept = _SpanSet(spans)
        for c in sorted(cands, key=lambda s: (s.start - s.end, s.start, s.entity)):
            i, j = kept.overlapping(c.start, c.end)
            hits = kept.spans[i:j]
            c_keep = cfg.action_for(c.entity) == "keep"

            def yields(h: Span) -> bool:
                if not (c.start <= h.start and h.end <= c.end):
                    # c inside a kept container (URL): the container gives way
                    return (not c_keep and h.start <= c.start and c.end <= h.end
                            and cfg.action_for(h.entity) == "keep")
                if h.entity == c.entity:
                    return (h.start, h.end) != (c.start, c.end)
                return not c_keep and cfg.action_for(h.entity) == "keep"

            if not hits or all(yields(h) for h in hits):
                kept.put(i, j, c)
        return kept.spans


_GIVEN = "\0given"   # internal key of the given-name value set


def _carry_groups(kept: list[Span], found: list[Span]) -> list[Span]:
    """A person group (LLM backend) survives losing an overlap: the winning span of the same
    entity takes the group of a grouped span it overlaps (a rule found "서윤" with a higher
    score than the LLM span that knew it is "박서윤")."""
    grouped = [sp for sp in found if sp.group]
    if not grouped:
        return kept
    out = []
    for sp in kept:
        if sp.group is None:
            g = next((x.group for x in grouped
                      if x.entity == sp.entity and x.start < sp.end and sp.start < x.end), None)
            if g is not None:
                sp = Span(sp.start, sp.end, sp.entity, sp.text, sp.score, sp.source, g)
        out.append(sp)
    return out


def _person_groups(span_lists: list[list[Span]]
                   ) -> tuple[dict[tuple[str, str], str], set[str]]:
    """((entity, value) -> person group, PERSON span texts) for the spans of one document.

    A group counts only when its canonical spelling is itself a detected PERSON span of the
    document (a model that writes person "어머니" for "오정애" does not make "어머니" a name
    or the restore value). The first valid group of a spelling is used where a span of that
    spelling has none, so "서윤" gets the pseudonym of "박서윤" in every chunk and request,
    and a propagated copy keeps it too."""
    persons = {sp.text for spans in span_lists for sp in spans if sp.entity == E.PERSON}
    out: dict[tuple[str, str], str] = {}
    for spans in span_lists:
        for sp in spans:
            if sp.group and sp.group in persons and sp.group != sp.text:
                out.setdefault((sp.entity, sp.text), sp.group)
    return out, persons


def _regroup(spans: list[Span], groups: dict[tuple[str, str], str], persons: set[str]
             ) -> list[Span]:
    """Spans with their person group checked: an invalid group is dropped, a missing one is
    filled in from `groups` (see _person_groups)."""
    if not groups and not any(sp.group for sp in spans):
        return spans
    out = []
    for sp in spans:
        own = sp.group if sp.group and sp.group in persons and sp.group != sp.text else None
        g = own or groups.get((sp.entity, sp.text))
        if g != sp.group:
            sp = Span(sp.start, sp.end, sp.entity, sp.text, sp.score, sp.source, g)
        out.append(sp)
    return out


def _propagatable(value: str) -> bool:
    if len(value) < MIN_PROPAGATE_LEN:
        return False
    if _NUMERIC_VALUE.fullmatch(value):
        return sum(ch.isdigit() for ch in value) >= MIN_PROPAGATE_DIGITS
    return True
