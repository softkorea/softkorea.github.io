#!/usr/bin/env python3
"""Value-level leak score for deid on held-out corpora (bench/fresh_a.jsonl, bench/fresh_b.jsonl).

Usage
  python bench/score_values.py CORPUS.jsonl [CORPUS2 ...] [--config deid.toml] [--json OUT]
                               [--verbose] [--limit N]

Run it with the Python that has deid installed, e.g. for pipx
"$(pipx environment --value PIPX_LOCAL_VENVS)/deid-kit/bin/python" (see bench/README.md).
Without --config the built-in defaults are used (DEID_CONFIG and ~/.config/deid/config.toml are
NOT read), so the numbers are the rules-only baseline. With --config the file is loaded with
deid.config.load_config, e.g. a config whose [ner] backend sends text to the clinic LLM server.

Corpus lines: {"id", "genre", "lang", "text", "pii": [{"value", "entity"}], "keep": [str]}

Every document goes through deid.api.deidentify(text, cfg, Vault(), detector=det) with ONE
Detector built before the loop (a fresh Vault per document). Then:

  Leak / recall (value level). Gold values are de-duplicated per document by (entity, value);
    a gold value that does not occur in its input is counted as "absent" and skipped. A gold
    value is LEAKED if
      (a) it still appears verbatim in the output, or
      (b) it has >= 6 digits and its digit string occurs inside the digits of some run of
          [0-9 .-] in the output (separators removed). Full-width digits and separators are
          folded to ASCII first. "010 4827 3391" leaks gold "010-4827-3391"; a masked run such
          as "010-****-3391" is broken by "*" and does not.
    recall = protected / evaluated, per gold entity, per corpus, per language and overall.
    Strict: a short gold value that also occurs inside unrelated output text counts as leaked.
  Keep survival: keep strings present in the input that are still present verbatim in the output.
  Spurious changed regions: input and output are split into \\w+ runs, whitespace runs and single
    punctuation characters; difflib.SequenceMatcher(autojunk=False) aligns the two token lists
    and every non-"equal" opcode is one changed region (an insertion is a zero-width region at
    its input position). A region is spurious when it touches no occurrence of any gold value
    of that document (overlap, or a zero-width region at/inside a gold occurrence). This is
    over-masking: a visit date or a drug name that was changed although nothing there was PII.
  Spans by entity: deid's own report counts (every replaced or kept span, so repeated
    occurrences count again). Useful to see e.g. more BIRTHDATE spans than gold birth dates.
  Time: wall time of the deidentify() calls only; the Detector build is reported separately.
  LLM requests: if a recognizer of the Detector has an integer `request_count` (the
    OpenAI-compatible backend), the number of requests made during the run; also its
    failed_requests / dropped / realigned counters when present (JSON "llm").

A document whose deidentify() raises (e.g. the LLM pass failed and the backend is set to fail)
is reported as failed and left out of the metrics. Error lines show the exception type and
deid's message, never document text. --verbose and --json list leaked values and spurious regions:
that is fine for these synthetic corpora, but do not point this tool at real records.

The exit status is always 0: this is a measurement, not a gate.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, TextIO

DIGIT_RUN = re.compile(r"[0-9][0-9 .\-]*")
TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
# Full-width digits and separators -> ASCII before the digit rule ("０１０－６６２１－３０９８").
_FW = str.maketrans("０１２３４５６７８９－．　", "0123456789-. ")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
MIN_DIGITS = 6
# Consecutive failed documents after which the run stops (server off, wrong key: every further
# document would fail too, each after the backend's timeout).
STOP_AFTER_FAILURES = 3
FIELDS = ("id", "text", "pii")


# ------------------------------------------------------------------------------- corpus

def load_corpus(path: str | Path) -> list[dict]:
    """Read a JSONL corpus. Errors name the file and line, never the line's content."""
    docs = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"{path}:{lineno}: JSON 형식 오류 (invalid JSON)") from None
            missing = [k for k in FIELDS if k not in d] if isinstance(d, dict) else list(FIELDS)
            if missing:
                raise ValueError(f"{path}:{lineno}: 필드 없음 (missing field) {', '.join(missing)}")
            docs.append(d)
    return docs


def dedupe_gold(pii: Iterable[dict]) -> list[dict]:
    seen, out = set(), []
    for g in pii:
        key = (g["entity"], g["value"])
        if key not in seen and g["value"]:
            seen.add(key)
            out.append(g)
    return out


# ------------------------------------------------------------------------------- metrics

def occurrences(text: str, value: str) -> list[tuple[int, int]]:
    out, i = [], text.find(value) if value else -1
    while i >= 0:
        out.append((i, i + len(value)))
        i = text.find(value, i + 1)
    return out


def digits(s: str) -> str:
    return re.sub(r"[^0-9]", "", s.translate(_FW))


def digit_runs(text: str) -> list[str]:
    return [digits(m.group()) for m in DIGIT_RUN.finditer(text.translate(_FW))]


def leak_kind(value: str, output: str, runs: list[str] | None = None) -> str | None:
    """"verbatim", "digits" or None (protected)."""
    if value in output:
        return "verbatim"
    d = digits(value)
    if len(d) >= MIN_DIGITS:
        if runs is None:
            runs = digit_runs(output)
        if any(d in run for run in runs):
            return "digits"
    return None


def changed_regions(src: str, out: str) -> list[tuple[int, int, str, str]]:
    """(input_start, input_end, input_text, output_text) for every non-equal token opcode."""
    a = [(m.start(), m.group()) for m in TOKEN.finditer(src)]
    b = [m.group() for m in TOKEN.finditer(out)]
    sm = difflib.SequenceMatcher(None, [t for _, t in a], b, autojunk=False)
    regions = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        start = a[i1][0] if i1 < len(a) else len(src)
        end = (a[i2 - 1][0] + len(a[i2 - 1][1])) if i2 > i1 else start
        regions.append((start, end, src[start:end], "".join(b[j1:j2])))
    return regions


def touches(region: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    s, e = region
    if s == e:
        return any(gs <= s <= ge for gs, ge in spans)
    return any(s < ge and gs < e for gs, ge in spans)


def score_doc(doc: dict, output: str) -> dict:
    """Metrics of one document given deid's output (no timing, no report)."""
    text = doc["text"]
    runs = digit_runs(output)
    evaluated: dict[str, int] = defaultdict(int)
    leaked: dict[str, int] = defaultdict(int)
    leaks, gold_spans, absent = [], [], 0
    for g in dedupe_gold(doc.get("pii", [])):
        occ = occurrences(text, g["value"])
        if not occ:
            absent += 1
            continue
        gold_spans += occ
        evaluated[g["entity"]] += 1
        how = leak_kind(g["value"], output, runs)
        if how:
            leaked[g["entity"]] += 1
            leaks.append({"entity": g["entity"], "value": g["value"], "how": how})
    keep_n = keep_ok = 0
    keep_damaged = []
    for k in doc.get("keep", []):
        if not k or k not in text:
            continue
        keep_n += 1
        if k in output:
            keep_ok += 1
        else:
            keep_damaged.append(k)
    regions = changed_regions(text, output)
    spurious = [
        {"input": src, "output": out, "context": text[max(0, s - 15):e + 15]}
        for s, e, src, out in regions if not touches((s, e), gold_spans)
    ]
    return {
        "evaluated": dict(evaluated), "leaked": dict(leaked), "leaks": leaks, "absent": absent,
        "keep_present": keep_n, "keep_survived": keep_ok, "keep_damaged": keep_damaged,
        "changed_regions": len(regions), "spurious": spurious,
    }


# ------------------------------------------------------------------------------- deid

def build_detector(config: str | None):
    """(cfg, detector, seconds). No --config -> built-in defaults, not the user's file."""
    from deid.config import default_config, load_config
    from deid.engine import Detector

    cfg = load_config(config) if config else default_config()
    t0 = time.perf_counter()
    det = Detector(cfg)
    return cfg, det, time.perf_counter() - t0


# Integer diagnostics an LLM recognizer may expose (looked up with getattr, so this file works
# with or without deid.backends.openai_compat): requests sent, requests that failed, items the
# model returned that are not in the text, items re-aligned to the text's whitespace.
LLM_COUNTERS = ("request_count", "failed_requests", "dropped", "realigned")


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def llm_counters(detector: Any) -> dict[str, int] | None:
    """Sums of LLM_COUNTERS over recognizers that have an integer `request_count`; None when
    the detector has no such recognizer (rules only, or another backend)."""
    out: dict[str, int] | None = None
    for rec in getattr(detector, "recognizers", None) or []:
        if _int(getattr(rec, "request_count", None)) is None:
            continue
        out = out if out is not None else dict.fromkeys(LLM_COUNTERS, 0)
        for name in LLM_COUNTERS:
            out[name] += _int(getattr(rec, name, None)) or 0
    return out


def llm_request_count(detector: Any) -> int | None:
    """Sum of `request_count` over the detector's recognizers, or None without an LLM backend."""
    c = llm_counters(detector)
    return None if c is None else c["request_count"]


def backend_summary(cfg: Any) -> dict:
    """Non-secret [ner] settings for the report (never keys, key names, or URLs)."""
    ner = getattr(cfg, "ner", None) or {}
    out = {}
    for k in ("backend", "model", "effort"):
        v = ner.get(k) if isinstance(ner, dict) else None
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out[k] = str(v)
    return out


def safe_error(e: BaseException) -> str:
    """Exception type and message, with any 'Bearer …' token blanked, first line only."""
    msg = _BEARER.sub("Bearer ***", str(e)).strip().splitlines()
    return f"{type(e).__name__}: {msg[0][:300]}" if msg else type(e).__name__


# ------------------------------------------------------------------------------- run

def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _bucket() -> dict:
    return {"evaluated": 0, "leaked": 0}


def evaluate(corpora: list[tuple[str, list[dict]]], detector: Any, cfg: Any,
             progress: TextIO | None = None) -> dict:
    """Run deidentify over every document and aggregate. Ctrl-C keeps the partial result."""
    from deid.api import deidentify
    from deid.vault import Vault

    per_entity: dict[str, dict] = defaultdict(_bucket)
    per_corpus: dict[str, dict] = defaultdict(_bucket)
    per_lang: dict[str, dict] = defaultdict(_bucket)
    spans: dict[str, int] = defaultdict(int)
    docs_out: list[dict] = []
    totals = {"absent": 0, "keep_present": 0, "keep_survived": 0, "changed_regions": 0,
              "spurious": 0, "spurious_chars": 0, "warnings": 0, "docs_with_warnings": 0}
    items = [(cname, doc) for cname, docs in corpora for doc in docs]
    n_total = len(items)
    llm0 = llm_counters(detector)
    total_s, done, failed, in_a_row = 0.0, 0, 0, 0
    interrupted = stopped = False
    try:
        for cname, doc in items:
            if in_a_row >= STOP_AFTER_FAILURES:
                stopped = True
                break
            if progress is not None:
                progress.write(f"\r  {done + failed + 1}/{n_total} ")
                progress.flush()
            rec: dict[str, Any] = {"corpus": cname, "id": str(doc.get("id", "?"))}
            t0 = time.perf_counter()
            try:
                output, report = deidentify(doc["text"], cfg, Vault(), detector=detector)
            except Exception as e:  # noqa: BLE001 - one failed document must not end the run
                rec["ms"] = round(1000 * (time.perf_counter() - t0), 1)
                rec["error"] = safe_error(e)
                failed += 1
                in_a_row += 1
                docs_out.append(rec)
                continue
            dt = time.perf_counter() - t0
            total_s += dt
            done += 1
            in_a_row = 0
            res = score_doc(doc, output)
            lang = str(doc.get("lang") or "?")
            for ent, n in res["evaluated"].items():
                lk = res["leaked"].get(ent, 0)
                for b in (per_entity[ent], per_corpus[cname], per_lang[lang]):
                    b["evaluated"] += n
                    b["leaked"] += lk
            for ent, n in (getattr(report, "counts", None) or {}).items():
                spans[ent] += n
            warnings = list(getattr(report, "warnings", None) or [])
            totals["warnings"] += len(warnings)
            totals["docs_with_warnings"] += bool(warnings)
            totals["absent"] += res["absent"]
            totals["keep_present"] += res["keep_present"]
            totals["keep_survived"] += res["keep_survived"]
            totals["changed_regions"] += res["changed_regions"]
            totals["spurious"] += len(res["spurious"])
            totals["spurious_chars"] += sum(len(s["input"]) for s in res["spurious"])
            rec.update(ms=round(1000 * dt, 1), leaks=res["leaks"], spurious=res["spurious"],
                       keep_damaged=res["keep_damaged"], warnings=warnings)
            docs_out.append(rec)
    except KeyboardInterrupt:
        interrupted = True
    if progress is not None:
        progress.write("\r" + " " * 16 + "\r")
        progress.flush()
    llm1 = llm_counters(detector)
    llm = None if llm1 is None else {k: v - (llm0 or {}).get(k, 0) for k, v in llm1.items()}
    evaluated = sum(b["evaluated"] for b in per_entity.values())
    leaked = sum(b["leaked"] for b in per_entity.values())

    def table(d: dict) -> dict:
        return {k: {**v, "recall": _ratio(v["evaluated"] - v["leaked"], v["evaluated"])}
                for k, v in sorted(d.items())}

    return {
        "overall": {"evaluated": evaluated, "leaked": leaked, "protected": evaluated - leaked,
                    "recall": _ratio(evaluated - leaked, evaluated), "absent_from_input": totals["absent"]},
        "per_entity": table(per_entity),
        "per_corpus": table(per_corpus),
        "per_lang": table(per_lang),
        "keep": {"survived": totals["keep_survived"], "present": totals["keep_present"],
                 "rate": _ratio(totals["keep_survived"], totals["keep_present"])},
        "spurious": {"regions": totals["spurious"], "chars": totals["spurious_chars"],
                     "all_changed_regions": totals["changed_regions"],
                     "per_doc": round(totals["spurious"] / done, 3) if done else None},
        "spans_by_entity": dict(sorted(spans.items())),
        "timing": {"total_s": round(total_s, 3),
                   "ms_per_doc": round(1000 * total_s / done, 1) if done else None},
        "llm_requests": None if llm is None else llm["request_count"],
        "llm": llm,
        "docs": {"total": n_total, "scored": done, "failed": failed,
                 "skipped": n_total - done - failed, "interrupted": interrupted,
                 "stopped_after_failures": stopped},
        "warnings": {"total": totals["warnings"], "docs": totals["docs_with_warnings"]},
        "per_doc": docs_out,
    }


# ------------------------------------------------------------------------------- printing

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _width(s: str) -> int:
    """Terminal columns: Hangul and other wide characters take two."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _l(s: object, n: int) -> str:
    s = str(s)
    return s + " " * max(0, n - _width(s))


def _r(s: object, n: int) -> str:
    s = str(s)
    return " " * max(0, n - _width(s)) + s


def _row(label: str, gold: object, leaked: object, recall: object, spans: object = "") -> str:
    return _l(label, 20) + _r(gold, 6) + _r(leaked, 14) + _r(recall, 16) + _r(spans, 13)


def format_table(res: dict, meta: dict) -> str:
    lines = ["deid 값 단위 누출 점수 (value-level leak score)"]
    be = meta.get("backend") or {}
    if be.get("backend"):
        desc = ", ".join(f"{k}={v}" for k, v in be.items())
        lines.append(f"설정 config: {meta.get('config')}  [ner] {desc}")
    else:
        lines.append(f"설정 config: {meta.get('config')}  [ner] 없음 (rules only)")
    lines.append("코퍼스 corpus: " + ", ".join(f"{c['name']} {c['docs']}문서" for c in meta["corpora"]))
    head = _row("항목 entity", "gold", "누출 leaked", "보호율 recall", "탐지 spans")
    rule = "-" * _width(head)
    lines += ["", head, rule]
    spans = res["spans_by_entity"]
    for ent, b in res["per_entity"].items():
        lines.append(_row(ent, b["evaluated"], b["leaked"], _pct(b["recall"]), spans.get(ent, 0)))
    for ent in sorted(set(spans) - set(res["per_entity"])):
        lines.append(_row(ent, "-", "-", "-", spans[ent]))
    lines.append(rule)
    o = res["overall"]
    lines.append(_row("전체 OVERALL", o["evaluated"], o["leaked"], _pct(o["recall"])).rstrip())
    for group in ("per_corpus", "per_lang"):
        for k, b in res[group].items():
            label = ("  " + k) if group == "per_corpus" else f"  lang={k}"
            lines.append(_row(label, b["evaluated"], b["leaked"], _pct(b["recall"])).rstrip())
    kp, sp, tm, dc = res["keep"], res["spurious"], res["timing"], res["docs"]
    # from the counts, not from the 3-digit "per_doc" (9/95 = 0.0947 must read 0.09, not 0.10)
    per_doc = "n/a" if not dc["scored"] else f"{sp['regions'] / dc['scored']:.2f}"
    ms = "n/a" if tm["ms_per_doc"] is None else f"{tm['ms_per_doc']:.1f}"
    lines += [
        "",
        _l("keep 보존 survival", 22) + f"{_pct(kp['rate'])} ({kp['survived']}/{kp['present']})",
        _l("과잉 변경 spurious", 22) + f"{sp['regions']} ({per_doc}/문서 doc; 변경 영역 전체 all changed {sp['all_changed_regions']})",
        _l("시간 time", 22) + f"{tm['total_s']:.2f} s, {ms} ms/문서 doc (탐지기 생성 build {meta['construct_s']:.2f} s)",
    ]
    if res["llm_requests"] is None:
        lines.append(_l("LLM 요청 requests", 22) + "- (LLM 백엔드 없음 no LLM backend)")
    else:
        per = f", {res['llm_requests'] / dc['scored']:.2f}/문서 doc" if dc["scored"] else ""
        bad = (res.get("llm") or {}).get("failed_requests", 0)
        lines.append(_l("LLM 요청 requests", 22) + f"{res['llm_requests']}{per}"
                     + (f", 실패 failed {bad}" if bad else ""))
    lines.append(_l("문서 docs", 22) + f"채점 scored {dc['scored']}/{dc['total']}, 실패 failed {dc['failed']}"
                 + (f", 중단 interrupted (남은 문서 skipped {dc['skipped']})" if dc["interrupted"] else ""))
    if dc.get("stopped_after_failures"):
        lines.append(f"연속 {STOP_AFTER_FAILURES}개 문서가 실패해 멈췄습니다: 서버·키·설정을 확인하세요 "
                     f"(stopped after {STOP_AFTER_FAILURES} failures in a row; {dc['skipped']} docs not run)")
    if res["warnings"]["total"]:
        lines.append(_l("경고 warnings", 22) + f"{res['warnings']['total']} ({res['warnings']['docs']}문서 docs)")
    if o["absent_from_input"]:
        lines.append(f"(본문에 없는 gold 값 {o['absent_from_input']}개는 제외 / gold values absent from input skipped)")
    if dc["failed"]:
        lines.append("주의: 실패한 문서는 점수에서 빠졌습니다 (failed documents are excluded from the scores).")
    return "\n".join(lines)


def format_verbose(res: dict) -> str:
    out = []
    for d in res["per_doc"]:
        head = f"[{d['corpus']} {d['id']}]"
        if d.get("error"):
            out.append(f"{head} 실패 failed: {d['error']}")
            continue
        rows = [f"  누출 leaked   {lk['entity']:<16} {lk['value']!r}" + (" (digits)" if lk["how"] == "digits" else "")
                for lk in d["leaks"]]
        rows += [f"  과잉 spurious {s['input']!r} -> {s['output']!r}   ...{s['context']!r}..."
                 for s in d["spurious"]]
        rows += [f"  keep 손상     {k!r}" for k in d["keep_damaged"]]
        rows += [f"  경고 warning  {w}" for w in d["warnings"]]
        if rows:
            out.append(head)
            out += rows
    return "\n".join(out) if out else "(누출·과잉 변경 없음 / no leaks, no spurious regions)"


# ------------------------------------------------------------------------------- main

def run(paths: list[str], config: str | None = None, limit: int | None = None,
        json_out: str | None = None, verbose: bool = False, out: TextIO | None = None,
        progress: TextIO | None = None) -> dict | None:
    """Score the corpora and print the table; returns the result dict (None on a setup error)."""
    out = out if out is not None else sys.stdout
    corpora: list[tuple[str, list[dict]]] = []
    try:
        for p in paths:
            corpora.append((Path(p).name, load_corpus(p)))
    except (OSError, ValueError) as e:
        print(f"코퍼스를 읽을 수 없습니다 (cannot read corpus): {safe_error(e)}", file=out)
        return None
    if limit is not None:
        left, cut = max(0, limit), []
        for name, docs in corpora:
            cut.append((name, docs[:left]))
            left -= len(cut[-1][1])
        corpora = cut
    try:
        cfg, det, construct_s = build_detector(config)
    except Exception as e:  # noqa: BLE001 - config / backend problems are reported, not raised
        print(f"설정 또는 탐지기 오류 (config / detector error): {safe_error(e)}", file=out)
        return None
    meta = {
        "config": config or "기본값 (built-in defaults)",
        "backend": backend_summary(cfg),
        "corpora": [{"name": n, "docs": len(d)} for n, d in corpora],
        "construct_s": round(construct_s, 3),
    }
    res = evaluate(corpora, det, cfg, progress=progress)
    print(format_table(res, meta), file=out)
    if verbose:
        print("\n" + format_verbose(res), file=out)
    if json_out:
        try:
            Path(json_out).write_text(json.dumps({"meta": meta, **res}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
            print(f"\n저장 wrote {json_out}", file=out)
        except OSError as e:
            print(f"\nJSON 저장 실패 (cannot write {json_out}): {safe_error(e)}", file=out)
    return {"meta": meta, **res}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="deid 값 단위 누출 점수 (value-level leak score on held-out corpora)")
    ap.add_argument("corpus", nargs="+", help="JSONL corpus file(s), e.g. bench/fresh_a.jsonl")
    ap.add_argument("--config", help="deid TOML config (default: built-in defaults, rules only)")
    ap.add_argument("--json", dest="json_out", metavar="OUT", help="write full results to this file")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list leaked values and spurious regions per document")
    ap.add_argument("--limit", type=int, metavar="N", help="score only the first N documents in total")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 0  # argparse already printed the usage; this tool always exits 0
    progress = sys.stderr if sys.stderr.isatty() else None
    run(args.corpus, config=args.config, limit=args.limit, json_out=args.json_out,
        verbose=args.verbose, progress=progress)
    return 0


if __name__ == "__main__":
    sys.exit(main())
