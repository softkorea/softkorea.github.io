"""Tests for deid.formats.pdf: synthetic PDFs built with PyMuPDF (Korean font), the OUTPUT is
re-opened and its text extracted to prove PII is gone while clinical text survives.
Synthetic people only."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

fitz = pytest.importorskip("pymupdf")

from deid import ocr  # noqa: E402
from deid.anonymizer import Anonymizer  # noqa: E402
from deid.config import default_config  # noqa: E402
from deid.engine import Detector  # noqa: E402
from deid.formats import pdf as P  # noqa: E402

NANUM = Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
HAS_TESSERACT = "tesseract" in ocr.available_engines() and "kor" in ocr.tesseract_languages()
needs_ocr = pytest.mark.skipif(not (HAS_TESSERACT and NANUM.exists()),
                               reason="tesseract (kor) or NanumGothic font missing")


def make_cfg(**ocr_opts):
    cfg = default_config()
    cfg.ocr.update({"engine": "tesseract"})
    cfg.ocr.update(ocr_opts)
    return cfg


def run(src: Path, dst: Path, cfg=None):
    cfg = cfg or make_cfg()
    return P.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)


class Pdf:
    """Small builder: Korean text via NanumGothic (embedded) or the built-in CJK font."""

    def __init__(self):
        self.doc = fitz.open()
        self.page = None

    def new_page(self, width=595, height=842):
        self.page = self.doc.new_page(width=width, height=height)
        if NANUM.exists():
            self.page.insert_font(fontname="ng", fontfile=str(NANUM))
            self.font = "ng"
        else:  # pragma: no cover - other machines
            self.font = "korea"
        return self.page

    def text(self, xy, s, size=11, **kw):
        self.page.insert_text(xy, s, fontname=self.font, fontsize=size, **kw)

    def save(self, path: Path, **kw) -> Path:
        self.doc.save(str(path), **kw)
        return path


def all_text(path: Path) -> str:
    doc = fitz.open(str(path))
    try:
        parts = []
        for page in doc:
            parts.append(page.get_text())
            parts.append(page.get_text(flags=fitz.TEXT_IGNORE_ACTUALTEXT))
            for annot in page.annots():
                parts += [annot.info.get("content", ""), annot.info.get("title", ""),
                          annot.info.get("subject", "")]
            for w in page.widgets():
                parts.append(str(w.field_value or ""))
        parts += [t[1] for t in doc.get_toc()]
        parts += [str(v) for v in (doc.metadata or {}).values() if v]
        return "\n".join(parts).replace("\xa0", " ")
    finally:
        doc.close()


def compact(s: str) -> str:
    return re.sub(r"\s+", "", s)


# ---------------------------------------------------------------------------------------------
# Text PDFs
# ---------------------------------------------------------------------------------------------


def test_text_pdf_cross_page_propagation(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "환자명: 홍길동 (남/45세)")
    b.text((72, 120), "주소: 서울특별시 강남구 테헤란로 123")
    b.text((72, 140), "연락처 010-1234-5678, 주민번호 900101-1234567")
    b.text((72, 160), "진단: 고혈압, 이상지질혈증. 진료일 2024-03-15")
    b.new_page()
    b.text((72, 100), "경과: 홍길동 당뇨 조절 양호, 메트포르민 유지")   # no context: propagation
    src = b.save(tmp_path / "in.pdf")
    rep = run(src, tmp_path / "out.pdf")
    out = all_text(tmp_path / "out.pdf")
    for pii in ("홍길동", "010-1234-5678", "1234-5678", "900101-1234567", "테헤란로"):
        assert pii not in out and compact(pii) not in compact(out)
    for clinical in ("고혈압", "이상지질혈증", "당뇨", "메트포르민", "2024-03-15"):
        assert clinical in out
    assert "[이름1]" in out
    doc = fitz.open(str(tmp_path / "out.pdf"))
    assert "[이름1]" in doc[1].get_text()            # same token on page 2
    assert "010-****-5678" in doc[0].get_text()      # mask overlay is real text
    doc.close()
    assert rep.counts["PERSON"] >= 2 and rep.counts["PHONE"] == 1 and rep.counts["RRN"] == 1
    assert rep.source.endswith("in.pdf") and rep.output.endswith("out.pdf")
    assert "홍길동" not in str(rep.to_dict())


def test_redaction_removes_glyphs_and_draws_box(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "보호자 김민준 연락 바람")
    src = b.save(tmp_path / "in.pdf")
    run(src, tmp_path / "out.pdf")
    doc = fitz.open(str(tmp_path / "out.pdf"))
    page = doc[0]
    words = [w[4] for w in page.get_text("words")]
    assert not any("김민준" in w for w in words)
    # a black box covers where the name was
    pix = page.get_pixmap(dpi=72, clip=fitz.Rect(100, 88, 140, 102))
    dark = sum(1 for i in range(0, len(pix.samples), pix.n) if pix.samples[i] < 60)
    assert dark > 0.4 * pix.width * pix.height
    doc.close()


def test_table_row_label_in_separate_cell(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "성명")        # label and value are separate text lines on one row
    b.text((250, 100), "이서연")
    b.text((72, 120), "진단")
    b.text((250, 120), "급성 기관지염")
    src = b.save(tmp_path / "in.pdf")
    run(src, tmp_path / "out.pdf")
    out = all_text(tmp_path / "out.pdf")
    assert "이서연" not in out
    assert "급성 기관지염" in out or "급성기관지염" in compact(out)


def test_metadata_link_toc_annotations_embedded(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "환자 홍길동님 검사 결과: 정상 범위")
    b.text((72, 120), "문의: minjun@example.com")
    b.page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 108, 300, 124),
                        "uri": "mailto:minjun@example.com"})
    b.page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 300, 200, 320),
                        "uri": "https://example.org/guide"})
    annot = b.page.add_text_annot((400, 400), "김민준 보호자 재진 예약 필요")
    annot.set_info(title="이서연", subject="홍길동 메모")
    annot.update()
    b.doc.set_toc([[1, "홍길동 검사결과", 1]])
    b.doc.set_metadata({"author": "이서연", "title": "홍길동 검사결과", "subject": "홍길동",
                        "keywords": "홍길동, 검사", "creator": "EMR"})
    b.doc.set_xml_metadata('<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                           '홍길동</x:xmpmeta><?xpacket end="w"?>')
    b.doc.embfile_add("memo.txt", "홍길동 010-1234-5678".encode("utf-8"))
    src = b.save(tmp_path / "in.pdf")
    rep = run(src, tmp_path / "out.pdf")
    doc = fitz.open(str(tmp_path / "out.pdf"))
    md = doc.metadata
    for key in ("author", "title", "subject", "keywords", "creator"):
        assert not md.get(key)
    assert not doc.get_xml_metadata()
    assert doc.embfile_count() == 0
    uris = [lk.get("uri") for lk in doc[0].get_links()]
    assert "mailto:minjun@example.com" not in uris
    assert "https://example.org/guide" in uris          # ordinary links stay
    toc = doc.get_toc()
    assert toc and "홍길동" not in toc[0][1] and "검사결과" in toc[0][1]
    annots = list(doc[0].annots())
    assert len(annots) == 1
    assert annots[0].info["title"] == ""
    assert "김민준" not in annots[0].info["content"] and "재진 예약" in annots[0].info["content"]
    assert "홍길동" not in annots[0].info["subject"]
    doc.close()
    out = all_text(tmp_path / "out.pdf")
    for pii in ("홍길동", "김민준", "이서연", "minjun@example.com"):
        assert pii not in out
    assert any("첨부" in w for w in rep.warnings)
    assert any("링크" in w for w in rep.warnings)


def test_author_registered_as_known_name(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "작성 최수아 / 검토 완료")    # author name without honorific in the text
    b.doc.set_metadata({"author": "최수아"})
    src = b.save(tmp_path / "in.pdf")
    run(src, tmp_path / "out.pdf")
    assert "최수아" not in all_text(tmp_path / "out.pdf")


def test_form_field_values_typed_by_field_name(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 80), "진료 신청서")
    for i, (name, value) in enumerate((("성명", "박지훈"), ("연락처", "010-9876-5432"),
                                       ("증상", "기침, 발열"))):
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = name
        w.field_value = value
        w.rect = fitz.Rect(72, 100 + 30 * i, 300, 120 + 30 * i)
        b.page.add_widget(w)
    src = b.save(tmp_path / "in.pdf")
    run(src, tmp_path / "out.pdf")
    doc = fitz.open(str(tmp_path / "out.pdf"))
    values = {w.field_name: w.field_value for w in doc[0].widgets()}
    doc.close()
    assert "박지훈" not in values["성명"] and values["성명"]
    assert "9876-5432" not in values["연락처"]
    assert values["증상"] == "기침, 발열"


def test_rotated_page(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "환자명: 홍길동 / 진단: 편두통")
    b.page.set_rotation(90)
    src = b.save(tmp_path / "in.pdf")
    run(src, tmp_path / "out.pdf")
    out = all_text(tmp_path / "out.pdf")
    assert "홍길동" not in out and "편두통" in out


def test_hidden_layer_text_is_checked(tmp_path):
    b = Pdf()
    b.new_page()
    ocg = b.doc.add_ocg("숨김", on=False)
    b.text((72, 100), "공개 메모: 혈압 조절 양호")
    b.text((72, 140), "숨은 메모: 환자명: 홍길동", oc=ocg)
    src = b.save(tmp_path / "in.pdf")
    assert "홍길동" not in fitz.open(str(src))[0].get_text()   # invisible to plain extraction
    rep = run(src, tmp_path / "out.pdf")
    doc = fitz.open(str(tmp_path / "out.pdf"))
    assert not doc.get_ocgs() or not doc.layer_ui_configs()   # no layer can hide text now
    text = doc[0].get_text().replace("\xa0", " ")
    doc.close()
    assert "홍길동" not in text and "혈압 조절 양호" in text
    assert any("레이어" in w for w in rep.warnings)


def test_text_outside_the_page_is_removed(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "본문: 상태 안정")
    b.text((700, 100), "환자명: 홍길동")      # beyond the right edge (x > 595)
    src = b.save(tmp_path / "in.pdf")
    run(src, tmp_path / "out.pdf")
    doc = fitz.open(str(tmp_path / "out.pdf"))
    text = doc[0].get_text(clip=fitz.INFINITE_RECT())
    doc.close()
    assert "홍길동" not in text and "상태 안정" in text.replace("\xa0", " ")


def test_keep_entities_and_clinical_numbers_untouched(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "검사일 2024-03-15 HbA1c 7.2% LDL 130 mg/dL")
    src = b.save(tmp_path / "in.pdf")
    rep = run(src, tmp_path / "out.pdf")
    out = all_text(tmp_path / "out.pdf")
    assert "2024-03-15" in out and "HbA1c 7.2%" in out and "130" in out
    assert rep.counts.get("DATE", 0) == 1


def test_password_protected_pdf_is_refused(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "환자명: 홍길동")
    src = b.save(tmp_path / "in.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="1234",
                 owner_pw="5678")
    dst = tmp_path / "out.pdf"
    with pytest.raises(P.PdfPasswordError) as ei:
        run(src, dst)
    assert "암호" in str(ei.value) and "password" in str(ei.value)
    assert not dst.exists()


def test_owner_password_only_pdf_is_processed(tmp_path):
    b = Pdf()
    b.new_page()
    b.text((72, 100), "환자명: 홍길동, 진단: 위염")
    src = b.save(tmp_path / "in.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="5678",
                 permissions=fitz.PDF_PERM_PRINT)
    run(src, tmp_path / "out.pdf")
    doc = fitz.open(str(tmp_path / "out.pdf"))
    assert not doc.needs_pass and not doc.is_encrypted
    doc.close()
    assert "홍길동" not in all_text(tmp_path / "out.pdf")


def test_broken_file_and_same_path(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    with pytest.raises(P.PdfError):
        run(bad, tmp_path / "out.pdf")
    assert not (tmp_path / "out.pdf").exists()
    b = Pdf()
    b.new_page()
    b.text((72, 100), "메모")
    src = b.save(tmp_path / "in.pdf")
    with pytest.raises(ValueError):
        run(src, src)


def test_fail_closed_when_redaction_cannot_remove_text(tmp_path, monkeypatch):
    """If removal silently fails and the image fallback is unavailable, nothing is written."""
    b = Pdf()
    b.new_page()
    b.text((72, 100), "환자명: 홍길동")
    src = b.save(tmp_path / "in.pdf")
    monkeypatch.setattr(fitz.Page, "apply_redactions", lambda self, *a, **k: True)
    monkeypatch.setattr(P._PdfJob, "rasterize", lambda self, pno, ocr=True: None)
    dst = tmp_path / "out.pdf"
    with pytest.raises(P.PdfVerifyError) as ei:
        run(src, dst)
    assert "홍길동" not in str(ei.value)
    assert not dst.exists()


def _actualtext_pdf(path: Path) -> Path:
    """'Patient XX visited' whose /ActualText says the hidden words are a name."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Patient XX visited", fontsize=11)
    xref = page.get_contents()[0]
    doc.update_stream(xref, b"q BT 1 0 0 1 72 742 Tm /helv 11 Tf (Patient ) Tj "
                            b"/Span <</ActualText (Mr John Smith)>> BDC (XX) Tj EMC "
                            b"( visited for asthma review) Tj ET Q")
    doc.save(str(path))
    return path


@needs_ocr
def test_actualtext_leftover_triggers_image_fallback(tmp_path):
    src = _actualtext_pdf(tmp_path / "in.pdf")
    assert "John Smith" in fitz.open(str(src))[0].get_text()
    rep = run(src, tmp_path / "out.pdf")
    out = all_text(tmp_path / "out.pdf")
    assert "John" not in out and "Smith" not in out
    assert any("그림으로 바꿨습니다" in w for w in rep.warnings)


def test_images_on_text_pages_are_reported_when_not_checked(tmp_path):
    from PIL import Image

    b = Pdf()
    b.new_page()
    b.text((72, 100), "첨부 사진 참조")
    buf = io.BytesIO()
    Image.new("RGB", (200, 120), (120, 160, 200)).save(buf, format="PNG")
    b.page.insert_image(fitz.Rect(72, 300, 172, 360), stream=buf.getvalue())
    src = b.save(tmp_path / "in.pdf")
    rep = run(src, tmp_path / "out.pdf", make_cfg(pdf_images=False))
    assert any("이미지 1개는 검사하지 않음" in w for w in rep.warnings)


# ---------------------------------------------------------------------------------------------
# Scanned pages (OCR)
# ---------------------------------------------------------------------------------------------


def _scan_image(lines, size=(1240, 1754), font_size=40):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(NANUM), font_size)
    y = 150
    for line in lines:
        draw.text((100, y), line, font=font, fill="black")
        y += 80
    return img


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _ocr_text(img) -> str:
    gray = img.convert("L")
    out = []
    for psm in (4, 6, 11):
        lines, _, _ = ocr.lines_from_tesseract(ocr.tesseract_data(gray, "kor+eng", psm))
        out += [ln.text for ln in lines]
    return "\n".join(out)


def _page_image(path: Path, pno: int = 0, dpi: int = 150):
    from PIL import Image

    doc = fitz.open(str(path))
    pix = doc[pno].get_pixmap(dpi=dpi, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    return img


@needs_ocr
def test_scanned_pdf_page_is_ocr_redacted_and_rebuilt(tmp_path):
    pytest.importorskip("deid.formats.image")
    img = _scan_image(["진료의뢰서", "환자명: 홍길동", "연락처: 010-1234-5678",
                       "진단: 고혈압, 당뇨병"])
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=_png(img))
    src = tmp_path / "scan.pdf"
    doc.save(str(src))
    rep = run(src, tmp_path / "out.pdf")
    assert rep.counts.get("PERSON", 0) >= 1 and rep.counts.get("PHONE", 0) >= 1
    assert any("스캔" in w for w in rep.warnings)
    out = fitz.open(str(tmp_path / "out.pdf"))
    assert out.page_count == 1 and not out[0].get_text().strip()
    assert abs(out[0].rect.width - 595) < 1 and abs(out[0].rect.height - 842) < 1
    out.close()
    seen = compact(_ocr_text(_page_image(tmp_path / "out.pdf")))
    assert "홍길동" not in seen and "1234" not in seen and "5678" not in seen
    assert "고혈압" in seen or "당뇨" in seen


@needs_ocr
def test_searchable_scan_with_invisible_ocr_layer(tmp_path):
    """An invisible OCR text layer over a scan: the layer feeds detection, the page is
    rebuilt as an image (no hidden text survives)."""
    pytest.importorskip("deid.formats.image")
    img = _scan_image(["경과기록", "보호자 김민준 동반", "혈압 130/80"])
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=_png(img))
    page.insert_font(fontname="ng", fontfile=str(NANUM))
    page.insert_text((48, 120), "보호자 김민준 동반", fontname="ng", fontsize=19, render_mode=3)
    src = tmp_path / "ocr.pdf"
    doc.save(str(src))
    run(src, tmp_path / "out.pdf")
    out = fitz.open(str(tmp_path / "out.pdf"))
    assert not out[0].get_text().strip()
    out.close()
    assert "김민준" not in compact(_ocr_text(_page_image(tmp_path / "out.pdf")))


@needs_ocr
def test_mixed_document_text_name_reaches_scanned_page(tmp_path):
    pytest.importorskip("deid.formats.image")
    b = Pdf()
    b.new_page()
    b.text((72, 100), "환자명: 최수아")
    page = b.doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=_png(_scan_image(["최수아 진료 소견", "기관지염 호전"])))
    src = b.save(tmp_path / "mixed.pdf")
    rep = run(src, tmp_path / "out.pdf")
    assert "최수아" not in all_text(tmp_path / "out.pdf")
    seen = compact(_ocr_text(_page_image(tmp_path / "out.pdf", 1)))
    assert "최수아" not in seen
    assert any("스캔" in w for w in rep.warnings)


@needs_ocr
def test_large_image_on_text_page_is_ocr_checked(tmp_path):
    pytest.importorskip("deid.formats.image")
    b = Pdf()
    b.new_page()
    b.text((72, 60), "첨부: 외부 의뢰서 사본")
    img = _scan_image(["환자명: 홍길동", "진단: 요추 염좌"], size=(1240, 600))
    b.page.insert_image(fitz.Rect(40, 100, 555, 349), stream=_png(img))
    src = b.save(tmp_path / "in.pdf")
    rep = run(src, tmp_path / "out.pdf")          # default pdf_images = "auto"
    assert rep.counts.get("PERSON", 0) >= 1
    seen = compact(_ocr_text(_page_image(tmp_path / "out.pdf", dpi=200)))
    assert "홍길동" not in seen
    assert "첨부" in all_text(tmp_path / "out.pdf")   # the text layer stays text


def test_form_only_page_is_processed(tmp_path):
    """A page holding nothing but a filled-in form field is not 'blank'."""
    doc = fitz.open()
    page = doc.new_page()
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_name = "환자명"
    w.field_value = "정하은"
    w.rect = fitz.Rect(72, 100, 300, 120)
    page.add_widget(w)
    src = tmp_path / "form.pdf"
    doc.save(str(src))
    run(src, tmp_path / "out.pdf")
    out = fitz.open(str(tmp_path / "out.pdf"))
    values = [wd.field_value for wd in out[0].widgets()]
    out.close()
    assert values and "정하은" not in values[0]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rasterized_page_covers_text_findings_on_rotated_pages(tmp_path, rotation):
    """Rebuilding a page as an image paints text-layer findings at the right place."""
    from PIL import Image

    b = Pdf()
    b.new_page()
    b.text((300, 400), "환자명: 홍길동", size=20)
    b.page.set_rotation(rotation)
    src = b.save(tmp_path / "in.pdf")
    words = [w for w in fitz.open(str(src))[0].get_text("words") if "홍길동" in w[4]]
    cfg = make_cfg()
    doc, _ = P._open(src, fitz)
    page = doc[0]
    zoom = 300 / 72
    target = fitz.Rect(words[0][:4]) * page.rotation_matrix * fitz.Matrix(zoom, zoom)
    job = P._PdfJob(doc, fitz, Detector(cfg), Anonymizer(cfg), cfg, P.Report())
    job.classify()
    job.collect_slots()
    job.batch.run()
    job.rasterize(0, ocr=False)
    pix = doc[0].get_pixmap(dpi=300)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    hist = img.crop(tuple(int(v) for v in target)).histogram()
    assert sum(hist[:60]) / sum(hist) > 0.95
    assert not doc[0].get_text().strip()
    doc.close()


@needs_ocr
def test_annotation_that_cannot_be_rewritten_is_caught(tmp_path, monkeypatch):
    """If writing an annotation fails, verification notices the leftover and the page is
    rebuilt as an image (annotations are flattened away)."""
    b = Pdf()
    b.new_page()
    b.text((72, 100), "외래 경과 기록")
    annot = b.page.add_text_annot((300, 300), "보호자 김민준 연락 요망")
    annot.update()
    src = b.save(tmp_path / "in.pdf")
    monkeypatch.setattr(fitz.Annot, "set_info", lambda self, *a, **k: (_ for _ in ()).throw(
        ValueError("boom")))
    # 1) the object-string sweep rewrites the annotation's /Contents itself: no rasterizing
    rep = run(src, tmp_path / "out.pdf")
    assert "김민준" not in all_text(tmp_path / "out.pdf")
    assert not any("그림으로 바꿨습니다" in w for w in rep.warnings)
    assert any("문자열" in w for w in rep.warnings)
    # 2) without the sweep, verification notices the leftover and rebuilds the page
    monkeypatch.setattr(P._PdfJob, "sweep_strings", lambda self: None)
    rep = run(src, tmp_path / "out2.pdf")
    assert "김민준" not in all_text(tmp_path / "out2.pdf")
    assert any("그림으로 바꿨습니다" in w for w in rep.warnings)


def test_exif_inside_embedded_jpeg_is_stripped(tmp_path):
    from PIL import Image

    exif = Image.Exif()
    exif[0x013B] = "Hong Gildong"                      # Artist
    exif[0x8825] = {1: "N", 2: (37.0, 30.0, 0.0)}     # GPS
    buf = io.BytesIO()
    Image.new("RGB", (200, 150), (10, 120, 30)).save(buf, format="JPEG", exif=exif.tobytes())
    b = Pdf()
    b.new_page()
    b.text((72, 60), "상처 사진")
    b.page.insert_image(fitz.Rect(72, 80, 272, 230), stream=buf.getvalue())
    src = b.save(tmp_path / "in.pdf")
    assert b"Hong Gildong" in src.read_bytes()
    run(src, tmp_path / "out.pdf", make_cfg(pdf_images=False))
    data = (tmp_path / "out.pdf").read_bytes()
    assert b"Hong Gildong" not in data
    doc = fitz.open(str(tmp_path / "out.pdf"))
    pix = doc[0].get_pixmap(dpi=72, clip=fitz.Rect(150, 140, 160, 150))
    doc.close()
    r, g, bl = pix.samples[:3]
    assert abs(r - 10) < 15 and abs(g - 120) < 15 and abs(bl - 30) < 15   # still renders


def test_names_wrapped_inside_a_word_across_lines(tmp_path):
    """Korean text is often wrapped mid-word ("홍길" / "동님은"); English at spaces."""
    b = Pdf()
    b.new_page()
    b.text((72, 100), "진료 경과: 환자 홍길\n동님은 혈압 안정, 보호자 김민\n준 씨 동반함. Seen with Mr John\n"
                      "Smith today.")
    src = b.save(tmp_path / "in.pdf")
    rep = run(src, tmp_path / "out.pdf")
    doc = fitz.open(str(tmp_path / "out.pdf"))
    text = doc[0].get_text()
    doc.close()
    flat = compact(text)
    for frag in ("홍길", "김민", "John", "Smith"):
        assert frag not in flat, frag
    assert "님은혈압안정" in flat and "today" in flat
    assert text.count("[이름") >= 3          # placeholders are extractable text
    assert rep.counts["PERSON"] <= 4         # seam findings are not double counted


# ---------------------------------------------------------------------------------------------
# Red-team regressions
# ---------------------------------------------------------------------------------------------


def _image_samples(path: Path) -> list[bytes]:
    d = fitz.open(str(path))
    out = []
    for x in range(1, d.xref_length()):
        try:
            if d.xref_get_key(x, "Subtype") == ("name", "/Image"):
                out.append(fitz.Pixmap(d, x).samples)
        except Exception:  # noqa: BLE001
            pass
    d.close()
    return out


def _orphan_pages(path: Path) -> list[int]:
    d = fitz.open(str(path))
    live = {p.xref for p in d}
    out = [x for x in range(1, d.xref_length())
           if d.xref_get_key(x, "Type") == ("name", "/Page") and x not in live]
    d.close()
    return out


def _all_streams(path: Path) -> bytes:
    d = fitz.open(str(path))
    parts = []
    for x in range(1, d.xref_length()):
        try:
            if d.xref_is_stream(x):
                parts.append(d.xref_stream(x) or b"")
            else:
                parts.append(d.xref_object(x, compressed=True).encode("latin-1", "replace"))
        except Exception:  # noqa: BLE001
            pass
    d.close()
    return b"\n".join(parts)


@needs_ocr
@pytest.mark.parametrize("kind", ["openaction", "named_dest", "widget"])
def test_file_leaks_01_rebuilt_scan_page_leaves_no_orphan(tmp_path, kind):
    """file_leaks-01: the original page of a rebuilt scan stayed in the file (with its
    unredacted scan) when an open action, a named destination or a form field referred to it."""
    img = _scan_image(["환자명: 하은별", "연락처 010-2468-1357"], size=(1240, 520))
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(20, 20, 575, 300), stream=_png(img))
    pxref = page.xref
    if kind == "widget":
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "memo"
        w.field_value = "x"
        w.rect = fitz.Rect(400, 700, 500, 720)
        page.add_widget(w)
    doc.new_page(width=595, height=842).insert_text((72, 72), "page two", fontname="helv")
    cat = doc.pdf_catalog()
    if kind == "openaction":
        doc.xref_set_key(cat, "OpenAction", f"[{pxref} 0 R /Fit]")
    elif kind == "named_dest":
        doc.xref_set_key(cat, "Names", f"<</Dests<</Names[(scan)[{pxref} 0 R /Fit]]>>>>")
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    doc.save(str(src))
    original = _image_samples(src)
    run(src, dst)
    assert not any(s in original for s in _image_samples(dst))
    assert _orphan_pages(dst) == []
    out = fitz.open(str(dst))
    if kind == "openaction":   # still a valid action, now pointing at the rebuilt page
        assert out.xref_get_key(out.pdf_catalog(), "OpenAction")[1].startswith(f"[{out[0].xref} 0 R")
    out.close()


@needs_ocr
def test_file_leaks_01_fallback_page_old_content_stream_is_gone(tmp_path):
    """file_leaks-01: a text page rebuilt because its /ActualText could not be removed kept the
    old content stream alive through /OpenAction."""
    src = _actualtext_pdf(tmp_path / "in.pdf")
    doc = fitz.open(str(src))
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"[{doc[0].xref} 0 R /Fit]")
    doc.save(str(tmp_path / "in2.pdf"))
    rep = run(tmp_path / "in2.pdf", tmp_path / "out.pdf")
    assert any("그림으로 바꿨습니다" in w for w in rep.warnings)
    assert b"John Smith" not in _all_streams(tmp_path / "out.pdf")
    assert _orphan_pages(tmp_path / "out.pdf") == []


def test_file_leaks_01_page_orphaned_in_the_input_is_emptied(tmp_path):
    """file_leaks-01: a page deleted in an editor but still referenced is never processed: it
    is emptied (and the reference moved to a live page)."""
    b = Pdf()
    b.new_page()
    b.text((72, 100), "검사 결과 정상")
    b.new_page()
    b.text((72, 100), "환자 하은별 님 연락처 010-2468-1357")
    gone = b.page.xref
    b.doc.delete_page(1)
    b.doc.xref_set_key(b.doc.pdf_catalog(), "OpenAction", f"[{gone} 0 R /Fit]")
    src = b.save(tmp_path / "in.pdf")
    assert _orphan_pages(src)
    run(src, tmp_path / "out.pdf")
    assert _orphan_pages(tmp_path / "out.pdf") == []
    out = fitz.open(str(tmp_path / "out.pdf"))
    assert out.page_count == 1
    out.close()


def _ocr_page(path: Path, pno: int) -> str:
    return compact(_ocr_text(_page_image(path, pno, dpi=150)))


@needs_ocr
def test_file_leaks_02_stencil_smask_and_outlined_text_are_checked(tmp_path):
    """file_leaks-02: stencil-mask images, soft-masked (transparent) images and text drawn as
    vector outlines on text pages were never OCR'd as displayed."""
    from PIL import Image, ImageDraw, ImageFont

    f = ImageFont.truetype(str(NANUM), 48)
    lines = ["진료의뢰서", "환자명: 하은별", "연락처 010-2233-4455"]

    def draw(mode, size, bg, fg):
        im = Image.new(mode, size, bg)
        d = ImageDraw.Draw(im)
        for i, ln in enumerate(lines):
            d.text((80, 80 + i * 200), ln, font=f, fill=fg)
        return im

    doc = fitz.open()
    # A: stencil mask (/ImageMask, 1-bit) + a real text footer -> a "text page"
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 820), "Scanned p.1", fontname="helv", fontsize=8)
    W, H = 1654, 800
    x = doc.get_new_xref()
    doc.update_object(x, f"<</Type/XObject/Subtype/Image/Width {W}/Height {H}/ImageMask true"
                         f"/BitsPerComponent 1/Decode[0 1]>>")
    doc.update_stream(x, draw("1", (W, H), 1, 0).tobytes())
    rx = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
    doc.xref_set_key(rx, "XObject", f"<</Im1 {x} 0 R>>")
    c = doc.get_new_xref()
    doc.update_object(c, "<<>>")
    doc.update_stream(c, b"q 0 g 555 0 0 268 20 500 cm /Im1 Do Q")
    cont = doc.xref_get_key(page.xref, "Contents")
    arr = cont[1] if cont[0] == "array" else f"[{cont[1]}]"
    doc.xref_set_key(page.xref, "Contents", arr[:-1] + f" {c} 0 R]")
    # B: black text on a transparent background (/SMask)
    alpha = draw("L", (1654, 800), 0, 255)
    black = Image.new("L", alpha.size, 0)
    buf = io.BytesIO()
    Image.merge("RGBA", (black, black, black, alpha)).save(buf, "PNG")
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 820), "Scanned p.2", fontname="helv", fontsize=8)
    page.insert_image(fitz.Rect(20, 30, 575, 298), stream=buf.getvalue())
    # C: text drawn as filled vector shapes
    bm = draw("1", (1654, 800), 1, 0)
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 820), "Scanned p.3", fontname="helv", fontsize=8)
    sh = page.new_shape()
    px = bm.load()
    for y in range(0, bm.height):
        xx = 0
        while xx < bm.width:
            if px[xx, y] == 0:
                x0 = xx
                while xx < bm.width and px[xx, y] == 0:
                    xx += 1
                sh.draw_rect(fitz.Rect(20 + x0 * 0.33, 30 + y * 0.33, 20 + xx * 0.33,
                                       30 + (y + 1) * 0.33))
            else:
                xx += 1
    sh.finish(color=None, fill=(0, 0, 0), width=0)
    sh.commit()
    src, dst = tmp_path / "masked.pdf", tmp_path / "out.pdf"
    doc.save(str(src))
    for pno in range(3):
        assert "010-2233-4455" in _ocr_page(src, pno), "test setup: OCR cannot read the input"
    rep = run(src, dst)
    for pno in range(3):
        seen = _ocr_page(dst, pno)
        assert "010-2233-4455" not in seen and "하은별" not in seen, pno
    assert any("masked image" in w for w in rep.warnings)        # stencil + outlines: rebuilt
    out = fitz.open(str(dst))
    assert "Scanned p.2" in out[1].get_text()                    # transparent image: in place
    out.close()


def test_file_leaks_02_outline_regions_ignore_cell_shading(tmp_path):
    """file_leaks-02: plain filled rectangles (table shading) are not outlined text."""
    b = Pdf()
    page = b.new_page()
    b.text((72, 60), "검사 결과")
    for i in range(60):
        page.draw_rect(fitz.Rect(72, 100 + i * 10, 300, 108 + i * 10), color=None,
                       fill=(0.9, 0.9, 0.9))
    job = P._PdfJob(b.doc, fitz, Detector(make_cfg()), Anonymizer(make_cfg()), make_cfg(),
                    P.Report())
    assert job._outline_regions(page) == []


def test_file_leaks_03_spaced_variants():
    """file_leaks-03: letter-spaced rows get joined variants with exact character boxes."""
    def row(text):
        chars = [P._Char(c, None if c == " " else (i, 0, i + 1, 1), i) for i, c in enumerate(text)]
        return P._Row(0, chars)
    texts = [r.text for r in P._spaced_variants([row("성 명 하 은 별"),
                                                  row("주민등록번호 8 5 0 3 1 5 - 1 2 3 4 5 6 1"),
                                                  row("연 락 처 0 1 0 - 2 4 6 8 - 1 3 5 7"),
                                                  row("상기 환자를 의뢰합니다.")])]
    assert "성명 하은별" in texts
    assert "주민등록번호 850315-1234561" in texts
    assert "연락처 010-2468-1357" in texts
    assert not any(t.startswith("상기") for t in texts)
    assert all(r.seam for r in P._spaced_variants([row("성 명 하 은 별")]))


def test_file_leaks_03_letter_spaced_form_cells(tmp_path):
    """file_leaks-03: distributed-alignment name cells and one-digit-per-box numbers."""
    b = Pdf()
    b.new_page()

    def distributed(text, x0, x1, y):
        step = (x1 - x0 - 11) / (len(text) - 1)
        for i, ch in enumerate(text):
            b.text((x0 + i * step, y), ch)

    def boxed(digits, x0, y):
        for i, ch in enumerate(digits):
            b.text((x0 + i * 16, y), ch)

    distributed("성명", 70, 150, 110)
    distributed("하은별", 200, 300, 110)
    b.text((70, 140), "주민등록번호")
    boxed("850315-1234561", 170, 140)
    distributed("연락처", 70, 150, 170)
    boxed("010-2468-1357", 170, 170)
    b.text((70, 220), "상기 환자를 의뢰합니다.")
    src = b.save(tmp_path / "form.pdf")
    rep = run(src, tmp_path / "out.pdf")
    out = compact(all_text(tmp_path / "out.pdf"))
    for v in ("하은별", "850315-1234561", "010-2468-1357"):
        assert v not in out
    assert "상기환자를의뢰합니다." in out and "성명" in out
    assert rep.counts == {"PERSON": 1, "RRN": 1, "PHONE": 1}      # counted once each


def test_file_leaks_07_pdfa3_associated_file_is_removed(tmp_path):
    """file_leaks-07: an attachment listed in the catalog /AF survived embfile_del()."""
    payload = "이름,연락처,주민등록번호\n하은별,010-4321-8765,850315-1234561\n".encode("utf-8")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Lab summary - see attached data file.", fontname="helv")
    ef = doc.get_new_xref()
    doc.update_object(ef, "<</Type/EmbeddedFile/Subtype/text#2Fcsv>>")
    doc.update_stream(ef, payload)
    fs = doc.get_new_xref()
    doc.update_object(fs, f"<</Type/Filespec/F(labs.csv)/UF(labs.csv)/AFRelationship/Data"
                          f"/EF<</F {ef} 0 R/UF {ef} 0 R>>>>")
    af_only = doc.get_new_xref()                 # a second file only in a page's /AF
    doc.update_object(af_only, "<</Type/EmbeddedFile>>")
    doc.update_stream(af_only, b"name,phone\nHa Eunbyul,010-4321-8765\n")
    fs2 = doc.get_new_xref()
    doc.update_object(fs2, f"<</Type/Filespec/F(x.csv)/EF<</F {af_only} 0 R>>>>")
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "Names", f"<</EmbeddedFiles<</Names[(labs.csv) {fs} 0 R]>>>>")
    doc.xref_set_key(cat, "AF", f"[{fs} 0 R]")
    doc.xref_set_key(page.xref, "AF", f"[{fs2} 0 R]")
    src = tmp_path / "pdfa3.pdf"
    doc.save(str(src))
    rep = run(src, tmp_path / "out.pdf")
    blob = _all_streams(tmp_path / "out.pdf")
    assert payload not in blob and b"010-4321-8765" not in blob
    assert any("2 attached/embedded file(s) removed" in w for w in rep.warnings)


def test_file_leaks_07_verification_counts_leftover_embedded_files(tmp_path):
    """file_leaks-07: an embedded-file stream left anywhere makes verification fail."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "x", fontname="helv")
    ef = doc.get_new_xref()
    doc.update_object(ef, "<</Type/EmbeddedFile>>")
    doc.update_stream(ef, b"data")
    doc.xref_set_key(doc.pdf_catalog(), "Extra", f"{ef} 0 R")
    assert P._leftover_objects(fitz.open("pdf", doc.tobytes())) == 1


def test_file_leaks_08_signature_value_is_removed(tmp_path):
    """file_leaks-08: the signature dictionary kept the signer's name and certificate."""
    name, email = "하은별", "eunbyul.ha@example.com"
    blob = ("CN=" + name + ",E=" + email).encode("utf-8") + bytes(range(40))
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Consent form. Signed electronically.", fontname="helv")
    sig = doc.get_new_xref()
    name_hex = name.encode("utf-16-be").hex()
    doc.update_object(sig, f"<</Type/Sig/Filter/Adobe.PPKLite/SubFilter/adbe.pkcs7.detached"
                           f"/Name<FEFF{name_hex}>/Location(Seoul)/ContactInfo(010-4321-8765)"
                           f"/M(D:20240301090000+09'00')/Contents<{blob.hex()}>"
                           f"/ByteRange[0 0 0 0]>>")
    w = doc.get_new_xref()
    doc.update_object(w, f"<</Type/Annot/Subtype/Widget/FT/Sig/T(Signature1)/Rect[0 0 0 0]"
                         f"/F 132/P {page.xref} 0 R/V {sig} 0 R>>")
    doc.xref_set_key(page.xref, "Annots", f"[{w} 0 R]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<</Fields[{w} 0 R]/SigFlags 3>>")
    doc.xref_set_key(doc.pdf_catalog(), "Perms", f"<</DocMDP {sig} 0 R>>")
    src = tmp_path / "signed.pdf"
    doc.save(str(src))
    rep = run(src, tmp_path / "out.pdf")
    out = fitz.open(str(tmp_path / "out.pdf"))
    objs = "".join(out.xref_object(x, compressed=True) for x in range(1, out.xref_length())
                   if not out.xref_is_stream(x)).upper()
    out.close()
    assert name_hex.upper() not in objs
    assert blob.hex().upper()[:40] not in objs
    assert any("digital signature" in w for w in rep.warnings)


def test_file_leaks_09_hidden_and_print_only_annotations_are_removed(tmp_path):
    """file_leaks-09: Hidden / NoView annotations kept appearance-only text that text
    extraction never sees; visible ones are still processed normally."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Lab report - results within normal limits.", fontname="helv")
    font_xref = page.get_fonts()[0][0]
    annots = []
    for flags, text in ((2, "Patient phone 010-1111-2222"),
                        (32 | 4, "Printed by staff, tel 010-3333-4444"),
                        (4, "Reviewed")):
        ap = doc.get_new_xref()
        doc.update_object(ap, f"<</Type/XObject/Subtype/Form/BBox[0 0 300 40]"
                              f"/Resources<</Font<</F1 {font_xref} 0 R>>>>>>")
        doc.update_stream(ap, f"BT /F1 12 Tf 5 20 Td ({text}) Tj ET".encode())
        a = doc.get_new_xref()
        y = 200 + 60 * len(annots)
        doc.update_object(a, f"<</Type/Annot/Subtype/Stamp/Rect[100 {y} 400 {y + 40}]/F {flags}"
                             f"/AP<</N {ap} 0 R>>>>")
        annots.append(a)
    doc.xref_set_key(page.xref, "Annots", "[" + " ".join(f"{a} 0 R" for a in annots) + "]")
    src = tmp_path / "report.pdf"
    doc.save(str(src))
    rep = run(src, tmp_path / "out.pdf")
    blob = _all_streams(tmp_path / "out.pdf")
    assert b"010-1111-2222" not in blob and b"010-3333-4444" not in blob
    assert b"Reviewed" in blob                                    # visible stamp kept
    assert any("2 hidden or print-only" in w for w in rep.warnings)


def test_file_leaks_unshown_streams_are_verified(tmp_path):
    """Verification searches streams that no page shows for removed values (e.g. a removed
    phone number left in an orphaned or unreferenced-by-pages stream)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "x", fontname="helv")
    s = doc.get_new_xref()
    doc.update_object(s, "<<>>")
    doc.update_stream(s, "메모 하은별 010-4321-8765".encode("utf-16-be"))
    doc.xref_set_key(doc.pdf_catalog(), "Extra", f"{s} 0 R")
    out = fitz.open("pdf", doc.tobytes())
    assert P._unshown_stream_hits(out, {"010-4321-8765"}) == 1
    assert P._unshown_stream_hits(out, {"010-9999-0000"}) == 0
    # page content is verified by text extraction, not by this search
    assert P._unshown_stream_hits(fitz.open("pdf", doc.tobytes()), {"x"}) == 0


def test_file_leaks_02_outline_probe_is_grouped_and_cached(tmp_path, monkeypatch):
    """file_leaks-02: vector shapes are probed per group (a header logo and a footer logo are
    two small areas, not one page-sized one), and the same logo on every page is OCR'd once."""
    import math as _m

    from deid.formats import image as I

    b = Pdf()
    for _ in range(3):
        page = b.new_page()
        for cx, cy in ((60, 50), (520, 800)):
            sh = page.new_shape()
            for k in range(40):
                a = k * _m.pi / 20
                sh.draw_bezier((cx, cy), (cx + 30 * _m.cos(a), cy + 30 * _m.sin(a)),
                               (cx + 30 * _m.cos(a + 0.4), cy + 30 * _m.sin(a + 0.4)), (cx, cy))
            sh.finish(color=None, fill=(0.2, 0.4, 0.8))
            sh.commit()
        b.text((72, 300), "경과기록 혈압 132/84 mmHg")
    regions = P._PdfJob(b.doc, fitz, Detector(make_cfg()), Anonymizer(make_cfg()), make_cfg(),
                        P.Report())._outline_regions(b.doc[0])
    assert len(regions) == 2 and all(r.width < 80 and r.height < 80 for r in regions)
    src = b.save(tmp_path / "logos.pdf")
    sizes = []
    real = I.redact_image

    def counting(img, *a, **k):
        sizes.append(img.size)
        return real(img, *a, **k)
    monkeypatch.setattr(I, "redact_image", counting)
    run(src, tmp_path / "out.pdf")
    assert len(sizes) == 2          # two different logos, OCR'd once each for three pages
