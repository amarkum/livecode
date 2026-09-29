from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from livecode.lsp_client import _frame_lsp_message, _read_lsp_frame  # noqa: F401

_MAX_SERVERS = 8
_server_slots = threading.Semaphore(_MAX_SERVERS)

_LANG_SERVERS = {
    "python": [sys.executable, "-m", "pylsp"],
}


def register_livecode_lsp_routes(app, socketio, rt):
    try:
        from flask_sock import Sock
    except Exception as exc:
        logger = rt.__dict__.get("logger")
        if logger:
            logger.warning("LiveCode LSP bridge disabled (flask-sock missing): %s", exc)
        return

    logger = rt.__dict__.get("logger")
    request = rt.__dict__["request"]

    sock = Sock(app)

    @sock.route("/livecode/lsp/<lang>")
    def livecode_lsp_bridge(ws, lang):
        argv = _LANG_SERVERS.get((lang or "").lower())
        if not argv:
            ws.close(1008, f"unsupported language: {lang}")
            return

        project = request.args.get("project") or ""
        project = os.path.abspath(os.path.expanduser(project)) if project else ""
        if not project or not os.path.isdir(project):
            ws.close(1008, "project path not found")
            return

        if not _server_slots.acquire(blocking=False):
            ws.close(1013, "too many language servers active")
            return

        proc = None
        pump = None
        try:
            proc = subprocess.Popen(
                argv,
                cwd=project,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if logger:
                logger.info("LiveCode LSP: started %s (pid=%s) for %s", lang, proc.pid, project)

            stop = threading.Event()

            def pump_stdout_to_ws():
                try:
                    while not stop.is_set():
                        body = _read_lsp_frame(proc.stdout)
                        if body is None:
                            break
                        try:
                            ws.send(body.decode("utf-8"))
                        except Exception:
                            break
                finally:
                    stop.set()

            def drain_stderr():
                try:
                    for raw in iter(proc.stderr.readline, b""):
                        if logger and raw.strip():
                            logger.debug("pylsp[%s]: %s", proc.pid, raw.decode("utf-8", "replace").rstrip())
                except Exception:
                    pass

            pump = threading.Thread(target=pump_stdout_to_ws, name=f"lsp-out-{proc.pid}", daemon=True)
            pump.start()
            threading.Thread(target=drain_stderr, name=f"lsp-err-{proc.pid}", daemon=True).start()

            while not stop.is_set():
                try:
                    message = ws.receive(timeout=1)
                except Exception:
                    break
                if message is None:
                    if proc.poll() is not None:
                        break
                    continue
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                try:
                    json.loads(message)
                except ValueError:
                    continue
                try:
                    proc.stdin.write(_frame_lsp_message(message))
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError):
                    break
            stop.set()
        except FileNotFoundError:
            if logger:
                logger.warning("LiveCode LSP: %s not installed (%s)", lang, argv)
            try:
                ws.close(1011, "language server not installed")
            except Exception:
                pass
        except Exception:
            if logger:
                logger.exception("LiveCode LSP bridge error")
        finally:
            if proc is not None:
                for finish in (proc.terminate, proc.kill):
                    if proc.poll() is not None:
                        break
                    try:
                        finish()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        pipe.close()
                    except Exception:
                        pass
            if pump is not None:
                pump.join(timeout=2)
            _server_slots.release()
            if logger:
                logger.info("LiveCode LSP: closed bridge for %s", project)
