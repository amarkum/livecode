"""restart_command: a dev server is stopped (with whatever still holds its port) and started again."""
import os
import socket
import subprocess
import sys
import time

from livecode import bg_commands as bg


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serves(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return True
    except OSError:
        return False


def test_restart_a_background_server():
    port = _port()
    first = bg.start_background(f"{sys.executable} -m http.server {port} --bind 127.0.0.1", "/tmp", dict(os.environ), "s")
    bg.wait_for(first["command_id"], 10, "Serving")
    try:
        out = bg.restart(first["command_id"], wait_seconds=10, until="Serving")
        assert out["running"] and out["command_id"] != first["command_id"] and out["port"] == port
        assert out["restarted"] == first["command_id"] and out["was_running"]
        assert not bg.describe(first["command_id"])["running"]
        assert _serves(port)
    finally:
        bg.stop_all()


def test_restart_frees_a_port_held_by_a_server_started_elsewhere():
    port = _port()
    outside = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while not _serves(port) and time.time() < deadline:
        time.sleep(0.1)
    try:
        out = bg.restart(command=f"{sys.executable} -m http.server {port} --bind 127.0.0.1", cwd="/tmp", env=dict(os.environ),
                         session_id="s", port=port, wait_seconds=10, until="Serving")
        assert out["running"] and outside.pid in out["freed_port"]["pids"]
        outside.wait(timeout=5)
        assert _serves(port)
    finally:
        outside.kill()
        bg.stop_all()


def test_port_is_read_from_the_output_or_the_command():
    assert bg._port_of("npm run dev -- --port 5173", "") == 5173
    assert bg._port_of("vite", "  Local:   http://localhost:5174/") == 5174
    assert bg._port_of("flask run", "") == 0


def test_errors():
    assert bg.restart("bg_missing")["error_kind"] == "unknown_command"
    assert bg.restart(command="", cwd="/tmp")["error_kind"] == "invalid_input"
