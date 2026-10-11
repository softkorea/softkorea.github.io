"""Vault: tokens, restore, merge, encrypted save/load."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from deid.anonymizer import Anonymizer
from deid.types import Span
from deid.vault import Vault, VaultError, normalize_value

PASS = "correct horse battery staple"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def filled_vault() -> Vault:
    v = Vault()
    v.token_for("PERSON", "김민준", "이름")
    v.token_for("PERSON", "이서연", "이름")
    v.token_for("MRN", "12345678", "등록번호")
    v.token_for("EMAIL", "minjun@example.com", "이메일")
    return v


def test_token_for_is_idempotent_and_numbered_per_entity():
    v = Vault()
    assert v.token_for("PERSON", "김민준", "이름") == "[이름1]"
    assert v.token_for("MRN", "12345678", "등록번호") == "[등록번호1]"
    assert v.token_for("PERSON", "이서연", "이름") == "[이름2]"
    assert v.token_for("PERSON", "김민준", "이름") == "[이름1]"
    assert len(v) == 3
    assert v.tokens["[이름2]"] == "이서연"


def test_same_text_different_entities_get_different_tokens():
    v = Vault()
    assert v.token_for("MRN", "12345678", "등록번호") != v.token_for("ACCOUNT", "12345678", "계좌번호")


@pytest.mark.parametrize("entity,a,b", [
    ("PHONE", "010-1234-5678", "010 1234 5678"),
    ("PHONE", "010.1234.5678", "01012345678"),
    ("MRN", "ab-1234", "AB1234"),
    ("EMAIL", "Minjun@Example.COM", "minjun@example.com"),
    ("PERSON", "John  Smith", "John Smith"),
    ("PERSON", "김 민준", "김민준"),          # spaced Korean name is the same person
])
def test_normalization_equal(entity, a, b):
    assert normalize_value(entity, a) == normalize_value(entity, b)


def test_normalization_keeps_distinct_names_distinct():
    assert normalize_value("PERSON", "김민준") != normalize_value("PERSON", "김민준호")
    assert normalize_value("PERSON", "John Smith") != normalize_value("PERSON", "JohnSmith")
    assert normalize_value("PERSON", "John") != normalize_value("PERSON", "john")


def test_restore_round_trip_with_more_than_ten_tokens():
    names = ["김민준", "이서연", "박도윤", "최하은", "정지호", "강수아", "조예준", "윤지우",
             "장시우", "임서윤", "한지민", "오태윤"]
    text = " / ".join(f"{n} 환자" for n in names) + " / 다시 김민준"
    spans, pos = [], 0
    for n in names + ["김민준"]:
        s = text.index(n, pos)
        spans.append(Span(s, s + len(n), "PERSON", n, 0.9, "t"))
        pos = s + len(n)
    a = Anonymizer()
    out = a.anonymize(text, spans).text
    assert "[이름10]" in out and "[이름12]" in out and "김민준" not in out
    assert a.vault.restore(out) == text


def test_restore_leaves_unknown_brackets_alone():
    v = filled_vault()
    assert v.restore("[이름1]과 [이름9], [주민번호]") == "김민준과 [이름9], [주민번호]"
    assert Vault().restore("[이름1]") == "[이름1]"


def test_restore_after_new_tokens_added():
    v = Vault()
    v.token_for("PERSON", "김민준", "이름")
    assert v.restore("[이름1]") == "김민준"
    v.token_for("PERSON", "이서연", "이름")
    assert v.restore("[이름1] [이름2]") == "김민준 이서연"


# ---------------------------------------------------------------------- merge


def test_merge_disjoint_and_overlapping_vaults():
    a = Vault()
    a.token_for("PERSON", "김민준", "이름")
    b = Vault()
    b.token_for("PERSON", "김민준", "이름")
    b.token_for("MRN", "12345678", "등록번호")
    a.merge(b)
    assert len(a) == 2
    assert a.restore("[이름1] [등록번호1]") == "김민준 12345678"
    assert a.token_for("MRN", "12345678", "등록번호") == "[등록번호1]"
    assert a.token_for("MRN", "87654321", "등록번호") == "[등록번호2]"


def test_merge_conflict_raises_and_leaves_vault_unchanged():
    a, b = Vault(), Vault()
    a.token_for("PERSON", "김민준", "이름")
    b.token_for("PERSON", "이서연", "이름")
    b.token_for("MRN", "12345678", "등록번호")
    with pytest.raises(VaultError):
        a.merge(b)
    assert len(a) == 1 and a.restore("[등록번호1]") == "[등록번호1]"


# ---------------------------------------------------------------------- save / load


def test_save_load_round_trip_and_numbering_continues(tmp_path):
    p = tmp_path / "vault.json"
    filled_vault().save(p, PASS)
    v = Vault.load(p, PASS)
    assert len(v) == 4
    assert v.restore("[이름2] [등록번호1] [이메일1]") == "이서연 12345678 minjun@example.com"
    assert v.token_for("EMAIL", "MINJUN@example.com", "이메일") == "[이메일1]"
    assert v.token_for("PERSON", "홍길동", "이름") == "[이름3]"


def test_saved_file_format_hides_plaintext(tmp_path):
    p = tmp_path / "vault.json"
    filled_vault().save(p, PASS)
    raw = p.read_bytes()
    blob = json.loads(raw)
    assert set(blob) == {"v", "salt", "data"} and blob["v"] == 1
    for secret in ("김민준".encode(), "12345678".encode(), b"minjun", "이름".encode()):
        assert secret not in raw


def test_each_save_uses_a_fresh_salt(tmp_path):
    v = filled_vault()
    v.save(tmp_path / "a.json", PASS)
    v.save(tmp_path / "b.json", PASS)
    a, b = (json.loads((tmp_path / n).read_text()) for n in ("a.json", "b.json"))
    assert a["salt"] != b["salt"] and a["data"] != b["data"]


@posix_only
def test_saved_file_is_0600_even_when_overwriting(tmp_path):
    p = tmp_path / "vault.json"
    p.write_text("old")
    os.chmod(p, 0o644)
    filled_vault().save(p, PASS)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [f.name for f in tmp_path.iterdir()] == ["vault.json"]  # no temp file left behind


def test_wrong_passphrase_raises_vault_error(tmp_path):
    p = tmp_path / "vault.json"
    filled_vault().save(p, PASS)
    with pytest.raises(VaultError, match="passphrase"):
        Vault.load(p, "wrong passphrase")


@pytest.mark.parametrize("content", [
    b"not json",
    b"[]",
    b'{"v": 1}',
    b'{"v": 1, "salt": "!!", "data": "x"}',
])
def test_damaged_file_raises_vault_error(tmp_path, content):
    p = tmp_path / "vault.json"
    p.write_bytes(content)
    with pytest.raises(VaultError):
        Vault.load(p, PASS)


def test_tampered_data_raises_vault_error(tmp_path):
    p = tmp_path / "vault.json"
    filled_vault().save(p, PASS)
    blob = json.loads(p.read_text())
    data, i = blob["data"], len(blob["data"]) // 2
    blob["data"] = data[:i] + ("B" if data[i] == "A" else "A") + data[i + 1:]
    p.write_text(json.dumps(blob))
    with pytest.raises(VaultError):
        Vault.load(p, PASS)


def test_unsupported_version_raises(tmp_path):
    p = tmp_path / "vault.json"
    filled_vault().save(p, PASS)
    blob = json.loads(p.read_text())
    blob["v"] = 2
    p.write_text(json.dumps(blob))
    with pytest.raises(VaultError, match="version"):
        Vault.load(p, PASS)


def test_empty_passphrase_rejected(tmp_path):
    with pytest.raises(ValueError):
        filled_vault().save(tmp_path / "v.json", "")


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        Vault.load(tmp_path / "nope.json", PASS)
