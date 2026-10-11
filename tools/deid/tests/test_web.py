"""Tests for deid.web: local server with token, Host/Origin checks, text/restore/file APIs."""
from __future__ import annotations

import http.client
import json
import re
import socket
import tempfile
from urllib.parse import quote, unquote

import pytest

from deid import web
from deid.config import default_config

NOTE = "환자명: 홍길동\n연락처 010-1234-5678\n홍길동 님 혈압 정상"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    srv = web.make_server(0, default_config())
    with web.running(srv):
        yield srv


def request(srv, method, path, body=None, headers=None, token=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=30)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{srv.port}")
        hdrs = dict(headers or {})
        if token:
            hdrs.setdefault("X-Deid-Token", srv.token)
        if body is not None:
            hdrs.setdefault("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def post_json(srv, path, obj, **kw):
    status, headers, body = request(srv, "POST", path,
                                    json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                    {"Content-Type": "application/json", **kw.pop("headers", {})},
                                    **kw)
    return status, headers, json.loads(body)


# ---------------------------------------------------------------------- access control


def test_binds_to_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"
    assert server.url == f"http://127.0.0.1:{server.port}/?t={server.token}"
    assert len(server.token) >= 24


def test_token_required(server):
    status, _, body = request(server, "GET", "/", token=False)
    assert status == 403 and "토큰" in json.loads(body)["error"]
    status, _, _ = request(server, "GET", "/?t=wrong", token=False)
    assert status == 403
    status, _, _ = post_json(server, "/api/text", {"text": NOTE}, token=False)
    assert status == 403
    status, _, _ = post_json(server, "/api/text", {"text": NOTE},
                             headers={"X-Deid-Token": "x" * 32}, token=False)
    assert status == 403


def test_token_in_query_or_header(server):
    status, headers, body = request(server, "GET", f"/?t={server.token}", token=False)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    status, _, _ = request(server, "GET", "/")
    assert status == 200


@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1:1", "attacker.local",
                                  "", "localhost.evil.com:{port}"])
def test_host_header_checked(server, host):
    status, _, body = request(server, "GET", f"/?t={server.token}",
                              host=host.format(port=server.port) or " ")
    assert status == 403


def test_localhost_host_allowed(server):
    status, _, _ = request(server, "GET", "/", host=f"localhost:{server.port}")
    assert status == 200


def test_cross_origin_post_rejected(server):
    status, _, data = post_json(server, "/api/text", {"text": NOTE},
                                headers={"Origin": "https://evil.example"})
    assert status == 403
    status, _, data = post_json(server, "/api/text", {"text": NOTE},
                                headers={"Origin": "null"})
    assert status == 403
    status, _, data = post_json(server, "/api/text", {"text": NOTE},
                                headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    status, _, data = post_json(server, "/api/text", {"text": NOTE},
                                headers={"Origin": f"http://127.0.0.1:{server.port}",
                                         "Sec-Fetch-Site": "same-origin"})
    assert status == 200


def test_rejected_body_does_not_poison_keepalive(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    body = json.dumps({"text": NOTE}).encode()
    conn.request("POST", "/api/text", body=body,
                 headers={"X-Deid-Token": server.token, "Origin": "http://evil.example"})
    r = conn.getresponse()
    r.read()
    assert r.status == 403
    conn.request("POST", "/api/text", body=body, headers={"X-Deid-Token": server.token})
    r = conn.getresponse()
    assert r.status == 200 and "[이름1]" in json.loads(r.read())["text"]
    conn.close()


def test_unknown_path_404(server):
    assert request(server, "GET", "/etc/passwd")[0] == 404
    assert post_json(server, "/api/nothing", {"text": "x"})[0] == 404


# ---------------------------------------------------------------------- page


def test_page_is_self_contained(server):
    status, headers, body = request(server, "GET", "/")
    page = body.decode("utf-8")
    assert status == 200
    assert "__TOKEN__" not in page and "__NONCE__" not in page and "__MAX_MB__" not in page
    assert server.token in page
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page)   # no CDN / external
    assert "<link" not in page and "@import" not in page
    for word in ("비식별화", "원래대로 복원", "복사", "자동 탐지는 완벽하지 않으니 공유 전 결과를 확인하세요",
                 "prefers-color-scheme"):
        assert word in page
    csp = headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in page
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"


# ---------------------------------------------------------------------- text APIs


def test_text_round_trip_with_restore(server):
    status, _, data = post_json(server, "/api/text", {"text": NOTE})
    assert status == 200
    assert "홍길동" not in data["text"] and "1234" not in data["text"]
    assert data["counts"] == {"PERSON": 2, "PHONE": 1}
    assert data["summary"] == "이름 2, 전화번호 1"
    status, _, back = post_json(server, "/api/restore", {"text": data["text"] + "\n[이름9]"})
    assert status == 200
    assert back["text"].startswith("환자명: 홍길동\n")
    assert back["restored"] == 2 and back["unknown"] == 1


def test_session_vault_shared_with_cli(server, monkeypatch, capsys):
    import io
    import sys

    from deid import cli

    _, _, data = post_json(server, "/api/text", {"text": "보호자 김민준 님"})
    token = re.search(r"\[이름\d+\]", data["text"]).group()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(f"{token} 연락".encode())))
    assert cli.main(["text", "--restore"]) == 0
    assert capsys.readouterr().out == "김민준 연락"


@pytest.mark.parametrize("body", [b"not json", b'{"txt": 1}', b'["a"]', b'{"text": 5}'])
def test_bad_json(server, body):
    status, _, data = request(server, "POST", "/api/text", body,
                              {"Content-Type": "application/json"})
    assert status == 400
    assert "error" in json.loads(data)


# ---------------------------------------------------------------------- file API


def test_file_upload_txt(server, tmp_path):
    name = "홍길동_진료기록.txt"
    status, headers, body = request(server, "POST", "/api/file", NOTE.encode("utf-8"),
                                    {"X-Filename": quote(name),
                                     "Content-Type": "application/octet-stream"})
    assert status == 200
    out = body.decode("utf-8")
    assert "홍길동" not in out and "[이름1]" in out and "010-****-5678" in out
    report = json.loads(unquote(headers["X-Deid-Report"]))
    assert report["counts"] == {"PERSON": 2, "PHONE": 1}
    assert report["filename"] == "[이름1]_진료기록.deid.txt"
    assert unquote(headers["X-Deid-Filename"]) == "[이름1]_진료기록.deid.txt"
    cd = headers["Content-Disposition"]
    assert cd.startswith('attachment; filename="deid-output.txt"')
    assert "filename*=UTF-8''" in cd and "홍길동" not in unquote(cd)
    assert list((tmp_path / "tmp").iterdir()) == []      # upload temp dir removed


def test_file_upload_multipart(server, tmp_path):
    boundary = "deidBoundary123"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"이서연 메모.txt\"\r\nContent-Type: text/plain\r\n\r\n").encode("utf-8")
    body += "보호자 이서연 님 연락처 010-2222-3333".encode("utf-8")
    body += f"\r\n--{boundary}--\r\n".encode()
    status, headers, data = request(server, "POST", "/api/file", body,
                                    {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert status == 200
    text = data.decode("utf-8")
    assert "이서연" not in text and "2222" not in text
    assert unquote(headers["X-Deid-Filename"]).endswith(".deid.txt")
    assert "이서연" not in unquote(headers["X-Deid-Filename"])


def test_file_upload_path_components_stripped(server, tmp_path):
    status, headers, _ = request(server, "POST", "/api/file", "환자 홍길동 님".encode(),
                                 {"X-Filename": quote("../../etc/메모.txt")})
    assert status == 200
    assert unquote(headers["X-Deid-Filename"]) == "메모.deid.txt"


def test_file_upload_unsupported(server):
    status, _, body = request(server, "POST", "/api/file", b"\xd0\xcf\x11\xe0",
                              {"X-Filename": quote("검사.xls")})
    assert status == 415
    assert ".xlsx" in json.loads(body)["error"]
    status, _, body = request(server, "POST", "/api/file", b"abc", {"X-Filename": "noext"})
    assert status == 400


def test_file_upload_needs_name(server):
    status, _, body = request(server, "POST", "/api/file", b"abc",
                              {"Content-Type": "application/octet-stream"})
    assert status == 400


def test_file_processing_error_is_reported(server):
    status, _, body = request(server, "POST", "/api/file", b"\xff\xfe\x00\xd8broken\x00\x00\xd8",
                              {"X-Filename": quote("broken.txt")})
    assert status in (415, 422)
    assert json.loads(body)["error"]


def test_oversize_upload_rejected_without_reading(server):
    with socket.create_connection(("127.0.0.1", server.port), timeout=10) as s:
        head = (f"POST /api/file HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                f"X-Deid-Token: {server.token}\r\nX-Filename: big.txt\r\n"
                f"Content-Length: {web.MAX_UPLOAD + 1}\r\n\r\n")
        s.sendall(head.encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    status_line, _, rest = data.partition(b"\r\n")
    assert b" 413 " in status_line
    assert "최대 50 MB" in rest.decode("utf-8")


def test_missing_content_length(server):
    with socket.create_connection(("127.0.0.1", server.port), timeout=10) as s:
        s.sendall((f"POST /api/text HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                   f"X-Deid-Token: {server.token}\r\nTransfer-Encoding: chunked\r\n\r\n"
                   "5\r\nhello\r\n0\r\n\r\n").encode())
        data = s.recv(65536)
    assert b" 411 " in data.split(b"\r\n", 1)[0]


def test_safe_upload_name():
    assert web.safe_upload_name("C:\\Users\\a\\메모.txt") == "메모.txt"
    assert web.safe_upload_name("../x/.hidden.txt") == "hidden.txt"
    assert web.safe_upload_name('a<b>:"c".txt') == "a_b___c_.txt"
    with pytest.raises(web.HttpError):
        web.safe_upload_name("")


def test_make_server_tries_next_port(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_HOME", str(tmp_path / "home"))
    first = web.make_server(0, default_config())
    try:
        second = web.make_server(first.port, default_config(), tries=5)
        try:
            assert second.port != first.port
        finally:
            second.server_close()
        with pytest.raises(OSError, match="--port"):
            web.make_server(first.port, default_config(), tries=1)
    finally:
        first.server_close()
