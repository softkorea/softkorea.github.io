"""OCR abstraction: text lines with word and per-character boxes, from one of three engines.

Engines (``cfg.ocr["engine"]``):

* ``"livetext"`` - macOS 14 (Sonoma)+ Live Text through ``ocrmac`` (``framework="livetext"``,
  ``unit="token"``). Tokens are fine-grained (character-level) boxes; they are grouped into
  lines here and the line text is rebuilt (a space where the horizontal gap is large).
* ``"vision"`` - Apple Vision through ``ocrmac`` (``framework="vision"``). One result per text
  line; per-character boxes are estimated across the line box from character width weights.
* ``"tesseract"`` - the tesseract binary (TSV output, same data as ``pytesseract.image_to_data``)
  with ``kor+eng``, several page-segmentation passes run in parallel (one thread each). Words
  come with their own boxes; character boxes inside a word are estimated from width weights.
  The image is passed through stdin, so no temporary file holds it.
* ``"auto"`` (default) - on macOS livetext, then vision, then tesseract; elsewhere tesseract.

Every engine returns :class:`OcrLine` objects whose ``text`` is the words joined by single
spaces and whose :meth:`OcrLine.char_boxes` gives one box per character of ``text`` (spaces
get zero-width boxes), so a detector span ``[start, end)`` on ``line.text`` maps to the union
of its character boxes (:meth:`OcrLine.span_box`).

Config keys (``[ocr]`` in config.toml; all optional)::

    engine = "auto"            # auto | livetext | vision | tesseract
    lang = "kor+eng"           # tesseract languages
    psm = [4, 6, 11]           # tesseract page segmentation modes; all passes are combined
    upscale = "auto"           # auto: x2 (LANCZOS) when the image is < 1000 px high; or true/false
    invert = "auto"            # auto: invert mostly-dark (dark mode) images before tesseract
    min_conf = 30              # tesseract words below this are flagged (never dropped if non-empty)
    timeout = 180              # seconds per tesseract pass (0 = no limit)
    tesseract_cmd = ""         # explicit path to the tesseract binary (default: found on PATH)
    languages = ["ko-KR", "en-US"]   # Apple Vision / Live Text language preference
    recognition_level = "accurate"   # Apple Vision
    space_gap = 0.25           # Live Text: gap (x char height) above which a space is inserted

Nothing here sends data anywhere: tesseract runs as a local process, ocrmac uses the OS.
"""
from __future__ import annotations

import functools
import io
import os
import platform
import shutil
import statistics
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image

    from .config import Config

Box = tuple[float, float, float, float]   # (x1, y1, x2, y2), PIL pixel coordinates

ENGINES = ("auto", "livetext", "vision", "tesseract")
DEFAULT_LANG = "kor+eng"
# Several layouts, all kept: 4 (columns) and 6 (one block) read most screens, 11 (sparse)
# picks up scattered UI labels. Measured on synthetic EMR screenshots, no single mode found
# every name; the union did.
DEFAULT_PSM: tuple[int, ...] = (4, 6, 11)
DEFAULT_MIN_CONF = 30.0
DEFAULT_LANGUAGES = ("ko-KR", "en-US")
UPSCALE_BELOW_PX = 1000
DARK_MEAN = 100            # mean luminance (0-255) below which an image counts as dark mode
LOW_CONF_WARN_RATIO = 0.3  # warn when this share of words is below min_conf
SPACE_WEIGHT = 0.3         # width of a space in em, for laying out estimated boxes

TESSERACT_HINT = (
    "Tesseract OCR이 필요합니다 / Tesseract OCR is required: "
    "macOS: brew install tesseract tesseract-lang ; "
    "Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-kor"
)
OCRMAC_HINT = (
    "Apple Vision/Live Text OCR에는 ocrmac이 필요합니다 (macOS 전용) / "
    "Apple Vision OCR needs ocrmac (macOS only): pip install ocrmac"
)
LOW_CONF_WARNING = (
    "OCR 신뢰도가 낮은 글자가 많음 — 결과 이미지를 직접 확인하세요 "
    "(many low-confidence OCR words; please check the output by eye)"
)


class OcrError(RuntimeError):
    """OCR engine missing, misconfigured or failed. Messages never contain recognized text."""


# ---------------------------------------------------------------------- width weights


def char_weight(ch: str) -> float:
    """Relative advance width of one character (Hangul/CJK/fullwidth = 1.0)."""
    if ch.isspace():
        return 0.0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 1.0
    if ch.isascii():
        if ch.isupper() or ch.isdigit():
            return 0.6
        if ch.islower():
            return 0.5
        return 0.35
    if ch.isalpha() or ch.isdigit():
        return 0.55
    return 0.35


def _layout_weight(ch: str) -> float:
    return SPACE_WEIGHT if ch.isspace() else char_weight(ch)


def split_box(box: Box, text: str) -> list[Box]:
    """One box per character of `text`, laid out left to right inside `box` by width weights.

    Whitespace takes up room (SPACE_WEIGHT) but is returned as a zero-width box at its start.
    """
    x1, y1, x2, y2 = box
    weights = [_layout_weight(c) for c in text]
    total = sum(weights)
    if not text:
        return []
    if total <= 0:
        return [(x1, y1, x1, y2) for _ in text]
    scale = (x2 - x1) / total
    out: list[Box] = []
    x = x1
    for ch, w in zip(text, weights):
        nx = x + w * scale
        out.append((x, y1, x, y2) if ch.isspace() else (x, y1, nx, y2))
        x = nx
    return out


def union_box(boxes: Iterable[Box]) -> Box | None:
    bs = list(boxes)
    if not bs:
        return None
    return (min(b[0] for b in bs), min(b[1] for b in bs),
            max(b[2] for b in bs), max(b[3] for b in bs))


def _norm_box(raw: Any) -> Box:
    """(x1, y1, x2, y2) floats with x1 <= x2 and y1 <= y2; ValueError if unusable."""
    vals = [float(v) for v in raw]
    if len(vals) != 4 or any(v != v for v in vals):  # NaN check
        raise ValueError("bad box")
    x1, y1, x2, y2 = vals
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


# ---------------------------------------------------------------------- data types


@dataclass
class OcrWord:
    """One word (no spaces inside). `chars` holds exact per-character boxes when known."""

    text: str
    box: Box
    conf: float = -1.0                     # engine confidence 0-100 (-1 unknown)
    chars: list[Box] | None = None

    def char_boxes(self) -> list[Box]:
        if self.chars is not None and len(self.chars) == len(self.text):
            return list(self.chars)
        return split_box(self.box, self.text)


@dataclass
class OcrLine:
    """One text line. `text` is the words joined with single spaces."""

    text: str
    words: list[OcrWord] = field(default_factory=list)
    box: Box = (0.0, 0.0, 0.0, 0.0)
    engine: str = ""
    estimated: bool = False   # True when character boxes are estimated across a whole line

    @classmethod
    def from_words(cls, words: Sequence[OcrWord], engine: str = "",
                   estimated: bool = False) -> "OcrLine":
        ws = [w for w in words if w.text]
        box = union_box(w.box for w in ws) or (0.0, 0.0, 0.0, 0.0)
        return cls(" ".join(w.text for w in ws), list(ws), box, engine, estimated)

    @property
    def height(self) -> float:
        """Typical text height of the line (median word height; robust to stray tall boxes)."""
        hs = [w.box[3] - w.box[1] for w in self.words if w.box[3] > w.box[1]]
        if hs:
            return float(statistics.median(hs))
        return max(0.0, self.box[3] - self.box[1])

    def word_starts(self) -> list[int] | None:
        """Offset of each word in `text`, or None if `text` is not the joined words."""
        if " ".join(w.text for w in self.words) != self.text:
            return None
        starts, pos = [], 0
        for w in self.words:
            starts.append(pos)
            pos += len(w.text) + 1
        return starts

    def char_boxes(self) -> list[Box]:
        """One box per character of `text`; spaces get zero-width boxes."""
        if self.words and self.word_starts() is not None:
            out: list[Box] = []
            for i, w in enumerate(self.words):
                if i:
                    prev = out[-1] if out else w.box
                    out.append((prev[2], prev[1], prev[2], prev[3]))
                out.extend(w.char_boxes())
            if len(out) == len(self.text):
                return out
        return split_box(self.box, self.text)

    def span_boxes(self, start: int, end: int) -> list[Box]:
        """Boxes of the characters in text[start:end] (zero-width space boxes skipped)."""
        boxes = self.char_boxes()[max(0, start):max(0, end)]
        solid = [b for b in boxes if b[2] > b[0]]
        return solid or boxes

    def span_box(self, start: int, end: int) -> Box | None:
        return union_box(self.span_boxes(start, end))


@dataclass
class OcrResult:
    lines: list[OcrLine]
    engine: str = ""
    warnings: list[str] = field(default_factory=list)
    words: int = 0
    low_conf_words: int = 0

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


# ---------------------------------------------------------------------- environment


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _macos_version() -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in platform.mac_ver()[0].split(".") if p.isdigit())
    except Exception:  # pragma: no cover - defensive
        return ()


def _import_ocrmac():
    try:
        from ocrmac import ocrmac  # type: ignore
    except Exception as e:  # ImportError, or pyobjc failing to load off macOS
        raise OcrError(f"{OCRMAC_HINT} ({type(e).__name__})") from e
    return ocrmac


# Where Homebrew / MacPorts put tesseract; GUI launchers (Shortcuts, Automator, Finder quick
# actions) often run with a PATH that does not include them.
_TESSERACT_FALLBACKS = ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
                        "/opt/local/bin/tesseract", "/usr/bin/tesseract")


def _find_tesseract() -> str | None:
    """tesseract binary: pytesseract's configured command, PATH, then well-known locations."""
    try:
        import pytesseract  # type: ignore
        configured = str(getattr(pytesseract.pytesseract, "tesseract_cmd", "") or "")
    except Exception:  # pytesseract is optional: deid runs the binary itself
        configured = ""
    if configured and configured != "tesseract":
        found = shutil.which(configured) or (configured if os.path.isfile(configured) else None)
        if found:
            return found
    found = shutil.which("tesseract")
    if found:
        return found
    for cand in _TESSERACT_FALLBACKS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _tesseract_cmd() -> str:
    cmd = _find_tesseract()
    if cmd is None:
        raise OcrError(TESSERACT_HINT)
    return cmd


def _tesseract_env() -> dict[str, str]:
    """Environment for tesseract: one OpenMP thread per process (we run passes in parallel;
    OpenMP spin-waiting makes tesseract crawl when CPUs are busy)."""
    env = dict(os.environ)
    env.setdefault("OMP_THREAD_LIMIT", "1")
    return env


def _run_cmd(args: list[str], timeout: float | None, data: bytes | None = None
             ) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, input=data, capture_output=True, timeout=timeout,
                              env=_tesseract_env(), check=False)
    except FileNotFoundError as e:
        raise OcrError(TESSERACT_HINT) from e
    except subprocess.TimeoutExpired as e:
        raise OcrError(f"Tesseract 시간 초과 / tesseract timed out after {timeout:.0f}s "
                       "([ocr] timeout)") from e
    except OSError as e:
        raise OcrError(f"Tesseract 실행 실패 / cannot run tesseract ({type(e).__name__})") from e


def _stderr_line(proc: subprocess.CompletedProcess) -> str:
    lines = [ln.strip() for ln in proc.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else f"exit code {proc.returncode}"


@functools.lru_cache(maxsize=8)
def _tesseract_version(cmd: str) -> str:
    proc = _run_cmd([cmd, "--version"], timeout=60)
    out = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip().splitlines()
    if proc.returncode != 0 or not out:
        raise OcrError(TESSERACT_HINT)
    return out[0].replace("tesseract", "").strip() or out[0]


@functools.lru_cache(maxsize=8)
def _tesseract_langs(cmd: str) -> frozenset[str] | None:
    proc = _run_cmd([cmd, "--list-langs"], timeout=60)
    if proc.returncode != 0:
        return None   # let the OCR call itself report the problem
    lines = (proc.stdout or proc.stderr).decode("utf-8", "replace").splitlines()
    return frozenset(ln.strip() for ln in lines[1:] if ln.strip() and " " not in ln.strip())


def tesseract_languages() -> list[str]:
    """Installed tesseract languages ([] when tesseract is missing)."""
    try:
        cmd = _tesseract_cmd()
        _tesseract_version(cmd)
        return sorted(_tesseract_langs(cmd) or ())
    except OcrError:
        return []


def _check_tesseract(lang: str, cmd: str | None = None) -> str:
    """Path of a working tesseract that has `lang` ("kor+eng"); OcrError with a hint if not."""
    if cmd:
        found = shutil.which(cmd) or (cmd if os.path.isfile(cmd) else None)
        if found is None:
            raise OcrError(f"[ocr] tesseract_cmd 경로에 tesseract가 없습니다 / not found: {cmd}")
        cmd = found
    else:
        cmd = _tesseract_cmd()
    _tesseract_version(cmd)
    have = _tesseract_langs(cmd)
    if have is not None:
        missing = [code for code in lang.split("+") if code and code not in have]
        if missing:
            raise OcrError(
                f"Tesseract 언어 데이터가 없습니다: {', '.join(missing)} / missing tesseract "
                f"language data: {', '.join(missing)}. macOS: brew install tesseract-lang ; "
                "Ubuntu: sudo apt install tesseract-ocr-kor"
            )
    return cmd


_TSV_INT = ("level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top",
            "width", "height")


def parse_tsv(tsv: str) -> dict[str, list]:
    """tesseract TSV output -> pytesseract-style DICT (column name -> list)."""
    rows = tsv.splitlines()
    if not rows:
        return {"text": []}
    header = rows[0].split("\t")
    out: dict[str, list] = {h: [] for h in header}
    for row in rows[1:]:
        if not row.strip():
            continue
        cells = row.split("\t", len(header) - 1)
        cells += [""] * (len(header) - len(cells))
        for h, v in zip(header, cells):
            if h in _TSV_INT:
                out[h].append(int(_num(v, 0)))
            elif h == "conf":
                out[h].append(_num(v))
            else:
                out[h].append(v)
    return out


def tesseract_data(img: "Image.Image", lang: str = DEFAULT_LANG, psm: int = 6, extra: str = "",
                   timeout: float | None = None, cmd: str | None = None) -> dict[str, list]:
    """Word boxes for `img` (pytesseract image_to_data DICT layout).

    The image goes to tesseract through stdin (no temporary file holding the picture).
    """
    cmd = cmd or _tesseract_cmd()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    args = [cmd, "stdin", "stdout", "-l", lang, "--psm", str(int(psm))]
    args += [a for a in extra.split() if a]
    args.append("tsv")
    proc = _run_cmd(args, timeout=timeout, data=buf.getvalue())
    if proc.returncode != 0:
        raise OcrError(f"Tesseract OCR 실패 / tesseract failed (psm {psm}): {_stderr_line(proc)}")
    return parse_tsv(proc.stdout.decode("utf-8", "replace"))


def engine_status() -> dict[str, str]:
    """Engine name -> "ok ..." or "missing: <install hint>", for `deid doctor`."""
    out: dict[str, str] = {}
    if _is_macos():
        try:
            _import_ocrmac()
            ver = _macos_version()
            out["livetext"] = ("ok" if ver >= (14,) else
                               "unavailable: macOS 14 (Sonoma) 이상 필요 / needs macOS 14+")
            out["vision"] = "ok"
        except OcrError as e:
            out["livetext"] = out["vision"] = f"missing: {e}"
    else:
        out["livetext"] = out["vision"] = "unavailable: macOS 전용 / macOS only"
    try:
        cmd = _tesseract_cmd()
        ver = _tesseract_version(cmd)
        langs = _tesseract_langs(cmd)
        if langs is not None and "kor" not in langs:
            out["tesseract"] = (f"incomplete: {ver}, 한국어(kor) 데이터 없음 / no Korean data: "
                                "brew install tesseract-lang")
        else:
            shown = ", ".join(sorted(langs)) if langs else "?"
            out["tesseract"] = f"ok: {ver} ({shown})"
    except OcrError as e:
        out["tesseract"] = f"missing: {e}"
    return out


def available_engines() -> list[str]:
    """Engines usable on this machine, in the order "auto" tries them."""
    return [name for name, status in engine_status().items() if status.startswith("ok")]


def _auto_order() -> list[str]:
    return ["livetext", "vision", "tesseract"] if _is_macos() else ["tesseract"]


# ---------------------------------------------------------------------- entry points


def ocr_image(img: "Image.Image", cfg: "Config | None" = None,
              engine: str | None = None) -> OcrResult:
    """Run OCR on a PIL image. Raises OcrError if the engine is missing or fails."""
    opts = dict(getattr(cfg, "ocr", None) or {})
    name = (engine or opts.get("engine") or "auto").strip().lower()
    if name in ("apple", "mac", "macos"):
        name = "vision"
    if name not in ENGINES:
        raise OcrError(f"알 수 없는 OCR 엔진 / unknown OCR engine {name!r}; use one of {ENGINES}")
    if name != "auto":
        return _run_engine(name, img, opts)
    errors: list[str] = []
    result: OcrResult | None = None
    for cand in _auto_order():
        try:
            res = _run_engine(cand, img, opts)
        except OcrError as e:  # e.g. Live Text unsupported on this macOS version
            errors.append(f"[{cand}] {e}")
            continue
        if res.lines:
            return res
        result = result or res   # empty: let a later engine have a look before giving up
    if result is not None:
        return result
    raise OcrError("사용 가능한 OCR 엔진이 없습니다 / no usable OCR engine: " + " | ".join(errors))


def _run_engine(name: str, img: "Image.Image", opts: dict) -> OcrResult:
    try:
        return _ENGINE_FUNCS[name](img, opts)
    except OcrError:
        raise
    except Exception as e:  # engine bug / OS framework error: report the type, not data
        raise OcrError(f"{name} OCR 실패 / {name} OCR failed ({type(e).__name__})") from e


def ocr_lines(img: "Image.Image", cfg: "Config | None" = None,
              engine: str | None = None) -> list[OcrLine]:
    return ocr_image(img, cfg, engine).lines


# ---------------------------------------------------------------------- image prep


def flatten_rgb(img: "Image.Image") -> "Image.Image":
    """RGB copy with any transparency composited onto white (transparent pixels are often
    black underneath, which would hide dark text from OCR)."""
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        return bg.convert("RGB")
    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        return img.convert("I").point(lambda v: v * (1 / 256)).convert("L").convert("RGB")
    if img.mode == "F":
        return img.convert("L").convert("RGB")
    return img.convert("RGB") if img.mode != "RGB" else img


def _upscale_factor(img: "Image.Image", opts: dict) -> int:
    """auto: x2 below UPSCALE_BELOW_PX height; true/false; or an explicit factor 1-4."""
    up = opts.get("upscale", "auto")
    auto = 2 if img.height < UPSCALE_BELOW_PX else 1
    if isinstance(up, str):
        s = up.strip().lower()
        if s in ("yes", "on", "true"):
            return 2
        if s in ("no", "off", "false"):
            return 1
        try:
            up = int(s)
        except ValueError:
            return auto
    if isinstance(up, bool):
        return 2 if up else 1
    if isinstance(up, (int, float)):
        return max(1, min(4, int(up)))
    return auto


def prepare_for_tesseract(img: "Image.Image", opts: dict | None = None) -> tuple["Image.Image", int]:
    """Grayscale, dark-mode inverted, upscaled copy for tesseract and the scale factor used."""
    from PIL import Image, ImageOps, ImageStat

    opts = opts or {}
    gray = flatten_rgb(img).convert("L")
    inv = opts.get("invert", "auto")
    if inv is True or (inv == "auto" and ImageStat.Stat(gray).mean[0] < DARK_MEAN):
        gray = ImageOps.invert(gray)
    factor = _upscale_factor(gray, opts)
    if factor > 1:
        gray = gray.resize((gray.width * factor, gray.height * factor), Image.LANCZOS)
    return gray, factor


# ---------------------------------------------------------------------- tesseract


def _psm_list(value: Any) -> list[int]:
    if value is None:
        return list(DEFAULT_PSM)
    vals = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for v in vals:
        try:
            p = int(v)
        except (TypeError, ValueError):
            raise OcrError(f"잘못된 psm 값 / invalid tesseract psm {v!r}") from None
        if not 0 <= p <= 13:
            raise OcrError(f"잘못된 psm 값 / invalid tesseract psm {v!r}")
        if p not in out:
            out.append(p)
    return out or list(DEFAULT_PSM)


def _num(v: Any, default: float = -1.0) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def lines_from_tesseract(data: dict, scale: float = 1.0, engine: str = "tesseract",
                         min_conf: float = DEFAULT_MIN_CONF) -> tuple[list[OcrLine], int, int]:
    """Group pytesseract image_to_data(DICT) words into lines.

    Returns (lines, n_words, n_low_conf). Words that are empty/whitespace are dropped; words
    with low confidence are KEPT (better to over-redact than to miss text).
    """
    texts = data.get("text") or []
    n = len(texts)
    col = {k: (data.get(k) or [0] * n) for k in
           ("level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width",
            "height", "conf")}
    groups: dict[tuple, list[OcrWord]] = {}
    n_words = n_low = 0
    s = float(scale) if scale else 1.0
    for i in range(n):
        level = int(_num(col["level"][i], 5))
        raw = texts[i]
        text = "" if raw is None else str(raw).strip()
        if level != 5 or not text:
            continue
        text = " ".join(text.split())     # paranoia: no inner whitespace runs
        conf = _num(col["conf"][i])
        n_words += 1
        if 0 <= conf < min_conf:
            n_low += 1
        x, y = _num(col["left"][i], 0), _num(col["top"][i], 0)
        w, h = max(0.0, _num(col["width"][i], 0)), max(0.0, _num(col["height"][i], 0))
        box = (x / s, y / s, (x + w) / s, (y + h) / s)
        key = tuple(int(_num(col[k][i], 0)) for k in ("page_num", "block_num", "par_num",
                                                      "line_num"))
        bucket = groups.setdefault(key, [])
        if " " not in text:
            bucket.append(OcrWord(text, box, conf))
            continue
        # Rare: a "word" with an inner space; split it into words using estimated boxes.
        sub, pos = split_box(box, text), 0
        for piece in text.split(" "):
            bucket.append(OcrWord(piece, union_box(sub[pos:pos + len(piece)]) or box, conf))
            pos += len(piece) + 1
    lines = [OcrLine.from_words(ws, engine) for ws in groups.values() if ws]
    return lines, n_words, n_low


def _ocr_tesseract(img: "Image.Image", opts: dict) -> OcrResult:
    lang = str(opts.get("lang") or DEFAULT_LANG)
    cmd = _check_tesseract(lang, opts.get("tesseract_cmd") or None)
    min_conf = float(opts.get("min_conf", DEFAULT_MIN_CONF))
    timeout = float(opts.get("timeout", 180) or 0) or None
    extra = str(opts.get("tesseract_config", "") or "")
    prepared, factor = prepare_for_tesseract(img, opts)
    psms = _psm_list(opts.get("psm"))
    workers = max(1, min(len(psms), os.cpu_count() or 1))

    def run(psm: int) -> dict:
        return tesseract_data(prepared, lang, psm, extra, timeout, cmd)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            datas = list(pool.map(run, psms))
    else:
        datas = [run(p) for p in psms]
    lines: list[OcrLine] = []
    n_words = n_low = 0
    for psm, data in zip(psms, datas):
        got, nw, nl = lines_from_tesseract(data, factor, f"tesseract/psm{psm}", min_conf)
        lines += got
        n_words += nw
        n_low += nl
    res = OcrResult(lines, "tesseract", words=n_words, low_conf_words=n_low)
    if n_words >= 5 and n_low / n_words > LOW_CONF_WARN_RATIO:
        res.warnings.append(LOW_CONF_WARNING)
    return res


# ---------------------------------------------------------------------- Apple (ocrmac)


def _languages(opts: dict) -> list[str]:
    langs = opts.get("languages") or list(DEFAULT_LANGUAGES)
    return [str(x) for x in (langs if isinstance(langs, (list, tuple)) else [langs])]


def _parse_ocrmac(results: Any) -> list[tuple[str, float, Box]]:
    """[(text, conf 0-100, box)] from ocrmac recognize(px=True); fail closed on bad entries."""
    out = []
    for item in results or []:
        try:
            text, conf, raw_box = item[0], item[1], item[2]
        except (TypeError, IndexError, KeyError):
            raise OcrError("ocrmac 결과 형식을 이해할 수 없습니다 / unexpected ocrmac result") from None
        text = "" if text is None else str(text)
        if not text.strip():
            continue
        try:
            box = _norm_box(raw_box)
        except (TypeError, ValueError):
            raise OcrError("ocrmac 결과에 좌표가 없습니다 / ocrmac result without a box") from None
        c = _num(conf, -1.0)
        out.append((text, c * 100 if 0 <= c <= 1 else c, box))
    return out


def _make_ocr(om: Any, img: "Image.Image", **kw: Any) -> Any:
    try:
        return om.OCR(img, **kw)
    except TypeError:
        # Older ocrmac without framework/unit keywords (vision only) - retry with fewer options.
        if kw.get("framework") == "livetext":
            raise
        kw.pop("framework", None)
        kw.pop("unit", None)
        return om.OCR(img, **kw)


def _ocr_vision(img: "Image.Image", opts: dict) -> OcrResult:
    om = _import_ocrmac()
    rgb = flatten_rgb(img)
    ocr = _make_ocr(om, rgb, framework="vision",
                    recognition_level=str(opts.get("recognition_level", "accurate")),
                    language_preference=_languages(opts))
    lines = []
    for text, conf, box in _parse_ocrmac(ocr.recognize(px=True)):
        lines.append(line_from_box(text, box, conf, engine="vision"))
    lines.sort(key=lambda ln: (ln.box[1], ln.box[0]))
    return OcrResult(lines, "vision", words=sum(len(ln.words) for ln in lines))


def line_from_box(text: str, box: Box, conf: float = -1.0, engine: str = "") -> OcrLine:
    """OcrLine for a whole-line result: words and character boxes estimated inside `box`."""
    norm = " ".join(text.split())
    chars = split_box(box, norm)
    words, pos = [], 0
    for piece in norm.split(" "):
        cb = chars[pos:pos + len(piece)]
        words.append(OcrWord(piece, union_box(cb) or box, conf, cb))
        pos += len(piece) + 1
    line = OcrLine.from_words(words, engine, estimated=True)
    line.box = box
    return line


def _ocr_livetext(img: "Image.Image", opts: dict) -> OcrResult:
    om = _import_ocrmac()
    rgb = flatten_rgb(img)
    try:
        ocr = om.OCR(rgb, framework="livetext", language_preference=_languages(opts),
                     unit="token")
    except TypeError:
        ocr = om.OCR(rgb, framework="livetext", unit="token")
    tokens = _parse_ocrmac(ocr.recognize(px=True))
    gap = float(opts.get("space_gap", 0.25))
    lines = group_tokens(tokens, gap_ratio=gap, engine="livetext")
    return OcrResult(lines, "livetext", words=sum(len(ln.words) for ln in lines))


def _explode(tokens: Iterable[tuple[str, float, Box]]) -> list[tuple[str, float, Box]]:
    """One entry per non-space character; multi-character tokens are split by width weights."""
    out = []
    for text, conf, box in tokens:
        if len(text) == 1:
            if not text.isspace():
                out.append((text, conf, box))
            continue
        for ch, cb in zip(text, split_box(box, text)):
            if not ch.isspace():
                out.append((ch, conf, cb))
    return out


def _v_overlap(a: Box, b: tuple[float, float]) -> float:
    return min(a[3], b[1]) - max(a[1], b[0])


def group_tokens(tokens: Iterable[tuple[str, float, Box]], gap_ratio: float = 0.25,
                 engine: str = "livetext") -> list[OcrLine]:
    """Group character-level boxes into lines (vertical overlap, then x order) and words.

    A space is inserted between two characters when the horizontal gap exceeds
    `gap_ratio` x the line's median character height.
    """
    chars = _explode(tokens)
    chars.sort(key=lambda t: ((t[2][1] + t[2][3]) / 2, t[2][0]))
    rows: list[dict] = []   # {"items": [...], "y": (median y1, median y2)}
    for item in chars:
        box = item[2]
        h = box[3] - box[1]
        best, best_ov = None, 0.0
        for row in rows:
            ry = row["y"]
            ov = _v_overlap(box, ry)
            need = 0.5 * max(1e-6, min(h, ry[1] - ry[0])) if h > 0 else 0.0
            if ov >= need and ov > best_ov:
                best, best_ov = row, ov
        if best is None:
            rows.append({"items": [item], "y": (box[1], box[3])})
        else:
            best["items"].append(item)
            its = best["items"]
            best["y"] = (statistics.median(i[2][1] for i in its),
                         statistics.median(i[2][3] for i in its))
    lines = []
    for row in rows:
        items = sorted(row["items"], key=lambda t: (t[2][0], t[2][2]))
        hs = [i[2][3] - i[2][1] for i in items if i[2][3] > i[2][1]]
        med_h = statistics.median(hs) if hs else 0.0
        words: list[OcrWord] = []
        cur: list[tuple[str, float, Box]] = []
        for it in items:
            if cur and it[2][0] - cur[-1][2][2] > gap_ratio * med_h:
                words.append(_word_from_chars(cur))
                cur = []
            cur.append(it)
        if cur:
            words.append(_word_from_chars(cur))
        lines.append(OcrLine.from_words(words, engine))
    lines.sort(key=lambda ln: (ln.box[1], ln.box[0]))
    return lines


def _word_from_chars(items: Sequence[tuple[str, float, Box]]) -> OcrWord:
    boxes = [i[2] for i in items]
    confs = [i[1] for i in items if i[1] >= 0]
    return OcrWord("".join(i[0] for i in items), union_box(boxes) or boxes[0],
                   min(confs) if confs else -1.0, boxes)


_ENGINE_FUNCS = {
    "tesseract": _ocr_tesseract,
    "vision": _ocr_vision,
    "livetext": _ocr_livetext,
}
