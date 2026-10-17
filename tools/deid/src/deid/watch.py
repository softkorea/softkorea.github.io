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

Settings follow the config file (:class:`LiveConfig`): each pass checks its modification time,
size and inode and re-reads it when they changed, so names added to ``names`` reach a watcher
that has been running since login. A file that no longer loads keeps the previous settings and
is reported once (log line, and a notification without details with ``notify``).
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


# ---------------------------------------------------------------------- live config


def config_error_text(e: BaseException, path: str | os.PathLike | None = None) -> str:
    """The message of a config loading error (Korean, from deid.config), in full."""
    if isinstance(e, FileNotFoundError):
        shown = path if path is not None else (e.filename or "")
        return f"설정 파일을 찾을 수 없습니다 (config file not found): {shown}"
    text = str(e).strip()
    if not text:
        return type(e).__name__
    if isinstance(e, ValueError):
        return " ".join(text.split())[:2000]
    return f"{text.splitlines()[0][:400]} ({type(e).__name__})"


def config_location(path: str | os.PathLike | None = None) -> tuple[Path, bool]:
    """(file, explicit) for `path` or the default config location, also when it does not exist
    yet (so creating it later is noticed). `explicit` is True for --config and DEID_CONFIG,
    whose file must exist; the default file is optional."""
    if path is not None:
        return Path(path).expanduser(), True
    env = os.environ.get("DEID_CONFIG")
    if env:
        return Path(env).expanduser(), True
    return Path("~/.config/deid/config.toml").expanduser(), False


@dataclass
class ConfigChange:
    """What :meth:`LiveConfig.refresh` found: new settings in use (`ok`) or a file that does
    not load (the previous settings stay in use; `message` says why)."""

    ok: bool
    message: str
    problems: list[str] = field(default_factory=list)   # soft warnings of the new file


class LiveConfig:
    """A Config that follows its file.

    :meth:`refresh` stats the file (modification time, size, inode, change time - an editor
    that saves by renaming counts too) and re-reads it only when that changed. A file that
    no longer loads (syntax error, unknown key, deleted --config file) keeps the previous
    settings; `error` holds the reason until a later version of the file loads. The default
    file (~/.config/deid/config.toml) may also appear or disappear (-> default settings).
    """

    def __init__(self, path: str | os.PathLike | None = None, cfg: "Config | None" = None, *,
                 follow: bool = True) -> None:
        self.path, self.explicit = config_location(path)
        self.follow = follow
        self.error: str | None = None
        self.generation = 0
        self._lock = threading.Lock()
        self._sig = self._signature() if follow else None
        if cfg is None:
            from .config import default_config, load_config

            if not follow:
                cfg = load_config()
            elif self.explicit or self._sig is not None:
                cfg = load_config(self.path)    # raises: a bad file at start is an error
            else:
                cfg = default_config()
        self.cfg = cfg

    @classmethod
    def static(cls, cfg: "Config") -> "LiveConfig":
        """Fixed settings that never reload (a Config handed in by a caller)."""
        return cls(cfg=cfg, follow=False)

    def _signature(self) -> tuple | None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        except OSError as e:   # unreadable: loading will say why
            return ("error", type(e).__name__)
        return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)

    def refresh(self) -> ConfigChange | None:
        """Re-read the file when it changed. None when nothing changed."""
        if not self.follow:
            return None
        with self._lock:
            sig = self._signature()
            if sig == self._sig:
                return None
            self._sig = sig     # taken before reading: a write still going on shows up next time
            from .config import default_config, load_config

            try:
                if sig is None and not self.explicit:
                    cfg = default_config()    # the optional default file was removed
                else:
                    cfg = load_config(self.path)
            except Exception as e:  # noqa: BLE001 - keep working with the previous settings
                self.error = config_error_text(e, self.path)
                return ConfigChange(False, self.error)
            self.cfg = cfg
            self.error = None
            self.generation += 1
            problems: list[str] = []
            if sig is not None:
                try:
                    from . import config as config_mod

                    report = getattr(config_mod, "validate_report", None)
                    problems = list(report(self.path)) if report else []
                except Exception:  # noqa: BLE001 - warnings are a bonus
                    problems = []
            return ConfigChange(True, "설정 파일을 다시 읽었습니다 (config reloaded)"
                                if sig is not None else
                                "설정 파일이 없어져 기본 설정을 씁니다 (config file removed: defaults)",
                                problems)


def _log_config_change(change: ConfigChange, log: "Log", notify: bool) -> None:
    if change.ok:
        log(change.message)
        for p in change.problems:
            log(f"   ! 설정 확인 필요 (config warning): {p}")
        return
    log("! 설정 파일 오류 - 고칠 때까지 이전 설정으로 계속 처리합니다 (config error: keeping the "
        f"previous settings): {change.message}")
    if notify:   # lock screen: no details (the config file holds names)
        from . import mac
        try:
            mac.notify("deid 감시: 설정 파일 오류",
                       "이전 설정으로 계속 처리합니다 - deid doctor로 확인하세요")
        except Exception:  # noqa: BLE001 - a notification never stops the watcher
            pass


# ---------------------------------------------------------------------- LLM server notice


def transport_notice(cfg: "Config | None") -> str | None:
    """Warning (Korean + short English) when ``[ner] backend = "openai"`` (or ``"ollama"`` with
    a remote host) sends the ORIGINAL text to an LLM server outside this computer (e.g. Strata
    on the clinic PC); None when the backend is off or the server is this computer. Never
    raises; used by the CLI (stderr), the watch log and the web page."""
    if cfg is None:
        return None
    try:
        from .backends import openai_compat as oc

        if oc.ner_enabled(cfg):
            return oc.transport_warning(oc.ner_section(cfg), raw_text=True)
        ner = dict(cfg.ner or {})
        if str(ner.get("backend") or "").strip().casefold() in ("ollama", "llm"):
            # [ner] backend = "ollama" with a host outside this computer ([ner] allow_remote)
            from .backends.ollama import DEFAULT_HOST, check_host

            host = str(ner.get("host") or (cfg.llm or {}).get("host") or DEFAULT_HOST)
            return oc.transport_warning({"base_url": check_host(host, allow_remote=True)},
                                        raw_text=True)
        return None
    except Exception:  # noqa: BLE001 - a notice must never stop anything
        return None


def _rules_only(cfg: "Config") -> "Config":
    """`cfg` without an OpenAI-compatible [ner] backend, for log display names: a file name
    must not cost another request (and another timeout) to an LLM server that may just have
    failed. Other settings, including other [ner] backends, are kept as they are."""
    import dataclasses

    try:
        from .backends import openai_compat as oc

        if not oc.ner_enabled(cfg):
            return cfg
    except Exception:  # noqa: BLE001  # pragma: no cover
        return cfg
    return dataclasses.replace(cfg, ner={})


def _llm_failure(e: BaseException) -> bool:
    return type(e).__name__ == "LLMError" and (type(e).__module__ or "").startswith("deid.")


# ---------------------------------------------------------------------- watcher


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
        stem = formats.deid_filename(path.stem, Detector(_rules_only(cfg)), Anonymizer(cfg, vault),
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
        detector = Detector(cfg)
        try:
            report = formats.process_to_dir(
                path, out_dir, detector, Anonymizer(cfg, vault), cfg,
                rel_dir=None if str(rel_dir) in ("", ".") else rel_dir)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the watcher
            shown = _safe_display(path, cfg, vault)
            log(f"FAIL {shown}: {_failure_reason(e, path, in_dir, shown)}")
            if _llm_failure(e):   # on_error = "fail": nothing written for this file
                log("   ! LLM 서버 문제로 이 파일은 결과를 만들지 않았습니다. 서버를 확인(deid doctor)한 뒤 "
                    "파일을 다시 저장하거나 다시 넣으면 처리합니다 (LLM server problem: file not "
                    "processed; save or drop it again to retry)")
            state.files[key] = {"sig": sig_list, "status": FAILED}
            state.dirty = True
            if notify:   # lock screen: no file names at all
                from . import mac
                mac.notify("deid 감시 실패", f"{_kind(path)} 1개를 처리하지 못했습니다 - 로그를 확인하세요")
            continue
        # e.g. the LLM backend fell back to the rules while de-identifying the file name
        drain = getattr(detector, "drain_warnings", None)
        if callable(drain):
            report.warnings.extend(w for w in drain() if w not in report.warnings)
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
            from .backends.openai_compat import FALLBACK_NOTE, is_fallback_warning

            note = (f" - {FALLBACK_NOTE}" if any(is_fallback_warning(w) for w in report.warnings)
                    else "")
            mac.notify("deid 처리 완료", f"{_kind(path)} 1개: {summary}{note} - 결과를 확인하세요")
    for key in [k for k in state.files if k not in seen_keys]:
        del state.files[key]   # removed from IN: forget, so putting it back reprocesses it
        state.dirty = True
    return reports


def run_watch(in_dir: str | os.PathLike, out_dir: str | os.PathLike, *,
              interval: float = DEFAULT_INTERVAL, cfg: "Config | LiveConfig | None" = None,
              notify: bool = False, log: Log | None = None,
              stop: threading.Event | None = None, state_path: str | os.PathLike | None = None,
              max_passes: int | None = None) -> int:
    """Poll forever (until Ctrl-C, `stop` is set or `max_passes`). Returns an exit code.

    `cfg`: a :class:`LiveConfig` (re-read when its file changes; `deid watch` passes one),
    None (the default config file, followed the same way) or a fixed Config.
    """
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
    if isinstance(cfg, LiveConfig):
        live = cfg
    elif cfg is None:
        try:
            live = LiveConfig()
        except Exception as e:  # noqa: BLE001
            log(f"설정 파일 오류 (config error): {config_error_text(e)}")
            return 2
    else:
        live = LiveConfig.static(cfg)
    state = WatchState.load(state_path if state_path is not None
                            else state_path_for(in_dir, out_dir))
    vault = Vault()
    interval = max(0.2, float(interval))
    from . import __version__

    log(f"감시 시작 (watching, deid {__version__}): {in_dir} -> {out_dir}, {interval:g}초 간격. "
        "설정 파일을 고치면 다음 확인 때 다시 읽습니다. 종료: Ctrl-C")
    notice = transport_notice(live.cfg)
    if notice:
        log(f"! {notice}")
    passes = 0
    try:
        while True:
            purge_expired_session()   # the session must not outlive its expiry on disk
            change = live.refresh()   # names added to config.toml apply from this pass on
            if change is not None:
                _log_config_change(change, log, notify)
                if change.ok and transport_notice(live.cfg) != notice:
                    notice = transport_notice(live.cfg)
                    log(f"! {notice}" if notice else
                        "LLM 서버로 원문을 보내지 않습니다 (raw text no longer leaves this computer)")
            try:
                watch_once(state, in_dir, out_dir, live.cfg, vault=vault, log=log, notify=notify)
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


__all__ = ("WatchState", "watch_once", "run_watch", "scan", "file_key", "state_path_for",
           "LiveConfig", "ConfigChange", "config_error_text", "config_location", "transport_notice")
