"""Word documents (.docx .docm .dotx .dotm), processed at the XML level of the package.

Every XML part of the document folder (body, headers/footers of all sections, footnotes,
endnotes, comments, text boxes, glossary, SmartArt and chart text) is scanned for paragraphs
(any element named ``p``); the texts of their ``t`` elements are concatenated, all paragraphs of
the file go through ``detector.detect_document`` together, and each replacement is written into
the run where the finding starts while its remaining characters are removed from the following
runs. Run formatting is kept (a name whose first syllable is bold becomes a bold "[이름1]").
Tabs and line breaks count as separators for detection and are kept.

Also handled:

* track changes: deleted text (``w:delText``, ``w:moveFrom``) is wiped - deleted text still
  leaks otherwise; author names of insertions/deletions/comments become "익명"; ``people.xml``
  presence info (e-mail / account ids) is removed. Authors are also registered as known names,
  so the same names in the text are masked.
* field codes (``w:instrText``, ``w:fldSimple``), alt text / titles of pictures and shapes,
  watermark text, hyperlink tooltips, chart caches, custom XML data bound to content controls.
* hyperlinks: mailto:/tel: targets are anonymized, web links are checked, local file paths
  (user name in the path, often a patient's name in the file name) become "file.<ext>".
* properties: author, last modified by, title, subject, keywords, comments, category, company,
  manager and custom properties are cleared; document variables are removed; the saved
  thumbnail is blanked.
* images: JPEG/PNG metadata (EXIF/GPS, text chunks) is stripped. Raster images are OCR'd and
  PII is painted over: ``[ocr] docx_images = "auto"`` (default) OCRs every image big enough to
  hold a screenshot (>= 40,000 pixels), ``true`` all of them, ``false`` none; a warning counts
  the images that were not checked. Vector pictures (EMF/WMF - Word's "paste as picture" and
  the visible preview of embedded objects - and SVG) carry their text as text: it is checked
  with the document, an EMF/WMF showing personal data is replaced by an empty picture of the
  same size, SVG text is replaced. Embedded objects (Excel sheets, OLE, alternative-format
  chunks) cannot be checked and are emptied (warning). VBA macros are kept but not scanned
  (warning).
* picture names: Word stores the original file name / path of an inserted picture
  (``pic:cNvPr name``, ``wp:docPr descr`` of Word 2007/2010), often "상처사진_<patient>.jpg";
  values that are file names or paths become "file.<ext>".
* safety net: after the main pass, any other text node that still contains a removed value is
  replaced too.

The zip is rewritten with the same entry order and compression; untouched parts stay
byte-identical. Legacy .doc files and password-protected documents are refused with a clear
message. The generic pieces (:class:`ZipPackage`, :class:`TextSpec`, :class:`ParagraphWalker`,
:class:`PackageJob`, :func:`strip_image_metadata`) are shared with ``hwpx.py``.
"""
from __future__ import annotations

import gzip
import io
import posixpath
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable
from urllib.parse import quote, unquote

from .. import entities as E
from ..types import AnonResult, Report, Span
from .text import (
    ZIP_TOO_BIG_ERROR,
    Batch,
    check_paths,
    field_entities,
    write_atomic,
    zip_size_problem,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..anonymizer import Anonymizer
    from ..config import Config
    from ..engine import Detector

EXTENSIONS = frozenset({".docx", ".docm", ".dotx", ".dotm"})
ANONYMOUS = "익명"
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # OLE compound file: .doc, .hwp, encrypted Office
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
RASTER_EXTS = frozenset({".png", ".jpg", ".jpeg", ".jpe", ".gif", ".bmp", ".tif", ".tiff", ".webp"})
VECTOR_EXTS = frozenset({".emf", ".wmf", ".emz", ".wmz", ".svg"})
IMAGE_EXTS = RASTER_EXTS | VECTOR_EXTS | {".pict", ".pct", ".jxr", ".wdp"}
# Extensions that make an attribute value a file name ("CT_홍길동_850315.png").
FILE_NAME_EXTS = IMAGE_EXTS | {
    ".heic", ".heif", ".pdf", ".doc", ".docx", ".hwp", ".hwpx", ".xls", ".xlsx", ".xlsm", ".csv",
    ".ppt", ".pptx", ".txt", ".rtf", ".dcm", ".dicom", ".mp4", ".mov", ".avi", ".zip", ".png"}
AUTO_OCR_PIXELS = 40_000     # [ocr] docx_images = "auto": OCR images of at least this many
AUTO_OCR_MIN_SIDE = 24       # pixels (a screenshot; icons and bullets are skipped)
CONTACT_SCHEMES = frozenset({"mailto", "tel", "sms", "callto", "facetime", "kakaotalk"})
WEB_SCHEMES = frozenset({"http", "https", "ftp"})
URL_SAFE = ":/?#[]@!$&'()*+,;=-._~"
TEXT_ATTRS = frozenset({"descr", "title", "alt", "string", "tooltip", "instr"})
NAME_ATTR_OWNERS = frozenset({"docPr", "cNvPr"})
CORE_CLEAR = frozenset({"creator", "lastModifiedBy", "title", "subject", "keywords", "description",
                        "category", "contentStatus", "identifier"})
APP_CLEAR = frozenset({"Company", "Manager", "HyperlinkBase", "Template"})
# Attributes never rewritten by the safety net (identifiers, namespaces, references).
_RESIDUAL_SKIP_ATTRS = frozenset({"id", "Id", "Target", "Type", "PartName", "ContentType"})
_GENERIC_AUTHORS = frozenset("""
author user admin administrator owner unknown pc home 사용자 작성자 관리자 python-docx
""".split()) | {"microsoft office user", "microsoft office 사용자", "windows user", "windows 사용자",
                "microsoft office", "apache poi", "libreoffice", ANONYMOUS}

_OPEN_ERROR = ("Word 문서를 열 수 없습니다. 손상되었거나 .docx 형식이 아닙니다. Word에서 .docx로 다시 저장한 뒤 "
               "시도하세요. (cannot open the Word document: damaged or not a .docx file)")
_LEGACY_ERROR = ("예전 Word 형식(.doc)이거나 암호가 걸린 문서입니다. 암호를 해제하고 Word에서 '다른 이름으로 저장' → "
                 ".docx로 저장한 뒤 다시 시도하세요. (legacy .doc or password-protected document: "
                 "remove the password and save as .docx first)")
_INTERNAL_ERROR = ("문서 텍스트를 안전하게 바꾸지 못했습니다. 파일을 저장하지 않았습니다. "
                   "(internal error: a replacement could not be written safely; nothing was written)")


class DocumentError(ValueError):
    """The document cannot be processed safely. Messages never contain document text."""


def local(tag: Any) -> str:
    """Local name of an lxml tag ("{ns}t" -> "t"); "" for comments and processing instructions."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------------------------
# Zip package
# ---------------------------------------------------------------------------------------------


class ZipPackage:
    """All entries of a zip file in memory, written back in the same order."""

    def __init__(self, infos: list[zipfile.ZipInfo], data: dict[str, bytes]) -> None:
        self.infos = infos
        self.data = data

    @classmethod
    def read(cls, path: Path, open_error: str, legacy_error: str) -> "ZipPackage":
        try:
            with open(path, "rb") as f:
                head = f.read(8)
        except OSError as e:
            raise DocumentError(open_error) from e
        if head == CFB_MAGIC:
            raise DocumentError(legacy_error)
        try:
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                problem = zip_size_problem(infos)
                if problem:
                    raise DocumentError(problem)
                data: dict[str, bytes] = {}
                for info in infos:
                    if info.filename in data or info.flag_bits & 0x1:   # duplicate / encrypted entry
                        raise DocumentError(open_error)
                    data[info.filename] = z.read(info)
        except DocumentError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, KeyError, RuntimeError,
                NotImplementedError, EOFError, ValueError) as e:
            raise DocumentError(open_error) from e
        except MemoryError as e:
            raise DocumentError(ZIP_TOO_BIG_ERROR) from e
        return cls(infos, data)

    @property
    def names(self) -> list[str]:
        return [i.filename for i in self.infos if i.filename in self.data]

    def get(self, name: str) -> bytes | None:
        return self.data.get(name)

    def set(self, name: str, data: bytes) -> None:
        if name not in self.data:
            raise KeyError(name)
        self.data[name] = data

    def remove(self, name: str) -> None:
        self.data.pop(name, None)

    def to_bytes(self, stored: Iterable[str] = ()) -> bytes:
        """Zip bytes: same entry order, names, dates and compression (`stored` entries are
        written uncompressed, e.g. the HWPX 'mimetype' entry)."""
        stored = set(stored)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", allowZip64=True) as z:
            for info in self.infos:
                if info.filename not in self.data:
                    continue
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.external_attr = info.external_attr
                zi.create_system = info.create_system
                ctype = zipfile.ZIP_STORED if info.filename in stored else info.compress_type
                if ctype not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    ctype = zipfile.ZIP_DEFLATED
                zi.compress_type = ctype
                z.writestr(zi, self.data[info.filename])
        return buf.getvalue()


# ---------------------------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------------------------


def _etree() -> Any:
    try:
        from lxml import etree
    except ImportError as e:  # pragma: no cover - lxml comes with python-docx
        raise RuntimeError("lxml이 필요합니다: pip install lxml (lxml is required)") from e
    return etree


def parse_xml(data: bytes, error: str) -> Any:
    """Parse XML safely (no entities, no network); DocumentError(error) if it is not XML."""
    etree = _etree()
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True,
                             remove_blank_text=False, strip_cdata=False, load_dtd=False)
    try:
        root = etree.fromstring(data, parser)
    except (etree.XMLSyntaxError, ValueError) as e:
        raise DocumentError(error) from e
    if root is None:
        raise DocumentError(error)
    # Entities are not expanded (no XXE / billion laughs), so text written as "&p;" would
    # never reach the detector while the declaration (with the value) is written back.
    # OOXML forbids DTDs and Word refuses such files: refuse them too.
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is not None and any(True for _ in dtd.iterentities()):
        raise DocumentError(DTD_ERROR)
    return root


DTD_ERROR = ("문서 안에 XML 엔티티(DTD) 선언이 있어 안전하게 처리할 수 없습니다 "
             "(an XML part declares entities (DTD); refused)")


_DECL = re.compile(rb"^(?:\xef\xbb\xbf)?\s*<\?xml[^>]*\?>\s*")


def serialize_xml(root: Any, original: bytes) -> bytes:
    """Serialize like the original: the XML declaration (and BOM) is copied byte for byte."""
    etree = _etree()
    tree = root.getroottree()
    enc = tree.docinfo.encoding or "UTF-8"
    m = _DECL.match(original)
    if enc.replace("-", "").lower() in ("utf8", "ascii", "usascii"):
        body = etree.tostring(tree, encoding="UTF-8", xml_declaration=False)
        return (m.group() if m else b"") + body
    return etree.tostring(tree, encoding=enc, xml_declaration=bool(m))


# ---------------------------------------------------------------------------------------------
# Paragraph text model (namespace-agnostic; shared with HWPX)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TextSpec:
    """Which local element names make up paragraph text in a vocabulary."""

    para: frozenset[str] = frozenset({"p"})
    text: frozenset[str] = frozenset({"t"})
    field: frozenset[str] = frozenset({"instrText"})          # separate text (field codes)
    deleted_text: frozenset[str] = frozenset({"delText", "delInstrText"})
    deleted_containers: frozenset[str] = frozenset({"del", "moveFrom"})
    delete_begin: frozenset[str] = frozenset()                 # HWPX track-change markers
    delete_end: frozenset[str] = frozenset()
    separators: tuple[tuple[str, str], ...] = (
        ("tab", "\t"), ("ptab", "\t"), ("br", "\n"), ("cr", "\n"), ("noBreakHyphen", "-"),
    )
    skip: frozenset[str] = frozenset()
    preserve_space: bool = True                                # set xml:space on changed w:t

    def sep(self, name: str) -> str | None:
        for n, ch in self.separators:
            if n == name:
                return ch
        return None

    def is_property(self, name: str) -> bool:
        return name.endswith("Pr") or name.endswith("PrChange") or name in self.skip


DOCX_SPEC = TextSpec()


@dataclass
class _Seg:
    elem: Any          # lxml element (None: virtual separator such as a tab)
    attr: str          # "text" | "tail"
    start: int
    orig: str
    value: str

    @property
    def writable(self) -> bool:
        return self.elem is not None

    @property
    def end(self) -> int:
        return self.start + len(self.orig)


def _wordml(tag: str) -> bool:
    ns = tag[1:].split("}", 1)[0] if tag.startswith("{") else ""
    return ns.endswith("wordprocessingml/2006/main") or ns.endswith("wordprocessingml/main")


class ParaText:
    """The text of one paragraph assembled from text nodes, with run-aware write-back."""

    def __init__(self, spec: TextSpec, owner: Any = None) -> None:
        self.spec = spec
        self.owner = owner          # the paragraph element
        self.changed = False        # some text node was rewritten or wiped
        self.segs: list[_Seg] = []
        self._n = 0

    def add(self, elem: Any, attr: str, value: str) -> None:
        self.segs.append(_Seg(elem, attr, self._n, value, value))
        self._n += len(value)

    def add_virtual(self, ch: str) -> None:
        self.segs.append(_Seg(None, "", self._n, ch, ch))
        self._n += len(ch)

    @property
    def text(self) -> str:
        return "".join(s.orig for s in self.segs)

    def current(self) -> str:
        return "".join(s.value for s in self.segs)

    def has_text(self) -> bool:
        return any(s.writable and s.orig.strip() for s in self.segs)

    def apply(self, res: AnonResult) -> int:
        """Write `res` (an anonymization of self.text) back into the text nodes.

        Each replacement goes into the node where its span starts (keeping that run's
        formatting); the rest of the span is removed from the following nodes. Returns the
        number of nodes changed.
        """
        for rep in sorted(res.replacements, key=lambda r: (r.start, r.end), reverse=True):
            if rep.replacement != rep.original:
                self._replace(rep.start, rep.end, rep.replacement)
        new = self.current()
        if new != res.text:  # only virtual separators inside a span may differ
            for rep in res.replacements:
                if (rep.action != "keep" and rep.original
                        and new.count(rep.original) > res.text.count(rep.original)):
                    raise DocumentError(_INTERNAL_ERROR)
        return self.commit()

    def _replace(self, start: int, end: int, new: str) -> None:
        hit = [g for g in self.segs if g.writable and g.start < end and g.end > start]
        for i, g in enumerate(hit):
            ls = max(start, g.start) - g.start
            le = min(end, g.end) - g.start
            g.value = g.value[:ls] + (new if i == 0 else "") + g.value[le:]

    def commit(self) -> int:
        n = 0
        for g in self.segs:
            if g.writable and g.value != g.orig:
                setattr(g.elem, g.attr, g.value or None)
                if (g.attr == "text" and self.spec.preserve_space and local(g.elem.tag) == "t"
                        and _wordml(g.elem.tag)):
                    g.elem.set(XML_SPACE, "preserve")
                g.orig = g.value
                n += 1
        if n:
            self.changed = True
        return n


@dataclass
class _WalkState:
    main: ParaText
    fields: ParaText
    marked: bool = False          # inside a deleteBegin ... deleteEnd range
    first: str | None = None
    retro: int | None = None      # a deleteEnd came first: everything before it was deleted

    def marker(self, begin: bool) -> None:
        if self.first is None:
            self.first = "begin" if begin else "end"
            if not begin:
                self.retro = len(self.main.segs)
        self.marked = begin


class ParagraphWalker:
    """Collect paragraph texts of XML trees; deleted (tracked) text is wiped on the way."""

    def __init__(self, spec: TextSpec) -> None:
        self.spec = spec
        self.processed: set[tuple[Any, str]] = set()
        self._alive: list[Any] = []       # keeps lxml proxies (and so their identity) alive
        self.cleared = 0

    def paragraphs(self, root: Any) -> list[tuple[ParaText, ParaText]]:
        """(text, field-code text) of every paragraph in document order (nested paragraphs -
        table cells, text boxes - are separate entries)."""
        return [self.paragraph(el) for el in root.iter() if local(el.tag) in self.spec.para]

    def paragraph(self, p: Any) -> tuple[ParaText, ParaText]:
        st = _WalkState(ParaText(self.spec, p), ParaText(self.spec, p))
        before = self.cleared
        self._children(p, st, False)
        if st.retro:
            old, st.main = st.main.segs, ParaText(self.spec, p)
            for i, g in enumerate(old):
                if i < st.retro:
                    if g.writable:
                        setattr(g.elem, g.attr, None)
                        self.cleared += 1
                elif g.writable:
                    st.main.add(g.elem, g.attr, g.orig)
                else:
                    st.main.add_virtual(g.orig)
        if self.cleared != before:
            st.main.changed = True
        return st.main, st.fields

    def _children(self, el: Any, st: _WalkState, deleted: bool) -> None:
        spec = self.spec
        for child in el:
            name = local(child.tag)
            if not name or name in spec.para or spec.is_property(name):
                continue
            if name in spec.text:
                self._text(child, st, deleted)
            elif name in spec.field:
                self._piece(child, "text", st, deleted, st.fields)
            elif name in spec.deleted_text:
                self._piece(child, "text", st, True)
            elif name in spec.delete_begin:
                st.marker(True)
            elif name in spec.delete_end:
                st.marker(False)
            else:
                sep = spec.sep(name)
                if sep is not None:
                    if not (deleted or st.marked):
                        st.main.add_virtual(sep)
                else:
                    self._children(child, st, deleted or name in spec.deleted_containers)

    def _text(self, t: Any, st: _WalkState, deleted: bool) -> None:
        spec = self.spec
        self._piece(t, "text", st, deleted)
        for sub in t:
            name = local(sub.tag)
            if name in spec.delete_begin:
                st.marker(True)
            elif name in spec.delete_end:
                st.marker(False)
            elif name:
                sep = spec.sep(name)
                if sep is not None and not (deleted or st.marked):
                    st.main.add_virtual(sep)
                if sub.text:
                    self._piece(sub, "text", st, deleted)
            self._piece(sub, "tail", st, deleted)

    def _piece(self, el: Any, attr: str, st: _WalkState, deleted: bool,
               target: ParaText | None = None) -> None:
        value = getattr(el, attr)
        if not value:
            return
        self.processed.add((el, attr))
        self._alive.append(el)
        if deleted or st.marked:
            setattr(el, attr, None)
            self.cleared += 1
            return
        (target if target is not None else st.main).add(el, attr, value)


# ---------------------------------------------------------------------------------------------
# Form-style tables: "성명 | 홍길동" rows and header rows type the value cells
# ---------------------------------------------------------------------------------------------

LABEL_MAX_CHARS = 16


def _nearest(el: Any, name: str) -> Any:
    for anc in el.iterancestors():
        if local(anc.tag) == name:
            return anc
    return None


def _cell_paragraphs(tc: Any, spec: TextSpec) -> list[Any]:
    return [p for p in tc.iter() if local(p.tag) in spec.para and _nearest(p, "tc") is tc]


def _cell_text(tc: Any, spec: TextSpec) -> str:
    parts = []
    for p in _cell_paragraphs(tc, spec):
        parts.append("".join("".join(t.itertext()) for t in p.iter()
                             if local(t.tag) in spec.text and _nearest(t, "p") is p))
    return " ".join(x for x in parts if x).strip()


def _label_entity(text: str) -> str | None:
    if not text or len(text) > LABEL_MAX_CHARS:
        return None
    try:
        from .tabular import column_entity
    except ImportError:  # pragma: no cover
        return None
    return column_entity(text.rstrip(" :：").strip())


def table_types(root: Any, spec: TextSpec) -> dict[Any, str]:
    """Paragraph element -> entity for cells of form-style tables.

    A cell right of a label cell ("성명", "연락처", "주민등록번호" ...) is typed with the label's
    entity; in a table whose first row holds two or more such labels, the cells below are typed
    by column. Typed values are registered as known values, so they are masked everywhere.
    """
    out: dict[Any, str] = {}
    for tbl in root.iter():
        if local(tbl.tag) != "tbl":
            continue
        rows = [tr for tr in tbl.iter() if local(tr.tag) == "tr" and _nearest(tr, "tbl") is tbl]
        header: dict[int, str] = {}
        for ri, tr in enumerate(rows):
            cells = [tc for tc in tr.iter() if local(tc.tag) == "tc" and _nearest(tc, "tr") is tr]
            texts = [_cell_text(tc, spec) for tc in cells]
            labels = [_label_entity(t) for t in texts]
            if ri == 0 and sum(1 for x in labels if x) >= 2:
                header = {i: ent for i, ent in enumerate(labels) if ent}
                continue
            typed: dict[int, str] = {}
            for i, ent in header.items():
                if i < len(cells) and texts[i] and not labels[i]:
                    typed[i] = ent
            for i in range(len(cells) - 1):
                if labels[i] and texts[i + 1] and not labels[i + 1]:
                    typed.setdefault(i + 1, labels[i])
            for i, ent in typed.items():
                for p in _cell_paragraphs(cells[i], spec):
                    out[p] = ent
    return out


# ---------------------------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------------------------

_JPEG_KEEP_APP = (0xE0, 0xE2, 0xEE)   # JFIF, ICC profile (checked below), Adobe (colour transform)


def _strip_jpeg(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        return data
    out = bytearray(b"\xff\xd8")
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return data            # not a marker where one should be: leave the file alone
        marker = data[i + 1]
        if marker == 0xFF:         # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > n:
            return data
        seg = data[i:i + 2 + length]
        if marker == 0xDA:         # start of scan: the rest is image data
            out += data[i:]
            return bytes(out)
        drop = (0xE1 <= marker <= 0xEF and marker not in _JPEG_KEEP_APP) or marker == 0xFE
        if marker == 0xE2 and not seg[4:16].startswith(b"ICC_PROFILE"):
            drop = True
        if not drop:
            out += seg
        i += 2 + length
    return data


_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_DROP = (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME")


def _strip_png(data: bytes) -> bytes:
    if not data.startswith(_PNG_SIG):
        return data
    out = bytearray(_PNG_SIG)
    i = len(_PNG_SIG)
    n = len(data)
    while i + 12 <= n:
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            return data
        if ctype not in _PNG_DROP:
            out += data[i:end]
        i = end
        if ctype == b"IEND":
            return bytes(out)
    return data


def strip_image_metadata(data: bytes, ext: str) -> bytes:
    """Remove EXIF/GPS/XMP/comments (JPEG) and text/EXIF chunks (PNG) without re-encoding."""
    ext = ext.lower()
    if ext in (".jpg", ".jpeg", ".jpe") or data.startswith(b"\xff\xd8"):
        return _strip_jpeg(data)
    if ext == ".png" or data.startswith(_PNG_SIG):
        return _strip_png(data)
    return data


_PIL_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".gif": "GIF",
                ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF", ".webp": "WEBP"}


def encode_image(img: Any, ext: str, fallback_format: str | None = None) -> bytes:
    """Encode a PIL image in the format of `ext` (pixels only, no metadata)."""
    fmt = _PIL_FORMATS.get(ext.lower()) or fallback_format or "PNG"
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if fmt in ("BMP",) and img.mode not in ("RGB", "L", "P", "1"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    kwargs: dict[str, Any] = {"quality": 95} if fmt in ("JPEG", "WEBP") else {}
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def blank_image_like(data: bytes, ext: str) -> bytes | None:
    """A white image with the size and format of `data`, or None if it cannot be decoded."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            size, fmt = im.size, im.format
    except Exception:  # noqa: BLE001 - unknown/vector format
        return None
    from PIL import Image

    blank = Image.new("RGB", size, (255, 255, 255))
    try:
        return encode_image(blank, ext, fmt)
    except Exception:  # noqa: BLE001
        return encode_image(blank, ".png")


# ---------------------------------------------------------------------------------------------
# Vector pictures: EMF / WMF (+ gzip'd EMZ / WMZ). Word, Excel and Hangul store "paste as
# picture" of a table or EMR screen, and the visible preview of an embedded object, as EMF; the
# drawn text is stored as text records. We read that text (it is checked with the document) and
# can replace a picture by an empty one with the same frame (same size on the page).
# ---------------------------------------------------------------------------------------------

_EMF_SIGNATURE = 0x464D4520                     # " EMF"
_EMFPLUS = 0x2B464D45                           # "EMF+" GDI comment
_EMR_BITMAP = frozenset({0x4C, 0x4D, 0x4E, 0x4F, 0x50, 0x51, 0x72, 0x74})
_WMF_PLACEABLE = b"\xd7\xcd\xc6\x9a"
_WMF_BITMAP = frozenset({0x0922, 0x0B23, 0x0940, 0x0B41, 0x0D33, 0x0F43})
_VECTOR_MAX = 64 * 1024 * 1024                  # unpacked EMZ/WMZ limit
_MAX_STRINGS = 100_000


def _printable_runs(s: str) -> list[str]:
    """Readable pieces of a decoded string (control characters split it)."""
    out = []
    for run in re.split(r"[\x00-\x1f\x7f\ufffd\ud800-\udfff]+", s):
        run = run.strip()
        if len(run) >= 2 and any(ch.isalnum() for ch in run):
            out.append(run)
    return out


def _utf16_scan(buf: bytes) -> list[str]:
    return _printable_runs(buf[: len(buf) // 2 * 2].decode("utf-16-le", "replace"))


def _decode(raw: bytes, wide: bool) -> str:
    return raw.decode("utf-16-le", "replace") if wide else raw.decode("cp949", "replace")


def _emr_text(rec: bytes, off: int, wide: bool, fixed_end: int) -> list[str]:
    """An EMRTEXT structure at `off` of an EMF record; when its declared string is missing
    or out of range, the bytes after the fixed part are scanned instead."""
    if off + 16 <= len(rec):
        chars, off_string = struct.unpack_from("<II", rec, off + 8)
        width = 2 if wide else 1
        if chars and 8 <= off_string and off_string + chars * width <= len(rec):
            return [_decode(rec[off_string:off_string + chars * width], wide)]
    tail = rec[fixed_end:]
    if not tail:
        return []
    return _utf16_scan(tail) if wide else _printable_runs(tail.decode("cp949", "replace"))


def _emfplus_texts(buf: bytes) -> list[str]:
    out: list[str] = []
    pos = 0
    while pos + 12 <= len(buf):
        rtype, _flags, size, dsize = struct.unpack_from("<HHII", buf, pos)
        if size < 12 or pos + size > len(buf):
            return out + _utf16_scan(buf[pos:])
        data = buf[pos + 12:pos + 12 + min(dsize, size - 12)]
        if rtype == 0x401C and len(data) >= 28:              # EmfPlusDrawString
            n = struct.unpack_from("<I", data, 8)[0]
            out.append(data[28:28 + 2 * n].decode("utf-16-le", "replace"))
        elif rtype == 0x4036 and len(data) >= 16:            # EmfPlusDrawDriverString
            opts, _m, n = struct.unpack_from("<III", data, 4)
            if opts & 1:                                     # glyphs are characters
                out.append(data[16:16 + 2 * n].decode("utf-16-le", "replace"))
        pos += size
    return out


def _emf_texts(data: bytes) -> tuple[list[str], bool] | None:
    n = len(data)
    if n < 88:
        return None
    rtype, size = struct.unpack_from("<II", data, 0)
    if rtype != 1 or struct.unpack_from("<I", data, 40)[0] != _EMF_SIGNATURE:
        return None
    texts: list[str] = []
    bitmap = False
    pos = 0
    while pos + 8 <= n:
        rtype, size = struct.unpack_from("<II", data, pos)
        if size < 8 or pos + size > n:
            return None
        rec = data[pos:pos + size]
        if rtype == 1 and size >= 72:                        # header: description string
            nd, od = struct.unpack_from("<II", rec, 60)
            if nd and od + 2 * nd <= size:
                texts += _printable_runs(rec[od:od + 2 * nd].decode("utf-16-le", "replace"))
        elif rtype in (0x53, 0x54):                          # EXTTEXTOUTA / W
            texts += _emr_text(rec, 36, rtype == 0x54, 76)
        elif rtype in (0x5F, 0x60) and size >= 40:           # POLYTEXTOUTA / W
            count = min(struct.unpack_from("<I", rec, 36)[0], _MAX_STRINGS)
            for i in range(count):
                if 40 + 40 * (i + 1) > size:
                    break
                texts += _emr_text(rec, 40 + 40 * i, rtype == 0x60, 40 + 40 * count)
        elif rtype == 0x6C and size >= 36:                   # SMALLTEXTOUT
            chars, opts = struct.unpack_from("<II", rec, 16)
            start = 36 if opts & 0x100 else 52
            wide = not opts & 0x200
            texts.append(_decode(rec[start:start + chars * (2 if wide else 1)], wide))
        elif rtype == 0x46 and size >= 16:                   # GDI comment (EMF+ or app data)
            dsize = struct.unpack_from("<I", rec, 8)[0]
            payload = rec[12:12 + dsize]
            if len(payload) >= 4 and struct.unpack_from("<I", payload, 0)[0] == _EMFPLUS:
                texts += _emfplus_texts(payload[4:])
            else:
                texts += _utf16_scan(payload)
        elif rtype in _EMR_BITMAP:
            bitmap = True
        pos += size
        if rtype == 0x0E:                                    # EOF
            break
        if len(texts) > _MAX_STRINGS:
            break
    return texts, bitmap


def _wmf_start(data: bytes) -> int | None:
    pos = 22 if data[:4] == _WMF_PLACEABLE else 0
    if pos + 18 > len(data):
        return None
    typ, hsize = struct.unpack_from("<HH", data, pos)
    return pos if typ in (1, 2) and hsize == 9 else None


def _wmf_texts(data: bytes) -> tuple[list[str], bool] | None:
    start = _wmf_start(data)
    if start is None:
        return None
    texts: list[str] = []
    bitmap = False
    pos, n = start + 18, len(data)
    while pos + 6 <= n:
        words, func = struct.unpack_from("<IH", data, pos)
        size = words * 2
        if size < 6 or pos + size > n:
            return None
        rec = data[pos:pos + size]
        if func == 0x0521 and size >= 8:                     # TEXTOUT
            length = struct.unpack_from("<H", rec, 6)[0]
            texts.append(rec[8:8 + length].decode("cp949", "replace"))
        elif func == 0x0A32 and size >= 14:                  # EXTTEXTOUT
            length, opts = struct.unpack_from("<HH", rec, 10)
            at = 14 + (8 if opts & 0x0006 else 0)
            texts.append(rec[at:at + length].decode("cp949", "replace"))
        elif func == 0x0626 and size >= 10:                  # ESCAPE (comments, embedded EMF)
            texts += _utf16_scan(rec[10:])
        elif func in _WMF_BITMAP:
            bitmap = True
        if func == 0:
            break
        pos += size
        if len(texts) > _MAX_STRINGS:
            break
    return texts, bitmap


def _gunzip(data: bytes) -> bytes | None:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            out = g.read(_VECTOR_MAX + 1)
    except (OSError, EOFError, ValueError):
        return None
    return out if len(out) <= _VECTOR_MAX else None


def vector_texts(data: bytes, ext: str) -> tuple[list[str], bool] | None:
    """(texts drawn by an EMF/WMF/EMZ/WMZ picture, whether it also holds bitmaps), or None
    when the picture cannot be read."""
    ext = ext.lower()
    if ext in (".emz", ".wmz"):
        inner = _gunzip(data)
        return None if inner is None else vector_texts(inner, ".emf" if ext == ".emz" else ".wmf")
    if data[:4] == b"\x01\x00\x00\x00" and ext in (".emf", ".wmf"):
        return _emf_texts(data)
    if ext in (".emf", ".wmf"):
        return _wmf_texts(data)
    return None


_EMF_EOF = struct.pack("<IIIII", 0x0E, 20, 0, 16, 20)


def _blank_emf(data: bytes) -> bytes | None:
    if len(data) < 88:
        return None
    rtype, size = struct.unpack_from("<II", data, 0)
    if rtype != 1 or size < 88 or size > len(data) or size % 4:
        return None
    head = bytearray(data[:size])
    nd, od = struct.unpack_from("<II", head, 60)
    if nd and 88 <= od <= size:
        end = min(size, od + 2 * nd)
        head[od:end] = b"\0" * (end - od)          # description (application / picture name)
    struct.pack_into("<II", head, 60, 0, 0)
    struct.pack_into("<I", head, 68, 0)             # no palette
    struct.pack_into("<IIH", head, 48, len(head) + len(_EMF_EOF), 2, 1)
    return bytes(head) + _EMF_EOF


def _blank_wmf(data: bytes) -> bytes | None:
    start = _wmf_start(data)
    if start is None:
        return None
    head = data[:start]
    meta = bytearray(data[start:start + 18])
    eof = struct.pack("<IH", 3, 0)
    struct.pack_into("<IH", meta, 6, (18 + len(eof)) // 2, 0)   # size (words), no objects
    struct.pack_into("<IH", meta, 12, 3, 0)                      # largest record, members
    return head + bytes(meta) + eof


# Smallest valid pictures, used when the original cannot be rebuilt.
_MIN_EMF = (struct.pack("<II", 1, 88) + struct.pack("<4i", 0, 0, 1, 1)
            + struct.pack("<4i", 0, 0, 100, 100) + struct.pack("<II", _EMF_SIGNATURE, 0x10000)
            + struct.pack("<IIHHIII", 88 + 20, 2, 1, 0, 0, 0, 0)
            + struct.pack("<4i", 1024, 768, 320, 240))
_MIN_EMF = _MIN_EMF + _EMF_EOF
_MIN_WMF = (struct.pack("<HHHIHIH", 1, 9, 0x300, 12, 0, 3, 0) + struct.pack("<IH", 3, 0))


def blank_vector(data: bytes, ext: str) -> bytes:
    """An empty EMF/WMF (EMZ/WMZ) picture with the frame of `data` (or a minimal one)."""
    ext = ext.lower()
    if ext in (".emz", ".wmz"):
        inner = _gunzip(data) or b""
        return gzip.compress(blank_vector(inner, ".emf" if ext == ".emz" else ".wmf"), mtime=0)
    if data[:4] == b"\x01\x00\x00\x00":
        return _blank_emf(data) or _MIN_EMF
    return _blank_wmf(data) or (_MIN_EMF if ext == ".emf" else _MIN_WMF)


def image_ocr_mode(value: Any) -> str:
    """[ocr] docx_images / hwpx_images: "auto" (default) | "all" (true) | "off" (false)."""
    if value is None:
        return "auto"
    if isinstance(value, bool):
        return "all" if value else "off"
    v = str(value).strip().lower()
    if v in ("all", "true", "yes", "on", "1", "always"):
        return "all"
    if v in ("auto", ""):
        return "auto"
    return "off"


_FILE_LIKE = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/]|file:)?[^\r\n<>|"*?]*\.([A-Za-z0-9]{2,5})""")


_GENERIC_FILE = re.compile(r"(?i:image|picture|img|pic|file|그림|사진|이미지)[ _\-]?\d*\.[A-Za-z0-9]{2,5}")


def file_name_like(value: Any) -> bool:
    """'CT_홍길동_850315.png', 'C:\\Users\\me\\20240301 홍길동 상처.jpg' (a whole value)."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    m = _FILE_LIKE.fullmatch(v)
    if not m or len(v) > 400:
        return False
    return "." + m.group(1).lower() in FILE_NAME_EXTS or bool(re.search(r"[\\/]", v))


# ---------------------------------------------------------------------------------------------
# Shared job: collect slots in XML parts, detect once, write back, safety net
# ---------------------------------------------------------------------------------------------


def neutral_file_name(path: str) -> str:
    """'C:\\Users\\hong\\홍길동_검사.pdf' -> 'file.pdf' (only the extension survives)."""
    base = unquote(re.split(r"[\\/]", path.rstrip("\\/"))[-1])
    ext = posixpath.splitext(base)[1]
    return "file" + ext if re.fullmatch(r"\.[A-Za-z0-9]{1,6}", ext) else "file"


def name_like(value: Any) -> str | None:
    """A metadata author value that may be a person's name (not 'Microsoft Office User')."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not (2 <= len(v) <= 40) or v.casefold() in _GENERIC_AUTHORS:
        return None
    if any(ch.isdigit() for ch in v) or "@" in v or "\\" in v or "/" in v:
        return None
    return v


def _apply_to(original: str, res: AnonResult) -> str:
    """Apply the replacements of `res` (an anonymization of a same-length variant of
    `original`, e.g. with "_" read as " ") to `original`."""
    parts, pos = [], 0
    for r in sorted(res.replacements, key=lambda r: r.start):
        if r.replacement == r.original or r.start < pos:
            continue
        parts += (original[pos:r.start], r.replacement)
        pos = r.end
    parts.append(original[pos:])
    return "".join(parts)


def _ocr_worthy(data: bytes) -> bool:
    """[ocr] *_images = "auto": OCR pictures big enough to be a screenshot or a photo of a
    document; icons, bullets and small logos are skipped (and counted as not checked)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001 - undecodable: let the OCR step report it
        return True
    return min(w, h) >= AUTO_OCR_MIN_SIDE and w * h >= AUTO_OCR_PIXELS


class PackageJob:
    """Base for zip+XML document handlers (DOCX, HWPX)."""

    open_error = _OPEN_ERROR

    def __init__(self, pkg: ZipPackage, detector: "Detector", anonymizer: "Anonymizer",
                 cfg: "Config", report: Report, spec: TextSpec) -> None:
        self.pkg = pkg
        self.detector = detector
        self.anonymizer = anonymizer
        self.cfg = cfg
        self.report = report
        self.batch = Batch(detector, anonymizer, report)
        self.walker = ParagraphWalker(spec)
        self.trees: dict[str, Any] = {}
        self.dirty: set[str] = set()
        self.paras: list[tuple[str, ParaText]] = []
        self.mapping: dict[str, str] = {}      # removed original value -> replacement
        self.handled: set[tuple[Any, str]] = set()
        self._alive: list[Any] = []
        self.notes: dict[str, int] = {}
        self.vector_state: dict[str, str] = {}   # picture part -> "ok" | "bitmap" | "unreadable"
        self.vector_blank: set[str] = set()      # EMF/WMF parts that showed personal data

    # -- bookkeeping -------------------------------------------------------------------------

    def note(self, key: str, n: int = 1) -> None:
        self.notes[key] = self.notes.get(key, 0) + n

    def tree(self, name: str) -> Any:
        root = self.trees.get(name)
        if root is None:
            data = self.pkg.get(name)
            if data is None:
                raise DocumentError(self.open_error)
            root = parse_xml(data, self.open_error)
            self.trees[name] = root
        return root

    def known_person(self, value: Any) -> None:
        v = name_like(value)
        if v:
            self.batch.add_known(E.PERSON, v)

    # -- slots -------------------------------------------------------------------------------

    def slot(self, text: str, setter: Callable[[AnonResult], None], entity: str | None = None) -> None:
        def wrapped(res: AnonResult) -> None:
            for r in res.replacements:
                if r.action != "keep" and r.original.strip() and r.replacement != r.original:
                    self.mapping.setdefault(r.original, r.replacement)
            setter(res)
        self.batch.add(text, wrapped, entity)

    def add_paragraphs(self, name: str) -> None:
        root = self.tree(name)
        before = self.walker.cleared
        types = table_types(root, self.walker.spec)
        for main, fields in self.walker.paragraphs(root):
            entity = types.get(main.owner)
            for pt, ent in ((main, entity), (fields, None)):
                if pt.has_text():
                    self.slot(pt.text, lambda res, pt=pt, n=name: self._apply_para(n, pt, res), ent)
            self.paras.append((name, main))
        if self.walker.cleared != before:
            self.note("deleted_text", self.walker.cleared - before)
            self.dirty.add(name)

    def _apply_para(self, name: str, pt: ParaText, res: AnonResult) -> None:
        if pt.apply(res):
            self.dirty.add(name)

    def attr_slot(self, name: str, el: Any, key: str, underscores: bool = False) -> None:
        """Check an attribute value. `underscores`: identifiers such as bookmark names
        ("환자_홍길동_0101234") are detected with "_" read as a space."""
        value = el.get(key)
        if not value or not value.strip():
            return
        self._alive.append(el)
        if file_name_like(value):     # an original picture file name / path
            new = value if _GENERIC_FILE.fullmatch(value.strip()) else neutral_file_name(value)
            if new != value:
                el.set(key, new)
                self.dirty.add(name)
                self.note("file_name")
            return

        def set_attr(res: AnonResult, el: Any = el, key: str = key) -> None:
            if underscores:
                el.set(key, _apply_to(value, res))
            else:
                el.set(key, res.text)
            self.dirty.add(name)
        self.slot(value.replace("_", " ") if underscores else value, set_attr)

    def element_slot(self, name: str, el: Any, entity: str | None = None,
                     attr: str = "text") -> None:
        value = getattr(el, attr)
        if not value or not value.strip():
            return
        self.handled.add((el, attr))
        self._alive.append(el)

        def set_text(res: AnonResult, el: Any = el) -> None:
            setattr(el, attr, res.text)
            self.dirty.add(name)
        self.slot(value, set_text, entity)

    def text_attr_slots(self, name: str) -> None:
        """Human-readable attributes: alt text, titles, watermark text, tooltips, field codes."""
        for el in self.tree(name).iter():
            owner = local(el.tag)
            if not owner:
                continue
            for key in list(el.attrib):
                lk = local(key)
                if lk in TEXT_ATTRS or (lk == "name" and owner in NAME_ATTR_OWNERS):
                    self.attr_slot(name, el, key)

    def leaf_slots(self, name: str) -> None:
        """Any other element text with letters (unknown structures, equation scripts ...) is
        checked too. Call after the paragraphs and special slots of the part."""
        processed = self.walker.processed | self.handled
        for el in self.tree(name).iter():
            if (local(el.tag) and len(el) == 0 and el.text and (el, "text") not in processed
                    and any(ch.isalpha() for ch in el.text)):
                self.element_slot(name, el)

    # -- finishing ---------------------------------------------------------------------------

    def residual(self) -> int:
        """Replace removed values left in text nodes or attribute values no slot covered
        (unknown structures, content-control tags, bookmark-like names)."""
        values = sorted({v for v in self.mapping if len(v.strip()) >= 2}, key=len, reverse=True)
        if not values:
            return 0
        rx = re.compile("|".join(map(re.escape, values)))
        n = 0
        processed = self.walker.processed | self.handled
        for name, root in self.trees.items():
            for el in root.iter():
                for attr in ("text", "tail"):
                    value = getattr(el, attr)
                    if not value or (el, attr) in processed:
                        continue
                    if not any(ch.isalpha() for ch in value):
                        continue   # numbers-only nodes are layout values, never touched
                    new, k = rx.subn(lambda m: self.mapping[m.group()], value)
                    if k:
                        setattr(el, attr, new)
                        n += k
                        self.dirty.add(name)
                if not isinstance(el.tag, str):
                    continue   # comments / processing instructions have no attributes
                for key, value in list(el.attrib.items()):
                    if not value or local(key) in _RESIDUAL_SKIP_ATTRS:
                        continue
                    if not any(ch.isalpha() for ch in value):
                        continue
                    new, k = rx.subn(lambda m: self.mapping[m.group()], value)
                    if k:
                        el.set(key, new)
                        n += k
                        self.dirty.add(name)
        return n

    def save_trees(self) -> None:
        for name in self.dirty:
            root = self.trees.get(name)
            if root is not None:
                self.pkg.set(name, serialize_xml(root, self.pkg.get(name) or b""))

    # -- pictures ------------------------------------------------------------------------------

    def collect_vectors(self, names: Iterable[str]) -> None:
        """Text of vector pictures becomes slots of the document's batch (call before the
        batch runs): an EMF/WMF whose text holds personal data is blanked by image_parts,
        SVG text nodes are rewritten like any XML part."""
        for name in names:
            ext = posixpath.splitext(name)[1].lower()
            if ext not in VECTOR_EXTS:
                continue
            data = self.pkg.get(name) or b""
            if ext == ".svg":
                self.vector_state[name] = self._svg_slots(name)
                continue
            found = vector_texts(data, ext)
            if found is None:
                self.vector_state[name] = "unreadable"
                continue
            texts, bitmap = found
            text = "\n".join(t.strip() for t in texts if t and t.strip())
            if text:
                self.slot(text, lambda res, n=name: self.vector_blank.add(n))
            self.vector_state[name] = "bitmap" if bitmap else "ok"

    def _svg_slots(self, name: str) -> str:
        try:
            root = self.tree(name)
        except DocumentError:
            return "unreadable"
        state = "ok"
        for el in root.iter():
            lname = local(el.tag)
            if not lname:
                continue
            for attr in ("text", "tail"):
                value = getattr(el, attr)
                if value and any(ch.isalnum() for ch in value):
                    self.element_slot(name, el, attr=attr)
            for key in list(el.attrib):
                lk = local(key)
                if lk in ("aria-label", "title", "alt", "label", "aria-description"):
                    self.attr_slot(name, el, key)
                elif lk == "href" and (el.get(key) or "").lstrip()[:11].lower() == "data:image/":
                    state = "bitmap"      # an embedded raster image cannot be checked here
        return state

    def image_parts(self, names: Iterable[str], mode: Any) -> None:
        """Strip image metadata, blank vector pictures that showed personal data and OCR
        raster images (mode "auto" | "all" | "off", see image_ocr_mode)."""
        if mode not in ("auto", "all", "off"):
            mode = image_ocr_mode(mode)
        unchecked = unreadable = no_ocr = 0
        for name in names:
            ext = posixpath.splitext(name)[1].lower()
            data = self.pkg.get(name) or b""
            if ext not in IMAGE_EXTS:
                continue
            if ext in VECTOR_EXTS:
                if name in self.vector_blank:
                    self.pkg.set(name, blank_vector(data, ext))
                    self.note("vector_blanked")
                elif self.vector_state.get(name) != "ok":
                    unreadable += 1
                continue
            if ext not in RASTER_EXTS:
                unreadable += 1
            elif mode == "off" or (mode == "auto" and not _ocr_worthy(data)):
                unchecked += 1
            else:
                from ..ocr import OcrError
                try:
                    new = self._ocr_image(data, ext)
                except OcrError:
                    if mode == "all":     # OCR asked for explicitly: fail closed
                        raise
                    no_ocr += 1           # default "auto": warn instead of refusing the file
                else:
                    if new is None:
                        unreadable += 1
                    else:
                        data = new
            self.pkg.set(name, strip_image_metadata(data, ext))
        if unchecked:
            self.note("image_unchecked", unchecked)
        if unreadable:
            self.note("image_unreadable", unreadable)
        if no_ocr:
            self.note("image_no_ocr", no_ocr)

    def _ocr_image(self, data: bytes, ext: str) -> bytes | None:
        from PIL import Image, ImageOps

        from .image import check_pixels, redact_image

        try:
            with Image.open(io.BytesIO(data)) as im:
                check_pixels(im.size, self.cfg)      # before decoding: pixel bombs
                im.load()
                fmt = im.format
                img = ImageOps.exif_transpose(im.copy())
        except Exception:  # noqa: BLE001 - undecodable or too large: counted as unchecked
            return None
        out, rep = redact_image(img, self.detector, self.cfg, self.anonymizer)
        rep.warnings = [f"문서 안 이미지 / embedded image: {w}" for w in rep.warnings]
        self.report.merge(rep)
        if any(self.cfg.action_for(ent) != "keep" for ent in rep.counts):
            self.note("image_redacted")
            return encode_image(out, ext, fmt)
        return data

    def common_warnings(self, image_option_name: str) -> None:
        w, n = self.report.warnings, self.notes
        if n.get("image_unchecked"):
            k = n["image_unchecked"]
            w.append(f"문서 안 이미지 {k}개는 검사하지 않음 — 그림 속 개인정보는 직접 확인하세요 "
                     f"({k} image(s) inside the document were NOT checked; set [ocr] "
                     f"{image_option_name} = true to OCR them)")
        if n.get("image_no_ocr"):
            k = n["image_no_ocr"]
            w.append(f"OCR 엔진이 없어 문서 안 이미지 {k}개를 검사하지 못했습니다 — 'deid doctor'로 설치 "
                     f"상태를 확인하고, 그림 속 개인정보는 직접 확인하세요 ({k} image(s) were NOT "
                     f"checked: no working OCR engine; run 'deid doctor')")
        if n.get("image_unreadable"):
            k = n["image_unreadable"]
            w.append(f"문서 안 그림 {k}개는 형식 때문에 확인할 수 없었습니다 (그림 속 이미지·알 수 없는 "
                     f"형식) — 직접 확인하세요 ({k} picture(s) could not be checked: unsupported or "
                     f"undecodable picture format, or a bitmap inside a vector picture; check "
                     f"them yourself)")
        if n.get("vector_blanked"):
            k = n["vector_blanked"]
            w.append(f"개인정보가 보이는 벡터 그림(EMF/WMF) {k}개를 빈 그림으로 바꿨습니다 "
                     f"({k} vector picture(s) showing personal data were replaced by an empty "
                     f"picture)")
        if n.get("file_name"):
            k = n["file_name"]
            w.append(f"그림/개체에 저장된 원본 파일 이름 {k}개를 'file.확장자'로 바꿨습니다 "
                     f"({k} original file name(s) of pictures/objects were neutralised)")
        if n.get("embedded"):
            k = n["embedded"]
            w.append(f"문서에 포함된 개체(엑셀 표, OLE 개체 등) {k}개는 검사할 수 없어 내용을 비웠습니다 "
                     f"({k} embedded object(s) could not be checked and were emptied)")
        if n.get("deleted_text"):
            w.append(f"변경 내용 추적에 남아 있던 삭제된 글자 {n['deleted_text']}곳을 지웠습니다 "
                     f"(deleted tracked-change text removed in {n['deleted_text']} place(s))")
        if n.get("residual"):
            w.append(f"추가 검사에서 개인정보 {n['residual']}곳을 더 가렸습니다 "
                     f"(safety net: {n['residual']} more occurrence(s) replaced)")
        if n.get("link"):
            w.append(f"메일/전화/파일 링크 {n['link']}개의 주소를 가렸습니다 "
                     f"({n['link']} mailto:/tel:/file link target(s) anonymized)")


# ---------------------------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------------------------


def _part_order(name: str) -> tuple[int, str]:
    base = posixpath.basename(name)
    for i, prefix in enumerate(("header", "footer", "footnotes", "endnotes", "comments")):
        if base.startswith(prefix):
            return (i + 1, name)
    return (9, name)


def _rels_source_dir(rels_name: str) -> str:
    # word/_rels/document.xml.rels -> word ; _rels/.rels -> ""
    return posixpath.dirname(posixpath.dirname(rels_name))


class _DocxJob(PackageJob):

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.main = "word/document.xml"
        self.folder = "word"

    def run(self) -> None:
        self._find_main()
        self._collect_people()
        text_parts = sorted((n for n in self.pkg.names
                             if n.startswith(self.folder + "/") and n.endswith(".xml")
                             and "/_rels/" not in n and n != self.main), key=_part_order)
        for name in [self.main, *text_parts]:
            self.add_paragraphs(name)
            self.text_attr_slots(name)
            self.leaf_slots(name)          # also chart caches (c:v), unknown structures
        for name in self.pkg.names:
            if re.fullmatch(r"customXml/item\d*\.xml", name):
                self._custom_xml(name)
        self._collect_rels()
        self.collect_vectors(self._media())
        self.batch.run()
        self._scrub_authors()
        self._scrub_props()
        self._scrub_settings()
        n = self.residual()
        if n:
            self.note("residual", n)
        self.save_trees()
        self._binary_parts()
        self._warnings()

    # -- structure ---------------------------------------------------------------------------

    def _find_main(self) -> None:
        names = set(self.pkg.names)
        if "_rels/.rels" in names:
            for rel in self.tree("_rels/.rels"):
                if (rel.get("Type") or "").endswith("/officeDocument"):
                    target = (rel.get("Target") or "").lstrip("/")
                    if target in names:
                        self.main = target
        if self.main not in names or "[Content_Types].xml" not in names:
            raise DocumentError(_OPEN_ERROR)
        self.folder = posixpath.dirname(self.main) or "word"

    def _collect_people(self) -> None:
        names = set(self.pkg.names)
        if "docProps/core.xml" in names:
            for el in self.tree("docProps/core.xml"):
                if local(el.tag) in ("creator", "lastModifiedBy"):
                    self.known_person(el.text)
        for name in self.pkg.names:
            if not (name.startswith(self.folder + "/") and name.endswith(".xml")):
                continue
            data = self.pkg.get(name) or b""
            if b"author" not in data:
                continue
            for el in self.tree(name).iter():
                for key, value in el.attrib.items():
                    if local(key) == "author":
                        self.known_person(value)

    def _custom_xml(self, name: str) -> None:
        """Custom XML data (content-control bindings, SharePoint/OneDrive library columns in
        <documentManagement>): a leaf value is typed by its element name like a spreadsheet
        header (<PatientName> -> PERSON, <ChartNo> -> MRN); a SharePoint <UserInfo> holds a
        person (DisplayName) and an account id (AccountName), which is cleared."""
        try:
            root = self.tree(name)
        except DocumentError:
            self.pkg.set(name, b"")
            self.note("embedded")
            return
        for el in root.iter():
            lname = local(el.tag)
            if not lname or len(el) or not el.text or not el.text.strip():
                continue
            parent = el.getparent()
            pname = local(parent.tag) if parent is not None else ""
            if pname.lower() == "userinfo":
                if lname.lower() in ("accountname", "loginname", "username", "account"):
                    self.handled.add((el, "text"))
                    self._alive.append(el)
                    el.text = None
                    self.dirty.add(name)
                    continue
                entity = (E.PERSON if lname.lower() == "displayname"
                          else E.EMAIL if "mail" in lname.lower() else None)
            else:
                siblings = [local(c.tag) for c in parent] if parent is not None else [lname]
                ents = field_entities(siblings, pname or None)
                entity = ents[siblings.index(lname)] if lname in siblings else None
            if entity is None and not any(c.isalpha() for c in el.text):
                continue
            self.element_slot(name, el, entity)

    # -- relationships -----------------------------------------------------------------------

    def _collect_rels(self) -> None:
        for name in self.pkg.names:
            if not name.endswith(".rels"):
                continue
            for rel in self.tree(name):
                if local(rel.tag) == "Relationship" and rel.get("TargetMode") == "External":
                    self._external(name, rel)

    def _external(self, name: str, rel: Any) -> None:
        target = rel.get("Target") or ""
        if not target.strip():
            return
        self._alive.append(rel)
        scheme, colon, rest = target.partition(":")
        low = scheme.lower()
        if colon and low in CONTACT_SCHEMES:
            value = unquote(rest.split("?", 1)[0]).strip()
            if not value:
                return
            ent = E.EMAIL if "@" in value else E.PHONE
            self.batch.add_known(ent, value)
            new, _ = self.anonymizer.replacement_for(Span(0, len(value), ent, value, 1.0, "link"))
            if new != value:
                rel.set("Target", f"{scheme}:{quote(new, safe=URL_SAFE)}")
                self.mapping.setdefault(value, new)
                self.dirty.add(name)
                self.note("link")
            return
        if colon and len(scheme) > 1 and low in WEB_SCHEMES:
            def set_url(res: AnonResult, rel: Any = rel) -> None:
                rel.set("Target", quote(res.text, safe=URL_SAFE))
                self.dirty.add(name)
            self.slot(unquote(target), set_url)
            return
        # Local files (attached template, linked files): the path holds a user name and the
        # file name often a patient's name; the link cannot work after de-identification anyway.
        neutral = neutral_file_name(target)
        if target != neutral:
            rel.set("Target", neutral)
            self.dirty.add(name)
            self.note("link")

    # -- scrubbing ---------------------------------------------------------------------------

    def _scrub_authors(self) -> None:
        for name, root in self.trees.items():
            if not name.startswith(self.folder + "/"):
                continue
            drop = []
            for el in root.iter():
                lname = local(el.tag)
                if lname == "presenceInfo":
                    drop.append(el)
                    continue
                for key in list(el.attrib):
                    if local(key) in ("author", "initials") and el.get(key) != ANONYMOUS:
                        el.set(key, ANONYMOUS)
                        self.dirty.add(name)
            for el in drop:
                el.getparent().remove(el)
                self.dirty.add(name)

    def _scrub_props(self) -> None:
        names = set(self.pkg.names)
        if "docProps/core.xml" in names:
            root = self.tree("docProps/core.xml")
            for el in root:
                if local(el.tag) in CORE_CLEAR and (el.text or len(el)):
                    el.text = None
                    for child in list(el):
                        el.remove(child)
                    self.dirty.add("docProps/core.xml")
        if "docProps/app.xml" in names:
            root = self.tree("docProps/app.xml")
            for el in root.iter():
                lname = local(el.tag)
                if lname in APP_CLEAR and el.text:
                    el.text = None
                    self.dirty.add("docProps/app.xml")
                elif lname == "TitlesOfParts":
                    for sub in el.iter():
                        if local(sub.tag) == "lpstr" and sub.text:
                            sub.text = None
                            self.dirty.add("docProps/app.xml")
        if "docProps/custom.xml" in names:
            root = self.tree("docProps/custom.xml")
            if len(root):
                for child in list(root):
                    root.remove(child)
                self.dirty.add("docProps/custom.xml")

    def _scrub_settings(self) -> None:
        name = f"{self.folder}/settings.xml"
        if name not in self.trees:
            return
        root = self.trees[name]
        for el in list(root.iter()):
            # docVars: hidden variables; mailMerge: data-source path and SQL query, which name
            # the user's folders and often a patient list file.
            if local(el.tag) in ("docVars", "mailMerge"):
                el.getparent().remove(el)
                self.dirty.add(name)

    # -- binary parts --------------------------------------------------------------------------

    def _media(self) -> list[str]:
        folder = self.folder + "/"
        return [n for n in self.pkg.names if n.startswith(folder) and "/media/" in n]

    def _binary_parts(self) -> None:
        folder = self.folder + "/"
        self.image_parts(self._media(), image_ocr_mode((self.cfg.ocr or {}).get("docx_images")))
        for name in self.pkg.names:
            base = posixpath.basename(name).lower()
            activex = "/activex/" in name.lower() and base.endswith(".bin")   # control state
            if name.startswith(folder) and ("/embeddings/" in name or activex
                                            or base.startswith("afchunk")):
                if self.pkg.get(name):
                    self.pkg.set(name, b"")
                    self.note("embedded")
            elif base == "vbaproject.bin":
                self.note("vba")
        self._thumbnail()

    def _thumbnail(self) -> None:
        names = set(self.pkg.names)
        if "_rels/.rels" not in names:
            return
        rels = self.tree("_rels/.rels")
        for rel in list(rels):
            if not (rel.get("Type") or "").endswith("/thumbnail"):
                continue
            target = (rel.get("Target") or "").lstrip("/")
            if target not in names:
                continue
            ext = posixpath.splitext(target)[1].lower()
            blank = blank_image_like(self.pkg.get(target) or b"", ext)
            if blank is not None:
                self.pkg.set(target, blank)
                continue
            # A vector thumbnail cannot be blanked: drop the part and its relationship.
            self.pkg.remove(target)
            rels.remove(rel)
            self.pkg.set("_rels/.rels", serialize_xml(rels, self.pkg.get("_rels/.rels") or b""))
            self.dirty.discard("_rels/.rels")
            self._drop_override("/" + target)

    def _drop_override(self, part: str) -> None:
        ct = self.tree("[Content_Types].xml")
        for el in list(ct):
            if local(el.tag) == "Override" and el.get("PartName") == part:
                ct.remove(el)
        self.pkg.set("[Content_Types].xml", serialize_xml(ct, self.pkg.get("[Content_Types].xml") or b""))

    def _warnings(self) -> None:
        self.common_warnings("docx_images")
        if self.notes.get("vba"):
            self.report.warnings.append(
                "매크로(VBA)는 검사하지 않고 그대로 두었습니다. 코드 안에 개인정보가 없는지 직접 확인하세요 "
                "(VBA macros were kept as-is and NOT scanned)")


def process(src: Path, dst: Path, detector: "Detector", anonymizer: "Anonymizer",
            cfg: "Config") -> Report:
    """De-identify one Word document into `dst`."""
    src, dst = Path(src), Path(dst)
    check_paths(src, dst)
    ext = src.suffix.lower()
    if ext == ".doc" or ext == ".dot":
        raise DocumentError(_LEGACY_ERROR)
    if ext not in EXTENSIONS:
        raise DocumentError(f"Word 문서(.docx)가 아닙니다: {src.suffix} (not a .docx file)")
    report = Report(source=str(src), output=str(dst))
    pkg = ZipPackage.read(src, _OPEN_ERROR, _LEGACY_ERROR)
    _DocxJob(pkg, detector, anonymizer, cfg, report, DOCX_SPEC).run()
    write_atomic(dst, pkg.to_bytes())
    return report


__all__ = (
    "EXTENSIONS", "ANONYMOUS", "DocumentError", "ZipPackage", "TextSpec", "DOCX_SPEC",
    "ParaText", "ParagraphWalker", "PackageJob", "parse_xml", "serialize_xml", "local",
    "strip_image_metadata", "blank_image_like", "encode_image", "name_like", "process",
)
