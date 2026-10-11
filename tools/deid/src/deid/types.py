"""Shared data types. This module is the contract between all deid modules."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Span:
    """One detected piece of PII inside a text.

    start/end are Python string offsets (end exclusive) into the text that was
    passed to the detector. `text` must equal text[start:end].
    """

    start: int
    end: int
    entity: str          # one of deid.entities.* constants (or a custom entity name)
    text: str
    score: float         # 0.0-1.0 confidence
    source: str          # recognizer name, e.g. "kr_rrn", "names", "dict", "ner_hf", "propagate"

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"invalid span offsets {self.start}:{self.end}")
        if len(self.text) != self.end - self.start:
            raise ValueError("span text length does not match offsets")


@dataclass(frozen=True)
class Replacement:
    """One replacement the anonymizer made. `original` is never written to reports."""

    start: int           # offset in the ORIGINAL text
    end: int
    entity: str
    original: str
    replacement: str
    action: str          # "pseudo" | "mask" | "redact" | "generalize" | "keep"


@dataclass
class AnonResult:
    text: str
    replacements: list[Replacement] = field(default_factory=list)


@dataclass
class Report:
    """Per-file summary. Contains counts only, never raw PII values."""

    source: str = ""
    output: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, entity: str, n: int = 1) -> None:
        self.counts[entity] = self.counts.get(entity, 0) + n

    def merge(self, other: "Report") -> None:
        for k, v in other.counts.items():
            self.add(k, v)
        self.warnings.extend(other.warnings)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "output": self.output,
            "total": self.total,
            "counts": dict(sorted(self.counts.items())),
            "warnings": list(self.warnings),
        }
