"""Tests for deid.formats.text (plain text, JSON, HTML, XML, encodings, deid_text)."""
from __future__ import annotations

import codecs
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

from deid.anonymizer import Anonymizer
from deid.config import default_config
from deid.engine import Detector
from deid.formats import text as T

NAMES = ("홍길동", "김민준", "이서연", "최수아")


@pytest.fixture()
def env():
    cfg = default_config()
    return Detector(cfg), Anonymizer(cfg), cfg


def run(tmp_path: Path, env, name: str, data: bytes) -> tuple[bytes, object]:
    det, an, cfg = env
    src = tmp_path / name
    src.write_bytes(data)
    dst = tmp_path / "out" / name
    report = T.process(src, dst, det, an, cfg)
    return dst.read_bytes(), report


def assert_clean(text: str, *values: str) -> None:
    for v in values:
        assert v not in text, f"PII left in output: {v!r}"


def assert_report_clean(report, *values: str) -> None:
    blob = json.dumps(report.to_dict(), ensure_ascii=False)
    for v in values:
        assert v not in blob


# --------------------------------------------------------------------------- encodings


@pytest.mark.parametrize("codec,bom,expected", [
    ("utf-8", b"", "utf-8"),
    ("utf-8", codecs.BOM_UTF8, "utf-8"),
    ("cp949", b"", "cp949"),
    ("euc-kr", b"", "cp949"),
    ("utf-16-le", codecs.BOM_UTF16_LE, "utf-16-le"),
    ("utf-16-be", codecs.BOM_UTF16_BE, "utf-16-be"),
    ("utf-32-le", codecs.BOM_UTF32_LE, "utf-32-le"),
])
def test_detect_encoding(codec, bom, expected):
    text = "환자 홍길동님 내원\r\n"
    enc = T.detect_encoding(bom + text.encode(codec))
    assert enc.codec == expected and enc.bom == bom
    assert enc.decode(bom + text.encode(codec)) == text
    assert enc.encode(text) == bom + text.encode(expected)


def test_detect_encoding_bomless_utf16():
    data = "Patient Hong 홍길동, phone 010-1234-5678\n".encode("utf-16-le")
    assert T.detect_encoding(data).codec == "utf-16-le"


def test_detect_encoding_rejects_binary_and_garbage():
    with pytest.raises(T.TextEncodingError):
        T.detect_encoding(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01")
    with pytest.raises(T.TextEncodingError):
        T.detect_encoding(b"\xff\xfe\xfd\x80\x81 abc \xc8")


def test_cp949_crlf_round_trip(tmp_path, env):
    text = "환자 홍길동님 내원\r\n연락처 010-1234-5678\r\n홍길동 재방문 예정\r\n"
    out, report = run(tmp_path, env, "note.txt", text.encode("cp949"))
    decoded = out.decode("cp949")          # output keeps the input encoding
    assert "\r\n" in decoded and decoded.count("\r\n") == 3 and "\n\n" not in decoded
    assert_clean(decoded, "홍길동", "1234-5678")
    assert "010-****-5678" in decoded
    assert report.counts["PERSON"] == 2 and report.counts["PHONE"] == 1
    assert report.source.endswith("note.txt") and report.output.endswith("note.txt")
    assert_report_clean(report, "홍길동", "010-1234-5678")


def test_utf8_bom_and_utf16_kept(tmp_path, env):
    text = "보호자 김민준씨 연락 바람\n"
    out, _ = run(tmp_path, env, "a.md", codecs.BOM_UTF8 + text.encode("utf-8"))
    assert out.startswith(codecs.BOM_UTF8)
    assert_clean(out[3:].decode("utf-8"), "김민준")
    out, _ = run(tmp_path, env, "b.log", codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
    assert out.startswith(codecs.BOM_UTF16_LE)
    assert_clean(out[2:].decode("utf-16-le"), "김민준")


def test_binary_input_fails_closed(tmp_path, env):
    det, an, cfg = env
    src = tmp_path / "x.txt"
    src.write_bytes(b"\x00\x01\x02\x03binary\x00\xff")
    dst = tmp_path / "out.txt"
    with pytest.raises(T.TextEncodingError) as e:
        T.process(src, dst, det, an, cfg)
    assert "encoding" in str(e.value) or "binary" in str(e.value)
    assert not dst.exists()
    assert not list(tmp_path.glob("*.deid-tmp"))


def test_refuses_to_overwrite_source(tmp_path, env):
    det, an, cfg = env
    src = tmp_path / "x.txt"
    src.write_text("홍길동님", encoding="utf-8")
    with pytest.raises(ValueError):
        T.process(src, src, det, an, cfg)
    assert src.read_text(encoding="utf-8") == "홍길동님"


def test_unencodable_replacement_falls_back(tmp_path):
    cfg = default_config()
    cfg.labels["PERSON"] = "이름☃"   # snowman is not in CP949
    det, an = Detector(cfg), Anonymizer(cfg)
    src = tmp_path / "x.txt"
    src.write_bytes("환자 홍길동님".encode("cp949"))
    report = T.process(src, tmp_path / "o.txt", det, an, cfg)
    out = (tmp_path / "o.txt").read_bytes().decode("cp949")
    assert_clean(out, "홍길동")
    assert report.warnings


# --------------------------------------------------------------------------- deid_text


def test_deid_text_basic(env):
    det, an, _ = env
    out, report = T.deid_text("환자 홍길동님 010-1234-5678\n\n홍길동 재방문", det, an)
    assert out == "환자 [이름1]님 010-****-5678\n\n[이름1] 재방문"
    assert report.counts == {"PERSON": 2, "PHONE": 1}
    assert report.source == "" and report.output == ""


def test_split_paragraphs_keeps_text():
    para = "가나다라 마바사 아자차카.\n" * 40
    text = (para + "\n") * 30 + "x" * 50000 + "\n끝"
    chunks = T.split_paragraphs(text, max_chars=2000)
    assert len(chunks) > 5
    assert "".join(chunks) == text
    assert all(c.endswith(("\n", "끝")) for c in chunks)


def test_deid_text_long_text_propagates_across_chunks(env, monkeypatch):
    det, an, _ = env
    calls = []
    orig = det.detect_document

    def spy(chunks):
        calls.append(len(chunks))
        return orig(chunks)

    monkeypatch.setattr(det, "detect_document", spy)
    filler = "\n\n".join(f"{i}번째 문단: 경과 관찰 중, 특이 소견 없음." * 5 for i in range(400))
    text = "환자 이서연님 내원.\n\n" + filler + "\n\n이서연 다음 주 재방문."
    out, report = T.deid_text(text, det, an)
    assert len(calls) == 1 and calls[0] > 1        # one detect_document over many chunks
    assert_clean(out, "이서연")
    assert out.endswith("[이름1] 다음 주 재방문.")
    assert report.counts["PERSON"] == 2


def test_srt_timestamps_untouched(tmp_path, env):
    srt = ("1\n00:00:01,000 --> 00:00:04,000\n홍길동 환자분 안녕하세요\n\n"
           "2\n00:00:05,000 --> 00:00:07,500\n홍길동 씨 010-1234-5678로 연락 주세요\n")
    out, _ = run(tmp_path, env, "talk.srt", srt.encode("utf-8"))
    text = out.decode("utf-8")
    assert "00:00:01,000 --> 00:00:04,000" in text and "00:00:05,000 --> 00:00:07,500" in text
    assert_clean(text, "홍길동", "1234-5678")


# --------------------------------------------------------------------------- JSON


PATIENT = {
    "patient": {
        "name": "김민준",
        "age": 39,
        "phone": 1012345678,
        "rrn": "900101-1234567",
        "dob": "1985-03-02",
        "address": "서울특별시 강남구 테헤란로 123",
        "memo": "김민준 보호자 이서연 동행, 이서연 연락처 010-2222-3333",
    },
    "drug": {"name": "타이레놀", "dose": 500},
    "contacts": ["minjun@example.com"],
    "010-9876-5432": "키에 들어간 번호",
}


@pytest.mark.parametrize("indent", [2, 4, "\t", None])
def test_json_structure_and_values(tmp_path, env, indent):
    raw = json.dumps(PATIENT, ensure_ascii=False, indent=indent) + "\n"
    out, report = run(tmp_path, env, "p.json", raw.encode("utf-8"))
    text = out.decode("utf-8")
    data = json.loads(text)
    assert_clean(text, "김민준", "이서연", "1234567", "1012345678", "2222-3333", "9876-5432",
                 "minjun@", "테헤란로", "1985-03-02")
    p = data["patient"]
    assert p["name"] == "[이름1]"
    assert p["memo"].startswith("[이름1] 보호자 [이름2] 동행")
    assert p["phone"] == "010****5678"            # number restored with its leading 0, masked
    assert p["rrn"] == "[주민번호]" and p["dob"] == "1985년생"
    assert p["address"] == "서울특별시 강남구 [주소]"
    assert p["age"] == 39
    assert data["drug"] == {"name": "타이레놀", "dose": 500}   # generic "name" without person context
    assert data["contacts"] == ["[이메일1]"]
    assert list(data) == ["patient", "drug", "contacts", "010-****-5432"]
    assert any("JSON" in w for w in report.warnings)
    # layout: same indentation style and trailing newline
    expected = json.dumps(json.loads(text), ensure_ascii=False, indent=indent) + "\n"
    assert text == expected
    assert_report_clean(report, "김민준", "이서연")


def test_json_crlf_and_compact_separators(tmp_path, env):
    raw = json.dumps({"환자명": "홍길동", "메모": "홍길동님 내원"}, ensure_ascii=False,
                     indent=2).replace("\n", "\r\n")
    out, _ = run(tmp_path, env, "c.json", raw.encode("utf-8"))
    text = out.decode("utf-8")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert json.loads(text) == {"환자명": "[이름1]", "메모": "[이름1]님 내원"}
    compact = '{"name":"홍길동","phone":"010-1234-5678","sex":"M"}'
    out, _ = run(tmp_path, env, "d.json", compact.encode("utf-8"))
    assert out.decode("utf-8") == '{"name":"[이름1]","phone":"010-****-5678","sex":"M"}'


def test_json_ascii_escapes_kept_ascii(tmp_path, env):
    raw = json.dumps({"patient_name": "홍길동", "n": 1})   # ensure_ascii=True -> \\uXXXX
    out, _ = run(tmp_path, env, "e.json", raw.encode("ascii"))
    text = out.decode("ascii")
    assert text.isascii()
    assert json.loads(text) == {"patient_name": "[이름1]", "n": 1}


def test_invalid_json_falls_back_to_text(tmp_path, env):
    out, report = run(tmp_path, env, "bad.json", '{"memo": "환자 홍길동님", oops'.encode())
    assert out.decode("utf-8") == '{"memo": "환자 [이름1]님", oops'
    assert any("invalid JSON" in w for w in report.warnings)


def test_jsonl(tmp_path, env):
    raw = ('{"name": "홍길동", "age": 30}\n\n'
           '{"note": "홍길동 재방문", "tel": 1098765432}\r\n')
    out, _ = run(tmp_path, env, "x.jsonl", raw.encode("utf-8"))
    text = out.decode("utf-8")
    assert text == ('{"name": "[이름1]", "age": 30}\n\n'
                    '{"note": "[이름1] 재방문", "tel": "010****5432"}\r\n')


# --------------------------------------------------------------------------- HTML


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(("start", tag, tuple(sorted(k for k, _ in attrs))))

    def handle_endtag(self, tag):
        self.tags.append(("end", tag))


def _tag_shape(markup: str) -> list[tuple]:
    p = _Tags()
    p.feed(markup)
    p.close()
    return p.tags


HTML = """<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="author" content="최수아">
<title>환자 홍길동님 진료 기록</title>
<style>.name { color: red } /* 홍길동 */</style>
<script type="application/ld+json">{"patient": "홍길동"}</script>
</head>
<body class="patient-view" id="홍길동-not-touched">
<!-- 보호자 이서연 메모 -->
<h1 title="환자명: 김민준">진료 요약</h1>
<p data-note="연락처 010-1234-5678">환자 <b>홍길동</b>님 &amp; 보호자 &#51060;&#49436;&#50672;&#45784;</p>
<img src="photo.png" alt="환자 홍길동님 사진">
<a href="mailto:minjun@example.com" class="mail">메일 보내기</a>
<input type="text" name="patient" value=김민준님>
<p>BP &lt; 120 and HR > 60</p>
</body></html>
"""


def test_html_text_and_attributes(tmp_path, env):
    out, report = run(tmp_path, env, "r.html", HTML.encode("utf-8"))
    text = out.decode("utf-8")
    # everything a reader can see or a crawler can read is clean
    visible = text.replace('id="홍길동-not-touched"', "")
    assert_clean(visible, "홍길동", "김민준", "이서연", "최수아", "1234-5678", "minjun@")
    assert "&#51060;" not in text                      # char refs decoded before detection
    assert '<meta name="author" content="[이름' in text  # meta author is a PERSON
    assert 'title="환자명: [이름' in text
    hong = re.search(r"<title>환자 (\[이름\d+\])님", text).group(1)   # 최수아 came first
    assert f'alt="환자 {hong}님 사진"' in text
    assert f'{{"patient": "{hong}"}}' in text           # script bodies are checked
    assert f"/* {hong} */" in text                      # style bodies are checked too
    assert 'id="홍길동-not-touched"' in text            # structural attributes are not
    assert "<!-- 보호자 [이름" in text
    assert 'class="patient-view"' in text and 'href="mailto:[이메일1]"' in text
    assert "value=[이름" in text
    assert "BP &lt; 120 and HR > 60" in text
    assert _tag_shape(text) == _tag_shape(HTML)          # markup structure unchanged
    assert_report_clean(report, "홍길동", "김민준")


def test_html_unquoted_attribute_requoted_when_needed():
    cfg = default_config()
    cfg.labels["PERSON"] = "환자 이름"          # replacement token now contains a space
    det, an = Detector(cfg), Anonymizer(cfg)
    html = '<p title="환자 홍길동님"><input value=홍길동님 data-x=1></p>'
    out, _ = T.deid_document(html, "html", det, an)
    assert out == '<p title="환자 [환자 이름1]님"><input value="[환자 이름1]님" data-x=1></p>'
    assert _tag_shape(out) == _tag_shape(html)


# --------------------------------------------------------------------------- XML


XML = """<?xml version="1.0" encoding="UTF-8"?>
<records xmlns:h="urn:hospital">
  <patient id="A-1" name="이서연">
    <name>이서연</name>
    <phone>010-2222-3333</phone>
    <birthDate>1990-02-02</birthDate>
    <h:memo>이서연 환자 &lt;재방문&gt; &amp; 보호자 동행</h:memo>
    <![CDATA[이서연 메모 <raw>]]>
  </patient>
  <item><name>타이레놀</name></item>
  <note>환자명: 홍길동</note>
</records>
"""


def test_xml_elements_attributes_and_structure(tmp_path, env):
    out, report = run(tmp_path, env, "r.xml", XML.encode("utf-8"))
    text = out.decode("utf-8")
    assert_clean(text, "이서연", "홍길동", "2222-3333", "1990-02-02")
    root = ET.fromstring(out)
    src = ET.fromstring(XML.encode("utf-8"))
    assert [e.tag for e in root.iter()] == [e.tag for e in src.iter()]
    p = root.find("patient")
    assert p.get("id") == "A-1" and p.get("name") == p.findtext("name") == "[이름1]"
    assert p.findtext("phone") == "010-****-3333"
    assert p.findtext("birthDate") == "1990년생"
    assert p.findtext("{urn:hospital}memo") == "[이름1] 환자 <재방문> & 보호자 동행"
    assert "<![CDATA[[이름1] 메모 <raw>]]>" in text
    assert root.find("item").findtext("name") == "타이레놀"  # "name" outside a person element
    assert root.findtext("note") == "환자명: [이름2]"
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<records xmlns:h="urn:hospital">')
    assert report.counts["PERSON"] >= 5


def test_xml_container_text_not_typed(env):
    det, an, _ = env
    xml = "<patient>메모 있음<name>김민준</name>추가 메모</patient>"
    out, _ = T.deid_document(xml, "xml", det, an)
    # "patient" is a container here, so its own text is not swallowed as a name
    assert out == "<patient>메모 있음<name>[이름1]</name>추가 메모</patient>"


# --------------------------------------------------------------------------- helpers


def test_typed_spans_person_lists_and_relations():
    spans = T.typed_spans("PERSON", "홍길동, 김민준(모) / 이서연님")
    assert [s.text for s in spans] == ["홍길동", "김민준", "이서연"]
    assert T.typed_spans("PERSON", "-") == [] and T.typed_spans("PERSON", "없음") == []
    assert T.typed_spans("PERSON", "본인") == []
    assert [s.text for s in T.typed_spans("PHONE", "010-1234-5678 010-9876-5432")] == [
        "010-1234-5678", "010-9876-5432"]
    assert [s.text for s in T.typed_spans("PHONE", " 010-1234-5678(본인) ")] == ["010-1234-5678(본인)"]
    assert [s.text for s in T.typed_spans("EMAIL", "a@x.com; b@y.org")] == ["a@x.com", "b@y.org"]


def test_numeric_text():
    assert T.numeric_text(1012345678, "PHONE") == "01012345678"
    assert T.numeric_text(15881234, "PHONE") == "15881234"
    assert T.numeric_text(501013234567, "RRN") == "0501013234567"
    assert T.numeric_text(42, None) is None
    assert T.numeric_text(True, "PHONE") is None
    assert T.numeric_text(1012345678.0, None) == "01012345678"
    assert T.numeric_text(3.5, None) is None


def test_kind_for_and_extensions():
    assert T.kind_for(Path("a.JSON")) == "json"
    assert T.kind_for(Path("a.htm")) == "html"
    assert T.kind_for(Path("a.xml")) == "xml"
    assert T.kind_for(Path("a.vtt")) == "text"
    assert ".csv" not in T.EXTENSIONS and ".txt" in T.EXTENSIONS


def test_html_table_columns_typed_like_a_spreadsheet(env):
    det, an, _ = env
    html = ("<table border=1>\n"
            "<tr><th>이름</th><th>연락처</th><th>메모</th></tr>\n"
            "<tr><td>홍길동</td><td>010-1234-5678</td><td>김민준 보호자 동행</td></tr>\n"
            "<tr><td><b>김민준</b> 님</td><td>02-123-4567<td>특이사항 없음\n"
            "<tr><td>이서연</td><td>-</td><td><table><tr><td>중첩</td></tr></table>이서연 재방문</td></tr>\n"
            "</table>\n<p>홍길동 외 2명</p>")
    out, report = T.deid_document(html, "html", det, an)
    assert_clean(out, "홍길동", "김민준", "이서연", "1234-5678")
    assert "<td>[이름1]</td>" in out and "<td>[이름2] 보호자 동행</td>" in out
    assert "<b>[이름2]</b> 님" in out and "<td>-</td>" in out and "<td>중첩</td>" in out
    assert "<p>[이름1] 외 2명</p>" in out
    assert _tag_shape(out) == _tag_shape(html)
    assert report.counts == {"PERSON": 6, "PHONE": 2}


def test_rtf_is_refused(tmp_path, env):
    det, an, cfg = env
    rtf = r"{\rtf1\ansi\ansicpg949 \'c8\'ab\'b1\'e6\'b5\'bf \par}"
    for name in ("memo.rtf", "memo.txt"):           # by extension or by content
        src = tmp_path / name
        src.write_text(rtf, encoding="ascii")
        with pytest.raises(ValueError, match="RTF"):
            T.process(src, tmp_path / ("out-" + name), det, an, cfg)
        assert not (tmp_path / ("out-" + name)).exists()


# --------------------------------------------------------------------------- tables pasted as text


def _deid(text: str, cfg=None) -> tuple[str, object]:
    cfg = cfg or default_config()
    return T.deid_text(text, Detector(cfg), Anonymizer(cfg))


def _shape(text: str) -> list[int]:
    return [line.count("\t") for line in text.splitlines()]


def test_tl01_tsv_clipboard_headers_typed_by_column():
    """tl01-tsv-clipboard-headers: Excel cells copied as TSV are typed by their header row
    exactly like a .tsv file (bare names, MRNs and birth dates have no other context)."""
    src = ("등록번호\t환자명\t생년월일\t연락처\n10293847\t표순자\t1949-02-11\t010-2283-1190\n"
           "10293911\t맹옥분\t1952-07-30\t010-7721-4432\n")
    out, rep = _deid(src)
    assert_clean(out, "표순자", "맹옥분", "10293847", "10293911", "1949-02-11", "1952-07-30",
                 "2283-1190")
    assert out.splitlines()[0] == "등록번호\t환자명\t생년월일\t연락처"
    assert _shape(out) == _shape(src) and out.endswith("\n")
    assert "1949년생" in out
    assert rep.counts["PERSON"] == 2 and rep.counts["MRN"] == 2

    src = ("번호\t이름\t성별\t나이\t연락처\t진단\n1\t국현우\t남\t45\t010-2283-1190\tHTN\n"
           "2\t소윤진\t여\t38\t01077214432\tDM")
    out, _ = _deid(src)
    assert_clean(out, "국현우", "소윤진")
    assert "\tHTN" in out and "\t남\t45\t" in out


def test_tl01_csv_text_with_header_row():
    """tl01-tsv-clipboard-headers: comma-separated rows under a recognised header row."""
    src = ("성명,주민번호,휴대폰,비고\n김도윤,8503151234567,010-1234-5679,재진\n"
           "이서연,920407-2345678,010-2345-6780,초진")
    out, _ = _deid(src)
    assert_clean(out, "김도윤", "이서연", "8503151234567", "920407-2345678")
    assert out.count(",") == src.count(",") and ",재진" in out and ",초진" in out
    # prose with commas is no table: nothing is typed, the text stays as it was
    prose = "환자는 두통, 어지럼, 오심을 호소함.\n혈압은 120/80, 맥박 72회, 체온 36.5도."
    assert _deid(prose)[0] == prose


def test_mac_clip_tsv_rows_leak_crlf_and_quoted_cells():
    """mac_clip_tsv_rows_leak: rows copied from Excel/Numbers (CRLF line ends, quoted cells
    with line breaks) under a 이름 header lose every name; layout is byte-identical."""
    names = ("최유나", "박도현", "윤서진")
    src = "이름\t나이\t진단\t담당\r\n" + "".join(
        f"{n}\t{a}\t{d}\t외래\r\n" for n, a, d in zip(names, (45, 52, 38),
                                                       ("고혈압", "당뇨", "갑상선기능저하증")))
    out, rep = _deid(src)
    assert_clean(out, *names)
    assert out.count("\r\n") == src.count("\r\n") and _shape(out) == _shape(src)
    assert "\t고혈압\t외래" in out and rep.counts["PERSON"] == 3
    src = '이름\t메모\t연락처\r\n최유나\t"재진\n오후 내원"\t010-1111-2222\r\n박도현\t-\t-\r\n'
    out, _ = _deid(src)
    assert_clean(out, "최유나", "박도현", "1111-2222")
    assert '"재진\n오후 내원"' in out


def test_tl01_headerless_rows_with_sex_and_age():
    """tl01-tsv-clipboard-headers: headerless rows 'name | sex | age | ...'."""
    out, _ = _deid("여민하\tF\t29\t고혈압\n도하준\tM\t61\t당뇨\n1\t선우재희\t남\t45세\t천식\n")
    assert_clean(out, "여민하", "도하준", "선우재희")
    assert "고혈압" in out and "당뇨" in out and "천식" in out
    # a diagnosis list with sex/age but no names stays as it is
    rows = "고혈압\tM\t61\n당뇨\tF\t45\n"
    assert _deid(rows)[0] == rows


def test_tl01_text_file_with_tsv_content(tmp_path, env):
    """tl01-tsv-clipboard-headers: the same rows saved as .txt are typed too."""
    data = "이름\t나이\t연락처\n최유나\t45\t010-1111-2222\n박도현\t52\t-\n".encode("utf-8")
    out, _ = run(tmp_path, env, "rows.txt", data)
    text = out.decode("utf-8")
    assert_clean(text, "최유나", "박도현")
    assert _shape(text) == _shape(data.decode())


def test_tl01_form_rows_and_generic_name_header():
    """Label/value rows ("성명<TAB>홍길동") are typed; an '이름' column of drug names is not."""
    out, _ = _deid("성명\t홍길동\n연락처\t010-1234-5678\n주소\t두통\n")
    assert_clean(out, "홍길동", "1234-5678")
    assert "주소\t두통" in out
    drugs = "이름\t용량\t용법\n암로디핀\t5mg\t1일 1회\n메트포르민\t1000mg\t1일 2회\n"
    assert _deid(drugs)[0] == drugs


def test_over_mask_01_looks_like_address():
    """over_mask-01: '주소' is also the chief complaint (主訴); only address-like values are
    typed ADDRESS."""
    for v in ("서울특별시 강남구 테헤란로 152", "서울 강남구", "경기 성남시 분당구", "역삼동 123-4",
              "세종대로 110", "래미안 101동 1203호", "123 Main Street", "Seoul, Korea"):
        assert T.looks_like_address(v), v
    for v in ("두통", "우측 하복부 통증", "어지럼", "3일 전부터 두통, 오심 동반", "열성 경기",
              "운동 2회 후 흉통", "허리 통증 3주", "ST elevation", "abd pain x 3 days", "-"):
        assert not T.looks_like_address(v), v
    assert T.typed_spans("ADDRESS", "두통") == []
    assert [s.text for s in T.typed_spans("ADDRESS", "서울시 강남구 테헤란로 152")] == [
        "서울시 강남구 테헤란로 152"]


def test_over_mask_01_chief_complaint_json_and_html(tmp_path, env):
    """over_mask-01: JSON key / HTML header '주소' over symptoms is not ADDRESS and the symptom
    is not propagated; a real address under the same key still is."""
    data = json.dumps({"visit": {"주소": "어지럼", "현병력": "기립 시 어지럼"},
                       "patient": {"주소": "서울특별시 강남구 테헤란로 152", "메모": "테헤란로 152 거주"}},
                      ensure_ascii=False).encode("utf-8")
    out, _ = run(tmp_path, env, "cc.json", data)
    doc = json.loads(out)
    assert doc["visit"] == {"주소": "어지럼", "현병력": "기립 시 어지럼"}
    assert "테헤란로" not in doc["patient"]["주소"]
    html_src = ("<table><tr><th>내원일</th><th>주소</th><th>진단</th></tr>"
                "<tr><td>2024-03-15</td><td>우측 하복부 통증</td><td>급성 충수염 의심</td></tr>"
                "<tr><td>2024-03-16</td><td>두통</td><td>긴장성 두통</td></tr></table>")
    out, _ = run(tmp_path, env, "cc.html", html_src.encode("utf-8"))
    assert out.decode("utf-8") == html_src


def test_merge_typed_scales_to_large_pasted_tables():
    """A pasted table with thousands of typed cells is merged without quadratic work."""
    rows = "\n".join(f"김민{chr(0xAC00 + i % 400)}\t010-2283-{i:04d}" for i in range(3000))
    out, rep = _deid("이름\t연락처\n" + rows)
    assert rep.counts["PERSON"] >= 3000 - 50 and "2283-0001" not in out
