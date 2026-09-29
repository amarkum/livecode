"""Long-running commands the agent started with run_command(background=true).

Each command runs in its own process group with output going to a log file,
so dev servers and watchers keep running while the agent works. Everything
still running is stopped when the server process exits.
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any

MAX_RUNNING_PER_SESSION = 8
STATUS_TAIL_CHARS = 16_000
MAX_WAIT_SECONDS = 120

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")

_lock = threading.Lock()
_commands: dict[str, dict[str, Any]] = {}


def clean_terminal_text(text: str) -> str:
    """Strip ANSI escapes and collapse carriage-return progress redraws."""
    cleaned = _ANSI_RE.sub("", text or "")
    if "\r" in cleaned:
        cleaned = "\n".join(line.rsplit("\r", 1)[-1] for line in cleaned.replace("\r\n", "\n").split("\n"))
    return cleaned


def _log_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "livecode-bg")
    os.makedirs(path, exist_ok=True)
    return path


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_process_tree(proc: subprocess.Popen, *, grace_s: float = 3.0) -> None:
    """Stop ``proc`` and everything it started, even after ``proc`` itself exited."""
    if os.name != "posix":
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        proc.poll()
        if not _group_alive(proc.pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.poll()


def _read_tail(path: str, max_chars: int = STATUS_TAIL_CHARS) -> tuple[str, int]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_chars * 4:
                f.seek(size - max_chars * 4)
            raw = f.read()
    except OSError:
        return "", 0
    text = clean_terminal_text(raw.decode("utf-8", errors="replace"))
    if len(text) > max_chars:
        text = "…" + text[-max_chars:]
    return text, size


def start_background(command: str, cwd: str, env: dict[str, str], session_id: str | None) -> dict[str, Any]:
    owner = session_id or ""
    with _lock:
        running = [
            c for c in _commands.values()
            if c["session_id"] == owner and c["proc"].poll() is None
        ]
        if len(running) >= MAX_RUNNING_PER_SESSION:
            ids = ", ".join(c["id"] for c in running)
            return {
                "error": f"{len(running)} background commands are already running ({ids}). Stop one with kill_command first.",
                "error_kind": "too_many_background_commands",
            }
    command_id = f"bg_{uuid.uuid4().hex[:8]}"
    log_path = os.path.join(_log_dir(), f"{command_id}.log")
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=os.name == "posix",
            )
    except OSError as exc:
        return {"error": str(exc)}
    entry = {
        "id": command_id,
        "session_id": owner,
        "command": command,
        "cwd": cwd,
        "env": dict(env),
        "proc": proc,
        "log_path": log_path,
        "started_at": time.time(),
    }
    with _lock:
        _commands[command_id] = entry
    # Give it a moment so an immediate failure (bad flag, port in use) shows up now.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)
    return describe(command_id)


def describe(command_id: str) -> dict[str, Any]:
    with _lock:
        entry = _commands.get(command_id)
    if not entry:
        return {"error": f"No background command with id {command_id}.", "error_kind": "unknown_command"}
    proc = entry["proc"]
    exit_code = proc.poll()
    output, size = _read_tail(entry["log_path"])
    return {
        "success": True,
        "background": True,
        "command_id": command_id,
        "command": entry["command"],
        "running": exit_code is None,
        "exit_code": exit_code,
        "pid": proc.pid,
        "elapsed_s": round(time.time() - entry["started_at"], 1),
        "output": output,
        "output_bytes": size,
        "log_file": entry["log_path"],
    }


def wait_for(command_id: str, wait_seconds: float = 0, until: str = "") -> dict[str, Any]:
    with _lock:
        entry = _commands.get(command_id)
    if not entry:
        return describe(command_id)
    pattern = None
    if until:
        try:
            pattern = re.compile(until)
        except re.error as exc:
            return {"error": f"Invalid until pattern: {exc}", "error_kind": "invalid_input"}
    deadline = time.monotonic() + max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
    matched = False
    while True:
        if entry["proc"].poll() is not None:
            break
        if pattern is not None:
            output, _size = _read_tail(entry["log_path"])
            if pattern.search(output):
                matched = True
                break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    out = describe(command_id)
    if pattern is not None:
        out["until_matched"] = matched
    return out


def kill(command_id: str) -> dict[str, Any]:
    with _lock:
        entry = _commands.get(command_id)
    if not entry:
        return describe(command_id)
    kill_process_tree(entry["proc"])
    out = describe(command_id)
    out["killed"] = True
    return out


_URL_PORT_RE = re.compile(r"(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|\[::\])(?::(\d{2,5}))", re.I)
_FLAG_PORT_RE = re.compile(r"(?:--port[= ]|-p ?|PORT=)(\d{2,5})\b")
_PORT_BUSY_RE = re.compile(r"EADDRINUSE|address already in use|port (?:\d+ )?is (?:already )?in use|Only one usage of each socket address", re.I)


def _port_of(command: str, output: str) -> int:
    """The port a dev server listens on: the URL it printed, else a --port flag in its command."""
    for source, pattern in ((output, _URL_PORT_RE), (command, _FLAG_PORT_RE), (command, _URL_PORT_RE)):
        found = pattern.findall(source or "")
        if found:
            try:
                port = int(found[-1] if source is output else found[0])
            except ValueError:
                continue
            if 1 <= port <= 65535:
                return port
    return 0


def port_listeners(port: int) -> list[int]:
    """PIDs of the processes listening on a TCP port (lsof first, then ss or fuser)."""
    if not port or os.name != "posix":
        return []
    attempts = (
        ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        ["fuser", f"{port}/tcp"],
        ["ss", "-ltnpH", f"sport = :{port}"],
    )
    for argv in attempts:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        text = done.stdout or ""
        if argv[0] == "fuser":
            text = (done.stdout or "") + " " + (done.stderr or "")
            pids = [int(p) for p in re.findall(r"\b(\d+)\b", text) if p != str(port)]
        elif argv[0] == "ss":
            pids = [int(p) for p in re.findall(r"pid=(\d+)", text)]
        else:
            pids = [int(p) for p in re.findall(r"\b(\d+)\b", text)]
        pids = sorted({p for p in pids if p > 1 and p != os.getpid()})
        if pids or done.returncode == 0:
            return pids
    return []


def free_port(port: int, *, grace_s: float = 3.0) -> list[int]:
    """Stops whatever still listens on ``port`` (a server left over from an earlier run, a child that
    outlived its process group). Returns the PIDs it signalled."""
    pids = port_listeners(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + grace_s
    while pids and time.monotonic() < deadline and port_listeners(port):
        time.sleep(0.1)
    for pid in port_listeners(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def restart(command_id: str = "", *, command: str = "", cwd: str = "", env: dict[str, str] | None = None,
            session_id: str | None = None, port: int = 0, wait_seconds: float = 0, until: str = "") -> dict[str, Any]:
    """Stops a background command (and anything left on its port) and starts it again.

    With no command_id, ``command`` is started fresh after the port is freed: for a server that was
    started outside LiveCode (the user's terminal, an earlier session). A server whose port is still
    busy when it comes back up gets the port freed and one more start."""
    with _lock:
        entry = _commands.get(command_id) if command_id else None
    if command_id and not entry:
        return describe(command_id)
    if entry is not None:
        before, _size = _read_tail(entry["log_path"], 4000)
        command = command or entry["command"]
        cwd = cwd or entry["cwd"]
        env = env or entry.get("env") or dict(os.environ)
        session_id = entry["session_id"] if session_id is None else session_id
        port = port or _port_of(entry["command"], before)
        was_running = entry["proc"].poll() is None
        kill_process_tree(entry["proc"])
    else:
        before, was_running = "", False
        port = port or _port_of(command, "")
    if not command.strip():
        return {"error": "Pass command_id (a background command to restart) or command (what to start).", "error_kind": "invalid_input"}
    if not cwd:
        return {"error": "cwd is needed to start a command.", "error_kind": "invalid_input"}
    freed = free_port(port) if port else []
    out = start_background(command, cwd, env or dict(os.environ), session_id)
    if out.get("error"):
        return out
    if out.get("exit_code") is not None and _PORT_BUSY_RE.search(out.get("output") or "") and port:
        # It died on a busy port: whatever holds it took longer to go; clear it and try once more.
        freed += free_port(port, grace_s=5.0)
        time.sleep(0.5)
        out = start_background(command, cwd, env or dict(os.environ), session_id)
        if out.get("error"):
            return out
    if wait_seconds or until:
        out = wait_for(out["command_id"], wait_seconds, until)
    if command_id:
        out["restarted"] = command_id
        out["was_running"] = was_running
    if port:
        out["port"] = port
    if freed:
        out["freed_port"] = {"port": port, "pids": freed}
    running = out.get("running")
    out["hint"] = (
        ("Restarted" if command_id else "Started") + (f" on port {port}" if port else "") + ". " +
        ("It is running: reload the page in the browser (reload {hard: true} if it still shows old code)." if running
         else "It exited already: read its output above, fix the cause, and start it again.")
    )
    return out


def stop_all() -> None:
    with _lock:
        entries = list(_commands.values())
    for entry in entries:
        kill_process_tree(entry["proc"], grace_s=1.0)


atexit.register(stop_all)
