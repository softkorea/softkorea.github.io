"""Configuration loading (TOML)."""
from __future__ import annotations

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
    with open(p, "rb") as f:
        raw = f.read()
    if tomllib is not None:
        data = tomllib.loads(raw.decode("utf-8"))
    else:  # python 3.10 without tomli: the small built-in reader below
        data = parse_toml(raw.decode("utf-8"))
    return config_from_dict(data, cfg)


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


def config_from_dict(data: dict, base: Config | None = None) -> Config:
    cfg = base or default_config()
    for ent, act in (data.get("actions") or {}).items():
        if act not in VALID_ACTIONS:
            raise ValueError(f"invalid action {act!r} for {ent}; use one of {sorted(VALID_ACTIONS)}")
        cfg.actions[ent] = act
    cfg.labels.update(data.get("labels") or {})
    cfg.names.extend(str(n).strip() for n in data.get("names") or [] if str(n).strip())
    cfg.allowlist.extend(str(a) for a in data.get("allowlist") or [])
    for p in data.get("patterns") or []:
        cfg.custom_patterns.append(
            CustomPattern(
                entity=p["entity"],
                regex=p["regex"],
                score=float(p.get("score", 0.9)),
                context=list(p.get("context") or []),
            )
        )
    if "min_score" in data:
        cfg.min_score = float(data["min_score"])
    if data.get("enabled"):
        cfg.enabled = set(data["enabled"])
    if data.get("disabled"):
        base_set = cfg.enabled if cfg.enabled is not None else set(entities.ALL) | {
            p.entity for p in cfg.custom_patterns
        }
        cfg.enabled = base_set - set(data["disabled"])
    cfg.ner.update(data.get("ner") or {})
    cfg.ocr.update(data.get("ocr") or {})
    cfg.llm.update(data.get("llm") or {})
    cols = data.get("columns") or {}
    if not isinstance(cols, dict):
        raise ValueError("[columns] must be a table of \"header\" = \"TYPE\" entries")
    cfg.columns.update({str(k): str(v) for k, v in cols.items()})
    return cfg
