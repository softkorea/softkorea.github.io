"""Tests for deid.formats.hwpx: minimal but realistic HWPX packages (OWPML) are built in the
test, processed, and every part of the output is checked. Synthetic people only."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from deid.anonymizer import Anonymizer
from deid.config import default_config
from deid.engine import Detector
from deid.formats import hwpx as H
from deid.formats.docx import CFB_MAGIC, DocumentError

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"
HH = "http://www.hancom.co.kr/hwpml/2011/head"


def run(src: Path, dst: Path, cfg=None):
    cfg = cfg or default_config()
    return H.process(src, dst, Detector(cfg), Anonymizer(cfg), cfg)


def section(body: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
            f'<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">{body}</hs:sec>')


def para(*runs: str, pid: int = 0, lineseg: bool = True) -> str:
    seg = ('<hp:linesegarray><hp:lineseg textpos="0" vertpos="0" vertsize="1000" '
           'textheight="1000" baseline="850" spacing="600" horzpos="0" horzsize="42520" '
           'flags="393216"/></hp:linesegarray>') if lineseg else ""
    return (f'<hp:p id="{pid}" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" '
            f'merged="0">{"".join(runs)}{seg}</hp:p>')


def run_t(text: str, char_pr: int = 0) -> str:
    return f'<hp:run charPrIDRef="{char_pr}"><hp:t>{text}</hp:t></hp:run>'


CONTENT_HPF = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
    '<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'version="" unique-identifier="" id=""><opf:metadata><opf:title>홍길동 진료 요약</opf:title>'
    '<opf:language>ko</opf:language><opf:meta name="creator" content="text">최수아</opf:meta>'
    '<opf:meta name="subject" content="text">홍길동</opf:meta>'
    '<opf:meta name="description" content="text">보호자 김민준 연락</opf:meta>'
    '<opf:meta name="lastsaveby" content="text">최수아</opf:meta>'
    '<opf:meta name="CreatedDate" content="text">2024-03-15T01:02:03Z</opf:meta>'
    '<opf:meta name="ModifiedDate" content="text">2024-03-16T01:02:03Z</opf:meta>'
    '<opf:meta name="keyword" content="text">홍길동</opf:meta></opf:metadata>'
    '<opf:manifest><opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
    '<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
    '<opf:item id="image1" href="BinData/image1.jpg" media-type="image/jpg" isEmbeded="1"/>'
    '</opf:manifest><opf:spine><opf:itemref idref="header" linear="yes"/>'
    '<opf:itemref idref="section0" linear="yes"/></opf:spine></opf:package>'
)
HEADER = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n<hh:head xmlns:hh="{HH}" '
    'version="1.4" secCnt="1"><hh:refList><hh:fontfaces itemCnt="0"/></hh:refList>'
    '<hh:trackChangeAuthors><hh:trackChangeAuthor name="이서연" mark="1" color="#FF0000" id="1"/>'
    '</hh:trackChangeAuthors></hh:head>'
)
VERSION = ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n<hv:HCFVersion '
           'xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" tagetApplication="WORDPROCESSOR" '
           'major="5" minor="1" micro="0" buildNumber="1" os="1" xmlVersion="1.4" '
           'application="Hancom Office Hangul" appVersion="11, 0, 0, 0"/>')
MANIFEST = ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n<odf:manifest '
            'xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>')
CONTAINER = ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n<ocf:container '
             'xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container"><ocf:rootfiles>'
             '<ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>'
             '</ocf:rootfiles></ocf:container>')

SECTION0 = section(
    para(run_t("환자명: "), run_t("홍", 1), run_t("길동<hp:tab/>연락처 010-1234-5678"), pid=1)
    + para(run_t("홍길동님은 고혈압으로 내원.<hp:lineBreak/>보호자 김민준 동반"), pid=2)
    + para('<hp:run charPrIDRef="0"><hp:tbl rowCnt="2" colCnt="2"><hp:tr>'
           '<hp:tc><hp:subList><hp:p id="10"><hp:run><hp:t>성명</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
           '<hp:tc><hp:subList><hp:p id="11"><hp:run><hp:t>이서연</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
           '</hp:tr><hp:tr>'
           '<hp:tc><hp:subList><hp:p id="12"><hp:run><hp:t>주민등록번호</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
           '<hp:tc><hp:subList><hp:p id="13"><hp:run><hp:t>900101-1234567</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
           '</hp:tr></hp:tbl></hp:run>', run_t("표 아래 메모"), pid=3)
    + para(run_t('진단: 당뇨<hp:deleteBegin Id="1" TcId="1" paraend="0"/>환자 박지훈 기록'
                 '<hp:deleteEnd Id="1" TcId="1" paraend="0"/> 조절 양호'), pid=4)
    + para('<hp:run charPrIDRef="0"><hp:ctrl><hp:fieldBegin id="1" type="HYPERLINK" name="">'
           '<hp:parameters cnt="2" name=""><hp:integerParam name="Prop">0</hp:integerParam>'
           '<hp:stringParam name="Path">mailto:minjun@example.com</hp:stringParam>'
           '</hp:parameters></hp:fieldBegin></hp:ctrl><hp:t>메일 보내기</hp:t>'
           '<hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl></hp:run>', pid=5)
    + para('<hp:run charPrIDRef="0"><hp:ctrl><hp:header id="1" applyPageType="BOTH">'
           '<hp:subList><hp:p id="20"><hp:run><hp:t>머리말 등록번호: 12345678</hp:t></hp:run></hp:p>'
           '</hp:subList></hp:header></hp:ctrl></hp:run>', run_t("본문 계속: 흉통 없음"), pid=6)
)
SECTION1 = section(para(run_t("2쪽 경과: 홍길동 당뇨 조절 양호"), pid=30))
PRV_TEXT = "환자명: 홍길동\t연락처 010-1234-5678\r\n홍길동님은 고혈압으로 내원.\r\n<성명><이서연>\r\n"


def jpeg_with_exif() -> bytes:
    exif = Image.Exif()
    exif[0x013B] = "Hong Gildong"
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 30)).save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def png(size=(160, 220), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def build(path: Path, sections=(SECTION0,), extra: dict[str, bytes] | None = None,
          mimetype_first: bool = True, header: str = HEADER, prv_text: str = PRV_TEXT,
          manifest: str = MANIFEST) -> Path:
    entries: list[tuple[str, bytes, int]] = [
        ("version.xml", VERSION.encode(), zipfile.ZIP_DEFLATED),
        ("Contents/header.xml", header.encode(), zipfile.ZIP_DEFLATED),
    ]
    for i, sec in enumerate(sections):
        entries.append((f"Contents/section{i}.xml", sec.encode(), zipfile.ZIP_DEFLATED))
    entries += [
        ("Preview/PrvText.txt", prv_text.encode("utf-8"), zipfile.ZIP_DEFLATED),
        ("Preview/PrvImage.png", png(), zipfile.ZIP_DEFLATED),
        ("settings.xml", b'<?xml version="1.0" encoding="UTF-8"?><ha:HWPApplicationSetting '
                         b'xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app"/>', zipfile.ZIP_DEFLATED),
        ("META-INF/container.xml", CONTAINER.encode(), zipfile.ZIP_DEFLATED),
        ("META-INF/manifest.xml", manifest.encode(), zipfile.ZIP_DEFLATED),
        ("Contents/content.hpf", CONTENT_HPF.encode(), zipfile.ZIP_DEFLATED),
    ]
    for name, data in (extra or {}).items():
        entries.append((name, data, zipfile.ZIP_DEFLATED))
    mime = ("mimetype", b"application/hwp+zip", zipfile.ZIP_STORED)
    entries = [mime, *entries] if mimetype_first else [*entries[:2], mime, *entries[2:]]
    with zipfile.ZipFile(path, "w") as z:
        for name, data, ctype in entries:
            z.writestr(zipfile.ZipInfo(name, date_time=(2024, 3, 15, 9, 0, 0)), data,
                       compress_type=ctype)
    return path


def read_all(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def text_of(parts: dict[str, bytes]) -> str:
    return "\n".join(v.decode("utf-8", "replace") for k, v in parts.items()
                     if k.endswith((".xml", ".hpf", ".txt")))


# ---------------------------------------------------------------------------------------------


def test_sections_processed_run_aware(tmp_path):
    src = build(tmp_path / "in.hwpx", sections=(SECTION0, SECTION1))
    rep = run(src, tmp_path / "out.hwpx")
    parts = read_all(tmp_path / "out.hwpx")
    everything = text_of(parts)
    for pii in ("홍길동", "김민준", "이서연", "010-1234-5678", "900101-1234567", "12345678",
                "minjun@example.com", "박지훈", "최수아"):
        assert pii not in everything, pii
    sec0 = parts["Contents/section0.xml"].decode()
    # the name split over runs: the token sits in the run where it started (charPr 1 = bold)
    assert '<hp:run charPrIDRef="1"><hp:t>[이름1]</hp:t></hp:run>' in sec0
    assert '<hp:t><hp:tab/>연락처 010-****-5678</hp:t>' in sec0
    assert "[이름1]님은 고혈압으로 내원.<hp:lineBreak/>보호자 [이름2] 동반" in sec0
    assert "<hp:t>[주민번호]</hp:t>" in sec0            # typed by the 주민등록번호 label cell
    assert "표 아래 메모" in sec0 and "흉통 없음" in sec0
    assert "mailto:[이메일1]" in sec0
    assert "조절 양호" in sec0 and "기록" not in sec0      # tracked deletion wiped
    assert "[이름1]" in parts["Contents/section1.xml"].decode()   # cross-section propagation
    assert rep.counts["PERSON"] >= 4 and rep.counts["RRN"] == 1
    assert any("삭제된 글자" in w for w in rep.warnings)
    assert "홍길동" not in str(rep.to_dict())


def test_layout_cache_dropped_only_for_changed_paragraphs(tmp_path):
    body = (para(run_t("환자명: 홍길동"), pid=1) + para(run_t("특이사항 없음"), pid=2))
    src = build(tmp_path / "in.hwpx", sections=(section(body),))
    run(src, tmp_path / "out.hwpx")
    sec = read_all(tmp_path / "out.hwpx")["Contents/section0.xml"].decode()
    first, second = sec.split('<hp:p id="2"')
    assert "linesegarray" not in first          # rewritten paragraph: Hangul re-lays it out
    assert "linesegarray" in second             # untouched paragraph keeps its layout cache


def test_package_layout_and_untouched_parts(tmp_path):
    src = build(tmp_path / "in.hwpx")
    run(src, tmp_path / "out.hwpx")
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(tmp_path / "out.hwpx") as b:
        ia, ib = a.infolist(), b.infolist()
        assert [i.filename for i in ia] == [i.filename for i in ib]
        assert ib[0].filename == "mimetype" and ib[0].compress_type == zipfile.ZIP_STORED
        assert b.read("mimetype") == b"application/hwp+zip"
        assert [i.compress_type for i in ia] == [i.compress_type for i in ib]
        for name in ("version.xml", "settings.xml", "META-INF/container.xml",
                     "META-INF/manifest.xml"):
            assert a.read(name) == b.read(name), name
        sec = b.read("Contents/section0.xml")
        assert sec.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n<hs:sec')


def test_mimetype_moved_to_front(tmp_path):
    src = build(tmp_path / "in.hwpx", mimetype_first=False)
    run(src, tmp_path / "out.hwpx")
    with zipfile.ZipFile(tmp_path / "out.hwpx") as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    raw = (tmp_path / "out.hwpx").read_bytes()
    assert raw[30:38] == b"mimetype" and raw[38:57] == b"application/hwp+zip"   # OCF rule


def test_metadata_preview_and_authors(tmp_path):
    src = build(tmp_path / "in.hwpx")
    run(src, tmp_path / "out.hwpx")
    parts = read_all(tmp_path / "out.hwpx")
    hpf = parts["Contents/content.hpf"].decode()
    for pii in ("홍길동", "최수아", "김민준"):
        assert pii not in hpf
    assert "2024-03-15T01:02:03Z" in hpf and "<opf:language>ko</opf:language>" in hpf
    assert 'href="BinData/image1.jpg"' in hpf           # manifest untouched
    header = parts["Contents/header.xml"].decode()
    assert 'name="익명"' in header and "이서연" not in header
    assert "fontfaces" in header
    prv = parts["Preview/PrvText.txt"].decode("utf-8")
    assert prv and "홍길동" not in prv and "이서연" not in prv and "1234-5678" not in prv
    assert "[이름1]" in prv
    with Image.open(io.BytesIO(parts["Preview/PrvImage.png"])) as im:
        assert im.size == (160, 220) and im.format == "PNG"
        assert im.convert("L").getextrema() == (255, 255)


def test_bindata_images_and_ole(tmp_path):
    extra = {"BinData/image1.jpg": jpeg_with_exif(),
             "BinData/ole1.ole": "OLE 홍길동 엑셀".encode()}
    src = build(tmp_path / "in.hwpx", extra=extra)
    rep = run(src, tmp_path / "out.hwpx")
    parts = read_all(tmp_path / "out.hwpx")
    jpg = parts["BinData/image1.jpg"]
    assert b"Hong Gildong" not in jpg
    with Image.open(io.BytesIO(jpg)) as im:
        assert im.size == (64, 48) and not dict(im.getexif())
    assert parts["BinData/ole1.ole"] == b""
    assert any("이미지 1개는 검사하지 않음" in w for w in rep.warnings)
    assert any("포함된 개체" in w for w in rep.warnings)


def test_shape_comment_memo_author_and_paths(tmp_path):
    body = para(
        '<hp:run charPrIDRef="0"><hp:pic id="7"><hp:shapeComment>환자 윤서준 님 상처 사진'
        '</hp:shapeComment></hp:pic></hp:run>',
        '<hp:run charPrIDRef="0"><hp:ctrl><hp:fieldBegin id="2" type="MEMO" name="">'
        '<hp:parameters cnt="1" name=""><hp:stringParam name="Author">정하은</hp:stringParam>'
        '</hp:parameters><hp:subList><hp:p id="40"><hp:run><hp:t>메모: 정하은 확인 필요</hp:t>'
        '</hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl><hp:t>본문</hp:t></hp:run>', pid=1)
    header = HEADER.replace("</hh:head>", '<hh:docOption><hh:linkinfo path="C:\\Users\\hong\\'
                                          '문서\\홍길동.hwpx" pageInherit="0"/></hh:docOption>'
                                          "</hh:head>")
    src = build(tmp_path / "in.hwpx", sections=(section(body),), header=header)
    run(src, tmp_path / "out.hwpx")
    parts = read_all(tmp_path / "out.hwpx")
    sec = parts["Contents/section0.xml"].decode()
    assert "윤서준" not in sec and "상처 사진" in sec
    assert "정하은" not in sec and '<hp:stringParam name="Author">익명</hp:stringParam>' in sec
    head = parts["Contents/header.xml"].decode()
    assert "Users" not in head and "hong" not in head and "홍길동" not in head


def test_legacy_hwp_and_broken_files(tmp_path):
    hwp = tmp_path / "old.hwp"
    hwp.write_bytes(CFB_MAGIC + b"\x00" * 512)
    with pytest.raises(DocumentError) as ei:
        run(hwp, tmp_path / "out.hwpx")
    assert "한글에서 HWPX 또는 PDF로 저장 후 다시 시도" in str(ei.value)
    renamed = tmp_path / "renamed.hwpx"           # a binary .hwp with the wrong extension
    renamed.write_bytes(CFB_MAGIC + b"\x00" * 512)
    with pytest.raises(DocumentError) as ei:
        run(renamed, tmp_path / "out.hwpx")
    assert "HWPX" in str(ei.value)
    not_zip = tmp_path / "bad.hwpx"
    not_zip.write_bytes(b"garbage")
    with pytest.raises(DocumentError):
        run(not_zip, tmp_path / "out.hwpx")
    no_sections = tmp_path / "empty.hwpx"
    with zipfile.ZipFile(no_sections, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
    with pytest.raises(DocumentError):
        run(no_sections, tmp_path / "out.hwpx")
    assert not (tmp_path / "out.hwpx").exists()


def test_encrypted_package_is_refused(tmp_path):
    manifest = MANIFEST.replace("/>", '><odf:file-entry odf:full-path="Contents/section0.xml">'
                                      '<odf:encryption-data/></odf:file-entry></odf:manifest>')
    src = build(tmp_path / "in.hwpx", manifest=manifest)
    with pytest.raises(DocumentError) as ei:
        run(src, tmp_path / "out.hwpx")
    assert "암호" in str(ei.value)
    assert not (tmp_path / "out.hwpx").exists()


def test_same_path_refused(tmp_path):
    src = build(tmp_path / "in.hwpx")
    with pytest.raises(ValueError):
        run(src, src)


def test_deletion_spanning_paragraphs(tmp_path):
    body = (para(run_t('경과 <hp:deleteBegin Id="3" TcId="3" paraend="1"/>보호자 강도윤 연락'), pid=1)
            + para(run_t('삭제 계속<hp:deleteEnd Id="3" TcId="3" paraend="0"/> 이후 안정'), pid=2))
    src = build(tmp_path / "in.hwpx", sections=(section(body),))
    run(src, tmp_path / "out.hwpx")
    sec = read_all(tmp_path / "out.hwpx")["Contents/section0.xml"].decode()
    assert "강도윤" not in sec and "삭제 계속" not in sec
    assert "경과 " in sec and "이후 안정" in sec


def test_text_outside_paragraph_runs_is_checked(tmp_path):
    body = para('<hp:run charPrIDRef="0"><hp:equation id="9"><hp:script>환자명: 홍길동 TIMES 2'
                '</hp:script></hp:equation><hp:t>수식 참고</hp:t></hp:run>', pid=1)
    src = build(tmp_path / "in.hwpx", sections=(section(body),))
    run(src, tmp_path / "out.hwpx")
    sec = read_all(tmp_path / "out.hwpx")["Contents/section0.xml"].decode()
    assert "홍길동" not in sec and "TIMES 2" in sec and "수식 참고" in sec


# --------------------------------------------------------------------------- red-team regressions


def test_file_leaks_12_attribute_text(tmp_path):
    """file_leaks-12-hwpx-attributes: 글맵시 text, form object values/captions, 누름틀 field
    names and bookmark names (read with '_' as a space) are de-identified."""
    def run_x(inner: str) -> str:
        return f'<hp:run charPrIDRef="0">{inner}<hp:t/></hp:run>'
    body = (
        para(run_t("외래 안내문"), pid=1)
        + para(run_x('<hp:textart id="5" text="환자 국민아 님 010-8901-0001"><hp:textartPr '
                     'fontName="함초롬바탕"/></hp:textart>'), pid=2)
        + para(run_x('<hp:comboBox name="담당의" selectedValue="환자 경민호 010-8901-0002">'
                     '<hp:listItem displayText="환자 경민호 010-8901-0002" value="환자 경민호 010-8901-0002"/>'
                     '<hp:listItem displayText="환자 도지원 010-8901-0003" value="v2"/></hp:comboBox>'), pid=3)
        + para(run_x('<hp:btn name="b1" caption="환자 마동석 님 010-8901-0004" value="UNCHECKED"/>'), pid=4)
        + para('<hp:run charPrIDRef="0"><hp:ctrl><hp:fieldBegin id="7" type="CLICK_HERE" '
               'name="환자 사이다 010-8901-0006" editable="1"/></hp:ctrl><hp:t>입력</hp:t><hp:ctrl>'
               '<hp:fieldEnd beginIDRef="7"/></hp:ctrl></hp:run>', pid=5)
        + para('<hp:run charPrIDRef="0"><hp:ctrl><hp:bookmark name="환자_반하나_01089010005"/></hp:ctrl>'
               '<hp:t>참고</hp:t></hp:run>', pid=6)
    )
    src = build(tmp_path / "in.hwpx", sections=(section(body),))
    run(src, tmp_path / "out.hwpx")
    sec = read_all(tmp_path / "out.hwpx")["Contents/section0.xml"].decode()
    for v in ("010-8901-0001", "010-8901-0002", "010-8901-0003", "010-8901-0004", "01089010005",
              "010-8901-0006", "국민아", "경민호", "마동석", "반하나"):
        assert v not in sec, v
    assert 'value="UNCHECKED"' in sec and 'value="v2"' in sec and 'name="담당의"' in sec
    assert '<hp:bookmark name="환자_[이름' in sec          # underscores kept around the token


def test_file_leaks_06_hwpx_picture_comment_file_name(tmp_path):
    """file_leaks-06-office-picture-filenames: Hangul's picture comment keeps a neutral file
    name and loses the photo's date/camera lines."""
    comment = ("그림입니다.\n원본 그림의 이름: 상처사진_하은별.jpg\n원본 그림의 크기: 가로 4032pixel, "
               "세로 3024pixel\n사진 찍은 날짜: 2024년 03월 01일 오전 10:12\n카메라 모델: SM-S918N")
    body = para(run_t("증례: 하지 궤양, 상처 사진 첨부"),
                f'<hp:run charPrIDRef="0"><hp:pic id="1"><hp:shapeComment>{comment}</hp:shapeComment>'
                '</hp:pic><hp:t/></hp:run>', pid=1)
    src = build(tmp_path / "in.hwpx", sections=(section(body),))
    rep = run(src, tmp_path / "out.hwpx")
    sec = read_all(tmp_path / "out.hwpx")["Contents/section0.xml"].decode()
    assert "하은별" not in sec and "사진 찍은 날짜" not in sec and "SM-S918N" not in sec
    assert "원본 그림의 이름: file.jpg" in sec and "원본 그림의 크기: 가로 4032pixel" in sec
    assert any("파일 이름" in w for w in rep.warnings)
    assert H.clean_picture_comment("환자 사진") == "환자 사진"


def test_file_leaks_10_hwpx_bindata_emf_blanked(tmp_path):
    """file_leaks-10-office-vector-image-text: the same vector-picture check serves HWPX
    BinData (an EMF of a pasted table showing a patient)."""
    import struct

    from deid.formats.docx import vector_texts
    txt = "환자 하은별 010-2233-4455".encode("utf-16-le")
    emf = (struct.pack("<II", 1, 88) + b"\0" * 32 + b" EMF" + b"\0" * 44
           + struct.pack("<II", 0x54, 84 + len(txt)) + b"\0" * 76 + txt
           + struct.pack("<II", 0x0E, 20) + b"\0" * 12)
    src = build(tmp_path / "in.hwpx", extra={"BinData/image2.emf": emf})
    rep = run(src, tmp_path / "out.hwpx")
    out = read_all(tmp_path / "out.hwpx")["BinData/image2.emf"]
    assert txt not in out and vector_texts(out, ".emf") == ([], False)
    assert any("벡터 그림" in w for w in rep.warnings)


def test_security_zip_bomb_hwpx_refused(tmp_path, monkeypatch):
    """security-zip-bomb-docx-hwpx: an HWPX that unpacks beyond the budget is refused."""
    from deid.formats import text as T
    src = build(tmp_path / "in.hwpx", extra={"BinData/image1.bin": b"\0" * (3 * 1024 * 1024)})
    monkeypatch.setattr(T, "ZIP_MAX_ENTRY", 1024 * 1024)
    with pytest.raises(DocumentError, match="zip bomb"):
        run(src, tmp_path / "out.hwpx")
    assert not (tmp_path / "out.hwpx").exists()
