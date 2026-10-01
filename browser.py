from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from typing import Any, Callable, Iterator
from urllib.parse import quote_plus, urlparse

from livecode import browser_snapshot, browser_stream

# "fit" (the default): 1280x800 until the Browser tab sizes it to the pane. "desktop": 1920x1080, fixed.
VIEWPORT = {"width": 1280, "height": 800}
DESKTOP_VIEWPORT = {"width": 1920, "height": 1080}
MIN_VIEWPORT = (320, 320)
MAX_VIEWPORT = (2560, 1600)
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
CLICK_TIMEOUT_MS = 6_000
CALL_TIMEOUT_S = 75.0
SETTLE_TIMEOUT_MS = 80
POST_NAVIGATE_LOAD_TIMEOUT_MS = 6000
MAX_TABS = 12
MAX_SHOTS_KEPT = 60
FRAME_MIN_INTERVAL_S = 0.12
FRAME_JPEG_QUALITY = 85
STANDARD_FRAME_JPEG_QUALITY = 70
SNAPSHOT_MAX_ELEMENTS = 250
SNAPSHOT_MAX_CHARS = 12_000
SCRIPT_RESULT_MAX_CHARS = 20_000
INSTALL_HINT = "pip install playwright && python -m playwright install chromium"
LAUNCH_ARGS = ["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled", "--disable-smooth-scrolling"]

AGENT_ACTIONS = (
    "navigate", "snapshot", "screenshot", "crop", "compare", "inspect", "resize", "click", "type", "press",
    "scroll", "wait", "javascript_exec", "back", "forward", "reload", "tabs", "new_tab", "switch_tab", "close_tab",
    "hover", "drag", "upload", "select", "check", "dialog", "find", "zoom", "console", "network", "downloads", "batch",
    "figma",
)
INTERACTIVE_ACTIONS = frozenset({"click", "type", "press", "javascript_exec", "drag", "upload", "select", "check", "dialog"})
MAX_BATCH = 8

DEVICE_PRESETS: dict[str, tuple[int, int]] = {
    "desktop-hd": (1920, 1080),
    "desktop": (1440, 900),
    "laptop": (1280, 800),
    "tablet-landscape": (1024, 768),
    "ipad-air": (820, 1180),
    "tablet": (768, 1024),
    "android": (412, 915),
    "mobile": (390, 844),
    "iphone-se": (375, 667),
}

_ALLOWED_SCHEMES = {"http", "https", "about"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"}
_SHOT_ID = re.compile(r"^[a-f0-9]{16}$")
_STORAGE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


class BrowserUnavailable(RuntimeError):
    pass


class BrowserError(RuntimeError):
    pass


class BrowserStuck(BrowserError):
    pass


def playwright_installed() -> bool:
    try:
        import playwright.sync_api
    except Exception:
        return False
    return True


MAX_RUNNING_JOBS = 8
IDLE_PUMP_MS = 20


class _Job:
    __slots__ = ("fn", "args", "kwargs", "future", "label", "priority", "glet", "wake", "context")

    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict, future: Future, label: str, priority: bool) -> None:
        self.fn, self.args, self.kwargs, self.future = fn, args, kwargs, future
        self.label, self.priority = label, priority
        self.glet: Any = None
        self.wake = 0.0
        self.context: dict[str, Any] = {}


class _Worker:

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._input: "deque[_Job]" = deque()
        self._jobs: "deque[_Job]" = deque()
        self._sleeping: list[_Job] = []
        self._running: list[_Job] = []
        self._current: _Job | None = None
        self._main: Any = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self.playwright: Any = None
        self.browser: Any = None
        self.remote = False
        self.endpoint = ""
        self.version = ""
        self.local: Any = None
        self.busy = ""
        self.sessions = 0
        self.streams: "set[browser_stream.Stream]" = set()
        self.name = "livecode-browser"

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float = CALL_TIMEOUT_S, label: str = "",
             priority: bool = False, **kwargs: Any) -> Any:
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        self._ensure_thread()
        future: Future = Future()
        with self._cv:
            (self._input if priority else self._jobs).append(_Job(fn, args, kwargs, future, label, priority))
            self._cv.notify()
        return future.result(timeout=timeout)

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        _CURRENT.worker = self
        try:
            from greenlet import getcurrent, greenlet
        except ImportError:
            while True:
                job = self._take(None)
                if job is not None:
                    _ACTIVE.__dict__.clear()
                    self._body(job)
                    if job in self._running:
                        self._running.remove(job)
        self._main = getcurrent()
        while True:
            job = self._next_runnable()
            if job.glet is None:
                job.glet = greenlet(lambda job=job: self._body(job), parent=self._main)
            self._switch_to(job)

    def _body(self, job: _Job) -> None:
        if not job.future.set_running_or_notify_cancel():
            return
        self._running.append(job)
        try:
            job.future.set_result(job.fn(*job.args, **job.kwargs))
        except BaseException as exc:
            job.future.set_exception(exc)

    def _switch_to(self, job: _Job) -> None:
        _ACTIVE.__dict__.clear()
        _ACTIVE.__dict__.update(job.context)
        self._current = job
        if job.label:
            self.busy = job.label
        try:
            job.glet.switch()
        finally:
            self._current = None
            job.context = dict(_ACTIVE.__dict__)
            _ACTIVE.__dict__.clear()
            if job.glet.dead:
                if job in self._running:
                    self._running.remove(job)
                self.busy = next((other.label for other in reversed(self._running) if other.label), "")

    def _next_runnable(self) -> _Job:
        while True:
            if self._input:
                with self._cv:
                    if self._input:
                        return self._input.popleft()
            now = time.monotonic()
            if self._sleeping:
                soonest = min(self._sleeping, key=lambda job: job.wake)
                if soonest.wake <= now:
                    self._sleeping.remove(soonest)
                    return soonest
            agents = sum(1 for job in self._running if not job.priority) < MAX_RUNNING_JOBS
            wait: float | None = None
            page = None
            if self.streams:
                wait = 0.0
            elif self._sleeping:
                wait = max(0.0, min(job.wake for job in self._sleeping) - now)
                page = self._sleeper_page() if wait else None
            new = self._take(0.0 if page is not None else wait, agents=agents)
            if new is not None:
                return new
            if self.streams:
                self._pump()
            elif page is not None:
                try:
                    page.wait_for_timeout(max(1.0, min(wait * 1000.0, IDLE_PUMP_MS)))
                except Exception:
                    new = self._take(min(wait, IDLE_PUMP_MS / 1000.0), agents=agents)
                    if new is not None:
                        return new

    def _sleeper_page(self) -> Any:
        for job in self._sleeping:
            session = job.context.get("session")
            try:
                pages = [job.context.get("page"), *(list(session.tabs.values()) if session is not None else [])]
            except Exception:
                pages = [job.context.get("page")]
            for page in pages:
                try:
                    if page is not None and not page.is_closed():
                        return page
                except Exception:
                    pass
        return None

    def _take(self, timeout: float | None, agents: bool = True) -> _Job | None:
        with self._cv:
            if not self._input and not (agents and self._jobs) and timeout != 0:
                self._cv.wait(timeout)
            if self._input:
                return self._input.popleft()
            if agents and self._jobs:
                return self._jobs.popleft()
            return None

    def pause(self, ms: float) -> bool:
        job = self._current
        if job is None:
            return False
        from greenlet import getcurrent

        if job.glet is not getcurrent():
            return False
        job.wake = time.monotonic() + max(0.0, ms) / 1000.0
        self._sleeping.append(job)
        self._main.switch()
        return True

    def _service_streams(self) -> None:
        for stream in list(self.streams):
            try:
                alive = stream.service()
            except Exception:
                alive = True
            if not alive:
                self.streams.discard(stream)

    def _pump(self) -> None:
        self._service_streams()
        for stream in list(self.streams):
            if stream.page is None:
                continue
            try:
                stream.page.wait_for_timeout(browser_stream.PUMP_MS)
                return
            except Exception:
                stream.drop_page()
        time.sleep(0.02)

    def _playwright(self) -> Any:
        if self.playwright is None:
            try:
                from playwright.sync_api import sync_playwright
            except Exception as exc:
                raise BrowserUnavailable(f"The built-in browser needs Playwright on the LiveCode server: {INSTALL_HINT}") from exc
            self.playwright = sync_playwright().start()
        return self.playwright

    def chromium(self) -> Any:
        endpoint = cdp_endpoint()
        if self.browser is not None and self.browser.is_connected() and endpoint == self.endpoint:
            return self.browser
        if self.browser is not None and endpoint != self.endpoint:
            _reset_engine(self)
        if endpoint:
            try:
                self.browser = self._playwright().chromium.connect_over_cdp(endpoint, timeout=10_000)
            except BrowserUnavailable:
                raise
            except Exception as exc:
                raise BrowserUnavailable(
                    f"Could not attach to the Chrome at {endpoint} ({_first_line(exc)}). Start it with "
                    "--remote-debugging-port=9222 --user-data-dir=<a profile folder>, or disconnect it in "
                    "Settings > Agent > Browser to use the built-in browser."
                ) from exc
            self.remote = True
            try:
                self.version = str(self.browser.version or "")
            except Exception:
                self.version = ""
        else:
            self.browser = self._launch()
            self.remote = False
            self.version = ""
        self.endpoint = endpoint
        return self.browser

    def local_browser(self) -> Any:
        if not self.remote:
            return self.chromium()
        if self.local is None or not self.local.is_connected():
            self.local = self._launch()
        return self.local

    def _launch(self) -> Any:
        playwright = self._playwright()
        args = [*LAUNCH_ARGS, f"--force-device-scale-factor={_device_scale_factor():g}"]
        launch = launch_settings()
        options: dict[str, Any] = {"headless": launch["headless"], "args": args, "ignore_default_args": ["--enable-automation"]}
        executable = launch["executable_path"]
        if executable:
            options["executable_path"] = executable
        proxy = launch["proxy"]
        if proxy:
            options["proxy"] = {"server": proxy}
        channel = os.environ.get("LIVECODE_BROWSER_CHANNEL", "").strip()
        if channel and not executable:
            try:
                return playwright.chromium.launch(channel=channel, **options)
            except Exception:
                pass
        if not executable:
            try:
                return playwright.chromium.launch(channel="chromium", **options)
            except Exception:
                pass
        try:
            return playwright.chromium.launch(**options)
        except Exception as exc:
            if executable:
                raise BrowserUnavailable(f"Could not start the browser at {executable} ({_first_line(exc)}). Check the path in Settings > Browser.") from exc
            try:
                return playwright.chromium.launch(channel="chrome", **options)
            except Exception:
                raise BrowserUnavailable(
                    f"Could not start Chromium ({_first_line(exc)}). Install it with "
                    "`python -m playwright install chromium`, or set LIVECODE_BROWSER_EXECUTABLE."
                ) from exc


_CURRENT = threading.local()
MAX_LANES = max(1, int(os.environ.get("LIVECODE_BROWSER_LANES", "3") or 3))
_shared_worker = _Worker()
_shared_worker.name = "livecode-browser-shared"
_lanes: list[_Worker] = []
_lanes_lock = threading.Lock()


def _pick_lane() -> _Worker:
    with _lanes_lock:
        idle = min(_lanes, key=lambda lane: lane.sessions) if _lanes else None
        if idle is None or (idle.sessions >= 1 and len(_lanes) < MAX_LANES):
            idle = _Worker()
            idle.name = f"livecode-browser-{len(_lanes) + 1}"
            _lanes.append(idle)
        idle.sessions += 1
        return idle


def _all_workers() -> list[_Worker]:
    return [_shared_worker, *_lanes]


class _WorkerProxy:
    def _current(self) -> _Worker:
        return getattr(_CURRENT, "worker", None) or _shared_worker

    def __getattr__(self, name: str) -> Any:
        return getattr(self._current(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._current(), name, value)

    def call(self, fn: Callable[..., Any], *args: Any, session: Any = None, **kwargs: Any) -> Any:
        worker = session.worker if session is not None else self._current()
        return worker.call(fn, *args, **kwargs)


_worker = _WorkerProxy()


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else exc.__class__.__name__)[:200]


CONFIG_PATH = os.path.expanduser("~/.livecode/browser.json")
_CDP_URL = re.compile(r"^(https?|wss?)://[\w.\-\[\]:]+(:\d{1,5})?(/[\w./\-%]*)?$", re.I)


def _saved_config() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cdp_endpoint() -> str:
    return (os.environ.get("LIVECODE_BROWSER_CDP_URL", "").strip() or str(_saved_config().get("cdp_url") or "").strip())


def connection_status() -> dict[str, Any]:
    endpoint = cdp_endpoint()
    out: dict[str, Any] = {"engine": "chrome" if endpoint else "builtin", "endpoint": endpoint}
    if os.environ.get("LIVECODE_BROWSER_CDP_URL", "").strip():
        out["source"] = "env"
    elif endpoint:
        out["source"] = "settings"
    shared = _shared_worker
    if endpoint and shared.remote and shared.endpoint == endpoint and shared.browser is not None:
        out["connected"] = True
        if shared.version:
            out["version"] = shared.version
    return out


def set_cdp_endpoint(url: str) -> dict[str, Any]:
    url = str(url or "").strip().rstrip("/")
    if url and not _CDP_URL.match(url):
        raise BrowserError("Enter Chrome's debugging address, e.g. http://127.0.0.1:9222.")
    if os.environ.get("LIVECODE_BROWSER_CDP_URL", "").strip():
        raise BrowserError("LIVECODE_BROWSER_CDP_URL is set on the LiveCode server; change it there.")
    previous = _saved_config()

    def _write(config: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        os.replace(tmp, CONFIG_PATH)

    def _reset_everywhere() -> None:
        for worker in _all_workers():
            if worker._thread is not None and worker._thread.is_alive():
                worker.call(_reset_engine, worker, label="connect", timeout=40)

    _reset_everywhere()
    _write({**previous, "cdp_url": url})
    if url:
        try:
            _shared_worker.call(_shared_worker.chromium, label="connect", timeout=40)
        except BrowserUnavailable:
            _write(previous)
            _reset_everywhere()
            raise
    return connection_status()


def _reset_engine(worker: "_Worker | None" = None) -> None:
    worker = worker or _worker._current()
    for session in list(_sessions.values()):
        if session.owner is not worker and session.owner is not None:
            continue
        if session.context is not None:
            if session.shared:
                for page in list(session.tabs.values()):
                    try:
                        page.close()
                    except Exception:
                        pass
                if session.page_handler is not None:
                    try:
                        session.context.remove_listener("page", session.page_handler)
                    except Exception:
                        pass
            else:
                _save_storage(session, force=True)
                try:
                    session.context.close()
                except Exception:
                    pass
        session.context = None
        session.page_handler = None
        session.shared = False
        session.tabs.clear()
        session.active = ""
        session.touch()
    if worker.browser is not None:
        try:
            worker.browser.close()
        except Exception:
            pass
    worker.browser = None
    worker.remote = False
    worker.endpoint = ""
    worker.version = ""


class _Agent:

    def __init__(self, key: str, label: str = "", sub: bool = False) -> None:
        self.key = key
        self.label = label
        self.sub = sub
        self.tab = ""
        self.ref_tab = ""
        self.seen: frozenset[str] | None = None
        self.notes: list[str] = []
        self.fp = ""
        self.no_progress = 0
        self.fail_streak = 0
        self.scroll_stuck = 0
        self.scroll_streak = 0
        self.console_seen = 0
        self.downloads_seen = 0
        self.done = False
        self.design_rounds: dict[str, dict[str, str]] = {}


MAX_AGENTS_KEPT = 64


class _Session:

    def __init__(self, key: str, storage_dir: str) -> None:
        self.key = key
        self.storage_dir = storage_dir
        self.context: Any = None
        self.tabs: dict[str, Any] = {}
        self.active = ""
        self.next_tab = 1
        self.seq = 0
        self.frame: tuple[int, bytes, float] | None = None
        self.last_dialog = ""
        self.last_saved = 0.0
        self.last_state: dict[str, Any] = {}
        self.viewport_default = default_viewport()
        self.viewport = _default_viewport_size(self.viewport_default)
        self.viewport_mode = "fit" if self.viewport_default == "fit" else "fixed"
        self.shared = False
        self.page_handler: Callable[[Any], None] | None = None
        self.links: dict[str, str] = {}
        self.tab_info: dict[str, dict[str, Any]] = {}
        self.console_log: "deque[dict[str, Any]]" = deque(maxlen=300)
        self.network_log: "deque[dict[str, Any]]" = deque(maxlen=400)
        self.downloads: list[dict[str, Any]] = []
        self.history: "deque[dict[str, Any]]" = deque(maxlen=200)
        self.dialog_plan: dict[str, Any] | None = None
        self.dialog_log: "deque[str]" = deque(maxlen=20)
        self.zoom = 1.0
        self.console_n = 0
        self.user = _Agent("user")
        self.agents: dict[str, _Agent] = {}
        self.creating = False
        self._lane: _Worker | None = None
        self.owner: _Worker | None = None
        self.stream: "browser_stream.Stream | None" = None
        self.view_box: tuple[int, int] | None = None
        self.input = _Input()
        self.last_input: tuple[str, float, str] = ("", 0.0, "")
        self.wheel_at: tuple[float, float] | None = None

    @property
    def view_sharp(self) -> bool:
        return view_quality() == "sharp"

    @property
    def worker(self) -> "_Worker":
        if cdp_endpoint():
            return _shared_worker
        if self._lane is None:
            self._lane = _pick_lane()
        return self._lane

    @property
    def downloads_dir(self) -> str:
        return os.path.join(self.storage_dir, "downloads")

    @property
    def state_file(self) -> str:
        return os.path.join(self.storage_dir, "storage_state.json")

    @property
    def shots_dir(self) -> str:
        return os.path.join(self.storage_dir, "shots")

    def touch(self) -> None:
        self.seq += 1
        self.frame = None


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def storage_dir_for(state_path: str) -> str:
    from livecode.project_store import project_file

    path = project_file(state_path, "browser")
    os.makedirs(path, exist_ok=True)
    return path


def storage_key_for(state_path: str) -> str:
    return os.path.basename(os.path.dirname(storage_dir_for(state_path)))


def _session(state_path: str) -> _Session:
    key = str(state_path or "").strip()
    if not key:
        raise BrowserError("Open a project to use the browser.")
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            session = _Session(key, storage_dir_for(key))
            _sessions[key] = session
        return session


def _context(session: _Session) -> Any:
    if session.context is not None:
        owner = getattr(session.context, "browser", None)
        if owner is not None and owner.is_connected() and owner is _worker.browser:
            return session.context
        session.context = None
        session.tabs.clear()
        session.tab_info.clear()
        session.active = ""
        session.touch()
    wanted = default_viewport()
    if wanted != session.viewport_default:
        session.viewport_default = wanted
        session.viewport = _default_viewport_size(wanted)
        session.viewport_mode = "fit" if wanted == "fit" else "fixed"
    browser = _worker.chromium()
    session.owner = _worker._current()
    session.shared = _worker.remote

    def _on_page(page: Any) -> None:
        if session.creating:
            return
        if session.shared and not _opened_by_session(session, page):
            return
        opener, causer = _popup_source(session, page)
        owner = causer if causer and causer != "user" else str((session.tab_info.get(opener) or {}).get("owner") or "")
        _follow_popup(session, _adopt_page(session, page, owner=owner, opener=opener), opener, causer)

    if _worker.remote:
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        context.on("page", _on_page)
        session.page_handler = _on_page
        session.context = context
        return context
    options: dict[str, Any] = {
        "viewport": dict(session.viewport),
        "device_scale_factor": _device_scale_factor(),
        "accept_downloads": True,
        "locale": "en-US",
    }
    user_agent = _user_agent(_worker.version or getattr(browser, "version", ""))
    if user_agent:
        options["user_agent"] = user_agent
    if os.path.isfile(session.state_file):
        options["storage_state"] = session.state_file
    try:
        context = browser.new_context(**options)
    except Exception:
        options.pop("storage_state", None)
        context = browser.new_context(**options)
    context.set_default_timeout(ACTION_TIMEOUT_MS)
    context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    if automation_signals_reduced():
        try:
            context.add_init_script(_NORMALIZE_JS)
        except Exception:
            pass
    context.on("page", _on_page)
    session.page_handler = _on_page
    session.context = context
    try:
        _apply_default_cookies(session, context)
    except Exception:
        pass
    return context


def _device_scale_factor() -> float:
    raw = os.environ.get("LIVECODE_BROWSER_SCALE", "").strip() or _read_browser_settings().get("device_scale", 2)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 2.0
    if not math.isfinite(value):
        value = 2.0
    return min(3.0, max(1.0, value))


def _user_agent(version: str) -> str:
    match = re.match(r"^(\d+)\.", str(version or "").strip())
    if not match:
        return ""
    if sys.platform == "darwin":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform.startswith("win"):
        platform = "Windows NT 10.0; Win64; x64"
    else:
        platform = "X11; Linux x86_64"
    return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{match.group(1)}.0.0.0 Safari/537.36"


def _opened_by_session(session: _Session, page: Any) -> bool:
    try:
        opener = page.opener()
    except Exception:
        return False
    return opener is not None and any(opener is known for known in session.tabs.values())


def _bring_to_front(page: Any) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass


POPUP_ATTRIBUTION_S = 3.0


def _popup_source(session: _Session, page: Any) -> tuple[str, str]:
    try:
        opener = page.opener()
    except Exception:
        opener = None
    opener_id = _tab_of(session, opener) if opener is not None else ""
    tab_id, at, key = session.last_input
    recent = tab_id in session.tabs and time.monotonic() - at < POPUP_ATTRIBUTION_S
    if not opener_id and recent:
        opener_id = tab_id
    return opener_id, key if recent and tab_id == opener_id else ""


def _follow_popup(session: _Session, tab_id: str, opener: str, causer: str) -> None:
    if not session.active or session.active not in session.tabs or (opener and session.active == opener):
        session.active = tab_id
    for agent in session.agents.values():
        if agent.key == causer and (not agent.tab or agent.tab == opener):
            agent.tab = tab_id
            agent.notes.append(f"The page opened a new tab {tab_id}" + (f" from {opener}" if opener else "")
                               + f"; your actions now go to {tab_id}" + (f" (switch_tab {opener} to go back)." if opener else "."))
        elif opener and agent.tab == opener:
            agent.notes.append(f"Tab {tab_id} opened from your tab {opener}; your actions still go to {opener}.")
    session.touch()


def _tab_of(session: _Session, page: Any) -> str:
    for tab_id, known in session.tabs.items():
        if known is page:
            return tab_id
    return ""


def _new_page(session: _Session, context: Any) -> Any:
    session.creating = True
    try:
        return context.new_page()
    finally:
        session.creating = False


def _adopt_page(session: _Session, page: Any, *, owner: str = "", opener: str = "") -> str:
    known_id = _tab_of(session, page)
    if known_id:
        return known_id
    tab_id = f"t{session.next_tab}"
    session.next_tab += 1
    session.tabs[tab_id] = page

    info = session.tab_info.setdefault(tab_id, {"loading": False, "status": None, "failed": "", "crashed": False, "favicon": "", "favicon_for": ""})
    info.update({"owner": owner, "opener": opener, "refs": {"frames": {}, "desc": {}}})

    def _on_dialog(dialog: Any) -> None:
        plan, session.dialog_plan = session.dialog_plan, None
        kind = dialog.type
        accept = bool(plan["accept"]) if plan is not None else kind in ("alert", "beforeunload")
        note = f"{kind} \"{dialog.message[:200]}\" was {'accepted' if accept else 'dismissed'}"
        session.last_dialog = f"{kind}: {dialog.message}"[:300] + (" (accepted)" if accept else " (dismissed)")
        session.dialog_log.append(note)
        try:
            if accept:
                dialog.accept(plan.get("text") if plan and plan.get("text") is not None else None) if kind == "prompt" else dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            pass

    def _on_close(_page: Any = None) -> None:
        if session.tabs.get(tab_id) is not page:
            return
        del session.tabs[tab_id]
        closed = session.tab_info.pop(tab_id, None) or {}
        back = str(closed.get("opener") or "")
        back = back if back in session.tabs else ""
        if session.active == tab_id:
            session.active = back or (next(reversed(session.tabs), "") if session.tabs else "")
        closing = _ACTIVE.get("agent") if _ACTIVE.get("closing") == tab_id else None
        for agent in session.agents.values():
            if agent.ref_tab == tab_id:
                agent.ref_tab = ""
            if agent.tab != tab_id:
                continue
            agent.tab = back
            if agent is not closing:
                agent.notes.append(f"Tab {tab_id} closed" + (f"; your actions go back to {back}." if back else "."))
        session.touch()

    def _is_main_navigation(request: Any) -> bool:
        try:
            return bool(request.is_navigation_request()) and request.frame == page.main_frame
        except Exception:
            return False

    def _on_request(request: Any) -> None:
        info["inflight"] = info.get("inflight", 0) + 1
        info["net_at"] = time.monotonic()
        if _is_main_navigation(request):
            info["nav_request"] = request
            info["loading"] = True
            info["failed"] = ""
            info["status"] = None
            info["favicon_for"] = ""
            session.touch()

    def _on_response(response: Any) -> None:
        try:
            request = response.request
            session.network_log.append({
                "t": round(time.time(), 1), "tab": tab_id, "method": request.method, "url": response.url[:300],
                "status": response.status, "type": request.resource_type,
            })
            if _is_main_navigation(request):
                info["status"] = response.status
        except Exception:
            pass

    def _on_request_done(_request: Any = None) -> None:
        info["inflight"] = max(0, info.get("inflight", 0) - 1)
        info["net_at"] = time.monotonic()

    def _on_request_failed(request: Any) -> None:
        _on_request_done()
        try:
            failure = str(request.failure or "failed")
            session.network_log.append({"t": round(time.time(), 1), "tab": tab_id, "method": request.method, "url": request.url[:300],
                                        "status": 0, "type": request.resource_type, "error": failure[:120]})
            if _is_main_navigation(request) and request is info.get("nav_request", request):
                info["failed"] = failure[:160]
                info["loading"] = False
                session.touch()
        except Exception:
            pass

    def _on_console(message: Any) -> None:
        try:
            location = message.location or {}
            session.console_n += 1
            session.console_log.append({
                "n": session.console_n, "t": round(time.time(), 1), "tab": tab_id, "level": message.type, "text": message.text[:500],
                "source": f"{str(location.get('url') or '')[:120]}:{location.get('lineNumber', '')}" if location.get("url") else "",
            })
        except Exception:
            pass

    def _on_page_error(error: Any) -> None:
        session.console_n += 1
        session.console_log.append({"n": session.console_n, "t": round(time.time(), 1), "tab": tab_id, "level": "pageerror",
                                    "text": str(error)[:500], "source": ""})

    def _on_navigated(frame: Any) -> None:
        try:
            if frame != page.main_frame:
                return
            url = frame.url
            if not url.startswith("chrome-error:"):
                info["commits"] = info.get("commits", 0) + 1
            info["refs"]["frames"].clear()
            info["refs"]["desc"].clear()
            if url and url != "about:blank" and (not session.history or session.history[-1].get("url") != url):
                session.history.append({"t": round(time.time(), 1), "tab": tab_id, "url": url[:500], "title": ""})
        except Exception:
            pass

    def _on_loaded(_page: Any = None) -> None:
        info["loading"] = False
        if session.history and session.history[-1].get("url") == page.url:
            session.history[-1]["title"] = _title(page)[:200]
        if abs(session.zoom - 1.0) > 0.001:
            _apply_zoom(page, session.zoom)
        session.touch()

    def _on_crash(_page: Any = None) -> None:
        info["crashed"] = True
        info["loading"] = False
        session.touch()

    def _on_download(download: Any) -> None:
        try:
            os.makedirs(session.downloads_dir, exist_ok=True)
            name = re.sub(r"[^\w.\- ]+", "_", download.suggested_filename or "download")[:120] or "download"
            dest = os.path.join(session.downloads_dir, name)
            stem, ext = os.path.splitext(name)
            n = 1
            while os.path.exists(dest):
                n += 1
                dest = os.path.join(session.downloads_dir, f"{stem} ({n}){ext}")
            download.save_as(dest)
            session.downloads.append({"t": round(time.time(), 1), "tab": tab_id, "name": os.path.basename(dest), "url": download.url[:300],
                                      "size": os.path.getsize(dest), "path": dest})
            session.touch()
        except Exception as exc:
            session.downloads.append({"t": round(time.time(), 1), "tab": tab_id, "name": download.suggested_filename or "download",
                                      "url": download.url[:300], "error": _first_line(exc)})

    page.on("dialog", _on_dialog)
    page.on("close", _on_close)
    page.on("request", _on_request)
    page.on("response", _on_response)
    page.on("requestfinished", _on_request_done)
    page.on("requestfailed", _on_request_failed)
    page.on("console", _on_console)
    page.on("pageerror", _on_page_error)
    page.on("framenavigated", _on_navigated)
    page.on("domcontentloaded", lambda _p=None: session.touch())
    page.on("load", _on_loaded)
    page.on("crash", _on_crash)
    page.on("download", _on_download)
    if session.viewport != VIEWPORT or session.shared:
        try:
            page.set_viewport_size(dict(session.viewport))
        except Exception:
            pass
    session.touch()
    return tab_id


def _page(session: _Session, tab_id: str = "") -> Any:
    context = _context(session)
    if tab_id:
        page = session.tabs.get(tab_id)
        if page is None or page.is_closed():
            raise BrowserError(f"No tab {tab_id}. Open tabs: {', '.join(session.tabs) or 'none'}.")
        return page
    page = session.tabs.get(session.active)
    if page is not None and not page.is_closed():
        return page
    for other_id, other in reversed(list(session.tabs.items())):
        if not other.is_closed():
            session.active = other_id
            return other
    page = _new_page(session, context)
    session.active = _adopt_page(session, page)
    return page


def _title(page: Any) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def _save_storage(session: _Session, force: bool = False) -> None:
    if session.context is None or session.shared:
        return
    now = time.time()
    if not force and now - session.last_saved < 3.0:
        return
    try:
        try:
            session.context.storage_state(path=session.state_file, indexed_db=True)
        except TypeError:
            session.context.storage_state(path=session.state_file)
        session.last_saved = now
    except Exception:
        pass


_FAVICON_JS = """
(() => {
  const links = Array.from(document.querySelectorAll('link[rel~="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"]'))
    .filter((l) => l.href && !l.href.startsWith("data:") || (l.href || "").length < 4000);
  const size = (l) => parseInt((l.getAttribute("sizes") || "0").split("x")[0], 10) || 0;
  links.sort((a, b) => Math.abs(size(a) - 32) - Math.abs(size(b) - 32));
  if (links.length) return links[0].href;
  return location.protocol.startsWith("http") ? location.origin + "/favicon.ico" : "";
})()
"""


def _favicon(session: _Session, tab_id: str, page: Any) -> str:
    info = session.tab_info.setdefault(tab_id, {"loading": False, "status": None, "failed": "", "crashed": False, "favicon": "", "favicon_for": ""})
    try:
        url = page.url
    except Exception:
        return info.get("favicon") or ""
    if info.get("favicon_for") == url or info.get("loading") or not url.startswith(("http://", "https://")):
        return info.get("favicon") or ""
    try:
        info["favicon"] = page.evaluate(_FAVICON_JS) or ""
    except Exception:
        info["favicon"] = ""
    info["favicon_for"] = url
    return info["favicon"]


def _apply_zoom(page: Any, level: float) -> None:
    try:
        page.evaluate("(z) => { document.documentElement.style.zoom = z === 1 ? '' : String(z); }", level)
    except Exception:
        pass


def _state(session: _Session) -> dict[str, Any]:
    tabs = []
    for tab_id, page in list(session.tabs.items()):
        _yield_to_input()
        if page.is_closed():
            continue
        info = session.tab_info.get(tab_id) or {}
        entry = {
            "id": tab_id, "url": page.url, "title": _title(page), "loading": bool(info.get("loading")),
            "status": info.get("status"), "failed": info.get("failed") or "", "crashed": bool(info.get("crashed")),
            "favicon": _favicon(session, tab_id, page),
        }
        owner = session.agents.get(str(info.get("owner") or ""))
        if owner is not None and owner.sub:
            entry["agent"] = owner.label or "Subagent"
            entry["agent_done"] = owner.done
        tabs.append(entry)
    active = session.tabs.get(session.active)
    state = {
        "available": True,
        "running": session.context is not None,
        "tabs": tabs,
        "active": session.active if active is not None else "",
        "url": active.url if active is not None and not active.is_closed() else "",
        "title": _title(active) if active is not None and not active.is_closed() else "",
        "seq": session.seq,
        "viewport": dict(session.viewport),
        "viewport_mode": session.viewport_mode,
        "dialog": session.last_dialog,
        "zoom": session.zoom,
        "engine": "chrome" if session.shared else "builtin",
        "platform": sys.platform,
    }
    session.last_state = state
    return state


def normalize_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise BrowserError("Enter a URL to open.")
    if text.lower() == "about:blank":
        return "about:blank"
    host_port = re.match(r"^(localhost|\[[0-9a-fA-F:]+\]|[\w.-]+):(\d{1,5})(?=$|[/?#])", text)
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", text)
    if scheme and not host_port:
        if scheme.group(1).lower() not in _ALLOWED_SCHEMES:
            raise BrowserError(f"{scheme.group(1)}: addresses can't be opened in the built-in browser.")
        return text
    if " " in text:
        return "https://www.google.com/search?q=" + quote_plus(text)
    host = re.split(r"[/:?#]", text, maxsplit=1)[0].lower()
    if _is_local_host(host):
        return "http://" + text
    if "." not in host:
        return "https://www.google.com/search?q=" + quote_plus(text)
    return "https://" + text


def _is_local_host(host: str) -> bool:
    host = (host or "").lower().strip("[]")
    if host in _LOCAL_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        return True
    return bool(re.match(r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host))


def _short_url(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.netloc:
        return url or ""
    path = parsed.path if parsed.path not in ("", "/") else ""
    return (parsed.netloc + path)[:120]


EMPTY_PAGE_WAIT_S = 10
NETWORK_IDLE_S = 0.5


def _network_idle(session: "_Session", page: Any) -> bool:
    info = session.tab_info.get(_tab_of(session, page)) or {}
    return not info.get("inflight") and time.monotonic() - float(info.get("net_at") or 0.0) >= NETWORK_IDLE_S


_SPARSE_JS = """
(() => {
  // A blank tab, an image, a PDF or plain text stays as it is: nothing is still to render.
  if (location.href === "about:blank" || !/html/i.test(document.contentType || "")) return false;
  const text = document.body ? document.body.innerText.trim().length : 0;
  const controls = document.querySelectorAll("a[href], button, input, textarea, select, [role=button]").length;
  return document.readyState !== "complete" ? text < 80 && controls < 4 : text < 40 && controls < 2;
})()
"""


def _take_snapshot(session: "_Session", page: Any, *, view_only: bool = False, query: str = "", selector: str = "",
                   max_chars: int = 0) -> tuple[str, int]:
    waited = 0
    if not view_only and not selector:
        last = ""
        while waited < EMPTY_PAGE_WAIT_S:
            try:
                sparse = page.evaluate(_SPARSE_JS)
            except Exception:
                break
            if not sparse:
                break
            signature = _page_signature(page)
            if last and signature == last and _network_idle(session, page):
                break
            last = signature
            _pause(page, 1000)
            waited += 1
    _yield_to_input()
    data = browser_snapshot.collect(_refs(session, page), page, max_elements=SNAPSHOT_MAX_ELEMENTS, view_only=view_only, query=query, selector=selector)
    _agent().ref_tab = _tab_of(session, page)
    if data.get("error"):
        raise BrowserError(str(data["error"]))
    if waited:
        data["waited"] = waited
    count = len(data.get("elements") or []) + sum(len(f.get("elements") or []) for f in data.get("frames") or [])
    limit = max_chars or (VIEW_SNAPSHOT_MAX_CHARS if view_only else SNAPSHOT_MAX_CHARS)
    return browser_snapshot.format_snapshot(data, max(1500, min(limit, 40_000))), count


class _ThreadState(threading.local):
    def get(self, key: str, default: Any = None) -> Any:
        return self.__dict__.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        self.__dict__[key] = value


_ACTIVE = _ThreadState()


def _agent() -> _Agent:
    agent = _ACTIVE.get("agent")
    if agent is not None:
        return agent
    session = _ACTIVE.get("session")
    return session.user if session is not None else _Agent("user")


def _refs(session: _Session | None, page: Any) -> dict[str, dict[int, Any]]:
    info = session.tab_info.get(_tab_of(session, page)) if session is not None else None
    if info is None:
        return {"frames": {}, "desc": {}}
    return info.setdefault("refs", {"frames": {}, "desc": {}})

_ARIA_ROLES = {
    "link": "link", "button": "button", "textbox": "textbox", "search": "searchbox", "checkbox": "checkbox", "radio": "radio",
    "select": "combobox", "combobox": "combobox", "tab": "tab", "menuitem": "menuitem", "option": "option", "switch": "switch",
    "submit": "button", "reset": "button", "slider": "slider", "range": "slider",
}


def _relocate_ref(session: _Session | None, page: Any, ref_num: int) -> Any:
    refs = _refs(session, page)
    desc = refs["desc"].get(ref_num) or {}
    if not desc:
        return None
    frame = refs["frames"].get(ref_num)
    scope = frame if frame is not None else page
    candidates = []
    if desc.get("href"):
        candidates.append(scope.locator(f'a[href="{desc["href"]}"]'))
    role, name = _ARIA_ROLES.get(str(desc.get("role") or "")), str(desc.get("name") or "")
    if role and name:
        candidates.append(scope.get_by_role(role, name=name, exact=True))
    for candidate in candidates:
        try:
            if candidate.count() == 1:
                return candidate.first
        except Exception:
            continue
    return None


def _first_visible(locator: Any) -> Any:
    try:
        visible = locator.filter(visible=True)
        if visible.count():
            return visible.first
    except Exception:
        pass
    return locator.first


_FINAL_ACTION_RX = (r"^\s*(submit( application| order| form)?|send( message| now| invitation)?|place( your)? order|pay( now)?|"
                    r"confirm( order| purchase| payment| booking)?|buy now|purchase|post|publish|book now|complete (order|purchase)|apply now)\s*$")
_FINAL_ACTION_JS = """
(el, o) => {
  const rx = new RegExp(o.rx, "i");
  const nameOf = (b) => (b.getAttribute("aria-label") || b.innerText || b.value || "").replace(/\\s+/g, " ").trim();
  if (o.mode === "click") {
    const b = el.closest("button, [role=button], input[type=submit]");
    return b && rx.test(nameOf(b)) && (b.form || b.closest("form, dialog, [role=dialog]")) ? nameOf(b) : "";
  }
  const form = el.form || el.closest("form");
  if (!form) return "";
  for (const b of form.querySelectorAll("button, input[type=submit], [role=button]")) if (rx.test(nameOf(b))) return nameOf(b);
  return "";
}
"""
_FINAL_ACTION_ADVICE = (
    "It changes something outside the browser (it sends a message, submits an application or a form, posts, or spends money). "
    "Do not do it on your own: ask the user for a clear yes. If their request already clearly told you to do exactly this, "
    "repeat the same call with confirm: true."
)


def _guard_final_action(locator: Any, args: dict[str, Any], mode: str) -> None:
    if args.get("confirm") in (True, "true", "yes", 1):
        return
    try:
        name = locator.evaluate(_FINAL_ACTION_JS, {"rx": _FINAL_ACTION_RX, "mode": mode}, timeout=1500)
    except Exception:
        return
    if name:
        what = f'the "{name}" button' if mode == "click" else f'Enter here (it triggers the "{name}" button of this form)'
        raise BrowserError(f"Held back: this would press {what}. " + _FINAL_ACTION_ADVICE)


def _covered_by_own_label(locator: Any) -> bool:
    try:
        return bool(locator.evaluate(
            """(el) => {
              const r = el.getBoundingClientRect();
              const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
              if (!top) return false;
              if (top === el || el.contains(top) || top.contains(el)) return true;
              const label = top.closest("label");
              if (label && (label.control === el || label.contains(el))) return true;
              const parent = el.parentElement;
              return !!(parent && parent.contains(top) && label);
            }""", timeout=1500))
    except Exception:
        return False


def _click_locator(locator: Any, *, button: str, count: int, timeout_ms: int) -> str:
    quick = min(timeout_ms, 3000)
    try:
        locator.click(button=button, click_count=count, timeout=quick)
        return ""
    except Exception as exc:
        first = exc
        if "intercepts pointer events" in str(exc) and not _covered_by_own_label(locator):
            raise
    try:
        locator.scroll_into_view_if_needed(timeout=1500)
        locator.click(button=button, click_count=count, timeout=1500)
        return "scrolled it into view first"
    except Exception:
        pass
    try:
        locator.click(button=button, click_count=count, force=True, timeout=2000)
        return "forced the click (the page kept saying the element was not ready)"
    except Exception:
        pass
    if button == "left":
        try:
            locator.evaluate("(el) => el.click()", timeout=2000)
            return "clicked it through the page's own script (a normal click was blocked)"
        except Exception:
            pass
    raise first


def _locator(page: Any, args: dict[str, Any]) -> Any:
    ref = args.get("ref")
    if ref not in (None, ""):
        try:
            ref_num = int(ref)
        except (TypeError, ValueError):
            raise BrowserError(f"ref must be a number from the latest snapshot, not {ref!r}.")
        session = _ACTIVE.get("session")
        frame = _refs(session, page)["frames"].get(ref_num)
        scope = frame if frame is not None else page
        try:
            locator = scope.locator(f'[data-livecode-ref="{ref_num}"]')
            found = locator.count()
        except Exception:
            locator, found = None, 0
        if found:
            return locator.first
        moved = _relocate_ref(session, page, ref_num)
        if moved is not None:
            return moved
        raise BrowserError(f"No element [{ref_num}] on the page now. Take a new snapshot; refs change when the page does.")
    selector = str(args.get("selector") or "").strip()
    if selector:
        locator = page.locator(selector)
        if locator.count() == 0:
            raise BrowserError(f"Nothing on the page matches the selector {selector!r}.")
        return _first_visible(locator)
    text = str(args.get("text") or "").strip()
    if text:
        locator = page.get_by_text(text, exact=False)
        if locator.count() == 0:
            raise BrowserError(f"No element on the page shows the text {text!r}.")
        return _first_visible(locator)
    raise BrowserError("Say which element: pass ref (from a snapshot), selector, or text.")


_QUIET_START_JS = r"""
() => {
  const old = window.__livecodeQuiet;
  if (old) old.obs.disconnect();
  const state = { t: performance.now(), obs: null };
  state.obs = new MutationObserver(() => { state.t = performance.now(); });
  state.obs.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
  Object.defineProperty(window, "__livecodeQuiet", { value: state, configurable: true, enumerable: false, writable: true });
}
"""
_QUIET_POLL_JS = "() => { const s = window.__livecodeQuiet; return s ? performance.now() - s.t : -1; }"
_QUIET_STOP_JS = "() => { const s = window.__livecodeQuiet; if (s) { s.obs.disconnect(); delete window.__livecodeQuiet; } }"
QUIET_MS = 200
QUIET_POLL_MS = 50
SETTLE_QUIET_MAX_MS = 1500
SETTLE_NETWORK_MS = 2000
WAIT_CHUNK_MS = 200


def _pause(page: Any, ms: float) -> None:
    worker = getattr(_CURRENT, "worker", None)
    if worker is not None and threading.current_thread() is worker._thread and worker.pause(ms):
        return
    page.wait_for_timeout(ms)


def _yield_to_input() -> None:
    worker = getattr(_CURRENT, "worker", None)
    if worker is None or not worker._input or threading.current_thread() is not worker._thread:
        return
    job = worker._current
    if job is not None and not job.priority:
        worker.pause(0)


BACKGROUND_SLICE_MS = 20
_IMPL_NAMES = {"full_page": "fullPage", "omit_background": "omitBackground", "mask_color": "maskColor"}


def _in_background(target: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    worker = getattr(_CURRENT, "worker", None)
    job = worker._current if worker is not None and threading.current_thread() is worker._thread else None
    # Slicing leans on Playwright's private sync wrappers (_impl_obj, _loop, _sync); when a
    # Playwright version lacks any of them, or greenlet is missing, run the call inline instead.
    impl = getattr(target, "_impl_obj", None)
    loop = getattr(target, "_loop", None)
    impl_method = getattr(impl, method, None) if impl is not None else None
    if (job is None or job.priority or impl_method is None or loop is None or not callable(getattr(target, "_sync", None))
            or not hasattr(loop, "create_task")):
        return getattr(target, method)(*args, **kwargs)
    try:
        from playwright.sync_api._generated import mapping
    except ImportError:
        return getattr(target, method)(*args, **kwargs)
    if not callable(getattr(mapping, "from_maybe_impl", None)):
        return getattr(target, method)(*args, **kwargs)
    try:
        task = loop.create_task(impl_method(*args, **{_IMPL_NAMES.get(k, k): v for k, v in kwargs.items()}))
    except (AttributeError, TypeError, RuntimeError):
        return getattr(target, method)(*args, **kwargs)
    try:
        while not task.done():
            target._sync(asyncio.wait({task}, timeout=BACKGROUND_SLICE_MS / 1000.0))
            if not task.done():
                worker.pause(BACKGROUND_SLICE_MS)
    except BaseException:
        task.cancel()
        raise
    return mapping.from_maybe_impl(task.result())


def _wait_until(page: Any, attempt: Callable[[float], Any], ms: float) -> Any:
    deadline = time.monotonic() + ms / 1000.0
    while True:
        chunk = max(1.0, min(WAIT_CHUNK_MS, (deadline - time.monotonic()) * 1000.0))
        try:
            return attempt(chunk)
        except Exception as exc:
            if "Timeout" not in str(exc) or time.monotonic() >= deadline:
                raise
        _pause(page, 40)


def _wait_load(page: Any, state: str, ms: float) -> bool:
    try:
        _wait_until(page, lambda t: page.wait_for_load_state(state, timeout=t), ms)
        return True
    except Exception:
        return False


def _quiet(page: Any, max_ms: float) -> bool:
    page.evaluate(_QUIET_START_JS)
    deadline = time.monotonic() + max_ms / 1000.0
    try:
        while time.monotonic() < deadline:
            _pause(page, QUIET_POLL_MS)
            quiet = float(page.evaluate(_QUIET_POLL_JS))
            if quiet < 0:
                return False
            if quiet >= QUIET_MS:
                break
        return True
    finally:
        try:
            page.evaluate(_QUIET_STOP_JS)
        except Exception:
            pass


def _settle(page: Any, *, network: bool = False) -> None:
    for attempt in range(2):
        _wait_load(page, "domcontentloaded", 5000)
        try:
            if _quiet(page, SETTLE_QUIET_MAX_MS) or attempt:
                break
        except Exception:
            if attempt:
                break
    if network:
        _wait_load(page, "networkidle", SETTLE_NETWORK_MS)


_PAGE_SIGNATURE_JS = """
(() => {
  const text = document.body ? document.body.innerText : "";
  let hash = 5381;
  for (let i = 0; i < text.length && i < 60000; i++) hash = ((hash << 5) + hash + text.charCodeAt(i)) | 0;
  return [location.href, text.length, hash, document.getElementsByTagName("*").length,
    document.querySelectorAll("dialog[open], [role=dialog], [aria-modal=true]").length].join("|");
})()
"""


def _page_signature(page: Any) -> str:
    try:
        return str(page.evaluate(_PAGE_SIGNATURE_JS))
    except Exception:
        return ""


def _wait_for_change(page: Any, before: str, max_ms: int) -> bool:
    if not before:
        return False
    waited = 0
    while waited < max_ms:
        _pause(page, 250)
        waited += 250
        now = _page_signature(page)
        if now and now != before:
            try:
                _quiet(page, 900)
            except Exception:
                pass
            return True
    return False


def _timeout_ms(args: dict[str, Any], default: int) -> int:
    try:
        seconds = float(args.get("timeout"))
    except (TypeError, ValueError):
        return default
    return int(min(max(seconds, 1.0), 120.0) * 1000)


def _after_input(session: _Session, page: Any, before_url: str, result: dict[str, Any]) -> dict[str, Any]:
    before_tab = _tab_of(session, page) or str(_ACTIVE.get("tab") or "")
    _settle(page, network=not page.is_closed() and page.url != before_url)
    current_id = _current_tab_id(session, _agent())
    current = session.tabs.get(current_id)
    if current is not None and current is not page and not current.is_closed():
        _wait_load(current, "domcontentloaded", NAV_TIMEOUT_MS)
        if page.is_closed():
            result["closed_tab"] = before_tab
        else:
            result["opened_tab"] = current_id
        page = current
    _ACTIVE["page"] = page
    result["url"] = page.url
    result["title"] = _title(page)
    if page.url != before_url or result.get("opened_tab") or result.get("closed_tab"):
        result["navigated"] = True
        _flag_auth_wall(result, before_url, page.url)
    session.touch()
    return result


_TABLESS_ACTIONS = frozenset({"tabs", "dialog", "console", "network", "downloads", "resize", "figma"})


def _current_tab_id(session: _Session, agent: _Agent) -> str:
    if agent is not session.user and agent.tab in session.tabs:
        return agent.tab
    if agent.sub:
        return ""
    return session.active if session.active in session.tabs else ""


def _agent_tab(session: _Session, agent: _Agent, action: str, args: dict[str, Any]) -> str:
    if agent is session.user:
        return ""
    if args.get("ref") not in (None, "") and agent.ref_tab in session.tabs:
        return agent.ref_tab
    if agent.tab in session.tabs:
        return agent.tab
    agent.tab = ""
    if not agent.sub:
        agent.tab = session.active if session.active in session.tabs else ""
        return agent.tab
    if action in _TABLESS_ACTIONS:
        return ""
    agent.tab = _open_tab(session, agent)
    return agent.tab


def _make_room(session: _Session) -> None:
    open_ids = [tab_id for tab_id, page in session.tabs.items() if not page.is_closed()]
    if len(open_ids) < MAX_TABS:
        return
    for tab_id in open_ids:
        owner = session.agents.get(str((session.tab_info.get(tab_id) or {}).get("owner") or ""))
        if owner is not None and owner.done and tab_id != session.active:
            try:
                session.tabs[tab_id].close()
            except Exception:
                continue
            return
    raise BrowserError(f"At most {MAX_TABS} tabs; close one first.")


def _open_tab(session: _Session, agent: _Agent) -> str:
    context = _context(session)
    _make_room(session)
    tab_id = _adopt_page(session, _new_page(session, context), owner="" if agent is session.user else agent.key)
    if session.active not in session.tabs:
        session.active = tab_id
    return tab_id


def _do_new_tab(session: _Session, agent: _Agent, args: dict[str, Any]) -> dict[str, Any]:
    background = bool(args.get("background"))
    if background and not _current_tab_id(session, agent):
        blank = _open_tab(session, agent)
        if agent is not session.user:
            agent.tab = blank
    tab_id = _open_tab(session, agent)
    page = session.tabs[tab_id]
    if not background:
        if agent is not session.user:
            agent.tab = tab_id
        if not agent.sub:
            session.active = tab_id
    _ACTIVE["page"] = page
    result: dict[str, Any] = {"tab_id": tab_id}
    url = str(args.get("url") or "").strip()
    if url:
        session.links[tab_id] = url
        result.update(_do_navigate(session, page, url, _timeout_ms(args, NAV_TIMEOUT_MS)))
    if _worker.remote and session.active in session.tabs and session.active != tab_id:
        _bring_to_front(session.tabs[session.active])
    session.touch()
    return result


NAV_DOM_WAIT_MS = 15_000
GOTO_SLICE_MS = 150
_AUTH_WALL_ADVICE = (
    "The site sent the browser to a sign-in page: it is not signed in here. Never type credentials. Ask the user to sign in "
    "in the Browser tab (they can take control), or import cookies from their browser / attach their Chrome in Settings, "
    "then continue from where you were."
)


def _flag_auth_wall(out: dict[str, Any], before: str, after: str) -> None:
    def _key(url: str) -> str:
        parsed = urlparse(url)
        return parsed.path + "?" + parsed.query

    if _LOGIN_URL.search(_key(after)) and not _LOGIN_URL.search(_key(before)):
        out["auth_wall"] = True
        out["warning"] = ((out["warning"] + " ") if out.get("warning") else "") + _AUTH_WALL_ADVICE
_LOGIN_URL = re.compile(r"/(log-?in|sign-?in|authwall|uas/login|accounts/login|sso|signup)(/|\?|$)|[?&](session_redirect|redirect_uri)=", re.I)


def _stop_loading(page: Any) -> bool:
    try:
        cdp = page.context.new_cdp_session(page)
        try:
            cdp.send("Page.stopLoading")
        finally:
            cdp.detach()
        return True
    except Exception:
        return False


def _goto(session: _Session, page: Any, target: str, timeout_ms: int) -> int | None:
    info = session.tab_info.get(_tab_of(session, page))
    if info is None or timeout_ms <= GOTO_SLICE_MS:
        response = page.goto(target, wait_until="commit", timeout=timeout_ms)
        return response.status if response is not None else None
    commits = info.get("commits", 0)
    info["failed"] = ""
    try:
        response = page.goto(target, wait_until="commit", timeout=GOTO_SLICE_MS)
        return response.status if response is not None else None
    except Exception as exc:
        if "Timeout" not in str(exc):
            raise
    deadline = time.monotonic() + (timeout_ms - GOTO_SLICE_MS) / 1000.0
    while time.monotonic() < deadline:
        _pause(page, 40)
        if page.is_closed():
            raise BrowserError("The tab closed while it was loading.")
        if info.get("failed"):
            raise RuntimeError(f"{info['failed']} at {target}")
        if info.get("commits", 0) != commits:
            return info.get("status")
    raise RuntimeError(f"Timeout {timeout_ms}ms exceeded.")


def _do_navigate(session: _Session, page: Any, url: str, timeout_ms: int = NAV_TIMEOUT_MS) -> dict[str, Any]:
    target = normalize_url(url)
    status = None
    try:
        status = _goto(session, page, target, timeout_ms)
    except BrowserError:
        raise
    except Exception as exc:
        message = _first_line(exc)
        if "net::" in message or "Timeout" in message:
            parsed = urlparse(target)
            hint = ""
            if "Timeout" in message and _stop_loading(page):
                hint = " Loading was stopped, so the tab still shows the page it showed before."
            if "ERR_CONNECTION_REFUSED" in message and _is_local_host(parsed.hostname or ""):
                hint = (f" Nothing is listening on {parsed.netloc}: start the dev server first (run_command with "
                        "background: true, e.g. npm run dev), wait until it prints its URL, then open that URL.")
            raise BrowserError(f"Could not open {target}: {message}{hint}") from exc
        raise
    still_loading = not _wait_load(page, "domcontentloaded", min(timeout_ms, NAV_DOM_WAIT_MS))
    if not still_loading:
        _wait_load(page, "load", POST_NAVIGATE_LOAD_TIMEOUT_MS)
        _settle(page, network=True)
    session.touch()
    _save_storage(session)
    out: dict[str, Any] = {"url": page.url, "title": _title(page)}
    if still_loading:
        out["warning"] = (f"The page had not finished loading after {NAV_DOM_WAIT_MS // 1000}s (a heavy or slow site), so this is what has "
                          "rendered so far. Read it with snapshot and carry on: do not reload or navigate again unless it stays empty.")
    _flag_auth_wall(out, target, page.url)
    if status is not None:
        out["status"] = status
        if status in (403, 429, 503):
            out["warning"] = (f"The page answered HTTP {status}: the site may be refusing automated browsers or rate limiting. "
                              "Do not retry in a loop; check the snapshot, and if it is blocked tell the user (attaching their own Chrome in Settings usually helps).")
        elif status >= 400:
            out["warning"] = f"The page answered HTTP {status}."
    return out


_FIND_JS = r"""
(o) => {
  const css = document.getElementById("__livecode_find_style");
  if (!css) {
    const style = document.createElement("style");
    style.id = "__livecode_find_style";
    style.textContent = "::highlight(livecode-find){background:#ffe066;color:#000}::highlight(livecode-find-current){background:#ff9f1a;color:#000}";
    document.documentElement.appendChild(style);
  }
  if (window.CSS && CSS.highlights) { CSS.highlights.delete("livecode-find"); CSS.highlights.delete("livecode-find-current"); }
  if (o.clear || !o.text) return { count: 0, current: 0, matches: [] };
  const needle = o.text.toLowerCase();
  const ranges = [], matches = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, value = node.nodeValue, lower = value.toLowerCase();
    const parent = node.parentElement;
    if (!parent || !/\S/.test(value)) continue;
    let at = lower.indexOf(needle);
    if (at < 0) continue;
    const cs = getComputedStyle(parent);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    while (at >= 0 && ranges.length < 500) {
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      const r = range.getBoundingClientRect();
      if (r.width > 0 || r.height > 0) {
        ranges.push(range);
        const ref = (parent.closest("[data-livecode-ref]") || {}).getAttribute ? parent.closest("[data-livecode-ref]").getAttribute("data-livecode-ref") : "";
        matches.push({ text: value.slice(Math.max(0, at - 40), at + needle.length + 60).replace(/\s+/g, " ").trim(), y: Math.round(r.top + scrollY), ref: ref ? Number(ref) : 0 });
      }
      at = lower.indexOf(needle, at + needle.length);
    }
  }
  const count = ranges.length;
  const current = count ? ((o.index % count) + count) % count : 0;
  if (count && window.CSS && CSS.highlights) {
    CSS.highlights.set("livecode-find", new Highlight(...ranges));
    CSS.highlights.set("livecode-find-current", new Highlight(ranges[current]));
  }
  if (count) {
    const el = ranges[current].startContainer.parentElement;
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", inline: "nearest" });
  }
  return { count: count, current: current, matches: matches.slice(0, 12) };
}
"""


def _format_find(found: dict[str, Any], text: str) -> str:
    count = int(found.get("count") or 0)
    if not text:
        return "Cleared the highlights."
    if not count:
        return f"No visible text matches {text!r}."
    lines = [f"{count} match{'es' if count != 1 else ''} for {text!r}; showing match {int(found.get('current') or 0) + 1} (scrolled into view):"]
    for item in found.get("matches") or []:
        lines.append(f"- y {item.get('y')}" + (f" [{item['ref']}]" if item.get("ref") else "") + f": {item.get('text')}")
    return "\n".join(lines)


def _do_console(session: _Session, args: dict[str, Any]) -> dict[str, Any]:
    level = str(args.get("level") or "all").lower()
    limit = int(min(max(int(args.get("limit") or 30), 1), 200))
    entries = list(session.console_log)
    if level in ("error", "errors"):
        entries = [e for e in entries if e["level"] in ("error", "pageerror")]
    elif level in ("warning", "warn", "warnings"):
        entries = [e for e in entries if e["level"] in ("error", "pageerror", "warning")]
    if args.get("clear"):
        session.console_log.clear()
    shown = entries[-limit:]
    lines = [f"[{e['level']}] {e['text']}" + (f"  ({e['source']})" if e.get("source") else "") for e in shown]
    return {"console": shown, "count": len(entries), "result": "\n".join(lines) or "The console is empty."}


def _do_network(session: _Session, args: dict[str, Any]) -> dict[str, Any]:
    needle = str(args.get("filter") or args.get("url") or "").lower()
    status = str(args.get("status") or "").lower()
    kind = str(args.get("type") or "").lower()
    limit = int(min(max(int(args.get("limit") or 40), 1), 300))
    entries = list(session.network_log)
    if needle:
        entries = [e for e in entries if needle in e["url"].lower()]
    if kind:
        entries = [e for e in entries if e.get("type") == kind]
    if status in ("failed", "errors", "error"):
        entries = [e for e in entries if e["status"] == 0 or e["status"] >= 400]
    elif status.isdigit():
        entries = [e for e in entries if e["status"] == int(status)]
    if args.get("clear"):
        session.network_log.clear()
    shown = entries[-limit:]
    lines = [f"{e['status'] or 'FAILED'} {e['method']} {e.get('type', '')} {e['url']}" + (f"  ({e['error']})" if e.get("error") else "") for e in shown]
    return {"network": shown, "count": len(entries), "result": "\n".join(lines) or "No requests recorded yet."}


def _do_action(session: _Session, action: str, args: dict[str, Any], reference: "_Reference | None" = None) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    agent = _agent()
    explicit = str(args.get("tab_id") or "").strip()
    session.last_dialog = ""
    _ACTIVE["session"] = session
    if not args.get("live"):
        session.wheel_at = None
    if action != "scroll":
        agent.scroll_streak = 0
        agent.scroll_stuck = 0

    if action == "tabs":
        _context(session)
        return {}
    if action == "new_tab":
        _context(session)
        return _do_new_tab(session, agent, args)
    if action == "switch_tab":
        if not explicit:
            raise BrowserError("Pass tab_id: one of the ids the tabs action lists.")
        page = _page(session, explicit)
        if agent is not session.user:
            agent.tab = explicit
        if not agent.sub or session.active not in session.tabs:
            session.active = explicit
            _bring_to_front(page)
        _ACTIVE["page"] = page
        session.touch()
        return {}
    if action == "close_tab":
        target = explicit or _current_tab_id(session, agent)
        if not target and agent.sub:
            raise BrowserError("You have no tab of your own open; pass tab_id to close another one.")
        page = _page(session, target)
        closed = _tab_of(session, page)
        _ACTIVE["closing"] = closed
        page.close()
        session.touch()
        _save_storage(session)
        return {"closed_tab": closed}

    if action == "dialog":
        accept = args.get("accept", True) not in (False, "false", "no", 0)
        session.dialog_plan = {"accept": accept, "text": args.get("text")}
        return {"note": f"The next dialog the page opens will be {'accepted' if accept else 'dismissed'}"
                        + (f" with the text {args.get('text')!r}" if accept and args.get("text") is not None else "") + ". Now do the action that opens it."}
    if action == "console":
        return _do_console(session, args)
    if action == "network":
        return _do_network(session, args)
    if action == "downloads":
        rows = [{k: v for k, v in item.items() if k != "path"} for item in session.downloads[-30:]]
        lines = [f"{item['name']} ({item.get('size', '?')} bytes)" + (f" - failed: {item['error']}" if item.get("error") else "") for item in rows]
        return {"downloads": rows, "result": "\n".join(lines) or "No downloads yet. Files a page downloads are saved in the project's browser profile."}

    if action == "resize":
        return _do_resize(session, args)
    if action == "crop" and reference is not None:
        return _do_image_crop(session, reference, args)

    page = _page(session, explicit or _agent_tab(session, agent, action, args))
    tab_id = _tab_of(session, page)
    if agent is not session.user and not agent.tab and not agent.sub:
        agent.tab = tab_id
    _ACTIVE["page"] = page
    _ACTIVE["tab"] = tab_id
    if args.get("live") and action in _LIVE_ACTIONS:
        return _do_live(session, page, action, args)
    if _worker.remote and action in ("screenshot", "crop", "compare", "inspect"):
        _bring_to_front(page)
    if action in ("navigate", "back", "forward"):
        session.links.pop(tab_id, None)
    if action in ("click", "type", "press", "check", "select", "upload"):
        session.last_input = (tab_id, time.monotonic(), agent.key)
    if action == "navigate":
        return _do_navigate(session, page, str(args.get("url") or ""), _timeout_ms(args, NAV_TIMEOUT_MS))
    if action in ("back", "forward"):
        (page.go_back if action == "back" else page.go_forward)(wait_until="domcontentloaded")
        session.touch()
        return {"url": page.url, "title": _title(page)}
    if action == "reload":
        page.reload(wait_until="domcontentloaded")
        session.touch()
        return {"url": page.url, "title": _title(page)}
    if action == "snapshot":
        text, count = _take_snapshot(session, page, query=str(args.get("query") or ""), selector=str(args.get("selector") or ""),
                                     max_chars=int(args.get("max_chars") or 0))
        return {"url": page.url, "title": _title(page), "elements": count, "snapshot": text}
    if action == "hover":
        locator = _locator(page, args)
        target = _describe_locator(locator, args)
        locator.hover(timeout=_timeout_ms(args, CLICK_TIMEOUT_MS))
        return _after_input(session, page, page.url, {"hovered": target})
    if action == "drag":
        source = _locator(page, args)
        target = _describe_locator(source, args)
        timeout = _timeout_ms(args, ACTION_TIMEOUT_MS)
        if args.get("to_ref") not in (None, "") or str(args.get("to_selector") or "").strip():
            dest = _locator(page, {"ref": args.get("to_ref"), "selector": args.get("to_selector")})
            source.drag_to(dest, timeout=timeout)
            dragged = f"{target} -> {_describe_locator(dest, {})}"
        elif args.get("to_x") is not None and args.get("to_y") is not None:
            box = source.bounding_box()
            if box is None:
                raise BrowserError("The element to drag has no box on screen.")
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.mouse.move(float(args["to_x"]), float(args["to_y"]), steps=12)
            page.mouse.up()
            dragged = f"{target} -> ({int(float(args['to_x']))}, {int(float(args['to_y']))})"
        else:
            raise BrowserError("Say where to drag to: to_ref, to_selector, or to_x and to_y.")
        return _after_input(session, page, page.url, {"dragged": dragged})
    if action == "upload":
        raw = args.get("files") if args.get("files") is not None else args.get("path")
        names = [str(v) for v in (raw if isinstance(raw, (list, tuple)) else [raw]) if v not in (None, "")]
        if not names:
            raise BrowserError("Pass files: the project file path (or a list of them) to upload.")
        resolver = _ACTIVE.get("resolve_path")
        paths = []
        for name in names:
            resolved = resolver(name) if resolver else None
            if not resolved or not os.path.isfile(resolved):
                raise BrowserError(f"Cannot upload {name!r}: pass a file path inside the project.")
            paths.append(resolved)
        locator = _locator(page, args)
        target = _describe_locator(locator, args)
        is_input = bool(locator.evaluate("(el) => el.tagName === 'INPUT' && el.type === 'file'", timeout=2000))
        if is_input:
            locator.set_input_files(paths, timeout=_timeout_ms(args, ACTION_TIMEOUT_MS))
        else:
            with page.expect_file_chooser(timeout=_timeout_ms(args, ACTION_TIMEOUT_MS)) as chooser:
                locator.click()
            chooser.value.set_files(paths)
        return _after_input(session, page, page.url, {"uploaded": [os.path.basename(p) for p in paths], "typed_into": target})
    if action == "select":
        locator = _locator(page, args)
        target = _describe_locator(locator, args)
        wanted = str(args.get("value") if args.get("value") is not None else args.get("text") or "")
        if not wanted:
            raise BrowserError("Pass value: the option's text or value to choose.")
        chosen = None
        for how in ("label", "value"):
            try:
                locator.select_option(**{how: wanted}, timeout=3000)
                chosen = wanted
                break
            except Exception:
                continue
        if chosen is None:
            options = locator.evaluate("(el) => Array.from(el.options || []).map((o) => o.text.trim()).slice(0, 30)", timeout=2000) or []
            raise BrowserError(f"No option {wanted!r} in {target}." + (f" Options: {', '.join(options)}." if options else ""))
        return _after_input(session, page, page.url, {"selected": f"{chosen} in {target}"})
    if action == "check":
        locator = _locator(page, args)
        target = _describe_locator(locator, args)
        want = args.get("checked", True) not in (False, "false", "no", 0)
        (locator.check if want else locator.uncheck)(timeout=_timeout_ms(args, CLICK_TIMEOUT_MS))
        return _after_input(session, page, page.url, {"checked": f"{'checked' if want else 'unchecked'} {target}"})
    if action == "find":
        text = str(args.get("text") or args.get("query") or "").strip()
        if not text and not args.get("clear"):
            raise BrowserError("Pass text: what to find on the page.")
        found = page.evaluate(_FIND_JS, {"text": text, "index": int(args.get("index") or 0), "clear": bool(args.get("clear"))})
        session.touch()
        return {"find": found, "result": _format_find(found, text)}
    if action == "zoom":
        try:
            level = float(args.get("level") if args.get("level") is not None else args.get("zoom"))
        except (TypeError, ValueError):
            raise BrowserError("Pass level: a zoom such as 1 (100%), 1.5 or 150.")
        level = level / 100.0 if level > 5 else level
        session.zoom = min(3.0, max(0.25, level))
        for tab in list(session.tabs.values()):
            _apply_zoom(tab, session.zoom)
        session.touch()
        return {"zoom": session.zoom}
    if action == "click":
        before_url = page.url
        x, y = args.get("x"), args.get("y")
        button = str(args.get("button") or "left").lower()
        if button not in ("left", "right", "middle"):
            raise BrowserError("button is left, right or middle.")
        how = ""
        if x is not None and y is not None and args.get("ref") in (None, "") and not args.get("selector") and not args.get("text"):
            page.mouse.click(float(x), float(y), button=button, click_count=2 if args.get("double") else 1)
            target = f"({int(float(x))}, {int(float(y))})"
        else:
            locator = _locator(page, args)
            _guard_final_action(locator, args, "click")
            target = _describe_locator(locator, args)
            how = _click_locator(locator, button=button, count=2 if args.get("double") else 1, timeout_ms=_timeout_ms(args, CLICK_TIMEOUT_MS))
        result = _after_input(session, page, before_url, {"clicked": target, **({"note": f"The normal click did not work, so I {how}."} if how else {})})
        _save_storage(session)
        return result
    if action == "type":
        before_url = page.url
        text = str(args.get("text") if args.get("text") is not None else "")
        if args.get("ref") in (None, "") and not args.get("selector"):
            page.keyboard.type(text)
            target = "the focused element"
        else:
            locator = _locator(page, {"ref": args.get("ref"), "selector": args.get("selector")})
            target = _describe_locator(locator, args)
            _fill(page, locator, text)
        if args.get("submit") and (args.get("ref") not in (None, "") or args.get("selector")):
            _guard_final_action(locator, args, "type")
        search_like = False
        if args.get("ref") not in (None, "") or args.get("selector"):
            try:
                search_like = bool(locator.evaluate("(el) => el.type === 'search' || el.getAttribute('role') === 'searchbox' || el.getAttribute('role') === 'combobox' || /search/i.test((el.getAttribute('aria-label') || '') + (el.getAttribute('placeholder') || '') + (el.name || ''))", timeout=1500))
            except Exception:
                search_like = False
        signature = _page_signature(page) if (args.get("submit") or search_like) else ""
        if args.get("submit"):
            page.keyboard.press("Enter")
        reacted = _wait_for_change(page, signature, 4500 if args.get("submit") else 2500) if signature else False
        result = _after_input(session, page, before_url, {"typed_into": target, "submitted": bool(args.get("submit"))})
        if signature and not reacted:
            result["note"] = ("The page did not visibly react to the search within a few seconds. Results often load a moment later: "
                              "wait {change: true} then read it again, or take a screenshot, before concluding there are no matches.")
        _save_storage(session)
        return result
    if action == "press":
        key = str(args.get("key") or args.get("text") or "").strip()
        if not key:
            raise BrowserError("Pass key, e.g. Enter, Tab, Escape, ArrowDown or Control+A.")
        before_url = page.url
        signature = _page_signature(page) if key.lower() in ("enter", "return") else ""
        page.keyboard.press(key)
        if signature:
            _wait_for_change(page, signature, 3500)
        return _after_input(session, page, before_url, {"pressed": key})
    if action == "type_text":
        page.keyboard.type(str(args.get("text") or ""))
        session.touch()
        return {}
    if action == "scroll":
        return _do_scroll(session, page, args)
    if action == "wait" and args.get("change"):
        limit = int(min(max(float(args.get("seconds") or 8), 0.5), 30) * 1000)
        changed = _wait_for_change(page, _page_signature(page), limit)
        session.touch()
        return {"url": page.url, "title": _title(page), "waited_s": limit / 1000, "changed": changed,
                **({} if changed else {"note": "The page did not change in that time."})}
    if action == "wait":
        text = str(args.get("text") or "").strip()
        selector = str(args.get("selector") or "").strip()
        seconds = min(max(float(args.get("seconds") or (10 if (text or selector) else 1)), 0.1), 30.0)
        try:
            if selector:
                _wait_until(page, lambda t: page.wait_for_selector(selector, timeout=t), seconds * 1000)
            elif text:
                found = page.get_by_text(text, exact=False).first
                _wait_until(page, lambda t: found.wait_for(timeout=t), seconds * 1000)
            else:
                _pause(page, seconds * 1000)
        except Exception as exc:
            if "Timeout" not in str(exc):
                raise
            what = f"the selector {selector!r}" if selector else f"the text {text!r}"
            raise BrowserError(f"Waited {seconds:g}s; {what} did not appear.") from exc
        session.touch()
        return {"url": page.url, "title": _title(page), "waited_s": seconds}
    if action == "screenshot":
        return _do_screenshot(session, page, full_page=bool(args.get("full_page")), marks=bool(args.get("marks")))
    if action == "crop":
        return _do_crop(session, page, args)
    if action == "inspect_point":
        return _do_inspect_point(page, args)
    if action == "inspect":
        return _do_inspect(page, args)
    if action == "compare":
        return _do_compare(session, page, args, reference)
    if action == "javascript_exec":
        return _do_script(page, str(args.get("script") or args.get("text") or ""))
    if action == "click_point":
        page.mouse.click(float(args.get("x") or 0), float(args.get("y") or 0), click_count=2 if args.get("double") else 1)
        session.last_input = (tab_id, time.monotonic(), agent.key)
        _settle(page)
        session.touch()
        return {}
    raise BrowserError(f"Unknown browser action {action!r}. Use one of: {', '.join(AGENT_ACTIONS)}.")


def _fill(page: Any, locator: Any, text: str) -> None:
    try:
        tag = str(locator.evaluate("(el) => el.tagName", timeout=2000)).lower()
    except Exception:
        tag = ""
    if tag == "select":
        try:
            locator.select_option(label=text, timeout=3000)
        except Exception:
            locator.select_option(value=text, timeout=3000)
        return
    try:
        locator.fill(text)
    except Exception:
        locator.click()
        page.keyboard.type(text)


def _scroll_delta(args: dict[str, Any]) -> tuple[float, float]:
    if args.get("dx") is not None or args.get("dy") is not None:
        return float(args.get("dx") or 0), float(args.get("dy") or 0)
    amount = float(args.get("amount") or 600)
    direction = str(args.get("direction") or "down").lower()
    return {
        "up": (0.0, -amount), "down": (0.0, amount), "left": (-amount, 0.0), "right": (amount, 0.0),
    }.get(direction, (0.0, amount))


def _describe_locator(locator: Any, args: dict[str, Any]) -> str:
    try:
        label = locator.evaluate(
            "(el) => (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') || "
            "el.getAttribute('title') || el.getAttribute('name') || el.tagName.toLowerCase()).replace(/\\s+/g, ' ').trim().slice(0, 80)",
            timeout=2000,
        )
    except Exception:
        label = ""
    if label:
        return str(label)
    if args.get("ref") not in (None, ""):
        return f"[{args.get('ref')}]"
    return str(args.get("selector") or args.get("text") or "element")


def _save_shot(session: _Session, data: bytes) -> dict[str, Any]:
    os.makedirs(session.shots_dir, exist_ok=True)
    shot_id = uuid.uuid4().hex[:16]
    with open(os.path.join(session.shots_dir, shot_id + ".jpg"), "wb") as handle:
        handle.write(data)
    _prune_shots(session.shots_dir)
    width, height = _jpeg_size(data)
    return {
        "shot_id": shot_id,
        "storage_key": os.path.basename(os.path.dirname(session.storage_dir)),
        "width": width,
        "height": height,
    }


def _do_screenshot(session: _Session, page: Any, *, full_page: bool = False, marks: bool = False) -> dict[str, Any]:
    marked = 0
    if marks and not full_page:
        try:
            browser_snapshot.collect(_refs(session, page), page, max_elements=SNAPSHOT_MAX_ELEMENTS, view_only=True)
            _agent().ref_tab = _tab_of(session, page)
            marked = int(page.evaluate(browser_snapshot.MARKS_JS, 80) or 0)
        except Exception:
            marked = 0
    try:
        data = page.screenshot(type="jpeg", quality=75, full_page=full_page, timeout=20_000, scale="css")
    finally:
        if marked:
            try:
                page.evaluate(browser_snapshot.UNMARK_JS)
            except Exception:
                pass
    out = {"url": page.url, "title": _title(page), **_save_shot(session, data), "full_page": full_page}
    if marked:
        out["marks"] = marked
    return out


def _has_element_target(args: dict[str, Any]) -> bool:
    return args.get("ref") not in (None, "") or bool(str(args.get("selector") or "").strip()) or bool(str(args.get("text") or "").strip())


_GEOMETRY_JS = r"""
(el, opts) => {
  let r = el ? el.getBoundingClientRect() : null;
  if (el && opts && opts.text) {
    // A text layer in a design is as big as its text; the element may be a wider block.
    const range = document.createRange();
    range.selectNodeContents(el);
    const t = range.getBoundingClientRect();
    if (t.width >= 1 && t.height >= 1) r = t;
  }
  const doc = document.documentElement, body = document.body;
  return {
    box: r ? { x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height } : null,
    textOnly: !!(el && !el.children.length && (el.textContent || "").trim()),
    scrollX: scrollX, scrollY: scrollY, vw: innerWidth, vh: innerHeight, layoutWidth: doc.clientWidth || innerWidth,
    docWidth: Math.max(doc.scrollWidth, body ? body.scrollWidth : 0, innerWidth),
    docHeight: Math.max(doc.scrollHeight, body ? body.scrollHeight : 0, innerHeight),
  };
}
"""


def _page_geometry(page: Any) -> dict[str, Any]:
    return page.evaluate(_GEOMETRY_JS, None)


def _padding(args: dict[str, Any], default: float) -> float:
    try:
        value = float(args["padding"]) if args.get("padding") is not None else default
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0.0), 200.0)


def _target_rect(page: Any, args: dict[str, Any], default_padding: float = 8,
                 text_box: bool = False) -> tuple[dict[str, int], str, dict[str, Any]]:
    if _has_element_target(args):
        locator = _locator(page, args)
        options = {"text": bool(text_box)}
        geo = locator.evaluate(_GEOMETRY_JS, options)
        box = geo.get("box")
        if not box or box["width"] < 1 or box["height"] < 1:
            raise BrowserError("That element has no visible box, so there is nothing to capture.")
        in_view = (box["y"] >= geo["scrollY"] and box["y"] + box["height"] <= geo["scrollY"] + geo["vh"]
                   and box["x"] >= geo["scrollX"] and box["x"] + box["width"] <= geo["scrollX"] + geo["vw"])
        if not in_view:
            try:
                block = "center" if box["height"] <= geo["vh"] else "start"
                locator.evaluate(f"(el) => el.scrollIntoView({{block: '{block}', inline: 'nearest'}})")
                _pause(page, 120)
                geo = locator.evaluate(_GEOMETRY_JS, options)
                box = geo.get("box") or box
            except Exception:
                pass
        pad = _padding(args, default_padding)
        rect = {"x": box["x"] - pad, "y": box["y"] - pad, "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad}
        label = _describe_locator(locator, args)
    else:
        try:
            rect = {key: float(args[key]) for key in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError):
            raise BrowserError("Say what to capture: ref, selector or text for an element, or x, y, width and height for a region.")
        if rect["width"] < 2 or rect["height"] < 2:
            raise BrowserError("The region needs a width and height of at least 2 pixels.")
        geo = _page_geometry(page)
        where = "page" if args.get("full_page") else "view"
        label = f"region {int(rect['width'])}×{int(rect['height'])} at ({int(rect['x'])}, {int(rect['y'])}) of the {where}"
        if not args.get("full_page"):
            rect["x"] += geo["scrollX"]
            rect["y"] += geo["scrollY"]
    left, top = max(rect["x"], 0.0), max(rect["y"], 0.0)
    right = min(rect["x"] + rect["width"], float(geo["docWidth"]))
    bottom = min(rect["y"] + rect["height"], float(geo["docHeight"]))
    if right - left < 2 or bottom - top < 2:
        raise BrowserError("That region is outside the page.")
    out = {"x": int(round(left)), "y": int(round(top))}
    out["width"] = max(2, int(round(right)) - out["x"])
    out["height"] = max(2, int(round(bottom)) - out["y"])
    return out, label, geo


def _capture(page: Any, rect: dict[str, int], geo: dict[str, Any], **fmt: Any) -> bytes:
    sx, sy = float(geo.get("scrollX") or 0), float(geo.get("scrollY") or 0)
    if (rect["x"] >= sx and rect["y"] >= sy and rect["x"] + rect["width"] <= sx + geo["vw"]
            and rect["y"] + rect["height"] <= sy + geo["vh"]):
        clip = {"x": rect["x"] - sx, "y": rect["y"] - sy, "width": rect["width"], "height": rect["height"]}
        return page.screenshot(clip=clip, timeout=20_000, scale="css", **fmt)
    return page.screenshot(full_page=True, clip=dict(rect), timeout=30_000, scale="css", **fmt)


def _do_crop(session: _Session, page: Any, args: dict[str, Any]) -> dict[str, Any]:
    rect, label, geo = _target_rect(page, args, default_padding=8)
    data = _capture(page, rect, geo, type="jpeg", quality=85)
    saved = _save_shot(session, data)
    _remember_shot(session, saved["shot_id"], cut=True)
    return {"url": page.url, "title": _title(page), **saved, "region": rect, "target": label}


_SCROLL_POSITION_JS = (
    "[Math.round(scrollX), Math.round(scrollY), Math.max(0, Math.round(Math.max(document.documentElement.scrollHeight, "
    "document.body ? document.body.scrollHeight : 0) - innerHeight))]"
)


_SCROLL_PANE_JS = r"""
(o) => {
  const scrollable = (el) => {
    const overflow = getComputedStyle(el).overflowY;
    return (overflow === "auto" || overflow === "scroll" || overflow === "overlay") && el.scrollHeight > el.clientHeight + 1;
  };
  const scroll = (el) => {
    const before = el.scrollTop;
    el.scrollBy(0, o.dy);
    const label = (el.getAttribute("aria-label") || el.id || (el.className && String(el.className).split(" ")[0]) || el.tagName.toLowerCase()).slice(0, 60);
    return { moved: el.scrollTop !== before, y: Math.round(el.scrollTop), max: Math.max(0, Math.round(el.scrollHeight - el.clientHeight)), pane: label };
  };
  const stops = (el) => el && el !== document.documentElement && el !== document.body;
  for (let el = document.elementFromPoint(o.x, o.y); stops(el); el = el.parentElement) {
    if (scrollable(el)) return scroll(el);
  }
  let best = null, bestArea = 0;
  for (const el of document.querySelectorAll("*")) {
    if (!scrollable(el)) continue;
    const r = el.getBoundingClientRect();
    const w = Math.min(r.right, innerWidth) - Math.max(r.left, 0), h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    if (w < 80 || h < 80) continue;
    const area = w * h;
    if (area > bestArea) { best = el; bestArea = area; }
  }
  return best ? scroll(best) : null;
}
"""
_PAGE_FINGERPRINT_JS = """
(() => {
  const text = document.body ? document.body.innerText : "";
  let hash = 5381;
  for (let i = 0; i < text.length && i < 60000; i++) hash = ((hash << 5) + hash + text.charCodeAt(i)) | 0;
  let form = 5381;
  for (const el of document.querySelectorAll("input, select, textarea, details")) {
    const state = (el.type || el.tagName) + (el.checked ? "1" : "0") + (el.open ? "o" : "") + (el.type === "password" ? "" : String(el.value || "").slice(0, 60));
    for (let i = 0; i < state.length; i++) form = ((form << 5) + form + state.charCodeAt(i)) | 0;
  }
  return [location.href, Math.round(scrollY), text.length, hash, form, document.getElementsByTagName("*").length,
    document.querySelectorAll("dialog[open], [role=dialog], [aria-modal=true]").length].join("|");
})()
"""
PROGRESS_ACTIONS = frozenset({"click", "type", "press", "scroll", "javascript_exec"})
FINGERPRINT_ACTIONS = PROGRESS_ACTIONS | {"hover", "drag", "select", "check", "upload", "dialog"}
READBACK_ACTIONS = frozenset({"click", "type", "press", "hover", "drag", "select", "check", "upload", "dialog"})
NO_PROGRESS_LIMIT = 3
SCROLL_STUCK_LIMIT = 3
SCROLL_STREAK_WARN = 6
SCROLL_STREAK_LIMIT = 10
_SCROLL_STOP_ADVICE = (
    "Stop scrolling. The snapshot already holds the whole page's text: read it for what you need, click a link or button "
    "(a 'Load more', a category, a search box), dismiss any popup, consent, location or login dialog that is in the way, or read the "
    "data with javascript_exec (e.g. return [...document.querySelectorAll('a')].filter(a => /keyword/i.test(a.textContent))"
    ".map(a => [a.textContent.trim(), a.href]))."
)


# The Browser panel's older point-and-type path (live: true), kept for frontends that do not
# send /livecode/browser/input events yet.
_LIVE_ACTIONS = frozenset({"click_point", "scroll", "type_text", "press"})


def _do_live(session: _Session, page: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action == "click_point":
        x = float(args.get("x") or 0)
        y = float(args.get("y") or 0)
        page.mouse.click(x, y, click_count=2 if args.get("double") else 1)
        session.wheel_at = (x, y)
        session.last_input = (session.active, time.monotonic(), "user")
    elif action == "press":
        key = str(args.get("key") or args.get("text") or "").strip()
        if not key:
            raise BrowserError("Pass key, e.g. Enter, Tab, Escape, ArrowDown or Control+A.")
        page.keyboard.press(key)
        session.last_input = (session.active, time.monotonic(), "user")
    elif action == "type_text":
        page.keyboard.type(str(args.get("text") or ""))
    else:
        dx, dy = _scroll_delta(args)
        viewport = page.viewport_size or VIEWPORT
        px = float(args["x"]) if args.get("x") is not None else viewport["width"] / 2
        py = float(args["y"]) if args.get("y") is not None else viewport["height"] / 2
        if session.wheel_at != (px, py):
            page.mouse.move(px, py)
            session.wheel_at = (px, py)
        page.mouse.wheel(dx, dy)
    session.touch()
    return {}


INPUT_MAX_EVENTS = 400
INPUT_MAX_TEXT = 100_000
CURSOR_PROBE_S = 0.05
_MOUSE_BUTTONS = {0: "left", 1: "middle", 2: "right"}
_MODIFIER_BITS = (("Alt", 1), ("Control", 2), ("Meta", 4), ("Shift", 8))
_MODIFIER_KEYS = frozenset(name for name, _bit in _MODIFIER_BITS)
_NAMED_KEYS = frozenset({
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Insert", "Home", "End", "PageUp", "PageDown",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "ContextMenu", *(f"F{n}" for n in range(1, 13)),
})
_POINTER_EVENTS = frozenset({"move", "down", "up", "wheel", "probe"})


class _Input:
    def __init__(self) -> None:
        self.page: Any = None
        self.keys: set[str] = set()
        self.buttons: set[str] = set()
        self.cursor = ""
        self.probed = 0.0

    def reset(self, page: Any) -> None:
        self.page = page
        self.keys.clear()
        self.buttons.clear()
        self.cursor = ""


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


_CURSOR_JS = r"""
(p) => {
  let el = document.elementFromPoint(p.x, p.y);
  while (el && el.shadowRoot) {
    const inner = el.shadowRoot.elementFromPoint(p.x, p.y);
    if (!inner || inner === el) break;
    el = inner;
  }
  if (!el || el.tagName === "IFRAME" || el.tagName === "FRAME") return "default";
  const style = getComputedStyle(el);
  if (style.cursor && style.cursor !== "auto") return style.cursor.split(",").pop().trim();
  if (el.isContentEditable || el.tagName === "TEXTAREA") return "text";
  if (el.tagName === "INPUT") return /^(button|submit|reset|checkbox|radio|range|color|file|image)$/i.test(el.type || "") ? "default" : "text";
  if (style.userSelect === "none") return "default";
  const hit = document.caretRangeFromPoint ? document.caretRangeFromPoint(p.x, p.y) : null;
  const node = hit && hit.startContainer;
  if (node && node.nodeType === 3 && node.nodeValue.trim()) {
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      if (p.x >= r.left && p.x <= r.right && p.y >= r.top && p.y <= r.bottom) return "text";
    }
  }
  return "default";
}
"""
_SELECTION_JS = r"""
() => {
  if (!document.hasFocus()) return null;
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (el && (el.tagName === "IFRAME" || el.tagName === "FRAME")) return null;
  if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA") && typeof el.selectionStart === "number") {
    return el.type === "password" ? "" : el.value.slice(el.selectionStart, el.selectionEnd);
  }
  return String(window.getSelection() || "");
}
"""
_PASTE_JS = r"""
(text) => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el || el.tagName === "IFRAME" || el.tagName === "FRAME") return true;
  const data = new DataTransfer();
  data.setData("text/plain", text);
  return el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true, composed: true }));
}
"""


def _coalesce_input(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events[:INPUT_MAX_EVENTS]:
        kind = event.get("t")
        last = out[-1] if out else None
        if last is not None and kind == last.get("t") == "move":
            out[-1] = event
        elif last is not None and kind == last.get("t") == "wheel" and last.get("m") == event.get("m"):
            out[-1] = {**event, "dx": _num(last.get("dx")) + _num(event.get("dx")), "dy": _num(last.get("dy")) + _num(event.get("dy"))}
        else:
            out.append(event)
    return out


def _sync_modifiers(page: Any, state: _Input, mods: Any) -> None:
    if mods is None:
        return
    bits = int(_num(mods))
    for name, bit in _MODIFIER_BITS:
        if bits & bit and name not in state.keys:
            page.keyboard.down(name)
            state.keys.add(name)
        elif not bits & bit and name in state.keys:
            page.keyboard.up(name)
            state.keys.discard(name)


def _release_input(page: Any, state: _Input) -> None:
    for button in list(state.buttons):
        try:
            page.mouse.up(button=button)
        except Exception:
            pass
    for name in list(state.keys):
        try:
            page.keyboard.up(name)
        except Exception:
            pass
    state.buttons.clear()
    state.keys.clear()


def _selected_text(page: Any) -> str:
    for frame in page.frames:
        try:
            text = frame.evaluate(_SELECTION_JS)
        except Exception:
            continue
        if text is not None:
            return str(text)[:INPUT_MAX_TEXT]
    return ""


def _paste(page: Any, text: str) -> None:
    try:
        proceed = page.evaluate(_PASTE_JS, text) is not False
    except Exception:
        proceed = True
    if proceed:
        page.keyboard.insert_text(text)


def _press_key(page: Any, key: str) -> None:
    if not key or key in _MODIFIER_KEYS or (len(key) != 1 and key not in _NAMED_KEYS):
        return
    try:
        page.keyboard.press(key)
    except Exception:
        pass


def _dispatch_input(session: _Session, events: list[dict[str, Any]]) -> dict[str, Any]:
    page = session.tabs.get(session.active)
    state = session.input
    if page is None or page.is_closed():
        state.reset(None)
        return {"ok": False}
    if state.page is not page:
        if state.page is not None and not state.page.is_closed():
            _release_input(state.page, state)
        state.reset(page)
    out: dict[str, Any] = {"ok": True}
    at: tuple[float, float] | None = None
    probe = changed = False
    for event in events:
        kind = str(event.get("t") or "")
        try:
            if kind == "release":
                _release_input(page, state)
                continue
            if kind == "copy":
                out["clipboard"] = _selected_text(page)
                continue
            _sync_modifiers(page, state, event.get("m"))
            if kind in _POINTER_EVENTS:
                x, y = _num(event.get("x")), _num(event.get("y"))
                if kind != "probe" and (kind == "move" or at != (x, y)):
                    page.mouse.move(x, y)
                at, probe = (x, y), True
                if kind in ("down", "up"):
                    button = _MOUSE_BUTTONS.get(int(_num(event.get("b"))), "left")
                    count = max(1, min(int(_num(event.get("n"), 1)), 3))
                    if kind == "down":
                        page.mouse.down(button=button, click_count=count)
                        state.buttons.add(button)
                        session.last_input = (session.active, time.monotonic(), "user")
                    else:
                        page.mouse.up(button=button, click_count=count)
                        state.buttons.discard(button)
                    changed = True
                elif kind == "wheel":
                    page.mouse.wheel(_num(event.get("dx")), _num(event.get("dy")))
            elif kind == "char":
                text = str(event.get("c") or "")[:8]
                if text:
                    page.keyboard.type(text)
                    changed = True
            elif kind == "key":
                _press_key(page, str(event.get("k") or ""))
                session.last_input = (session.active, time.monotonic(), "user")
                changed = True
            elif kind in ("text", "paste"):
                text = str(event.get("s") or "")[:INPUT_MAX_TEXT]
                if text and kind == "text":
                    page.keyboard.insert_text(text)
                elif text:
                    _paste(page, text)
                changed = changed or bool(text)
        except Exception as exc:
            out["error"] = _first_line(exc)
    if probe and at is not None and not state.buttons:
        now = time.monotonic()
        forced = any(event.get("t") == "probe" for event in events)
        if forced or now - state.probed >= CURSOR_PROBE_S:
            state.probed = now
            try:
                cursor = str(page.evaluate(_CURSOR_JS, {"x": at[0], "y": at[1]}) or "default")[:40]
            except Exception:
                cursor = ""
            if cursor and (cursor != state.cursor or forced):
                state.cursor = cursor
                out["cursor"] = cursor
    if changed:
        session.touch()
    return out


def input_events(state_path: str, events: Any) -> dict[str, Any]:
    session = _session(state_path)
    items = [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []
    for event in reversed(items):
        if event.get("t") == "view":
            set_view_box(state_path, event.get("w"), event.get("h"))
            break
    batch = _coalesce_input([event for event in items if event.get("t") != "view"])
    if not batch or session.context is None:
        return {"ok": session.context is not None}
    return session.worker.call(_dispatch_input, session, batch, priority=True, timeout=15)


def _do_scroll(session: _Session, page: Any, args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    to = str(args.get("to") or "").strip().lower()
    before = page.evaluate(_SCROLL_POSITION_JS)
    pane = None
    dx = dy = 0.0
    if _has_element_target(args):
        locator = _locator(page, args)
        locator.evaluate("(el) => el.scrollIntoView({block: 'center', inline: 'nearest'})")
        out["target"] = _describe_locator(locator, args)
        _pause(page, 150)
        box = (locator.evaluate(_GEOMETRY_JS, {}) or {}).get("box")
        if box:
            out["region"] = {key: int(round(box[key])) for key in ("x", "y", "width", "height")}
    elif to in ("top", "bottom") or args.get("to_y") is not None:
        try:
            y = float(args["to_y"]) if args.get("to_y") is not None else (0.0 if to == "top" else 1e9)
        except (TypeError, ValueError):
            raise BrowserError("to_y is a page y in CSS pixels.")
        page.evaluate("(y) => window.scrollTo(scrollX, y)", y)
        after = page.evaluate(_SCROLL_POSITION_JS)
        if after[1] == before[1] and before[2] == 0 and to in ("top", "bottom"):
            viewport = page.viewport_size or VIEWPORT
            page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)
            page.mouse.wheel(0, -100_000 if to == "top" else 100_000)
    else:
        dx, dy = _scroll_delta(args)
        viewport = page.viewport_size or VIEWPORT
        px = float(args["x"]) if args.get("x") is not None else viewport["width"] / 2
        py = float(args["y"]) if args.get("y") is not None else viewport["height"] / 2
        page.mouse.move(px, py)
        page.mouse.wheel(dx, dy)
        _pause(page, 120)
        if dy and page.evaluate(_SCROLL_POSITION_JS)[1] == before[1]:
            pane = page.evaluate(_SCROLL_PANE_JS, {"x": px, "y": py, "dx": dx, "dy": dy})
    _pause(page, 150)
    session.touch()
    position = page.evaluate(_SCROLL_POSITION_JS)
    moved = position[0] != before[0] or position[1] != before[1] or bool(pane and pane.get("moved"))
    out.update({"url": page.url, "scroll_x": position[0], "scroll_y": position[1], "scroll_max_y": position[2], "moved": moved})
    if pane:
        out["scroll_pane"] = pane
    going_down = to == "bottom" or (not to and dy > 0) or (args.get("to_y") is not None and position[1] > before[1])
    if pane:
        at_end = pane["y"] >= pane["max"] - 2 if dy > 0 else pane["y"] <= 2
    else:
        at_end = position[1] >= position[2] - 2 if going_down else position[1] <= 2
    agent = _agent()
    if not moved and not _has_element_target(args):
        agent.scroll_stuck += 1
        if at_end:
            out["warning"] = ("Already at the " + ("bottom" if going_down else "top") + " of the page: scrolling further does nothing. " + _SCROLL_STOP_ADVICE)
        else:
            out["warning"] = ("The page did not scroll. Something may be blocking it (a popup, cookie banner, or a location or login dialog), or the "
                              "content is not in a scrollable area. " + _SCROLL_STOP_ADVICE)
        if agent.scroll_stuck >= SCROLL_STUCK_LIMIT:
            raise BrowserStuck(f"The page has not moved for {agent.scroll_stuck} scrolls in a row. " + _SCROLL_STOP_ADVICE)
    else:
        agent.scroll_stuck = 0
    agent.scroll_streak += 1
    if agent.scroll_streak >= SCROLL_STREAK_LIMIT:
        agent.scroll_streak = 0
        raise BrowserStuck(f"You have scrolled {SCROLL_STREAK_LIMIT} times in a row without another action. " + _SCROLL_STOP_ADVICE)
    if agent.scroll_streak >= SCROLL_STREAK_WARN and "warning" not in out:
        out["warning"] = f"That was scroll {agent.scroll_streak} in a row. " + _SCROLL_STOP_ADVICE
    return out


def _device_name(value: Any) -> str:
    return re.sub(r"[\s_]+", "-", str(value or "").strip().lower())


def _do_resize(session: _Session, args: dict[str, Any]) -> dict[str, Any]:
    mode = "fit" if str(args.get("mode") or "fixed").strip().lower() == "fit" else "fixed"
    device = _device_name(args.get("device"))
    if device in ("fit", "auto", "tab", "pane", "fit-to-tab"):
        session.viewport_mode = "fit"
        session.touch()
        return {"viewport": dict(session.viewport), "viewport_mode": "fit",
                "note": "The viewport follows the Browser tab's size again."}
    if mode == "fit" and session.viewport_mode == "fixed":
        return {"viewport": dict(session.viewport), "viewport_mode": "fixed", "ignored": True}
    if device:
        if device not in DEVICE_PRESETS:
            raise BrowserError(f"Unknown device {device!r}. Use one of: {', '.join(DEVICE_PRESETS)}; or width and height.")
        width, height = (float(v) for v in DEVICE_PRESETS[device])
    else:
        if args.get("width") in (None, "") and args.get("height") in (None, ""):
            raise BrowserError(f"Pass width and height in CSS pixels, or device: one of {', '.join(DEVICE_PRESETS)}.")
        try:
            width = float(args.get("width") or session.viewport["width"])
            height = float(args.get("height") or session.viewport["height"])
        except (TypeError, ValueError):
            raise BrowserError("width and height are numbers of CSS pixels.")
    size = {
        "width": int(round(min(max(width, MIN_VIEWPORT[0]), MAX_VIEWPORT[0]))),
        "height": int(round(min(max(height, MIN_VIEWPORT[1]), MAX_VIEWPORT[1]))),
    }
    out: dict[str, Any] = {}
    if (size["width"], size["height"]) != (int(round(width)), int(round(height))) and mode == "fixed":
        out["note"] = (f"Clamped to {size['width']}×{size['height']}: the viewport can be {MIN_VIEWPORT[0]}–{MAX_VIEWPORT[0]} "
                       f"wide and {MIN_VIEWPORT[1]}–{MAX_VIEWPORT[1]} tall. For a taller design, compare with full_page.")
    session.viewport_mode = mode
    if session.viewport != size:
        session.viewport = size
        for tab in list(session.tabs.values()):
            try:
                tab.set_viewport_size(dict(size))
            except Exception:
                pass
    session.touch()
    out.update({"viewport": dict(size), "viewport_mode": mode})
    if device:
        out["device"] = device
    page = session.tabs.get(_current_tab_id(session, _agent()) or session.active)
    if mode == "fixed" and page is not None and not page.is_closed() and page.url not in ("", "about:blank"):
        try:
            _pause(page, 200)
            geo = _page_geometry(page)
            out["url"] = page.url
            out["page_height"] = int(round(geo["docHeight"]))
            extra = int(round(geo["docWidth"] - geo["vw"]))
            if extra > 1:
                out["overflow_x"] = extra
                out["warning"] = f"The page is {extra} px wider than the viewport at this width: something overflows sideways."
        except Exception:
            pass
    return out


_DOM_HELPERS_JS = r"""
  const SKIP = new Set(["script", "style", "noscript", "template", "meta", "link", "br", "head", "title", "base"]);
  const hex = (c) => {
    const m = String(c || "").match(/rgba?\(([^)]+)\)/);
    if (!m) return c === "transparent" ? "" : String(c || "");
    const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
    const a = p.length > 3 ? p[3] : 1;
    if (!a) return "";
    const h = "#" + p.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
    return a >= 0.995 ? h : h + " " + Math.round(a * 100) + "%";
  };
  const num = (v) => { const n = parseFloat(v); return isNaN(n) ? String(v) : String(Math.round(n * 10) / 10); };
  const sides = (s, prop, suffix) => {
    const v = ["Top", "Right", "Bottom", "Left"].map((k) => num(s[prop + k + (suffix || "")]));
    if (v.every((x) => x === "0")) return "";
    if (v.every((x) => x === v[0])) return v[0];
    if (v[0] === v[2] && v[1] === v[3]) return v[0] + " " + v[1];
    return v.join(" ");
  };
  const visible = (el, s) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    s = s || getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && Number(s.opacity) !== 0;
  };
  const ownText = (el) => {
    let t = "";
    if (!el.children.length) t = el.innerText || el.textContent || "";
    else for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
    t = t.replace(/\s+/g, " ").trim();
    if (!t && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) t = el.value || el.getAttribute("placeholder") || "";
    if (!t && el.tagName === "IMG") t = el.getAttribute("alt") || "";
    return t.slice(0, 80);
  };
  const cssPath = (el) => {
    if (el === document.body || el === document.documentElement) return el.tagName.toLowerCase();
    const ok = (v) => /^[A-Za-z_][\w-]*$/.test(v);
    if (el.id && ok(el.id) && document.querySelectorAll("#" + el.id).length === 1) return "#" + el.id;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body && node !== document.documentElement && parts.length < 7) {
      if (node !== el && node.id && ok(node.id)) { parts.unshift("#" + node.id); break; }
      let part = node.tagName.toLowerCase();
      const cls = Array.from(node.classList).filter(ok).slice(0, 2);
      if (cls.length) part += "." + cls.join(".");
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1 && same.filter((c) => cls.every((k) => c.classList.contains(k))).length > 1) {
          part += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
        }
      }
      parts.unshift(part);
      node = parent;
    }
    let path = parts.join(" > ");
    try { if (document.querySelectorAll(path).length !== 1) path = "body > " + path; } catch (e) {}
    return path;
  };
  const describe = (el) => {
    let name = el.tagName.toLowerCase();
    if (el.id) name += "#" + el.id;
    const cls = (typeof el.className === "string" ? el.className : "").trim().split(/\s+/).filter(Boolean).slice(0, 3);
    if (cls.length) name += "." + cls.join(".");
    const t = ownText(el);
    return t ? name + ' "' + t.slice(0, 48) + '"' : name;
  };
  const styleOf = (el, full) => {
    const s = getComputedStyle(el), out = {};
    const tag = el.tagName.toLowerCase();
    if (ownText(el) || ["input", "textarea", "select", "button"].includes(tag) || full) {
      const family = s.fontFamily.split(",")[0].replace(/["']/g, "").trim();
      out.font = family + " " + s.fontWeight + " " + num(s.fontSize) + "px" + (s.lineHeight !== "normal" ? " /" + num(s.lineHeight) + "px" : "");
      out.color = hex(s.color);
      if (s.letterSpacing !== "normal" && parseFloat(s.letterSpacing)) out.letter_spacing = num(s.letterSpacing) + "px";
      if (!["start", "left", "-webkit-auto"].includes(s.textAlign)) out.align = s.textAlign;
      if (s.textTransform !== "none") out.transform = s.textTransform;
      if (s.textDecorationLine && s.textDecorationLine !== "none") out.decoration = s.textDecorationLine;
    }
    const bg = hex(s.backgroundColor);
    if (bg) out.bg = bg;
    if (s.backgroundImage && s.backgroundImage !== "none") out.bg_image = s.backgroundImage.slice(0, 90);
    const edges = ["Top", "Right", "Bottom", "Left"].filter((k) => parseFloat(s["border" + k + "Width"]) > 0 && s["border" + k + "Style"] !== "none")
      .map((k) => [k.toLowerCase(), num(s["border" + k + "Width"]) + "px " + s["border" + k + "Style"] + " " + hex(s["border" + k + "Color"])]);
    if (edges.length === 4 && edges.every((e) => e[1] === edges[0][1])) out.border = edges[0][1];
    else if (edges.length) out.border = edges.map((e) => e[0] + " " + e[1]).join(", ");
    const radius = ["TopLeft", "TopRight", "BottomRight", "BottomLeft"].map((k) => num(s["border" + k + "Radius"]));
    if (radius.some((x) => x !== "0")) out.radius = radius.every((x) => x === radius[0]) ? radius[0] : radius.join(" ");
    const pad = sides(s, "padding");
    if (pad) out.padding = pad;
    if (full) { const margin = sides(s, "margin"); if (margin) out.margin = margin; }
    if (s.display.includes("flex")) {
      out.layout = "flex " + (s.flexDirection.startsWith("column") ? "column" : "row") +
        (parseFloat(s.columnGap) || parseFloat(s.rowGap) ? " gap " + (s.flexDirection.startsWith("column") ? num(s.rowGap) : num(s.columnGap)) : "") +
        (s.alignItems !== "normal" && s.alignItems !== "stretch" ? " align " + s.alignItems : "") +
        (s.justifyContent !== "normal" && s.justifyContent !== "flex-start" ? " justify " + s.justifyContent : "") +
        (s.flexWrap === "wrap" ? " wrap" : "");
    } else if (s.display.includes("grid")) {
      out.layout = "grid cols " + s.gridTemplateColumns.split(" ").length +
        (parseFloat(s.columnGap) ? " gap " + num(s.rowGap) + " " + num(s.columnGap) : "");
    } else if (full) out.display = s.display;
    if (s.boxShadow && s.boxShadow !== "none") out.shadow = s.boxShadow.replace(/rgba?\([^)]*\)/g, (c) => hex(c)).slice(0, 90);
    if (Number(s.opacity) < 0.995) out.opacity = Math.round(Number(s.opacity) * 100) / 100;
    if (full) {
      if (s.position !== "static") out.position = s.position;
      if (s.maxWidth !== "none") out.max_width = s.maxWidth;
      if (s.overflow !== "visible") out.overflow = s.overflow;
    }
    return out;
  };
  const boxOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round((r.left + scrollX) * 10) / 10, y: Math.round((r.top + scrollY) * 10) / 10,
             width: Math.round(r.width * 10) / 10, height: Math.round(r.height * 10) / 10 };
  };
"""

_OUTLINE_JS = "(root, o) => {" + _DOM_HELPERS_JS + r"""
  const STYLE_KEYS = ["font", "color", "bg", "bg_image", "border", "radius", "padding", "layout", "shadow", "opacity",
                      "letter_spacing", "align", "transform"];
  const lines = [];
  let count = 0, truncated = false, chars = 0;
  const meaningful = (el, s) => {
    const tag = el.tagName.toLowerCase();
    if (ownText(el)) return true;
    if (["img", "svg", "video", "canvas", "picture", "iframe", "input", "textarea", "select", "button", "a", "hr"].includes(tag)) return true;
    if (hex(s.backgroundColor) || (s.backgroundImage && s.backgroundImage !== "none") || (s.boxShadow && s.boxShadow !== "none")) return true;
    if (parseFloat(s.borderTopWidth) || parseFloat(s.borderBottomWidth) || parseFloat(s.borderLeftWidth)) return true;
    if ((s.display.includes("flex") || s.display.includes("grid")) && el.children.length > 1) return true;
    return false;
  };
  const walk = (el, depth) => {
    if (truncated) return;
    const tag = el.tagName.toLowerCase();
    if (SKIP.has(tag)) return;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" && !el.children.length) return;
    const show = el === root || (visible(el, s) && meaningful(el, s));
    let next = depth;
    if (show) {
      const b = boxOf(el), st = styleOf(el, false);
      const bits = [describe(el), b.x + "," + b.y + " " + b.width + "×" + b.height];
      if (ownText(el) && !el.children.length) {
        // A design measures a text layer by its text; a block element can be much wider.
        const range = document.createRange();
        range.selectNodeContents(el);
        const t = range.getBoundingClientRect();
        if (t.width >= 1 && (t.width < b.width - 2 || Math.abs(t.left + scrollX - b.x) > 2)) {
          bits.push("text " + Math.round((t.left + scrollX) * 10) / 10 + "," + Math.round((t.top + scrollY) * 10) / 10 + " " +
                    Math.round(t.width * 10) / 10 + "×" + Math.round(t.height * 10) / 10);
        }
      }
      for (const k of STYLE_KEYS) if (st[k] !== undefined && st[k] !== "") bits.push(k.replace("_", " ") + " " + st[k]);
      const line = "  ".repeat(depth) + bits[0] + " " + bits.slice(1).join(" · ") + " [" + cssPath(el) + "]";
      if (lines.length >= o.maxLines || chars + line.length > o.maxChars) { truncated = true; return; }
      lines.push(line); chars += line.length + 1; count++;
      next = depth + 1;
    }
    if (tag === "svg" || next > o.maxDepth) return;
    for (const child of el.children) walk(child, next);
  };
  const start = root || document.body;
  if (start) walk(start, 0);
  const result = { outline: lines.join("\n"), count: count, truncated: truncated };
  if (root) {
    result.element = { name: describe(root), selector: cssPath(root), box: boxOf(root), styles: styleOf(root, true) };
    const t = (root.innerText || root.value || "").replace(/\s+/g, " ").trim();
    if (t) result.element.text = t.slice(0, 300);
  }
  const doc = document.documentElement;
  result.page_size = [Math.max(doc.scrollWidth, innerWidth), Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0)];
  return result;
}"""

_ELEMENTS_AT_JS = "(points) => {" + _DOM_HELPERS_JS + r"""
  const all = document.body ? document.body.getElementsByTagName("*") : [];
  const boxes = [];
  for (let i = 0; i < all.length && i < 8000; i++) {
    const el = all[i];
    if (SKIP.has(el.tagName.toLowerCase()) || el.ownerSVGElement) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    boxes.push([el, r.left + scrollX, r.top + scrollY, r.width, r.height]);
  }
  return points.map(([x, y]) => {
    let best = null, area = Infinity;
    for (const [el, bx, by, bw, bh] of boxes) {
      if (x < bx || y < by || x > bx + bw || y > by + bh || bw * bh > area) continue;
      const s = getComputedStyle(el);
      if (s.visibility === "hidden" || Number(s.opacity) === 0) continue;
      best = el; area = bw * bh;
    }
    return best ? { element: describe(best), selector: cssPath(best) } : null;
  });
}"""

_ELEMENTS_JS = "(root, o) => {" + _DOM_HELPERS_JS + r"""
  // The elements to compare with the design one by one: those that show something (text, an
  // image, a control, a background, border or shadow), in document order, parents first. Pure
  // layout wrappers are left out, and so is whatever only paints the page's own background.
  const R = o.rect, out = [], parents = [];
  const round = (v) => Math.round(v * 10) / 10;
  // Boxes to 1/100 px: the comparison takes the page's side of a box from them as exact.
  const at = (v) => Math.round(v * 100) / 100;
  const opaque = (c) => c && !c.includes("%");
  const visuals = (el, s, tag) => {
    const f = {};
    if (ownText(el) && !["input", "textarea", "select", "img"].includes(tag)) f.text = true;
    if (["input", "textarea", "select", "button"].includes(tag)) f.control = true;
    if (["img", "svg", "canvas", "video", "picture", "iframe"].includes(tag) || (s.backgroundImage && s.backgroundImage !== "none")) f.image = true;
    const bg = hex(s.backgroundColor);
    if (bg) f.bg = opaque(bg) ? bg : "translucent";
    const edges = ["Top", "Right", "Bottom", "Left"].filter((k) => parseFloat(s["border" + k + "Width"]) > 0 && s["border" + k + "Style"] !== "none" && hex(s["border" + k + "Color"]));
    if (edges.length) f.border = hex(s["border" + edges[0] + "Color"]);
    if (edges.length === 4) f.outline = true;  // a border all round: its box can be measured by it
    if (s.boxShadow && s.boxShadow !== "none") f.shadow = true;
    return f;
  };
  const styles = (el, s, f) => {
    const st = {};
    if (f.text || f.control) {
      st.font_size = round(parseFloat(s.fontSize) || 0);
      st.font_weight = s.fontWeight;
      st.font_family = s.fontFamily.split(",")[0].replace(/["']/g, "").trim();
      st.line_height = s.lineHeight === "normal" ? 0 : round(parseFloat(s.lineHeight) || 0);
      st.letter_spacing = s.letterSpacing === "normal" ? 0 : round(parseFloat(s.letterSpacing) || 0);
      st.color = hex(s.color);
      st.align = s.textAlign;
    }
    if (opaque(f.bg)) st.bg = f.bg;
    if (f.border) { st.border = f.border; st.border_width = round(parseFloat(s.borderTopWidth) || parseFloat(s.borderLeftWidth) || 0); }
    st.radius = round(Math.max(...["TopLeft", "TopRight", "BottomRight", "BottomLeft"].map((k) => parseFloat(s["border" + k + "Radius"]) || 0)));
    return st;
  };
  const walk = (el, parent, depth) => {
    const tag = el.tagName.toLowerCase();
    if (SKIP.has(tag) || out.length >= 1500) return;
    const s = getComputedStyle(el);
    if (s.display === "none") return;
    let me = parent;
    if (s.visibility !== "hidden" && Number(s.opacity) > 0.05) {
      const r = el.getBoundingClientRect();
      const box = { x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height };
      const ix = Math.min(box.x + box.width, R.x + R.width) - Math.max(box.x, R.x);
      const iy = Math.min(box.y + box.height, R.y + R.height) - Math.max(box.y, R.y);
      const inside = box.width >= 3 && box.height >= 3 && ix > 0 && iy > 0 && ix * iy >= 0.5 * box.width * box.height;
      const f = inside ? visuals(el, s, tag) : {};
      // An element as large as the compared area only paints its background.
      const backdrop = el !== root && box.width >= R.width * 0.98 && box.height >= R.height * 0.9 && !f.text && !f.control && !f.image;
      if (Object.keys(f).length && !backdrop) {
        const item = { i: out.length, parent: parent, depth: depth, name: describe(el), selector: cssPath(el),
                       box: { x: at(box.x), y: at(box.y), width: at(box.width), height: at(box.height) },
                       flags: f, style: styles(el, s, f) };
        if (f.text) {
          const range = document.createRange();
          range.selectNodeContents(el);
          const t = range.getBoundingClientRect();
          if (t.width >= 1 && t.height >= 1) item.text_box = { x: at(t.left + scrollX), y: at(t.top + scrollY), width: at(t.width), height: at(t.height) };
          item.text = ownText(el).slice(0, 60);
          item.chars = (el.innerText || "").replace(/\s+/g, " ").trim().length;
        }
        out.push(item);
        parents.push(parent);
        me = item.i;
      }
    }
    if (tag === "svg") return;
    for (const child of el.children) walk(child, me, depth + 1);
  };
  const start = root || document.body;
  if (start) walk(start, -1, 0);
  const total = out.length;
  if (out.length <= o.max) return { elements: out, total: total };
  // Too many: keep what shows content first (text, images, controls), then the larger boxes.
  const score = (e) => (e.flags.text || e.flags.image || e.flags.control ? 4 : 1) * Math.sqrt(e.box.width * e.box.height);
  const keep = new Set(out.slice().sort((a, b) => score(b) - score(a)).slice(0, o.max).map((e) => e.i));
  const index = new Map(), kept = [];
  for (const e of out) if (keep.has(e.i)) { index.set(e.i, kept.length); kept.push(e); }
  for (const e of kept) {
    let p = parents[e.i];
    while (p >= 0 && !index.has(p)) p = parents[p];
    e.parent = p >= 0 ? index.get(p) : -1;
  }
  kept.forEach((e, k) => { e.i = k; });
  return { elements: kept, total: total };
}"""

ELEMENTS_MAX = 120

INSPECT_MAX_LINES = 220
INSPECT_MAX_CHARS = 14_000


def _do_inspect(page: Any, args: dict[str, Any]) -> dict[str, Any]:
    options = {"maxLines": INSPECT_MAX_LINES, "maxChars": INSPECT_MAX_CHARS, "maxDepth": 14}
    out: dict[str, Any] = {"url": page.url, "title": _title(page)}
    if _has_element_target(args):
        locator = _locator(page, args)
        data = locator.evaluate(_OUTLINE_JS, options)
        out["target"] = _describe_locator(locator, args)
    else:
        data = page.evaluate(f"(o) => ({_OUTLINE_JS})(null, o)", options)
        out["target"] = "page"
    out.update({key: data.get(key) for key in ("outline", "count", "truncated", "element", "page_size") if data.get(key) not in (None, "")})
    out["coords"] = "Boxes are x,y width×height in page coordinates (CSS px from the page's top-left), as a design tool measures layers."
    if data.get("truncated"):
        out["hint"] = "The outline is cut short: inspect a section (selector) for the rest."
    return out


_INSPECT_JS = r"""
([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  let label = el.tagName.toLowerCase();
  if (el.id) label += "#" + el.id;
  const cls = (typeof el.className === "string" ? el.className : "").trim().split(/\s+/).filter(Boolean).slice(0, 2);
  if (cls.length) label += "." + cls.join(".");
  const text = (el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("alt") || el.getAttribute("title") || "")
    .replace(/\s+/g, " ").trim().slice(0, 80);
  return { x: r.left, y: r.top, width: r.width, height: r.height, label: label, text: text };
}
"""


def _do_inspect_point(page: Any, args: dict[str, Any]) -> dict[str, Any]:
    info = page.evaluate(_INSPECT_JS, [float(args.get("x") or 0), float(args.get("y") or 0)])
    if not info:
        return {"element": None}
    viewport = page.viewport_size or VIEWPORT
    left, top = max(info["x"], 0), max(info["y"], 0)
    right = min(info["x"] + info["width"], viewport["width"])
    bottom = min(info["y"] + info["height"], viewport["height"])
    rect = {"x": round(left), "y": round(top), "width": round(max(right - left, 0)), "height": round(max(bottom - top, 0))}
    return {"element": {"rect": rect, "label": info.get("label") or "", "text": info.get("text") or ""}}


def _prune_shots(folder: str) -> None:
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".jpg")]
    except OSError:
        return
    if len(names) <= MAX_SHOTS_KEPT:
        return
    names.sort(key=lambda n: os.path.getmtime(os.path.join(folder, n)))
    for name in names[: len(names) - MAX_SHOTS_KEPT]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _jpeg_size(data: bytes, default: tuple[int, int] | None = None) -> tuple[int, int]:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _JPEG_SOF:
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        if marker == 0xFF:
            i += 1
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return default if default is not None else (VIEWPORT["width"], VIEWPORT["height"])


def _image_size(data: bytes) -> tuple[int, int]:
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:3] == b"\xff\xd8\xff":
            return _jpeg_size(data, (0, 0))
        if data[:4] == b"GIF8":
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
            if chunk == b"VP8 ":
                return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
        if data[:2] == b"BM":
            return (abs(int.from_bytes(data[18:22], "little", signed=True)),
                    abs(int.from_bytes(data[22:26], "little", signed=True)))
    except (IndexError, ValueError):
        pass
    return 0, 0


def _do_script(page: Any, script: str) -> dict[str, Any]:
    if not script.strip():
        raise BrowserError("Pass script: the JavaScript to run in the page.")
    try:
        value = page.evaluate(script)
    except Exception as exc:
        if "await is only valid" not in str(exc):
            raise BrowserError(f"The script threw: {_first_line(exc).replace('Page.evaluate: ', '')}") from exc
        cdp = page.context.new_cdp_session(page)
        try:
            reply = cdp.send("Runtime.evaluate", {
                "expression": script, "replMode": True, "awaitPromise": True, "returnByValue": True,
            })
        finally:
            try:
                cdp.detach()
            except Exception:
                pass
        if reply.get("exceptionDetails"):
            details = reply["exceptionDetails"]
            message = ((details.get("exception") or {}).get("description") or details.get("text") or "error").splitlines()[0]
            raise BrowserError(f"The script threw: {message}")
        value = (reply.get("result") or {}).get("value")
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    truncated = len(text) > SCRIPT_RESULT_MAX_CHARS
    return {
        "url": page.url,
        "result": text[:SCRIPT_RESULT_MAX_CHARS] + ("…" if truncated else ""),
        "truncated": truncated,
    }


def _do_frame(session: _Session, since: int) -> tuple[int, bytes | None]:
    if session.context is None or not session.tabs:
        return session.seq, None
    page = session.tabs.get(session.active)
    if page is None or page.is_closed():
        return session.seq, None
    now = time.time()
    if session.frame is not None and now - session.frame[2] < FRAME_MIN_INTERVAL_S:
        seq, data, _ = session.frame
        return seq, (data if seq != since else None)
    try:
        quality = FRAME_JPEG_QUALITY if session.view_sharp else STANDARD_FRAME_JPEG_QUALITY
        data = page.screenshot(type="jpeg", quality=quality, timeout=5000, caret="initial")
    except Exception:
        return session.seq, None
    previous = session.frame[1] if session.frame else None
    if data != previous:
        session.seq += 1
    session.frame = (session.seq, data, now)
    return session.seq, (data if session.seq != since else None)


MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_REFERENCES_KEPT = 100
COMPARE_DELTA_E = 8.0
COMPARE_FLAT_DELTA_E = 3.0
COMPARE_SMOOTH_DELTA_E = 3.5
COMPARE_SSIM_NOISE = 0.9
COMPARE_SSIM_RADIUS = 3
_IMAGE_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
    "image/bmp": ".bmp", "image/svg+xml": ".svg",
}
_refs_lock = threading.Lock()


class _Reference:

    __slots__ = ("data", "mime", "label", "scale", "crop", "tab", "url", "cut", "area", "figma", "layer", "note")

    def __init__(self, data: bytes, mime: str, label: str, *, scale: float = 0.0,
                 crop: dict[str, float] | None = None, tab: str = "", url: str = "",
                 figma: dict[str, Any] | None = None) -> None:
        self.data = data
        self.mime = mime
        self.label = label
        self.scale = scale
        self.crop = crop
        self.tab = tab
        self.url = url
        self.cut = False
        self.area = False
        self.figma = figma
        self.layer: dict[str, Any] | None = None
        self.note = ""

    @property
    def live(self) -> bool:
        return not self.data and bool(self.tab or self.url)


def _sniff_image_mime(data: bytes) -> str:
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    start = data[:1000].lstrip().lower()
    if start.startswith(b"<svg") or (start.startswith(b"<?xml") and b"<svg" in start):
        return "image/svg+xml"
    return ""


def _checked_image(data: bytes, what: str) -> tuple[bytes, str]:
    if len(data) > MAX_REFERENCE_BYTES:
        raise BrowserError(f"The {what} is larger than {MAX_REFERENCE_BYTES // (1024 * 1024)} MB.")
    mime = _sniff_image_mime(data)
    if not mime:
        raise BrowserError(f"The {what} is not a PNG, JPEG, GIF, WebP, BMP or SVG image.")
    return data, mime


def _decode_data_url(url: str) -> tuple[bytes, str]:
    match = re.match(r"^data:([\w.+/-]*)((?:;[^,]*)?),(.*)$", str(url or ""), re.S)
    if not match:
        raise BrowserError("Expected an image data: URL.")
    payload = match.group(3)
    if len(payload) > MAX_REFERENCE_BYTES * 4 // 3 + 1024:
        raise BrowserError(f"The image is larger than {MAX_REFERENCE_BYTES // (1024 * 1024)} MB.")
    try:
        if ";base64" in match.group(2):
            data = base64.b64decode(payload, validate=False)
        else:
            from urllib.parse import unquote_to_bytes

            data = unquote_to_bytes(payload)
    except ValueError as exc:
        raise BrowserError("The image data does not decode.") from exc
    return _checked_image(data, "image")


def _refs_dir(session: _Session) -> str:
    return os.path.join(session.storage_dir, "refs")


def _load_refs_index(session: _Session) -> dict[str, Any]:
    try:
        with open(os.path.join(_refs_dir(session), "index.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_refs_index(session: _Session, index: dict[str, Any]) -> None:
    folder = _refs_dir(session)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "index.json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(index, handle)
    os.replace(tmp, os.path.join(folder, "index.json"))


def _prune_refs(session: _Session, index: dict[str, Any]) -> None:
    refs = index.get("refs") or {}
    if len(refs) <= MAX_REFERENCES_KEPT:
        return
    oldest = sorted(refs, key=lambda ref_id: float((refs[ref_id] or {}).get("added") or 0))
    for ref_id in oldest[: len(refs) - MAX_REFERENCES_KEPT]:
        meta = refs.pop(ref_id, None) or {}
        try:
            os.remove(os.path.join(_refs_dir(session), str(meta.get("file") or "")))
        except OSError:
            pass
        for order in (index.get("sessions") or {}).values():
            if ref_id in order:
                order.remove(ref_id)


def register_attachments(state_path: str, session_id: str, urls: list[str]) -> int:
    session_id = str(session_id or "").strip()
    if not session_id or not urls:
        return 0
    session = _session(state_path)
    added = 0
    with _refs_lock:
        index = _load_refs_index(session)
        refs = index.setdefault("refs", {})
        order = index.setdefault("sessions", {}).setdefault(session_id, [])
        latest = index.setdefault("latest", {})
        for url in urls:
            try:
                data, mime = _decode_data_url(url)
            except BrowserError:
                continue
            ref_id = hashlib.sha1(data).hexdigest()[:16]
            if ref_id not in refs:
                name = ref_id + _IMAGE_EXTENSIONS.get(mime, ".img")
                os.makedirs(_refs_dir(session), exist_ok=True)
                with open(os.path.join(_refs_dir(session), name), "wb") as handle:
                    handle.write(data)
                refs[ref_id] = {"file": name, "mime": mime, "added": time.time()}
            if ref_id not in order:
                order.append(ref_id)
                added += 1
            latest[session_id] = {"ref": ref_id, "at": time.time()}
        _prune_refs(session, index)
        _save_refs_index(session, index)
    return added


def set_panel_reference(state_path: str, data_url: str, name: str = "") -> dict[str, Any]:
    data, mime = _decode_data_url(data_url)
    session = _session(state_path)
    with _refs_lock:
        folder = _refs_dir(session)
        os.makedirs(folder, exist_ok=True)
        for old in os.listdir(folder):
            if old.startswith("panel."):
                try:
                    os.remove(os.path.join(folder, old))
                except OSError:
                    pass
        file_name = "panel" + _IMAGE_EXTENSIONS.get(mime, ".img")
        with open(os.path.join(folder, file_name), "wb") as handle:
            handle.write(data)
        index = _load_refs_index(session)
        index["panel"] = {"file": file_name, "mime": mime, "set_at": time.time(), "name": str(name or "")[:120]}
        _save_refs_index(session, index)
    return {"name": str(name or ""), "bytes": len(data), "mime": mime}


def panel_reference(state_path: str) -> dict[str, Any] | None:
    session = _session(state_path)
    with _refs_lock:
        meta = (_load_refs_index(session).get("panel") or None)
    if not meta:
        return None
    try:
        ref = _read_ref_file(session, meta, "reference")
    except BrowserError:
        return None
    return {"image": f"data:{ref.mime};base64," + base64.b64encode(ref.data).decode("ascii"), "name": str(meta.get("name") or "")}


def clear_panel_reference(state_path: str) -> None:
    session = _session(state_path)
    with _refs_lock:
        index = _load_refs_index(session)
        meta = index.pop("panel", None) or {}
        if meta.get("file"):
            try:
                os.remove(os.path.join(_refs_dir(session), str(meta["file"])))
            except OSError:
                pass
        _save_refs_index(session, index)


def _read_ref_file(session: _Session, meta: dict[str, Any], label: str) -> _Reference:
    path = os.path.join(_refs_dir(session), os.path.basename(str(meta.get("file") or "")))
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise BrowserError(f"The {label} is no longer saved; attach it again.") from exc
    return _Reference(data, str(meta.get("mime") or _sniff_image_mime(data) or "image/png"), label)


_IMAGE_URL = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)(?:$|[?#])", re.I)


def _fetch_image_or_page(url: str) -> tuple[bytes, str] | None:
    from urllib.request import Request, urlopen

    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 LiveCode"}), timeout=20) as response:
            kind = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not kind.startswith("image/") and not _IMAGE_URL.search(url):
                return None
            data = response.read(MAX_REFERENCE_BYTES + 1)
    except (OSError, ValueError) as exc:
        if _IMAGE_URL.search(url):
            raise BrowserError(f"Could not download {url}: {_first_line(exc)}") from exc
        return None
    if not _sniff_image_mime(data):
        return None
    return _checked_image(data, "downloaded image")


def load_reference(state_path: str, spec: Any, *, session_id: str = "", resolve_path: Callable[[str], str | None] | None = None) -> _Reference:
    session = _session(state_path)
    text = str(spec or "").strip()
    low = text.lower()
    with _refs_lock:
        index = _load_refs_index(session)
    refs = index.get("refs") or {}
    sid = str(session_id or "")
    order = list((index.get("sessions") or {}).get(sid, []))
    panel = index.get("panel") or None
    design = (index.get("designs") or {}).get(sid) or None
    figma_meta = index.get("figma") or None

    if low in ("", "latest", "attachment", "attachment:latest", "last", "design"):
        latest = (index.get("latest") or {}).get(sid) or {}
        ref_id = latest.get("ref") or (order[-1] if order else "")
        candidates = []
        if ref_id and ref_id in refs:
            candidates.append((float(latest.get("at") or 0), "attachment"))
        if panel:
            candidates.append((float(panel.get("set_at") or 0), "panel"))
        if design and design.get("spec"):
            candidates.append((float(design.get("at") or 0), "design"))
        if figma_meta:
            candidates.append((float(figma_meta.get("set_at") or 0), "figma"))
        if not candidates:
            raise BrowserError(
                "There is no design to compare with yet. Ask the user to attach a screenshot of it (from Figma, Canva or "
                "any design tool) or give its link, or pass reference: tab:<id> of a tab showing it, a page or image "
                "URL, shot:<id> of an earlier screenshot or crop, or a project image path."
            )
        newest = max(candidates)[1]
        if newest == "panel":
            return _read_ref_file(session, panel, "the Browser tab's reference" + (f" ({panel.get('name')})" if panel.get("name") else ""))
        if newest == "figma":
            return _figma_reference(session, figma_meta, "")
        if newest == "design":
            reference = load_reference(state_path, design["spec"], session_id=session_id, resolve_path=resolve_path)
            if design.get("crop") and not reference.crop:
                reference.crop = dict(design["crop"])
                reference.area = True
            if design.get("scale") and not reference.scale:
                reference.scale = float(design["scale"])
            reference.label = str(design.get("label") or reference.label)
            return reference
        number = order.index(ref_id) + 1 if ref_id in order else len(order)
        return _read_ref_file(session, refs[ref_id], f"attachment {number}")
    match = re.match(r"^(?:attachment|image)?\s*[:#]?\s*(\d{1,3})$", low)
    if match:
        number = int(match.group(1))
        if not order:
            raise BrowserError("The user has not attached an image in this chat.")
        if number < 1 or number > len(order):
            raise BrowserError(f"There {'is' if len(order) == 1 else 'are'} {len(order)} attached image{'s' if len(order) != 1 else ''} in this chat; pick attachment:1 to attachment:{len(order)}.")
        return _read_ref_file(session, refs[order[number - 1]], f"attachment {number}")
    if low == "panel":
        if not panel:
            raise BrowserError("No reference is set in the Browser tab's compare view.")
        return _read_ref_file(session, panel, "the Browser tab's reference")
    if low.startswith("shot:"):
        shot_id = low[5:].strip().removesuffix(".jpg")
        path = shot_path(os.path.basename(os.path.dirname(session.storage_dir)), shot_id)
        if not path:
            raise BrowserError(f"No saved screenshot {shot_id}; only the newest {MAX_SHOTS_KEPT} are kept.")
        meta = (index.get("shots") or {}).get(shot_id) or {}
        with open(path, "rb") as handle:
            reference = _Reference(handle.read(), "image/jpeg", f"screenshot {shot_id[:8]}", scale=float(meta.get("scale") or 1.0))
        reference.cut = bool(meta.get("cut"))
        return reference
    if low == "figma" or low.startswith("figma:"):
        if not figma_meta:
            raise BrowserError("No Figma frame is loaded: call figma with the frame's link first (it needs a Figma token), "
                               "or compare with the link itself, which opens in a tab without one.")
        return _figma_reference(session, figma_meta, text.split(":", 1)[1].strip() if ":" in text else "")
    if low.startswith("tab:"):
        tab_id = text[4:].strip()
        if not tab_id:
            raise BrowserError("Say which tab: tab:<id>, one of the ids the tabs action lists.")
        return _Reference(b"", "", f"tab {tab_id}", scale=1.0, tab=tab_id)
    if low.startswith(("http://", "https://")):
        from livecode import figma as _figma

        if _figma.is_figma_url(text):
            try:
                return load_figma_design(state_path, text, session_id=session_id)[0]
            except FigmaUnavailable as exc:
                live = _Reference(b"", "", _short_url(text), scale=1.0, url=text)
                live.note = f"Figma's API is not available ({_first_line(exc)}), so the link was opened in a tab and captured."
                return live
        image = _fetch_image_or_page(text)
        if image:
            return _Reference(image[0], image[1], _short_url(text))
        return _Reference(b"", "", _short_url(text), scale=1.0, url=text)
    if low.startswith("data:"):
        data, mime = _decode_data_url(text)
        return _Reference(data, mime, "image")
    full = resolve_path(text) if resolve_path else None
    if not full or not os.path.isfile(full):
        raise BrowserError(f"No image at {text} in the project.")
    if os.path.getsize(full) > MAX_REFERENCE_BYTES:
        raise BrowserError(f"{text} is larger than {MAX_REFERENCE_BYTES // (1024 * 1024)} MB.")
    with open(full, "rb") as handle:
        data, mime = _checked_image(handle.read(), text)
    return _Reference(data, mime, os.path.basename(text))


def design_context(state_path: str, session_id: str) -> bool:
    try:
        session = _session(state_path)
    except BrowserError:
        return False
    with _refs_lock:
        index = _load_refs_index(session)
    sid = str(session_id or "")
    return bool(sid and ((index.get("sessions") or {}).get(sid) or (index.get("designs") or {}).get(sid)
                         or sid in ((index.get("figma") or {}).get("chats") or [])))


def _remember_design(session: _Session, session_id: str, spec: str, label: str,
                     crop: dict[str, float] | None = None, scale: float = 0.0) -> None:
    sid = str(session_id or "")
    if not sid or not spec:
        return
    with _refs_lock:
        index = _load_refs_index(session)
        designs = index.setdefault("designs", {})
        before = designs.get(sid) or {}
        if crop is None and before.get("spec") == spec:
            crop, scale = before.get("crop"), float(before.get("scale") or 0)
        designs[sid] = {"spec": spec, "label": label[:120], "at": time.time()}
        if crop:
            designs[sid]["crop"] = {key: round(float(crop[key]), 2) for key in ("x", "y", "width", "height")}
        if scale:
            designs[sid]["scale"] = scale
        if len(designs) > 200:
            for old in sorted(designs, key=lambda key: float(designs[key].get("at") or 0))[: len(designs) - 200]:
                designs.pop(old, None)
        _save_refs_index(session, index)


FIGMA_TREE_DEPTH = 12
FIGMA_MAX_PIXELS = 30_000_000
FIGMA_REUSE_S = 6 * 3600
_FIGMA_NODE_ID = re.compile(r"^I?\d+[:-]\d+(?:[;:-]\d+)*$")
_figma_trees: dict[str, tuple[float, dict[str, Any]]] = {}


class FigmaUnavailable(BrowserError):
    pass


def figma_configured() -> bool:
    from livecode import figma as _figma

    return bool(_figma.token())


def _figma_tree(session: _Session, meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not meta or not meta.get("tree"):
        return None
    path = os.path.join(_refs_dir(session), os.path.basename(str(meta["tree"])))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _figma_trees.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as handle:
            tree = json.load(handle)
    except (OSError, ValueError):
        return None
    _figma_trees[path] = (mtime, tree)
    return tree


def load_figma_design(state_path: str, url: str = "", node_id: str = "", session_id: str = "",
                      refresh: bool = False) -> tuple[_Reference, dict[str, Any]]:
    from livecode import figma as _figma

    session = _session(state_path)
    with _refs_lock:
        meta = dict(_load_refs_index(session).get("figma") or {})
    try:
        if url:
            file_key, wanted = _figma.parse_url(url)
        elif meta.get("file_key"):
            file_key, wanted = str(meta["file_key"]), ""
        else:
            raise BrowserError("Pass url: the Figma link to the frame (in Figma, right-click the frame -> Copy link to selection).")
        if node_id:
            wanted = node_id.replace("-", ":")
        same = meta.get("file_key") == file_key and (
            (wanted and wanted == meta.get("node_id")) or (url and not wanted and url == meta.get("url")))
        if same and not refresh and time.time() - float(meta.get("fetched_at") or 0) < FIGMA_REUSE_S \
                and _figma_tree(session, meta) is not None:
            meta = _touch_figma(session, session_id)
            return _figma_reference(session, meta, ""), meta
        if not _figma.token():
            raise FigmaUnavailable("Figma isn't configured (no access token)")
        tree = _figma.fetch_node(file_key, wanted, depth=FIGMA_TREE_DEPTH)
        box = tree.get("absoluteBoundingBox") or {}
        width, height = float(box.get("width") or 0), float(box.get("height") or 0)
        if width < 1 or height < 1:
            raise BrowserError(f"The Figma node {tree.get('name')!r} has no size to render.")
        scale = 2.0
        while scale > 0.25 and width * height * scale * scale > FIGMA_MAX_PIXELS:
            scale /= 2
        png = _figma.render_node(file_key, str(tree.get("id") or wanted), scale)
    except _figma.FigmaError as exc:
        raise FigmaUnavailable(str(exc)) from exc
    mime = _sniff_image_mime(png)
    if mime not in ("image/png", "image/jpeg"):
        raise FigmaUnavailable("Figma's render is not an image")
    image_w, _image_h = _image_size(png)
    now = time.time()
    with _refs_lock:
        folder = _refs_dir(session)
        os.makedirs(folder, exist_ok=True)
        for name, payload in (("figma.png", png), ("figma-tree.json", json.dumps(tree).encode("utf-8"))):
            tmp = os.path.join(folder, name + ".tmp")
            with open(tmp, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, os.path.join(folder, name))
        index = _load_refs_index(session)
        index["figma"] = {
            "file": "figma.png", "mime": mime, "tree": "figma-tree.json", "set_at": now, "fetched_at": now,
            "file_key": file_key, "node_id": str(tree.get("id") or wanted), "name": str(tree.get("name") or "")[:200],
            "type": str(tree.get("type") or ""), "url": url or str(meta.get("url") or ""),
            "width": width, "height": height, "scale": (image_w / width) if image_w else scale,
            "origin": [float(box.get("x") or 0), float(box.get("y") or 0)],
            "chats": ([str(session_id)] if session_id else []) + [c for c in (meta.get("chats") or []) if c != session_id][:19],
        }
        _save_refs_index(session, index)
        meta = index["figma"]
    return _figma_reference(session, meta, ""), meta


def _touch_figma(session: _Session, session_id: str) -> dict[str, Any]:
    with _refs_lock:
        index = _load_refs_index(session)
        meta = index.get("figma") or {}
        meta["set_at"] = time.time()
        chats = [c for c in (meta.get("chats") or []) if c != session_id]
        meta["chats"] = ([str(session_id)] if session_id else []) + chats[:19]
        index["figma"] = meta
        _save_refs_index(session, index)
    return meta


def _figma_reference(session: _Session, meta: dict[str, Any], node_spec: str) -> _Reference:
    from livecode import figma as _figma

    name = str(meta.get("name") or "frame")
    base = _read_ref_file(session, meta, f"Figma {name}")
    tree = _figma_tree(session, meta)
    origin = tuple(float(v) for v in (meta.get("origin") or (0, 0)))
    scale = float(meta.get("scale") or 0)
    reference = _Reference(base.data, base.mime, f"Figma {name}", scale=scale,
                           figma={"tree": tree, "origin": origin} if tree else None)
    if not node_spec:
        return reference
    layer = _figma.find_child(tree, node_spec) if tree else None
    if layer is None:
        tops = ", ".join(str(child.get("name")) for child in ((tree or {}).get("children") or [])[:20])
        more = " Load another frame with figma {node: <id>} or {url}." if _FIGMA_NODE_ID.match(node_spec) else ""
        raise BrowserError(f"No layer {node_spec!r} in the Figma frame {name!r}." + (f" Its top layers: {tops}." if tops else "") + more)
    box = layer.get("absoluteBoundingBox") or {}
    image_w, image_h = _image_size(base.data)
    x0 = max(0.0, (float(box.get("x", 0)) - origin[0]) * scale)
    y0 = max(0.0, (float(box.get("y", 0)) - origin[1]) * scale)
    x1 = min(float(image_w or 1e9), (float(box.get("x", 0)) - origin[0] + float(box.get("width", 0))) * scale)
    y1 = min(float(image_h or 1e9), (float(box.get("y", 0)) - origin[1] + float(box.get("height", 0))) * scale)
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise BrowserError(f"The layer {layer.get('name')!r} lies outside the frame's render (or has no size).")
    reference.crop = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    reference.label = f"Figma {layer.get('name')}"
    reference.layer = {"id": layer.get("id"), "name": layer.get("name"), "type": layer.get("type")}
    return reference


def _figma_layer_at(figma_info: dict[str, Any], x: float, y: float) -> str:
    tree = figma_info.get("tree") or {}
    ox, oy = figma_info.get("origin") or (0.0, 0.0)
    px, py = ox + x, oy + y
    best: dict[str, Any] | None = None

    def walk(node: dict[str, Any], depth: int) -> None:
        nonlocal best
        if node.get("visible") is False or depth > 60:
            return
        box = node.get("absoluteBoundingBox") or {}
        if not box:
            return
        if not (box["x"] <= px <= box["x"] + box["width"] and box["y"] <= py <= box["y"] + box["height"]):
            return
        best = node
        for child in node.get("children") or []:
            walk(child, depth + 1)

    walk(tree, 0)
    if best is None or best is tree:
        return ""
    label = f"{best.get('name')} ({str(best.get('type') or '').lower()})"
    if best.get("type") == "TEXT" and best.get("characters"):
        label += ' "' + re.sub(r"\s+", " ", str(best["characters"]))[:40] + '"'
    return label + f" [{best.get('id')}]"


def _figma_in_browser(state_path: str, url: str, reason: str, agent: _Agent | None = None) -> dict[str, Any]:
    session = _session(state_path)
    tab_id = _worker.call(lambda: _tab_showing(session, url) if session.context is not None else "", timeout=20, session=session)
    if not tab_id:
        tab_id = str(perform(state_path, "new_tab", {"url": url, "background": True}, agent=agent).get("tab_id") or "")
    shot = perform(state_path, "screenshot", {"tab_id": tab_id}, agent=agent) if tab_id else {}
    return {
        "success": True,
        "action": "figma",
        "fallback": "browser",
        "reason": reason,
        "tab_id": tab_id,
        "url": shot.get("url") or url,
        "title": shot.get("title") or "",
        "shot_id": shot.get("shot_id"),
        "storage_key": shot.get("storage_key"),
        "shot_url": shot_url(shot.get("storage_key") or "", shot.get("shot_id") or "") if shot.get("shot_id") else "",
        "width": shot.get("width"),
        "height": shot.get("height"),
        "hint": (f"Figma's API is not available ({reason}), so the link is open in background tab {tab_id}, as any design "
                 f"tool's link would be. Find the design's frame on it (screenshot {{tab_id: {tab_id}}}; inspect it when the "
                 f"frame is an element) and compare with reference: tab:{tab_id} and reference_region, or crop it once and "
                 "use the crop's shot:<id>. If the page asks to sign in, ask the user for screenshots of the design (or to "
                 "attach their Chrome, or import their cookies)."),
    }


def _figma_action(state_path: str, args: dict[str, Any], session_id: str = "", agent: _Agent | None = None) -> dict[str, Any]:
    from livecode import figma as _figma

    url = str(args.get("url") or "").strip()
    node = str(args.get("node") or "").strip()
    refresh = bool(args.get("refresh"))
    if url and not _figma.is_figma_url(url):
        raise BrowserError("That is not a Figma link. For another tool's link, open it with new_tab and screenshot it.")
    session = _session(state_path)
    with _refs_lock:
        meta = _load_refs_index(session).get("figma") or None
    tree = _figma_tree(session, meta)
    loaded = False
    try:
        if url or meta is None or tree is None:
            if not url:
                raise BrowserError("Pass url: the Figma link to the frame (in Figma, right-click the frame -> Copy link to selection).")
            before = float((meta or {}).get("fetched_at") or 0)
            meta = load_figma_design(state_path, url, session_id=session_id, refresh=refresh)[1]
            loaded = float(meta.get("fetched_at") or 0) != before
        elif node and _FIGMA_NODE_ID.match(node) and _figma.find_child(tree, node) is None:
            meta = load_figma_design(state_path, "", node, session_id=session_id, refresh=refresh)[1]
            loaded = True
            node = ""
        else:
            meta = _touch_figma(session, session_id)
    except FigmaUnavailable as exc:
        if not url:
            raise
        return _figma_in_browser(state_path, url, _first_line(exc), agent)
    tree = _figma_tree(session, meta) or {}
    origin = tuple(float(v) for v in (meta.get("origin") or (0, 0)))
    layer = _figma.find_child(tree, node) if node else None
    if node and layer is None:
        _figma_reference(session, meta, node)
    reference = _figma_reference(session, meta, str(layer.get("id")) if layer else "")
    text, items = _figma.outline(layer or tree, max_depth=10, origin=origin)
    shot = perform(state_path, "crop", {}, reference=reference, agent=agent)
    width, height = int(round(float(meta.get("width") or 0))), int(round(float(meta.get("height") or 0)))
    out: dict[str, Any] = {
        "success": True,
        "action": "figma",
        "design": {"name": meta.get("name"), "id": meta.get("node_id"), "type": str(meta.get("type") or "").lower(),
                   "width": width, "height": height, "file_key": meta.get("file_key")},
        "scale": round(float(meta.get("scale") or 0), 3),
        "loaded": loaded,
        "outline": text,
        "layers": len(items),
        "reference": f"figma:{layer.get('id')}" if layer else "figma",
        "shot_id": shot.get("shot_id"),
        "storage_key": shot.get("storage_key"),
        "shot_url": shot_url(shot.get("storage_key") or "", shot.get("shot_id") or ""),
        "width": shot.get("width"),
        "height": shot.get("height"),
    }
    if not loaded:
        out["cached"] = True
    if layer:
        out["layer"] = _figma.node_details(layer, origin)
    viewport = session.viewport
    tips = ["Layer boxes are x,y width×height from the frame's top-left: the page's coordinates when the page is "
            f"{width} px wide."]
    if viewport.get("width") != width and not layer:
        tips.append(f"Resize the browser to {width} wide first (resize {{width: {width}, height: {min(max(height, 320), 1000)}}}).")
    tips.append('Compare the page with reference "figma" (the frame; full_page for all of it) and each element with its '
                'layer: compare {reference: "figma:<layer id>", selector: "…"}. Layers need no more API calls; Figma limits '
                "them, so load another frame only when you need it.")
    out["hint"] = " ".join(tips)
    return out


_COMPARE_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body { margin: 0; background: #111113; }
#board { display: inline-block; padding: 24px 26px 26px; background: #111113; color: #e4e4e4;
  font: 13px/18px -apple-system, "Segoe UI", system-ui, Ubuntu, sans-serif; }
.hdr { display: flex; align-items: flex-start; justify-content: space-between; gap: 28px; margin-bottom: 16px; padding: 18px 20px;
  background: #18181c; border: 1px solid #2a2a30; border-radius: 14px; }
.hdr .main { min-width: 0; flex: 1 1 auto; }
.verdict { display: inline-flex; align-items: center; gap: 7px; padding: 3px 11px 3px 9px; border-radius: 999px; font-size: 11px; line-height: 16px;
  font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; background: rgba(239, 68, 68, 0.14); color: #f87171; }
.verdict::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.verdict.nearly { background: rgba(86, 156, 214, 0.16); color: #7cb8ee; } .verdict.identical { background: rgba(34, 197, 94, 0.14); color: #4ade80; }
.title { margin: 10px 0 4px; color: #fff; font-size: 22px; line-height: 28px; font-weight: 650; letter-spacing: -0.015em; }
.route { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; color: #8e8e98; font-size: 12.5px; line-height: 18px; }
.route b { color: #d4d4da; font-weight: 500; } .route .arrow { color: #5c5c66; }
.note { margin-top: 12px; padding: 7px 11px; border-left: 3px solid #ff9f1a; border-radius: 0 8px 8px 0; background: rgba(255, 159, 26, 0.08);
  color: #e8d3b0; font-size: 12.5px; line-height: 18px; max-width: 720px; }
.counts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
.count { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px 3px 8px; border: 1px solid #2a2a30; border-radius: 999px;
  background: #111113; color: #c9c9d1; font-size: 12px; line-height: 16px; }
.count b { color: #fff; font-weight: 650; font-variant-numeric: tabular-nums; }
.count .swatch { margin: 0; }
.swatch.ok { background: #22c55e; } .swatch.nearly { background: #569cd6; }
.stats { display: flex; gap: 10px; flex: 0 0 auto; }
.stat { min-width: 124px; padding: 11px 14px 12px; background: #111113; border: 1px solid #2a2a30; border-radius: 11px; }
.stat .l { color: #8e8e98; font-size: 10.5px; line-height: 14px; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase; }
.stat .v { margin-top: 3px; color: #fff; font-size: 24px; line-height: 28px; font-weight: 650; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.stat .v small { margin-left: 2px; color: #8e8e98; font-size: 13px; font-weight: 500; letter-spacing: 0; }
.stat .n { margin-top: 2px; color: #8e8e98; font-size: 11px; line-height: 14px; }
.meter { position: relative; height: 5px; margin-top: 8px; border-radius: 3px; background: #2a2a30; }
.meter span { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px; background: #ef4444; }
.meter.nearly span { background: #569cd6; } .meter.identical span { background: #22c55e; }
.meter em { position: absolute; top: -3px; width: 2px; height: 11px; margin-left: -1px; border-radius: 1px; background: #e4e4e4; }
.fix { margin: 14px 0 0; padding: 12px 0 0; border-top: 1px solid #2a2a30; list-style: none; }
.fix h5 { margin: 0 0 6px; color: #8e8e98; font-size: 10.5px; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase; }
.fix li { display: flex; align-items: baseline; gap: 8px; color: #b9b9c2; font-size: 12.5px; line-height: 20px; }
.fix li b { flex: 0 0 auto; color: #fff; font-weight: 600; }
.fix li i { flex: 0 0 auto; min-width: 10px; padding: 0 4px; border-radius: 3px; background: #ef4444; color: #fff; text-align: center;
  font: 600 10px/15px -apple-system, "Segoe UI", system-ui, sans-serif; font-style: normal; }
.fix li i.s-missing { background: #a855f7; } .fix li i.s-lacking { background: #ff9f1a; color: #111; }
.cols { display: flex; align-items: flex-start; gap: 16px; }
figure { display: flex; flex-direction: column; gap: 12px; margin: 0; padding: 14px; background: #1b1b1f; border: 1px solid #2a2a30;
  border-radius: 14px; box-shadow: 0 1px 0 rgba(255, 255, 255, 0.03) inset; }
figcaption { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; min-height: 22px; color: #8e8e98; font-size: 12px; word-break: break-word; }
.tag { padding: 2px 9px; border-radius: 999px; font-size: 11px; line-height: 16px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase;
  background: rgba(255, 255, 255, 0.08); color: #dcdce2; }
.tag.design { background: rgba(86, 156, 214, 0.18); color: #86bdf0; }
.tag.diff { background: rgba(255, 45, 140, 0.16); color: #ff77b4; }
.chip { display: inline-flex; align-items: center; color: #a6a6b0; }
.frame { align-self: start; }
.frame { position: relative; }
canvas { display: block; border: 1px solid #2c2c31; border-radius: 8px; background: #fff; box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4); }
.box { position: absolute; border: 1.5px solid #ff2d8c; box-sizing: border-box; }
.box.k-size { border-color: #ff9f1a; } .box.k-missing { border-color: #ef4444; } .box.k-extra { border-color: #a855f7; }
.box.k-moved { border-color: #38bdf8; } .box.k-color { border-color: #f5b400; } .box.k-edges { border-color: #8a8a93; }
.box.minor { border-style: dashed; opacity: 0.6; }
.box i { position: absolute; left: -1.5px; top: -16px; min-width: 10px; padding: 0 3px; height: 15px; border-radius: 3px 3px 3px 0;
  background: #ff2d8c; color: #fff; font: 600 10px/15px -apple-system, "Segoe UI", system-ui, sans-serif; font-style: normal; text-align: center; }
.box.k-size i { background: #ff9f1a; } .box.k-missing i { background: #ef4444; } .box.k-extra i { background: #a855f7; }
.box.k-moved i { background: #38bdf8; } .box.k-color i { background: #f5b400; color: #111; } .box.k-edges i { background: #8a8a93; }
.box.low i { top: auto; bottom: -16px; border-radius: 0 3px 3px 3px; }
.swatch { display: inline-block; width: 9px; height: 9px; margin: 0 5px 0 0; border-radius: 3px; background: #ff2d8c; }
.swatch.k-size { background: #ff9f1a; } .swatch.k-missing { background: #ef4444; } .swatch.k-extra { background: #a855f7; }
.swatch.k-moved { background: #38bdf8; } .swatch.k-color { background: #f5b400; }
.swatch.aa { background: #6e6028; }
.box.s-different { border-color: #ef4444; } .box.s-missing { border-color: #a855f7; border-style: dashed; }
.box.s-lacking { border-color: #ff9f1a; border-style: dashed; }
.box.s-different i { background: #ef4444; } .box.s-missing i { background: #a855f7; } .box.s-lacking i { background: #ff9f1a; color: #111; }
.swatch.s-different { background: #ef4444; } .swatch.s-missing { background: #a855f7; } .swatch.s-lacking { background: #ff9f1a; }
.cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
.card { padding: 12px; background: #1b1b1f; border: 1px solid #2a2a30; border-radius: 12px; }
.card h4 { margin: 0 0 6px; color: #fff; font-size: 12px; line-height: 16px; font-weight: 600; word-break: break-word; }
.card h4 i { display: inline-block; min-width: 10px; margin-right: 6px; padding: 0 4px; border-radius: 3px; background: #ef4444;
  color: #fff; font: 600 10px/15px -apple-system, "Segoe UI", system-ui, sans-serif; font-style: normal; text-align: center; }
.card h4 i.s-missing { background: #a855f7; }
.card ul { margin: 0 0 10px; padding-left: 15px; color: #b9b9c2; font-size: 11px; line-height: 15px; }
.pair { display: flex; gap: 10px; align-items: flex-start; }
.pair div { display: flex; flex-direction: column; gap: 4px; color: #8e8e98; font-size: 10px; letter-spacing: 0.04em; text-transform: uppercase; }
.pair canvas { border-radius: 4px; box-shadow: none; }
</style></head><body><div id="board"></div><script>
const load = (src, what) => new Promise((resolve, reject) => {
  const img = new Image();
  img.onload = () => resolve(img);
  img.onerror = () => reject(new Error("The " + what + " image could not be read."));
  img.src = src;
});
const make = (w, h) => { const c = document.createElement("canvas"); c.width = w; c.height = h; return c; };
const r1 = (v) => Math.round(v * 10) / 10;
// Colour as the eye sees it: CIE L*a*b* (D65), and the CIE94 and CIEDE2000 colour differences,
// where a difference of about 2.3 is the smallest most people notice.
const LAB = new Map();
const lin = (c) => { c /= 255; return c > 0.04045 ? Math.pow((c + 0.055) / 1.055, 2.4) : c / 12.92; };
const labOf = (r, g, b) => {
  const key = (r << 16) | (g << 8) | b;
  let v = LAB.get(key);
  if (v) return v;
  const R = lin(r), G = lin(g), B = lin(b);
  const f = (t) => t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 * t + 16) / 116;
  const fx = f((R * 0.4124564 + G * 0.3575761 + B * 0.1804375) / 0.95047);
  const fy = f(R * 0.2126729 + G * 0.7151522 + B * 0.0721750);
  const fz = f((R * 0.0193339 + G * 0.1191920 + B * 0.9503041) / 1.08883);
  v = [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
  if (LAB.size < 262144) LAB.set(key, v);
  return v;
};
const labPlane = (P, n) => {
  const out = new Float32Array(n * 3);
  for (let p = 0, i = 0, j = 0; p < n; p++, i += 4, j += 3) {
    const v = labOf(P[i], P[i + 1], P[i + 2]);
    out[j] = v[0]; out[j + 1] = v[1]; out[j + 2] = v[2];
  }
  return out;
};
const dE94 = (X, i, Y, j) => {
  const dL = X[i] - Y[j], a1 = X[i + 1], b1 = X[i + 2], a2 = Y[j + 1], b2 = Y[j + 2];
  const c1 = Math.sqrt(a1 * a1 + b1 * b1), c2 = Math.sqrt(a2 * a2 + b2 * b2), dC = c1 - c2;
  const da = a1 - a2, db = b1 - b2, dH2 = Math.max(0, da * da + db * db - dC * dC);
  const sC = 1 + 0.045 * c1, sH = 1 + 0.015 * c1;
  return Math.sqrt(dL * dL + (dC / sC) * (dC / sC) + dH2 / (sH * sH));
};
const dE2000 = (x, y) => {
  // Sharma, Wu and Dalal (2005).
  const [L1, a1, b1] = x, [L2, a2, b2] = y;
  const C1 = Math.hypot(a1, b1), C2 = Math.hypot(a2, b2), Cm = (C1 + C2) / 2;
  const G = 0.5 * (1 - Math.sqrt(Math.pow(Cm, 7) / (Math.pow(Cm, 7) + Math.pow(25, 7))));
  const a1p = (1 + G) * a1, a2p = (1 + G) * a2, C1p = Math.hypot(a1p, b1), C2p = Math.hypot(a2p, b2);
  const hue = (b, a) => { if (!a && !b) return 0; const t = Math.atan2(b, a) * 180 / Math.PI; return t < 0 ? t + 360 : t; };
  const h1p = hue(b1, a1p), h2p = hue(b2, a2p), dLp = L2 - L1, dCp = C2p - C1p;
  let dhp = 0;
  if (C1p * C2p) { dhp = h2p - h1p; if (dhp > 180) dhp -= 360; else if (dhp < -180) dhp += 360; }
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin(dhp * Math.PI / 360);
  const Lm = (L1 + L2) / 2, Cmp = (C1p + C2p) / 2;
  let hm = h1p + h2p;
  if (C1p * C2p) { if (Math.abs(h1p - h2p) > 180) hm += h1p + h2p < 360 ? 360 : -360; hm /= 2; }
  const T = 1 - 0.17 * Math.cos((hm - 30) * Math.PI / 180) + 0.24 * Math.cos(2 * hm * Math.PI / 180)
    + 0.32 * Math.cos((3 * hm + 6) * Math.PI / 180) - 0.2 * Math.cos((4 * hm - 63) * Math.PI / 180);
  const dTheta = 30 * Math.exp(-Math.pow((hm - 275) / 25, 2));
  const Rc = 2 * Math.sqrt(Math.pow(Cmp, 7) / (Math.pow(Cmp, 7) + Math.pow(25, 7)));
  const Sl = 1 + 0.015 * Math.pow(Lm - 50, 2) / Math.sqrt(20 + Math.pow(Lm - 50, 2));
  const Sc = 1 + 0.045 * Cmp, Sh = 1 + 0.015 * Cmp * T, Rt = -Math.sin(2 * dTheta * Math.PI / 180) * Rc;
  return Math.sqrt(Math.pow(dLp / Sl, 2) + Math.pow(dCp / Sc, 2) + Math.pow(dHp / Sh, 2) + Rt * (dCp / Sc) * (dHp / Sh));
};
const hex = (c) => "#" + c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();

window.cropImage = async function (o) {
  const img = await load(o.src, "design");
  const IW = img.naturalWidth || 1, IH = img.naturalHeight || 1;
  const c = o.crop || { x: 0, y: 0, width: IW, height: IH };
  const f = Math.min(o.maxWidth / c.width, o.maxHeight / c.height, o.upscale && c.width < 240 ? 2 : 1);
  const canvas = make(Math.max(1, Math.round(c.width * f)), Math.max(1, Math.round(c.height * f)));
  const ctx = canvas.getContext("2d");
  if (o.format !== "image/png") {
    ctx.fillStyle = "#ffffff";  // JPEG has no transparency
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, c.x, c.y, c.width, c.height, 0, 0, canvas.width, canvas.height);
  const format = o.format === "image/png" ? "image/png" : "image/jpeg";
  return { data: canvas.toDataURL(format, 0.92), width: canvas.width, height: canvas.height, image: [IW, IH], factor: canvas.width / c.width };
};

// The design lined up with the page: which part of the design image, at what scale, and both
// drawn in the page's CSS pixels (the design over the page, so its transparency shows the page).
const prepare = (o, ref, cur, readable) => {
  const PW = cur.naturalWidth, PH = cur.naturalHeight;
  const IW = ref.naturalWidth || PW, IH = ref.naturalHeight || PH;
  const element = o.mode === "element";
  let crop = o.refCrop ? Object.assign({}, o.refCrop) : null;
  let scale = o.refScale || 0, sameRegion = false;
  // A design of the whole page (1x-4x its width): compare with the same region of it.
  if (!crop && o.autoRegion && o.region && o.layoutWidth) {
    const k = scale || Math.round(IW / o.layoutWidth);
    if (k > 0 && (scale || k <= 4) && Math.abs(IW / k - o.layoutWidth) <= 2) {
      const r = o.region;
      if (r.x * k < IW - 1 && r.y * k < IH - 1) {
        crop = { x: r.x * k, y: r.y * k, width: Math.min(r.width * k, IW - r.x * k), height: Math.min(r.height * k, IH - r.y * k) };
        scale = k; sameRegion = true;
      }
    }
  }
  const whole = !crop || (crop.x < 1 && crop.y < 1 && crop.width > IW - 1 && crop.height > IH - 1);
  if (!crop) crop = { x: 0, y: 0, width: IW, height: IH };
  // The design's size in the page's CSS pixels.
  let refW, refH, fitted = false;
  if (element) {
    if (!scale) {
      // An image of just this element: the export scale (1x-4x) that fits its size.
      let best = 0, bestErr = Infinity;
      for (let k = 1; k <= 4; k++) {
        const err = Math.abs(crop.width / k - PW) / PW + Math.abs(crop.height / k - PH) / PH;
        if (err < bestErr) { bestErr = err; best = k; }
      }
      if (bestErr <= 0.35) scale = best;
    }
    if (scale) { refW = crop.width / scale; refH = crop.height / scale; }
    else { fitted = true; refW = PW; refH = crop.height * PW / crop.width; }
  } else {
    // The view or the whole page: the design is lined up with the page's width.
    refW = PW; refH = crop.height * PW / crop.width;
    fitted = !scale || Math.abs(crop.width / scale - PW) > 2;
  }
  refW = Math.max(1, Math.round(refW)); refH = Math.max(1, Math.round(refH));
  // An area cut out of a screenshot by eye can be a pixel or two off, and the page's top
  // must meet the design's top: line the two up there before comparing.
  let snapped = null;
  if (o.snap && !element && PH >= 24 && refH >= 24) {
    const k = crop.width / refW, pad = 8, bw = PW, bh = Math.min(160, PH, refH), rw2 = bw + 2 * pad;
    const pc = make(bw, bh).getContext("2d", { willReadFrequently: true });
    pc.drawImage(cur, 0, 0, bw, bh, 0, 0, bw, bh);
    const rc2 = make(rw2, bh + 2 * pad).getContext("2d", { willReadFrequently: true });
    rc2.imageSmoothingQuality = "high";
    rc2.drawImage(ref, crop.x - pad * k, crop.y - pad * k, rw2 * k, (bh + 2 * pad) * k, 0, 0, rw2, bh + 2 * pad);
    const P = pc.getImageData(0, 0, bw, bh).data, Q = rc2.getImageData(0, 0, rw2, bh + 2 * pad).data;
    const lum = (D, i) => 0.299 * D[i] + 0.587 * D[i + 1] + 0.114 * D[i + 2];
    const err = (dx, dy) => {
      let sum = 0, n = 0;
      for (let y = 0; y < bh; y += 2) {
        for (let x = 0; x < bw; x += 3) {
          const j = ((y + pad + dy) * rw2 + (x + pad + dx)) * 4;
          if (Q[j + 3] < 128) continue;
          sum += Math.abs(lum(P, (y * bw + x) * 4) - lum(Q, j)); n++;
        }
      }
      return n > 64 ? sum / n : Infinity;
    };
    const e0 = err(0, 0);
    let bx = 0, by = 0, be = e0;
    for (let dy = -pad; dy <= pad; dy++) for (let dx = -pad; dx <= pad; dx++) {
      const e = err(dx, dy);
      if (e < be - 1e-6) { be = e; bx = dx; by = dy; }
    }
    if ((bx || by) && be < e0 * 0.7) {
      crop = Object.assign({}, crop, { x: crop.x + bx * k, y: crop.y + by * k });
      snapped = { x: bx, y: by };
    }
  }
  const CW = element ? Math.max(PW, refW) : PW, CH = element ? Math.max(PH, refH) : Math.min(PH, refH);
  // Both go through a canvas of their own size first: scaling a canvas and scaling an
  // image are filtered differently, which would show as differences between equal pictures.
  const opts = readable ? { willReadFrequently: true } : undefined;
  const pageCanvas = make(PW, PH);
  pageCanvas.getContext("2d", opts).drawImage(cur, 0, 0);
  // The design drawn over the page, so a transparent design shows the page's own background.
  const refCanvas = make(refW, refH), rc = refCanvas.getContext("2d", opts);
  rc.drawImage(pageCanvas, 0, 0);
  rc.imageSmoothingQuality = "high";
  rc.drawImage(ref, crop.x, crop.y, crop.width, crop.height, 0, 0, refW, refH);
  return { PW, PH, IW, IH, element, crop, scale, sameRegion, whole, refW, refH, fitted, snapped, CW, CH, pageCanvas, refCanvas };
};

// The board's header: the verdict and what was compared with what, the numbers that matter, the
// counts by kind (the colours of the boxes below) and, when given, what to fix first.
const boardHeader = (h) => {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const hdr = el("div", "hdr"), main = el("div", "main");
  main.append(el("span", "verdict " + h.tone, h.verdict), el("div", "title", h.title));
  const route = el("div", "route");
  route.append(el("b", "", "Design"), el("span", "", h.design), el("span", "arrow", "→"), el("b", "", "Page"), el("span", "", h.page));
  main.append(route);
  if (h.counts && h.counts.length) {
    const counts = el("div", "counts");
    for (const [cls, value, label] of h.counts) {
      const c = el("span", "count");
      c.append(el("span", "swatch " + cls));
      if (value !== null) c.append(el("b", "", String(value)));
      c.append(label);
      counts.append(c);
    }
    main.append(counts);
  }
  if (h.note) main.append(el("div", "note", h.note));
  if (h.fix && h.fix.length) {
    const fix = el("ul", "fix");
    fix.append(el("h5", "", h.fixTitle || "Fix first"));
    for (const f of h.fix) {
      const li = el("li");
      li.append(el("i", f.cls ? "s-" + f.cls : "", String(f.n)), el("b", "", f.name), el("span", "", f.text));
      fix.append(li);
    }
    main.append(fix);
  }
  const stats = el("div", "stats");
  for (const t of h.stats || []) {
    const st = el("div", "stat");
    st.append(el("div", "l", t.label));
    const v = el("div", "v", String(t.value));
    if (t.unit) v.append(el("small", "", t.unit));
    st.append(v);
    if (t.meter !== undefined) {
      const m = el("div", "meter " + h.tone), bar = el("span");
      bar.style.width = Math.max(0, Math.min(100, t.meter)) + "%";
      m.append(bar);
      if (t.mark !== undefined) { const tick = el("em"); tick.style.left = Math.max(0, Math.min(100, t.mark)) + "%"; m.append(tick); }
      st.append(m);
    }
    if (t.note) st.append(el("div", "n", t.note));
    stats.append(st);
  }
  hdr.append(main, stats);
  return hdr;
};

window.renderCompare = async function (o) {
  const ref = await load(o.ref, "reference");
  const cur = await load(o.page, "page");
  const { PW, PH, IW, IH, element, crop, scale, sameRegion, whole, refW, refH, fitted, snapped, CW, CH, pageCanvas, refCanvas } =
    prepare(o, ref, cur, false);
  // Compare at most 720 pixels wide: text drawn at another scale then differs only in
  // anti-aliasing, which the averaging removes, and real changes stay visible.
  const s = Math.min(1, 720 / CW, Math.sqrt(2e6 / (CW * CH)));
  const cw = Math.max(1, Math.round(CW * s)), ch = Math.max(1, Math.round(CH * s));
  const rw = Math.min(CW, refW), rh = Math.min(CH, refH), pw = Math.min(CW, PW), ph = Math.min(CH, PH);
  const scaled = (src, w, h) => {
    const x = make(cw, ch).getContext("2d", { willReadFrequently: true });
    x.imageSmoothingQuality = "high";
    x.drawImage(src, 0, 0, w, h, 0, 0, w * s, h * s);
    return x;
  };
  const ca = scaled(refCanvas, rw, rh), cb = scaled(pageCanvas, pw, ph);
  const A = ca.getImageData(0, 0, cw, ch).data, B = cb.getImageData(0, 0, cw, ch).data;
  const N = cw * ch;
  const diff = make(cw, ch), dctx = diff.getContext("2d");
  const out = dctx.createImageData(cw, ch), D = out.data;
  // M: 1 a difference, 2 only one of the images covers the pixel (their sizes differ).
  // I: bit 1 the design shows something other than its backdrop there, bit 2 the page does.
  const M = new Uint8Array(N), I = new Uint8Array(N);
  const XA = labPlane(A, N), XB = labPlane(B, N);
  const LA = new Float32Array(N), LB = new Float32Array(N);
  for (let p = 0; p < N; p++) { LA[p] = XA[p * 3]; LB[p] = XB[p * 3]; }
  // The perceived colour distance (CIE94): a change the eye barely sees stays below the limits.
  // Inside flat areas (a background, a button, an icon's fill) no renderer adds noise, so a
  // smaller change counts there: a slightly different tint is a design difference.
  const limit = o.deltaE, flatLimit = o.flatDeltaE;
  const de = (X, p, Y, q) => dE94(X, p * 3, Y, q * 3);
  // A pixel differs only when no pixel next to it in the other image matches it (both
  // ways), so a one-pixel shift or a softer edge is not a difference.
  const unmatched = (X, Y, x, y, p, lim) => {
    for (let dy = -1; dy <= 1; dy++) {
      const yy = y + dy;
      if (yy < 0 || yy >= ch) continue;
      for (let dx = -1; dx <= 1; dx++) {
        const xx = x + dx;
        if (xx < 0 || xx >= cw) continue;
        if ((dx || dy) && de(X, p, Y, yy * cw + xx) <= lim) return false;
      }
    }
    return true;
  };
  // Anti-aliasing (pixelmatch's test): an edge pixel between a darker and a brighter
  // neighbour that sit in flat areas of both images. Text drawn by another renderer
  // (a design tool, another scale) differs there, and that is not a design difference.
  const bright = (P, i) => 0.29889531 * P[i] + 0.58662247 * P[i + 1] + 0.11448223 * P[i + 2];
  const flat = (P, x1, y1) => {
    const x0 = Math.max(x1 - 1, 0), y0 = Math.max(y1 - 1, 0), x2 = Math.min(x1 + 1, cw - 1), y2 = Math.min(y1 + 1, ch - 1);
    const pos = (y1 * cw + x1) * 4;
    let same = x1 === x0 || x1 === x2 || y1 === y0 || y1 === y2 ? 1 : 0;
    for (let x = x0; x <= x2; x++) for (let y = y0; y <= y2; y++) {
      if (x === x1 && y === y1) continue;
      const q = (y * cw + x) * 4;
      if (P[pos] === P[q] && P[pos + 1] === P[q + 1] && P[pos + 2] === P[q + 2]) same++;
      if (same > 2) return true;
    }
    return false;
  };
  const antialiased = (P, x1, y1, Q) => {
    const x0 = Math.max(x1 - 1, 0), y0 = Math.max(y1 - 1, 0), x2 = Math.min(x1 + 1, cw - 1), y2 = Math.min(y1 + 1, ch - 1);
    const b0 = bright(P, (y1 * cw + x1) * 4);
    let same = x1 === x0 || x1 === x2 || y1 === y0 || y1 === y2 ? 1 : 0;
    let min = 0, max = 0, minX = 0, minY = 0, maxX = 0, maxY = 0;
    for (let x = x0; x <= x2; x++) for (let y = y0; y <= y2; y++) {
      if (x === x1 && y === y1) continue;
      const d = b0 - bright(P, (y * cw + x) * 4);
      if (d === 0) { same++; if (same > 2) return false; }
      else if (d < min) { min = d; minX = x; minY = y; }
      else if (d > max) { max = d; maxX = x; maxY = y; }
    }
    if (min === 0 || max === 0) return false;
    return (flat(P, minX, minY) && flat(Q, minX, minY)) || (flat(P, maxX, maxY) && flat(Q, maxX, maxY));
  };
  const smooth = (X, x1, y1, p) => {
    for (let y = Math.max(0, y1 - 1); y <= Math.min(ch - 1, y1 + 1); y++) {
      for (let x = Math.max(0, x1 - 1); x <= Math.min(cw - 1, x1 + 1); x++) {
        if (de(X, p, X, y * cw + x) > o.smoothDeltaE) return false;
      }
    }
    return true;
  };
  // A flood fill (the pixel and its eight neighbours alike) is never anti-aliasing.
  const still = (P, x1, y1) => {
    const i = (y1 * cw + x1) * 4;
    for (let y = Math.max(0, y1 - 1); y <= Math.min(ch - 1, y1 + 1); y++) {
      for (let x = Math.max(0, x1 - 1); x <= Math.min(cw - 1, x1 + 1); x++) {
        const j = (y * cw + x) * 4;
        if (P[i] !== P[j] || P[i + 1] !== P[j + 1] || P[i + 2] !== P[j + 2]) return false;
      }
    }
    return true;
  };
  // Structural similarity (SSIM) of the lightness around a pixel: another renderer's text and
  // edges keep their structure (it stays near 1), a changed word, icon or shape does not.
  const ssim = (xa, ya, xb, yb, skipSize) => {
    let n = 0, sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0;
    for (let y = ya; y <= yb; y++) for (let x = xa, p = y * cw + xa; x <= xb; x++, p++) {
      if (skipSize && M[p] === 2) continue;
      const u = LA[p], v = LB[p];
      n++; sa += u; sb += v; saa += u * u; sbb += v * v; sab += u * v;
    }
    if (!n) return 1;
    const ma = sa / n, mb = sb / n, va = saa / n - ma * ma, vb = sbb / n - mb * mb, cov = sab / n - ma * mb;
    return ((2 * ma * mb + 1) * (2 * cov + 9)) / ((ma * ma + mb * mb + 1) * (va + vb + 9));
  };
  const ssimAt = (x, y, r) => ssim(Math.max(0, x - r), Math.max(0, y - r), Math.min(cw - 1, x + r), Math.min(ch - 1, y + r), false);
  const backdrop = (P) => {
    const counts = new Map();
    let best = 0, at = 0;
    for (let p = 0; p < N; p += 7) {
      const i = p * 4;
      if (P[i + 3] < 128) continue;
      const key = ((P[i] >> 4) << 8) | ((P[i + 1] >> 4) << 4) | (P[i + 2] >> 4);
      const n = (counts.get(key) || 0) + 1;
      counts.set(key, n);
      if (n > best) { best = n; at = i; }
    }
    return labOf(P[at], P[at + 1], P[at + 2]);
  };
  const bgA = backdrop(A), bgB = backdrop(B);
  let differ = 0, missing = 0, content = 0, same = 0;
  for (let y = 0; y < ch; y++) {
    for (let x = 0; x < cw; x++) {
      const p = y * cw + x, i = p * 4;
      const ma = A[i + 3] < 128, mb = B[i + 3] < 128;
      if (ma || mb) {
        // Only one of them reaches here: their sizes differ.
        if (ma !== mb) { missing++; content++; M[p] = 2; D[i] = 255; D[i + 1] = 159; D[i + 2] = 26; }
        else { D[i] = 24; D[i + 1] = 24; D[i + 2] = 24; }
        D[i + 3] = 255;
        continue;
      }
      const d = A[i] === B[i] && A[i + 1] === B[i + 1] && A[i + 2] === B[i + 2] ? 0 : de(XA, p, XB, p);
      const ink = (dE94(XA, p * 3, bgA, 0) > flatLimit ? 1 : 0) | (dE94(XB, p * 3, bgB, 0) > flatLimit ? 2 : 0);
      I[p] = ink;
      let kind = 0;
      if (d > flatLimit && (d > limit || (smooth(XA, x, y, p) && smooth(XB, x, y, p))) &&
          unmatched(XA, XB, x, y, p, d > limit ? limit : flatLimit) && unmatched(XB, XA, x, y, p, d > limit ? limit : flatLimit)) {
        const noise = antialiased(A, x, y, B) || antialiased(B, x, y, A) ||
          (!still(A, x, y) && !still(B, x, y) && ssimAt(x, y, o.ssimRadius) >= o.ssimNoise);
        kind = noise ? 3 : 1;
      }
      if (ink || kind === 1) {
        content++;
        if (d <= flatLimit || kind === 3) same++;
      }
      if (kind === 1) { differ++; M[p] = 1; D[i] = 255; D[i + 1] = 45; D[i + 2] = 140; }
      else if (kind === 3) { D[i] = 110; D[i + 1] = 96; D[i + 2] = 40; }
      else { const g = 30 + 0.3 * (0.3 * B[i] + 0.59 * B[i + 1] + 0.11 * B[i + 2]); D[i] = g; D[i + 1] = g; D[i + 2] = g; }
      D[i + 3] = 255;
    }
  }
  dctx.putImageData(out, 0, 0);
  const bad = differ + missing;
  const contentPct = content ? 100 * same / content : 100;
  const matchPct = contentPct >= 100 ? 100 : Math.min(99.9, Math.floor(contentPct * 10) / 10);
  // How alike the two are in structure: SSIM over 8-pixel blocks that hold something. It stays
  // high when only colours or rendering differ, and drops when layout or content does.
  let ssimSum = 0, ssimBlocks = 0;
  for (let by = 0; by + 8 <= ch; by += 8) for (let bx = 0; bx + 8 <= cw; bx += 8) {
    let busyBlock = false;
    for (let y = by; y < by + 8 && !busyBlock; y++) for (let x = bx; x < bx + 8; x++) if (I[y * cw + x] || M[y * cw + x]) { busyBlock = true; break; }
    if (!busyBlock) continue;
    ssimSum += ssim(bx, by, bx + 7, by + 7, true);
    ssimBlocks++;
  }
  const structure = ssimBlocks ? Math.max(0, Math.min(100, Math.round(1000 * ssimSum / ssimBlocks) / 10)) : 100;
  // How far apart the two images' lightness is where they do match (compression, rendering):
  // content lined up again after a move differs by about this much.
  let noiseSum = 0, noiseN = 0;
  for (let p = 0; p < N; p += 3) if (!M[p] && I[p]) { noiseSum += Math.abs(LA[p] - LB[p]); noiseN++; }
  const noiseL = noiseN ? noiseSum / noiseN : 0;
  const lum = (P) => { const L = new Float32Array(cw * ch); for (let p = 0, i = 0; p < L.length; p++, i += 4) L[p] = 0.299 * P[i] + 0.587 * P[i + 1] + 0.114 * P[i + 2]; return L; };
  // An element whose content is only moved: the shift that lines it up with the design.
  let offset = null;
  if (element && bad && cw * ch <= 400000) {
    const ow = Math.round(Math.min(PW, refW) * s), oh = Math.round(Math.min(PH, refH) * s);
    const La = lum(A), Lb = lum(B);
    const R = Math.min(12, Math.floor(Math.min(ow, oh) / 3));
    const err = (dx, dy) => {
      let sum = 0, n = 0;
      const x0 = Math.max(0, -dx), x1 = Math.min(ow, ow - dx), y0 = Math.max(0, -dy), y1 = Math.min(oh, oh - dy);
      for (let y = y0; y < y1; y += 2) {
        const ra = y * cw, rb = (y + dy) * cw + dx;
        for (let x = x0; x < x1; x += 2) { sum += Math.abs(La[ra + x] - Lb[rb + x]); n++; }
      }
      return n > 24 ? sum / n : Infinity;
    };
    const e0 = err(0, 0);
    let bx = 0, by = 0, be = e0;
    for (let dy = -R; dy <= R; dy++) for (let dx = -R; dx <= R; dx++) {
      if (!dx && !dy) continue;
      const e = err(dx, dy);
      if (e < be) { be = e; bx = dx; by = dy; }
    }
    // Only a clear, real move: content that differs (a changed word) also finds a "better" shift.
    // Text: renderers place glyphs a pixel or two apart, so only 3 px or more is a move.
    const moved = { x: Math.round(bx / s), y: Math.round(by / s) }, least = o.textTarget ? 3 : 2;
    if ((Math.abs(moved.x) >= least || Math.abs(moved.y) >= least) && e0 > 1 && be < e0 * 0.45) offset = moved;
  }
  // The view or the page: bands of it that sit higher or lower than in the design (a
  // margin or a height above them differs), found by lining up each band.
  const shifts = [];
  if (!element && CH >= 32) {
    // At (up to) full resolution, so a 2 px move is measured as 2 px, and 1 px (how
    // renderers snap text to the pixel grid) is ignored.
    const sb = Math.min(1, Math.sqrt(4e6 / (CW * CH)));
    const bw = Math.max(1, Math.round(CW * sb)), bh = Math.max(1, Math.round(CH * sb));
    const grab = (src) => {
      const x = make(bw, bh).getContext("2d", { willReadFrequently: true });
      x.drawImage(src, 0, 0, CW, CH, 0, 0, bw, bh);
      const P = x.getImageData(0, 0, bw, bh).data, L = new Float32Array(bw * bh);
      for (let p = 0, i = 0; p < L.length; p++, i += 4) L[p] = 0.299 * P[i] + 0.587 * P[i + 1] + 0.114 * P[i + 2];
      return L;
    };
    const La = grab(refCanvas), Lb = grab(pageCanvas);
    const BAND = Math.max(24, Math.round(80 * sb)), R = Math.max(4, Math.round(40 * sb)), step = Math.max(1, Math.round(2 * sb));
    const bands = [];
    for (let y0 = 0; y0 < bh; y0 += BAND) {
      const y1 = Math.min(bh, y0 + BAND);
      let lo = 255, hi = 0;
      for (let y = y0; y < y1; y += 2) for (let x = 0; x < bw; x += 3) { const v = La[y * bw + x]; if (v < lo) lo = v; if (v > hi) hi = v; }
      if (hi - lo < 24 || y1 - y0 < 8) { bands.push(null); continue; }  // a plain band says nothing about position
      const err = (dy) => {
        let sum = 0, n = 0;
        for (let y = y0; y < y1; y += step) {
          const yb = y + dy;
          if (yb < 0 || yb >= bh) continue;
          const ra = y * bw, rb = yb * bw;
          for (let x = 0; x < bw; x += 2) { sum += Math.abs(La[ra + x] - Lb[rb + x]); n++; }
        }
        return n > 32 ? sum / n : Infinity;
      };
      const e0 = err(0);
      let best = 0, be = e0;
      for (let dy = -R; dy <= R; dy++) { if (!dy) continue; const e = err(dy); if (e < be - 1e-6) { be = e; best = dy; } }
      const dy = Math.round(best / sb);
      bands.push(Math.abs(dy) >= 2 && e0 > 0.8 && be < e0 * 0.5 ? dy : 0);
    }
    let run = null;
    // One band a couple of pixels off is how renderers place text; a real move shows
    // in several bands, or is larger.
    const flush = () => {
      if (run && run.dy && (run.bands > 1 || Math.abs(run.dy) >= 3)) {
        shifts.push({ from_y: Math.round(run.start / sb), to_y: Math.min(CH, Math.round(run.end / sb)), dy: run.dy });
      }
      run = null;
    };
    bands.forEach((dy, index) => {
      const start = index * BAND, end = Math.min(bh, start + BAND);
      if (dy === null) { if (run) run.end = end; return; }
      if (run && Math.abs(run.dy - dy) <= 1) { run.end = end; run.bands++; return; }
      flush();
      run = { dy: dy, start: start, end: end, bands: 1 };
    });
    flush();
  }
  // The differing areas: differing pixels grouped in 8-pixel cells, touching cells joined.
  const CELL = 8, gw = Math.ceil(cw / CELL), gh = Math.ceil(ch / CELL), G = gw * gh;
  const cnt = new Uint32Array(G), miss = new Uint32Array(G);
  const gx0 = new Int32Array(G).fill(1 << 30), gy0 = new Int32Array(G).fill(1 << 30), gx1 = new Int32Array(G).fill(-1), gy1 = new Int32Array(G).fill(-1);
  for (let y = 0; y < ch; y++) {
    for (let x = 0; x < cw; x++) {
      const m = M[y * cw + x];
      if (!m) continue;
      const g = Math.floor(y / CELL) * gw + Math.floor(x / CELL);
      cnt[g]++; if (m === 2) miss[g]++;
      if (x < gx0[g]) gx0[g] = x; if (x > gx1[g]) gx1[g] = x;
      if (y < gy0[g]) gy0[g] = y; if (y > gy1[g]) gy1[g] = y;
    }
  }
  const seen = new Uint8Array(G), groups = [];
  for (let g0 = 0; g0 < G; g0++) {
    if (seen[g0] || cnt[g0] < 2) continue;
    const stack = [g0];
    seen[g0] = 1;
    const a = { count: 0, missing: 0, x0: 1 << 30, y0: 1 << 30, x1: -1, y1: -1 };
    while (stack.length) {
      const g = stack.pop();
      a.count += cnt[g]; a.missing += miss[g];
      a.x0 = Math.min(a.x0, gx0[g]); a.y0 = Math.min(a.y0, gy0[g]); a.x1 = Math.max(a.x1, gx1[g]); a.y1 = Math.max(a.y1, gy1[g]);
      const cx = g % gw, cy = (g - cx) / gw;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const nx = cx + dx, ny = cy + dy;
        if (nx < 0 || ny < 0 || nx >= gw || ny >= gh) continue;
        const ng = ny * gw + nx;
        if (!seen[ng] && cnt[ng] >= 2) { seen[ng] = 1; stack.push(ng); }
      }
    }
    groups.push(a);
  }
  groups.sort((p, q) => q.count - p.count);
  const big = groups.filter((a) => a.count >= 6);
  // A small, sparse area is how two renderers differ on the same text or edge; a dense
  // or large one is a change (another colour, word, size or position).
  const minor = (a) => {
    const px = a.count / (s * s), density = a.count / ((a.x1 - a.x0 + 1) * (a.y1 - a.y0 + 1));
    return a.missing * 2 <= a.count && (px <= o.noisePixels || (px <= o.nearlyPixels && density < o.noiseDensity));
  };
  const ranked = (big.length ? big : groups).map((a) => Object.assign(a, { minor: minor(a) }))
    .sort((p, q) => (p.minor - q.minor) || (q.count - p.count));
  // What an area is: something the page lacks or adds, something moved, a recolour, or other content.
  const quant = (P, i) => ((P[i] >> 3) << 10) | ((P[i + 1] >> 3) << 5) | (P[i + 2] >> 3);
  const dominant = (P, x0, y0, x1, y1) => {
    const counts = new Map();
    let best = -1, bestN = 0;
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const p = y * cw + x;
      if (M[p] !== 1) continue;
      const i = p * 4, key = quant(P, i);
      let e = counts.get(key);
      if (!e) counts.set(key, e = [0, 0, 0, 0]);
      e[0]++; e[1] += P[i]; e[2] += P[i + 1]; e[3] += P[i + 2];
      if (e[0] > bestN) { bestN = e[0]; best = key; }
    }
    if (best < 0) return null;
    const e = counts.get(best);
    return [e[1] / e[0], e[2] / e[0], e[3] / e[0]];
  };
  // The share of an area unlike the colour around it, in one image: near 0 where that image
  // shows only its background there.
  const busy = (P, X, x0, y0, x1, y1, fallback) => {
    const counts = new Map(), pad = 3;
    let best = -1, bestN = 0;
    for (let y = Math.max(0, y0 - pad); y <= Math.min(ch - 1, y1 + pad); y++) {
      for (let x = Math.max(0, x0 - pad); x <= Math.min(cw - 1, x1 + pad); x++) {
        if (x >= x0 && x <= x1 && y >= y0 && y <= y1) continue;
        const i = (y * cw + x) * 4, key = quant(P, i), n = (counts.get(key) || 0) + 1;
        counts.set(key, n);
        if (n > bestN) { bestN = n; best = i; }
      }
    }
    const base = bestN >= 8 ? labOf(P[best], P[best + 1], P[best + 2]) : fallback;
    let n = 0, hit = 0;
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) { n++; if (dE94(X, (y * cw + x) * 3, base, 0) > flatLimit) hit++; }
    return n ? hit / n : 0;
  };
  const correlation = (x0, y0, x1, y1) => {
    const xa = Math.max(0, x0 - 2), ya = Math.max(0, y0 - 2), xb = Math.min(cw - 1, x1 + 2), yb = Math.min(ch - 1, y1 + 2);
    const step = Math.max(1, Math.floor(Math.sqrt((xb - xa + 1) * (yb - ya + 1) / 20000)));
    let n = 0, sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0;
    for (let y = ya; y <= yb; y += step) for (let x = xa; x <= xb; x += step) {
      const p = y * cw + x, u = LA[p], v = LB[p];
      n++; sa += u; sb += v; saa += u * u; sbb += v * v; sab += u * v;
    }
    const ma = sa / n, mb = sb / n, va = saa / n - ma * ma, vb = sbb / n - mb * mb;
    if (va < 1 && vb < 1) return 1;
    if (va < 1 || vb < 1) return 0;
    return (sab / n - ma * mb) / Math.sqrt(va * vb);
  };
  // Where the design's pixels around an area turn up in the page nearby: a clear best match
  // away from the same spot means the content is only moved.
  const displacement = (x0, y0, x1, y1) => {
    const R = Math.max(3, Math.min(24, Math.round(Math.max(x1 - x0, y1 - y0) / 2) + 3));
    const bx0 = Math.max(0, x0 - R), by0 = Math.max(0, y0 - R), bx1 = Math.min(cw - 1, x1 + R), by1 = Math.min(ch - 1, y1 + R);
    const step = Math.max(1, Math.floor(Math.sqrt((bx1 - bx0 + 1) * (by1 - by0 + 1) / 2500)));
    const err = (dx, dy) => {
      let sum = 0, n = 0;
      for (let y = by0; y <= by1; y += step) {
        const yy = y + dy;
        if (yy < 0 || yy >= ch) continue;
        for (let x = bx0; x <= bx1; x += step) {
          const xx = x + dx;
          if (xx < 0 || xx >= cw) continue;
          sum += Math.abs(LA[y * cw + x] - LB[yy * cw + xx]); n++;
        }
      }
      return n > 24 ? sum / n : Infinity;
    };
    const e0 = err(0, 0);
    if (!(e0 > 0.5)) return null;
    let best = e0, bx = 0, by = 0;
    for (let dy = -R; dy <= R; dy++) for (let dx = -R; dx <= R; dx++) {
      if (!dx && !dy) continue;
      const e = err(dx, dy);
      if (e < best) { best = e; bx = dx; by = dy; }
    }
    return (Math.abs(bx) >= 2 || Math.abs(by) >= 2) && best < e0 * 0.35 && best <= 0.3 + 1.5 * noiseL ? { dx: bx, dy: by } : null;
  };
  const classify = (a) => {
    if (a.missing * 2 > a.count) return { kind: "size" };
    const x0 = a.x0, y0 = a.y0, x1 = a.x1, y1 = a.y1;
    const fa = busy(A, XA, x0, y0, x1, y1, bgA), fb = busy(B, XB, x0, y0, x1, y1, bgB);
    // Plainly missing or added: one image shows only its surroundings there, the other something.
    const gone = fa >= 0.08 && fb <= 0.04, added = fb >= 0.08 && fa <= 0.04;
    if (a.minor) {
      // Small and sparse is how renderers differ, unless something is plainly there in only one of them.
      if (!gone && !added) return { kind: "edges" };
      a.minor = false;
    }
    const move = displacement(x0, y0, x1, y1);
    if (move) return { kind: "moved", moved: { dx: Math.round(move.dx / s), dy: Math.round(move.dy / s) } };
    if (gone) return { kind: "missing" };
    if (added) return { kind: "extra" };
    // Where the differing pixels lie: inside fills (a recolour) or only along edges.
    let fills = 0, total = 0;
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const p = y * cw + x;
      if (M[p] !== 1) continue;
      total++;
      if (smooth(XA, x, y, p) && smooth(XB, x, y, p)) fills++;
    }
    const inFills = total ? fills / total : 0, corr = correlation(x0, y0, x1, y1);
    // Only edges differ and the shapes are the same ones: text or edges drawn a fraction of a
    // pixel apart, or by another renderer. A changed word, spacing or size loses its structure.
    if (inFills < 0.2 && corr >= 0.65 &&
        ssim(Math.max(0, x0 - 1), Math.max(0, y0 - 1), Math.min(cw - 1, x1 + 1), Math.min(ch - 1, y1 + 1), true) >= 0.55) {
      a.minor = true;
      return { kind: "edges" };
    }
    const colA = dominant(A, x0, y0, x1, y1), colB = dominant(B, x0, y0, x1, y1);
    if (colA && colB && inFills >= 0.4 && corr >= 0.8) {
      const e = dE2000(labOf(Math.round(colA[0]), Math.round(colA[1]), Math.round(colA[2])),
                       labOf(Math.round(colB[0]), Math.round(colB[1]), Math.round(colB[2])));
      if (e >= 2) return { kind: "color", design_color: hex(colA), page_color: hex(colB), delta_e: r1(e) };
    }
    return { kind: "content" };
  };
  const top = ranked.slice(0, 12).map((a) => Object.assign(a, { cls: classify(a) }))
    .sort((p, q) => (p.minor - q.minor) || (q.count - p.count)).slice(0, 8);
  const areas = top.map((a, index) => {
    const x = Math.floor(a.x0 / s), y = Math.floor(a.y0 / s);
    const area = {
      n: index + 1, x: x, y: y, width: Math.max(1, Math.ceil((a.x1 + 1) / s) - x), height: Math.max(1, Math.ceil((a.y1 + 1) / s) - y),
      ...a.cls,
      differs_pct: Math.round(1000 * a.count / ((a.x1 - a.x0 + 1) * (a.y1 - a.y0 + 1))) / 10,
      pixels: Math.round(a.count / (s * s)),
    };
    if (a.minor) area.minor = true;
    return area;
  });
  const diffPct = N ? 100 * bad / N : 0;
  const sizeOff = element ? Math.max(Math.abs(refW - PW), Math.abs(refH - PH)) : 0;
  const major = ranked.filter((a) => !a.minor).length;
  // Anything more than rendering noise (a missing, extra, moved or recoloured part) means the
  // page does not match yet, however many of its pixels do.
  let verdict = "different";
  if (!bad && matchPct >= 99.5 && sizeOff <= 1 && !shifts.length) verdict = "identical";
  // (At 100% accuracy only an identical page passes.)
  else if (!major && matchPct >= o.matchThreshold && sizeOff <= 2 && !offset && !shifts.length && !(o.tolerance === 0)) verdict = "nearly identical";
  const notes = [];
  let sizeDiff = "";
  if (element) {
    if (sizeOff > 1) {
      const dw = PW - refW, dh = PH - refH, parts = [];
      if (Math.abs(dw) > 1) parts.push(Math.abs(dw) + " px " + (dw > 0 ? "wider" : "narrower"));
      if (Math.abs(dh) > 1) parts.push(Math.abs(dh) + " px " + (dh > 0 ? "taller" : "shorter"));
      sizeDiff = "The page's " + (o.targetKind || "element") + " is " + PW + "×" + PH + "; the design's is " + refW + "×" + refH +
        " (" + (parts.join(" and ") || "about the same size") + ").";
      notes.push(sizeDiff);
    }
    if (offset) {
      const moves = [];
      if (offset.x) moves.push(Math.abs(offset.x) + " px " + (offset.x > 0 ? "right" : "left"));
      if (offset.y) moves.push(Math.abs(offset.y) + " px " + (offset.y > 0 ? "lower" : "higher"));
      notes.push("The page's content sits " + moves.join(" and ") + " than the design's.");
    }
    if (fitted) notes.push("The design image's scale is unknown, so it was scaled to the element's width; pass reference_scale (2 for an @2x export) to compare sizes.");
  } else {
    if (snapped) notes.push("The design area was moved " + [snapped.x ? Math.abs(snapped.x) + " px " + (snapped.x > 0 ? "right" : "left") : "",
      snapped.y ? Math.abs(snapped.y) + " px " + (snapped.y > 0 ? "down" : "up") : ""].filter(Boolean).join(" and ") +
      " to line its top up with the page's.");
    if (scale && Math.abs(crop.width / scale - PW) > 2) {
      const dw = Math.round(crop.width / scale);
      notes.push("The design is " + dw + " px wide but the page is " + PW + " px: resize the browser to " + dw + " wide to compare like for like.");
    } else if (!scale && Math.abs(IW - PW) > 2) {
      notes.push("The design (" + IW + "×" + IH + " px) was scaled to the page's width; if it is an @2x export, pass reference_scale: 2.");
    }
    for (const sh of shifts.slice(0, 4)) {
      notes.push("From y " + (o.region.y + sh.from_y) + " to " + (o.region.y + sh.to_y) + " the page's content sits " + Math.abs(sh.dy) + " px " +
        (sh.dy > 0 ? "lower" : "higher") + " than the design's: something above it is " + Math.abs(sh.dy) + " px " + (sh.dy > 0 ? "taller" : "shorter") +
        " (a height, margin or padding).");
    }
    if (Math.abs(refH - PH) > Math.max(2, PH * 0.01)) {
      notes.push("The design " + (sameRegion && !whole ? "region " : "") + "is " + (refH > PH ? "taller" : "shorter") + " than the " +
        (o.mode === "page" ? "page" : "view") + " (" + refH + " vs " + PH + " px at the page's width); the top " + CH + " px were compared." +
        (refH > PH && o.mode === "view" ? " Compare with full_page for the whole page." : ""));
    }
  }
  for (const a of areas.filter((area) => !area.minor).slice(0, 4)) {
    if (a.kind === "missing") notes.push("Area " + a.n + " is in the design but not on the page.");
    else if (a.kind === "extra") notes.push("Area " + a.n + " is on the page but not in the design.");
    else if (a.kind === "color") notes.push("Area " + a.n + " has the design's shape in another colour: the design's " + a.design_color + " is " + a.page_color + " on the page (\u0394E " + a.delta_e + ").");
    else if (a.kind === "moved") {
      const same = areas.filter((b) => b.kind === "moved" && !b.minor && b.moved.dx === a.moved.dx && b.moved.dy === a.moved.dy);
      if (same[0] !== a) continue;
      const moves = [];
      if (a.moved.dx) moves.push(Math.abs(a.moved.dx) + " px " + (a.moved.dx > 0 ? "right" : "left"));
      if (a.moved.dy) moves.push(Math.abs(a.moved.dy) + " px " + (a.moved.dy > 0 ? "lower" : "higher"));
      notes.push((same.length > 1 ? "Areas " + same.map((b) => b.n).join(" and ") + " are" : "Area " + a.n + " is") +
        " the design's content, " + moves.join(" and ") + " on the page.");
    }
  }
  if (verdict === "different" && !major && sizeOff <= 2 && !offset && !shifts.length) {
    // Below the match threshold with nothing that stands out: many thin differences along edges.
    notes.push("No one area stands out: the differences are thin and run along edges. Unless the design image is blurry or " +
      "heavily compressed, compare corner radius, borders and font weight or size" + (element ? " (inspect lists the element's)" : "") + ".");
  }
  const stats = {
    similarity: matchPct, structure: structure, diff_pct: bad ? Math.max(0.1, r1(diffPct)) : 0, verdict: verdict, threshold: o.matchThreshold,
    content_pixels: Math.round(content / (s * s)),
    page_size: [PW, PH], reference_size: [refW, refH], reference_image_size: [IW, IH],
    reference_scale: scale ? Math.round(scale * 1000) / 1000 : 0, fitted: fitted, same_region: sameRegion, whole_reference: whole,
    reference_region: { x: r1(crop.x / (scale || 1)), y: r1(crop.y / (scale || 1)), width: r1(crop.width / (scale || 1)), height: r1(crop.height / (scale || 1)) },
    compared: [CW, CH], offset: offset, size_diff: sizeDiff, notes: notes, differences: areas,
    snapped_crop: snapped ? { x: crop.x, y: crop.y, width: crop.width, height: crop.height } : null,
    shifts: shifts.slice(0, 6).map((sh) => ({ from_y: o.region.y + sh.from_y, to_y: o.region.y + sh.to_y, dy: sh.dy })),
    board: null,
  };
  // The analysis alone: the element by element comparison draws a board of its own.
  if (o.noBoard) return stats;
  // The board.
  const up = element && CW < 300 ? Math.min(2, 300 / CW) : 1;
  const k = Math.min(o.columnWidth / CW, up, o.maxHeight / Math.max(refH, PH));
  const board = document.getElementById("board");
  board.textContent = "";
  const match = matchPct;
  const tone = verdict === "identical" ? "identical" : verdict === "different" ? "different" : "nearly";
  const refCaption = o.refLabel + " \u00b7 " + (whole ? IW + "\u00d7" + IH : (sameRegion ? "same region of " : "part of ") + IW + "\u00d7" + IH) +
    (scale && Math.abs(scale - 1) > 0.01 ? " @" + r1(scale) + "x" : "");
  const KIND_LABELS = { missing: "not on the page", extra: "not in the design", moved: "moved", color: "another colour",
    content: "look different", size: "size differs", edges: "only edges differ" };
  const byKind = {};
  for (const a of areas) if (!a.minor) byKind[a.kind] = (byKind[a.kind] || 0) + 1;
  const shownMajor = areas.filter((a) => !a.minor).length, unshown = Math.max(0, ranked.filter((a) => !a.minor).length - top.length);
  const counts = Object.keys(KIND_LABELS).filter((kind) => byKind[kind]).map((kind) => ["k-" + kind, byKind[kind], KIND_LABELS[kind]]);
  counts.push(["aa", null, "rendering noise: ignore"]);
  board.append(boardHeader({
    tone: tone,
    verdict: verdict === "identical" ? "Identical" : verdict === "different" ? "Different" : "Nearly identical",
    title: verdict === "identical" ? "Identical to the design" : verdict !== "different" ? "Matches the design: only rendering noise is left" :
      shownMajor ? shownMajor + (shownMajor === 1 ? " difference" : " differences") + " from the design" : "Doesn\u2019t match the design",
    design: refCaption, page: o.pageLabel + " \u00b7 " + PW + "\u00d7" + PH, counts: counts, note: sizeDiff,
    stats: [
      { label: "Match", value: match, unit: "%", meter: match, mark: o.matchThreshold, note: "needs " + o.matchThreshold + "% of the content" },
      { label: "Structure", value: structure, unit: "%", note: "layout and shapes" },
      { label: "To fix", value: shownMajor, note: (shownMajor ? (shownMajor === 1 ? "area, numbered below" : "areas, numbered below") : "no one area") +
        (unshown ? " \u00b7 " + unshown + " smaller not drawn" : "") },
    ],
  }));
  const view = (src, sw, sh) => {
    const c = make(Math.max(1, Math.round(sw * k)), Math.max(1, Math.round(sh * k)));
    const x = c.getContext("2d");
    x.imageSmoothingQuality = "high";
    x.drawImage(src, 0, 0, src.width || src.naturalWidth, src.height || src.naturalHeight, 0, 0, c.width, c.height);
    return c;
  };
  const framed = (canvas, numbered) => {
    const wrap = document.createElement("div");
    wrap.className = "frame";
    wrap.appendChild(canvas);
    for (const a of areas) {
      const box = document.createElement("div");
      box.className = "box k-" + a.kind + (a.minor ? " minor" : "") + (a.y * k < 18 ? " low" : "");
      box.style.left = (1 + a.x * k - 2) + "px";
      box.style.top = (1 + a.y * k - 2) + "px";
      box.style.width = (a.width * k + 4) + "px";
      box.style.height = (a.height * k + 4) + "px";
      if (numbered) { const n = document.createElement("i"); n.textContent = a.n; box.appendChild(n); }
      wrap.appendChild(box);
    }
    return wrap;
  };
  const figure = (name, caption, content, swatches) => {
    const f = document.createElement("figure");
    const cap = document.createElement("figcaption");
    cap.style.maxWidth = Math.max(160, Math.round(CW * k)) + "px";
    const tag = document.createElement("span");
    tag.className = "tag " + (name === "Design" ? "design" : name === "What differs" ? "diff" : "page");
    tag.textContent = name;
    cap.append(tag);
    if (swatches) {
      for (const [cls, text] of swatches) {
        const chip = document.createElement("span");
        chip.className = "chip";
        const sw = document.createElement("span");
        sw.className = "swatch" + (cls ? " " + cls : "");
        chip.append(sw, text);
        cap.append(chip);
      }
    } else {
      const meta = document.createElement("span");
      meta.textContent = caption;
      cap.append(meta);
    }
    f.append(cap, content);
    return f;
  };
  const cols = document.createElement("div");
  cols.className = "cols";
  cols.append(
    figure("Design", refCaption, view(refCanvas, refW, refH)),
    figure("Your page", o.pageLabel + " · " + PW + "×" + PH, framed(view(cur, PW, PH), false)),
    figure("What differs", "numbered as in the results \u00b7 dashed: minor", framed(view(diff, CW, CH), true))
  );
  board.appendChild(cols);
  const box = board.getBoundingClientRect();
  stats.board = { width: Math.ceil(box.width), height: Math.ceil(box.height) };
  return stats;
};

// ---- Element by element ------------------------------------------------------------------------
// Each element of the page is looked for in the design on its own: its crop is lined up with the
// design near where it belongs (where its parent turned up), then measured against it: position
// and size, background and text colour, font size, spacing and wrapping, corner radius, shadow,
// and how much of it matches pixel for pixel.
const EM = 16;  // the margin around an element's crop, in CSS px
// How close counts as the same, from the accuracy setting: base at the default (90%, where only
// rendering noise is let through), exact at 100%, and looser in proportion below 90%.
const tol = (S, base, exact) => Math.max(0, exact + (base - exact) * S.tol);
const ctxOf = (canvas) => canvas.getContext("2d", { willReadFrequently: true });
const rgbLab = (c) => labOf(Math.round(c[0]), Math.round(c[1]), Math.round(c[2]));
const hexLab = (h) => { const m = /^#?([0-9a-f]{6})/i.exec(String(h || "")); if (!m) return null; const v = parseInt(m[1], 16); return labOf(v >> 16, (v >> 8) & 255, v & 255); };
const hexRgb = (h) => { const m = /^#?([0-9a-f]{6})/i.exec(String(h || "")); if (!m) return null; const v = parseInt(m[1], 16); return [v >> 16, (v >> 8) & 255, v & 255]; };
const median = (v) => { const s = v.filter((x) => x !== null && isFinite(x)).sort((p, q) => p - q); return s.length ? s[s.length >> 1] : null; };
const px = (v) => Math.round(Math.abs(v)) + " px";

// A crop of a canvas with its colours in L*a*b*; outside the canvas it is transparent.
const region = (ctx, x, y, w, h) => {
  const X = Math.round(x), Y = Math.round(y), W = Math.max(1, Math.round(w)), H = Math.max(1, Math.round(h));
  const P = ctx.getImageData(X, Y, W, H).data;
  return { P: P, X: labPlane(P, W * H), w: W, h: H, x: X, y: Y };
};

// Lightness at full, half and quarter size, for lining elements up coarse to fine.
const pyramid = (canvas) => {
  const levels = [];
  for (let k = 0, f = 1; k < 3; k++, f /= 2) {
    let c = canvas;
    if (k) {
      c = make(Math.max(1, Math.round(canvas.width * f)), Math.max(1, Math.round(canvas.height * f)));
      const x = ctxOf(c);
      x.imageSmoothingQuality = "high";
      x.drawImage(canvas, 0, 0, c.width, c.height);
    }
    const P = ctxOf(c).getImageData(0, 0, c.width, c.height).data, L = new Uint8Array(c.width * c.height);
    for (let p = 0, i = 0; p < L.length; p++, i += 4) L[p] = (P[i] * 77 + P[i + 1] * 150 + P[i + 2] * 29) >> 8;
    levels.push({ L: L, w: c.width, h: c.height, f: c.width / canvas.width });
  }
  return levels;
};

// Where a part of the page (x, y, w, h) best matches the design within R px of (ox, oy): the
// normalised cross-correlation of their lightness, which a change of colour alone keeps high.
const search = (A, B, x, y, w, h, ox, oy, R) => {
  const f = A.f;
  const X = Math.round(x * f), Y = Math.round(y * f), W = Math.max(2, Math.round(w * f)), H = Math.max(2, Math.round(h * f));
  const RR = Math.max(1, Math.round(R * f)), OX = Math.round(ox * f), OY = Math.round(oy * f);
  const step = Math.max(1, Math.floor(Math.sqrt(W * H / 2500)));
  const tx = [], ty = [], tv = [];
  for (let yy = 0; yy < H; yy += step) {
    const py = Y + yy;
    if (py < 0 || py >= A.h) continue;
    for (let xx = 0; xx < W; xx += step) {
      const qx = X + xx;
      if (qx < 0 || qx >= A.w) continue;
      tx.push(xx); ty.push(yy); tv.push(A.L[py * A.w + qx]);
    }
  }
  const n = tv.length;
  if (n < 12) return null;
  let s1 = 0, s2 = 0;
  for (let k = 0; k < n; k++) { s1 += tv[k]; s2 += tv[k] * tv[k]; }
  if (s2 / n - (s1 / n) * (s1 / n) < 6) return { flat: true, dx: ox, dy: oy, ncc: 0 };
  let best = -Infinity, bx = 0, by = 0, bn = 0;
  for (let dy = -RR; dy <= RR; dy++) {
    for (let dx = -RR; dx <= RR; dx++) {
      const qx0 = X + OX + dx, qy0 = Y + OY + dy;
      let m = 0, st = 0, stt = 0, sb = 0, sbb = 0, stb = 0;
      for (let k = 0; k < n; k++) {
        const qx = qx0 + tx[k], qy = qy0 + ty[k];
        if (qx < 0 || qy < 0 || qx >= B.w || qy >= B.h) continue;
        const t = tv[k], v = B.L[qy * B.w + qx];
        m++; st += t; stt += t * t; sb += v; sbb += v * v; stb += t * v;
      }
      if (m < n * 0.6) continue;
      const mt = st / m, mb = sb / m, vt = stt / m - mt * mt, vb = sbb / m - mb * mb;
      if (vt < 3 || vb < 3) continue;
      const ncc = (stb / m - mt * mb) / Math.sqrt(vt * vb);
      // A slight preference for staying where it belongs, and for lying wholly in the design.
      const score = ncc - 0.04 * Math.hypot(dx, dy) / RR - 0.2 * (1 - m / n);
      if (score > best) { best = score; bx = dx; by = dy; bn = ncc; }
    }
  }
  return best === -Infinity ? null : { dx: (OX + bx) / f, dy: (OY + by) / f, ncc: bn };
};

const locate = (S, b, prior, R) => {
  const m = Math.max(2, Math.min(8, Math.round(0.15 * Math.min(b.width, b.height)) + 2));
  const x = b.x - m, y = b.y - m, w = b.width + 2 * m, h = b.height + 2 * m;
  let lv = 2;
  while (lv > 0 && w * h * S.pp[lv].f * S.pp[lv].f < 160) lv--;
  let r = search(S.pp[lv], S.dp[lv], x, y, w, h, prior.dx, prior.dy, R);
  if (!r || r.flat) return r;
  for (let k = lv - 1; k >= 0; k--) {
    const q = search(S.pp[k], S.dp[k], x, y, w, h, r.dx, r.dy, 2 / S.pp[k].f);
    if (q && !q.flat) r = q;
  }
  return r;
};

// The commonest colour among a crop's pixels in a rectangle that pass keep(x, y), with its share.
const commonColor = (R, x0, y0, x1, y1, keep) => {
  const counts = new Map();
  let best = -1, bestN = 0, total = 0;
  for (let y = Math.max(0, Math.round(y0)); y <= Math.min(R.h - 1, Math.round(y1)); y++) {
    for (let x = Math.max(0, Math.round(x0)); x <= Math.min(R.w - 1, Math.round(x1)); x++) {
      if (keep && !keep(x, y)) continue;
      const i = (y * R.w + x) * 4;
      if (R.P[i + 3] < 128) continue;
      total++;
      const key = ((R.P[i] >> 3) << 10) | ((R.P[i + 1] >> 3) << 5) | (R.P[i + 2] >> 3);
      let e = counts.get(key);
      if (!e) counts.set(key, e = [0, 0, 0, 0]);
      e[0]++; e[1] += R.P[i]; e[2] += R.P[i + 1]; e[3] += R.P[i + 2];
      if (e[0] > bestN) { bestN = e[0]; best = key; }
    }
  }
  if (best < 0) return null;
  const e = counts.get(best);
  return { rgb: [e[1] / e[0], e[2] / e[0], e[3] / e[0]], share: bestN / total, n: total };
};

// Along a line from outside an element inward: the sub-pixel point where pixels turn closer to
// its own colour (ins) than to its surroundings' (out). t in px from the start, or null. (The
// distance is a symmetric one: dE94 weighs chroma by its first colour's, which would pull the
// point towards the more colourful side.)
const labDist = (X, i, Y, j) => Math.hypot(X[i] - Y[j], X[i + 1] - Y[j + 1], X[i + 2] - Y[j + 2]);
const scanEdge = (R, x, y, sx, sy, steps, ins, out) => {
  let prev = null;
  for (let t = 0; t <= steps; t++) {
    const qx = Math.round(x + sx * t), qy = Math.round(y + sy * t);
    if (qx < 0 || qy < 0 || qx >= R.w || qy >= R.h || R.P[(qy * R.w + qx) * 4 + 3] < 128) { prev = null; continue; }
    const p = qy * R.w + qx, q = labDist(R.X, p * 3, out, 0) - labDist(R.X, p * 3, ins, 0);
    // Already inside where the scan starts: the edge lies further out than it looks, unknown.
    if (q > 0) return prev === null ? null : t - 1 + (-prev) / (q - prev);
    prev = q;
  }
  return null;
};

// An element's box as it is drawn: each edge scanned on several lines across its middle, from out.l
// (out.r, out.t, out.b) px outside where it is expected to inn.x (inn.y) px inside it.
const boxEdges = (R, x0, y0, x1, y1, ins, out, reach, inn) => {
  const lines = (a, b) => {
    const n = Math.max(3, Math.min(9, Math.round((b - a) / 6)));
    return Array.from({ length: n }, (_, k) => Math.round(a + (b - a) * (0.2 + 0.6 * k / (n - 1))));
  };
  const rows = lines(y0, y1), cols = lines(x0, x1);
  const side = (values) => { const good = values.filter((v) => v !== null); return good.length * 2 >= values.length ? median(good) : null; };
  const left = side(rows.map((y) => { const t = scanEdge(R, x0 - reach.l, y, 1, 0, reach.l + inn.x, ins, out); return t === null ? null : x0 - reach.l + t; }));
  const right = side(rows.map((y) => { const t = scanEdge(R, x1 + reach.r, y, -1, 0, reach.r + inn.x, ins, out); return t === null ? null : x1 + reach.r - t; }));
  const top = side(cols.map((x) => { const t = scanEdge(R, x, y0 - reach.t, 0, 1, reach.t + inn.y, ins, out); return t === null ? null : y0 - reach.t + t; }));
  const bottom = side(cols.map((x) => { const t = scanEdge(R, x, y1 + reach.b, 0, -1, reach.b + inn.y, ins, out); return t === null ? null : y1 + reach.b - t; }));
  if (left === null || right === null || top === null || bottom === null || right - left < 2 || bottom - top < 2) return null;
  return { left: left, right: right, top: top, bottom: bottom };
};

// A crop's colour at a point between pixel centres (which sit at i + 0.5), interpolated.
const labAt = (R, X, Y) => {
  const fx = X - 0.5, fy = Y - 0.5, x0 = Math.floor(fx), y0 = Math.floor(fy), u = fx - x0, v = fy - y0;
  const get = (x, y, c) => R.X[(Math.max(0, Math.min(R.h - 1, y)) * R.w + Math.max(0, Math.min(R.w - 1, x))) * 3 + c];
  const out = [0, 0, 0];
  for (let c = 0; c < 3; c++) out[c] = (get(x0, y0, c) * (1 - u) + get(x0 + 1, y0, c) * u) * (1 - v) + (get(x0, y0 + 1, c) * (1 - u) + get(x0 + 1, y0 + 1, c) * u) * v;
  return out;
};

// A rounded corner's arc crosses the diagonal r(1 - 1/sqrt 2) in from the box's corner: there a
// fill begins, and a border of width bw peaks bw / (2 sqrt 2) further in. Sampled every 1/4 px.
const cornerRadius = (R, e, ins, out, thin, bw) => {
  const K = 1 - Math.SQRT1_2, reach = Math.min(28, Math.floor(Math.min(e.right - e.left, e.bottom - e.top) / 2));
  // The edges are where pixels turn (between two pixel indices): +0.5 puts them between centres.
  const corners = [[e.left + 0.5, e.top + 0.5, 1, 1], [e.right + 0.5, e.top + 0.5, -1, 1],
                   [e.right + 0.5, e.bottom + 0.5, -1, -1], [e.left + 0.5, e.bottom + 0.5, 1, -1]];
  const least = Math.max(2.5, 0.2 * dE94(ins, 0, out, 0));
  return median(corners.map(([cx, cy, sx, sy]) => {
    const vals = [];
    for (let t = 0; t <= reach; t += 0.25) {
      const lab = labAt(R, cx + sx * t, cy + sy * t);
      vals.push(thin ? dE94(lab, 0, out, 0) : dE94(lab, 0, out, 0) - dE94(lab, 0, ins, 0));
    }
    if (thin) {
      let bi = 0;
      for (let k = 1; k < vals.length; k++) if (vals[k] > vals[bi]) bi = k;
      if (vals[bi] < least) return null;
      const a = vals[Math.max(0, bi - 1)], c = vals[Math.min(vals.length - 1, bi + 1)], den = a - 2 * vals[bi] + c;
      const t = (bi + (den < 0 ? Math.max(-0.5, Math.min(0.5, (a - c) / (2 * den))) : 0)) * 0.25;
      return Math.max(0, (t - bw / (2 * Math.SQRT2)) / K);
    }
    for (let k = 0; k < vals.length; k++) {
      if (vals[k] <= 0) continue;
      return k ? (k - 1 + -vals[k - 1] / (vals[k] - vals[k - 1])) * 0.25 / K : 0;
    }
    return null;
  }));
};

// How much darker than its surroundings the band just outside an element is: its shadow or outline.
const ringDark = (R, e, out, avoid) => {
  let sum = 0, n = 0;
  const visit = (x, y) => {
    if (x < 0 || y < 0 || x >= R.w || y >= R.h || R.P[(y * R.w + x) * 4 + 3] < 128) return;
    for (const a of avoid) if (x >= a[0] && x <= a[2] && y >= a[1] && y <= a[3]) return;
    sum += out[0] - R.X[(y * R.w + x) * 3]; n++;
  };
  for (let d = 1; d <= 6; d++) {
    const x0 = Math.floor(e.left - d), x1 = Math.ceil(e.right + d - 1), y0 = Math.floor(e.top - d), y1 = Math.ceil(e.bottom + d - 1);
    for (let x = x0; x <= x1; x += 2) { visit(x, y0); visit(x, y1); }
    for (let y = y0 + 2; y < y1; y += 2) { visit(x0, y); visit(x1, y); }
  }
  return n >= 20 ? sum / n : null;
};

// The colour around an element past its shadow: the commonest in a band 26-34 px out.
const farColor = (ctx, b, dx, dy) => {
  const x = Math.round(b.x + dx - 34), y = Math.round(b.y + dy - 34), w = Math.round(b.width + 68), h = Math.round(b.height + 68);
  const R = { P: ctx.getImageData(x, y, w, h).data, w: w, h: h };
  return commonColor(R, 0, 0, w - 1, h - 1, (px, py) => px < 8 || py < 8 || px >= w - 8 || py >= h - 8);
};

// Text's ink in a rectangle: pixels at least halfway from the backdrop to the text's own colour,
// with its box, its lines (runs of rows with ink, each with its own box), the colour of its strokes,
// and the ink-weighted spread (sx, sy): sub-pixel measures of its width and height that
// anti-aliasing, which differs between renderers, barely moves.
const inkOf = (R, x0, y0, x1, y1, bg, cutAt, bgRgb) => {
  x0 = Math.max(0, Math.round(x0)); y0 = Math.max(0, Math.round(y0)); x1 = Math.min(R.w - 1, Math.round(x1)); y1 = Math.min(R.h - 1, Math.round(y1));
  if (x1 < x0 || y1 < y0) return null;
  const W = x1 - x0 + 1, d = new Float32Array(W * (y1 - y0 + 1)), strong = [];
  for (let y = y0, k = 0; y <= y1; y++) for (let x = x0; x <= x1; x++, k++) {
    const p = y * R.w + x;
    if (R.P[p * 4 + 3] < 128) continue;
    d[k] = dE94(R.X, p * 3, bg, 0);
    if (d[k] > 12) strong.push(d[k]);
  }
  if (strong.length < 4) return null;
  strong.sort((p, q) => p - q);
  const core = cutAt || strong[Math.floor(strong.length * 0.9)], cut = Math.max(10, core * 0.5);
  const rows = new Uint16Array(y1 - y0 + 1), rx0 = new Int32Array(y1 - y0 + 1).fill(1 << 30), rx1 = new Int32Array(y1 - y0 + 1).fill(-1);
  const rc = new Float64Array((y1 - y0 + 1) * 4);  // per row: the strokes' colour sums and count
  let ix0 = Infinity, ix1 = -1, iy0 = Infinity, iy1 = -1, cr = 0, cg = 0, cb = 0, cn = 0, ink = 0, mr = 0, mg = 0, mb = 0;
  let sw = 0, swx = 0, swy = 0, swxx = 0, swyy = 0;
  for (let y = y0, k = 0; y <= y1; y++) for (let x = x0; x <= x1; x++, k++) {
    if (d[k] >= core * 0.25) {
      const wgt = Math.min(1, d[k] / core);
      sw += wgt; swx += wgt * x; swy += wgt * y; swxx += wgt * x * x; swyy += wgt * y * y;
    }
    if (d[k] < cut) continue;
    ink++; rows[y - y0]++;
    if (x < rx0[y - y0]) rx0[y - y0] = x; if (x > rx1[y - y0]) rx1[y - y0] = x;
    if (x < ix0) ix0 = x; if (x > ix1) ix1 = x; if (y < iy0) iy0 = y; if (y > iy1) iy1 = y;
    const i = (y * R.w + x) * 4;
    mr += R.P[i]; mg += R.P[i + 1]; mb += R.P[i + 2];
    if (d[k] >= core * 0.85) {
      const r = (y - y0) * 4;
      cr += R.P[i]; cg += R.P[i + 1]; cb += R.P[i + 2]; cn++;
      rc[r] += R.P[i]; rc[r + 1] += R.P[i + 1]; rc[r + 2] += R.P[i + 2]; rc[r + 3]++;
    }
  }
  if (ix1 < 0 || !sw) return null;
  // The text's colour however it was drawn: its direction from the background is the strokes' mean
  // (the colour fringes of subpixel-rendered text cancel out in it), and how far it goes is how far
  // the best-covered pixels (the stroke centres, furthest along that direction) reach.
  let dir = null, reach = 0;
  if (bgRgb && ink) {
    const m = [mr / ink - bgRgb[0], mg / ink - bgRgb[1], mb / ink - bgRgb[2]], len = Math.hypot(m[0], m[1], m[2]);
    if (len > 4) {
      dir = m.map((v) => v / len);
      const along = [];
      for (let y = y0, k = 0; y <= y1; y++) for (let x = x0; x <= x1; x++, k++) {
        if (d[k] < cut) continue;
        const i = (y * R.w + x) * 4;
        along.push((R.P[i] - bgRgb[0]) * dir[0] + (R.P[i + 1] - bgRgb[1]) * dir[1] + (R.P[i + 2] - bgRgb[2]) * dir[2]);
      }
      along.sort((p, q) => q - p);
      const n = Math.min(along.length, Math.max(3, Math.ceil(along.length * 0.03)));
      for (let j = 0; j < n; j++) reach += along[j] / n;
    }
  }
  // Each line: its rows, its left and right ends, and the colour of its strokes.
  const lines = [];
  let start = -1, gap = 0;
  const close = (endRow) => {
    let a = 1 << 30, b = -1, sr = 0, sg = 0, sb = 0, n = 0;
    for (let r = start; r <= endRow; r++) {
      if (rx0[r] < a) a = rx0[r]; if (rx1[r] > b) b = rx1[r];
      sr += rc[r * 4]; sg += rc[r * 4 + 1]; sb += rc[r * 4 + 2]; n += rc[r * 4 + 3];
    }
    lines.push([y0 + start, y0 + endRow, a, b, n ? [sr / n, sg / n, sb / n] : null]);
  };
  for (let r = 0; r < rows.length; r++) {
    if (rows[r]) { if (start < 0) start = r; gap = 0; }
    else if (start >= 0 && ++gap > 2) { close(r - gap); start = -1; gap = 0; }
  }
  if (start >= 0) close(rows.length - 1 - gap);
  const mx = swx / sw, my = swy / sw;
  return { x0: ix0, x1: ix1, y0: iy0, y1: iy1, lines: lines, color: cn ? [cr / cn, cg / cn, cb / cn] : null, dir: dir, reach: reach, ink: ink, core: core,
           mx: mx, my: my, sx: Math.sqrt(Math.max(0, swxx / sw - mx * mx)), sy: Math.sqrt(Math.max(0, swyy / sw - my * my)) };
};

// How alike two text ink boxes look once one is scaled onto the other: the same words at
// another size or spacing stay alike, other words or another font do not. Softened, the same
// words drawn by two renderers score 0.85 and more; other words well under 0.5.
const SAME_WORDS = 0.75;
const inkSimilarity = (A, a, B, b, blur) => {
  const W = Math.min(400, a.x1 - a.x0 + 1), H = Math.min(120, a.y1 - a.y0 + 1);
  if (W < 3 || H < 3) return 0;
  const sample = (R, x0, y0, x1, y1) => {
    const out = new Float32Array(W * H), sx = (x1 - x0 + 1) / W, sy = (y1 - y0 + 1) / H;
    for (let j = 0; j < H; j++) for (let i = 0; i < W; i++) {
      const fx = x0 + (i + 0.5) * sx - 0.5, fy = y0 + (j + 0.5) * sy - 0.5;
      const xa = Math.max(0, Math.min(R.w - 1, Math.floor(fx))), ya = Math.max(0, Math.min(R.h - 1, Math.floor(fy)));
      const xb = Math.min(R.w - 1, xa + 1), yb = Math.min(R.h - 1, ya + 1);
      const u = Math.max(0, Math.min(1, fx - xa)), v = Math.max(0, Math.min(1, fy - ya));
      const L = (x, y) => R.X[(y * R.w + x) * 3];
      out[j * W + i] = (L(xa, ya) * (1 - u) + L(xb, ya) * u) * (1 - v) + (L(xa, yb) * (1 - u) + L(xb, yb) * u) * v;
    }
    return out;
  };
  let p = sample(A, a.x0, a.y0, a.x1, a.y1), q = sample(B, b.x0, b.y0, b.x1, b.y1);
  if (blur) {
    // Softened a little: renderers differ in anti-aliasing and hinting, words in their shapes.
    const soft = (v) => {
      const out = new Float32Array(v.length);
      for (let j = 0; j < H; j++) for (let i = 0; i < W; i++) {
        let s = 0, n = 0;
        for (let y = Math.max(0, j - 1); y <= Math.min(H - 1, j + 1); y++) for (let x = Math.max(0, i - 1); x <= Math.min(W - 1, i + 1); x++) { s += v[y * W + x]; n++; }
        out[j * W + i] = s / n;
      }
      return out;
    };
    p = soft(p); q = soft(q);
  }
  let sp = 0, sq = 0, spp = 0, sqq = 0, spq = 0;
  for (let k = 0; k < p.length; k++) { sp += p[k]; sq += q[k]; spp += p[k] * p[k]; sqq += q[k] * q[k]; spq += p[k] * q[k]; }
  const n = p.length, mp = sp / n, mq = sq / n, vp = spp / n - mp * mp, vq = sqq / n - mq * mq;
  return vp < 1 || vq < 1 ? 0 : (spq / n - mp * mq) / Math.sqrt(vp * vq);
};

// Pixel by pixel within an element's own pixels, with the rules of the whole comparison: a pixel
// differs only when no pixel next to it matches (both ways) and it is not rendering noise.
const nearMatch = (X, Y, w, h, x, y, p, lim) => {
  for (let dy = -1; dy <= 1; dy++) {
    const yy = y + dy;
    if (yy < 0 || yy >= h) continue;
    for (let dx = -1; dx <= 1; dx++) {
      const xx = x + dx;
      if (xx < 0 || xx >= w || (!dx && !dy)) continue;
      if (dE94(X, p * 3, Y, (yy * w + xx) * 3) <= lim) return true;
    }
  }
  return false;
};
const smoothAt = (X, w, h, x, y, p, lim) => {
  for (let yy = Math.max(0, y - 1); yy <= Math.min(h - 1, y + 1); yy++) for (let xx = Math.max(0, x - 1); xx <= Math.min(w - 1, x + 1); xx++) {
    if (dE94(X, p * 3, X, (yy * w + xx) * 3) > lim) return false;
  }
  return true;
};
const stillAt = (P, w, h, x, y) => {
  const i = (y * w + x) * 4;
  for (let yy = Math.max(0, y - 1); yy <= Math.min(h - 1, y + 1); yy++) for (let xx = Math.max(0, x - 1); xx <= Math.min(w - 1, x + 1); xx++) {
    const j = (yy * w + xx) * 4;
    if (P[i] !== P[j] || P[i + 1] !== P[j + 1] || P[i + 2] !== P[j + 2]) return false;
  }
  return true;
};
const flatAt = (P, w, h, x1, y1) => {
  const x0 = Math.max(x1 - 1, 0), y0 = Math.max(y1 - 1, 0), x2 = Math.min(x1 + 1, w - 1), y2 = Math.min(y1 + 1, h - 1), pos = (y1 * w + x1) * 4;
  let same = x1 === x0 || x1 === x2 || y1 === y0 || y1 === y2 ? 1 : 0;
  for (let x = x0; x <= x2; x++) for (let y = y0; y <= y2; y++) {
    if (x === x1 && y === y1) continue;
    const q = (y * w + x) * 4;
    if (P[pos] === P[q] && P[pos + 1] === P[q + 1] && P[pos + 2] === P[q + 2]) same++;
    if (same > 2) return true;
  }
  return false;
};
const aliasAt = (P, w, h, x1, y1, Q) => {
  const x0 = Math.max(x1 - 1, 0), y0 = Math.max(y1 - 1, 0), x2 = Math.min(x1 + 1, w - 1), y2 = Math.min(y1 + 1, h - 1);
  const lum = (i) => 0.29889531 * P[i] + 0.58662247 * P[i + 1] + 0.11448223 * P[i + 2];
  const b0 = lum((y1 * w + x1) * 4);
  let same = x1 === x0 || x1 === x2 || y1 === y0 || y1 === y2 ? 1 : 0, min = 0, max = 0, minX = 0, minY = 0, maxX = 0, maxY = 0;
  for (let x = x0; x <= x2; x++) for (let y = y0; y <= y2; y++) {
    if (x === x1 && y === y1) continue;
    const d = b0 - lum((y * w + x) * 4);
    if (d === 0) { same++; if (same > 2) return false; }
    else if (d < min) { min = d; minX = x; minY = y; }
    else if (d > max) { max = d; maxX = x; maxY = y; }
  }
  if (min === 0 || max === 0) return false;
  return (flatAt(P, w, h, minX, minY) && flatAt(Q, w, h, minX, minY)) || (flatAt(P, w, h, maxX, maxY) && flatAt(Q, w, h, maxX, maxY));
};
const ssimAround = (X, Y, w, h, x, y, r) => {
  let n = 0, sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0;
  for (let yy = Math.max(0, y - r); yy <= Math.min(h - 1, y + r); yy++) for (let xx = Math.max(0, x - r); xx <= Math.min(w - 1, x + r); xx++) {
    const u = X[(yy * w + xx) * 3], v = Y[(yy * w + xx) * 3];
    n++; sa += u; sb += v; saa += u * u; sbb += v * v; sab += u * v;
  }
  const ma = sa / n, mb = sb / n, va = saa / n - ma * ma, vb = sbb / n - mb * mb, cov = sab / n - ma * mb;
  return ((2 * ma * mb + 1) * (2 * cov + 9)) / ((ma * ma + mb * mb + 1) * (va + vb + 9));
};
const pixelMatch = (A, B, own, bgA, bgB, o) => {
  const w = A.w, h = A.h, N = w * h, step = Math.max(1, Math.floor(Math.sqrt(N / 250000)));
  const mask = new Uint8Array(N);
  let content = 0, same = 0;
  for (let y = 0; y < h; y += step) for (let x = 0; x < w; x += step) {
    const p = y * w + x, i = p * 4;
    if (!own[p] || A.P[i + 3] < 128) continue;
    if (B.P[i + 3] < 128) { content++; mask[p] = 2; continue; }
    const d = A.P[i] === B.P[i] && A.P[i + 1] === B.P[i + 1] && A.P[i + 2] === B.P[i + 2] ? 0 : dE94(A.X, p * 3, B.X, p * 3);
    const ink = (bgA && dE94(A.X, p * 3, bgA, 0) > o.flatDeltaE) || (bgB && dE94(B.X, p * 3, bgB, 0) > o.flatDeltaE);
    let differs = false;
    if (d > o.flatDeltaE && (d > o.deltaE || (smoothAt(A.X, w, h, x, y, p, o.smoothDeltaE) && smoothAt(B.X, w, h, x, y, p, o.smoothDeltaE)))) {
      const lim = d > o.deltaE ? o.deltaE : o.flatDeltaE;
      if (!nearMatch(A.X, B.X, w, h, x, y, p, lim) && !nearMatch(B.X, A.X, w, h, x, y, p, lim)) {
        differs = !(aliasAt(A.P, w, h, x, y, B.P) || aliasAt(B.P, w, h, x, y, A.P) ||
          (!stillAt(A.P, w, h, x, y) && !stillAt(B.P, w, h, x, y) && ssimAround(A.X, B.X, w, h, x, y, o.ssimRadius) >= o.ssimNoise));
      }
    }
    if (ink || differs) { content++; if (!differs) same++; }
    if (differs) mask[p] = 1;
  }
  return { match: content ? 100 * same / content : 100, content: content, step: step, mask: mask };
};

// The words for a move or a size change of the page's element against the design's.
const movedText = (dx, dy) => {
  const parts = [];
  if (Math.abs(dx) >= 0.5) parts.push(px(dx) + (dx > 0 ? " further left" : " further right"));
  if (Math.abs(dy) >= 0.5) parts.push(px(dy) + (dy > 0 ? " higher" : " lower"));
  return "sits " + parts.join(" and ") + " than in the design";
};
const sizeText = (dw, dh) => {
  const parts = [];
  if (Math.abs(dw) >= 2) parts.push(px(dw) + (dw > 0 ? " narrower" : " wider"));
  if (Math.abs(dh) >= 2) parts.push(px(dh) + (dh > 0 ? " shorter" : " taller"));
  return parts.join(" and ") + " than in the design";
};

const measureText = (S, e, off, near, findings, cont) => {
  const st = e.style || {}, tb = e.text_box;
  // The page's text.
  const P = region(S.pctx, tb.x - 3, tb.y - 3, tb.width + 6, tb.height + 6);
  const pb = commonColor(P, 0, 0, P.w - 1, P.h - 1);
  const pInk = pb && inkOf(P, 2, 2, P.w - 3, P.h - 3, rgbLab(pb.rgb), 0, pb.rgb);
  if (!pInk) return null;
  // The design's, around the same place: wider, and taller below (it may wrap onto more lines),
  // short of the neighbours beside and above it.
  let padL = Math.min(40, Math.round(0.25 * tb.width) + 4), padR = padL, padT = Math.min(24, Math.round(0.5 * tb.height) + 3);
  for (const a of near) {
    if (a[1] <= tb.y + tb.height && a[3] >= tb.y) {
      if (a[2] < tb.x) padL = Math.min(padL, Math.max(0, Math.floor((tb.x - a[2]) / 2)));
      if (a[0] > tb.x + tb.width) padR = Math.min(padR, Math.max(0, Math.floor((a[0] - tb.x - tb.width) / 2)));
    }
    if (a[0] <= tb.x + tb.width && a[2] >= tb.x && a[3] < tb.y) padT = Math.min(padT, Math.max(0, Math.floor((tb.y - a[3]) / 2)));
  }
  // Nor past the box it is in (its own, as a button's, or its parent's), as large as the design has it.
  let below = Math.min(400, Math.round(4 * tb.height) + 24);
  if (cont) {
    padL = Math.min(padL, Math.max(0, Math.floor(tb.x - cont.x - 1)));
    padR = Math.min(padR, Math.max(0, Math.floor(cont.x + cont.width + cont.dw - tb.x - tb.width - 1)));
    padT = Math.min(padT, Math.max(0, Math.floor(tb.y - cont.y - 1)));
    if (cont.own) below = Math.min(below, Math.max(2, Math.floor(cont.y + cont.height + cont.dh - tb.y - tb.height - 1)));
  }
  const D = region(S.dctx, tb.x + off.dx - padL, tb.y + off.dy - padT, tb.width + padL + padR, tb.height + padT + below);
  const where = padT + Math.round(tb.height) + 3;
  const db = commonColor(D, 0, 0, D.w - 1, Math.min(D.h - 1, where));
  const seed = db && inkOf(D, 0, 0, D.w - 1, Math.min(D.h - 1, where), rgbLab(db.rgb));
  const all = seed && inkOf(D, 0, 0, D.w - 1, D.h - 1, rgbLab(db.rgb), seed.core);
  const lines = all ? all.lines : [];
  const first = lines.findIndex((l) => l[1] >= padT - 2 && l[0] <= padT + tb.height + 2);
  if (first < 0) {
    findings.push({ kind: "text", text: "its text is not in the design here" });
    return { pInk: pInk, missing: true, resized: true };
  }
  // Its lines: the first where the text belongs, then those that follow as lines of one block
  // (close below, of like height and colour, sharing the edge the text is aligned to).
  const align = String(st.align || "start");
  const anchor = (l) => align === "center" ? (l[2] + l[3]) / 2 : align === "right" || align === "end" ? l[3] : l[2];
  const glyph = (l) => l[1] - l[0] + 1, g0 = glyph(lines[first]);
  const gaps = pInk.lines.slice(1).map((l, k) => l[0] - pInk.lines[k][1] - 1);
  let last = first;
  for (let k = first + 1; k < lines.length; k++) {
    const gap = lines[k][0] - lines[last][1] - 1;
    if (gap > Math.max(3, 0.9 * g0, 1.5 * Math.max(0, ...gaps) + 2)) break;
    if (Math.abs(glyph(lines[k]) - g0) > Math.max(3, 0.45 * g0)) break;
    if (align !== "justify" && Math.abs(anchor(lines[k]) - anchor(lines[first])) > Math.max(12, 0.1 * tb.width)) break;
    const c0 = lines[first][4], ck = lines[k][4];
    if (c0 && ck && dE2000(rgbLab(c0), rgbLab(ck)) > 15) break;
    last = k;
  }
  const dInk = inkOf(D, 0, lines[first][0] - 1, D.w - 1, lines[last][1] + 1, rgbLab(db.rgb), seed.core, db.rgb);
  if (!dInk) return { pInk: pInk, missing: true, resized: true };
  const out = { pInk: pInk, dInk: dInk, resized: false, same: true };
  const lp = pInk.lines.length, ld = dInk.lines.length, size = st.font_size || 0, half = (v) => Math.round(v);
  const pw = pInk.x1 - pInk.x0 + 1, dw = dInk.x1 - dInk.x0 + 1, ph = pInk.y1 - pInk.y0 + 1, dh = dInk.y1 - dInk.y0 + 1;
  const firstLine = (ink) => ({ x0: ink.lines[0][2], x1: ink.lines[0][3], y0: ink.lines[0][0], y1: ink.lines[0][1] });
  const fontSize = (r) => {
    findings.push({ kind: "font_size", text: "font size about " + half(size * r) + "px in the design (" + size + "px here)", page: size, design: half(size * r) });
    out.resized = true;
  };
  let ry = 1;
  if (lp !== ld) {
    findings.push({ kind: "wrap", text: "the design sets it on " + ld + " line" + (ld === 1 ? "" : "s") + " (" + lp + " here): width, font size or letter spacing", page: lp, design: ld });
    out.resized = true;
  } else if (lp === 1) {
    if (S.o.debug) out.sims = [inkSimilarity(P, pInk, D, dInk, false), inkSimilarity(P, pInk, D, dInk, true)];
    if (inkSimilarity(P, pInk, D, dInk, true) < SAME_WORDS) {
      findings.push({ kind: "text", text: "its text looks different in the design: other words, font or weight" });
      out.same = false; out.resized = true;
    } else {
      // The same words: the height of their ink gives the type's size, the width beyond it the spacing.
      const r = dInk.sy / pInk.sy;
      if (Math.abs(r - 1) >= tol(S, 0.06, 0.02) && Math.abs(dh - ph) >= tol(S, 1.5, 0.5) && size) { ry = r; fontSize(r); }
      const rx = dInk.sx / pInk.sx;
      if (Math.abs(rx - ry) >= tol(S, 0.04, 0.015) && Math.abs(dw - pw * ry) >= tol(S, 3, 1)) {
        if (ry === 1 && (e.chars || 0) > 1) {
          const now = st.letter_spacing || 0, want = Math.round((now + (dw - pw) / (e.chars - 1)) * 10) / 10;
          findings.push({ kind: "letter_spacing", text: "letter spacing about " + want + "px in the design (" + now + "px here)", page: now, design: want });
        } else {
          findings.push({ kind: "text_width", text: "its text is " + Math.round(Math.abs(rx / ry - 1) * 100) + "% " + (rx > ry ? "wider" : "narrower") +
            " in the design than its size explains: letter spacing, weight or font" });
        }
        out.resized = true;
      }
    }
  } else {
    // As many lines in both: their pitch is the line height, the height of their type its size.
    const pitch = (ink) => median(ink.lines.slice(1).map((l, k) => l[0] - ink.lines[k][0]));
    const type = (ink) => median(ink.lines.map((l) => l[1] - l[0] + 1));
    const pp = pitch(pInk), pd = pitch(dInk), gp = type(pInk), gd = type(dInk);
    if (inkSimilarity(P, firstLine(pInk), D, firstLine(dInk), true) < SAME_WORDS) {
      findings.push({ kind: "text", text: "its text looks different in the design: other words, font or weight" });
      out.same = false; out.resized = true;
    } else if (gp && gd && Math.abs(gd - gp) >= tol(S, 1.5, 0.5) && Math.abs(gd / gp - 1) >= tol(S, 0.06, 0.02) && size) { ry = gd / gp; fontSize(ry); }
    if (pp && pd && Math.abs(pd - pp) >= tol(S, 1.5, 0.75)) {
      const now = st.line_height || pp, want = Math.round(now * pd / pp);
      findings.push({ kind: "line_height", text: "line height about " + want + "px in the design (" + Math.round(now) + "px here)", page: Math.round(now), design: want });
      out.resized = true;
    }
  }
  // Where the text is in the design: its edge for its alignment, and the middle of its (first) line,
  // less what the size explains. Renderers place a glyph's top a pixel or two apart; its mass hardly.
  const edge = (ink, R) => (align === "center" ? (ink.x0 + ink.x1) / 2 : align === "right" || align === "end" ? ink.x1 : ink.x0) + R.x;
  const middle = (ink) => ink.lines.length === 1 ? ink.my : (ink.lines[0][0] + ink.lines[0][1]) / 2;
  const lead = middle(pInk) + P.y - tb.y;
  out.dx = edge(dInk, D) - edge(pInk, P);
  out.dy = middle(dInk) + D.y - lead * ry - tb.y;
  if (pInk.dir && dInk.dir && pInk.ink >= 12 && dInk.ink >= 12) {
    // Each side's text colour as its strokes show it: the background, and as far towards the text's
    // colour as its best-covered pixels go.
    const shown = (ink, bg, k) => bg.rgb.map((v, c) => Math.max(0, Math.min(255, v + k * ink.reach * ink.dir[c])));
    const pc = shown(pInk, pb, 1), dc = shown(dInk, db, 1);
    const e00 = dE2000(rgbLab(pc), rgbLab(dc)), css = hexRgb(st.color);
    if (e00 >= tol(S, css ? 3 : out.resized ? 9 : 6, 1)) {
      // Thin strokes never reach the text's colour fully: how far the page's go towards its CSS colour
      // says how far the design's go towards its own.
      let c = dc, off = e00;
      if (css) {
        const full = (css[0] - pb.rgb[0]) * pInk.dir[0] + (css[1] - pb.rgb[1]) * pInk.dir[1] + (css[2] - pb.rgb[2]) * pInk.dir[2];
        const alpha = full > 0 ? Math.max(0.3, Math.min(1, pInk.reach / full)) : 1;
        c = shown(dInk, db, 1 / alpha);
        // Drawn at another scale or by another renderer, the same colour's strokes differ: the colour
        // they work back to decides (within about 3.5 of the CSS one when it is the same; less closely
        // for small text, whose thin strokes a screenshot at 1.5x with coloured fringes blurs most).
        // (A JPEG keeps colour at half resolution, so thin strokes take on some of what is around them:
        // there hue and saturation count half.)
        const soft = (lab) => S.jpeg ? [lab[0], lab[1] / 2, lab[2] / 2] : lab;
        off = dE2000(soft(rgbLab(c)), soft(rgbLab(css)));
      }
      if (off >= (!css ? 0 : tol(S, (out.resized ? 6 : 4.5) + 0.67 * Math.max(0, 18 - (st.font_size || 16)), 1.5))) {
        findings.push({ kind: "text_color", text: "text colour " + (st.color || hex(pc)) + "; the design's looks like " + hex(c), page: st.color || hex(pc), design: hex(c), delta_e: r1(off) });
      }
    }
  }
  return out;
};

const measureElement = (S, e) => {
  const o = S.o, b = e.box, st = e.style || {};
  const parent = e.parent >= 0 ? S.results[e.parent] : null;
  // Where it should be: where its parent turned out to be (its measured place, else its match).
  const pdisp = parent && parent.found ? (parent.disp || parent.offset) : S.prior;
  const prior = { dx: Math.round(pdisp.dx), dy: Math.round(pdisp.dy) };
  const big = Math.max(b.width, b.height);
  const R = parent ? Math.min(24, Math.max(6, Math.round(4 + 0.08 * big))) : Math.min(48, Math.max(12, Math.round(12 + 0.1 * big)));
  let loc = locate(S, b, prior, R);
  if ((!loc || (!loc.flat && loc.ncc < 0.6)) && S.wide < 16 && b.width * b.height >= 200) {
    // Not near where it belongs: look further, once for a few elements.
    S.wide++;
    const wide = locate(S, b, S.prior, 160);
    if (wide && !wide.flat && wide.ncc >= 0.7 && wide.ncc > (loc && !loc.flat ? loc.ncc : 0) + 0.15) loc = wide;
  }
  const flat = !!(loc && loc.flat), found = !!loc && (flat || loc.ncc >= 0.5);
  // A box that can be measured by its edges: a fill, an image, a control, or a border all round (a
  // rule on one side only has no box to measure).
  const boxy = (e.flags.bg && e.flags.bg !== "translucent") || e.flags.control || e.flags.image || e.flags.outline;
  // A poor match (the element changed size or content) settles on a compromise: measure such an
  // element where it belongs instead, and let its edges and text say where it is. A bare wrapper's
  // match follows its children, so it is compared where its parent puts it.
  const aligned = !!loc && !flat && loc.ncc >= 0.8;
  const offset = aligned && (boxy || e.flags.text) ? { dx: Math.round(loc.dx), dy: Math.round(loc.dy) } : prior;
  const A = region(S.pctx, b.x - EM, b.y - EM, b.width + 2 * EM, b.height + 2 * EM);
  const B = region(S.dctx, b.x - EM + offset.dx, b.y - EM + offset.dy, b.width + 2 * EM, b.height + 2 * EM);
  const bx0 = Math.round(b.x) - A.x, by0 = Math.round(b.y) - A.y, bx1 = bx0 + Math.max(1, Math.round(b.width)) - 1, by1 = by0 + Math.max(1, Math.round(b.height)) - 1;
  // Its own pixels: inside its box, outside its descendants' (they are compared on their own, and
  // the edges of their text spill a pixel or two past their boxes).
  const own = new Uint8Array(A.w * A.h);
  for (let y = Math.max(0, by0); y <= Math.min(A.h - 1, by1); y++) own.fill(1, y * A.w + Math.max(0, bx0), y * A.w + Math.min(A.w - 1, bx1) + 1);
  for (const k of S.kids[e.i]) {
    const kb = S.items[k].box, kx0 = Math.max(0, Math.round(kb.x) - 2 - A.x), kx1 = Math.min(A.w - 1, Math.round(kb.x + kb.width) + 1 - A.x);
    for (let y = Math.max(0, Math.round(kb.y) - 2 - A.y); y <= Math.min(A.h - 1, Math.round(kb.y + kb.height) + 1 - A.y); y++) if (kx1 >= kx0) own.fill(0, y * A.w + kx0, y * A.w + kx1 + 1);
  }
  let ownCount = 0;
  for (let p = 0; p < own.length; p++) ownCount += own[p];
  const mine = (x, y) => own[y * A.w + x] === 1;
  const cA = commonColor(A, bx0, by0, bx1, by1, mine), cB = commonColor(B, bx0, by0, bx1, by1, mine);
  const bgA = cA ? rgbLab(cA.rgb) : null, bgB = cB ? rgbLab(cB.rgb) : null;
  // Other elements beside it (not its ancestors or descendants): in the page, and in crop coordinates.
  const nearPage = S.near[e.i].map((k) => { const nb = S.items[k].box; return [nb.x, nb.y, nb.x + nb.width - 1, nb.y + nb.height - 1]; });
  const near = nearPage.map((a) => [a[0] - A.x, a[1] - A.y, a[2] - A.x, a[3] - A.y]);
  const findings = [];
  const rec = { i: e.i, parent: e.parent, name: e.name, selector: e.selector, box: b, found: found, offset: offset, ncc: loc && !loc.flat ? Math.round(loc.ncc * 100) / 100 : null };
  // Nothing like it where it belongs: the design shows its background there, or something else.
  const absent = () => {
    let busy = 0, n = 0;
    for (let y = Math.max(0, by0); y <= Math.min(B.h - 1, by1); y += 2) for (let x = Math.max(0, bx0); x <= Math.min(B.w - 1, bx1); x += 2) {
      const p = y * B.w + x;
      if (B.P[p * 4 + 3] < 128) { n++; busy++; continue; }
      n++; if (bgB && dE94(B.X, p * 3, bgB, 0) > o.flatDeltaE) busy++;
    }
    const outside = B.P[((by0 + by1 >> 1) * B.w + (bx0 + bx1 >> 1)) * 4 + 3] < 128;
    rec.found = false;
    rec.status = "missing";
    rec.findings = [{ kind: "missing", text: outside ? "not in the design: the design ends before it" :
      n && busy / n < 0.03 ? "not in the design: the design shows only its background here" : "not in the design here: the design shows something else in its place" }];
    delete rec.match;
    return rec;
  };
  // No match by shape: text may be there at another size, a box at another size: measure those
  // before calling it missing.
  if (!found && !boxy && !(e.flags.text && e.text_box)) return absent();
  const pm = pixelMatch(A, B, own, bgA, bgB, o);
  rec.match = r1(pm.match);
  // Its box as drawn: where its own colour meets its surroundings' (the outer frame of the crop).
  const frame = (x, y) => (x < 4 || y < 4 || x >= A.w - 4 || y >= A.h - 4) && (x < bx0 - 2 || x > bx1 + 2 || y < by0 - 2 || y > by1 + 2);
  const touches = bx0 < EM - 1 || by0 < EM - 1 || bx1 > A.w - EM || by1 > A.h - EM || b.x < 1 || b.y < 1 || b.x + b.width > S.g.PW - 1 || b.y + b.height > S.g.PH - 1;
  let geo = null;
  if (boxy && !touches && b.width >= 6 && b.height >= 6) {
    // The design's side from a wider crop: its box may be a good deal larger or smaller.
    const M2 = Math.max(EM, Math.min(64, Math.round(0.25 * big)));
    const D = region(S.dctx, b.x - M2 + offset.dx, b.y - M2 + offset.dy, b.width + 2 * M2, b.height + 2 * M2);
    const dx0 = Math.round(b.x) + offset.dx - D.x, dy0 = Math.round(b.y) + offset.dy - D.y;
    const dx1 = dx0 + (bx1 - bx0), dy1 = dy0 + (by1 - by0);
    const inset = (R0, x0, y0, x1, y1, keep) => commonColor(R0, x0 + Math.min(4, b.width * 0.2), y0 + Math.min(4, b.height * 0.2), x1 - Math.min(4, b.width * 0.2), y1 - Math.min(4, b.height * 0.2), keep);
    const opaque = e.flags.bg && e.flags.bg !== "translucent";
    const iA = opaque ? inset(A, bx0, by0, bx1, by1, mine) : null, iB = opaque ? inset(D, dx0, dy0, dx1, dy1, null) : null;
    const frameD = (x, y) => (x < 4 || y < 4 || x >= D.w - 4 || y >= D.h - 4) && (x < dx0 - 2 || x > dx1 + 2 || y < dy0 - 2 || y > dy1 + 2);
    const oA = commonColor(A, 0, 0, A.w - 1, A.h - 1, frame), oB = commonColor(D, 0, 0, D.w - 1, D.h - 1, frameD);
    const border = e.flags.outline ? hexLab(e.flags.border) : null;
    const insA = iA ? rgbLab(iA.rgb) : border, insB = iB ? rgbLab(iB.rgb) : border;
    // A white card on a light grey page is barely 4 apart: enough, lined up over several scan lines.
    // How far out from each side a scan may go: never past its parent's edge or into a neighbour.
    const room = { l: Infinity, r: Infinity, t: Infinity, b: Infinity };
    if (parent) {
      const pb = parent.box;
      room.l = b.x - pb.x - 1; room.r = pb.x + pb.width - b.x - b.width - 1; room.t = b.y - pb.y - 1; room.b = pb.y + pb.height - b.y - b.height - 1;
    }
    for (const a of nearPage) {
      if (a[1] <= b.y + b.height && a[3] >= b.y) {
        if (a[2] < b.x) room.l = Math.min(room.l, b.x - a[2] - 2);
        if (a[0] > b.x + b.width) room.r = Math.min(room.r, a[0] - b.x - b.width - 1);
      }
      if (a[0] <= b.x + b.width && a[2] >= b.x) {
        if (a[3] < b.y) room.t = Math.min(room.t, b.y - a[3] - 2);
        if (a[1] > b.y + b.height) room.b = Math.min(room.b, a[1] - b.y - b.height - 1);
      }
    }
    const E = Math.min(EM - 2, Math.max(4, Math.round(0.3 * Math.min(b.width, b.height))));
    const within = (r, cap) => ({ l: Math.floor(Math.min(cap.x, r.l)), r: Math.floor(Math.min(cap.x, r.r)), t: Math.floor(Math.min(cap.y, r.t)), b: Math.floor(Math.min(cap.y, r.b)) });
    const reachA = within(room, { x: E, y: E });
    const reachD = within(room, { x: Math.min(M2 - 2, Math.max(E, Math.round(0.25 * b.width))), y: Math.min(M2 - 2, Math.max(E, Math.round(0.25 * b.height))) });
    const roomy = Math.min(reachA.l, reachA.r, reachA.t, reachA.b) >= 3;
    if (roomy && insA && insB && oA && oB && dE94(insA, 0, rgbLab(oA.rgb), 0) >= 2.5 && dE94(insB, 0, rgbLab(oB.rgb), 0) >= 2.5) {
      const outA = rgbLab(oA.rgb), outB = rgbLab(oB.rgb);
      const innA = { x: E, y: E }, innD = { x: Math.min(Math.round(b.width / 2), reachD.l + reachD.r), y: Math.min(Math.round(b.height / 2), reachD.t + reachD.b) };
      const eA = boxEdges(A, bx0, by0, bx1, by1, insA, outA, reachA, innA), eB = boxEdges(D, dx0, dy0, dx1, dy1, insB, outB, reachD, innD);
      if (eA && eB) {
        // The page's edges as its DOM box has them wherever the drawn ones agree: the capture snaps
        // them to its pixels (half pixels at 2x) and the design's renderer to its own, so a 140.5 px
        // box can be drawn 140 wide in one and 140.5 in the other. (Edges here lie between pixel
        // centres: the boundary before pixel k is at k - 0.5.)
        const exact = (drawn, dom) => Math.abs(drawn - dom) <= 0.75 ? dom : drawn;
        const gA = { left: exact(eA.left, b.x - A.x - 0.5), right: exact(eA.right, b.x + b.width - A.x - 0.5),
                     top: exact(eA.top, b.y - A.y - 0.5), bottom: exact(eA.bottom, b.y + b.height - A.y - 0.5) };
        geo = { dx: D.x + eB.left - (A.x + gA.left), dy: D.y + eB.top - (A.y + gA.top),
                dw: (eB.right - eB.left) - (gA.right - gA.left), dh: (eB.bottom - eB.top) - (gA.bottom - gA.top) };
        if (b.width >= 12 && b.height >= 12) {
          const thin = !iA, bw = st.border_width || 1;
          const rA = cornerRadius(A, eA, insA, outA, thin, bw), rB = cornerRadius(D, eB, insB, outB, thin, bw);
          if (o.debug) rec.radius = [rA, rB, eA, eB];
          if (rA !== null && rB !== null && Math.abs(rB - rA) >= tol(S, 2, 1)) {
            // The page's own measure against its CSS radius corrects the design's for how it was drawn.
            const now = st.radius || 0, want = Math.max(0, Math.round(rB + (now - rA)));
            if (want !== Math.round(now)) findings.push({ kind: "radius", text: "corner radius about " + want + "px in the design (" + Math.round(now) + "px here)", page: Math.round(now), design: want });
          }
        }
        // A shadow darkens what is around the element: measured against the colour further out, past it.
        const fA = farColor(S.pctx, b, 0, 0), fB = farColor(S.dctx, b, D.x + eB.left - (A.x + eA.left), D.y + eB.top - (A.y + eA.top));
        const nearD = nearPage.map((a) => [a[0] + offset.dx - D.x, a[1] + offset.dy - D.y, a[2] + offset.dx - D.x, a[3] + offset.dy - D.y]);
        const sA = ringDark(A, eA, fA ? rgbLab(fA.rgb) : outA, near), sB = ringDark(D, eB, fB ? rgbLab(fB.rgb) : outB, nearD);
        if (sA !== null && sB !== null && Math.abs(sB - sA) >= tol(S, 1.5, 0.5)) {
          findings.push({ kind: "shadow", text: sB > sA ? "the design's shadow or outline around it is stronger" : "its shadow or outline is stronger than the design's", page: r1(sA), design: r1(sB) });
        }
      }
      // A solid fill of the design's: its exact colour, for the colours read off text (see elementsFinish).
      if (iB && iB.share >= 0.5) rec.fill = hex(iB.rgb);
      if (iA && iB) {
        const e00 = dE2000(rgbLab(iA.rgb), rgbLab(iB.rgb));
        if (e00 >= tol(S, 2.5, 1)) findings.push({ kind: "background", text: "background " + (st.bg || hex(iA.rgb)) + "; the design's is " + hex(iB.rgb), page: st.bg || hex(iA.rgb), design: hex(iB.rgb), delta_e: r1(e00) });
      }
    }
  }
  rec.geometry = geo;
  // Text is found by its ink around where it belongs: lining up by shape misplaces text whose size
  // or spacing changed, unless the element around it (a button, a field) was measured.
  const textOff = geo ? { dx: Math.round(geo.dx), dy: Math.round(geo.dy) } : loc && !loc.flat && loc.ncc >= 0.9 ? offset : prior;
  // The box the text lies in (its own, as a button's, else its parent's), as large as the design has it.
  const holder = boxy ? { box: b, geo: geo, own: true } : parent ? { box: parent.box, geo: parent.geometry, own: false } : null;
  const cont = holder && { x: holder.box.x, y: holder.box.y, width: holder.box.width, height: holder.box.height, own: holder.own,
                           dw: holder.geo ? Math.max(0, holder.geo.dw) : 0, dh: holder.geo ? Math.max(0, holder.geo.dh) : 0 };
  const text = e.flags.text && e.text_box ? measureText(S, e, textOff, nearPage, findings, cont) : null;
  if (o.debug) rec.text = text;
  if (!found && !geo && !(text && text.dInk && !text.missing)) return absent();
  rec.found = true;
  // Where it sits against its parent: a parent that moved carries its children along.
  const disp = geo ? { dx: geo.dx, dy: geo.dy } : text && text.dInk ? { dx: text.dx, dy: text.dy } : offset;
  rec.disp = disp;
  let mx = disp.dx - pdisp.dx, my = disp.dy - pdisp.dy;
  rec.rel = { dx: mx, dy: my };
  rec.textual = !!(e.flags.text && !geo && text && text.dInk && text.same !== false);
  const least = tol(S, e.flags.text && !geo ? 3 : 2, 1);
  if (text && text.resized && text.dInk) {
    // Text of another size moves its ink (and renderers place it a pixel or so apart): a shift that
    // small comes from the size. Once the size matches, the next compare measures the position.
    const give = 0.5 * Math.abs((text.dInk.y1 - text.dInk.y0) - (text.pInk.y1 - text.pInk.y0)) + tol(S, 3, 1);
    if (Math.abs(my) <= give) my = 0;
    if (Math.abs(mx) <= give && !geo) mx = 0;
  }
  if (text && text.missing) mx = my = 0;
  // Only what shows a place of its own moves: a box, text, an image. A bare wrapper (a rule under
  // a bar, a translucent tint) is placed by its children, and they report their own moves.
  if (!boxy && !(text && text.dInk)) mx = my = 0;
  if (Math.abs(mx) >= least || Math.abs(my) >= least) findings.push({ kind: "moved", text: movedText(Math.abs(mx) >= least ? mx : 0, Math.abs(my) >= least ? my : 0), dx: Math.round(mx), dy: Math.round(my) });
  const grown = tol(S, 2, 1);
  if (geo && (Math.abs(geo.dw) >= grown || Math.abs(geo.dh) >= grown)) findings.push({ kind: "size", text: sizeText(Math.abs(geo.dw) >= grown ? geo.dw : 0, Math.abs(geo.dh) >= grown ? geo.dh : 0), dw: Math.round(geo.dw), dh: Math.round(geo.dh) });
  // Pixels decide for images, icons and boxes (a real area of them: more than a stray edge); text
  // has its own measures (and renders differently anyway).
  const differing = pm.content * (1 - pm.match / 100) * pm.step * pm.step;
  if (!findings.length && !e.flags.text && pm.content >= 30 && pm.match < 80 && differing >= Math.max(40, tol(S, 0.03, 0.005) * ownCount)) {
    findings.push({ kind: "content", text: "looks different in the design: other content, icon or image" });
  }
  // Near 100% accuracy what is only nearly the same is not enough: its pixels must match too.
  const exact = 100 - 6 * S.tol;
  if (!findings.length && S.tol < 0.5 && pm.content >= 1 && pm.match < exact) {
    findings.push({ kind: "pixels", text: r1(pm.match) + "% of its pixels match the design; " + r1(S.o.accuracy) + "% accuracy needs " + (exact >= 100 ? "all of them" : r1(exact) + "%"), match: r1(pm.match) });
  }
  rec.findings = findings;
  rec.status = findings.length ? "different" : pm.match < 97 ? "nearly" : "matches";
  if (rec.status === "different") rec.mask = pm.mask;
  return rec;
};

window.elementsBegin = async function (o) {
  const base = await window.renderCompare(Object.assign({}, o, { noBoard: true }));
  const ref = await load(o.ref, "reference"), cur = await load(o.page, "page");
  const g = prepare(o, ref, cur, true);
  const items = o.elements || [];
  // Each element's descendants (compared on their own) and its neighbours (the rest, bar ancestors).
  const kids = items.map(() => []), up = items.map(() => new Set());
  for (const e of items) for (let p = e.parent; p >= 0; p = items[p].parent) { kids[p].push(e.i); up[e.i].add(p); }
  const near = items.map((e) => {
    const b = e.box, out = [];
    for (const f of items) {
      if (f.i === e.i || up[e.i].has(f.i) || up[f.i].has(e.i)) continue;
      const c = f.box, reach = 3 * EM;
      if (c.x > b.x + b.width + reach || c.x + c.width < b.x - reach || c.y > b.y + b.height + reach || c.y + c.height < b.y - reach) continue;
      out.push(f.i);
    }
    return out;
  });
  const backdrop = (canvas) => { const R = region(ctxOf(canvas), 0, 0, canvas.width, canvas.height); return commonColor(R, 0, 0, R.w - 1, R.h - 1, (x, y) => !(x % 3) && !(y % 3)); };
  // A design of part of the page (its top, a section) says nothing about what lies past its edges.
  const H = g.element ? Infinity : g.refH + 2, W = g.element ? Infinity : g.refW + 2;
  const beyond = new Set(items.filter((e) => e.box.y + e.box.height > H || e.box.x + e.box.width > W).map((e) => e.i));
  window.__el = { o: o, g: g, base: base, items: items, kids: kids, near: near, results: new Array(items.length), next: 0, wide: 0, beyond: beyond,
                  jpeg: /^data:image\/jpe?g/i.test(String(o.ref || "")), tol: Number.isFinite(o.tolerance) ? o.tolerance : 1,
                  prior: { dx: 0, dy: 0 }, pctx: ctxOf(g.pageCanvas), dctx: ctxOf(g.refCanvas), pp: pyramid(g.pageCanvas), dp: pyramid(g.refCanvas),
                  backdrop: [backdrop(g.pageCanvas), backdrop(g.refCanvas)] };
  return { count: items.length };
};

window.elementsStep = function (count) {
  const S = window.__el;
  const end = Math.min(S.items.length, S.next + count);
  for (; S.next < end; S.next++) S.results[S.next] = S.beyond.has(S.next) ? null : measureElement(S, S.items[S.next]);
  return { done: S.next >= S.items.length, at: S.next };
};

window.compareElements = async function (o) {
  await window.elementsBegin(o);
  window.elementsStep(Infinity);
  return window.elementsFinish();
};

window.elementsFinish = function () {
  const S = window.__el, o = S.o, g = S.g;
  const res = S.results.filter(Boolean);
  // Designs reuse their colours. A text colour read off thin, blended strokes is only close; when
  // it is that close to a solid colour the design shows (a fill) or the page's styles use, it is that one.
  const palette = new Map();
  const addColor = (value) => { const rgb = hexRgb(value); if (rgb) palette.set(hex(rgb), rgbLab(rgb)); };
  for (const r of res) addColor(r.fill);
  for (const e of S.items) { const st = e.style || {}; addColor(st.color); addColor(st.bg); addColor(st.border); }
  for (const r of res) for (const f of r.findings || []) {
    if (f.kind !== "text_color" || !hexRgb(f.design)) continue;
    const lab = rgbLab(hexRgb(f.design)), now = hexRgb(f.page) ? hex(hexRgb(f.page)) : "";
    let best = "", least = 3.5;
    for (const [value, other] of palette) { const d = dE2000(lab, other); if (d < least && value !== now) { least = d; best = value; } }
    if (best) { f.design = best; f.text = "text colour " + f.page + "; the design's is " + best; }
  }
  // Siblings of an element: the one right above it (overlapping it across), the one right before it
  // on its row (overlapping it down).
  const siblings = (r, keep) => res.filter((q) => q !== r && q.parent === r.parent && q.i < r.i && (!keep || keep(q)));
  const above = (r, keep) => siblings(r, keep).filter((q) => q.box.y + q.box.height <= r.box.y + 2 &&
    q.box.x < r.box.x + r.box.width && q.box.x + q.box.width > r.box.x).pop();
  const before = (r, keep) => siblings(r, keep).filter((q) => q.box.x + q.box.width <= r.box.x + 2 &&
    q.box.y < r.box.y + r.box.height && q.box.y + q.box.height > r.box.y).pop();
  // When a line of text's line height grows, the extra space goes half above and half below it:
  // the line moves by d and what follows it by 2d. Say so on the line, where the fix is.
  for (const r of res) {
    const moved = (r.findings || []).find((f) => f.kind === "moved" && f.dy);
    if (!moved) continue;
    const line = above(r);
    const lh = line ? (S.items[line.i].style || {}).line_height : 0;
    // (Renderers place text a pixel or so apart, so the halves need only roughly agree.)
    if (line && line.textual && lh && Math.abs(moved.dy) >= 4 && Math.abs(line.rel.dy) >= 1.5 &&
        Math.abs(2 * line.rel.dy - moved.dy) <= Math.max(2, 0.35 * Math.abs(moved.dy)) &&
        !(line.findings || []).some((f) => f.kind === "line_height" || f.kind === "font_size" || f.kind === "wrap")) {
      const want = Math.round(lh + moved.dy);
      line.findings = (line.findings || []).filter((f) => f.kind !== "moved");
      line.findings.push({ kind: "line_height", text: "line height about " + want + "px in the design (" + Math.round(lh) + "px here), which moves what follows it",
                           page: Math.round(lh), design: want });
      line.status = "different";
    }
  }
  // A move right after a sibling changed size is that change's doing: the one above it for a move
  // up or down, the one before it on its row for a move sideways.
  const resized = (q) => (q.findings || []).some((f) => ["size", "font_size", "line_height", "wrap", "text", "text_width", "letter_spacing", "missing"].includes(f.kind));
  for (const r of res) {
    const moved = (r.findings || []).find((f) => f.kind === "moved");
    if (!moved) continue;
    const up = moved.dy ? above(r, resized) : null, side = moved.dx ? before(r, resized) : null;
    const cause = Math.abs(moved.dx) > Math.abs(moved.dy) ? side : up;
    if (cause) moved.text += " (" + cause.name.slice(0, 48) + (cause === up ? " above" : " before") + " it differs: fix that first)";
  }
  const bad = res.filter((r) => r.status === "different" || r.status === "missing").sort((p, q) => (p.box.y - q.box.y) || (p.box.x - q.box.x));
  bad.forEach((r, k) => { r.n = k + 1; });
  const counts = { compared: res.length, matches: 0, nearly: 0, different: 0, missing: 0 };
  for (const r of res) counts[r.status]++;
  // Parts of the design the page lacks: what the whole comparison found missing where no element is.
  const lacking = [];
  for (const a of S.base.differences || []) {
    if (a.kind !== "missing" || a.minor) continue;
    const hit = res.some((r) => {
      const d = r.disp || r.offset, x0 = Math.max(a.x, r.box.x + d.dx), y0 = Math.max(a.y, r.box.y + d.dy);
      const x1 = Math.min(a.x + a.width, r.box.x + d.dx + r.box.width), y1 = Math.min(a.y + a.height, r.box.y + d.dy + r.box.height);
      return x1 > x0 && y1 > y0 && (x1 - x0) * (y1 - y0) >= 0.5 * a.width * a.height && r.status !== "missing";
    });
    if (!hit) lacking.push({ n: bad.length + lacking.length + 1, x: a.x, y: a.y, width: a.width, height: a.height });
  }
  // The page's background against the design's.
  let background = null;
  const [bp, bd] = S.backdrop;
  if (bp && bd && bp.share > 0.2 && bd.share > 0.2) {
    const e00 = dE2000(rgbLab(bp.rgb), rgbLab(bd.rgb));
    if (e00 >= 2) background = { page: hex(bp.rgb), design: hex(bd.rgb), delta_e: r1(e00), text: "the page's background is " + hex(bp.rgb) + "; the design's is " + hex(bd.rgb) };
  }
  const verdict = !bad.length && !lacking.length && !background ? (counts.nearly ? "nearly identical" : S.base.verdict === "identical" ? "identical" : "nearly identical") : "different";
  const exported = bad.map((r) => {
    const d = r.disp || r.offset, geo = r.geometry;
    const out = { n: r.n, element: r.name, selector: r.selector, status: r.status, box: r.box,
                  findings: (r.findings || []).map((f) => f.text) };
    if (r.match !== undefined) out.match = r.match;
    if (r.status !== "missing") out.design_box = { x: r1(r.box.x + d.dx), y: r1(r.box.y + d.dy), width: r1(r.box.width + (geo ? geo.dw : 0)), height: r1(r.box.height + (geo ? geo.dh : 0)) };
    for (const f of r.findings || []) {
      if (f.kind === "moved") out.moved = { dx: f.dx, dy: f.dy };
      else if (f.kind === "size") out.size = { dw: f.dw, dh: f.dh };
      else if (f.page !== undefined && f.design !== undefined) out[f.kind] = { page: f.page, design: f.design };
    }
    return out;
  });
  const result = { verdict: verdict, counts: counts, elements: exported, lacking: lacking, background: background,
                   statuses: res.map((r) => [r.selector, r.status]), beyond: S.beyond.size,
                   similarity: S.base.similarity, structure: S.base.structure, notes: S.base.notes, shifts: S.base.shifts,
                   page_size: S.base.page_size, reference_size: S.base.reference_size, reference_scale: S.base.reference_scale,
                   reference_region: S.base.reference_region, same_region: S.base.same_region, snapped_crop: S.base.snapped_crop, board: null };
  if (o.debug) result.debug = res.map((r) => ({ selector: r.selector, status: r.status, offset: r.offset, ncc: r.ncc, match: r.match,
    radius: r.radius, disp: r.disp, findings: (r.findings || []).map((f) => f.text),
    geometry: r.geometry, text: r.text && { p: r.text.pInk && { ...r.text.pInk, lines: r.text.pInk.lines }, d: r.text.dInk && { ...r.text.dInk }, sims: r.text.sims } }));
  if (o.noBoard) return result;
  // The board: the design and the page with what differs numbered, then a close-up of each.
  const board = document.getElementById("board");
  board.textContent = "";
  const CW = Math.max(g.PW, g.refW), k = Math.min(o.columnWidth / CW, 1, o.maxHeight / Math.max(g.refH, g.PH));
  const tone = verdict === "identical" ? "identical" : verdict === "different" ? "different" : "nearly";
  const good = counts.matches + counts.nearly, toFix = bad.length + lacking.length + (background ? 1 : 0);
  const tally = [["ok", counts.matches, "match"]];
  if (counts.nearly) tally.push(["nearly", counts.nearly, "nearly match"]);
  if (counts.different) tally.push(["s-different", counts.different, "differ"]);
  if (counts.missing) tally.push(["s-missing", counts.missing, "on the page, not in the design"]);
  if (lacking.length) tally.push(["s-lacking", lacking.length, "in the design, not on the page"]);
  if (background) tally.push(["s-different", null, "background " + background.page + " \u2192 " + background.design]);
  // What to fix first: the first few changes, those that are the same change (four links in the
  // same new colour) as one.
  const fix = [];
  for (const r of bad) {
    const text = ((r.findings || [])[0] || {}).text || "";
    const same = fix.find((f) => f.text === text);
    if (same) { same.names.push(r.name); continue; }
    if (fix.length === 3) continue;
    fix.push({ n: r.n, cls: r.status === "missing" ? "missing" : "", names: [r.name], text: text });
  }
  for (const f of fix) {
    const quoted = f.names.map((name) => /"(.*)"/.exec(name)), all = quoted.every(Boolean) && f.names.every((name) => name.split(" ")[0] === f.names[0].split(" ")[0]);
    f.name = f.names.length === 1 ? f.names[0].slice(0, 48) : all ? f.names[0].split(" ")[0] + " " + quoted.map((m) => "\u201c" + m[1].slice(0, 18) + "\u201d").slice(0, 4).join(", ") +
      (f.names.length > 4 ? " +" + (f.names.length - 4) : "") : f.names.length + " \u00d7 " + f.names[0].split(" ")[0];
  }
  const shown = S.beyond.size ? " \u00b7 " + S.beyond.size + " past the design's edge not compared" : "";
  board.append(boardHeader({
    tone: tone,
    verdict: verdict === "identical" ? "Identical" : verdict === "different" ? "Different" : "Nearly identical",
    title: verdict === "different" ? toFix + (toFix === 1 ? " thing" : " things") + " to change to match the design" : "Every element matches the design",
    design: o.refLabel + " \u00b7 " + g.IW + "\u00d7" + g.IH, page: o.pageLabel + " \u00b7 " + g.PW + "\u00d7" + g.PH + shown,
    counts: tally, fix: fix, fixTitle: "Fix first \u00b7 top to bottom, each one numbered below",
    stats: [
      { label: "Elements", value: good, unit: "/" + counts.compared, meter: counts.compared ? 100 * good / counts.compared : 100, note: "match or nearly match" },
      { label: "Pixels", value: S.base.similarity, unit: "%", note: "of the content the same" },
      { label: "To fix", value: toFix, note: toFix ? "numbered below" : "nothing" },
    ],
  }));
  const view = (src, sw, sh) => {
    const c = make(Math.max(1, Math.round(sw * k)), Math.max(1, Math.round(sh * k)));
    const x = c.getContext("2d");
    x.imageSmoothingQuality = "high";
    x.drawImage(src, 0, 0, sw, sh, 0, 0, c.width, c.height);
    return c;
  };
  const framed = (canvas, boxes) => {
    const wrap = document.createElement("div");
    wrap.className = "frame";
    wrap.appendChild(canvas);
    for (const a of boxes) {
      const box = document.createElement("div");
      box.className = "box s-" + a.cls + (a.y * k < 18 ? " low" : "");
      box.style.left = (1 + a.x * k - 2) + "px";
      box.style.top = (1 + a.y * k - 2) + "px";
      box.style.width = (a.width * k + 4) + "px";
      box.style.height = (a.height * k + 4) + "px";
      const n = document.createElement("i");
      n.textContent = a.n;
      box.appendChild(n);
      wrap.appendChild(box);
    }
    return wrap;
  };
  const figure = (name, caption, content) => {
    const f = document.createElement("figure");
    const cap = document.createElement("figcaption");
    cap.style.maxWidth = Math.max(160, Math.round(CW * k)) + "px";
    const tag = document.createElement("span");
    tag.className = "tag " + (name === "Design" ? "design" : "page");
    tag.textContent = name;
    const meta = document.createElement("span");
    meta.textContent = caption;
    cap.append(tag, meta);
    f.append(cap, content);
    return f;
  };
  const onDesign = bad.filter((r) => r.status !== "missing").map((r) => {
    const d = r.disp || r.offset, geo = r.geometry;
    return { n: r.n, cls: "different", x: r.box.x + d.dx, y: r.box.y + d.dy, width: r.box.width + (geo ? geo.dw : 0), height: r.box.height + (geo ? geo.dh : 0) };
  }).concat(lacking.map((a) => ({ n: a.n, cls: "lacking", x: a.x, y: a.y, width: a.width, height: a.height })));
  const onPage = bad.map((r) => ({ n: r.n, cls: r.status === "missing" ? "missing" : "different", x: r.box.x, y: r.box.y, width: r.box.width, height: r.box.height }));
  const cols = document.createElement("div");
  cols.className = "cols";
  cols.append(
    figure("Design", o.refLabel + " · " + g.IW + "×" + g.IH, framed(view(g.refCanvas, g.refW, g.refH), onDesign)),
    figure("Your page", o.pageLabel + " · " + g.PW + "×" + g.PH, framed(view(g.pageCanvas, g.PW, g.PH), onPage))
  );
  board.appendChild(cols);
  if (bad.length) {
    const cards = document.createElement("div");
    cards.className = "cards";
    const crop = (canvas, x, y, w, h, q) => {
      const c = make(Math.max(1, Math.round(w * q)), Math.max(1, Math.round(h * q)));
      const x2 = c.getContext("2d");
      x2.imageSmoothingEnabled = q < 1;
      x2.fillStyle = "#ffffff";
      x2.fillRect(0, 0, c.width, c.height);
      x2.drawImage(canvas, x, y, w, h, 0, 0, c.width, c.height);
      return c;
    };
    for (const r of bad.slice(0, 9)) {
      const card = document.createElement("div");
      card.className = "card";
      const title = document.createElement("h4");
      const num = document.createElement("i");
      num.textContent = r.n;
      if (r.status === "missing") num.className = "s-missing";
      title.append(num, r.name.slice(0, 70));
      const list = document.createElement("ul");
      for (const f of (r.findings || []).slice(0, 4)) { const li = document.createElement("li"); li.textContent = f.text; list.appendChild(li); }
      // Text is shown by its own box (a heading's block is often far wider than its words).
      const item = S.items[r.i], f = r.textual && item.text_box ? item.text_box : r.box;
      const m = Math.max(8, Math.round(0.12 * Math.max(f.width, f.height))), d = r.disp || r.offset, w = f.width + 2 * m, h = f.height + 2 * m;
      const q = Math.min(3, 225 / w, 150 / h);
      const pair = document.createElement("div");
      pair.className = "pair";
      const side = (label, canvas) => { const col = document.createElement("div"); col.append(label, canvas); return col; };
      pair.append(
        side("Design", crop(g.refCanvas, f.x - m + d.dx, f.y - m + d.dy, w, h, q)),
        side("Page", crop(g.pageCanvas, f.x - m, f.y - m, w, h, q))
      );
      card.append(title, list, pair);
      cards.appendChild(card);
    }
    board.appendChild(cards);
  }
  const box = board.getBoundingClientRect();
  result.board = { width: Math.ceil(box.width), height: Math.ceil(box.height) };
  return result;
};
</script></body></html>"""

BROWSER_SETTINGS_PATH = os.path.expanduser("~/.livecode/browser.json")
DEFAULT_MATCH_THRESHOLD = 85.0
DEFAULT_DESIGN_ACCURACY = 90.0
MIN_DESIGN_ACCURACY = 50.0


_settings_cache: tuple[str, tuple[int, int], dict[str, Any]] | None = None
_settings_lock = threading.Lock()


def _read_browser_settings() -> dict[str, Any]:
    # Cached by the file's mtime and size: the live view asks for view_quality every few ms,
    # and the file is shared with the Chrome connection setting, which writes it directly.
    global _settings_cache
    path = BROWSER_SETTINGS_PATH
    try:
        info = os.stat(path)
        stamp = (info.st_mtime_ns, info.st_size)
    except OSError:
        stamp = (0, -1)
    cached = _settings_cache
    if cached is not None and cached[0] == path and cached[1] == stamp:
        return dict(cached[2])
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    _settings_cache = (path, stamp, data)
    return dict(data)


def _write_browser_settings(settings: dict[str, Any]) -> None:
    global _settings_cache
    with _settings_lock:
        os.makedirs(os.path.dirname(BROWSER_SETTINGS_PATH), exist_ok=True)
        tmp = BROWSER_SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        os.replace(tmp, BROWSER_SETTINGS_PATH)
        _settings_cache = None


_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


def _flag(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    word = str(value).strip().lower() if isinstance(value, str) else None
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise BrowserError(f"{name} is true or false.")


def _saved_flag(key: str, default: bool) -> bool:
    try:
        return _flag(_read_browser_settings().get(key, default), key)
    except BrowserError:
        return default


VIEW_QUALITIES = ("sharp", "standard")
DEFAULT_VIEW_QUALITY = "sharp"
DEFAULT_VIEWPORTS = ("fit", "desktop")
DEFAULT_DEFAULT_VIEWPORT = "fit"


def _choice(value: Any, allowed: tuple[str, ...], name: str) -> str:
    word = str(value or "").strip().lower()
    if word not in allowed:
        raise BrowserError(f"{name} is one of: {', '.join(allowed)}.")
    return word


def _saved_choice(key: str, allowed: tuple[str, ...], default: str) -> str:
    word = str(_read_browser_settings().get(key) or "").strip().lower()
    return word if word in allowed else default


def design_gate_enabled() -> bool:
    return _saved_flag("design_gate", True)


def agent_tabs_enabled() -> bool:
    return _saved_flag("agent_tabs", True)


def view_quality() -> str:
    return _saved_choice("view_quality", VIEW_QUALITIES, DEFAULT_VIEW_QUALITY)


def default_viewport() -> str:
    return _saved_choice("default_viewport", DEFAULT_VIEWPORTS, DEFAULT_DEFAULT_VIEWPORT)


def _default_viewport_size(kind: str) -> dict[str, int]:
    return dict(DESKTOP_VIEWPORT if kind == "desktop" else VIEWPORT)


def browser_settings() -> dict[str, Any]:
    _migrate_match_threshold()
    return {
        **accuracy_settings(),
        "reduce_automation_signals": automation_signals_reduced(),
        "design_gate": design_gate_enabled(),
        "agent_tabs": agent_tabs_enabled(),
        "view_quality": view_quality(),
        "default_viewport": default_viewport(),
        **launch_settings(),
        "env_overrides": launch_env_overrides(),
        "allowed": {
            "design_accuracy": [MIN_DESIGN_ACCURACY, 100.0],
            "view_quality": list(VIEW_QUALITIES),
            "default_viewport": list(DEFAULT_VIEWPORTS),
            "device_scale": [1.0, 3.0],
        },
    }


def save_browser_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Validates every value given before saving any of them; unknown keys are ignored."""
    updates: dict[str, Any] = {}
    if "reduce_automation_signals" in data:
        updates["reduce_automation_signals"] = _flag(data.get("reduce_automation_signals"), "reduce_automation_signals")
    if "design_accuracy" in data:
        updates["design_accuracy"] = _design_accuracy_value(data.get("design_accuracy"))
    elif "match_threshold" in data:
        updates["design_accuracy"] = _accuracy_from_threshold(data.get("match_threshold"))
    if "design_gate" in data:
        updates["design_gate"] = _flag(data.get("design_gate"), "design_gate")
    if "agent_tabs" in data:
        updates["agent_tabs"] = _flag(data.get("agent_tabs"), "agent_tabs")
    if "view_quality" in data:
        updates["view_quality"] = _choice(data.get("view_quality"), VIEW_QUALITIES, "view_quality")
    if "default_viewport" in data:
        updates["default_viewport"] = _choice(data.get("default_viewport"), DEFAULT_VIEWPORTS, "default_viewport")
    if "headless" in data:
        updates["headless"] = _flag(data.get("headless"), "headless")
    if "executable_path" in data:
        updates["executable_path"] = _executable_value(data.get("executable_path"))
    if "proxy" in data:
        updates["proxy"] = _proxy_value(data.get("proxy"))
    if "device_scale" in data:
        updates["device_scale"] = _device_scale_value(data.get("device_scale"))
    if updates:
        settings = _read_browser_settings()
        before = launch_settings()
        if "design_accuracy" in updates:
            settings.pop("match_threshold", None)
        settings.update(updates)
        _write_browser_settings(settings)
        if launch_settings() != before:
            _restart_local_browsers()
    return browser_settings()


# Launch options for the browser LiveCode starts itself. An environment variable on the server wins
# over the saved value, so a deployment can pin them.
_LAUNCH_ENV = {
    "executable_path": "LIVECODE_BROWSER_EXECUTABLE",
    "proxy": "LIVECODE_BROWSER_PROXY",
    "headless": "LIVECODE_BROWSER_HEADLESS",
    "device_scale": "LIVECODE_BROWSER_SCALE",
}
_PROXY_URL = re.compile(r"^(https?|socks[45]?)://[^\s/]+(:\d{1,5})?/?$", re.I)


def launch_env_overrides() -> dict[str, str]:
    return {key: env for key, env in _LAUNCH_ENV.items() if os.environ.get(env, "").strip()}


def launch_settings() -> dict[str, Any]:
    saved = _read_browser_settings()
    headless_env = os.environ.get("LIVECODE_BROWSER_HEADLESS", "").strip().lower()
    if headless_env in _TRUE_WORDS or headless_env in _FALSE_WORDS:
        headless = headless_env in _TRUE_WORDS
    else:
        headless = saved.get("headless", True) not in (False, "false", 0)
    return {
        "headless": headless,
        "executable_path": os.environ.get("LIVECODE_BROWSER_EXECUTABLE", "").strip() or str(saved.get("executable_path") or "").strip(),
        "proxy": os.environ.get("LIVECODE_BROWSER_PROXY", "").strip() or str(saved.get("proxy") or "").strip(),
        "device_scale": _device_scale_factor(),
    }


def _executable_value(value: Any) -> str:
    path = os.path.expanduser(str(value or "").strip())
    if not path:
        return ""
    if path.endswith(".app") and os.path.isdir(path):
        inner = os.path.join(path, "Contents", "MacOS", os.path.basename(path)[:-4])
        if os.path.isfile(inner):
            path = inner
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise BrowserError(f"No browser program at {path}. Enter the path to a Chrome or Chromium executable.")
    return path


def _proxy_value(value: Any) -> str:
    text = str(value or "").strip()
    if text and not _PROXY_URL.match(text):
        raise BrowserError("Enter a proxy like http://127.0.0.1:8080 or socks5://127.0.0.1:1080.")
    return text


def _device_scale_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number) or not 1.0 <= number <= 3.0:
        raise BrowserError("Pixel density is a number from 1 to 3.")
    return round(number, 2)


def _close_local(worker: "_Worker") -> None:
    if not worker.remote:
        _reset_engine(worker)
    if worker.local is not None:
        try:
            worker.local.close()
        except Exception:
            pass
        worker.local = None


def _restart_local_browsers() -> None:
    """Closes browsers LiveCode launched, so the next action starts one with the new launch options.
    An attached Chrome is left alone."""
    for worker in _all_workers():
        if worker._thread is not None and worker._thread.is_alive():
            try:
                worker.call(_close_local, worker, label="relaunch", timeout=40)
            except Exception:
                pass


# Attaching to a Chrome over the DevTools protocol.
DEBUG_PROFILE_DIR = os.path.expanduser("~/.livecode/chrome-debug-profile")
DEFAULT_DEBUG_PORT = 9222
_DETECT_PORTS = (9222, 9223, 9224, 9229)


def probe_cdp(url: str, timeout: float = 2.0) -> dict[str, Any]:
    """Asks a DevTools endpoint for its version without attaching to it.
    {ok, endpoint, browser, protocol, user_agent} or {ok: False, endpoint, error}."""
    import urllib.request

    endpoint = str(url or "").strip().rstrip("/")
    if not endpoint or not _CDP_URL.match(endpoint):
        return {"ok": False, "endpoint": endpoint, "error": "Enter Chrome's debugging address, e.g. http://127.0.0.1:9222."}
    http = re.sub(r"^ws", "http", endpoint, flags=re.I)
    http = re.sub(r"/devtools/browser/.*$", "", http)
    try:
        with urllib.request.urlopen(http + "/json/version", timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:
        return {"ok": False, "endpoint": endpoint, "error": f"Nothing answered at {http} ({_first_line(exc)})."}
    if not isinstance(info, dict) or not info.get("webSocketDebuggerUrl"):
        return {"ok": False, "endpoint": endpoint, "error": f"{http} answered, but it is not a Chrome debugging endpoint."}
    return {
        "ok": True,
        "endpoint": endpoint,
        "browser": str(info.get("Browser") or ""),
        "protocol": str(info.get("Protocol-Version") or ""),
        "user_agent": str(info.get("User-Agent") or ""),
    }


def detect_cdp() -> dict[str, Any]:
    """The first Chrome debugging endpoint answering on this machine's usual ports."""
    tried = []
    for port in _DETECT_PORTS:
        url = f"http://127.0.0.1:{port}"
        tried.append(url)
        result = probe_cdp(url, timeout=0.8)
        if result.get("ok"):
            return {**result, "found": True}
    return {"ok": True, "found": False, "tried": tried}


def _chrome_candidates() -> list[str]:
    configured = launch_settings()["executable_path"]
    out = [configured] if configured else []
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Google Chrome Canary", "Chromium", "Microsoft Edge", "Brave Browser"):
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                out.append(os.path.join(root, f"{app}.app", "Contents", "MacOS", app))
    else:
        import shutil

        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                out.append(found)
    return [p for p in out if p and os.path.isfile(p) and os.access(p, os.X_OK)]


def launch_debug_chrome(port: int = DEFAULT_DEBUG_PORT) -> dict[str, Any]:
    """Starts a visible Chrome with remote debugging on a LiveCode profile and attaches to it.
    Reuses a Chrome already answering on the port."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise BrowserError("The debugging port is a number from 1024 to 65535.") from None
    if not 1024 <= port <= 65535:
        raise BrowserError("The debugging port is a number from 1024 to 65535.")
    url = f"http://127.0.0.1:{port}"
    if not probe_cdp(url, timeout=0.8).get("ok"):
        candidates = _chrome_candidates()
        if not candidates:
            raise BrowserError("Could not find Chrome on this machine. Set its path under Settings > Browser > Browser program.")
        os.makedirs(DEBUG_PROFILE_DIR, exist_ok=True)
        args = [
            candidates[0],
            f"--remote-debugging-port={port}",
            f"--user-data-dir={DEBUG_PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        try:
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise BrowserError(f"Could not start {candidates[0]} ({exc.strerror or exc}).") from exc
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if probe_cdp(url, timeout=0.8).get("ok"):
                break
            time.sleep(0.3)
        else:
            raise BrowserError(f"Chrome started but did not open its debugging port {port}. Close other Chrome windows using this profile and try again.")
    status = set_cdp_endpoint(url)
    return {**status, "profile": DEBUG_PROFILE_DIR}


def automation_signals_reduced() -> bool:
    return _read_browser_settings().get("reduce_automation_signals", True) not in (False, "false", 0)


def save_reduce_automation(value: Any) -> dict[str, Any]:
    settings = _read_browser_settings()
    settings["reduce_automation_signals"] = _flag(value, "reduce_automation_signals")
    _write_browser_settings(settings)
    return {"reduce_automation_signals": automation_signals_reduced()}


_NORMALIZE_JS = """
(() => {
  const define = (object, name, getter) => { try { Object.defineProperty(object, name, { get: getter, configurable: true }); } catch (e) {} };
  define(Navigator.prototype, "webdriver", () => undefined);
  if (!window.chrome) { try { window.chrome = { runtime: {}, app: { isInstalled: false } }; } catch (e) {} }
  define(Navigator.prototype, "languages", () => ["en-US", "en"]);
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      const names = ["PDF Viewer", "Chrome PDF Viewer", "Chromium PDF Viewer", "Microsoft Edge PDF Viewer", "WebKit built-in PDF"];
      define(Navigator.prototype, "plugins", () => Object.assign(names.map((name) => ({ name: name, filename: "internal-pdf-viewer", description: "Portable Document Format" })), { item(i) { return this[i]; }, namedItem(n) { return this.find((p) => p.name === n) || null; }, refresh() {} }));
    }
  } catch (e) {}
  try {
    const query = navigator.permissions && navigator.permissions.query.bind(navigator.permissions);
    if (query) navigator.permissions.query = (parameters) => (parameters && parameters.name === "notifications"
      ? Promise.resolve({ state: Notification.permission === "default" ? "prompt" : Notification.permission, onchange: null })
      : query(parameters));
  } catch (e) {}
})();
"""


def _accuracy_from_threshold(value: Any) -> float:
    """The design accuracy that gives the same match threshold as an older saved match_threshold
    (the inverse of match_threshold() below), so a user's stricter or looser choice carries over."""
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DESIGN_ACCURACY
    if not math.isfinite(threshold):
        return DEFAULT_DESIGN_ACCURACY
    accuracy = DEFAULT_DESIGN_ACCURACY + (threshold - DEFAULT_MATCH_THRESHOLD) / 1.5
    return round(min(100.0, max(MIN_DESIGN_ACCURACY, accuracy)), 1)


def _design_accuracy_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BrowserError(f"Design accuracy is a percentage from {MIN_DESIGN_ACCURACY:g} to 100.") from None
    if not math.isfinite(number) or not MIN_DESIGN_ACCURACY <= number <= 100:
        raise BrowserError(f"Design accuracy is a percentage from {MIN_DESIGN_ACCURACY:g} to 100.")
    return round(number, 1)


def design_accuracy() -> float:
    settings = _read_browser_settings()
    if "design_accuracy" not in settings and "match_threshold" in settings:
        return _accuracy_from_threshold(settings.get("match_threshold"))
    try:
        value = float(settings.get("design_accuracy", DEFAULT_DESIGN_ACCURACY))
    except (TypeError, ValueError):
        return DEFAULT_DESIGN_ACCURACY
    if not math.isfinite(value):
        return DEFAULT_DESIGN_ACCURACY
    return min(100.0, max(MIN_DESIGN_ACCURACY, value))


def _migrate_match_threshold() -> None:
    """Rewrites an older saved match_threshold as the design accuracy it stands for."""
    try:
        settings = _read_browser_settings()
        if "match_threshold" in settings:
            if "design_accuracy" not in settings:
                settings["design_accuracy"] = _accuracy_from_threshold(settings.get("match_threshold"))
            settings.pop("match_threshold", None)
            _write_browser_settings(settings)
    except OSError:
        pass


def design_tolerance() -> float:
    return round((100.0 - design_accuracy()) / (100.0 - DEFAULT_DESIGN_ACCURACY), 3)


def match_threshold() -> float:
    accuracy = design_accuracy()
    return round(min(100.0, max(10.0, DEFAULT_MATCH_THRESHOLD + (accuracy - DEFAULT_DESIGN_ACCURACY) * 1.5)), 1)


def save_design_accuracy(value: Any) -> dict[str, Any]:
    number = _design_accuracy_value(value)
    settings = _read_browser_settings()
    settings["design_accuracy"] = number
    settings.pop("match_threshold", None)
    _write_browser_settings(settings)
    return accuracy_settings()


def save_match_threshold(value: Any) -> dict[str, Any]:
    """Older clients: a match threshold is saved as the design accuracy it stands for."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BrowserError("The match threshold is a percentage from 1 to 100.") from None
    if not 1 <= number <= 100:
        raise BrowserError("The match threshold is a percentage from 1 to 100.")
    return save_design_accuracy(_accuracy_from_threshold(number))


def accuracy_settings() -> dict[str, Any]:
    return {"design_accuracy": design_accuracy(), "default_design_accuracy": DEFAULT_DESIGN_ACCURACY,
            "min_design_accuracy": MIN_DESIGN_ACCURACY, "match_threshold": match_threshold(),
            "default_match_threshold": DEFAULT_MATCH_THRESHOLD}


NOISE_PIXELS = 60
NOISE_DENSITY = 0.15
NEARLY_IDENTICAL_PIXELS = 400
NEARLY_IDENTICAL_PCT = 0.5


def _tools_page() -> Any:
    page = getattr(_worker, "tools_page", None)
    if page is not None:
        try:
            owner = page.context.browser
            if not page.is_closed() and owner is not None and owner.is_connected():
                return page
        except Exception:
            pass
    try:
        browser = _worker.local_browser()
    except BrowserUnavailable:
        if not _worker.remote:
            raise
        browser = _worker.chromium()
    context = browser.new_context(viewport={"width": 1800, "height": 1000}, device_scale_factor=1)
    page = context.new_page()
    _worker.tools_page = page
    return page


COMPARE_PAGES_KEPT = 2


@contextlib.contextmanager
def _compare_page() -> Iterator[Any]:
    tools = _tools_page()
    kept: list[Any] = getattr(_worker, "compare_pages", None) or []
    _worker.compare_pages = kept
    page = None
    while kept and page is None:
        page = kept.pop()
        try:
            if page.is_closed() or page.context is not tools.context:
                page = None
        except Exception:
            page = None
    if page is None:
        page = _in_background(tools.context, "new_page")
    done = False
    try:
        page.set_viewport_size({"width": 1800, "height": 1000})
        page.set_content(_COMPARE_PAGE)
        yield page
        done = True
    finally:
        try:
            if done and len(kept) < COMPARE_PAGES_KEPT:
                page.evaluate("() => { window.__el = null; document.body.replaceChildren(); }")
                kept.append(page)
            else:
                page.close()
        except Exception:
            pass


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime or _sniff_image_mime(data) or 'image/png'};base64," + base64.b64encode(data).decode("ascii")


def _compare_options(reference: _Reference, page_png: bytes, page_label: str, *, mode: str = "view",
                     region: dict[str, int] | None = None, layout_width: int = 0, ref_crop: dict[str, float] | None = None,
                     ref_scale: float = 0.0, auto_region: bool = True, target_kind: str = "element",
                     text_target: bool = False, snap: bool = False) -> dict[str, Any]:
    return {
        "ref": _data_url(reference.data, reference.mime),
        "page": _data_url(page_png, _sniff_image_mime(page_png) or "image/png"),
        "refLabel": reference.label,
        "pageLabel": page_label,
        "deltaE": COMPARE_DELTA_E,
        "flatDeltaE": COMPARE_FLAT_DELTA_E,
        "smoothDeltaE": COMPARE_SMOOTH_DELTA_E,
        "ssimNoise": COMPARE_SSIM_NOISE,
        "ssimRadius": COMPARE_SSIM_RADIUS,
        "columnWidth": 560,
        "maxHeight": 1600,
        "mode": mode,
        "region": region,
        "layoutWidth": layout_width,
        "refCrop": ref_crop,
        "refScale": ref_scale,
        "autoRegion": auto_region,
        "targetKind": target_kind,
        "textTarget": text_target,
        "snap": snap,
        "nearlyPixels": round(NEARLY_IDENTICAL_PIXELS * design_tolerance()),
        "nearlyPct": NEARLY_IDENTICAL_PCT,
        "matchThreshold": match_threshold(),
        "accuracy": design_accuracy(),
        "tolerance": design_tolerance(),
        "noiseDensity": NOISE_DENSITY,
        "noisePixels": round(NOISE_PIXELS * design_tolerance()),
    }


def _compare_error(exc: Exception) -> BrowserError:
    return BrowserError(_first_line(exc).replace("Page.evaluate: ", "").replace("Error: ", ""))


def _board_shot(tools: Any, board: dict[str, Any] | None) -> bytes:
    board = board or {}
    tools.set_viewport_size({
        "width": max(600, int(board.get("width") or 1800) + 8),
        "height": max(300, min(int(board.get("height") or 1000) + 8, 4000)),
    })
    return _in_background(tools.locator("#board"), "screenshot", type="jpeg", quality=88, timeout=20_000, scale="css")


def _render_elements(reference: _Reference, page_png: bytes, page_label: str, elements: list[dict[str, Any]],
                     **geometry: Any) -> tuple[bytes, dict[str, Any]]:
    options = _compare_options(reference, page_png, page_label, **geometry)
    options["elements"] = elements
    with _compare_page() as tools:
        try:
            stats = _in_background(tools, "evaluate", "o => window.compareElements(o)", options)
        except Exception as exc:
            raise _compare_error(exc) from exc
        return _board_shot(tools, stats.get("board")), stats


def _render_comparison(reference: _Reference, page_png: bytes, page_label: str, *, mode: str = "view",
                       region: dict[str, int] | None = None, layout_width: int = 0, ref_crop: dict[str, float] | None = None,
                       ref_scale: float = 0.0, auto_region: bool = True, target_kind: str = "element",
                       text_target: bool = False, snap: bool = False,
                       name_areas: Callable[[dict[str, Any]], list[str]] | None = None) -> tuple[bytes, dict[str, Any]]:
    options = _compare_options(reference, page_png, page_label, mode=mode, region=region, layout_width=layout_width,
                               ref_crop=ref_crop, ref_scale=ref_scale, auto_region=auto_region, target_kind=target_kind,
                               text_target=text_target, snap=snap)
    with _compare_page() as tools:
        try:
            stats = _in_background(tools, "evaluate", "o => window.renderCompare(o)", options)
        except Exception as exc:
            raise _compare_error(exc) from exc
        if name_areas is not None and stats.get("differences"):
            try:
                name_areas(stats)
            except Exception:
                pass
        return _board_shot(tools, stats.get("board")), stats


def _encode_reference(reference: _Reference, crop: dict[str, float] | None, *, max_width: int, max_height: int,
                      upscale: bool = False, png: bool = False) -> tuple[bytes, float]:
    with _compare_page() as tools:
        try:
            result = _in_background(tools, "evaluate", "o => window.cropImage(o)", {
                "src": _data_url(reference.data, reference.mime), "crop": crop, "maxWidth": max_width, "maxHeight": max_height,
                "upscale": upscale, "format": "image/png" if png else "image/jpeg",
            })
        except Exception as exc:
            raise _compare_error(exc) from exc
    return base64.b64decode(str(result.get("data") or "").split(",", 1)[-1]), float(result.get("factor") or 1.0)


_COMMON_DESIGN_WIDTHS = frozenset({1920, 1728, 1680, 1600, 1536, 1512, 1440, 1366, 1280, 1200, 1024, 1000, 834, 820, 800,
                                   768, 744, 430, 428, 414, 412, 393, 390, 375, 360, 320})


def _whole_width_scale(image_width: int, layout_width: int) -> float:
    if not image_width or not layout_width:
        return 0.0
    ratio = image_width / layout_width
    k = round(ratio)
    return float(k) if 1 <= k <= 4 and abs(ratio - k) <= 0.02 * k else 0.0


def _design_width_scale(image_width: int) -> float:
    for k in (1, 2, 3):
        if image_width and image_width % k == 0 and image_width // k in _COMMON_DESIGN_WIDTHS:
            return float(k)
    return 0.0


def _parse_region(value: Any, what: str) -> dict[str, float]:
    parts: Any = value
    if isinstance(value, str):
        parts = [p for p in re.split(r"[\s,;x×]+", value.strip()) if p]
    try:
        if isinstance(parts, dict):
            region = {key: float(parts[key]) for key in ("x", "y", "width", "height")}
        elif isinstance(parts, (list, tuple)) and len(parts) == 4:
            region = dict(zip(("x", "y", "width", "height"), (float(v) for v in parts)))
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise BrowserError(f"{what} is {{x, y, width, height}} in the design's CSS pixels.")
    if region["width"] < 1 or region["height"] < 1:
        raise BrowserError(f"{what} needs a width and height of at least 1 pixel.")
    return region


def _reference_geometry(reference: _Reference, args: dict[str, Any], layout_width: int, mode: str) -> tuple[dict[str, float] | None, float, bool]:
    image_w, image_h = _image_size(reference.data)
    try:
        scale = float(args.get("reference_scale") or 0)
    except (TypeError, ValueError):
        scale = 0.0
    if not 0 < scale <= 8:
        scale = float(reference.scale or 0)
    crop = dict(reference.crop) if reference.crop else None
    region = args.get("reference_region")
    if region not in (None, "", {}, []):
        box = _parse_region(region, "reference_region")
        k = scale or _whole_width_scale(image_w, layout_width) or _design_width_scale(image_w) or 1.0
        base_x, base_y = (crop["x"], crop["y"]) if crop else (0.0, 0.0)
        crop = {"x": base_x + box["x"] * k, "y": base_y + box["y"] * k, "width": box["width"] * k, "height": box["height"] * k}
        scale = k
    if crop and image_w and image_h:
        x0, y0 = max(0.0, crop["x"]), max(0.0, crop["y"])
        x1, y1 = min(float(image_w), crop["x"] + crop["width"]), min(float(image_h), crop["y"] + crop["height"])
        if x1 - x0 < 1 or y1 - y0 < 1:
            k = scale or 1.0
            raise BrowserError(f"reference_region is outside the design ({round(image_w / k)}×{round(image_h / k)} CSS px"
                               + (f" at {k:g}x" if k != 1 else "") + ").")
        crop = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    if not scale and mode != "element" and not crop:
        scale = _whole_width_scale(image_w, layout_width) or _design_width_scale(image_w)
    return crop, scale, crop is not None


def _name_areas(page: Any, reference: _Reference, rect: dict[str, int], stats: dict[str, Any]) -> list[str]:
    areas = stats.get("differences") or []
    points = [[rect["x"] + a["x"] + a["width"] / 2, rect["y"] + a["y"] + a["height"] / 2] for a in areas]
    try:
        found = page.evaluate(_ELEMENTS_AT_JS, points) or []
    except Exception:
        found = []
    region = stats.get("reference_region") or {}
    drawn_w = float((stats.get("reference_size") or [0])[0] or 0)
    factor = float(region.get("width") or 0) / drawn_w if drawn_w else 1.0
    labels = []
    for index, area in enumerate(areas):
        rel_x, rel_y = area["x"], area["y"]
        area["x"] = rect["x"] + rel_x
        area["y"] = rect["y"] + rel_y
        bits = []
        hit = found[index] if index < len(found) else None
        if hit:
            area["element"] = hit.get("element")
            area["selector"] = hit.get("selector")
            bits.append(str(hit.get("element") or ""))
        design_x, design_y = region.get("x", 0) + rel_x * factor, region.get("y", 0) + rel_y * factor
        if abs(design_x - area["x"]) > 1 or abs(design_y - area["y"]) > 1 or abs(factor - 1) > 0.01:
            area["design_box"] = {"x": round(design_x, 1), "y": round(design_y, 1),
                                  "width": round(area["width"] * factor, 1), "height": round(area["height"] * factor, 1)}
        if reference.figma:
            layer = _figma_layer_at(reference.figma, design_x + area["width"] * factor / 2, design_y + area["height"] * factor / 2)
            if layer:
                area["design_layer"] = layer
                bits.append("design: " + layer)
        area.pop("n", None)
        labels.append(" · ".join(bit for bit in bits if bit))
    return labels


def _do_compare(session: _Session, page: Any, args: dict[str, Any], reference: _Reference | None) -> dict[str, Any]:
    if reference is None:
        raise BrowserError("No reference image to compare with.")
    live = _resolve_live_reference(session, reference) if reference.live else {}
    base = dict(reference.crop) if reference.crop else None
    reference = _cut_out(reference)
    capture = {"type": "jpeg", "quality": 75} if reference.mime == "image/jpeg" else {"type": "png"}
    if _has_element_target(args) or args.get("width") is not None:
        mode = "element"
        text_layer = bool(reference.layer and reference.layer.get("type") == "TEXT")
        rect, target, geo = _target_rect(page, args, default_padding=0, text_box=text_layer)
        page_png = _capture(page, rect, geo, **capture)
    else:
        geo = _page_geometry(page)
        if args.get("full_page"):
            mode, target = "page", "full page"
            page_png = page.screenshot(full_page=True, timeout=30_000, scale="css", **capture)
            width, height = _image_size(page_png)
            rect = {"x": 0, "y": 0, "width": width, "height": height}
        else:
            mode, target = "view", "viewport"
            page_png = page.screenshot(timeout=20_000, scale="css", **capture)
            rect = {"x": int(round(geo["scrollX"])), "y": int(round(geo["scrollY"])), "width": int(geo["vw"]), "height": int(geo["vh"])}
    layout_width = int(round(geo.get("layoutWidth") or geo.get("vw") or VIEWPORT["width"]))
    crop, scale, chosen = _reference_geometry(reference, args, layout_width, mode)
    box = geo.get("box") if mode == "element" else None
    if box and crop and scale and chosen:
        left, top = box["x"] - rect["x"], box["y"] - rect["y"]
        right = rect["x"] + rect["width"] - box["x"] - box["width"]
        bottom = rect["y"] + rect["height"] - box["y"] - box["height"]
        image_w, image_h = _image_size(reference.data)
        x0, y0 = crop["x"] - left * scale, crop["y"] - top * scale
        x1, y1 = crop["x"] + crop["width"] + right * scale, crop["y"] + crop["height"] + bottom * scale
        if image_w and image_h:
            x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(float(image_w), x1), min(float(image_h), y1)
        if x1 - x0 >= 1 and y1 - y0 >= 1:
            crop = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    page_label = _short_url(page.url) or "page"
    if target != "viewport":
        page_label += " · " + (target if len(target) <= 40 else target[:38].rstrip() + "…")
    geometry = {
        "mode": mode, "region": rect, "layout_width": layout_width, "ref_crop": crop, "ref_scale": scale,
        "auto_region": not chosen, "target_kind": "region" if not _has_element_target(args) else "element",
        "text_target": mode == "element" and (bool(geo.get("textOnly")) or bool(reference.layer and reference.layer.get("type") == "TEXT")),
        "snap": (chosen or reference.cut) and (mode == "page" or (mode == "view" and rect["y"] == 0)),
    }
    by_element = args.get("elements") not in (None, False, "", 0, "0", "false", "no")
    if by_element:
        found = _page_elements(page, args, rect)
        items = found.get("elements") or []
        if not items:
            raise BrowserError(f"Nothing to compare element by element in the {target}: no visible text, images, controls or boxes.")
        data, stats = _render_elements(reference, page_png, page_label, _relative_to(items, rect), **geometry)
    else:
        data, stats = _render_comparison(reference, page_png, page_label, name_areas=lambda st: _name_areas(page, reference, rect, st),
                                         **geometry)
    out: dict[str, Any] = {
        "url": page.url,
        "title": _title(page),
        **_save_shot(session, data),
        "reference": reference.label,
        "target": target,
        "mode": mode,
        "region": rect,
        **live,
        "accuracy": design_accuracy(),
    }
    if by_element:
        out.update(_elements_result(reference, target, rect, stats, total=int(found.get("total") or len(items))))
    else:
        for key in ("similarity", "structure", "diff_pct", "verdict", "page_size", "reference_size", "reference_scale", "offset",
                    "size_diff", "shifts", "differences", "threshold"):
            if stats.get(key) not in (None, "", [], 0) or key in ("similarity", "diff_pct", "verdict", "threshold"):
                out[key] = stats.get(key)
    if chosen or stats.get("same_region"):
        out["reference_region"] = stats.get("reference_region")
    if chosen and mode != "element" and crop:
        area = dict(stats.get("snapped_crop") or crop)
        if base:
            area["x"] += base["x"]
            area["y"] += base["y"]
        out["_design_crop"] = area
        out["_design_scale"] = scale
    notes = [note for note in (stats.get("notes") or []) if note and note != stats.get("size_diff")]
    if by_element:
        notes = [note for note in notes if not note.startswith(("Area ", "Areas ", "No one area"))]
    if reference.note:
        notes.insert(0, reference.note)
    if notes:
        out["notes"] = " ".join(notes)
    if out.get("differences"):
        out["coords"] = "Areas are page coordinates (CSS px from the page's top-left); design_box is where it is in the design."
    if out.get("elements") or out.get("missing_on_page"):
        out["coords"] = "Boxes are page coordinates (CSS px from the page's top-left); design_box is where the element is in the design."
    return out


ELEMENTS_LISTED = 30
_BAD_STATUSES = frozenset({"different", "missing"})


def _page_elements(page: Any, args: dict[str, Any], rect: dict[str, int]) -> dict[str, Any]:
    options = {"rect": rect, "max": ELEMENTS_MAX}
    if _has_element_target(args):
        return _locator(page, args).evaluate(_ELEMENTS_JS, options) or {}
    return page.evaluate(f"(o) => ({_ELEMENTS_JS})(null, o)", options) or {}


def _shifted(box: dict[str, Any], dx: float, dy: float, digits: int = 1) -> dict[str, Any]:
    out = {**box, "x": round(float(box["x"]) + dx, digits), "y": round(float(box["y"]) + dy, digits)}
    for key in ("width", "height"):
        if key in box:
            out[key] = round(float(box[key]), digits)
    return out


def _relative_to(items: list[dict[str, Any]], rect: dict[str, int]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        moved = dict(item)
        moved["box"] = _shifted(item["box"], -rect["x"], -rect["y"], 2)
        if item.get("text_box"):
            moved["text_box"] = _shifted(item["text_box"], -rect["x"], -rect["y"], 2)
        out.append(moved)
    return out


def _elements_result(reference: _Reference, target: str, rect: dict[str, int], stats: dict[str, Any], *, total: int) -> dict[str, Any]:
    counts = stats.get("counts") or {}
    region = stats.get("reference_region") or {}
    drawn_w = float((stats.get("reference_size") or [0])[0] or 0)
    factor = float(region.get("width") or 0) / drawn_w if drawn_w else 1.0
    listed = []
    for item in (stats.get("elements") or [])[:ELEMENTS_LISTED]:
        entry = dict(item)
        entry["box"] = _shifted(item["box"], rect["x"], rect["y"])
        design = item.get("design_box")
        if design:
            entry["design_box"] = _shifted(design, rect["x"], rect["y"])
            if reference.figma:
                layer = _figma_layer_at(reference.figma, region.get("x", 0) + (design["x"] + design["width"] / 2) * factor,
                                        region.get("y", 0) + (design["y"] + design["height"] / 2) * factor)
                if layer:
                    entry["design_layer"] = layer
        listed.append(entry)
    lacking = [_shifted(area, rect["x"], rect["y"]) for area in stats.get("lacking") or []]
    compared = int(counts.get("compared") or 0)
    parts = [f"{counts.get('matches', 0)} match"]
    if counts.get("nearly"):
        parts.append(f"{counts['nearly']} nearly match")
    if counts.get("different"):
        parts.append(f"{counts['different']} differ")
    if counts.get("missing"):
        parts.append(f"{counts['missing']} {'is' if counts['missing'] == 1 else 'are'} not in the design")
    beyond = int(stats.get("beyond") or 0)
    summary = f"Compared {compared} element{'s' if compared != 1 else ''} with the design"
    if total > compared + beyond:
        summary += f" (the {compared + beyond} that show the most, of {total})"
    summary += ": " + ", ".join(parts) + "."
    if beyond:
        summary += (f" {beyond} element{'s' if beyond != 1 else ''} past the design's edge {'were' if beyond != 1 else 'was'} "
                    "not compared: the design shows less of the page.")
    if lacking:
        summary += f" {len(lacking)} part{'s' if len(lacking) != 1 else ''} of the design {'are' if len(lacking) != 1 else 'is'} not on the page."
    background = stats.get("background")
    if background:
        summary += " " + background["text"][0].upper() + background["text"][1:] + "."
    if len(stats.get("elements") or []) > len(listed):
        summary += f" The first {len(listed)} are listed."
    out: dict[str, Any] = {
        "compare": "elements", "verdict": stats.get("verdict"), "similarity": stats.get("similarity"), "structure": stats.get("structure"),
        "elements_compared": compared, "summary": summary, "elements": listed,
    }
    for key in ("page_size", "reference_size", "reference_scale", "shifts"):
        if stats.get(key) not in (None, "", [], 0):
            out[key] = stats[key]
    if lacking:
        out["missing_on_page"] = lacking
    if background:
        out["background"] = {"page": background["page"], "design": background["design"], "delta_e": background["delta_e"]}
    agent = _agent()
    key = f"{reference.label}|{target}"
    now = {str(selector): str(status) for selector, status in stats.get("statuses") or []}
    before = agent.design_rounds.get(key)
    if before is not None:
        fixed = sum(1 for sel, status in before.items() if status in _BAD_STATUSES
                    and (now.get(sel) not in (None, *_BAD_STATUSES) or (status == "missing" and sel not in now)))
        still = sum(1 for sel, status in now.items() if status in _BAD_STATUSES and before.get(sel) in _BAD_STATUSES)
        worse = sum(1 for sel, status in now.items() if status in _BAD_STATUSES and before.get(sel) not in (None, *_BAD_STATUSES))
        bits = [f"{fixed} fixed"] + ([f"{still} still differ"] if still else []) + ([f"{worse} newly differ"] if worse else [])
        out["progress"] = "Since the last element compare: " + ", ".join(bits) + "."
    agent.design_rounds[key] = now
    while len(agent.design_rounds) > 20:
        agent.design_rounds.pop(next(iter(agent.design_rounds)))
    return out


def _same_link(a: str, b: str) -> bool:
    a, b = str(a or "").strip().rstrip("/"), str(b or "").strip().rstrip("/")
    if not a or not b:
        return False
    if a == b:
        return True
    from livecode import figma as _figma

    if _figma.is_figma_url(a) and _figma.is_figma_url(b):
        try:
            return _figma.parse_url(a) == _figma.parse_url(b)
        except _figma.FigmaError:
            return False
    return False


def _tab_showing(session: _Session, url: str) -> str:
    for tab_id, page in session.tabs.items():
        if not page.is_closed() and (_same_link(page.url, url) or _same_link(session.links.get(tab_id, ""), url)):
            return tab_id
    return ""


def _resolve_live_reference(session: _Session, reference: _Reference) -> dict[str, Any]:
    info: dict[str, Any] = {}
    if reference.url and not reference.tab:
        reference.tab = _tab_showing(session, reference.url)
    if reference.url and not reference.tab:
        tab_id = _open_tab(session, _agent())
        page = session.tabs[tab_id]
        session.links[tab_id] = reference.url
        try:
            _do_navigate(session, page, reference.url)
            _wait_load(page, "networkidle", 8000)
            _pause(page, 600)
        finally:
            session.touch()
        reference.tab = tab_id
        info["opened_tab"] = tab_id
    page = session.tabs.get(reference.tab)
    if page is None or page.is_closed():
        raise BrowserError(f"No tab {reference.tab}. Open tabs: {', '.join(session.tabs) or 'none'}.")
    if _worker.remote and reference.tab != session.active:
        _bring_to_front(page)
    try:
        reference.data = page.screenshot(full_page=True, type="png", timeout=30_000, scale="css")
    finally:
        if _worker.remote and reference.tab != session.active and session.tabs.get(session.active) is not None:
            _bring_to_front(session.tabs[session.active])
    reference.mime = "image/png"
    reference.scale = 1.0
    reference.label = f"tab {reference.tab} · {_short_url(page.url) or _title(page) or 'page'}"
    info["reference_tab"] = reference.tab
    return info


def _cut_out(reference: _Reference) -> _Reference:
    if not reference.crop or not reference.area:
        return reference
    data, _factor = _encode_reference(reference, reference.crop, max_width=20_000, max_height=20_000, png=True)
    cut = _Reference(data, "image/png", reference.label, scale=reference.scale, tab=reference.tab, url=reference.url)
    cut.cut = True
    cut.note = reference.note
    return cut


def _remember_shot(session: _Session, shot_id: str, **meta: Any) -> None:
    with _refs_lock:
        index = _load_refs_index(session)
        shots = index.setdefault("shots", {})
        shots[shot_id] = {key: value for key, value in meta.items() if value}
        if len(shots) > MAX_SHOTS_KEPT * 2:
            for old in list(shots)[: len(shots) - MAX_SHOTS_KEPT * 2]:
                shots.pop(old, None)
        _save_refs_index(session, index)


def _do_image_crop(session: _Session, reference: _Reference, args: dict[str, Any]) -> dict[str, Any]:
    live = _resolve_live_reference(session, reference) if reference.live else {}
    reference = _cut_out(reference)
    crop, scale, _chosen = _reference_geometry(reference, args, session.viewport["width"], "element")
    data, factor = _encode_reference(reference, crop, max_width=6000, max_height=6000)
    saved = _save_shot(session, data)
    k = (scale or 1.0) * factor
    if abs(k - round(k)) < 0.01:
        k = float(round(k))
    _remember_shot(session, saved["shot_id"], scale=round(k, 4) if abs(k - 1) > 0.01 else 0, cut=bool(crop))
    image_w, image_h = _image_size(reference.data)
    out: dict[str, Any] = {
        **saved,
        "reference": reference.label,
        "source": "image",
        "target": ("part of " if crop else "") + reference.label,
        "reference_image_size": [image_w, image_h],
        **live,
    }
    if crop:
        out["reference_region"] = {key: round(crop[key] / (scale or 1.0), 1) for key in ("x", "y", "width", "height")}
    if abs(k - 1) > 0.01:
        out["reference_scale"] = round(k, 3)
    return out


def _idle_state(session: _Session) -> dict[str, Any]:
    return {"available": True, "running": False, "tabs": [], "active": "", "url": "", "title": "",
            "seq": session.seq, "viewport": dict(session.viewport), "viewport_mode": session.viewport_mode,
            "engine": "chrome" if cdp_endpoint() else "builtin"}


def _busy_state(session: _Session) -> dict[str, Any]:
    return {**(session.last_state or _idle_state(session)), "busy": session.worker.busy}


def status(state_path: str) -> dict[str, Any]:
    if not playwright_installed():
        return {"available": False, "running": False, "tabs": [], "error": f"The built-in browser needs Playwright on the LiveCode server: {INSTALL_HINT}"}
    session = _session(state_path)
    if session.context is None:
        return _idle_state(session)
    if session.worker.busy:
        return _busy_state(session)
    return session.worker.call(_state, session)


_NAVIGATING_ACTIONS = frozenset({"navigate", "back", "forward", "reload", "new_tab", "switch_tab"})
AUTO_SNAPSHOT_MAX_CHARS = 8000
VIEW_SNAPSHOT_MAX_CHARS = 5000


def _fingerprint(page: Any) -> str:
    try:
        return str(page.evaluate(_PAGE_FINGERPRINT_JS) or "")
    except Exception:
        return ""


def perform(state_path: str, action: str, args: dict[str, Any] | None = None, *, snapshot_after: bool = False,
            reference: _Reference | None = None, resolve_path: Callable[[str], str | None] | None = None,
            agent: _Agent | None = None, live: bool = False) -> dict[str, Any]:
    session = _session(state_path)
    args = dict(args or {})
    action = str(action or "").strip().lower()
    who = agent or session.user
    live = live and action in _LIVE_ACTIONS and who is session.user
    if live:
        args["live"] = True

    def _run() -> dict[str, Any]:
        _ACTIVE["session"] = session
        _ACTIVE["agent"] = who
        _ACTIVE["resolve_path"] = resolve_path
        _ACTIVE["page"] = None
        result = _do_action(session, action, args, reference)
        if live:
            return result if action == "scroll" else {**_state(session), **result}
        _yield_to_input()
        acted = _ACTIVE.get("page")
        if acted is not None and acted.is_closed():
            acted = None
        acted_tab = _tab_of(session, acted) if acted is not None else ""
        changed = False
        if (action in _NAVIGATING_ACTIONS or action == "snapshot") and acted is not None:
            who.fp = acted_tab + "|" + _fingerprint(acted)
            who.no_progress = 0
        if action in FINGERPRINT_ACTIONS and acted is not None:
            fingerprint = acted_tab + "|" + _fingerprint(acted)
            same_tab = who.fp.split("|", 1)[0] == acted_tab
            changed = bool(same_tab and who.fp and fingerprint != who.fp)
            who.no_progress = who.no_progress + 1 if same_tab and fingerprint == who.fp else 0
            who.fp = fingerprint
            result["no_progress"] = who.no_progress
            result["changed"] = changed
        navigating = action in _NAVIGATING_ACTIONS or bool(result.get("navigated"))
        view_only = not navigating
        wants_view = action == "scroll" or (changed and action in READBACK_ACTIONS)
        if snapshot_after and "snapshot" not in result and (navigating or wants_view) and acted is not None:
            try:
                text, count = _take_snapshot(session, acted, view_only=view_only, max_chars=VIEW_SNAPSHOT_MAX_CHARS if view_only else AUTO_SNAPSHOT_MAX_CHARS)
                result["snapshot"] = text
                result["elements"] = count
            except Exception:
                pass
        state = _state(session)
        merged = {**state, **result}
        if acted is not None and "url" not in result:
            merged["url"] = acted.url
            merged["title"] = _title(acted)
        if session.last_dialog:
            merged["dialog"] = session.last_dialog
        if who is not session.user:
            merged["_tabs"] = _tab_report(session, who, acted_tab, force=action in ("tabs", "new_tab", "switch_tab", "close_tab"))
        return merged

    if live:
        return session.worker.call(_run, label="", priority=True)
    return session.worker.call(_run, label=action or "browser")


def _agent_for(session: _Session, session_id: str, spec: Any) -> _Agent:
    agent_id = str(spec.get("id") or "").strip() if isinstance(spec, dict) else ""
    if agent_id and not agent_tabs_enabled():
        agent_id = ""  # tabs of their own are off: every subagent works in the main agent's tab
    key = f"sub:{session_id}:{agent_id}" if agent_id else f"main:{session_id}"
    label = str(spec.get("label") or "").strip()[:60] if agent_id else ""
    with _sessions_lock:
        agent = session.agents.get(key)
        if agent is None:
            if len(session.agents) >= MAX_AGENTS_KEPT:
                owners = {str(info.get("owner") or "") for info in list(session.tab_info.values())}
                for old_key, old in list(session.agents.items()):
                    if old.done and old_key not in owners:
                        del session.agents[old_key]
            agent = session.agents[key] = _Agent(key, label, sub=bool(agent_id))
            agent.console_seen = session.console_n
            agent.downloads_seen = len(session.downloads)
        elif label:
            agent.label = label
    return agent


def _tab_order(tab_id: str) -> int:
    return int(tab_id[1:]) if tab_id[1:].isdigit() else 0


def _tab_brief(session: _Session, tab_id: str, agent: _Agent, current: str) -> dict[str, Any]:
    page = session.tabs[tab_id]
    entry: dict[str, Any] = {"id": tab_id, "title": _title(page)[:80], "url": str(page.url or "")[:200]}
    if tab_id == current:
        entry["yours"] = True
    if tab_id == session.active:
        entry["shown"] = True
    owner = session.agents.get(str((session.tab_info.get(tab_id) or {}).get("owner") or ""))
    if owner is not None and owner is not agent and owner.sub:
        entry["by"] = owner.label or "a subagent"
    return entry


def _tab_report(session: _Session, agent: _Agent, acted_tab: str, *, force: bool = False) -> dict[str, Any]:
    current = _current_tab_id(session, agent)
    out: dict[str, Any] = {"tab_id": acted_tab or current}
    open_ids = [tab_id for tab_id, page in session.tabs.items() if not page.is_closed()]
    now = frozenset(open_ids)
    if not force and agent.seen == now and not agent.notes:
        return out
    notes = list(agent.notes)
    if agent.seen is not None and not notes:
        opened = sorted(now - agent.seen, key=_tab_order)
        closed = sorted(agent.seen - now, key=_tab_order)
        parts = ([f"opened {', '.join(opened)}"] if opened else []) + ([f"closed {', '.join(closed)}"] if closed else [])
        if parts:
            notes.append("Tabs changed since your last action: " + "; ".join(parts) + ".")
    agent.notes.clear()
    agent.seen = now
    if notes:
        out["tab_note"] = " ".join(notes)
    out["tabs"] = [_tab_brief(session, tab_id, agent, current) for tab_id in open_ids]
    return out


TURN_TABS_LISTED = 8


def turn_context(state_path: str, session_id: str) -> str:
    session = _sessions.get(str(state_path or "").strip())
    if session is None or session.context is None:
        return ""
    agent = _agent_for(session, session_id, None)
    agent.tab = agent.ref_tab = ""
    agent.seen = None
    agent.notes.clear()
    try:
        state = session.last_state if session.worker.busy else session.worker.call(_state, session, timeout=10)
    except Exception:
        state = session.last_state
    tabs = [tab for tab in (state.get("tabs") or []) if tab.get("url") and tab.get("url") != "about:blank"]
    if not tabs:
        return ""

    def _line(tab: dict[str, Any]) -> str:
        title = str(tab.get("title") or "").strip()[:60]
        text = f'{tab["id"]} ' + (f'"{title}" ' if title else "") + _short_url(str(tab.get("url") or ""))
        return text + (f" (opened by subagent {tab['agent']})" if tab.get("agent") else "")

    shown = next((tab for tab in tabs if tab.get("id") == state.get("active")), None)
    others = [tab for tab in tabs if tab is not shown][:TURN_TABS_LISTED]
    parts = []
    if shown is not None:
        parts.append(f"The user's Browser tab shows {_line(shown)}; browser actions without tab_id go there.")
    if others:
        parts.append("Also open: " + "; ".join(_line(tab) for tab in others) + ".")
    return "Browser: " + " ".join(parts)


def release_agent(state_path: str, session_id: str, agent_id: str) -> None:
    session = _sessions.get(str(state_path or "").strip())
    agent = session.agents.get(f"sub:{session_id}:{agent_id}") if session is not None and agent_id else None
    if agent is not None:
        agent.done = True
        session.touch()


def frame(state_path: str, since: int = -1, image: bool = True) -> tuple[int, bytes | None, dict[str, Any]]:
    session = _session(state_path)
    if session.context is None:
        return session.seq, None, _idle_state(session)
    if session.worker.busy:
        return session.seq, None, _busy_state(session)

    def _run() -> tuple[int, bytes | None, dict[str, Any]]:
        if not image:
            return session.seq, None, _state(session)
        seq, data = _do_frame(session, since)
        return seq, data, _state(session)

    return session.worker.call(_run, timeout=20)


def set_view_box(state_path: str, width: Any, height: Any) -> None:
    try:
        box = (int(float(width)), int(float(height)))
    except (TypeError, ValueError):
        return
    if box[0] > 0 and box[1] > 0:
        _session(state_path).view_box = box


def stream_open(state_path: str) -> "browser_stream.Stream | None":
    session = _session(state_path)
    if session.context is None or not session.tabs:
        return None

    def _run() -> "browser_stream.Stream":
        stream = session.stream
        if stream is None or stream.closed:
            stream = browser_stream.Stream(session)
            session.stream = stream
        stream.open_viewer()
        session.worker.streams.add(stream)
        stream.service()
        return stream

    return session.worker.call(_run, timeout=20)


def close(state_path: str) -> None:
    session = _sessions.get(str(state_path or "").strip())
    if session is None or session.context is None:
        return

    def _run() -> None:
        if session.stream is not None:
            session.stream.stop()
            session.stream = None
        if session.shared:
            for page in list(session.tabs.values()):
                try:
                    page.close()
                except Exception:
                    pass
            if session.page_handler is not None:
                try:
                    session.context.remove_listener("page", session.page_handler)
                except Exception:
                    pass
        else:
            _save_storage(session, force=True)
            try:
                session.context.close()
            except Exception:
                pass
        session.context = None
        session.page_handler = None
        session.shared = False
        session.tabs.clear()
        session.active = ""
        session.touch()

    session.worker.call(_run, label="close")


def shot_path(storage_key: str, shot_id: str) -> str | None:
    from livecode.project_store import PROJECTS_ROOT

    if not _STORAGE_KEY.match(storage_key or "") or not _SHOT_ID.match(shot_id or "") or storage_key in (".", ".."):
        return None
    path = os.path.join(PROJECTS_ROOT, storage_key, "browser", "shots", shot_id + ".jpg")
    return path if os.path.isfile(path) else None


def download_path(storage_key: str, name: str) -> str | None:
    from livecode.project_store import PROJECTS_ROOT

    base = os.path.basename(name or "")
    if not _STORAGE_KEY.match(storage_key or "") or storage_key in (".", "..") or not base or base != name or base.startswith("."):
        return None
    path = os.path.join(PROJECTS_ROOT, storage_key, "browser", "downloads", base)
    return path if os.path.isfile(path) else None


def panel_info(state_path: str, kind: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    session = _session(state_path)
    args = args or {}
    kind = str(kind or "").lower()
    limit = int(min(max(int(args.get("limit") or 100), 1), 400))
    if kind == "console":
        entries = list(session.console_log)[-limit:]
        return {"kind": kind, "entries": entries, "errors": sum(1 for e in session.console_log if e["level"] in ("error", "pageerror"))}
    if kind == "network":
        entries = list(session.network_log)
        if str(args.get("status") or "") == "failed":
            entries = [e for e in entries if e["status"] == 0 or e["status"] >= 400]
        needle = str(args.get("filter") or "").lower()
        if needle:
            entries = [e for e in entries if needle in e["url"].lower()]
        return {"kind": kind, "entries": entries[-limit:], "total": len(session.network_log)}
    if kind == "downloads":
        key = storage_key_for(state_path)
        items = [{k: v for k, v in item.items() if k != "path"} for item in session.downloads[-50:]]
        for item in items:
            if not item.get("error"):
                item["url_local"] = f"/livecode/browser/download/{key}/{item['name']}"
        return {"kind": kind, "entries": list(reversed(items))}
    if kind == "history":
        return {"kind": kind, "entries": list(reversed(list(session.history)))[:limit]}
    if kind == "clear":
        target = str(args.get("what") or "")
        if target == "console":
            session.console_log.clear()
        elif target == "network":
            session.network_log.clear()
        elif target == "history":
            session.history.clear()
        return {"kind": kind, "cleared": target}
    raise BrowserError("Unknown info kind.")


def shot_url(storage_key: str, shot_id: str) -> str:
    return f"/livecode/browser/shot/{storage_key}/{shot_id}.jpg"


def shot_data_url(storage_key: str, shot_id: str) -> str:
    path = shot_path(storage_key, shot_id)
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def action_needs_approval(args: dict[str, Any] | None) -> bool:
    action = str((args or {}).get("action") or "").strip().lower()
    if action == "batch":
        items = (args or {}).get("actions")
        return any(isinstance(item, dict) and str(item.get("action") or "").strip().lower() in INTERACTIVE_ACTIONS for item in (items or []))
    return action in INTERACTIVE_ACTIONS


def action_label(args: dict[str, Any] | None) -> str:
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    target = ""
    if args.get("ref") not in (None, ""):
        target = f"[{args.get('ref')}]"
    elif args.get("selector"):
        target = f"`{str(args.get('selector'))[:60]}`"
    elif args.get("text") and action in ("click", "wait", "crop", "compare", "inspect", "scroll"):
        target = f"“{str(args.get('text'))[:60]}”"
    url = str(args.get("url") or "").strip()
    if action == "navigate":
        return f"Navigating to {_short_url(url) or url[:80]}" if url else "Navigating"
    if action == "new_tab":
        where = "a background tab" if args.get("background") else "a new tab"
        return f"Opening {_short_url(url) or url[:80]} in {where}" if url else f"Opening {where}"
    if action == "resize":
        device = _device_name(args.get("device"))
        if device in ("fit", "auto", "tab", "pane", "fit-to-tab"):
            return "Fitting the browser to its tab"
        if device:
            return f"Resizing the browser to {device}"
        return f"Resizing the browser to {args.get('width') or '…'}×{args.get('height') or '…'}"
    if action == "figma":
        node = str(args.get("node") or "").strip()
        return f"Reading Figma layer {node[:60]}" if node and not url else "Loading the Figma design"
    reference = str(args.get("image") or args.get("reference") or "").strip()
    to = str(args.get("to") or "").strip().lower()
    if action == "batch":
        names = [str(item.get("action") or "?") for item in (args.get("actions") or []) if isinstance(item, dict)]
        return f"Running {len(names)} browser actions ({', '.join(names[:6])}{'…' if len(names) > 6 else ''})"
    labels = {
        "hover": f"Hovering {target}" if target else "Hovering",
        "drag": f"Dragging {target}" if target else "Dragging",
        "upload": "Uploading a file",
        "select": f"Choosing an option in {target}" if target else "Choosing an option",
        "check": f"Toggling {target}" if target else "Toggling a checkbox",
        "dialog": "Answering a page dialog",
        "find": f"Finding “{str(args.get('text') or args.get('query') or '')[:40]}”",
        "zoom": "Zooming the page",
        "console": "Reading the console",
        "network": "Reading network requests",
        "downloads": "Listing downloads",
        "snapshot": "Reading page",
        "screenshot": ("Taking full-page screenshot" if args.get("full_page") else "Taking screenshot")
                      + (f" of tab {args.get('tab_id')}" if args.get("tab_id") else ""),
        "crop": (f"Cropping {reference[:60]}" if reference
                 else f"Cropping {target}" if target else "Cropping a region"),
        "compare": ("Comparing " + (target or ("the full page" if args.get("full_page") else "the page")) + " with "
                    + (reference[:60] if reference else "the design")),
        "inspect": f"Inspecting {target}" if target else "Inspecting the page's layout",
        "click": f"Clicking {target}" if target else "Clicking",
        "type": f"Typing into {target}" if target else "Typing",
        "press": f"Pressing {str(args.get('key') or args.get('text') or '')[:30]}".rstrip(),
        "scroll": (f"Scrolling to {target}" if target else f"Scrolling to the {to}" if to in ("top", "bottom")
                   else "Scrolling" if args.get("to_y") is not None else f"Scrolling {str(args.get('direction') or 'down')}"),
        "wait": f"Waiting for {target}" if target else "Waiting",
        "javascript_exec": "Running page script",
        "back": "Going back",
        "forward": "Going forward",
        "reload": "Reloading page",
        "tabs": "Listing tabs",
        "switch_tab": f"Switching to tab {args.get('tab_id') or ''}".rstrip(),
        "close_tab": "Closing tab",
    }
    return labels.get(action, "Using the browser")


_RESULT_KEYS = ("status", "warning", "clicked", "typed_into", "submitted", "pressed", "opened_tab", "closed_tab", "waited_s",
                "scroll_y", "scroll_max_y", "snapshot", "elements", "result", "truncated", "dialog", "hovered", "dragged",
                "uploaded", "selected", "checked", "zoom", "note", "changed")
_SHOT_RESULT_KEYS = ("region", "target", "source", "mode", "reference", "similarity", "structure", "diff_pct", "verdict", "page_size",
                     "reference_size", "reference_scale", "reference_region", "reference_image_size", "offset", "size_diff",
                     "shifts", "differences", "notes", "coords", "reference_tab", "opened_tab", "tab_id", "threshold", "marks",
                     "compare", "summary", "elements_compared", "elements", "missing_on_page", "background", "progress", "accuracy")


FAIL_STREAK_SHOT = 2
_STUCK_SHOT_ADVICE = (
    "Playwright actions and page scripts are not getting through, so a screenshot of the page is attached. Look at it: "
    "work out what is blocking you or where the thing you need is, then act on what you see: the pink numbered boxes "
    "are refs you can pass to click or type; otherwise click {x, y} with the screenshot's coordinates, or press a key "
    "(Escape closes most popups). If you still cannot get on, tell the "
    "user what the page shows and what you need from them."
)


def _stuck_help(state_path: str, result: dict[str, Any], agent: _Agent, *, tab_id: str = "", force: bool = False) -> dict[str, Any]:
    if result.get("unavailable"):
        return result
    agent.fail_streak += 1
    if not result.get("error"):
        agent.fail_streak = 0
    elif not force and agent.fail_streak < FAIL_STREAK_SHOT:
        return result
    try:
        shot = perform(state_path, "screenshot", {"marks": True, **({"tab_id": tab_id} if tab_id else {})}, agent=agent)
    except Exception:
        return result
    if not shot.get("shot_id"):
        return result
    for key in ("shot_id", "storage_key", "width", "height"):
        result[key] = shot.get(key)
    result["shot_url"] = shot_url(shot.get("storage_key") or "", shot.get("shot_id") or "")
    if shot.get("marks"):
        result["marks"] = shot["marks"]
    result["url"] = shot.get("url") or result.get("url") or ""
    result["title"] = shot.get("title") or result.get("title") or ""
    result["stuck"] = True
    if result.get("error"):
        result["error"] = f"{result['error']} {_STUCK_SHOT_ADVICE}"
    else:
        result["warning"] = (f"Nothing on the page changed after {agent.no_progress + 1} actions in a row. " + _STUCK_SHOT_ADVICE)
        agent.no_progress = 0
    agent.fail_streak = 0
    return result


def _run_batch(state_path: str, args: dict[str, Any], agent: _Agent, *, session_id: str,
               resolve_path: Callable[[str], str | None] | None) -> dict[str, Any]:
    items = args.get("actions")
    if not isinstance(items, list) or not items:
        return {"error": "batch needs actions: a list of browser action objects, each with its own action and arguments.", "error_kind": "invalid_input"}
    if len(items) > MAX_BATCH:
        return {"error": f"A batch runs at most {MAX_BATCH} actions; split it.", "error_kind": "invalid_input"}
    steps: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or str(item.get("action") or "").lower() in ("batch", ""):
            return {"error": f"Step {index} must be an action object (not another batch).", "error_kind": "invalid_input", "steps": steps}
        sub = dict(item)
        if args.get("tab_id") and not sub.get("tab_id"):
            sub["tab_id"] = args["tab_id"]
        last = _agent_action(state_path, sub, agent, session_id=session_id, resolve_path=resolve_path, final=index == len(items))
        failed = bool(last.get("error"))
        note = last.get("clicked") or last.get("typed_into") or last.get("hovered") or last.get("selected") or last.get("checked") or last.get("dragged") or ""
        if not note and last.get("result"):
            note = str(last["result"])[:120]
        steps.append({"step": index, "action": sub.get("action"), "ok": not failed, "url": last.get("url", ""),
                      **({"detail": str(note)[:140]} if note else {}), **({"error": str(last["error"])[:200]} if failed else {})})
        if failed:
            break
    out = dict(last)
    out["action"] = "batch"
    out["steps"] = steps
    if last.get("error"):
        out["error"] = f"Stopped at step {len(steps)} of {len(items)}: {last['error']}"
    return out


def agent_action(state_path: str, args: dict[str, Any], *, session_id: str = "",
                 resolve_path: Callable[[str], str | None] | None = None, agent: dict[str, Any] | None = None) -> dict[str, Any]:
    who = _agent_for(_session(state_path), session_id, agent)
    return _agent_action(state_path, args, who, session_id=session_id, resolve_path=resolve_path, final=True)


def _agent_action(state_path: str, args: dict[str, Any], who: _Agent, *, session_id: str,
                  resolve_path: Callable[[str], str | None] | None, final: bool) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action not in AGENT_ACTIONS:
        return {"error": f"Unknown action {action!r}. Use one of: {', '.join(AGENT_ACTIONS)}.", "error_kind": "invalid_input"}
    if action == "batch":
        return _run_batch(state_path, args, who, session_id=session_id, resolve_path=resolve_path)
    reference = None
    args = dict(args)
    spec = str(args.get("reference") or "").strip()
    explicit = str(args.get("tab_id") or "").strip()
    try:
        if action == "figma":
            return _figma_action(state_path, args, session_id, who)
        if action == "crop":
            image = str(args.get("image") or spec).strip()
            if image.lower().startswith("tab:"):
                args["tab_id"] = image[4:].strip()
                args.pop("image", None)
                args.pop("reference", None)
            elif image:
                if args.get("reference_region") in (None, "", {}) and all(args.get(k) is not None for k in ("x", "y", "width", "height")):
                    args["reference_region"] = {k: args[k] for k in ("x", "y", "width", "height")}
                reference = load_reference(state_path, image, session_id=session_id, resolve_path=resolve_path)
        elif action == "compare":
            reference = load_reference(state_path, spec, session_id=session_id, resolve_path=resolve_path)
        state = perform(state_path, action, args, snapshot_after=final, reference=reference, resolve_path=resolve_path, agent=who)
        design_crop, design_scale = state.pop("_design_crop", None), state.pop("_design_scale", 0.0)
        figma_spec = spec.lower().startswith("figma") or ("figma.com" in spec.lower() and not state.get("reference_tab"))
        if action == "compare" and spec.lower() not in ("", "latest", "design", "last") and not figma_spec:
            remembered = f"tab:{state['reference_tab']}" if state.get("reference_tab") else spec
            _remember_design(_session(state_path), session_id, remembered, str(state.get("reference") or remembered),
                             design_crop, float(design_scale or 0))
    except BrowserUnavailable as exc:
        return {"error": str(exc), "unavailable": True}
    except BrowserStuck as exc:
        return _stuck_help(state_path, {"error": str(exc), "action": action}, who, tab_id=explicit, force=True)
    except BrowserError as exc:
        return _stuck_help(state_path, {"error": str(exc), "action": action}, who, tab_id=explicit)
    except TimeoutError:
        return _stuck_help(state_path, {"error": "The browser did not answer in time.", "action": action}, who, tab_id=explicit)
    except Exception as exc:
        message = _first_line(exc).replace("Locator.", "").replace("Page.", "")
        full = str(exc)
        covered = re.search(r"<([a-z0-9]+)[^>]*>[^\n]*?intercepts pointer events", full)
        if covered:
            message += (f" Another element ({covered.group(0)[:160]}) covers it: usually a popup, banner or overlay. Close it first: "
                        "click its own button (the snapshot's Overview lists an open dialog and its controls) or press Escape.")
        elif "Timeout" in message and action in ("click", "type", "hover", "check", "drag", "upload", "select"):
            message += " The element may be hidden, covered or disabled: take a snapshot or a screenshot, or scroll to it."
        return _stuck_help(state_path, {"error": message, "action": action}, who, tab_id=explicit)
    who.fail_streak = 0
    tabs = state.pop("_tabs", None) or {}
    tab = str(tabs.get("tab_id") or "")
    out: dict[str, Any] = {"success": True, "action": action, "url": state.get("url", ""), "title": state.get("title", "")}
    if state.get("no_progress", 0) >= NO_PROGRESS_LIMIT - 1 and action in PROGRESS_ACTIONS:
        out["_no_progress"] = True
    session = _session(state_path)
    if action not in ("console", "network", "downloads"):
        fresh = [e for e in list(session.console_log) if e.get("n", 0) > who.console_seen and (not tab or e.get("tab") == tab)]
        who.console_seen = session.console_n
        errors = [e for e in fresh if e["level"] in ("error", "pageerror")]
        if errors:
            out["page_errors"] = f"{len(errors)} new console error{'s' if len(errors) != 1 else ''}; the first: {errors[0]['text'][:200]} (read them all with the console action)"
    dialog_text = str(state.get("dialog") or "")
    if dialog_text.endswith("(dismissed)") and dialog_text.split(":", 1)[0] in ("confirm", "prompt"):
        out["dialog_note"] = "That confirm or prompt was dismissed (cancelled). To accept it, call dialog {accept: true} first, then repeat the action."
    if action != "downloads":
        fresh_files = [d for d in session.downloads[who.downloads_seen:] if not tab or d.get("tab") in (None, tab)]
        who.downloads_seen = len(session.downloads)
        if fresh_files:
            out["downloaded"] = [d["name"] for d in fresh_files][:5]
    for key in _RESULT_KEYS:
        if key in state and state[key] not in (None, ""):
            out[key] = state[key]
    out.update(tabs)
    if action == "resize":
        for key in ("viewport", "viewport_mode", "device", "note", "page_height", "overflow_x"):
            if state.get(key) not in (None, ""):
                out[key] = state[key]
    if action == "inspect":
        for key in ("target", "element", "outline", "count", "page_size", "coords", "hint"):
            if state.get(key) not in (None, ""):
                out[key] = state[key]
    if action == "scroll":
        for key in ("target", "region", "moved", "scroll_pane"):
            if state.get(key) not in (None, ""):
                out[key] = state[key]
    if action in ("screenshot", "crop", "compare"):
        out.update({
            "shot_id": state.get("shot_id"),
            "storage_key": state.get("storage_key"),
            "shot_url": shot_url(state.get("storage_key") or "", state.get("shot_id") or ""),
            "width": state.get("width"),
            "height": state.get("height"),
        })
        if action == "screenshot":
            out["full_page"] = bool(state.get("full_page"))
            out["viewport"] = state.get("viewport")
        for key in _SHOT_RESULT_KEYS:
            if state.get(key) not in (None, "", []):
                out[key] = state[key]
    if action == "new_tab" and state.get("tab_id"):
        out["tab_id"] = state["tab_id"]
    if out.pop("_no_progress", False):
        return _stuck_help(state_path, out, who, tab_id=tab, force=True)
    return out


_SAME_SITE = {
    "no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict",
    "unspecified": "Lax", "": "Lax",
}


def _clean_domain(value: str) -> str:
    text = str(value or "").strip()
    if "://" in text:
        text = urlparse(text).hostname or ""
    return text.split("/")[0].split(":")[0] if text and not text.startswith("[") else text


def _expires(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1
    if number <= 0:
        return -1
    if number > 1e11:
        number /= 1000.0
    return number


def _cookie(name: Any, value: Any, domain: str, *, path: str = "/", expires: Any = None, http_only: bool = False,
            secure: bool = False, same_site: Any = None, host_only: bool = False) -> dict[str, Any] | None:
    name = str(name or "").strip()
    domain = _clean_domain(domain)
    if not name or not domain:
        return None
    if host_only:
        domain = domain.lstrip(".")
    site = _SAME_SITE.get(str(same_site or "").strip().lower(), "Lax")
    if site == "None" and not secure:
        site = "Lax"
    return {
        "name": name,
        "value": "" if value is None else str(value),
        "domain": domain,
        "path": str(path or "/") or "/",
        "expires": _expires(expires),
        "httpOnly": bool(http_only),
        "secure": bool(secure),
        "sameSite": site,
    }


def parse_cookies(text: str, *, domain: str = "") -> tuple[list[dict[str, Any]], int]:
    body = str(text or "").lstrip("\ufeff").strip()
    if not body:
        return [], 0
    if body[0] in "[{":
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BrowserError(f"That JSON does not parse: {exc.msg} (line {exc.lineno}).") from exc
        items = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise BrowserError("Expected a JSON array of cookies, or an object with a \"cookies\" array.")
        out, skipped = [], 0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            session_cookie = bool(item.get("session"))
            cookie = _cookie(
                item.get("name"), item.get("value"),
                str(item.get("domain") or item.get("host") or domain),
                path=str(item.get("path") or "/"),
                expires=None if session_cookie else (item.get("expires") if item.get("expires") is not None else item.get("expirationDate", item.get("expiry"))),
                http_only=bool(item.get("httpOnly", item.get("httponly", False))),
                secure=bool(item.get("secure", False)),
                same_site=item.get("sameSite", item.get("samesite")),
                host_only=bool(item.get("hostOnly", False)),
            )
            if cookie:
                out.append(cookie)
            else:
                skipped += 1
        return out, skipped
    lines = [line for line in body.splitlines() if line.strip()]
    tabbed = [line for line in lines if line.count("\t") >= 6]
    if tabbed:
        out, skipped = [], 0
        for line in lines:
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                skipped += 1
                continue
            host, include_subdomains, path, secure, expires, name, value = parts[:7]
            host = host.strip()
            if include_subdomains.strip().upper() == "TRUE" and not host.startswith("."):
                host = "." + host
            cookie = _cookie(
                name, value.rstrip("\r"), host, path=path, expires=expires, http_only=http_only,
                secure=secure.strip().upper() == "TRUE",
                host_only=include_subdomains.strip().upper() != "TRUE",
            )
            if cookie:
                out.append(cookie)
            else:
                skipped += 1
        return out, skipped
    header = re.sub(r"^\s*cookie\s*:\s*", "", " ".join(lines), flags=re.I)
    if "=" not in header:
        raise BrowserError("Paste cookies as JSON, a cookies.txt file, or name=value pairs.")
    if not _clean_domain(domain):
        raise BrowserError("Cookie header pairs need the site's domain, e.g. github.com.")
    out, skipped = [], 0
    for pair in header.split(";"):
        if "=" not in pair:
            if pair.strip():
                skipped += 1
            continue
        name, value = pair.split("=", 1)
        cookie = _cookie(name.strip(), value.strip(), "." + _clean_domain(domain).lstrip("."), secure=True)
        if cookie:
            out.append(cookie)
        else:
            skipped += 1
    return out, skipped


_ATTACHED_COOKIES = ("The browser is your own Chrome (attached over CDP), which keeps its own cookies: sign in there, "
                     "or disconnect it in Settings > Agent > Browser to use imported cookies.")


def import_cookies(state_path: str, cookies: list[dict[str, Any]], source: str = "") -> dict[str, Any]:
    session = _session(state_path)
    if cdp_endpoint():
        raise BrowserError(_ATTACHED_COOKIES)
    source = _cookie_source(source or cookie_browser())
    save_cookie_store(source, cookies)
    active = source == cookie_browser()

    def _run() -> dict[str, Any]:
        context = _context(session)
        if not active:
            return {"imported": len(cookies), "rejected": 0, "reloaded": 0, "stored_in": source, "active": False,
                    **_cookie_summary(context), **cookie_stores()}
        added, failed = _add_cookies(context, cookies)
        _write_cookie_marker(session, source, read_cookie_store(source))
        _save_storage(session, force=True)
        reloaded = _reload_open_pages(session)
        session.touch()
        return {"imported": added, "rejected": failed, "reloaded": reloaded, "stored_in": source, "active": True,
                **_cookie_summary(context), **cookie_stores()}

    return session.worker.call(_run, label="cookies")


def set_default_cookie_browser(state_path: str, value: Any) -> dict[str, Any]:
    name = save_cookie_browser(value)
    if cdp_endpoint():
        return {"attached": True, **cookie_stores()}
    session = _session(state_path)

    def _run() -> dict[str, Any]:
        context = _context(session)
        _apply_default_cookies(session, context)
        reloaded = _reload_open_pages(session)
        return {"reloaded": reloaded, "default_browser": name, **_cookie_summary(context), **cookie_stores()}

    return session.worker.call(_run, label="cookies")


COOKIE_SITE_LIMIT = 1000


def _cookie_summary(context: Any) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for cookie in context.cookies():
        site = str(cookie.get("domain") or "").lstrip(".")
        counts[site] = counts.get(site, 0) + 1
    sites = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {"total": sum(counts.values()), "sites": [{"domain": d, "count": n} for d, n in sites[:COOKIE_SITE_LIMIT]], "site_count": len(sites)}


def _stored_cookie_summary(state_file: str) -> dict[str, Any]:
    stored = _stored_cookies(state_file)
    counts: dict[str, int] = {}
    for cookie in stored:
        site = str(cookie.get("domain") or "").lstrip(".")
        counts[site] = counts.get(site, 0) + 1
    sites = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {"total": len(stored), "sites": [{"domain": d, "count": n} for d, n in sites[:COOKIE_SITE_LIMIT]], "site_count": len(sites)}


def _cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (str(cookie.get("name") or ""), str(cookie.get("domain") or "").lstrip(".").lower(), str(cookie.get("path") or "/"))


def _reconcile_cookies(session: _Session, context: Any) -> int:
    stored = _stored_cookies(session.state_file)
    if not stored:
        return 0
    now = time.time()
    live = {_cookie_key(c) for c in context.cookies()}
    missing = [
        c for c in stored
        if _cookie_key(c) not in live and (float(c.get("expires") or -1) < 0 or float(c.get("expires") or -1) > now)
    ]
    added = 0
    for cookie in missing:
        try:
            context.add_cookies([cookie])
            added += 1
        except Exception:
            continue
    if added:
        _save_storage(session, force=True)
        session.touch()
    return added


def cookie_summary(state_path: str) -> dict[str, Any]:
    session = _session(state_path)
    if cdp_endpoint():
        return {"total": 0, "sites": [], "site_count": 0, "attached": True}
    context = session.context
    if context is not None and not session.shared:
        owner = getattr(context, "browser", None)
        if owner is not None and owner.is_connected() and owner is session.worker.browser:
            try:
                def _sync() -> dict[str, Any]:
                    _apply_default_cookies(session, context)
                    restored = _reconcile_cookies(session, context)
                    return {**_cookie_summary(context), "restored": restored, **cookie_stores()}
                return session.worker.call(_sync, label="cookies")
            except Exception:
                pass
    return {**_stored_cookie_summary(session.state_file), **cookie_stores()}


def _stored_cookies(state_file: str) -> list[dict[str, Any]]:
    try:
        with open(state_file, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else None
    return cookies if isinstance(cookies, list) else []


def clear_cookies(state_path: str, domain: str = "") -> dict[str, Any]:
    session = _session(state_path)
    if cdp_endpoint():
        raise BrowserError(_ATTACHED_COOKIES)

    def _run() -> dict[str, Any]:
        context = _context(session)
        site = _clean_domain(domain).lstrip(".")
        if site:
            kept = [c for c in context.cookies() if str(c.get("domain") or "").lstrip(".") != site
                    and not str(c.get("domain") or "").lstrip(".").endswith("." + site)]
            context.clear_cookies()
            if kept:
                context.add_cookies(kept)
        else:
            context.clear_cookies()
        _purge_cookie_store(cookie_browser(), site)
        _save_storage(session, force=True)
        session.touch()
        return {**_cookie_summary(context), **cookie_stores()}

    return session.worker.call(_run, label="cookies")


LOCAL_BROWSERS = ("chrome", "edge", "brave", "chromium", "firefox", "opera", "vivaldi", "safari", "island")
ISLAND_COOKIE_FILE = os.path.expanduser("~/Library/Application Support/Island/Default/Cookies")
ISLAND_KEYCHAIN_SERVICE = "Island Safe Storage"
ISLAND_KEYCHAIN_ACCOUNT = "Island"
LIVECODE_HOME = os.path.expanduser("~/.livecode")
MANUAL_COOKIE_SOURCE = "manual"
COOKIE_SOURCES = LOCAL_BROWSERS + (MANUAL_COOKIE_SOURCE,)
DEFAULT_COOKIE_BROWSER = "chrome"


def _cookie_source(value: Any) -> str:
    name = str(value or "").strip().lower()
    if name not in COOKIE_SOURCES:
        raise BrowserError(f"Pick one of: {', '.join(COOKIE_SOURCES)}.")
    return name


def cookie_store_path(source: str) -> str:
    return os.path.join(LIVECODE_HOME, _cookie_source(source), "cookies", "cookies.json")


def _cookie_alive(cookie: dict[str, Any], now: float) -> bool:
    try:
        expires = float(cookie.get("expires") or -1)
    except (TypeError, ValueError):
        return True
    return expires < 0 or expires > now


def read_cookie_store(source: str) -> list[dict[str, Any]]:
    try:
        with open(cookie_store_path(source), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return []
    return [c for c in cookies if isinstance(c, dict) and c.get("name") and c.get("value")]


def _write_cookie_store(source: str, cookies: list[dict[str, Any]]) -> None:
    path = cookie_store_path(source)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"browser": source, "updated": int(time.time()), "cookies": cookies}, handle)
    os.replace(tmp, path)


def save_cookie_store(source: str, cookies: list[dict[str, Any]]) -> int:
    now = time.time()
    merged = {_cookie_key(c): c for c in read_cookie_store(source)}
    for cookie in cookies:
        merged[_cookie_key(cookie)] = cookie
    kept = [c for c in merged.values() if _cookie_alive(c, now)]
    _write_cookie_store(source, kept)
    return len(kept)


def _purge_cookie_store(source: str, domain: str = "") -> None:
    stored = read_cookie_store(source)
    if not stored:
        return
    site = _clean_domain(domain).lstrip(".")
    if not site:
        kept: list[dict[str, Any]] = []
    else:
        kept = [c for c in stored if str(c.get("domain") or "").lstrip(".") != site
                and not str(c.get("domain") or "").lstrip(".").endswith("." + site)]
    _write_cookie_store(source, kept)


def cookie_browser() -> str:
    value = str(_read_browser_settings().get("cookie_browser") or "").strip().lower()
    return value if value in COOKIE_SOURCES else DEFAULT_COOKIE_BROWSER


def save_cookie_browser(value: Any) -> str:
    name = _cookie_source(value)
    settings = _read_browser_settings()
    settings["cookie_browser"] = name
    _write_browser_settings(settings)
    return name


def cookie_stores() -> dict[str, Any]:
    return {
        "default_browser": cookie_browser(),
        "stores": {source: len(read_cookie_store(source)) for source in COOKIE_SOURCES},
        "store_root": LIVECODE_HOME,
    }


def _cookie_marker_path(session: _Session) -> str:
    return os.path.join(session.storage_dir, "cookie_source.json")


def _read_cookie_marker(session: _Session) -> dict[str, Any]:
    try:
        with open(_cookie_marker_path(session), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cookie_marker(session: _Session, source: str, cookies: list[dict[str, Any]]) -> None:
    payload = {"browser": source, "keys": [list(_cookie_key(c)) for c in cookies]}
    try:
        with open(_cookie_marker_path(session), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError:
        pass


def _drop_cookies(context: Any, keys: set[tuple[str, str, str]]) -> int:
    if not keys:
        return 0
    live = context.cookies()
    kept = [c for c in live if _cookie_key(c) not in keys]
    if len(kept) == len(live):
        return 0
    context.clear_cookies()
    if kept:
        context.add_cookies(kept)
    return len(live) - len(kept)


def _add_cookies(context: Any, cookies: list[dict[str, Any]]) -> tuple[int, int]:
    added, failed = 0, 0
    for start in range(0, len(cookies), 50):
        batch = cookies[start:start + 50]
        try:
            context.add_cookies(batch)
            added += len(batch)
        except Exception:
            for cookie in batch:
                try:
                    context.add_cookies([cookie])
                    added += 1
                except Exception:
                    failed += 1
    return added, failed


def _apply_default_cookies(session: _Session, context: Any) -> int:
    source = cookie_browser()
    now = time.time()
    desired = [c for c in read_cookie_store(source) if _cookie_alive(c, now)]
    marker = _read_cookie_marker(session)
    switching = marker.get("browser") != source
    if switching:
        stale = {tuple(k) for k in marker.get("keys", []) if isinstance(k, list) and len(k) == 3}
        _drop_cookies(context, stale - {_cookie_key(c) for c in desired})
    live = {_cookie_key(c) for c in context.cookies()}
    fresh = [c for c in desired if switching or _cookie_key(c) not in live]
    added, _ = _add_cookies(context, fresh)
    if switching or added:
        _write_cookie_marker(session, source, desired)
        _save_storage(session, force=True)
        session.touch()
    return added


def _reload_open_pages(session: _Session) -> int:
    reloaded = 0
    for page in list(session.tabs.values()):
        try:
            if page.is_closed() or page.url in ("", "about:blank"):
                continue
            page.reload(wait_until="domcontentloaded", timeout=8000)
            reloaded += 1
        except Exception:
            continue
    return reloaded


def _osx_keychain_password_interactive(osx_key_service: str, osx_key_user: str) -> bytes:
    from browser_cookie3 import CHROMIUM_DEFAULT_PASSWORD

    cmd = ["/usr/bin/security", "find-generic-password", "-w", "-a", osx_key_user, "-s", osx_key_service]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _err = proc.communicate()
    if proc.returncode != 0:
        return CHROMIUM_DEFAULT_PASSWORD
    return out.strip()


def _allow_keychain_prompt(browser_cookie3: Any) -> None:
    if sys.platform != "darwin":
        return
    browser_cookie3._get_osx_keychain_password = _osx_keychain_password_interactive


def _local_browser_cookie_loader(browser_cookie3: Any, name: str) -> Callable[..., Any] | None:
    loader = getattr(browser_cookie3, name, None)
    if loader is not None or name != "island":
        return loader

    chromium_based = getattr(browser_cookie3, "ChromiumBased", None)
    if chromium_based is None:
        return None
    service = os.environ.get("LIVECODE_ISLAND_KEYCHAIN_SERVICE", "").strip() or ISLAND_KEYCHAIN_SERVICE
    account = os.environ.get("LIVECODE_ISLAND_KEYCHAIN_ACCOUNT", "").strip() or ISLAND_KEYCHAIN_ACCOUNT

    def _load_island_cookies(*, domain_name: str = "") -> Any:
        return chromium_based(
            "island", cookie_file=ISLAND_COOKIE_FILE, domain_name=domain_name,
            osx_key_service=service, osx_key_user=account,
        ).load()

    return _load_island_cookies


def local_cookie_import_available() -> bool:
    try:
        import browser_cookie3
    except Exception:
        return False
    return True


def read_local_browser_cookies(browser_name: str, domain: str = "") -> list[dict[str, Any]]:
    name = str(browser_name or "").strip().lower()
    if name not in LOCAL_BROWSERS:
        raise BrowserError(f"Pick one of: {', '.join(LOCAL_BROWSERS)}.")
    try:
        import browser_cookie3
    except Exception as exc:
        raise BrowserError("Reading a local browser's cookies needs the browser-cookie3 package on the LiveCode server: pip install browser-cookie3") from exc
    _allow_keychain_prompt(browser_cookie3)
    loader = _local_browser_cookie_loader(browser_cookie3, name)
    if loader is None:
        raise BrowserError(f"browser-cookie3 can't read {name} here.")
    site = _clean_domain(domain).lstrip(".")
    try:
        jar = loader(domain_name=site) if site else loader()
    except Exception as exc:
        raise BrowserError(f"Could not read {name}'s cookies: {_first_line(exc)}") from exc
    out = []
    found = 0
    for item in jar:
        found += 1
        if not item.value:
            continue
        cookie = _cookie(
            item.name, item.value, item.domain, path=item.path or "/", expires=item.expires,
            secure=bool(item.secure), http_only=bool((getattr(item, "_rest", None) or {}).get("HTTPOnly") is not None),
            host_only=not str(item.domain or "").startswith("."),
        )
        if cookie:
            out.append(cookie)
    if found and not out:
        raise BrowserError(
            f"Found {found} cookies in {name} but could not decrypt any of them, so nothing was imported "
            "(importing them would only add blank cookies and the site would still ask you to sign in). "
            "Allow the macOS Keychain prompt for this browser's \"Safe Storage\" key, or export the cookies from the browser and paste them instead."
        )
    return out


DEV_PORTS = (3000, 3001, 4200, 5000, 5173, 5174, 8000, 8080, 8081, 8888, 4000, 4321, 3333, 9000)


def detect_dev_servers(ports: tuple[int, ...] = DEV_PORTS, timeout: float = 0.25) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
                conn.settimeout(timeout * 2)
                conn.sendall(b"HEAD / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                head = conn.recv(64)
        except OSError:
            continue
        if head.startswith(b"HTTP/"):
            found.append({"port": port, "url": f"http://localhost:{port}/"})
    return found
