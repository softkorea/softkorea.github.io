"""Clipboard session vault: `deid clip` / `deid text` / web UI, then `deid restore`.

The pseudonym map ([이름1] -> 홍길동) must survive between commands so an LLM answer can be
re-identified locally, but the user should not have to type a passphrase for every paste.

* Vault file: ``~/Library/Application Support/deid/session.vault`` on macOS,
  ``$XDG_DATA_HOME/deid`` or ``~/.local/share/deid/session.vault`` elsewhere; encrypted by
  :class:`deid.vault.Vault` (scrypt + Fernet, mode 0600).
* Passphrase: a random 32-byte key, kept in the macOS login Keychain
  (``security ... -a deid -s deid-session``) when available, otherwise in a 0600 key file
  ``~/.config/deid/session.key``.
* Expiry: the session starts fresh once it is older than ``DEID_SESSION_HOURS`` (default 12)
  hours, so old patients' names do not pile up.
* ``DEID_HOME`` puts every session file (vault, key, lock, watch state) in that directory and
  disables the Keychain (unless ``DEID_KEYCHAIN=1``) - used by tests.

:func:`open_session` wraps load -> use -> save in an inter-process file lock, so a running web
UI and a `deid clip` cannot hand out the same token for two different people.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from . import mac
from .vault import Vault, VaultError

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .engine import Detector

KEYCHAIN_ACCOUNT = "deid"
KEYCHAIN_SERVICE = "deid-session"
DEFAULT_HOURS = 12.0
LOCK_TIMEOUT = 120.0
KEY_BYTES = 32

_THREAD_LOCK = threading.RLock()


class SessionError(RuntimeError):
    """The session vault could not be locked or written."""


# ---------------------------------------------------------------------- paths


def deid_home() -> Path | None:
    home = os.environ.get("DEID_HOME")
    return Path(home).expanduser() if home else None


def data_dir() -> Path:
    """Where the session vault and watch state live."""
    home = deid_home()
    if home is not None:
        return home
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/deid").expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg).expanduser() if xdg else Path("~/.local/share").expanduser()) / "deid"


def config_dir() -> Path:
    home = deid_home()
    if home is not None:
        return home
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path("~/.config").expanduser()) / "deid"


def user_dir() -> Path:
    """The user's working folder (~/Deid: in/, out/, screenshots). Override: DEID_DIR."""
    env = os.environ.get("DEID_DIR")
    return Path(env).expanduser() if env else Path("~/Deid").expanduser()


def vault_file() -> Path:
    return data_dir() / "session.vault"


def key_file() -> Path:
    return config_dir() / "session.key"


def meta_file() -> Path:
    return data_dir() / "session.json"


def lock_file() -> Path:
    return data_dir() / "session.lock"


def session_hours() -> float:
    raw = os.environ.get("DEID_SESSION_HOURS")
    try:
        hours = float(raw) if raw else DEFAULT_HOURS
    except ValueError:
        return DEFAULT_HOURS
    return hours if hours > 0 else DEFAULT_HOURS


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------- key storage


def _use_keychain() -> bool:
    flag = os.environ.get("DEID_KEYCHAIN")
    if flag is not None:
        return flag == "1" and mac.is_macos()
    if deid_home() is not None:
        return False
    return mac.is_macos() and shutil.which("security") is not None


def _read_key_file() -> str | None:
    path = key_file()
    try:
        st = path.stat()
        if st.st_mode & 0o077:
            os.chmod(path, 0o600)   # someone loosened it: tighten again
        key = path.read_text("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key or None


def _write_key_file(key: str) -> None:
    path = key_file()
    _mkdir_private(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def session_key(create: bool = False) -> str | None:
    """The session passphrase (Keychain first, then the key file); None if there is none."""
    if _use_keychain():
        try:
            key = mac.keychain_get(KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE)
        except mac.MacError:
            key = None
        if key:
            return key
    key = _read_key_file()
    if key or not create:
        return key
    key = secrets.token_hex(KEY_BYTES)
    if _use_keychain():
        try:
            if mac.keychain_set(KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, key):
                return key
        except mac.MacError:
            pass
    _write_key_file(key)
    return key


def key_location() -> str:
    """Human description of where the key is kept (for `deid doctor`)."""
    return "macOS 키체인 (Keychain)" if _use_keychain() else str(key_file())


# ---------------------------------------------------------------------- vault


def _created() -> float | None:
    try:
        data = json.loads(meta_file().read_text("utf-8"))
        return float(data["created"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def session_age_hours() -> float | None:
    created = _created()
    return None if created is None else max(0.0, (time.time() - created) / 3600.0)


def _expired() -> bool:
    age = session_age_hours()
    return age is None or age >= session_hours()


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _wipe_vault() -> None:
    _unlink(vault_file())
    _unlink(meta_file())


def load_session_vault() -> Vault:
    """The current session's vault; a new empty one when none exists, it expired, or the key
    no longer opens it (the stale file is deleted)."""
    with _THREAD_LOCK:
        if not vault_file().exists():
            _unlink(meta_file())
            return Vault()
        if _expired():
            _wipe_vault()
            return Vault()
        key = session_key(create=False)
        if not key:
            _wipe_vault()
            return Vault()
        try:
            return Vault.load(vault_file(), key)
        except (VaultError, OSError):
            _wipe_vault()
            return Vault()


def save_session_vault(v: Vault) -> None:
    """Encrypt `v` to the session file (creates the key and starts the expiry clock if new)."""
    with _THREAD_LOCK:
        key = session_key(create=True)
        if not key:  # pragma: no cover - session_key(create=True) always returns a key
            raise SessionError("세션 키를 만들 수 없습니다 (cannot create session key)")
        _mkdir_private(data_dir())
        if not vault_file().exists() or _created() is None:
            meta = meta_file()
            tmp = meta.with_name(f".{meta.name}.{secrets.token_hex(4)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"created": time.time()}, f)
            os.replace(tmp, meta)
        v.save(vault_file(), key)


def forget_session() -> bool:
    """Delete the session vault, its metadata and key (file and Keychain). True if anything
    was removed."""
    with _THREAD_LOCK:
        removed = _unlink(vault_file())
        removed = _unlink(meta_file()) or removed
        removed = _unlink(key_file()) or removed
        if _use_keychain():
            try:
                removed = mac.keychain_delete(KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE) or removed
            except mac.MacError:
                pass
        return removed


# ---------------------------------------------------------------------- locking


@contextmanager
def _file_lock(timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows: in-process lock only
        yield
        return
    _mkdir_private(data_dir())
    fd = os.open(lock_file(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SessionError("다른 deid 작업이 끝나지 않았습니다. 잠시 후 다시 시도하세요 "
                                       "(another deid command is still using the session)")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def open_session(save: bool = True) -> Iterator[Vault]:
    """Locked load -> yield vault -> save if new tokens were issued."""
    with _THREAD_LOCK, _file_lock():
        v = load_session_vault()
        before = len(v)
        yield v
        if save and len(v) != before:
            save_session_vault(v)


# ---------------------------------------------------------------------- helpers

_TOKEN = re.compile(r"\[([^\[\]\d]{1,20})(\d{1,6})\]")


def known_values(vault: Vault, cfg: "Config") -> list[tuple[str, str]]:
    """(entity, original) pairs of a vault, for Detector.add_known.

    Tokens are "[{label}{n}]"; the label is mapped back to an entity with cfg.labels.
    """
    from .engine import PROPAGATE_ENTITIES

    by_label: dict[str, str] = {}
    for ent in sorted(PROPAGATE_ENTITIES):
        by_label.setdefault(cfg.label_for(ent), ent)
    out = []
    for token, original in vault.tokens.items():
        m = _TOKEN.fullmatch(token)
        ent = by_label.get(m.group(1)) if m else None
        if ent:
            out.append((ent, original))
    return out


def seed_detector(detector: "Detector", vault: Vault) -> int:
    """Register the vault's names/IDs as known values, so a name seen in an earlier paste
    (with context, "환자 홍길동") is also caught later without context. Returns the count."""
    pairs = known_values(vault, detector.cfg)
    for ent, value in pairs:
        detector.add_known(ent, value)
    return len(pairs)


def unknown_tokens(text: str, vault: Vault, cfg: "Config") -> int:
    """Number of pseudonym-looking tokens in `text` that this vault cannot restore."""
    labels = set(cfg.labels.values())
    return sum(1 for m in _TOKEN.finditer(text)
               if m.group(1) in labels and m.group(0) not in vault.tokens)


def count_tokens(text: str, vault: Vault) -> int:
    """How many known tokens occur in `text`."""
    return sum(text.count(t) for t in vault.tokens)


__all__ = (
    "load_session_vault", "save_session_vault", "forget_session", "open_session",
    "data_dir", "config_dir", "user_dir", "vault_file", "key_file", "session_key",
    "session_hours", "seed_detector", "known_values", "unknown_tokens", "count_tokens",
    "SessionError",
)
