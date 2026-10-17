"""File format dispatcher: pick the handler for a file by extension and run it safely.

Each handler module (text, tabular, pdf, docx, hwpx, image) exposes
``process(src, dst, detector, anonymizer, cfg) -> Report`` and is imported lazily, so a
missing optional dependency only affects its own file types.

Safety nets on top of the handlers:

* :func:`process_file` never lets an output that the failing handler touched survive an
  exception (no half-written, possibly un-processed file is left behind) and refuses to write
  over the source.
* :func:`process_to_dir` also de-identifies the output FILE NAME (and sub-folder names):
  "홍길동_진료기록.docx" becomes "[이름1]_진료기록.deid.docx" using the same tokens as the
  content, because file names travel with shared files. A file name has no context ("환자",
  "님"), so a bare name such as "김민서.txt" or "박서준_외래기록.docx" is caught with a stricter
  file-name rule (:func:`guess_name_spans`) than the one used for prose.
"""
from __future__ import annotations

import functools
import importlib
import inspect
import os
import re
import secrets
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .. import entities as E
from ..types import Report, Span

if TYPE_CHECKING:  # pragma: no cover
    from ..anonymizer import Anonymizer
    from ..config import Config
    from ..engine import Detector

# extension -> handler module (deid.formats.<module>)
SUPPORTED: dict[str, str] = {
    **{ext: "text" for ext in (
        ".txt", ".text", ".md", ".markdown", ".log", ".srt", ".vtt",
        ".json", ".jsonl", ".ndjson", ".html", ".htm", ".xhtml", ".xml")},
    **{ext: "tabular" for ext in (".xlsx", ".xlsm", ".csv", ".tsv")},
    ".pdf": "pdf",
    **{ext: "docx" for ext in (".docx", ".docm", ".dotx", ".dotm")},
    ".hwpx": "hwpx",
    **{ext: "image" for ext in (
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif")},
}

KIND_KO = {
    "text": "텍스트(txt, md, json, html 외)", "tabular": "엑셀/표(xlsx, xlsm, csv, tsv)", "pdf": "PDF",
    "docx": "워드(docx, docm, dotx)", "hwpx": "한글(hwpx)", "image": "이미지(png, jpg, heic 외)",
}

_RESAVE = "다른 이름으로 저장 → {fmt} 형식으로 저장한 뒤 다시 시도하세요"
LEGACY: dict[str, str] = {
    ".xls": "예전 엑셀 형식(.xls)은 지원하지 않습니다. Excel에서 " + _RESAVE.format(fmt=".xlsx")
            + " (.xls is not supported: re-save as .xlsx)",
    ".doc": "예전 워드 형식(.doc)은 지원하지 않습니다. Word에서 " + _RESAVE.format(fmt=".docx")
            + " (.doc is not supported: re-save as .docx)",
    ".dot": "예전 워드 서식 파일(.dot)은 지원하지 않습니다. Word에서 " + _RESAVE.format(fmt=".docx")
            + " (.dot is not supported: re-save as .docx)",
    ".hwp": "한글 .hwp 형식은 지원하지 않습니다. 한글(한컴오피스)에서 " + _RESAVE.format(fmt=".hwpx")
            + " (.hwp is not supported: re-save as .hwpx)",
    ".rtf": "RTF 문서는 지원하지 않습니다. 워드에서 " + _RESAVE.format(fmt=".docx")
            + " (.rtf is not supported: re-save as .docx)",
    ".odt": "ODT 문서는 지원하지 않습니다. " + _RESAVE.format(fmt=".docx")
            + " (.odt is not supported: re-save as .docx)",
    ".ods": "ODS 표는 지원하지 않습니다. " + _RESAVE.format(fmt=".xlsx")
            + " (.ods is not supported: re-save as .xlsx)",
    ".ppt": "파워포인트는 지원하지 않습니다. PDF로 내보낸 뒤 다시 시도하세요 "
            "(PowerPoint is not supported: export to PDF first)",
    ".pptx": "파워포인트는 지원하지 않습니다. PDF로 내보낸 뒤 다시 시도하세요 "
             "(PowerPoint is not supported: export to PDF first)",
    ".key": "Keynote는 지원하지 않습니다. PDF로 내보낸 뒤 다시 시도하세요 "
            "(Keynote is not supported: export to PDF first)",
    ".pages": "Pages 문서는 지원하지 않습니다. Word(.docx)나 PDF로 내보낸 뒤 다시 시도하세요 "
              "(Pages is not supported: export to .docx or PDF)",
    ".numbers": "Numbers 문서는 지원하지 않습니다. Excel(.xlsx)로 내보낸 뒤 다시 시도하세요 "
                "(Numbers is not supported: export to .xlsx)",
    ".zip": "압축 파일은 지원하지 않습니다. 압축을 푼 폴더를 지정하세요 "
            "(zip archives are not supported: unzip and pass the folder)",
}

# Types that are skipped quietly in a folder run but deserve a "what to do" hint when a single
# file is named (README §1 "지원하지 않는 형식").
_DICOM_HINT = ("DICOM 영상(.dcm)은 처리하지 못합니다. PACS의 익명화 내보내기 기능을 쓰거나, 화면에 띄워 "
               "'deid shot'으로 캡처하세요 (DICOM is not supported: use the PACS anonymised export "
               "or capture the screen with 'deid shot')")
_MEDIA_HINT = ("동영상·음성 파일은 처리하지 못합니다 (video and audio files are not supported)")
_MAIL_HINT = ("메일 파일은 처리하지 못합니다. 본문은 텍스트로, 첨부 파일은 따로 저장해 처리하세요 "
              "(e-mail files are not supported: save the body as text and the attachments separately)")
HINTS: dict[str, str] = {
    ".dcm": _DICOM_HINT, ".dicom": _DICOM_HINT,
    ".gif": "GIF 이미지는 지원하지 않습니다. PNG로 바꾼 뒤 다시 시도하세요 (GIF is not supported: "
            "convert to PNG first)",
    ".eml": _MAIL_HINT, ".msg": _MAIL_HINT, ".emlx": _MAIL_HINT,
    **{ext: _MEDIA_HINT for ext in (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mp3", ".m4a",
                                    ".wav", ".aac", ".caf")},
}

OUTPUT_TAG = ".deid"


class UnsupportedFormatError(ValueError):
    """The file type cannot be processed (unknown, or a legacy format to re-save)."""


def handler_name(path: str | os.PathLike) -> str | None:
    return SUPPORTED.get(Path(path).suffix.lower())


def is_supported(path: str | os.PathLike) -> bool:
    return handler_name(path) is not None


def is_legacy(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in LEGACY


def is_deid_output(path: str | os.PathLike) -> bool:
    """True for files this tool wrote ("x.deid.docx", "x-2.deid.docx")."""
    return Path(path).stem.lower().endswith(OUTPUT_TAG)


def supported_list() -> str:
    groups: dict[str, list[str]] = {}
    for ext, mod in SUPPORTED.items():
        groups.setdefault(mod, []).append(ext)
    return ", ".join(" ".join(exts) for exts in groups.values())


def unsupported_message(path: str | os.PathLike) -> str:
    ext = Path(path).suffix.lower()
    if ext in LEGACY:
        return LEGACY[ext]
    if ext in HINTS:
        return HINTS[ext]
    shown = ext or "(확장자 없음 / no extension)"
    return (f"지원하지 않는 파일 형식입니다: {shown}. 지원 형식: {supported_list()} "
            f"(unsupported file type {shown})")


def output_path_for(src: str | os.PathLike, outdir: str | os.PathLike) -> Path:
    """outdir/<stem>.deid<ext> (the name before file-name de-identification)."""
    src = Path(src)
    return Path(outdir) / f"{src.stem}{OUTPUT_TAG}{src.suffix}"


def load_handler(name: str):
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ImportError as e:
        raise RuntimeError(f"'{name}' 형식 처리 모듈을 불러올 수 없습니다. 'deid doctor'로 설치 상태를 "
                           f"확인하세요 (cannot load the {name} handler: {e})") from e


def _stat_sig(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (b.exists() and os.path.samefile(a, b))
    except OSError:
        return False


def _accepted_kwargs(func: Any, kw: Mapping[str, Any]) -> dict[str, Any]:
    """Options the handler accepts; None means "not set" and is never passed."""
    kw = {k: v for k, v in kw.items() if v is not None}
    if not kw:
        return {}
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return kw
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kw
    return {k: v for k, v in kw.items() if k in params}


def process_file(src: str | os.PathLike, dst: str | os.PathLike, detector: "Detector",
                 anonymizer: "Anonymizer", cfg: "Config", **kw: Any) -> Report:
    """De-identify one file into `dst` with the handler for its extension.

    Extra keyword arguments (e.g. ``column_types`` for spreadsheets) are passed only to
    handlers that accept them. Unknown or legacy types raise :class:`UnsupportedFormatError`
    (a ValueError) with a Korean + English message. If the handler fails, an output it
    created or modified is deleted before the exception propagates.
    """
    src, dst = Path(src), Path(dst)
    name = handler_name(src)
    if name is None:
        raise UnsupportedFormatError(unsupported_message(src))
    if not src.is_file():
        raise FileNotFoundError("입력 파일을 찾을 수 없습니다 (input file not found)")
    if _same_file(src, dst):
        raise ValueError("출력 파일이 원본과 같습니다. 원본은 덮어쓰지 않습니다 "
                         "(output must differ from the input file)")
    handler = load_handler(name)
    before = _stat_sig(dst)
    try:
        report = handler.process(src, dst, detector, anonymizer, cfg,
                                 **_accepted_kwargs(handler.process, kw))
        if not isinstance(report, Report):
            raise TypeError(f"{name} handler returned {type(report).__name__}, not Report")
        if _stat_sig(dst) is None:
            raise RuntimeError("출력 파일이 만들어지지 않았습니다 (the handler wrote no output)")
        drain = getattr(detector, "drain_warnings", None)
        if callable(drain):   # detector warnings not taken by the handler (LLM on_error)
            report.warnings.extend(w for w in drain() if w not in report.warnings)
    except BaseException:
        if _stat_sig(dst) != before:
            _remove(dst)
        drain = getattr(detector, "drain_warnings", None)
        if callable(drain):   # they belong to this failed file, not to the next one
            drain()
        raise
    report.source = str(src)
    report.output = str(dst)
    return report


# ---------------------------------------------------------------------- file names

_NAME_SEP = re.compile(r"[_+=,;~#()\[\]{}]")   # shown to the detector as spaces
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')

# --- bare person names in file names --------------------------------------------------
# Clinics name files after patients ("김민서.jpg", "박서준_외래기록.docx", "이도윤 소견서.pdf").
# Such a name has no context for the detector, so file and folder names get a stricter rule:
# a standalone Hangul word of 3 syllables (1-syllable surname + 2-syllable given name) or of
# 3-4 syllables with a 2-syllable surname (남궁, 황보 ...) is a name when its given-name part
# is made of syllables common in Korean given names. Medical/office words that start with a
# surname syllable are mostly 2 or 4+ syllables (진료기록, 조직검사, 장기요양) or contain a
# syllable that given names do not use (소견서, 진단서, 심전도); the rest are listed below.
_GIVEN_FIRST = frozenset(
    "가갑강건경계광귀규금기길끝나남다대덕도동로루막만말명문미민범병보복봉분사삼상샛서석선성세"
    "소수숙순슬승시아애양언연영예옥용우원월유윤은을이인일재점정종주준지진차창채철초춘칠태필하학"
    "한해행현형혜홍화효희")
_GIVEN_LAST = frozenset(
    "건결겸경국권규균근기길나남녀년늘덕동든라란람랑래련렬례록롱룡름리린림만명모무묵문미민배범별"
    "복봉분비빈빛삼상서석선섭성솔솜수숙순슬식실심아안애양연열엽영오옥온완용우욱운울웅원유윤율은"
    "이익인일임자재정조종주준중지진찬창채천철춘탁태택필하학한해향혁현형혜호홍화환효후훈흠희")
# Common words that would otherwise look like "surname + given name" in a file name.
_FILENAME_NON_NAMES = frozenset("""
구성원 조사원 연수원 정보원 백만원 홍보지 여행지 주소지 소재지 주문지 공지문 공문서 지원서 진정서
전원서 최종안 동영상 성분명 주소록 방문록 진행도 사용자 지원자 방문자 신규자 고용자 주문자 장애자
소유자 주사기 정수기 공유기 복사기 단말기 동기화 최우선
""".split())
_HANGUL_WORD = re.compile(r"(?<![가-힣])[가-힣]{3,5}(?![가-힣])")
# One syllable that may follow a name inside a file-name word: "김민서의 소견서", "김민서외 2명".
_NAME_TAILS = frozenset("의님씨외께")


@functools.lru_cache(maxsize=1)
def _names_lists() -> tuple[frozenset, frozenset, frozenset, tuple, frozenset]:
    """(1-syllable surnames, 2-syllable surnames, stopwords, non-name suffixes, finals)."""
    try:
        from ..recognizers import names as N
    except ImportError:  # pragma: no cover - broken install: surnames only
        return frozenset("김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구"
                         "민진지엄채원천방공현함변염여추도소석선설마길연위표명기반라왕금옥육인"), \
            frozenset(), frozenset(), (), frozenset()
    stop = frozenset(getattr(N, "STOPWORDS", ())) | frozenset(getattr(N, "_LABEL_WORDS", ()))
    return (frozenset(getattr(N, "SURNAMES1", ())), frozenset(getattr(N, "SURNAMES2", ())), stop,
            tuple(getattr(N, "_NONNAME_SUFFIXES", ())), frozenset(getattr(N, "_NONNAME_FINALS", "")))


def looks_like_bare_name(word: str) -> bool:
    """True when a standalone Hangul word of a FILE NAME is probably a person's name.

    Stricter than masking every surname-initial word (which would hide 진료기록 or 문진표) and
    looser than the prose recognizers (which need context such as "환자" or "님").
    """
    word = unicodedata.normalize("NFC", word)
    if not 3 <= len(word) <= 4 or not all("가" <= c <= "힣" for c in word):
        return False
    sur1, sur2, stop, suffixes, finals = _names_lists()
    if word in stop or word in _FILENAME_NON_NAMES:
        return False
    if word[-1] in finals or (suffixes and word.endswith(suffixes)):
        return False   # 피로감, 신장염, 위궤양 ...
    givens = []
    if len(word) == 3 and word[0] in sur1:
        givens.append(word[1:])
    if word[:2] in sur2:
        givens.append(word[2:])
    for g in givens:
        if len(g) == 2 and g[0] in _GIVEN_FIRST and g[1] in _GIVEN_LAST:
            return True
        if len(g) == 1 and (g in _GIVEN_FIRST or g in _GIVEN_LAST):
            return True   # 남궁민, 선우진
    return False


def guess_name_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of standalone Hangul words in a file/folder name that look like names
    (a trailing 의/님/씨/외/께 is left out of the span)."""
    out = []
    for m in _HANGUL_WORD.finditer(text):
        word = m.group()
        if looks_like_bare_name(word):
            out.append(m.span())
        elif len(word) >= 4 and word[-1] in _NAME_TAILS and looks_like_bare_name(word[:-1]):
            out.append((m.start(), m.end() - 1))
    return out


def _vault_spans(text: str, anonymizer: "Anonymizer") -> list[tuple[int, int, str]]:
    """(start, end, token) for every pseudonymized original found verbatim in `text`."""
    vault = getattr(anonymizer, "vault", None)
    out = []
    for token, original in list(getattr(vault, "tokens", {}).items()):
        value = (original or "").strip()
        if len(value) < 2:
            continue
        start = text.find(value)
        while start >= 0:
            out.append((start, start + len(value), token))
            start = text.find(value, start + 1)
    return out


def deid_filename(name: str, detector: "Detector", anonymizer: "Anonymizer", *,
                  guess_names: bool = True) -> str:
    """De-identify a file or folder name (without extension).

    Runs the detector on the name (separators like "_" shown as spaces), replaces every value
    the anonymizer's vault already pseudonymized (names found in the file's content) with the
    same tokens and - unless `guess_names` is False - also treats bare words that look like
    a person's name (:func:`guess_name_spans`: "김민서", "박서준_외래기록") as PERSON, because a
    file name has no context. Characters that are unsafe in file names are replaced. The
    result is NFC (macOS often stores Hangul file names decomposed).
    """
    if not name:
        return name
    name = unicodedata.normalize("NFC", name)
    probe = _NAME_SEP.sub(" ", name)
    found: list[tuple[int, int, str]] = []
    for sp in detector.detect(probe):
        span = Span(sp.start, sp.end, sp.entity, name[sp.start:sp.end], sp.score, sp.source)
        rep, action = anonymizer.replacement_for(span)
        if action != "keep":
            found.append((sp.start, sp.end, rep))
    found += _vault_spans(name, anonymizer)
    cfg = getattr(detector, "cfg", None)
    if guess_names and (cfg is None or cfg.is_enabled(E.PERSON)):
        allow = set(getattr(cfg, "allowlist", None) or ())
        for s, e in guess_name_spans(probe):
            word = name[s:e]
            if word in allow or any(s < fe and fs < e for fs, fe, _ in found):
                continue
            rep, action = anonymizer.replacement_for(Span(s, e, E.PERSON, word, 0.6, "filename"))
            if action != "keep":
                found.append((s, e, rep))
    if not found:
        return name
    # Longest first, then leftmost; drop overlapping matches.
    chosen: list[tuple[int, int, str]] = []
    for s, e, rep in sorted(found, key=lambda t: (t[0] - t[1], t[0])):
        if all(e <= cs or s >= ce for cs, ce, _ in chosen):
            chosen.append((s, e, rep))
    parts, pos = [], 0
    for s, e, rep in sorted(chosen):
        parts += (name[pos:s], _UNSAFE_NAME.sub("_", rep))
        pos = e
    parts.append(name[pos:])
    out = "".join(parts).strip() or "deid"
    return "_" + out[1:] if out.startswith(".") else out


def _unique(path: Path, taken: set[Path] | None) -> Path:
    if taken is None or path not in taken:
        return path
    stem = path.stem[: -len(OUTPUT_TAG)] if path.stem.endswith(OUTPUT_TAG) else path.stem
    n = 2
    while True:
        cand = path.with_name(f"{stem}-{n}{OUTPUT_TAG}{path.suffix}")
        if cand not in taken:
            return cand
        n += 1


def process_to_dir(src: str | os.PathLike, outdir: str | os.PathLike, detector: "Detector",
                   anonymizer: "Anonymizer", cfg: "Config", *,
                   rel_dir: str | os.PathLike | None = None, taken: set[Path] | None = None,
                   **kw: Any) -> Report:
    """De-identify `src` into `outdir[/rel_dir]/<de-identified stem>.deid<ext>`.

    The handler writes to a hidden temporary name first; the final name is chosen after
    processing, so names found in the content are also removed from the file name and from
    `rel_dir` folder names. `taken` (a set shared by one run) avoids two inputs writing the
    same output; an output left by an earlier run is replaced. Report.output is the final
    path.
    """
    src, outdir = Path(src), Path(outdir)
    if handler_name(src) is None:
        raise UnsupportedFormatError(unsupported_message(src))
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = outdir / f".deid-{secrets.token_hex(6)}.part{src.suffix.lower()}"
    try:
        report = process_file(src, tmp, detector, anonymizer, cfg, **kw)
        folder = outdir
        for part in Path(rel_dir).parts if rel_dir else ():
            if part in ("", ".", ".."):
                continue
            folder = folder / deid_filename(part, detector, anonymizer)
        stem = deid_filename(src.stem, detector, anonymizer)
        final = _unique(folder / f"{stem}{OUTPUT_TAG}{src.suffix}", taken)
        if _same_file(src, final):
            raise ValueError("출력 파일이 원본과 같습니다. 원본은 덮어쓰지 않습니다 "
                             "(output must differ from the input file)")
        folder.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
    finally:
        _remove(tmp)
    if taken is not None:
        taken.add(final)
    report.source = str(src)
    report.output = str(final)
    return report


# ---------------------------------------------------------------------- summaries


def format_counts(counts: Mapping[str, int] | Report, cfg: "Config | None" = None) -> str:
    """'이름 3, 전화번호 2, 날짜 1(유지)' - Korean labels, biggest first; entities whose action
    is 'keep' are marked (유지 = left unchanged)."""
    if isinstance(counts, Report):
        counts = counts.counts
    if not counts or not any(counts.values()):
        return "찾은 개인정보 없음"
    order = {e: i for i, e in enumerate(E.ALL)}
    merged: dict[tuple[str, bool], list[int]] = {}
    for ent, n in counts.items():
        if not n:
            continue
        label = cfg.label_for(ent) if cfg is not None else E.label(ent)
        keep = cfg is not None and cfg.action_for(ent) == "keep"
        item = merged.setdefault((label, keep), [0, len(order)])
        item[0] += n
        item[1] = min(item[1], order.get(ent, len(order)))
    ranked = sorted(merged.items(), key=lambda kv: (kv[0][1], -kv[1][0], kv[1][1], kv[0][0]))
    return ", ".join(f"{label} {n}{'(유지)' if keep else ''}" for (label, keep), (n, _) in ranked)


def iter_files(root: str | os.PathLike, skip_dirs: Iterable[str | os.PathLike] = ()
               ) -> Iterable[Path]:
    """Files under `root` (sorted, recursive) except hidden ones, Office lock files (~$x),
    deid outputs, anything inside `skip_dirs` and symbolic links (a link planted in a watched
    folder must not pull in a file from elsewhere, e.g. ~/Documents; linked folders are not
    entered either). A file named explicitly on the command line is still processed."""
    root = Path(root)
    skip = set()
    for d in skip_dirs:
        try:
            skip.add(Path(d).resolve())
        except OSError:
            pass
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if d.startswith(".") or d == "deid_out":
                continue
            try:
                if (here / d).resolve() in skip:
                    continue
            except OSError:
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            if f.startswith((".", "~$")) or is_deid_output(f):
                continue
            path = here / f
            if path.is_symlink():
                continue
            yield path


__all__ = (
    "SUPPORTED", "LEGACY", "UnsupportedFormatError", "handler_name", "is_supported", "is_legacy",
    "is_deid_output", "unsupported_message", "output_path_for", "process_file", "process_to_dir",
    "deid_filename", "guess_name_spans", "looks_like_bare_name", "format_counts", "iter_files",
    "supported_list",
)
