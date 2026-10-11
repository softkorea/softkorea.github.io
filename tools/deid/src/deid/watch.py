"""Folder watcher: de-identify every new or changed file dropped into IN, write it to OUT.

Polling only (no external dependencies). A file is processed once it is stable: the same
size and modification time on two consecutive polls, so half-copied files are not read.
Originals are never moved or deleted. Processed files are remembered in a state file
(``<data dir>/watch/<hash>.json``) keyed by a hash of the relative path, so neither the state
file nor the log contains original file names (they often hold patient names): the log shows
the de-identified output name and counts only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import formats
from .types import Report

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .vault import Vault

STATE_VERSION = 1
DEFAULT_INTERVAL = 2.0

PENDING, DONE, FAILED, SKIPPED = "pending", "done", "failed", "skipped"

Log = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def file_key(rel: str) -> str:
    """Opaque key for a relative path (the state file never stores file names)."""
    return hashlib.sha256(rel.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def state_path_for(in_dir: str | os.PathLike, out_dir: str | os.PathLike) -> Path:
    from .session import data_dir

    ident = f"{Path(in_dir).resolve()}\0{Path(out_dir).resolve()}"
    return data_dir() / "watch" / (hashlib.sha256(ident.encode("utf-8", "surrogatepass"))
                                   .hexdigest()[:16] + ".json")


@dataclass
class WatchState:
    """key -> {"sig": [mtime_ns, size], "status": pending|done|failed|skipped, "out": rel}."""

    files: dict[str, dict] = field(default_factory=dict)
    path: Path | None = None
    dirty: bool = False

    @classmethod
    def load(cls, path: str | os.PathLike | None) -> "WatchState":
        if path is None:
            return cls()
        p = Path(path)
        try:
            data = json.loads(p.read_text("utf-8"))
            if data.get("v") != STATE_VERSION or not isinstance(data.get("files"), dict):
                raise ValueError("bad state")
            files = {str(k): dict(v) for k, v in data["files"].items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError, AttributeError):
            files = {}
        return cls(files=files, path=p)

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps({"v": STATE_VERSION, "files": self.files}), "utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


def scan(in_dir: Path, out_dir: Path) -> dict[str, tuple[Path, tuple[int, int]]]:
    """rel path -> (path, (mtime_ns, size)) of candidate files (skips OUT if inside IN)."""
    found: dict[str, tuple[Path, tuple[int, int]]] = {}
    for path in formats.iter_files(in_dir, skip_dirs=[out_dir]):
        try:
            st = path.stat()
        except OSError:
            continue  # vanished between listing and stat
        if not path.is_file():
            continue
        rel = path.relative_to(in_dir).as_posix()
        found[rel] = (path, (st.st_mtime_ns, st.st_size))
    return found


_HANGUL_RUN = re.compile(r"(?<![가-힣])[가-힣]{2,4}(?![가-힣])")


def _mask_possible_names(name: str) -> str:
    """Mask every 2-4 syllable Hangul word starting with a Korean surname ("홍길동" -> "홍**").

    Used for log lines about files that produced no output: a bare file name like
    "홍길동.hwp" has no context for the detector, and over-masking a log line is harmless.
    """
    try:
        from .recognizers.names import STOPWORDS, SURNAMES
    except ImportError:  # pragma: no cover
        return _HANGUL_RUN.sub(lambda m: m.group()[0] + "*" * (len(m.group()) - 1), name)

    def mask(m: re.Match) -> str:
        word = m.group()
        if word in STOPWORDS or not (word[0] in SURNAMES or word[:2] in SURNAMES):
            return word
        return word[0] + "*" * (len(word) - 1)

    return _HANGUL_RUN.sub(mask, name)


def _safe_display(path: Path, cfg: "Config", vault: "Vault | None" = None) -> str:
    """Display name for log lines about a file that produced no output (no raw names)."""
    from .anonymizer import Anonymizer
    from .engine import Detector

    try:
        stem = formats.deid_filename(path.stem, Detector(cfg), Anonymizer(cfg, vault))
    except Exception:  # pragma: no cover - never let logging fail the watcher
        stem = "(file)"
    return _mask_possible_names(stem) + path.suffix


def watch_once(state: WatchState, in_dir: str | os.PathLike, out_dir: str | os.PathLike,
               cfg: "Config | None" = None, *, vault: "Vault | None" = None,
               log: Log | None = None, notify: bool = False) -> list[Report]:
    """One polling pass. Returns reports of the files processed in this pass.

    New or changed files are first marked pending; a pending file whose (mtime, size) is
    unchanged on the next pass is processed into OUT (same relative folder, de-identified
    names). Failures are logged and not retried until the file changes.
    """
    from .anonymizer import Anonymizer
    from .config import load_config
    from .engine import Detector
    from .vault import Vault

    in_dir, out_dir = Path(in_dir), Path(out_dir)
    cfg = cfg if cfg is not None else load_config()
    vault = vault if vault is not None else Vault()
    log = log or _default_log
    reports: list[Report] = []
    current = scan(in_dir, out_dir)
    seen_keys = set()
    for rel, (path, sig) in current.items():
        key = file_key(rel)
        seen_keys.add(key)
        entry = state.files.get(key)
        sig_list = list(sig)
        if entry is not None and entry.get("sig") == sig_list and entry.get("status") != PENDING:
            continue  # already handled this version
        if entry is None or entry.get("sig") != sig_list:
            state.files[key] = {"sig": sig_list, "status": PENDING}
            state.dirty = True
            continue  # wait one more poll: the file may still be copying
        # stable pending file
        if not formats.is_supported(path):
            if formats.is_legacy(path):
                log(f"FAIL {_safe_display(path, cfg, vault)}: {formats.unsupported_message(path)}")
                state.files[key] = {"sig": sig_list, "status": FAILED}
            else:
                state.files[key] = {"sig": sig_list, "status": SKIPPED}
            state.dirty = True
            continue
        rel_dir = Path(rel).parent
        try:
            report = formats.process_to_dir(
                path, out_dir, Detector(cfg), Anonymizer(cfg, vault), cfg,
                rel_dir=None if str(rel_dir) in ("", ".") else rel_dir)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the watcher
            msg = str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__
            log(f"FAIL {_safe_display(path, cfg, vault)}: {msg}")
            state.files[key] = {"sig": sig_list, "status": FAILED}
            state.dirty = True
            if notify:
                from . import mac
                mac.notify("deid 감시 실패", f"{_safe_display(path, cfg, vault)} 처리 실패 - 로그를 확인하세요")
            continue
        out_rel = Path(report.output).relative_to(out_dir).as_posix()
        summary = formats.format_counts(report, cfg)
        log(f"OK {out_rel} ({summary})")
        for w in report.warnings:
            log(f"   ! {w}")
        state.files[key] = {"sig": sig_list, "status": DONE, "out": out_rel}
        state.dirty = True
        reports.append(report)
        if notify:
            from . import mac
            mac.notify("deid 처리 완료", f"{Path(out_rel).name} ({summary})")
    for key in [k for k in state.files if k not in seen_keys]:
        del state.files[key]   # removed from IN: forget, so putting it back reprocesses it
        state.dirty = True
    return reports


def run_watch(in_dir: str | os.PathLike, out_dir: str | os.PathLike, *,
              interval: float = DEFAULT_INTERVAL, cfg: "Config | None" = None,
              notify: bool = False, log: Log | None = None,
              stop: threading.Event | None = None, state_path: str | os.PathLike | None = None,
              max_passes: int | None = None) -> int:
    """Poll forever (until Ctrl-C, `stop` is set or `max_passes`). Returns an exit code."""
    from .config import load_config
    from .vault import Vault

    in_dir, out_dir = Path(in_dir).expanduser(), Path(out_dir).expanduser()
    log = log or _default_log
    if in_dir.exists() and not in_dir.is_dir():
        log("입력 경로가 폴더가 아닙니다 (IN must be a folder)")
        return 2
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if in_dir.resolve() == out_dir.resolve():
            log("입력 폴더와 출력 폴더는 달라야 합니다 (IN and OUT must differ)")
            return 2
    except OSError:  # pragma: no cover
        pass
    cfg = cfg if cfg is not None else load_config()
    state = WatchState.load(state_path if state_path is not None
                            else state_path_for(in_dir, out_dir))
    vault = Vault()
    interval = max(0.2, float(interval))
    log(f"감시 시작 (watching): {in_dir} -> {out_dir}, {interval:g}초 간격. 종료: Ctrl-C")
    passes = 0
    try:
        while True:
            try:
                watch_once(state, in_dir, out_dir, cfg, vault=vault, log=log, notify=notify)
                state.save()
            except OSError as e:  # e.g. IN temporarily unavailable (network/sync folder)
                log(f"폴더를 읽을 수 없습니다 (cannot read folder): {type(e).__name__}")
            passes += 1
            if max_passes is not None and passes >= max_passes:
                break
            if stop is not None:
                if stop.wait(interval):
                    break
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        log("감시를 종료합니다 (stopped)")
    finally:
        try:
            state.save()
        except OSError:  # pragma: no cover
            pass
        sys.stdout.flush()
    return 0


__all__ = ("WatchState", "watch_once", "run_watch", "scan", "file_key", "state_path_for")
