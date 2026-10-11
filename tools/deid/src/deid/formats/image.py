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
4. Re-check (``verify``, default 1 round): when something was covered, the redacted image is
   OCR'd again and anything still detected is covered too (a warning says how many). An image
   in which nothing was covered is unchanged, so it is not OCR'd twice.
5. Sideways text: when the OCR result looks poor (or comes from Apple's engines), tesseract's
   orientation detection is asked; a picture taken or scanned sideways / upside down is also
   checked turned upright (``orientation = "auto" | true | false``).
6. Messenger screenshots: a line holding nothing but a name-shaped word (3 syllables starting
   with a surname, e.g. the chat title or a sender label) is covered when it appears twice or
   in the title bar (``bare_names = true``).
7. Barcodes and QR / 2-D codes (labels, wristbands, prescriptions) encode identifiers: they are
   found by their bar / module pattern (and by ``zxing-cpp`` when installed) and covered
   (``barcodes = "cover" | "warn" | "off"``), counted as OTHER.

Config (``[ocr]``): ``style = "box" | "blur" | "pixelate"``, ``verify = 1`` (0 disables the
re-check), the keys above, plus the OCR keys documented in :mod:`deid.ocr`. Faces are NOT
detected (``redact_faces`` only produces a warning); handwriting is usually not readable by OCR.
Decompression bombs: images over ``max_image_pixels`` (100 M per frame), files over
``max_total_pixels`` (1.5 G over all frames) or ``max_frames`` (300) are refused before
decoding.

Output files are rebuilt from pixel data only: EXIF (incl. GPS and orientation, which is
applied to the pixels first), XMP, comments and text chunks are dropped; only the ICC colour
profile (and DPI) are kept, and colour data under fully transparent pixels is blanked. The
format follows the destination extension; JPEG quality 95. Multi-page TIFFs are written page
by page (pages are not all kept in memory).
"""
from __future__ import annotations

import functools
import math
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

from .. import entities as E
from ..ocr import Box, OcrLine, OcrWord, detect_rotation, flatten_rgb, ocr_image
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
# Estimated character positions drift with the distance from where they are anchored (the
# ends of the line box, or of the word when word positions came from the ink): measured up to
# ~7.5% of that distance with real fonts, so estimated spans get this share as extra padding.
DRIFT_RATIO = 0.1
GAP_JOIN_RATIO = 0.3        # OCR words closer than this (x height) are re-joined in a variant
SEEN_OVERLAP = 0.5          # boxes overlapping more than this share are the same finding
COVERED_SHARE = 0.8         # re-check: a finding this much inside a covered area is done
GOOD_CONF = 75              # mean tesseract word confidence of upright, readable text
HIDDEN_ALPHA = 8            # pixels this transparent are invisible: their colour is blanked

MAX_IMAGE_PIXELS = 100_000_000      # per frame (A3 at 600 dpi is ~70 M)
MAX_TOTAL_PIXELS = 1_500_000_000    # all frames of one file (~170 A4 pages at 300 dpi)
MAX_FRAMES = 300

NO_TEXT_WARNING = (
    "텍스트를 찾지 못함 — 손글씨/저해상도일 수 있음. 결과를 직접 확인하세요 "
    "(no text found: handwriting or low resolution? please check the image by eye)"
)
FACES_WARNING = (
    "얼굴 가리기는 지원하지 않습니다 — 사진 속 얼굴은 직접 가리세요 "
    "(face redaction is not supported; cover faces manually)"
)
HEIF_HINT = (
    "HEIC/HEIF 이미지를 처리하려면 pillow-heif가 필요합니다: pipx inject deid-kit pillow-heif "
    "(pipx로 설치하지 않았다면 python3 -m pip install pillow-heif; 또는 사진 앱에서 JPEG로 내보낸 뒤 "
    "다시 시도) / HEIC needs pillow-heif: pipx inject deid-kit pillow-heif (pip install pillow-heif)"
)


class ImageTooLargeError(ValueError):
    """The image (or multi-page file) exceeds the pixel / page budget (decompression bomb)."""


def _budget(opts: dict, key: str, default: int) -> int:
    try:
        v = int(opts.get(key) or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def check_pixels(size: tuple[int, int], cfg: "Config | None" = None, frames: int = 1,
                 total: int | None = None) -> None:
    """Raise ImageTooLargeError when one frame of `size`, `frames` frames or `total` pixels
    exceed the budget (``[ocr] max_image_pixels / max_frames / max_total_pixels``)."""
    opts = dict(getattr(cfg, "ocr", None) or {})
    w, h = (int(v) for v in size)
    if w * h > _budget(opts, "max_image_pixels", MAX_IMAGE_PIXELS):
        raise ImageTooLargeError(
            f"이미지가 너무 큽니다({w}x{h}, {w * h / 1e6:.0f}M 화소) — 해상도를 낮춰 다시 저장한 뒤 "
            f"시도하세요 (image too large: {w}x{h} pixels; [ocr] max_image_pixels)")
    if frames > _budget(opts, "max_frames", MAX_FRAMES):
        raise ImageTooLargeError(
            f"쪽 수가 너무 많습니다({frames}쪽) — 파일을 나눠서 처리하세요 "
            f"(too many pages: {frames}; split the file; [ocr] max_frames)")
    if total is not None and total > _budget(opts, "max_total_pixels", MAX_TOTAL_PIXELS):
        raise ImageTooLargeError(
            f"전체 화소 수가 너무 많습니다({total / 1e6:.0f}M) — 파일을 나누거나 300dpi 이하로 다시 "
            f"스캔하세요 (too many pixels in total: {total / 1e6:.0f} M; split the file or scan "
            f"at 300 dpi; [ocr] max_total_pixels)")


def clear_hidden_colour(img: "Image.Image") -> "Image.Image":
    """Blank (white) the colour of (almost) fully transparent pixels, in place: OCR checks the
    picture composited onto white, but a tool that ignores the alpha channel would show the
    colour data stored under transparency ("erased to transparent" text)."""
    if img.mode not in ("RGBA", "LA"):
        return img
    hidden = img.getchannel("A").point(lambda v: 255 if v < HIDDEN_ALPHA else 0)
    if hidden.getbbox() is not None:
        img.paste((255, 255, 255, 0) if img.mode == "RGBA" else (255, 0), (0, 0) + img.size,
                  hidden)
    return img


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


# Messenger screenshots: the chat partner's name stands alone (title bar, sender label).
BARE_NAME_SCORE = 0.55
BARE_NAME_SOURCE = "ocr_bare_name"
TITLE_BAND = 0.15           # top share of the image that holds a title bar
_VERB_FINAL = frozenset("요죠다까네게래니냐자")
# Words seen alone in UI / EMR screenshots that look like surname + 2 syllables.
_BARE_NAME_STOP = frozenset("""
상병명 주상병 부상병 기왕력 이메일 모니터 동영상 고양이 강아지 백화점 도서관 우체국 소방서 연구소
채팅방 선물함 설정창 알림창 공지글 안내창 전송함 보관함 사진첩 오픈채팅 하이패스 신분증 주민증
가족관계 주민번호 전화번호 연락처 진단명 처방명 수술명 검사명 약품명 성명란 서명란 인증서
""".split())


def _names_module():
    try:
        from ..recognizers import names
    except Exception:  # noqa: BLE001 - the heuristic is a bonus
        return None
    return names


def _name_shaped(flat: str) -> bool:
    """A bare word that looks like a full Korean name: surname + 2-syllable given name."""
    names = _names_module()
    if names is None or not all(_is_hangul(c) for c in flat):
        return False
    if len(flat) == 3:
        ok = flat[0] in names.SURNAMES1
    elif len(flat) == 4:
        ok = flat[:2] in names.SURNAMES2
    else:
        return False
    stop = names.STOPWORDS
    if not ok or flat in stop or flat in _BARE_NAME_STOP or flat[:2] in stop or flat[-1] in _VERB_FINAL:
        return False
    if _near_stopword(flat, stop):
        return False      # an OCR misreading of a common word ("고혈안" for 고혈압)
    if flat[-1] in getattr(names, "_BAD_FINALS", ()) or flat[-1] in getattr(names, "_NONNAME_FINALS", ()):
        return False
    rejected = getattr(names, "_rejected", None)
    try:
        return not (rejected is not None and rejected(flat, flat))
    except Exception:  # noqa: BLE001 - signature changed: the checks above stand
        return True


@functools.lru_cache(maxsize=4)
def _stop_patterns(stop: frozenset) -> frozenset:
    """'고혈?' / '고?압' / '?혈압' keys of every same-length stopword and UI word."""
    out = set()
    for w in stop | _BARE_NAME_STOP:
        if len(w) in (3, 4):
            out.update(w[:i] + "?" + w[i + 1:] for i in range(len(w)))
    return frozenset(out)


def _near_stopword(flat: str, stop: frozenset) -> bool:
    keys = _stop_patterns(stop)
    return any(flat[:i] + "?" + flat[i + 1:] in keys for i in range(len(flat)))


def _bare_candidate(line: OcrLine) -> tuple[int, int, str] | None:
    """(start, end, name) when the whole line is one name-shaped word (OCR may split it into
    syllables: "하 은 별"); stray punctuation from icons ("<", "=") is ignored."""
    starts = line.word_starts()
    if starts is None:
        return None
    keep = [(st, w.text) for st, w in zip(starts, line.words) if any(c.isalnum() for c in w.text)]
    if not keep or len(keep) > 4:
        return None
    flat = "".join(t for _, t in keep).strip(".,:;!?·'\"()[]")
    if not _name_shaped(flat):
        return None
    a = keep[0][0] + (len(keep[0][1]) - len(keep[0][1].lstrip(".,:;!?·'\"()[]")))
    last_st, last = keep[-1]
    b = last_st + len(last.rstrip(".,:;!?·'\"()[]"))
    return a, b, flat


def _real_word(w: OcrWord) -> bool:
    """Text, not icon noise ('Q', '=', '<' read from a search / menu / back button)."""
    alnum = [c for c in w.text if c.isalnum()]
    if any(_is_hangul(c) for c in alnum):
        return w.conf < 0 or w.conf >= 50
    return len(alnum) >= 2 and (w.conf < 0 or w.conf >= 60)


def _isolated(box: Box, others: Sequence[OcrLine]) -> bool:
    """No other text anywhere on the same row: a messenger title / sender label row holds
    only the name, while a table cell or a form label shares its row with other cells."""
    h = max(box[3] - box[1], 1.0)
    for line in others:
        for w in line.words:
            if not _real_word(w):
                continue
            wh = max(w.box[3] - w.box[1], 1.0)
            if min(box[3], w.box[3]) - max(box[1], w.box[1]) < 0.5 * min(h, wh):
                continue                                  # another row
            ww = max(w.box[2] - w.box[0], 1e-6)
            if min(box[2], w.box[2]) - max(box[0], w.box[0]) > 0.5 * ww:
                continue                                  # the same text (another OCR pass)
            return False
    return True


def bare_name_spans(lines: Sequence[OcrLine], size: tuple[int, int] | None = None
                    ) -> dict[int, list[tuple[int, int]]]:
    """line index -> name spans for lines that hold only a name-shaped word, stand alone on
    their row, and either repeat (title + sender label) or sit in the title bar."""
    cands = []
    for idx, line in enumerate(lines):
        hit = _bare_candidate(line)
        if hit is None:
            continue
        box = line.span_box(hit[0], hit[1])
        if box is None or not _isolated(box, [ln for i, ln in enumerate(lines) if i != idx]):
            continue
        cands.append((idx, hit, box))
    if not cands:
        return {}
    if size is None:
        size = (max(ln.box[2] for ln in lines), max(ln.box[3] for ln in lines))
    width, height = max(size[0], 1), max(size[1], 1)
    out: dict[int, list[tuple[int, int]]] = {}
    for idx, (a, b, flat), box in cands:
        places: list[Box] = []
        for _, hit, other in cands:
            if hit[2] == flat and not any(_overlap_share(other, p) > SEEN_OVERLAP for p in places):
                places.append(other)
        cx = (box[0] + box[2]) / 2
        title = box[3] <= TITLE_BAND * height and abs(cx - width / 2) <= 0.25 * width
        if len(places) >= 2 or title:
            out.setdefault(idx, []).append((a, b))
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
                 context: Sequence[OcrLine] = (), slots: bool = True,
                 size: tuple[int, int] | None = None) -> list[list[Span]]:
    """PII spans per OCR line, as offsets into ``line.text``.

    All lines (plus ``context`` lines, e.g. from an earlier OCR pass, whose own spans are not
    returned) go through ``detector.detect_document`` together so values propagate between
    lines. With `slots`, the word after a name label ("환자명: ...") or before a name-only
    honorific is also reported as PERSON even when OCR garbled it, and so is a name standing
    alone on its line (messenger title / sender label, see :func:`bare_name_spans`; `size` is
    the image size). Same-entity overlaps are merged; spans of different entities may overlap.
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
    opts = dict(getattr(cfg, "ocr", None) or {})
    if slots_on and _on(opts.get("bare_names", True)):
        for idx, found_spans in bare_name_spans(lines, size).items():
            for a, b in found_spans:
                text = lines[idx].text[a:b]
                if text in allow or text.replace(" ", "") in allow:
                    continue
                if any(sp.entity == E.PERSON and sp.start <= a and b <= sp.end
                       for sp in per_line[idx]):
                    continue
                per_line[idx].append(Span(a, b, E.PERSON, text, BARE_NAME_SCORE,
                                          BARE_NAME_SOURCE))
    return [_merge_same_entity(sps, ln.text) if sps else []
            for sps, ln in zip(per_line, lines)]


def _on(value: object, default: bool = True) -> bool:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("no", "off", "false", "0", "none"):
            return False
        if v in ("yes", "on", "true", "1"):
            return True
        return default
    return default if value is None else bool(value)


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
        if line.estimated:
            px += DRIFT_RATIO * _anchor_distance(box, line)
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


def _anchor_distance(box: Box, line: OcrLine) -> float:
    """Distance from the middle of `box` to the nearer end of what its estimated character
    boxes were spread across: the word (ink-aligned lines) or the whole line box."""
    cx = (box[0] + box[2]) / 2
    anchor = line.box
    if getattr(line, "aligned", False):
        for w in line.words:
            if w.box[0] - 1 <= cx <= w.box[2] + 1:
                anchor = w.box
                break
    return max(0.0, min(cx - anchor[0], anchor[2] - cx))


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


# ---------------------------------------------------------------------- barcodes / QR codes

CODE_CELL = 16              # analysis cell (px) - at least a couple of bars / modules wide
CODE_MIN_SIDE = 40          # px: smaller marks cannot hold a readable code
CODE_MIN_RUNS = 16          # bar edges along a row of a 1-D code (8 bars)
CODE_MIN_MODULE_RUNS = 8    # module edges along rows and columns of a 2-D code (QR v1: 21)
CODE_MAX_MID = 0.2          # share of mid-grey pixels: printed codes are black on white
CODES_WARNING = (
    "바코드/QR 코드 {n}개를 가렸습니다 — 환자 정보가 담겨 있을 수 있습니다 "
    "({n} barcode/QR code(s) covered: they can encode identifiers)"
)
CODES_FOUND_WARNING = (
    "바코드/QR 코드로 보이는 부분 {n}개가 있습니다 — 환자 정보가 담겨 있을 수 있으니 직접 확인하세요 "
    "({n} barcode/QR-like area(s) found and NOT covered ([ocr] barcodes = \"warn\"); check them)"
)


def _otsu(gray: "Image.Image") -> int:
    hist = gray.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * v for i, v in enumerate(hist))
    w_b = s_b = 0
    best, thr = -1.0, 127
    for t in range(256):
        w_b += hist[t]
        if not w_b:
            continue
        w_f = total - w_b
        if not w_f:
            break
        s_b += t * hist[t]
        m_b, m_f = s_b / w_b, (sum_all - s_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, thr = var, t
    return thr


def _text_word(w: OcrWord) -> bool:
    """An OCR word that is real text (not OCR noise read from bars or modules)."""
    alnum = [c for c in w.text if c.isalnum()]
    if any(_is_hangul(c) for c in alnum):
        return len(alnum) >= 1 and (w.conf < 0 or w.conf >= 60)
    return len(alnum) >= 3 and (w.conf < 0 or w.conf >= 80) and not set(w.text) <= set("|Il1![]i")


def find_codes(img: "Image.Image", lines: Sequence[OcrLine] = ()) -> list[Box]:
    """Boxes of barcode / 2-D code like areas: black-on-white regions full of bar edges
    (1-D: edges along rows only; 2-D: along rows and columns) that are not OCR'd text."""
    from PIL import ImageChops

    gray = flatten_rgb(img).convert("L")
    width, height = gray.size
    if width < CODE_MIN_SIDE or height < CODE_MIN_SIDE:
        return []
    thr = _otsu(gray)
    ink = gray.point(lambda v: 255 if v <= thr else 0)
    edge_h = ImageChops.difference(ink, ImageChops.offset(ink, 1, 0))
    edge_v = ImageChops.difference(ink, ImageChops.offset(ink, 0, 1))
    mid = gray.point(lambda v: 255 if 70 <= v <= 185 else 0)
    c = max(CODE_CELL, math.ceil(math.sqrt(width * height / 60_000)))
    fh, fv, fi, fm = (m.reduce(c).tobytes() for m in (edge_h, edge_v, ink, mid))
    cols, rows = math.ceil(width / c), math.ceil(height / c)
    text = bytearray(cols * rows)
    for line in lines:
        for w in line.words:
            if not _text_word(w):
                continue
            x0, y0 = max(0, int(w.box[0] // c)), max(0, int(w.box[1] // c))
            x1, y1 = min(cols - 1, int(w.box[2] // c)), min(rows - 1, int(w.box[3] // c))
            for y in range(y0, y1 + 1):
                text[y * cols + x0:y * cols + x1 + 1] = b"\x01" * (x1 - x0 + 1)
    cand = bytearray(cols * rows)
    for i in range(cols * rows):
        if (not text[i] and fm[i] <= 0.3 * 255 and 0.1 * 255 <= fi[i] <= 0.9 * 255
                and max(fh[i], fv[i]) >= 0.03 * 255):
            cand[i] = 1
    out: list[Box] = []
    seen = bytearray(cols * rows)
    for start in range(cols * rows):
        if not cand[start] or seen[start]:
            continue
        stack, members = [start], []
        seen[start] = 1
        while stack:
            i = stack.pop()
            members.append(i)
            y, x = divmod(i, cols)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < rows and 0 <= nx < cols:
                        j = ny * cols + nx
                        if cand[j] and not seen[j]:
                            seen[j] = 1
                            stack.append(j)
        xs = [i % cols for i in members]
        ys = [i // cols for i in members]
        bx0, by0 = min(xs) * c, min(ys) * c
        bx1, by1 = min(width, (max(xs) + 1) * c), min(height, (max(ys) + 1) * c)
        bw, bh = bx1 - bx0, by1 - by0
        n = len(members)
        eh = sum(fh[i] for i in members) / (255 * n)
        ev = sum(fv[i] for i in members) / (255 * n)
        dark = sum(fi[i] for i in members) / (255 * n)
        grey = sum(fm[i] for i in members) / (255 * n)
        fill = n / ((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1))
        if grey > CODE_MAX_MID or fill < 0.6 or bw < CODE_MIN_SIDE:
            continue
        bars = (eh >= 0.04 and ev <= 0.5 * eh and eh * bw >= CODE_MIN_RUNS
                and 0.15 <= dark <= 0.85 and bh >= 0.5 * CODE_MIN_SIDE)
        matrix = (eh >= 0.03 and ev >= 0.03 and 0.33 <= ev / eh <= 3.0 and 0.3 <= dark <= 0.7
                  and eh * bw >= CODE_MIN_MODULE_RUNS and ev * bh >= CODE_MIN_MODULE_RUNS
                  and bh >= CODE_MIN_SIDE and max(bw, bh) <= 6 * min(bw, bh))
        if bars or matrix:
            out.append((float(bx0), float(by0), float(bx1), float(by1)))
    return out + _zxing_codes(img)


def _zxing_codes(img: "Image.Image") -> list[Box]:
    """Codes the optional zxing-cpp decoder finds (pip install zxing-cpp); [] without it."""
    try:
        import zxingcpp  # type: ignore
    except Exception:  # noqa: BLE001 - optional
        return []
    out: list[Box] = []
    try:
        for res in zxingcpp.read_barcodes(flatten_rgb(img)):
            pos = res.position
            pts = [(p.x, p.y) for p in (pos.top_left, pos.top_right, pos.bottom_right,
                                        pos.bottom_left)]
            out.append((float(min(p[0] for p in pts)), float(min(p[1] for p in pts)),
                        float(max(p[0] for p in pts)), float(max(p[1] for p in pts))))
    except Exception:  # noqa: BLE001 - decoder problems: the pattern search still ran
        return out
    return out


def _cover_codes(work: "Image.Image", lines: Sequence[OcrLine], opts: dict,
                 report: Report, anonymizer: "Anonymizer | None", cfg: "Config | None") -> int:
    mode = str(opts.get("barcodes", "cover")).strip().lower()
    if mode in ("off", "false", "no", "0", "none"):
        return 0
    boxes: list[Box] = []
    for b in find_codes(work, lines):
        if not any(_overlap_share(b, o) > SEEN_OVERLAP for o in boxes):
            boxes.append(b)
    if not boxes:
        return 0
    if mode == "warn" or (cfg is not None and cfg.action_for(E.OTHER) == "keep"):
        report.warnings.append(CODES_FOUND_WARNING.format(n=len(boxes)))
        return 0
    from PIL import ImageDraw

    draw = ImageDraw.Draw(work)
    for b in boxes:
        pad = 4
        draw.rectangle((max(0, b[0] - pad), max(0, b[1] - pad),
                        min(work.width, b[2] + pad) - 1, min(work.height, b[3] + pad) - 1),
                       fill=_black(work.mode))
        report.add(E.OTHER)
    report.warnings.append(CODES_WARNING.format(n=len(boxes)))
    return len(boxes)


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
    (never returns an unprocessed image silently) and ImageTooLargeError over the pixel budget.
    """
    cfg = cfg if cfg is not None else getattr(detector, "cfg", None)
    opts = dict(getattr(cfg, "ocr", None) or {})
    report = Report()
    check_pixels(img.size, cfg)
    work = to_working_image(img)
    if work is img:
        work = img.copy()
    work.info = {}
    clear_hidden_colour(work)
    if opts.get("redact_faces"):
        report.warnings.append(FACES_WARNING)
    res = ocr_image(work, cfg)
    report.warnings.extend(w for w in res.warnings if w not in report.warnings)
    lines = list(res.lines)
    extra = _redact_pass(work, res.lines, detector, cfg, anonymizer, report, opts)
    rotation = _rotation(work, res, cfg, opts)
    if rotation:
        # Sideways / upside-down picture: check it turned upright as well.
        from PIL import Image

        turn = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_90}
        back = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_270}
        upright = work.transpose(turn[rotation])
        res2 = ocr_image(upright, cfg)
        report.warnings.extend(w for w in res2.warnings if w not in report.warnings)
        report.warnings.append(_rotation_warning(rotation))
        if res2.lines:
            extra += _redact_pass(upright, res2.lines, detector, cfg, anonymizer, report, opts)
            work = upright.transpose(back[rotation])
            lines += _rotate_lines_back(res2.lines, rotation, upright.size)
    codes = _cover_codes(work, lines, opts, report, anonymizer, cfg)
    if not lines and not codes:
        report.warnings.append(NO_TEXT_WARNING)
    if extra:
        report.warnings.append(_verify_warning(extra))
    return work, report


def _redact_pass(work: "Image.Image", lines: Sequence[OcrLine], detector: "Detector",
                 cfg: "Config | None", anonymizer: "Anonymizer | None", report: Report,
                 opts: dict) -> int:
    """Cover what is found on `lines` (an OCR of `work`), then re-check. Returns how many
    regions the re-check covered."""
    if not lines:
        return 0
    red = _Redactor(work, cfg, anonymizer, report)
    painted = red.apply(lines, detect_lines(lines, detector, size=work.size))
    if not painted:
        return 0     # nothing covered: the picture is unchanged, a re-check would repeat this
    context = list(lines)
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
    return extra


def _rotation(work: "Image.Image", res, cfg: "Config | None", opts: dict) -> int:
    """Clockwise rotation that turns `work` upright (0: upright or unknown). Asked only when
    the OCR result looks poor or comes from an engine without comparable confidences."""
    mode = opts.get("orientation", "auto")
    if isinstance(mode, str) and mode.strip().lower() in ("auto", ""):
        confs = [w.conf for ln in res.lines for w in ln.words if w.conf >= 0]
        good = (res.engine == "tesseract" and confs
                and sum(confs) / len(confs) >= GOOD_CONF)
        if good:
            return 0
    elif not _on(mode, False):
        return 0
    return detect_rotation(work, cfg)


def _rotate_lines_back(lines: Sequence[OcrLine], rotation: int, size: tuple[int, int]
                       ) -> list[OcrLine]:
    """Lines found on the upright copy, with word boxes mapped back onto the original picture
    (only used to tell text from barcodes there)."""
    w, h = size   # upright image size

    def back(b: Box) -> Box:
        x0, y0, x1, y1 = b
        if rotation == 180:
            return (w - x1, h - y1, w - x0, h - y0)
        if rotation == 90:     # original = upright turned 90 degrees anticlockwise
            return (y0, w - x1, y1, w - x0)
        return (h - y1, x0, h - y0, x1)          # 270

    out = []
    for ln in lines:
        words = [OcrWord(wd.text, back(wd.box), wd.conf) for wd in ln.words]
        out.append(OcrLine(ln.text, words, back(ln.box), ln.engine, True))
    return out


def _rotation_warning(rotation: int) -> str:
    return (f"글자가 {rotation}° 돌아간 그림으로 보여 바로 세운 상태로도 검사했습니다 — 결과를 직접 "
            f"확인하세요 (the picture looks rotated by {rotation} degrees; it was also checked "
            f"upright - please check the output)")


def _require_heif() -> None:
    try:
        import pillow_heif  # type: ignore
    except ImportError as e:
        raise RuntimeError(HEIF_HINT) from e
    pillow_heif.register_heif_opener()


def _clean_frame(img: "Image.Image", fmt: str) -> "Image.Image":
    """Fresh image built from pixel data only (no EXIF/XMP/text/comment info, no colour data
    under transparent pixels)."""
    from PIL import Image

    if img.mode in ("RGBA", "LA"):
        img = clear_hidden_colour(img.copy())
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        if img.mode == "RGBA":
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.alpha_composite(img)
            img = bg
        img = img.convert("RGB")
    elif fmt in ("WEBP", "HEIF") and img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return Image.frombytes(img.mode, img.size, img.tobytes())


def _save_kwargs(fmt: str, icc: bytes | None, dpi: tuple | None) -> dict:
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
    return kwargs


def save_tiff_pages(pages: Iterable["Image.Image"], dst: Path, icc: bytes | None = None,
                    dpi: tuple | None = None) -> int:
    """Write a multi-page TIFF atomically, page by page as `pages` yields them (only one page
    is held in memory). Returns the number of pages written."""
    from PIL import TiffImagePlugin

    kwargs = _save_kwargs("TIFF", icc, dpi)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".deid-", suffix=dst.suffix, dir=str(dst.parent))
    os.close(fd)
    n = 0
    try:
        with open(tmp, "w+b") as fp:
            writer = TiffImagePlugin.AppendingTiffWriter(fp)
            for page in pages:
                _clean_frame(page, "TIFF").save(writer, format="TIFF", **kwargs)
                writer.newFrame()
                n += 1
            del writer        # finalized while the file is still open
        if not n:
            raise ValueError("no image frames to save")
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return n


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
    kwargs = _save_kwargs(fmt, icc, dpi)
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
    with Image.open(src) as im:
        icc = im.info.get("icc_profile")
        dpi = im.info.get("dpi")
        n_frames = int(getattr(im, "n_frames", 1) or 1)
        keep_frames = n_frames if out_fmt in _MULTI_FRAME_FORMATS else 1
        # Decompression bombs: page count and pixel sizes come from the headers; check them
        # all before decoding anything.
        check_pixels((0, 0), cfg, frames=keep_frames)
        total = 0
        for idx in range(keep_frames):
            if keep_frames > 1:
                im.seek(idx)
            check_pixels(im.size, cfg)
            total += im.size[0] * im.size[1]
        check_pixels((0, 0), cfg, total=total)
        im.seek(0)

        def pages() -> Iterable["Image.Image"]:
            for idx, frame in enumerate(ImageSequence.Iterator(im)):
                if idx >= keep_frames:
                    break
                page = ImageOps.exif_transpose(frame.copy())
                out, rep = redact_image(page, detector, cfg, anonymizer)
                del page
                if n_frames > 1:
                    rep.warnings = [f"{idx + 1}쪽 / page {idx + 1}: {w}" for w in rep.warnings]
                report.merge(rep)
                yield out

        if out_fmt == "TIFF" and keep_frames > 1:
            save_tiff_pages(pages(), dst, icc=icc, dpi=dpi)   # one page in memory at a time
        else:
            frames = list(pages())
        if keep_frames < n_frames:
            report.warnings.append(
                f"{out_fmt}는 여러 쪽을 담을 수 없어 첫 쪽만 저장했습니다 "
                f"(only the first of {n_frames} frames was saved)")
    if not (out_fmt == "TIFF" and keep_frames > 1):
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
