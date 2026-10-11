"""Recognizer protocol and small shared helpers."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from ..types import Span

if TYPE_CHECKING:
    from ..config import Config


class Recognizer(Protocol):
    name: str
    entities: tuple[str, ...]

    def find(self, text: str, cfg: "Config") -> list[Span]: ...


def context_before(text: str, start: int, window: int = 20) -> str:
    return text[max(0, start - window):start].lower()


def context_around(text: str, start: int, end: int, window: int = 20) -> str:
    return text[max(0, start - window):min(len(text), end + window)].lower()


def make_span(text: str, start: int, end: int, entity: str, score: float, source: str) -> Span:
    return Span(start=start, end=end, entity=entity, text=text[start:end], score=score, source=source)


class RegexRecognizer:
    """Simple regex recognizer used for custom patterns from the config.

    The span is the named group ``pii`` when the regex defines one, otherwise the
    whole match. Plain groups such as ``(A|B)`` never narrow the span.
    Example: ``등록번호\\s*:?\\s*(?P<pii>\\d{6,8})`` masks only the number.
    """

    def __init__(self, name: str, entity: str, regex: str, score: float = 0.9,
                 context: list[str] | None = None, flags: int = 0, window: int = 20) -> None:
        self.name = name
        self.entity = entity
        self.entities = (entity,)
        self.regex = re.compile(regex, flags)
        self.score = score
        self.context = [c.lower() for c in (context or [])]
        self.window = window

    def find(self, text: str, cfg: "Config") -> list[Span]:
        out: list[Span] = []
        for m in self.regex.finditer(text):
            if "pii" in self.regex.groupindex and m.group("pii") is not None:
                s, e = m.span("pii")
            else:
                s, e = m.span()
            if e <= s:
                continue
            if self.context and not any(c in context_before(text, s, self.window) for c in self.context):
                continue
            out.append(make_span(text, s, e, self.entity, self.score, self.name))
        return out
