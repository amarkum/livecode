from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import site
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .workspace_config import workspace_from_payload

MCP_TOOL_PREFIX = "mcp__"
_STATUS_CACHE: dict[
    tuple[str, tuple[str, ...] | None, tuple[str, ...] | None],
    dict[str, Any],
] = {}
_TOOL_CACHE: dict[
    tuple[str, tuple[str, ...] | None, tuple[tuple[str, tuple[str, ...]], ...] | None, int | None],
    tuple[list[dict[str, Any]], dict[str, "MCPToolBinding"]],
] = {}
_SERVER_TOOL_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_CONTEXT_CACHE: dict[tuple[str, tuple[str, ...] | None], str] = {}
_NAME_CACHE: dict[str, tuple[str, str]] = {}
_TOOL_ANNOTATION_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()
_SERVER_TOOL_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_MCP_RUNTIME_STATE: dict[str, dict[str, Any]] = {}
_TRUSTED_MCP_FINGERPRINTS: set[str] = set()
_TRUST_STORE_LOADED = False
_TRUST_STORE_LOCK = threading.RLock()
_MCP_IMPORT_LOCK = threading.RLock()
_TRUST_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_SECRET_KEYS = ("key", "token", "secret", "password", "authorization", "cookie", "credential")
_SECRET_VALUE_MARKERS: set[str] = set()
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class MCPToolBinding:
    encoded_name: str
    server_name: str
    tool_name: str
    annotations: dict[str, Any]


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    source: str
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    cwd: str = ""
    builtin: bool = False
    config_path: str = ""
    folder_name: str = ""
    folder_order: int = -1
    config_disabled: bool = False


@dataclass(frozen=True)
class MCPConfigEntry:
    path: Path
    source: str
    folder_name: str = ""
    folder_order: int = -1
    inline_servers: dict[str, Any] | None = None
    servers_key: str = "mcpServers"
    workspace_folder: str = ""


def _project_key(project_path: str) -> str:
    return os.path.abspath(os.path.expanduser(project_path or ""))


def _normalized_inline_mcp(workspace_payload: dict[str, Any] | None) -> str:
    if not isinstance(workspace_payload, dict):
        return ""
    servers = workspace_payload.get("mcpServers")
    if not isinstance(servers, dict):
        return ""
    return json.dumps(servers, sort_keys=True, separators=(",", ":"), default=str)


def _workspace_key(project_path: str, workspace_payload: dict[str, Any] | None = None) -> str:
    config_hash = hashlib.sha1(_normalized_inline_mcp(workspace_payload).encode("utf-8")).hexdigest()[:16]
    if not workspace_payload:
        return f"{_project_key(project_path)}|mcp:{config_hash}"
    try:
        workspace = workspace_from_payload(project_path, workspace_payload)
    except ValueError:
        return f"{_project_key(project_path)}|mcp:{config_hash}"
    parts = [folder.path for folder in workspace.folders]
    if not parts:
        return f"{_project_key(project_path)}|mcp:{config_hash}"
    config_path = workspace.config_path or ""
    return "workspace:" + "|".join([*parts, f"config:{config_path}", f"mcp:{config_hash}"])


def _enabled_key(enabled_servers: list[str] | tuple[str, ...] | None) -> tuple[str, ...] | None:
    if enabled_servers is None:
        return None
    return tuple(sorted(str(s) for s in enabled_servers if str(s).strip()))


def _allowlist_key(tool_allowlist_by_server: dict[str, set[str] | frozenset[str] | tuple[str, ...]] | None) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    if not tool_allowlist_by_server:
        return None
    return tuple(
        sorted(
            (str(server), tuple(sorted(str(name) for name in names)))
            for server, names in tool_allowlist_by_server.items()
        )
    )


def _safe_name(value: str) -> str:
    clean = _NAME_RE.sub("_", str(value or "").strip())[:48].strip("_")
    return clean or "server"


def encode_mcp_tool_name(server_name: str, tool_name: str) -> str:
    server = _safe_name(server_name)
    tool = _safe_name(tool_name)
    friendly_name = f"{MCP_TOOL_PREFIX}{server}__{tool}"
    existing = _NAME_CACHE.get(friendly_name)
    if len(friendly_name) <= 64 and existing in (None, (server_name, tool_name)):
        _NAME_CACHE[friendly_name] = (server_name, tool_name)
        return friendly_name

    digest = hashlib.sha1(f"{server_name}\n{tool_name}".encode("utf-8")).hexdigest()[:10]
    prefix = f"{MCP_TOOL_PREFIX}{server[:18]}__"
    room = max(8, 64 - len(prefix) - len(digest) - 1)
    name = f"{prefix}{tool[:room]}_{digest}"
    _NAME_CACHE[name] = (server_name, tool_name)
    return name


def mcp_tool_annotations(encoded_name: str) -> dict[str, Any]:
    return dict(_TOOL_ANNOTATION_CACHE.get(str(encoded_name or ""), {}))


def decode_mcp_tool_name(name: str) -> tuple[str, str]:
    if name in _NAME_CACHE:
        return _NAME_CACHE[name]
    if not str(name or "").startswith(MCP_TOOL_PREFIX):
        raise ValueError("Not an MCP tool name")
    rest = name[len(MCP_TOOL_PREFIX):]
    if "__" not in rest:
        raise ValueError("Invalid MCP tool name")
    server, tool = rest.split("__", 1)
    return server, tool


def is_mcp_tool_name(name: str) -> bool:
    return str(name or "").startswith(MCP_TOOL_PREFIX)


def _contains_secret_key(key: str) -> bool:
    low = str(key or "").lower()
    return any(part in low for part in _SECRET_KEYS)


def _remember_secret_value(value: Any) -> None:
    text = str(value or "")
    if len(text) >= 4:
        _SECRET_VALUE_MARKERS.add(text)


def _sanitize_url(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return "[redacted-url]"
    if not parts.scheme or not parts.netloc:
        return text
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.username or parts.password:
        host = f"[redacted]@{host}"
    query = "[redacted]" if parts.query else ""
    fragment = "[redacted]" if parts.fragment and _contains_secret_key(parts.fragment) else parts.fragment
    return urlunsplit((parts.scheme, host, parts.path, query, fragment))


def _redact(value: Any, key: str = "") -> Any:
    if _contains_secret_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact(v, key) for v in value)
    if isinstance(value, str):
        redacted = _sanitize_url(_PLACEHOLDER_RE.sub("[redacted]", value))
        for secret in sorted(_SECRET_VALUE_MARKERS, key=len, reverse=True):
            if secret:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted
    return value


def _effective_stdio_env(cfg: MCPServerConfig) -> dict[str, str] | None:
    configured_env = dict(cfg.env)
    if not configured_env:
        return None
    return {**os.environ, **configured_env}


def _effective_stdio_cwd(cfg: MCPServerConfig) -> str:
    if cfg.cwd:
        return cfg.cwd
    if cfg.transport == "stdio" and cfg.source == "project" and cfg.config_path:
        return str(Path(cfg.config_path).parent)
    return ""


def _resolved_stdio_command(cfg: MCPServerConfig) -> str:
    if cfg.transport != "stdio" or not cfg.command:
        return cfg.command
    command = os.path.expanduser(cfg.command)
    env = _effective_stdio_env(cfg)
    path_value = (env or os.environ).get("PATH")
    if os.path.isabs(command):
        return os.path.realpath(command)
    if os.sep in command or (os.altsep and os.altsep in command):
        return os.path.realpath(os.path.join(_effective_stdio_cwd(cfg) or os.getcwd(), command))
    resolved = shutil.which(command, path=path_value)
    return os.path.realpath(resolved) if resolved else command


def _runtime_key(cfg: MCPServerConfig) -> str:
    return "|".join([cfg.name, cfg.source, cfg.config_path, _resolved_stdio_command(cfg), chr(0).join(cfg.args), cfg.cwd, cfg.url])


def server_fingerprint(cfg: MCPServerConfig) -> str:
    payload = {
        "name": cfg.name,
        "transport": cfg.transport,
        "source": cfg.source,
        "url": cfg.url,
        "command": cfg.command,
        "resolved_command": _resolved_stdio_command(cfg) if cfg.transport == "stdio" else "",
        "args": list(cfg.args),
        "env": list(cfg.env),
        "headers": list(cfg.headers),
        "cwd": cfg.cwd,
        "config_path": cfg.config_path,
        "folder_name": cfg.folder_name,
        "folder_order": cfg.folder_order,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _trust_store_path() -> Path:
    override = os.environ.get("LIVECODE_MCP_TRUST_STORE", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".livecode" / "mcp-trust.json"


def _load_trusted_mcp_fingerprints() -> None:
    global _TRUST_STORE_LOADED
    with _TRUST_STORE_LOCK:
        if _TRUST_STORE_LOADED:
            return
        _TRUST_STORE_LOADED = True
        path = _trust_store_path()
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("version") != 1:
            return
        values = data.get("trusted_fingerprints")
        if not isinstance(values, list):
            return
        for value in values:
            if isinstance(value, str) and _TRUST_FINGERPRINT_RE.fullmatch(value):
                _TRUSTED_MCP_FINGERPRINTS.add(value)


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        return


def _persist_trusted_mcp_fingerprints() -> None:
    path = _trust_store_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(parent, 0o700)
    payload = {
        "version": 1,
        "trusted_fingerprints": sorted(_TRUSTED_MCP_FINGERPRINTS),
    }
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(parent), prefix=f".{path.name}.", suffix=".tmp", delete=False) as fh:
            tmp_name = fh.name
            json.dump(payload, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path = Path(tmp_name)
        _chmod_best_effort(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def trust_mcp_server(project_path: str, server_name: str, workspace_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _servers_by_name(project_path, workspace_payload).get(server_name)
    if cfg is None:
        return {"success": False, "error": f"MCP server not configured: {server_name}"}
    if cfg.builtin:
        return {"success": True, "trusted": True, "fingerprint": server_fingerprint(cfg)}
    with _TRUST_STORE_LOCK:
        _load_trusted_mcp_fingerprints()
        fingerprint = server_fingerprint(cfg)
        already_trusted = fingerprint in _TRUSTED_MCP_FINGERPRINTS
        _TRUSTED_MCP_FINGERPRINTS.add(fingerprint)
        try:
            _persist_trusted_mcp_fingerprints()
        except OSError as exc:
            if not already_trusted:
                _TRUSTED_MCP_FINGERPRINTS.discard(fingerprint)
            return {"success": False, "trusted": False, "fingerprint": fingerprint, "error": f"Unable to persist MCP server trust: {exc}"}
    refresh_mcp_cache(project_path, workspace_payload)
    return {"success": True, "trusted": True, "fingerprint": fingerprint}


def is_trusted_for_probe(cfg: MCPServerConfig) -> bool:
    return True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + chr(10) + "...[truncated]"


class _BoundedStderrSink:
    def __init__(self, max_chars: int = 8000) -> None:
        self._max_chars = max(0, int(max_chars))
        self._value = ""
        self._lock = threading.Lock()

    def write(self, value: Any) -> int:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        with self._lock:
            self._value = (self._value + text)[-self._max_chars:] if self._max_chars else ""
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        with self._lock:
            return self._value


def _read_bounded_stderr_file(stderr_file: Any, max_bytes: int = 8000) -> str:
    try:
        stderr_file.flush()
        stderr_file.seek(0, os.SEEK_END)
        size = stderr_file.tell()
        stderr_file.seek(max(0, size - max_bytes), os.SEEK_SET)
        value = stderr_file.read()
    except OSError:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _env_summary(cfg: MCPServerConfig) -> dict[str, Any]:
    return {key: "[redacted]" for key, _ in cfg.env}


def _server_diagnostics(cfg: MCPServerConfig, error: str = "") -> dict[str, Any]:
    runtime = _MCP_RUNTIME_STATE.get(_runtime_key(cfg), {})
    diagnostics = {
        "command": cfg.command if cfg.transport == "stdio" else "",
        "resolved_command": _resolved_stdio_command(cfg) if cfg.transport == "stdio" else "",
        "arg_count": len(cfg.args) if cfg.transport == "stdio" else 0,
        "working_directory": runtime.get("working_directory") or _effective_stdio_cwd(cfg) or cfg.folder_name or str(Path(cfg.config_path).parent),
        "pid": runtime.get("pid"),
        "last_started": runtime.get("last_started"),
        "last_exit_code": runtime.get("last_exit_code"),
        "last_error": _truncate_text(_redact(runtime.get("last_error") or "")),
        "stdout": _truncate_text(_redact(runtime.get("stdout") or "")),
        "stderr": _truncate_text(_redact(runtime.get("stderr") or error or "")),
        "environment": _env_summary(cfg),
        "fingerprint": server_fingerprint(cfg),
        "trusted": is_trusted_for_probe(cfg),
    }
    return _redact(diagnostics)


def _format_exception(error: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()

    def collect(exc: BaseException, depth: int) -> None:
        if id(exc) in seen:
            return
        seen.add(id(exc))
        message = str(exc).strip()
        name = type(exc).__name__
        parts.append(f"{name}: {message}" if message else name)
        nested = getattr(exc, "exceptions", None)
        if depth >= 4 or not isinstance(nested, tuple):
            return
        for child in nested[:8]:
            if isinstance(child, BaseException):
                collect(child, depth + 1)

    collect(error, 0)
    return " | ".join(parts)


def _exception_leaf_type_names(error: BaseException) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()

    def collect(exc: BaseException) -> None:
        if id(exc) in seen:
            return
        seen.add(id(exc))
        nested = getattr(exc, "exceptions", None)
        if isinstance(nested, tuple) and nested:
            for child in nested:
                if isinstance(child, BaseException):
                    collect(child)
            return
        names.append(type(exc).__name__)

    collect(error)
    return names


def _is_stdio_teardown_error(error: BaseException) -> bool:
    names = _exception_leaf_type_names(error)
    return bool(names) and all(name == "BrokenResourceError" for name in names)


def _mark_probe_failure(cfg: MCPServerConfig, error: Exception) -> None:
    state = _MCP_RUNTIME_STATE.setdefault(_runtime_key(cfg), {})
    state["last_exit_code"] = getattr(error, "returncode", None)
    formatted = _format_exception(error)
    if state.get("stderr"):
        state["last_error"] = formatted
    else:
        state["stderr"] = formatted


_EDITOR_PLACEHOLDER_RE = re.compile(r"\$\{(env:)?([A-Za-z_][A-Za-z0-9_]*|/)\}")


def _resolve_placeholders(value: Any, context: dict[str, str] | None = None) -> Any:
    ctx = context or {}
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(v, ctx) for v in value]
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            is_env, name = match.group(1), match.group(2)
            if not is_env and name in ctx:
                return ctx[name]
            resolved = os.environ.get(name, "")
            _remember_secret_value(resolved)
            return resolved
        return _EDITOR_PLACEHOLDER_RE.sub(replace, value)
    return value


def _placeholder_context(workspace_folder: str) -> dict[str, str]:
    folder = os.path.abspath(workspace_folder) if workspace_folder else ""
    return {
        "userHome": str(Path.home()),
        "workspaceFolder": folder,
        "workspaceFolderBasename": os.path.basename(folder) if folder else "",
        "pathSeparator": os.sep,
        "/": os.sep,
    }


def _strip_json_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_strip_json_comments(raw))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return values
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        if text.startswith("export "):
            text = text[len("export "):].strip()
        key, _sep, value = text.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
            _remember_secret_value(value)
    return values


def _app_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() or (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[2]


def global_mcp_config_path() -> Path:
    return Path.home() / ".livecode" / "mcp.json"


def _config_entries(project_path: str, workspace_payload: dict[str, Any] | None = None) -> list[MCPConfigEntry]:
    workspace = workspace_from_payload(project_path, workspace_payload)
    primary = workspace.primary_path
    home = Path.home()
    entries: list[MCPConfigEntry] = [
        MCPConfigEntry(global_mcp_config_path(), "user", workspace_folder=primary),
        MCPConfigEntry(home / ".claude.json", "user", workspace_folder=primary),
    ]
    if workspace.mcp_servers:
        entries.append(
            MCPConfigEntry(
                Path(workspace.config_path or workspace.primary_path),
                "workspace",
                inline_servers=workspace.mcp_servers,
                workspace_folder=primary,
            )
        )
    entries.append(MCPConfigEntry(_app_repo_root() / ".mcp.json", "workspace", workspace_folder=primary))
    for order, folder in enumerate(workspace.folders):
        base = Path(folder.path)
        for rel, key in ((".mcp.json", "mcpServers"), (".vscode/mcp.json", "servers")):
            entries.append(
                MCPConfigEntry(
                    base / rel,
                    "project",
                    folder_name=folder.name,
                    folder_order=order,
                    servers_key=key,
                    workspace_folder=folder.path,
                )
            )
    seen: set[Path] = set()
    unique: list[MCPConfigEntry] = []
    for entry in entries:
        resolved = entry.path.expanduser().resolve()
        if entry.inline_servers is None and resolved in seen:
            continue
        seen.add(resolved)
        unique.append(entry)
    return unique


def _resolve_server_cwd(value: str, config_path: Path) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    base = config_path if config_path.is_dir() else config_path.parent
    return os.path.abspath(str(base / expanded))


def _server_from_config(
    name: str,
    raw: dict[str, Any],
    source: str,
    config_path: Path,
    folder_name: str = "",
    folder_order: int = -1,
    workspace_folder: str = "",
) -> MCPServerConfig | None:
    resolved = _resolve_placeholders(raw, _placeholder_context(workspace_folder))
    transport = str(resolved.get("type") or resolved.get("transport") or "").strip().lower()
    url = str(resolved.get("url") or "").strip()
    command = str(resolved.get("command") or "").strip()
    if url and not transport:
        transport = "http"
    if command and not transport:
        transport = "stdio"
    if transport in ("streamable-http", "http"):
        if not url:
            return None
        transport = "http"
    elif transport == "sse":
        if not url:
            return None
    elif transport == "stdio":
        if not command:
            return None
    else:
        return None
    headers = resolved.get("headers") if isinstance(resolved.get("headers"), dict) else {}
    env = resolved.get("env") if isinstance(resolved.get("env"), dict) else {}
    env_file = str(resolved.get("envFile") or "").strip()
    if env_file and transport == "stdio":
        env_path = _resolve_server_cwd(env_file, config_path)
        env = {**_read_env_file(env_path), **env}
    args = resolved.get("args") if isinstance(resolved.get("args"), list) else []
    cwd = _resolve_server_cwd(str(resolved.get("cwd") or ""), config_path)
    return MCPServerConfig(
        name=name,
        transport=transport,
        source=source,
        url=url,
        command=command,
        args=tuple(str(a) for a in args),
        env=tuple((str(k), str(v)) for k, v in env.items()),
        headers=tuple((str(k), str(v)) for k, v in headers.items()),
        cwd=cwd,
        config_path=str(config_path),
        folder_name=folder_name,
        folder_order=folder_order,
        config_disabled=resolved.get("disabled") is True,
    )


def load_mcp_servers(project_path: str, workspace_payload: dict[str, Any] | None = None) -> list[MCPServerConfig]:
    servers: list[MCPServerConfig] = []
    seen: set[str] = set()
    for entry in _config_entries(project_path, workspace_payload):
        raw_servers = entry.inline_servers
        if raw_servers is None:
            data = _read_json_file(entry.path)
            raw_servers = data.get(entry.servers_key) if isinstance(data.get(entry.servers_key), dict) else {}
        for name, raw in raw_servers.items():
            if not isinstance(raw, dict):
                continue
            server_name = str(name).strip()
            if not server_name or server_name in seen:
                continue
            cfg = _server_from_config(
                server_name,
                raw,
                entry.source,
                entry.path,
                folder_name=entry.folder_name,
                folder_order=entry.folder_order,
                workspace_folder=entry.workspace_folder,
            )
            if cfg is None:
                continue
            servers.append(cfg)
            seen.add(server_name)
    return servers


def _find_pypi_mcp_site_packages() -> Path | None:
    search_roots = [Path(p) for p in site.getsitepackages()]
    user_site = site.getusersitepackages()
    if user_site:
        search_roots.append(Path(user_site))
    for base in search_roots:
        if (base / "mcp" / "client" / "session.py").is_file():
            return base
    return None


@contextlib.contextmanager
def _pypi_mcp_imports():
    with _MCP_IMPORT_LOCK:
        pypi_root = _find_pypi_mcp_site_packages()
        if pypi_root is None:
            raise ImportError("PyPI mcp package not found. Install with: pip install mcp")
        app_root = Path(__file__).resolve().parents[1]
        saved_path = sys.path.copy()
        saved_modules = {name: mod for name, mod in sys.modules.items() if name == "mcp" or name.startswith("mcp.")}
        try:
            for name in list(sys.modules):
                if name == "mcp" or name.startswith("mcp."):
                    local_file = getattr(sys.modules[name], "__file__", "") or ""
                    if local_file and Path(local_file).resolve().is_relative_to(app_root / "mcp"):
                        del sys.modules[name]
            filtered = [entry for entry in sys.path if Path(entry or ".").resolve() != app_root]
            root_str = str(pypi_root)
            if root_str not in filtered:
                filtered.insert(0, root_str)
            sys.path[:] = filtered
            yield
        finally:
            sys.path[:] = saved_path
            for name in list(sys.modules):
                if (name == "mcp" or name.startswith("mcp.")) and name not in saved_modules:
                    del sys.modules[name]
            sys.modules.update(saved_modules)


def _load_mcp_modules(transport: str) -> tuple[Any, Any, Any, Any]:
    with _pypi_mcp_imports():
        from mcp.client.session import ClientSession
        http_client = None
        other_client = None
        stdio_params = None
        if transport == "http":
            http_client = _streamable_http_client_compat()
        elif transport == "sse":
            from mcp.client.sse import sse_client
            other_client = sse_client
        elif transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client
            other_client = stdio_client
            stdio_params = StdioServerParameters
        return ClientSession, http_client, other_client, stdio_params


def _streamable_http_client_compat() -> Any:
    from mcp.client import streamable_http

    legacy = getattr(streamable_http, "streamablehttp_client", None)
    if legacy is not None:
        return legacy
    modern = streamable_http.streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    @contextlib.asynccontextmanager
    async def _client(url: str, headers: dict[str, str] | None = None):
        async with create_mcp_http_client(headers=headers) as http:
            async with modern(url, http_client=http) as streams:
                yield streams[0], streams[1], None

    return _client


def _structured_tool_content(result: Any) -> tuple[bool, Any]:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    return structured is not None, structured


def _bounded_result_text(value: Any) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:4000]


def _parse_tool_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {"ok": False, "error": "Empty MCP tool result"}
    parts: list[str] = []
    content = getattr(result, "content", None)
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, (list, tuple)):
        for block in content:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
    raw = "\n".join(parts).strip()
    has_structured, structured = _structured_tool_content(result)
    if bool(getattr(result, "isError", False)):
        if raw:
            return {"ok": False, "error": raw}
        if has_structured:
            return {"ok": False, "error": _bounded_result_text(structured) or "MCP tool returned an error"}
        return {"ok": False, "error": "MCP tool returned an error"}
    if has_structured:
        if isinstance(structured, dict) and structured.get("ok") is False:
            return structured
        return {"ok": True, "data": structured}
    if not raw:
        return {"ok": True, "data": {}}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"result": raw}
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        return parsed
    return {"ok": True, "data": parsed}


@contextlib.asynccontextmanager
async def _client_stream(cfg: MCPServerConfig):
    ClientSession, streamablehttp_client, other_client, stdio_params = _load_mcp_modules(cfg.transport)
    if cfg.transport == "http":
        headers = dict(cfg.headers)
        async with streamablehttp_client(cfg.url, headers=headers or None) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return
    if cfg.transport == "sse":
        headers = dict(cfg.headers)
        async with other_client(cfg.url, headers=headers or None) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return
    if cfg.transport == "stdio":
        if other_client is None or stdio_params is None:
            raise ValueError("MCP stdio transport unavailable")
        cwd = _effective_stdio_cwd(cfg)
        kwargs = {"command": _resolved_stdio_command(cfg), "args": list(cfg.args), "env": _effective_stdio_env(cfg)}
        try:
            supports_cwd = "cwd" in inspect.signature(stdio_params).parameters
        except (TypeError, ValueError):
            supports_cwd = True
        if cwd and not supports_cwd:
            raise RuntimeError("Configured MCP stdio cwd requires mcp>=1.10.0")
        if supports_cwd:
            kwargs["cwd"] = cwd or None
        state = _MCP_RUNTIME_STATE.setdefault(_runtime_key(cfg), {})
        state["working_directory"] = cwd or os.getcwd()
        params = stdio_params(**kwargs)
        with tempfile.TemporaryFile(mode="w+b") as errlog:
            try:
                async with other_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            finally:
                captured = _read_bounded_stderr_file(errlog).strip()
                if captured:
                    state = _MCP_RUNTIME_STATE.setdefault(_runtime_key(cfg), {})
                    state["stderr"] = captured
        return
    raise ValueError(f"Unsupported MCP transport: {cfg.transport}")


def _protocol_model_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump(exclude_none=True)
        return data if isinstance(data, dict) else {}
    legacy_dump = getattr(value, "dict", None)
    if callable(legacy_dump):
        data = legacy_dump(exclude_none=True)
        return data if isinstance(data, dict) else {}
    return {}


def _safe_tool_annotations(value: Any) -> dict[str, Any]:
    raw = _protocol_model_dict(value)
    allowed = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    return {key: raw[key] for key in allowed if key in raw and isinstance(raw[key], bool)}


async def _list_server_tools(cfg: MCPServerConfig) -> list[dict[str, Any]]:
    if not is_trusted_for_probe(cfg):
        raise PermissionError("MCP server requires explicit trust before probing")
    tools: list[dict[str, Any]] | None = None
    try:
        async with _client_stream(cfg) as session:
            response = await asyncio.wait_for(session.list_tools(), timeout=_mcp_operation_timeout("list_tools"))
            tools = []
            for tool in response.tools or []:
                tools.append({
                    "name": str(getattr(tool, "name", "")),
                    "description": str(getattr(tool, "description", "") or ""),
                    "inputSchema": _protocol_model_dict(getattr(tool, "inputSchema", None)) or {},
                    "annotations": _safe_tool_annotations(getattr(tool, "annotations", None)),
                })
    except Exception as exc:
        if tools is None or not _is_stdio_teardown_error(exc):
            raise
    return [tool for tool in tools if tool["name"]]


async def _call_server_tool(cfg: MCPServerConfig, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not is_trusted_for_probe(cfg):
        raise PermissionError("MCP stdio server requires explicit trust before execution")
    parsed: dict[str, Any] | None = None
    try:
        async with _client_stream(cfg) as session:
            result = await asyncio.wait_for(session.call_tool(tool_name, arguments=arguments or {}), timeout=_mcp_operation_timeout("call_tool"))
            parsed = _parse_tool_result(result)
    except Exception as exc:
        if parsed is None or not _is_stdio_teardown_error(exc):
            raise
    return parsed


def _mcp_cache_ttl_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("LIVECODE_MCP_CACHE_TTL_SECONDS", "60")))
    except ValueError:
        return 60.0


def _server_tool_cache_key(cfg: MCPServerConfig) -> tuple[str, str]:
    return (_runtime_key(cfg), str(hash(cfg)))


def _cached_server_tool_snapshot(cfg: MCPServerConfig) -> list[dict[str, Any]]:
    key = _server_tool_cache_key(cfg)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _SERVER_TOOL_CACHE.get(key)
        if cached is None:
            return []
        ts, tools = cached
        if now - ts > _mcp_cache_ttl_seconds():
            return []
        return copy.deepcopy(tools)


def _cached_server_tools(cfg: MCPServerConfig) -> list[dict[str, Any]]:
    key = _server_tool_cache_key(cfg)
    with _CACHE_LOCK:
        server_lock = _SERVER_TOOL_LOCKS.setdefault(key, threading.Lock())
    with server_lock:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _SERVER_TOOL_CACHE.get(key)
            if cached is not None:
                ts, tools = cached
                if now - ts <= _mcp_cache_ttl_seconds():
                    return copy.deepcopy(tools)
                _SERVER_TOOL_CACHE.pop(key, None)
        tools = _run_async(_list_server_tools(cfg))
        with _CACHE_LOCK:
            _SERVER_TOOL_CACHE[key] = (time.monotonic(), copy.deepcopy(tools))
        return copy.deepcopy(tools)


_DEFAULT_MCP_TIMEOUTS = {"list_tools": 30.0, "call_tool": 60.0}
_DEFAULT_MCP_TIMEOUT = 30.0


def _mcp_timeout(operation: str = "") -> float:
    default = _DEFAULT_MCP_TIMEOUTS.get(operation, _DEFAULT_MCP_TIMEOUT)
    env_name = f"LIVECODE_MCP_{operation.upper()}_TIMEOUT" if operation else "LIVECODE_MCP_TIMEOUT"
    raw = os.environ.get(env_name, os.environ.get("LIVECODE_MCP_TIMEOUT", ""))
    try:
        return max(1.0, float(raw)) if raw else default
    except ValueError:
        return default


def _mcp_operation_timeout(operation: str = "") -> float:
    return _mcp_timeout(operation)


async def _bounded_coro(coro, operation: str = ""):
    return await asyncio.wait_for(coro, timeout=_mcp_operation_timeout(operation))


def _run_async(coro, operation: str = ""):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_bounded_coro(coro, operation))
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _bounded_coro(coro, operation)).result(timeout=_mcp_operation_timeout(operation) + 1.0)


def _servers_by_name(project_path: str, workspace_payload: dict[str, Any] | None = None) -> dict[str, MCPServerConfig]:
    return {cfg.name: cfg for cfg in load_mcp_servers(project_path, workspace_payload)}


def _filtered_servers(
    project_path: str,
    enabled_servers: list[str] | tuple[str, ...] | None,
    workspace_payload: dict[str, Any] | None = None,
    *,
    include_disabled: bool = False,
) -> list[MCPServerConfig]:
    servers = load_mcp_servers(project_path, workspace_payload)
    if not include_disabled:
        servers = [cfg for cfg in servers if not cfg.config_disabled]
    if enabled_servers is None:
        return servers
    allowed = {str(s) for s in enabled_servers if str(s).strip()}
    if not allowed:
        return []
    return [cfg for cfg in servers if cfg.name in allowed]


def openai_schema_and_binding_for_mcp_tool(server_name: str, tool: Any) -> tuple[dict[str, Any], MCPToolBinding]:
    name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")
    description = tool.get("description") if isinstance(tool, dict) else getattr(tool, "description", "")
    schema = tool.get("inputSchema") if isinstance(tool, dict) else getattr(tool, "inputSchema", None)
    annotations = tool.get("annotations") if isinstance(tool, dict) else getattr(tool, "annotations", None)
    schema = _protocol_model_dict(schema)
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
    safe_annotations = _safe_tool_annotations(annotations)
    encoded_name = encode_mcp_tool_name(server_name, str(name))
    _TOOL_ANNOTATION_CACHE[encoded_name] = dict(safe_annotations)
    schema_payload = {
        "type": "function",
        "function": {
            "name": encoded_name,
            "description": f"MCP {server_name}/{name}: {str(description or '')}"[:1024],
            "parameters": schema,
        },
    }
    binding = MCPToolBinding(encoded_name, server_name, str(name), dict(safe_annotations))
    return schema_payload, binding


def openai_schema_for_mcp_tool(server_name: str, tool: Any) -> dict[str, Any]:
    schema, _binding = openai_schema_and_binding_for_mcp_tool(server_name, tool)
    return schema


def _allowed_mcp_tool_names(
    server_name: str,
    tool_allowlist_by_server: dict[str, set[str] | frozenset[str] | tuple[str, ...]] | None,
) -> set[str] | frozenset[str] | tuple[str, ...] | None:
    if not tool_allowlist_by_server:
        return None
    return tool_allowlist_by_server.get(server_name)


def _mcp_tool_name(tool: Any) -> str:
    name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")
    return str(name or "")


def _copy_mcp_bindings(bindings: dict[str, MCPToolBinding]) -> dict[str, MCPToolBinding]:
    return {name: MCPToolBinding(binding.encoded_name, binding.server_name, binding.tool_name, dict(binding.annotations)) for name, binding in bindings.items()}


def list_mcp_tools_with_bindings(
    project_path: str,
    enabled_servers: list[str] | tuple[str, ...] | None = None,
    limit: int | None = None,
    tool_allowlist_by_server: dict[str, set[str] | frozenset[str] | tuple[str, ...]] | None = None,
    workspace_payload: dict[str, Any] | None = None,
    tool_denylist_by_server: dict[str, set[str] | frozenset[str] | tuple[str, ...] | list[str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, MCPToolBinding]]:
    safe_limit = max(0, limit) if limit is not None else None
    key = (
        _workspace_key(project_path, workspace_payload),
        _enabled_key(enabled_servers),
        _allowlist_key(tool_allowlist_by_server),
        safe_limit,
        _allowlist_key(tool_denylist_by_server),
    )
    with _CACHE_LOCK:
        cached = _TOOL_CACHE.get(key)
        if cached is not None:
            cached_schemas, cached_bindings = cached
            return copy.deepcopy(cached_schemas), _copy_mcp_bindings(cached_bindings)
    schemas: list[dict[str, Any]] = []
    bindings: dict[str, MCPToolBinding] = {}
    for cfg in _filtered_servers(project_path, enabled_servers, workspace_payload):
        if safe_limit is not None and len(schemas) >= safe_limit:
            break
        allowed_names = _allowed_mcp_tool_names(cfg.name, tool_allowlist_by_server)
        denied_names = set((tool_denylist_by_server or {}).get(cfg.name) or ())
        try:
            for tool in _cached_server_tools(cfg):
                tool_name = _mcp_tool_name(tool)
                if allowed_names is not None and tool_name not in allowed_names:
                    continue
                if tool_name in denied_names:
                    continue
                if safe_limit is not None and len(schemas) >= safe_limit:
                    break
                schema, binding = openai_schema_and_binding_for_mcp_tool(cfg.name, tool)
                schemas.append(schema)
                bindings[binding.encoded_name] = binding
        except Exception:
            continue
    with _CACHE_LOCK:
        _TOOL_CACHE[key] = (copy.deepcopy(schemas), _copy_mcp_bindings(bindings))
    return copy.deepcopy(schemas), _copy_mcp_bindings(bindings)


def list_mcp_tools(
    project_path: str,
    enabled_servers: list[str] | tuple[str, ...] | None = None,
    limit: int | None = None,
    tool_allowlist_by_server: dict[str, set[str] | frozenset[str] | tuple[str, ...]] | None = None,
    workspace_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    schemas, _bindings = list_mcp_tools_with_bindings(
        project_path,
        enabled_servers,
        limit=limit,
        tool_allowlist_by_server=tool_allowlist_by_server,
        workspace_payload=workspace_payload,
    )
    return schemas


_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _display_command_line(cfg: MCPServerConfig) -> str:
    parts = [cfg.command]
    hide_next = False
    for arg in cfg.args:
        if hide_next:
            parts.append("••••")
            hide_next = False
            continue
        flag, sep, value = arg.partition("=")
        if arg.startswith("-") and _contains_secret_key(flag):
            if sep:
                parts.append(f"{flag}=••••")
            else:
                parts.append(arg)
                hide_next = True
            continue
        parts.append(arg)
    return str(_redact(" ".join(_quote_arg(p) for p in parts)))


def _quote_arg(value: str) -> str:
    return value if value == "••••" or value.endswith("=••••") else shlex.quote(value)


def _managed_config_paths(project_path: str, workspace_payload: dict[str, Any] | None = None) -> dict[str, Path]:
    try:
        workspace = workspace_from_payload(project_path, workspace_payload)
        primary = Path(workspace.primary_path)
    except ValueError:
        primary = Path(os.path.abspath(os.path.expanduser(project_path or "")))
    return {"project": primary / ".mcp.json", "global": global_mcp_config_path()}


def _is_managed_config(project_path: str, config_path: str, workspace_payload: dict[str, Any] | None = None) -> bool:
    if not config_path:
        return False
    target = Path(config_path).expanduser().resolve()
    return any(target == p.expanduser().resolve() for p in _managed_config_paths(project_path, workspace_payload).values())


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    from livecode.search_replace import write_text_atomic

    private = path.expanduser().resolve() == global_mcp_config_path().expanduser().resolve()
    write_text_atomic(str(path), json.dumps(data, indent=2) + "\n", new_file_mode=0o600 if private else None)


def _read_json_for_update(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"Could not read {path}: {exc}"
    if not raw.strip():
        return {}, ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            json.loads(_strip_json_comments(raw))
        except json.JSONDecodeError:
            return {}, f"{path} is not valid JSON; fix it first."
        return {}, f"{path} has comments or trailing commas; edit it by hand so they are kept."
    if not isinstance(data, dict):
        return {}, f"{path} does not hold a JSON object."
    return data, ""


def _clean_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k).strip(): str(v) for k, v in value.items() if str(k).strip()}


def add_mcp_server(
    project_path: str,
    name: str,
    spec: dict[str, Any],
    *,
    scope: str = "project",
    workspace_payload: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    server_name = str(name or "").strip()
    if not _SERVER_NAME_RE.fullmatch(server_name):
        return {"success": False, "error": "Use a name of letters, digits, '.', '_' or '-' (up to 64 characters)."}
    transport = str(spec.get("type") or "stdio").strip().lower()
    entry: dict[str, Any] = {"type": transport}
    if transport == "stdio":
        command = str(spec.get("command") or "").strip()
        if not command:
            return {"success": False, "error": "A stdio server needs a command."}
        entry["command"] = command
        args = spec.get("args")
        if isinstance(args, str):
            try:
                args = shlex.split(args)
            except ValueError as exc:
                return {"success": False, "error": f"Could not parse the arguments: {exc}"}
        if isinstance(args, list) and args:
            entry["args"] = [str(a) for a in args]
        env = _clean_str_map(spec.get("env"))
        if env:
            entry["env"] = env
    elif transport in ("http", "sse"):
        url = str(spec.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return {"success": False, "error": "The URL must start with http:// or https://."}
        entry["url"] = url
        headers = _clean_str_map(spec.get("headers"))
        if headers:
            entry["headers"] = headers
    else:
        return {"success": False, "error": "Type must be stdio, http, or sse."}
    paths = _managed_config_paths(project_path, workspace_payload)
    if scope not in paths:
        return {"success": False, "error": "Scope must be project or global."}
    path = paths[scope]
    data, error = _read_json_for_update(path)
    if error:
        return {"success": False, "error": error, "config_path": str(path)}
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    if server_name in servers and not overwrite:
        return {"success": False, "error": f"A server named {server_name} is already in {path}."}
    servers[server_name] = entry
    data["mcpServers"] = servers
    try:
        _write_json_atomic(path, data)
    except OSError as exc:
        return {"success": False, "error": f"Could not write {path}: {exc}"}
    refresh_mcp_cache()
    return {"success": True, "name": server_name, "path": str(path), "scope": scope}


def remove_mcp_server(
    project_path: str,
    name: str,
    workspace_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    server_name = str(name or "").strip()
    cfg = _servers_by_name(project_path, workspace_payload).get(server_name)
    if cfg is None or cfg.builtin:
        return {"success": False, "error": f"MCP server not configured: {server_name}"}
    if not _is_managed_config(project_path, cfg.config_path, workspace_payload):
        return {"success": False, "error": f"{server_name} comes from {cfg.config_path}; edit that file to remove it.", "config_path": cfg.config_path}
    path = Path(cfg.config_path)
    data, error = _read_json_for_update(path)
    if error:
        return {"success": False, "error": error, "config_path": str(path)}
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    if server_name not in servers:
        return {"success": False, "error": f"{server_name} is not in {path}."}
    del servers[server_name]
    data["mcpServers"] = servers
    try:
        _write_json_atomic(path, data)
    except OSError as exc:
        return {"success": False, "error": f"Could not write {path}: {exc}"}
    refresh_mcp_cache()
    return {"success": True, "name": server_name, "path": str(path)}


def ensure_project_mcp_config(project_path: str) -> str:
    project = Path(os.path.abspath(os.path.expanduser(project_path or "")))
    if not project.is_dir():
        raise ValueError(f"Project path not found: {project_path}")
    path = project / ".mcp.json"
    if not path.exists():
        path.write_text(json.dumps({"mcpServers": {}}, indent=2) + "\n", encoding="utf-8")
    return str(path)


def call_mcp_tool(
    project_path: str,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    workspace_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _servers_by_name(project_path, workspace_payload).get(server_name)
    if cfg is None:
        return {"ok": False, "error": f"MCP server not configured: {server_name}"}
    if not is_trusted_for_probe(cfg):
        return {"ok": False, "server": server_name, "tool": tool_name, "error": "MCP server requires explicit trust before tool execution"}
    try:
        result = _run_async(_call_server_tool(cfg, tool_name, arguments or {}))
        result.setdefault("server", server_name)
        result.setdefault("tool", tool_name)
        return result
    except Exception as exc:
        return {"ok": False, "server": server_name, "tool": tool_name, "error": _format_exception(exc)}


def get_mcp_status(
    project_path: str,
    enabled_servers: list[str] | tuple[str, ...] | None = None,
    workspace_payload: dict[str, Any] | None = None,
    probe_servers: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    probe_key = _enabled_key(enabled_servers if probe_servers is None else probe_servers)
    key = (_workspace_key(project_path, workspace_payload), _enabled_key(enabled_servers), probe_key)
    with _CACHE_LOCK:
        cached = _STATUS_CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    items: list[dict[str, Any]] = []
    probe_allowed = None if probe_key is None else set(probe_key)
    for cfg in _filtered_servers(project_path, enabled_servers, workspace_payload, include_disabled=True):
        selected = probe_allowed is None or cfg.name in probe_allowed
        trusted = is_trusted_for_probe(cfg)
        item: dict[str, Any] = {
            "name": cfg.name,
            "transport": cfg.transport,
            "source": cfg.source,
            "builtin": cfg.builtin,
            "url": _sanitize_url(cfg.url) if cfg.transport != "stdio" else "",
            "command": cfg.command if cfg.transport == "stdio" else "",
            "command_line": _display_command_line(cfg) if cfg.transport == "stdio" else "",
            "config_path": cfg.config_path,
            "editable": _is_managed_config(project_path, cfg.config_path, workspace_payload),
            "folder_name": cfg.folder_name,
            "folder_order": cfg.folder_order,
            "connected": False,
            "selected": selected,
            "disabled": not selected,
            "config_disabled": cfg.config_disabled,
            "tool_count": 0,
            "tools": [],
            "trusted": trusted,
            "requires_trust": not cfg.builtin and not trusted,
            "fingerprint": server_fingerprint(cfg),
        }
        if cfg.config_disabled:
            item["diagnostics"] = _server_diagnostics(cfg, "")
            items.append(_redact(item))
            continue
        if not selected:
            tools = _cached_server_tool_snapshot(cfg)
            if not tools and cfg.builtin and trusted:
                try:
                    tools = _cached_server_tools(cfg)
                except Exception:
                    tools = []
            item["tool_count"] = len(tools)
            item["tools"] = [
                {
                    "name": str(tool.get("name", "")),
                    "description": str(tool.get("description", "") or ""),
                }
                for tool in tools
                if tool.get("name")
            ]
            item["diagnostics"] = _server_diagnostics(cfg, "")
            items.append(_redact(item))
            continue
        if not trusted:
            item["error"] = "MCP server requires explicit trust before probing"
            item["diagnostics"] = _server_diagnostics(cfg, str(item.get("error") or ""))
            items.append(_redact(item))
            continue
        try:
            tools = _cached_server_tools(cfg)
            item["connected"] = True
            _MCP_RUNTIME_STATE.setdefault(_runtime_key(cfg), {}).pop("last_error", None)
            item["tool_count"] = len(tools)
            item["tools"] = [
                {
                    "name": str(tool.get("name", "")),
                    "description": str(tool.get("description", "") or ""),
                }
                for tool in tools
                if tool.get("name")
            ]
            domains = sorted({str(t.get("name", "")).split("_", 1)[0] for t in tools if t.get("name")})
            item["domains"] = domains[:20]
        except Exception as exc:
            _mark_probe_failure(cfg, exc)
            item["error"] = str(_redact(_format_exception(exc)))
        item["diagnostics"] = _server_diagnostics(cfg, str(item.get("error") or ""))
        items.append(_redact(item))
    status = {"servers": items, "server_count": len(items)}
    with _CACHE_LOCK:
        _STATUS_CACHE[key] = copy.deepcopy(status)
    return copy.deepcopy(status)


def connected_mcp_context(
    project_path: str,
    enabled_servers: list[str] | tuple[str, ...] | None = None,
    workspace_payload: dict[str, Any] | None = None,
    disabled_tools: dict[str, list[str]] | None = None,
) -> str:
    key = (_workspace_key(project_path, workspace_payload), _enabled_key(enabled_servers), _allowlist_key(disabled_tools))
    with _CACHE_LOCK:
        cached = _CONTEXT_CACHE.get(key)
    if cached:
        return cached
    status = get_mcp_status(project_path, enabled_servers, workspace_payload, probe_servers=enabled_servers)
    connected = [s for s in status.get("servers", []) if s.get("connected")]
    if not connected:
        return ""
    lines = ["MCP servers connected this turn:"]
    for server in connected[:8]:
        off = set((disabled_tools or {}).get(str(server.get("name"))) or ())
        names = [str(t.get("name")) for t in (server.get("tools") or []) if t.get("name") and str(t.get("name")) not in off]
        lines.append(f"- {server.get('name')} ({len(names)} tools)")
        groups: dict[str, list[str]] = {}
        for name in names:
            prefix, _sep, rest = name.partition("_")
            groups.setdefault(prefix if rest else "", []).append(rest or name)
        for prefix, rests in groups.items():
            body = ", ".join(rests[:30]) + (", ..." if len(rests) > 30 else "")
            lines.append(f"  {prefix}_{{{body}}}" if prefix else f"  {body}")
    lines.append(
        "When a request concerns an external system these servers cover (tickets, issues, cloud, data), "
        "call the matching MCP tool directly (or call_mcp_tool with server_name and tool_name). "
        "If no connected MCP tool can do it, or the call fails, tell the user it was not found in MCP "
        "and stop; do not search the repo for integration code or guess."
    )
    context = "\n".join(lines)
    with _CACHE_LOCK:
        _CONTEXT_CACHE[key] = context
    return context


def refresh_mcp_cache(project_path: str | None = None, workspace_payload: dict[str, Any] | None = None) -> None:
    with _CACHE_LOCK:
        if not project_path:
            _STATUS_CACHE.clear()
            _TOOL_CACHE.clear()
            _SERVER_TOOL_CACHE.clear()
            _SERVER_TOOL_LOCKS.clear()
            _CONTEXT_CACHE.clear()
            return
        prefix = _workspace_key(project_path, workspace_payload)
        for cache in (_STATUS_CACHE, _TOOL_CACHE, _CONTEXT_CACHE):
            for key in list(cache):
                if key[0] == prefix:
                    del cache[key]
        for cfg in _filtered_servers(project_path, None, workspace_payload):
            for key in list(_SERVER_TOOL_CACHE):
                if key[0] == _runtime_key(cfg):
                    del _SERVER_TOOL_CACHE[key]


def get_mcp_server_diagnostics(project_path: str, server_name: str | None = None, workspace_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    enabled_servers = [server_name] if server_name else None
    status = get_mcp_status(project_path, enabled_servers=enabled_servers, workspace_payload=workspace_payload)
    servers = status.get("servers") if isinstance(status, dict) else []
    if server_name:
        for server in servers or []:
            if server.get("name") == server_name:
                return {"success": True, "server": server}
        return {"success": False, "error": "MCP server not configured: " + server_name}
    return {"success": True, "servers": servers or []}





def lifecycle_mcp_servers(
    project_path: str,
    action: str,
    server_name: str | None = None,
    enabled_servers: list[str] | tuple[str, ...] | None = None,
    workspace_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_name = str(action or "refresh").strip().lower()
    target = str(server_name or "").strip()
    if action_name == "trust":
        if not target:
            return {"success": False, "supported": True, "action": action_name, "error": "server_name required"}
        trusted = trust_mcp_server(project_path, target, workspace_payload)
        if not trusted.get("success"):
            return {"supported": True, "action": action_name, **trusted}
        status = get_mcp_status(
            project_path,
            enabled_servers=None,
            workspace_payload=workspace_payload,
            probe_servers=[target],
        )
        return {"success": True, "supported": True, "action": action_name, "server_name": target, "status": status}
    if action_name == "stop":
        return {
            "success": False,
            "supported": False,
            "action": action_name,
            "error": "Stop is unsupported because LiveCode probes MCP stdio servers transiently and does not own a persistent process registry.",
        }
    if action_name not in {"start", "restart", "reload", "reload_config", "refresh", "probe"}:
        return {"success": False, "supported": False, "action": action_name, "error": "Unsupported MCP action: " + action_name}
    refresh_mcp_cache(project_path, workspace_payload)
    selected = [target] if target else enabled_servers
    status = get_mcp_status(
        project_path,
        enabled_servers=None,
        workspace_payload=workspace_payload,
        probe_servers=selected,
    )
    servers = status.get("servers") if isinstance(status, dict) else []
    if target and not any(server.get("name") == target for server in servers):
        return {"success": False, "supported": True, "action": action_name, "error": "MCP server not configured: " + target, "status": status}
    return {"success": True, "supported": True, "action": action_name, "status": status}
