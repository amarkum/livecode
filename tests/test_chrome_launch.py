"""Launching a Chrome of its own for the agent, and what the connection reports."""
import stat
import sys

import pytest

from livecode import browser


@pytest.fixture
def fake_chrome(tmp_path, monkeypatch):
    """A stand-in for Chrome: serves /json/version on the --remote-debugging-port it is given."""
    script = tmp_path / "fake-chrome"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import http.server, json, sys\n"
        "port = int(next(a for a in sys.argv if a.startswith('--remote-debugging-port=')).split('=')[1])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'Browser': 'Chrome/999'}).encode()\n"
        "        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)\n"
        "    def log_message(self, *a): pass\n"
        "print('fake chrome', sys.argv[1:], flush=True)\n"
        "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LIVECODE_CHROME_EXECUTABLE", str(script))
    monkeypatch.delenv("LIVECODE_BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(browser, "CHROME_DEBUG_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setattr(browser, "CHROME_DEBUG_LOG", str(tmp_path / "chrome-debug.log"))
    monkeypatch.setattr(browser, "CONFIG_PATH", str(tmp_path / "browser.json"))
    yield script
    browser._stop_managed_chrome()


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("port", [80, 1023, 65536, "abc"])
def test_ports_outside_1024_to_65535_are_refused(port):
    with pytest.raises(browser.BrowserError):
        browser.launch_chrome_and_attach(port)


def test_refused_when_the_server_sets_a_cdp_url(monkeypatch):
    monkeypatch.setenv("LIVECODE_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    with pytest.raises(browser.BrowserError, match="LIVECODE_BROWSER_CDP_URL"):
        browser.launch_chrome_and_attach(9333)


def test_a_missing_executable_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVECODE_CHROME_EXECUTABLE", str(tmp_path / "nope"))
    with pytest.raises(browser.BrowserUnavailable, match="LIVECODE_CHROME_EXECUTABLE"):
        browser.find_chrome_executable()


def test_launch_starts_chrome_with_its_own_profile_then_attaches(fake_chrome, monkeypatch, tmp_path):
    port = _free_port()
    attached = []
    monkeypatch.setattr(browser, "set_cdp_endpoint", lambda url: attached.append(url) or browser.connection_status())
    monkeypatch.setattr(browser, "cdp_endpoint", lambda: attached[-1] if attached else "")
    status = browser.launch_chrome_and_attach(port)
    assert attached == [f"http://127.0.0.1:{port}"]
    assert status["managed_launch"] is True and status["pid"] and status["profile_dir"] == str(tmp_path / "profile")
    log = (tmp_path / "chrome-debug.log").read_text()
    assert f"--remote-debugging-port={port}" in log and f"--user-data-dir={tmp_path / 'profile'}" in log
    process = browser._managed_chrome["process"]
    browser._stop_managed_chrome()
    assert process.poll() is not None, "turning it off closes the Chrome LiveCode started"
    assert browser.connection_status()["managed_launch"] is False


def test_a_chrome_that_never_opens_its_port_times_out(monkeypatch, tmp_path):
    sleeper = tmp_path / "slow-chrome"
    sleeper.write_text("#!/bin/sh\nexec sleep 30\n")
    sleeper.chmod(0o755)
    monkeypatch.setenv("LIVECODE_CHROME_EXECUTABLE", str(sleeper))
    monkeypatch.delenv("LIVECODE_BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(browser, "CHROME_LAUNCH_WAIT_S", 1.0)
    monkeypatch.setattr(browser, "CHROME_DEBUG_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setattr(browser, "CHROME_DEBUG_LOG", str(tmp_path / "chrome-debug.log"))
    with pytest.raises(browser.BrowserUnavailable, match="did not open its debugging port"):
        browser.launch_chrome_and_attach(_free_port())


def test_the_connection_route_launches(app_client, monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "launch_chrome_and_attach", lambda port: calls.append(port) or {"engine": "chrome", "managed_launch": True})
    reply = app_client.post("/livecode/browser/connection", json={"launch": True, "port": 9555}).get_json()
    assert reply["success"] and reply["managed_launch"] and calls == [9555]
    status = app_client.get("/livecode/browser/connection").get_json()
    assert "managed_launch" in status
