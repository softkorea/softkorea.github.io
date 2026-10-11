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
