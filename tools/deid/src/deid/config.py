"""Configuration loading (TOML)."""
from __future__ import annotations

import os
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
    if tomllib is None:  # pragma: no cover
        raise RuntimeError("python 3.10 needs 'pip install tomli' to read config files")
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return config_from_dict(data, cfg)


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
    return cfg
