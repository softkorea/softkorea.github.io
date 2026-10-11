"""Tests for deid.watch: one polling pass (watch_once) driven directly, plus run_watch."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from deid import formats, watch
from deid.config import default_config
from deid.vault import Vault
from deid.watch import WatchState, watch_once

NOTE = "환자명: 홍길동\n연락처 010-1234-5678\n홍길동 님 혈압 정상\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    logs: list[str] = []
    state = WatchState(path=tmp_path / "state.json")
    cfg = default_config()
    vault = Vault()

    def once(**kw):
        return watch_once(state, in_dir, out_dir, cfg, vault=vault, log=logs.append, **kw)

    return in_dir, out_dir, state, logs, once


def outputs(out_dir: Path) -> list[Path]:
    return sorted(p for p in out_dir.rglob("*") if p.is_file())


def test_processes_after_stable_poll(env):
    in_dir, out_dir, state, logs, once = env
    src = in_dir / "진료기록.txt"
    src.write_text(NOTE, encoding="utf-8")
    assert once() == []                       # first sighting: wait for stability
    assert outputs(out_dir) == []
    reports = once()
    assert len(reports) == 1
    out = out_dir / "진료기록.deid.txt"
    assert outputs(out_dir) == [out]
    text = out.read_text("utf-8")
    assert "홍길동" not in text and "1234" not in text
    assert "[이름1]" in text
    assert reports[0].counts["PERSON"] == 2
    assert any(line.startswith("OK 진료기록.deid.txt (이름 2, 전화번호 1)") for line in logs)
    assert once() == []                       # already done
    assert src.read_text("utf-8") == NOTE      # original untouched, still in place


def test_waits_while_file_is_growing(env):
    in_dir, out_dir, state, logs, once = env
    src = in_dir / "memo.txt"
    src.write_text("환자 김민준 님", encoding="utf-8")
    once()
    with open(src, "a", encoding="utf-8") as f:
        f.write(" 010-9876-5432")
    assert once() == []                        # size changed: still pending
    assert outputs(out_dir) == []
    assert len(once()) == 1
    assert "9876" not in (out_dir / "memo.deid.txt").read_text("utf-8")


def test_changed_file_is_reprocessed(env):
    in_dir, out_dir, state, logs, once = env
    src = in_dir / "memo.txt"
    src.write_text("환자 김민준 님", encoding="utf-8")
    once(), once()
    src.write_text("환자 이서연 님 재방문, 연락처 010-2222-3333", encoding="utf-8")
    os.utime(src, ns=(1, 1))   # make the change visible even within the same mtime tick
    assert once() == []
    assert len(once()) == 1
    text = (out_dir / "memo.deid.txt").read_text("utf-8")
    assert "이서연" not in text and "3333" in text and "2222" not in text


def test_no_raw_names_in_logs_or_state(env):
    in_dir, out_dir, state, logs, once = env
    (in_dir / "홍길동_소견서.txt").write_text(NOTE, encoding="utf-8")
    once(), once()
    state.save()
    assert [p.name for p in outputs(out_dir)] == ["[이름1]_소견서.deid.txt"]
    assert logs and all("홍길동" not in line for line in logs)
    raw = state.path.read_text("utf-8")
    assert "홍길동" not in raw and "소견서" not in raw
    data = json.loads(raw)
    assert data["v"] == 1 and len(data["files"]) == 1


def test_subfolders_are_kept_and_deidentified(env):
    in_dir, out_dir, state, logs, once = env
    sub = in_dir / "환자 김민준"
    sub.mkdir()
    (sub / "메모.txt").write_text("환자 김민준 님 내원", encoding="utf-8")
    once(), once()
    assert [p.relative_to(out_dir).as_posix() for p in outputs(out_dir)] == [
        "환자 [이름1]/메모.deid.txt"]


def test_skips_hidden_lock_unknown_and_outputs(env):
    in_dir, out_dir, state, logs, once = env
    (in_dir / ".DS_Store").write_bytes(b"x")
    (in_dir / "~$문서.docx").write_bytes(b"x")
    (in_dir / "data.bin").write_bytes(b"x")
    (in_dir / "old.deid.txt").write_text("이미 처리됨", encoding="utf-8")
    once(), once(), once()
    assert outputs(out_dir) == []
    assert logs == []


def test_known_unsupported_type_explained_once(env):
    in_dir, out_dir, state, logs, once = env
    (in_dir / "발표.pptx").write_bytes(b"x")
    once(), once(), once()
    assert len(logs) == 1 and "PDF" in logs[0]


def test_legacy_format_logged_once(env):
    in_dir, out_dir, state, logs, once = env
    (in_dir / "홍길동.hwp").write_bytes(b"HWP Document File")
    once(), once(), once(), once()
    fails = [line for line in logs if line.startswith("FAIL")]
    assert len(fails) == 1
    assert ".hwpx" in fails[0] and "홍길동" not in fails[0]
    assert fails[0].startswith("FAIL 홍**.hwp: ")
    assert outputs(out_dir) == []


def test_failure_is_logged_and_not_retried(env, monkeypatch):
    in_dir, out_dir, state, logs, once = env
    calls = []

    from deid.formats.docx import DocumentError

    def boom(*a, **k):   # a deid handler error: its message is safe to log
        calls.append(1)
        raise DocumentError("처리 실패 (synthetic failure)")

    monkeypatch.setattr(formats, "process_to_dir", boom)
    (in_dir / "a.txt").write_text("환자 홍길동 님", encoding="utf-8")
    once(), once(), once()
    assert len(calls) == 1
    assert any(line.startswith("FAIL a.txt: 처리 실패") for line in logs)
    assert outputs(out_dir) == []


def test_partial_output_never_left_on_failure(env, monkeypatch):
    in_dir, out_dir, state, logs, once = env
    from deid.formats import text as T

    def half(src, dst, *a, **k):
        Path(dst).write_text("환자 홍길동 (unprocessed)", encoding="utf-8")
        raise RuntimeError("crash mid-write")

    monkeypatch.setattr(T, "process", half)
    (in_dir / "a.txt").write_text("환자 홍길동 님", encoding="utf-8")
    once(), once()
    assert outputs(out_dir) == []


def test_out_dir_inside_in_dir_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    in_dir = tmp_path / "in"
    out_dir = in_dir / "out"
    in_dir.mkdir()
    (in_dir / "a.txt").write_text("환자 홍길동 님", encoding="utf-8")
    state = WatchState()
    for _ in range(4):
        watch_once(state, in_dir, out_dir, default_config(), log=lambda m: None)
    assert [p.name for p in out_dir.rglob("*") if p.is_file()] == ["a.deid.txt"]


def test_removed_file_is_forgotten(env):
    in_dir, out_dir, state, logs, once = env
    src = in_dir / "a.txt"
    src.write_text("환자 홍길동 님", encoding="utf-8")
    once(), once()
    assert len(state.files) == 1
    src.unlink()
    once()
    assert state.files == {}


def test_notify_counts_only(env, monkeypatch):
    """security-filename-raw-names: lock-screen notifications carry no file name at all."""
    in_dir, out_dir, state, logs, once = env
    from deid import mac

    sent = []
    monkeypatch.setattr(mac, "notify", lambda t, m: sent.append((t, m)) or True)
    (in_dir / "홍길동.txt").write_text("환자 홍길동 님", encoding="utf-8")
    once(notify=True), once(notify=True)
    assert len(sent) == 1
    assert "홍길동" not in sent[0][1] and "[이름1]" not in sent[0][1]
    assert "TXT 파일 1개" in sent[0][1] and "이름 1" in sent[0][1]


def test_state_round_trip(tmp_path):
    st = WatchState(path=tmp_path / "s.json")
    st.files["k"] = {"sig": [1, 2], "status": "done", "out": "a.deid.txt"}
    st.dirty = True
    st.save()
    assert WatchState.load(tmp_path / "s.json").files == st.files
    (tmp_path / "bad.json").write_text("{not json")
    assert WatchState.load(tmp_path / "bad.json").files == {}


def test_run_watch_persists_state_between_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    (in_dir / "a.txt").write_text("환자 홍길동 님", encoding="utf-8")
    logs: list[str] = []
    rc = watch.run_watch(in_dir, out_dir, interval=0.2, cfg=default_config(), log=logs.append,
                         max_passes=2)
    assert rc == 0
    assert (out_dir / "a.deid.txt").exists()
    assert watch.state_path_for(in_dir, out_dir).exists()
    (out_dir / "a.deid.txt").unlink()
    logs.clear()
    watch.run_watch(in_dir, out_dir, interval=0.2, cfg=default_config(), log=logs.append,
                    max_passes=2)
    assert not (out_dir / "a.deid.txt").exists()   # unchanged file is not processed again
    assert not any(line.startswith("OK") for line in logs)


def test_run_watch_stops_on_event_and_ctrl_c(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    stop = threading.Event()
    stop.set()
    assert watch.run_watch(tmp_path / "in", tmp_path / "out", stop=stop, cfg=default_config(),
                           log=lambda m: None) == 0

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(watch, "watch_once", interrupt)
    logs: list[str] = []
    assert watch.run_watch(tmp_path / "in", tmp_path / "out", cfg=default_config(),
                           log=logs.append) == 0
    assert any("종료" in line for line in logs)


def test_run_watch_rejects_same_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    logs: list[str] = []
    assert watch.run_watch(tmp_path, tmp_path, cfg=default_config(), log=logs.append,
                           max_passes=1) == 2


# ---------------------------------------------------------------------- regression: ux fixes


def test_security_filename_raw_names_watch_outputs_and_logs(env, monkeypatch):
    """Bare names in dropped file/folder names: no raw name in OUT, the log or notifications."""
    in_dir, out_dir, state, logs, once = env
    from deid import mac

    sent = []
    monkeypatch.setattr(mac, "notify", lambda t, m: sent.append(f"{t}: {m}") or True)
    body = "검사 결과 특이 소견 없음."
    (in_dir / "김민서.txt").write_text(body, encoding="utf-8")
    (in_dir / "박서준_외래기록.txt").write_text(body, encoding="utf-8")
    (in_dir / "이도윤").mkdir()
    (in_dir / "이도윤" / "scan1.txt").write_text(body, encoding="utf-8")
    once(notify=True), once(notify=True)
    names = ("김민서", "박서준", "이도윤")
    rels = [p.relative_to(out_dir).as_posix() for p in outputs(out_dir)]
    assert len(rels) == 3 and not any(n in r for r in rels for n in names)
    assert len(logs) == 3 and not any(n in line for line in logs for n in names)
    assert len(sent) == 3 and not any(n in m for m in sent for n in names)


def test_security_watch_log_fullpath_third_party_error(env):
    """PIL errors embed the full source path; the FAIL line must not."""
    in_dir, out_dir, state, logs, once = env
    (in_dir / "김민서_진료사진.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    (in_dir / "김민서_검사결과.jpg").write_bytes(b"not really a jpeg")
    once(), once()
    fails = [line for line in logs if line.startswith("FAIL")]
    assert len(fails) == 2
    for line in fails:
        assert "김민서" not in line and str(in_dir) not in line
        assert "이미지 파일을 읽을 수 없습니다" in line


def test_security_watch_log_fullpath_scrubs_paths(env, monkeypatch):
    in_dir, out_dir, state, logs, once = env
    from deid.formats.docx import DocumentError

    errors = iter([
        lambda p: DocumentError(f"문서를 열 수 없습니다: {p}"),                # deid's own text
        lambda p: PermissionError(13, "Permission denied", str(p)),          # OSError: strerror
        lambda p: KeyError(p.stem),                                         # unknown: type only
    ])

    def boom(path, *a, **k):
        raise next(errors)(Path(path))

    monkeypatch.setattr(formats, "process_to_dir", boom)
    sub = in_dir / "박서준"
    sub.mkdir()
    for name in ("김민서_소견.docx", "최유나.docx", "이도윤.docx"):
        (sub / name).write_bytes(b"x")
    once(), once()
    fails = [line for line in logs if line.startswith("FAIL")]
    assert len(fails) == 3
    for line in fails:
        assert not any(n in line for n in ("김민서", "최유나", "이도윤", "박서준", str(in_dir)))
    assert any("문서를 열 수 없습니다" in line for line in fails)
    assert any("권한이 없습니다 (PermissionError)" in line for line in fails)
    assert any("(KeyError)" in line for line in fails)


def test_security_deid_folders_world_readable_watch(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEID_DIR", str(tmp_path / "Deid"))
    old = os.umask(0o022)
    try:
        ud = tmp_path / "Deid"
        assert watch.run_watch(ud / "in", ud / "out", cfg=default_config(), log=lambda m: None,
                               max_passes=1) == 0
        for d in (ud, ud / "in", ud / "out"):
            assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
        ud.chmod(0o755)                                  # older version: tightened again
        (ud / "in").chmod(0o755)
        watch.run_watch(ud / "in", ud / "out", cfg=default_config(), log=lambda m: None,
                        max_passes=1)
        assert stat.S_IMODE(ud.stat().st_mode) == 0o700
        assert stat.S_IMODE((ud / "in").stat().st_mode) == 0o700
        mine = tmp_path / "shared_in"                    # a folder the user chose: left alone
        mine.mkdir()
        mine.chmod(0o750)
        watch.run_watch(mine, tmp_path / "new_out", cfg=default_config(), log=lambda m: None,
                        max_passes=1)
        assert stat.S_IMODE(mine.stat().st_mode) == 0o750
        assert stat.S_IMODE((tmp_path / "new_out").stat().st_mode) == 0o700
    finally:
        os.umask(old)


def test_security_session_never_expires_on_disk_watch_pass(tmp_path, monkeypatch):
    import time

    from deid import session

    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEID_SESSION_HOURS", raising=False)
    v = Vault()
    v.token_for("PERSON", "김민서", "이름")
    session.save_session_vault(v)
    session.meta_file().write_text(json.dumps({"created": time.time() - 13 * 3600}))
    watch.run_watch(tmp_path / "in", tmp_path / "out", cfg=default_config(),
                    log=lambda m: None, max_passes=1)
    assert not session.vault_file().exists() and not session.meta_file().exists()
    assert not session.key_file().exists()             # the key goes too
    session.save_session_vault(v)                      # a fresh session survives a pass
    watch.run_watch(tmp_path / "in", tmp_path / "out", cfg=default_config(),
                    log=lambda m: None, max_passes=1)
    assert session.load_session_vault().restore("[이름1]") == "김민서"
