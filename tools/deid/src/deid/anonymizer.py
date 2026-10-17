"""Turn detected spans into replacement text: pseudo, mask, redact, generalize or keep."""
from __future__ import annotations

import datetime
import re
import unicodedata

from . import entities as E
from .config import Config, default_config
from .types import AnonResult, Replacement, Span
from .vault import Vault

# Two-digit years: 00-29 -> 2000-2029, 30-99 -> 1930-1999. Birth dates are never in the
# future, so a 2-digit birth year after the current year is moved back to the 1900s.
YY_PIVOT = 30

_DIGITS = re.compile(r"[0-9\uff10-\uff19]+")   # ASCII and full-width digits
_KO_YEAR = re.compile(r"(?<![0-9])([0-9]{4}|[0-9]{2})\s*년")
_KO_MONTH = re.compile(r"(?<![0-9])([0-9]{1,2})\s*월")
_WORD = re.compile(r"[^\W_]+")
_EN_MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_EN_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_YEAR4 = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")


class Anonymizer:
    """Apply the configured action for each span; pseudonyms come from a (shared) Vault."""

    def __init__(self, cfg: Config | None = None, vault: Vault | None = None) -> None:
        self.cfg = cfg if cfg is not None else default_config()
        self.vault = vault if vault is not None else Vault()

    def replacement_for(self, span: Span) -> tuple[str, str]:
        """(replacement_text, action actually applied) for one span."""
        action = self.cfg.action_for(span.entity)
        label = self.cfg.label_for(span.entity)
        if action == "pseudo":
            group = getattr(span, "group", None)
            if group:   # one person, several spellings ("박서윤", "서윤", "박모씨"): one token
                return self.vault.token_for(span.entity, span.text, label, group=group), action
            return self.vault.token_for(span.entity, span.text, label), action
        if action == "mask":
            return mask_value(span.entity, span.text), action
        if action == "redact":
            return f"[{label}]", action
        if action == "generalize":
            return generalize_value(span.entity, span.text, label)
        if action == "keep":
            return span.text, action
        raise ValueError(f"unknown action {action!r} for {span.entity}")

    def anonymize(self, text: str, spans: list[Span]) -> AnonResult:
        """Replace spans (any order, non-overlapping). Replacement offsets refer to `text`."""
        ordered = sorted(spans, key=lambda s: (s.start, s.end))
        prev = None
        for sp in ordered:
            if text[sp.start:sp.end] != sp.text:
                raise ValueError(f"span {sp.start}:{sp.end} does not match the text")
            if prev is not None and sp.start < prev.end:
                raise ValueError(f"overlapping spans {prev.start}:{prev.end} and {sp.start}:{sp.end}")
            prev = sp
        # Tokens are issued left to right so numbering follows first appearance.
        reps = []
        for sp in ordered:
            new, action = self.replacement_for(sp)
            reps.append(Replacement(sp.start, sp.end, sp.entity, sp.text, new, action))
        # Equivalent to applying right to left on the original offsets, but linear time.
        parts, pos = [], 0
        for r in reps:
            parts += (text[pos:r.start], r.replacement)
            pos = r.end
        parts.append(text[pos:])
        return AnonResult(text="".join(parts), replacements=reps)


# ---------------------------------------------------------------------- mask


def _mask_where(value: str, keep: set[int], only_digits: bool = False) -> str:
    """Replace letters/digits (or digits only) with '*' except at `keep` indices."""
    def hide(i: int, ch: str) -> bool:
        return i not in keep and (ch.isdigit() if only_digits else ch.isalnum())
    return "".join("*" if hide(i, ch) else ch for i, ch in enumerate(value))


def _mask_ends(value: str) -> str:
    """Keep the first and last letter/digit (only the first if there are two)."""
    idx = [i for i, ch in enumerate(value) if ch.isalnum()]
    keep = {idx[0], idx[-1]} if len(idx) >= 3 else set(idx[:1]) if len(idx) == 2 else set()
    return _mask_where(value, keep)


def _mask_person(value: str) -> str:
    if len(value.split()) > 1:  # "John Smith" -> "J*** S****"
        return _WORD.sub(lambda m: m.group()[0] + "*" * (len(m.group()) - 1), value)
    return _mask_ends(value)  # 김민준 -> 김*준, 이수 -> 이*


# Spoken digits ("공일공-4321-8765") are masked like their numeric form, so the two
# spellings of one number never reveal different groups.
_HANGUL_DIGITS = str.maketrans("공영일이삼사오육칠팔구", "00123456789")


def _mask_phone(value: str) -> str:
    view = value.translate(_HANGUL_DIGITS)
    groups = list(_DIGITS.finditer(view))
    if not groups:
        return _mask_ends(value)
    if len(groups) >= 2:  # 010-****-5678, +82 10-****-5678, 1588-****
        target = groups[-2] if len(groups) >= 3 else groups[-1]
        hide = set(range(*target.span()))
    else:  # 01012345678 -> 010****5678
        s, e = groups[0].span()
        head = 2 if view.startswith("02", s) else 3
        lo = s + head if e - s - 4 > head else s
        hide = set(range(lo, max(lo, e - 4)))
    return "".join("*" if i in hide else ch for i, ch in enumerate(value))


def _mask_digits(value: str, keep_first: int = 0, keep_last: int = 0) -> str:
    idx = [i for i, ch in enumerate(value) if ch.isdigit()]
    keep = set(idx[:keep_first]) | (set(idx[-keep_last:]) if keep_last else set())
    return _mask_where(value, keep, only_digits=True)


def _mask_email(value: str) -> str:
    local, sep, domain = value.rpartition("@")
    if not sep or not local:
        return _mask_ends(value)
    return local[0] + "*" * (len(local) - 1) + "@" + domain


def mask_value(entity: str, value: str) -> str:
    """Partial mask that keeps the string's shape (length and separators)."""
    if entity == E.PERSON:
        return _mask_person(value)
    if entity == E.PHONE:
        return _mask_phone(value)
    if entity in (E.RRN, E.FRN):
        return _mask_digits(value, keep_first=7)   # 900101-1******, 900101-******* stays
    if entity == E.CARD:
        return _mask_digits(value, keep_last=4)
    if entity == E.EMAIL:
        return _mask_email(value)
    return _mask_ends(value)


# ---------------------------------------------------------------------- generalize


def _address_prefix(text: str) -> str:
    try:
        from .recognizers.address import generalize_address
    except ImportError:
        return ""
    return (generalize_address(text) or "").strip()


def _century(yy: str, birth: bool = False) -> int:
    n = int(yy)
    year = 2000 + n if n < YY_PIVOT else 1900 + n
    if birth and year > datetime.date.today().year:
        year -= 100
    return year


def _year4(s: str) -> int | None:
    n = int(s)
    return n if 1900 <= n <= 2099 else None


def _month(s: str) -> int | None:
    n = int(s)
    return n if 1 <= n <= 12 else None


def _valid_md(m: str, d: str) -> bool:
    return 1 <= int(m) <= 12 and 1 <= int(d) <= 31


def parse_year_month(value: str, birth: bool = False) -> tuple[int | None, int | None]:
    """Best-effort (year, month) from a Korean, numeric or English date.

    2-digit years use YY_PIVOT; with `birth` they are also never later than this year.
    """
    ky, km = _KO_YEAR.search(value), _KO_MONTH.search(value)
    if ky or km:  # 1985년 3월 2일, 85년생, 3월 2일
        year = None
        if ky:
            year = _year4(ky.group(1)) if len(ky.group(1)) == 4 else _century(ky.group(1), birth)
        return year, _month(km.group(1)) if km else None
    em = _EN_MONTH.search(value)
    if em:  # March 15, 2024 / 15 Mar 2024
        y4 = _YEAR4.search(value)
        return (int(y4.group()) if y4 else None), _EN_MONTHS.index(em.group(1).lower()[:3]) + 1
    g = _DIGITS.findall(value)
    if len(g) == 1:
        d = g[0]
        if len(d) == 8:                                   # 19850302
            return _year4(d[:4]), _month(d[4:6])
        if len(d) == 6 and _valid_md(d[2:4], d[4:]):      # 850302
            return _century(d[:2], birth), int(d[2:4])
        if len(d) == 4:                                   # 1985
            return _year4(d), None
        return None, None
    if len(g) < 2:
        return None, None
    a, b, c = g[0], g[1], g[2] if len(g) > 2 else None
    if len(a) == 4:                                       # 1985.03.02, 1985-3
        return _year4(a), _month(b)
    if len(b) == 4 and c is None:                         # 03/1985
        return _year4(b), _month(a)
    if c is None or len(a) > 2 or len(b) > 2:
        return None, None
    if len(c) == 4:                                       # 03/02/1985, 15/03/1985
        return _year4(c), _month(b) if int(a) > 12 else _month(a)
    if len(a) == 2 and len(c) <= 2 and _valid_md(b, c):  # 85.03.02 (Korean order first)
        return _century(a, birth), int(b)
    if len(c) == 2 and _valid_md(a, b):                   # 03/02/85
        return _century(c, birth), int(a)
    return None, None


def generalize_value(entity: str, value: str, label: str) -> tuple[str, str]:
    """ADDRESS -> "서울특별시 강남구 [주소]", BIRTHDATE -> "1985년생", DATE -> "2024년 3월".

    Unknown parts fall back to "[label]"; other entities are redacted. Full-width digits and
    decomposed Hangul are read like their plain forms ("１９８５．０３．１５" -> "1985년생").
    """
    value = unicodedata.normalize("NFKC", value)
    if entity == E.ADDRESS:
        prefix = _address_prefix(value)
        return (f"{prefix} [{label}]" if prefix else f"[{label}]"), "generalize"
    if entity == E.BIRTHDATE:
        year, _ = parse_year_month(value, birth=True)
        return (f"{year}년생" if year else f"[{label}]"), "generalize"
    if entity == E.DATE:
        year, month = parse_year_month(value)
        return (f"{year}년 {month}월" if year and month else f"[{label}]"), "generalize"
    return f"[{label}]", "redact"
