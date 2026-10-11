"""Regression tests for the UX gaps of the completeness review (synthetic people only):

* config edits reach a running `deid watch` / `deid serve` (LiveConfig), a broken edit keeps
  the previous settings and says so;
* config mistakes stop every command with the Korean message (exit 2, no traceback), and
  `deid doctor` lists validate_report problems as [!!];
* version 0.2.0 and the git commit / install source in `deid --version` and `deid doctor`;
* the clipboard session expires after 12 hours WITHOUT USE; `deid forget` says what it deleted;
* `deid restore --vault` / `deid text --restore --vault`, and a clear message without a TTY.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import deid
from deid import cli, mac, session, watch, web
from deid.config import default_config
from deid.watch import LiveConfig

from test_mac import FakeMac
from test_web import post_json, request

NAME = "설윤재"          # synthetic; not caught without a names entry or context
LINE = f"{NAME} 오늘 외래 방문"
BAD = '[actions]\nADRESS = "redact"\n'     # typo: refused by deid.config


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "deidhome"))
    for var in ("DEID_CONFIG", "DEID_VAULT_PASS", "DEID_DIR", "DEID_SHOT_DIR", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "DEID_KEYCHAIN", "DEID_SESSION_HOURS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture()
def fake_mac(monkeypatch):
    f = FakeMac()
    monkeypatch.setattr(mac, "is_macos", lambda: True)
    monkeypatch.setattr(mac, "run", f)
    return f


def set_stdin(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


def write(path: Path, text: str) -> None:
    """Write and make sure the change is visible even on coarse-mtime file systems."""
    old = path.stat().st_mtime_ns if path.exists() else None
    path.write_text(text, encoding="utf-8")
    if old is not None and path.stat().st_mtime_ns == old:
        os.utime(path, ns=(old + 10_000_000, old + 10_000_000))


def wait_for(cond, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ---------------------------------------------------------------------- (1) live config


def test_gap_liveconfig_follows_file(tmp_path):
    cfgfile = tmp_path / "c.toml"
    write(cfgfile, "names = []\n")
    live = LiveConfig(cfgfile)
    assert live.cfg.names == [] and live.refresh() is None          # unchanged: no reload
    write(cfgfile, f'names = ["{NAME}"]\n')
    change = live.refresh()
    assert change is not None and change.ok and "다시 읽었습니다" in change.message
    assert live.cfg.names == [NAME] and live.generation == 1
    write(cfgfile, BAD)                                              # broken edit
    change = live.refresh()
    assert change is not None and not change.ok and "ADRESS" in change.message
    assert live.cfg.names == [NAME]                                  # previous settings kept
    assert live.error and "ADDRESS" in live.error                    # with the suggestion
    assert live.refresh() is None                                    # reported once
    write(cfgfile, f'names = ["{NAME}", "남궁서진"]\n')
    assert live.refresh().ok and live.error is None and len(live.cfg.names) == 2
    cfgfile.unlink()                                                 # --config file deleted
    change = live.refresh()
    assert not change.ok and "찾을 수 없습니다" in change.message and len(live.cfg.names) == 2


def test_gap_liveconfig_default_location_appears_and_goes(isolated):
    default = isolated / ".config" / "deid" / "config.toml"
    live = LiveConfig()                    # no file yet: defaults, but the place is watched
    assert live.path == default and not live.explicit and live.cfg.names == []
    default.parent.mkdir(parents=True)
    write(default, f'names = ["{NAME}"]\n')
    assert live.refresh().ok and live.cfg.names == [NAME]
    default.unlink()
    change = live.refresh()
    assert change.ok and "기본 설정" in change.message and live.cfg.names == []


def test_gap_liveconfig_static_and_soft_problems(tmp_path):
    cfg = default_config()
    assert LiveConfig.static(cfg).refresh() is None and LiveConfig.static(cfg).cfg is cfg
    cfgfile = tmp_path / "c.toml"
    write(cfgfile, "allowlist = []\n")
    live = LiveConfig(cfgfile)
    write(cfgfile, f'allowlist = []\n[ner]\nnames = ["{NAME}"]\n')   # appended under [ner]
    change = live.refresh()
    assert change.ok and live.cfg.names == [NAME]
    assert any("[ner]" in p for p in change.problems)               # reported, not silent


def test_gap_liveconfig_bad_file_at_start_raises(tmp_path):
    cfgfile = tmp_path / "c.toml"
    write(cfgfile, BAD)
    with pytest.raises(ValueError):
        LiveConfig(cfgfile)


def _run_watch_thread(in_dir, out_dir, live, logs, notify=False):
    stop = threading.Event()
    t = threading.Thread(target=watch.run_watch, args=(in_dir, out_dir),
                         kwargs=dict(interval=0.2, cfg=live, log=logs.append, stop=stop,
                                     notify=notify),
                         daemon=True)
    t.start()
    return stop, t


def test_gap_watch_picks_up_config_edits(tmp_path):
    cfgfile = tmp_path / "config.toml"
    write(cfgfile, "names = []\n")
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    logs: list[str] = []
    stop, t = _run_watch_thread(in_dir, out_dir, LiveConfig(cfgfile), logs)
    try:
        assert wait_for(lambda: any("감시 시작" in m for m in logs))
        write(cfgfile, f'names = ["{NAME}"]\n')          # staff name added after the start
        assert wait_for(lambda: any("다시 읽었습니다" in m for m in logs))
        (in_dir / "a.txt").write_text(LINE, encoding="utf-8")
        out = out_dir / "a.deid.txt"
        assert wait_for(out.exists)
        assert out.read_text("utf-8") == "[이름1] 오늘 외래 방문"
    finally:
        stop.set()
        t.join(10)


def test_gap_watch_broken_config_keeps_previous_and_warns(tmp_path, fake_mac):
    cfgfile = tmp_path / "config.toml"
    write(cfgfile, f'names = ["{NAME}"]\n')
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    logs: list[str] = []
    stop, t = _run_watch_thread(in_dir, out_dir, LiveConfig(cfgfile), logs, notify=True)
    try:
        assert wait_for(lambda: any("감시 시작" in m for m in logs))
        write(cfgfile, f'names = ["{NAME}"]\n' + BAD)
        assert wait_for(lambda: any("설정 파일 오류" in m for m in logs))
        warning = next(m for m in logs if "설정 파일 오류" in m)
        assert "이전 설정으로 계속" in warning and "ADRESS" in warning
        (in_dir / "b.txt").write_text(LINE, encoding="utf-8")
        out = out_dir / "b.deid.txt"
        assert wait_for(out.exists)
        assert NAME not in out.read_text("utf-8")        # the old names still apply
        assert sum("설정 파일 오류" in m for m in logs) == 1   # once per version of the file
    finally:
        stop.set()
        t.join(10)
    title, message = next(n for n in fake_mac.notifications if "설정" in n[0])
    assert "이전 설정" in message and "ADRESS" not in message and NAME not in message


def test_gap_watch_without_cfg_follows_default_file(tmp_path, isolated):
    default = isolated / ".config" / "deid" / "config.toml"
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    logs: list[str] = []
    stop = threading.Event()
    t = threading.Thread(target=watch.run_watch, args=(in_dir, out_dir),
                         kwargs=dict(interval=0.2, log=logs.append, stop=stop), daemon=True)
    t.start()
    try:
        assert wait_for(lambda: any("감시 시작" in m for m in logs))
        default.parent.mkdir(parents=True)
        write(default, f'names = ["{NAME}"]\n')
        assert wait_for(lambda: any("다시 읽었습니다" in m for m in logs))
        (in_dir / "c.txt").write_text(LINE, encoding="utf-8")
        assert wait_for((out_dir / "c.deid.txt").exists)
        assert NAME not in (out_dir / "c.deid.txt").read_text("utf-8")
    finally:
        stop.set()
        t.join(10)


def test_gap_cli_watch_and_serve_get_live_config(tmp_path, monkeypatch):
    cfgfile = tmp_path / "c.toml"
    write(cfgfile, f'names = ["{NAME}"]\n')
    got = {}
    monkeypatch.setattr(watch, "run_watch", lambda i, o, **k: got.update(w=k) or 0)
    monkeypatch.setattr(web, "serve", lambda port, open_browser, cfg: got.update(s=cfg) or 0)
    assert cli.main(["watch", "IN", "OUT", "--config", str(cfgfile)]) == 0
    assert cli.main(["serve", "--no-browser", "--config", str(cfgfile)]) == 0
    for live in (got["w"]["cfg"], got["s"]):
        assert isinstance(live, LiveConfig) and live.follow and live.path == cfgfile
        assert live.cfg.names == [NAME]


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    import tempfile

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    cfgfile = tmp_path / "config.toml"
    write(cfgfile, "names = []\n")
    srv = web.make_server(0, LiveConfig(cfgfile))
    with web.running(srv):
        yield srv, cfgfile


def test_gap_web_rereads_config_per_request(live_server):
    srv, cfgfile = live_server
    status, _, data = post_json(srv, "/api/text", {"text": LINE})
    assert status == 200 and data["text"] == LINE and "config_warning" not in data
    write(cfgfile, f'names = ["{NAME}"]\n')
    status, _, data = post_json(srv, "/api/text", {"text": LINE})
    assert status == 200 and NAME not in data["text"] and "config_warning" not in data
    status, headers, body = request(srv, "POST", "/api/file", LINE.encode("utf-8"),
                                    {"X-Filename": "a.txt"})
    assert status == 200 and NAME not in body.decode("utf-8")


def test_gap_web_broken_config_warns_and_keeps_previous(live_server):
    from urllib.parse import unquote

    srv, cfgfile = live_server
    write(cfgfile, f'names = ["{NAME}"]\n')
    post_json(srv, "/api/text", {"text": "x"})
    write(cfgfile, f'names = ["{NAME}"]\n' + BAD)
    status, _, data = post_json(srv, "/api/text", {"text": LINE})
    assert status == 200 and NAME not in data["text"]              # previous settings
    assert "설정 파일 오류" in data["config_warning"] and "ADRESS" in data["config_warning"]
    status, _, data = post_json(srv, "/api/restore", {"text": data["text"]})
    assert status == 200 and data["text"] == LINE and "config_warning" in data
    status, headers, _ = request(srv, "POST", "/api/file", LINE.encode("utf-8"),
                                 {"X-Filename": "b.txt"})
    info = json.loads(unquote(headers["X-Deid-Report"]))
    assert status == 200 and "ADRESS" in info["config_warning"]
    status, _, page = request(srv, "GET", "/")
    html = page.decode("utf-8")
    banner = html[html.index('id="config-warning"'):]
    banner = banner[:banner.index("</div>")]
    assert "hidden" not in banner.split(">", 1)[0] and "설정 파일 오류" in banner
    assert "&#x27;ADRESS&#x27;" in banner                          # HTML-escaped
    status, _, body = request(srv, "GET", "/", token=False)         # not for strangers
    assert status == 403 and "ADRESS" not in body.decode("utf-8")
    write(cfgfile, f'names = ["{NAME}"]\n')                         # fixed: warning gone
    status, _, data = post_json(srv, "/api/text", {"text": LINE})
    assert "config_warning" not in data
    status, _, page = request(srv, "GET", "/")
    html = page.decode("utf-8")
    assert 'id="config-warning" class="notice bad" role="alert" hidden>' in html
    assert "__CONFIG" not in html


def test_gap_web_static_config_still_settable():
    srv = web.make_server(0, default_config())
    try:
        cfg = default_config()
        cfg.names.append(NAME)
        srv.cfg = cfg
        assert srv.cfg is cfg and srv.refresh_config() is None
    finally:
        srv.server_close()


# ---------------------------------------------------------------------- (2) config errors


@pytest.mark.parametrize("argv", [
    ["text"], ["text", "--restore"], ["file", "a.txt"], ["check", "a.txt"],
    ["watch", "IN", "OUT"], ["serve", "--no-browser"],
])
def test_gap_cli_config_error_is_clean(tmp_path, capsys, monkeypatch, argv):
    cfgfile = tmp_path / "bad.toml"
    write(cfgfile, BAD)
    write(tmp_path / "a.txt", LINE)
    set_stdin(monkeypatch, LINE.encode())
    assert cli.main(argv + ["--config", str(cfgfile)]) == 2
    io_ = capsys.readouterr()
    assert "설정 파일 오류" in io_.err and str(cfgfile) in io_.err
    assert "'ADDRESS'을(를) 말씀하신 건가요?" in io_.err and "deid doctor" in io_.err
    assert "Traceback" not in io_.err + io_.out and io_.out == ""
    assert not (tmp_path / "deid_out").exists() and not (tmp_path / "OUT").exists()


def test_gap_cli_config_error_via_env_and_mac_commands(tmp_path, capsys, monkeypatch, fake_mac):
    cfgfile = tmp_path / "bad.toml"
    write(cfgfile, '[actions]\nphone = "redakt"\n')
    monkeypatch.setenv("DEID_CONFIG", str(cfgfile))
    mac.copy_text(LINE)
    assert cli.main(["restore"]) == 2
    assert "redakt" in capsys.readouterr().err
    assert cli.main(["clip"]) == 2
    assert fake_mac.clipboard == b""                                 # never left in place
    assert "설정 파일 오류" in capsys.readouterr().err
    monkeypatch.setenv("DEID_CONFIG", str(tmp_path / "missing.toml"))
    assert cli.main(["text"]) == 2
    err = capsys.readouterr().err
    assert "찾을 수 없습니다" in err and str(tmp_path / "missing.toml") in err and "None" not in err


def test_gap_install_watch_refuses_broken_config(isolated, capsys, monkeypatch, fake_mac):
    default = isolated / ".config" / "deid" / "config.toml"
    default.parent.mkdir(parents=True)
    write(default, BAD)
    assert cli.main(["install-watch"]) == 2
    assert "설정 파일 오류" in capsys.readouterr().err
    assert not (isolated / "Library/LaunchAgents/com.softkorea.deid.watch.plist").exists()
    assert cli.main(["install-watch", "--print"]) == 0             # printing still works
    write(default, f'names = ["{NAME}"]\n')
    assert cli.main(["install-watch"]) == 0


def _doctor(monkeypatch, capsys) -> tuple[int, str]:
    from deid.backends import ollama

    monkeypatch.setattr(ollama, "available", lambda *a, **k: False)
    rc = cli.main(["doctor"])
    return rc, capsys.readouterr().out


def test_gap_doctor_lists_config_problems(tmp_path, capsys, monkeypatch):
    cfgfile = tmp_path / "c.toml"
    monkeypatch.setenv("DEID_CONFIG", str(cfgfile))
    write(cfgfile, f'allowlist = []\n[ner]\nnames = ["{NAME}"]\n')  # lands in [ner]
    rc, out = _doctor(monkeypatch, capsys)
    assert "[OK] 설정 파일" not in out
    assert "[!!] 설정 파일 (config)" in out and "[!!] 설정 파일 확인 필요" in out and "[ner]" in out
    write(cfgfile, BAD)
    rc, out = _doctor(monkeypatch, capsys)
    assert rc == 2 and "[XX] 설정 파일 (config)" in out and "ADRESS" in out
    assert sum("ADRESS" in ln for ln in out.splitlines()) == 1       # not repeated as [!!]
    write(cfgfile, f'names = ["{NAME}"]\n')
    rc, out = _doctor(monkeypatch, capsys)
    assert "[OK] 설정 파일 (config)" in out and "[!!] 설정 파일" not in out
    assert NAME not in out                                           # names are only counted


def test_gap_doctor_works_without_validate_report(tmp_path, capsys, monkeypatch):
    from deid import config

    monkeypatch.delattr(config, "validate_report")
    cfgfile = tmp_path / "c.toml"
    monkeypatch.setenv("DEID_CONFIG", str(cfgfile))
    write(cfgfile, f'names = ["{NAME}"]\n')
    rc, out = _doctor(monkeypatch, capsys)
    assert "[OK] 설정 파일 (config)" in out
    monkeypatch.setenv("DEID_CONFIG", str(tmp_path / "missing.toml"))
    rc, out = _doctor(monkeypatch, capsys)
    assert rc == 2 and "[XX] 설정 파일 (config)" in out


# ---------------------------------------------------------------------- (3) version


def test_gap_version_bumped_everywhere():
    try:
        import tomllib
    except ImportError:  # pragma: no cover - python 3.10
        tomllib = pytest.importorskip("tomli")
    root = Path(__file__).resolve().parent.parent
    proj = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]
    assert proj["version"] == deid.__version__ == "0.2.0"


def test_gap_version_shows_git_commit(capsys):
    assert cli.main(["--version"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("deid 0.2.0 (")
    if cli._source_checkout() is not None and any(
            (d / ".git").exists() for d in (cli._source_checkout(), *cli._source_checkout().parents)):
        import re
        assert re.search(r"git [0-9a-f]{7}", out)


def test_gap_git_head_reader(tmp_path):
    sha = "0123456789abcdef0123456789abcdef01234567"
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "refs" / "heads" / "main").write_text(sha + "\n")
    assert cli._git_head(repo) == (sha, "main")
    (repo / ".git" / "refs" / "heads" / "main").unlink()              # packed refs
    (repo / ".git" / "packed-refs").write_text(f"# pack-refs\n{sha} refs/heads/main\n")
    assert cli._git_head(repo) == (sha, "main")
    (repo / ".git" / "HEAD").write_text(sha + "\n")                   # detached
    assert cli._git_head(repo) == (sha, "")
    wt = tmp_path / "wt"                                              # git worktree
    (repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (repo / ".git" / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/feature\n")
    (repo / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    (repo / ".git" / "packed-refs").write_text(f"{sha} refs/heads/feature\n")
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'wt'}\n")
    assert cli._git_head(wt) == (sha, "feature")
    (wt / ".git").write_text("garbage\n")
    assert cli._git_head(wt) is None


def test_gap_build_info_install_sources(monkeypatch):
    from importlib import metadata

    monkeypatch.setattr(cli, "_source_checkout", lambda: None)

    class Dist:
        def __init__(self, data):
            self.data = data

        def read_text(self, name):
            return json.dumps(self.data) if name == "direct_url.json" else None

    def use(data):
        monkeypatch.setattr(metadata, "distribution", lambda name: Dist(data))
        return cli.build_info()

    info = use({"url": "https://user:ghp_secret@github.com/softkorea/softkorea.github.io.git",
                "vcs_info": {"vcs": "git", "commit_id": "abcdef0123456789"},
                "subdirectory": "tools/deid"})
    assert info.startswith("git abcdef0") and "ghp_secret" not in info and "user" not in info
    assert "github.com/softkorea/softkorea.github.io.git#subdirectory=tools/deid" in info
    assert use({"url": "file:///Users/doc/deid", "dir_info": {}}) == "설치 원본 폴더 /Users/doc/deid"

    def boom(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", boom)
    assert cli.build_info().startswith("설치 위치 ")
    monkeypatch.setattr(cli, "_source_checkout", lambda: (_ for _ in ()).throw(OSError("x")))
    assert cli.build_info()                                           # never raises


def test_gap_doctor_shows_version_and_source(capsys, monkeypatch):
    rc, out = _doctor(monkeypatch, capsys)
    line = next(ln for ln in out.splitlines() if "deid 버전" in ln)
    assert "0.2.0" in line and cli.build_info() in line


# ---------------------------------------------------------------------- (4) session expiry


def _meta() -> dict:
    return json.loads(session.meta_file().read_text())


def _set_meta(**kw) -> None:
    meta = _meta()
    meta.update(kw)
    session.meta_file().write_text(json.dumps(meta))


def _new_session(name: str = "한지민") -> str:
    from deid.vault import Vault

    v = Vault()
    tok = v.token_for("PERSON", name, "이름")
    session.save_session_vault(v)
    return tok


def test_gap_session_expiry_counts_from_last_use():
    _new_session()
    meta = _meta()
    assert meta["last_used"] >= meta["created"]
    now = time.time()
    _set_meta(created=now - 30 * 3600, last_used=now - 1 * 3600)   # old, but used an hour ago
    assert session.purge_expired_session() is False
    assert _meta()["last_used"] == pytest.approx(now - 3600)        # a check is not a use
    assert len(session.load_session_vault()) == 1                    # still there
    assert _meta()["last_used"] >= now - 1                           # the load was a use
    assert _meta()["created"] == pytest.approx(now - 30 * 3600)      # start time kept
    assert session.session_age_hours() == pytest.approx(30, abs=0.01)
    assert session.session_idle_hours() < 0.01
    _set_meta(last_used=time.time() - 13 * 3600)                     # unused for 13 hours
    assert session.purge_expired_session() is True
    assert not session.vault_file().exists() and not session.key_file().exists()


def test_gap_session_last_use_in_the_future_expires():
    _new_session()
    _set_meta(last_used=time.time() + 48 * 3600)                     # clock moved back
    assert session.purge_expired_session() is True


def test_gap_session_save_and_open_count_as_use(monkeypatch):
    _new_session()
    _set_meta(last_used=time.time() - 11 * 3600)
    with session.open_session(save=False):
        pass
    assert session.session_idle_hours() < 0.01
    monkeypatch.setenv("DEID_SESSION_HOURS", "2")                    # still configurable
    _set_meta(last_used=time.time() - 3 * 3600)
    assert len(session.load_session_vault()) == 0


def test_gap_session_expired_is_deleted_at_next_run(capsys, monkeypatch):
    set_stdin(monkeypatch, "환자 한지민 님".encode())
    assert cli.main(["text", "-q"]) == 0
    assert capsys.readouterr().out == "환자 [이름1] 님"
    _set_meta(created=time.time() - 20 * 3600, last_used=time.time() - 11 * 3600)
    set_stdin(monkeypatch, "[이름1] 님".encode())
    assert cli.main(["text", "--restore", "-q"]) == 0              # used 11 h ago: still there
    assert capsys.readouterr().out == "한지민 님"
    _set_meta(last_used=time.time() - 13 * 3600)                     # 13 h without use
    assert cli.main(["init-config", "--path", "c.toml"]) == 0        # any command purges it
    assert not session.vault_file().exists() and not session.key_file().exists()
    set_stdin(monkeypatch, "[이름1] 님".encode())
    assert cli.main(["text", "--restore", "-q"]) == 0
    assert capsys.readouterr().out.endswith("[이름1] 님")


def test_gap_doctor_shows_idle_time(capsys, monkeypatch):
    _new_session()
    _set_meta(created=time.time() - 5 * 3600, last_used=time.time() - 2 * 3600)
    rc, out = _doctor(monkeypatch, capsys)
    line = next(ln for ln in out.splitlines() if "세션 보관함" in ln)
    assert "5.0시간 전 시작" in line and "마지막 사용 2.0시간 전" in line and "쓰지 않으면" in line


def test_gap_forget_says_what_it_deleted(capsys, monkeypatch):
    assert cli.main(["forget"]) == 0
    assert "삭제할 세션 정보가 없습니다" in capsys.readouterr().out
    set_stdin(monkeypatch, "환자 한지민 님, 보호자 남궁서진 님, 등록번호 00482917".encode())
    assert cli.main(["text", "-q"]) == 0
    capsys.readouterr()
    assert cli.main(["forget"]) == 0
    out = capsys.readouterr().out
    assert "가명 대응표" in out and str(session.vault_file()) in out
    assert "가명 3개 (이름 2, 등록번호 1)" in out
    assert "세션 키 파일" in out and str(session.key_file()) in out and "세션 정보" in out
    assert "한지민" not in out and "남궁서진" not in out and "00482917" not in out
    assert not session.vault_file().exists() and not session.key_file().exists()


def test_gap_forget_lists_keychain_item(fake_mac, capsys, monkeypatch):
    monkeypatch.setenv("DEID_KEYCHAIN", "1")
    _new_session()
    assert fake_mac.keychain
    assert cli.main(["forget"]) == 0
    out = capsys.readouterr().out
    assert "키체인 항목" in out and "deid-session" in out and "가명 1개" in out
    assert not fake_mac.keychain


# ---------------------------------------------------------------------- (5) vault restore


@pytest.fixture()
def vault_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEID_VAULT_PASS", "pw-for-tests")
    write(tmp_path / "a.txt", f"환자 {NAME} 님 내원")
    vault = tmp_path / "proj.vault"
    assert cli.main(["file", str(tmp_path / "a.txt"), "--vault", str(vault)]) == 0
    assert (tmp_path / "deid_out" / "a.deid.txt").read_text("utf-8") == "환자 [이름1] 님 내원"
    capsys.readouterr()
    return vault


def test_gap_text_restore_with_vault(vault_file, capsys, monkeypatch):
    set_stdin(monkeypatch, "[이름1] 님은 다음 주 재진".encode())
    assert cli.main(["text", "--restore", "--vault", str(vault_file)]) == 0
    io_ = capsys.readouterr()
    assert io_.out == f"{NAME} 님은 다음 주 재진" and "복원 (restored): 1개" in io_.err


def test_gap_restore_with_vault_on_mac(vault_file, capsys, fake_mac):
    mac.copy_text("[이름1] 님 혈압 정상")
    assert cli.main(["restore", "--vault", str(vault_file)]) == 0
    assert mac.paste_text() == f"{NAME} 님 혈압 정상"
    assert cli.main(["restore", "--vault", str(vault_file.with_name("none.vault"))]) == 2
    assert "보관함 파일이 없습니다" in capsys.readouterr().err


def test_gap_restore_vault_without_terminal_says_how(vault_file, capsys, monkeypatch, fake_mac):
    monkeypatch.delenv("DEID_VAULT_PASS")
    monkeypatch.setattr(cli, "_can_prompt", lambda: False)        # Automator / Shortcuts
    set_stdin(monkeypatch, "[이름1] 님".encode())
    assert cli.main(["text", "--restore", "--vault", str(vault_file)]) == 2
    err = capsys.readouterr().err
    assert "터미널" in err and "DEID_VAULT_PASS" in err and "암호를 잊으면" in err
    mac.copy_text("[이름1] 님")
    assert cli.main(["restore", "--vault", str(vault_file), "--notify"]) == 2
    assert "DEID_VAULT_PASS" in capsys.readouterr().err
    title, message = fake_mac.notifications[-1]
    assert "실패" in title and "DEID_VAULT_PASS" in message and NAME not in message
    assert mac.paste_text() == "[이름1] 님"                         # clipboard untouched


def test_gap_text_restore_vault_prompts_on_tty_with_piped_stdin(vault_file, capsys, monkeypatch):
    """`deid text --restore --vault v < 답변.txt` in Terminal: stdin is the file, the
    passphrase is asked on the terminal (getpass uses /dev/tty)."""
    import getpass

    monkeypatch.delenv("DEID_VAULT_PASS")
    asked = []
    monkeypatch.setattr(cli, "_can_prompt", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": asked.append(prompt) or "pw-for-tests")
    set_stdin(monkeypatch, "[이름1] 님".encode())
    assert cli.main(["text", "--restore", "--vault", str(vault_file)]) == 0
    assert capsys.readouterr().out == f"{NAME} 님" and len(asked) == 1


def test_gap_can_prompt_uses_dev_tty_only_in_a_terminal(monkeypatch):
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    opened = []
    real_open = os.open

    def fake_open(path, flags, *a):
        opened.append(path)
        if path != "/dev/tty":
            raise OSError
        return real_open(os.devnull, os.O_RDONLY)

    monkeypatch.setattr(sys, "stdin", Stream(True))
    assert cli._can_prompt() is True
    monkeypatch.setattr(sys, "stdin", Stream(False))
    monkeypatch.setattr(sys, "stderr", Stream(False))                 # Automator, launchd
    monkeypatch.setattr(os, "open", fake_open)
    assert cli._can_prompt() is False and opened == []
    monkeypatch.setattr(sys, "stderr", Stream(True))                  # stdin redirected
    assert cli._can_prompt() is True and opened == ["/dev/tty"]

    def no_tty(path, flags, *a):
        raise OSError("no controlling terminal")

    monkeypatch.setattr(os, "open", no_tty)
    assert cli._can_prompt() is False


def test_gap_restore_help_documents_vault(capsys):
    assert cli.main(["restore", "--help"]) == 0
    out = capsys.readouterr().out
    assert "deid restore --vault ~/deid.vault" in out
    assert "deid text --restore --vault ~/deid.vault < 답변.txt" in out and "DEID_VAULT_PASS" in out
