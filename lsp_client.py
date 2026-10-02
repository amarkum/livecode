from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from typing import Any

def _read_lsp_frame(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if b":" in line:
            name, _, value = line.partition(b":")
            headers[name.strip().lower()] = value.strip()
    try:
        length = int(headers.get(b"content-length", b"0"))
    except ValueError:
        return None
    if length <= 0:
        return b""
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return body


def _frame_lsp_message(text: str) -> bytes:
    body = text.encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


_REQUEST_TIMEOUT_S = 12.0
_IDLE_SHUTDOWN_S = 300.0

_procs: dict[str, "_PylspProc"] = {}
_procs_lock = threading.Lock()
_reaper_started = False


def _uri_for(path: str) -> str:
    return "file://" + os.path.abspath(path)


def _dedupe_paths(paths: list[str]) -> list[str]:
    out = []
    seen = set()
    for path in paths:
        clean = os.path.abspath(os.path.expanduser(str(path or "")))
        if not clean or clean in seen or not os.path.isdir(clean):
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _python_import_paths(project_root: str) -> list[str]:
    root = os.path.abspath(os.path.expanduser(project_root))
    candidates = [root]
    if not os.path.isdir(root):
        return []
    for rel in ("src", "lib", "app"):
        candidates.append(os.path.join(root, rel))
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith((".", "__")):
            continue
        if os.path.isfile(os.path.join(path, "__init__.py")):
            candidates.append(root)
        if os.path.isdir(os.path.join(path, "src")):
            candidates.append(os.path.join(path, "src"))
        if os.path.isfile(os.path.join(path, "pyproject.toml")) or os.path.isfile(os.path.join(path, "setup.py")):
            candidates.append(path)
    return _dedupe_paths(candidates)


def _pylsp_env(project_root: str) -> dict[str, str]:
    env = os.environ.copy()
    paths = _python_import_paths(project_root)
    existing = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    pythonpath = os.pathsep.join(_dedupe_paths(paths + existing))
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


class LspUnavailable(RuntimeError):
    pass


class _PylspProc:
    """One language server process for a project root. Despite the name it runs any server
    lsp_servers resolves; Python keeps its PYTHONPATH and jedi extra-path configuration."""

    def __init__(self, project_root: str, lang_id: str = "python") -> None:
        from livecode import lsp_servers

        self.root = os.path.abspath(project_root)
        self.lang_id = lang_id
        self.lang = lsp_servers.language(lang_id) or lsp_servers.language("python")
        self.last_used = time.time()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._docs_lock = threading.RLock()
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self._docs: dict[str, dict[str, Any]] = {}
        self._diagnostics: dict[str, list[dict]] = {}
        self._diag_event = threading.Condition()
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._import_paths = _python_import_paths(self.root) if lang_id == "python" else []
        self._alive = True
        resolved = lsp_servers.resolve(lang_id, self.root)
        if not resolved.get("argv"):
            raise LspUnavailable(str(resolved.get("error") or f"no {lang_id} language server"))
        self.server_name = str(resolved.get("server") or lang_id)
        env = os.environ.copy() if lang_id != "python" else _pylsp_env(self.root)
        env["PATH"] = lsp_servers._search_path()
        try:
            self._proc = subprocess.Popen(
                resolved["argv"],
                cwd=self.root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise LspUnavailable(f"failed to start {self.server_name}: {exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name=f"pylsp-read-{self._proc.pid}", daemon=True
        )
        self._reader.start()
        threading.Thread(
            target=self._drain_stderr, name=f"pylsp-err-{self._proc.pid}", daemon=True
        ).start()
        self._initialize()

    def _send(self, obj: dict) -> None:
        if not self._alive or self._proc.poll() is not None:
            raise LspUnavailable(f"{self.server_name} exited")
        frame = _frame_lsp_message(json.dumps(obj))
        try:
            with self._send_lock:
                self._proc.stdin.write(frame)
                self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            self._alive = False
            raise LspUnavailable(str(exc)) from exc

    def _request(self, method: str, params: dict, timeout: float = _REQUEST_TIMEOUT_S) -> Any:
        if method == "initialize":
            # jdtls, rust-analyzer and metals take a while to index before they answer.
            timeout = max(timeout, 60.0)
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            slot: dict[str, Any] = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise LspUnavailable(f"timeout waiting for {method}")
        if slot["error"]:
            raise LspUnavailable(str(slot["error"]))
        return slot["result"]

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_loop(self) -> None:
        try:
            while True:
                body = _read_lsp_frame(self._proc.stdout)
                if body is None:
                    break
                try:
                    msg = json.loads(body)
                except ValueError:
                    continue
                self._dispatch(msg)
        finally:
            self._alive = False
            with self._lock:
                for slot in self._pending.values():
                    slot["error"] = slot["error"] or f"{self.server_name} closed"
                    slot["event"].set()
                self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid is not None and ("result" in msg or "error" in msg):
            with self._lock:
                slot = self._pending.pop(mid, None)
            if slot is not None:
                slot["result"] = msg.get("result")
                slot["error"] = msg.get("error", {}).get("message") if msg.get("error") else None
                slot["event"].set()
            return
        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            with self._diag_event:
                self._diagnostics[params.get("uri", "")] = params.get("diagnostics") or []
                self._diag_event.notify_all()
            return
        if mid is not None:
            result: Any = None
            if method == "workspace/configuration":
                config = {"plugins": {"jedi": {"extra_paths": self._import_paths}}} if self.lang_id == "python" else {}
                result = [config for _ in (msg.get("params", {}).get("items") or [])]
            try:
                self._send({"jsonrpc": "2.0", "id": mid, "result": result})
            except LspUnavailable:
                pass

    def _drain_stderr(self) -> None:
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr_lines.append(line[:1000])
        except Exception:
            pass

    def error_detail(self) -> str:
        detail = "\n".join(self._stderr_lines).strip()
        return detail[-4000:]

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "processId": None,
                "clientInfo": {"name": "LiveCode-harness", "version": "1"},
                "rootUri": _uri_for(self.root),
                "workspaceFolders": [{"uri": _uri_for(self.root), "name": os.path.basename(self.root) or "project"}],
                "capabilities": {
                    "textDocument": {
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "definition": {"linkSupport": False},
                        "references": {},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "completion": {
                            "completionItem": {
                                "snippetSupport": False,
                                "documentationFormat": ["markdown", "plaintext"],
                            },
                            "contextSupport": True,
                        },
                        "rename": {"prepareSupport": False},
                        "publishDiagnostics": {"relatedInformation": False},
                    },
                    "workspace": {"workspaceFolders": True, "configuration": True},
                },
            },
        )
        self._notify("initialized", {})

    def close(self) -> None:
        self._alive = False
        try:
            self._notify("shutdown", {})
            self._notify("exit", {})
        except Exception:
            pass
        for finish in (self._proc.terminate, self._proc.kill):
            if self._proc.poll() is not None:
                break
            try:
                finish()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass

    def _language_id(self, abs_path: str) -> str:
        from livecode import lsp_servers

        return lsp_servers.language_id_for_path(self.lang, abs_path)

    def _ensure_open(self, abs_path: str) -> str:
        uri = _uri_for(abs_path)
        try:
            st = os.stat(abs_path)
        except OSError as exc:
            raise LspUnavailable(f"cannot stat {abs_path}: {exc}") from exc
        stamp = (st.st_mtime_ns, st.st_size)
        with self._docs_lock:
            rec = self._docs.get(uri)
            if rec is not None and rec["stamp"] == stamp:
                return uri
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError as exc:
                raise LspUnavailable(str(exc)) from exc
            with self._diag_event:
                self._diagnostics.pop(uri, None)
            if rec is None:
                self._docs[uri] = {"version": 1, "stamp": stamp}
                self._notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": uri, "languageId": self._language_id(abs_path), "version": 1, "text": text}},
                )
            else:
                rec["version"] += 1
                rec["stamp"] = stamp
                self._notify(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": rec["version"]},
                        "contentChanges": [{"text": text}],
                    },
                )
        return uri

    def definition(self, abs_path: str, line: int, char: int) -> Any:
        uri = self._ensure_open(abs_path)
        return self._request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": char}},
        )

    def references(self, abs_path: str, line: int, char: int, include_decl: bool = True) -> Any:
        uri = self._ensure_open(abs_path)
        return self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": char},
                "context": {"includeDeclaration": include_decl},
            },
        )

    def hover(self, abs_path: str, line: int, char: int) -> Any:
        uri = self._ensure_open(abs_path)
        return self._request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": char}},
        )

    def document_symbols(self, abs_path: str) -> Any:
        uri = self._ensure_open(abs_path)
        return self._request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})

    def completion(self, abs_path: str, line: int, char: int) -> Any:
        uri = self._ensure_open(abs_path)
        return self._request(
            "textDocument/completion",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": char}},
        )

    def rename(self, abs_path: str, line: int, char: int, new_name: str) -> Any:
        uri = self._ensure_open(abs_path)
        return self._request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": char},
                "newName": new_name,
            },
        )

    def diagnostics(self, abs_path: str, wait_s: float = 3.0) -> list[dict]:
        uri = self._ensure_open(abs_path)
        deadline = time.time() + max(0.3, wait_s)
        with self._diag_event:
            while uri not in self._diagnostics:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._diag_event.wait(timeout=min(remaining, 0.5))
            return list(self._diagnostics.get(uri) or [])


def _reaper() -> None:
    while True:
        time.sleep(60)
        now = time.time()
        with _procs_lock:
            stale = [k for k, p in _procs.items() if now - p.last_used > _IDLE_SHUTDOWN_S]
            for k in stale:
                proc = _procs.pop(k, None)
                if proc:
                    try:
                        proc.close()
                    except Exception:
                        pass


def _get_proc(project_root: str, lang_id: str = "python") -> _PylspProc:
    global _reaper_started
    root = os.path.abspath(os.path.expanduser(project_root))
    key = f"{lang_id}:{root}"
    with _procs_lock:
        proc = _procs.get(key)
        if proc is not None and proc._alive and proc._proc.poll() is None:
            proc.last_used = time.time()
            return proc
        if proc is not None:
            try:
                proc.close()
            except Exception:
                pass
            _procs.pop(key, None)
        proc = _PylspProc(root, lang_id)
        _procs[key] = proc
        if not _reaper_started:
            threading.Thread(target=_reaper, name="pylsp-reaper", daemon=True).start()
            _reaper_started = True
        return proc


def _lang_for(abs_path: str) -> str:
    from livecode import lsp_servers

    lang = lsp_servers.language_for_path(abs_path)
    return lang["id"] if lang else ""


def _guard(abs_path: str) -> dict | None:
    if not _lang_for(abs_path):
        return {"error": "No language server covers this file type (see Settings > Languages)", "error_kind": "invalid_input"}
    if not os.path.isfile(abs_path):
        return {"error": f"File not found: {abs_path}", "error_kind": "invalid_input"}
    return None


def _format_unavailable(exc: Exception, detail: str = "", lang_id: str = "python") -> dict:
    from livecode import lsp_servers

    lang = lsp_servers.language(lang_id) or {"label": lang_id}
    message = str(exc).strip() or "language server unavailable"
    if "No module named pylsp" in detail or "No module named 'pylsp'" in detail:
        message = "python-lsp-server is not installed in the active Python environment"
    if detail and detail not in message:
        message = f"{message}\n{detail}"
    return {"error": f"{lang['label']} language server unavailable: {message}", "error_kind": "unavailable"}


def _drop_proc(project_root: str, proc: _PylspProc | None = None, lang_id: str = "python") -> None:
    root = os.path.abspath(os.path.expanduser(project_root))
    key = f"{lang_id}:{root}"
    with _procs_lock:
        cached = _procs.get(key)
        if proc is None or cached is proc:
            _procs.pop(key, None)
    target = proc or cached
    if target is not None:
        try:
            target.close()
        except Exception:
            pass


def _run(project_root: str, abs_path: str, fn_name: str, *args) -> Any:
    bad = _guard(abs_path)
    if bad:
        return bad
    lang_id = _lang_for(abs_path)
    last_exc: Exception | None = None
    last_detail = ""
    for attempt in range(2):
        proc: _PylspProc | None = None
        try:
            proc = _get_proc(project_root, lang_id)
            return getattr(proc, fn_name)(abs_path, *args)
        except LspUnavailable as exc:
            last_exc = exc
            last_detail = proc.error_detail() if proc is not None else ""
            _drop_proc(project_root, proc, lang_id)
            # "Not installed" or "turned off" will not change on a second try.
            if attempt == 0 and proc is not None:
                continue
            return _format_unavailable(exc, last_detail, lang_id)
        except Exception as exc:
            return {"error": f"LSP query failed: {exc}", "error_kind": "unavailable"}
    return _format_unavailable(last_exc or LspUnavailable("language server unavailable"), last_detail, lang_id)


def definition(project_root: str, abs_path: str, line: int, char: int) -> Any:
    return _run(project_root, abs_path, "definition", int(line), int(char))


def references(project_root: str, abs_path: str, line: int, char: int, include_decl: bool = True) -> Any:
    return _run(project_root, abs_path, "references", int(line), int(char), bool(include_decl))


def hover(project_root: str, abs_path: str, line: int, char: int) -> Any:
    return _run(project_root, abs_path, "hover", int(line), int(char))


def document_symbols(project_root: str, abs_path: str) -> Any:
    return _run(project_root, abs_path, "document_symbols")


def completion(project_root: str, abs_path: str, line: int, char: int) -> Any:
    return _run(project_root, abs_path, "completion", int(line), int(char))


def rename(project_root: str, abs_path: str, line: int, char: int, new_name: str) -> Any:
    return _run(project_root, abs_path, "rename", int(line), int(char), str(new_name or ""))


def diagnostics(project_root: str, abs_path: str, wait_s: float = 3.0) -> Any:
    return _run(project_root, abs_path, "diagnostics", float(wait_s))


def is_available(abs_path: str = "") -> bool:
    """Whether a language server is ready for this file (Python when no path is given)."""
    from livecode import lsp_servers

    lang_id = _lang_for(abs_path) if abs_path else "python"
    return bool(lang_id) and bool(lsp_servers.resolve(lang_id).get("argv"))


def shutdown_all() -> None:
    with _procs_lock:
        for proc in list(_procs.values()):
            try:
                proc.close()
            except Exception:
                pass
        _procs.clear()
