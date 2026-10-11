"""Folder watcher: de-identify every new or changed file dropped into IN, write it to OUT.

Polling only (no external dependencies). A file is processed once it is stable: the same
size and modification time on two consecutive polls, so half-copied files are not read.
Originals are never moved or deleted. Processed files are remembered in a state file
(``<data dir>/watch/<hash>.json``) keyed by a hash of the relative path, so neither the state
file nor the log contains original file names (they often hold patient names): the log shows
the de-identified output name and counts only, failures show a masked name ("김**_진***.png")
and an error text with every path and name removed, and notifications (lock screen) show
counts only. IN and OUT are created owner-only (0700), and each pass also deletes an expired
clipboard session (see :func:`deid.session.purge_expired_session`).
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
import unicodedata
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
        from .session import make_private_dir

        make_private_dir(self.path.parent)
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
    name = unicodedata.normalize("NFC", name)
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
    """Display name for log lines about a file that produced no output (no raw names).

    Names the vault already knows become their tokens; every other surname-initial word is
    masked (no new tokens are issued for a file that produced nothing).
    """
    from .anonymizer import Anonymizer
    from .engine import Detector

    try:
        stem = formats.deid_filename(path.stem, Detector(cfg), Anonymizer(cfg, vault),
                                     guess_names=False)
    except Exception:  # pragma: no cover - never let logging fail the watcher
        stem = "(file)"
    return _mask_possible_names(stem) + path.suffix


# Short Korean explanations for errors raised by third-party libraries, whose own messages
# often embed the full source path (PIL: "cannot identify image file '/.../김민서.png'").
_FRIENDLY_ERRORS = {
    "UnidentifiedImageError": "이미지 파일을 읽을 수 없습니다: 손상되었거나 이미지가 아닙니다",
    "DecompressionBombError": "이미지가 너무 큽니다",
    "BadZipFile": "파일이 손상되었거나 형식이 다릅니다",
    "PermissionError": "파일을 읽을 권한이 없습니다",
    "FileNotFoundError": "파일이 처리 중에 사라졌습니다 (옮겨졌거나 동기화 중)",
    "IsADirectoryError": "파일이 아니라 폴더입니다",
    "UnicodeDecodeError": "글자 인코딩을 알 수 없습니다",
    "MemoryError": "메모리가 부족합니다",
}


def _own_error(e: BaseException) -> bool:
    """True for errors deid raised itself (its messages never contain file names or
    content); False for errors raised inside other libraries."""
    if (type(e).__module__ or "").split(".")[0] == "deid":
        return True
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    origin = str(tb.tb_frame.f_globals.get("__name__", "")) if tb is not None else ""
    return origin.split(".")[0] == "deid" and isinstance(e, (ValueError, RuntimeError, OSError))


def _scrub(text: str, path: Path, in_dir: Path, shown: str) -> str:
    """Replace every spelling of the file's path, name, stem and folder names in `text`."""
    reps: dict[str, str] = {}
    try:
        rel = path.relative_to(in_dir)
    except ValueError:
        rel = Path(path.name)
    for p in (path, path.absolute(), in_dir / rel):
        for form in (str(p), p.as_posix()):
            reps[form] = shown
    try:
        reps[str(path.resolve())] = shown
    except OSError:  # pragma: no cover
        pass
    reps[path.name] = shown
    if len(path.stem) >= 2:
        reps[path.stem] = shown
    for part in rel.parent.parts:
        if part not in ("", "."):
            reps[part] = _mask_possible_names(part)
    for raw, new in list(reps.items()):
        for norm in ("NFC", "NFD"):
            reps.setdefault(unicodedata.normalize(norm, raw), new)
    for raw in sorted(reps, key=len, reverse=True):
        if raw:
            text = text.replace(raw, reps[raw])
    return text


def _failure_reason(e: BaseException, path: Path, in_dir: Path, shown: str) -> str:
    """One-line reason for a FAIL log line, without the original file name or path."""
    name = type(e).__name__
    text = str(e).strip()
    if _own_error(e) and text:
        msg = text.splitlines()[0][:300]
    elif name in _FRIENDLY_ERRORS:
        msg = f"{_FRIENDLY_ERRORS[name]} ({name})"
    elif isinstance(e, OSError) and e.strerror:
        msg = f"{e.strerror} ({name})"
    else:
        msg = f"처리하지 못했습니다 ({name})"
    return _scrub(msg, path, in_dir, shown)


def _kind(path: str | os.PathLike) -> str:
    ext = Path(path).suffix.lstrip(".").upper()
    return f"{ext} 파일" if ext else "파일"


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
                if notify:
                    from . import mac
                    mac.notify("deid 감시 실패", f"{_kind(path)} 1개를 처리하지 못했습니다 - 로그를 확인하세요")
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
            shown = _safe_display(path, cfg, vault)
            log(f"FAIL {shown}: {_failure_reason(e, path, in_dir, shown)}")
            state.files[key] = {"sig": sig_list, "status": FAILED}
            state.dirty = True
            if notify:   # lock screen: no file names at all
                from . import mac
                mac.notify("deid 감시 실패", f"{_kind(path)} 1개를 처리하지 못했습니다 - 로그를 확인하세요")
            continue
        # The output name is de-identified (formats.deid_filename) and is what OUT shows anyway.
        out_rel = Path(report.output).relative_to(out_dir).as_posix()
        summary = formats.format_counts(report, cfg)
        log(f"OK {out_rel} ({summary})")
        for w in report.warnings:
            log(f"   ! {w}")
        state.files[key] = {"sig": sig_list, "status": DONE, "out": out_rel}
        state.dirty = True
        reports.append(report)
        if notify:   # lock screen: counts only, like `deid file --notify`
            from . import mac
            mac.notify("deid 처리 완료", f"{_kind(path)} 1개: {summary} - 결과를 확인하세요")
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
    from .session import prepare_work_dir, purge_expired_session
    from .vault import Vault

    in_dir, out_dir = Path(in_dir).expanduser(), Path(out_dir).expanduser()
    log = log or _default_log
    if in_dir.exists() and not in_dir.is_dir():
        log("입력 경로가 폴더가 아닙니다 (IN must be a folder)")
        return 2
    try:   # owner-only: originals moved here must not be readable by other accounts
        prepare_work_dir(in_dir)
        prepare_work_dir(out_dir)
    except OSError as e:
        log(f"폴더를 만들 수 없습니다 (cannot create folder): {type(e).__name__}")
        return 2
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
            purge_expired_session()   # the session must not outlive its expiry on disk
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
