"""Test setup: the package imports itself as ``livecode``, so its parent folder goes on sys.path, and
HOME points at a scratch folder so tests never read or write the real ~/.livecode (settings, projects,
browser profiles)."""
from __future__ import annotations

import functools
import http.server
import os
import shutil
import socket
import sys
import tempfile
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, os.path.join(REPO, "host"))  # the host app package (app.runtime), as server.py sets it up

_HOME = tempfile.mkdtemp(prefix="livecode-test-home-")
os.environ["HOME"] = _HOME
os.environ.setdefault("LIVECODE_BROWSER_LANES", "1")
if not os.environ.get("LIVECODE_BROWSER_EXECUTABLE") and os.path.exists("/opt/pw-browsers/chromium"):
    os.environ["LIVECODE_BROWSER_EXECUTABLE"] = "/opt/pw-browsers/chromium"

FIXTURES = os.path.join(HERE, "fixtures")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def site_dir():
    """A copy of the design demo pages that tests may add variants to (an app with a fix applied)."""
    path = tempfile.mkdtemp(prefix="lc-site-")
    shutil.copytree(os.path.join(FIXTURES, "design_demo"), path, dirs_exist_ok=True)
    return path


@pytest.fixture(scope="session")
def site(site_dir):
    """The design demo pages, served on a local port: ``site + "/app.html"``."""
    port = _free_port()
    handler = functools.partial(_Quiet, directory=site_dir)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@functools.lru_cache(maxsize=1)
def _browser_ok() -> str:
    try:
        from livecode import browser
    except Exception as exc:  # pragma: no cover - missing dependencies
        return f"livecode.browser does not import: {exc}"
    if not browser.playwright_installed():
        return "Playwright is not installed"
    state = tempfile.mkdtemp(prefix="lc-probe-")
    result = browser.agent_action(state, {"action": "navigate", "url": "about:blank"}, session_id="probe")
    return "" if result.get("success") else f"the browser does not start: {result.get('error')}"


@pytest.fixture(scope="session")
def browser_ready():
    reason = _browser_ok()
    if reason:
        pytest.skip(reason)
    return True


_SHOTS_SCRIPT = r"""
import sys
from playwright.sync_api import sync_playwright
url, out, exe = sys.argv[1], sys.argv[2], sys.argv[3]
with sync_playwright() as p:
    b = p.chromium.launch(**({"executable_path": exe} if exe else {}))
    for dpr in (1, 2):
        pg = b.new_page(viewport={"width": 1100, "height": 700}, device_scale_factor=dpr)
        pg.goto(url)
        sfx = "" if dpr == 1 else "@2x"
        pg.locator(".card").first.screenshot(path=f"{out}/card{sfx}.png")
        pg.locator("input.search").screenshot(path=f"{out}/input{sfx}.png")
        pg.locator("form.filters").screenshot(path=f"{out}/form{sfx}.png")
        if dpr == 1:
            pg.screenshot(path=f"{out}/page.png", full_page=True)
    b.close()
"""


@pytest.fixture(scope="session")
def design_shots(site, browser_ready):
    """Screenshots of the design the way a user takes them: a card, an input box and the filters form
    (at 1x and as a 2x retina screenshot), and the whole page. {name: path}."""
    import subprocess

    out = tempfile.mkdtemp(prefix="lc-shots-")
    done = subprocess.run([sys.executable, "-c", _SHOTS_SCRIPT, site + "/design.html", out,
                           os.environ.get("LIVECODE_BROWSER_EXECUTABLE", "")], capture_output=True, text=True, timeout=120)
    if done.returncode:
        pytest.skip("could not take the design screenshots: " + (done.stderr or done.stdout)[-400:])
    return {name[:-4]: os.path.join(out, name) for name in os.listdir(out)}


@pytest.fixture(scope="session")
def app_client():
    """The real LiveCode Flask app (every route registered), driven through Flask's test client."""
    import importlib
    server = importlib.import_module("livecode.server")
    return server.app.test_client()
