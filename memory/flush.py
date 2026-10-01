from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from livecode.memory.autosave import extract_real_user_queries
from livecode.memory.index import embed_missing_chunks, reindex_file
from livecode.memory.storage import (
    _skip_workspace_write,
    memory_lock,
    memory_root,
    slugify,
    write_session_log,
    write_text_atomic,
)

MAX_FLUSH_WRITE_CHARS = 8000
MAX_FLUSH_INPUT_CHARS = 24_000
_ROLE_CHAR_CAPS = {"user": 4000, "assistant": 4000, "tool": 1200}

FLUSH_MIN_NEW_QUERIES = 2
FLUSH_MIN_NEW_CHARS = 1500
FLUSH_MIN_INTERVAL_S = 300.0
FLUSH_CONTEXT_OVERLAP = 4
_FLUSH_STATE_NAME = ".flush_state.json"
_FLUSH_STATE_MAX_AGE_S = 60 * 86400

_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

FLUSH_SYSTEM_PROMPT = (
    "You are a memory assistant. Extract ALL useful information from this conversation "
    "that would help you be more effective in future sessions with this user. "
    "Write a concise markdown summary with ## headers covering:\n\n"
    "- **Decisions & rationale** — what was chosen and why\n"
    "- **Technical context** — architecture, APIs, patterns, tools, file paths discussed\n"
    "- **Debugging techniques & tools** — external APIs, CLI commands, query patterns, "
    "investigation workflows, or services discovered or used during debugging\n"
    "- **Problems & solutions** — bugs found, how they were fixed, workarounds\n\n"
    "Omit any section where there is nothing substantive to report. "
    "Do NOT include user preferences like OS, shell, or editor — these belong in global memory. "
    "Do NOT include an ephemeral progress section — transient status is not useful for future sessions.\n\n"
    "Respond with NO_REPLY if nothing genuinely useful was learned — a routine task "
    "that followed standard patterns, brief Q&A, or sessions with no novel decisions "
    "or discoveries are not worth persisting. Only write content that a future session "
    "would concretely benefit from."
)

def has_markdown_headers(text: str) -> bool:
    return any(line.startswith("## ") for line in (text or "").splitlines())

def is_no_reply(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    upper = stripped.upper()
    return upper == "NO_REPLY" or upper.startswith("NO_REPLY\n")

def process_flush_response(raw: str, *, max_chars: int = MAX_FLUSH_WRITE_CHARS) -> str | None:
    text = (raw or "").strip()
    if is_no_reply(text):
        return None
    if not has_markdown_headers(text):
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n"
    return text

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _flush_state_path(project_path: str, *, create: bool) -> str:
    return os.path.join(memory_root(project_path, create=create), _FLUSH_STATE_NAME)


def _load_flush_state(project_path: str) -> dict[str, Any]:
    try:
        with open(_flush_state_path(project_path, create=False), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_flush(project_path: str, session_id: str, count: int, digest: str | None = None) -> None:
    if not session_id or _skip_workspace_write(project_path):
        return
    with memory_lock(project_path):
        state = _load_flush_state(project_path)
        now = time.time()
        state = {
            sid: entry for sid, entry in state.items()
            if isinstance(entry, dict) and now - float(entry.get("at") or 0) < _FLUSH_STATE_MAX_AGE_S
        }
        entry = dict(state.get(session_id) or {})
        entry.update({"count": int(count), "at": now})
        if digest:
            entry["hash"] = digest
        state[session_id] = entry
        try:
            write_text_atomic(_flush_state_path(project_path, create=True), json.dumps(state))
        except OSError:
            pass


def run_memory_flush(
    project_path: str,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    model: str,
    call_summarize: Callable[[str, list[dict[str, str]]], str],
    last_flush_hash: str | None = None,
    since: int = 0,
) -> dict[str, Any]:
    since = max(0, min(int(since or 0), len(messages or [])))
    window = _flush_window((messages or [])[max(0, since - FLUSH_CONTEXT_OVERLAP):])
    if len(window) < 2:
        return {"status": "skipped", "reason": "too_short"}
    conversation = _format_conversation_for_flush(window)
    if since:
        conversation = (
            "Earlier parts of this conversation were already saved. Record only what is new "
            "in the part below; the first messages are repeated for context.\n\n" + conversation
        )
    prompt_messages: list[dict[str, str]] = [
        {"role": "system", "content": FLUSH_SYSTEM_PROMPT},
        {"role": "user", "content": conversation},
    ]
    try:
        raw = call_summarize(model, prompt_messages) or ""
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
    if last_flush_hash is None:
        last_flush_hash = (_load_flush_state(project_path).get(session_id) or {}).get("hash")
    accepted = process_flush_response(raw)
    if not accepted:
        _record_flush(project_path, session_id, len(messages))
        return {"status": "skipped", "reason": "quality_gate"}
    h = _content_hash(accepted)
    if last_flush_hash and h == last_flush_hash:
        _record_flush(project_path, session_id, len(messages))
        return {"status": "skipped", "reason": "duplicate"}

    real = extract_real_user_queries(messages)
    topic = slugify(real[0], 30) if real else "flush"
    topic = topic or "flush"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = write_session_log(
        project_path,
        date=date,
        topic_slug=topic,
        session_id=session_id,
        content=accepted,
        append=True,
    )
    if path:
        reindex_file(project_path, path, "session")
        embed_missing_chunks(project_path)
    _record_flush(project_path, session_id, len(messages), h)
    return {"status": "written", "path": path, "hash": h, "chars": len(accepted)}


def maybe_flush_session(
    project_path: str,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    model: str,
    call_summarize: Callable[[str, list[dict[str, str]]], str] | None,
    force: bool = False,
) -> dict[str, Any] | None:
    if not call_summarize or not session_id or not project_path:
        return None
    key = f"{os.path.realpath(memory_root(project_path, create=False))}:{session_id}"
    with _RUNNING_LOCK:
        if key in _RUNNING:
            return {"status": "skipped", "reason": "running"}
        _RUNNING.add(key)
    try:
        state = _load_flush_state(project_path).get(session_id) or {}
        since = int(state.get("count") or 0)
        if since > len(messages or []):
            since = 0
        new = (messages or [])[since:]
        if not force:
            if len(extract_real_user_queries(new)) < FLUSH_MIN_NEW_QUERIES:
                return {"status": "skipped", "reason": "too_little_new"}
            if sum(len(_message_text(m)) for m in new) < FLUSH_MIN_NEW_CHARS:
                return {"status": "skipped", "reason": "too_little_new"}
            if time.time() - float(state.get("at") or 0) < FLUSH_MIN_INTERVAL_S:
                return {"status": "skipped", "reason": "too_soon"}
        return run_memory_flush(
            project_path,
            session_id,
            messages,
            model=model,
            call_summarize=call_summarize,
            last_flush_hash=state.get("hash"),
            since=since,
        )
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(key)


def _flush_window(messages: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    filtered = [m for m in (messages or []) if m.get("role") in {"user", "assistant", "tool"}]
    return filtered[-limit:]

def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content") or ""
    if isinstance(content, list):
        return " ".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
    return str(content)


def _format_conversation_for_flush(messages: list[dict[str, Any]], max_chars: int = MAX_FLUSH_INPUT_CHARS) -> str:
    parts: list[str] = []
    used = 0
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = _message_text(msg)
        cap = _ROLE_CHAR_CAPS.get(role, 2000)
        if len(content) > cap:
            content = content[:cap] + "…"
        block = f"### {role}\n{content}"
        if parts and used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(reversed(parts))
