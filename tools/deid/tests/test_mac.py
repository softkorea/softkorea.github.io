"""Tests for deid.mac: every macOS tool call goes through mac.run, which is faked here."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from deid import mac


class FakeMac:
    """Records commands and emulates pbcopy/pbpaste, osascript, screencapture, security."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[bytes | None] = []
        self.clipboard = b""
        self.info = "«class utf8», 10"
        self.keychain: dict[tuple[str, str], str] = {}
        self.capture_ok = True
        self.capture_bytes = b"\x89PNG shot"
        self.image_set: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def __call__(self, args, *, input=None, timeout=None):
        args = list(args)
        self.calls.append(args)
        self.inputs.append(input)
        tool = args[0]
        out, rc = b"", 0
        if tool in self.fail:
            return subprocess.CompletedProcess(args, 1, b"", b"boom")
        if tool == "pbcopy":
            self.clipboard = input or b""
            self.info = "«class utf8», 10" if self.clipboard else ""
        elif tool == "pbpaste":
            out = self.clipboard
        elif tool == "osascript":
            script = "\n".join(args[i + 1] for i, a in enumerate(args) if a == "-e")
            argv = [a for i, a in enumerate(args[1:], 1) if a != "-e" and args[i - 1] != "-e"]
            if script == "clipboard info":
                out = self.info.encode("utf-8")
            elif "display notification" in script:
                self.notifications.append((argv[0], argv[1]))
            elif "write png" in script:
                if "PNGf" in self.info:
                    Path(argv[0]).write_bytes(b"\x89PNG fake")
                else:
                    rc = 1
            elif "set the clipboard to" in script:
                self.image_set.append(argv[0])
                self.info = "«class PNGf», 9"
        elif tool == "screencapture":
            if self.capture_ok:
                Path(args[-1]).write_bytes(self.capture_bytes)
            else:
                rc = 1
        elif tool == "security":
            acct = args[args.index("-a") + 1]
            svc = args[args.index("-s") + 1]
            if args[1] == "find-generic-password":
                if (acct, svc) in self.keychain:
                    out = (self.keychain[(acct, svc)] + "\n").encode()
                else:
                    rc = 44
            elif args[1] == "add-generic-password":
                self.keychain[(acct, svc)] = args[args.index("-w") + 1]
            elif args[1] == "delete-generic-password":
                rc = 0 if self.keychain.pop((acct, svc), None) is not None else 44
        return subprocess.CompletedProcess(args, rc, out, b"")


@pytest.fixture()
def fake(monkeypatch):
    f = FakeMac()
    monkeypatch.setattr(mac, "is_macos", lambda: True)
    monkeypatch.setattr(mac, "run", f)
    return f


# ---------------------------------------------------------------------- clipboard text


def test_korean_round_trip_through_fake_pbcopy(fake):
    text = "환자 홍길동 님, 연락처 010-1234-5678 ✓"
    mac.copy_text(text)
    assert fake.calls[-1] == ["pbcopy"]
    assert fake.inputs[-1] == text.encode("utf-8")
    assert mac.paste_text() == text
    assert fake.calls[-1] == ["pbpaste", "-Prefer", "txt"]


def test_clear_clipboard_sets_empty_text(fake):
    mac.copy_text("이서연")
    mac.clear_clipboard()
    assert fake.clipboard == b""


def test_pbpaste_failure_is_friendly(fake):
    fake.fail.add("pbpaste")
    with pytest.raises(mac.MacError, match="클립보드"):
        mac.paste_text()


def test_real_run_sets_utf8_locale(monkeypatch):
    monkeypatch.setenv("LANG", "C")
    monkeypatch.delenv("LC_ALL", raising=False)
    proc = mac.run([sys.executable, "-c",
                    "import os,sys; sys.stdout.write(os.environ['LANG']+'|'+os.environ['LC_ALL'])"])
    assert proc.returncode == 0
    assert proc.stdout.decode() == "en_US.UTF-8|en_US.UTF-8"


def test_missing_tool_raises_mac_error():
    with pytest.raises(mac.MacError, match="찾을 수 없습니다"):
        mac.run(["deid-no-such-tool-xyz"])


def test_non_macos_raises_friendly_error(monkeypatch):
    calls = []
    monkeypatch.setattr(mac, "is_macos", lambda: False)
    monkeypatch.setattr(mac, "run", lambda *a, **k: calls.append(a))
    for fn, args in ((mac.paste_text, ()), (mac.copy_text, ("x",)), (mac.clipboard_info, ()),
                     (mac.screencapture, ("/tmp/x.png",)), (mac.keychain_get, ("a", "b"))):
        with pytest.raises(mac.MacError, match="macOS"):
            fn(*args)
    assert calls == []


# ---------------------------------------------------------------------- clipboard images


def test_clipboard_info_and_image_detection(fake):
    fake.info = "«class PNGf», 1234, «class 8BPS», 5678, TIFF picture, 9999"
    assert mac.clipboard_info() == fake.info
    assert fake.calls[-1] == ["osascript", "-e", "clipboard info"]
    assert mac.clipboard_has_image()
    assert mac.info_has_image("TIFF picture, 100")
    assert not mac.info_has_image("«class utf8», 10, «class ut16», 22")
    assert mac.info_has_files("«class furl», 40, «class utf8», 10")


def test_get_clipboard_image_passes_path_as_argv(fake, tmp_path):
    fake.info = "«class PNGf», 9"
    target = tmp_path / 'we"ird name.png'
    assert mac.get_clipboard_image(target)
    call = fake.calls[-1]
    assert call[0] == "osascript"
    assert call[-1] == str(target)                     # path is an argv item ...
    assert all(str(target) not in a for a in call[1:-1])  # ... never pasted into the script
    assert any("«class PNGf»" in a for a in call)
    assert target.read_bytes().startswith(b"\x89PNG")


def test_get_clipboard_image_without_image(fake, tmp_path):
    fake.info = "«class utf8», 10"
    assert not mac.get_clipboard_image(tmp_path / "x.png")


def test_set_clipboard_image(fake, tmp_path):
    p = tmp_path / "out.png"
    p.write_bytes(b"\x89PNG")
    mac.set_clipboard_image(p)
    assert fake.image_set == [str(p)]
    script = " ".join(fake.calls[-1])
    assert "set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)" in script


def test_set_clipboard_image_failure(fake, tmp_path):
    fake.fail.add("osascript")
    with pytest.raises(mac.MacError):
        mac.set_clipboard_image(tmp_path / "x.png")


# ---------------------------------------------------------------------- screencapture


@pytest.mark.parametrize("mode,expected", [
    ("selection", ["screencapture", "-i", "-x"]),
    ("window", ["screencapture", "-i", "-W", "-o", "-x"]),
    ("full", ["screencapture", "-x"]),
])
def test_screencapture_args(fake, tmp_path, mode, expected):
    p = tmp_path / "s.png"
    assert mac.screencapture(p, mode)
    assert fake.calls[-1] == expected + [str(p)]


def test_screencapture_cancelled(fake, tmp_path):
    fake.capture_ok = False
    assert not mac.screencapture(tmp_path / "s.png")


def test_screencapture_bad_mode(fake, tmp_path):
    with pytest.raises(ValueError):
        mac.screencapture(tmp_path / "s.png", "region")


# ---------------------------------------------------------------------- keychain / notify


def test_keychain_commands(fake):
    assert mac.keychain_get("deid", "deid-session") is None
    assert fake.calls[-1] == ["security", "find-generic-password", "-a", "deid", "-s",
                              "deid-session", "-w"]
    assert mac.keychain_set("deid", "deid-session", "ab12")
    assert fake.calls[-1] == ["security", "add-generic-password", "-a", "deid", "-s",
                              "deid-session", "-w", "ab12", "-U"]
    assert mac.keychain_get("deid", "deid-session") == "ab12"
    assert mac.keychain_delete("deid", "deid-session")
    assert fake.calls[-1] == ["security", "delete-generic-password", "-a", "deid", "-s",
                              "deid-session"]
    assert not mac.keychain_delete("deid", "deid-session")


def test_notify_uses_argv(fake):
    assert mac.notify("deid", '이름 2, "전화번호" 1')
    assert fake.notifications == [("deid", '이름 2, "전화번호" 1')]
    call = fake.calls[-1]
    assert call[-2:] == ["deid", '이름 2, "전화번호" 1']


def test_notify_is_noop_off_macos(monkeypatch):
    calls = []
    monkeypatch.setattr(mac, "is_macos", lambda: False)
    monkeypatch.setattr(mac, "run", lambda *a, **k: calls.append(a))
    assert mac.notify("t", "m") is False
    assert calls == []


def test_notify_never_raises(fake):
    fake.fail.add("osascript")
    assert mac.notify("t", "m") is False


# ---------------------------------------------------------------------- launchd agent


def test_watch_agent_plist_is_valid(tmp_path):
    data = mac.watch_agent_plist("/Users/a/.local/bin/deid", "/Users/a/Deid/in",
                                 "/Users/a/Deid/out", log_path="/Users/a/Library/Logs/deid-watch.log")
    assert data.startswith(b"<?xml")
    pl = plistlib.loads(data)
    assert pl["Label"] == "com.softkorea.deid.watch"
    assert pl["ProgramArguments"] == ["/Users/a/.local/bin/deid", "watch", "/Users/a/Deid/in",
                                      "/Users/a/Deid/out"]
    assert pl["RunAtLoad"] is True and pl["KeepAlive"] is True
    assert pl["StandardOutPath"] == "/Users/a/Library/Logs/deid-watch.log"
    assert pl["StandardErrorPath"] == pl["StandardOutPath"]
    assert pl["EnvironmentVariables"]["LANG"] == "en_US.UTF-8"
    assert "/opt/homebrew/bin" in pl["EnvironmentVariables"]["PATH"]


def test_watch_agent_plist_options_and_command_prefix():
    pl = plistlib.loads(mac.watch_agent_plist(["/usr/bin/python3", "-m", "deid"], "/i", "/o",
                                              notify=True, interval=5))
    assert pl["ProgramArguments"] == ["/usr/bin/python3", "-m", "deid", "watch", "/i", "/o",
                                      "--interval", "5", "--notify"]
    assert pl["StandardOutPath"].endswith("Library/Logs/deid-watch.log")


def test_watch_agent_plist_escapes_xml():
    pl = plistlib.loads(mac.watch_agent_plist("/a/deid", "/x/<in>&", "/y/out"))
    assert pl["ProgramArguments"][2] == "/x/<in>&"


def test_install_and_uninstall_watch_agent(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = mac.watch_agent_plist("/a/deid", tmp_path / "in", tmp_path / "out")
    path = mac.install_watch_agent(plist)
    uid = os.getuid()
    assert path == tmp_path / "Library/LaunchAgents/com.softkorea.deid.watch.plist"
    assert path.read_bytes() == plist
    assert ["launchctl", "bootout", f"gui/{uid}/com.softkorea.deid.watch"] in fake.calls
    assert fake.calls[-1] == ["launchctl", "bootstrap", f"gui/{uid}", str(path)]
    assert (tmp_path / "Library/Logs").is_dir()
    assert mac.uninstall_watch_agent()
    assert not path.exists()
    assert fake.calls[-1] == ["launchctl", "bootout", f"gui/{uid}/com.softkorea.deid.watch"]
    assert not mac.uninstall_watch_agent()


def test_install_watch_agent_bootstrap_failure(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake.fail.add("launchctl")
    with pytest.raises(mac.MacError, match="launchctl"):
        mac.install_watch_agent(mac.watch_agent_plist("/a/deid", "/i", "/o"))
