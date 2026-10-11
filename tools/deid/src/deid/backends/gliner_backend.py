"""Optional GLiNER zero-shot backend (``pip install "deid-kit[gliner]"``).

GLiNER finds entities for free-text labels ("person", "phone number" ...), so new PII types
need no retraining. Default model: ``urchade/gliner_multi_pii-v1`` (multilingual community
model; check its license and test it on your own notes). It is downloaded once into the
Hugging Face cache and later loaded offline (HF_HUB_OFFLINE=1).
"""
from __future__ import annotations

import importlib.util
import re
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .. import entities as E
from ..recognizers.base import make_span
from ..types import Span
from . import OTHER, chunk_text, entity_for, merge_spans, resolve_device, trim_bounds

if TYPE_CHECKING:
    from ..config import Config

DEFAULT_MODEL = "urchade/gliner_multi_pii-v1"
INSTALL_HINT = 'the "gliner" NER backend needs gliner: pip install "deid-kit[gliner]"'

DEFAULT_LABELS: dict[str, str] = {
    "person": E.PERSON,
    "phone number": E.PHONE,
    "address": E.ADDRESS,
    "email": E.EMAIL,
    "resident registration number": E.RRN,
    "medical record number": E.MRN,
}


def is_installed() -> bool:
    """True if gliner can be imported (does not import it)."""
    return importlib.util.find_spec("gliner") is not None


def _custom_entity(label: str) -> str:
    """"staff id" -> "STAFF_ID" for labels without a deid entity."""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "_", label).strip("_").upper() or OTHER


def label_entities(labels: Sequence[str] | Mapping[str, Any] | None) -> dict[str, str]:
    """{gliner label: deid entity}; a list is mapped by name, a dict is taken as given."""
    if labels is None:
        return dict(DEFAULT_LABELS)
    if isinstance(labels, str):
        raise TypeError("labels must be a list or a {label: entity} mapping, not a string")
    if isinstance(labels, Mapping):
        pairs = [(str(k), str(v).strip()) for k, v in labels.items() if v and str(v).strip()]
        out = {k: entity_for(v, default=_custom_entity(v)) for k, v in pairs}
    else:
        out = {str(lab): entity_for(lab, default=None) or _custom_entity(str(lab))
               for lab in labels if str(lab).strip()}
    if not out:
        raise ValueError("GLiNER needs at least one label")
    return out


class GlinerRecognizer:
    """PII spans from a GLiNER model for a fixed set of natural-language labels."""

    name = "gliner"

    def __init__(self, model: str = DEFAULT_MODEL,
                 labels: Sequence[str] | Mapping[str, Any] | None = None,
                 threshold: float = 0.5, device: str = "cpu", max_chars: int = 400,
                 overlap: int = 80, model_obj: Any = None) -> None:
        self.model = model
        self.label_map = label_entities(labels)
        self.labels = list(self.label_map)
        self._by_fold = {k.casefold(): v for k, v in self.label_map.items()}
        self.entities = tuple(sorted(set(self.label_map.values())))
        self.threshold = float(threshold)
        self.device = device
        if str(device).strip().lower() != "auto":
            resolve_device(device)
        self.max_chars, self.overlap = max_chars, overlap
        chunk_text("", max_chars, overlap)  # validate chunk parameters
        self._model = model_obj

    def load(self) -> Any:
        """Load the model now (cache first, then a one-time download); returns it."""
        if self._model is None:
            try:
                from gliner import GLiNER
            except ImportError as e:
                raise RuntimeError(INSTALL_HINT) from e
            try:
                mdl = GLiNER.from_pretrained(self.model, local_files_only=True)
            except (OSError, ValueError):
                try:
                    mdl = GLiNER.from_pretrained(self.model)
                except (OSError, ValueError) as e:
                    raise RuntimeError(
                        f"could not load GLiNER model {self.model!r}: {e}\n"
                        "The first run downloads it once from huggingface.co into the HF "
                        "cache; afterwards it runs offline (HF_HUB_OFFLINE=1)."
                    ) from e
            device = resolve_device(self.device)
            if device != "cpu":
                mdl = mdl.to(device)
            self._model = mdl
        return self._model

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not text.strip():
            return []
        mdl = self.load()
        spans: list[Span] = []
        for offset, chunk in chunk_text(text, self.max_chars, self.overlap):
            if not chunk.strip():
                continue
            for it in mdl.predict_entities(chunk, self.labels, threshold=self.threshold) or []:
                entity = self._by_fold.get(str(it.get("label", "")).casefold())
                score = float(it.get("score") or 0.0)
                start, end = it.get("start"), it.get("end")
                if entity is None or score < self.threshold or start is None or end is None:
                    continue
                s, e = trim_bounds(chunk, max(0, int(start)), min(len(chunk), int(end)), entity)
                if e > s:
                    spans.append(make_span(text, offset + s, offset + e, entity,
                                           min(score, 1.0), self.name))
        return merge_spans(text, spans)
