from __future__ import annotations

import json
import os
import time
from typing import Any

from livecode.session import _summary_path, session_dir

def _reminders_key() -> str:
    return "session_reminders"

def load_session_reminders(project_path: str, session_id: str) -> dict[str, Any]:
    path = _summary_path(project_path, session_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return dict(meta.get(_reminders_key()) or {})
    except (json.JSONDecodeError, OSError):
        return {}

def save_session_reminders(project_path: str, session_id: str, reminders: dict[str, Any]) -> None:
    path = _summary_path(project_path, session_id)
    meta: dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta[_reminders_key()] = reminders
    meta["reminders_updated_at"] = time.time()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

def record_file_edited(project_path: str, session_id: str, file_path: str) -> None:
    rem = load_session_reminders(project_path, session_id)
    edited = list(rem.get("files_edited") or [])
    if file_path and file_path not in edited:
        edited.append(file_path)
    rem["files_edited"] = edited[-20:]
    save_session_reminders(project_path, session_id, rem)

def load_todo_state(project_path: str, session_id: str) -> list[dict[str, Any]]:
    """The agent's live task list for this session (see harness TodoNudge/TodoGate)."""
    rem = load_session_reminders(project_path, session_id)
    todos = rem.get("todos")
    return list(todos) if isinstance(todos, list) else []


def save_todo_state(project_path: str, session_id: str, todos: list[dict[str, Any]]) -> None:
    rem = load_session_reminders(project_path, session_id)
    rem["todos"] = list(todos or [])[:50]
    save_session_reminders(project_path, session_id, rem)


def todo_pending_count(todos: list[dict[str, Any]]) -> tuple[int, int]:
    """Return ``(pending, in_progress)`` counts for a todo list."""
    pending = in_progress = 0
    for item in todos or []:
        status = str((item or {}).get("status") or "pending").lower()
        if status == "in_progress":
            in_progress += 1
        elif status not in ("completed", "cancelled"):
            pending += 1
    return pending, in_progress


def load_goal_state(project_path: str, session_id: str) -> dict[str, Any]:
    rem = load_session_reminders(project_path, session_id)
    goal = rem.get("goal")
    return dict(goal) if isinstance(goal, dict) else {}


def save_goal_state(project_path: str, session_id: str, goal: dict[str, Any]) -> None:
    rem = load_session_reminders(project_path, session_id)
    rem["goal"] = dict(goal or {})
    save_session_reminders(project_path, session_id, rem)


def record_compaction_ran(project_path: str, session_id: str) -> None:
    rem = load_session_reminders(project_path, session_id)
    rem["compaction_ran"] = True
    rem["compaction_at"] = time.time()
    save_session_reminders(project_path, session_id, rem)

def clear_compaction_reminder(project_path: str, session_id: str) -> None:
    rem = load_session_reminders(project_path, session_id)
    rem.pop("compaction_ran", None)
    save_session_reminders(project_path, session_id, rem)

def build_reminder_text(project_path: str, session_id: str) -> str:
    rem = load_session_reminders(project_path, session_id)
    lines: list[str] = []
    goal = rem.get("goal") or {}
    goal_text = str(goal.get("text") or "").strip()
    if goal_text and goal.get("status") in (None, "active", "blocked"):
        tag = " (marked blocked)" if goal.get("status") == "blocked" else ""
        lines.append(f"Current goal{tag}: {goal_text[:240]}")
    todos = rem.get("todos") or []
    if todos:
        pend, prog = todo_pending_count(todos)
        done = sum(1 for t in todos if str((t or {}).get("status")).lower() == "completed")
        active = next((t.get("content") for t in todos if str((t or {}).get("status")).lower() == "in_progress"), None)
        summary = f"Task list: {done} done · {prog} in progress · {pend} pending"
        if active:
            summary += f" — active: {str(active)[:120]}"
        lines.append(summary)
    edited = rem.get("files_edited") or []
    if edited:
        lines.append(f"Files edited this session: {', '.join(edited[-8:])}")
    if rem.get("compaction_ran"):
        lines.append("Conversation was compacted — rely on the summary prefix for older context.")
    if rem.get("pending_permission"):
        lines.append("A tool permission may be pending user approval.")
    return "\n".join(lines)
