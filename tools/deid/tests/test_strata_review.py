"""Regression tests for the security / correctness review of the OpenAI-compatible ("Strata")
backend (2026-10). Every person, number and key here is invented; keys are obviously fake.

Each test names the review finding it covers (SEC-n security, Cn correctness).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from deid import cli
from deid import entities as E
from deid.anonymizer import Anonymizer
from deid.api import deidentify
from deid.backends import build_ner_recognizer
from deid.backends import ollama
from deid.backends import openai_compat as oc
from deid.backends.openai_compat import LLMError, OpenAIRecognizer
from deid.config import Config, config_from_dict, default_config
from deid.engine import Detector
from deid.vault import Vault

from test_cli_strata import (NAME, NOTE, closed_url, fake_mac, isolated,  # noqa: F401 - fixtures
                             ner_config, no_secret, set_stdin, strata, use_config)
from test_openai_backend import ENV, KEY, FakeStrata, section

ROOT = Path(__file__).resolve().parents[1]


def completion(content) -> dict:
    return {"id": "x", "object": "chat.completion", "model": "strata",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


def items_json(*items: dict) -> str:
    return json.dumps({"items": list(items)}, ensure_ascii=False)


# ====================================================================== SEC-1 key as a name

PASTED = ["test_key_not_real_Q2xkfP0aZr8VbNcT7yLm", "test-key-not-real.Q2xkfP0aZr8VbNcT7yLmW3eD",
          "Q2x_kfP0aZr8VbNcT7yLmW3eD1sJhUoXiGnKqRtYvB4", "9f86d081884c7d659a2feaa0c55ad015",
          "strata_live_9f86d081884c7d659a2f", "Ab3dEf9hIjK2LmN4oPq7",
          "123e4567-e89b-12d3-a456-426614174000", "abcdefghijklmnopqrstuvwxyzabcdefghij",
          "TESTKEYNOTREAL2K9X7Q4M", "sk-test-not-real-0000"]


@pytest.mark.parametrize("value", PASTED)
@pytest.mark.parametrize("field", ["api_key_env", "api_key_keychain"])
def test_key_pasted_as_a_name_is_refused_without_echo(field, value):
    with pytest.raises(ValueError) as exc:
        config_from_dict({"ner": section(**{"api_key_env": None, field: value})})
    assert value not in str(exc.value) and field in str(exc.value)


@pytest.mark.parametrize("kw", [{"api_key_env": "STRATA_API_KEY"}, {"api_key_env": "X_KEY"},
                                {"api_key_env": "DEID_STRATA_KEY"},
                                {"api_key_env": "CLINIC1_STRATA2_KEY"},
                                {"api_key_keychain": "deid-strata"},
                                {"api_key_keychain": "clinic1.strata_2026"}])
def test_ordinary_names_are_accepted(kw):
    cfg = config_from_dict({"ner": section(**dict({"api_key_env": None}, **kw))})
    conf = oc.settings(oc.ner_section(cfg))
    assert (conf.api_key_env, conf.api_key_keychain) == (kw.get("api_key_env"),
                                                         kw.get("api_key_keychain"))


def test_pasted_key_never_reaches_messages_or_security_argv(tmp_path, monkeypatch, capsys,
                                                           strata, fake_mac):
    kc = "test-key-not-real.Q2xkfP0aZr8VbNcT7yLmW3eD"
    env = "test_key_not_real_Q2xkfP0aZr8VbNcT7yLm"
    for key_line in (f'api_key_keychain = "{kc}"', f'api_key_env = "{env}"'):
        use_config(tmp_path, monkeypatch, f'[ner]\nbackend = "openai"\nbase_url = "{strata.url}/v1"\n'
                                          f"{key_line}\n")
        for argv in (["text"], ["doctor"]):
            set_stdin(monkeypatch, NOTE)
            rc = cli.main(argv)
            out, err = capsys.readouterr()
            assert rc == 2 and kc not in out + err and env not in out + err
    assert not any(kc in a or env in a for call in fake_mac.calls for a in call)
    assert strata.requests == []


def test_settings_built_by_hand_get_the_same_name_checks(monkeypatch):
    conf = oc.Settings(table="ner", base_url="http://127.0.0.1:9/v1",
                       api_key_env="test_key_not_real_Q2xkfP0aZr8VbNcT7yLm")
    with pytest.raises(LLMError) as exc:
        oc.resolve_api_key(conf)
    assert exc.value.kind == "key" and "Q2xkfP0aZr8VbNcT7yLm" not in str(exc.value)


# ====================================================================== SEC-2 deid's own secrets


@pytest.mark.parametrize("kw", [{"api_key_keychain": "deid-session"},
                                {"api_key_env": "DEID_VAULT_PASS"}, {"api_key_env": "DEID_HOME"},
                                {"api_key_env": "DEID_VAULT_KEY"},
                                {"api_key_env": "DEID_SESSION_HOURS"}])
def test_deid_own_secrets_are_never_a_key_source(kw):
    with pytest.raises(ValueError, match="deid"):
        config_from_dict({"ner": section(**dict({"api_key_env": None}, **kw))})
    with pytest.raises(ValueError):
        config_from_dict({"llm": section(**dict({"api_key_env": None}, **kw))})


def test_session_key_and_vault_passphrase_are_not_sent(tmp_path, monkeypatch, capsys, fake_mac):
    srv = FakeStrata(key=None)
    try:
        fake_mac.keychain[("deid", "deid-session")] = "test-session-key-not-real"
        monkeypatch.setenv("DEID_VAULT_PASS", "test-vault-pass-not-real")
        for key_line in ('api_key_keychain = "deid-session"', 'api_key_env = "DEID_VAULT_PASS"'):
            use_config(tmp_path, monkeypatch,
                       f'[ner]\nbackend = "openai"\nbase_url = "{srv.url}/v1"\n{key_line}\n')
            set_stdin(monkeypatch, NOTE)
            assert cli.main(["text"]) == 2
            out, err = capsys.readouterr()
            assert "not-real" not in out + err
        assert srv.requests == []
        assert not any("deid-session" in a for call in fake_mac.calls for a in call)
    finally:
        srv.close()


# ====================================================================== SEC-5 key for another server


def test_ner_borrows_the_llm_key_only_for_the_same_server():
    llm = {"backend": "openai", "base_url": "http://127.0.0.1:8080/v1", "api_key_env": "A_KEY",
           "api_key_keychain": "deid-a"}
    other = oc.merge_ner({"backend": "openai", "base_url": "http://127.0.0.1:9090/v1"}, llm)
    assert "api_key_env" not in other and "api_key_keychain" not in other
    same = oc.merge_ner({"backend": "openai", "base_url": "http://127.0.0.1:8080/v1/"}, llm)
    assert same["api_key_env"] == "A_KEY" and same["api_key_keychain"] == "deid-a"
    borrowed = oc.merge_ner({"backend": "openai"}, llm)
    assert borrowed["base_url"] == llm["base_url"] and borrowed["api_key_env"] == "A_KEY"
    default_port = oc.merge_ner({"backend": "openai", "base_url": "http://localhost/v1"},
                                dict(llm, base_url="http://LOCALHOST:80/v1"))
    assert default_port["api_key_env"] == "A_KEY"


def test_other_ner_server_does_not_get_the_llm_key(tmp_path, monkeypatch, capsys):
    a, b = FakeStrata(key="test-key-for-a-not-real"), FakeStrata(key=None)
    try:
        monkeypatch.setenv("A_KEY", "test-key-for-a-not-real")
        use_config(tmp_path, monkeypatch,
                   f'[llm]\nbackend = "openai"\nbase_url = "{a.url}/v1"\napi_key_env = "A_KEY"\n'
                   f'[ner]\nbackend = "openai"\nbase_url = "{b.url}/v1"\n')
        set_stdin(monkeypatch, NOTE)
        assert cli.main(["text"]) == 0
        assert b.chats and all("authorization" not in r["headers"] for r in b.requests)
    finally:
        a.close()
        b.close()


# ====================================================================== SEC-3 server by name


@pytest.mark.parametrize("url", ["http://CLINIC1:8080/v1", "http://clinic1.local:8080/v1",
                                 "http://clinic1.example:8080/v1", "http://192.168.1:8080/v1"])
def test_remote_http_server_must_be_an_ip_address(url):
    for table in ("ner", "llm"):
        with pytest.raises(ValueError, match="IP 주소") as exc:
            config_from_dict({table: section(base_url=url, allow_remote=True)})
        assert "ipconfig" in str(exc.value)


def test_ip_addresses_https_names_and_this_computer_are_accepted():
    for url in ("http://192.168.0.20:8080/v1", "http://[fe80::1]:8080/v1",
                "https://clinic1.example/v1", "http://localhost:8080/v1"):
        assert config_from_dict({"ner": section(base_url=url, allow_remote=True)})
    assert oc.is_ip_literal("10.0.0.1") and oc.is_ip_literal("[::1]")
    assert not oc.is_ip_literal("CLINIC1") and not oc.is_ip_literal("3232235530")


# ====================================================================== SEC-4 huge answers


def test_huge_item_is_dropped_quickly(strata):
    big = "가나다라마바사아자차카타파하" * 8000                     # 112,000 chars, no spaces
    strata.replies = [items_json({"text": big, "type": "이름"})]
    rec = OpenAIRecognizer(section(strata, timeout=5))
    t = time.monotonic()
    assert rec.find("어제 오정애 와서 상담함. 혈압 128/82\n") == []
    assert time.monotonic() - t < 3 and rec.dropped == 1


def test_oversized_body_and_too_many_items_are_llm_errors(strata, monkeypatch):
    monkeypatch.setattr(oc, "MAX_RESPONSE_BYTES", 50_000)
    strata.replies = [(200, json.dumps(completion("x" * 60_000)).encode())]
    with pytest.raises(LLMError) as exc:
        OpenAIRecognizer(section(strata)).find("환자 오정애 상담")
    assert exc.value.kind == "bad_response" and "너무 큽니다" in str(exc.value)
    many = items_json(*[{"text": "오정애", "type": "이름"}] * (oc.MAX_ITEMS + 1))
    strata.replies = [many]
    with pytest.raises(LLMError) as exc:
        OpenAIRecognizer(section(strata)).find("환자 오정애 상담")
    assert exc.value.kind == "bad_response"


def test_answer_parsing_is_linear_time():
    t = time.monotonic()
    assert not ollama.is_deidentified("가" * 300_000)
    assert ollama.is_deidentified("김*준 " * 20_000)
    ollama._strip_think("<think>" * 300_000)
    assert ollama._unfence("```" + "a" * 1_000_000) is None
    oc.realign("가 나" * 2000, ["가나" * 2000])
    assert time.monotonic() - t < 3


def test_long_person_value_is_ignored(strata):
    strata.replies = [items_json({"text": "오정애", "type": "이름", "person": "(" * 100_000})]
    spans = OpenAIRecognizer(section(strata)).find("환자 오정애 상담")
    assert [(s.text, s.group) for s in spans] == [("오정애", None)]


# ====================================================================== SEC-6 control sequences


def test_check_output_has_no_terminal_control_sequences(capsys):
    items = [{"text": "오정애\x1b[2K", "entity": E.PERSON,
              "reason": "\x1b[1A\x1b[2K\x1b]52;c;cm0gLXJmIH4K\x07looks fine‮"}]
    assert cli._show_check(items, "어제 오정애 와서", default_config(), "strata") == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "‮" not in out
    assert "오정애" in out and "looks fine" in out


# ====================================================================== SEC-7 / C9 malformed answers


@pytest.mark.parametrize("body", [
    completion([{"type": "text", "text": 5}]),
    completion([{"type": "text", "text": None}, 5]),
    completion([{"type": "text", "text": ["nested"]}]),
    b"[" * 100_000 + b"]" * 100_000,
    completion("[" * 100_000),
])
def test_malformed_answers_are_llm_errors(strata, body):
    strata.replies = [(200, body if isinstance(body, bytes) else json.dumps(body).encode())]
    with pytest.raises(LLMError):
        OpenAIRecognizer(section(strata)).find("환자 오정애 상담")


def test_content_parts_with_text_are_joined(strata):
    strata.replies = [(200, json.dumps(completion(
        [{"type": "reasoning"}, {"type": "text", "text": items_json({"text": "오정애", "type": "이름"})}]),
        ensure_ascii=False).encode())]
    assert [s.text for s in OpenAIRecognizer(section(strata)).find("환자 오정애 상담")] == ["오정애"]


def test_malformed_answer_falls_back_with_rules_only(tmp_path, monkeypatch, capsys, strata):
    ner_config(tmp_path, monkeypatch, strata, on_error="rules_only")
    strata.replies = [(200, json.dumps(completion([{"type": "text", "text": 5}])).encode())]
    set_stdin(monkeypatch, "환자명: 김하늘\n" + NOTE)
    assert cli.main(["text"]) == 0
    out, err = capsys.readouterr()
    assert "김하늘" not in out and "규칙 기반 탐지만" in err and "Traceback" not in err


# ====================================================================== SEC-8 ollama [ner]


def test_ollama_ner_does_not_take_allow_remote_from_llm():
    ner = {"backend": "ollama", "model": "gemma3:4b", "host": "http://192.168.0.20:11434"}
    with pytest.raises(ValueError, match="non-local"):
        build_ner_recognizer(Config(ner=dict(ner), llm={"allow_remote": True}))
    rec = build_ner_recognizer(Config(ner=dict(ner, allow_remote=True)))
    assert rec.allow_remote


# ====================================================================== SEC-9 / C5 / SEC-10 config


def test_columns_titled_like_secrets_load_as_before():
    cfg = config_from_dict({"columns": {"password": "OTHER", "Token": "skip", "key": "MRN",
                                        "Secret": "text", "API Key": "PERSON"},
                            "actions": {"OTHER": "redact"}})
    assert cfg.columns == {"password": "OTHER", "Token": "skip", "key": "MRN", "Secret": "text",
                           "API Key": "PERSON"}
    cfg = config_from_dict({"ner": {"backend": "hf", "model": "m",
                                    "label_map": {"PASSWORD": "OTHER", "KEY": ""}}})
    assert cfg.ner["label_map"] == {"PASSWORD": "OTHER", "KEY": ""}


@pytest.mark.parametrize("data,where", [
    ({"columns": {"api_key": "sk-test-not-real-0000"}}, "[columns]"),
    ({"columns": {"Token": "test-key-not-real-1234"}}, "[columns]"),
    ({"actions": {"token": "test-key-not-real-1234"}}, "[actions]"),
    ({"labels": {"api_key": "test-key-not-real-1234"}}, "[labels]"),
    ({"ner": {"backend": "hf", "model": "m", "label_map": {"api_key": "test-key-not-real-1"}}},
     "[ner.label_map]"),
])
def test_secret_appended_under_a_data_table_is_refused_without_echo(data, where):
    with pytest.raises(ValueError) as exc:
        config_from_dict(data)
    msg = str(exc.value)
    assert where in msg and "api_key_env" in msg and "not-real" not in msg


@pytest.mark.parametrize("name", ["strata_key", "x_api_key", "access_key", "private_key",
                                  "api_key_value", "passphrase", "credentials", "openai_key",
                                  "STRATA-KEY", "auth_header", "bearer_value"])
def test_secret_name_variants_are_refused(name):
    for data in ({"ner": {"backend": "openai", name: "test-key-not-real-1234"}},
                 {"llm": {name: "test-key-not-real-1234"}},
                 {name: "test-key-not-real-1234"},
                 {"llm": {"extra": {name: "test-key-not-real-1234"}}}):
        with pytest.raises(ValueError) as exc:
            config_from_dict(data)
        assert "not-real" not in str(exc.value) and "api_key_env" in str(exc.value)


def test_known_keys_with_secret_like_words_still_load():
    cfg = config_from_dict({"ner": section(max_tokens=3000, api_key_keychain="deid-strata")})
    assert cfg.ner["max_tokens"] == 3000


# ====================================================================== C1 one spelling, one token


def test_given_name_in_a_request_without_the_full_name_gets_the_same_token(strata):
    filler = "".join(f"{i}번째 줄: 혈압 측정, 특이사항 없음.\n" for i in range(12))
    text = "서윤 씨 두통 호소.\n" + filler + "환자 박서윤 내원. 서윤 씨 호전.\n"
    strata.replies = [
        items_json({"text": "서윤", "type": "이름"}),                   # request 1: no full name
        items_json({"text": "박서윤", "type": "이름", "person": "박서윤"},
                   {"text": "서윤", "type": "이름", "person": "박서윤"}),
    ]
    cfg = config_from_dict({"ner": section(strata, batch_chars=200)})
    out, _ = deidentify(text, cfg, Vault(), detector=Detector(cfg))
    assert len(strata.chats) == 2 and "서윤" not in out
    assert out.splitlines()[0] == "[이름1] 씨 두통 호소." and "[이름2]" not in out
    assert out.splitlines()[-1] == "환자 [이름1] 내원. [이름1] 씨 호전."


# ====================================================================== C2 vault alias as canonical


def test_canonical_spelling_that_is_an_alias_keeps_its_token(tmp_path):
    v = Vault()
    assert v.token_for(E.PERSON, "박서윤", "이름") == "[이름1]"
    assert v.token_for(E.PERSON, "서윤", "이름", group="박서윤") == "[이름1]"
    assert v.token_for(E.PERSON, "박모씨", "이름", group="서윤") == "[이름1]"
    assert v.token_for(E.PERSON, "서윤", "이름") == "[이름1]"
    assert v.restore("[이름1]") == "박서윤"
    path = tmp_path / "v.deid"
    v.save(path, "test-pass-not-real")
    again = Vault.load(path, "test-pass-not-real")
    assert again.token_for(E.PERSON, "서윤", "이름") == "[이름1]"
    assert again.token_for(E.PERSON, "박모씨", "이름") == "[이름1]"


# ====================================================================== C3 visit dates


@pytest.mark.parametrize("typ", ["생년월일", "기타"])
def test_rule_visit_date_is_kept_when_the_llm_mislabels_it(strata, typ):
    text = "2026-10-15 내원. 혈압 128/82. 2026-10-29 진료 예정."
    strata.replies = [items_json({"text": "2026-10-15", "type": typ},
                                 {"text": "2026-10-29", "type": typ})]
    cfg = config_from_dict({"ner": section(strata)})
    out, _ = deidentify(text, cfg, Vault(), detector=Detector(cfg))
    assert out == text


def test_birthdate_with_context_is_still_generalised(strata):
    text = "생년월일 1985-03-02, 2026-10-15 내원."
    strata.replies = [items_json({"text": "1985-03-02", "type": "생년월일"})]
    cfg = config_from_dict({"ner": section(strata)})
    out, _ = deidentify(text, cfg, Vault(), detector=Detector(cfg))
    assert "1985-03-02" not in out and "2026-10-15 내원" in out


def test_equal_range_keep_span_wins_only_with_a_higher_score():
    from deid.engine import resolve_overlaps
    from deid.types import Span

    date = Span(0, 10, E.DATE, "2026-10-15", 0.85, "date")
    llm = Span(0, 10, E.BIRTHDATE, "2026-10-15", 0.7, "openai")
    keep = lambda ent: ent == E.DATE  # noqa: E731
    assert resolve_overlaps([date, llm], keep) == [date]
    strong = Span(0, 10, E.BIRTHDATE, "2026-10-15", 0.9, "date")
    assert resolve_overlaps([date, strong], keep) == [strong]
    url = Span(0, 30, E.URL, "https://x.example/p?t=01023456", 0.9, "url")
    inner = Span(22, 30, E.PHONE, "01023456", 0.6, "phone")
    assert resolve_overlaps([url, inner], lambda e: e == E.URL) == [inner]


# ====================================================================== C6 notifications


def test_rules_only_fallback_shows_in_notifications(tmp_path, monkeypatch, capsys, fake_mac):
    monkeypatch.setenv(ENV, KEY)
    ner_config(tmp_path, monkeypatch, closed_url(), on_error="rules_only")
    fake_mac.clipboard = NOTE.encode()
    assert cli.main(["clip", "--notify"]) == 0
    assert "LLM 탐지 실패: 규칙만 적용" in fake_mac.notifications[-1][1]
    (tmp_path / "a.txt").write_text(NOTE, encoding="utf-8")
    assert cli.main(["file", "--notify", "a.txt"]) == 0
    assert "LLM 탐지 실패: 규칙만 적용" in fake_mac.notifications[-1][1]
    assert NAME not in "".join(t + m for t, m in fake_mac.notifications)


def test_no_fallback_note_when_the_server_answered(tmp_path, monkeypatch, capsys, strata,
                                                   fake_mac):
    ner_config(tmp_path, monkeypatch, strata, on_error="rules_only")
    fake_mac.clipboard = NOTE.encode()
    assert cli.main(["clip", "--notify"]) == 0
    assert "LLM" not in fake_mac.notifications[-1][1]


# ====================================================================== C7 role words


def test_relation_word_written_as_person_is_not_a_name(strata):
    text = "보호자 오정애(어머니)와 함께 옴. 정애 씨 걱정 많음.\n어머니께 검사 결과 설명함."
    strata.replies = [items_json({"text": "오정애", "type": "이름", "person": "어머니"},
                                 {"text": "정애", "type": "이름", "person": "어머니"})]
    cfg = config_from_dict({"ner": section(strata)})
    vault = Vault()
    out, _ = deidentify(text, cfg, vault, detector=Detector(cfg))
    assert "오정애" not in out and "정애 씨" not in out
    assert out.count("어머니") == 2                      # the ordinary word stays
    tok = re.search(r"\[이름\d+\]", out).group()
    assert vault.restore(tok) == "오정애"


def test_canonical_spelling_sharing_a_syllable_is_still_masked(strata):
    # the model grouped "서윤" under "박서윤" but did not list the full name itself
    strata.replies = [items_json({"text": "서윤", "type": "이름", "person": "박서윤"})]
    cfg = config_from_dict({"ner": section(strata)})
    out, _ = deidentify("환자 박서윤 내원. 서윤 씨 두통.", cfg, Vault(), detector=Detector(cfg))
    assert out == "환자 [이름1] 내원. [이름1] 씨 두통."


# ====================================================================== C10 README


def test_readme_toml_examples_quote_hangul_keys():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not re.findall(r'(?<!["\w])[가-힣]+\s*=\s*"', readme)
    p = Path(__file__).parent / "_hangul_key.toml"
    try:
        p.write_text('[actions]\n"기관" = "가명"\n', encoding="utf-8")
        from deid.config import load_config

        assert load_config(p).action_for(E.ORG) == "pseudo"
    finally:
        p.unlink(missing_ok=True)


def test_score_values_spurious_per_doc_is_rounded_once(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("score_values_r", ROOT / "bench" / "score_values.py")
    sv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sv)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(json.dumps({"id": "d1", "lang": "ko", "text": "환자명: 김민준 내원",
                                  "pii": [{"value": "김민준", "entity": "PERSON"}]},
                                 ensure_ascii=False) + "\n", encoding="utf-8")
    res = sv.run([str(corpus)], out=__import__("io").StringIO())
    meta = res.pop("meta")
    res["spurious"].update(regions=9, per_doc=round(9 / 95, 3))   # 0.095 once rounded
    res["docs"].update(scored=95, total=95)
    assert "9 (0.09/문서 doc" in sv.format_table(res, meta)
