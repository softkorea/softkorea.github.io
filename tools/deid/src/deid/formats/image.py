"""Images and screenshots: OCR -> detect PII on the text lines -> paint over it -> save clean.

Pipeline for one image (:func:`redact_image`):

1. OCR (:mod:`deid.ocr`: Apple Live Text / Vision on macOS when ``ocrmac`` is installed,
   otherwise tesseract ``kor+eng``) gives text lines with per-character boxes.
2. The detector runs over ALL lines of the image at once (``detect_document``), so a name
   recognised in one line (e.g. "환자명: 홍길동") is also found on every other line. OCR often
   breaks Korean words into single syllables ("이 서 연 선 생 님"), so each line is also
   checked in re-joined variants (small gaps closed, split syllables joined); the union of
   everything found is redacted.
3. Every span that is not configured as ``keep`` is covered: the union of its character
   boxes, padded by ~15% of the text height (more when boxes are estimated across a whole
   line), filled solid black (``style="box"``, default) or blurred / pixelated.
4. Re-check (``verify``, default 1 round): the redacted image is OCR'd again and anything
   still detected is covered too (a warning says how many).

Config (``[ocr]``): ``style = "box" | "blur" | "pixelate"``, ``verify = 1`` (0 disables the
re-check), plus the OCR keys documented in :mod:`deid.ocr`. Faces are NOT detected
(``redact_faces`` only produces a warning); handwriting is usually not readable by OCR.

Output files are rebuilt from pixel data only: EXIF (incl. GPS and orientation, which is
applied to the pixels first), XMP, comments and text chunks are dropped; only the ICC colour
profile (and DPI) are kept. The format follows the destination extension; JPEG quality 95.
"""
from __future__ import annotations

import math
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

from .. import entities as E
from ..ocr import Box, OcrLine, ocr_image
from ..types import Report, Span

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image

    from ..anonymizer import Anonymizer
    from ..config import Config
    from ..engine import Detector

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif")
HEIF_EXTS = (".heic", ".heif")
STYLES = ("box", "blur", "pixelate")
_FORMATS = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".bmp": "BMP",
    ".tif": "TIFF", ".tiff": "TIFF", ".heic": "HEIF", ".heif": "HEIF",
}
_MULTI_FRAME_FORMATS = ("TIFF", "PNG", "WEBP")

PAD_RATIO = 0.15            # padding around a span, x text height
EST_PAD_RATIO = 0.3         # extra horizontal padding when char boxes are line estimates
GAP_JOIN_RATIO = 0.3        # OCR words closer than this (x height) are re-joined in a variant
SEEN_OVERLAP = 0.5          # boxes overlapping more than this share are the same finding
COVERED_SHARE = 0.8         # re-check: a finding this much inside a covered area is done

NO_TEXT_WARNING = (
    "텍스트를 찾지 못함 — 손글씨/저해상도일 수 있음. 결과를 직접 확인하세요 "
    "(no text found: handwriting or low resolution? please check the image by eye)"
)
FACES_WARNING = (
    "얼굴 가리기는 지원하지 않습니다 — 사진 속 얼굴은 직접 가리세요 "
    "(face redaction is not supported; cover faces manually)"
)
HEIF_HINT = (
    "HEIC/HEIF 이미지를 처리하려면 pillow-heif가 필요합니다: pip install pillow-heif "
    "(또는 사진 앱에서 JPEG로 내보낸 뒤 다시 시도) / HEIC needs pillow-heif: pip install pillow-heif"
)


def _verify_warning(n: int) -> str:
    return (f"재검사에서 개인정보 {n}건을 추가로 찾아 가렸습니다 — 결과를 직접 확인하세요 "
            f"(re-check found and covered {n} more item(s); please check the output)")


# ---------------------------------------------------------------------- detection on lines


def _is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


def _single_syllable(word: str) -> bool:
    """Exactly one Hangul syllable and no other letters/digits ("서", "명:", "(이")."""
    alnum = [c for c in word if c.isalnum()]
    return len(alnum) == 1 and _is_hangul(alnum[0])


# Word-initial syllables tesseract often misreads in small Korean text -> the surname meant.
OCR_SURNAME_FIXES = {"흥": "홍"}
_FIX_RX = re.compile(r"(?<![\uac00-\ud7a3])(" + "|".join(OCR_SURNAME_FIXES) + r")(?=[\uac00-\ud7a3])")


def _spaced(word: str) -> str:
    """Regex for `word` that tolerates OCR spaces between its characters."""
    return r"\s?".join(map(re.escape, word))


# Name slots: the value after these labels is a name even when OCR garbled it ("환자명: AMG").
_SLOT_STRONG = ("환자명", "환자성명", "환자이름", "수진자명", "성명", "보호자명", "환아명")
_SLOT_COLON = ("이름", "성함", "보호자", "담당의", "주치의", "환자", "name", "patient name",
               "patient")
_SLOT_RX = re.compile(
    r"(?<![\w])(?:(?:" + "|".join(_spaced(w) for w in _SLOT_STRONG) + r")(?=[\s:：;])\s?[:：;]?"
    r"|(?:" + "|".join(_spaced(w) for w in _SLOT_COLON) + r")\s?[:：;])"
    r"\s?(?P<v>[^\s:：;,.()\[\]{}<>|/\\]{2,12})",
    re.IGNORECASE,
)
# Garbled (non-Hangul) word right before an honorific that only follows names ("S32 님").
_HONORIFIC_SLOT_RX = re.compile(
    r"(?<![\w])(?P<v>[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*)\s?(?:님|씨|선생님|환자분|보호자분)")
SLOT_SCORE = 0.55
SLOT_SOURCE = "ocr_slot"


def _slot_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of name-slot values in one OCR line text."""
    out = []
    for rx in (_SLOT_RX, _HONORIFIC_SLOT_RX):
        for m in rx.finditer(text):
            v = m.group("v")
            if any(c.isalpha() for c in v):
                out.append(m.span("v"))
    return out


def _variants(line: OcrLine) -> list[tuple[str, list[int]]]:
    """Texts to run the detector on for one line, each with a map text index -> line.text index.

    Variant 0 is line.text itself; the others drop some of the single spaces between OCR words:
    (1) gaps narrower than GAP_JOIN_RATIO x height, (2) between two single-syllable Hangul
    words, (3) next to any single-syllable Hangul word.
    """
    text = line.text
    base = (text, list(range(len(text))))
    starts = line.word_starts()
    words = line.words
    if starts is None or len(words) < 2:
        return [base]
    h = line.height or 1.0
    rules: list[set[int]] = [set(), set(), set()]
    for i in range(len(words) - 1):
        left, right = words[i], words[i + 1]
        if right.box[0] - left.box[2] < GAP_JOIN_RATIO * h:
            rules[0].add(i)
        if left.text and right.text and _is_hangul(left.text[-1]) and _is_hangul(right.text[0]):
            one_l, one_r = _single_syllable(left.text), _single_syllable(right.text)
            if one_l and one_r:
                rules[1].add(i)
            if one_l or one_r:
                rules[2].add(i)
    out = [base]
    seen = {text}
    for removed in rules:
        if not removed:
            continue
        parts: list[str] = []
        mp: list[int] = []
        for i, w in enumerate(words):
            if i and (i - 1) not in removed:
                parts.append(" ")
                mp.append(starts[i] - 1)
            parts.append(w.text)
            mp.extend(range(starts[i], starts[i] + len(w.text)))
        vt = "".join(parts)
        if vt not in seen:
            seen.add(vt)
            out.append((vt, mp))
    for vt, mp in list(out):
        fixed = _FIX_RX.sub(lambda m: OCR_SURNAME_FIXES[m.group(1)], vt)
        if fixed not in seen:
            seen.add(fixed)
            out.append((fixed, mp))
    return out


def _merge_same_entity(spans: Iterable[Span], text: str) -> list[Span]:
    """Union overlapping spans of the same entity; different entities may still overlap."""
    by_ent: dict[str, list[Span]] = {}
    for sp in spans:
        by_ent.setdefault(sp.entity, []).append(sp)
    out: list[Span] = []
    for ent, group in by_ent.items():
        group.sort(key=lambda s: (s.start, -s.end))
        cur = group[0]
        for sp in group[1:]:
            if sp.start < cur.end:
                if sp.end > cur.end or sp.score > cur.score:
                    end = max(cur.end, sp.end)
                    cur = Span(cur.start, end, ent, text[cur.start:end],
                               max(cur.score, sp.score), cur.source)
            else:
                out.append(cur)
                cur = sp
        out.append(cur)
    out.sort(key=lambda s: (s.start, s.end, s.entity))
    return out


def detect_lines(lines: Sequence[OcrLine], detector: "Detector",
                 context: Sequence[OcrLine] = (), slots: bool = True) -> list[list[Span]]:
    """PII spans per OCR line, as offsets into ``line.text``.

    All lines (plus ``context`` lines, e.g. from an earlier OCR pass, whose own spans are not
    returned) go through ``detector.detect_document`` together so values propagate between
    lines. With `slots`, the word after a name label ("환자명: ...") or before a name-only
    honorific is also reported as PERSON even when OCR garbled it. Same-entity overlaps are
    merged; spans of different entities may overlap.
    """
    every = list(context) + list(lines)
    chunks: list[str] = []
    owners: list[tuple[int, list[int]]] = []
    for idx, line in enumerate(every):
        for vt, mp in _variants(line):
            chunks.append(vt)
            owners.append((idx, mp))
    found = detector.detect_document(chunks) if chunks else []
    n_ctx = len(context)
    cfg = getattr(detector, "cfg", None)
    slots_on = slots and (cfg is None or cfg.is_enabled(E.PERSON))
    allow = set(getattr(cfg, "allowlist", None) or ())
    per_line: list[list[Span]] = [[] for _ in lines]
    for (idx, mp), spans, chunk in zip(owners, found, chunks):
        if idx < n_ctx:
            continue
        line = every[idx]
        hits = [(sp.start, sp.end, sp.entity, sp.score, sp.source) for sp in spans]
        if slots_on:
            for a, b in _slot_spans(chunk):
                if chunk[a:b] not in allow and not any(a < sp.end and sp.start < b for sp in spans):
                    hits.append((a, b, E.PERSON, SLOT_SCORE, SLOT_SOURCE))
        for a, b, ent, score, source in hits:
            s, e = mp[a], mp[b - 1] + 1
            per_line[idx - n_ctx].append(Span(s, e, ent, line.text[s:e], score, source))
    return [_merge_same_entity(sps, ln.text) if sps else []
            for sps, ln in zip(per_line, lines)]


# ---------------------------------------------------------------------- painting


def _overlap_share(a: Box, b: Box) -> float:
    """Intersection area / area of the smaller box (0 when one has no area)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / small if small > 0 else 0.0


def _inside_share(box: Box, region: Box) -> float:
    """Share of `box`'s area that lies inside `region`."""
    w = min(box[2], region[2]) - max(box[0], region[0])
    h = min(box[3], region[3]) - max(box[1], region[1])
    area = (box[2] - box[0]) * (box[3] - box[1])
    if w <= 0 or h <= 0 or area <= 0:
        return 0.0
    return (w * h) / area


def _black(mode: str):
    return {"RGB": (0, 0, 0), "RGBA": (0, 0, 0, 255), "LA": (0, 255)}.get(mode, 0)


class _Redactor:
    """Paints regions onto `img` (in place) and keeps the report counts free of duplicates."""

    def __init__(self, img: "Image.Image", cfg: "Config | None", anonymizer: "Anonymizer | None",
                 report: Report) -> None:
        self.img = img
        self.cfg = cfg
        self.anonymizer = anonymizer
        self.report = report
        style = str(((getattr(cfg, "ocr", None) or {}).get("style") or "box")).lower()
        if style not in STYLES:
            raise ValueError(f"알 수 없는 가림 방식 / unknown redaction style {style!r}; use {STYLES}")
        self.style = style
        self.counted: list[Box] = []
        self.painted: list[Box] = []
        self.kept: list[Box] = []

    def _action(self, span: Span) -> str:
        if self.anonymizer is not None:
            return self.anonymizer.replacement_for(span)[1]
        if self.cfg is not None:
            return self.cfg.action_for(span.entity)
        return "redact"

    def _region(self, box: Box, line: OcrLine) -> tuple[int, int, int, int] | None:
        h = max(box[3] - box[1], 0.5 * line.height, 1.0)
        px = PAD_RATIO * h + (EST_PAD_RATIO * h if line.estimated else 0.0)
        py = PAD_RATIO * h
        x1 = max(0, math.floor(box[0] - max(px, 1.0)))
        y1 = max(0, math.floor(box[1] - max(py, 1.0)))
        x2 = min(self.img.width, math.ceil(box[2] + max(px, 1.0)))
        y2 = min(self.img.height, math.ceil(box[3] + max(py, 1.0)))
        if x2 <= x1 or y2 <= y1:
            return None
        return (x1, y1, x2, y2)

    def _paint(self, region: tuple[int, int, int, int], height: float) -> None:
        from PIL import Image, ImageDraw, ImageFilter

        if self.style == "box":
            ImageDraw.Draw(self.img).rectangle(
                (region[0], region[1], region[2] - 1, region[3] - 1), fill=_black(self.img.mode))
            return
        crop = self.img.crop(region)
        if self.style == "blur":
            radius = max(6.0, 0.75 * height)
            done = crop.filter(ImageFilter.GaussianBlur(radius)).filter(
                ImageFilter.GaussianBlur(radius))
        else:  # pixelate: blocks at least one text-height wide
            block = max(8, int(round(height)))
            small = crop.resize((max(1, crop.width // block), max(1, crop.height // block)),
                                Image.BOX)
            done = small.resize(crop.size, Image.NEAREST)
        self.img.paste(done, region[:2])

    def apply(self, lines: Sequence[OcrLine], spans: Sequence[Sequence[Span]],
              recheck: bool = False) -> int:
        """Count and cover spans; returns how many regions were newly covered.

        `recheck`: spans over places that were already judged (lying inside a covered area,
        or where a `keep` value was found) are ignored - after covering, OCR sees less context
        there (a date next to a covered birth date may then look like a birth date itself).
        Anything sticking out of the covered areas is still covered.
        """
        found: list[tuple[Span, OcrLine, Box]] = []
        for line, sps in zip(lines, spans):
            for sp in sps:
                box = line.span_box(sp.start, sp.end)
                if box is not None:
                    found.append((sp, line, box))
        new = 0
        for sp, line, box in sorted(found, key=lambda f: -f[0].score):
            if recheck and (any(_inside_share(box, p) > COVERED_SHARE for p in self.painted)
                            or any(_overlap_share(box, k) > SEEN_OVERLAP for k in self.kept)):
                continue
            if not any(_overlap_share(box, c) > SEEN_OVERLAP for c in self.counted):
                self.counted.append(box)
                self.report.add(sp.entity)
            if self._action(sp) == "keep":
                self.kept.append(box)
                continue
            region = self._region(box, line)
            if region is None:
                continue
            self._paint(region, max(box[3] - box[1], line.height))
            self.painted.append(region)
            new += 1
        return new


def redact_lines_spans(img: "Image.Image", lines: Sequence[OcrLine],
                       spans: Sequence[Sequence[Span]], cfg: "Config | None" = None,
                       anonymizer: "Anonymizer | None" = None,
                       report: Report | None = None) -> "Image.Image":
    """Cover the given spans (offsets into each ``lines[i].text``) on a copy of `img`.

    For callers that run their own detection (e.g. pdf.py running ``detect_document`` over a
    whole PDF): ``spans[i]`` belongs to ``lines[i]``. Spans whose action is ``keep`` are only
    counted. Counts go into `report` when given. Returns the new image.
    """
    if len(spans) != len(lines):
        raise ValueError("spans must have one list per OCR line")
    out = to_working_image(img)
    if out is img:
        out = img.copy()
    _Redactor(out, cfg, anonymizer, report if report is not None else Report()).apply(lines, spans)
    return out


# ---------------------------------------------------------------------- whole images


def to_working_image(img: "Image.Image") -> "Image.Image":
    """RGB / RGBA / L version of `img` that can be painted on (palette, CMYK, 16-bit ...
    converted). Returns `img` itself when it already is one of those modes."""
    mode = img.mode
    if mode in ("RGB", "RGBA", "L"):
        return img
    if mode == "1":
        return img.convert("L")
    if mode in ("LA", "PA") or (mode == "P" and "transparency" in img.info):
        return img.convert("RGBA")
    if mode in ("I;16", "I;16B", "I;16L", "I"):
        return img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if mode == "F":
        return img.convert("L")
    return img.convert("RGB")


def redact_image(img: "Image.Image", detector: "Detector", cfg: "Config | None" = None,
                 anonymizer: "Anonymizer | None" = None) -> tuple["Image.Image", Report]:
    """OCR `img`, detect PII on all its lines together and cover it. Returns (new image, report).

    The input image is not modified. Raises deid.ocr.OcrError when no OCR engine works
    (never returns an unprocessed image silently).
    """
    cfg = cfg if cfg is not None else getattr(detector, "cfg", None)
    opts = dict(getattr(cfg, "ocr", None) or {})
    report = Report()
    work = to_working_image(img)
    if work is img:
        work = img.copy()
    work.info = {}
    if opts.get("redact_faces"):
        report.warnings.append(FACES_WARNING)
    res = ocr_image(work, cfg)
    report.warnings.extend(w for w in res.warnings if w not in report.warnings)
    if not res.lines:
        report.warnings.append(NO_TEXT_WARNING)
        return work, report
    red = _Redactor(work, cfg, anonymizer, report)
    red.apply(res.lines, detect_lines(res.lines, detector))
    context = list(res.lines)
    extra = 0
    for _ in range(max(0, int(opts.get("verify", 1)))):
        again = ocr_image(work, cfg)
        # No label slots here: a covered name makes the label point at the next word.
        found = red.apply(again.lines, detect_lines(again.lines, detector, context, slots=False),
                          recheck=True)
        context += again.lines
        extra += found
        if not found:
            break
    if extra:
        report.warnings.append(_verify_warning(extra))
    return work, report


def _require_heif() -> None:
    try:
        import pillow_heif  # type: ignore
    except ImportError as e:
        raise RuntimeError(HEIF_HINT) from e
    pillow_heif.register_heif_opener()


def _clean_frame(img: "Image.Image", fmt: str) -> "Image.Image":
    """Fresh image built from pixel data only (no EXIF/XMP/text/comment info)."""
    from PIL import Image

    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        if img.mode == "RGBA":
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.alpha_composite(img)
            img = bg
        img = img.convert("RGB")
    elif fmt in ("WEBP", "HEIF") and img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return Image.frombytes(img.mode, img.size, img.tobytes())


def save_clean(frames: Sequence["Image.Image"], dst: Path, icc: bytes | None = None,
               dpi: tuple | None = None) -> None:
    """Write frames to `dst` (format by extension) atomically, without metadata."""
    fmt = _FORMATS.get(dst.suffix.lower())
    if fmt is None:
        raise ValueError(f"지원하지 않는 출력 형식 / unsupported output type: {dst.suffix}")
    if fmt == "HEIF":
        _require_heif()
    if not frames:
        raise ValueError("no image frames to save")
    clean = [_clean_frame(f, fmt) for f in frames]
    kwargs: dict = {}
    if icc and fmt in ("JPEG", "PNG", "WEBP", "TIFF", "HEIF"):
        kwargs["icc_profile"] = icc
    if dpi and fmt in ("JPEG", "PNG", "TIFF"):
        kwargs["dpi"] = tuple(float(v) for v in dpi[:2])
    if fmt == "JPEG":
        kwargs.update(quality=95, subsampling=0)   # 4:4:4 keeps small text sharp
    elif fmt in ("WEBP", "HEIF"):
        kwargs.update(quality=95)
    elif fmt == "TIFF":
        kwargs.update(compression="tiff_lzw")
    if len(clean) > 1 and fmt in _MULTI_FRAME_FORMATS:
        kwargs.update(save_all=True, append_images=clean[1:])
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".deid-", suffix=dst.suffix, dir=str(dst.parent))
    os.close(fd)
    try:
        clean[0].save(tmp, format=fmt, **kwargs)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def process(src: Path, dst: Path, detector: "Detector", anonymizer: "Anonymizer",
            cfg: "Config") -> Report:
    """De-identify one image file (all frames of a multi-page TIFF) into `dst`."""
    from PIL import Image, ImageOps, ImageSequence

    src, dst = Path(src), Path(dst)
    from .text import check_paths

    check_paths(src, dst)        # never overwrite the original picture
    ext = src.suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ValueError(f"지원하지 않는 이미지 형식 / unsupported image type: {src.suffix}")
    out_fmt = _FORMATS.get(dst.suffix.lower())
    if out_fmt is None:
        raise ValueError(f"지원하지 않는 출력 형식 / unsupported output type: {dst.suffix}")
    if ext in HEIF_EXTS or out_fmt == "HEIF":
        _require_heif()
    report = Report(source=str(src), output=str(dst))
    frames: list[Image.Image] = []
    with Image.open(src) as im:
        icc = im.info.get("icc_profile")
        dpi = im.info.get("dpi")
        n_frames = int(getattr(im, "n_frames", 1) or 1)
        keep_frames = n_frames if out_fmt in _MULTI_FRAME_FORMATS else 1
        for idx, frame in enumerate(ImageSequence.Iterator(im)):
            if idx >= keep_frames:
                break
            page = ImageOps.exif_transpose(frame.copy())
            out, rep = redact_image(page, detector, cfg, anonymizer)
            if n_frames > 1:
                rep.warnings = [f"{idx + 1}쪽 / page {idx + 1}: {w}" for w in rep.warnings]
            report.merge(rep)
            frames.append(out)
        if keep_frames < n_frames:
            report.warnings.append(
                f"{out_fmt}는 여러 쪽을 담을 수 없어 첫 쪽만 저장했습니다 "
                f"(only the first of {n_frames} frames was saved)")
    save_clean(frames, dst, icc=icc, dpi=dpi)
    return report


def redact_screenshot(path_in: str | os.PathLike, path_out: str | os.PathLike,
                      detector: "Detector", anonymizer: "Anonymizer | None" = None,
                      cfg: "Config | None" = None) -> Report:
    """De-identify a screenshot file (e.g. from `screencapture -i`) into `path_out`.

    Same as :func:`process`; `path_out` without a known image extension gets ``.png``.
    """
    dst = Path(path_out)
    if dst.suffix.lower() not in _FORMATS:
        dst = dst.with_name(dst.name + ".png")
    cfg = cfg if cfg is not None else detector.cfg
    if anonymizer is None:
        from ..anonymizer import Anonymizer
        anonymizer = Anonymizer(cfg)
    return process(Path(path_in), dst, detector, anonymizer, cfg)
