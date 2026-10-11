"""Tests for deid.formats.image: real tesseract OCR on generated screenshots, then the OUTPUT is
OCR'd again to prove the PII is gone while clinical text survives. Synthetic people only."""
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageSequence

from deid import entities as E
from deid import ocr
from deid.anonymizer import Anonymizer
from deid.config import default_config
from deid.engine import Detector
from deid.formats import image as I
from deid.ocr import OcrError, OcrLine, OcrWord
from deid.types import Report, Span

FONT_DIR = Path("/usr/share/fonts/truetype/nanum")
FONT = FONT_DIR / "NanumGothic.ttf"
BOLD = FONT_DIR / "NanumGothicBold.ttf"
HAS_TESSERACT = "tesseract" in ocr.available_engines() and "kor" in ocr.tesseract_languages()
needs_ocr = pytest.mark.skipif(not (HAS_TESSERACT and FONT.exists()),
                               reason="tesseract (kor) or NanumGothic font missing")


def make_cfg(**ocr_opts):
    cfg = default_config()
    cfg.ocr.update({"engine": "tesseract"})
    cfg.ocr.update(ocr_opts)
    return cfg


def font(size, bold=False):
    return ImageFont.truetype(str(BOLD if bold and BOLD.exists() else FONT), size)


class Canvas:
    """Draw lines of text and remember where given substrings ended up."""

    def __init__(self, size, bg="white", fg="black"):
        self.img = Image.new("RGB", size, bg)
        self.draw = ImageDraw.Draw(self.img)
        self.fg = fg
        self.boxes: dict[str, list[tuple[int, int, int, int]]] = {}

    def text(self, xy, text, size=22, mark=(), fill=None, bold=False):
        f = font(size, bold)
        self.draw.text(xy, text, fill=fill or self.fg, font=f)
        for m in mark:
            i = text.index(m)
            off = f.getlength(text[:i])
            self.boxes.setdefault(m, []).append(
                self.draw.textbbox((xy[0] + off, xy[1]), m, font=f))


def read_all(img: Image.Image, scales=(1, 2)) -> str:
    """Everything tesseract can read in `img` with several layouts and scales (one line of
    text per OCR line; independent of deid's own pass settings)."""
    gray = img.convert("L")
    jobs = [(psm, s) for psm in (3, 6, 11) for s in scales]

    def run(job):
        psm, s = job
        im = gray if s == 1 else gray.resize((gray.width * s, gray.height * s), Image.LANCZOS)
        lines, _, _ = ocr.lines_from_tesseract(ocr.tesseract_data(im, "kor+eng", psm))
        return "\n".join(ln.text for ln in lines)
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return "\n".join(pool.map(run, jobs))


def leaked(text: str, pii: list[str]) -> list[str]:
    """PII strings (spacing ignored) or their digit runs (>= 4 digits) still readable."""
    compact = re.sub(r"\s+", "", text)
    runs = re.findall(r"\d+", text)
    out = []
    for p in pii:
        if re.sub(r"\s+", "", p) in compact:
            out.append(p)
            continue
        for d in re.findall(r"\d{4,}", p):
            if any(d in r for r in runs):
                out.append(p)
                break
    return out


def black_share(img: Image.Image, box) -> float:
    """Share of (near) pure black pixels inside `box`."""
    crop = img.convert("RGB").crop(tuple(int(round(v)) for v in box)).convert("L")
    return crop.histogram()[0] / max(1, crop.width * crop.height)


# ---------------------------------------------------------------------- the EMR screenshot


EMR_PII = ["홍길동", "김민준", "이서연", "12345678", "010-2345-6789", "790302-1234567",
           "테헤란로 123", "010-9876-5432", "1979-03-02"]
EMR_KEEP = ["metformin", "amlodipine", "혈압", "두통", "고혈압", "처방"]


def emr_screenshot() -> Canvas:
    c = Canvas((1280, 720), bg=(245, 247, 250))
    d = c.draw
    d.rectangle([0, 0, 1280, 44], fill=(40, 70, 120))
    c.text((16, 10), "외래 진료기록 - 내과", size=22, fill="white", bold=True)
    d.rectangle([12, 60, 470, 700], fill="white", outline=(200, 200, 200))
    rows = [("환자명: 홍길동 (남/45)", ["홍길동"]), ("등록번호: 12345678", ["12345678"]),
            ("생년월일: 1979-03-02", ["1979-03-02"]), ("연락처: 010-2345-6789", ["010-2345-6789"]),
            ("주민등록번호: 790302-1234567", ["790302-1234567"]),
            ("주소: 서울특별시 강남구 테헤란로 123", ["테헤란로 123"]),
            ("보호자: 김민준 (아들)", ["김민준"])]
    y = 76
    for t, m in rows:
        c.text((24, y), t, size=22, mark=m)
        y += 48
    d.rectangle([490, 60, 1268, 700], fill="white", outline=(200, 200, 200))
    cols = [506, 680, 900, 1080]
    for x, h in zip(cols, ["날짜", "처방", "용량", "비고"]):
        c.text((x, 76), h, size=22, bold=True, fill=(60, 60, 60))
    y = 120
    for row in [("2024-05-02", "metformin", "500mg bid", "유지"),
                ("2024-05-02", "amlodipine", "5mg qd", "혈압 조절")]:
        for x, cell in zip(cols, row):
            c.text((x, y), cell, size=22)
        y += 44
    notes = [("S) 혈압 140/90, 두통 호소", []), ("A) 고혈압, 제2형 당뇨병", []),
             ("P) 이서연 선생님께 협진 의뢰", ["이서연"]),
             ("보호자 김민준 님과 통화함 (010-9876-5432)", ["김민준", "010-9876-5432"]),
             ("홍길동 혈압 재측정 예정", ["홍길동"])]     # name without any context: propagation
    y += 10
    for t, m in notes:
        c.text((506, y), t, size=22, mark=m)
        y += 42
    return c


@pytest.fixture(scope="module")
def emr_result(tmp_path_factory):
    if not (HAS_TESSERACT and FONT.exists()):
        pytest.skip("tesseract (kor) or NanumGothic font missing")
    tmp = tmp_path_factory.mktemp("emr")
    c = emr_screenshot()
    src = tmp / "emr.png"
    c.img.save(src)
    cfg = make_cfg()
    det = Detector(cfg)
    an = Anonymizer(cfg)
    dst = tmp / "emr_deid.png"
    report = I.process(src, dst, det, an, cfg)
    out = Image.open(dst)
    out.load()
    return c, report, out, read_all(out)


@needs_ocr
def test_emr_pii_not_readable_after_redaction(emr_result):
    _, _, _, text = emr_result
    assert leaked(text, EMR_PII) == []


@needs_ocr
def test_emr_clinical_text_still_readable(emr_result):
    _, _, _, text = emr_result
    compact = re.sub(r"\s+", "", text)
    readable = [w for w in EMR_KEEP if w in compact]
    assert len(readable) >= len(EMR_KEEP) - 1, readable
    assert "metformin" in readable and "혈압" in readable


@needs_ocr
def test_emr_pixels_over_pii_are_black(emr_result):
    c, _, out, _ = emr_result
    for value, boxes in c.boxes.items():
        for box in boxes:
            assert black_share(out, box) > 0.97, value
    # clinical text is untouched
    assert black_share(out, (680, 120, 820, 150)) < 0.05      # "metformin" cell


@needs_ocr
def test_emr_report_counts_without_raw_values(emr_result):
    _, report, out, _ = emr_result
    for ent in (E.PERSON, E.PHONE, E.RRN, E.ADDRESS, E.MRN, E.BIRTHDATE):
        assert report.counts.get(ent, 0) >= 1, ent
    assert report.counts[E.PERSON] >= 4
    assert report.counts[E.PHONE] == 2
    dumped = repr(report.to_dict())
    for p in EMR_PII + ["길동", "민준", "서연"]:
        assert p not in dumped
    assert report.source.endswith("emr.png") and report.output.endswith("emr_deid.png")
    assert out.size == (1280, 720) and out.format == "PNG"


@needs_ocr
def test_dark_mode_screenshot(tmp_path):
    c = Canvas((900, 260), bg=(30, 30, 30), fg=(225, 225, 225))
    c.text((20, 20), "환자명: 김민준  연락처 010-3456-7890", size=24,
           mark=["김민준", "010-3456-7890"])
    c.text((20, 80), "metformin 500mg 복용 중, 혈압 안정적", size=24)
    c.text((20, 140), "이메일 minjun.kim@example.com 으로 결과 전송", size=24,
           mark=["minjun.kim@example.com"])
    src, dst = tmp_path / "dark.png", tmp_path / "dark_out.png"
    c.img.save(src)
    cfg = make_cfg()
    report = I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    text = read_all(out)
    assert leaked(text, ["김민준", "010-3456-7890", "minjun.kim", "example.com"]) == []
    assert "metformin" in text
    for boxes in c.boxes.values():
        for box in boxes:
            assert black_share(out, box) > 0.97
    assert report.counts[E.EMAIL] == 1 and report.counts[E.PERSON] >= 1


@needs_ocr
def test_jpeg_input_with_exif_gps_is_stripped(tmp_path):
    c = Canvas((800, 200))
    c.text((20, 30), "보호자 이서연 님 010-1111-2222 통화", size=26, mark=["이서연", "010-1111-2222"])
    c.text((20, 100), "혈압 130/80 metformin 유지", size=26)
    exif = Image.Exif()
    exif[0x010F] = "Phone Maker"            # Make
    exif[0x0110] = "Model X"                # Model
    exif[0x013B] = "Artist Name"            # Artist
    exif[0x8825] = {1: "N", 2: (37.0, 30.0, 0.0), 3: "E", 4: (127.0, 2.0, 0.0)}  # GPS IFD
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src, dst = tmp_path / "photo.jpg", tmp_path / "photo_deid.jpg"
    c.img.save(src, quality=92, exif=exif, icc_profile=icc, comment=b"secret comment")
    assert Image.open(src).getexif().get_ifd(0x8825)          # fixture really has GPS
    cfg = make_cfg()
    report = I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    assert out.format == "JPEG"
    assert "exif" not in out.info and len(out.getexif()) == 0
    assert "comment" not in out.info and "xmp" not in out.info
    assert out.info.get("icc_profile") == icc                  # colour profile kept
    raw = dst.read_bytes()
    for needle in (b"Phone Maker", b"Artist Name", b"secret comment", b"Exif"):
        assert needle not in raw
    assert leaked(read_all(out, scales=(1,)), ["이서연", "010-1111-2222"]) == []
    assert report.counts[E.PERSON] >= 1 and report.counts[E.PHONE] == 1


@needs_ocr
def test_exif_orientation_applied_and_no_text_warning(tmp_path):
    img = Image.new("RGB", (120, 60), (250, 250, 250))
    exif = Image.Exif()
    exif[0x0112] = 6                       # rotate 90 CW on display
    src, dst = tmp_path / "blank.jpg", tmp_path / "blank_out.jpg"
    img.save(src, exif=exif)
    cfg = make_cfg()
    report = I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    assert out.size == (60, 120)            # pixels rotated, tag dropped
    assert len(out.getexif()) == 0
    assert I.NO_TEXT_WARNING in report.warnings
    assert report.total == 0


@needs_ocr
def test_multipage_tiff_all_frames(tmp_path):
    pages = []
    p1 = Canvas((760, 140))
    p1.text((20, 40), "환자명: 김민준 외래 내원", size=26, mark=["김민준"])
    p2 = Canvas((760, 140))
    p2.text((20, 40), "연락처 010-2345-6789 metformin", size=26, mark=["010-2345-6789"])
    pages = [p1, p2]
    src, dst = tmp_path / "scan.tiff", tmp_path / "scan_deid.tif"
    p1.img.save(src, save_all=True, append_images=[p2.img])
    cfg = make_cfg(verify=0)
    report = I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    frames = [f.copy() for f in ImageSequence.Iterator(out)]
    assert len(frames) == 2
    for page, frame in zip(pages, frames):
        for boxes in page.boxes.values():
            for box in boxes:
                assert black_share(frame, box) > 0.97
    assert "metformin" in read_all(frames[1], scales=(1,))
    assert report.counts[E.PERSON] == 1 and report.counts[E.PHONE] == 1


@needs_ocr
@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif"])
def test_output_formats_round_trip(tmp_path, ext):
    img = Image.new("RGB", (64, 48), (240, 240, 240))
    src = tmp_path / "blank.png"
    img.save(src, pnginfo=_png_text("Author", "Some Body"))
    dst = tmp_path / f"out{ext}"
    cfg = make_cfg()
    I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    assert out.size == (64, 48)
    assert out.format == I._FORMATS[ext]
    assert "Author" not in out.info and b"Some Body" not in dst.read_bytes()
    assert not list(tmp_path.glob(".deid-*"))                 # no temp files left


def _png_text(k, v):
    from PIL import PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text(k, v)
    return info


@needs_ocr
def test_rgba_png_keeps_alpha_and_covers_text(tmp_path):
    c = Canvas((700, 120))
    c.text((20, 40), "환자명: 홍길동 (남/45)", size=26, mark=["홍길동"])
    rgba = c.img.convert("RGBA")
    src, dst = tmp_path / "a.png", tmp_path / "a_out.png"
    rgba.save(src)
    cfg = make_cfg(verify=0)
    I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    assert out.mode == "RGBA"
    for box in c.boxes["홍길동"]:
        assert black_share(out.convert("RGB"), box) > 0.97
        assert out.getpixel(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2))[3] == 255


@needs_ocr
def test_redact_screenshot_adds_png_suffix(tmp_path):
    c = Canvas((600, 100))
    c.text((20, 30), "담당의: 이서연 선생님", size=24, mark=["이서연"])
    src = tmp_path / "shot.png"
    c.img.save(src)
    cfg = make_cfg(verify=0)
    report = I.redact_screenshot(src, tmp_path / "shot_out", Detector(cfg), cfg=cfg)
    out_path = tmp_path / "shot_out.png"
    assert out_path.exists() and report.output == str(out_path)
    assert black_share(Image.open(out_path), c.boxes["이서연"][0]) > 0.97


@needs_ocr
def test_style_pixelate_and_blur(tmp_path):
    c = Canvas((600, 100))
    c.text((20, 30), "환자명: 홍길동 metformin", size=26, mark=["홍길동"])
    for style in ("pixelate", "blur"):
        cfg = make_cfg(style=style, verify=0)
        out, report = I.redact_image(c.img, Detector(cfg), cfg)
        box = c.boxes["홍길동"][0]
        assert out.crop(box).tobytes() != c.img.crop(box).tobytes()
        assert black_share(out, box) < 0.5          # not a black box
        assert "홍길동" not in re.sub(r"\s+", "", read_all(out, scales=(1,)))
        assert report.counts[E.PERSON] == 1
    with pytest.raises(ValueError, match="style"):
        I.redact_image(c.img, Detector(make_cfg()), make_cfg(style="sparkle", verify=0))


# ---------------------------------------------------------------------- without real OCR


def test_unsupported_and_heic_without_plugin(tmp_path, monkeypatch):
    cfg = default_config()
    det, an = Detector(cfg), Anonymizer(cfg)
    with pytest.raises(ValueError, match="unsupported image type"):
        I.process(tmp_path / "a.gif", tmp_path / "b.png", det, an, cfg)
    with pytest.raises(ValueError, match="unsupported output type"):
        I.process(tmp_path / "a.png", tmp_path / "b.xyz", det, an, cfg)
    monkeypatch.setitem(sys.modules, "pillow_heif", None)
    (tmp_path / "p.heic").write_bytes(b"\0" * 16)
    with pytest.raises(RuntimeError, match="pillow-heif"):
        I.process(tmp_path / "p.heic", tmp_path / "p.jpg", det, an, cfg)
    assert not (tmp_path / "p.jpg").exists()


def test_ocr_failure_writes_nothing(tmp_path, monkeypatch):
    src, dst = tmp_path / "x.png", tmp_path / "x_out.png"
    Image.new("RGB", (40, 40), "white").save(src)

    def boom(img, cfg=None, engine=None):
        raise OcrError("no usable OCR engine")
    monkeypatch.setattr(I, "ocr_image", boom)
    cfg = default_config()
    with pytest.raises(OcrError):
        I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    assert not dst.exists()
    assert list(tmp_path.iterdir()) == [src]


def _word_line(words, y=10, h=20, engine="test"):
    """OcrLine from (text, x1, x2) triples."""
    return OcrLine.from_words([OcrWord(t, (x1, y, x2, y + h), 90) for t, x1, x2 in words], engine)


def test_detect_lines_rejoins_split_korean_syllables():
    det = Detector(default_config())
    # tesseract-style output: "이서연 선생님께" read as single syllables
    line = _word_line([("P)", 0, 20), ("이", 30, 50), ("서", 51, 71), ("연", 72, 92),
                       ("선", 104, 124), ("생", 125, 145), ("님", 146, 166), ("께", 167, 187),
                       ("협진", 199, 239)])
    spans = I.detect_lines([line], det)[0]
    assert [(s.entity, s.text) for s in spans] == [(E.PERSON, "이 서 연")]
    box = line.span_box(spans[0].start, spans[0].end)
    assert box == (30, 10, 92, 30)


def test_detect_lines_propagates_between_lines_and_label_slots():
    det = Detector(default_config())
    lines = [
        _word_line([("환자명:", 0, 60), ("홍길동", 70, 130)]),
        _word_line([("홍길동", 0, 60), ("혈압", 70, 110), ("재측정", 120, 180)], y=50),
        _word_line([("성명:", 0, 50), ("AMG", 60, 100), ("(남/45)", 110, 170)], y=90),  # garbled
        _word_line([("metformin", 0, 90), ("500mg", 100, 150)], y=130),
    ]
    spans = I.detect_lines(lines, det)
    assert [s.text for s in spans[0]] == ["홍길동"]
    assert [s.text for s in spans[1]] == ["홍길동"]
    assert [(s.entity, s.text) for s in spans[2]] == [(E.PERSON, "AMG")]
    assert spans[3] == []
    # Hangul garble is not an English name: only the label slot rule finds it
    garbled = _word_line([("성명:", 0, 50), ("ㅁㅁ삐", 60, 100)])
    assert [(s.text, s.source) for s in I.detect_lines([garbled], det)[0]] == \
        [("ㅁㅁ삐", I.SLOT_SOURCE)]
    assert I.detect_lines([garbled], det, slots=False) == [[]]


def test_detect_lines_fixes_common_surname_misread():
    det = Detector(default_config())
    line = _word_line([("흥길동", 0, 60), ("환자", 70, 110), ("내원", 120, 160)])
    spans = I.detect_lines([line], det)[0]
    assert [(s.entity, s.text) for s in spans] == [(E.PERSON, "흥길동")]


def test_variants_map_back_to_line_offsets():
    line = _word_line([("환", 0, 20), ("자", 21, 41), ("명:", 42, 62), ("홍길동", 75, 135)])
    texts = [t for t, _ in I._variants(line)]
    assert texts[0] == "환 자 명: 홍길동"
    assert "환자명: 홍길동" in texts
    for t, mp in I._variants(line):
        assert len(mp) == len(t)
        assert all(line.text[mp[i]] == ch for i, ch in enumerate(t))


def test_redact_lines_spans_paints_and_counts():
    img = Image.new("RGB", (300, 80), "white")
    line = _word_line([("환자명:", 10, 70), ("홍길동", 80, 140), ("2024-05-02", 150, 260)])
    s_name = line.text.index("홍길동")
    s_date = line.text.index("2024")
    spans = [[Span(s_name, s_name + 3, E.PERSON, "홍길동", 0.9, "names"),
              Span(s_date, s_date + 10, E.DATE, "2024-05-02", 0.85, "date")]]
    cfg = default_config()                           # DATE action is "keep"
    report = Report()
    out = I.redact_lines_spans(img, [line], spans, cfg, Anonymizer(cfg), report)
    assert img.getpixel((100, 20)) == (255, 255, 255)        # input untouched
    assert black_share(out, (80, 10, 140, 30)) == 1.0         # name covered (+ padding)
    assert out.getpixel((77, 20)) == (0, 0, 0)
    assert black_share(out, (155, 10, 255, 30)) == 0.0        # kept date not covered
    assert black_share(out, (10, 10, 60, 30)) == 0.0          # label not covered
    assert report.counts == {E.PERSON: 1, E.DATE: 1}
    with pytest.raises(ValueError):
        I.redact_lines_spans(img, [line], [], cfg)


def test_redact_lines_spans_estimated_lines_get_wider_padding():
    img = Image.new("L", (400, 60), 255)
    line = ocr.line_from_box("연락처 010-1234-5678", (10, 10, 250, 40), engine="vision")
    s = line.text.index("010")
    out = I.redact_lines_spans(img, [line], [[Span(s, s + 13, E.PHONE, "010-1234-5678",
                                                   0.9, "kr_phone")]])
    box = line.span_box(s, s + 13)
    assert out.mode == "L"
    assert out.getpixel((int(box[0]) - 10, 25)) == 0          # 0.45 x height padding
    assert out.getpixel((5, 25)) == 255


def test_redact_image_with_fake_vision_engine(monkeypatch):
    calls = []

    def fake_ocr(img, cfg=None, engine=None):
        calls.append(img.copy())
        line = ocr.line_from_box("환자명: 홍길동", (10.0, 20.0, 210.0, 50.0), 99.0, "vision")
        return ocr.OcrResult([line], "vision")
    monkeypatch.setattr(I, "ocr_image", fake_ocr)
    cfg = default_config()
    cfg.ocr["redact_faces"] = True
    img = Image.new("RGB", (600, 100), "white")
    out, report = I.redact_image(img, Detector(cfg), cfg, Anonymizer(cfg))
    assert len(calls) == 2                       # OCR + one re-check round
    assert out.getpixel((150, 35)) == (0, 0, 0)
    assert out.getpixel((30, 35)) == (255, 255, 255)
    assert report.counts == {E.PERSON: 1}         # re-check saw the same place: not re-counted
    assert I.FACES_WARNING in report.warnings
    assert not any("재검사" in w for w in report.warnings)


def test_recheck_covers_text_found_only_in_second_pass(monkeypatch):
    rounds = []
    name = _word_line([("환자명:", 10, 80), ("홍길동", 90, 160)], y=10)

    def fake_ocr(img, cfg=None, engine=None):
        rounds.append(1)
        if len(rounds) == 1:
            return ocr.OcrResult([name], "tesseract")
        return ocr.OcrResult([name,
                              _word_line([("연락처", 10, 70), ("010-2345-6789", 80, 220)], y=60)],
                             "tesseract")
    monkeypatch.setattr(I, "ocr_image", fake_ocr)
    monkeypatch.setattr(I, "detect_rotation", lambda img, cfg=None: 0)
    cfg = default_config()
    cfg.ocr["verify"] = 2
    out, report = I.redact_image(Image.new("RGB", (300, 100), "white"), Detector(cfg), cfg)
    assert out.getpixel((150, 70)) == (0, 0, 0)
    assert report.counts == {E.PERSON: 1, E.PHONE: 1}
    assert any("재검사" in w and "1" in w for w in report.warnings)
    assert len(rounds) == 3                        # stops after a clean round


def test_over_mask_16_no_recheck_when_nothing_was_covered(monkeypatch):
    """over_mask-16: an image in which nothing was covered is unchanged; OCR'ing it again can
    only repeat the first pass, so it is OCR'd once."""
    rounds = []

    def fake_ocr(img, cfg=None, engine=None):
        rounds.append(1)
        return ocr.OcrResult([_word_line([("metformin", 10, 100), ("500mg", 110, 170)])],
                             "tesseract")
    monkeypatch.setattr(I, "ocr_image", fake_ocr)
    monkeypatch.setattr(I, "detect_rotation", lambda img, cfg=None: 0)
    cfg = default_config()
    out, report = I.redact_image(Image.new("RGB", (300, 100), "white"), Detector(cfg), cfg)
    assert len(rounds) == 1 and report.counts == {}
    assert I.NO_TEXT_WARNING not in report.warnings


def test_working_image_modes():
    assert I.to_working_image(Image.new("P", (4, 4))).mode == "RGB"
    assert I.to_working_image(Image.new("1", (4, 4))).mode == "L"
    assert I.to_working_image(Image.new("LA", (4, 4))).mode == "RGBA"
    assert I.to_working_image(Image.new("CMYK", (4, 4))).mode == "RGB"
    i16 = Image.new("I;16", (4, 4), 65535)
    assert I.to_working_image(i16).getpixel((0, 0)) == 255


# ---------------------------------------------------------------------- red-team regressions


@needs_ocr
def test_file_leaks_04_sideways_picture_is_checked_upright(tmp_path):
    """file_leaks-04: a document photographed / scanned sideways (no EXIF orientation) used to
    pass through unchanged with no warning."""
    c = Canvas((1000, 330))
    c.text((30, 30), "환자명: 하은별", size=32)
    c.text((30, 130), "연락처 010-2233-4455", size=32)
    c.text((30, 230), "주민등록번호 850315-1234561", size=32)
    src, dst = tmp_path / "sideways.png", tmp_path / "sideways_out.png"
    c.img.rotate(90, expand=True).save(src)
    cfg = make_cfg()
    report = I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    assert report.counts.get(E.PHONE) == 1 and report.counts.get(E.RRN) == 1
    assert any("90" in w and "upright" in w for w in report.warnings)
    upright = Image.open(dst).rotate(-90, expand=True)
    assert upright.size == c.img.size
    assert not leaked(read_all(upright, scales=(1,)), ["010-2233-4455", "850315-1234561"])


def test_file_leaks_04_rotation_maps_back_onto_the_picture(monkeypatch):
    """file_leaks-04: covering done on the upright copy lands on the right pixels."""
    img = Image.new("RGB", (100, 300), "white")      # upright copy is 300 x 100

    def fake_ocr(im, cfg=None, engine=None):
        if im.size == (300, 100):                     # upright: a phone number at x 100-250
            return ocr.OcrResult([_word_line([("연락처", 10, 90), ("010-2345-6789", 100, 250)],
                                              y=40)], "tesseract")
        return ocr.OcrResult([], "tesseract")
    monkeypatch.setattr(I, "ocr_image", fake_ocr)
    monkeypatch.setattr(I, "detect_rotation", lambda im, cfg=None: 90)
    cfg = default_config()
    out, report = I.redact_image(img, Detector(cfg), cfg)
    assert report.counts == {E.PHONE: 1} and out.size == (100, 300)
    # upright (x, y) = (175, 50) is original (50, 300 - 175) after turning back
    assert out.getpixel((50, 125)) == (0, 0, 0)
    assert out.getpixel((50, 250)) == (255, 255, 255)     # 연락처 label itself stays


def test_file_leaks_04_no_orientation_check_for_confident_text(monkeypatch):
    """file_leaks-04: readable, confident tesseract output does not pay for OSD."""
    calls = []
    monkeypatch.setattr(I, "ocr_image", lambda im, cfg=None, engine=None: ocr.OcrResult(
        [_word_line([("metformin", 10, 100)])], "tesseract"))
    monkeypatch.setattr(I, "detect_rotation", lambda im, cfg=None: calls.append(1) or 0)
    cfg = default_config()
    I.redact_image(Image.new("RGB", (200, 60), "white"), Detector(cfg), cfg)
    assert calls == []
    cfg.ocr["orientation"] = True
    I.redact_image(Image.new("RGB", (200, 60), "white"), Detector(cfg), cfg)
    assert calls == [1]


def _kakao(name="하은별", sender_twice=False):
    c = Canvas((720, 560), bg=(178, 199, 217))
    c.draw.rectangle((0, 0, 720, 60), fill=(160, 180, 200))
    c.text((300, 15), name, size=26, mark=[name])
    c.draw.ellipse((20, 80, 80, 140), fill=(240, 200, 150))
    c.text((95, 80), name, size=22, fill=(60, 60, 60), mark=[name])
    c.draw.rounded_rectangle((95, 115, 640, 190), 12, fill="white")
    c.text((110, 125), "선생님 어제 처방받은 약 먹고 어지러워요", size=22)
    c.text((110, 155), "오후에 다시 가도 될까요?", size=22)
    c.draw.rounded_rectangle((330, 220, 700, 270), 12, fill=(254, 229, 0))
    c.text((345, 232), "네 오후 3시에 오세요", size=22)
    return c


@needs_ocr
def test_file_leaks_15_chat_title_and_sender_name_covered(tmp_path):
    """file_leaks-15: the chat partner's bare name (title bar, sender label) is covered."""
    c = _kakao()
    src, dst = tmp_path / "kakao.png", tmp_path / "kakao_out.png"
    c.img.save(src)
    cfg = make_cfg()
    report = I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    for box in c.boxes["하은별"]:
        assert black_share(out, box) > 0.9
    assert "하은별" not in re.sub(r"\s+", "", read_all(out, scales=(1,)))
    assert "어지러워요" in re.sub(r"\s+", "", read_all(out, scales=(1,)))
    assert report.counts.get(E.PERSON) == 2


def _line_at(text, x, y, h=24, conf=90.0):
    words, pos = [], x
    for t in text.split(" "):
        w = 22 * len(t)
        words.append(OcrWord(t, (pos, y, pos + w, y + h), conf))
        pos += w + 10
    return OcrLine.from_words(words, "test")


def test_file_leaks_15_bare_name_rules():
    """file_leaks-15: a lone name-shaped line counts when repeated or in the title bar; ordinary
    words, verbs, lines with other text on the row, and single occurrences mid-picture don't."""
    size = (720, 560)
    title = _line_at("하 은 별", 300, 15)
    sender = _line_at("하은별", 95, 80)
    assert I.bare_name_spans([title], size) == {0: [(0, 5)]}           # title bar
    assert I.bare_name_spans([sender], size) == {}                      # once, mid-picture
    assert I.bare_name_spans([sender, _line_at("하은별", 95, 300)], size) == {0: [(0, 3)],
                                                                         1: [(0, 3)]}
    for word in ("고혈압", "고혈안", "오세요", "강아지", "상병명", "주세요", "진료의", "하은", "이메일"):
        assert I.bare_name_spans([_line_at(word, 300, 15)], size) == {}, word
    # another word on the same row: a table cell / label, not a lone name
    row = [_line_at("하은별", 300, 15), _line_at("외래", 400, 15)]
    assert I.bare_name_spans(row, size) == {}
    far = [_line_at("하은별", 300, 15), _line_at("2024-03-01", 650, 15)]   # anywhere on the row
    assert I.bare_name_spans(far, size) == {}
    icons = [_line_at("하은별", 300, 15), _line_at("Q", 650, 15), _line_at("<", 10, 15)]
    assert I.bare_name_spans(icons, size) == {0: [(0, 3)]}              # icon noise ignored
    # wired into detect_lines (and switchable)
    det = Detector(default_config())
    spans = I.detect_lines([title], det, size=size)[0]
    assert [(s.entity, s.source) for s in spans] == [(E.PERSON, I.BARE_NAME_SOURCE)]
    cfg = default_config()
    cfg.ocr["bare_names"] = False
    assert I.detect_lines([title], Detector(cfg), size=size)[0] == []
    cfg = default_config()
    cfg.allowlist = ["하은별"]
    assert I.detect_lines([title], Detector(cfg), size=size)[0] == []


CODE39 = {"0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn",
          "4": "nnnwwnnnw", "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw",
          "8": "wnnwnnwnn", "9": "nnwwnnwnn", "-": "nwnnnnwnw", "*": "nwnnwnwnn"}


def _code39(draw, x, y, text, h=90, narrow=3, wide=8):
    x0 = x
    for ch in f"*{text}*":
        for i, el in enumerate(CODE39[ch]):
            w = wide if el == "w" else narrow
            if i % 2 == 0:
                draw.rectangle((x, y, x + w - 1, y + h), fill="black")
            x += w
        x += narrow
    return (x0, y, x, y + h)


def _matrix_code(draw, x, y, n=29, m=6, seed=7):
    import random
    rng = random.Random(seed)
    for r in range(n):
        for c in range(n):
            if rng.random() < 0.5:
                draw.rectangle((x + c * m, y + r * m, x + c * m + m - 1, y + r * m + m - 1),
                               fill="black")
    for fx, fy in ((0, 0), (n - 7, 0), (0, n - 7)):        # QR-style finder patterns
        for k, col in ((7, "black"), (5, "white"), (3, "black")):
            o = (7 - k) // 2
            draw.rectangle((x + (fx + o) * m, y + (fy + o) * m, x + (fx + o + k) * m - 1,
                            y + (fy + o + k) * m - 1), fill=col)
    return (x, y, x + n * m, y + n * m)


def test_file_leaks_16_find_codes_bars_and_matrix_not_text():
    """file_leaks-16: 1-D barcodes and 2-D (QR-like) codes are found; text, a table grid and
    a bar chart are not."""
    img = Image.new("RGB", (900, 400), "white")
    d = ImageDraw.Draw(img)
    bar = _code39(d, 20, 20, "850315-1234561")
    qr = _matrix_code(d, 600, 180)
    found = I.find_codes(img)
    assert any(I._inside_share(bar, f) > 0.95 for f in found)
    assert any(I._inside_share(qr, f) > 0.95 for f in found)
    assert len(found) == 2
    plain = Canvas((900, 300))
    for i in range(5):
        plain.text((20, 20 + 50 * i), "경과기록 혈압 132/84 mmHg metformin 500mg 1111 lllll", size=22)
    for x in range(0, 900, 90):
        plain.draw.line((x, 0, x, 300), fill="black")
    assert I.find_codes(plain.img, ocr.OcrResult([], "x").lines) == [] or HAS_TESSERACT
    chart = Image.new("RGB", (900, 400), "white")
    dc = ImageDraw.Draw(chart)
    for i in range(30):
        dc.rectangle((20 + i * 28, 380 - 10 * (i % 9 + 3), 38 + i * 28, 380), fill=(40, 90, 200))
    assert I.find_codes(chart) == []


@needs_ocr
def test_file_leaks_16_barcode_on_label_is_covered(tmp_path):
    """file_leaks-16: the printed RRN was covered but its barcode still decoded to it."""
    c = Canvas((760, 260))
    c.text((20, 15), "환자명: 하은별", size=28)
    c.text((20, 55), "주민등록번호 850315-1234561", size=28)
    bar = _code39(c.draw, 20, 120, "850315-1234561")
    src, dst = tmp_path / "label.png", tmp_path / "label_out.png"
    c.img.save(src)
    cfg = make_cfg()
    report = I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    assert black_share(Image.open(dst), bar) > 0.97
    assert report.counts.get(E.OTHER) == 1 and report.counts.get(E.RRN) == 1
    assert any("barcode" in w for w in report.warnings)
    # plain text images get no code warning
    t = Canvas((760, 120))
    t.text((20, 40), "혈압 132/84 mmHg metformin 500mg", size=26)
    _, rep = I.redact_image(t.img, Detector(cfg), cfg)
    assert E.OTHER not in rep.counts and not any("barcode" in w for w in rep.warnings)


def test_file_leaks_16_barcode_modes(monkeypatch):
    """file_leaks-16: barcodes = "warn" only warns, "off" skips the search."""
    img = Image.new("RGB", (760, 160), "white")
    bar = _code39(ImageDraw.Draw(img), 20, 30, "12345678")
    monkeypatch.setattr(I, "ocr_image", lambda im, cfg=None, engine=None: ocr.OcrResult(
        [], "tesseract"))
    monkeypatch.setattr(I, "detect_rotation", lambda im, cfg=None: 0)
    cfg = default_config()
    out, rep = I.redact_image(img, Detector(cfg), cfg)
    assert black_share(out, bar) > 0.97 and rep.counts == {E.OTHER: 1}
    assert I.NO_TEXT_WARNING not in rep.warnings
    cfg.ocr["barcodes"] = "warn"
    out, rep = I.redact_image(img, Detector(cfg), cfg)
    assert black_share(out, bar) < 0.6 and rep.counts == {}
    assert any("NOT covered" in w for w in rep.warnings)
    cfg.ocr["barcodes"] = "off"
    out, rep = I.redact_image(img, Detector(cfg), cfg)
    assert not any("barcode" in w for w in rep.warnings)


def test_file_leaks_21_colour_under_transparency_is_blanked(tmp_path, monkeypatch):
    """file_leaks-21: text 'erased to transparent' kept its colour data and reappeared once the
    alpha channel was ignored."""
    img = Image.new("RGBA", (300, 100), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 50, 300, 100), fill=(255, 255, 255, 0))
    d.rectangle((20, 60, 200, 90), fill=(0, 0, 0, 0))          # black, but fully transparent
    d.rectangle((20, 10, 60, 30), fill=(0, 0, 0, 255))          # visible: untouched
    src, dst = tmp_path / "erased.png", tmp_path / "erased_out.png"
    img.save(src)
    monkeypatch.setattr(I, "ocr_image", lambda im, cfg=None, engine=None: ocr.OcrResult(
        [], "tesseract"))
    monkeypatch.setattr(I, "detect_rotation", lambda im, cfg=None: 0)
    cfg = default_config()
    I.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(dst)
    assert out.mode == "RGBA"
    assert out.getpixel((100, 75)) == (255, 255, 255, 0)
    assert out.getpixel((40, 20)) == (0, 0, 0, 255)
    la = Image.new("LA", (10, 10), (0, 0))
    assert I.clear_hidden_colour(la).getpixel((5, 5)) == (255, 0)
    # save_clean also blanks it (frames that did not go through redact_image)
    I.save_clean([img], tmp_path / "direct.png")
    assert Image.open(tmp_path / "direct.png").getpixel((100, 75)) == (255, 255, 255, 0)


def test_security_image_pixel_bomb_refused_before_decoding(tmp_path, monkeypatch):
    """security-image-pixel-bomb: page count and total pixels are checked from the headers,
    before any page is decoded or OCR'd."""
    called = []
    real_redact = I.redact_image
    monkeypatch.setattr(I, "redact_image", lambda *a, **k: called.append(1))
    src = tmp_path / "bomb.tiff"
    frames = [Image.new("1", (400, 300), 1) for _ in range(5)]
    frames[0].save(src, save_all=True, append_images=frames[1:], compression="group4")
    cfg = default_config()
    for key, value, msg in (("max_total_pixels", 500_000, "too many pixels"),
                            ("max_frames", 4, "too many pages"),
                            ("max_image_pixels", 100_000, "image too large")):
        cfg.ocr = {key: value}
        with pytest.raises(I.ImageTooLargeError, match=msg):
            I.process(src, tmp_path / "out.tiff", Detector(cfg), Anonymizer(cfg), cfg)
    assert called == [] and not (tmp_path / "out.tiff").exists()
    assert not list(tmp_path.glob(".deid-*"))
    # defaults: a 60-page 36 MP file (2.16 G pixels) is over budget; one frame > 100 M too
    with pytest.raises(I.ImageTooLargeError):
        I.check_pixels((6000, 6000), None, frames=60, total=60 * 36_000_000)
    with pytest.raises(I.ImageTooLargeError):
        I.check_pixels((12000, 9000), None)
    I.check_pixels((4961, 7016), None, frames=40, total=40 * 4961 * 7016)   # 40 A4 @600dpi
    with pytest.raises(I.ImageTooLargeError):     # also images from PDFs / DOCX / HWPX
        real_redact(Image.new("1", (11000, 10000)), Detector(cfg), cfg)


def test_security_image_pixel_bomb_tiff_pages_are_streamed(tmp_path, monkeypatch):
    """security-image-pixel-bomb: a multi-page TIFF is written page by page (only one redacted
    page is alive at a time) and keeps all its pages."""
    import gc
    import weakref

    alive = []
    monkeypatch.setattr(I, "ocr_image", lambda im, cfg=None, engine=None: ocr.OcrResult(
        [], "tesseract"))
    monkeypatch.setattr(I, "detect_rotation", lambda im, cfg=None: 0)
    real = I.redact_image

    def tracking(img, *a, **k):
        gc.collect()
        alive.append(sum(1 for r in refs if r() is not None))
        out, rep = real(img, *a, **k)
        refs.append(weakref.ref(out))
        return out, rep
    refs: list = []
    monkeypatch.setattr(I, "redact_image", tracking)
    src = tmp_path / "scan.tiff"
    frames = [Image.new("L", (200 + i, 100), 255) for i in range(6)]
    frames[0].save(src, save_all=True, append_images=frames[1:])
    cfg = default_config()
    I.process(src, tmp_path / "out.tif", Detector(cfg), Anonymizer(cfg), cfg)
    out = Image.open(tmp_path / "out.tif")
    assert out.n_frames == 6
    assert [(out.seek(i), out.size)[1] for i in range(6)] == [(200 + i, 100) for i in range(6)]
    assert max(alive) <= 1
