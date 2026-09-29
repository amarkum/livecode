from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

from .project_store import normalize_project_path

PLANS_ROOT = os.path.expanduser("~/.livecode/plans")
PLAN_SUFFIX = ".plan.md"
MAX_SLUG_CHARS = 60
FRONTMATTER_FENCE = "---"

def plans_root(*, create: bool = True) -> str:
    if create:
        os.makedirs(PLANS_ROOT, exist_ok=True)
    return PLANS_ROOT

def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "").strip().lower()).strip("_")
    return (slug[:MAX_SLUG_CHARS].rstrip("_")) or "plan"

def new_plan_filename(title: str) -> str:
    return f"{slugify(title)}_{uuid.uuid4().hex[:8]}{PLAN_SUFFIX}"

def safe_filename(filename: str) -> str:
    name = os.path.basename((filename or "").strip())
    if not name or name != (filename or "").strip():
        raise ValueError("invalid plan filename")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("invalid plan filename")
    if not name.endswith(PLAN_SUFFIX):
        raise ValueError(f"plan filename must end with {PLAN_SUFFIX}")
    return name

def plan_path(filename: str, *, create: bool = True) -> str:
    return os.path.join(plans_root(create=create), safe_filename(filename))

def plan_exists(filename: str) -> bool:
    try:
        return os.path.isfile(plan_path(filename, create=False))
    except ValueError:
        return False

def split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    text = content or ""
    if not text.startswith(FRONTMATTER_FENCE):
        return {}, text.strip()
    end = text.find(f"\n{FRONTMATTER_FENCE}", len(FRONTMATTER_FENCE))
    if end < 0:
        return {}, text.strip()
    block = text[len(FRONTMATTER_FENCE):end]
    body = text[end + len(FRONTMATTER_FENCE) + 1:].strip()
    meta: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    return meta, body

def strip_frontmatter(content: str) -> str:
    return split_frontmatter(content)[1]

def _render_frontmatter(meta: dict[str, Any]) -> str:
    lines = [FRONTMATTER_FENCE]
    for key, value in meta.items():
        text = str(value if value is not None else "").replace("\n", " ")
        lines.append(f'{key}: "{text}"' if ":" in text or "#" in text else f"{key}: {text}")
    lines.append(FRONTMATTER_FENCE)
    return "\n".join(lines)

def write_plan(
    body: str,
    *,
    title: str,
    project_path: str = "",
    session_id: str = "",
    filename: str = "",
    overview: str | None = None,
) -> dict[str, Any]:
    plan_title = (title or "").strip() or "Untitled plan"
    name = safe_filename(filename) if filename else new_plan_filename(plan_title)
    path = os.path.join(plans_root(), name)
    now = time.time()
    created_at = now
    existing_meta: dict[str, str] = {}
    if os.path.isfile(path):
        existing_meta, _ = split_frontmatter(_read_text(path))
        try:
            created_at = float(existing_meta.get("created_at") or now)
        except (TypeError, ValueError):
            created_at = now
    if overview is None:
        overview = existing_meta.get("overview") or ""
    meta = {
        "title": plan_title,
        "overview": " ".join(str(overview or "").split())[:600],
        "project_path": normalize_project_path(project_path) if project_path else "",
        "session_id": session_id or "",
        "created_at": created_at,
        "updated_at": now,
    }
    content = f"{_render_frontmatter(meta)}\n\n{(body or '').strip()}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {
        "file": name,
        "path": path,
        "title": plan_title,
        "overview": meta["overview"],
        "meta": meta,
        "body": (body or "").strip(),
        "todos": parse_plan_todos(body),
    }

def read_plan(filename: str) -> dict[str, Any]:
    path = plan_path(filename, create=False)
    if not os.path.isfile(path):
        raise FileNotFoundError(filename)
    content = _read_text(path)
    meta, body = split_frontmatter(content)
    return {
        "file": os.path.basename(path),
        "path": path,
        "title": meta.get("title") or _title_from_body(body) or os.path.basename(path),
        "overview": meta.get("overview") or "",
        "meta": meta,
        "content": content,
        "body": body,
        "todos": parse_plan_todos(body),
    }

def list_plans(*, limit: int = 200, project_path: str = "") -> list[dict[str, Any]]:
    root = plans_root(create=False)
    if not os.path.isdir(root):
        return []
    wanted = normalize_project_path(project_path) if project_path else ""
    plans: list[dict[str, Any]] = []
    for name in os.listdir(root):
        if not name.endswith(PLAN_SUFFIX):
            continue
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
            meta, body = split_frontmatter(_read_text(path, limit=4096))
        except OSError:
            continue
        plan_project = meta.get("project_path") or ""
        if wanted and plan_project and plan_project != wanted:
            continue
        plans.append({
            "file": name,
            "title": meta.get("title") or _title_from_body(body) or name,
            "project_path": plan_project,
            "session_id": meta.get("session_id") or "",
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    plans.sort(key=lambda p: p["mtime"], reverse=True)
    return plans[: max(1, limit)]

def delete_plan(filename: str) -> bool:
    path = plan_path(filename, create=False)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True

def _title_from_body(body: str) -> str:
    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""

def _read_text(path: str, *, limit: int = 0) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(limit) if limit else f.read()


TODO_SECTION_HEADING = "## Task checklist"
_TODO_LINE_RE = re.compile(r"^(\s*[-*]\s+\[)([ xX~-])(\]\s+)(.*)$")
_TODO_MARKS = {"completed": "x", "in_progress": "~", "cancelled": "-", "pending": " "}
_MARK_STATUS = {"x": "completed", "X": "completed", "~": "in_progress", "-": "cancelled", " ": "pending"}


def _todo_section_bounds(lines: list[str]) -> tuple[int, int]:
    """Line range [start, end) of the checklist section body, or (-1, -1)."""
    for idx, line in enumerate(lines):
        if line.strip().lower() == TODO_SECTION_HEADING.lower():
            end = len(lines)
            for j in range(idx + 1, len(lines)):
                if lines[j].startswith("#"):
                    end = j
                    break
            return idx + 1, end
    return -1, -1


def parse_plan_todos(body: str) -> list[dict[str, str]]:
    """The plan's checklist items as todo_write-style dicts with ids plan-1, plan-2, …"""
    lines = (body or "").splitlines()
    start, end = _todo_section_bounds(lines)
    if start < 0:
        return []
    todos: list[dict[str, str]] = []
    for line in lines[start:end]:
        match = _TODO_LINE_RE.match(line)
        if not match or not match.group(4).strip():
            continue
        todos.append({
            "id": f"plan-{len(todos) + 1}",
            "content": match.group(4).strip(),
            "status": _MARK_STATUS.get(match.group(2), "pending"),
        })
    return todos


def apply_todo_statuses(body: str, statuses: dict[str, str]) -> str:
    """Rewrite checklist marks for the given plan-N ids; other lines stay as they are."""
    lines = (body or "").splitlines()
    start, end = _todo_section_bounds(lines)
    if start < 0 or not statuses:
        return body
    index = 0
    for pos in range(start, end):
        match = _TODO_LINE_RE.match(lines[pos])
        if not match or not match.group(4).strip():
            continue
        index += 1
        status = statuses.get(f"plan-{index}")
        mark = _TODO_MARKS.get(str(status or ""))
        if mark is not None:
            lines[pos] = f"{match.group(1)}{mark}{match.group(3)}{match.group(4)}"
    return "\n".join(lines)


def sync_plan_todo_statuses(filename: str, todos: list[dict]) -> list[dict[str, str]] | None:
    """Mirror the build's task list onto the plan file. Returns the plan's todos, or None."""
    statuses = {
        str(t.get("id")): str(t.get("status") or "pending")
        for t in todos or []
        if isinstance(t, dict) and str(t.get("id") or "").startswith("plan-")
    }
    if not statuses:
        return None
    plan = read_plan(filename)
    body = apply_todo_statuses(plan["body"], statuses)
    if body != plan["body"]:
        meta = plan["meta"]
        write_plan(
            body,
            title=plan["title"],
            project_path=meta.get("project_path") or "",
            session_id=meta.get("session_id") or "",
            filename=plan["file"],
        )
    return parse_plan_todos(body)
