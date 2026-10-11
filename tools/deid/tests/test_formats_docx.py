"""Tests for deid.formats.docx: documents built with python-docx (plus raw XML for track
changes, text boxes, footnotes, fields and hyperlinks); the OUTPUT package is re-read and every
XML part searched for the original values. Synthetic people only."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")

from deid import ocr  # noqa: E402
from deid.anonymizer import Anonymizer  # noqa: E402
from deid.config import default_config  # noqa: E402
from deid.engine import Detector  # noqa: E402
from deid.formats import docx as D  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
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
    return D.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)


def save(document, path: Path) -> Path:
    document.save(str(path))
    return path


def package_text(path: Path) -> str:
    """All XML / rels parts of the package, decoded (for 'is the value anywhere?' checks)."""
    with zipfile.ZipFile(path) as z:
        return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist()
                         if n.endswith((".xml", ".rels")))


def body_text(path: Path) -> str:
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts += [c.text for c in row.cells]
    return "\n".join(parts)


def patch_part(path: Path, name: str, fn) -> None:
    """Rewrite one part of the package in place (fn: str -> str)."""
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            if info.filename == name:
                data = fn(data.decode("utf-8")).encode("utf-8")
            z.writestr(info, data)


def add_part(path: Path, name: str, xml: str, rel_type: str, content_type: str,
             rel_id: str = "rIdTest1", target_mode: str | None = None) -> None:
    """Add a part plus its relationship from document.xml and its content type."""
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            if info.filename == "word/_rels/document.xml.rels":
                mode = f' TargetMode="{target_mode}"' if target_mode else ""
                data = data.replace(b"</Relationships>",
                                    f'<Relationship Id="{rel_id}" Type="{rel_type}" '
                                    f'Target="{name}"{mode}/></Relationships>'.encode())
            elif info.filename == "[Content_Types].xml" and content_type:
                data = data.replace(b"</Types>", f'<Override PartName="/word/{name}" '
                                    f'ContentType="{content_type}"/></Types>'.encode())
            z.writestr(info, data)
        if not target_mode:
            z.writestr(f"word/{name}", xml)


def basic_doc() -> "docx.Document":
    d = docx.Document()
    p = d.add_paragraph("환자명: ")
    r = p.add_run("홍")
    r.bold = True
    p.add_run("길동 (남/45세), 연락처 010-1234-5678")
    d.add_paragraph("홍길동님은 고혈압과 이상지질혈증으로 내원함. 아토르바스타틴 10mg 처방.")
    return d


# ---------------------------------------------------------------------------------------------


def test_name_split_across_runs_keeps_first_run_format(tmp_path):
    src = save(basic_doc(), tmp_path / "in.docx")
    rep = run(src, tmp_path / "out.docx")
    out = docx.Document(str(tmp_path / "out.docx"))
    p0 = out.paragraphs[0]
    assert p0.text == "환자명: [이름1] (남/45세), 연락처 010-****-5678"
    bold = [r.text for r in p0.runs if r.bold]
    assert bold == ["[이름1]"]
    assert out.paragraphs[1].text.startswith("[이름1]님은 고혈압과 이상지질혈증")
    assert "아토르바스타틴 10mg" in out.paragraphs[1].text
    all_xml = package_text(tmp_path / "out.docx")
    assert "홍길동" not in all_xml and "1234-5678" not in all_xml
    assert "길동" not in all_xml        # the second run lost its part of the name
    assert rep.counts == {"PERSON": 2, "PHONE": 1}
    assert "홍길동" not in str(rep.to_dict())


def test_tables_headers_footers_all_sections(tmp_path):
    d = basic_doc()
    t = d.add_table(rows=3, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "성명", "이서연"          # label | value
    t.cell(1, 0).text, t.cell(1, 1).text = "주민등록번호", "900101-1234567"
    t.cell(2, 0).text, t.cell(2, 1).text = "진단", "만성 위염"
    inner = t.cell(2, 1).add_table(rows=1, cols=1)                   # nested table
    inner.cell(0, 0).text = "보호자 김민준 연락 요망"
    s0 = d.sections[0]
    s0.header.paragraphs[0].text = "환자 홍길동 / 등록번호: 12345678"
    s0.footer.paragraphs[0].text = "주치의 최수아"
    s0.different_first_page_header_footer = True
    s0.first_page_header.paragraphs[0].text = "첫 쪽: 홍길동"
    d.settings.odd_and_even_pages_header_footer = True
    s0.even_page_footer.paragraphs[0].text = "짝수 쪽: 이메일 minjun@example.com"
    s1 = d.add_section()
    s1.header.is_linked_to_previous = False
    s1.header.paragraphs[0].text = "2장 머리말 박지훈 님"
    src = save(d, tmp_path / "in.docx")
    run(src, tmp_path / "out.docx")
    all_xml = package_text(tmp_path / "out.docx")
    for pii in ("홍길동", "이서연", "900101-1234567", "김민준", "12345678", "최수아",
                "minjun@example.com", "박지훈"):
        assert pii not in all_xml, pii
    assert "만성 위염" in body_text(tmp_path / "out.docx")
    out = docx.Document(str(tmp_path / "out.docx"))
    assert out.tables[0].cell(0, 0).text == "성명"
    assert out.tables[0].cell(1, 1).text == "[주민번호]"


def test_core_properties_and_author_names(tmp_path):
    d = basic_doc()
    cp = d.core_properties
    cp.author, cp.last_modified_by = "이서연", "최수아"
    cp.title, cp.subject, cp.keywords = "홍길동 진료기록", "외래", "홍길동, 고혈압"
    cp.comments, cp.category = "홍길동 보호자 메모", "진료"
    d.add_paragraph("검토: 이서연 / 확인: 최수아")        # no honorific: known via properties
    src = save(d, tmp_path / "in.docx")
    run(src, tmp_path / "out.docx")
    cp2 = docx.Document(str(tmp_path / "out.docx")).core_properties
    for attr in ("author", "last_modified_by", "title", "subject", "keywords", "comments",
                 "category"):
        assert not getattr(cp2, attr), attr
    all_xml = package_text(tmp_path / "out.docx")
    assert "이서연" not in all_xml and "최수아" not in all_xml


@pytest.mark.skipif(not hasattr(docx.Document(), "add_comment"), reason="python-docx < 1.2")
def test_comments_text_and_authors(tmp_path):
    d = basic_doc()
    para = d.add_paragraph("경과 관찰")
    d.add_comment(para.runs, text="김민준 보호자에게 결과 설명함", author="이서연", initials="SY")
    src = save(d, tmp_path / "in.docx")
    run(src, tmp_path / "out.docx")
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        comments = z.read("word/comments.xml").decode()
    assert "김민준" not in comments and "결과 설명함" in comments
    assert 'w:author="익명"' in comments and 'w:initials="익명"' in comments
    assert "이서연" not in package_text(tmp_path / "out.docx")


TRACKED = (
    '<w:p><w:r><w:t xml:space="preserve">경과: </w:t></w:r>'
    '<w:ins w:id="91" w:author="이서연" w:date="2024-03-15T10:00:00Z">'
    '<w:r><w:t>보호자 김민준 동반</w:t></w:r></w:ins>'
    '<w:del w:id="92" w:author="이서연" w:date="2024-03-15T10:00:00Z">'
    '<w:r><w:delText>환자 박지훈 기록 삭제</w:delText></w:r></w:del>'
    '<w:r><w:t xml:space="preserve"> 호전</w:t></w:r></w:p>'
)
TEXTBOX = (
    '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml" style="width:200pt">'
    '<v:textbox><w:txbxContent><w:p><w:r><w:t>글상자: 환자명 정하은</w:t></w:r></w:p>'
    '</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>'
)
FIELD = (
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:seoyeon@example.com" </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>메일 보내기</w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
)


def _inject_body(path: Path, xml: str) -> None:
    patch_part(path, "word/document.xml", lambda s: s.replace("<w:sectPr", xml + "<w:sectPr", 1))


def test_track_changes_textbox_and_field_codes(tmp_path):
    src = save(basic_doc(), tmp_path / "in.docx")
    _inject_body(src, TRACKED + TEXTBOX + FIELD)
    rep = run(src, tmp_path / "out.docx")
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        doc_xml = z.read("word/document.xml").decode()
    assert "박지훈" not in doc_xml and "기록 삭제" not in doc_xml     # deleted text wiped
    assert "김민준" not in doc_xml and "동반" in doc_xml
    assert "정하은" not in doc_xml and "글상자" in doc_xml
    assert "seoyeon@example.com" not in doc_xml and "HYPERLINK" in doc_xml
    assert 'w:author="익명"' in doc_xml and "이서연" not in doc_xml
    assert any("삭제된 글자" in w for w in rep.warnings)
    docx.Document(str(tmp_path / "out.docx"))   # still a valid document


def test_footnotes_endnotes_parts(tmp_path):
    src = save(basic_doc(), tmp_path / "in.docx")
    ns = f'xmlns:w="{W}"'
    foot = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:footnotes {ns}>'
            '<w:footnote w:id="1"><w:p><w:r><w:t>각주: 의뢰인 강도윤 님</w:t></w:r></w:p>'
            '</w:footnote></w:footnotes>')
    end = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:endnotes {ns}>'
           '<w:endnote w:id="1"><w:p><w:r><w:t>미주: 연락처 010-5555-1234</w:t></w:r></w:p>'
           '</w:endnote></w:endnotes>')
    base = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    ct = "application/vnd.openxmlformats-officedocument.wordprocessingml."
    add_part(src, "footnotes.xml", foot, base + "footnotes", ct + "footnotes+xml", "rIdF1")
    add_part(src, "endnotes.xml", end, base + "endnotes", ct + "endnotes+xml", "rIdE1")
    run(src, tmp_path / "out.docx")
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        f_xml, e_xml = z.read("word/footnotes.xml").decode(), z.read("word/endnotes.xml").decode()
    assert "강도윤" not in f_xml and "각주" in f_xml
    assert "5555-1234" not in e_xml and "미주" in e_xml
    assert f_xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')


def test_hyperlink_targets(tmp_path):
    d = basic_doc()
    para = d.add_paragraph("문의 ")
    part = d.part
    rels = [
        ("mailto:minjun@example.com", "메일"),
        ("tel:010-2222-3333", "전화"),
        ("file:///C:/Users/hong/Documents/%ED%99%8D%EA%B8%B8%EB%8F%99_%EA%B2%80%EC%82%AC.pdf", "파일"),
        ("https://example.org/guide?page=2", "안내"),
    ]
    for url, label in rels:
        rid = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/"
                             "relationships/hyperlink", is_external=True)
        h = para._p.makeelement(f"{{{W}}}hyperlink",
                                {"{http://schemas.openxmlformats.org/officeDocument/2006/"
                                 "relationships}id": rid})
        r = para._p.makeelement(f"{{{W}}}r", {})
        t = para._p.makeelement(f"{{{W}}}t", {})
        t.text = label
        r.append(t)
        h.append(r)
        para._p.append(h)
    src = save(d, tmp_path / "in.docx")
    rep = run(src, tmp_path / "out.docx")
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        rels_xml = z.read("word/_rels/document.xml.rels").decode()
    assert "minjun@example.com" not in rels_xml and "mailto:" in rels_xml
    assert "2222-3333" not in rels_xml and "tel:010-****-3333" in rels_xml
    assert "Users" not in rels_xml and "hong" not in rels_xml        # path cut to a file name
    assert "%ED%99%8D%EA%B8%B8%EB%8F%99" not in rels_xml and "홍길동" not in rels_xml
    assert "https://example.org/guide?page=2" in rels_xml
    assert any("링크" in w for w in rep.warnings)


def test_images_metadata_stripped_and_reported(tmp_path):
    from PIL import Image

    img = Image.new("RGB", (160, 90), (40, 120, 200))
    exif = Image.Exif()
    exif[0x013B] = "Hong Gildong"            # Artist
    exif[0x010E] = "patient photo"           # ImageDescription
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    d = basic_doc()
    d.add_picture(io.BytesIO(buf.getvalue()))
    src = save(d, tmp_path / "in.docx")
    rep = run(src, tmp_path / "out.docx")
    assert any("이미지 1개는 검사하지 않음" in w for w in rep.warnings)
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        data = z.read(media[0])
    assert b"Hong Gildong" not in data
    with Image.open(io.BytesIO(data)) as im:
        assert not dict(im.getexif())
        assert im.size == (160, 90)


@needs_ocr
def test_docx_images_option_runs_ocr(tmp_path):
    pytest.importorskip("deid.formats.image")
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(NANUM), 40)
    draw.text((30, 40), "환자명: 홍길동", font=font, fill="black")
    draw.text((30, 110), "진단: 골다공증", font=font, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    d = basic_doc()
    d.add_picture(io.BytesIO(buf.getvalue()))
    src = save(d, tmp_path / "in.docx")
    rep = run(src, tmp_path / "out.docx", make_cfg(docx_images=True))
    assert not any("검사하지 않음" in w for w in rep.warnings)
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        data = z.read([n for n in z.namelist() if n.startswith("word/media/")][0])
    out = Image.open(io.BytesIO(data)).convert("L")
    lines, _, _ = ocr.lines_from_tesseract(ocr.tesseract_data(out, "kor+eng", 6))
    seen = re.sub(r"\s+", "", "".join(ln.text for ln in lines))
    assert "홍길동" not in seen


def test_thumbnail_custom_props_docvars_and_alt_text(tmp_path):
    d = basic_doc()
    src = save(d, tmp_path / "in.docx")
    with zipfile.ZipFile(src) as z:
        assert "docProps/thumbnail.jpeg" in z.namelist()
    patch_part(src, "word/settings.xml", lambda s: s.replace(
        "</w:settings>", '<w:docVars><w:docVar w:name="patient" w:val="홍길동"/></w:docVars>'
                         "</w:settings>"))
    _inject_body(src, '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
                      'drawingml/2006/wordprocessingDrawing"><wp:docPr id="5" name="그림 1" '
                      'descr="환자 윤서준 님 상처 사진"/></wp:inline></w:drawing></w:r></w:p>')
    custom = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns='
              '"http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
              'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
              '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="환자">'
              '<vt:lpwstr>홍길동</vt:lpwstr></property></Properties>')
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("docProps/custom.xml", custom)
    run(src, tmp_path / "out.docx")
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        thumb = z.read("docProps/thumbnail.jpeg")
        custom_out = z.read("docProps/custom.xml").decode()
        settings = z.read("word/settings.xml").decode()
        doc_xml = z.read("word/document.xml").decode()
    from PIL import Image

    with Image.open(io.BytesIO(thumb)) as im:
        assert im.convert("L").getextrema()[0] > 240          # blank (white) thumbnail
    assert "홍길동" not in custom_out and "property" not in custom_out
    assert "docVar" not in settings
    assert "윤서준" not in doc_xml and "상처 사진" in doc_xml


def test_embedded_objects_emptied_and_custom_xml(tmp_path):
    src = save(basic_doc(), tmp_path / "in.docx")
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("word/embeddings/Microsoft_Excel_Worksheet.xlsx", "PK fake xlsx 홍길동".encode())
        z.writestr("customXml/item9.xml", '<?xml version="1.0"?><patient><name>홍길동</name>'
                                          "<memo>보호자 김민준 님</memo></patient>")
    rep = run(src, tmp_path / "out.docx")
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        assert z.read("word/embeddings/Microsoft_Excel_Worksheet.xlsx") == b""
        item = z.read("customXml/item9.xml").decode()
    assert "홍길동" not in item and "김민준" not in item
    assert any("포함된 개체" in w for w in rep.warnings)


def test_zip_layout_preserved_and_untouched_parts_identical(tmp_path):
    src = save(basic_doc(), tmp_path / "in.docx")
    run(src, tmp_path / "out.docx")
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(tmp_path / "out.docx") as b:
        assert [i.filename for i in a.infolist()] == [i.filename for i in b.infolist()]
        assert [i.compress_type for i in a.infolist()] == [i.compress_type for i in b.infolist()]
        for name in ("word/styles.xml", "word/fontTable.xml", "[Content_Types].xml"):
            assert a.read(name) == b.read(name)
        assert a.read("word/document.xml") != b.read("word/document.xml")
    docx.Document(str(tmp_path / "out.docx"))


def test_legacy_encrypted_and_broken_files_are_refused(tmp_path):
    doc_file = tmp_path / "old.doc"
    doc_file.write_bytes(D.CFB_MAGIC + b"\x00" * 512)
    with pytest.raises(D.DocumentError) as ei:
        run(doc_file, tmp_path / "out.docx")
    assert ".docx" in str(ei.value)
    enc = tmp_path / "locked.docx"           # encrypted Office files are OLE containers
    enc.write_bytes(D.CFB_MAGIC + b"\x00" * 512)
    with pytest.raises(D.DocumentError) as ei:
        run(enc, tmp_path / "out.docx")
    assert "암호" in str(ei.value)
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(D.DocumentError):
        run(bad, tmp_path / "out.docx")
    assert not (tmp_path / "out.docx").exists()
    src = save(basic_doc(), tmp_path / "in.docx")
    with pytest.raises(ValueError):
        run(src, src)


def test_paragraph_writer_unit():
    """Replacement goes into the run where the span starts; separators are kept."""
    from lxml import etree

    xml = (f'<w:p xmlns:w="{W}"><w:r><w:t>성명: 김</w:t></w:r><w:r><w:tab/></w:r>'
           '<w:r><w:t>민준</w:t></w:r><w:r><w:t xml:space="preserve"> 님</w:t></w:r></w:p>')
    p = etree.fromstring(xml)
    walker = D.ParagraphWalker(D.DOCX_SPEC)
    (main, fields), = walker.paragraphs(p)
    assert main.text == "성명: 김\t민준 님"
    from deid.types import AnonResult, Replacement

    res = AnonResult("성명: [이름1] 님", [Replacement(4, 8, "PERSON", "김\t민준", "[이름1]", "pseudo")])
    main.apply(res)
    ts = [t.text for t in p.iter(f"{{{W}}}t")]
    assert ts == ["성명: [이름1]", None, " 님"]
    assert main.current() == "성명: [이름1]\t 님"


# --------------------------------------------------------------------------- red-team regressions


def _emf(text: str) -> bytes:
    """A small well-formed EMF drawing `text` with one EXTTEXTOUTW record."""
    import struct
    txt = text.encode("utf-16-le")
    pad = (-len(txt)) % 4
    head = (struct.pack("<II", 1, 88) + struct.pack("<4i", 0, 0, 100, 20)
            + struct.pack("<4i", 0, 0, 2000, 400) + struct.pack("<II", 0x464D4520, 0x10000)
            + struct.pack("<IIHHIII", 0, 3, 1, 0, 0, 0, 0) + struct.pack("<4i", 1024, 768, 320, 240))
    rec = (struct.pack("<II", 0x54, 76 + len(txt) + pad) + struct.pack("<4i", 0, 0, 100, 20)
           + struct.pack("<Iff", 1, 1.0, 1.0) + struct.pack("<ii", 5, 5)
           + struct.pack("<III", len(text), 76, 0) + struct.pack("<4i", 0, 0, 100, 20)
           + struct.pack("<I", 0) + txt + b"\0" * pad)
    eof = struct.pack("<IIIII", 0x0E, 20, 0, 16, 20)
    data = bytearray(head + rec + eof)
    struct.pack_into("<I", data, 48, len(data))
    return bytes(data)


def _wmf(text: str) -> bytes:
    """A small WMF drawing `text` (cp949) with one TEXTOUT record."""
    import struct
    raw = text.encode("cp949")
    body = raw + (b"\0" if len(raw) % 2 else b"")
    rec = struct.pack("<IHH", (8 + len(body) + 4) // 2, 0x0521, len(raw)) + body + struct.pack("<hh", 5, 5)
    eof = struct.pack("<IH", 3, 0)
    head = struct.pack("<HHHIHIH", 1, 9, 0x300, (18 + len(rec) + len(eof)) // 2, 0, len(rec) // 2, 0)
    return head + rec + eof


def test_file_leaks_10_vector_pictures_emf_wmf_svg(tmp_path):
    """file_leaks-10-office-vector-image-text: text inside EMF/WMF/SVG pictures is checked;
    an EMF/WMF showing personal data becomes an empty picture with the same frame, SVG text
    is replaced, a picture without personal data is kept, and the warning never suggests an
    option that cannot help."""
    from deid.formats.docx import vector_texts
    emf_txt, svg_txt = "환자 하은별 님 010-2233-4455", "환자 국민아 님 010-8901-0010"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="40"><title>환자 국민아 님'
           f'</title><text x="5" y="25">{svg_txt}<tspan>확인</tspan> 끝</text></svg>')
    import gzip
    src = save(basic_doc(), tmp_path / "in.docx")
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("word/media/table.emf", _emf(emf_txt))
        z.writestr("word/media/chart.emz", gzip.compress(_emf("보호자 김민준 010-3333-4444")))
        z.writestr("word/media/old.wmf", _wmf("환자 이서연 님 010-5555-6666"))
        z.writestr("word/media/logo.emf", _emf("ACME Clinic"))
        z.writestr("word/media/icon.svg", svg)
        z.writestr("word/media/broken.emf", b"\x01\0\0\0garbage")
    for cfg in (make_cfg(docx_images=True), make_cfg()):
        rep = run(src, tmp_path / "out.docx", cfg)
        with zipfile.ZipFile(tmp_path / "out.docx") as z:
            parts = {n: z.read(n) for n in z.namelist()}
        emf = parts["word/media/table.emf"]
        assert emf_txt.encode("utf-16-le") not in emf
        assert vector_texts(emf, ".emf") == ([], False) and emf[8:40] == _emf(emf_txt)[8:40]
        assert vector_texts(gzip.decompress(parts["word/media/chart.emz"]), ".emf") == ([], False)
        assert "이서연".encode("cp949") not in parts["word/media/old.wmf"]
        assert vector_texts(parts["word/media/old.wmf"], ".wmf") == ([], False)
        assert parts["word/media/logo.emf"] == _emf("ACME Clinic")
        out_svg = parts["word/media/icon.svg"].decode()
        assert "국민아" not in out_svg and "8901-0010" not in out_svg and "<tspan>확인</tspan>" in out_svg
        assert any("벡터 그림" in w and "3" in w for w in rep.warnings)
        assert any("1 picture(s) could not be checked" in w for w in rep.warnings)   # broken.emf
        assert not any("docx_images = true" in w for w in rep.warnings)


def test_file_leaks_06_picture_file_names_neutralised(tmp_path):
    """file_leaks-06-office-picture-filenames: the original file name / path Word stores for
    a picture (cNvPr name, docPr descr) becomes 'file.<ext>'; real alt text is still checked."""
    from PIL import Image
    d = docx.Document()
    d.add_paragraph("증례: 하지 궤양, 상처 사진 첨부")
    buf = io.BytesIO()
    Image.new("RGB", (80, 60), (200, 120, 120)).save(buf, "PNG")
    d.add_picture(io.BytesIO(buf.getvalue()))
    d.add_picture(io.BytesIO(buf.getvalue()))
    src = save(d, tmp_path / "in.docx")

    def patch(s: str) -> str:
        s = re.sub(r'(<pic:cNvPr [^>]*name=")[^"]*"', r'\1CT_하은별_850315.png"', s, count=1)
        s = re.sub(r'(<wp:docPr [^>]*?)/>', r'\1 descr="C:\\Users\\doctor\\Desktop\\20240301 하은별 '
                   r'상처.jpg"/>', s, count=1)
        return re.sub(r'(<wp:docPr id="2"[^>]*?)/>', r'\1 descr="환자 윤서준 님 상처 사진"/>', s, count=1)
    patch_part(src, "word/document.xml", patch)
    rep = run(src, tmp_path / "out.docx")
    xml = zipfile.ZipFile(tmp_path / "out.docx").read("word/document.xml").decode()
    assert "하은별" not in xml and "Users" not in xml and "850315" not in xml
    assert 'name="file.png"' in xml and 'descr="file.jpg"' in xml
    assert "윤서준" not in xml and "상처 사진" in xml
    assert any("파일 이름" in w for w in rep.warnings)
    assert D.file_name_like("image1.png") and not D.file_name_like("그림 1")


def test_file_leaks_11_custom_xml_sharepoint_metadata(tmp_path):
    """file_leaks-11-docx-customxml: SharePoint/OneDrive library columns are typed by their
    element name; the owner's UserInfo is a person and its account id is cleared."""
    src = save(basic_doc(), tmp_path / "in.docx")
    item = ('<?xml version="1.0" encoding="utf-8"?><p:properties xmlns:p="http://schemas.microsoft.com/'
            'office/2006/metadata/properties"><documentManagement>'
            '<PatientName xmlns="5c1d">하은별</PatientName><ChartNo xmlns="5c1d">20240311</ChartNo>'
            '<Ward xmlns="5c1d">7병동</Ward><Owner xmlns="5c1d"><UserInfo><DisplayName>배수지</DisplayName>'
            '<AccountId>12</AccountId><AccountName>i:0#.w|corp\\sjbae</AccountName></UserInfo></Owner>'
            '</documentManagement></p:properties>')
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("customXml/item7.xml", item)
    run(src, tmp_path / "out.docx")
    out = zipfile.ZipFile(tmp_path / "out.docx").read("customXml/item7.xml").decode()
    for v in ("하은별", "20240311", "배수지", "sjbae"):
        assert v not in out, v
    assert "7병동" in out and "<AccountId>12</AccountId>" in out


@needs_ocr
def test_file_leaks_14_default_config_ocrs_screenshots(tmp_path):
    """file_leaks-14-docx-images-default-off: with the default config ([ocr] docx_images =
    "auto") a pasted EMR screenshot is OCR'd and covered; tiny pictures are only counted."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(NANUM), 40)
    draw.text((30, 40), "환자명: 홍길동", font=font, fill="black")
    draw.text((30, 110), "연락처 010-2233-4455", font=font, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    small = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(small, format="PNG")
    d = basic_doc()
    d.add_picture(io.BytesIO(buf.getvalue()))
    d.add_picture(io.BytesIO(small.getvalue()))
    src = save(d, tmp_path / "in.docx")
    cfg = make_cfg()
    assert "docx_images" not in cfg.ocr
    rep = run(src, tmp_path / "out.docx", cfg)
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        media = sorted(n for n in z.namelist() if n.startswith("word/media/"))
        big = [z.read(n) for n in media if Image.open(io.BytesIO(z.read(n))).size == (900, 200)][0]
    lines, _, _ = ocr.lines_from_tesseract(ocr.tesseract_data(Image.open(io.BytesIO(big)).convert("L"),
                                                              "kor+eng", 6))
    seen = re.sub(r"\s+", "", "".join(ln.text for ln in lines))
    assert "홍길동" not in seen and "2233-4455" not in seen
    assert any("이미지 1개는 검사하지 않음" in w for w in rep.warnings)


def test_file_leaks_14_auto_mode_without_ocr_engine(tmp_path, monkeypatch):
    """file_leaks-14: the "auto" default never refuses a document when no OCR engine works
    (warning instead); an explicit docx_images = true still fails closed."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, format="PNG")
    d = basic_doc()
    d.add_picture(io.BytesIO(buf.getvalue()))
    src = save(d, tmp_path / "in.docx")

    def no_ocr(self, data, ext):
        raise ocr.OcrError("no OCR engine")
    monkeypatch.setattr(D.PackageJob, "_ocr_image", no_ocr)
    rep = run(src, tmp_path / "out.docx")
    assert any("OCR 엔진이 없어" in w for w in rep.warnings)
    assert (tmp_path / "out.docx").exists()
    with pytest.raises(ocr.OcrError):
        run(src, tmp_path / "out2.docx", make_cfg(docx_images=True))
    assert not (tmp_path / "out2.docx").exists()
    assert D.image_ocr_mode(None) == "auto" and D.image_ocr_mode(True) == "all"
    assert D.image_ocr_mode(False) == "off" and D.image_ocr_mode("auto") == "auto"


def test_over_mask_01_chief_complaint_form_table(tmp_path):
    """over_mask-01: a '주소 | 두통' form row is the chief complaint, not an address."""
    d = docx.Document()
    t = d.add_table(rows=4, cols=2)
    for i, (k, v) in enumerate([("주소", "두통"), ("현병력", "3일 전부터 두통 및 오심"),
                                ("진단", "긴장성 두통"), ("자택 주소", "서울특별시 강남구 테헤란로 152")]):
        t.cell(i, 0).text = k
        t.cell(i, 1).text = v
    src = save(d, tmp_path / "in.docx")
    run(src, tmp_path / "out.docx")
    cells = [[c.text for c in row.cells] for row in docx.Document(str(tmp_path / "out.docx")).tables[0].rows]
    assert cells[:3] == [["주소", "두통"], ["현병력", "3일 전부터 두통 및 오심"], ["진단", "긴장성 두통"]]
    assert "테헤란로" not in cells[3][1]


def test_security_zip_bomb_docx_refused(tmp_path, monkeypatch):
    """security-zip-bomb-docx-hwpx: entries are size-checked before anything is inflated."""
    from deid.formats import text as T
    src = save(basic_doc(), tmp_path / "in.docx")
    with zipfile.ZipFile(src, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/media/pad.bin", b"\0" * (3 * 1024 * 1024))
    monkeypatch.setattr(T, "ZIP_MAX_ENTRY", 1024 * 1024)
    with pytest.raises(D.DocumentError, match="zip bomb"):
        run(src, tmp_path / "out.docx")
    monkeypatch.setattr(T, "ZIP_MAX_ENTRY", 256 * 1024 * 1024)
    monkeypatch.setattr(T, "ZIP_RATIO_CHECK", 1024 * 1024)       # 3 MB of zeros: ratio > 200
    with pytest.raises(D.DocumentError, match="zip bomb"):
        run(src, tmp_path / "out.docx")
    monkeypatch.setattr(T, "ZIP_RATIO_CHECK", 32 * 1024 * 1024)
    monkeypatch.setattr(T, "ZIP_MAX_TOTAL", 2 * 1024 * 1024)
    with pytest.raises(D.DocumentError, match="zip bomb"):
        run(src, tmp_path / "out.docx")
    assert not (tmp_path / "out.docx").exists()
