"""Tests for deid.ocr: box estimation, tesseract grouping, and the macOS (ocrmac) paths with a
fake ocrmac module. Real tesseract is used only where it is installed."""
from __future__ import annotations

import random
import sys
import types
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from deid import ocr
from deid.config import default_config
from deid.ocr import (
    OcrError, OcrLine, OcrWord, char_weight, group_tokens, line_from_box, lines_from_tesseract,
    ocr_image, prepare_for_tesseract, split_box, union_box,
)

FONT = Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
HAS_TESSERACT = "tesseract" in ocr.available_engines() and "kor" in ocr.tesseract_languages()
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract with kor not installed")
needs_font = pytest.mark.skipif(not FONT.exists(), reason="NanumGothic font not installed")


def cfg_with(**ocr_opts):
    cfg = default_config()
    cfg.ocr.update(ocr_opts)
    return cfg


# ---------------------------------------------------------------------- weights and boxes


def test_char_weights():
    assert char_weight("홍") == 1.0
    assert char_weight("漢") == 1.0
    assert char_weight("Ａ") == 1.0          # fullwidth
    assert char_weight("A") == char_weight("7") == 0.6
    assert char_weight("a") == 0.5
    assert char_weight("-") == char_weight(":") == 0.35
    assert char_weight(" ") == 0.0


def test_split_box_proportional_and_spaces_zero_width():
    boxes = split_box((0, 10, 100, 30), "홍a 1")
    assert len(boxes) == 4
    # layout weights: 1.0, 0.5, space 0.3, 0.6 -> total 2.4
    assert boxes[0] == pytest.approx((0, 10, 100 / 2.4, 30))
    assert boxes[1][2] == pytest.approx(150 / 2.4)
    assert boxes[2][0] == boxes[2][2]          # space: zero width
    assert boxes[3][2] == pytest.approx(100)
    assert all(b[1] == 10 and b[3] == 30 for b in boxes)


def test_split_box_edge_cases():
    assert split_box((0, 0, 10, 10), "") == []
    spaces = split_box((5, 0, 10, 10), "  ")
    assert len(spaces) == 2 and all(b[0] == b[2] for b in spaces)


def test_line_char_boxes_one_per_char_and_span_box():
    words = [OcrWord("환자명:", (10, 5, 80, 25), 90), OcrWord("홍길동", (90, 5, 150, 25), 90)]
    line = OcrLine.from_words(words, "test")
    assert line.text == "환자명: 홍길동"
    cb = line.char_boxes()
    assert len(cb) == len(line.text)
    sp = line.text.index(" ")
    assert cb[sp][0] == cb[sp][2]               # space is zero-width
    # the name maps exactly to its word box
    s = line.text.index("홍길동")
    assert line.span_box(s, s + 3) == pytest.approx((90, 5, 150, 25))
    # a single syllable gets a third of the word
    assert line.span_box(s + 1, s + 2) == pytest.approx((110, 5, 130, 25))
    assert line.height == 20


def test_word_with_exact_char_boxes_used_verbatim():
    chars = [(0, 0, 9, 10), (12, 0, 30, 10)]
    w = OcrWord("ab", (0, 0, 30, 10), chars=chars)
    assert w.char_boxes() == chars
    # wrong length -> estimated instead
    assert len(OcrWord("abc", (0, 0, 30, 10), chars=chars).char_boxes()) == 3


def test_line_text_not_matching_words_falls_back_to_line_estimate():
    line = OcrLine("abc def", [OcrWord("x", (0, 0, 5, 5))], (0, 0, 70, 10))
    assert len(line.char_boxes()) == 7


def test_union_box():
    assert union_box([]) is None
    assert union_box([(1, 2, 3, 4), (0, 5, 2, 9)]) == (0, 2, 3, 9)


# ---------------------------------------------------------------------- tesseract grouping


def _tess_dict(rows):
    keys = ("level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top",
            "width", "height", "conf", "text")
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


def test_lines_from_tesseract_groups_and_scales():
    rows = [
        (1, 1, 0, 0, 0, 0, 0, 0, 2000, 1000, -1, ""),          # page row
        (4, 1, 1, 1, 1, 0, 20, 20, 400, 40, -1, ""),           # line row
        (5, 1, 1, 1, 1, 1, 20, 20, 100, 40, 95.5, "환자명:"),
        (5, 1, 1, 1, 1, 2, 140, 20, 120, 40, "12", "홍길동"),     # low conf, kept
        (5, 1, 1, 1, 1, 3, 270, 20, 10, 40, 5, "  "),          # whitespace, dropped
        (5, 1, 1, 1, 2, 1, 20, 80, 200, 40, 91, "010-1234-5678"),
        (5, 1, 2, 1, 1, 1, 600, 20, 200, 40, 88, "metformin"),
        (5, 1, 2, 1, 1, 2, 820, 20, 200, 40, 88, "500 mg"),     # inner space -> two words
    ]
    lines, n_words, n_low = lines_from_tesseract(_tess_dict(rows), scale=2, min_conf=30)
    assert [ln.text for ln in lines] == ["환자명: 홍길동", "010-1234-5678", "metformin 500 mg"]
    assert n_words == 5 and n_low == 1
    first = lines[0]
    assert first.words[1].box == (70, 10, 130, 30)              # mapped back by /2
    assert first.box == (10, 10, 130, 30)
    assert first.words[1].conf == 12
    last = lines[2]
    assert [w.text for w in last.words] == ["metformin", "500", "mg"]
    assert last.words[1].box[0] == pytest.approx(410)
    assert last.words[2].box[2] == pytest.approx(510)
    assert len(last.char_boxes()) == len(last.text)


def test_lines_from_tesseract_empty():
    lines, n, low = lines_from_tesseract({"text": []})
    assert lines == [] and n == 0 and low == 0


def test_prepare_upscales_small_images_only():
    small = Image.new("RGB", (640, 360), "white")
    big = Image.new("RGB", (1600, 1200), "white")
    img, f = prepare_for_tesseract(small, {})
    assert f == 2 and img.size == (1280, 720) and img.mode == "L"
    img, f = prepare_for_tesseract(big, {})
    assert f == 1 and img.size == (1600, 1200)
    assert prepare_for_tesseract(small, {"upscale": False})[1] == 1
    assert prepare_for_tesseract(big, {"upscale": True})[1] == 2
    assert prepare_for_tesseract(big, {"upscale": 3})[1] == 3


def test_prepare_inverts_dark_mode_and_flattens_alpha():
    dark = Image.new("RGB", (50, 50), (20, 20, 20))
    img, _ = prepare_for_tesseract(dark, {"upscale": False})
    assert img.getpixel((5, 5)) > 200
    assert prepare_for_tesseract(dark, {"upscale": False, "invert": False})[0].getpixel((5, 5)) < 50
    # transparent pixels are usually black underneath: they must become white, not black
    rgba = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    img, _ = prepare_for_tesseract(rgba, {"upscale": False})
    assert img.getpixel((5, 5)) == 255


def test_invalid_engine_and_psm():
    img = Image.new("RGB", (10, 10), "white")
    with pytest.raises(OcrError, match="unknown OCR engine"):
        ocr_image(img, cfg_with(engine="paddle"))
    with pytest.raises(OcrError, match="psm"):
        ocr._psm_list(["x"])
    with pytest.raises(OcrError, match="psm"):
        ocr._psm_list(99)
    assert ocr._psm_list(None) == list(ocr.DEFAULT_PSM)
    assert ocr._psm_list([6, "6", 11]) == [6, 11]


FAKE_TESSERACT = """#!/bin/sh
case "$1" in
  --version) echo "tesseract 9.9.9"; exit 0 ;;
  --list-langs) echo 'List of available languages in "/fake/" (3):'; echo eng; echo kor; echo osd; exit 0 ;;
esac
bytes=$(wc -c | tr -d ' ')
echo "omp=$OMP_THREAD_LIMIT bytes=$bytes args=$*" >> "$DEID_FAKE_LOG"
if [ -n "$DEID_FAKE_FAIL" ]; then echo "Error opening data file kor.traineddata" >&2; exit 1; fi
if [ -n "$DEID_FAKE_SLEEP" ]; then sleep "$DEID_FAKE_SLEEP"; fi
printf 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
printf '1\t1\t0\t0\t0\t0\t0\t0\t200\t100\t-1\t\n'
printf '5\t1\t1\t1\t1\t1\t20\t20\t100\t40\t96.5\t홍길동\n'
printf '5\t1\t1\t1\t1\t2\t130\t20\t40\t40\t12\tsa\ty\n'
"""


@pytest.fixture
def fake_tesseract(tmp_path, monkeypatch):
    """A shell script standing in for the tesseract binary; returns the call log path."""
    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("needs a POSIX shell")
    exe = tmp_path / "tesseract"
    exe.write_text(FAKE_TESSERACT, encoding="utf-8")
    exe.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    monkeypatch.setenv("DEID_FAKE_LOG", str(log))
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    monkeypatch.setattr(ocr, "_find_tesseract", lambda: str(exe))
    return log


def test_tesseract_runs_binary_via_stdin_in_parallel_passes(fake_tesseract):
    img = Image.new("RGB", (100, 50), "white")
    res = ocr_image(img, cfg_with(engine="tesseract", psm=[4, 6, 11], upscale=False,
                                  lang="kor+eng"))
    calls = fake_tesseract.read_text().splitlines()
    assert len(calls) == 3
    assert all("omp=1 " in c for c in calls)                 # one thread per process
    assert all(" args=stdin stdout -l kor+eng --psm " in c and c.endswith(" tsv") for c in calls)
    assert sorted(c.split("--psm ")[1].split()[0] for c in calls) == ["11", "4", "6"]
    assert all(int(c.split("bytes=")[1].split()[0]) > 50 for c in calls)   # PNG on stdin
    assert [ln.engine for ln in res.lines] == ["tesseract/psm4", "tesseract/psm6",
                                               "tesseract/psm11"]
    line = res.lines[0]
    assert line.text == "홍길동 sa y"                          # tab inside text kept as a split
    assert line.words[0].box == (20, 20, 120, 60) and line.words[0].conf == 96.5


def test_explicit_tesseract_cmd(fake_tesseract, monkeypatch, tmp_path):
    exe = str(tmp_path / "tesseract")
    monkeypatch.setattr(ocr, "_find_tesseract", lambda: None)     # not on PATH
    res = ocr_image(Image.new("RGB", (40, 40)), cfg_with(engine="tesseract", psm=6,
                                                       tesseract_cmd=exe))
    assert res.lines and fake_tesseract.read_text()
    with pytest.raises(OcrError, match="not found"):
        ocr_image(Image.new("RGB", (40, 40)), cfg_with(engine="tesseract",
                                                       tesseract_cmd=str(tmp_path / "none")))


def test_tesseract_user_thread_limit_respected(fake_tesseract, monkeypatch):
    monkeypatch.setenv("OMP_THREAD_LIMIT", "3")
    ocr_image(Image.new("RGB", (40, 40)), cfg_with(engine="tesseract", psm=6))
    assert "omp=3 " in fake_tesseract.read_text()


def test_tesseract_failure_and_timeout_are_reported(fake_tesseract, monkeypatch):
    img = Image.new("RGB", (40, 40))
    monkeypatch.setenv("DEID_FAKE_FAIL", "1")
    with pytest.raises(OcrError, match="tesseract failed.*Error opening data file"):
        ocr_image(img, cfg_with(engine="tesseract", psm=6))
    monkeypatch.delenv("DEID_FAKE_FAIL")
    monkeypatch.setenv("DEID_FAKE_SLEEP", "5")
    with pytest.raises(OcrError, match="timed out"):
        ocr_image(img, cfg_with(engine="tesseract", psm=6, timeout=0.5))


def test_parse_tsv():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight"
           "\tconf\ttext\n5\t1\t2\t1\t3\t1\t10\t11\t12\t13\t87.25\t혈압\n\n"
           "5\t1\t2\t1\t3\t2\t30\t11\t5\t13\t-1\t\n")
    d = ocr.parse_tsv(tsv)
    assert d["text"] == ["혈압", ""] and d["left"] == [10, 30] and d["conf"] == [87.25, -1.0]
    assert d["block_num"] == [2, 2] and d["line_num"] == [3, 3]
    assert ocr.parse_tsv("") == {"text": []}


def test_missing_tesseract_gives_install_hint(monkeypatch):
    monkeypatch.setattr(ocr, "_find_tesseract", lambda: None)
    with pytest.raises(OcrError, match="brew install tesseract"):
        ocr_image(Image.new("RGB", (10, 10)), cfg_with(engine="tesseract"))
    assert ocr.tesseract_languages() == []
    assert ocr.engine_status()["tesseract"].startswith("missing")


def test_find_tesseract_without_pytesseract(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    exe = tmp_path / "tesseract"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert ocr._find_tesseract() == str(exe)
    # GUI launchers have a short PATH: fall back to Homebrew/MacPorts locations
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(ocr, "_TESSERACT_FALLBACKS", (str(tmp_path / "nope"), str(exe)))
    assert ocr._find_tesseract() == str(exe)


def test_missing_language_data_is_reported(monkeypatch):
    monkeypatch.setattr(ocr, "_find_tesseract", lambda: "/fake/tesseract")
    monkeypatch.setattr(ocr, "_tesseract_version", lambda cmd: "5.0")
    monkeypatch.setattr(ocr, "_tesseract_langs", lambda cmd: frozenset({"eng", "osd"}))
    with pytest.raises(OcrError, match="kor") as ei:
        ocr_image(Image.new("RGB", (10, 10)), cfg_with(engine="tesseract"))
    assert "tesseract-lang" in str(ei.value)
    assert ocr.engine_status()["tesseract"].startswith("incomplete")


def test_low_confidence_warning(monkeypatch):
    monkeypatch.setattr(ocr, "_check_tesseract", lambda lang, cmd=None: "tesseract")
    rows = [(5, 1, 1, 1, 1, i, 10 * i, 0, 8, 10, 10, f"w{i}") for i in range(6)]
    monkeypatch.setattr(ocr, "tesseract_data", lambda *a, **k: _tess_dict(rows))
    res = ocr_image(Image.new("RGB", (100, 20), "white"), cfg_with(engine="tesseract", psm=6))
    assert res.engine == "tesseract"
    assert res.low_conf_words == 6 and res.words == 6
    assert len(res.lines) == 1 and res.lines[0].text == "w0 w1 w2 w3 w4 w5"   # kept
    assert ocr.LOW_CONF_WARNING in res.warnings


# ---------------------------------------------------------------------- fake ocrmac


class _FakeOCR:
    """Stand-in for ocrmac.ocrmac.OCR returning canned results per framework."""

    results: dict = {}
    calls: list = []
    fail: set = set()

    def __init__(self, image, **kw):
        _FakeOCR.calls.append(kw)
        if kw.get("framework") in _FakeOCR.fail:
            raise RuntimeError("framework not available")
        self.image = image
        self.kw = kw

    def recognize(self, px=False):
        assert px is True, "deid must ask ocrmac for pixel coordinates"
        return list(_FakeOCR.results.get(self.kw.get("framework", "vision"), []))


@pytest.fixture
def fake_ocrmac(monkeypatch):
    pkg = types.ModuleType("ocrmac")
    mod = types.ModuleType("ocrmac.ocrmac")
    mod.OCR = _FakeOCR
    pkg.ocrmac = mod
    monkeypatch.setitem(sys.modules, "ocrmac", pkg)
    monkeypatch.setitem(sys.modules, "ocrmac.ocrmac", mod)
    _FakeOCR.results = {}
    _FakeOCR.calls = []
    _FakeOCR.fail = set()
    return _FakeOCR


def test_vision_engine_lines_and_estimated_boxes(fake_ocrmac):
    fake_ocrmac.results["vision"] = [
        ("연락처 010-1234-5678", 0.97, (100, 220, 400, 250)),
        ("환자명: 홍길동", 0.5, (10.0, 20.0, 210.0, 50.0)),
        ("   ", 0.1, (0, 0, 1, 1)),                       # blank: ignored
    ]
    res = ocr_image(Image.new("RGB", (640, 480), "white"), cfg_with(engine="vision"))
    kw = fake_ocrmac.calls[-1]
    assert kw["framework"] == "vision"
    assert kw["recognition_level"] == "accurate"
    assert kw["language_preference"] == ["ko-KR", "en-US"]
    assert res.engine == "vision"
    assert [ln.text for ln in res.lines] == ["환자명: 홍길동", "연락처 010-1234-5678"]  # top->bottom
    line = res.lines[0]
    assert line.estimated and line.box == (10.0, 20.0, 210.0, 50.0)
    assert line.words[1].conf == pytest.approx(50)            # 0-1 -> 0-100
    cb = line.char_boxes()
    assert len(cb) == len(line.text)
    # weights: 환자명 3.0, ':' 0.35, space 0.3, 홍길동 3.0 -> name starts at 3.65/6.65 of 200 px
    s = line.text.index("홍")
    box = line.span_box(s, s + 3)
    assert box[0] == pytest.approx(10 + 200 * 3.65 / 6.65)
    assert box[2] == pytest.approx(210)
    assert box[1] == 20 and box[3] == 50


def test_vision_custom_languages_and_level(fake_ocrmac):
    fake_ocrmac.results["vision"] = [("abc", 1.0, (0, 0, 30, 10))]
    ocr_image(Image.new("RGB", (64, 64)), cfg_with(engine="vision", languages=["en-US"],
                                                    recognition_level="fast"))
    kw = fake_ocrmac.calls[-1]
    assert kw["language_preference"] == ["en-US"] and kw["recognition_level"] == "fast"


def test_vision_passes_rgb_image(fake_ocrmac):
    fake_ocrmac.results["vision"] = []
    seen = {}
    orig = fake_ocrmac.__init__

    def spy(self, image, **kw):
        seen["mode"] = image.mode
        orig(self, image, **kw)
    fake_ocrmac.__init__ = spy
    try:
        ocr_image(Image.new("RGBA", (20, 20)), cfg_with(engine="vision"))
    finally:
        fake_ocrmac.__init__ = orig
    assert seen["mode"] == "RGB"


def test_malformed_ocrmac_results_fail_closed(fake_ocrmac):
    fake_ocrmac.results["vision"] = [("홍길동", 0.9)]                 # no box
    with pytest.raises(OcrError, match="unexpected ocrmac result"):
        ocr_image(Image.new("RGB", (64, 64)), cfg_with(engine="vision"))
    fake_ocrmac.results["vision"] = [("홍길동", 0.9, None)]
    with pytest.raises(OcrError, match="without a box"):
        ocr_image(Image.new("RGB", (64, 64)), cfg_with(engine="vision"))


def _char_tokens(text, x, y, cw=20, ch=24, space=8):
    """Character-level Live Text tokens for `text` starting at (x, y)."""
    out = []
    for c in text:
        if c == " ":
            x += space
            continue
        w = cw if ord(c) > 0x2000 else cw * 0.55
        out.append((c, 1.0, (x, y, x + w, y + ch)))
        x += w + 1
    return out


def test_livetext_groups_characters_into_lines(fake_ocrmac):
    tokens = (_char_tokens("환자명: 홍길동", 10, 10)
              + _char_tokens("보호자 김민준 님", 12, 60)
              + _char_tokens("metformin", 400, 13))           # same row, far right column
    random.Random(4).shuffle(tokens)
    fake_ocrmac.results["livetext"] = tokens
    res = ocr_image(Image.new("RGB", (800, 120), "white"), cfg_with(engine="livetext"))
    kw = fake_ocrmac.calls[-1]
    assert kw["framework"] == "livetext" and kw["unit"] == "token"
    assert "recognition_level" not in kw
    assert res.engine == "livetext"
    assert [ln.text for ln in res.lines] == ["환자명: 홍길동 metformin", "보호자 김민준 님"]
    line = res.lines[1]
    assert not line.estimated
    cb = line.char_boxes()
    assert len(cb) == len(line.text)
    s = line.text.index("김민준")
    # real character boxes: the name's union is exactly its three tokens
    exp = [t[2] for t in tokens if t[0] in "김민준"]
    assert line.span_box(s, s + 3) == pytest.approx(union_box(exp))


def test_livetext_multi_char_tokens_are_split(fake_ocrmac):
    fake_ocrmac.results["livetext"] = [("홍길", 1.0, (0, 0, 40, 20)), ("동", 1.0, (41, 0, 61, 20)),
                                       ("님", 1.0, (75, 0, 95, 20))]
    res = ocr_image(Image.new("RGB", (100, 30)), cfg_with(engine="livetext"))
    assert [ln.text for ln in res.lines] == ["홍길동 님"]
    cb = res.lines[0].char_boxes()
    assert cb[0] == pytest.approx((0, 0, 20, 20)) and cb[1] == pytest.approx((20, 0, 40, 20))


def test_group_tokens_space_threshold():
    toks = [("a", 1, (0, 0, 10, 20)), ("b", 1, (12, 0, 22, 20)), ("c", 1, (40, 0, 50, 20))]
    assert group_tokens(toks, gap_ratio=0.25)[0].text == "ab c"     # gaps 2 and 18 vs 5
    assert group_tokens(toks, gap_ratio=1.0)[0].text == "abc"


def test_line_from_box_words():
    line = line_from_box("a  bc", (0, 0, 100, 10))
    assert line.text == "a bc" and [w.text for w in line.words] == ["a", "bc"]
    assert len(line.char_boxes()) == 4


def test_auto_prefers_livetext_then_vision_on_macos(fake_ocrmac, monkeypatch):
    monkeypatch.setattr(ocr, "_is_macos", lambda: True)
    fake_ocrmac.results["livetext"] = _char_tokens("홍길동", 0, 0)
    fake_ocrmac.results["vision"] = [("vision text", 1.0, (0, 0, 50, 10))]
    img = Image.new("RGB", (100, 40))
    assert ocr_image(img, cfg_with()).engine == "livetext"
    fake_ocrmac.fail = {"livetext"}                                  # e.g. macOS 13
    assert ocr_image(img, cfg_with()).engine == "vision"
    fake_ocrmac.fail = set()
    fake_ocrmac.results["livetext"] = []                             # nothing found: ask vision too
    assert ocr_image(img, cfg_with()).engine == "vision"


def test_auto_falls_back_to_tesseract_and_reports_all_missing(monkeypatch):
    monkeypatch.setattr(ocr, "_is_macos", lambda: True)
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    monkeypatch.setattr(ocr, "_find_tesseract", lambda: None)
    with pytest.raises(OcrError) as ei:
        ocr_image(Image.new("RGB", (10, 10)), cfg_with())
    msg = str(ei.value)
    assert "pip install ocrmac" in msg and "brew install tesseract" in msg


def test_auto_off_macos_uses_tesseract_only(monkeypatch, fake_ocrmac):
    monkeypatch.setattr(ocr, "_is_macos", lambda: False)
    called = []
    monkeypatch.setitem(ocr._ENGINE_FUNCS, "tesseract",
                        lambda img, opts: called.append(1) or ocr.OcrResult([], "tesseract"))
    res = ocr_image(Image.new("RGB", (10, 10)), cfg_with())
    assert res.engine == "tesseract" and called and not fake_ocrmac.calls


def test_engine_exceptions_are_wrapped_without_data(fake_ocrmac):
    fake_ocrmac.fail = {"vision"}
    with pytest.raises(OcrError, match="vision OCR failed"):
        ocr_image(Image.new("RGB", (10, 10)), cfg_with(engine="vision"))


def test_engine_status_and_available(monkeypatch, fake_ocrmac):
    monkeypatch.setattr(ocr, "_is_macos", lambda: True)
    monkeypatch.setattr(ocr, "_macos_version", lambda: (14, 5))
    status = ocr.engine_status()
    assert status["livetext"] == "ok" and status["vision"] == "ok"
    avail = ocr.available_engines()
    assert avail[:2] == ["livetext", "vision"]
    monkeypatch.setattr(ocr, "_macos_version", lambda: (13, 6))
    assert "livetext" not in ocr.available_engines()
    monkeypatch.setattr(ocr, "_is_macos", lambda: False)
    assert "vision" not in ocr.available_engines()
    assert set(ocr.engine_status()) == {"livetext", "vision", "tesseract"}


# ---------------------------------------------------------------------- real tesseract


@needs_tesseract
@needs_font
def test_tesseract_reads_korean_and_boxes_match_drawing():
    font = ImageFont.truetype(str(FONT), 24)
    img = Image.new("RGB", (900, 160), "white")
    d = ImageDraw.Draw(img)
    text = "환자명: 홍길동 연락처 010-2345-6789"
    d.text((30, 40), text, fill="black", font=font)
    d.text((30, 100), "metformin 500mg 혈압 조절", fill="black", font=font)
    res = ocr_image(img, cfg_with(engine="tesseract", psm=6))
    assert res.engine == "tesseract" and len(res.lines) >= 2
    joined = " ".join(ln.text for ln in res.lines)
    assert "010-2345-6789" in joined and "metformin" in joined
    line = next(ln for ln in res.lines if "010-2345-6789" in ln.text)
    s = line.text.index("010-2345-6789")
    got = line.span_box(s, s + 13)
    off = font.getlength(text[:text.index("010")])
    want = d.textbbox((30 + off, 40), "010-2345-6789", font=font)
    # boxes are in ORIGINAL image coordinates (OCR ran on a x2 upscaled copy)
    assert got[0] == pytest.approx(want[0], abs=6) and got[2] == pytest.approx(want[2], abs=6)
    assert got[1] == pytest.approx(want[1], abs=6) and got[3] == pytest.approx(want[3], abs=6)


@needs_tesseract
def test_tesseract_blank_image_has_no_lines():
    res = ocr_image(Image.new("RGB", (300, 120), "white"), cfg_with(engine="tesseract"))
    assert res.lines == [] and res.text == ""
