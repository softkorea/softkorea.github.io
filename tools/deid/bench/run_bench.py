#!/usr/bin/env python3
"""Score deid's Detector on bench/corpus.jsonl: per-entity precision / recall / F1.

Two matching rules:
  exact    predicted (start, end, entity) equals a gold span
  overlap  same entity and the character ranges overlap (each gold/predicted span counts once)
The ANY row ignores the entity: a gold span counts as covered when any predicted span overlaps
it. That is the number that matters for leaks.

Usage: python bench/run_bench.py [--corpus bench/corpus.jsonl] [--config deid.toml] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from deid.config import default_config, load_config
from deid.engine import Detector

ANY = "ANY"


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def _match(gold: list[dict], pred: list[dict], same_entity: bool) -> tuple[set[int], set[int]]:
    """Greedy one-to-one overlap matching; returns (matched gold idx, matched pred idx)."""
    used_g: set[int] = set()
    used_p: set[int] = set()
    for pi, p in enumerate(pred):
        for gi, g in enumerate(gold):
            if gi in used_g or not _overlaps(g, p):
                continue
            if same_entity and g["entity"] != p["entity"]:
                continue
            used_g.add(gi)
            used_p.add(pi)
            break
    return used_g, used_p


def _prf(tp_p: int, n_pred: int, tp_g: int, n_gold: int) -> tuple[float, float, float]:
    p = tp_p / n_pred if n_pred else 1.0
    r = tp_g / n_gold if n_gold else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def evaluate(samples: list[dict], detector: Detector) -> tuple[dict, list[dict]]:
    """Per-entity counters and per-sample error records."""
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    errors: list[dict] = []
    for s in samples:
        text, gold = s["text"], s["spans"]
        pred = [{"start": sp.start, "end": sp.end, "entity": sp.entity} for sp in detector.detect(text)]
        exact = {(g["start"], g["end"], g["entity"]) for g in gold}
        for g in gold:
            stats[g["entity"]]["gold"] += 1
        for p in pred:
            stats[p["entity"]]["pred"] += 1
            stats[p["entity"]]["exact"] += (p["start"], p["end"], p["entity"]) in exact
        mg, mp = _match(gold, pred, same_entity=True)
        for gi in mg:
            stats[gold[gi]["entity"]]["ov_gold"] += 1
        for pi in mp:
            stats[pred[pi]["entity"]]["ov_pred"] += 1
        ag, ap = _match(gold, pred, same_entity=False)
        st = stats[ANY]
        st["gold"] += len(gold)
        st["pred"] += len(pred)
        st["exact"] += sum((p["start"], p["end"], p["entity"]) in exact for p in pred)
        st["ov_gold"] += len(ag)
        st["ov_pred"] += len(ap)
        missed = [g for i, g in enumerate(gold) if i not in mg]
        spurious = [p for i, p in enumerate(pred) if i not in mp]
        if missed or spurious:
            errors.append({"id": s.get("id", "?"), "text": text, "missed": missed, "spurious": spurious})
    return stats, errors


def format_table(stats: dict) -> str:
    head = (f"{'entity':<17}{'gold':>5}{'pred':>5} | {'exact P':>8}{'R':>7}{'F1':>7} | "
            f"{'overlap P':>9}{'R':>7}{'F1':>7}")
    lines = [head, "-" * len(head)]
    ents = sorted(e for e in stats if e != ANY)
    for ent in ents + [ANY]:
        st = stats[ent]
        ep, er, ef = _prf(st["exact"], st["pred"], st["exact"], st["gold"])
        op, orr, of = _prf(st["ov_pred"], st["pred"], st["ov_gold"], st["gold"])
        if ent == ANY:
            lines.append("-" * len(head))
        lines.append(f"{ent:<17}{st['gold']:>5}{st['pred']:>5} | {ep:>8.3f}{er:>7.3f}{ef:>7.3f} | "
                     f"{op:>9.3f}{orr:>7.3f}{of:>7.3f}")
    return "\n".join(lines)


def format_errors(errors: list[dict]) -> str:
    out = []
    for e in errors:
        t = e["text"]
        out.append(f"[{e['id']}] {t!r}")
        for g in e["missed"]:
            out.append(f"    missed   {g['entity']:<16} {t[g['start']:g['end']]!r}")
        for p in e["spurious"]:
            out.append(f"    spurious {p['entity']:<16} {t[p['start']:p['end']]!r}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default=str(Path(__file__).with_name("corpus.jsonl")))
    ap.add_argument("--config", help="deid TOML config (default: built-in defaults)")
    ap.add_argument("-v", "--verbose", action="store_true", help="list missed and spurious spans")
    args = ap.parse_args(argv)

    with open(args.corpus, encoding="utf-8") as f:
        samples = [json.loads(line) for line in f if line.strip()]
    cfg = load_config(args.config) if args.config else default_config()
    detector = Detector(cfg)
    t0 = time.perf_counter()
    stats, errors = evaluate(samples, detector)
    dt = time.perf_counter() - t0
    negatives = sum(1 for s in samples if not s["spans"])
    print(f"{len(samples)} samples ({negatives} without PII), {dt * 1000:.0f} ms")
    print(format_table(stats))
    if args.verbose and errors:
        print("\nErrors (overlap rule):")
        print(format_errors(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
