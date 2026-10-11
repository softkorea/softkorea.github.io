"""Detection engine: run recognizers, filter, resolve overlaps, propagate known values.

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
"""
from __future__ import annotations

import re
from bisect import bisect_left
from typing import Iterable

from . import entities as E
from .config import Config, default_config
from .recognizers.base import Recognizer
from .types import Span

PROPAGATE_ENTITIES = frozenset({E.PERSON, E.EMAIL, E.PHONE, E.RRN, E.MRN})
PROPAGATE_SCORE = 0.85
PROPAGATE_SOURCE = "propagate"
MIN_PROPAGATE_LEN = 2
MIN_PROPAGATE_DIGITS = 5

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


def _suffix_lists() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(particles, honorifics), longest first: built-ins merged with recognizers.names if present."""
    parts, hons = set(_FALLBACK_PARTICLES), set(_FALLBACK_HONORIFICS)
    try:
        from .recognizers import names
    except ImportError:
        names = None
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


def resolve_overlaps(spans: Iterable[Span]) -> list[Span]:
    """Keep non-overlapping spans: higher score wins, then longer, then earlier start."""
    kept = _SpanSet()
    order = sorted(spans, key=lambda s: (-s.score, s.start - s.end, s.start, s.entity, s.source))
    for sp in order:
        i, j = kept.overlapping(sp.start, sp.end)
        if i == j:
            kept.put(i, j, sp)
    return kept.spans


class Detector:
    """Find PII spans in text using a list of recognizers plus value propagation."""

    def __init__(self, cfg: Config | None = None, recognizers: list[Recognizer] | None = None,
                 extra_recognizers: list[Recognizer] | None = None) -> None:
        self.cfg = cfg if cfg is not None else default_config()
        recs = _default_recognizers(self.cfg) if recognizers is None else list(recognizers)
        self.recognizers: list[Recognizer] = recs + list(extra_recognizers or [])
        self._known: dict[str, set[str]] = {}
        self._particles, self._honorifics = _suffix_lists()
        parts = "|".join(map(re.escape, self._particles))
        hons = "|".join(map(re.escape, self._honorifics))
        suffixes = "|".join(map(re.escape, sorted(self._particles + self._honorifics,
                                                  key=lambda s: (-len(s), s))))
        self._ko_post_long = f"(?:{_WORD_POST}|(?=(?:{suffixes})))"
        self._ko_post_short = f"(?=(?:{hons})?(?:{parts}){{0,2}}{_WORD_POST})"

    # ------------------------------------------------------------------ public API

    def add_known(self, entity: str, value: str) -> None:
        """Register a value that must be found everywhere via propagation."""
        value = (value or "").strip()
        if len(value) >= MIN_PROPAGATE_LEN:
            self._known.setdefault(entity, set()).add(value)

    def detect(self, text: str) -> list[Span]:
        """Sorted, non-overlapping spans for one text (propagation within this text)."""
        return self.detect_document([text])[0]

    def detect_document(self, chunks: list[str]) -> list[list[Span]]:
        """Like detect() per chunk, but values found in any chunk propagate to all chunks."""
        base = [self._detect_base(c) for c in chunks]
        matchers = self._matchers(self._collect_values(base))
        return [self._propagate(c, spans, matchers) for c, spans in zip(chunks, base)]

    # ------------------------------------------------------------------ internals

    def _detect_base(self, text: str) -> list[Span]:
        if not text:
            return []
        cfg, allow = self.cfg, set(self.cfg.allowlist)
        found: list[Span] = []
        for rec in self.recognizers:
            for sp in rec.find(text, cfg):
                if text[sp.start:sp.end] != sp.text:
                    raise ValueError(f"recognizer {getattr(rec, 'name', rec)!r} returned a span "
                                     f"that does not match the text at {sp.start}:{sp.end}")
                if cfg.is_enabled(sp.entity) and sp.text not in allow and sp.score >= cfg.min_score:
                    found.append(sp)
        return resolve_overlaps(found)

    def _collect_values(self, span_lists: list[list[Span]]) -> dict[str, set[str]]:
        values = {ent: set(vs) for ent, vs in self._known.items()}
        values.setdefault(E.PERSON, set()).update(n.strip() for n in self.cfg.names)
        for spans in span_lists:
            for sp in spans:
                if sp.entity in PROPAGATE_ENTITIES:
                    values.setdefault(sp.entity, set()).add(sp.text.strip())
        allow = set(self.cfg.allowlist)
        out: dict[str, set[str]] = {}
        for ent, vs in values.items():
            if not self.cfg.is_enabled(ent):
                continue
            keep = {v for v in vs if v not in allow and _propagatable(v)}
            if ent == E.EMAIL:
                keep = {v.lower() for v in keep}
            if keep:
                out[ent] = keep
        return out

    def _post(self, value: str) -> str:
        kind = _char_kind(value[-1])
        if kind == "num":
            return _NUM_POST
        if kind == "ascii":
            return _ASCII_POST
        if kind == "word":
            return self._ko_post_short if len(value) <= 2 else self._ko_post_long
        return ""

    def _matchers(self, values: dict[str, set[str]]) -> list[tuple[str, re.Pattern]]:
        """One combined regex per entity; alternatives grouped by first character."""
        pre_for = {"num": _NUM_PRE, "ascii": _ASCII_PRE, "word": _WORD_PRE, "other": ""}
        out = []
        for ent in sorted(values):
            groups: dict[str, list[str]] = {}
            for v in sorted(values[ent], key=lambda s: (-len(s), s)):
                groups.setdefault(v[0], []).append(v)
            alts = []
            for first, vals in sorted(groups.items()):
                if ent == E.EMAIL:
                    pre, rests = _EMAIL_PRE, [re.escape(v[1:]) + _EMAIL_POST for v in vals]
                else:
                    pre = pre_for[_char_kind(first)]
                    rests = [re.escape(v[1:]) + self._post(v) for v in vals]
                alts.append(f"{pre}{re.escape(first)}(?:{'|'.join(rests)})")
            out.append((ent, re.compile("|".join(alts), re.IGNORECASE if ent == E.EMAIL else 0)))
        return out

    def _propagate(self, text: str, spans: list[Span],
                   matchers: list[tuple[str, re.Pattern]]) -> list[Span]:
        """Add propagated matches that do not clash with detected spans.

        A match is added where nothing overlaps it, or replaces same-entity spans it fully
        contains (extending a partial detection). It never displaces a different entity.
        """
        if not text or not matchers:
            return spans
        cands = [Span(m.start(), m.end(), ent, m.group(), PROPAGATE_SCORE, PROPAGATE_SOURCE)
                 for ent, rx in matchers for m in rx.finditer(text)]
        kept = _SpanSet(spans)
        for c in sorted(cands, key=lambda s: (s.start - s.end, s.start, s.entity)):
            i, j = kept.overlapping(c.start, c.end)
            hits = kept.spans[i:j]
            if not hits or all(h.entity == c.entity and c.start <= h.start and h.end <= c.end
                               and (h.start, h.end) != (c.start, c.end) for h in hits):
                kept.put(i, j, c)
        return kept.spans


def _propagatable(value: str) -> bool:
    if len(value) < MIN_PROPAGATE_LEN:
        return False
    if _NUMERIC_VALUE.fullmatch(value):
        return sum(ch.isdigit() for ch in value) >= MIN_PROPAGATE_DIGITS
    return True
