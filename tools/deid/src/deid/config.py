"""Configuration loading (TOML) and validation.

Mistakes in config.toml are not ignored: unknown top-level keys and tables, unknown entity
names in [actions] / [labels] / enabled / disabled, unknown actions and unknown [columns]
types raise ValueError with a Korean message that names the nearest valid spelling. Entity
names are matched case-insensitively ("phone" -> "PHONE", "이름" -> "PERSON").
`validate_report(path)` lists the errors and the softer problems (e.g. `names = [...]` that
ended up inside the [ner] table, unknown [ocr] keys) for `deid doctor`.
"""
from __future__ import annotations

import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import entities

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - python 3.10
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:  # pragma: no cover
        tomllib = None  # type: ignore

VALID_ACTIONS = {"pseudo", "mask", "redact", "generalize", "keep"}
# Korean spellings of the actions accepted in [actions].
ACTION_ALIASES = {
    "가명": "pseudo", "가명화": "pseudo", "가명처리": "pseudo",
    "마스킹": "mask", "부분": "mask", "부분가림": "mask", "일부가림": "mask",
    "가림": "redact", "삭제": "redact", "지움": "redact", "제거": "redact",
    "일반화": "generalize", "범주화": "generalize",
    "유지": "keep", "그대로": "keep",
}

TOP_LEVEL_KEYS = ("names", "allowlist", "min_score", "enabled", "disabled")
TABLES = ("actions", "labels", "columns", "ocr", "ner", "llm")
ARRAY_TABLES = ("patterns",)
# Keys read from the free-form tables; others are reported by validate_report (not errors).
OCR_KEYS = frozenset("""
engine lang psm adaptive upscale invert min_conf timeout tesseract_cmd tesseract_config languages
recognition_level space_gap style verify orientation barcodes bare_names redact_faces pdf_images
pdf_dpi docx_images hwpx_images max_image_pixels max_frames max_total_pixels pad padding
""".split())
NER_KEYS = frozenset("""
backend model device label_map pii_model max_chars overlap labels threshold host allow_remote
timeout min_score
""".split())
LLM_KEYS = frozenset("model host allow_remote timeout".split())
_TABLE_KEYS = {"ocr": OCR_KEYS, "ner": NER_KEYS, "llm": LLM_KEYS}
PATTERN_KEYS = ("entity", "regex", "score", "context")
# [columns] values besides entity names / Korean labels (formats.tabular.parse_column_kind).
COLUMN_SKIP = frozenset({"skip", "ignore", "keep", "none", "off", "no"})
COLUMN_TEXT = frozenset({"text", "auto", "free", "detect", "freetext"})
_PLACE_HINT = ("파일 맨 위(첫 번째 [표] 제목보다 위)의 {key} = [ ... ] 목록 안에 넣으세요")


@dataclass
class CustomPattern:
    entity: str
    regex: str
    score: float = 0.9
    context: list[str] = field(default_factory=list)


@dataclass
class Config:
    actions: dict[str, str] = field(default_factory=lambda: dict(entities.DEFAULT_ACTIONS))
    labels: dict[str, str] = field(default_factory=lambda: dict(entities.LABELS_KO))
    names: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    custom_patterns: list[CustomPattern] = field(default_factory=list)
    min_score: float = 0.5
    enabled: set[str] | None = None
    ner: dict = field(default_factory=dict)
    ocr: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    # Spreadsheet/table column overrides: header text -> entity name | "skip" | "text"
    # (TOML table [columns]; read by formats.tabular, validated there).
    columns: dict[str, str] = field(default_factory=dict)

    def action_for(self, entity: str) -> str:
        return self.actions.get(entity, "redact")

    def label_for(self, entity: str) -> str:
        return self.labels.get(entity, entity)

    def is_enabled(self, entity: str) -> bool:
        return self.enabled is None or entity in self.enabled


def default_config() -> Config:
    return Config()


def _default_path() -> Path | None:
    env = os.environ.get("DEID_CONFIG")
    if env:
        return Path(env).expanduser()
    p = Path("~/.config/deid/config.toml").expanduser()
    return p if p.exists() else None


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load a TOML config on top of the defaults. Missing default file -> defaults."""
    p = Path(path).expanduser() if path is not None else _default_path()
    cfg = default_config()
    if p is None:
        return cfg
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    return config_from_dict(read_toml(p), cfg)


_DUP_TABLE = re.compile(r"Cannot declare \(([^)]*)\) twice|duplicate table \[([^\]]*)\]")
_LINE_NO = re.compile(r"line (\d+)")


def read_toml(path: str | os.PathLike) -> dict:
    """Parse a config file; syntax errors become ValueError with a Korean explanation."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError("설정 파일이 UTF-8 글자가 아닙니다. 텍스트 편집기에서 UTF-8로 저장하세요 "
                         f"(config file is not UTF-8: {e})") from None
    try:
        if tomllib is not None:
            return tomllib.loads(text)
        return parse_toml(text)   # python 3.10 without tomli: the small built-in reader below
    except ValueError as e:       # tomllib.TOMLDecodeError and TomlError are ValueErrors
        raise ValueError(_toml_error_ko(str(e))) from e


def _toml_error_ko(msg: str) -> str:
    line = _LINE_NO.search(msg)
    at = f" ({line.group(1)}번째 줄)" if line else ""
    dup = _DUP_TABLE.search(msg)
    if dup:
        name = (dup.group(1) or dup.group(2) or "").replace("'", "").rstrip(",").replace(", ", ".")
        return (f"설정 파일에 같은 [{name}] 표가 두 번 있습니다{at}. 예시를 붙여넣었다면 파일 내용을 모두 "
                f"지운 뒤 붙여넣거나, 두 [{name}] 아래 내용을 하나로 합치세요 ({msg})")
    if "overwrite a value" in msg or "duplicate key" in msg:
        return f"설정 파일에 같은 설정 이름이 두 번 있습니다{at}. 하나만 남기세요 ({msg})"
    return (f"설정 파일 문법(TOML) 오류{at}: 따옴표, 대괄호 [ ], 쉼표를 확인하세요. 값은 \"홍길동\"처럼, "
            f"한글 이름(열 제목 등)은 \"담당\" = \"PERSON\"처럼 따옴표로 감쌉니다 ({msg})")


# ---------------------------------------------------------------------------- minimal TOML

class TomlError(ValueError):
    """A config file that the built-in TOML reader cannot parse."""


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_NUMBER = re.compile(r"[+-]?(?:0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|"
                     r"[0-9_]+(?:\.[0-9_]+)?(?:[eE][+-]?[0-9_]+)?|inf|nan)")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


class _TomlReader:
    """Reader for the TOML subset deid config files use (python 3.10 without tomli).

    Supports comments, [tables], [[arrays of tables]], bare/quoted/dotted keys, basic and
    literal strings (also triple-quoted), integers, floats, booleans, arrays (multi-line) and
    inline tables. Dates/times are not supported.
    """

    def __init__(self, text: str) -> None:
        self.s = text.replace("\r\n", "\n")
        self.i = 0

    def error(self, msg: str) -> TomlError:
        line = self.s.count("\n", 0, self.i) + 1
        return TomlError(f"config TOML line {line}: {msg}")

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_ws(self, newlines: bool = False) -> None:
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def parse(self) -> dict:
        root: dict = {}
        table = root
        declared: set[tuple[str, ...]] = set()
        while True:
            self.skip_ws(newlines=True)
            if self.i >= len(self.s):
                return root
            if self.s.startswith("[[", self.i):
                self.i += 2
                keys = self.key_path()
                self.expect("]]")
                parent = self.descend(root, keys[:-1])
                arr = parent.setdefault(keys[-1], [])
                if not isinstance(arr, list):
                    raise self.error(f"{keys[-1]!r} is not an array of tables")
                table = {}
                arr.append(table)
            elif self.peek() == "[":
                self.i += 1
                keys = self.key_path()
                self.expect("]")
                if tuple(keys) in declared:
                    raise self.error(f"duplicate table [{'.'.join(keys)}]")
                declared.add(tuple(keys))
                table = self.descend(root, keys)
            else:
                self.key_value(table)
            self.end_of_line()

    def descend(self, table: dict, keys: list[str]) -> dict:
        for k in keys:
            nxt = table.setdefault(k, {})
            if isinstance(nxt, list) and nxt and isinstance(nxt[-1], dict):
                nxt = nxt[-1]
            if not isinstance(nxt, dict):
                raise self.error(f"{k!r} is not a table")
            table = nxt
        return table

    def expect(self, tok: str) -> None:
        self.skip_ws()
        if not self.s.startswith(tok, self.i):
            raise self.error(f"expected {tok!r}")
        self.i += len(tok)

    def end_of_line(self) -> None:
        self.skip_ws()
        if self.i < len(self.s) and self.s[self.i] != "\n":
            raise self.error("unexpected text after value")

    def key_path(self) -> list[str]:
        keys = []
        while True:
            self.skip_ws()
            c = self.peek()
            if c == '"':
                keys.append(self.basic_string())
            elif c == "'":
                keys.append(self.literal_string())
            else:
                m = _BARE_KEY.match(self.s, self.i)
                if not m:
                    raise self.error("expected a key")
                keys.append(m.group())
                self.i = m.end()
            self.skip_ws()
            if self.peek() != ".":
                return keys
            self.i += 1

    def key_value(self, table: dict) -> None:
        keys = self.key_path()
        self.expect("=")
        self.skip_ws()
        target = self.descend(table, keys[:-1])
        if keys[-1] in target:
            raise self.error(f"duplicate key {keys[-1]!r}")
        target[keys[-1]] = self.value()

    def value(self):
        c = self.peek()
        if c == '"':
            return self.basic_string()
        if c == "'":
            return self.literal_string()
        if c == "[":
            self.i += 1
            out = []
            while True:
                self.skip_ws(newlines=True)
                if self.peek() == "]":
                    self.i += 1
                    return out
                out.append(self.value())
                self.skip_ws(newlines=True)
                if self.peek() == ",":
                    self.i += 1
                elif self.peek() != "]":
                    raise self.error("expected ',' or ']' in array")
        if c == "{":
            self.i += 1
            out: dict = {}
            self.skip_ws()
            if self.peek() == "}":
                self.i += 1
                return out
            while True:
                self.key_value(out)
                self.skip_ws()
                if self.peek() == ",":
                    self.i += 1
                elif self.peek() == "}":
                    self.i += 1
                    return out
                else:
                    raise self.error("expected ',' or '}' in inline table")
        for word, val in (("true", True), ("false", False)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        m = _NUMBER.match(self.s, self.i)
        if m and m.group():
            self.i = m.end()
            txt = m.group().replace("_", "")
            try:
                if txt.lstrip("+-")[:2] in ("0x", "0o", "0b"):
                    return int(txt, 0)
                if any(ch in txt for ch in ".eEn") or txt.lstrip("+-") == "inf":
                    return float(txt)
                return int(txt)
            except ValueError:
                raise self.error(f"bad number {m.group()!r}") from None
        raise self.error("unsupported value")

    def literal_string(self) -> str:
        if self.s.startswith("'''", self.i):
            end = self.s.find("'''", self.i + 3)
            if end < 0:
                raise self.error("unterminated string")
            out = self.s[self.i + 3:end]
            self.i = end + 3
            return out[1:] if out.startswith("\n") else out
        end = self.s.find("'", self.i + 1)
        if end < 0 or "\n" in self.s[self.i:end]:
            raise self.error("unterminated string")
        out = self.s[self.i + 1:end]
        self.i = end + 1
        return out

    def basic_string(self) -> str:
        triple = self.s.startswith('"""', self.i)
        self.i += 3 if triple else 1
        if triple and self.peek() == "\n":
            self.i += 1
        out = []
        while True:
            if self.i >= len(self.s):
                raise self.error("unterminated string")
            c = self.s[self.i]
            if triple and self.s.startswith('"""', self.i):
                self.i += 3
                return "".join(out)
            if not triple and c == '"':
                self.i += 1
                return "".join(out)
            if c == "\n" and not triple:
                raise self.error("unterminated string")
            if c == "\\":
                nxt = self.s[self.i + 1:self.i + 2]
                if nxt in _ESCAPES:
                    out.append(_ESCAPES[nxt])
                    self.i += 2
                elif nxt in ("u", "U"):
                    n = 4 if nxt == "u" else 8
                    hexd = self.s[self.i + 2:self.i + 2 + n]
                    try:
                        out.append(chr(int(hexd, 16)))
                    except ValueError:
                        raise self.error("bad unicode escape") from None
                    self.i += 2 + n
                elif triple and nxt in (" ", "\t", "\n"):
                    self.i += 1
                    self.skip_ws(newlines=True)
                else:
                    raise self.error(f"bad escape \\{nxt}")
                continue
            out.append(c)
            self.i += 1


def parse_toml(text: str) -> dict:
    """Parse TOML text with the built-in subset reader (used when tomllib/tomli is missing)."""
    return _TomlReader(text).parse()


def _suggest(word: str, choices, cutoff: float = 0.5) -> str:
    """"'X'을(를) 말씀하신 건가요? " for the nearest valid spelling, or ""."""
    choices = list(dict.fromkeys(choices))
    folded = {c.casefold(): c for c in choices}
    near = difflib.get_close_matches(str(word).casefold(), list(folded), n=1, cutoff=cutoff)
    return f"'{folded[near[0]]}'을(를) 말씀하신 건가요? " if near else ""


def _norm_key(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(name).strip()).casefold()


def _ambiguous_labels() -> set[str]:
    seen: dict[str, int] = {}
    for lab in entities.LABELS_KO.values():
        seen[lab] = seen.get(lab, 0) + 1
    return {lab for lab, n in seen.items() if n > 1}


class _Entities:
    """Case-insensitive entity lookup: built-ins, Korean labels and custom pattern entities."""

    def __init__(self, custom) -> None:
        self.names = list(entities.ALL) + [c for c in dict.fromkeys(custom) if c not in entities.ALL]
        self.by_key = {_norm_key(e): e for e in self.names}
        bad = _ambiguous_labels()
        self.by_label = {lab: ent for ent, lab in entities.LABELS_KO.items() if lab not in bad}

    def get(self, name) -> str | None:
        if not isinstance(name, str):
            return None
        key = name.strip()
        return self.by_key.get(_norm_key(key)) or self.by_label.get(key.replace(" ", ""))

    def require(self, name, where: str) -> str:
        ent = self.get(name)
        if ent is None:
            raise ValueError(
                f"설정 {where}: 알 수 없는 항목 이름 {name!r}. {_suggest(str(name), self.names)}"
                f"쓸 수 있는 이름: {', '.join(self.names)} (unknown entity {name!r})")
        return ent


def _string_list(value, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
        raise ValueError(f"설정 {key}: [\"...\", \"...\"] 처럼 따옴표 친 글자들의 목록이어야 합니다 "
                         f"({key} must be a list of strings)")
    return [str(v) for v in value]


def _column_kind(value, header: str, ents: _Entities) -> str:
    v = str(value).strip() if isinstance(value, (str, int, float)) else ""
    low = v.casefold()
    if low in COLUMN_SKIP:
        return "skip"
    if low in COLUMN_TEXT:
        return "text"
    ent = ents.get(v) if v else None
    if ent is not None:
        return ent
    raise ValueError(
        f"설정 [columns]: '{header}' 열의 유형 {value!r}을(를) 알 수 없습니다. "
        f"{_suggest(v, ents.names + ['skip', 'text'] + list(ents.by_label))}"
        "PERSON, PHONE, RRN, ADDRESS, EMAIL, MRN, BIRTHDATE 같은 항목 이름(또는 이름, 전화번호 같은 "
        f"한글 표시 이름)이나 skip(그대로 두기), text(자동 탐지)를 쓰세요 (unknown column type {value!r})")


def _action(value, ent: str) -> str:
    v = str(value).strip() if isinstance(value, str) else ""
    act = ACTION_ALIASES.get(v.replace(" ", ""), v.casefold())
    if act not in VALID_ACTIONS:
        raise ValueError(
            f"설정 [actions]: {ent}의 처리 방법 {value!r}을(를) 알 수 없습니다. "
            f"{_suggest(v, sorted(VALID_ACTIONS))}pseudo, mask, redact, generalize, keep 중 하나를 쓰세요 "
            f"(invalid action {value!r} for {ent}; use one of {sorted(VALID_ACTIONS)})")
    return act


def config_from_dict(data: dict, base: Config | None = None, *,
                     problems: list[str] | None = None) -> Config:
    """Apply a parsed config.toml dict on top of `base` (default_config()).

    Raises ValueError (Korean message) for anything that would otherwise be silently ignored.
    Softer problems are appended to `problems` when given (see validate_report)."""
    cfg = base or default_config()
    warn = problems.append if problems is not None else (lambda _msg: None)
    if not isinstance(data, dict):
        raise ValueError("설정 파일의 내용이 표(TOML) 형식이 아닙니다 (config must be a TOML table)")
    data = dict(data)

    # names / allowlist that ended up inside a table (a line appended at the end of the file
    # lands in the last table, e.g. [ner]) are lifted to the top level, with a warning.
    lifted: dict[str, list] = {}
    pats = data.get("patterns")
    holders = [(f"[{t}]", data, t) for t in TABLES if isinstance(data.get(t), dict)]
    if isinstance(pats, list):
        pats = data["patterns"] = [dict(x) if isinstance(x, dict) else x for x in pats]
        holders += [(f"[[patterns]] {i}번째", pats, i - 1)
                    for i, x in enumerate(pats, 1) if isinstance(x, dict)]
    for where, parent, slot in holders:
        sub = parent[slot] = dict(parent[slot])
        for key in ("names", "allowlist"):
            if key in sub and isinstance(sub[key], (list, str)):
                lifted.setdefault(key, []).extend(_string_list(sub.pop(key), key))
                warn(f"{key} 목록이 {where} 표 안에 들어가 있습니다. 이번에는 적용했지만, "
                     + _PLACE_HINT.format(key=key) + f" ({key} found inside {where})")
        if where in ("[ocr]", "[ner]", "[llm]") or where.startswith("[[patterns]]"):
            for key in ("enabled", "disabled", "min_score"):
                if key in sub and not (key == "min_score" and where == "[ner]"):
                    warn(f"{key}가 {where} 표 안에 있어 적용되지 않습니다. 파일 맨 위(첫 번째 [표] "
                         f"제목보다 위)로 옮기세요 ({key} inside {where} has no effect)")

    allowed = TOP_LEVEL_KEYS + TABLES + ARRAY_TABLES
    for key, value in data.items():
        if key in allowed:
            continue
        if isinstance(value, dict):
            raise ValueError(
                f"설정 파일: 알 수 없는 표 [{key}]. {_suggest(key, TABLES + ARRAY_TABLES)}쓸 수 있는 표: "
                f"[actions], [labels], [columns], [[patterns]], [ocr], [ner], [llm] (unknown table [{key}])")
        raise ValueError(
            f"설정 파일: 알 수 없는 설정 {key!r}. {_suggest(key, allowed)}맨 위에 쓸 수 있는 것: "
            f"{', '.join(TOP_LEVEL_KEYS)} (unknown key {key!r})")
    for table in TABLES:
        if table in data and not isinstance(data[table], dict):
            raise ValueError(f"설정 [{table}]는 표여야 합니다: 따로 한 줄에 [{table}]를 쓰고 그 아래에 "
                             f"이름 = \"값\"을 적으세요 ([{table}] must be a table)")
    if pats is not None and not isinstance(pats, list):
        raise ValueError("설정 [patterns]: 대괄호 두 개로 [[patterns]]라고 써야 합니다 (항목마다 한 번씩) "
                         "([[patterns]] must be an array of tables)")

    # [[patterns]] first: their entities are valid names everywhere else.
    ents0 = _Entities([p.entity for p in cfg.custom_patterns])
    new_patterns = []
    for i, p in enumerate(pats or [], 1):
        if not isinstance(p, dict):
            raise ValueError(f"설정 [[patterns]] {i}번째: entity = ..., regex = ... 를 가진 표여야 합니다")
        if "entity" not in p or "regex" not in p:
            missing = "entity" if "entity" not in p else "regex"
            raise ValueError(f"설정 [[patterns]] {i}번째 항목에 {missing}가 없습니다. entity = \"STAFF_ID\", "
                             f"regex = 'EMP-[0-9]{{6}}' 처럼 둘 다 쓰세요 (pattern {i}: missing {missing!r})")
        for k in p:
            if k not in PATTERN_KEYS and k not in ("enabled", "disabled", "min_score"):
                warn(f"[[patterns]] {i}번째 항목의 {k!r}는 쓰이지 않습니다. {_suggest(k, PATTERN_KEYS)}"
                     f"(unknown pattern key {k!r})")
        raw_ent = str(p["entity"]).strip()
        ent = ents0.get(raw_ent)   # "mrn" -> "MRN"; an earlier custom spelling is reused
        if ent is None:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", raw_ent):
                raise ValueError(f"설정 [[patterns]] {i}번째: entity 이름 {raw_ent!r}은(는) 영문자, 숫자, _ 만 "
                                 f"쓰세요 (예: STAFF_ID) (invalid entity name {raw_ent!r})")
            ent = raw_ent
            ents0 = _Entities([x.entity for x in cfg.custom_patterns + new_patterns] + [ent])
            near = _suggest(ent, entities.ALL, cutoff=0.8)   # "PERSN", not "CHART" (CARD)
            if near:
                warn(f"[[patterns]] {i}번째의 entity {raw_ent!r}은(는) 새 항목으로 취급됩니다. {near}"
                     f"(new entity {raw_ent!r})")
        try:
            re.compile(str(p["regex"]))
        except re.error as e:
            raise ValueError(f"설정 [[patterns]] {i}번째({ent})의 regex가 잘못되었습니다: {e} "
                             f"(invalid regex for {ent!r})") from None
        try:
            score = float(p.get("score", 0.9))
        except (TypeError, ValueError):
            raise ValueError(f"설정 [[patterns]] {i}번째의 score는 0.0-1.0 사이 숫자여야 합니다") from None
        context = p.get("context") or []
        new_patterns.append(CustomPattern(entity=ent, regex=str(p["regex"]), score=score,
                                          context=_string_list(context, "context")))
    ents = _Entities([p.entity for p in cfg.custom_patterns + new_patterns])

    actions = {}
    for key, act in (data.get("actions") or {}).items():
        ent = ents.require(key, "[actions]")
        if isinstance(act, (list, dict)):
            raise ValueError(f"설정 [actions]: {key} = \"mask\" 처럼 처리 방법 하나만 쓰세요 "
                             f"(목록을 넣으려 했다면 " + _PLACE_HINT.format(key="names") + ")")
        actions[ent] = _action(act, ent)
    labels = {}
    for key, lab in (data.get("labels") or {}).items():
        ent = ents.require(key, "[labels]")
        if not isinstance(lab, str) or not lab.strip():
            raise ValueError(f"설정 [labels]: {key} = \"환자\" 처럼 따옴표 친 표시 이름을 쓰세요 "
                             f"(label for {key} must be a non-empty string)")
        labels[ent] = lab.strip()
    columns = {}
    for header, kind in (data.get("columns") or {}).items():
        columns[str(header)] = _column_kind(kind, str(header), ents)

    def entity_list(key: str) -> set[str]:
        return {ents.require(v, key) for v in _string_list(data[key], key)}

    enabled = entity_list("enabled") if data.get("enabled") else None
    disabled = entity_list("disabled") if data.get("disabled") else None
    min_score = None
    if "min_score" in data:
        try:
            min_score = float(data["min_score"])
        except (TypeError, ValueError):
            min_score = -1.0
        if not 0.0 <= min_score <= 1.0:
            raise ValueError(f"설정 min_score는 0.0-1.0 사이 숫자여야 합니다: {data['min_score']!r} "
                             "(min_score must be a number between 0 and 1)")
    names = _string_list(data.get("names") or [], "names") + lifted.get("names", [])
    allow = _string_list(data.get("allowlist") or [], "allowlist") + lifted.get("allowlist", [])
    for table, known in _TABLE_KEYS.items():
        for k in (data.get(table) or {}):
            if k not in known and k not in TOP_LEVEL_KEYS:   # those are reported above
                warn(f"[{table}]의 {k!r}는 알 수 없는 설정이라 무시됩니다. {_suggest(k, sorted(known))}"
                     f"(unknown [{table}] key {k!r})")

    # Everything checked: apply.
    cfg.custom_patterns.extend(new_patterns)
    cfg.actions.update(actions)
    cfg.labels.update(labels)
    cfg.names.extend(n.strip() for n in names if n.strip())
    cfg.allowlist.extend(allow)
    if min_score is not None:
        cfg.min_score = min_score
    if enabled:
        cfg.enabled = enabled
    if disabled:
        base_set = cfg.enabled if cfg.enabled is not None else set(entities.ALL) | {
            p.entity for p in cfg.custom_patterns
        }
        cfg.enabled = base_set - disabled
    cfg.ner.update(data.get("ner") or {})
    cfg.ocr.update(data.get("ocr") or {})
    cfg.llm.update(data.get("llm") or {})
    cfg.columns.update(columns)
    return cfg


def validate(path: str | os.PathLike | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a config file (default location when `path` is None).

    Errors stop every deid command (load_config raises them); warnings are applied or ignored
    settings the user should fix. A missing default file is no problem."""
    p = Path(path).expanduser() if path is not None else _default_path()
    if p is None:
        return [], []
    if not p.exists():
        return [f"설정 파일을 찾을 수 없습니다: {p} (config file not found)"], []
    warnings: list[str] = []
    try:
        config_from_dict(read_toml(p), default_config(), problems=warnings)
    except (ValueError, TypeError, OSError) as e:
        return [str(e)], warnings
    return [], warnings


def validate_report(path: str | os.PathLike | None = None) -> list[str]:
    """Problems in the config file (errors first, then warnings), as Korean sentences."""
    errors, warnings = validate(path)
    return errors + warnings
