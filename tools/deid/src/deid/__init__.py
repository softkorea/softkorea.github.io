"""deid: Korean-first local de-identification toolkit.

Quick start::

    from deid import deidentify
    text, report = deidentify("환자명: 홍길동, 연락처 010-1234-5678")
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"

# Public name -> submodule; imported on first use so "import deid" stays cheap.
_EXPORTS = {
    "deidentify": "api",
    "Detector": "engine",
    "Anonymizer": "anonymizer",
    "Vault": "vault",
    "VaultError": "vault",
    "Config": "config",
    "default_config": "config",
    "load_config": "config",
    "Span": "types",
    "Report": "types",
}
__all__ = ["__version__", *_EXPORTS]

if TYPE_CHECKING:  # pragma: no cover
    from .anonymizer import Anonymizer
    from .api import deidentify
    from .config import Config, default_config, load_config
    from .engine import Detector
    from .types import Report, Span
    from .vault import Vault, VaultError


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module 'deid' has no attribute {name!r}")
    value = getattr(import_module(f".{mod}", __name__), name)
    globals()[name] = value
    return value
