"""deid backends.

Optional ML/LLM detectors that run next to the rule-based recognizers. Heavy libraries
(transformers, torch, gliner) are imported lazily, so importing this package is cheap.

Config (TOML)::

    [ner]
    backend = "hf"                       # "hf" | "gliner" | "ollama" (omit to disable)
    model = "kiyuyeon/pii-ner-kcelectra"
    device = "auto"                      # "cpu" | "mps" | "auto"
    min_score = 0.6
    [ner.label_map]                      # optional, model label -> deid entity ("" = ignore)
    LOC = "ADDRESS"
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .. import entities as E
from ..recognizers.base import make_span
from ..types import Span

if TYPE_CHECKING:
    from ..config import Config
    from ..recognizers.base import Recognizer

OTHER = E.OTHER  # entity for identifiers an LLM reports without a known type

_ALIASES: dict[str, tuple[str, ...]] = {
    E.PERSON: ("name", "person_name", "full_name", "patient", "patient_name", "doctor", "staff",
               "이름", "성명", "환자명", "인명", "사람"),
    E.PHONE: ("phone", "phone_number", "telephone", "telephone_number", "tel", "mobile",
              "mobile_number", "mobile_phone_number", "fax", "fax_number",
              "전화번호", "휴대폰", "휴대폰번호", "연락처"),
    E.EMAIL: ("e_mail", "mail", "email_address", "이메일"),
    E.ADDRESS: ("addr", "street_address", "home_address", "주소"),
    E.RRN: ("ssn", "resident_registration_number", "resident_number", "national_id",
            "주민번호", "주민등록번호"),
    E.FRN: ("foreigner_registration_number", "alien_registration_number",
            "외국인등록번호", "외국인번호"),
    E.BIRTHDATE: ("birth_date", "date_of_birth", "dob", "birthday", "birth", "생년월일", "생일"),
    E.MRN: ("medical_record_number", "chart_number", "patient_id", "hospital_id",
            "등록번호", "차트번호", "환자번호", "병록번호"),
    E.CARD: ("card_number", "credit_card", "credit_card_number", "카드번호"),
    E.ACCOUNT: ("account_number", "bank_account", "bank_account_number", "계좌번호"),
    E.PASSPORT: ("passport_number", "여권번호"),
    E.DRIVER_LICENSE: ("drivers_license", "driver_license_number", "drivers_license_number",
                       "driving_license", "운전면허번호", "면허번호"),
    E.HEALTH_INSURANCE: ("health_insurance_number", "insurance_number",
                         "건강보험증번호", "보험번호"),
    E.BRN: ("business_registration_number", "사업자등록번호", "사업자번호"),
    E.IP: ("ip_address",),
    E.PLATE: ("license_plate", "license_plate_number", "car_number", "vehicle_number", "차량번호"),
    E.URL: ("website",),
}
# Type words of the OpenAI-compatible backend's prompt (openai_compat.TYPES: 기관, 전화, 기타 ...)
# and their synonyms. ONLY that backend uses them (llm_type_entity): the GLiNER, HF and Ollama
# label mappings stay as they were, so e.g. a GLiNER label "organization" is still its own
# (redacted) entity and an Ollama item typed 날짜 is still OTHER.
_LLM_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    E.PHONE: ("전화", "팩스"),
    E.EMAIL: ("메일",),
    E.CARD: ("카드",),
    E.ACCOUNT: ("계좌",),
    E.PASSPORT: ("여권",),
    E.DRIVER_LICENSE: ("면허", "운전면허"),
    # A model that types a visit date as 날짜 must not get it redacted as OTHER (DATE is kept).
    E.DATE: ("날짜", "일자", "진료일", "방문일", "내원일"),
    # Proper names of hospitals, clinics, pharmacies, companies, schools (the prompt's 기관):
    # every synonym a model may pick maps to ORG, so the result does not depend on the word.
    E.ORG: ("org", "organization", "organisation", "institution", "hospital", "clinic",
            "pharmacy", "company", "school", "workplace", "employer",
            "기관", "기관명", "기관·직장·학교", "직장", "직장명", "학교", "학교명", "회사", "회사명",
            "병원", "병원명", "의원", "의원명", "약국", "약국명", "보건소"),
    OTHER: ("기타", "other_identifier", "번호"),
}
_SEP = re.compile(r"[\s\-]+")

# [ner] / [llm] backend names served by backends.openai_compat ("strata" = the clinic server).
OPENAI_BACKENDS = frozenset({"openai", "strata", "openai_compat", "openai-compatible"})


def _alias_key(label: str) -> str:
    return _SEP.sub("_", str(label).strip().casefold().replace("'", ""))


# ORG is produced only by the OpenAI-compatible backend (LLM_TYPE_ALIASES).
ENTITY_ALIASES: dict[str, str] = {ent.casefold(): ent for ent in E.ALL if ent != E.ORG}
ENTITY_ALIASES.update({a: ent for ent, names in _ALIASES.items() for a in names})
LLM_TYPE_ALIASES: dict[str, str] = dict(ENTITY_ALIASES)
LLM_TYPE_ALIASES.update({_alias_key(a): ent for ent, names in _LLM_TYPE_ALIASES.items()
                         for a in names})


def entity_for(label: object, default: str | None = OTHER) -> str | None:
    """Map a free-form PII label ("phone number", "이름", "DOB") to a deid entity."""
    if not isinstance(label, str) or not label.strip():
        return default
    return ENTITY_ALIASES.get(_alias_key(label), default)


def llm_type_entity(label: object, default: str | None = OTHER) -> str | None:
    """entity_for for the OpenAI-compatible backend's type words (기관 -> ORG, 날짜 -> DATE,
    전화 -> PHONE, 기타 -> OTHER ...)."""
    if not isinstance(label, str) or not label.strip():
        return default
    return LLM_TYPE_ALIASES.get(_alias_key(label), default)


def resolve_device(device: str | None) -> str:
    """"auto" -> "mps" when torch reports Apple-GPU support, else "cpu"."""
    d = (device or "cpu").strip().lower()
    if d == "auto":
        try:
            import torch
            return "mps" if torch.backends.mps.is_available() else "cpu"
        except Exception:  # torch missing or too old for mps
            return "cpu"
    if d in ("cpu", "mps") or d == "cuda" or d.startswith("cuda:"):
        return d
    raise ValueError(f"unknown device {device!r}; use 'cpu', 'mps' or 'auto'")


# ---------------------------------------------------------------------- chunking


def chunk_text(text: str, max_chars: int = 400, overlap: int = 80) -> list[tuple[int, str]]:
    """Split text into overlapping (offset, chunk) windows of at most max_chars.

    Cuts prefer a newline, then whitespace, in the second half of a window; the next
    window starts about `overlap` chars earlier, so an entity cut at one edge is seen whole.
    """
    if max_chars < 2:
        raise ValueError("max_chars must be >= 2")
    if not 0 <= overlap < max_chars // 2:
        raise ValueError("overlap must be >= 0 and smaller than max_chars // 2")
    n = len(text)
    if n <= max_chars:
        return [(0, text)] if text else []
    out: list[tuple[int, str]] = []
    start = 0
    while start + max_chars < n:
        end = _break_before(text, start + max_chars // 2, start + max_chars)
        out.append((start, text[start:end]))
        nxt = end - overlap
        start = _after_space(text, nxt, nxt + overlap // 2)
    out.append((start, text[start:]))
    return out


def _break_before(text: str, lo: int, hi: int) -> int:
    """Cut position in (lo, hi]: after the last newline, else after the last space, else hi."""
    nl = text.rfind("\n", lo, hi)
    if nl != -1:
        return nl + 1
    for i in range(hi - 1, lo - 1, -1):
        if text[i].isspace():
            return i + 1
    return hi


def _after_space(text: str, lo: int, hi: int) -> int:
    """Position just after the first whitespace in [lo, hi), else lo."""
    for i in range(lo, min(hi, len(text))):
        if text[i].isspace():
            return i + 1
    return lo


# ---------------------------------------------------------------------- span helpers

_EDGE = set(" \t\r\n 　.,;:!?\"'`()[]{}<>「」『』《》〈〉“”‘’·…|")
_PAIRS = {")": "(", "]": "[", "}": "{", ">": "<", "」": "「", "』": "『", "》": "《", "〉": "〈"}
_OPENERS = {v: k for k, v in _PAIRS.items()}
# Trailing words a model may glue to a name. Single-syllable particles (은, 이 ...) are NOT
# trimmed: they are also common last syllables of given names (하은, 지은).
_NAME_TAILS = tuple(sorted((
    "님", "씨", "환자분", "환자", "선생님", "원장님", "교수님", "간호사", "보호자",
    "께서", "에게서", "에게", "한테서", "한테",
), key=len, reverse=True))


def trim_bounds(text: str, start: int, end: int, entity: str = "") -> tuple[int, int]:
    """Shrink [start, end) past edge whitespace/punctuation (and honorifics for PERSON).

    A bracket is kept when its partner is inside the span ("서울시 (역삼동)").
    """
    while start < end:
        first, last = text[start], text[end - 1]
        if end - start >= 2 and _OPENERS.get(first) == last:
            start, end = start + 1, end - 1
        elif first in _EDGE and _OPENERS.get(first, "\0") not in text[start + 1:end]:
            start += 1
        elif last in _EDGE and _PAIRS.get(last, "\0") not in text[start:end - 1]:
            end -= 1
        else:
            break
    if entity == E.PERSON:
        for tail in _NAME_TAILS:
            if text.endswith(tail, start, end) and len(text[start:end - len(tail)].rstrip()) >= 2:
                end -= len(tail)
                while end > start and text[end - 1].isspace():
                    end -= 1
                break
    return start, end


def merge_spans(text: str, spans: list[Span]) -> list[Span]:
    """Merge same-entity spans that overlap or touch (chunk seams, split sub-word groups)."""
    out: list[Span] = []
    for sp in sorted(spans, key=lambda s: (s.entity, s.start, s.end)):
        last = out[-1] if out and out[-1].entity == sp.entity else None
        if last is not None and sp.start <= last.end:
            out[-1] = make_span(text, last.start, max(last.end, sp.end), sp.entity,
                                max(last.score, sp.score), last.source)
        else:
            out.append(sp)
    return sorted(out, key=lambda s: (s.start, s.end, s.entity))


# ---------------------------------------------------------------------- factory


def build_ner_recognizer(cfg: "Config") -> "Recognizer | None":
    """Recognizer for cfg.ner["backend"] ("hf", "gliner", "ollama", "openai"/"strata"),
    or None when unset."""
    ner = dict(cfg.ner or {})
    backend = str(ner.get("backend") or "").strip().lower()
    if backend in ("", "none", "off", "false"):
        return None
    chunking = {k: int(ner[k]) for k in ("max_chars", "overlap") if k in ner}
    if backend in ("hf", "transformers"):
        from .ner_hf import HFNerRecognizer

        model = str(ner.get("model") or "").strip()
        if not model:
            raise ValueError('[ner] backend "hf" needs model = "...", '
                             'e.g. "kiyuyeon/pii-ner-kcelectra"')
        return HFNerRecognizer(
            model, label_map=ner.get("label_map"), device=ner.get("device", "cpu"),
            min_score=float(ner.get("min_score", 0.6)), pii_model=ner.get("pii_model"),
            **chunking,
        )
    if backend == "gliner":
        from .gliner_backend import DEFAULT_MODEL, GlinerRecognizer

        labels = ner.get("labels")
        if labels is None and ner.get("label_map"):
            labels = ner["label_map"]
        return GlinerRecognizer(
            str(ner.get("model") or DEFAULT_MODEL), labels=labels,
            threshold=float(ner.get("threshold", ner.get("min_score", 0.5))),
            device=ner.get("device", "cpu"), **chunking,
        )
    if backend in ("ollama", "llm"):
        from .ollama import DEFAULT_HOST, OllamaRecognizer

        llm = dict(cfg.llm or {})
        model = str(ner.get("model") or llm.get("model") or "").strip()
        if not model:
            raise ValueError('[ner] backend "ollama" needs model = "..." (or [llm] model), '
                             'e.g. "gemma3:4b"')
        # allow_remote only from [ner]: the original text needs [ner]'s own permission ([llm]
        # allow_remote is for `deid check`, which sends de-identified text only).
        return OllamaRecognizer(
            model, host=str(ner.get("host") or llm.get("host") or DEFAULT_HOST),
            allow_remote=bool(ner.get("allow_remote", False)),
            timeout=float(ner.get("timeout", llm.get("timeout", 120))),
        )
    if backend in OPENAI_BACKENDS:
        from .openai_compat import OpenAIRecognizer, ner_section

        return OpenAIRecognizer(ner_section(cfg))
    raise ValueError(f"[ner] backend {backend!r}을(를) 알 수 없습니다. hf, gliner, ollama, openai 중 "
                     f"하나를 쓰세요 (unknown [ner] backend {backend!r}; use 'hf', 'gliner', "
                     "'ollama' or 'openai')")
