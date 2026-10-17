"""CLI, watch and web with an OpenAI-compatible LLM server ("Strata" on the clinic PC).

Runs against FakeStrata (tests/test_openai_backend.py): an in-process server on 127.0.0.1 that
checks the Bearer key (401), validates reasoning_effort (400), can answer 502
structured_output_failed, serves one request at a time and has /health and /v1/models.
A "remote" server is simulated by making openai_compat.is_loopback say False, so the
requests still go to the local fake. Synthetic people and obviously fake keys only.
"""
from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from deid import cli, mac, watch, web
from deid.backends import openai_compat as oc
from deid.config import config_from_dict, load_config

from test_mac import FakeMac
from test_openai_backend import ENV, KEY, WRONG_KEY, FakeStrata

NAME = "오정애"                    # no label, no honorific: the rules alone do not find it
NOTE = f"어제 {NAME} 와서 상담함. 혈압 128/82, 다음 주 재방문.\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "deidhome"))
    for var in ("DEID_CONFIG", "DEID_VAULT_PASS", "DEID_DIR", "DEID_SHOT_DIR", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "DEID_KEYCHAIN", "DEID_SESSION_HOURS", ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture
def strata(monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    srv = FakeStrata()
    srv.table[NAME] = ("이름", None)
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture
def remote(monkeypatch):
    """Treat 127.0.0.1 as another computer (the fake still answers locally)."""
    monkeypatch.setattr(oc, "is_loopback", lambda hostname: False)


@pytest.fixture
def fake_mac(monkeypatch):
    f = FakeMac()
    monkeypatch.setattr(mac, "is_macos", lambda: True)
    monkeypatch.setattr(mac, "run", f)
    return f


@pytest.fixture
def no_ollama(monkeypatch):
    from deid.backends import ollama

    def refuse(*a, **k):
        raise AssertionError("Ollama must not be used")

    monkeypatch.setattr(ollama, "available", refuse)
    monkeypatch.setattr(ollama, "llm_review", refuse)


def closed_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def table_toml(name: str, url: str, *, allow_remote: bool = False, key: str = "env",
               **extra) -> str:
    lines = [f"[{name}]", 'backend = "openai"', f'base_url = "{url}"', 'model = "strata"',
             'effort = "none"']
    if key == "env":
        lines.append(f'api_key_env = "{ENV}"')
    elif key:
        lines.append(f'api_key_keychain = "{key}"')
    if allow_remote:
        lines.append("allow_remote = true")
    for k, v in extra.items():
        lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def use_config(tmp_path, monkeypatch, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("DEID_CONFIG", str(p))
    return p


def ner_config(tmp_path, monkeypatch, srv_or_url, **kw) -> Path:
    url = srv_or_url if isinstance(srv_or_url, str) else srv_or_url.url + "/v1"
    return use_config(tmp_path, monkeypatch, table_toml("ner", url, **kw))


def set_stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode()), encoding="utf-8"))


def no_secret(*texts: str) -> bool:
    return all(KEY not in t and WRONG_KEY not in t for t in texts)


# ====================================================================== deid text / file


def test_text_uses_server_for_detection_without_warning_for_this_computer(
        tmp_path, monkeypatch, capsys, strata):
    ner_config(tmp_path, monkeypatch, strata)
    set_stdin(monkeypatch, NOTE)
    assert cli.main(["text"]) == 0
    out, err = capsys.readouterr()
    assert NAME not in out and "[이름1]" in out and "혈압 128/82" in out
    assert "원문" not in err                       # loopback: no transport warning
    assert len(strata.chats) == 1
    assert strata.chats[0]["headers"]["authorization"] == f"Bearer {KEY}"
    assert no_secret(out, err)


def test_text_remote_server_warns_once_on_stderr(tmp_path, monkeypatch, capsys, strata, remote):
    ner_config(tmp_path, monkeypatch, strata, allow_remote=True)
    set_stdin(monkeypatch, NOTE)
    assert cli.main(["text", "-q"]) == 0
    out, err = capsys.readouterr()
    assert "[이름1]" in out and "!" not in out      # the warning never goes into the output
    assert err.count("비식별화 전 원문") == 1 and "암호화되지 않아" in err
    assert "127.0.0.1" in err and no_secret(err)


def test_text_server_down_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV, KEY)
    ner_config(tmp_path, monkeypatch, closed_url())
    set_stdin(monkeypatch, NOTE)
    assert cli.main(["text"]) == 2
    out, err = capsys.readouterr()
    assert out == ""                                         # nothing written
    assert "아무것도 출력하지 않았습니다" in err and "연결할 수 없습니다" in err
    assert "deid doctor" in err and "Traceback" not in err
    assert NAME not in err and "혈압" not in err


def test_text_missing_key_names_the_variable_only(tmp_path, monkeypatch, capsys, strata):
    monkeypatch.delenv(ENV)
    ner_config(tmp_path, monkeypatch, strata)
    set_stdin(monkeypatch, NOTE)
    assert cli.main(["text"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and ENV in err and strata.requests == []


def test_file_wrong_key_stops_run_and_writes_nothing(tmp_path, monkeypatch, capsys, strata):
    strata.key = "server-side-test-key-not-real"      # the client sends KEY: 401
    ner_config(tmp_path, monkeypatch, strata)
    src = tmp_path / "in"
    src.mkdir()
    for i in range(3):
        (src / f"note{i}.txt").write_text(NOTE, encoding="utf-8")
    assert cli.main(["file", str(src)]) == 2
    out, err = capsys.readouterr()
    outdir = src / "deid_out"
    assert not outdir.exists() or not any(outdir.iterdir())   # no output file at all
    assert "401" in err and "나머지 파일 2개는 처리하지 않았습니다" in err
    assert len(strata.chats) == 1                           # the other files were not tried
    assert no_secret(out, err) and NAME not in out + err
    assert "실패 3개" in out


def test_file_json_report_lists_stopped_files(tmp_path, monkeypatch, capsys, strata):
    strata.key = "server-side-test-key-not-real"
    ner_config(tmp_path, monkeypatch, strata)
    for i in range(2):
        (tmp_path / f"n{i}.txt").write_text(NOTE, encoding="utf-8")
    assert cli.main(["file", "n0.txt", "n1.txt", "--report", "json"]) == 2
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data["ok"] == 0 and data["failed"] == 2
    assert all(not f["ok"] for f in data["files"])
    assert "LLM" in data["files"][1]["error"] and no_secret(out, err)


def test_file_structured_output_failure_of_one_file_continues(tmp_path, monkeypatch, capsys,
                                                               strata):
    strata.sof_marker = "MARK-7"
    ner_config(tmp_path, monkeypatch, strata)
    (tmp_path / "a.txt").write_text(NOTE + "MARK-7\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text(NOTE, encoding="utf-8")
    assert cli.main(["file", "a.txt", "b.txt"]) == 2
    out, err = capsys.readouterr()
    assert "FAIL a.txt" in err and "OK b.txt" in out
    assert not (tmp_path / "deid_out" / "a.deid.txt").exists()
    assert NAME not in (tmp_path / "deid_out" / "b.deid.txt").read_text(encoding="utf-8")
    assert "structured_output_failed" in err and "deid doctor" in err


def test_file_rules_only_shows_warnings_also_from_the_file_name(tmp_path, monkeypatch, capsys,
                                                                 strata):
    strata.sof_marker = "외래기록"                       # only the file name fails
    ner_config(tmp_path, monkeypatch, strata, on_error="rules_only")
    (tmp_path / "외래기록.txt").write_text(NOTE, encoding="utf-8")
    assert cli.main(["file", "외래기록.txt"]) == 0
    out, err = capsys.readouterr()
    assert "OK 외래기록.txt" in out
    assert "   ! LLM 탐지가 글 1부분 중 1부분에서 실패" in out     # drained after the file name
    result = (tmp_path / "deid_out" / "외래기록.deid.txt").read_text(encoding="utf-8")
    assert NAME not in result                             # the content request worked


def test_file_rules_only_server_down_keeps_rules_and_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV, KEY)
    ner_config(tmp_path, monkeypatch, closed_url(), on_error="rules_only")
    (tmp_path / "a.txt").write_text("환자명: 김하늘\n" + NOTE, encoding="utf-8")
    assert cli.main(["file", "a.txt", "--report", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    entry = data["files"][0]
    assert entry["ok"] and any("규칙 기반 탐지만" in w for w in entry["warnings"])
    result = (tmp_path / "deid_out" / "a.deid.txt").read_text(encoding="utf-8")
    assert "김하늘" not in result                          # the rules still worked


def test_file_remote_warning_once_for_many_files(tmp_path, monkeypatch, capsys, strata, remote):
    ner_config(tmp_path, monkeypatch, strata, allow_remote=True)
    for i in range(3):
        (tmp_path / f"n{i}.txt").write_text(NOTE, encoding="utf-8")
    assert cli.main(["file", "n0.txt", "n1.txt", "n2.txt"]) == 0
    err = capsys.readouterr().err
    assert err.count("비식별화 전 원문") == 1


def test_remote_host_without_allow_remote_is_a_config_error(tmp_path, monkeypatch, capsys,
                                                            strata, remote):
    ner_config(tmp_path, monkeypatch, strata)            # no allow_remote
    set_stdin(monkeypatch, NOTE)
    assert cli.main(["text"]) == 2
    err = capsys.readouterr().err
    assert "allow_remote = true" in err and strata.requests == []


def test_main_reports_unhandled_llm_error_without_traceback(monkeypatch, capsys):
    def boom(args):
        raise oc.LLMError("LLM 서버(CLINIC1:8080)에 연결할 수 없습니다 (cannot reach)", None,
                          "connection")

    monkeypatch.setattr(cli, "cmd_forget", boom)
    assert cli.main(["forget"]) == 2
    err = capsys.readouterr().err
    assert "LLM 서버 문제로 멈췄습니다" in err and "deid doctor" in err


# ====================================================================== clip / shot


def test_clip_llm_failure_clears_clipboard(tmp_path, monkeypatch, capsys, strata, fake_mac):
    strata.key = "server-side-test-key-not-real"
    ner_config(tmp_path, monkeypatch, strata)
    fake_mac.clipboard = NOTE.encode()
    assert cli.main(["clip", "--notify"]) == 2
    err = capsys.readouterr().err
    assert fake_mac.clipboard == b""                      # the original cannot be pasted
    assert "401" in err and "deid doctor" in err and no_secret(err)
    assert fake_mac.notifications and "LLM 서버 문제" in fake_mac.notifications[-1][1]


def test_clip_remote_warns_and_masks(tmp_path, monkeypatch, capsys, strata, remote, fake_mac):
    ner_config(tmp_path, monkeypatch, strata, allow_remote=True)
    fake_mac.clipboard = NOTE.encode()
    assert cli.main(["clip"]) == 0
    err = capsys.readouterr().err
    assert NAME.encode() not in fake_mac.clipboard and b"[" in fake_mac.clipboard
    assert "비식별화 전 원문" in err


def test_shot_llm_failure_saves_nothing(tmp_path, monkeypatch, capsys, strata, fake_mac):
    from deid import formats

    strata.key = "server-side-test-key-not-real"
    ner_config(tmp_path, monkeypatch, strata)

    def process(src, dst, detector, anonymizer, cfg, **kw):   # stands in for OCR + redaction
        detector.detect_document([NOTE, "다음 줄"])
        raise AssertionError("unreachable")

    monkeypatch.setattr(formats, "process_file", process)
    out_dir = tmp_path / "shots"
    assert cli.main(["shot", "-o", str(out_dir)]) == 2
    err = capsys.readouterr().err
    assert "저장하지 않았습니다" in err and "deid doctor" in err and no_secret(err)
    assert not out_dir.exists() or not list(out_dir.glob("*.png"))


# ====================================================================== deid check


def check_config(tmp_path, monkeypatch, url: str, **kw) -> Path:
    return use_config(tmp_path, monkeypatch, table_toml("llm", url, **kw))


def test_check_routes_to_openai_review(tmp_path, monkeypatch, capsys, strata, no_ollama):
    strata.table = {"길동이": ("기타", None)}
    check_config(tmp_path, monkeypatch, strata.url + "/v1")
    f = tmp_path / "memo.deid.txt"
    f.write_text("[이름1] 님 내원\n별명은 길동이\n", encoding="utf-8")
    assert cli.main(["check", str(f)]) == 1
    out, err = capsys.readouterr()
    assert "strata: 남은 개인정보로 의심되는 항목 1개" in out
    assert "[기타] 길동이 (줄 2)" in out
    assert len(strata.chats) == 1
    payload = strata.chats[0]["payload"]
    assert payload["reasoning_effort"] == "none" and payload["model"] == "strata"
    assert "이미 비식별화된" in payload["messages"][1]["content"]       # the review prompt
    assert "[이름1] 님 내원" in payload["messages"][1]["content"]
    assert "!" not in err                                 # this computer: no notice
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml", "memo.deid.txt",
                                                          "userhome"]   # nothing written


def test_check_sends_only_deidentified_text_of_an_original(tmp_path, monkeypatch, capsys, strata,
                                                          no_ollama):
    check_config(tmp_path, monkeypatch, strata.url + "/v1")
    f = tmp_path / "memo.txt"
    f.write_text("환자 홍길동 님 010-1234-5678 내원\n", encoding="utf-8")
    assert cli.main(["check", str(f)]) == 0
    out, err = capsys.readouterr()
    sent = strata.chats[0]["payload"]["messages"][1]["content"]
    assert "홍길동" not in sent and "1234-5678" not in sent and "[이름1]" in sent
    assert "먼저 비식별화한 결과를 검사합니다" in err
    assert "남은 개인정보 의심 항목이 없습니다" in out


def test_check_host_option_overrides_base_url_and_llm_the_model(tmp_path, monkeypatch, capsys,
                                                               strata, no_ollama):
    check_config(tmp_path, monkeypatch, closed_url())
    f = tmp_path / "a.deid.txt"
    f.write_text("[이름1] 님 내원\n", encoding="utf-8")
    assert cli.main(["check", str(f), "--host", strata.url + "/v1", "--llm", "other-model"]) == 0
    assert strata.chats[0]["payload"]["model"] == "other-model"
    assert "other-model:" in capsys.readouterr().out


def test_check_remote_notice_once_and_as_is_refused(tmp_path, monkeypatch, capsys, strata, remote,
                                                    no_ollama):
    check_config(tmp_path, monkeypatch, strata.url + "/v1", allow_remote=True)
    f = tmp_path / "a.deid.txt"
    f.write_text("[이름1] 님 내원\n", encoding="utf-8")
    assert cli.main(["check", str(f)]) == 0
    out, err = capsys.readouterr()
    assert err.count("비식별화된 텍스트가 점검을 위해") == 1 and "http" in err
    assert f"({strata.url[7:]})" in out                 # model (host) for a remote server
    original = tmp_path / "memo.txt"
    original.write_text("환자 홍길동 님 내원\n", encoding="utf-8")
    n = len(strata.requests)
    assert cli.main(["check", str(original), "--as-is"]) == 2
    err = capsys.readouterr().err
    assert "--as-is" in err and "그대로 보내지 않습니다" in err
    assert len(strata.requests) == n                     # nothing sent


def test_check_remote_host_option_needs_allow_remote(tmp_path, monkeypatch, capsys, strata,
                                                     no_ollama):
    # "localhost" is this computer; 127.0.0.1 (the fake) plays the clinic PC
    monkeypatch.setattr(oc, "is_loopback", lambda h: h == "localhost")
    f = tmp_path / "a.deid.txt"
    f.write_text("[이름1] 님 내원\n", encoding="utf-8")
    check_config(tmp_path, monkeypatch, "http://localhost:9/v1")
    assert cli.main(["check", str(f), "--host", strata.url + "/v1"]) == 2
    assert "allow_remote = true" in capsys.readouterr().err and strata.requests == []
    check_config(tmp_path, monkeypatch, "http://localhost:9/v1", allow_remote=True)
    assert cli.main(["check", str(f), "--host", strata.url + "/v1"]) == 0
    assert len(strata.chats) == 1


def test_check_llm_error_is_clean(tmp_path, monkeypatch, capsys, strata, no_ollama):
    strata.key = "server-side-test-key-not-real"
    check_config(tmp_path, monkeypatch, strata.url + "/v1")
    f = tmp_path / "a.deid.txt"
    f.write_text("[이름1] 님 내원, 별명 길동이\n", encoding="utf-8")
    assert cli.main(["check", str(f)]) == 2
    out, err = capsys.readouterr()
    assert "LLM 검사 실패" in err and "401" in err and "deid doctor" in err
    assert "길동이" not in err and no_secret(out, err) and "Traceback" not in err


def test_check_original_with_failing_ner_sends_nothing_to_review(tmp_path, monkeypatch, capsys,
                                                                 strata, no_ollama):
    monkeypatch.setenv(ENV, KEY)
    use_config(tmp_path, monkeypatch, table_toml("ner", closed_url())
               + table_toml("llm", strata.url + "/v1"))
    f = tmp_path / "memo.txt"
    f.write_text(NOTE, encoding="utf-8")
    assert cli.main(["check", str(f)]) == 2
    err = capsys.readouterr().err
    assert "아무것도 보내지 않았습니다" in err and strata.requests == []


def test_check_without_openai_still_uses_ollama(tmp_path, monkeypatch, capsys):
    from deid.backends import ollama

    seen = {}
    monkeypatch.setattr(ollama, "available", lambda host, **k: True)
    monkeypatch.setattr(ollama, "llm_review",
                        lambda text, model, host, **k: seen.update(model=model) or [])
    f = tmp_path / "a.deid.txt"
    f.write_text("[이름1] 님 내원\n", encoding="utf-8")
    assert cli.main(["check", str(f)]) == 0
    assert seen["model"] == cli.DEFAULT_LLM


# ====================================================================== doctor


def doctor(monkeypatch, capsys) -> tuple[int, str]:
    from deid.backends import ollama

    monkeypatch.setattr(ollama, "available", lambda *a, **k: False)
    capsys.readouterr()
    rc = cli.main(["doctor"])
    return rc, capsys.readouterr().out


def test_doctor_shows_settings_key_source_health_and_model_never_the_key(
        tmp_path, monkeypatch, capsys, strata):
    use_config(tmp_path, monkeypatch, table_toml("ner", strata.url + "/v1")
               + table_toml("llm", strata.url + "/v1"))
    rc, out = doctor(monkeypatch, capsys)
    assert no_secret(out)
    assert f"[OK] LLM 서버 [ner] 탐지 (detection): {strata.url}/v1, model strata, effort none, " \
           "allow_remote false, on_error fail" in out
    assert f"[OK] API 키 [ner] (API key): 환경변수 {ENV}에서 찾음" in out
    assert "loaded=true" in out and "images=false" in out
    assert "모델 qwen3.8-flash-next-iq3_s" in out
    assert "[ner]와 같은 서버" in out                      # [llm]: same server, checked once
    assert "Ollama" not in out                           # deid check uses the [llm] server
    assert "원문이 이 Mac 밖으로" not in out and "unencrypted" not in out
    assert [r["path"] for r in strata.requests] == ["/health", "/v1/models"]


def test_doctor_remote_http_and_raw_text_warnings(tmp_path, monkeypatch, capsys, strata, remote):
    ner_config(tmp_path, monkeypatch, strata, allow_remote=True)
    rc, out = doctor(monkeypatch, capsys)
    assert "[!!] 원문이 이 Mac 밖으로 나감" in out
    assert "[!!] 암호화되지 않은 연결 [ner] (unencrypted http)" in out
    assert "allow_remote true" in out and no_secret(out)


def test_doctor_missing_key_names_source_and_skips_server(tmp_path, monkeypatch, capsys, strata):
    ner_config(tmp_path, monkeypatch, strata)
    monkeypatch.delenv(ENV)
    rc, out = doctor(monkeypatch, capsys)
    assert "[!!] API 키 [ner] (API key)" in out and ENV in out
    assert strata.requests == []


def test_doctor_wrong_key_and_unreachable_server(tmp_path, monkeypatch, capsys, strata):
    strata.key = "server-side-test-key-not-real"
    ner_config(tmp_path, monkeypatch, strata)
    rc, out = doctor(monkeypatch, capsys)
    assert "[!!] LLM 서버 응답 [ner] (server)" in out and "401" in out and no_secret(out)
    ner_config(tmp_path, monkeypatch, closed_url())
    rc, out = doctor(monkeypatch, capsys)
    assert "[!!] LLM 서버 응답 [ner] (server)" in out and "연결할 수 없습니다" in out
    assert "127.0.0.1.local" not in out                   # an IP address: no name hint


def test_doctor_unreachable_ip_suggests_a_changed_address(tmp_path, monkeypatch, capsys):
    def down(section, timeout=5.0):
        raise oc.LLMError("LLM 서버(192.168.0.20:8080)에 연결할 수 없습니다 (cannot reach)", None,
                          "connection")

    monkeypatch.setattr(oc, "health", down)
    monkeypatch.setenv(ENV, KEY)
    ner_config(tmp_path, monkeypatch, "http://192.168.0.20:8080/v1", allow_remote=True)
    rc, out = doctor(monkeypatch, capsys)
    assert "IP 주소가 192.168.0.20에서 바뀌었을 수" in out and "ipconfig" in out and no_secret(out)


def test_remote_http_server_by_name_is_refused(tmp_path, monkeypatch, capsys):
    """A NAME over plain http (CLINIC1, CLINIC1.local) is refused at config load: on another
    network any device that answers the name would get the original text and the key."""
    monkeypatch.setenv(ENV, KEY)
    for url in ("http://CLINIC1:8080/v1", "http://clinic1.local:8080/v1"):
        ner_config(tmp_path, monkeypatch, url, allow_remote=True)
        rc, out = doctor(monkeypatch, capsys)
        assert rc == 2 and "IP 주소를 적으세요" in out and no_secret(out)
    # https verifies the server's certificate: a name is fine there
    cfg = config_from_dict({"ner": {"backend": "openai", "base_url": "https://clinic1.example/v1",
                                    "allow_remote": True}})
    assert oc.ner_enabled(cfg)


def test_doctor_keychain_source_and_session_key_refused(tmp_path, monkeypatch, capsys, strata,
                                                        fake_mac):
    fake_mac.keychain[("deid", "deid-strata")] = KEY
    ner_config(tmp_path, monkeypatch, strata, key="deid-strata")
    rc, out = doctor(monkeypatch, capsys)
    assert "키체인 항목 'deid-strata'(계정 deid)에서 찾음" in out and no_secret(out)
    assert "loaded=true" in out
    fake_mac.keychain[("deid", "deid-session")] = "session-key-not-real"
    ner_config(tmp_path, monkeypatch, strata, key="deid-session")
    n = len(strata.requests)
    rc, out = doctor(monkeypatch, capsys)
    # refused when the config is loaded (every command, not only doctor): never read, never sent
    assert rc == 2 and "[XX] 설정 파일" in out and "세션 보관함의 열쇠" in out
    assert "session-key-not-real" not in out and len(strata.requests) == n
    assert not any("deid-session" in c for c in fake_mac.calls)


def test_doctor_without_openai_backend_is_unchanged(tmp_path, monkeypatch, capsys):
    rc, out = doctor(monkeypatch, capsys)
    assert "LLM 서버 [" not in out and "Ollama" in out


# ====================================================================== deid key


@pytest.fixture
def prompt(monkeypatch):
    """getpass answers with the queued values; records the prompts."""
    answers: list[str] = []
    asked: list[str] = []

    def fake_getpass(text=""):
        asked.append(text)
        return answers.pop(0)

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(cli, "_can_prompt", lambda: True)
    return answers


def test_key_set_reads_getpass_and_stores_via_security_stdin(fake_mac, prompt, capsys):
    prompt.append(f"  {KEY}\n")
    assert cli.main(["key", "set", "deid-strata"]) == 0
    out, err = capsys.readouterr()
    assert fake_mac.keychain[("deid", "deid-strata")] == KEY
    assert all(KEY not in " ".join(c) for c in fake_mac.calls)       # never in argv
    assert any(i and KEY.encode() in i for i in fake_mac.inputs)     # only on stdin
    assert no_secret(out, err)
    assert 'api_key_keychain = "deid-strata"' in out


def test_key_set_then_detection_uses_the_keychain(tmp_path, monkeypatch, capsys, fake_mac, prompt,
                                                  strata):
    monkeypatch.delenv(ENV)
    prompt.append(KEY)
    assert cli.main(["key", "set", "deid-strata"]) == 0
    ner_config(tmp_path, monkeypatch, strata, key="deid-strata")
    set_stdin(monkeypatch, NOTE)
    assert cli.main(["text"]) == 0
    out, err = capsys.readouterr()
    assert NAME not in out and strata.chats[0]["headers"]["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("value", ["test/key=not+real", "test key not real", "테스트키-not-real",
                                   "-test-key-not-real"])
def test_key_set_refuses_characters_keychain_set_cannot_pass(fake_mac, prompt, capsys, value):
    prompt.append(value)
    assert cli.main(["key", "set", "deid-strata"]) == 2
    out, err = capsys.readouterr()
    assert fake_mac.keychain == {} and value not in out + err
    assert "security add-generic-password -a deid -s deid-strata -U -w" in err
    assert "api_key_env" in err


def test_key_set_empty_and_no_terminal(fake_mac, prompt, monkeypatch, capsys):
    prompt.append("   ")
    assert cli.main(["key", "set", "deid-strata"]) == 2
    assert "비어 있어" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_can_prompt", lambda: False)
    assert cli.main(["key", "set", "deid-strata"]) == 2
    assert "터미널" in capsys.readouterr().err and fake_mac.keychain == {}


def test_key_status_and_delete_never_show_the_key(fake_mac, capsys):
    fake_mac.keychain[("deid", "deid-strata")] = KEY
    assert cli.main(["key", "status", "deid-strata"]) == 0
    out = capsys.readouterr().out
    assert "있음" in out and KEY not in out
    assert cli.main(["key", "delete", "deid-strata"]) == 0
    assert fake_mac.keychain == {}
    assert cli.main(["key", "status", "deid-strata"]) == 1
    assert "없음" in capsys.readouterr().out
    assert cli.main(["key", "delete", "deid-strata"]) == 1


def test_key_name_that_looks_like_a_key_is_not_echoed(fake_mac, capsys):
    name = "sk-testkeynotreal0123456789abcdef"
    assert cli.main(["key", "set", name]) == 2
    out, err = capsys.readouterr()
    assert name not in out + err and "터미널 기록" in err and fake_mac.calls == []


def test_key_session_name_is_reserved(fake_mac, prompt, capsys):
    prompt.append(KEY)
    assert cli.main(["key", "set", "deid-session"]) == 2
    assert "세션 보관함" in capsys.readouterr().err and fake_mac.keychain == {}


def test_key_off_macos_explains_the_environment_variable(monkeypatch, capsys):
    monkeypatch.setattr(mac, "is_macos", lambda: False)
    assert cli.main(["key", "set", "deid-strata"]) == 2
    err = capsys.readouterr().err
    assert "macOS" in err and 'api_key_env = "STRATA_API_KEY"' in err and "read -rs" in err


def test_key_keychain_refusal_is_reported(fake_mac, prompt, capsys):
    prompt.append(KEY)
    fake_mac.fail.add("security")
    assert cli.main(["key", "set", "deid-strata"]) == 2
    out, err = capsys.readouterr()
    assert "저장하지 못했습니다" in err and no_secret(out, err)


def test_key_help_and_usage():
    assert cli.main(["key", "--help"]) == 0
    assert cli.main(["key", "show", "deid-strata"]) == 2
    assert cli.main(["key"]) == 2


# ====================================================================== watch


def test_watch_logs_notice_at_start_and_fails_closed(tmp_path, monkeypatch, strata, remote):
    strata.key = "server-side-test-key-not-real"
    p = ner_config(tmp_path, monkeypatch, strata, allow_remote=True)
    in_dir, out_dir = tmp_path / "w_in", tmp_path / "w_out"
    in_dir.mkdir()
    (in_dir / f"{NAME}_외래.txt").write_text(NOTE, encoding="utf-8")
    lines: list[str] = []
    rc = watch.run_watch(in_dir, out_dir, interval=0.2, cfg=watch.LiveConfig(p, load_config(p)),
                         log=lines.append, max_passes=3, state_path=tmp_path / "state.json")
    text = "\n".join(lines)
    assert rc == 0
    assert lines[1].startswith("! 주의: 비식별화 전 원문")              # right after the start line
    assert "FAIL" in text and "401" in text and "다시 넣으면 처리합니다" in text
    assert NAME not in text and no_secret(text)
    assert not any(out_dir.iterdir())
    assert len(strata.chats) == 1          # the log name was made without asking the server


def test_watch_rules_only_warning_in_log(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    p = ner_config(tmp_path, monkeypatch, closed_url(), on_error="rules_only")
    in_dir, out_dir = tmp_path / "w_in", tmp_path / "w_out"
    in_dir.mkdir()
    (in_dir / "memo.txt").write_text("환자명: 김하늘\n" + NOTE, encoding="utf-8")
    lines: list[str] = []
    watch.run_watch(in_dir, out_dir, interval=0.2, cfg=watch.LiveConfig(p, load_config(p)),
                    log=lines.append, max_passes=3, state_path=tmp_path / "state.json")
    text = "\n".join(lines)
    assert "OK memo.deid.txt" in text and "규칙 기반 탐지만" in text
    assert "원문" not in lines[1]                         # this computer: no notice
    assert "김하늘" not in (out_dir / "memo.deid.txt").read_text(encoding="utf-8")


def test_watch_notice_follows_config_changes(tmp_path, monkeypatch, strata, remote):
    p = use_config(tmp_path, monkeypatch, "names = []\n")
    live = watch.LiveConfig(p, load_config(p))
    in_dir, out_dir = tmp_path / "w_in", tmp_path / "w_out"
    lines: list[str] = []

    def log(msg):
        lines.append(msg)
        if "감시 시작" in msg:
            p.write_text(table_toml("ner", strata.url + "/v1", allow_remote=True), encoding="utf-8")

    watch.run_watch(in_dir, out_dir, interval=0.2, cfg=live, log=log, max_passes=2,
                    state_path=tmp_path / "state.json")
    text = "\n".join(lines)
    assert "config reloaded" in text and "! 주의: 비식별화 전 원문" in text


def test_watch_display_detector_drops_only_the_openai_backend():
    from deid.config import config_from_dict

    gliner = config_from_dict({"ner": {"backend": "gliner"}})
    assert watch._rules_only(gliner) is gliner
    cfg = config_from_dict({"ner": {"backend": "openai", "base_url": "http://127.0.0.1:9/v1"},
                            "names": ["강지윤"]})
    rules = watch._rules_only(cfg)
    assert rules.ner == {} and rules.names == ["강지윤"] and cfg.ner["backend"] == "openai"


def test_transport_notice_helper():
    from deid.config import config_from_dict, default_config

    assert watch.transport_notice(None) is None
    assert watch.transport_notice(default_config()) is None
    cfg = config_from_dict({"ner": {"backend": "openai", "base_url": "http://127.0.0.1:8080/v1"}})
    assert watch.transport_notice(cfg) is None
    cfg = config_from_dict({"ner": {"backend": "strata", "base_url": "http://192.168.0.20:8080/v1",
                                    "allow_remote": True}})
    notice = watch.transport_notice(cfg)
    assert "192.168.0.20:8080" in notice and "http" in notice and "원문" in notice
    # [ner] backend = "ollama" with a host outside this computer sends raw text too
    cfg = config_from_dict({"ner": {"backend": "ollama", "model": "gemma3:4b", "allow_remote": True,
                                    "host": "http://192.168.0.20:11434"}})
    notice = watch.transport_notice(cfg)
    assert "192.168.0.20:11434" in notice and "원문" in notice
    cfg = config_from_dict({"ner": {"backend": "ollama", "model": "gemma3:4b"}})
    assert watch.transport_notice(cfg) is None


# ====================================================================== serve


_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_page(server) -> str:
    with _HTTP.open(server.url, timeout=5) as r:
        return r.read().decode("utf-8")


def post(server, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"X-Deid-Token": server.token,
                                          "Content-Type": "application/json"})
    try:
        with _HTTP.open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_serve_page_says_local_only_without_remote_server(tmp_path):
    from deid.config import default_config
    from deid.vault import Vault
    from contextlib import contextmanager

    @contextmanager
    def session():
        yield Vault()

    server = web.make_server(0, default_config(), session=session)
    with web.running(server):
        page = get_page(server)
    assert web.LOCAL_NOTE in page and 'id="llm-warning" class="notice bad" role="alert" hidden' in page
    assert "__LLM_" not in page and "__LOCAL_NOTE__" not in page


def test_serve_page_banner_and_text_with_remote_server(tmp_path, monkeypatch, strata, remote):
    from contextlib import contextmanager

    from deid.vault import Vault

    p = ner_config(tmp_path, monkeypatch, strata, allow_remote=True)

    @contextmanager
    def session():
        yield Vault()

    server = web.make_server(0, load_config(p), session=session)
    with web.running(server):
        page = get_page(server)
        status, data = post(server, "/api/text", {"text": NOTE})
        strata.key = "server-side-test-key-not-real"
        bad_status, bad = post(server, "/api/text", {"text": NOTE})
    assert web.LOCAL_NOTE not in page and "진료실 LLM 서버로" in page
    assert 'role="alert">주의: 비식별화 전 원문' in page
    assert status == 200 and NAME not in data["text"] and "[이름1]" in data["text"]
    assert bad_status == 422 and "401" in bad["error"] and NAME not in bad["error"]
    assert no_secret(json.dumps(bad, ensure_ascii=False))


def test_serve_banner_prints_notice(tmp_path, monkeypatch, strata, remote):
    p = ner_config(tmp_path, monkeypatch, strata, allow_remote=True)

    def stop(self, poll_interval=0.5):
        raise KeyboardInterrupt

    monkeypatch.setattr(web.DeidServer, "serve_forever", stop)
    out = io.StringIO()
    assert web.serve(0, open_browser=False, cfg=load_config(p), out=out) == 0
    assert "! 주의: 비식별화 전 원문" in out.getvalue()


# ====================================================================== docs / examples


def test_example_config_openai_lines_are_valid(tmp_path):
    """Un-commenting the two 'LLM server' blocks of `deid init-config` gives a valid config."""
    out, active = [], False
    for ln in cli.EXAMPLE_CONFIG.splitlines():
        if ln.startswith("[") or not ln.strip():
            active = False
        elif "진료실 LLM 서버" in ln:
            active = True
        elif active and ln.startswith("# "):
            ln = ln[2:]
        out.append(ln)
    p = tmp_path / "c.toml"
    p.write_text("\n".join(out), encoding="utf-8")
    cfg = load_config(p)
    assert oc.ner_enabled(cfg) and oc.llm_backend(cfg) == "openai"
    assert cfg.ner["base_url"] == "http://192.168.0.20:8080/v1" and cfg.ner["on_error"] == "fail"
    assert cfg.llm["api_key_keychain"] == "deid-strata"


def test_check_and_key_help_mention_the_server(capsys):
    assert cli.main(["check", "--help"]) == 0
    out = capsys.readouterr().out
    assert "base_url" in out and "allow_remote" in out
    assert cli.main(["--help"]) == 0
    assert "deid key set deid-strata" in capsys.readouterr().out
