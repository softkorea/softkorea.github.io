"""Hangul (한글) HWPX documents: a zip of OWPML XML parts.

* Text: every ``<hp:p>`` of ``Contents/section*.xml`` (and master pages) - including table
  cells, text boxes, headers/footers, footnotes and memos, which are nested paragraphs - is
  rebuilt from its ``<hp:t>`` texts (``<hp:tab/>``, ``<hp:lineBreak/>`` ... inside ``hp:t`` are
  separators; text after them lives in their XML tails), all paragraphs of the file go through
  ``detector.detect_document`` together, and replacements are written into the run where the
  finding starts (same run-aware rewriting as for .docx, matched by local element names). The
  cached line layout (``hp:linesegarray``) of changed paragraphs is dropped so Hangul lays them
  out again. Text between ``deleteBegin``/``deleteEnd`` track-change markers is wiped.
* Field parameters (hyperlink targets such as mailto:, memo parameters) and shape comments are
  de-identified; absolute file paths (linked files) become "file.<ext>"; track-change and memo
  author names become "익명".
* ``Contents/content.hpf`` metadata (title, creator, last saved by, subject, description,
  keywords) is cleared; author names are also registered as known names.
* ``Preview/PrvText.txt`` (plain-text preview of the first page - it would leak) is rebuilt
  from the de-identified text; ``Preview/PrvImage.*`` (thumbnail) is replaced by a blank image
  of the same size.
* ``BinData``: image metadata is stripped; text inside images is checked only with ``[ocr]
  hwpx_images = true`` (or ``docx_images``), otherwise a warning counts the unchecked images.
  Embedded OLE objects are emptied (warning).
* Everything else (version.xml, settings.xml, META-INF/*, header.xml styles) is kept
  byte-identical; the zip keeps its entry order and ``mimetype`` is written first, uncompressed.

Legacy binary .hwp files are refused with a hint to save as HWPX or PDF in Hangul first.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..types import Report
from .docx import (
    ANONYMOUS,
    IMAGE_EXTS,
    DocumentError,
    PackageJob,
    TextSpec,
    ZipPackage,
    blank_image_like,
    encode_image,
    local,
    neutral_file_name,
)
from .text import TextEncoding, TextEncodingError, check_paths, detect_encoding, write_atomic

if TYPE_CHECKING:  # pragma: no cover
    from ..anonymizer import Anonymizer
    from ..config import Config
    from ..engine import Detector

EXTENSIONS = frozenset({".hwpx"})
MIMETYPE = "mimetype"
FONT_EXTS = frozenset({".ttf", ".otf", ".ttc", ".woff", ".woff2", ".fnt"})
PREVIEW_TEXT = "Preview/PrvText.txt"

HWPX_SPEC = TextSpec(
    para=frozenset({"p"}),
    text=frozenset({"t"}),
    field=frozenset(),
    deleted_text=frozenset(),
    deleted_containers=frozenset(),
    delete_begin=frozenset({"deleteBegin"}),
    delete_end=frozenset({"deleteEnd"}),
    separators=(("tab", "\t"), ("lineBreak", "\n"), ("hyphen", "-"), ("nbSpace", " "),
                ("fwSpace", " ")),
    skip=frozenset({"linesegarray"}),
    preserve_space=False,
)

_OPEN_ERROR = ("한글(HWPX) 문서를 열 수 없습니다. 손상되었거나 HWPX 형식이 아닙니다. "
               "(cannot open the HWPX document: damaged or not an .hwpx file)")
_LEGACY_ERROR = ("한글 .hwp(예전 형식) 파일은 직접 처리할 수 없습니다. 한글에서 HWPX 또는 PDF로 저장 후 "
                 "다시 시도하세요('다른 이름으로 저장'). (legacy binary .hwp is not supported: "
                 "save it as .hwpx or .pdf in Hangul first)")
_PASSWORD_ERROR = ("암호가 걸렸거나 배포용으로 보호된 한글 문서입니다. 한글에서 보호를 해제하고 다시 저장한 뒤 "
                   "시도하세요. (the HWPX document is password-protected or encrypted)")

_SECTION = re.compile(r"Contents/section(\d+)\.xml")
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|file:)", re.I)


def _natural(name: str) -> tuple[int, str]:
    m = _SECTION.fullmatch(name)
    return (int(m.group(1)) if m else 10**9, name)


class _HwpxJob(PackageJob):
    open_error = _OPEN_ERROR

    def run(self) -> None:
        names = self.pkg.names
        mt = (self.pkg.get(MIMETYPE) or b"").strip()
        sections = sorted((n for n in names if _SECTION.fullmatch(n)), key=_natural)
        if not sections or (mt and not mt.startswith(b"application/hwp")):
            raise DocumentError(_OPEN_ERROR)
        manifest = self.pkg.get("META-INF/manifest.xml") or b""
        if b"encryption-data" in manifest or b"encrypt" in manifest.lower():
            raise DocumentError(_PASSWORD_ERROR)
        others = sorted(n for n in names if n.startswith("Contents/") and n.endswith(".xml")
                        and n not in sections and n != "Contents/header.xml")
        self.text_parts = [*sections, *others]
        self.sections = sections
        for name in self.text_parts:
            try:
                self.tree(name)
            except DocumentError as e:
                raise DocumentError(_PASSWORD_ERROR if b"<" not in (self.pkg.get(name) or b"")[:64]
                                    else _OPEN_ERROR) from e
        self._people(scrub=False)
        for name in self.text_parts:
            self.add_paragraphs(name)
            self._params(name)
            self.leaf_slots(name)          # equation scripts, unknown structures
        self.batch.run()
        self._people(scrub=True)
        self._scrub_hpf()
        self._scrub_paths()
        n = self.residual()
        if n:
            self.note("residual", n)
        self._drop_layout_cache()
        self.save_trees()
        self._preview()
        self._bindata()
        self.common_warnings("hwpx_images")
        if any(n.startswith("Scripts/") and self.pkg.get(n) for n in self.pkg.names):
            self.report.warnings.append(
                "문서 매크로(스크립트)는 검사하지 않고 그대로 두었습니다. 코드 안에 개인정보가 없는지 직접 "
                "확인하세요 (HWPX scripts were kept as-is and NOT scanned)")

    # -- collection --------------------------------------------------------------------------

    def _params(self, name: str) -> None:
        for el in self.tree(name).iter():
            lname = local(el.tag)
            if lname == "stringParam":
                if "author" in (el.get("name") or "").lower():
                    continue          # handled by _people
                self.element_slot(name, el)
            elif lname in ("shapeComment", "comment") and len(el) == 0:
                self.element_slot(name, el)

    def _author_parts(self) -> list[str]:
        parts = list(self.text_parts)
        if "Contents/header.xml" in self.pkg.names:
            parts.append("Contents/header.xml")
        return parts

    def _people(self, scrub: bool) -> None:
        """Author names (track changes, memos) and document creator: registered as known
        names first, replaced by "익명" after detection."""
        if not scrub and "Contents/content.hpf" in self.pkg.names:
            for el in self.tree("Contents/content.hpf").iter():
                lname = local(el.tag).lower()
                key = (el.get("name") or "").lower()
                if lname == "creator" or key in ("creator", "lastsaveby"):
                    self.known_person(el.text)
        for name in self._author_parts():
            for el in self.tree(name).iter():
                lname = local(el.tag).lower()
                if not lname:
                    continue
                author_el = "author" in lname or (
                    lname == "stringparam" and "author" in (el.get("name") or "").lower())
                if author_el:
                    for key in [k for k in el.attrib if local(k) == "name" and "author" in lname]:
                        self._author_value(name, el, key, scrub)
                    if el.text and el.text.strip() and len(el) == 0:
                        self.handled.add((el, "text"))
                        self._alive.append(el)
                        if scrub:
                            if el.text != ANONYMOUS:
                                el.text = ANONYMOUS
                                self.dirty.add(name)
                        else:
                            self.known_person(el.text)
                for key in list(el.attrib):
                    lk = local(key).lower()
                    if "author" in lk and not lk.endswith(("id", "idref", "ref")):
                        self._author_value(name, el, key, scrub)

    def _author_value(self, name: str, el: Any, key: str, scrub: bool) -> None:
        value = el.get(key)
        if not value:
            return
        if scrub:
            if value != ANONYMOUS:
                el.set(key, ANONYMOUS)
                self.dirty.add(name)
        else:
            self.known_person(value)

    # -- scrubbing ---------------------------------------------------------------------------

    def _scrub_hpf(self) -> None:
        name = "Contents/content.hpf"
        if name not in self.pkg.names:
            return
        root = self.tree(name)
        for el in root.iter():
            if local(el.tag) != "metadata":
                continue
            for child in el:
                cname = local(child.tag).lower()
                key = (child.get("name") or "").lower()
                if not cname or cname == "language" or "date" in key or "date" in cname:
                    continue
                if child.text or len(child):
                    child.text = None
                    for sub in list(child):
                        child.remove(sub)
                    self.dirty.add(name)

    def _scrub_paths(self) -> None:
        """Absolute file paths (linked files, link info) often contain a user name."""
        parts = [*self._author_parts()]
        if "Contents/content.hpf" in self.pkg.names:
            parts.append("Contents/content.hpf")
        for name in parts:
            for el in self.tree(name).iter():
                for key in list(el.attrib):
                    if local(key).lower() in ("path", "href"):
                        value = el.get(key) or ""
                        if _ABS_PATH.match(value):
                            el.set(key, neutral_file_name(value))
                            self.dirty.add(name)
                            self.note("link")

    def _drop_layout_cache(self) -> None:
        for name, pt in self.paras:
            owner = getattr(pt, "owner", None)
            if owner is None or not pt.changed:
                continue
            for child in list(owner):
                if local(child.tag) == "linesegarray":
                    owner.remove(child)
                    self.dirty.add(name)

    # -- previews and binary data ------------------------------------------------------------

    def _preview(self) -> None:
        names = self.pkg.names
        if PREVIEW_TEXT in names:
            data = self.pkg.get(PREVIEW_TEXT) or b""
            try:
                enc = detect_encoding(data)
                old = enc.decode(data)
            except TextEncodingError:
                enc, old = TextEncoding("utf-8"), ""
            nl = "\r\n" if "\r\n" in old or not old else "\n"
            lines = [pt.current() for n, pt in self.paras if n in self.sections]
            text = nl.join(line for line in lines if line.strip())
            limit = max(len(old), 0)
            text = text[:limit]
            try:
                self.pkg.set(PREVIEW_TEXT, enc.encode(text))
            except UnicodeEncodeError:
                self.pkg.set(PREVIEW_TEXT, enc.encode(text, errors="replace"))
        for name in names:
            if name.startswith("Preview/") and posixpath.splitext(name)[1].lower() in IMAGE_EXTS:
                ext = posixpath.splitext(name)[1].lower()
                blank = blank_image_like(self.pkg.get(name) or b"", ext)
                if blank is None:
                    from PIL import Image

                    blank = encode_image(Image.new("RGB", (1, 1), (255, 255, 255)), ext)
                self.pkg.set(name, blank)

    def _bindata(self) -> None:
        names = [n for n in self.pkg.names if n.startswith("BinData/") and not n.endswith("/")]
        images = [n for n in names if posixpath.splitext(n)[1].lower() in IMAGE_EXTS]
        opts = self.cfg.ocr or {}
        option = bool(opts.get("hwpx_images", opts.get("docx_images", False)))
        self.image_parts(images, option)
        for name in names:
            ext = posixpath.splitext(name)[1].lower()
            if name in images or ext in FONT_EXTS:
                continue
            if self.pkg.get(name):
                self.pkg.set(name, b"")
                self.note("embedded")


def _stored_first(pkg: ZipPackage) -> None:
    """The OCF container format wants 'mimetype' as the first entry."""
    infos = pkg.infos
    for i, info in enumerate(infos):
        if info.filename == MIMETYPE and i:
            infos.insert(0, infos.pop(i))
            break


def process(src: Path, dst: Path, detector: "Detector", anonymizer: "Anonymizer",
            cfg: "Config") -> Report:
    """De-identify one HWPX document into `dst`."""
    src, dst = Path(src), Path(dst)
    check_paths(src, dst)
    ext = src.suffix.lower()
    if ext == ".hwp":
        raise DocumentError(_LEGACY_ERROR)
    if ext not in EXTENSIONS:
        raise DocumentError(f"한글 HWPX 문서가 아닙니다: {src.suffix} (not an .hwpx file)")
    report = Report(source=str(src), output=str(dst))
    pkg = ZipPackage.read(src, _OPEN_ERROR, _LEGACY_ERROR)
    _HwpxJob(pkg, detector, anonymizer, cfg, report, HWPX_SPEC).run()
    _stored_first(pkg)
    write_atomic(dst, pkg.to_bytes(stored=(MIMETYPE,)))
    return report


__all__ = ("EXTENSIONS", "HWPX_SPEC", "process")
