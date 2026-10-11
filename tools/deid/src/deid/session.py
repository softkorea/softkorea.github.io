"""Clipboard session vault: `deid clip` / `deid text` / web UI, then `deid restore`.

The pseudonym map ([이름1] -> 홍길동) must survive between commands so an LLM answer can be
re-identified locally, but the user should not have to type a passphrase for every paste.

* Vault file: ``~/Library/Application Support/deid/session.vault`` on macOS,
  ``$XDG_DATA_HOME/deid`` or ``~/.local/share/deid/session.vault`` elsewhere; encrypted by
  :class:`deid.vault.Vault` (scrypt + Fernet, mode 0600).
* Passphrase: a random 32-byte key, kept in the macOS login Keychain
  (``security ... -a deid -s deid-session``) when available, otherwise in a 0600 key file
  ``~/.config/deid/session.key``.
* Expiry: the session starts fresh once it has not been used for ``DEID_SESSION_HOURS``
  (default 12) hours - counted from the LAST use (every load or save of the vault updates
  ``last_used`` in ``session.json``), so a conversation that is still going on keeps its
  tokens while an idle session's names do not pile up. An expired session is deleted
  together with its key (:func:`purge_expired_session`) at the next run of any deid command,
  on every pass of the folder watcher and periodically by ``deid serve`` - not only when
  the next clip needs it. Checking for expiry never counts as use.
* A Keychain that cannot be read (locked, prompt cancelled, timeout) is an error, never "no
  key": the vault is kept and :class:`SessionError` explains what to do, so tokens already
  pasted into an LLM chat are never re-issued for a different person.
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
    make_private_dir(path, tighten=True)


def make_private_dir(path: str | os.PathLike, *, tighten: bool = False) -> Path:
    """Create `path` and every missing parent as an owner-only (0700) folder.

    ``mkdir`` alone gives 0755 under the usual umask, and other local accounts can enter a
    macOS home folder, so originals moved into ~/Deid/in would be readable by them. Folders
    that already exist keep their permissions unless `tighten` is True.
    """
    path = Path(path).expanduser()
    missing = []
    p = path
    while not p.exists() and p.parent != p:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            continue
        try:
            os.chmod(d, 0o700)   # mkdir's mode is filtered by the umask
        except OSError:  # pragma: no cover
            pass
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)   # raises the usual error (e.g. a file there)
    if tighten:
        try:
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o700)
        except OSError:  # pragma: no cover - not our folder
            pass
    return path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def prepare_work_dir(path: str | os.PathLike) -> Path:
    """Create a folder deid writes patient files to (watch IN/OUT, screenshots) as 0700.

    Inside the deid work folder (``user_dir()``, ~/Deid) existing folders are tightened too,
    from ~/Deid down to `path`; elsewhere only the folders created here are made private (a
    folder the user chose keeps the permissions they gave it).
    """
    path = Path(path).expanduser().absolute()
    root = user_dir().absolute()
    try:   # DEID_DIR=~ must never chmod the home folder itself
        shared = root.resolve() in (Path.home().resolve(), Path(root.anchor))
    except (OSError, RuntimeError):  # pragma: no cover
        shared = True
    if shared or not _inside(path, root):
        return make_private_dir(path)
    make_private_dir(root, tighten=True)
    d = root
    for part in path.resolve().relative_to(root.resolve()).parts:
        d = d / part
        make_private_dir(d, tighten=True)
    return path


def loose_permissions(path: str | os.PathLike) -> bool:
    """True when an existing folder or file is accessible to the group or other accounts."""
    try:
        return os.name == "posix" and bool(os.stat(path).st_mode & 0o077)
    except OSError:
        return False


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


KEYCHAIN_ERROR = ("macOS 키체인을 열 수 없어 세션 보관함을 읽지 못했습니다 (잠김, 암호 창 취소, 응답 없음). "
                  "로그인 키체인의 잠금을 풀고 다시 시도하세요. 보관함은 지우지 않았습니다. 계속 안 되면 "
                  "'deid forget'으로 세션을 새로 시작할 수 있습니다 (the login Keychain is locked or did "
                  "not answer; the session vault was kept)")


def _keychain_key() -> tuple[str | None, bool]:
    """(key, readable): the Keychain key or None; readable is False when the Keychain could
    not be asked (locked, cancelled, timed out) - unlike a missing item, that says nothing
    about whether a key exists."""
    if not _use_keychain():
        return None, True
    try:
        return mac.keychain_get(KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE), True
    except mac.MacError:
        return None, False


def _session_keys() -> tuple[list[str], bool]:
    """Every key that may open the vault (Keychain, key file) and whether the Keychain was
    readable."""
    kc, readable = _keychain_key()
    keys = [k for k in (kc, _read_key_file()) if k]
    return list(dict.fromkeys(keys)), readable


def keychain_problem() -> str | None:
    """A message when the Keychain holding the session key cannot be read right now."""
    return None if _keychain_key()[1] else KEYCHAIN_ERROR


def session_key(create: bool = False) -> str | None:
    """The session passphrase (Keychain first, then the key file); None if there is none.

    With `create`, a missing key is created - but not while the Keychain is unreadable and a
    vault exists (its key may be in the locked Keychain): that raises SessionError.
    """
    key, readable = _keychain_key()
    if key:
        return key
    key = _read_key_file()
    if key or not create:
        return key
    if not readable and vault_file().exists():
        raise SessionError(KEYCHAIN_ERROR)
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


def _meta() -> tuple[float, float] | None:
    """(created, last_used) from session.json; None when missing or unreadable. Files
    written by deid 0.1 have no last_used: their creation time counts."""
    try:
        data = json.loads(meta_file().read_text("utf-8"))
        created = float(data["created"])
        last = float(data.get("last_used", created))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return created, last


def _created() -> float | None:
    meta = _meta()
    return None if meta is None else meta[0]


def _hours_since(t: float | None) -> float | None:
    return None if t is None else max(0.0, (time.time() - t) / 3600.0)


def session_age_hours() -> float | None:
    """Hours since the session was started (first token issued)."""
    return _hours_since(_created())


def session_idle_hours() -> float | None:
    """Hours since the session was last used (loaded or saved): what expiry counts."""
    meta = _meta()
    return _hours_since(None if meta is None else meta[1])


def _expired() -> bool:
    meta = _meta()
    if meta is None:
        return True
    idle = (time.time() - meta[1]) / 3600.0
    # A last use more than an hour in the future (clock moved back, edited file) must not
    # keep the session alive indefinitely.
    return idle >= session_hours() or idle < -1.0


def _write_meta(created: float, last_used: float) -> None:
    meta = meta_file()
    tmp = meta.with_name(f".{meta.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"created": created, "last_used": last_used}, f)
        os.replace(tmp, meta)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _touch() -> None:
    """Record a use of the session (expiry counts from the last use). Best effort."""
    meta = _meta()
    if meta is None:
        return
    try:
        _write_meta(meta[0], time.time())
    except OSError:  # pragma: no cover - read-only folder: the session just expires earlier
        pass


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
    """The current session's vault; a new empty one when none exists, it expired (the vault
    and its key are deleted), or no key opens it (the stale file is deleted).

    Raises SessionError, keeping the vault, when the Keychain could not be read and no other
    key opens the vault.
    """
    with _THREAD_LOCK:
        if not vault_file().exists():
            _unlink(meta_file())
            return Vault()
        if _expired():
            forget_session()
            return Vault()
        keys, keychain_readable = _session_keys()
        for key in keys:
            try:
                v = Vault.load(vault_file(), key)
            except VaultError:
                continue
            except OSError:
                break
            _touch()     # in use: the expiry clock starts again
            return v
        else:
            if not keychain_readable:
                raise SessionError(KEYCHAIN_ERROR)
        _wipe_vault()
        return Vault()


def save_session_vault(v: Vault) -> None:
    """Encrypt `v` to the session file (creates the key if new; records the use, so the
    expiry clock starts again)."""
    with _THREAD_LOCK:
        key = session_key(create=True)
        if not key:  # pragma: no cover - session_key(create=True) always returns a key
            raise SessionError("세션 키를 만들 수 없습니다 (cannot create session key)")
        _mkdir_private(data_dir())
        now = time.time()
        created = _created() if vault_file().exists() else None
        _write_meta(created if created is not None else now, now)
        v.save(vault_file(), key)


def forget_session() -> bool:
    """Delete the session vault, its metadata and key (file and Keychain). True if anything
    was removed."""
    return bool(forget_session_items())


def _describe_vault() -> str:
    """" - 가명 5개 (이름 3, 전화번호 2), 3.0시간 전 시작, 마지막 사용 0.5시간 전" for `deid forget`.
    Counts and ages only, never values; best effort (a locked Keychain: no counts)."""
    parts = []
    try:
        from . import entities

        keys, _ = _session_keys()
        for key in keys:
            try:
                v = Vault.load(vault_file(), key)
            except (VaultError, OSError):
                continue
            by: dict[str, int] = {}
            for ent, _orig, _tok in v.items():
                lab = entities.label(ent)
                by[lab] = by.get(lab, 0) + 1
            detail = ", ".join(f"{lab} {n}" for lab, n in by.items())
            parts.append(f"가명 {len(v)}개" + (f" ({detail})" if detail else ""))
            break
    except Exception:  # noqa: BLE001 - a description never blocks deleting
        pass
    age, idle = session_age_hours(), session_idle_hours()
    if age is not None:
        parts.append(f"{age:.1f}시간 전 시작")
    if idle is not None:
        parts.append(f"마지막 사용 {idle:.1f}시간 전")
    return (" - " + ", ".join(parts)) if parts else ""


def forget_session_items(describe: bool = False) -> list[str]:
    """Delete the session vault, its metadata and key (file and Keychain) and return what was
    deleted, one Korean line per item (`describe`: with the number of pseudonyms and the
    session's age - never the values)."""
    with _THREAD_LOCK:
        removed: list[str] = []
        about = _describe_vault() if describe and vault_file().exists() else ""
        if _unlink(vault_file()):
            removed.append(f"가명 대응표 (session vault): {vault_file()}{about}")
        if _unlink(meta_file()):
            removed.append(f"세션 정보 (session info): {meta_file()}")
        if _unlink(key_file()):
            removed.append(f"세션 키 파일 (key file): {key_file()}")
        if _use_keychain():
            try:
                if mac.keychain_delete(KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE):
                    removed.append(f"키체인 항목 (Keychain item): {KEYCHAIN_SERVICE}")
            except mac.MacError:
                pass
        return removed


def purge_expired_session(lock_timeout: float = 0.0) -> bool:
    """Delete an expired session: vault, metadata and key (file and Keychain).

    Expiry used to be checked only when the next clip/text/restore loaded the vault, so a
    session stayed on disk (and in backups) for as long as deid was not used again. Every
    deid command, each folder-watch pass and `deid serve` call this. Never raises and never
    waits for a busy session (another deid command holds it: try again next time). True if
    something was deleted.
    """
    try:
        if not (vault_file().exists() or meta_file().exists()) or not _expired():
            return False
        if not _THREAD_LOCK.acquire(blocking=False):
            return False
        try:
            with _file_lock(timeout=lock_timeout):
                if not (vault_file().exists() or meta_file().exists()) or not _expired():
                    return False
                return forget_session()
        finally:
            _THREAD_LOCK.release()
    except (SessionError, OSError):
        return False


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
    "load_session_vault", "save_session_vault", "forget_session", "forget_session_items",
    "open_session", "session_age_hours", "session_idle_hours",
    "purge_expired_session", "data_dir", "config_dir", "user_dir", "vault_file", "key_file",
    "session_key", "session_hours", "seed_detector", "known_values", "unknown_tokens",
    "count_tokens", "make_private_dir", "prepare_work_dir", "loose_permissions",
    "keychain_problem", "SessionError",
)
