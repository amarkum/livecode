from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable

MAX_TITLE_CHARS = 48
MAX_ACTIONS_KEPT = 40
MAX_FINDINGS_CHARS = 16_000
MAX_FINDINGS_DISPLAY_CHARS = 4_000

READ_TOOLS = frozenset({"read_repo_file", "ast_symbols", "lsp_document_symbols"})
SEARCH_TOOLS = frozenset({
    "grep_repo", "glob_files", "find_files", "find_symbol", "find_references", "list_symbols",
    "list_repo_dir", "web_search", "memory_search", "git_log", "lsp_references",
})
EDIT_TOOLS = frozenset({"write_file", "edit_file", "multi_edit"})
SCOPED_WRITER_EXCLUDED_TOOLS = frozenset({"run_command", "kill_command", "command_status"})

_VERBS: dict[str, tuple[str, str]] = {
    "read_repo_file": ("Reading", "Read"),
    "ast_symbols": ("Reading", "Read"),
    "lsp_document_symbols": ("Reading", "Read"),
    "grep_repo": ("Grepping", "Grepped"),
    "glob_files": ("Searching files", "Searched files"),
    "find_files": ("Searching files", "Searched files"),
    "find_symbol": ("Searching symbols", "Searched symbols"),
    "find_references": ("Finding references to", "Found references to"),
    "list_symbols": ("Searching symbols", "Searched symbols"),
    "list_repo_dir": ("Listing", "Listed"),
    "git_log": ("Reading history of", "Read history of"),
    "web_search": ("Searching web", "Searched web"),
    "web_fetch": ("Fetching", "Fetched"),
    "memory_search": ("Searching memory for", "Searched memory for"),
    "memory_get": ("Reading memory", "Read memory"),
    "write_file": ("Editing", "Edited"),
    "edit_file": ("Editing", "Edited"),
    "multi_edit": ("Editing", "Edited"),
    "run_command": ("Running", "Ran"),
    "command_status": ("Checking command", "Checked command"),
    "lsp_diagnostics": ("Reading lints", "Read lints"),
    "lsp_definition": ("Finding definition in", "Found definition in"),
    "lsp_references": ("Finding references in", "Found references in"),
    "lsp_hover": ("Inspecting", "Inspected"),
}


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _short(value: Any, limit: int = 48) -> str:
    text = _one_line(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _basename(path: Any) -> str:
    text = str(path or "").replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1] or text


def subagent_title(args: dict) -> str:
    title = _one_line(args.get("title"))
    if not title:
        goal = _one_line(args.get("goal"))
        first = re.split(r"(?<=[.!?;:])\s", goal, maxsplit=1)[0]
        title = " ".join(first.split()[:6])
    title = title.strip(" .;:-")
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return (title[:1].upper() + title[1:]) if title else "Subagent"


def _line_range(args: dict) -> str:
    try:
        start = int(args.get("start_line") or 0)
        end = int(args.get("end_line") or 0)
    except (TypeError, ValueError):
        return ""
    if start and end:
        return f" L{start}-{end}"
    return f" L{start}" if start else ""


def _action_object(tool: str, args: dict) -> str:
    if tool == "read_repo_file":
        return _basename(args.get("file_path")) + _line_range(args)
    if tool in READ_TOOLS or tool in EDIT_TOOLS or tool.startswith("lsp_"):
        return _basename(args.get("file_path"))
    if tool == "grep_repo":
        where = _short(args.get("directory"), 32)
        pattern = _short(args.get("pattern"), 40)
        return f"{pattern} in {where}" if where and where not in (".", "./") else pattern
    if tool == "glob_files":
        return _short(args.get("pattern"), 40)
    if tool == "find_files":
        return _short(args.get("query"), 40)
    if tool in ("find_symbol", "find_references"):
        return _short(args.get("name"), 40)
    if tool == "list_symbols":
        return _short(args.get("path") or "project", 40)
    if tool == "list_repo_dir":
        return _short(args.get("directory") or "project", 40)
    if tool == "git_log":
        return _short(args.get("path") or "the repo", 40)
    if tool in ("web_search", "memory_search"):
        return _short(args.get("query"), 40)
    if tool == "web_fetch":
        return _short(args.get("url"), 48)
    if tool == "run_command":
        return _short(args.get("description") or args.get("command"), 48)
    return ""


def action_text(tool: str, args: dict | None, result: dict | None = None, *, running: bool = False) -> str:
    args = args if isinstance(args, dict) else {}
    verbs = _VERBS.get(tool)
    if verbs is None:
        name = tool.split("__")[-1] if tool.startswith("mcp__") else tool
        if tool == "call_mcp_tool":
            name = str(args.get("tool_name") or args.get("tool") or tool)
        return f"{'Running' if running else 'Ran'} {_short(name, 40)}"
    obj = _action_object(tool, args)
    text = f"{verbs[0] if running else verbs[1]} {obj}".strip()
    if running or not isinstance(result, dict):
        return text
    if tool in EDIT_TOOLS:
        if result.get("error"):
            return f"Couldn't edit {obj}".strip()
        if result.get("is_new_file"):
            text = f"Created {obj}".strip()
        added, removed = int(result.get("additions") or 0), int(result.get("deletions") or 0)
        counts = " ".join(part for part in (f"+{added}" if added else "", f"-{removed}" if removed else "") if part)
        if counts:
            text += " " + counts
    return text


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


class SubagentProgress:

    def __init__(
        self,
        agent_id: str,
        title: str,
        emit: Callable[[dict[str, Any]], None] | None = None,
        *,
        model: str = "",
    ):
        self.agent_id = agent_id
        self.title = title
        self.model = str(model or "")
        self._emit = emit
        self._lock = threading.Lock()
        self.started_at = time.monotonic()
        self.files_read: set[str] = set()
        self.searches = 0
        self.actions: list[str] = []
        self.files_changed: dict[str, dict[str, Any]] = {}
        self.action = ""
        self.action_running = False
        self.state = "running"

    def elapsed_s(self) -> float:
        return round(time.monotonic() - self.started_at, 1)

    def outcome(self) -> str:
        if self.files_changed:
            names = [_basename(path) for path in self.files_changed]
            return "Edited " + (", ".join(names) if len(names) <= 2 else _plural(len(names), "file"))
        parts = []
        if self.files_read:
            parts.append("read " + _plural(len(self.files_read), "file"))
        if self.searches:
            parts.append(_plural(self.searches, "search", "searches"))
        text = ", ".join(parts) or "done"
        return text[:1].upper() + text[1:]

    def _snapshot(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "title": self.title,
            "model": self.model,
            "state": self.state,
            "action": self.action,
            "action_running": self.action_running,
            "files_read": len(self.files_read),
            "searches": self.searches,
            "files_changed": len(self.files_changed),
            "elapsed_s": self.elapsed_s(),
        }

    def _send(self, **extra: Any) -> None:
        if not self._emit:
            return
        try:
            self._emit({**self._snapshot(), **extra})
        except Exception:
            pass

    def tool_started(self, tool: str, args: dict | None) -> None:
        with self._lock:
            self.action = action_text(tool, args, running=True)
            self.action_running = True
        self._send()

    def tool_finished(self, tool: str, args: dict | None, result: Any) -> None:
        args = args if isinstance(args, dict) else {}
        result = result if isinstance(result, dict) else {}
        ok = not result.get("error")
        changed: dict[str, Any] | None = None
        with self._lock:
            if tool in READ_TOOLS and ok:
                self.files_read.add(str(result.get("file") or args.get("file_path") or ""))
            elif tool in SEARCH_TOOLS:
                self.searches += 1
            if tool in EDIT_TOOLS and ok and result.get("success") and not result.get("no_changes"):
                path = str(result.get("file_path") or args.get("file_path") or "")
                entry = self.files_changed.setdefault(path, {
                    "path": path,
                    "additions": 0,
                    "deletions": 0,
                    "absolute_path": str(result.get("absolute_path") or ""),
                    "workspace_root": str(result.get("workspace_root") or ""),
                    "relative_path": str(result.get("relative_path") or ""),
                })
                entry["additions"] += int(result.get("additions") or 0)
                entry["deletions"] += int(result.get("deletions") or 0)
                changed = {
                    "path": path,
                    "absolute_path": entry["absolute_path"],
                    "additions": int(result.get("additions") or 0),
                    "deletions": int(result.get("deletions") or 0),
                }
            self.action = action_text(tool, args, result)
            self.action_running = False
            self.actions.append(self.action)
            del self.actions[:-MAX_ACTIONS_KEPT]
        if changed:
            self._send(changed_file=changed)
        else:
            self._send()

    def finish(self, state: str, error: str = "") -> None:
        with self._lock:
            self.state = state
            self.action_running = False
            if state == "failed":
                self.action = "Failed: " + _short(error, 80) if error else "Failed"
            elif state == "stopped":
                self.action = "Stopped"
            else:
                self.action = self.outcome()
        self._send(duration_s=self.elapsed_s())

    def display_fields(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "model": self.model,
            "duration_s": self.elapsed_s(),
            "files_read": len(self.files_read),
            "searches": self.searches,
            "outcome": self.outcome(),
            "actions": list(self.actions),
            "files_changed": list(self.files_changed.values()),
        }


class FileScope:

    def __init__(self, entries: list[str], resolve: Callable[[str], str | None]):
        self.entries: list[str] = []
        self.paths: list[tuple[str, bool]] = []
        self.unresolved: list[str] = []
        for raw in entries:
            text = str(raw or "").strip()
            if not text:
                continue
            full = resolve(text)
            if not full:
                self.unresolved.append(text)
                continue
            self.entries.append(text)
            is_dir = text.endswith(("/", "\\")) or os.path.isdir(full)
            self.paths.append((_norm(full), is_dir))

    def __bool__(self) -> bool:
        return bool(self.paths)

    def allows(self, full: str | None) -> bool:
        if not full:
            return False
        target = _norm(full)
        return any(target == path or (is_dir and _under(target, path)) for path, is_dir in self.paths)

    def overlaps(self, other: "FileScope") -> bool:
        for a, a_dir in self.paths:
            for b, b_dir in other.paths:
                if a == b or (a_dir and _under(b, a)) or (b_dir and _under(a, b)):
                    return True
        return False


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _under(path: str, folder: str) -> bool:
    return path.startswith(folder.rstrip(os.sep) + os.sep)


def subagent_read_only(args: dict) -> bool:
    value = args.get("read_only", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no")
    return value is not False


def scope_entries(args: dict) -> list[str]:
    files = args.get("files")
    if isinstance(files, str):
        files = [files]
    return [str(f) for f in files if str(f or "").strip()] if isinstance(files, list) else []


def is_scoped_writer_call(args: dict) -> bool:
    return not subagent_read_only(args) and bool(scope_entries(args))


def compact_subagent_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return {"result": str(result)[:MAX_FINDINGS_CHARS]}
    findings = str(result.get("result") or "")
    if len(findings) > MAX_FINDINGS_CHARS:
        findings = findings[:MAX_FINDINGS_CHARS] + "\n... [truncated]"
    out: dict[str, Any] = {"success": bool(result.get("success")), "title": result.get("title") or "", "result": findings}
    changed = [
        f"{item.get('path')} (+{int(item.get('additions') or 0)} -{int(item.get('deletions') or 0)})"
        for item in result.get("files_changed") or []
        if isinstance(item, dict) and item.get("path")
    ]
    if changed:
        out["files_changed"] = changed
    if result.get("error"):
        out["error"] = result["error"]
    if result.get("state") == "stopped":
        out["stopped"] = True
    return out


def display_subagent_result(result: dict) -> dict:
    findings = str(result.get("result") or "")
    if len(findings) > MAX_FINDINGS_DISPLAY_CHARS:
        findings = findings[:MAX_FINDINGS_DISPLAY_CHARS].rstrip() + "…"
    keys = ("success", "error", "title", "goal", "read_only", "files", "state", "model", "duration_s",
            "files_read", "searches", "outcome", "actions")
    out = {key: result.get(key) for key in keys if result.get(key) is not None}
    out["result"] = findings
    out["files_changed"] = [
        {k: item.get(k) for k in ("path", "additions", "deletions", "absolute_path")}
        for item in result.get("files_changed") or []
        if isinstance(item, dict)
    ]
    return out


def run_subagent_turn(
    *,
    project_path: str,
    goal: str,
    parent_session_id: str,
    run_turn_fn: Callable[..., Any],
    read_only: bool = True,
    max_iterations: int = 30,
    **turn_kwargs: Any,
) -> dict[str, Any]:
    child_session = f"{parent_session_id}_sub_{uuid.uuid4().hex[:8]}"
    question = (
        f"{goal}\n\n"
        "When you are done, reply with your findings for the main agent."
    )
    answer_parts: list[str] = []
    error = ""
    usage_by_model: dict[str, dict[str, int]] = {}
    try:
        for chunk in run_turn_fn(
            project_path,
            question,
            session_id=child_session,
            max_iterations=max_iterations,
            **turn_kwargs,
        ):
            if isinstance(chunk, str) and chunk.startswith("data: "):
                try:
                    payload = json.loads(chunk[6:].strip())
                    if payload.get("token"):
                        answer_parts.append(payload["token"])
                    if payload.get("answer"):
                        answer_parts.append(payload["answer"])
                    if payload.get("error"):
                        error = str(payload["error"])
                    for model, usage in (payload.get("usage_by_model") or {}).items():
                        bucket = usage_by_model.setdefault(
                            model, {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
                        )
                        for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                            bucket[key] = bucket.get(key, 0) + int(usage.get(key) or 0)
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        error = str(exc)

    result = "".join(answer_parts).strip()
    return {
        "success": not error,
        "goal": goal,
        "read_only": read_only,
        "child_session_id": child_session,
        "result": result or error or "Subagent completed with no output",
        "error": error or None,
        "usage_by_model": usage_by_model,
    }
