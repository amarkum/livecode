from __future__ import annotations

import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from livecode import project_store as project_store_mod
from livecode.project_store import is_ephemeral_project_path, project_file

MEMORY_FILENAME = "MEMORY.md"
SESSIONS_DIRNAME = "sessions"
INDEX_FILENAME = "index.sqlite"

_MEMORY_LOCKS: dict[str, threading.RLock] = {}
_MEMORY_LOCKS_GUARD = threading.Lock()


@contextmanager
def memory_lock(project_path: str) -> Iterator[None]:
    key = os.path.realpath(memory_root(project_path, create=False))
    with _MEMORY_LOCKS_GUARD:
        lock = _MEMORY_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def write_text_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _skip_workspace_write(project_path: str) -> bool:
    if not is_ephemeral_project_path(project_path):
        return False
    default_root = os.path.join(os.path.expanduser("~/.livecode"), "projects")
    try:
        return os.path.realpath(project_store_mod.PROJECTS_ROOT) == os.path.realpath(default_root)
    except OSError:
        return True

def slugify(input_text: str, max_len: int = 30) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in (input_text or "").lower())
    result: list[str] = []
    prev_dash = False
    for c in slug:
        if c == "-":
            if not prev_dash:
                result.append("-")
            prev_dash = True
        else:
            result.append(c)
            prev_dash = False
    truncated = "".join(result)[:max_len]
    return truncated.strip("-")

def memory_root(project_path: str, *, create: bool = True) -> str:
    path = project_file(project_path, "memory", create=create)
    if create:
        os.makedirs(path, exist_ok=True)
    return path

def memory_md_path(project_path: str, *, create: bool = True) -> str:
    root = memory_root(project_path, create=create)
    return os.path.join(root, MEMORY_FILENAME)

def sessions_dir(project_path: str, *, create: bool = True) -> str:
    path = os.path.join(memory_root(project_path, create=create), SESSIONS_DIRNAME)
    if create:
        os.makedirs(path, exist_ok=True)
    return path

def index_db_path(project_path: str, *, create: bool = True) -> str:
    return os.path.join(memory_root(project_path, create=create), INDEX_FILENAME)

def session_log_path(project_path: str, date: str, topic_slug: str, session_id: str) -> str:
    sid8 = (session_id or "session")[:8]
    slug = topic_slug or "session"
    filename = f"{date}-{slug}-{sid8}.md"
    return os.path.join(sessions_dir(project_path), filename)

def read_memory_md(project_path: str) -> str:
    path = memory_md_path(project_path, create=False)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""

_NOTES_HEADING = "## Notes"


def _last_heading(text: str) -> str:
    heading = ""
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line.strip()
    return heading


def append_memory_md(project_path: str, note: str) -> str:
    text = _normalize_memory_content(note or "")
    if not text:
        return read_memory_md(project_path)
    if _skip_workspace_write(project_path):
        return read_memory_md(project_path)
    path = memory_md_path(project_path, create=True)
    with memory_lock(project_path):
        existing = read_memory_md(project_path)
        if text.startswith(_NOTES_HEADING + "\n"):
            bullets = text[len(_NOTES_HEADING):].strip()
            if bullets and bullets in existing:
                return existing
            if _last_heading(existing) == _NOTES_HEADING:
                text = bullets
        combined = existing.rstrip() + "\n\n" + text if existing.strip() else text
        if not combined.endswith("\n"):
            combined += "\n"
        write_text_atomic(path, combined)
    return combined

def write_session_log(
    project_path: str,
    *,
    date: str,
    topic_slug: str,
    session_id: str,
    content: str,
    append: bool = False,
) -> str | None:
    body = (content or "").strip()
    if not body:
        return None
    if _skip_workspace_write(project_path):
        return None
    path = session_log_path(project_path, date, topic_slug, session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with memory_lock(project_path):
        if append and os.path.isfile(path):
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            block = f"\n\n---\n\n<!-- flush {stamp} -->\n\n{body}"
            with open(path, "a", encoding="utf-8") as f:
                f.write(block)
        else:
            write_text_atomic(path, body)
    return path

def list_memory_files(project_path: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    root = memory_root(project_path, create=False)
    md = os.path.join(root, MEMORY_FILENAME)
    if os.path.isfile(md):
        files.append({"path": MEMORY_FILENAME, "source": "workspace", "abs_path": md})
    sess = os.path.join(root, SESSIONS_DIRNAME)
    if os.path.isdir(sess):
        for name in sorted(os.listdir(sess)):
            if not name.endswith(".md"):
                continue
            abs_path = os.path.join(sess, name)
            if os.path.isfile(abs_path):
                files.append(
                    {
                        "path": f"{SESSIONS_DIRNAME}/{name}",
                        "source": "session",
                        "abs_path": abs_path,
                    }
                )
    return files

def read_memory_file(
    project_path: str,
    rel_path: str,
    *,
    from_line: int = 0,
    lines: int | None = None,
) -> dict[str, Any]:
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    if ".." in rel.split("/") or rel.startswith("/"):
        return {"error": "invalid path"}
    root = os.path.realpath(memory_root(project_path, create=False))
    abs_path = os.path.realpath(os.path.join(root, rel))
    if abs_path != root and not abs_path.startswith(root + os.sep):
        return {"error": "path escapes memory root"}
    if not os.path.isfile(abs_path):
        return {"error": "file not found", "path": rel}
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.read().splitlines()
    except OSError as exc:
        return {"error": str(exc), "path": rel}
    start = max(0, int(from_line or 0))
    if lines is None:
        slice_lines = all_lines[start:]
    else:
        slice_lines = all_lines[start : start + max(0, int(lines))]
    return {
        "path": rel,
        "from_line": start,
        "line_count": len(slice_lines),
        "total_lines": len(all_lines),
        "content": "\n".join(slice_lines),
    }

def _normalize_memory_content(content: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    if not re.search(r"^##\s+", text, re.M):
        if not text.startswith("- ") and not text.startswith("* "):
            text = f"- {text}"
        text = f"## Notes\n\n{text}"
    return text


_SESSION_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.md$")
MAX_EDITABLE_CHARS = 2_000_000


def editable_memory_rel(rel_path: str) -> str | None:
    """The memory files the Settings view may open and save: MEMORY.md and the session logs, nothing else."""
    rel = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if rel == MEMORY_FILENAME:
        return rel
    head, _, name = rel.partition("/")
    if head == SESSIONS_DIRNAME and "/" not in name and _SESSION_LOG_NAME_RE.match(name):
        return rel
    return None


def list_editable_memory(project_path: str) -> list[dict[str, Any]]:
    """MEMORY.md first (listed even before it exists, so it can be written), then the session logs, newest first."""
    root = memory_root(project_path, create=False)
    entries: list[dict[str, Any]] = []
    md = os.path.join(root, MEMORY_FILENAME)
    entries.append(_memory_entry(MEMORY_FILENAME, "workspace", md))
    sess = os.path.join(root, SESSIONS_DIRNAME)
    logs: list[dict[str, Any]] = []
    if os.path.isdir(sess):
        for name in os.listdir(sess):
            rel = f"{SESSIONS_DIRNAME}/{name}"
            abs_path = os.path.join(sess, name)
            if editable_memory_rel(rel) and os.path.isfile(abs_path):
                logs.append(_memory_entry(rel, "session", abs_path))
    logs.sort(key=lambda e: (e["modified"] or 0, e["path"]), reverse=True)
    return entries + logs


def _memory_entry(rel: str, source: str, abs_path: str) -> dict[str, Any]:
    try:
        info = os.stat(abs_path)
        size, modified, exists = info.st_size, info.st_mtime, True
    except OSError:
        size, modified, exists = 0, None, False
    return {"path": rel, "name": os.path.basename(rel), "source": source, "size": size, "modified": modified, "exists": exists}


def _editable_abs_path(project_path: str, rel: str) -> str:
    root = os.path.realpath(memory_root(project_path, create=False))
    abs_path = os.path.realpath(os.path.join(root, rel))
    if not abs_path.startswith(root + os.sep):
        raise ValueError("path escapes the memory folder")
    return abs_path


def read_editable_memory(project_path: str, rel_path: str) -> dict[str, Any]:
    rel = editable_memory_rel(rel_path)
    if rel is None:
        raise ValueError(f"not an editable memory file: {rel_path}")
    abs_path = _editable_abs_path(project_path, rel)
    if not os.path.isfile(abs_path):
        if rel == MEMORY_FILENAME:
            return {"path": rel, "content": "", "exists": False}
        raise FileNotFoundError(f"{rel} does not exist")
    with open(abs_path, "rb") as f:
        raw = f.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{rel} is not UTF-8 text") from None
    return {"path": rel, "content": content, "exists": True, "size": len(raw)}


def save_editable_memory(project_path: str, rel_path: str, content: Any) -> dict[str, Any]:
    """Saves one memory file atomically under the project's memory lock. Ephemeral workspaces keep no memory."""
    rel = editable_memory_rel(rel_path)
    if rel is None:
        raise ValueError(f"not an editable memory file: {rel_path}")
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError("content must be text")
    if len(content) > MAX_EDITABLE_CHARS:
        raise ValueError(f"content is longer than {MAX_EDITABLE_CHARS} characters")
    if _skip_workspace_write(project_path):
        return {"path": rel, "saved": False, "skipped": "This workspace is temporary, so it keeps no memory."}
    memory_root(project_path, create=True)
    if rel.startswith(SESSIONS_DIRNAME + "/"):
        sessions_dir(project_path, create=True)
    abs_path = _editable_abs_path(project_path, rel)
    with memory_lock(project_path):
        write_text_atomic(abs_path, content)
    return {"path": rel, "saved": True, "abs_path": abs_path, "size": len(content.encode("utf-8"))}
