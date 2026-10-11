"""One-call convenience API."""
from __future__ import annotations

from .anonymizer import Anonymizer
from .config import Config, default_config
from .engine import Detector
from .types import Report
from .vault import Vault


def deidentify(text: str, cfg: Config | None = None, vault: Vault | None = None, *,
               detector: Detector | None = None) -> tuple[str, Report]:
    """De-identify `text`; returns (new_text, report with per-entity counts).

    `cfg` None -> default_config() (pass deid.config.load_config() for the user's file).
    Reuse `vault` to keep pseudonyms consistent across calls and to restore them later, and
    `detector` to avoid rebuilding recognizers (e.g. a loaded NER model) on every call.
    """
    if cfg is None:
        cfg = detector.cfg if detector is not None else default_config()
    det = detector if detector is not None else Detector(cfg)
    result = Anonymizer(cfg, vault).anonymize(text, det.detect(text))
    report = Report()
    for r in result.replacements:
        report.add(r.entity)
    return result.text, report
