"""Regression tests from the final verification pass over the completeness gaps
(synthetic people only):

* a single unsupported file (DICOM, GIF, e-mail, video) says what to do instead, while a
  folder run still only counts it as skipped (README §1 "지원하지 않는 형식");
* `deid text` with a [ner] backend that cannot load stops with a clean message (exit 2, no
  traceback, nothing on stdout) instead of a Python traceback;
* the header word 이메일 after an 이름 label is not taken for a name;
* `deid --version` shows the install time for a folder install (no commit to compare).
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from deid import cli, formats
from deid.config import default_config
from deid.formats.text import deid_text
from deid.engine import Detector
from deid.anonymizer import Anonymizer


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


def set_stdin(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


# ---------------------------------------------------------------------- unsupported files


@pytest.mark.parametrize("ext,words", [
    (".dcm", ["DICOM", "익명화 내보내기", "deid shot"]),
    (".DCM", ["DICOM"]),
    (".gif", ["PNG"]),
    (".eml", ["메일", "첨부"]),
    (".mp4", ["동영상"]),
    (".m4a", ["음성"]),
])
def test_gap_unsupported_hint_says_what_to_do(ext, words):
    msg = formats.unsupported_message(f"검사{ext}")
    for w in words:
        assert w in msg
    assert "지원 형식:" not in msg               # a hint, not the generic list
    assert not formats.is_legacy(f"검사{ext}")   # folder runs still count these as skipped


def test_gap_unsupported_unknown_type_keeps_generic_list():
    msg = formats.unsupported_message("a.xyz")
    assert ".xyz" in msg and "지원 형식:" in msg


def test_gap_unsupported_single_file_and_folder(tmp_path, capsys):
    (tmp_path / "in").mkdir()
    for name in ("영상.dcm", "그림.gif"):
        (tmp_path / "in" / name).write_bytes(b"x")
    assert cli.main(["file", str(tmp_path / "in" / "영상.dcm"), "-o", str(tmp_path / "o1")]) == 2
    err = capsys.readouterr()
    assert "익명화 내보내기" in err.out + err.err
    cli.main(["file", str(tmp_path / "in"), "-o", str(tmp_path / "o2")])
    out = capsys.readouterr()
    text = out.out + out.err
    assert "건너뜀" in text and ".dcm 1" in text and ".gif 1" in text
    assert "익명화 내보내기" not in text


# ---------------------------------------------------------------------- deid text failures


def test_gap_text_backend_failure_is_clean(capsys, monkeypatch):
    import deid.formats.text as text_mod

    def boom(*a, **k):
        raise RuntimeError('the "gliner" NER backend needs gliner: pipx inject deid-kit gliner')

    monkeypatch.setattr(text_mod, "deid_text", boom)
    set_stdin(monkeypatch, "환자 표다은 님 내원".encode())
    assert cli.main(["text"]) == 2
    io_ = capsys.readouterr()
    assert io_.out == ""                                  # nothing half-processed on stdout
    assert "pipx inject deid-kit gliner" in io_.err and "deid doctor" in io_.err
    assert "Traceback" not in io_.err and "표다은" not in io_.err


def test_gap_text_missing_ner_backend_config_is_clean(tmp_path, capsys, monkeypatch):
    pytest.importorskip("deid.backends.gliner_backend")
    try:
        import gliner  # noqa: F401
        pytest.skip("gliner is installed")
    except ImportError:
        pass
    cfgfile = tmp_path / "ner.toml"
    cfgfile.write_text('[ner]\nbackend = "gliner"\n', encoding="utf-8")
    set_stdin(monkeypatch, "환자 표다은 님 내원".encode())
    assert cli.main(["text", "--config", str(cfgfile)]) == 2
    io_ = capsys.readouterr()
    assert io_.out == "" and "gliner" in io_.err and "Traceback" not in io_.err


# ---------------------------------------------------------------------- 이메일 header word


def _run(text: str) -> str:
    cfg = default_config()
    out, _ = deid_text(text, Detector(cfg), Anonymizer(cfg))
    return out


def test_gap_email_header_word_not_a_name():
    out = _run("이름\t이메일\n표다은\tdaeun@example.com\n")
    first, second = out.splitlines()[:2]
    assert first == "이름\t이메일"
    assert "표다은" not in second and "daeun@example.com" not in second
    assert second.startswith("[이름1]")                  # the real name gets the first token
    assert _run("이름 이메일 전화") == "이름 이메일 전화"
    assert "[이메일" in _run("이메일: daeun@example.com")


def test_gap_email_header_does_not_hide_real_names():
    out = _run("환자명: 표다은, 이메일 daeun@example.com")
    assert "표다은" not in out and "daeun@example.com" not in out


# ---------------------------------------------------------------------- version / install time


def test_gap_build_info_folder_install_time(tmp_path, monkeypatch):
    from importlib import metadata

    monkeypatch.setattr(cli, "_source_checkout", lambda: None)
    distinfo = tmp_path / "deid_kit-0.2.0.dist-info"
    distinfo.mkdir()
    (distinfo / "direct_url.json").write_text(
        json.dumps({"url": "file:///Users/doc/deid", "dir_info": {}}), encoding="utf-8")
    os.utime(distinfo / "direct_url.json", (1791745200, 1791745200))

    class Dist:
        _path = distinfo

        def read_text(self, name):
            p = distinfo / name
            return p.read_text(encoding="utf-8") if p.exists() else None

    monkeypatch.setattr(metadata, "distribution", lambda name: Dist())
    info = cli.build_info()
    assert info.startswith("설치 원본 폴더 /Users/doc/deid, 설치 20")
    assert len(info.rsplit("설치 ", 1)[1]) == len("2026-10-11 19:00")

    # editable installs and git installs keep their own wording (no install time)
    (distinfo / "direct_url.json").write_text(
        json.dumps({"url": "file:///Users/doc/deid", "dir_info": {"editable": True}}),
        encoding="utf-8")
    assert cli.build_info() == "편집 가능 설치 폴더 /Users/doc/deid"

    class NoPath:
        def read_text(self, name):
            return json.dumps({"url": "file:///Users/doc/deid", "dir_info": {}})

    monkeypatch.setattr(metadata, "distribution", lambda name: NoPath())
    assert cli.build_info() == "설치 원본 폴더 /Users/doc/deid"
