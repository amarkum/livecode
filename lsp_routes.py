from __future__ import annotations

import json
import os
import subprocess
import threading

from livecode.lsp_client import _frame_lsp_message, _read_lsp_frame  # noqa: F401

from livecode import lsp_servers

_MAX_SERVERS = 12
_server_slots = threading.Semaphore(_MAX_SERVERS)


def server_env(lang_id: str, project: str) -> dict[str, str] | None:
    # pylsp resolves project imports through PYTHONPATH; other servers get the inherited env
    # plus the extra bin folders, so servers that spawn helpers (jdtls -> java, gopls -> go) find them.
    if lang_id == "python":
        from livecode.lsp_client import _pylsp_env

        env = _pylsp_env(project)
    else:
        env = os.environ.copy()
    env["PATH"] = lsp_servers._search_path()
    return env


def register_livecode_lsp_routes(app, socketio, rt):
    logger = rt.__dict__.get("logger")
    request = rt.__dict__["request"]
    jsonify = rt.__dict__["jsonify"]
    bridge = {"available": False, "reason": ""}

    @app.route("/livecode/lsp/settings", methods=["GET", "POST"])
    def livecode_lsp_settings():
        if request.method == "GET":
            return jsonify({"success": True, **lsp_servers.status(), "bridge": bridge})
        try:
            status = lsp_servers.update(request.get_json(silent=True) or {})
        except lsp_servers.LspSettingsError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"Could not save the setting: {e.strerror or e}"}), 500
        return jsonify({"success": True, **status, "bridge": bridge})

    @app.route("/livecode/lsp/install", methods=["POST"])
    def livecode_lsp_install():
        data = request.get_json(silent=True) or {}
        try:
            job = lsp_servers.start_install(
                str(data.get("language") or ""),
                str(data.get("server") or ""),
                lint_plugins=bool(data.get("lint_plugins")),
            )
        except lsp_servers.LspSettingsError as e:
            return jsonify({"error": str(e)}), 400
        if logger:
            logger.info("LiveCode LSP: installing %s with %s", job["server"], job["command"])
        return jsonify({"success": True, "job": job})

    @app.route("/livecode/lsp/install/<job_id>", methods=["GET"])
    def livecode_lsp_install_status(job_id):
        job = lsp_servers.install_status(job_id)
        if job is None:
            return jsonify({"error": "No such install job."}), 404
        return jsonify({"success": True, "job": job})

    @app.route("/livecode/lsp/config", methods=["GET"])
    def livecode_lsp_config():
        return jsonify({"success": True, **lsp_servers.client_config(), "bridge": bridge})

    try:
        from flask_sock import Sock
    except Exception as exc:
        bridge["reason"] = "The editor's language-server bridge needs flask-sock on the LiveCode server: pip install flask-sock"
        if logger:
            logger.warning("LiveCode LSP bridge disabled (flask-sock missing): %s", exc)
        return
    bridge["available"] = True

    sock = Sock(app)

    @sock.route("/livecode/lsp/<lang>")
    def livecode_lsp_bridge(ws, lang):
        lang = (lang or "").lower()
        project = request.args.get("project") or ""
        project = os.path.abspath(os.path.expanduser(project)) if project else ""
        if not project or not os.path.isdir(project):
            ws.close(1008, "project path not found")
            return
        resolved = lsp_servers.resolve(lang, project)
        argv = resolved.get("argv")
        if not argv:
            ws.close(1008, str(resolved.get("error") or f"unsupported language: {lang}")[:120])
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
                env=server_env(lang, project),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if logger:
                logger.info("LiveCode LSP: started %s %s (pid=%s) for %s", lang, resolved.get("server"), proc.pid, project)

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
                            logger.debug("%s[%s]: %s", lang, proc.pid, raw.decode("utf-8", "replace").rstrip())
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
