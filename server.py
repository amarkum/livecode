"""Standalone LiveCode server.

Run from the repo root:  python3 server.py  (then open http://localhost:9000)
"""
from __future__ import annotations

import fcntl
import logging
import os
import pty
import select
import signal
import struct
import sys
import termios
import threading
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
# The package imports itself as `livecode`, so its parent must be importable;
# host/ provides the `app` package the agent expects from its host.
sys.path.insert(0, os.path.dirname(REPO_DIR))
sys.path.insert(0, os.path.join(REPO_DIR, "host"))

if os.path.basename(REPO_DIR) != "livecode":
    sys.exit("The repo folder must be named 'livecode' (the package imports itself by that name).")

from flask import Flask, Response, jsonify, redirect, request, stream_with_context  # noqa: E402
from flask_socketio import SocketIO  # noqa: E402

from app import runtime as host_runtime  # noqa: E402
import helpers  # noqa: E402
from ide_socket import register_ide_socketio  # noqa: E402

logging.basicConfig(level=os.environ.get("LIVECODE_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("LiveCode")

MONACO_CDN = "https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min"
PORT = int(os.environ.get("LIVECODE_PORT", "9000"))

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = os.environ.get("LIVECODE_SECRET_KEY") or os.urandom(16).hex()
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=[f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"])


def _execute_command_pty_unused(*_args, **_kwargs):
    raise NotImplementedError("execute_command_pty is not used by the LiveCode tool loop")


rt = types.SimpleNamespace(
    jsonify=jsonify,
    request=request,
    Response=Response,
    stream_with_context=stream_with_context,
    LIVECODE_LOGGER=logger,
    logger=logger,
    create_diff_html=helpers.create_diff_html,
    model_supports_multimodal=host_runtime.model_supports_multimodal,
    execute_command_pty=_execute_command_pty_unused,
    get_model_max_tokens=host_runtime.get_model_max_tokens,
    LARGE_CONTENT_CHAR_THRESHOLD=host_runtime.LARGE_CONTENT_CHAR_THRESHOLD,
    LARGE_FILE_COUNT_THRESHOLD=host_runtime.LARGE_FILE_COUNT_THRESHOLD,
    _repo_grep=helpers._repo_grep,
    _repo_read_file=helpers._repo_read_file,
    _repo_list_dir=helpers._repo_list_dir,
    _repo_ast_symbols=helpers._repo_ast_symbols,
)

from livecode.routes import register_livecode_routes  # noqa: E402
from livecode.lsp_routes import register_livecode_lsp_routes  # noqa: E402

register_livecode_routes(app, socketio, rt)
register_livecode_lsp_routes(app, socketio, rt)
register_ide_socketio(socketio, rt)


# --- terminal (one PTY shell per socket) ------------------------------------

_terminals: dict[str, tuple[int, int]] = {}
_terminals_lock = threading.Lock()


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _pump_terminal(sid: str, fd: int) -> None:
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                with _terminals_lock:
                    if sid not in _terminals:
                        return
                continue
            data = os.read(fd, 65536)
        except OSError:
            return
        if not data:
            return
        socketio.emit("terminal_output", {"output": data.decode("utf-8", errors="replace")}, room=sid)


def _start_terminal(sid: str, cwd: str | None, rows: int, cols: int) -> int:
    with _terminals_lock:
        if sid in _terminals:
            return _terminals[sid][1]
    shell = os.environ.get("SHELL") or "/bin/zsh"
    workdir = os.path.expanduser(cwd) if cwd and os.path.isdir(os.path.expanduser(cwd)) else os.path.expanduser("~")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(workdir)
        env = dict(os.environ, TERM="xterm-256color")
        os.execvpe(shell, [shell, "-l"], env)
    _set_winsize(fd, rows, cols)
    with _terminals_lock:
        _terminals[sid] = (pid, fd)
    threading.Thread(target=_pump_terminal, args=(sid, fd), daemon=True).start()
    return fd


@socketio.on("terminal_init")
def on_terminal_init(data):
    data = data or {}
    _start_terminal(request.sid, data.get("cwd"), int(data.get("rows") or 24), int(data.get("cols") or 80))


@socketio.on("terminal_input")
def on_terminal_input(data):
    fd = _start_terminal(request.sid, None, 24, 80)
    text = (data or {}).get("input") or ""
    if text:
        os.write(fd, text.encode("utf-8"))


@socketio.on("terminal_resize")
def on_terminal_resize(data):
    with _terminals_lock:
        entry = _terminals.get(request.sid)
    if entry:
        data = data or {}
        _set_winsize(entry[1], int(data.get("rows") or 24), int(data.get("cols") or 80))


@socketio.on("disconnect")
def on_disconnect(*_args):
    with _terminals_lock:
        entry = _terminals.pop(request.sid, None)
    if entry:
        pid, fd = entry
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# --- page shell ----------------------------------------------------------------

@app.route("/templates/js/monaco-editor/<path:filename>")
def monaco_redirect(filename):
    return redirect(f"{MONACO_CDN}/{filename}")


@app.route("/asset/<path:filename>")
def host_asset(filename):
    from flask import send_from_directory
    return send_from_directory(os.path.join(REPO_DIR, "host", "static", "asset"), filename, max_age=86400)


@app.route("/host/static/<path:filename>")
def host_static(filename):
    from flask import send_from_directory
    return send_from_directory(os.path.join(REPO_DIR, "host", "static"), filename)


def _read(rel: str) -> str:
    with open(os.path.join(REPO_DIR, rel), encoding="utf-8") as fh:
        return fh.read()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LiveCode</title>
<link rel="icon" type="image/png" href="/livecode/assets/favicon.png">
<script>
  window.MonacoEnvironment = {
    getWorkerUrl: function () {
      var code = "self.MonacoEnvironment={baseUrl:'__MONACO__/'};importScripts('__MONACO__/vs/base/worker/workerMain.js');";
      return 'data:text/javascript;charset=utf-8,' + encodeURIComponent(code);
    }
  };
</script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<link rel="stylesheet" href="/host/static/host.css">
<link rel="stylesheet" href="/livecode/static/css/livecode.css">
<style>
  html, body { margin: 0; height: 100%; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }
  body.dark-theme { background: #1e1e1e; color: #ddd; }
  body.white-theme { background: #f8fafc; color: #1e293b; }
  body.black-theme { background: #0a0a0a; color: #d4d4d4; }
  body.pink-theme { background: #fdf2f8; color: #831843; }
  #ide-editor-section { height: 100vh; }
</style>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script src="https://cdn.jsdelivr.net/npm/socket.io-client@4.7.5/dist/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>if (window.mermaid) mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });</script>
<script src="__MONACO__/vs/loader.js"></script>
<script>
  // Hooks the LiveCode frontend expects from its host page.
  window.closeAllSectionsExcept = window.closeAllSectionsExcept || function () {};
  window.updateDockIndicators = window.updateDockIndicators || function () {};
  window.livecodeRecordToolOpen = window.livecodeRecordToolOpen || function () {};
  window.LIVECODE_MONACO_VS = "__MONACO__/vs";
</script>
</head>
<body class="dark-theme">
<script>
  try {
    var saved = localStorage.getItem("livecode-theme");
    if (["dark", "white", "black", "pink"].indexOf(saved) >= 0) document.body.className = saved + "-theme";
  } catch (e) {}
</script>
__SECTION__
<div id="chatbot-model-dropdown" class="airflow-dropdown-opaque chatbot-model-dropdown" style="display:none;position:fixed;min-width:180px;max-height:320px;overflow:hidden;border:1px solid rgba(71,85,105,0.4);border-radius:12px;box-shadow:0 -4px 20px rgba(0,0,0,0.3);z-index:10050;flex-direction:column;" role="listbox" aria-label="Select model">
  <div id="chatbot-model-dropdown-list" class="chatbot-model-dropdown-list" style="flex:1;overflow-y:auto;max-height:320px;"></div>
</div>
<script src="/asset/material-icons.js"></script>
<script src="/host/static/livecode-host.js"></script>
<script src="/livecode/static/js/livecode.js"></script>
<script src="/livecode/static/js/livecode-ts-intel.js"></script>
<script src="/livecode/static/js/livecode-lsp.js"></script>
<script>
  document.addEventListener('DOMContentLoaded', function () { window.createAndShowIDEEditor(); });
</script>
</body>
</html>
"""


@app.route("/")
def index():
    html = PAGE.replace("__MONACO__", MONACO_CDN).replace("__SECTION__", _read("templates/sections/ide-editor.html"))
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    logger.info("LiveCode running at http://localhost:%s", PORT)
    socketio.run(app, host="127.0.0.1", port=PORT, debug=False, allow_unsafe_werkzeug=True)
