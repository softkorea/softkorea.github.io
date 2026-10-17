"""`deid` command line.

    deid file 진료기록.docx 검사결과.xlsx       -> deid_out/진료기록.deid.docx ...
    deid clip                                   클립보드 텍스트/이미지 비식별화 (macOS)
    deid restore                                LLM 답변의 [이름1] 등을 원래 값으로 복원 (macOS)
    deid shot                                   화면 일부 캡처 -> 가림 -> 클립보드 (macOS)
    deid text < in.txt > out.txt                표준입력 -> 표준출력
    deid watch ~/Deid/in ~/Deid/out             폴더 감시
    deid serve                                  로컬 웹 화면
    deid check out.deid.txt --llm gemma3:4b     로컬 LLM으로 남은 개인정보 점검
    deid doctor                                 설치 상태 점검
    deid key set deid-strata                    진료실 LLM 서버의 API 키를 키체인에 저장 (macOS)

Exit codes: 0 ok, 1 nothing done / suspicions found / cancelled, 2 error or a file failed.

With ``[ner] backend = "openai"`` (an OpenAI-compatible server such as Strata on the clinic
PC) every detecting command prints a warning to stderr once when the ORIGINAL text leaves this
computer; an LLM failure with ``on_error = "fail"`` stops the command with exit code 2 and no
output. API keys are never read from argv, printed or written anywhere by this module.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator, Sequence

from . import __version__, formats, mac

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .types import Report
    from .vault import Vault

EXIT_OK, EXIT_NOTHING, EXIT_FAIL = 0, 1, 2


def _width(text: str) -> int:
    """Terminal columns: Hangul and other wide characters take two."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _columns(rows: Sequence[tuple[str, str]], indent: int = 2) -> str:
    """Two aligned columns (command, explanation), aligned by display width."""
    col = max(_width(cmd) for cmd, _ in rows) + 3
    return "".join(f"{' ' * indent}{cmd}{' ' * (col - _width(cmd))}{what}\n" for cmd, what in rows)


EXAMPLES = _columns([
    ("deid file 진료기록.docx 검사결과.xlsx", "파일 비식별화 -> 같은 폴더의 deid_out 폴더에 저장"),
    ("deid file ~/Desktop/자료", "폴더 안의 파일을 모두 비식별화"),
    ("deid clip", "클립보드 텍스트/이미지 비식별화 (macOS)"),
    ("deid restore", "LLM 답변의 [이름1] 등을 원래 값으로 복원 (macOS)"),
    ("deid shot", "화면 일부 캡처 -> 가림 -> 클립보드 (macOS)"),
    ("deid text < 원본.txt > 결과.txt", "표준입력 -> 표준출력"),
    ("deid watch ~/Deid/in ~/Deid/out", "폴더 감시: 넣은 파일을 자동 비식별화"),
    ("deid serve", "브라우저에서 쓰는 로컬 화면"),
    ("deid check 결과.deid.txt", "로컬 LLM으로 남은 개인정보 점검"),
    ("deid doctor", "설치 상태 점검"),
    ("deid key set deid-strata", "진료실 LLM 서버의 API 키를 키체인에 저장 (macOS)"),
])
FILE_EXAMPLES = _columns([
    ("deid file 진료기록.docx", "-> deid_out/진료기록.deid.docx (원본 옆)"),
    ("deid file ~/Desktop/자료 -o ~/Desktop/공유용", "폴더 전체, 결과는 '공유용' 폴더에"),
    ("deid file 명단.xlsx --column \"담당=PERSON\"", "제목만으로 모르는 열을 이름 열로 지정"),
    ("deid file 자료 --vault ~/deid.vault", "여러 번 나눠 실행해도 같은 사람은 같은 가명"),
    ("deid file 자료 --dry-run", "저장하지 않고 찾은 개수만 보기"),
])
DEFAULT_LLM = "gemma3:4b"


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _err(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _utf8_stdio() -> None:
    """Korean must survive even when started without a UTF-8 locale (launchd, Automator)."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError, AttributeError):  # pragma: no cover
                pass


def _first_line(e: BaseException) -> str:
    text = str(e).strip()
    return text.splitlines()[0][:400] if text else type(e).__name__


# ---------------------------------------------------------------------- version


def _git_head(root: Path) -> tuple[str, str] | None:
    """(commit, branch) of the git checkout at `root`, read from the files in .git (no git
    command: on a Mac without developer tools that would open an install dialog)."""
    git = root / ".git"
    if git.is_file():     # worktree / submodule: "gitdir: <path>"
        line = git.read_text("utf-8", "replace").strip()
        if not line.startswith("gitdir:"):
            return None
        git = (root / line.split(":", 1)[1].strip()).resolve()
    if not git.is_dir():
        return None
    head = (git / "HEAD").read_text("utf-8", "replace").strip()
    if not head.startswith("ref:"):
        return (head[:40], "") if re.fullmatch(r"[0-9a-f]{40,64}", head) else None
    ref = head.split(":", 1)[1].strip()
    common = git
    if (git / "commondir").is_file():
        common = (git / (git / "commondir").read_text("utf-8", "replace").strip()).resolve()
    branch = ref.removeprefix("refs/heads/")
    for base in dict.fromkeys((git, common)):
        f = base / ref
        if f.is_file():
            sha = f.read_text("utf-8", "replace").strip()
            if re.fullmatch(r"[0-9a-f]{40,64}", sha):
                return sha, branch
    for base in dict.fromkeys((git, common)):
        packed = base / "packed-refs"
        if packed.is_file():
            for line in packed.read_text("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[0-9a-f]{40,64}", parts[0]):
                    return parts[0], branch
    return None


def _source_checkout() -> Path | None:
    """The source folder (tools/deid) when deid runs from it (editable install or PYTHONPATH),
    else None. An installed copy in site-packages never counts, even when a folder above it
    happens to be a git repository."""
    pkg = Path(__file__).resolve().parent
    if pkg.parent.name != "src" or not (pkg.parent.parent / "pyproject.toml").is_file():
        return None
    return pkg.parent.parent


def _strip_userinfo(url: str) -> str:
    """A URL without "user:token@" (pip records the URL exactly as typed)."""
    return re.sub(r"(?<=://)[^/@\s]*@", "", url)


def build_info() -> str:
    """Where this deid comes from, for `deid --version` and `deid doctor`: the git commit when
    running from a git checkout, else what pip recorded at install time (git commit and URL,
    or the folder), else the install folder. Best effort: never raises."""
    try:
        src = _source_checkout()
        if src is not None:
            for d in (src, *src.parents):
                if (d / ".git").exists():
                    head = _git_head(d)
                    if head:
                        sha, branch = head
                        return (f"git {sha[:7]}" + (f", 브랜치 {branch}" if branch else "")
                                + f", 소스 폴더 {src}")
                    break
            return f"소스 폴더 {src}"
    except Exception:  # noqa: BLE001
        pass
    try:
        from importlib import metadata

        dist = metadata.distribution("deid-kit")
        raw = dist.read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            url = _strip_userinfo(str(info.get("url", "")))
            sub = (info.get("subdirectory") or "").strip("/")
            where = url + (f"#subdirectory={sub}" if sub else "")
            vcs = info.get("vcs_info") or {}
            if vcs.get("commit_id"):
                return f"git {str(vcs['commit_id'])[:7]}, 설치 원본 {where}"
            if url.startswith("file://"):
                from urllib.parse import unquote, urlsplit

                folder = unquote(urlsplit(url).path)
                if (info.get("dir_info") or {}).get("editable"):
                    return f"편집 가능 설치 폴더 {folder}"
                # a folder install has no commit: the install time shows that an update took
                return f"설치 원본 폴더 {folder}" + _installed_at(dist)
            if where:
                return f"설치 원본 {where}"
    except Exception:  # noqa: BLE001
        pass
    try:
        return f"설치 위치 {Path(__file__).resolve().parent}"
    except Exception:  # noqa: BLE001  # pragma: no cover
        return "설치 정보 없음"


def _installed_at(dist: object) -> str:
    """", 설치 YYYY-MM-DD HH:MM" from the time pip wrote direct_url.json; "" when unknown."""
    try:
        path = getattr(dist, "_path", None)
        if path is None:
            return ""
        import time

        mtime = (Path(path) / "direct_url.json").stat().st_mtime
        return ", 설치 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    except Exception:  # noqa: BLE001
        return ""


def version_text() -> str:
    return f"deid {__version__} ({build_info()})"


class _VersionAction(argparse.Action):
    """--version, computed only when asked (build_info reads files)."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS,
                 help=None):  # noqa: A002, ANN001
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0,
                         help=help)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        parser._print_message(version_text() + "\n", sys.stdout)
        parser.exit()


# ---------------------------------------------------------------------- shared helpers


CONFIG_FIX_HINT = ("설정 파일을 고친 뒤 'deid doctor'로 확인하세요. 처음부터 다시 만들려면 "
                   "'deid init-config --force' (fix the file, then run 'deid doctor')")


def _load_cfg(path: str | None) -> "Config":
    """The settings for a command. A config file that does not load stops the command with
    its full (Korean) message and exit code 2 - never a traceback, never silently ignored."""
    from .config import load_config
    from .watch import config_error_text, config_location

    where, _ = config_location(path)
    try:
        return load_config(path)
    except FileNotFoundError as e:
        raise CliError(f"설정 파일을 찾을 수 없습니다 (config file not found): {where}") from e
    except Exception as e:  # noqa: BLE001 - TOML syntax, unknown key or entity, bad action ...
        raise CliError(f"설정 파일 오류 (config error): {where}\n  {config_error_text(e, where)}"
                       f"\n  -> {CONFIG_FIX_HINT}") from e


def _live_cfg(path: str | None):
    """Settings for the long-running commands (watch, serve): loaded now (a bad file stops the
    command), then re-read whenever the file changes."""
    from .watch import LiveConfig

    return LiveConfig(path, _load_cfg(path))


NO_TTY_PASS_ERROR = (
    "보관함 파일(--vault)의 암호를 물어볼 터미널 창이 없습니다. 터미널 앱에서 직접 실행하거나, "
    "빠른 동작·단축어·자동 실행에서는 DEID_VAULT_PASS 환경변수에 암호를 넣어 실행하세요 "
    "(예: DEID_VAULT_PASS='암호' deid restore --vault ~/deid.vault). 암호를 잊으면 그 보관함으로는 "
    "복원할 수 없습니다 (no terminal to ask for the vault passphrase: run it in Terminal or set "
    "DEID_VAULT_PASS)")


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())      # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def _can_prompt() -> bool:
    """True when a passphrase can be asked for: stdin is the terminal, or it is redirected
    ('deid text --restore --vault v < 답변.txt') while the command still runs in a terminal
    window - getpass then asks on /dev/tty. Never from Automator, Shortcuts or launchd."""
    if _isatty(sys.stdin):
        return True
    if not _isatty(sys.stderr):
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return False
    os.close(fd)
    return True


def _passphrase(confirm: bool) -> str:
    env = os.environ.get("DEID_VAULT_PASS")
    if env:
        return env
    if not _can_prompt():
        raise CliError(NO_TTY_PASS_ERROR)
    pw = getpass.getpass("보관함 암호 (vault passphrase): ")
    if not pw:
        raise CliError("암호가 비어 있습니다 (empty passphrase)")
    if confirm and getpass.getpass("암호 확인 (repeat passphrase): ") != pw:
        raise CliError("두 암호가 다릅니다 (passphrases do not match)")
    return pw


def _load_file_vault(path: str, create: bool) -> tuple["Vault", str]:
    from .vault import Vault, VaultError

    p = Path(path).expanduser()
    if p.exists():
        pw = _passphrase(confirm=False)
        try:
            return Vault.load(p, pw), pw
        except VaultError as e:
            raise CliError("보관함을 열 수 없습니다: 암호가 틀렸거나 파일이 손상되었습니다 "
                           "(wrong passphrase or damaged vault)") from e
        except OSError as e:
            raise CliError(f"보관함 파일을 읽을 수 없습니다 (cannot read vault): {_first_line(e)}") from e
    if not create:
        raise CliError(f"보관함 파일이 없습니다 (vault file not found): {p}")
    return Vault(), _passphrase(confirm=True)


@contextmanager
def _vault_scope(vault_path: str | None, *, save: bool = True, create: bool = True
                 ) -> Iterator["Vault"]:
    """--vault FILE (passphrase) or the clipboard session vault (no passphrase)."""
    if vault_path:
        vault, pw = _load_file_vault(vault_path, create)
        before = len(vault)
        try:
            yield vault
        finally:
            # Also after an error or Ctrl-C: outputs already written use these tokens, and
            # the next run must give the same people the same tokens.
            if save and (len(vault) != before or not Path(vault_path).expanduser().exists()):
                p = Path(vault_path).expanduser()
                p.parent.mkdir(parents=True, exist_ok=True)
                vault.save(p, pw)
        return
    from .session import SessionError, open_session

    try:
        with open_session(save=save) as vault:
            yield vault
    except SessionError as e:
        raise CliError(str(e)) from e


def _detector(cfg: "Config", vault: "Vault | None" = None):
    from .engine import Detector
    from .session import seed_detector

    det = Detector(cfg)
    if vault is not None and len(vault):
        seed_detector(det, vault)
    return det


def _session_detector(cfg: "Config"):
    """Detector seeded with the clipboard session's names (images: tokens are not drawn, so
    nothing is saved). A name pasted earlier with context ("환자 홍길동") is then also covered
    in a screenshot where it has none. An unreadable session only means less seeding."""
    try:
        from .session import open_session

        with open_session(save=False) as vault:
            return _detector(cfg, vault)
    except Exception:  # noqa: BLE001 - seeding is a bonus, never a reason to fail
        return _detector(cfg)


def _read_stdin() -> str:
    try:
        if sys.stdin.isatty():
            _err("텍스트를 붙여넣고 Ctrl-D를 누르세요 (paste text, then press Ctrl-D)")
    except (AttributeError, ValueError, OSError):
        pass
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        return sys.stdin.read()
    data = buf.read()
    from .formats.text import detect_encoding

    try:
        return detect_encoding(data).decode(data)
    except ValueError as e:
        raise CliError(f"입력 텍스트를 읽을 수 없습니다 (cannot decode input): {_first_line(e)}") from e


def _write_stdout(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:  # pragma: no cover - `deid text | head`
        pass


def _summary(report: "Report", cfg: "Config") -> str:
    return formats.format_counts(report, cfg)


def _notify(enabled: bool, title: str, message: str) -> None:
    if enabled:
        mac.notify(title, message)


# ---------------------------------------------------------------------- LLM server ([ner]/[llm])

# LLMError kinds after which the remaining files of a `deid file` run are not tried: the server
# is unreachable or refuses the key, so every file would fail the same way (and wait for the
# timeout each time).
_LLM_STOP_KINDS = frozenset({"key", "auth", "timeout", "connection", "redirect", "not_found"})
LLM_HINT = ("-> 'deid doctor'로 LLM 서버 연결과 API 키를 확인하세요 (run 'deid doctor' to check "
            "the LLM server and the API key)")
_notices_shown: set[str] = set()


def _transport_notice(cfg: "Config") -> None:
    """Warn on stderr, once per command, when [ner] sends the ORIGINAL text to an LLM server
    outside this computer (an OpenAI-compatible backend such as Strata on the clinic PC)."""
    from .watch import transport_notice

    msg = transport_notice(cfg)
    if msg and msg not in _notices_shown:
        _notices_shown.add(msg)
        _err(f"! {msg}")


def _is_llm_error(e: BaseException) -> bool:
    """True for backends.openai_compat.LLMError (server down, key refused, no usable answer).
    Its message never contains document text or the API key."""
    if type(e).__name__ != "LLMError":
        return False
    try:
        from .backends.openai_compat import LLMError
    except Exception:  # noqa: BLE001  # pragma: no cover
        return False
    return isinstance(e, LLMError)


def _drain(detector: object) -> list[str]:
    """Pending detector warnings (LLM backend with on_error = "rules_only"), removed from it."""
    drain = getattr(detector, "drain_warnings", None)
    return list(drain()) if callable(drain) else []


def _llm_fell_back(warnings: Iterable[str]) -> bool:
    """True when the LLM pass failed and on_error = "rules_only" applied only the rules."""
    from .backends.openai_compat import is_fallback_warning

    return any(is_fallback_warning(w) for w in warnings)


def _fallback_note(warnings: Iterable[str]) -> str:
    """" - LLM 탐지 실패: 규칙만 적용" for notifications (stderr is not seen from a keyboard
    shortcut or a Finder Quick Action), else ""."""
    from .backends.openai_compat import FALLBACK_NOTE

    return f" - {FALLBACK_NOTE}" if _llm_fell_back(warnings) else ""


# ---------------------------------------------------------------------- deid file


@dataclass
class _Job:
    src: Path
    outdir: Path
    rel_dir: Path | None
    base: Path            # display paths relative to this


def _display(path: Path, base: Path) -> str:
    for root in (base, Path.cwd()):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(path)


def _collect(paths: Sequence[str], outdir: str | None
             ) -> tuple[list[_Job], list[tuple[str, str]], dict[str, int]]:
    """(jobs, failures [(display, message)], skipped {ext: n}) for `deid file` arguments."""
    jobs: list[_Job] = []
    failures: list[tuple[str, str]] = []
    skipped: dict[str, int] = {}
    out_override = Path(outdir).expanduser().absolute() if outdir else None
    for raw in paths:
        p = Path(raw).expanduser().absolute()
        if p.is_dir():
            out = out_override or p / "deid_out"
            n_before = len(jobs) + len(failures)
            for f in formats.iter_files(p, skip_dirs=[out]):
                rel = f.parent.relative_to(p)
                if formats.is_supported(f):
                    jobs.append(_Job(f, out, None if str(rel) == "." else rel, p))
                elif formats.is_legacy(f):
                    failures.append((_display(f, p), formats.unsupported_message(f)))
                else:
                    ext = f.suffix.lower() or "(없음)"
                    skipped[ext] = skipped.get(ext, 0) + 1
            if len(jobs) + len(failures) == n_before:
                _err(f"{raw}: 처리할 수 있는 파일이 없습니다 (no supported files in this folder)")
        elif p.is_file():
            if formats.is_supported(p):
                jobs.append(_Job(p, out_override or p.parent / "deid_out", None, p.parent))
            else:
                failures.append((p.name, formats.unsupported_message(p)))
        else:
            failures.append((raw, "파일이나 폴더가 없습니다 (no such file or folder)"))
    return jobs, failures, skipped


def _column_types(items: Sequence[str] | None, cfg: "Config | None" = None
                  ) -> dict[str, str] | None:
    """--column HEADER=TYPE options, checked together with the config's [columns] table.

    Returns only the command-line part (the spreadsheet handler adds cfg.columns itself);
    an unknown type in either place stops the run before any file is touched.
    """
    out = {}
    for item in items or ():
        header, sep, kind = item.partition("=")
        if not sep or not header.strip() or not kind.strip():
            raise CliError(f"--column 형식은 '열이름=유형' 입니다 (expected HEADER=TYPE): {item}")
        out[header.strip()] = kind.strip()
    merged = dict(getattr(cfg, "columns", None) or {})
    merged.update(out)
    if merged:
        from .formats.tabular import parse_column_kind

        for header, kind in merged.items():
            try:
                parse_column_kind(kind)
            except ValueError:
                where = "--column" if header in out else "설정 파일 [columns] (config)"
                raise CliError(f"{where}: '{header}' 열의 유형 '{kind}'을(를) 알 수 없습니다. "
                               "PERSON, PHONE, RRN, ADDRESS, EMAIL, MRN, BIRTHDATE 같은 항목 이름이나 "
                               "skip(그대로 두기), text(자동 탐지)를 쓰세요 "
                               f"(unknown column type {kind!r})") from None
    return out or None


def cmd_file(args: argparse.Namespace) -> int:
    from .anonymizer import Anonymizer

    cfg = _load_cfg(args.config)
    columns = _column_types(args.column, cfg)
    jobs, failures, skipped = _collect(args.paths, args.outdir)
    as_json = args.report == "json"
    results: list[dict] = []
    for disp, msg in failures:
        if not as_json:
            _err(f"FAIL {disp}: {msg}")
        results.append({"source": disp, "ok": False, "error": msg})
    n_ok = 0
    n_stopped = 0
    llm_failed = False
    fallback_warnings: list[str] = []   # Report.warnings of the files done (for --notify)
    if jobs:
        _transport_notice(cfg)
        if args.vault or args.session:   # --vault wins over --session
            scope = _vault_scope(args.vault or None, save=not args.dry_run)
        else:
            scope = _null_vault()
        with scope as vault:
            det = _detector(cfg, vault)
            anonymizer = Anonymizer(cfg, vault)
            taken: set[Path] = set()
            stopped = llm_failed = False
            for job in jobs:
                disp = _display(job.src, job.base)
                if stopped:   # the LLM server is down or refuses the key: do not even try
                    msg = ("처리하지 않음 - 앞 파일에서 LLM 서버 문제가 있었습니다 (not processed: "
                           "LLM server problem)")
                    results.append({"source": str(job.src), "ok": False, "error": msg})
                    failures.append((disp, msg))
                    n_stopped += 1
                    continue
                try:
                    if args.dry_run:
                        with tempfile.TemporaryDirectory(prefix="deid-dry-") as td:
                            report = formats.process_to_dir(job.src, td, det, anonymizer, cfg,
                                                             rel_dir=job.rel_dir,
                                                             column_types=columns)
                        report.output = ""
                    else:
                        report = formats.process_to_dir(job.src, job.outdir, det, anonymizer, cfg,
                                                        rel_dir=job.rel_dir, taken=taken,
                                                        column_types=columns)
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # noqa: BLE001 - report and continue with the next file
                    _drain(det)   # warnings of the failed file must not reach the next one
                    msg = _first_line(e)
                    if not as_json:
                        _err(f"FAIL {disp}: {msg}")
                    results.append({"source": str(job.src), "ok": False, "error": msg})
                    failures.append((disp, msg))
                    if _is_llm_error(e):
                        llm_failed = True
                        stopped = getattr(e, "kind", "") in _LLM_STOP_KINDS
                    continue
                # e.g. from the de-identified file name (one more detection after the content)
                report.warnings.extend(w for w in _drain(det) if w not in report.warnings)
                fallback_warnings.extend(report.warnings)
                n_ok += 1
                summary = _summary(report, cfg)
                entry = report.to_dict()
                entry.update(ok=True, summary=summary)
                results.append(entry)
                if as_json:
                    continue
                if args.dry_run:
                    _out(f"{disp}: {summary}")
                else:
                    _out(f"OK {disp} -> {_display(Path(report.output), job.base)} ({summary})")
                for w in report.warnings:
                    _out(f"   ! {w}")
    n_fail = len(failures)
    if as_json:
        _out(json.dumps({"files": results, "ok": n_ok, "failed": n_fail, "skipped": skipped,
                         "dry_run": bool(args.dry_run)}, ensure_ascii=False, indent=2))
    else:
        if skipped:
            shown = ", ".join(f"{ext} {n}" for ext, n in sorted(skipped.items()))
            _err(f"건너뜀 (skipped, unsupported): {shown}")
        if n_stopped:
            _err(f"LLM 서버 문제로 나머지 파일 {n_stopped}개는 처리하지 않았습니다 (stopped: {n_stopped} "
                 f"file(s) not processed)\n  {LLM_HINT}")
        elif llm_failed:
            _err(f"  {LLM_HINT}")
        if n_ok + n_fail:
            _out(f"완료: 성공 {n_ok}개, 실패 {n_fail}개 (done: {n_ok} ok, {n_fail} failed)")
        if n_ok and not args.dry_run:
            _out("자동 탐지는 완벽하지 않습니다. 공유하기 전에 결과를 확인하세요.")
    if args.notify:   # counts only: file names may contain patient names (lock screen)
        _notify(True, "deid 실패 있음" if n_fail else "deid 비식별화 완료",
                f"성공 {n_ok}개, 실패 {n_fail}개" + _fallback_note(fallback_warnings)
                + (" - 결과를 확인하세요" if n_ok else ""))
    if n_fail:
        return EXIT_FAIL
    return EXIT_OK if n_ok else EXIT_NOTHING


@contextmanager
def _null_vault() -> Iterator["Vault"]:
    from .vault import Vault

    yield Vault()


# ---------------------------------------------------------------------- text / clipboard


def cmd_text(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args.config)
    text = _read_stdin()
    if args.restore:
        from .session import count_tokens, unknown_tokens

        with _vault_scope(args.vault, save=False, create=False) as vault:
            in_links = vault.tokens_in_links(text)
            n = count_tokens(text, vault) - in_links
            unknown = unknown_tokens(text, vault, cfg)
            out = vault.restore(text, links=False)
        _write_stdout(out)
        if not args.quiet:
            _err(f"복원 (restored): {n}개" + (f", 복원 못 한 토큰 {unknown}개" if unknown else "")
                 + (f", {_links_note(in_links)}" if in_links else ""))
        return EXIT_OK
    from .anonymizer import Anonymizer
    from .formats.text import deid_text

    _transport_notice(cfg)
    with _vault_scope(args.vault) as vault:
        try:
            out, report = deid_text(text, _detector(cfg, vault), Anonymizer(cfg, vault))
        except RuntimeError as e:  # a [ner] backend that is not installed, an LLM server that
            # failed with on_error = "fail" (LLMError): no output at all
            hint = LLM_HINT if _is_llm_error(e) else "-> 'deid doctor'로 확인하세요 (run 'deid doctor')"
            raise CliError(f"비식별화하지 못했습니다 - 아무것도 출력하지 않았습니다 (failed; nothing "
                           f"written): {e}\n  {hint}") from e
    _write_stdout(out)
    if not args.quiet:
        _err(f"비식별화 (de-identified): {_summary(report, cfg)}")
        for w in report.warnings:
            _err(f"   ! {w}")
    return EXIT_OK


def _links_note(k: int) -> str:
    return (f"링크(URL) 안의 가명 {k}개는 복원하지 않음 - 링크를 통해 원래 값이 밖으로 나가지 않도록 "
            f"({k} token(s) inside links left as they are)")


def _clip_failed(notify: bool, detail: str, note: str = "처리하지 못해 클립보드를 비웠습니다") -> int:
    """Empty the clipboard so the original cannot be pasted by mistake, and say so."""
    mac.clear_clipboard()
    _err(f"비식별화 실패 - 실수로 붙여넣지 않도록 클립보드를 비웠습니다 (failed; clipboard cleared): "
         f"{detail}")
    _notify(notify, "deid 실패", note)
    return EXIT_FAIL


def _clip_image(cfg: "Config", notify: bool) -> int:
    from .anonymizer import Anonymizer

    with tempfile.TemporaryDirectory(prefix="deid-clip-") as td:
        src, dst = Path(td) / "clipboard.png", Path(td) / "clipboard.deid.png"
        if not mac.get_clipboard_image(src):
            raise CliError("클립보드 이미지를 읽을 수 없습니다 (cannot read the clipboard image)")
        report = formats.process_file(src, dst, _session_detector(cfg), Anonymizer(cfg), cfg)
        mac.set_clipboard_image(dst)
    summary = _summary(report, cfg)
    _err(f"이미지 가림 완료 (image redacted): {summary}")
    for w in report.warnings:
        _err(f"   ! {w}")
    _notify(notify, "deid 이미지 가림 완료",
            f"{summary}{_fallback_note(report.warnings)} - 붙여넣기 전에 확인하세요")
    return EXIT_OK


def _clip(cfg: "Config", notify: bool) -> int:
    info = mac.clipboard_info()
    if mac.info_has_files(info):
        # A copied Finder file would be pasted (uploaded) as the original: clear it too.
        mac.clear_clipboard()
        _err("클립보드에 복사된 파일은 비식별화할 수 없어 실수로 원본을 붙여넣지 않도록 클립보드를 "
             "비웠습니다. 파일은 'deid file' 또는 Finder 빠른 동작을 사용하세요 "
             "(the clipboard held files: cleared; use 'deid file')")
        _notify(notify, "deid", "복사한 파일은 처리할 수 없어 클립보드를 비웠습니다 - 빠른 동작을 쓰세요")
        return EXIT_NOTHING
    text = mac.paste_text()
    if not text.strip():
        if mac.info_has_image(info):
            _transport_notice(cfg)
            return _clip_image(cfg, notify)
        if not text and info.strip():
            # Neither text nor an image (e.g. PDF data): we cannot check it, so it must go.
            mac.clear_clipboard()
            _err("클립보드 내용을 읽을 수 없는 형식이라 실수로 붙여넣지 않도록 비웠습니다 "
                 "(unsupported clipboard content: cleared)")
            _notify(notify, "deid", "처리할 수 없는 형식이라 클립보드를 비웠습니다")
            return EXIT_NOTHING
        _err("클립보드가 비어 있습니다 (clipboard is empty)")
        _notify(notify, "deid", "클립보드가 비어 있습니다")
        return EXIT_NOTHING
    from .anonymizer import Anonymizer
    from .formats.text import deid_text

    _transport_notice(cfg)
    with _vault_scope(None) as vault:
        out, report = deid_text(text, _detector(cfg, vault), Anonymizer(cfg, vault))
    mac.copy_text(out)
    summary = _summary(report, cfg)
    _err(f"비식별화 완료, 클립보드에 복사함 (de-identified, copied): {summary}")
    for w in report.warnings:
        _err(f"   ! {w}")
    _notify(notify, "deid 비식별화 완료",
            f"{summary}{_fallback_note(report.warnings)} - 붙여넣기 전에 확인하세요")
    return EXIT_OK


def cmd_clip(args: argparse.Namespace) -> int:
    """Every failure - config error, unreadable clipboard, OCR, Keychain - empties the
    clipboard: with a keyboard shortcut the next Cmd+V would otherwise paste the original."""
    mac.require_macos("deid clip")
    try:
        cfg = _load_cfg(args.config)
    except CliError as e:   # the notification never quotes the config file (it holds names)
        return _clip_failed(args.notify, e.message,
                            "설정 파일 오류로 처리하지 못해 클립보드를 비웠습니다 - deid doctor로 확인하세요")
    try:
        return _clip(cfg, args.notify)
    except KeyboardInterrupt:
        mac.clear_clipboard()
        raise
    except Exception as e:  # noqa: BLE001 - never leave the original in place silently
        detail = e.message if isinstance(e, CliError) else _first_line(e)
        if _is_llm_error(e):   # the server is down or refused the key (on_error = "fail")
            return _clip_failed(args.notify, f"{detail}\n  {LLM_HINT}",
                                "LLM 서버 문제로 처리하지 못해 클립보드를 비웠습니다 - deid doctor로 "
                                "확인하세요")
        keychain = "키체인" in detail
        return _clip_failed(args.notify, detail,
                            "키체인이 잠겨 있어 처리하지 못했습니다. 잠금을 풀고 다시 시도하세요 "
                            "(클립보드를 비웠습니다)" if keychain else
                            "처리하지 못해 클립보드를 비웠습니다")


def cmd_restore(args: argparse.Namespace) -> int:
    if not mac.is_macos():
        raise CliError("'deid restore'는 macOS 클립보드용입니다. 다른 환경에서는 "
                       "'deid text --restore < 답변.txt' (보관함 파일이면 'deid text --restore --vault "
                       "파일 < 답변.txt')를 사용하세요 (macOS only; use 'deid text --restore')")
    try:
        return _restore_clipboard(args)
    except CliError as e:
        # From a Quick Action or Shortcut nobody sees stderr: say it in a notification (no
        # file names or values - the message is one of ours).
        _notify(args.notify, "deid 복원 실패",
                "보관함 암호가 필요합니다 - 터미널에서 실행하거나 DEID_VAULT_PASS를 지정하세요"
                if e.message == NO_TTY_PASS_ERROR else "복원하지 못했습니다 - 터미널에서 확인하세요")
        raise


def _restore_clipboard(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args.config)
    from .session import count_tokens, unknown_tokens

    text = mac.paste_text()
    if not text.strip():
        _err("클립보드가 비어 있습니다 (clipboard is empty)")
        return EXIT_NOTHING
    with _vault_scope(args.vault, save=False, create=False) as vault:
        if not len(vault):
            if args.vault:
                _err("보관함 파일에 가명이 없습니다 (the vault file holds no tokens)")
            else:
                _err("복원할 정보가 없습니다. 세션이 만료(마지막 사용 후 기본 12시간)되었거나 "
                     "삭제되었습니다. 파일을 --vault로 비식별화했다면 'deid restore --vault 파일'을 "
                     "쓰세요 (no session tokens: expired or forgotten)")
            _notify(args.notify, "deid 복원", "복원할 가명 정보가 없습니다")
            return EXIT_NOTHING
        in_links = vault.tokens_in_links(text)
        n = count_tokens(text, vault) - in_links
        unknown = unknown_tokens(text, vault, cfg)
        out = vault.restore(text, links=False)
    if n:
        mac.copy_text(out)
    msg = (f"복원 {n}개" + (f", 복원 못 한 토큰 {unknown}개 (다른 세션/파일의 토큰)" if unknown else "")
           + (f", {_links_note(in_links)}" if in_links else ""))
    if n:
        _err(f"{msg} - 클립보드에 복사함 (restored, copied)")
    else:
        _err("복원할 토큰이 없습니다 (no known tokens in the clipboard)" +
             (f"; 모르는 토큰 {unknown}개" if unknown else ""))
    _notify(args.notify, "deid 복원", msg if n else "복원할 토큰이 없습니다")
    return EXIT_OK if n else EXIT_NOTHING


def _unique_file(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _shot_from_clipboard(before: "tuple[int | None, str] | None", raw: Path) -> bool | None:
    """After a capture that saved no file: None if it was cancelled; True when the screenshot
    went to the clipboard instead (Control held while selecting) and was read into `raw`;
    False when it went there but could not be read."""
    if before is None:
        return None
    after = mac.clipboard_state()
    if not (mac.clipboard_changed(before, after) and mac.info_has_image(after[1])):
        return None
    return mac.get_clipboard_image(raw)


def cmd_shot(args: argparse.Namespace) -> int:
    from .anonymizer import Anonymizer
    from .session import prepare_work_dir, user_dir

    mac.require_macos("deid shot")
    cfg = _load_cfg(args.config)
    mode = "window" if args.window else "full" if args.full else "selection"
    outdir = Path(args.outdir or os.environ.get("DEID_SHOT_DIR") or user_dir() / "out")
    outdir = outdir.expanduser()
    # Holding Control during an interactive capture makes screencapture put the RAW image
    # on the clipboard and save no file; remember the clipboard to notice that.
    before = mac.clipboard_state() if mode != "full" else None
    with tempfile.TemporaryDirectory(prefix="deid-shot-") as td:
        raw = Path(td) / "shot.png"
        from_clipboard = False
        if not mac.screencapture(raw, mode):
            got = _shot_from_clipboard(before, raw)
            if got is None:
                return EXIT_NOTHING   # cancelled (Esc): quietly
            from_clipboard = True
            if not got:
                mac.clear_clipboard()
                _err("캡처가 클립보드로 들어갔지만 읽을 수 없어 클립보드를 비웠습니다 (Control 키를 누른 채 "
                     "선택하면 원본이 클립보드로 갑니다) (capture went to the clipboard: cleared)")
                _notify(args.notify, "deid 실패", "캡처를 처리하지 못해 클립보드를 비웠습니다")
                return EXIT_FAIL
        _transport_notice(cfg)
        try:
            prepare_work_dir(outdir)
            dst = _unique_file(outdir / f"shot-{datetime.now():%Y%m%d-%H%M%S}.png")
            report = formats.process_file(raw, dst, _session_detector(cfg), Anonymizer(cfg), cfg)
        except Exception as e:  # noqa: BLE001
            if from_clipboard:
                mac.clear_clipboard()
            _err(f"스크린샷 비식별화 실패, 저장하지 않았습니다 (screenshot failed, not saved): "
                 f"{_first_line(e)}" + (" - 클립보드를 비웠습니다" if from_clipboard else "")
                 + (f"\n  {LLM_HINT}" if _is_llm_error(e) else ""))
            _notify(args.notify, "deid 실패", "스크린샷을 처리하지 못했습니다")
            return EXIT_FAIL
    summary = _summary(report, cfg)
    copied = False
    if not args.no_clipboard:
        try:
            mac.set_clipboard_image(dst)
            copied = True
        except mac.MacError as e:
            _err(f"   ! {e}")
    if from_clipboard and not copied:
        mac.clear_clipboard()   # the raw capture must not stay there
    if from_clipboard:
        _err("   ! Control 키를 누른 채 선택해 캡처가 클립보드로 갔습니다. 원본 대신 가린 이미지로 "
             "바꿨습니다 (capture went to the clipboard; replaced with the redacted image)")
    _out(f"OK {dst} ({summary})" + (" - 클립보드에 복사함" if copied else ""))
    for w in report.warnings:
        _err(f"   ! {w}")
    _notify(args.notify, "deid 스크린샷 가림 완료",
            f"{summary}{_fallback_note(report.warnings)} - 결과를 확인하세요")
    return EXIT_OK


# ---------------------------------------------------------------------- watch / serve


def cmd_watch(args: argparse.Namespace) -> int:
    from .watch import run_watch

    return run_watch(args.in_dir, args.out_dir, interval=args.interval,
                     cfg=_live_cfg(args.config), notify=args.notify)


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    return serve(args.port, open_browser=not args.no_browser, cfg=_live_cfg(args.config))


def _deid_command() -> str | list[str]:
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name == "deid" and argv0.is_file():
        return str(argv0.absolute())
    found = shutil.which("deid")
    if found:
        return str(Path(found).absolute())
    return [sys.executable, "-m", "deid"]


def cmd_install_watch(args: argparse.Namespace) -> int:
    from .session import prepare_work_dir, user_dir

    in_dir = Path(args.in_dir or user_dir() / "in").expanduser().absolute()
    out_dir = Path(args.out_dir or user_dir() / "out").expanduser().absolute()
    plist = mac.watch_agent_plist(_deid_command(), in_dir, out_dir, notify=not args.no_notify)
    if args.print_only:
        _write_stdout(plist.decode("utf-8"))
        return EXIT_OK
    if args.uninstall:
        removed = mac.uninstall_watch_agent()
        _out("자동 감시를 제거했습니다 (watch agent removed)" if removed else
             "설치된 자동 감시가 없습니다 (no watch agent installed)")
        return EXIT_OK
    mac.require_macos("deid install-watch")
    # The agent reads the default config file: one that does not load would stop it at every
    # start (launchd retries every 30 s), so refuse now with the reason.
    cfg = _load_cfg(None)
    _transport_notice(cfg)      # the agent will send originals to the LLM server too
    prepare_work_dir(in_dir)    # owner-only (0700): other accounts must not read originals
    prepare_work_dir(out_dir)
    path = mac.install_watch_agent(plist)
    _out(f"자동 감시를 설치했습니다 (installed): {path}")
    _out(f"  넣는 곳 (in):  {in_dir}")
    _out(f"  결과 (out):    {out_dir}")
    _out(f"  기록 (log):    {mac.default_watch_log()}")
    _out("로그인할 때마다 자동으로 시작됩니다. 설정 파일을 고치면 곧바로 다시 읽습니다. deid를 "
         "업데이트한 뒤에는 이 명령을 한 번 더 실행해 새 버전으로 다시 시작하세요. "
         "제거: deid install-watch --uninstall")
    return EXIT_OK


# ---------------------------------------------------------------------- check


def extract_text(path: Path, cfg: "Config | None" = None) -> str:
    """Plain text of a (de-identified) file for `deid check`."""
    ext = path.suffix.lower()
    kind = formats.handler_name(path)
    if kind == "text" or ext in (".csv", ".tsv"):
        from .formats.text import read_text

        return read_text(path)[0]
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
        try:
            lines = []
            for ws in wb.worksheets:
                lines.append(f"# {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if v is None else str(v) for v in row]
                    if any(c.strip() for c in cells):
                        lines.append("\t".join(cells))
            return "\n".join(lines)
        finally:
            wb.close()
    if kind == "docx":
        # Every XML part (body, headers/footers, footnotes, comments, text boxes); python-docx
        # would skip several of them and refuses .docm/.dotx.
        return _zip_paragraphs(path, r"word/(?!theme/|fontTable|styles|settings|webSettings)"
                                     r"[^/]+\.xml|word/[^/]+/[^/]+\.xml", "p", ("t", "delText"))
    if ext == ".pdf":
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover - PyMuPDF < 1.24.3
            import fitz

        with fitz.open(str(path)) as doc:
            return "\n".join(page.get_text() for page in doc)
    if ext == ".hwpx":
        return _zip_paragraphs(path, r"Contents/(?:section|masterpage)\d+\.xml", "p", ("t",))
    if kind == "image":
        from PIL import Image

        from .ocr import ocr_image

        with Image.open(path) as im:
            return ocr_image(im.convert("RGB"), cfg).text
    raise CliError(formats.unsupported_message(path))


def _zip_paragraphs(path: Path, part_rx: str, para: str, text_tags: tuple[str, ...]) -> str:
    """Paragraph texts of the XML parts of an Office/Hancom package whose names match."""
    import xml.etree.ElementTree as ET
    import zipfile

    def local(tag: object) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    from .formats.text import zip_size_problem

    texts = []
    with zipfile.ZipFile(path) as z:
        problem = zip_size_problem(z.infolist())    # zip bomb: refuse before unpacking
        if problem:
            raise CliError(problem)
        for n in sorted(z.namelist()):
            if not re.fullmatch(part_rx, n):
                continue
            root = ET.fromstring(z.read(n))
            for p in root.iter():
                if local(p.tag) != para:
                    continue
                line = "".join(t.text or "" for t in p.iter()
                               if local(t.tag) in text_tags and t.text)
                if line.strip():
                    texts.append(line)
    return "\n".join(texts)


def cmd_check(args: argparse.Namespace) -> int:
    """Ask an LLM which identifiers remain in a de-identified file: Ollama on this computer
    (default) or, with ``[llm] backend = "openai"``, an OpenAI-compatible server such as Strata.
    Only de-identified text is sent: an original file is de-identified first, and a remote
    server never gets a file as it is (--as-is) unless it looks like a deid output."""
    from .backends import openai_compat as oc

    cfg = _load_cfg(args.config)
    path = Path(args.path).expanduser()
    if not path.is_file():
        raise CliError("파일이 없습니다 (file not found)")
    conf = None
    if oc.llm_backend(cfg) == "openai":
        section = oc.llm_section(cfg)
        if args.host:
            section["base_url"] = args.host
        if args.llm:
            section["model"] = args.llm
        try:
            conf = oc.settings(section, "llm")
        except ValueError as e:   # remote host without allow_remote, bad URL ...
            raise CliError(_first_line(e)) from e
        if conf.is_remote and args.as_is and not formats.is_deid_output(path):
            raise CliError(
                f"--as-is: 이 파일은 deid 결과(이름.deid.확장자)로 보이지 않습니다. 이 컴퓨터 밖의 LLM 서버"
                f"({conf.host})로는 비식별화하지 않았을 수도 있는 글을 그대로 보내지 않습니다. --as-is 없이 "
                "실행하면 먼저 비식별화한 결과를 검사합니다 (refusing to send a file that may be original "
                "to a remote server; run without --as-is)")
    try:
        text = extract_text(path, cfg)
    except CliError:
        raise
    except Exception as e:  # noqa: BLE001
        raise CliError(f"파일에서 글자를 읽을 수 없습니다 (cannot read text): {_first_line(e)}") from e
    if not (args.as_is or formats.is_deid_output(path)):
        from .anonymizer import Anonymizer
        from .formats.text import deid_text

        _err("원본 파일로 보여 먼저 비식별화한 결과를 검사합니다 (input looks original: checking the "
             "de-identified version; use --as-is to check the file as it is)")
        _transport_notice(cfg)
        try:
            text, _ = deid_text(text, _detector(cfg), Anonymizer(cfg))
        except RuntimeError as e:   # e.g. the [ner] LLM server failed (on_error = "fail")
            hint = LLM_HINT if _is_llm_error(e) else "-> 'deid doctor'로 확인하세요 (run 'deid doctor')"
            raise CliError(f"먼저 비식별화하지 못해 검사하지 않았습니다 - 아무것도 보내지 않았습니다 "
                           f"(could not de-identify first; nothing sent): {e}\n  {hint}"
                           ) from e
    if conf is not None:
        return _check_openai(text, conf, cfg)
    from .backends import ollama

    model = args.llm or (cfg.llm or {}).get("model") or DEFAULT_LLM
    host = args.host or (cfg.llm or {}).get("host") or ollama.DEFAULT_HOST
    allow_remote = bool((cfg.llm or {}).get("allow_remote", False))
    timeout = float((cfg.llm or {}).get("timeout", 120))
    try:
        if not ollama.available(host, allow_remote=allow_remote):
            raise CliError("Ollama에 연결할 수 없습니다. Ollama 앱을 실행하거나 'ollama serve'를 실행한 뒤, "
                           f"'ollama pull {model}'로 모델을 받으세요 (Ollama is not running at {host})")
        items = ollama.llm_review(text, model, host, allow_remote=allow_remote, timeout=timeout)
    except ValueError as e:   # non-local host refused
        raise CliError(_first_line(e)) from e
    except ollama.OllamaError as e:
        raise CliError(f"LLM 검사 실패 (LLM check failed): {_first_line(e)}") from e
    return _show_check(items, text, cfg, model)


def _check_openai(text: str, conf, cfg: "Config") -> int:
    """`deid check` with an OpenAI-compatible server ([llm] backend = "openai")."""
    from .backends import openai_compat as oc

    notice = oc.transport_warning(conf, raw_text=False)
    if notice:
        _err(f"! {notice}")
    name = f"{conf.model} ({conf.host})" if conf.is_remote else conf.model
    try:
        items = oc.review(text, conf)
    except oc.LLMError as e:
        raise CliError(f"LLM 검사 실패 (LLM check failed): {_first_line(e)}\n  {LLM_HINT}") from e
    except ValueError as e:
        raise CliError(_first_line(e)) from e
    return _show_check(items, text, cfg, name)


def _show_check(items: list[dict], text: str, cfg: "Config", name: str) -> int:
    if not items:
        _out(f"{name}: 남은 개인정보 의심 항목이 없습니다 (no suspected leftovers). "
             "LLM 검사도 완벽하지 않으니 직접 확인하세요.")
        return EXIT_OK
    from . import entities as E

    lines = text.splitlines()
    _out(f"{name}: 남은 개인정보로 의심되는 항목 {len(items)}개 (suspected leftovers; shown here "
         "only, never saved):")
    for it in items:
        where = [str(i + 1) for i, ln in enumerate(lines) if it["text"] in ln][:5]
        label = cfg.label_for(it["entity"]) if it.get("entity") else E.label("OTHER")
        # text and reason come from the LLM server: printable characters only (no terminal
        # control sequences such as cursor moves or OSC 52 clipboard writes), length-limited
        reason = f" - {_shown_value(it['reason'], 200)}" if it.get("reason") else ""
        line_info = f" (줄 {', '.join(where)})" if where else ""
        _out(f"  - [{_shown_value(label, 40)}] {_shown_value(it['text'], 120)}{line_info}{reason}")
    _out("확인 후 원본을 고치거나 설정의 names/patterns에 추가한 뒤 다시 비식별화하세요.")
    return EXIT_NOTHING


# ---------------------------------------------------------------------- forget / doctor / init


def cmd_forget(args: argparse.Namespace) -> int:
    from .session import data_dir, forget_session_items

    removed = forget_session_items(describe=True)
    if not removed:
        _out(f"삭제할 세션 정보가 없습니다 (nothing to delete): {data_dir()}")
        return EXIT_OK
    _out("세션 보관함과 키를 삭제했습니다 (session vault and key deleted):")
    for item in removed:
        _out(f"  - {item}")
    _out("이제 그 전에 만든 [이름1] 같은 가명은 복원할 수 없습니다. --vault 보관함 파일과 결과 파일"
         "(deid_out 등)은 지우지 않았습니다 (vault files and outputs are not touched)")
    return EXIT_OK


def _config_path() -> Path:
    env = os.environ.get("DEID_CONFIG")
    return Path(env).expanduser() if env else Path("~/.config/deid/config.toml").expanduser()


def install_hint(*packages: str) -> str:
    """A command that installs `packages` into the environment deid runs from.

    A bare `pip install` cannot reach a pipx venv (the README's install path), and Homebrew
    Python refuses it (PEP 668 externally-managed), so name pipx or this interpreter.
    """
    import shlex

    prefix = Path(sys.prefix)
    if prefix.parent.name == "venvs" and prefix.parent.parent.name.lower().endswith("pipx"):
        return f"pipx inject {prefix.name} " + " ".join(packages)
    return f"{shlex.quote(sys.executable)} -m pip install " + " ".join(map(shlex.quote, packages))


class _Doctor:
    MARKS = {"ok": "[OK]", "warn": "[!!]", "fail": "[XX]", "info": "[--]"}

    def __init__(self) -> None:
        self.failed = 0

    def item(self, status: str, title: str, detail: str = "", hint: str = "") -> None:
        if status == "fail":
            self.failed += 1
        _out(f"{self.MARKS[status]} {title}" + (f": {detail}" if detail else ""))
        if hint:
            _out(f"       -> {hint}")


def _config_problems(path: Path) -> list[str]:
    """deid.config.validate_report(path) when this deid has it (errors first, then softer
    problems such as names = [...] that ended up inside the [ner] table); else []."""
    try:
        from . import config as config_mod

        report = getattr(config_mod, "validate_report", None)
        return [str(p) for p in report(path)] if report else []
    except Exception as e:  # noqa: BLE001 - the check itself must not break doctor
        return [_first_line(e)]


def _doctor_config(doc: "_Doctor", cfg_path: Path) -> "Config | None":
    """Config section of `deid doctor`: [XX] when the file does not load (every command stops
    with that message), [!!] for every other problem validate_report finds."""
    from .config import load_config
    from .watch import config_error_text

    error = None
    cfg = None
    try:
        cfg = load_config(cfg_path)
    except Exception as e:  # noqa: BLE001
        error = config_error_text(e, cfg_path)
    problems = _config_problems(cfg_path)
    if error is not None:
        doc.item("fail", "설정 파일 (config)", f"{cfg_path}: {error}",
                 "이 오류를 고칠 때까지 모든 deid 명령이 멈춥니다. 파일 내용을 고치거나 "
                 "deid init-config --force로 예시를 다시 만드세요")
    else:
        doc.item("warn" if problems else "ok", "설정 파일 (config)",
                 f"{cfg_path} 읽음 (loaded)" + (f" - 확인할 점 {len(problems)}개" if problems else ""))
    shown = " ".join((error or "").split())
    for p in problems:
        flat = " ".join(p.split())
        if shown and (flat == shown or flat in shown or shown in flat):
            continue     # already shown above
        doc.item("warn", "설정 파일 확인 필요 (config warning)", p)
    if cfg is not None:
        try:
            _column_types(None, cfg)
        except CliError as e:
            doc.item("fail", "설정 파일 [columns]", e.message,
                     "열 유형은 PERSON, PHONE, RRN 같은 항목 이름이나 skip, text 입니다")
        if cfg.names:
            doc.item("info", "설정 파일 names", f"항상 가릴 이름 {len(cfg.names)}개 (names: {len(cfg.names)})")
    return cfg


class _Skip(Exception):
    """Internal: skip the rest of a doctor section."""


def _shown_value(value: object, limit: int = 60) -> str:
    """A value the LLM server sent (model id, /health field), safe for the terminal."""
    if isinstance(value, bool) or value is None:
        return str(value).lower()
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text[:limit] + ("…" if len(text) > limit else "")


def _key_source_text(label: str) -> str:
    """Where the API key came from, from resolve_api_key's label - never the key."""
    if label.startswith("env:"):
        return f"환경변수 {label[4:]}에서 찾음 (found in env {label[4:]})"
    if label.startswith("keychain:"):
        return f"키체인 항목 '{label[9:]}'(계정 deid)에서 찾음 (found in the Keychain)"
    return "설정 안 함 - 키 없이 접속합니다 (no key configured)"


def _doctor_llm_servers(doc: "_Doctor", cfg: "Config") -> bool:
    """`deid doctor` lines for [ner] / [llm] backend = "openai" (e.g. Strata on the clinic PC):
    settings, where the API key comes from (never the key), /health and the first model id,
    and [!!] for raw text leaving this computer, unencrypted http, a missing key and an
    unreachable server or a refused key. Returns True when [llm] uses such a server."""
    from .backends import openai_compat as oc

    tables = []
    if oc.ner_enabled(cfg):
        tables.append(("ner", oc.ner_section(cfg)))
    llm_openai = oc.llm_backend(cfg) == "openai"
    if llm_openai:
        tables.append(("llm", oc.llm_section(cfg)))
    checked: dict[tuple, str] = {}
    for table, section in tables:
        use = "탐지 (detection)" if table == "ner" else "deid check"
        title = f"LLM 서버 [{table}] {use}"
        try:
            conf = oc.settings(section, table)
        except ValueError as e:
            doc.item("fail", title, _first_line(e), "설정 파일을 고친 뒤 다시 deid doctor")
            continue
        detail = (f"{conf.base_url}, model {conf.model}, effort {conf.effort}, allow_remote "
                  f"{str(conf.allow_remote).lower()}")
        if table == "ner":
            detail += f", on_error {conf.on_error}, batch_chars {conf.batch_chars}"
        doc.item("ok", title, detail)
        if conf.is_remote and table == "ner":
            doc.item("warn", "원문이 이 Mac 밖으로 나감 (raw text leaves this Mac)",
                     f"비식별화 전 진료 원문(이름·주민번호 등 포함)이 {conf.host}(으)로 전송됩니다",
                     "의도한 것이 아니면 설정 파일의 [ner] backend 줄을 지우세요")
        elif conf.is_remote:
            doc.item("info", "deid check 전송 (sent for checking)",
                     f"비식별화한 글만 {conf.host}(으)로 보냅니다 (de-identified text only)")
        if conf.is_remote and conf.base_url.lower().startswith("http://"):
            doc.item("warn", f"암호화되지 않은 연결 [{table}] (unencrypted http)",
                     f"{conf.host}까지 글과 API 키가 암호화되지 않은 채 갑니다. 같은 네트워크의 다른 기기가 "
                     "볼 수 있습니다",
                     "병원 내부망(유선 또는 보안 Wi-Fi)에서만 쓰세요. 서버가 https를 지원하면 base_url을 "
                     "https://로 바꾸세요")
        if table == "ner" and conf.on_error == "rules_only":
            doc.item("info", "LLM 서버 오류 때 (on_error)",
                     "규칙 기반 탐지만으로 계속하고 결과에 경고를 남깁니다",
                     "LLM이 찾던 것을 놓친 채 결과가 나올 수 있습니다. 권장: on_error = \"fail\"")
        try:
            key, label = oc.resolve_api_key(conf)
        except oc.LLMError as e:
            svc = conf.api_key_keychain
            doc.item("warn", f"API 키 [{table}] (API key)", _first_line(e),
                     f"deid key set {svc}" if svc else
                     f"터미널에서 'read -rs {conf.api_key_env} && export {conf.api_key_env}' 후 키를 붙여넣거나 "
                     "(키를 명령줄에 쓰지 마세요), 키체인을 쓰려면 deid key set deid-strata")
            continue
        del key   # never shown, never kept
        doc.item("ok" if label != "none" else "info", f"API 키 [{table}] (API key)",
                 _key_source_text(label))
        sig = (conf.root, conf.api_key_env, conf.api_key_keychain)
        if sig in checked:
            doc.item("info", f"LLM 서버 응답 [{table}]", f"[{checked[sig]}]와 같은 서버 (same server)")
            continue
        checked[sig] = table
        _doctor_llm_reply(doc, conf, table)
    return llm_openai


def _doctor_llm_reply(doc: "_Doctor", conf, table: str) -> None:
    """GET /health and /v1/models of one server (short timeouts)."""
    from .backends import openai_compat as oc

    title = f"LLM 서버 응답 [{table}] (server)"
    try:
        info = oc.health(conf)
    except oc.LLMError as e:
        if e.kind == "auth":
            hint = (f"deid key set {conf.api_key_keychain}로 키를 다시 넣으세요" if conf.api_key_keychain
                    else "환경변수의 키 값을 확인하세요" if conf.api_key_env
                    else "설정에 api_key_keychain = \"deid-strata\"를 적고 deid key set deid-strata")
        elif e.kind in ("connection", "timeout"):
            host = _url_host(conf.host)
            hint = "서버 PC가 켜져 있고 Strata가 실행 중인지, 같은 네트워크에 있는지 확인하세요"
            if host and oc.is_ip_literal(host) and not oc.is_loopback(host):
                hint += (f". PC의 IP 주소가 {host}에서 바뀌었을 수 있습니다 (Windows: ipconfig). 바뀌지 "
                         "않도록 공유기에서 고정(DHCP 예약)해 두세요")
        else:
            hint = "base_url이 …/v1 로 끝나는 OpenAI 호환 주소인지 확인하세요"
        doc.item("warn", title, _first_line(e), hint)
        return
    loaded = info.get("loaded")
    detail = (f"{conf.host} 응답함, /health loaded={_shown_value(loaded)}"
              + (f", images={_shown_value(info.get('images'))}" if "images" in info else ""))
    hint = ""
    status = "ok"
    if loaded is False:
        status, hint = "warn", "서버에 모델이 아직 올라오지 않았습니다. 잠시 뒤 다시 확인하세요"
    try:
        models = oc.list_models(conf)
    except oc.LLMError as e:
        doc.item(status, title, detail, hint)
        doc.item("warn", f"LLM 서버 모델 목록 [{table}] (/v1/models)", _first_line(e),
                 "API 키를 확인하세요 (deid key status 이름)" if e.kind == "auth" else "")
        return
    if models:
        detail += f", 모델 {_shown_value(models[0])}" + (f" 외 {len(models) - 1}개" if len(models) > 1
                                                       else "")
    else:
        detail += ", 모델 목록 비어 있음"
    doc.item(status, title, detail, hint)


def _url_host(netloc: str) -> str:
    """Host of "192.168.0.20:8080" as written (case kept); "" for an IPv6 literal."""
    if netloc.startswith("["):
        return ""
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib.util

    doc = _Doctor()
    _out(f"deid {__version__} 환경 점검 (environment check)")
    doc.item("ok", "deid 버전 (version)", f"{__version__} - {build_info()}",
             "업데이트 뒤 이 버전/커밋이 바뀌었는지 확인하세요 (자동 감시: deid install-watch 다시 "
             "실행, deid serve: 다시 시작)")
    v = sys.version_info
    doc.item("ok" if v >= (3, 10) else "fail", "Python", f"{v.major}.{v.minor}.{v.micro} "
             f"({sys.executable})", "" if v >= (3, 10) else "Python 3.10 이상이 필요합니다: "
             "brew install python@3.12")
    if mac.is_macos():
        doc.item("ok", "운영체제 (OS)", f"macOS {platform.mac_ver()[0]} ({platform.machine()})")
    else:
        doc.item("info", "운영체제 (OS)", f"{platform.system()} {platform.release()} "
                 f"({platform.machine()}) - clip/shot/restore는 macOS 전용")
    found = shutil.which("deid")
    doc.item("ok" if found else "warn", "deid 명령 위치 (command path)", found or "PATH에 없음",
             "" if found else "빠른 동작/단축어에는 전체 경로가 필요합니다: pipx install . 또는 "
             "PATH에 ~/.local/bin 추가")

    # OCR
    try:
        from . import ocr
        status = ocr.engine_status()
        find = getattr(ocr, "_find_tesseract", None)
        tess_path = find() if find else shutil.which("tesseract")
        langs = ocr.tesseract_languages()
    except Exception as e:  # noqa: BLE001
        status, tess_path, langs = {}, None, []
        doc.item("fail", "OCR 모듈", _first_line(e))
    tess = status.get("tesseract", "missing")
    if tess.startswith("ok"):
        doc.item("ok", "Tesseract OCR", f"{tess_path} - {tess[3:].strip()}")
    elif tess.startswith("incomplete"):
        doc.item("warn", "Tesseract OCR", f"{tess_path} - 한국어(kor) 데이터 없음",
                 "brew install tesseract-lang")
    else:
        doc.item("warn" if mac.is_macos() else "fail", "Tesseract OCR", "설치되지 않음 (missing)",
                 "brew install tesseract tesseract-lang" if mac.is_macos() else
                 "sudo apt install tesseract-ocr tesseract-ocr-kor")
    if tess_path and langs:
        doc.item("ok" if "kor" in langs else "warn", "Tesseract 한국어 (kor traineddata)",
                 "있음 (present)" if "kor" in langs else "없음 (missing)",
                 "" if "kor" in langs else "brew install tesseract-lang")
    has_ocrmac = importlib.util.find_spec("ocrmac") is not None
    if mac.is_macos():
        vision = status.get("vision", "")
        if has_ocrmac and vision.startswith("incomplete"):
            # ocrmac is there, but Vision reads Korean only from macOS 13 on
            doc.item("warn", "Apple Vision OCR (ocrmac)", vision.split(":", 1)[-1].strip(),
                     "macOS를 13(Ventura) 이상으로 올리거나 Tesseract를 쓰세요: "
                     "brew install tesseract tesseract-lang")
        else:
            doc.item("ok" if has_ocrmac else "warn", "Apple Vision OCR (ocrmac)",
                     "사용 가능" if has_ocrmac else "설치되지 않음",
                     "" if has_ocrmac else f"{install_hint('ocrmac')}  (한국어 인식이 더 정확합니다)")
        live = status.get("livetext", "")
        doc.item("ok" if live.startswith("ok") else "info", "Live Text (macOS 14+)",
                 "사용 가능" if live.startswith("ok") else live or "확인 불가")
    else:
        doc.item("info", "Apple Vision / Live Text", "macOS 전용 (macOS only)")
    engines = [k for k, s in status.items() if s.startswith("ok")]
    doc.item("ok" if engines else "fail", "이미지/스캔 PDF용 OCR 엔진",
             ", ".join(engines) if engines else "없음 - 이미지와 스캔 PDF를 처리할 수 없습니다",
             "" if engines else "brew install tesseract tesseract-lang 또는 " + install_hint("ocrmac"))
    heif = importlib.util.find_spec("pillow_heif") is not None
    doc.item("ok" if heif else "info", "HEIC 사진 (pillow-heif)", "사용 가능" if heif else "없음",
             "" if heif else "아이폰 HEIC 사진을 처리하려면: " + install_hint("pillow-heif"))

    # Config
    cfg_path = _config_path()
    cfg = None
    if cfg_path.exists():
        cfg = _doctor_config(doc, cfg_path)
    else:
        doc.item("fail" if os.environ.get("DEID_CONFIG") else "info", "설정 파일 (config)",
                 f"{cfg_path} 없음 - " + ("DEID_CONFIG가 없는 파일을 가리킵니다. 모든 명령이 멈춥니다"
                                         if os.environ.get("DEID_CONFIG") else "기본 설정 사용"),
                 "deid init-config 로 예시 설정 파일을 만들 수 있습니다")
    if cfg is None:
        from .config import default_config
        cfg = default_config()
    backend = (cfg.ner or {}).get("backend")
    if backend:
        mod = {"hf": "transformers", "gliner": "gliner"}.get(str(backend))
        if mod:
            ok = importlib.util.find_spec(mod) is not None
            doc.item("ok" if ok else "fail", f"NER 백엔드 ({backend})",
                     "설치됨" if ok else f"{mod} 없음",
                     "" if ok else install_hint(*(("transformers", "torch") if backend == "hf"
                                                  else ("gliner",))))

    # OpenAI-compatible LLM server (Strata on the clinic PC): [ner] and/or [llm]
    try:
        llm_openai = _doctor_llm_servers(doc, cfg)
    except Exception as e:  # noqa: BLE001 - the check itself must not break doctor
        llm_openai = False
        doc.item("warn", "LLM 서버 (LLM server)", _first_line(e))

    # Ollama
    try:
        from .backends import ollama
        if llm_openai:
            raise _Skip
        host = (cfg.llm or {}).get("host") or ollama.DEFAULT_HOST
        model = (cfg.llm or {}).get("model") or DEFAULT_LLM
        if ollama.available(host, timeout=2.0, allow_remote=bool((cfg.llm or {}).get("allow_remote"))):
            models = ollama.list_models(host, allow_remote=bool((cfg.llm or {}).get("allow_remote")))
            has = any(m == model or m.split(":")[0] == model for m in models)
            doc.item("ok" if has else "warn", "Ollama (선택: deid check)",
                     f"{host} 연결됨, 모델: {', '.join(models) or '없음'}",
                     "" if has else f"ollama pull {model}")
        else:
            doc.item("info", "Ollama (선택: deid check)", f"{host} 연결 안 됨 (not reachable)",
                     f"https://ollama.com 에서 설치 후 실행, 그리고 ollama pull {model}")
    except _Skip:   # deid check uses the [llm] OpenAI-compatible server instead
        pass
    except Exception as e:  # noqa: BLE001
        doc.item("info", "Ollama (선택)", _first_line(e))

    # Folders / session
    from .session import (data_dir, key_location, keychain_problem, loose_permissions,
                          purge_expired_session, session_age_hours, session_hours,
                          session_idle_hours, user_dir, vault_file)

    ud = user_dir()
    if ud.exists():
        ok = os.access(ud, os.W_OK)
        doc.item("ok" if ok else "fail", "작업 폴더 쓰기 (write access)", str(ud),
                 "" if ok else f"권한을 확인하세요: chmod u+w {ud}")
        loose = [d for d in (ud, ud / "in", ud / "out") if d.exists() and loose_permissions(d)]
        doc.item("warn" if loose else "ok", "작업 폴더 보호 (folder permissions)",
                 "다른 사용자 계정도 읽을 수 있습니다: " + ", ".join(map(str, loose)) if loose else
                 "이 계정만 읽을 수 있음 (owner only)",
                 "chmod 700 " + " ".join(f'"{d}"' for d in loose) if loose else "")
    else:
        parent_ok = os.access(ud.parent, os.W_OK)
        doc.item("info" if parent_ok else "fail", "작업 폴더 (work folder)",
                 f"{ud} 없음 - deid shot/watch가 처음 쓸 때 만듭니다" if parent_ok else
                 f"{ud.parent}에 쓸 수 없습니다")
    purged = purge_expired_session()
    age = session_age_hours() if vault_file().exists() else None
    idle = session_idle_hours() if vault_file().exists() else None
    hours = session_hours()
    if age is not None and idle is not None:
        state = (f"{age:.1f}시간 전 시작, 마지막 사용 {idle:.1f}시간 전 - {hours:g}시간 동안 쓰지 "
                 f"않으면 지움 (약 {max(0.0, hours - idle):.1f}시간 남음)")
    else:
        state = "만료되어 키와 함께 지움" if purged else "없음"
    doc.item("info", "세션 보관함 (clipboard session)",
             f"{data_dir()} - {state}, 키: {key_location()}")
    problem = keychain_problem() if vault_file().exists() else None
    if problem:
        doc.item("warn", "세션 키 (Keychain)", problem.split(" (")[0],
                 "키체인 접근 앱에서 로그인 키체인 잠금을 풀거나, deid forget 으로 세션을 새로 시작하세요")
    if mac.is_macos():
        missing = [t for t in ("pbcopy", "pbpaste", "osascript", "screencapture", "security")
                   if shutil.which(t) is None]
        doc.item("ok" if not missing else "fail", "macOS 도구 (clipboard/screenshot tools)",
                 "모두 있음" if not missing else "없음: " + ", ".join(missing))
        doc.item("info", "화면 기록 권한 (Screen Recording)",
                 "deid shot이 창 내용을 못 찍으면: 시스템 설정 > 개인정보 보호 및 보안 > 화면 기록에서 "
                 "터미널/단축어를 허용하세요")
    _out("")
    _out("문제가 없으면 [OK], 선택 기능은 [--], 확인이 필요하면 [!!], 꼭 고쳐야 하면 [XX] 입니다.")
    return EXIT_FAIL if doc.failed else EXIT_OK


EXAMPLE_CONFIG = """\
# deid 설정 파일 (config) - TOML 형식. '#'으로 시작하는 줄은 설명(주석)입니다.
# 위치: ~/.config/deid/config.toml  (다른 위치: deid file --config 경로, 또는 DEID_CONFIG 환경변수)
# 고친 뒤 'deid doctor'로 제대로 읽히는지 확인하세요.

# 항상 이름으로 가릴 사람 이름 (직원, 자주 나오는 환자 등). 문맥이 없어도 찾습니다.
names = [
  # "홍길동",
  # "김민준",
]

# 절대 개인정보로 보지 않을 낱말 (예: 병원 이름, 약 이름이 이름으로 잘못 잡힐 때)
allowlist = [
  # "서울내과",
]

# 이 점수보다 낮은 탐지는 버립니다 (0.0-1.0). 낮출수록 더 많이 가립니다.
# min_score = 0.5

# 특정 항목 끄기 (예: 날짜와 URL은 그대로 두기)
# disabled = ["DATE", "URL"]

# 항목별 처리 방법:
#   pseudo     -> [이름1] 처럼 일관된 가명 (deid restore로 복원 가능)
#   mask       -> 김*준, 010-****-5678 처럼 일부만 가림
#   redact     -> [주민번호] 처럼 통째로 가림
#   generalize -> 주소는 '서울특별시 강남구', 생년월일은 '1985년생'처럼 넓게
#   keep       -> 그대로 둠 (개수만 셉니다)
[actions]
# PERSON = "pseudo"
# PHONE = "mask"
# RRN = "redact"
# ADDRESS = "generalize"
# BIRTHDATE = "generalize"
# DATE = "keep"
# EMAIL = "pseudo"
# MRN = "pseudo"

# 결과에 쓰일 표시 이름 바꾸기
[labels]
# PERSON = "환자"

# 엑셀/CSV 열 유형 지정 (열 제목 = 유형). 제목만으로 알아보지 못하는 열을 알려줍니다.
#   유형: PERSON(이름), PHONE, RRN(주민번호), ADDRESS, EMAIL, MRN(등록번호), BIRTHDATE ...
#         skip = 그 열은 손대지 않음, text = 자동 탐지만
# 한 번만 바꾸려면: deid file 파일.xlsx --column "담당=PERSON"
[columns]
# "담당" = "PERSON"
# "약품명" = "skip"

# 이미지/스캔 PDF의 글자 인식 (OCR)
[ocr]
# engine = "auto"        # auto | livetext | vision | tesseract
# lang = "kor+eng"       # tesseract 언어
# min_conf = 30          # 이 신뢰도보다 낮은 글자가 많으면 경고 (글자를 버리지는 않음)
# style = "box"          # box(검은 상자) | blur(흐림) | pixelate(모자이크)
# verify = 1             # 가린 뒤 한 번 더 읽어 남은 것까지 가림 (0이면 끔)
# barcodes = "cover"     # 바코드/QR: cover(가림) | warn(경고만) | off
# orientation = "auto"   # 옆으로/거꾸로 찍힌 사진도 돌려서 읽기: auto | true | false
# pdf_images = "auto"    # PDF 안의 그림: auto(쪽의 20% 이상인 그림) | true(전부) | false(안 함)
# pdf_dpi = 300          # 스캔 PDF를 읽을 해상도
# docx_images = "auto"   # 워드 안의 그림: auto(어느 정도 큰 그림) | true(전부, OCR 없으면 실패) | false
# hwpx_images = "auto"   # 한글(hwpx) 안의 그림: docx_images와 같음
# max_image_pixels = 100000000   # 이보다 큰 그림은 처리하지 않음 (메모리 보호)

# deid check 에서 쓰는 LLM. 기본은 이 컴퓨터의 Ollama입니다.
[llm]
# model = "gemma3:4b"
# host = "http://127.0.0.1:11434"
# 진료실 LLM 서버(OpenAI 호환, 예: Strata)로 점검하려면 (비식별화한 글만 보냅니다):
# backend = "openai"
# base_url = "http://192.168.0.20:8080/v1"   # 진료실 PC의 IP 주소 (http는 이름 대신 IP만; ipconfig로 확인)
# api_key_keychain = "deid-strata"   # 키는 'deid key set deid-strata'로 키체인에 (설정 파일에 키 금지)
# model = "strata"
# effort = "none"
# allow_remote = true                # 이 컴퓨터가 아닌 서버에 꼭 필요

# 고급: 탐지를 돕는 신경망/LLM. 기본은 꺼짐(규칙만).
[ner]
# backend = "gliner"     # hf | gliner | ollama | openai (설치 필요: pipx inject deid-kit gliner)
# model = "urchade/gliner_multi_pii-v1"
# device = "mps"         # Apple Silicon GPU
# 진료실 LLM 서버로 탐지하려면 (주의: 비식별화 전 원문이 그 PC로 갑니다. http는 암호화되지 않음):
# backend = "openai"
# base_url = "http://192.168.0.20:8080/v1"   # 진료실 PC의 IP 주소
# api_key_keychain = "deid-strata"
# model = "strata"
# effort = "none"
# allow_remote = true    # [ner]에 직접 적어야 합니다 ([llm]의 값은 쓰지 않음)
# on_error = "fail"      # 서버 오류 때 멈춤(권장) | "rules_only" 규칙만으로 계속 + 경고

# 병원 고유 번호 형식 추가 (정규식). 필요하면 아래 블록의 '#'을 지우세요.
# [[patterns]]
# entity = "STAFF_ID"
# regex = "EMP-[0-9]{6}"
# score = 0.9
# context = ["사번", "직원번호"]
"""


def cmd_init_config(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else _config_path()
    if path.exists() and not args.force:
        _err(f"이미 설정 파일이 있습니다 (config already exists): {path}")
        _err("덮어쓰려면 --force 를 붙이세요 (use --force to overwrite)")
        return EXIT_NOTHING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    _out(f"설정 파일을 만들었습니다 (config written): {path}")
    _out("텍스트 편집기로 열어 names / allowlist 등을 고치세요. 확인: deid doctor")
    return EXIT_OK


# ---------------------------------------------------------------------- key (LLM server API key)


# What mac.keychain_set passes to `security -i` (one plain word); other keys are refused there.
_KEYCHAIN_SECRET = getattr(mac, "_KEYCHAIN_WORD", None) or re.compile(r"[A-Za-z0-9._@+-]{1,256}")


def _env_key_help(name: str = "STRATA_API_KEY") -> str:
    return (f"설정 파일의 [ner]/[llm]에 api_key_env = \"{name}\"을 적고, deid를 실행할 터미널에서 "
            f"'read -rs {name} && export {name}'을 실행한 뒤 키를 붙여넣고 Enter를 누르세요(화면에 보이지 "
            "않습니다). 키를 명령줄이나 설정 파일, ~/.zshrc 같은 파일에 그대로 적지 마세요 (use an environment "
            "variable instead: api_key_env)")


def cmd_key(args: argparse.Namespace) -> int:
    """`deid key set|delete|status NAME`: the API key of an LLM server in the macOS Keychain
    (account "deid", service NAME; config: api_key_keychain = "NAME").

    The key is read with getpass (never from argv, never echoed) and stored through
    mac.keychain_set, which hands it to `security -i` on stdin. Nothing here prints the key;
    `status` only says whether the item exists."""
    from .backends import openai_compat as oc
    from .session import KEYCHAIN_SERVICE

    name = (args.name or "").strip()
    if name.casefold() == KEYCHAIN_SERVICE.casefold():
        raise CliError(f"'{KEYCHAIN_SERVICE}'은 deid 세션 보관함의 열쇠라 쓸 수 없습니다. 다른 이름(예: "
                       "deid-strata)을 쓰세요 (reserved for the session vault key)")
    try:
        _, valid = oc.key_sources({"api_key_keychain": name}, "key")
    except ValueError:
        valid = None
    if not valid:   # the message never repeats NAME: it may be a key typed in the wrong place
        raise CliError(
            "키체인 항목 이름은 영문 소문자·숫자와 . _ - 로 된 64자 이하의 이름이어야 합니다 (예: deid-strata). "
            "키 값 자체를 이름 자리에 넣지 마세요: 명령줄에 쓴 글은 터미널 기록에 남습니다. 키를 실수로 적었다면 "
            "서버에서 새 키로 바꾸는 것이 안전합니다 (invalid Keychain item name; never type the key itself "
            "on the command line)")
    if not mac.is_macos():
        raise CliError("키체인은 macOS에만 있습니다. 이 컴퓨터에서는 환경변수를 쓰세요: "
                       + _env_key_help() + " (Keychain: macOS only)")
    account = oc.KEYCHAIN_ACCOUNT
    if args.action == "status":
        found = mac.keychain_get(account, valid) is not None   # MacError: locked, cancelled
        if found:
            _out(f"있음 (exists): 키체인 계정 {account}, 항목 {valid} - 키 값은 보여 주지 않습니다")
            return EXIT_OK
        _out(f"없음 (not found): 키체인 계정 {account}, 항목 {valid}. 넣기: deid key set {valid}")
        return EXIT_NOTHING
    if args.action == "delete":
        if mac.keychain_delete(account, valid):
            _out(f"삭제했습니다 (deleted): 키체인 계정 {account}, 항목 {valid}")
            return EXIT_OK
        _out(f"지울 항목이 없습니다 (not found): 키체인 계정 {account}, 항목 {valid}")
        return EXIT_NOTHING
    # set
    if not _can_prompt():
        raise CliError("API 키를 입력할 터미널 창이 없습니다. 터미널 앱에서 직접 실행하세요 (no terminal to "
                       "ask for the key)")
    _err(f"키체인(계정 {account}, 항목 {valid})에 넣을 API 키를 붙여넣고 Enter를 누르세요. 입력한 글자는 "
         "화면에 보이지 않습니다. 이미 있으면 새 키로 바뀝니다.")
    try:
        secret = getpass.getpass("API 키 (API key): ")
    except EOFError:
        raise CliError("입력이 없어 저장하지 않았습니다 (no input: nothing stored)") from None
    secret = secret.strip()
    if not secret:
        raise CliError("키가 비어 있어 저장하지 않았습니다 (empty key: nothing stored)")
    if not _KEYCHAIN_SECRET.fullmatch(secret) or secret.startswith("-"):
        secret = ""
        raise CliError(
            "이 키에는 deid가 키체인에 넣을 수 없는 글자(공백, / = : 등)가 있어 저장하지 않았습니다. 대신 터미널에서 "
            f"'security add-generic-password -a {account} -s {valid} -U -w'를 실행하면 키를 물어보니 거기에 "
            "붙여넣으세요(어떤 글자든 됩니다). 또는 " + _env_key_help() + " (the key has characters deid "
            "cannot pass to the Keychain; use 'security add-generic-password ... -w' or an environment "
            "variable)")
    try:
        ok = mac.keychain_set(account, valid, secret)
    finally:
        secret = ""
    if not ok:
        raise CliError("키체인에 저장하지 못했습니다. 로그인 키체인이 잠겨 있으면 키체인 접근 앱에서 잠금을 풀고 "
                       "다시 시도하세요 (could not store the key in the Keychain)")
    _out(f"저장했습니다 (stored): 키체인 계정 {account}, 항목 {valid}")
    _out(f"설정 파일의 [ner] 또는 [llm]에 api_key_keychain = \"{valid}\"를 적고 'deid doctor'로 "
         "확인하세요. 처음 쓸 때 macOS가 키체인 접근을 물으면 '허용'을 누르세요.")
    return EXIT_OK


# ---------------------------------------------------------------------- parser


class _KoFormatter(argparse.RawDescriptionHelpFormatter):
    """'사용법:' instead of 'usage:' (the rest of the help text is already Korean)."""

    def add_usage(self, usage, actions, groups, prefix=None):  # noqa: ANN001
        return super().add_usage(usage, actions, groups, "사용법 (usage): " if prefix is None else prefix)


# argparse error -> (Korean, short English); the long English original is not repeated.
_ARGPARSE_KO = (
    (re.compile(r"the following arguments are required: (.+)"),
     "꼭 필요한 값이 빠졌습니다: {0}", "missing: {0}"),
    (re.compile(r"unrecognized arguments: (.+)"), "알 수 없는 옵션입니다: {0}", "unknown option"),
    (re.compile(r"argument (\S+): invalid choice: (.+?) \(choose from (.+)\)"),
     "그런 명령은 없습니다: {1}. 다음 중에서 고르세요: {2}", "invalid choice"),
    (re.compile(r"argument (\S+): expected one argument"),
     "{0} 옵션 뒤에 값을 하나 써야 합니다", "{0} needs a value"),
    (re.compile(r"argument (\S+): invalid (\w+) value: (.+)"),
     "{0} 옵션의 값이 올바르지 않습니다: {2}", "invalid value"),
    (re.compile(r"argument (\S+): not allowed with argument (\S+)"),
     "{1}, {0} 옵션은 함께 쓸 수 없습니다", "use only one"),
)


class _KoParser(argparse.ArgumentParser):
    """Korean section titles and usage errors (short English in parentheses)."""

    def __init__(self, *args, **kw) -> None:
        kw.setdefault("formatter_class", _KoFormatter)
        super().__init__(*args, **kw)
        self._positionals.title = "입력 (arguments)"
        self._optionals.title = "옵션 (options)"

    def error(self, message: str):  # noqa: ANN201 - NoReturn
        text = message
        for rx, ko, en in _ARGPARSE_KO:
            m = rx.fullmatch(message.strip())
            if m:
                text = f"{ko.format(*m.groups())} ({en.format(*m.groups())})"
                break
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAIL, f"{self.prog}: 오류: {text}\n도움말: {self.prog} --help\n")


def _parser(**kw) -> argparse.ArgumentParser:
    p = _KoParser(add_help=False, **kw)
    p.add_argument("-h", "--help", action="help", help="도움말 보기 (show this help)")
    return p


def build_parser() -> argparse.ArgumentParser:
    p = _parser(prog="deid",
                description="한국어 개인정보 비식별화 도구 - 기본으로 모든 처리는 이 컴퓨터 안에서만 합니다. "
                            "설정에서 진료실 LLM 서버를 탐지에 연결하면([ner] backend = \"openai\") 글이 그 "
                            "서버로도 갑니다 (Korean-first local de-identification; by default nothing "
                            "leaves this computer).",
                epilog="사용 예 (examples):\n" + EXAMPLES +
                "\n\n종료 코드 (exit codes): 0 성공, 1 할 일 없음/의심 항목 있음/취소, 2 오류")
    p.add_argument("--version", action=_VersionAction,
                   help="버전과 설치 원본(git 커밋) 보기 (show version and source)")
    p._positionals.title = "명령 (commands)"
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=_KoParser,
                           help="아래 명령 중 하나 (자세히: deid COMMAND --help)")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="FILE",
                        help="설정 파일 (config file; 기본 ~/.config/deid/config.toml)")

    def add(name: str, help_text: str, description: str | None = None, parents=(common,),
            epilog: str | None = None):
        sp = sub.add_parser(name, help=help_text, description=description or help_text,
                            parents=list(parents), add_help=False, epilog=epilog)
        sp.add_argument("-h", "--help", action="help", help="도움말 보기 (show this help)")
        return sp

    sp = add("file", "파일/폴더 비식별화 (de-identify files or folders)",
             "파일을 비식별화해 '<폴더>/deid_out/이름.deid.확장자'로 저장합니다. 원본은 바꾸지 않습니다. "
             "(de-identify files; originals are never modified)\n"
             f"지원 형식 (supported): {formats.supported_list()}",
             epilog="사용 예 (examples):\n" + FILE_EXAMPLES)
    sp.add_argument("paths", nargs="+", metavar="PATH", help="파일 또는 폴더 (files or folders)")
    sp.add_argument("-o", "--outdir", metavar="OUTDIR",
                    help="결과 폴더 (output folder; 기본: 원본 옆 deid_out)")
    sp.add_argument("--vault", metavar="FILE",
                    help="가명 보관함 파일: 여러 번 실행해도 같은 사람은 같은 가명 "
                         "(encrypted vault for consistent tokens; 암호는 DEID_VAULT_PASS 또는 입력)")
    sp.add_argument("--session", action="store_true",
                    help="클립보드 세션 보관함 사용 - 나중에 deid restore로 복원 가능 "
                         "(use the clipboard session vault)")
    sp.add_argument("--report", choices=("text", "json"), default="text",
                    help="결과 표시 형식 (summary format)")
    sp.add_argument("--dry-run", action="store_true",
                    help="저장하지 않고 개수만 보기 (count only, write nothing)")
    sp.add_argument("--notify", action="store_true",
                    help="끝나면 알림 표시 - 개수만, 파일 이름 없이 (notification with counts; macOS)")
    sp.add_argument("--column", action="append", metavar="COL=TYPE",
                    help="엑셀/CSV 열 유형 지정 '열제목=유형', 예: '담당=PERSON', '메모=text', "
                         "'코드=skip'. 여러 번 쓸 수 있고 설정 파일의 [columns]보다 우선 "
                         "(spreadsheet column type override; repeatable)")
    sp.set_defaults(func=cmd_file)

    sp = add("text", "표준입력 -> 표준출력 (stdin -> stdout)",
             "표준입력의 텍스트를 비식별화해 표준출력으로 냅니다. 세션 보관함을 써서 나중에 복원할 수 "
             "있습니다. (de-identify stdin to stdout using the session vault)")
    sp.add_argument("--restore", action="store_true",
                    help="반대로 [이름1] 등을 원래 값으로 복원 (re-identify tokens)")
    sp.add_argument("--vault", metavar="FILE",
                    help="세션 대신 보관함 파일 사용 - 암호를 물어봄, 또는 DEID_VAULT_PASS. 복원: "
                         "deid text --restore --vault 파일 < 답변.txt (use a vault file)")
    sp.add_argument("-q", "--quiet", action="store_true", help="요약을 출력하지 않음 (no summary)")
    sp.set_defaults(func=cmd_text)

    sp = add("clip", "클립보드 비식별화 (de-identify the clipboard; macOS)",
             "클립보드의 텍스트(또는 이미지)를 비식별화해 다시 클립보드에 넣습니다. 실패하면 실수로 "
             "붙여넣지 않도록 클립보드를 비웁니다. (macOS)")
    sp.add_argument("--notify", action="store_true", help="알림 표시 (show a notification)")
    sp.set_defaults(func=cmd_clip)

    sp = add("restore", "클립보드의 가명을 원래 값으로 복원 (re-identify the clipboard; macOS)",
             "LLM 답변에 있는 [이름1] 같은 가명을 이 컴퓨터 안에서 원래 값으로 바꿉니다. (macOS)",
             epilog="사용 예 (examples):\n" + _columns([
                 ("deid restore", "clip/text/serve로 가린 가명 (세션 보관함)"),
                 ("deid restore --vault ~/deid.vault", "deid file --vault로 가린 가명 (암호를 물어봄)"),
                 ("deid text --restore --vault ~/deid.vault < 답변.txt", "클립보드 대신 파일 글"),
             ]) + "\n빠른 동작·단축어에서 --vault를 쓰려면 DEID_VAULT_PASS 환경변수가 필요합니다.")
    sp.add_argument("--vault", metavar="FILE",
                    help="세션 대신 보관함 파일 사용 - 암호를 물어봄, 또는 DEID_VAULT_PASS "
                         "(use a vault file)")
    sp.add_argument("--notify", action="store_true", help="알림 표시 (show a notification)")
    sp.set_defaults(func=cmd_restore)

    sp = add("shot", "화면 캡처 후 가림 (screenshot -> redact -> clipboard; macOS)",
             "영역을 선택해 캡처하고 개인정보를 가린 뒤 ~/Deid/out에 저장하고 클립보드에 넣습니다. "
             "Esc로 취소. (macOS)")
    sp.add_argument("-o", "--outdir", metavar="OUTDIR", help="저장 폴더 (default ~/Deid/out)")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--window", action="store_true", help="창 하나 캡처 (capture a window)")
    mode.add_argument("--full", action="store_true", help="전체 화면 캡처 (full screen)")
    sp.add_argument("--no-clipboard", action="store_true",
                    help="클립보드에 넣지 않음 (do not copy to the clipboard)")
    sp.add_argument("--notify", action="store_true", help="알림 표시 (show a notification)")
    sp.set_defaults(func=cmd_shot)

    sp = add("watch", "폴더 감시: 넣은 파일을 자동 비식별화 (watch a folder)",
             "IN 폴더에 새로 들어오거나 바뀐 파일을 OUT 폴더로 비식별화합니다. 원본은 옮기거나 지우지 "
             "않습니다. Ctrl-C로 종료. (poll IN, write OUT; originals untouched)")
    sp.add_argument("in_dir", metavar="IN", help="감시할 폴더 (input folder)")
    sp.add_argument("out_dir", metavar="OUT", help="결과 폴더 (output folder)")
    sp.add_argument("--interval", type=float, default=2.0, metavar="SEC",
                    help="확인 간격(초) (poll interval in seconds, default 2)")
    sp.add_argument("--notify", action="store_true", help="처리할 때마다 알림 (notify per file; macOS)")
    sp.set_defaults(func=cmd_watch)

    sp = add("serve", "로컬 웹 화면 (local web UI on 127.0.0.1)",
             "브라우저에서 쓰는 화면을 엽니다. 이 컴퓨터(127.0.0.1)에서만 접속됩니다.")
    sp.add_argument("--port", type=int, default=8765, help="포트 (port, default 8765)")
    sp.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않음 (do not open)")
    sp.set_defaults(func=cmd_serve)

    sp = add("check", "LLM으로 남은 개인정보 점검 (LLM review: Ollama or the [llm] server)",
             "비식별화된 파일을 LLM으로 다시 읽어 남아 있을지 모르는 개인정보를 보여줍니다. 기본은 이 컴퓨터의 "
             "Ollama이고, 설정에 [llm] backend = \"openai\"가 있으면 그 서버(예: 진료실 PC의 Strata)를 "
             "씁니다. 원본 파일은 먼저 비식별화한 결과만 보냅니다. 결과는 화면에만 표시하고 저장하지 않습니다.")
    sp.add_argument("path", metavar="FILE", help="검사할 파일 (file to check)")
    sp.add_argument("--llm", metavar="MODEL",
                    help=f"모델 (model; Ollama 기본 {DEFAULT_LLM}, openai 서버는 [llm] model)")
    sp.add_argument("--host", metavar="URL",
                    help="LLM 서버 주소: Ollama (기본 http://127.0.0.1:11434) 또는 [llm] backend = "
                         "\"openai\"일 때의 base_url (예: http://192.168.0.20:8080/v1; 이 컴퓨터가 아니면 [llm] "
                         "allow_remote = true 필요, http는 이름 대신 IP 주소)")
    sp.add_argument("--as-is", action="store_true",
                    help="비식별화하지 않고 그대로 검사 (check the text as it is)")
    sp.set_defaults(func=cmd_check)

    sp = add("forget", "세션 보관함 삭제 (wipe the clipboard session vault and key)",
             "clip/text/serve가 기억하는 가명 목록(세션 보관함)과 그 키를 지웁니다. 지운 뒤에는 "
             "그 전에 만든 [이름1] 등을 deid restore로 복원할 수 없습니다. 무엇을 지웠는지 보여줍니다. "
             "(보관함은 마지막으로 쓴 뒤 12시간 동안 쓰지 않으면 키와 함께 저절로 지워집니다: 다음에 "
             "deid를 실행할 때, 또는 폴더 감시나 웹 화면이 켜져 있으면 곧바로. 시간 바꾸기: "
             "DEID_SESSION_HOURS)", parents=())
    sp.set_defaults(func=cmd_forget)

    sp = add("doctor", "설치 상태 점검 (environment check)",
             "Python, 글자 인식(OCR: tesseract / Apple Vision), 설정 파일, LLM 서버(Ollama, 진료실 "
             "LLM 서버), 작업 폴더를 점검하고 고칠 방법을 알려줍니다. API 키는 어디서 찾았는지만 보여 주고 값은 "
             "보여 주지 않습니다. 문제가 있으면 이 결과를 그대로 보여주세요. "
             "(checks the installation and prints fixes)", parents=())
    sp.set_defaults(func=cmd_doctor)

    sp = add("key", "LLM 서버 API 키를 키체인에 넣기/지우기/확인 (API key in the Keychain; macOS)",
             "진료실 LLM 서버(예: Strata)의 API 키를 macOS 키체인(계정 deid, 항목 NAME)에 넣거나 지우거나 "
             "있는지 확인합니다. set은 키를 화면에 보이지 않게 물어봅니다 - 키를 명령줄에 쓰지 마세요. 설정 "
             "파일에는 api_key_keychain = \"NAME\"만 적습니다. (status never shows the key)",
             parents=(),
             epilog="사용 예 (examples):\n" + _columns([
                 ("deid key set deid-strata", "키를 물어보고 키체인에 저장 (이미 있으면 바꿈)"),
                 ("deid key status deid-strata", "있는지만 확인 (값은 보여 주지 않음)"),
                 ("deid key delete deid-strata", "키체인에서 지우기"),
             ]) + "\nmacOS가 아니면 환경변수를 쓰세요: api_key_env = \"STRATA_API_KEY\"")
    sp.add_argument("action", choices=("set", "delete", "status"),
                    help="set(넣기) | delete(지우기) | status(있는지 확인)")
    sp.add_argument("name", metavar="NAME",
                    help="키체인 항목 이름 = 설정의 api_key_keychain 값 (예: deid-strata)")
    sp.set_defaults(func=cmd_key)

    sp = add("init-config", "예시 설정 파일 만들기 (write an example config)", parents=())
    sp.add_argument("--force", action="store_true", help="있어도 덮어쓰기 (overwrite)")
    sp.add_argument("--path", metavar="FILE", help="저장 위치 (default ~/.config/deid/config.toml)")
    sp.set_defaults(func=cmd_init_config)

    sp = add("install-watch", "로그인 시 자동 폴더 감시 설치 (install the launchd watch agent; macOS)",
             "~/Deid/in 에 넣은 파일을 ~/Deid/out 으로 자동 비식별화하는 백그라운드 작업을 설치합니다.",
             parents=())
    sp.add_argument("--uninstall", action="store_true", help="제거 (remove the agent)")
    sp.add_argument("--in", dest="in_dir", metavar="DIR", help="감시 폴더 (default ~/Deid/in)")
    sp.add_argument("--out", dest="out_dir", metavar="DIR", help="결과 폴더 (default ~/Deid/out)")
    sp.add_argument("--no-notify", action="store_true", help="알림 끄기 (no notifications)")
    sp.add_argument("--print", dest="print_only", action="store_true",
                    help="설치하지 않고 plist만 출력 (print the plist only)")
    sp.set_defaults(func=cmd_install_watch)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdio()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # --help, --version, usage errors
        return e.code if isinstance(e.code, int) else (EXIT_OK if e.code is None else EXIT_FAIL)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_NOTHING
    try:   # an expired clipboard session (names!) must not wait for the next clip to go
        from .session import purge_expired_session
        purge_expired_session()
    except Exception:  # noqa: BLE001 - housekeeping never blocks a command
        pass
    _notices_shown.clear()
    try:
        return int(args.func(args))
    except CliError as e:
        _err(e.message)
        return e.code
    except mac.MacError as e:
        _err(str(e))
        return EXIT_FAIL
    except RuntimeError as e:   # LLMError not handled by the command: no traceback
        if not _is_llm_error(e):
            raise
        _err(f"LLM 서버 문제로 멈췄습니다 (stopped: LLM server problem): {_first_line(e)}\n  {LLM_HINT}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        _err("중단했습니다 (interrupted)")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
