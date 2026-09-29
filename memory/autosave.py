from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from livecode.memory.index import embed_missing_chunks, reindex_file
from livecode.memory.ranking import is_scaffold
from livecode.memory.storage import memory_lock, session_log_path, slugify, write_session_log

MIN_USER_MESSAGES = 3
MIN_TOTAL_QUERY_BYTES = 50

_SYNTHETIC_MARKERS = (
    "<user_info>",
    "<git_status>",
    "__auto_continue__",
    "<system-reminder>",
    "<memory-context>",
)

def extract_real_user_queries(messages: list[dict[str, Any]]) -> list[str]:
    queries: list[str] = []
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        if any(m in text for m in _SYNTHETIC_MARKERS):
            continue
        if text.startswith("**Project layout**") or text.startswith("<project_layout"):
            continue
        queries.append(text)
    return queries

def generate_metadata_summary(
    messages: list[dict[str, Any]],
    real_queries: list[str] | None = None,
) -> str:
    real = real_queries if real_queries is not None else extract_real_user_queries(messages)
    assistant_count = sum(1 for m in messages or [] if m.get("role") == "assistant")
    tool_count = sum(1 for m in messages or [] if m.get("role") == "tool")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "## Session Summary",
        "",
        f"- **Messages:** {len(real)} user, {assistant_count} assistant, {tool_count} tool results",
        f"- **Date:** {now}",
        "",
    ]
    topics = [q[:100] for q in real[:5]]
    if topics:
        lines.append("## Topics Discussed")
        lines.append("")
        for i, topic in enumerate(topics, 1):
            lines.append(f"{i}. {topic}")
        lines.append("")
    return "\n".join(lines)

def maybe_autosave_session(
    project_path: str,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    save_on_end: bool = True,
) -> str | None:
    if not save_on_end:
        return None
    real = extract_real_user_queries(messages)
    if len(real) < MIN_USER_MESSAGES:
        return None
    if sum(len(q) for q in real) < MIN_TOTAL_QUERY_BYTES:
        return None
    first = real[0] if real else ""
    topic = slugify(first, 30) or "session"
    summary = generate_metadata_summary(messages, real)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing_path = session_log_path(project_path, date, topic, session_id)
    with memory_lock(project_path):
        if os.path.isfile(existing_path):
            try:
                with open(existing_path, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            except OSError:
                existing = ""
            if existing.strip() and not is_scaffold(existing):
                return None
        path = write_session_log(
            project_path,
            date=date,
            topic_slug=topic,
            session_id=session_id,
            content=summary,
            append=False,
        )
    if path:
        reindex_file(project_path, path, "session")
        embed_missing_chunks(project_path)
    return path
