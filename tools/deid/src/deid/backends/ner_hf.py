"""Optional Hugging Face token-classification backend (``pip install "deid-kit[ner]"``).

Candidate Korean PII models (community models, not vetted by deid: check each model card's
license and test the quality on your own notes before relying on them):

* ``kiyuyeon/pii-ner-kcelectra``
* ``atonlee/koelectra-ko-pii-ner``

General Korean NER models (PER/LOC/ORG tags) also help with names. Place-name tags (LOC, LC)
become ADDRESS only for PII models (``pii_model``, auto-detected from "pii"/"deid" in the
model id) or when ``label_map`` says so; otherwise every city or hospital name would be masked.

The model is downloaded once into the Hugging Face cache (~/.cache/huggingface). Later runs
load it from the cache without contacting the hub; set HF_HUB_OFFLINE=1 to guarantee that.
Text is processed on this machine only.
"""
from __future__ import annotations

import importlib.util
import re
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .. import entities as E
from ..recognizers.base import make_span
from ..types import Span
from . import ENTITY_ALIASES, chunk_text, entity_for, merge_spans, resolve_device, trim_bounds

if TYPE_CHECKING:
    from ..config import Config

INSTALL_HINT = ('the "hf" NER backend needs transformers and torch: '
                'pip install "deid-kit[ner]"')

_LOCATION_LABELS = ("LOC", "LC", "LOCATION")

# Model label (upper case, "-"/" " -> "_", BIO prefix/suffix removed) -> deid entity or None.
DEFAULT_LABEL_MAP: dict[str, str | None] = {
    **{k.upper(): v for k, v in ENTITY_ALIASES.items() if k.isascii()},
    "PER": E.PERSON, "PS": E.PERSON, "PS_NAME": E.PERSON,
    "TEL": E.PHONE, "TELEPHONE": E.PHONE, "PHONENUMBER": E.PHONE,
    # Off by default: general NER models tag every place, organisation, date and number.
    **{k: None for k in _LOCATION_LABELS},
    **{k: None for k in ("GPE", "ORG", "OG", "ORGANIZATION", "HOSPITAL", "DATE", "DT", "DAT",
                         "TIME", "TI", "TIM", "QT", "NUM", "QUANTITY", "AGE", "MISC", "O")},
}

_BIO_PREFIX = re.compile(r"^[BIESLU]_(.+)$")
_BIO_SUFFIX = re.compile(r"^(.+)_[BIESLU]$")
_IGNORE_VALUES = {"", "NONE", "IGNORE", "SKIP", "O", "-"}


def is_installed() -> bool:
    """True if transformers and torch can be imported (does not import them)."""
    return all(importlib.util.find_spec(m) is not None for m in ("transformers", "torch"))


def _norm_label(label: object) -> str:
    return re.sub(r"[\s\-]+", "_", str(label).strip().upper())


def _norm_target(value: object) -> str | None:
    if value is None or str(value).strip().upper() in _IGNORE_VALUES:
        return None
    return entity_for(value, default=_norm_label(value))


class HFNerRecognizer:
    """PII spans from a transformers token-classification model, chunked for long texts."""

    name = "ner_hf"

    def __init__(self, model: str, label_map: Mapping[str, Any] | None = None,
                 device: str = "cpu", min_score: float = 0.6, pii_model: bool | None = None,
                 max_chars: int = 400, overlap: int = 80,
                 pipeline: Callable[[str], list[dict]] | None = None) -> None:
        self.model = model
        self.device = device
        if str(device).strip().lower() != "auto":
            resolve_device(device)  # validate early, without importing torch
        self.min_score = float(min_score)
        self.pii_model = (any(k in model.lower() for k in ("pii", "deid"))
                          if pii_model is None else bool(pii_model))
        self.max_chars, self.overlap = max_chars, overlap
        chunk_text("", max_chars, overlap)  # validate chunk parameters
        self.label_map = dict(DEFAULT_LABEL_MAP)
        if self.pii_model:
            self.label_map.update({k: E.ADDRESS for k in _LOCATION_LABELS})
        self.label_map.update({_norm_label(k): _norm_target(v)
                               for k, v in (label_map or {}).items()})
        self.entities = tuple(sorted({v for v in self.label_map.values() if v}))
        self._pipe = pipeline

    def map_label(self, label: object) -> str | None:
        """deid entity for a model label such as "PER", "B-PS", "PER-B"; None = ignore."""
        if not label:
            return None
        key = _norm_label(label)
        if key in self.label_map:
            return self.label_map[key]
        m = _BIO_PREFIX.match(key) or _BIO_SUFFIX.match(key)
        return self.label_map.get(m.group(1)) if m else None

    def load(self) -> Callable[[str], list[dict]]:
        """Load the pipeline now (cache first, then a one-time download); returns it."""
        if self._pipe is None:
            try:
                self._pipe = self._build(local_only=True)
            except (OSError, ValueError):
                self._pipe = self._build(local_only=False)
        return self._pipe

    def _build(self, local_only: bool) -> Callable[[str], list[dict]]:
        try:
            from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

            tok = AutoTokenizer.from_pretrained(self.model, local_files_only=local_only)
            mdl = AutoModelForTokenClassification.from_pretrained(
                self.model, local_files_only=local_only)
            return pipeline("token-classification", model=mdl, tokenizer=tok,
                            aggregation_strategy="simple", device=resolve_device(self.device))
        except ImportError as e:  # e.g. torch missing
            raise RuntimeError(f"{INSTALL_HINT} ({e})") from e
        except (OSError, ValueError) as e:
            if local_only:
                raise
            raise RuntimeError(
                f"could not load NER model {self.model!r}: {e}\n"
                "The first run downloads it once from huggingface.co into the HF cache; "
                "afterwards it runs offline (HF_HUB_OFFLINE=1)."
            ) from e

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not text.strip():
            return []
        pipe = self.load()
        spans: list[Span] = []
        for offset, chunk in chunk_text(text, self.max_chars, self.overlap):
            if chunk.strip():
                spans += self._spans(text, offset, chunk, pipe(chunk) or [])
        return merge_spans(text, spans)

    def _spans(self, text: str, offset: int, chunk: str, items: list[dict]) -> list[Span]:
        out = []
        for it in items:
            entity = self.map_label(it.get("entity_group") or it.get("entity"))
            score = float(it.get("score") or 0.0)
            start, end = it.get("start"), it.get("end")
            if entity is None or score < self.min_score or start is None or end is None:
                continue
            s, e = trim_bounds(chunk, max(0, int(start)), min(len(chunk), int(end)), entity)
            if e > s:
                out.append(make_span(text, offset + s, offset + e, entity, min(score, 1.0),
                                     self.name))
        return out
