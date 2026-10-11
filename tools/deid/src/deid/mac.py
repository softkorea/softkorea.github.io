"""Thin wrappers around macOS command-line tools used by the CLI.

pbpaste / pbcopy (clipboard text), osascript (clipboard images, notifications),
screencapture (screenshots), security (Keychain) and launchctl (the watch agent).

Every subprocess call goes through :func:`run`, so tests can monkeypatch one function. All
calls run with ``LANG=en_US.UTF-8`` so Korean text survives pbcopy/pbpaste (launchd, Shortcuts
and Automator start programs without a UTF-8 locale). Functions that need macOS raise
:class:`MacError` with a friendly Korean + English message elsewhere.

Dynamic values (paths, notification text) are passed to AppleScript as ``argv`` items, never
pasted into the script source, so quotes in a file name cannot change the script.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Sequence

UTF8_ENV = {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8"}
WATCH_LABEL = "com.softkorea.deid.watch"
# launchd starts agents with a minimal PATH; Homebrew (tesseract, ollama) lives here.
AGENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
IMAGE_CLASSES = ("PNGf", "TIFF", "JPEG", "GIFf", "jp2 ")


class MacError(RuntimeError):
    """A macOS tool is unavailable or failed. Messages never contain clipboard contents."""


def is_macos() -> bool:
    return sys.platform == "darwin"


def require_macos(feature: str = "이 기능") -> None:
    if not is_macos():
        raise MacError(f"{feature}은(는) macOS에서만 동작합니다 ({feature}: macOS only). "
                       "다른 환경에서는 'deid text' 또는 'deid file'을 사용하세요.")


def run(args: Sequence[str], *, input: bytes | None = None, timeout: float | None = 30,
        ) -> subprocess.CompletedProcess:
    """Run a command with a UTF-8 locale and captured output (the single subprocess helper)."""
    env = dict(os.environ)
    env.update(UTF8_ENV)
    try:
        return subprocess.run(list(args), input=input, capture_output=True, timeout=timeout,
                              env=env, check=False)
    except FileNotFoundError as e:
        raise MacError(f"'{args[0]}' 명령을 찾을 수 없습니다 (command not found: {args[0]})") from e
    except subprocess.TimeoutExpired as e:
        raise MacError(f"'{args[0]}' 명령이 응답하지 않습니다 (timed out: {args[0]})") from e


def _err(proc: subprocess.CompletedProcess) -> str:
    raw = proc.stderr or b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return text.strip().splitlines()[-1][:200] if text.strip() else f"exit {proc.returncode}"


def _osascript(lines: Sequence[str], *argv: str, timeout: float | None = 30
               ) -> subprocess.CompletedProcess:
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    return run(args + [str(a) for a in argv], timeout=timeout)


# ---------------------------------------------------------------------- clipboard text


def paste_text() -> str:
    """Current clipboard as plain text ("" when it holds no text)."""
    require_macos("클립보드 읽기")
    proc = run(["pbpaste", "-Prefer", "txt"], timeout=15)
    if proc.returncode != 0:
        raise MacError(f"클립보드를 읽을 수 없습니다 (pbpaste failed: {_err(proc)})")
    out = proc.stdout or b""
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)


def copy_text(text: str) -> None:
    """Replace the whole clipboard (all flavors: RTF, HTML, images) with plain `text`."""
    require_macos("클립보드 쓰기")
    proc = run(["pbcopy"], input=text.encode("utf-8"), timeout=15)
    if proc.returncode != 0:
        raise MacError(f"클립보드에 쓸 수 없습니다 (pbcopy failed: {_err(proc)})")


def clear_clipboard() -> None:
    """Best effort: empty the clipboard so un-processed data cannot be pasted by mistake."""
    try:
        copy_text("")
    except MacError:
        pass


# ---------------------------------------------------------------------- clipboard images


def clipboard_info() -> str:
    """AppleScript `clipboard info`, e.g. '«class PNGf», 1234, TIFF picture, 5678'."""
    require_macos("클립보드 읽기")
    proc = _osascript(["clipboard info"], timeout=15)
    if proc.returncode != 0:
        return ""
    out = proc.stdout or b""
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)


def info_has_image(info: str) -> bool:
    return any(c in info for c in IMAGE_CLASSES) or "picture" in info.lower()


def info_has_files(info: str) -> bool:
    return "furl" in info


def clipboard_has_image() -> bool:
    return info_has_image(clipboard_info())


_GET_IMAGE = (
    "on run argv",
    "set png to (the clipboard as «class PNGf»)",
    "set fh to open for access (POSIX file (item 1 of argv)) with write permission",
    "try",
    "set eof fh to 0",
    "write png to fh",
    "on error errMsg",
    "close access fh",
    "error errMsg",
    "end try",
    "close access fh",
    "end run",
)
_SET_IMAGE = (
    "on run argv",
    "set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)",
    "end run",
)


def get_clipboard_image(path: str | os.PathLike) -> bool:
    """Write the clipboard image as PNG to `path`; False if the clipboard holds no image."""
    require_macos("클립보드 이미지")
    path = Path(path)
    proc = _osascript(_GET_IMAGE, str(path), timeout=30)
    return proc.returncode == 0 and path.is_file() and path.stat().st_size > 0


def set_clipboard_image(path: str | os.PathLike) -> None:
    """Put a PNG file on the clipboard (replacing everything else on it)."""
    require_macos("클립보드 이미지")
    proc = _osascript(_SET_IMAGE, str(Path(path)), timeout=30)
    if proc.returncode != 0:
        raise MacError(f"이미지를 클립보드에 넣을 수 없습니다 (set clipboard image failed: {_err(proc)})")


# ---------------------------------------------------------------------- screenshots

SHOT_MODES = ("selection", "window", "full")


def screencapture_args(path: str | os.PathLike, mode: str = "selection") -> list[str]:
    if mode == "selection":
        return ["screencapture", "-i", "-x", str(path)]
    if mode == "window":   # -o: no window shadow
        return ["screencapture", "-i", "-W", "-o", "-x", str(path)]
    if mode == "full":
        return ["screencapture", "-x", str(path)]
    raise ValueError(f"unknown screenshot mode {mode!r}; use one of {SHOT_MODES}")


def screencapture(path: str | os.PathLike, mode: str = "selection") -> bool:
    """Take a screenshot into `path`. False when the user cancelled (Esc) or nothing was saved."""
    require_macos("화면 캡처")
    args = screencapture_args(path, mode)
    proc = run(args, timeout=None)   # interactive: the user may take their time
    p = Path(path)
    return proc.returncode == 0 and p.is_file() and p.stat().st_size > 0


# ---------------------------------------------------------------------- Keychain


def keychain_get(account: str, service: str) -> str | None:
    """Generic password from the login Keychain, or None if there is none."""
    require_macos("키체인")
    proc = run(["security", "find-generic-password", "-a", account, "-s", service, "-w"],
               timeout=15)
    if proc.returncode != 0:
        return None
    out = proc.stdout or b""
    value = (out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)).strip()
    return value or None


def keychain_set(account: str, service: str, secret: str) -> bool:
    """Create or update (-U) a generic password. False if the Keychain refused."""
    require_macos("키체인")
    proc = run(["security", "add-generic-password", "-a", account, "-s", service, "-w", secret,
                "-U"], timeout=15)
    return proc.returncode == 0


def keychain_delete(account: str, service: str) -> bool:
    require_macos("키체인")
    proc = run(["security", "delete-generic-password", "-a", account, "-s", service], timeout=15)
    return proc.returncode == 0


# ---------------------------------------------------------------------- notifications

_NOTIFY = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)


def notify(title: str, message: str) -> bool:
    """Show a macOS notification. Never raises; False when it could not be shown.

    Callers must pass counts / de-identified names only, never raw clipboard text.
    """
    if not is_macos():
        return False
    try:
        proc = _osascript(_NOTIFY, title[:100], message[:300], timeout=10)
    except MacError:
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------- launchd watch agent


def launch_agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def watch_agent_path(label: str = WATCH_LABEL) -> Path:
    return launch_agents_dir() / f"{label}.plist"


def default_watch_log() -> Path:
    return Path("~/Library/Logs/deid-watch.log").expanduser()


def watch_agent_plist(deid_path: str | Sequence[str], in_dir: str | os.PathLike,
                      out_dir: str | os.PathLike, *, label: str = WATCH_LABEL,
                      log_path: str | os.PathLike | None = None, notify: bool = False,
                      interval: float | None = None) -> bytes:
    """launchd agent plist (XML bytes) running `deid watch IN OUT` at login, kept alive.

    `deid_path` is the deid executable, or a full command prefix such as
    [sys.executable, "-m", "deid"].
    """
    prefix = [deid_path] if isinstance(deid_path, (str, os.PathLike)) else list(deid_path)
    args = [str(p) for p in prefix] + ["watch", str(in_dir), str(out_dir)]
    if interval is not None:
        args += ["--interval", f"{interval:g}"]
    if notify:
        args.append("--notify")
    log = str(log_path) if log_path is not None else str(default_watch_log())
    plist = {
        "Label": label,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PATH": AGENT_PATH,
        },
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False)


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def install_watch_agent(plist: bytes, label: str = WATCH_LABEL) -> Path:
    """Write the agent plist to ~/Library/LaunchAgents and (re)load it with launchctl."""
    require_macos("자동 감시 설치")
    path = watch_agent_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    default_watch_log().parent.mkdir(parents=True, exist_ok=True)
    run(["launchctl", "bootout", f"{_gui_domain()}/{label}"], timeout=30)  # ignore: not loaded
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(plist)
    os.replace(tmp, path)
    proc = run(["launchctl", "bootstrap", _gui_domain(), str(path)], timeout=30)
    if proc.returncode != 0:
        raise MacError(f"launchctl bootstrap 실패 (failed): {_err(proc)}")
    return path


def uninstall_watch_agent(label: str = WATCH_LABEL) -> bool:
    """Stop and remove the agent. True if a plist was removed."""
    require_macos("자동 감시 제거")
    path = watch_agent_path(label)
    run(["launchctl", "bootout", f"{_gui_domain()}/{label}"], timeout=30)
    if path.exists():
        path.unlink()
        return True
    return False


__all__ = (
    "MacError", "is_macos", "require_macos", "run", "paste_text", "copy_text", "clear_clipboard",
    "clipboard_info", "info_has_image", "info_has_files", "clipboard_has_image",
    "get_clipboard_image", "set_clipboard_image", "screencapture", "screencapture_args",
    "keychain_get", "keychain_set", "keychain_delete", "notify", "watch_agent_plist",
    "watch_agent_path", "install_watch_agent", "uninstall_watch_agent", "WATCH_LABEL",
)
