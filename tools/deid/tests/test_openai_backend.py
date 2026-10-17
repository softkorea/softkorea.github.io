"""OpenAI-compatible LLM backend ("Strata" on the clinic PC) against an in-process fake server.

The fake mimics the handoff spec: Bearer key check (401), reasoning_effort validated exactly
(400 for anything else), 502 structured_output_failed on demand, one request at a time,
GET /v1/models and GET /health, and a redirect that must not be followed. All people,
numbers and keys here are invented; the key is an obviously fake placeholder.
"""
from __future__ import annotations

import json
import math
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from deid import entities as E
from deid.anonymizer import Anonymizer
from deid.backends import build_ner_recognizer, entity_for, llm_type_entity
from deid.backends import openai_compat as oc
from deid.backends.openai_compat import LLMError, OpenAIRecognizer
from deid.config import Config, config_from_dict, default_config, validate_report
from deid.engine import Detector
from deid.types import Span
from deid.vault import Vault

KEY = "test-key-not-real"
WRONG_KEY = "test-key-also-not-real"
ENV = "STRATA_TEST_API_KEY"
SERVER_EFFORTS = {"none", "off", "minimal", "disabled", "false", "low", "medium", "high",
                  "xhigh", "max"}
_SEGMENT = re.compile(r"<<<(\d+)>>>\n")


# ====================================================================== fake server


class FakeStrata:
    """Strata look-alike on 127.0.0.1 (ephemeral port)."""

    def __init__(self, key: str | None = KEY) -> None:
        self.key = key
        self.requests: list[dict] = []
        self.table: dict[str, tuple] = {}        # value -> (type, person or None)
        self.extra: list[dict] = []              # items returned with every answer
        self.sof_marker: str | None = None       # segments containing it -> 502
        self.sof_first = 0                       # the first N chats -> 502
        self.replies: list = []                  # queued: content str or (status, body)
        self.fence = False
        self.delay = 0.0
        self.models = ["qwen3.8-flash-next-iq3_s"]
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()            # ONE request at a time, others queue
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, headers=None):
                data = body if isinstance(body, bytes) else json.dumps(
                    body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, payload):
                fake.requests.append({"method": self.command, "path": self.path,
                                      "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "payload": payload})

            def _authorized(self) -> bool:
                if fake.key is None:
                    return True
                if self.headers.get("Authorization") == f"Bearer {fake.key}":
                    return True
                self._send(401, {"error": {"message": "invalid or missing API key",
                                           "type": "authentication_error"}})
                return False

            def do_GET(self):
                self._record(None)
                if not self._authorized():
                    return
                if self.path == "/health":
                    return self._send(200, {"loaded": True, "images": False})
                if self.path == "/v1/models":
                    return self._send(200, {"object": "list", "data": [
                        {"id": m, "object": "model"} for m in fake.models]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                payload = json.loads(body.decode("utf-8"))
                self._record(payload)
                if self.path == "/redir/v1/chat/completions":
                    return self._send(307, b"", {"Location": fake.url + "/v1/chat/completions"})
                if self.path != "/v1/chat/completions":
                    return self._send(404, {"error": "not found"})
                if not self._authorized():
                    return
                with fake._lock:
                    fake.active += 1
                    fake.max_active = max(fake.max_active, fake.active)
                    try:
                        status, answer = fake.answer(payload)
                    finally:
                        fake.active -= 1
                self._send(status, answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------ chat logic

    @staticmethod
    def segments(payload: dict) -> list[str]:
        user = payload["messages"][1]["content"]
        body = user.split("[본문]\n", 1)[1]
        parts = _SEGMENT.split(body)
        return [parts[i + 1][:-1] if parts[i + 1].endswith("\n") else parts[i + 1]
                for i in range(1, len(parts), 2)]

    def answer(self, payload: dict) -> tuple[int, object]:
        if self.delay:
            time.sleep(self.delay)
        effort = payload.get("reasoning_effort")
        if effort is not None and effort not in SERVER_EFFORTS:
            return 400, {"error": {"message": "invalid reasoning_effort",
                                   "type": "invalid_request_error"}}
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, tuple):
                return reply
            return 200, self._completion(reply)
        segs = self.segments(payload)
        sof = {"error": {"message": "model output is not a JSON object",
                         "code": "structured_output_failed"}}
        if self.sof_first > 0:
            self.sof_first -= 1
            return 502, sof
        if self.sof_marker and any(self.sof_marker in s for s in segs):
            return 502, sof
        items = []
        for value, (typ, person) in self.table.items():
            if any(value in s for s in segs):
                item = {"text": value, "type": typ}
                if person:
                    item["person"] = person
                items.append(item)
        content = json.dumps({"items": items + self.extra}, ensure_ascii=False)
        if self.fence:
            content = f"결과입니다.\n```json\n{content}\n```"
        return 200, self._completion(content)

    @staticmethod
    def _completion(content: str) -> dict:
        return {"id": "chatcmpl-test", "object": "chat.completion", "model": "strata",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}]}

    @property
    def chats(self) -> list[dict]:
        return [r for r in self.requests
                if r["method"] == "POST" and r["path"].endswith("/chat/completions")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def strata(monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    srv = FakeStrata()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture
def closed_port_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def section(srv: FakeStrata | None = None, **kw) -> dict:
    sec = {"backend": "openai", "base_url": (srv.url if srv else "http://127.0.0.1:9") + "/v1",
           "api_key_env": ENV, "model": "strata", "effort": "none"}
    sec.update(kw)
    return sec


def ner_cfg(srv: FakeStrata, actions: dict | None = None, **kw) -> Config:
    data = {"ner": section(srv, **kw)}
    if actions:
        data["actions"] = actions
    return config_from_dict(data)


def secret_free(*messages: str, extra: tuple[str, ...] = ()) -> bool:
    return all(KEY not in m and WRONG_KEY not in m and all(x not in m for x in extra)
               for m in messages)


# ====================================================================== config


def test_config_accepts_openai_tables_without_problems(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[ner]\nbackend = "strata"\nbase_url = "http://192.168.0.20:8080/v1"\n'
        'api_key_env = "STRATA_API_KEY"\nmodel = "strata"\neffort = "xhigh"\nallow_remote = true\n'
        'timeout = 120\nbatch_chars = 3000\nmax_tokens = 4000\non_error = "rules-only"\n'
        '[llm]\nbackend = "openai"\nbase_url = "http://127.0.0.1:8080/v1"\n'
        'api_key_keychain = "deid-strata"\neffort = false\n', encoding="utf-8")
    assert validate_report(p) == []
    from deid.config import load_config

    cfg = load_config(p)
    assert cfg.ner["effort"] == "high" and cfg.ner["on_error"] == "rules_only"
    assert cfg.llm["effort"] == "none"
    conf = oc.settings(oc.ner_section(cfg))
    assert (conf.base_url, conf.effort, conf.allow_remote, conf.timeout, conf.batch_chars,
            conf.max_tokens, conf.on_error, conf.api_key_env) == (
        "http://192.168.0.20:8080/v1", "high", True, 120.0, 3000, 4000, "rules_only",
        "STRATA_API_KEY")
    llm = oc.settings(oc.llm_section(cfg))
    assert llm.effort == "none" and llm.api_key_keychain == "deid-strata" and llm.table == "llm"
    assert oc.llm_backend(cfg) == "openai" and oc.ner_enabled(cfg)


@pytest.mark.parametrize("value,canonical", [
    ("none", "none"), ("OFF", "none"), ("minimal", "none"), ("disabled", "none"),
    ("false", "none"), (False, "none"), (None, "none"), ("low", "low"), ("medium", "medium"),
    ("high", "high"), ("xhigh", "high"), ("max", "high"),
])
def test_effort_normalised_to_server_values(value, canonical):
    assert oc.normalize_effort(value) == canonical


@pytest.mark.parametrize("value", ["fast", "", "0", True, 3, "extreme"])
def test_invalid_effort_is_a_korean_config_error(value):
    with pytest.raises(ValueError, match="effort") as exc:
        config_from_dict({"ner": section(effort=value)})
    assert "none, low, medium, high" in str(exc.value) and "쓸 수 없습니다" in str(exc.value)


def test_defaults_when_keys_are_missing():
    conf = oc.settings({"base_url": "http://localhost:8080/v1/"}, "llm")
    assert (conf.base_url, conf.model, conf.effort, conf.allow_remote, conf.timeout,
            conf.batch_chars, conf.max_tokens, conf.on_error, conf.api_key_env,
            conf.api_key_keychain) == ("http://localhost:8080/v1", "strata", "none", False,
                                       180.0, 4000, 6000, "fail", None, None)
    assert conf.root == "http://localhost:8080" and conf.host == "localhost:8080"


@pytest.mark.parametrize("data,where", [
    ({"ner": {"backend": "openai", "api_key": "sk-test-not-real-0000"}}, "[ner]"),
    ({"llm": {"backend": "openai", "token": "sk-test-not-real-0000"}}, "[llm]"),
    ({"llm": {"Password": "sk-test-not-real-0000"}}, "[llm]"),
    ({"ocr": {"secret": "sk-test-not-real-0000"}}, "[ocr]"),
    ({"ner": {"label_map": {"key": "sk-test-not-real-0000"}}}, "[ner.label_map]"),
    ({"api-key": "sk-test-not-real-0000"}, "맨 위"),
    ({"patterns": [{"entity": "X", "regex": "a", "apikey": "sk-test-not-real-0000"}]},
     "[[patterns]]"),
    ({"columns": {"key": "sk-test-not-real-0000"}}, "[columns]"),
])
def test_secret_keys_are_rejected_without_echoing_the_value(data, where):
    with pytest.raises(ValueError) as exc:
        config_from_dict(data)
    msg = str(exc.value)
    assert where in msg and "api_key_env" in msg and "api_key_keychain" in msg
    assert "sk-test-not-real" not in msg


def test_secret_in_config_file_is_reported_by_validate(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[ner]\nbackend = "openai"\napi_key = "sk-test-not-real-1234"\n', encoding="utf-8")
    report = validate_report(p)
    assert len(report) == 1 and "api_key_env" in report[0] and "sk-test" not in report[0]


@pytest.mark.parametrize("name", ["sk-test-not-real-0000", "0123456789abcdef0123456789abcdef",
                                  "has space", "STRATA-KEY"])
def test_key_value_pasted_as_env_name_is_rejected_and_not_echoed(name):
    with pytest.raises(ValueError, match="api_key_env") as exc:
        config_from_dict({"ner": section(api_key_env=name)})
    assert name not in str(exc.value)
    with pytest.raises(ValueError, match="api_key_keychain") as exc:
        config_from_dict({"ner": section(api_key_env=None,
                                         api_key_keychain="sk-test-not-real-0000")})
    assert "sk-test" not in str(exc.value)


@pytest.mark.parametrize("url,match", [
    ("", "base_url"), ("CLINIC1:8080/v1", "http://"), ("ftp://127.0.0.1/v1", "http://"),
    ("http://user:pw-not-real@127.0.0.1:8080/v1", "비밀번호"),
    ("http://127.0.0.1:8080/v1?key=abc", "\\?"), ("http://127.0.0.1:99999/v1", "올바르지"),
    ("http://192.168.0.10:8080/v1", "allow_remote"),
    ("http://127.0.0.1.evil.example:8080/v1", "allow_remote"),
])
def test_base_url_validation(url, match):
    with pytest.raises(ValueError, match=match) as exc:
        config_from_dict({"ner": section(base_url=url)})
    assert "pw-not-real" not in str(exc.value) and "key=abc" not in str(exc.value)


def test_other_value_checks():
    for kw, match in [({"allow_remote": "maybe"}, "allow_remote"), ({"timeout": 0}, "timeout"),
                      ({"batch_chars": 10}, "batch_chars"), ({"max_tokens": "lots"}, "max_tokens"),
                      ({"on_error": "ignore"}, "on_error"), ({"model": ""}, "model")]:
        with pytest.raises(ValueError, match=match):
            config_from_dict({"ner": section(**kw)})
    with pytest.raises(ValueError, match="backend"):
        config_from_dict({"llm": {"backend": "gpt4"}})
    with pytest.raises(ValueError, match="알 수 없습니다"):
        config_from_dict({"ner": {"backend": "spacy"}})
    assert config_from_dict({"llm": {"backend": "Strata", "base_url": "http://[::1]:8080/v1"}})


def test_openai_keys_without_openai_backend_only_warn():
    problems: list[str] = []
    cfg = config_from_dict({"llm": {"model": "gemma3:4b", "effort": "fast"}}, problems=problems)
    assert cfg.llm == {"model": "gemma3:4b", "effort": "fast"}
    assert len(problems) == 1 and "effort" in problems[0] and "openai" in problems[0]
    assert oc.llm_backend(cfg) == "ollama" and not oc.ner_enabled(cfg)


def test_ner_falls_back_to_llm_connection_keys_but_never_allow_remote():
    llm = {"backend": "openai", "base_url": "http://192.168.0.20:8080/v1", "api_key_env": "X_KEY",
           "model": "m", "effort": "low", "allow_remote": True, "timeout": 30}
    with pytest.raises(ValueError, match="allow_remote"):   # raw text needs [ner]'s own consent
        config_from_dict({"ner": {"backend": "openai"}, "llm": llm})
    cfg = config_from_dict({"ner": {"backend": "openai", "allow_remote": True}, "llm": llm})
    conf = oc.settings(oc.ner_section(cfg))
    assert (conf.base_url, conf.api_key_env, conf.model, conf.effort, conf.timeout,
            conf.table) == ("http://192.168.0.20:8080/v1", "X_KEY", "m", "low", 30.0, "ner")
    # an Ollama [llm] table lends nothing
    with pytest.raises(ValueError, match="base_url"):
        config_from_dict({"ner": {"backend": "openai"}, "llm": {"model": "gemma3:4b"}})


# ====================================================================== API key


def test_resolve_key_from_env(monkeypatch):
    monkeypatch.setenv(ENV, f"  {KEY}\n")
    assert oc.resolve_api_key(section()) == (KEY, f"env:{ENV}")


def test_missing_env_key_names_the_variable(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(LLMError) as exc:
        oc.resolve_api_key(section())
    assert ENV in str(exc.value) and exc.value.kind == "key" and "export" in str(exc.value)


def test_no_key_source_means_no_key():
    assert oc.resolve_api_key({"base_url": "http://127.0.0.1:1/v1"}) == (None, "none")


def test_keychain_lookup(monkeypatch):
    from deid import mac

    calls = []

    def fake_get(account, service):
        calls.append((account, service))
        return KEY if service == "deid-strata" else None

    monkeypatch.setattr(mac, "keychain_get", fake_get)
    monkeypatch.delenv(ENV, raising=False)
    sec = section(api_key_keychain="deid-strata")      # env configured but empty -> Keychain
    assert oc.resolve_api_key(sec) == (KEY, "keychain:deid-strata")
    assert calls[-1] == ("deid", "deid-strata")
    with pytest.raises(LLMError) as exc:
        oc.resolve_api_key(section(api_key_env=None, api_key_keychain="other-item"))
    assert "other-item" in str(exc.value) and "add-generic-password" in str(exc.value)

    def locked(account, service):
        raise mac.MacError("키체인을 읽을 수 없습니다 (Keychain not readable: locked)")

    monkeypatch.setattr(mac, "keychain_get", locked)
    with pytest.raises(LLMError, match="키체인"):
        oc.resolve_api_key(section(api_key_env=None, api_key_keychain="deid-strata"))


def test_key_with_invalid_characters_is_refused_without_echo(monkeypatch):
    monkeypatch.setenv(ENV, "키 not-real")
    with pytest.raises(LLMError) as exc:
        oc.resolve_api_key(section())
    assert "not-real" not in str(exc.value)


def test_no_authorization_header_without_a_key_source(monkeypatch):
    srv = FakeStrata(key=None)
    try:
        srv.table = {"홍길동": ("이름", None)}
        rec = OpenAIRecognizer(section(srv, api_key_env=None))
        assert [s.text for s in rec.find("환자 홍길동 내원")] == ["홍길동"]
        assert "authorization" not in srv.chats[0]["headers"]
    finally:
        srv.close()


# ====================================================================== transport


@pytest.mark.parametrize("url", ["http://192.168.0.10:8080/v1", "https://api.example.com/v1",
                                 "http://10.0.0.1/v1", "http://[fe80::1]:8080/v1"])
def test_remote_hosts_refused_before_any_request(monkeypatch, url):
    def no_network(*a, **kw):
        raise AssertionError("network used")

    monkeypatch.setattr(oc, "_open", no_network)
    sec = section(base_url=url)
    for call in (lambda: OpenAIRecognizer(sec), lambda: oc.health(sec),
                 lambda: oc.list_models(sec), lambda: oc.review("[이름1] 내원", sec),
                 lambda: build_ner_recognizer(Config(ner=sec))):
        with pytest.raises(ValueError, match="allow_remote"):
            call()
    rec = OpenAIRecognizer(dict(sec, allow_remote=True))   # explicit consent: accepted
    assert rec.settings.is_remote


def test_transport_warning():
    assert oc.transport_warning(section(), raw_text=True) is None
    assert oc.transport_warning(section(base_url="http://[::1]:8080/v1"), raw_text=True) is None
    w = oc.transport_warning(section(base_url="http://192.168.0.20:8080/v1", allow_remote=True),
                             True)
    assert "원문" in w and "192.168.0.20:8080" in w and "http" in w and "암호화" in w
    w2 = oc.transport_warning({"base_url": "https://clinic1.example:8443/v1"}, raw_text=False)
    assert "원문" not in w2 and "비식별화된" in w2 and "암호화되지" not in w2


def test_request_payload_and_headers(strata):
    strata.table = {"김민준": ("이름", None)}
    rec = OpenAIRecognizer(section(strata, effort="off", max_tokens=1234))
    rec.find("환자 김민준 내원. 혈압 130/85.", default_config())
    (req,) = strata.chats
    p, h = req["payload"], req["headers"]
    assert req["path"] == "/v1/chat/completions"
    assert set(p) == {"model", "max_tokens", "reasoning_effort", "temperature", "response_format",
                      "messages"}
    assert (p["model"], p["max_tokens"], p["reasoning_effort"], p["temperature"],
            p["response_format"]) == ("strata", 1234, "none", 0, {"type": "json_object"})
    assert [m["role"] for m in p["messages"]] == ["system", "user"]
    assert p["messages"][0]["content"] == oc.SYSTEM_PROMPT
    user = p["messages"][1]["content"]
    assert "<<<1>>>\n환자 김민준 내원. 혈압 130/85." in user
    assert "90세" not in user and "기관" in user and "person" in user
    assert all(t in user for t in oc.TYPES)
    assert h["authorization"] == f"Bearer {KEY}"
    assert h["content-type"] == "application/json" and h["accept"] == "application/json"
    assert rec.request_count == 1


def test_redirect_is_not_followed(strata):
    rec = OpenAIRecognizer(section(base_url=strata.url + "/redir/v1"))
    with pytest.raises(LLMError, match="307") as exc:
        rec.find("환자 김민준 내원", default_config())
    assert exc.value.kind == "redirect"
    assert [r["path"] for r in strata.requests] == ["/redir/v1/chat/completions"]


def test_proxies_are_ignored(strata, monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    assert oc.health(section(strata)) == {"loaded": True, "images": False}


def test_health_and_models(strata, closed_port_url):
    assert oc.health(section(strata)) == {"loaded": True, "images": False}
    assert oc.list_models(section(strata)) == ["qwen3.8-flash-next-iq3_s"]
    assert [r["path"] for r in strata.requests] == ["/health", "/v1/models"]
    assert all(r["headers"]["authorization"] == f"Bearer {KEY}" for r in strata.requests)
    with pytest.raises(LLMError) as exc:
        oc.list_models(section(base_url=closed_port_url))
    assert exc.value.kind == "connection"


def test_401_and_400_messages_contain_no_text_or_key(strata, monkeypatch):
    text = "환자 설윤재 (010-2468-1357) 내원"
    monkeypatch.setenv(ENV, WRONG_KEY)
    with pytest.raises(LLMError) as exc:
        OpenAIRecognizer(section(strata)).find(text, default_config())
    e = exc.value
    assert e.status == 401 and e.kind == "auth" and "API 키" in str(e) and ENV in str(e)
    assert secret_free(str(e), repr(e), extra=("설윤재", "2468"))
    with pytest.raises(LLMError) as exc:
        oc.health(section(strata))
    assert exc.value.status == 401
    monkeypatch.setenv(ENV, KEY)
    # the server rejects an effort value deid would never send (e.g. an older config)
    rec = OpenAIRecognizer(section(strata))
    object.__setattr__(rec.settings, "effort", "turbo")
    with pytest.raises(LLMError) as exc:
        rec.find(text, default_config())
    e = exc.value
    assert e.status == 400 and e.kind == "rejected" and "effort" in str(e) and "batch_chars" in str(e)
    assert secret_free(str(e), repr(e), repr(rec), extra=("설윤재", "2468"))


def test_timeout_and_unreachable(strata, closed_port_url):
    strata.delay = 1.5
    with pytest.raises(LLMError) as exc:
        OpenAIRecognizer(section(strata, timeout=1)).find("환자 김민준", default_config())
    assert exc.value.kind == "timeout"
    strata.delay = 0
    with pytest.raises(LLMError) as exc:
        OpenAIRecognizer(section(base_url=closed_port_url)).find("환자 김민준", default_config())
    assert exc.value.kind == "connection" and exc.value.status is None


def test_server_errors_and_bad_answers(strata):
    rec = OpenAIRecognizer(section(strata))
    strata.replies.append((500, {"error": "boom"}))
    with pytest.raises(LLMError, match="500") as exc:
        rec.find("환자 김민준")
    assert exc.value.kind == "server"
    strata.replies.append((200, {"choices": []}))
    with pytest.raises(LLMError, match="content"):
        rec.find("환자 김민준")
    strata.replies.append("sorry, no JSON here")    # a single segment that never parses
    with pytest.raises(LLMError, match="JSON") as exc:
        rec.find("환자 김민준")
    assert exc.value.kind == "structured_output"


def test_code_fences_and_leading_text_are_tolerated(strata):
    strata.fence = True
    strata.table = {"김민준": ("이름", None)}
    assert [s.text for s in OpenAIRecognizer(section(strata)).find("환자 김민준 내원")] == ["김민준"]


# ====================================================================== batching


def test_short_texts_are_packed_into_few_requests(strata):
    texts = [f"{i:02d}번 환자 기록 " + "가" * 90 for i in range(40)]   # 100 chars each
    assert {len(t) for t in texts} == {100}
    rec = OpenAIRecognizer(section(strata, batch_chars=1000))
    out = rec.find_batch(texts, default_config())
    assert len(out) == 40 and out == [[] for _ in texts]
    assert len(strata.chats) == math.ceil(sum(map(len, texts)) / 1000) == rec.request_count == 4
    assert [len(FakeStrata.segments(r["payload"])) for r in strata.chats] == [10, 10, 10, 10]
    assert strata.max_active == 1


def test_detect_document_sends_one_request_for_forty_short_texts(strata):
    strata.table = {"김민준": ("이름", None), "행복내과의원": ("기관", None)}
    cfg = ner_cfg(strata)
    det = Detector(cfg)
    texts = [f"{i}번째 메모: 혈압 120/80, 특이사항 없음." for i in range(39)]
    texts.append("보호자 김민준과 통화함. 행복내과의원 의뢰서 지참.")
    out = det.detect_document(texts)
    assert len(strata.chats) == 1
    assert len(FakeStrata.segments(strata.chats[0]["payload"])) == 40
    last = {(s.entity, s.text) for s in out[-1]}
    assert (E.PERSON, "김민준") in last and (E.ORG, "행복내과의원") in last
    det.detect("환자 김민준 재진")            # detect() = one more request (find)
    assert len(strata.chats) == 2


def test_long_text_is_split_at_lines_with_correct_offsets(strata):
    lines = [f"{i:03d} 경과기록 혈압 120/80 정상 범위, 투약 지속함.\n" for i in range(120)]
    lines[7] = "007 보호자 김민준 면담함.\n"
    lines[95] = "095 김민준 보호자 재방문.\n"
    text = "".join(lines)
    strata.table = {"김민준": ("이름", None)}
    rec = OpenAIRecognizer(section(strata, batch_chars=500))
    spans = rec.find(text, default_config())
    assert [(s.start, s.end) for s in spans] == [m.span() for m in re.finditer("김민준", text)]
    assert all(text[s.start:s.end] == s.text for s in spans)
    for r in strata.chats:
        segs = FakeStrata.segments(r["payload"])
        assert sum(map(len, segs)) <= 500
        assert all(seg.endswith("\n") or seg == text[-len(seg):] for seg in segs)
    assert math.ceil(len(text) / 500) <= len(strata.chats) <= math.ceil(len(text) / 500) + 1


def test_split_text_and_pack_helpers():
    assert oc.split_text("", 100) == [] and oc.split_text(" \n ", 100) == []
    assert oc.split_text("짧은 글", 100) == [(0, "짧은 글")]
    text = "".join(f"line {i}\n" for i in range(50))
    pieces = oc.split_text(text, 60)
    assert "".join(p for _, p in pieces) == text
    assert all(text[o:o + len(p)] == p and len(p) <= 60 for o, p in pieces)
    one_line = "가나다 " * 100
    pieces = oc.split_text(one_line, 120)
    assert all(one_line[o:o + len(p)] == p and len(p) <= 120 for o, p in pieces)
    assert pieces[-1][0] + len(pieces[-1][1]) == len(one_line)
    segs = [oc.Segment(0, 0, "x" * n) for n in (30, 30, 50, 10, 90)]
    assert [sum(len(s.text) for s in b) for b in oc.pack(segs, 100)] == [60, 60, 90]


def test_detector_calls_find_batch_once_per_document():
    class Batchy:
        name, entities = "batchy", (E.PERSON,)

        def __init__(self):
            self.batch_calls, self.find_calls = [], []

        def find(self, text, cfg):
            self.find_calls.append(text)
            return []

        def find_batch(self, texts, cfg):
            self.batch_calls.append(list(texts))
            return [[Span(t.index("김민준"), t.index("김민준") + 3, E.PERSON, "김민준", 0.7, "batchy")]
                    if "김민준" in t else [] for t in texts]

    rec = Batchy()
    det = Detector(default_config(), recognizers=[rec])
    out = det.detect_document(["메모 하나", "", "보호자 김민준 통화", "메모 셋"])
    assert rec.batch_calls == [["메모 하나", "보호자 김민준 통화", "메모 셋"]] and rec.find_calls == []
    assert [[s.text for s in o] for o in out] == [[], [], ["김민준"], []]
    det.detect("김민준 재진")
    assert rec.find_calls == ["김민준 재진"] and len(rec.batch_calls) == 1


# ====================================================================== 502 split-retry


def test_structured_output_failure_splits_the_batch_once_per_level(strata):
    strata.table = {"김민준": ("이름", None), "이서연": ("이름", None)}
    strata.sof_first = 1
    rec = OpenAIRecognizer(section(strata))
    texts = ["보호자 김민준", "메모", "메모2", "담당 이서연"]
    out = rec.find_batch(texts, default_config())
    assert [[s.text for s in o] for o in out] == [["김민준"], [], [], ["이서연"]]
    sizes = [len(FakeStrata.segments(r["payload"])) for r in strata.chats]
    assert sizes == [4, 2, 2]


def test_structured_output_failure_gives_up_on_a_single_segment(strata):
    strata.table = {"김민준": ("이름", None), "이서연": ("이름", None)}
    strata.sof_marker = "MAGIC-FAIL"
    texts = ["보호자 김민준", "MAGIC-FAIL 메모", "메모2", "담당 이서연"]
    with pytest.raises(LLMError) as exc:
        OpenAIRecognizer(section(strata)).find_batch(texts, default_config())
    assert exc.value.status == 502 and exc.value.kind == "structured_output"
    assert "MAGIC" not in str(exc.value)
    sizes = [len(FakeStrata.segments(r["payload"])) for r in strata.chats]
    assert sizes == [4, 2, 1, 1]       # 4 -> halves; [0,1] -> [0] ok, [1] fails alone: give up
    strata.requests.clear()
    rec = OpenAIRecognizer(section(strata, on_error="rules_only"))
    out = rec.find_batch(texts, default_config())
    assert [[s.text for s in o] for o in out] == [["김민준"], [], [], ["이서연"]]
    assert [len(FakeStrata.segments(r["payload"])) for r in strata.chats] == [4, 2, 1, 1, 2]
    assert rec.failed_requests == 1 and len(rec.warnings) == 1
    assert "4부분 중 1부분" in rec.warnings[0]
    assert "규칙 기반" in rec.warnings[0] and "MAGIC" not in rec.warnings[0]


# ====================================================================== on_error


TEXT = "환자 설윤재(010-2468-1357) 내원. 보호자 설윤재 님과 통화."


def test_on_error_fail_propagates_and_file_handler_writes_nothing(strata, tmp_path, monkeypatch):
    from deid.formats import process_file
    from deid.formats.text import deid_text

    monkeypatch.setenv(ENV, WRONG_KEY)
    cfg = ner_cfg(strata)
    det = Detector(cfg)
    with pytest.raises(LLMError):
        det.detect(TEXT)
    with pytest.raises(LLMError):
        det.detect_document([TEXT, "메모"])
    with pytest.raises(LLMError):
        deid_text(TEXT, det, Anonymizer(cfg))
    src = tmp_path / "note.txt"
    src.write_text(TEXT, encoding="utf-8")
    dst = tmp_path / "out" / "note.deid.txt"
    dst.parent.mkdir()
    with pytest.raises(LLMError) as exc:
        process_file(src, dst, det, Anonymizer(cfg), cfg)
    assert not dst.exists() and list(dst.parent.iterdir()) == []
    assert secret_free(str(exc.value), extra=("설윤재", "2468"))


def test_on_error_rules_only_keeps_rules_and_warns(strata, tmp_path, monkeypatch):
    from deid import api
    from deid.formats import process_file

    monkeypatch.setenv(ENV, WRONG_KEY)
    cfg = ner_cfg(strata, on_error="rules_only")
    det = Detector(cfg)
    spans = det.detect(TEXT)
    assert {s.entity for s in spans} >= {E.PHONE}                  # rules still work
    warnings = det.drain_warnings()
    assert len(warnings) == 1 and "규칙 기반" in warnings[0] and "401" in warnings[0]
    assert det.warnings == [] and secret_free(*warnings, extra=("설윤재", "2468"))
    out, report = api.deidentify(TEXT, cfg, detector=det)
    assert "2468-1357" not in out and len(report.warnings) == 1
    src = tmp_path / "note.txt"
    src.write_text(TEXT, encoding="utf-8")
    rep = process_file(src, tmp_path / "note.deid.txt", det, Anonymizer(cfg), cfg)
    assert len(rep.warnings) == 1 and (tmp_path / "note.deid.txt").exists()
    assert det.warnings == []


def test_rules_only_unreachable_server_cools_down(closed_port_url, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    rec = OpenAIRecognizer(section(base_url=closed_port_url, on_error="rules_only",
                                   batch_chars=200))
    out = rec.find_batch(["가" * 150, "나" * 150, "다" * 150], default_config())
    assert out == [[], [], []]
    assert rec.request_count == 1 and rec.failed_requests == 1     # stopped after the first
    assert "3부분 중 3부분" in rec.warnings[0]
    rec.warnings.clear()
    assert rec.find("환자 김민준") == [] and rec.request_count == 1   # cool-down: not even tried
    assert len(rec.warnings) == 1


# ====================================================================== item handling


def test_non_literal_items_are_dropped_and_counted(strata):
    strata.table = {"김민준": ("이름", None)}
    strata.extra = [{"text": "최하은", "type": "이름"},              # not in the text
                    {"text": "[이름1]", "type": "이름"},             # placeholder
                    {"text": "박", "type": "이름"},                  # too short
                    {"text": "<<<1>>>", "type": "기타"}]
    rec = OpenAIRecognizer(section(strata))
    text = "보호자 김민준, [이름1] 환자와 박 선생 면담"
    spans = rec.find(text, default_config())
    assert [(s.entity, s.text) for s in spans] == [(E.PERSON, "김민준")]
    assert rec.dropped == 2 and rec.realigned == 0
    assert all(s.score == oc.LLM_SCORE and s.source == "openai" for s in spans)


def test_whitespace_only_differences_are_realigned(strata):
    text = "주소: 서울특별시 강남구\n테헤란로 152, 보호자 김 민준 연락함. 김 민준 님 재방문."
    strata.extra = [{"text": "서울특별시 강남구 테헤란로 152", "type": "주소"},
                    {"text": "김민준", "type": "이름"}]
    rec = OpenAIRecognizer(section(strata))
    spans = rec.find(text, default_config())
    got = [(s.entity, s.text) for s in spans]
    assert (E.ADDRESS, "서울특별시 강남구\n테헤란로 152") in got
    assert got.count((E.PERSON, "김 민준")) == 2 and rec.realigned == 2 and rec.dropped == 0
    assert all(text[s.start:s.end] == s.text for s in spans)


def test_type_labels_map_to_entities():
    for typ, ent in [("이름", E.PERSON), ("주소", E.ADDRESS), ("기관", E.ORG), ("전화", E.PHONE),
                     ("이메일", E.EMAIL), ("주민번호", E.RRN), ("외국인번호", E.FRN),
                     ("등록번호", E.MRN), ("계좌", E.ACCOUNT), ("카드", E.CARD),
                     ("차량번호", E.PLATE), ("여권", E.PASSPORT), ("면허", E.DRIVER_LICENSE),
                     ("사업자번호", E.BRN), ("보험번호", E.HEALTH_INSURANCE), ("IP", E.IP),
                     ("생년월일", E.BIRTHDATE), ("기타", E.OTHER), ("번호", E.OTHER)]:
        assert llm_type_entity(typ) == ent, typ
    assert all(llm_type_entity(t, default=None) for t in oc.TYPES)
    # every word a model may use for an organisation means ORG (kept by default)
    for typ in ("기관", "학교", "회사", "직장", "병원", "의원", "약국", "hospital", "clinic",
                "pharmacy", "organization", "ORG"):
        assert llm_type_entity(typ) == E.ORG, typ


def test_other_backends_keep_their_label_mapping():
    """GLiNER / HF / Ollama labels map as before the OpenAI-compatible backend: its type words
    (기관 -> ORG, 날짜 -> DATE, 전화 -> PHONE ...) apply to that backend only."""
    for typ in ("organization", "기관", "회사", "학교", "병원", "날짜", "진료일", "org"):
        assert entity_for(typ) == E.OTHER, typ
    assert entity_for("전화") == E.OTHER and entity_for("phone") == E.PHONE
    from deid.backends.gliner_backend import label_entities

    assert label_entities(["person", "organization", "회사"]) == {
        "person": E.PERSON, "organization": "ORGANIZATION", "회사": "회사"}


# ====================================================================== person groups


GROUP_TEXT = "환자 박서윤(45세) 내원. 서윤 씨는 어제부터 두통. 박모씨 보호자와 통화함."


def test_person_grouping_shares_one_pseudonym(strata):
    strata.table = {"박서윤": ("이름", "박서윤"), "서윤": ("이름", "박서윤"),
                    "박모씨": ("이름", "박서윤")}
    from deid.api import deidentify

    cfg = ner_cfg(strata)
    vault = Vault()
    out, _ = deidentify(GROUP_TEXT, cfg, vault, detector=Detector(cfg))
    assert "박서윤" not in out and "서윤" not in out and "박모씨" not in out
    assert out.count("[이름1]") == 3 and "[이름2]" not in out
    assert vault.restore("[이름1]") == "박서윤"         # the group's canonical spelling
    # later ungrouped occurrences (rules / another file) keep the same token via the alias
    assert vault.token_for(E.PERSON, "서윤", "이름") == "[이름1]"
    assert vault.token_for(E.PERSON, "박모씨", "이름") == "[이름1]"


def test_group_is_ignored_when_canonical_spelling_is_not_in_the_document(strata):
    text = "서윤 씨는 어제부터 두통. 박모씨 보호자와 통화함."
    strata.table = {"서윤": ("이름", "박서윤"), "박모씨": ("이름", "박서윤")}
    spans = OpenAIRecognizer(section(strata)).find(text, default_config())
    # ("씨" is an honorific: the name part of "박모씨" is "박모")
    assert [(s.text, s.group) for s in spans] == [("서윤", None), ("박모", None)]
    out = Anonymizer(default_config()).anonymize(text, spans).text
    assert out == "[이름1] 씨는 어제부터 두통. [이름2]씨 보호자와 통화함."


def test_group_spans_and_canonical_occurrences(strata):
    strata.table = {"서윤": ("이름", "박서윤")}     # the model did not list the full name itself
    spans = OpenAIRecognizer(section(strata)).find(GROUP_TEXT, default_config())
    got = [(s.text, s.group) for s in spans]
    assert ("서윤", "박서윤") in got and ("박서윤", None) in got


def test_vault_group_aliases_and_persistence(tmp_path):
    pytest.importorskip("cryptography")
    v = Vault()
    t1 = v.token_for(E.PERSON, "서윤", "이름", group="박서윤")
    assert t1 == "[이름1]" and v.tokens == {"[이름1]": "박서윤"}
    assert v.token_for(E.PERSON, "박서윤", "이름") == t1
    assert v.token_for(E.PERSON, "박 서윤", "이름") == t1                # spacing ignored
    assert v.token_for(E.PERSON, "박모씨", "이름", group="박서윤") == t1
    assert v.token_for(E.PERSON, "김민준", "이름") == "[이름2]"
    # an alias moves to a newer group; a value with its own token keeps it when ungrouped
    assert v.token_for(E.PERSON, "박모씨", "이름", group="박지훈") == "[이름3]"
    assert v.token_for(E.PERSON, "박모씨", "이름") == "[이름3]"
    assert v.token_for(E.PERSON, "김민준", "이름", group="김민준") == "[이름2]"
    assert v.restore("[이름1], [이름3]") == "박서윤, 박지훈"
    path = tmp_path / "v.vault"
    v.save(path, "test-pass-not-real")
    w = Vault.load(path, "test-pass-not-real")
    assert w.token_for(E.PERSON, "서윤", "이름") == t1 and w._aliases == v._aliases
    m = Vault()
    m.merge(v)
    assert m.token_for(E.PERSON, "박모씨", "이름") == "[이름3]"


class _Grouped:
    """Recognizer: "서윤" (before 씨) grouped under "박서윤"; the full name itself only when
    `full` (a model that listed it, or a rule that found it)."""
    name, entities = "grouped", (E.PERSON,)

    def __init__(self, full: bool = True) -> None:
        self.full = full

    def find(self, text, cfg):
        out = []
        i = text.find("서윤 씨")
        if i >= 0:
            out.append(Span(i, i + 2, E.PERSON, "서윤", 0.7, "grouped", "박서윤"))
        j = text.find("박서윤")
        if j >= 0 and self.full:
            out.append(Span(j, j + 3, E.PERSON, "박서윤", 0.7, "grouped"))
        return out


def test_propagated_copies_keep_the_group():
    det = Detector(default_config(), recognizers=[_Grouped()])
    chunks = ["환자 박서윤 내원", "서윤 씨 두통", "다음 날 서윤과 통화"]
    out = det.detect_document(chunks)
    found = [(s.text, s.group, s.source) for o in out for s in o]
    assert ("서윤", "박서윤", "grouped") in found and ("서윤", "박서윤", "propagate") in found


def test_group_needs_the_canonical_spelling_as_a_person_span():
    # nobody reported "박서윤" as a name: the group is dropped (it is no evidence of a name)
    det = Detector(default_config(), recognizers=[_Grouped(full=False)])
    out = det.detect_document(["환자 박서윤 내원", "서윤 씨 두통"])
    assert [(s.text, s.group) for o in out for s in o] == [("서윤", None)]


def test_ungrouped_spans_of_a_grouped_spelling_get_the_group():
    """One spelling, one pseudonym: "서윤" in a request without the full name (ungrouped) and
    "서윤" grouped under "박서윤" in another request share the token of "박서윤"."""
    class Split:
        name, entities = "split", (E.PERSON,)

        def find(self, text, cfg):
            out = []
            for value in ("서윤", "박서윤"):
                i = text.find(value + " ")
                while i >= 0:
                    grouped = value == "서윤" and "박서윤" in text
                    out.append(Span(i, i + len(value), E.PERSON, value, 0.7, "split",
                                    "박서윤" if grouped else None))
                    i = text.find(value + " ", i + 1)
            return [s for s in out if not (s.text == "서윤" and text[s.start - 1:s.start] == "박")]

    cfg = default_config()
    det = Detector(cfg, recognizers=[Split()])
    out = det.detect_document(["서윤 씨 두통 호소.", "환자 박서윤 내원. 서윤 씨 호전."])
    assert {(s.text, s.group) for o in out for s in o} == {("서윤", "박서윤"), ("박서윤", None)}
    vault = Vault()
    texts = ["서윤 씨 두통 호소.", "환자 박서윤 내원. 서윤 씨 호전."]
    results = [Anonymizer(cfg, vault).anonymize(t, spans).text for t, spans in zip(texts, out)]
    assert results == ["[이름1] 씨 두통 호소.", "환자 [이름1] 내원. [이름1] 씨 호전."]


# ====================================================================== ORG


def test_org_is_kept_by_default_and_redacted_when_configured(strata):
    assert E.ORG in E.ALL and E.label(E.ORG) == "기관" and E.DEFAULT_ACTIONS[E.ORG] == "keep"
    strata.table = {"행복내과의원": ("기관", None), "김민준": ("이름", None)}
    text = "보호자 김민준, 행복내과의원 의뢰서 지참."
    from deid.api import deidentify

    cfg = ner_cfg(strata)
    out, report = deidentify(text, cfg, detector=Detector(cfg))
    assert "행복내과의원" in out and "김민준" not in out and report.counts.get(E.ORG) == 1
    org = [s for s in Detector(cfg).detect(text) if s.entity == E.ORG]
    assert org and org[0].score == oc.KEEP_SCORE
    cfg = ner_cfg(strata, actions={"ORG": "redact"})
    out, _ = deidentify(text, cfg, detector=Detector(cfg))
    assert "행복내과의원" not in out and "[기관]" in out
    cfg = ner_cfg(strata, actions={"기관": "가명"})
    out, _ = deidentify(text, cfg, detector=Detector(cfg))
    assert "[기관1]" in out


# ====================================================================== review (deid check)


def test_review_reports_leftovers_and_ignores_placeholders(strata):
    reviewed = "[이름1] 환자(45세), 보호자 박지호 010-****-5678. 주소 [주소]. 담당의 김*준."
    strata.extra = [{"text": "박지호", "type": "이름", "reason": "보호자 이름"},
                    {"text": "[이름1]", "type": "이름", "reason": "placeholder"},
                    {"text": "010-****-5678", "type": "전화", "reason": "masked"},
                    {"text": "최하은", "type": "이름", "reason": "not there"}]
    items = oc.review(reviewed, dict(section(strata), _table="llm"))
    assert items == [{"text": "박지호", "entity": E.PERSON, "reason": "보호자 이름"}]
    user = strata.chats[0]["payload"]["messages"][1]["content"]
    assert "이미 비식별화된" in user and "[이름1]" in user and "reason" in user
    assert oc.review("   ", section(strata)) == [] and len(strata.chats) == 1


def test_review_failure_raises(strata, monkeypatch):
    monkeypatch.setenv(ENV, WRONG_KEY)
    with pytest.raises(LLMError) as exc:
        oc.review("[이름1] 보호자 박지호", section(strata))
    assert exc.value.status == 401 and "박지호" not in str(exc.value)


# ====================================================================== defaults unchanged


def test_without_backend_nothing_changes(monkeypatch):
    def no_network(*a, **kw):
        raise AssertionError("network used")

    monkeypatch.setattr(oc, "_open", no_network)
    from deid.recognizers.patterns import pattern_recognizers

    cfg = default_config()
    det = Detector(cfg)
    expected = [r.name for r in pattern_recognizers(cfg)] + ["address", "names", "names_en"]
    assert [r.name for r in det.recognizers] == expected
    assert not any(isinstance(r, OpenAIRecognizer) for r in det.recognizers)
    spans = det.detect_document(["환자 김민준 010-1234-5678", "보호자 김민준"])
    assert all(s.group is None for o in spans for s in o) and det.warnings == []
    cfg = config_from_dict({"llm": {"model": "gemma3:4b"}})
    assert [r.name for r in Detector(cfg).recognizers] == expected
    assert oc.llm_backend(cfg) == "ollama"
    assert Span(0, 2, E.PERSON, "서윤", 0.9, "x") == Span(0, 2, E.PERSON, "서윤", 0.9, "x", None)


def test_build_ner_recognizer_for_openai(strata):
    cfg = ner_cfg(strata, batch_chars=999)
    rec = build_ner_recognizer(cfg)
    assert isinstance(rec, OpenAIRecognizer) and rec.settings.batch_chars == 999
    assert KEY not in repr(rec) and KEY not in repr(rec._client) and KEY not in repr(rec.settings)
    assert strata.requests == []          # nothing is sent (or read from the Keychain) up front
