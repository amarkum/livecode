from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any, Callable, Literal

PermissionDecision = Literal["approved", "denied", "expired", "missing", "cancelled"]
_POLL_S = 0.5

_PERMISSIONS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_DEFAULT_TIMEOUT_S = 120

SENSITIVE_TOOLS = frozenset({"write_file", "edit_file", "multi_edit", "run_command"})

_DESTRUCTIVE_COMMAND_PATTERNS = [
    # Git
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+.*-[a-z]*f"),
    re.compile(r"\bgit\s+checkout\s+.*--\s+\S"),
    re.compile(r"\bgit\s+push\s+.*--force\b"),
    re.compile(r"\bgit\s+push\s+.*\s+-f\b"),
    re.compile(r"\bgit\s+branch\s+.*-D\b"),

    # Filesystem
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\b|\brm\s+-[a-z]*f[a-z]*r[a-z]*\b"),
]

def is_destructive_command(command: str) -> bool:
    if not command:
        return False
    return any(p.search(command) for p in _DESTRUCTIVE_COMMAND_PATTERNS)

def create_permission_request(
    session_id: str,
    tool_name: str,
    tool_args: dict,
    *,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> str:
    request_id = f"perm_{uuid.uuid4().hex[:16]}"
    event = threading.Event()
    with _LOCK:
        _PERMISSIONS[request_id] = {
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "event": event,
            "approved": None,
            "decision": None,
            "created_at": time.time(),
            "timeout_s": timeout_s,
        }
    return request_id

def resolve_permission(request_id: str, approved: bool) -> bool:
    with _LOCK:
        entry = _PERMISSIONS.get(request_id)
        if not entry:
            return False
        if entry.get("decision") is not None:
            return False
        entry["approved"] = approved
        entry["decision"] = "approved" if approved else "denied"
        entry["event"].set()
        return True

def wait_for_permission_result(request_id: str, cancel_check: Callable[[], bool] | None = None) -> PermissionDecision:
    """Waits for the user's decision; with cancel_check, pressing Stop ends the wait as "cancelled"."""
    with _LOCK:
        entry = _PERMISSIONS.get(request_id)
        if not entry:
            return "missing"
        event = entry["event"]
        raw_timeout = entry.get("timeout_s")
        timeout_s = _DEFAULT_TIMEOUT_S if raw_timeout is None else max(0.0, float(raw_timeout))
    deadline = time.monotonic() + timeout_s
    cancelled = False
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if cancel_check is not None:
            try:
                cancelled = bool(cancel_check())
            except Exception:
                cancelled = False
            if cancelled:
                break
        event.wait(timeout=min(_POLL_S, remaining))
    with _LOCK:
        entry = _PERMISSIONS.get(request_id)
        if not entry:
            return "missing"
        decision = entry.get("decision")
        if decision is None:
            decision = "cancelled" if cancelled else "expired"
            entry["decision"] = decision
        _PERMISSIONS.pop(request_id, None)
    return decision if decision in {"approved", "denied", "expired", "cancelled"} else "missing"


def wait_for_permission(request_id: str) -> bool | None:
    result = wait_for_permission_result(request_id)
    if result in {"expired", "missing", "cancelled"}:
        return None
    return result == "approved"

def cleanup_stale_permissions(max_age_s: int = 300) -> None:
    cutoff = time.time() - max_age_s
    with _LOCK:
        stale = [rid for rid, e in _PERMISSIONS.items() if e.get("created_at", 0) < cutoff]
        for rid in stale:
            entry = _PERMISSIONS.get(rid)
            if entry and entry.get("event") and entry.get("decision") is None:
                entry["decision"] = "expired"
                entry["event"].set()
