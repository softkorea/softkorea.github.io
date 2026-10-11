"""Local web UI: `deid serve` -> http://127.0.0.1:8765/?t=<token>.

Standard library only (ThreadingHTTPServer). Safety rules:

* binds to 127.0.0.1 only; every request must carry the random token generated at start
  (``?t=...`` or the ``X-Deid-Token`` header), so other local users/web pages cannot use it;
* the Host header must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (DNS rebinding) and
  POSTs with a foreign ``Origin`` / ``Sec-Fetch-Site: cross-site`` are refused;
* uploads are limited to 50 MB, processed in a TemporaryDirectory that is deleted afterwards
  (``deid-web-<pid>-*``). Closing the Terminal window (SIGHUP) or logging out (SIGTERM) stops
  the server like Ctrl-C, so those folders are still deleted, and a folder left behind by a
  server that was killed outright is swept at the next start;
* idle connections time out (``_Handler.timeout``), so idle sockets opened by another local
  process cannot use up the server's file descriptors;
* nothing is logged (request lines contain the token; bodies contain patient data);
* text de-identification uses the session vault, so "원래대로 복원" works (also later with
  `deid restore`); while the server runs, an expired session is deleted (with its key)
  within a minute, not only at the next request;
* settings follow the config file (:class:`deid.watch.LiveConfig`): every request re-reads it
  when it changed. A file that no longer loads keeps the previous settings, and every
  response says so (``config_warning`` in JSON / the report header, a banner on the page).

Endpoints: ``GET /`` (page), ``POST /api/text`` {text} -> {text, counts, summary, warnings},
``POST /api/restore`` {text} -> {text, restored, unknown, in_links} (tokens inside URLs are
not restored), ``POST /api/file`` (raw body with an
``X-Filename`` header, or multipart/form-data) -> processed file as a download with an
``X-Deid-Report`` header (url-encoded JSON counts).
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ContextManager
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import formats
from .types import Report
from .watch import LiveConfig

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .vault import Vault

MAX_UPLOAD = 50 * 1024 * 1024
DEFAULT_PORT = 8765
PURGE_INTERVAL = 60.0   # seconds between checks for an expired clipboard session
IDLE_TIMEOUT = 15.0     # seconds an open connection may stay silent
WORK_PREFIX = "deid-web-"
STALE_WORK_AGE = 6 * 3600.0   # untagged work folders (older versions) older than this are swept
_WORK_DIR = re.compile(r"deid-web-(?:(\d+)-)?[a-z0-9_]{8}")
TOKEN_HEADER = "X-Deid-Token"
PAGE = Path(__file__).with_name("web") / "index.html"
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _load_page() -> str:
    try:
        return PAGE.read_text("utf-8")
    except OSError:  # pragma: no cover - broken install
        return ("<!doctype html><meta charset=utf-8><title>deid</title>"
                "<p>web/index.html is missing from the deid installation.</p>")


def safe_upload_name(raw: str) -> str:
    """Base name of an uploaded file, without folders or characters unsafe on disk."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME.sub("_", name).strip().lstrip(".")
    if not name or not Path(name).suffix:
        raise HttpError(400, "파일 이름(확장자)이 필요합니다 (a file name with an extension is required)")
    return name[-200:]


def parse_multipart(content_type: str, body: bytes) -> tuple[str, bytes]:
    """(filename, data) of the first file part of a multipart/form-data body."""
    from email import policy
    from email.parser import BytesParser

    head = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("latin-1")
    try:
        msg = BytesParser(policy=policy.HTTP).parsebytes(head + body)
    except Exception as e:  # noqa: BLE001
        raise HttpError(400, "multipart 본문을 읽을 수 없습니다 (bad multipart body)") from e
    if not msg.is_multipart():
        raise HttpError(400, "multipart 본문이 아닙니다 (not multipart)")
    for part in msg.iter_parts():
        name = part.get_filename()
        if not name:
            continue
        if any("\udc80" <= ch <= "\udcff" for ch in name):   # raw UTF-8 bytes in the header
            name = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        data = part.get_payload(decode=True) or b""
        return name, data
    raise HttpError(400, "업로드된 파일이 없습니다 (no file part)")


class DeidServer(ThreadingHTTPServer):
    """The local server. `session` is a context-manager factory yielding the vault to use."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int = DEFAULT_PORT, cfg: "Config | LiveConfig | None" = None, *,
                 token: str | None = None,
                 session: Callable[[], ContextManager["Vault"]] | None = None) -> None:
        # Settings first: a config file that does not load must not leave a bound socket.
        if isinstance(cfg, LiveConfig):
            self.live = cfg
        elif cfg is None:
            self.live = LiveConfig()      # the default config file, followed
        else:
            self.live = LiveConfig.static(cfg)
        super().__init__(("127.0.0.1", port), _Handler)
        self.token = token or secrets.token_urlsafe(24)
        self._purge = session is None    # only the real session vault expires on disk
        if session is None:
            from .session import open_session
            session = open_session
        self.session = session
        self.work_lock = threading.Lock()
        self.page = _load_page()
        self._last_purge = float("-inf")
        self.service_actions()

    def service_actions(self) -> None:
        """Called by serve_forever() on every poll: delete an expired session (vault and key)
        at most once per PURGE_INTERVAL, so it does not outlive its expiry while the page is
        left open and unused."""
        if not self._purge:
            return
        now = time.monotonic()
        if now - self._last_purge < PURGE_INTERVAL:
            return
        self._last_purge = now
        try:
            from .session import purge_expired_session
            purge_expired_session()
        except Exception:  # noqa: BLE001 - housekeeping never stops the server
            pass

    @property
    def cfg(self) -> "Config":
        """The settings in use (the latest config file that loaded)."""
        return self.live.cfg

    @cfg.setter
    def cfg(self, value: "Config") -> None:
        self.live = LiveConfig.static(value)

    def refresh_config(self) -> str | None:
        """Re-read the config file if it changed (every request). Returns the current problem:
        None, or why the file does not load (the previous settings stay in use)."""
        try:
            self.live.refresh()
        except Exception:  # noqa: BLE001 - never fail a request over the settings check
            pass
        return self.config_warning()

    def config_warning(self) -> str | None:
        err = self.live.error
        if not err:
            return None
        return ("설정 파일 오류 - 고칠 때까지 이전 설정으로 처리합니다 (config error: the previous "
                f"settings are in use): {err}")

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts()}

    # ------------------------------------------------------------------ operations

    def _detector(self, vault: "Vault", cfg: "Config | None" = None):
        from .engine import Detector
        from .session import seed_detector

        det = Detector(cfg if cfg is not None else self.cfg)   # rebuilt per request
        seed_detector(det, vault)
        return det

    def deid_text(self, text: str) -> dict:
        from .anonymizer import Anonymizer
        from .formats.text import deid_text

        cfg = self.cfg     # one version of the settings for the whole request
        with self.work_lock, self.session() as vault:
            out, report = deid_text(text, self._detector(vault, cfg), Anonymizer(cfg, vault))
        return {"text": out, "counts": dict(sorted(report.counts.items())),
                "total": report.total, "summary": formats.format_counts(report, cfg),
                "warnings": list(report.warnings)}

    def restore_text(self, text: str) -> dict:
        from .session import count_tokens, unknown_tokens

        cfg = self.cfg
        with self.work_lock, self.session() as vault:
            in_links = vault.tokens_in_links(text)
            n = count_tokens(text, vault) - in_links
            unknown = unknown_tokens(text, vault, cfg)
            out = vault.restore(text, links=False)
        return {"text": out, "restored": n, "unknown": unknown, "in_links": in_links}

    def deid_file(self, name: str, data: bytes) -> tuple[str, bytes, Report]:
        from .anonymizer import Anonymizer

        cfg = self.cfg
        name = safe_upload_name(name)
        if not formats.is_supported(name):
            raise HttpError(415, formats.unsupported_message(name))
        with tempfile.TemporaryDirectory(prefix=work_prefix()) as td:
            src = Path(td) / "in" / name
            src.parent.mkdir()
            src.write_bytes(data)
            with self.work_lock, self.session() as vault:
                report = formats.process_to_dir(src, Path(td) / "out", self._detector(vault, cfg),
                                                Anonymizer(cfg, vault), cfg)
            out = Path(report.output)
            return out.name, out.read_bytes(), report


class _Handler(BaseHTTPRequestHandler):
    server: DeidServer
    server_version = "deid"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT    # per socket read: drops idle keep-alive / never-sending clients

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # never log: URLs carry the token, bodies carry patient data

    # ------------------------------------------------------------------ responses

    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None
              ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        obj = {"error": message}
        # only for requests that passed the token check: the message may quote the file
        warning = self.server.config_warning() if getattr(self, "_authed", False) else None
        if warning:
            obj["config_warning"] = warning
        self._json(status, obj)

    def _result(self, obj: dict) -> None:
        warning = self.server.config_warning()
        if warning:
            obj = dict(obj, config_warning=warning)
        self._json(200, obj)

    # ------------------------------------------------------------------ checks

    def _token_ok(self) -> bool:
        token = self.headers.get(TOKEN_HEADER)
        if token is None:
            vals = parse_qs(urlsplit(self.path).query).get("t")
            token = vals[0] if vals else None
        return token is not None and hmac.compare_digest(token.encode("utf-8", "replace"),
                                                         self.server.token.encode("ascii"))

    def _check(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts():
            raise HttpError(403, "허용되지 않은 Host 입니다 (forbidden host)")
        if not self._token_ok():
            raise HttpError(403, "접근 토큰이 없거나 틀렸습니다. 'deid serve'가 알려준 주소로 여세요 "
                                 "(missing or wrong access token)")
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin is not None and origin.lower() not in self.server.allowed_origins():
                raise HttpError(403, "다른 사이트에서 온 요청은 거부합니다 (cross-origin request)")
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site and site not in ("same-origin", "none"):
                raise HttpError(403, "다른 사이트에서 온 요청은 거부합니다 (cross-site request)")

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise HttpError(411, "Content-Length가 필요합니다 (chunked uploads are not supported)")
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            raise HttpError(411, "Content-Length가 필요합니다 (Content-Length required)")
        if length > MAX_UPLOAD:
            self.close_connection = True
            raise HttpError(413, f"파일이 너무 큽니다 (최대 {MAX_UPLOAD // (1024 * 1024)} MB) "
                                 "(upload too large)")
        data = self.rfile.read(length) if length else b""
        self._body_read = True
        if len(data) != length:
            self.close_connection = True
            raise HttpError(400, "요청 본문이 잘렸습니다 (truncated body)")
        return data

    def _json_body(self) -> str:
        body = self._read_body()
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise HttpError(400, "JSON 본문이 올바르지 않습니다 (invalid JSON)") from e
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise HttpError(400, "'text' 문자열이 필요합니다 ('text' string required)")
        return text

    # ------------------------------------------------------------------ dispatch

    def _handle(self, fn: Callable[[], None]) -> None:
        # A request body we did not read would be parsed as the next request on this
        # keep-alive connection: close the connection whenever we answer without reading it.
        has_body = bool(self.headers.get("Transfer-Encoding")) or \
            self.headers.get("Content-Length") not in (None, "0")
        self._body_read = not has_body
        self._authed = False
        try:
            self._check()
            self._authed = True
            self.server.refresh_config()   # config.toml edits apply from this request on
            fn()
        except HttpError as e:
            self._fail(e.status, e.message)
        except formats.UnsupportedFormatError as e:
            self._fail(415, str(e))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True   # the browser went away: nothing to answer
        except Exception as e:  # noqa: BLE001
            self._fail(422 if isinstance(e, (ValueError, RuntimeError, OSError)) else 500,
                       _error_message(e))

    def _fail(self, status: int, message: str) -> None:
        if not self._body_read:
            self.close_connection = True
        self._error(status, message)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(self._get)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._handle(self._post)

    def _get(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/", "/index.html"):
            raise HttpError(404, "없는 주소입니다 (not found)")
        nonce = secrets.token_urlsafe(16)
        warning = self.server.config_warning()
        page = (self.server.page.replace("__CONFIG_HIDDEN__", "" if warning else " hidden")
                .replace("__CONFIG_WARNING__", html.escape(warning or ""))
                .replace("__NONCE__", nonce)
                .replace("__TOKEN__", self.server.token)
                .replace("__MAX_MB__", str(MAX_UPLOAD // (1024 * 1024))))
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
               "connect-src 'self'; img-src 'self' data: blob:; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'none'")
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp})

    def _post(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/text":
            self._result(self.server.deid_text(self._json_body()))
        elif path == "/api/restore":
            self._result(self.server.restore_text(self._json_body()))
        elif path == "/api/file":
            self._file()
        else:
            raise HttpError(404, "없는 주소입니다 (not found)")

    def _file(self) -> None:
        ctype = self.headers.get("Content-Type") or ""
        raw_name = self.headers.get("X-Filename")
        if raw_name is None and not ctype.lower().startswith("multipart/form-data"):
            # Refuse before reading a body we could not use anyway.
            raise HttpError(400, "X-Filename 헤더 또는 multipart 업로드가 필요합니다 "
                                 "(X-Filename header or multipart upload required)")
        body = self._read_body()
        if raw_name is not None:
            name, data = unquote(raw_name), body
        else:
            name, data = parse_multipart(ctype, body)
        out_name, payload, report = self.server.deid_file(name, data)
        info = {"filename": out_name, "counts": dict(sorted(report.counts.items())),
                "total": report.total, "summary": formats.format_counts(report, self.server.cfg),
                "warnings": list(report.warnings)}
        warning = self.server.config_warning()
        if warning:
            info["config_warning"] = warning
        ascii_name = "deid-output" + Path(out_name).suffix.encode("ascii", "ignore").decode()
        headers = {
            "Content-Disposition": (f'attachment; filename="{ascii_name}"; '
                                    f"filename*=UTF-8''{quote(out_name, safe='')}"),
            "X-Deid-Report": quote(json.dumps(info, ensure_ascii=False), safe=""),
            "X-Deid-Filename": quote(out_name, safe=""),
        }
        self._send(200, payload, "application/octet-stream", headers)


def _error_message(e: BaseException) -> str:
    """Error text for the page: handler messages (no PII by contract), else the type only."""
    if isinstance(e, (ValueError, RuntimeError)):
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return f"처리하지 못했습니다: {msg[:400]}"
    return f"처리 중 오류가 발생했습니다 (processing failed: {type(e).__name__})"


def make_server(port: int = DEFAULT_PORT, cfg: "Config | None" = None, *,
                token: str | None = None, tries: int = 1,
                session: Callable[[], ContextManager["Vault"]] | None = None) -> DeidServer:
    """Bind a DeidServer on 127.0.0.1:`port` (or the next free port, up to `tries` ports)."""
    last: OSError | None = None
    for i in range(max(1, tries)):
        try:
            return DeidServer(port + i if port else 0, cfg, token=token, session=session)
        except OSError as e:
            last = e
            if not port:
                break
    raise OSError(f"포트 {port}를 사용할 수 없습니다. --port로 다른 번호를 지정하세요 "
                  f"(port {port} is busy: {last})")


def work_prefix() -> str:
    """Prefix of this process's temporary upload folders (the pid lets a later start tell
    folders of a live server from ones left behind by a killed one)."""
    return f"{WORK_PREFIX}{os.getpid()}-"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:          # EPERM: exists, belongs to someone else
        return True
    return True


def sweep_work_dirs(base: str | os.PathLike | None = None, *, own: bool = False) -> int:
    """Delete temporary upload folders (they hold ORIGINAL uploads) of servers that are gone.

    Only this account's ``deid-web-*`` folders directly in `base` (default: the temp folder)
    are touched: those tagged with the pid of a process that no longer exists, untagged ones
    (older versions) not modified for STALE_WORK_AGE, and with ``own=True`` this process's
    own folders (at shutdown). Returns the number of folders deleted."""
    root = Path(base) if base is not None else Path(tempfile.gettempdir())
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    uid = os.getuid() if hasattr(os, "getuid") else None
    me, now, n = os.getpid(), time.time(), 0
    for p in entries:
        m = _WORK_DIR.fullmatch(p.name)
        if not m:
            continue
        try:
            st = p.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or (uid is not None and st.st_uid != uid):
            continue
        pid = int(m.group(1)) if m.group(1) else None
        if pid == me:
            if not own:
                continue
        elif pid is not None:
            if _pid_alive(pid):
                continue
        elif now - st.st_mtime < STALE_WORK_AGE:
            continue
        shutil.rmtree(p, ignore_errors=True)
        n += not p.exists()
    return n


def _stop_on_signals() -> dict:
    """Make SIGHUP (Terminal window closed) and SIGTERM (logout, kill) stop the server like
    Ctrl-C, so the clean-up below still runs. Returns the previous handlers."""
    import signal

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    old: dict = {}
    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            old[sig] = signal.signal(sig, stop)
        except (ValueError, OSError):   # not the main thread / not supported
            pass
    return old


def _restore_signals(old: dict) -> None:
    import signal

    for sig, handler in old.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


def _say(out: Any, *lines: str) -> None:
    try:
        for line in lines:
            print(line, file=out, flush=True)
    except (OSError, ValueError):    # the Terminal window is already gone
        pass


@contextmanager
def running(server: DeidServer):
    """Run `server` in a background thread for the duration of the block (tests)."""
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        t.join(5)


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, cfg: "Config | None" = None,
          out=None) -> int:
    """Blocking `deid serve`. Returns an exit code."""
    out = out or sys.stdout
    sweep_work_dirs()       # uploads left behind by a server that was killed outright
    try:
        server = make_server(port, cfg, tries=10)
    except OSError as e:
        print(str(e), file=sys.stderr)
        return 2
    from . import __version__

    _say(out, f"deid {__version__} 웹 화면 (local web UI) - 이 컴퓨터에서만 열립니다 (127.0.0.1 only)",
         f"  {server.url}", "설정 파일(config.toml)을 고치면 다음 요청부터 적용됩니다. "
         "deid를 업데이트했다면 껐다가 다시 실행하세요.", "종료: Ctrl-C (press Ctrl-C to stop)")
    old = _stop_on_signals()
    try:
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(server.url)
            except Exception:  # noqa: BLE001 - the URL is printed anyway
                pass
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        _say(out, "", "종료합니다 (stopped)")
    finally:
        try:
            server.server_close()
        finally:
            # A request still running in its (daemon) thread dies with the process: delete
            # its temporary copy of the original upload now.
            sweep_work_dirs(own=True)
            _restore_signals(old)
    return 0


__all__ = ("DeidServer", "make_server", "serve", "running", "MAX_UPLOAD", "parse_multipart",
           "safe_upload_name", "sweep_work_dirs")
