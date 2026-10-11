"""Config loading, including the built-in TOML reader used on Python 3.10 without tomli."""
from __future__ import annotations

import pytest

from deid import config as C


def test_mac_py310_tomli_missing_builtin_reader_matches_tomllib():
    tomllib = pytest.importorskip("tomllib")
    from deid import cli

    samples = [cli.EXAMPLE_CONFIG, '''
names = ["홍길동", 'Jane Doe',]  # comment
min_score = 0.6
disabled = ["DATE"]
[actions]
PERSON = "mask"
[columns]
"담당" = "PERSON"
[[patterns]]
entity = "STAFF_ID"
regex = 'EMP-[0-9]{6}\\d'
context = ["사번", "직원\\t번호"]
[[patterns]]
entity = "X"
regex = """a\\
   b"""
[ner]
label_map = {PER = "PERSON", "LOC" = "ADDRESS"}
max_chars = 1_000
x.y = true
f = 1e3
''']
    for text in samples:
        assert C.parse_toml(text) == tomllib.loads(text)


@pytest.mark.parametrize("bad", ['a = ', 'a = "x', '[a', 'a = 1 2', 'a = 1\na = 2'])
def test_mac_py310_tomli_missing_builtin_reader_rejects_bad_toml(bad):
    with pytest.raises(ValueError, match="line"):
        C.parse_toml(bad)


def test_mac_py310_tomli_missing_load_config_without_tomllib(tmp_path, monkeypatch):
    from deid import cli

    p = tmp_path / "config.toml"
    body = cli.EXAMPLE_CONFIG.replace('  # "홍길동",', '  "홍길동",').replace('# PHONE = "mask"', 'PHONE = "redact"')
    p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(C, "tomllib", None)
    cfg = C.load_config(p)
    assert cfg.names == ["홍길동"] and cfg.action_for("PHONE") == "redact"
