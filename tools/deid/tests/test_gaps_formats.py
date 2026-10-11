"""Regression tests for the completeness critic's format gaps (synthetic people only).

* Comma-pasted 'table' typing: a typed cell must have the shape of its kind, untyped cells of
  a typed row are still detected, and a typed column never hides what detection found.
* Tesseract boxes: no sliver of a name's first or last glyph is left at the box edge.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deid import deidentify
from deid.anonymizer import Anonymizer
from deid.config import default_config
from deid.engine import Detector
from deid.formats import tabular as TB
from deid.formats import text as T
from deid.types import Report, Span


def _deid(text: str, cfg=None) -> str:
    cfg = cfg or default_config()
    return T.deid_text(text, Detector(cfg), Anonymizer(cfg))[0]


def _clean(out: str, *values: str) -> None:
    for v in values:
        assert v not in out, f"PII left in output: {v!r} in {out!r}"


# --------------------------------------------------------------------------- comma 'tables'

CRITIC_TWO_LINES = "생일 3월 15일, 45세\n서울 마포구 월드컵로 10 (합정동), 한강아파트 3동 201호"


def test_gap_csv_address_line_is_not_a_birthdate():
    """The critic's two lines: the address line under '생일 3월 15일, 45세' used to become
    '[생년월일], 한강아파트 3동 201호' (typed BIRTHDATE, apartment unit leaked)."""
    for out in (deidentify(CRITIC_TWO_LINES)[0], _deid(CRITIC_TWO_LINES)):
        line1, line2 = out.splitlines()
        _clean(out, "월드컵로", "한강아파트", "201호", "3월 15일")
        assert "[생년월일]" not in line2 and "[주소]" in line2
        assert line1.startswith("생일 ") and line1.endswith(", 45세")
    # each line on its own is masked the same way
    out2 = deidentify(CRITIC_TWO_LINES.splitlines()[1])[0]
    _clean(out2, "월드컵로", "한강아파트", "201호")


def test_gap_csv_birthdate_cells_need_a_date_shape():
    for v in ("1979-04-05", "79.04.05", "1979년 4월 5일", "3월 15일", "19790405", "1979",
              "85년생", "1979.04.05 (45세)", "1979-04-05/F", "2024. 3. 15.", "1985년 3월",
              "03/15/1985", "15 Mar 1985"):
        assert T.date_shaped(v), v
        assert T.value_shaped("BIRTHDATE", v) and T.value_shaped("DATE", v), v
    for v in ("서울 마포구 월드컵로 10 (합정동)", "한강아파트 3동 201호", "45세", "3동 201호",
              "2024년 진료비", "오전 10시", "혈압 120/80", "1234", "2층", "두통, 3일째"):
        assert not T.date_shaped(v), v
        assert not T._csv_cell_fits("BIRTHDATE", v), v
    # a real birth-date table is still typed
    out = deidentify("이름, 생년월일\n표다은, 1979-04-05\n설윤재, 3월 15일")[0]
    _clean(out, "표다은", "설윤재", "1979-04-05", "3월 15일")
    assert "1979년생" in out


def test_gap_csv_header_cell_with_a_value_is_not_a_header():
    """'생일 3월 15일' carries its value: it is no column header."""
    assert TB.column_entity("생일 3월 15일") is None
    assert TB.column_entity("만 45세 보호자") is None
    assert TB.column_entity("생일") == "BIRTHDATE" and TB.column_entity("보호자 연락처") == "PHONE"
    assert TB.column_entity("연락처1") == "PHONE"


def test_gap_csv_untyped_cell_of_typed_row_still_detected():
    """An address split by the comma of a pasted CSV row: the first half is typed by the 주소
    header, the unit after the comma (메모 column) must still be masked."""
    out = deidentify("주소, 메모\n서울 마포구 월드컵로 10 (합정동), 한강아파트 3동 201호")[0]
    _clean(out, "월드컵로", "한강아파트", "201호")
    assert out.splitlines()[0] == "주소, 메모"
    # the same row as TSV keeps the header-only rule and is masked too
    out = _deid("주소\t메모\n서울 마포구 월드컵로 10 (합정동)\t한강아파트 3동 201호 거주")
    _clean(out, "월드컵로", "한강아파트", "201호")


def test_gap_csv_typed_columns_need_value_shapes():
    """Audit of the other plausible() checks: an ID/phone/e-mail column of a comma 'table'
    in prose no longer swallows an address or a sentence as that kind of value."""
    out = deidentify("이름, 등록번호\n표다은, 서울 마포구 월드컵로 1234 설윤재 님")[0]
    _clean(out, "표다은", "설윤재", "월드컵로")
    assert "[등록번호" not in out and "[주소]" in out
    out = deidentify("이름, 연락처\n표다은, 2024년 진료비 1234567원 납부")[0]
    assert "1234567원" in out and "표다은" not in out
    for kind, good, bad in (
            ("RRN", "900101-1234567", "보호자 설윤재 님 900101"),
            ("MRN", "00482917", "서울 마포구 월드컵로 1234"),
            ("ACCOUNT", "국민 123-45-6789", "2024년 3월 내원 12345"),
            ("PASSPORT", "M12345678", "여권 갱신 예정 다음 달"),
            ("PHONE", "010-2345-6789 (본인)", "2024-03-15"),
            ("PHONE", "+82 10 2345 6789", "1234567원"),
            ("EMAIL", "dae@example.com", "@설윤재 님 확인"),
            ("IP", "192.168.0.12", "192.168 대역"),
            ("PLATE", "12가3456", "12층 가동 3456"),
            ("ADDRESS", "서울 마포구 월드컵로 10", "두통, 어지럼"),
    ):
        assert T.value_shaped(kind, good), (kind, good)
        assert not T.value_shaped(kind, bad), (kind, bad)
    # an unknown (custom) column type accepts short values only
    assert T.value_shaped("STAFF_ID", "N-2041") and not T.value_shaped(
        "STAFF_ID", "오늘 오후 회의 후 다시 연락 드리기로 함")
    # prose under a header-like line stays as it was
    prose = "연락처, 주소 확인 필요\n오늘 오후, 내일 오전 중 연락, 예정"
    assert deidentify(prose)[0] == prose


def test_gap_typed_phone_cell_keeps_other_pii_for_detection():
    """A phone column cell 'guardian name + number' was typed whole as PHONE, and a phone
    mask hides digits only: the guardian's name stayed readable."""
    assert [(s.entity, s.text) for s in T.typed_spans("PHONE", "보호자 설윤재 010-2345-6789")] == [
        ("PERSON", "설윤재"), ("PHONE", "010-2345-6789")]
    assert [(s.entity, s.text) for s in T.typed_spans("PHONE", "설윤재 010-2345-6789")] == [
        ("PERSON", "설윤재"), ("PHONE", "010-2345-6789")]
    # number + label or relation word only: typed whole, as before
    assert [s.text for s in T.typed_spans("PHONE", "모 010-2345-6789")] == ["모 010-2345-6789"]
    assert [s.text for s in T.typed_spans("PHONE", "HP 010-2345-6789")] == ["HP 010-2345-6789"]
    assert [s.text for s in T.typed_spans("PHONE", "구번호 010-1111-2222")] == ["010-1111-2222"]
    assert T.phone_runs("1234567원") == [] and T.phone_runs("2024-03-15") == []
    for out in (_deid("이름\t연락처\n표다은\t보호자 설윤재 010-2345-6789"),
                deidentify("이름, 연락처\n표다은, 보호자 설윤재 010-2345-6789")[0]):
        _clean(out, "표다은", "설윤재", "2345-6789")
        assert "보호자 [이름" in out


def test_gap_typed_phone_cell_in_csv_and_xlsx_files(tmp_path: Path):
    openpyxl = pytest.importorskip("openpyxl")
    cfg = default_config()
    out = TB.deid_csv_text("이름,연락처,메모\n표다은,설윤재 010-2345-6789,재진\n", ",",
                           Detector(cfg), Anonymizer(cfg), Report())
    _clean(out, "표다은", "설윤재", "2345-6789")
    assert out.endswith(",재진\n")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["이름", "연락처", "메모"])
    ws.append(["표다은", "보호자 설윤재 010-2345-6789", "재진"])
    src, dst = tmp_path / "c.xlsx", tmp_path / "out" / "c.xlsx"
    wb.save(src)
    TB.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    got = [c.value for c in openpyxl.load_workbook(dst).active[2]]
    assert "설윤재" not in got[1] and "2345-6789" not in got[1] and got[1].startswith("보호자 [이름")
    assert got[0].startswith("[이름") and got[2] == "재진"


def test_gap_weak_typed_span_yields_to_stronger_detection():
    """A column typed as a kept entity (user override 'DATE') or a masked one never shields
    a name or number inside the cell."""
    cfg = default_config()
    cfg.columns = {"방문": "DATE"}
    out = _deid("이름\t방문\n표다은\t2024-03-05 설윤재 님 동행, 010-2345-6789", cfg)
    _clean(out, "표다은", "설윤재", "2345-6789")
    assert "2024-03-05" in out
    # merge_typed directly: carved pieces keep the typed entity, the name wins inside
    text = "2024-03-05 설윤재 님"
    typed = [Span(0, len(text), "DATE", text, 1.0, "column")]
    det = [Span(11, 14, "PERSON", "설윤재", 0.9, "names")]
    merged = T.merge_typed(typed, det, lambda e: {"DATE": 0, "PERSON": 3}[e])
    assert [(s.entity, s.text) for s in merged] == [
        ("DATE", "2024-03-05"), ("PERSON", "설윤재"), ("DATE", "님")]


def test_gap_merge_typed_keeps_detected_part_outside_typed_cell():
    text = "서울 마포구 월드컵로 10 (합정동), 한강아파트 3동 201호"
    cut = text.index(",")
    typed = [Span(0, cut, "ADDRESS", text[:cut], 1.0, "column")]
    det = [Span(0, len(text), "ADDRESS", text, 0.9, "address")]
    merged = T.merge_typed(typed, det, lambda e: 2)
    assert [s.text for s in merged] == [text[:cut], "한강아파트 3동 201호"]
    assert all(s.text == text[s.start:s.end] for s in merged)
    # without strength (legacy callers) the typed span simply wins
    assert [s.text for s in T.merge_typed(typed, det)] == [text[:cut]]


# --------------------------------------------------------------------------- tesseract boxes

def _font_path(name: str) -> Path | None:
    for d in (Path("/usr/share/fonts/truetype/nanum"), Path.home() / "Library/Fonts",
              Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")):
        p = d / f"{name}.ttf"
        if p.exists():
            return p
    return None


def _has_tesseract() -> bool:
    try:
        from deid import ocr
        return "tesseract" in ocr.available_engines() and "kor" in ocr.tesseract_languages()
    except Exception:  # noqa: BLE001
        return False


NANUM = {k: _font_path(k) for k in ("NanumGothic", "NanumMyeongjo")}
needs_tesseract = pytest.mark.skipif(not _has_tesseract(), reason="tesseract (kor) missing")
INK = (20, 20, 160)        # text colour: painted boxes are pure black, so they differ from ink
# (prefix, name, neighbour word, rest): the neighbour must stay readable after redaction
BOX_LINES = (("환자명: ", "류하은", "혈압", " 128/82"),
             ("보호자 ", "남궁서진", "님", " 내원"),
             ("Patient: ", "Olivia Brennan", "metformin", " 500mg"))


def _tess_cfg():
    cfg = default_config()
    cfg.ocr.update({"engine": "tesseract"})
    return cfg


@needs_tesseract
@pytest.mark.parametrize("font_name", sorted(NANUM))
@pytest.mark.parametrize("size", [16, 22, 32, 48])
def test_gap_tesseract_boxes_leave_no_glyph_sliver(font_name, size):
    """Tesseract's Hangul word boxes can start inside the first glyph (the left stroke of
    '남' in '보호자 남궁서진 님', the left half of '류' stayed visible). After redaction no pixel
    of the name's own ink may be left inside its true bounding box (the name rendered alone
    at the same place), and the word after it keeps its ink and stays readable."""
    from PIL import Image, ImageDraw, ImageFont

    from deid import ocr
    from deid.formats.image import redact_image

    path = NANUM[font_name]
    if path is None:
        pytest.skip(f"{font_name} not installed")
    f = ImageFont.truetype(str(path), size)
    step = int(size * 2.2)
    texts = [pre + name + " " + nxt + rest for pre, name, nxt, rest in BOX_LINES]
    width = int(max(f.getlength(t) for t in texts)) + 80
    img = Image.new("RGB", (width, step * len(texts) + size), "white")
    draw = ImageDraw.Draw(img)
    names, nexts = [], []
    for k, ((pre, name, nxt, _rest), t) in enumerate(zip(BOX_LINES, texts)):
        x0, y0 = 30, size // 2 + k * step
        draw.text((x0, y0), t, fill=INK, font=f)
        alone = Image.new("L", img.size, 0)
        ImageDraw.Draw(alone).text((x0 + f.getlength(pre), y0), name, fill=255, font=f)
        after = Image.new("L", img.size, 0)
        ImageDraw.Draw(after).text((x0 + f.getlength(pre + name + " "), y0), nxt, fill=255, font=f)
        names.append(alone)
        nexts.append((nxt, after))
    cfg = _tess_cfg()
    out, rep = redact_image(img, Detector(cfg), cfg, Anonymizer(cfg))
    out = out.convert("RGB")
    px = out.load()
    assert rep.counts.get("PERSON", 0) >= len(BOX_LINES)
    for (pre, name, _n, _r), alone in zip(BOX_LINES, names):
        bb = alone.getbbox()
        al = alone.load()
        left = [(x, y) for x in range(bb[0], bb[2]) for y in range(bb[1], bb[3])
                if al[x, y] > 5 and px[x, y] != (0, 0, 0)]
        assert not left, f"{font_name} {size}px: {len(left)} ink pixels of {name!r} still visible"
    for nxt, after in nexts:
        bb = after.getbbox()
        al = after.load()
        ink = [(x, y) for x in range(bb[0], bb[2]) for y in range(bb[1], bb[3]) if al[x, y] > 128]
        hit = sum(1 for x, y in ink if px[x, y] == (0, 0, 0))
        assert hit <= 0.03 * len(ink), f"{nxt!r}: {hit}/{len(ink)} ink pixels painted over"
        # readable: OCR the rest of the line, right of the painted box
        black = [x for x in range(bb[0] - 1, 0, -1) if px[x, (bb[1] + bb[3]) // 2] == (0, 0, 0)]
        x1 = (black[0] + 1) if black else max(0, bb[0] - size)
        crop = out.crop((x1, max(0, bb[1] - size // 2), out.width, min(out.height, bb[3] + size // 2)))
        crop = crop.resize((crop.width * 2, crop.height * 2))
        lines, _, _ = ocr.lines_from_tesseract(ocr.tesseract_data(crop, "kor+eng", 7))
        read = "".join(ln.text for ln in lines).replace(" ", "")
        assert nxt in read, f"{font_name} {size}px: {nxt!r} not readable after redaction ({read!r})"


def test_gap_snap_to_ink_moves_box_sides_out_to_the_glyph_edge():
    """Unit check of the box snapping: a box that starts inside a glyph grows to the glyph's
    edge, stops at a word gap, reports the blank room to the next word (padding stops a pixel
    short of it), ignores regions painted before and leaves boxes in blank space alone."""
    from PIL import Image, ImageDraw

    from deid.formats.image import _Redactor
    from deid.ocr import OcrLine, OcrWord

    img = Image.new("RGB", (300, 60), "white")
    d = ImageDraw.Draw(img)
    # words drawn as 2 px strokes 2 px apart: previous word ink 20..61, the name 80..129,
    # the next word from 140 on
    for start, stop in ((20, 61), (80, 129), (140, 181)):
        for x in range(start, stop, 4):
            d.rectangle((x, 20, x + 1, 40), fill=(0, 0, 0))
    red = _Redactor(img, None, None, Report())
    box, room_l, room_r = red._snap_to_ink((90.0, 20.0, 120.0, 41.0), 20.0)
    assert box == (80.0, 20.0, 130.0, 41.0)
    assert room_r == 140 - 130 and room_l == float("inf")   # previous word out of view
    assert red._snap_to_ink((80.0, 20.0, 120.0, 41.0), 20.0)[1] == 80 - 62
    # a side never moves further than SNAP_MAX_RATIO x the line height (0.6 x 20 px)
    assert red._snap_to_ink((95.0, 20.0, 120.0, 41.0), 20.0)[0][0] == 84.0
    # a box overshooting into the word space: unchanged, room counted from its edge
    box, _, room_r = red._snap_to_ink((80.0, 20.0, 135.0, 41.0), 20.0)
    assert box == (80.0, 20.0, 135.0, 41.0) and room_r == 5
    # padding never reaches the next word's ink
    line = OcrLine.from_words([OcrWord("홍길동", (90.0, 20.0, 120.0, 41.0), 90.0)], "tesseract/psm6")
    region = red._region((90.0, 20.0, 120.0, 41.0), line)
    assert region[0] < 80 and 130 < region[2] < 140
    # a region painted earlier over the gap does not bridge it into the next word
    d.rectangle((125, 15, 145, 45), fill=(0, 0, 0))
    red.painted.append((125, 15, 146, 46))
    assert red._snap_to_ink((90.0, 20.0, 120.0, 41.0), 20.0)[0][2] < 140


def test_gap_split_syllable_ocr_line_keeps_label_and_honorific_apart():
    """'담 당 의 길 예 준 님' (small text OCR'd one syllable at a time) joined to
    '담당의길예준님' and the name was missed; the label/honorific-aware variant finds it."""
    from deid.formats.image import _variants, detect_lines
    from deid.ocr import OcrLine, OcrWord

    def line(*words):
        ws, x = [], 0.0
        for w in words:
            ws.append(OcrWord(w, (x, 0.0, x + 20.0 * len(w), 20.0), 90.0))
            x += 20.0 * len(w) + 12.0
        return OcrLine.from_words(ws, "tesseract")

    ln = line("담", "당", "의", "길", "예", "준", "님", "혈압", "128/82")
    assert "담당의 길예준 님 혈압 128/82" in [v[0] for v in _variants(ln)]
    ln2 = line("보호자", "황", "보", "민서", "010-4321-8765")
    assert "보호자 황보민서 010-4321-8765" in [v[0] for v in _variants(ln2)]
    cfg = default_config()
    spans = detect_lines([ln, ln2], Detector(cfg))
    assert [sp.text for sp in spans[0] if sp.entity == "PERSON"] == ["길 예 준"]
    assert any(sp.entity == "PERSON" and sp.text == "황 보 민서" for sp in spans[1])
