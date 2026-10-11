"""PDF files (PyMuPDF): true removal of PII from the text layer, OCR for scanned pages.

Pipeline for one file (:func:`process`):

1. Open (password-protected PDFs are refused with a clear message). Hidden layers (optional
   content) are made visible first, otherwise their text could not be seen and would stay in
   the file.
2. Every page is classified:

   * text page - has visible text. Characters come from ``page.get_text("rawdict")`` with their
     exact boxes; lines on the same visual row are joined (table cells "성명 | 홍길동" become
     "성명 홍길동" so label rules work).
   * scanned page - no visible text but images / drawings, or an invisible OCR text layer
     over a scan, or text whose characters cannot be decoded (broken fonts). These pages are
     rendered (300 dpi by default), OCR'd and painted over (:func:`deid.formats.image.
     redact_image`) and the page is REBUILT from the redacted picture (its text layer is gone).
   * blank page - no text, images or drawings: content left alone (its form fields,
     annotations and links are still processed).

3. ``detector.detect_document`` runs once over all rows of all pages plus annotation texts,
   form-field values, bookmark titles and link targets (a name found on page 1 is found on
   page 9 too). Values from the author field and mailto:/tel: links are registered as known.
4. Each finding that is not ``keep`` becomes a redaction annotation over the union of its
   character boxes; ``apply_redactions`` REMOVES the characters from the content stream
   (image pixels underneath are blanked) and draws a black box with the replacement text
   ([이름1], 010-****-5678 ...) in white, using the viewer's Korean font (not embedded).
5. Metadata scrubbing: document info and XMP metadata, page thumbnails, embedded files,
   file-attachment / media annotations, annotation authors and rich text, XFA form data,
   structure-tree alt texts, application private data, JavaScript and EXIF/GPS data inside
   embedded JPEG photos are removed; form-field values, annotation texts and bookmark titles
   are de-identified; mailto:/tel:/launch links and links whose target contains PII are
   removed.
6. The file is saved without history (``garbage=4, clean=True``), then VERIFIED: the output is
   re-opened and its text extracted; a page on which a removed value can still be extracted is
   rebuilt as an image (fallback), and if a value still remains the run fails and nothing is
   written.

Images inside text pages: ``[ocr] pdf_images = "auto"`` (default) OCRs images covering at least
20% of a page, ``true`` OCRs every image, ``false`` none; unchecked images are reported in a
warning. ``[ocr] pdf_dpi`` (default 300) sets the render resolution of scanned pages.
"""
from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import entities as E
from ..types import AnonResult, Report
from .text import Batch, check_paths, write_atomic

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image

    from ..anonymizer import Anonymizer
    from ..config import Config
    from ..engine import Detector

EXTENSIONS = frozenset({".pdf"})

RENDER_DPI = 300            # scanned pages are rendered (and OCR'd) at this resolution
OUTPUT_DPI = 200            # ... and stored at most at this resolution
MAX_RENDER_PIXELS = 36_000_000   # cap for huge pages (A3 at 300 dpi is ~17 M pixels)
JPEG_QUALITY = 90
SCAN_COVERAGE = 0.5         # images covering this share of a page without visible text = scan
AUTO_IMAGE_COVERAGE = 0.2   # pdf_images="auto": OCR images covering this share of a page
MIN_IMAGE_W, MIN_IMAGE_H = 24, 12   # smaller images cannot hold readable text
GARBLED_RATIO = 0.3         # share of undecodable characters that makes a text layer unusable
GARBLED_MIN = 5
OVERLAY_MIN_SIZE = 3.0      # smaller overlay text is written invisibly (still extractable)
OVERLAY_LINE_HEIGHT = 1.7   # box height needed per point of overlay font size
DEGENERATE = 0.5            # boxes thinner than this (pt) cannot carry a redaction

_DELETE_ANNOT_NAMES = ("PDF_ANNOT_FILE_ATTACHMENT", "PDF_ANNOT_SOUND", "PDF_ANNOT_MOVIE",
                       "PDF_ANNOT_RICH_MEDIA", "PDF_ANNOT_3D", "PDF_ANNOT_SCREEN")
_CONTACT_SCHEMES = ("mailto", "tel", "sms", "callto", "facetime", "kakaotalk")
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")
_WS = re.compile(r"\s+")
_GENERIC_AUTHORS = frozenset("""
author user admin administrator owner unknown pc home 사용자 작성자 관리자 microsoft word excel
""".split())


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------


class PdfError(ValueError):
    """The PDF cannot be processed safely. Messages never contain document text."""


class PdfPasswordError(PdfError):
    """The PDF needs a password to open."""


class PdfVerifyError(PdfError):
    """Removed values could still be extracted from the output; nothing was written."""


_OPEN_ERROR = ("PDF 파일을 열 수 없습니다. 손상되었거나 PDF가 아닌 파일일 수 있습니다. "
               "(cannot open the PDF: the file may be damaged or not a PDF)")
_PASSWORD_ERROR = ("암호가 걸린 PDF입니다. PDF 뷰어에서 암호를 해제한 사본(인쇄 → PDF로 저장 등)을 만든 뒤 "
                   "다시 시도하세요. (the PDF is password-protected: save an unprotected copy first)")


def _fitz() -> Any:
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24.3
    except ImportError:
        try:
            import fitz  # type: ignore[no-redef]  # older PyMuPDF
        except ImportError as e:  # pragma: no cover - hard dependency
            raise RuntimeError("PyMuPDF가 필요합니다: pip install pymupdf "
                               "(PyMuPDF is required to process PDF files)") from e
    return fitz


def _const(fitz: Any, name: str, default: int) -> int:
    return int(getattr(fitz, name, default))


# ---------------------------------------------------------------------------------------------
# Text layer: characters with boxes, grouped into visual rows
# ---------------------------------------------------------------------------------------------

Bbox = tuple[float, float, float, float]


@dataclass
class _Char:
    c: str
    bbox: Bbox | None        # None for the synthetic space between two lines of a row
    line: int                # line id (unique per page)


@dataclass
class _Row:
    page: int
    chars: list[_Char]
    secondary: bool = False  # extracted with ActualText ignored (glyph text)
    seam: bool = False       # end of one line + start of the next (words wrapped mid-word)
    result: AnonResult | None = None

    @property
    def text(self) -> str:
        return "".join(ch.c for ch in self.chars)


def _norm_char(c: str) -> str:
    if c.isspace():
        return " "
    return c


def _undecodable(c: str) -> bool:
    if c == "\ufffd":
        return True
    cat = unicodedata.category(c)
    return cat in ("Co", "Cs") or (cat == "Cc" and not c.isspace())


def _text_flags(fitz: Any, ignore_actualtext: bool = False) -> int:
    # No TEXT_MEDIABOX_CLIP: text placed outside the visible page is still in the file.
    flags = _const(fitz, "TEXT_PRESERVE_WHITESPACE", 1)
    if ignore_actualtext:
        flags |= _const(fitz, "TEXT_IGNORE_ACTUALTEXT", 0)
    return flags


def _get_text(page: Any, fitz: Any, kind: str, flags: int) -> Any:
    try:
        return page.get_text(kind, flags=flags, clip=fitz.INFINITE_RECT())
    except (TypeError, AttributeError):  # pragma: no cover - very old PyMuPDF
        return page.get_text(kind, flags=flags)


@dataclass
class _Lines:
    """Text lines of one page: chars per line, plus statistics."""

    lines: list[tuple[list[_Char], Bbox, bool, int]] = field(default_factory=list)  # +horiz, block
    total: int = 0
    bad: int = 0

    @property
    def garbled(self) -> bool:
        return self.bad >= GARBLED_MIN and self.bad >= GARBLED_RATIO * max(self.total, 1)

    def plain(self) -> str:
        return "\n".join("".join(ch.c for ch in chars) for chars, _, _, _ in self.lines)


def _page_lines(page: Any, fitz: Any, ignore_actualtext: bool = False) -> _Lines:
    raw = _get_text(page, fitz, "rawdict", _text_flags(fitz, ignore_actualtext))
    out = _Lines()
    line_id = 0
    for block_no, block in enumerate(raw.get("blocks", [])):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            lb = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            chars: list[_Char] = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if not c or c in _ZERO_WIDTH:
                        continue
                    if not c.isspace():
                        out.total += 1
                        if _undecodable(c):
                            out.bad += 1
                    x0, y0, x1, y1 = (float(v) for v in ch.get("bbox", lb))
                    if y1 - y0 <= 0:
                        y0, y1 = lb[1], lb[3]
                    chars.append(_Char(_norm_char(c), (x0, y0, x1, y1), line_id))
            if chars and any(not ch.c.isspace() for ch in chars):
                d = line.get("dir", (1.0, 0.0))
                horiz = abs(float(d[1])) < 0.1 and float(d[0]) > 0
                out.lines.append((chars, lb, horiz, block_no))  # type: ignore[arg-type]
                line_id += 1
    return out


def _rows(lines: _Lines, page_no: int, secondary: bool = False) -> list[_Row]:
    """Join horizontal lines that share a visual row (sorted left to right) into one row."""
    horiz = [(chars, bb) for chars, bb, h, _ in lines.lines if h]
    other = [chars for chars, _, h, _ in lines.lines if not h]
    horiz.sort(key=lambda it: ((it[1][1] + it[1][3]) / 2, it[1][0]))
    groups: list[tuple[float, float, list[tuple[list[_Char], Bbox]]]] = []
    for chars, bb in horiz:
        h = max(bb[3] - bb[1], 0.1)
        cy = (bb[1] + bb[3]) / 2
        if groups:
            y0, y1, members = groups[-1]
            gh = max(y1 - y0, 0.1)
            overlap = min(y1, bb[3]) - max(y0, bb[1])
            if overlap >= 0.5 * min(h, gh) and abs(cy - (y0 + y1) / 2) <= 0.5 * min(h, gh):
                members.append((chars, bb))
                groups[-1] = (min(y0, bb[1]), max(y1, bb[3]), members)
                continue
        groups.append((bb[1], bb[3], [(chars, bb)]))
    rows: list[_Row] = []
    for _, _, members in groups:
        members.sort(key=lambda it: it[1][0])
        joined: list[_Char] = []
        for i, (chars, _) in enumerate(members):
            if i:
                joined.append(_Char(" ", None, -1))
            joined.extend(chars)
        rows.append(_Row(page_no, joined, secondary))
    rows.extend(_Row(page_no, list(chars), secondary) for chars in other)
    return rows


SEAM_CHARS = 12


def _seams(lines: _Lines, page_no: int) -> list[_Row]:
    """Rows bridging two consecutive lines of one text block: the last SEAM_CHARS characters of
    a line + the first SEAM_CHARS of the next, joined directly (Korean wraps inside words:
    "홍길" / "동님은") and with a space (English: "Mr John" / "Smith")."""
    out: list[_Row] = []
    prev = None
    for chars, bb, horiz, block in lines.lines:
        if prev is not None and horiz and prev[2] and prev[3] == block and bb[1] > prev[1][1]:
            left = list(prev[0])
            while left and left[-1].c.isspace():
                left.pop()
            right = list(chars)
            while right and right[0].c.isspace():
                right.pop(0)
            if left and right and left[-1].c.isalpha() and right[0].c.isalpha():
                left, right = left[-SEAM_CHARS:], right[:SEAM_CHARS]
                out.append(_Row(page_no, left + right, seam=True))
                out.append(_Row(page_no, left + [_Char(" ", None, -1)] + right, seam=True))
        prev = (chars, bb, horiz, block)
    return out


def _span_rects(row: _Row, start: int, end: int, fitz: Any) -> list[Any]:
    """Union of the character boxes of row.text[start:end], one rect per source line."""
    groups: dict[int, list[Bbox]] = {}
    for ch in row.chars[start:end]:
        if ch.bbox is None or ch.c.isspace():
            continue
        groups.setdefault(ch.line, []).append(ch.bbox)
    rects = []
    for boxes in groups.values():
        r = fitz.Rect(min(b[0] for b in boxes), min(b[1] for b in boxes),
                      max(b[2] for b in boxes), max(b[3] for b in boxes))
        rects.append(r)
    return rects


def _degenerate(r: Any) -> bool:
    return r.width < DEGENERATE or r.height < DEGENERATE


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _norm_value(s: str) -> str:
    return _WS.sub("", "".join(c for c in s if c not in _ZERO_WIDTH)).casefold()


def _name_like_author(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not (2 <= len(v) <= 40) or v.casefold() in _GENERIC_AUTHORS:
        return None
    if any(ch.isdigit() for ch in v) or "@" in v or "\\" in v or "/" in v:
        return None
    return v


def _pages_text(pages: list[int]) -> str:
    nums = [p + 1 for p in sorted(set(pages))]
    if len(nums) > 12:
        return ", ".join(map(str, nums[:12])) + f" 외 {len(nums) - 12}"
    return ", ".join(map(str, nums))


def _pil_from_pixmap(pix: Any) -> "Image.Image":
    from PIL import Image

    mode = {1: "L", 3: "RGB"}.get(pix.n)
    if mode is None:
        raise PdfError("지원하지 않는 이미지 색상 형식 (unsupported image colour format)")
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _overlay_font(text: str) -> str:
    return "helv" if text.isascii() else "korea"


def _overlay_size(fitz: Any, text: str, rect: Any, font: str) -> float:
    try:
        unit = fitz.get_text_length(text, fontname=font, fontsize=1.0)
    except Exception:  # noqa: BLE001 - unknown metrics: no overlay text, black box only
        return 0.0
    if unit <= 0:
        return 0.0
    # insert_textbox needs about 1.6 x fontsize of height per line (CJK and Helvetica); a size
    # that fits on the first try avoids PyMuPDF's slow shrink-and-retry loop.
    size = math.floor(min(rect.height / OVERLAY_LINE_HEIGHT, rect.width * 0.95 / unit) * 2) / 2
    return size if size >= OVERLAY_MIN_SIZE else 0.0


# ---------------------------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------------------------


@dataclass
class _PageInfo:
    index: int
    kind: str                 # "text" | "scan" | "blank"
    reason: str = ""          # scan: "no_text" | "ocr_layer" | "garbled"
    rows: list[_Row] = field(default_factory=list)
    image_cov: float = 0.0

    @property
    def kept(self) -> bool:
        """The page object stays (not rebuilt from an image): its annotations, form fields
        and links must be processed - a "blank" page can still hold a filled-in form."""
        return self.kind in ("text", "blank")


class _PdfJob:
    def __init__(self, doc: Any, fitz: Any, detector: "Detector", anonymizer: "Anonymizer",
                 cfg: "Config", report: Report) -> None:
        self.doc = doc
        self.fitz = fitz
        self.detector = detector
        self.anonymizer = anonymizer
        self.cfg = cfg
        self.report = report
        self.batch = Batch(detector, anonymizer, report)
        self.pages: list[_PageInfo] = []
        self.notes: dict[str, int] = {}
        self.page_rects: dict[int, list[Any]] = {}        # page -> rects covered (unrotated)
        self.values: set[str] = set()                     # every removed original value
        self.value_entity: dict[str, str] = {}            # removed value -> its entity
        self.intended: dict[int, list[list[str]]] = {}    # page -> annotation/field texts we write
        self.annot_jobs: list[tuple[int, int, str, str]] = []   # page, xref, field, new text
        self.widget_jobs: list[tuple[int, int, str]] = []
        self.link_deletes: dict[int, list[dict]] = {}
        self.toc: list[list] = []
        self.toc_changed = False
        self.rasterized: list[int] = []
        self._shared = False
        opts = dict(getattr(cfg, "ocr", None) or {})
        self.image_mode = opts.get("pdf_images", "auto")
        self.dpi = int(opts.get("pdf_dpi", RENDER_DPI) or RENDER_DPI)

    # -- notes -------------------------------------------------------------------------------

    def note(self, key: str, n: int = 1) -> None:
        self.notes[key] = self.notes.get(key, 0) + n

    # -- preparation -------------------------------------------------------------------------

    def collect_metadata(self) -> None:
        author = _name_like_author((self.doc.metadata or {}).get("author"))
        if author:
            self.batch.add_known(E.PERSON, author)

    def classify(self) -> None:
        fitz, doc = self.fitz, self.doc
        for pno in range(doc.page_count):
            page = doc[pno]
            info = _PageInfo(pno, "blank")
            try:
                lines = _page_lines(page, fitz)
            except Exception:  # noqa: BLE001 - unreadable text layer: treat as scan
                info.kind, info.reason = "scan", "garbled"
                self.pages.append(info)
                continue
            area = abs(page.rect) or 1.0
            cov = 0.0
            n_images = 0
            try:
                for im in page.get_image_info():
                    n_images += 1
                    r = fitz.Rect(im["bbox"]) & page.rect
                    if not r.is_empty:
                        cov = max(cov, abs(r) / area)
            except Exception:  # noqa: BLE001
                pass
            info.image_cov = cov
            visible, invisible = self._visibility(page)
            if lines.lines and lines.garbled:
                info.kind, info.reason = "scan", "garbled"
            elif lines.lines and visible > 0:
                info.kind = "text"
                info.rows = _rows(lines, pno) + _seams(lines, pno)
                info.rows += self._actualtext_rows(page, pno, lines)
            elif lines.lines or invisible:
                # Text exists but none of it is visible: an OCR layer over a scan (or hidden text).
                info.kind, info.reason = "scan", "ocr_layer"
                info.rows = _rows(lines, pno) + _seams(lines, pno)
            elif n_images or self._has_drawings(page):
                info.kind, info.reason = "scan", "no_text"
            self.pages.append(info)

    def _visibility(self, page: Any) -> tuple[int, int]:
        try:
            trace = page.get_texttrace()
        except Exception:  # noqa: BLE001 - assume visible
            return 1, 0
        vis = inv = 0
        for sp in trace:
            n = len(sp.get("chars", ()))
            if sp.get("type") == 3 or float(sp.get("opacity", 1.0)) <= 0.0:
                inv += n
            else:
                vis += n
        return vis, inv

    @staticmethod
    def _has_drawings(page: Any) -> bool:
        try:
            return bool(page.get_drawings())
        except Exception:  # noqa: BLE001
            return True

    def _actualtext_rows(self, page: Any, pno: int, primary: _Lines) -> list[_Row]:
        """Glyph-text rows that differ from the ActualText-honoring extraction (hidden text
        behind /ActualText would otherwise never be seen)."""
        try:
            alt = _page_lines(page, self.fitz, ignore_actualtext=True)
        except Exception:  # noqa: BLE001
            return []
        if alt.plain() == primary.plain():
            return []
        seen = {r.text for r in _rows(primary, pno)}
        return [r for r in _rows(alt, pno, secondary=True) if r.text not in seen]

    # -- slots ----------------------------------------------------------------------------

    def collect_slots(self) -> None:
        for info in self.pages:
            for row in info.rows:
                self.batch.add(row.text, lambda res, r=row: setattr(r, "result", res))
        for info in self.pages:
            if not info.kept:
                continue
            page = self.doc[info.index]
            self._collect_links(page, info.index)
            self._collect_annots(page, info.index)
            self._collect_widgets(page, info.index)
        self._collect_toc()

    def _collect_links(self, page: Any, pno: int) -> None:
        fitz = self.fitz
        for link in page.get_links():
            kind = link.get("kind")
            uri = link.get("uri") or ""
            if kind in (_const(fitz, "LINK_LAUNCH", 3), _const(fitz, "LINK_GOTOR", 5)):
                self.link_deletes.setdefault(pno, []).append(link)
                self.note("link")
                continue
            if not uri:
                continue
            scheme, _, rest = uri.partition(":")
            if scheme.lower() in _CONTACT_SCHEMES:
                value = rest.split("?")[0].strip()
                if value:
                    try:
                        from urllib.parse import unquote
                        value = unquote(value)
                    except Exception:  # noqa: BLE001
                        pass
                    self.batch.add_known(E.EMAIL if "@" in value else E.PHONE, value)
                self.link_deletes.setdefault(pno, []).append(link)
                self.note("link")
                continue

            def drop(res: AnonResult, p: int = pno, lk: dict = link) -> None:
                if any(r.action != "keep" for r in res.replacements):
                    self.link_deletes.setdefault(p, []).append(lk)
                    self.note("link")

            self.batch.add(uri, drop)

    def _collect_annots(self, page: Any, pno: int) -> None:
        fitz = self.fitz
        delete_types = {_const(fitz, n, -100 - i) for i, n in enumerate(_DELETE_ANNOT_NAMES)}
        redact_type = _const(fitz, "PDF_ANNOT_REDACT", 12)
        for annot in page.annots():
            atype = annot.type[0]
            if atype in delete_types:
                self.annot_jobs.append((pno, annot.xref, "delete", ""))
                self.note("attachment")
                continue
            if atype == redact_type:
                continue
            info = annot.info or {}
            author = _name_like_author(info.get("title"))
            if author:
                self.batch.add_known(E.PERSON, author)
            self.annot_jobs.append((pno, annot.xref, "author", ""))
            for key in ("content", "subject"):
                value = info.get(key) or ""
                if value.strip():
                    holder = [value]
                    self.intended.setdefault(pno, []).append(holder)

                    def set_text(res: AnonResult, p: int = pno, x: int = annot.xref,
                                 k: str = key, h: list = holder) -> None:
                        self._record_result(res)
                        h[0] = res.text
                        self.annot_jobs.append((p, x, k, res.text))
                    self.batch.add(value, set_text)

    def _collect_widgets(self, page: Any, pno: int) -> None:
        fitz = self.fitz
        text_types = {_const(fitz, "PDF_WIDGET_TYPE_TEXT", 7), _const(fitz, "PDF_WIDGET_TYPE_COMBOBOX", 3),
                      _const(fitz, "PDF_WIDGET_TYPE_LISTBOX", 4)}
        for widget in page.widgets():
            if widget.field_type not in text_types:
                continue
            value = widget.field_value
            if not isinstance(value, str) or not value.strip():
                continue
            entity = None
            try:
                from .tabular import column_entity
                entity = column_entity(widget.field_name or "")
            except Exception:  # noqa: BLE001 - typing is a bonus
                entity = None
            holder = [value]
            self.intended.setdefault(pno, []).append(holder)

            def set_value(res: AnonResult, p: int = pno, x: int = widget.xref,
                          h: list = holder) -> None:
                self._record_result(res)
                h[0] = res.text
                self.widget_jobs.append((p, x, res.text))
            self.batch.add(value, set_value, entity)

    def _collect_toc(self) -> None:
        try:
            self.toc = self.doc.get_toc(simple=False)
        except Exception:  # noqa: BLE001
            self.toc = []
        for item in self.toc:
            title = item[1] if len(item) > 1 else ""
            if isinstance(title, str) and title.strip():
                def set_title(res: AnonResult, it: list = item) -> None:
                    self._record_result(res)
                    it[1] = res.text
                    self.toc_changed = True
                self.batch.add(title, set_title)

    # -- applying ----------------------------------------------------------------------------

    def apply_text_pages(self) -> None:
        fitz, doc = self.fitz, self.doc
        images = _const(fitz, "PDF_REDACT_IMAGE_PIXELS", 2)
        graphics = _const(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
        text_remove = _const(fitz, "PDF_REDACT_TEXT_REMOVE", 0)
        for info in self.pages:
            if not info.kept:
                continue
            page = doc[info.index]
            self._apply_annots(page, info.index)
            self._apply_links(page, info.index)
            overlays = self._add_redactions(page, info)
            if any(a.type[0] == _const(fitz, "PDF_ANNOT_REDACT", 12) for a in page.annots()):
                _apply_redactions(page, images=images, graphics=graphics, text=text_remove)
            if overlays:
                _draw_overlays(page, overlays, fitz)

    def _add_redactions(self, page: Any, info: _PageInfo) -> list[tuple[Any, str]]:
        """Add black redaction boxes; returns (rect, replacement text) overlays to draw after
        the redactions are applied."""
        fitz = self.fitz
        primary: list[Any] = []
        covered: set[int] = set()       # ids of characters already redacted on this page
        overlays: list[tuple[Any, str]] = []
        for row in sorted(info.rows, key=lambda r: (r.seam, r.secondary)):
            if row.result is None:
                continue
            for rep in row.result.replacements:
                if rep.action == "keep":
                    continue
                self._remember(rep)
                solid = [id(c) for c in row.chars[rep.start:rep.end]
                         if c.bbox is not None and not c.c.isspace()]
                if row.seam and solid and all(i in covered for i in solid):
                    # Already found by a row (or the other seam variant): not a new finding.
                    if self.report.counts.get(rep.entity, 0) > 0:
                        self.report.counts[rep.entity] -= 1
                        if not self.report.counts[rep.entity]:
                            del self.report.counts[rep.entity]
                    continue
                covered.update(solid)
                rects = [r for r in _span_rects(row, rep.start, rep.end, fitz) if not _degenerate(r)]
                if not rects:
                    continue
                if row.secondary and all(_covered(r, primary) for r in rects):
                    continue
                main = max(rects, key=lambda r: r.width)
                for r in rects:
                    page.add_redact_annot(r, fill=(0, 0, 0), cross_out=False)
                    if not row.secondary:
                        primary.append(r)
                    self.page_rects.setdefault(info.index, []).append(r)
                if rep.replacement.strip():
                    overlays.append((main, rep.replacement))
        return overlays

    def _record_result(self, res: AnonResult) -> None:
        for r in res.replacements:
            if r.action != "keep":
                self._remember(r)

    def _remember(self, rep: Any) -> None:
        self.values.add(rep.original)
        self.value_entity.setdefault(rep.original, rep.entity)

    def _apply_annots(self, page: Any, pno: int) -> None:
        jobs = [j for j in self.annot_jobs if j[0] == pno]
        if not jobs:
            return
        doc = self.doc
        freetext = _const(self.fitz, "PDF_ANNOT_FREE_TEXT", 2)
        by_xref: dict[int, dict[str, str]] = {}
        for _, xref, key, value in jobs:
            by_xref.setdefault(xref, {})[key] = value
        for annot in list(page.annots()):
            todo = by_xref.get(annot.xref)
            if todo is None:
                continue
            if "delete" in todo:
                page.delete_annot(annot)
                continue
            changed = {k: v for k, v in todo.items() if k in ("content", "subject")}
            if annot.type[0] == freetext and "content" in changed:
                # Its appearance shows the original text and cannot be regenerated reliably
                # with Korean text: remove the box.
                page.delete_annot(annot)
                self.note("freetext")
                continue
            info = annot.info or {}
            try:
                annot.set_info(content=changed.get("content", info.get("content", "")),
                               subject=changed.get("subject", info.get("subject", "")),
                               title="")
            except Exception:  # noqa: BLE001 - e.g. popups; leftovers are caught by verify()
                pass
            for key in ("RC", "T"):
                try:
                    if doc.xref_get_key(annot.xref, key)[0] != "null":
                        doc.xref_set_key(annot.xref, key, "null")
                except Exception:  # noqa: BLE001
                    pass
            if changed:
                self.note("annot")
            try:
                annot.update()
            except Exception:  # noqa: BLE001 - appearance regeneration is optional
                pass

    def _apply_links(self, page: Any, pno: int) -> None:
        for link in self.link_deletes.get(pno, []):
            try:
                page.delete_link(link)
            except Exception:  # noqa: BLE001
                pass

    def apply_widgets(self) -> None:
        if not self.widget_jobs:
            return
        for pno in sorted({j[0] for j in self.widget_jobs}):
            if not self.pages[pno].kept:
                continue
            page = self.doc[pno]
            new = {x: v for p, x, v in self.widget_jobs if p == pno}
            for widget in page.widgets():
                if widget.xref in new:
                    widget.field_value = new[widget.xref]
                    try:
                        widget.update()
                    except Exception:  # noqa: BLE001 - value is set; appearance best effort
                        pass
                    self.note("widget")

    def share_values(self) -> None:
        """Before the first OCR step: values found in the text layer become known values, so
        the same name is also found on scanned pages and in images (OCR runs its own
        detection)."""
        if self._shared:
            return
        self._shared = True
        try:
            from ..engine import PROPAGATE_ENTITIES
        except ImportError:  # pragma: no cover
            PROPAGATE_ENTITIES = frozenset({E.PERSON, E.EMAIL, E.PHONE, E.RRN, E.MRN})
        for info in self.pages:
            for row in info.rows:
                if row.result is None:
                    continue
                for r in row.result.replacements:
                    if r.entity in PROPAGATE_ENTITIES and r.action != "keep":
                        self.batch.add_known(r.entity, r.original)

    # -- images on text pages ----------------------------------------------------------------

    def check_images(self) -> None:
        mode = self.image_mode
        if isinstance(mode, str):
            mode = mode.strip().lower()
        everything = mode is True or mode in ("all", "true", "yes", "on", "1")
        auto = mode in ("auto", None, "")
        done: set[int] = set()
        unchecked: set[Any] = set()
        for info in self.pages:
            if info.kind != "text":
                continue
            page = self.doc[info.index]
            try:
                infos = page.get_image_info(xrefs=True)
            except Exception:  # noqa: BLE001
                infos = []
            area = abs(page.rect) or 1.0
            for im in infos:
                xref = int(im.get("xref") or 0)
                w, h = int(im.get("width") or 0), int(im.get("height") or 0)
                key = xref or (info.index, tuple(round(v) for v in im.get("bbox", ())))
                if w < MIN_IMAGE_W or h < MIN_IMAGE_H:
                    continue
                r = self.fitz.Rect(im["bbox"]) & page.rect
                cov = abs(r) / area if not r.is_empty else 0.0
                wanted = everything or (auto and cov >= AUTO_IMAGE_COVERAGE)
                if not wanted or not xref:
                    if key not in done:
                        unchecked.add(key)
                    continue
                if xref in done:
                    continue
                done.add(xref)
                unchecked.discard(xref)
                self._ocr_image(page, info.index, xref)
        if unchecked:
            self.note("image_unchecked", len(unchecked))

    def _ocr_image(self, page: Any, pno: int, xref: int) -> None:
        fitz = self.fitz
        from .image import redact_image

        self.share_values()
        try:
            pix = fitz.Pixmap(self.doc, xref)
            if pix.colorspace is None:
                return  # stencil mask: no picture content of its own
            if pix.n - pix.alpha not in (1, 3):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            if pix.alpha:
                pix = fitz.Pixmap(pix, 0)
            img = _pil_from_pixmap(pix)
        except PdfError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PdfError(f"{pno + 1}쪽의 그림을 읽을 수 없습니다 (cannot decode an image on "
                           f"page {pno + 1}: {type(e).__name__})") from e
        out, rep = redact_image(img, self.detector, self.cfg, self.anonymizer)
        rep.warnings = [f"{pno + 1}쪽 그림 / image on page {pno + 1}: {w}" for w in rep.warnings]
        self.report.merge(rep)
        if any(self.cfg.action_for(ent) != "keep" for ent in rep.counts):
            buf = io.BytesIO()
            out.save(buf, format="PNG")
            page.replace_image(xref, stream=buf.getvalue())
            self.note("image_redacted")

    # -- scanned pages -----------------------------------------------------------------------

    def rasterize(self, pno: int, ocr: bool = True) -> None:
        """Render page `pno` as displayed, cover known findings and OCR findings, and replace
        the page by the redacted picture (nothing of the old page survives)."""
        fitz, doc = self.fitz, self.doc
        from PIL import ImageDraw

        page = doc[pno]
        zoom = max(self.dpi, 72) / 72.0
        area = max(abs(page.rect), 1.0)
        zoom = min(zoom, math.sqrt(MAX_RENDER_PIXELS / area))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = _pil_from_pixmap(pix)
        if img.mode != "RGB":
            img = img.convert("RGB")
        mat = page.rotation_matrix * fitz.Matrix(zoom, zoom)
        rects = list(self.page_rects.get(pno, []))
        info = self.pages[pno]
        for row in info.rows:
            if row.result is None:
                continue
            for rep in row.result.replacements:
                if rep.action == "keep":
                    continue
                self._remember(rep)
                rects.extend(_span_rects(row, rep.start, rep.end, fitz))
        draw = ImageDraw.Draw(img)
        for r in rects:
            pr = fitz.Rect(r) * mat
            pad = max(2.0, 0.15 * min(pr.width, pr.height))
            draw.rectangle((pr.x0 - pad, pr.y0 - pad, pr.x1 + pad, pr.y1 + pad), fill=(0, 0, 0))
        if ocr:
            from .image import redact_image

            self.share_values()
            img, rep = redact_image(img, self.detector, self.cfg, self.anonymizer)
            rep.warnings = [f"{pno + 1}쪽 / page {pno + 1}: {w}" for w in rep.warnings]
            self.report.merge(rep)
        width, height = page.rect.width, page.rect.height
        scale = min(1.0, OUTPUT_DPI / 72.0 / zoom)
        if scale < 1.0:
            from PIL import Image

            img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        new = doc.new_page(pno, width=width, height=height)
        new.insert_image(new.rect, stream=buf.getvalue())
        doc.delete_page(pno + 1)
        self.rasterized.append(pno)
        info.kind = "image"

    def rasterize_scans(self) -> None:
        for info in self.pages:
            if info.kind == "scan":
                self.rasterize(info.index)

    # -- document-wide scrubbing -------------------------------------------------------------

    def finish_toc(self) -> None:
        if not self.toc or not (self.toc_changed or self.rasterized):
            return
        try:
            self.doc.set_toc(self.toc)
        except Exception:  # noqa: BLE001 - fall back to plain [level, title, page] entries
            self.doc.set_toc([[it[0], it[1], it[2]] for it in self.toc])

    def scrub(self) -> None:
        doc = self.doc
        try:
            doc.set_metadata({})
        except Exception:  # noqa: BLE001
            pass
        try:
            doc.del_xml_metadata()
        except Exception:  # noqa: BLE001
            pass
        try:
            names = list(doc.embfile_names())
        except Exception:  # noqa: BLE001
            names = []
        for name in names:
            doc.embfile_del(name)
        if names:
            self.note("embedded", len(names))
        cat = doc.pdf_catalog()
        for key in ("AcroForm/XFA", "StructTreeRoot", "PieceInfo"):
            try:
                if doc.xref_get_key(cat, key)[0] != "null":
                    doc.xref_set_key(cat, key, "null")
                    if key == "AcroForm/XFA":
                        self.note("xfa")
                    elif key == "StructTreeRoot":
                        self.note("struct")
            except Exception:  # noqa: BLE001
                pass
        for pno in range(doc.page_count):
            xref = doc[pno].xref
            for key in ("Thumb", "PieceInfo", "Metadata"):
                try:
                    if doc.xref_get_key(xref, key)[0] != "null":
                        doc.xref_set_key(xref, key, "null")
                except Exception:  # noqa: BLE001
                    pass
        from .docx import strip_image_metadata

        for xref in range(1, doc.xref_length()):
            try:
                if doc.xref_get_key(xref, "S") == ("name", "/JavaScript"):
                    doc.update_object(xref, "<</S/JavaScript/JS()>>")
                    continue
                if (doc.xref_get_key(xref, "Subtype") == ("name", "/Image")
                        and doc.xref_get_key(xref, "Filter") == ("name", "/DCTDecode")):
                    # JPEGs are embedded as they are: EXIF (camera owner, GPS) comes along.
                    raw = doc.xref_stream_raw(xref)
                    new = strip_image_metadata(raw, ".jpg")
                    if new != raw:
                        parms = doc.xref_get_key(xref, "DecodeParms")
                        doc.update_stream(xref, new, compress=False)
                        doc.xref_set_key(xref, "Filter", "/DCTDecode")
                        if parms[0] != "null":
                            doc.xref_set_key(xref, "DecodeParms", parms[1])
                if doc.xref_get_key(xref, "Type") == ("name", "/Metadata"):
                    doc.update_stream(xref, b" ")
                elif doc.xref_get_key(xref, "Metadata")[0] != "null":
                    doc.xref_set_key(xref, "Metadata", "null")
            except Exception:  # noqa: BLE001 - broken objects are dropped by garbage collection
                continue

    def sweep_strings(self) -> None:
        """Every text string left in the document's objects - form field names and tooltips,
        layer (OCG) names, page-label prefixes, link titles, keys no other step knows - is
        checked: values removed elsewhere are replaced, and the strings also go through
        detection themselves ("환자_홍길동" as a field name)."""
        doc, fitz = self.doc, self.fitz
        found: list[tuple[int, str, list[tuple[int, int, str]]]] = []
        texts: dict[str, None] = {}
        for xref, src, toks in _object_strings(doc):
            found.append((xref, src, toks))
            for _, _, t in toks:
                texts.setdefault(t, None)
        if not texts:
            return
        new_for = self._sweep_texts(list(texts))
        n = 0
        for xref, src, toks in found:
            parts, pos = [], 0
            for start, end, t in toks:
                new = new_for.get(t)
                if new is None or new == t:
                    continue
                parts += (src[pos:start], fitz.get_pdf_str(new))
                pos = end
            if not parts:
                continue
            parts.append(src[pos:])
            try:
                doc.update_object(xref, "".join(parts))
            except Exception:  # noqa: BLE001 - cannot rewrite: drop the object's content
                try:
                    doc.update_object(xref, "<<>>")
                except Exception:  # noqa: BLE001 - verification below still catches it
                    continue
            n += 1
        if n:
            self.note("strings", n)

    def _sweep_texts(self, texts: list[str]) -> dict[str, str]:
        from ..engine import resolve_overlaps
        from ..types import Span

        values = sorted({v for v in self.values if len(v.strip()) >= 2}, key=len, reverse=True)
        rx = re.compile("|".join(map(re.escape, values))) if values else None
        detected = self.detector.detect_document([t.replace("_", " ") for t in texts])
        out: dict[str, str] = {}
        for t, spans in zip(texts, detected):
            cands = [Span(sp.start, sp.end, sp.entity, t[sp.start:sp.end], sp.score, sp.source)
                     for sp in spans if self.cfg.action_for(sp.entity) != "keep"]
            if rx is not None:
                cands += [Span(m.start(), m.end(), self.value_entity.get(m.group(), E.OTHER),
                               m.group(), 0.99, "pdf_value") for m in rx.finditer(t)]
            if not cands:
                continue
            res = self.anonymizer.anonymize(t, resolve_overlaps(cands))
            if res.text != t:
                for r in res.replacements:
                    if r.original not in self.values:   # count new findings only
                        self.report.add(r.entity)
                        self._remember(r)
                out[t] = res.text
        return out

    # -- saving and verification -------------------------------------------------------------

    def save_bytes(self) -> bytes:
        fitz = self.fitz
        kwargs: dict[str, Any] = dict(garbage=4, deflate=True, clean=True)
        enc = getattr(fitz, "PDF_ENCRYPT_NONE", None)
        if enc is not None:
            kwargs["encryption"] = enc
        return self.doc.tobytes(**kwargs)

    def page_texts(self, doc: Any) -> list[tuple[str, str]]:
        """Per page: normalized text in both extraction modes (ActualText honoured / glyph
        text), each followed by the page's annotation and form-field texts."""
        fitz = self.fitz
        out = []
        for page in doc:
            extra = _annot_texts(page)
            modes = []
            for ignore in (False, True):
                try:
                    text = _get_text(page, fitz, "text", _text_flags(fitz, ignore))
                except Exception:  # noqa: BLE001
                    text = ""
                modes.append(_norm_value("\n".join([text, *extra])))
            out.append((modes[0], modes[1]))
        return out

    def expected(self) -> dict[tuple[int, str], int]:
        """How often each removed value may legitimately remain on each page: the number of
        times it occurs in the de-identified text we meant to produce (occurrences the detector
        did not flag, e.g. inside a longer word, or inside a replacement)."""
        allowed: dict[tuple[int, str], int] = {}
        norms = {v for v in (_norm_value(x) for x in self.values) if len(v) >= 2}
        for info in self.pages:
            parts = [row.result.text if row.result is not None else row.text
                     for row in info.rows if not row.seam]
            parts += [h[0] for h in self.intended.get(info.index, [])]   # what we meant to write
            text = _norm_value("\n".join(parts))
            for nv in norms:
                allowed[(info.index, nv)] = text.count(nv)
        return allowed

    def leaks(self, data: bytes, allowed: dict[tuple[int, str], int]
              ) -> tuple[list[int], int, int]:
        """(pages with leftovers, document-level leftovers, leftovers in other object strings)
        in the saved output. Object strings include annotations, so they do not block the
        per-page image fallback (which removes a page's annotations)."""
        out = self.fitz.open("pdf", data)
        try:
            texts = self.page_texts(out)
            pages = []
            for (pno, nv), n in allowed.items():
                if pno < len(texts) and max(texts[pno][0].count(nv), texts[pno][1].count(nv)) > n:
                    pages.append(pno)
            doc_level = 0
            md = out.metadata or {}
            for key in ("title", "author", "subject", "keywords", "creator"):
                if (md.get(key) or "").strip():
                    doc_level += 1
            if out.embfile_count():
                doc_level += 1
            try:
                if (out.get_xml_metadata() or "").strip():
                    doc_level += 1
            except Exception:  # noqa: BLE001
                pass
            strings = 0
            norms = sorted({nv for nv in (_norm_value(v) for v in self.values) if len(nv) >= 2})
            if norms:
                for _, _, toks in _object_strings(out):
                    for _, _, t in toks:
                        tn = _norm_value(t)
                        strings += sum(1 for nv in norms if nv in tn)
            toc_out = _norm_value("\n".join(str(t[1]) for t in out.get_toc()))
            toc_meant = _norm_value("\n".join(str(t[1]) for t in self.toc))
            for nv in {_norm_value(v) for v in self.values}:
                if len(nv) >= 2 and toc_out.count(nv) > toc_meant.count(nv):
                    doc_level += 1
            return sorted(set(pages)), doc_level, strings
        finally:
            out.close()

    def warnings(self) -> None:
        w, n = self.report.warnings, self.notes
        scans = [p.index for p in self.pages
                 if p.reason in ("no_text", "ocr_layer") and p.index in self.rasterized]
        if scans:
            w.append(f"{_pages_text(scans)}쪽은 스캔(이미지) 페이지라 OCR로 검사한 뒤 그림으로 다시 만들었습니다 "
                     f"— 글자 선택이 안 되며, 결과를 직접 확인하세요 (scanned page(s) {_pages_text(scans)} "
                     f"were OCR-checked and rebuilt as images; please check them by eye)")
        garbled = [p.index for p in self.pages if p.reason == "garbled"]
        if garbled:
            w.append(f"{_pages_text(garbled)}쪽은 글자 정보를 읽을 수 없어(깨진 글꼴) 그림으로 처리했습니다 "
                     f"(page(s) {_pages_text(garbled)}: text could not be decoded; handled as images)")
        if n.get("fallback"):
            w.append(f"{_pages_text(n['fallback_pages'])}쪽은 글자를 완전히 지울 수 없어 그림으로 바꿨습니다 "
                     f"(page(s) {_pages_text(n['fallback_pages'])}: text could not be removed "
                     f"completely and was rebuilt as an image)")
        if n.get("image_unchecked"):
            k = n["image_unchecked"]
            w.append(f"문서 안 이미지 {k}개는 검사하지 않음 — 그림 속 개인정보는 직접 확인하세요 "
                     f"({k} image(s) inside the document were NOT checked; set [ocr] pdf_images = "
                     f"true to OCR them)")
        if n.get("embedded") or n.get("attachment"):
            k = n.get("embedded", 0) + n.get("attachment", 0)
            w.append(f"첨부 파일/미디어 {k}개를 제거했습니다 (첨부 파일은 검사할 수 없음) "
                     f"({k} attached/embedded file(s) removed: they cannot be checked)")
        if n.get("freetext"):
            w.append(f"개인정보가 든 글상자 주석 {n['freetext']}개를 삭제했습니다 "
                     f"({n['freetext']} free-text annotation(s) containing personal data removed)")
        if n.get("link"):
            w.append(f"메일/전화/파일 링크 또는 개인정보가 든 링크 {n['link']}개를 제거했습니다 "
                     f"({n['link']} mailto:/tel:/file link(s) or links containing personal data "
                     f"removed)")
        if n.get("strings"):
            w.append(f"양식 필드 이름, 레이어 이름, 쪽 번호 표시 등 문서 속 문자열 {n['strings']}곳에서 개인정보를 "
                     f"가렸습니다 (personal data removed from {n['strings']} other PDF object(s): "
                     f"field names, layer names, page labels ...)")
        if n.get("xfa"):
            w.append("XFA 양식 데이터를 제거했습니다 (XFA form data removed)")
        if n.get("ocg"):
            w.append("숨겨진 레이어를 모두 보이게 바꿔 함께 검사했습니다 (hidden layers were made visible "
                     "so they could be checked)")


_PDF_ESCAPES = {"n": b"\n", "r": b"\r", "t": b"\t", "b": b"\b", "f": b"\f",
                "(": b"(", ")": b")", "\\": b"\\"}
_HEX_BODY = re.compile(r"[0-9A-Fa-f]*")
_BINARY_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]")
MAX_SWEEP_CHARS = 4000


def _pdf_string_tokens(src: str) -> list[tuple[int, int, bytes]]:
    """(start, end, raw bytes) of every (literal) and <hex> string in PDF object source."""
    out: list[tuple[int, int, bytes]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "(":
            j, depth, buf = i + 1, 1, bytearray()
            while j < n:
                ch = src[j]
                if ch == "\\" and j + 1 < n:
                    nx = src[j + 1]
                    if nx in _PDF_ESCAPES:
                        buf += _PDF_ESCAPES[nx]
                        j += 2
                    elif nx in "01234567":
                        k = j + 1
                        while k < n and k < j + 4 and src[k] in "01234567":
                            k += 1
                        buf.append(int(src[j + 1:k], 8) & 0xFF)
                        j = k
                    elif nx in "\r\n":
                        j += 3 if src.startswith("\r\n", j + 1) else 2
                    else:
                        j += 1
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if not depth:
                        break
                buf += ch.encode("utf-8")
                j += 1
            out.append((i, j + 1, bytes(buf)))
            i = j + 1
        elif c == "<" and src.startswith("<<", i):
            i += 2
        elif c == "<":
            j = src.find(">", i)
            if j < 0:
                break
            body = "".join(src[i + 1:j].split())
            if _HEX_BODY.fullmatch(body):
                out.append((i, j + 1, bytes.fromhex(body + "0" * (len(body) % 2))))
            i = j + 1
        elif c == "%":
            j = src.find("\n", i)
            i = n if j < 0 else j + 1
        else:
            i += 1
    return out


def _pdf_text(raw: bytes) -> str | None:
    """A PDF string as text (UTF-16 with BOM, UTF-8 as MuPDF writes page labels, else
    PDFDocEncoding ~ Latin-1); None for binary data."""
    try:
        if raw.startswith(b"\xfe\xff"):
            text = raw[2:].decode("utf-16-be")
        elif raw.startswith(b"\xff\xfe"):
            text = raw[2:].decode("utf-16-le")
        elif raw.startswith(b"\xef\xbb\xbf"):
            text = raw[3:].decode("utf-8")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
    except UnicodeDecodeError:
        return None
    if _BINARY_CHARS.search(text):
        return None
    return text


def _object_strings(doc: Any) -> list[tuple[int, str, list[tuple[int, int, str]]]]:
    """Per non-stream object: (xref, source, [(start, end, text)]) of its text strings."""
    out = []
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_is_stream(xref):
                continue
            src = doc.xref_object(xref, compressed=True)
        except Exception:  # noqa: BLE001 - free or broken entries
            continue
        if "(" not in src and "<" not in src.replace("<<", ""):
            continue
        toks = []
        for start, end, raw in _pdf_string_tokens(src):
            t = _pdf_text(raw)
            if t and len(t) <= MAX_SWEEP_CHARS and any(ch.isalnum() for ch in t):
                toks.append((start, end, t))
        if toks:
            out.append((xref, src, toks))
    return out


def _draw_overlays(page: Any, overlays: list[tuple[Any, str]], fitz: Any) -> None:
    """Write each replacement ([이름1], 010-****-5678 ...) in white on its black box. When it
    does not fit, it is written as invisible text instead, so copy & paste (and an LLM reading
    the PDF) still sees the placeholder rather than a gap."""
    shape = page.new_shape()
    align = _const(fitz, "TEXT_ALIGN_CENTER", 1)
    for rect, text in overlays:
        font = _overlay_font(text)
        size = _overlay_size(fitz, text, rect, font)
        rc = -1.0
        if size:
            lh = size * OVERLAY_LINE_HEIGHT
            y0 = rect.y0 + max(0.0, (rect.height - lh) / 2)
            box = fitz.Rect(rect.x0, y0, rect.x1, min(rect.y1, y0 + lh) + 0.01)
            try:
                rc = shape.insert_textbox(box, text, fontname=font, fontsize=size,
                                          color=(1, 1, 1), align=align)
            except Exception:  # noqa: BLE001
                rc = -1.0
        if rc < 0:
            try:
                unit = fitz.get_text_length(text, fontname=font, fontsize=1.0) or 1.0
                fs = max(0.5, min(rect.height * 0.8, rect.width * 0.95 / unit))
                shape.insert_text((rect.x0, rect.y1 - 0.2 * rect.height), text, fontname=font,
                                  fontsize=fs, render_mode=3)
            except Exception:  # noqa: BLE001 - the black box alone still hides the value
                pass
    shape.commit()


def _annot_texts(page: Any) -> list[str]:
    """Contents, subjects and authors of the page's annotations plus its form-field values."""
    out: list[str] = []
    try:
        for annot in page.annots():
            info = annot.info or {}
            out += [info.get("content") or "", info.get("subject") or "", info.get("title") or ""]
        for w in page.widgets():
            if isinstance(w.field_value, str):
                out.append(w.field_value)
    except Exception:  # noqa: BLE001
        pass
    return out


def _apply_redactions(page: Any, **kwargs: int) -> None:
    """page.apply_redactions with the options this PyMuPDF version knows (`text` and
    `graphics` arrived in 1.24.x; text removal is the default everywhere)."""
    for drop in ((), ("text",), ("text", "graphics")):
        try:
            page.apply_redactions(**{k: v for k, v in kwargs.items() if k not in drop})
            return
        except TypeError:
            continue
    raise PdfError("이 PyMuPDF 버전은 가리기(redaction)를 지원하지 않습니다: pip install -U pymupdf "
                   "(this PyMuPDF version cannot apply redactions)")


def _covered(r: Any, rects: list[Any]) -> bool:
    for o in rects:
        inter = r & o
        if not inter.is_empty and abs(inter) >= 0.5 * max(abs(r), 1e-6):
            return True
    return False


# ---------------------------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------------------------


def _open(src: Path, fitz: Any) -> tuple[Any, bool]:
    """Open `src`; returns (document, hidden_layers_were_shown)."""
    try:
        data = src.read_bytes()
    except OSError as e:
        raise PdfError(_OPEN_ERROR) from e
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001 - any parser failure
        raise PdfError(_OPEN_ERROR) from e
    if not getattr(doc, "is_pdf", True):
        doc.close()
        raise PdfError(_OPEN_ERROR)
    if doc.needs_pass:
        doc.close()
        raise PdfPasswordError(_PASSWORD_ERROR)
    shown = False
    try:
        cat = doc.pdf_catalog()
        if doc.xref_get_key(cat, "OCProperties")[0] != "null":
            # Content in "off" layers is not extracted (and so would never be redacted).
            # Without /OCProperties every layer is visible.
            doc.xref_set_key(cat, "OCProperties", "null")
            data = doc.tobytes()
            doc.close()
            doc = fitz.open(stream=data, filetype="pdf")
            shown = True
    except PdfError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PdfError(_OPEN_ERROR) from e
    return doc, shown


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------


def deid_pdf_bytes(src: Path, detector: "Detector", anonymizer: "Anonymizer", cfg: "Config",
                   report: Report) -> bytes:
    """De-identify `src` and return the verified output PDF as bytes."""
    fitz = _fitz()
    doc, shown = _open(src, fitz)
    try:
        job = _PdfJob(doc, fitz, detector, anonymizer, cfg, report)
        if shown:
            job.note("ocg")
        job.collect_metadata()
        job.classify()
        job.collect_slots()
        job.batch.run()
        job.apply_text_pages()
        job.apply_widgets()
        job.check_images()
        job.rasterize_scans()
        job.finish_toc()
        job.scrub()
        job.sweep_strings()
        allowed = job.expected()
        data = job.save_bytes()
        pages, doc_level, strings = job.leaks(data, allowed)
        if pages and not doc_level:
            # Fallback: text that redaction could not remove (e.g. /ActualText, odd fonts).
            # The black boxes are already drawn; rebuild those pages from a rendering.
            job.notes["fallback"] = len(pages)
            job.notes["fallback_pages"] = pages  # type: ignore[assignment]
            for pno in pages:
                job.rasterize(pno, ocr=True)
            job.finish_toc()
            data = job.save_bytes()
            pages, doc_level, strings = job.leaks(data, allowed)
        if pages or doc_level or strings:
            n = len(pages) + doc_level + strings
            raise PdfVerifyError(
                f"검증 실패: 지운 개인정보가 결과 PDF에서 여전히 추출됩니다({n}곳). 파일을 저장하지 않았습니다. "
                f"PDF를 인쇄 → 'PDF로 저장'한 사본이나 이미지로 변환한 뒤 다시 시도하세요. "
                f"(verification failed: removed values are still extractable in {n} place(s); "
                f"nothing was written)")
        job.warnings()
        return data
    finally:
        doc.close()


def process(src: Path, dst: Path, detector: "Detector", anonymizer: "Anonymizer",
            cfg: "Config") -> Report:
    """De-identify one PDF into `dst` (written only after verification succeeds)."""
    src, dst = Path(src), Path(dst)
    check_paths(src, dst)
    if src.suffix.lower() not in EXTENSIONS:
        raise PdfError(f"PDF 파일이 아닙니다: {src.suffix} (not a .pdf file)")
    report = Report(source=str(src), output=str(dst))
    data = deid_pdf_bytes(src, detector, anonymizer, cfg, report)
    write_atomic(dst, data)
    return report


__all__ = ("EXTENSIONS", "PdfError", "PdfPasswordError", "PdfVerifyError", "deid_pdf_bytes",
           "process")
