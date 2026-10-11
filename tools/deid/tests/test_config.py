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


# ---------------------------------------------------------------------- validation (critic gap)


def _write(tmp_path, body, name="config.toml"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_gap_config_names_appended_below_ner_are_applied_and_reported(tmp_path):
    from deid import cli

    p = _write(tmp_path, cli.EXAMPLE_CONFIG + '\nnames = ["설윤재"]\n')
    assert C.load_config(p).names == ["설윤재"]
    report = C.validate_report(p)
    assert len(report) == 1 and "[ner]" in report[0] and "names" in report[0]
    # the same for a names list after a [[patterns]] block or inside [actions]
    p = _write(tmp_path, '[[patterns]]\nentity = "STAFF_ID"\nregex = "EMP-[0-9]{5}"\nnames = ["설윤재"]\n')
    assert C.load_config(p).names == ["설윤재"] and C.load_config(p).custom_patterns[0].entity == "STAFF_ID"
    assert "[[patterns]]" in C.validate_report(p)[0]
    p = _write(tmp_path, '[actions]\nnames = ["설윤재"]\n')
    assert C.load_config(p).names == ["설윤재"]


def test_gap_config_example_file_has_no_problems(tmp_path):
    from deid import cli

    assert C.validate_report(_write(tmp_path, cli.EXAMPLE_CONFIG)) == []


@pytest.mark.parametrize("body, needle, hint", [
    ('name = ["설윤재"]\n', "'name'", "'names'"),
    ('[actions]\nADRESS = "redact"\n', "'ADRESS'", "'ADDRESS'"),
    ('[labels]\nPERSN = "환자"\n', "'PERSN'", "'PERSON'"),
    ('disabled = ["URLL"]\n', "'URLL'", "'URL'"),
    ('enabled = ["PHONEE"]\n', "'PHONEE'", "'PHONE'"),
    ('[columns]\n"담당" = "PERSN"\n', "[columns]", "'PERSON'"),
    ('[action]\nPERSON = "mask"\n', "[action]", "'actions'"),
    ('[actions]\nPERSON = "masc"\n', "'masc'", "'mask'"),
])
def test_gap_config_unknown_names_raise_with_suggestion(tmp_path, body, needle, hint):
    p = _write(tmp_path, body)
    with pytest.raises(ValueError) as exc:
        C.load_config(p)
    msg = str(exc.value)
    assert needle in msg and hint in msg and "말씀하신" in msg
    assert C.validate_report(p) == [msg]


def test_gap_config_entity_names_are_case_insensitive(tmp_path):
    p = _write(tmp_path, 'disabled = ["url"]\n[actions]\nphone = "redact"\nDriver-License = "mask"\n'
                         '"이름" = "가명"\n[labels]\nmrn = "차트"\n')
    cfg = C.load_config(p)
    assert cfg.action_for("PHONE") == "redact" and cfg.action_for("DRIVER_LICENSE") == "mask"
    assert cfg.action_for("PERSON") == "pseudo"
    assert "phone" not in cfg.actions
    assert cfg.label_for("MRN") == "차트"
    assert not cfg.is_enabled("URL") and cfg.is_enabled("PERSON")
    assert C.validate_report(p) == []


def test_gap_config_columns_are_validated_and_normalized(tmp_path):
    p = _write(tmp_path, '[columns]\n"담당" = "person"\n"보호자 연락" = "전화번호"\n"약품" = "Skip"\n'
                         '"메모" = "TEXT"\n"사번" = "staff_id"\n[[patterns]]\nentity = "STAFF_ID"\nregex = "E[0-9]{5}"\n')
    cfg = C.load_config(p)
    assert cfg.columns == {"담당": "PERSON", "보호자 연락": "PHONE", "약품": "skip", "메모": "text",
                           "사번": "STAFF_ID"}
    from deid.formats.tabular import parse_column_kind
    assert [parse_column_kind(v) for v in cfg.columns.values()]   # all still readable there
    with pytest.raises(ValueError, match=r"\[columns\]"):
        C.load_config(_write(tmp_path, '[columns]\n"담당" = "NURSE?"\n'))


def test_gap_config_duplicate_table_has_a_korean_hint(tmp_path):
    from deid import cli

    p = _write(tmp_path, cli.EXAMPLE_CONFIG + '\n[actions]\nPERSON = "mask"\n')
    with pytest.raises(ValueError) as exc:
        C.load_config(p)
    assert "[actions]" in str(exc.value) and "두 번" in str(exc.value)


def test_gap_config_duplicate_table_builtin_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "tomllib", None)
    p = _write(tmp_path, '[actions]\nPERSON = "mask"\n[actions]\nPHONE = "redact"\n')
    with pytest.raises(ValueError) as exc:
        C.load_config(p)
    assert "[actions]" in str(exc.value) and "두 번" in str(exc.value)


def test_gap_config_names_in_actions_used_to_be_cryptic(tmp_path):
    p = _write(tmp_path, '[actions]\nPERSON = ["a", "b"]\n')
    with pytest.raises(ValueError, match="names"):
        C.load_config(p)


@pytest.mark.parametrize("body, needle", [
    ('[ocr]\nengin = "tesseract"\n', "'engine'"),
    ('[llm]\nmodle = "gemma3:4b"\n', "'model'"),
    ('[ner]\nbackedn = "hf"\n', "'backend'"),
    ('[[patterns]]\nentity = "PERSN"\nregex = "abc"\n', "'PERSON'"),
    ('[[patterns]]\nentity = "X"\nregex = "a"\npatern = "b"\n', "'patern'"),
    ('[ocr]\ndisabled = ["URL"]\n', "disabled"),
])
def test_gap_config_soft_problems_are_reported_not_raised(tmp_path, body, needle):
    p = _write(tmp_path, body)
    C.load_config(p)
    report = C.validate_report(p)
    assert len(report) == 1 and needle in report[0]


def test_gap_config_patterns_validation(tmp_path):
    with pytest.raises(ValueError, match="regex"):
        C.load_config(_write(tmp_path, '[[patterns]]\nentity = "X"\nregex = "("\n'))
    with pytest.raises(ValueError, match=r"\[\[patterns\]\]"):
        C.load_config(_write(tmp_path, '[patterns]\nentity = "X"\nregex = "a"\n'))
    with pytest.raises(ValueError, match="regex"):
        C.load_config(_write(tmp_path, '[[patterns]]\nentity = "X"\npattern = "a"\n'))
    cfg = C.load_config(_write(tmp_path, '[[patterns]]\nentity = "mrn"\nregex = "R[0-9]{6}"\n'))
    assert cfg.custom_patterns[0].entity == "MRN"


def test_gap_config_min_score_and_report_paths(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="min_score"):
        C.load_config(_write(tmp_path, "min_score = 2\n"))
    assert C.validate_report(tmp_path / "missing.toml")[0].startswith("설정 파일을 찾을 수 없습니다")
    monkeypatch.delenv("DEID_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert C.validate_report() == []                 # no config file at all: nothing to report
    monkeypatch.setenv("DEID_CONFIG", str(_write(tmp_path, '[actions]\nPHONE = "redact"\n', "env.toml")))
    assert C.validate_report() == []
    errors, warnings = C.validate(_write(tmp_path, 'names = ["a"\n'))
    assert errors and "문법" in errors[0] and warnings == []
