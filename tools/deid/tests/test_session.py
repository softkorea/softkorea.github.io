"""Tests for deid.session: clipboard session vault, key storage, expiry, locking, seeding."""
from __future__ import annotations

import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from deid import mac, session
from deid.anonymizer import Anonymizer
from deid.config import default_config
from deid.engine import Detector
from deid.formats.text import deid_text
from deid.vault import Vault

from test_mac import FakeMac


@pytest.fixture()
def home(monkeypatch, tmp_path):
    h = tmp_path / "deidhome"
    monkeypatch.setenv("DEID_HOME", str(h))
    for var in ("DEID_KEYCHAIN", "DEID_SESSION_HOURS"):
        monkeypatch.delenv(var, raising=False)
    return h


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_paths_follow_deid_home(home):
    assert session.data_dir() == home
    assert session.vault_file() == home / "session.vault"
    assert session.key_file() == home / "session.key"


def test_default_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("DEID_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert session.data_dir() == tmp_path / "Library/Application Support/deid"
    assert session.vault_file() == tmp_path / "Library/Application Support/deid/session.vault"
    assert session.key_file() == tmp_path / ".config/deid/session.key"
    monkeypatch.setattr(sys, "platform", "linux")
    assert session.vault_file() == tmp_path / ".local/share/deid/session.vault"
    monkeypatch.delenv("DEID_DIR", raising=False)
    assert session.user_dir() == tmp_path / "Deid"


def test_empty_session(home):
    v = session.load_session_vault()
    assert isinstance(v, Vault) and len(v) == 0
    assert not session.vault_file().exists()


def test_save_and_load_round_trip(home):
    v = Vault()
    tok = v.token_for("PERSON", "홍길동", "이름")
    session.save_session_vault(v)
    assert mode(session.vault_file()) == 0o600
    assert mode(session.key_file()) == 0o600
    assert mode(home) == 0o700
    key = session.key_file().read_text().strip()
    assert len(key) == 64 and int(key, 16) >= 0
    assert "홍길동" not in session.vault_file().read_text("utf-8", "replace")
    loaded = session.load_session_vault()
    assert loaded.restore(f"{tok} 님") == "홍길동 님"
    # the key stays the same across saves
    session.save_session_vault(loaded)
    assert session.key_file().read_text().strip() == key


def test_expiry_starts_fresh(home):
    v = Vault()
    v.token_for("PERSON", "김민준", "이름")
    session.save_session_vault(v)
    meta = json.loads(session.meta_file().read_text())
    meta["created"] = meta["last_used"] = time.time() - 13 * 3600   # unused for 13 hours
    session.meta_file().write_text(json.dumps(meta))
    assert len(session.load_session_vault()) == 0
    assert not session.vault_file().exists()


def test_expiry_configurable(home, monkeypatch):
    v = Vault()
    v.token_for("PERSON", "이서연", "이름")
    session.save_session_vault(v)
    meta = {"created": time.time() - 2 * 3600}    # deid 0.1 meta: no last_used
    session.meta_file().write_text(json.dumps(meta))
    monkeypatch.setenv("DEID_SESSION_HOURS", "3")
    assert len(session.load_session_vault()) == 1
    session.meta_file().write_text(json.dumps(meta))   # (that load counted as a use)
    monkeypatch.setenv("DEID_SESSION_HOURS", "1")
    assert len(session.load_session_vault()) == 0


@pytest.mark.parametrize("raw", ["abc", "-5", "0", ""])
def test_bad_session_hours_fall_back(monkeypatch, raw):
    monkeypatch.setenv("DEID_SESSION_HOURS", raw)
    assert session.session_hours() == session.DEFAULT_HOURS


def test_created_kept_across_saves(home):
    v = Vault()
    v.token_for("PERSON", "홍길동", "이름")
    session.save_session_vault(v)
    first = json.loads(session.meta_file().read_text())["created"]
    time.sleep(0.01)
    v.token_for("PERSON", "김민준", "이름")
    session.save_session_vault(v)
    assert json.loads(session.meta_file().read_text())["created"] == first


def test_missing_meta_counts_as_expired(home):
    v = Vault()
    v.token_for("PERSON", "홍길동", "이름")
    session.save_session_vault(v)
    session.meta_file().unlink()
    assert len(session.load_session_vault()) == 0


def test_wrong_key_starts_fresh(home):
    v = Vault()
    v.token_for("PERSON", "홍길동", "이름")
    session.save_session_vault(v)
    session.key_file().write_text("00" * 32)
    assert len(session.load_session_vault()) == 0
    assert not session.vault_file().exists()


def test_loose_key_permissions_are_tightened(home):
    session.save_session_vault(Vault())
    session.key_file().chmod(0o644)
    session.load_session_vault()
    assert mode(session.key_file()) == 0o600


def test_forget_session(home):
    v = Vault()
    v.token_for("PERSON", "홍길동", "이름")
    session.save_session_vault(v)
    assert session.forget_session() is True
    assert not session.vault_file().exists()
    assert not session.key_file().exists()
    assert not session.meta_file().exists()
    assert session.forget_session() is False
    assert len(session.load_session_vault()) == 0


def test_open_session_saves_only_when_changed(home):
    with session.open_session() as v:
        pass
    assert not session.vault_file().exists()
    with session.open_session() as v:
        v.token_for("PERSON", "홍길동", "이름")
    assert session.vault_file().exists()
    with session.open_session(save=False) as v:
        v.token_for("PERSON", "김민준", "이름")
    assert len(session.load_session_vault()) == 1


def test_open_session_concurrent_threads_get_distinct_tokens(home):
    names = ["홍길동", "김민준", "이서연", "최수아", "박지훈", "정하윤"]
    tokens: dict[str, str] = {}

    def work(name):
        with session.open_session() as v:
            tokens[name] = v.token_for("PERSON", name, "이름")

    threads = [threading.Thread(target=work, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(tokens.values())) == len(names)
    v = session.load_session_vault()
    for name, tok in tokens.items():
        assert v.restore(tok) == name


def test_lock_timeout(home, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    session.data_dir().mkdir(parents=True, exist_ok=True)
    with open(session.lock_file(), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(session.SessionError, match="다른 deid"):
            with session._file_lock(timeout=0.2):
                pass


# ---------------------------------------------------------------------- keychain


@pytest.fixture()
def keychain(monkeypatch, home):
    fake = FakeMac()
    monkeypatch.setattr(mac, "is_macos", lambda: True)
    monkeypatch.setattr(mac, "run", fake)
    monkeypatch.setattr(session, "_use_keychain", lambda: True)
    return fake


def test_keychain_stores_key(keychain, home):
    v = Vault()
    tok = v.token_for("PERSON", "홍길동", "이름")
    session.save_session_vault(v)
    assert not session.key_file().exists()          # key went to the Keychain, not a file
    key = keychain.keychain[("deid", "deid-session")]
    assert len(key) == 64
    assert not any(key in a for c in keychain.calls for a in c)    # never on a command line
    add = [i for c, i in zip(keychain.calls, keychain.inputs) if c == ["security", "-i"]]
    assert add == [f"add-generic-password -U -a deid -s deid-session -w {key}\n".encode()]
    assert session.load_session_vault().restore(tok) == "홍길동"
    assert ["security", "find-generic-password", "-a", "deid", "-s", "deid-session",
            "-w"] in keychain.calls
    session.forget_session()
    assert ("deid", "deid-session") not in keychain.keychain


def test_keychain_failure_falls_back_to_key_file(keychain, home):
    keychain.fail.add("security")
    v = Vault()
    v.token_for("PERSON", "홍길동", "이름")
    session.save_session_vault(v)
    assert session.key_file().exists()
    assert len(session.load_session_vault()) == 1


def test_keychain_disabled_by_deid_home(monkeypatch, home):
    monkeypatch.setattr(mac, "is_macos", lambda: True)
    assert session._use_keychain() is False
    monkeypatch.setenv("DEID_KEYCHAIN", "0")
    assert session._use_keychain() is False


# ---------------------------------------------------------------------- helpers


def test_seed_detector_finds_names_from_earlier_pastes(home):
    cfg = default_config()
    with session.open_session() as v:
        out1, _ = deid_text("환자 홍길동 님 내원", Detector(cfg), Anonymizer(cfg, v))
    assert "홍길동" not in out1
    later = "홍길동 검사 결과 정상"
    plain, _ = deid_text(later, Detector(cfg), Anonymizer(cfg, Vault()))
    with session.open_session() as v:
        det = Detector(cfg)
        assert session.seed_detector(det, v) == 1
        out2, report = deid_text(later, det, Anonymizer(cfg, v))
    assert "홍길동" not in out2
    assert out2.startswith("[이름1]")  # same token as in the first paste
    assert report.counts.get("PERSON") == 1
    assert "홍길동" in plain   # without the session, a bare name has no context to be found


def test_known_values_maps_labels_back(home):
    cfg = default_config()
    v = Vault()
    v.token_for("PERSON", "홍길동", cfg.label_for("PERSON"))
    v.token_for("MRN", "12345678", cfg.label_for("MRN"))
    v.token_for("EMAIL", "minjun@example.com", cfg.label_for("EMAIL"))
    assert sorted(session.known_values(v, cfg)) == [
        ("EMAIL", "minjun@example.com"), ("MRN", "12345678"), ("PERSON", "홍길동")]


def test_count_and_unknown_tokens(home):
    cfg = default_config()
    v = Vault()
    t = v.token_for("PERSON", "홍길동", "이름")
    text = f"{t}와 {t}, 그리고 [이름7]과 [메모1]"
    assert session.count_tokens(text, v) == 2
    assert session.unknown_tokens(text, v, cfg) == 1


# ---------------------------------------------------------------------- regression: ux fixes


def _locked(keychain, code=36):
    """Make every `security` call fail like a locked login Keychain."""
    real = keychain.__call__

    def run(args, *, input=None, timeout=None):
        if args[0] == "security":
            keychain.calls.append(list(args))
            return __import__("subprocess").CompletedProcess(args, code, b"", b"locked")
        return real(args, input=input, timeout=timeout)

    return run


@pytest.mark.parametrize("code", [36, 51, 128])
def test_mac_keychain_error_wipes_session_locked_keychain_keeps_vault(keychain, home, monkeypatch,
                                                                     code):
    v = Vault()
    tok = v.token_for("PERSON", "최유나", "이름")
    session.save_session_vault(v)
    monkeypatch.setattr(mac, "run", _locked(keychain, code))
    with pytest.raises(session.SessionError, match="키체인"):
        session.load_session_vault()
    with pytest.raises(session.SessionError):
        with session.open_session() as s:
            s.token_for("PERSON", "김민서", "이름")
    assert session.vault_file().exists() and session.meta_file().exists()
    assert not session.key_file().exists()           # no second key was created
    monkeypatch.setattr(mac, "run", keychain)         # unlocked again
    assert session.load_session_vault().restore(tok) == "최유나"


def test_mac_keychain_error_wipes_session_timeout_and_missing_item(keychain, home, monkeypatch):
    v = Vault()
    v.token_for("PERSON", "최유나", "이름")
    session.save_session_vault(v)

    def timeout(args, *, input=None, timeout=None):
        raise mac.MacError("'security' 명령이 응답하지 않습니다 (timed out: security)")

    monkeypatch.setattr(mac, "run", timeout)
    with pytest.raises(session.SessionError):
        session.load_session_vault()
    assert session.vault_file().exists()
    monkeypatch.setattr(mac, "run", keychain)
    keychain.keychain.clear()                         # item really gone (exit 44): start fresh
    assert len(session.load_session_vault()) == 0
    assert not session.vault_file().exists()


def test_mac_keychain_error_wipes_session_second_key_still_opens(keychain, home, monkeypatch):
    """A key file made while the Keychain was unusable must keep working afterwards."""
    keychain.keychain[("deid", "deid-session")] = "ab" * 32      # an old Keychain key
    monkeypatch.setattr(mac, "run", _locked(keychain))
    v = Vault()
    tok = v.token_for("PERSON", "최유나", "이름")
    session.save_session_vault(v)                     # no vault yet: falls back to a key file
    assert session.key_file().exists()
    monkeypatch.setattr(mac, "run", keychain)
    assert session.load_session_vault().restore(tok) == "최유나"


def test_security_session_never_expires_on_disk_purge(home, monkeypatch):
    v = Vault()
    v.token_for("PERSON", "김민서", "이름")
    session.save_session_vault(v)
    assert session.purge_expired_session() is False           # fresh: kept
    assert session.vault_file().exists()
    session.meta_file().write_text(json.dumps({"created": time.time() - 13 * 3600}))
    fcntl = pytest.importorskip("fcntl")
    with open(session.lock_file(), "w") as f:                 # another deid command is busy
        fcntl.flock(f, fcntl.LOCK_EX)
        assert session.purge_expired_session() is False
    assert session.vault_file().exists()
    assert session.purge_expired_session() is True
    for p in (session.vault_file(), session.meta_file(), session.key_file()):
        assert not p.exists()
    assert session.purge_expired_session() is False


def test_security_session_never_expires_on_disk_purge_keychain(keychain, home):
    v = Vault()
    v.token_for("PERSON", "김민서", "이름")
    session.save_session_vault(v)
    session.meta_file().write_text(json.dumps({"created": time.time() - 13 * 3600}))
    assert session.purge_expired_session() is True
    assert ("deid", "deid-session") not in keychain.keychain   # the key goes too


def test_security_session_never_expires_on_disk_lazy_expiry_deletes_key(home):
    v = Vault()
    v.token_for("PERSON", "김민서", "이름")
    session.save_session_vault(v)
    session.meta_file().write_text(json.dumps({"created": time.time() - 13 * 3600}))
    assert len(session.load_session_vault()) == 0
    assert not session.key_file().exists()


def test_security_deid_folders_world_readable_private_dirs(home, monkeypatch, tmp_path):
    import os

    old = os.umask(0o022)
    try:
        deep = tmp_path / "a" / "b" / "c"
        session.make_private_dir(deep)
        for d in (tmp_path / "a", tmp_path / "a" / "b", deep):
            assert mode(d) == 0o700
        loose = tmp_path / "loose"
        loose.mkdir()
        loose.chmod(0o755)
        session.make_private_dir(loose)
        assert mode(loose) == 0o755                       # existing: untouched by default
        assert session.loose_permissions(loose)
        session.make_private_dir(loose, tighten=True)
        assert mode(loose) == 0o700 and not session.loose_permissions(loose)
        monkeypatch.setenv("DEID_DIR", str(tmp_path / "Deid"))
        (tmp_path / "Deid" / "in").mkdir(parents=True)
        (tmp_path / "Deid").chmod(0o755)
        (tmp_path / "Deid" / "in").chmod(0o755)
        session.prepare_work_dir(tmp_path / "Deid" / "in")
        assert mode(tmp_path / "Deid") == 0o700 and mode(tmp_path / "Deid" / "in") == 0o700
        (tmp_path / "file").write_text("x")
        with pytest.raises(OSError):
            session.make_private_dir(tmp_path / "file")
    finally:
        os.umask(old)


def test_security_deid_folders_world_readable_never_tightens_home(home, monkeypatch, tmp_path):
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    fake_home.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("DEID_DIR", str(fake_home))      # a careless DEID_DIR=~
    session.prepare_work_dir(fake_home / "in")
    assert mode(fake_home) == 0o755 and mode(fake_home / "in") == 0o700
