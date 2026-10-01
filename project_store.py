from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any

from .workspace_config import declared_primary_path

LIVECODE_ROOT = os.path.expanduser("~/.livecode")
PROJECTS_ROOT = os.path.join(LIVECODE_ROOT, "projects")
PROJECT_META_FILE = "project.json"
_PROJECT_STORE_LOCK = threading.RLock()
_EPHEMERAL_PATH_MARKERS = (
    "/pytest-",
    "/pytest/",
    "/pytest_of_",
    "/pytest-of-",
    "/var/folders/",
    "/private/var/folders/",
    "/tmp/",
    "/private/tmp/",
)

def normalize_project_path(project_path: str) -> str:
    expanded = os.path.abspath(os.path.expanduser(project_path or ""))
    try:
        return os.path.realpath(expanded)
    except OSError:
        return expanded

def path_to_project_slug(abs_path: str) -> str:
    encoded = abs_path or ""
    for ch in ("/", "\\", ":", ".", "_", " "):
        encoded = encoded.replace(ch, "-")
    return encoded.lstrip("-") or "project"

def project_key(project_path: str) -> str:
    root = normalize_project_path(project_path)
    digest = hashlib.sha256(os.path.normcase(root).encode("utf-8")).hexdigest()[:12]
    return f"{path_to_project_slug(root)}-{digest}"

def workspace_state_identity(workspace: Any) -> str:
    primary = str(getattr(workspace, "primary_path", "") or "").strip()
    if not primary:
        for folder in getattr(workspace, "folders", None) or ():
            candidate = str(getattr(folder, "path", "") or "").strip()
            if candidate:
                primary = candidate
                break
    return normalize_project_path(primary)

def workspace_state_path(project_path: str, workspace_payload: dict[str, Any] | None = None) -> str:
    """Where a workspace keeps its chats and memory: its primary folder's hashed project directory."""
    return normalize_project_path(declared_primary_path(project_path, workspace_payload))

def is_ephemeral_project_path(project_path: str) -> bool:
    root = normalize_project_path(project_path).replace("\\", "/")
    lower = root.lower()
    if any(marker in lower for marker in _EPHEMERAL_PATH_MARKERS):
        return True
    try:
        tmp = os.path.realpath(tempfile.gettempdir()).replace("\\", "/").lower()
        if tmp and (lower == tmp or lower.startswith(tmp.rstrip("/") + "/")):
            return True
    except OSError:
        pass
    return bool(re.search(r"/t(?:emp)?(?:/|$)", lower) and "/var/folders/" in lower)

def _metadata_project_path(path: str) -> str:
    try:
        with open(os.path.join(path, PROJECT_META_FILE), "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    return normalize_project_path(str(meta.get("project_path") or "")) if isinstance(meta, dict) else ""

@contextmanager
def _project_storage_lock():
    os.makedirs(PROJECTS_ROOT, exist_ok=True)
    with _PROJECT_STORE_LOCK:
        lock_path = os.path.join(PROJECTS_ROOT, ".project-storage.lock")
        try:
            import fcntl
        except ImportError:
            yield
            return
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

def project_dir(project_path: str) -> str:
    root = normalize_project_path(project_path)
    path = os.path.join(PROJECTS_ROOT, project_key(root))
    with _project_storage_lock():
        os.makedirs(path, exist_ok=True)
        _write_project_metadata(path, project_key(root), root)
        return path

def project_file(project_path: str, *parts: str, create: bool = True) -> str:
    base = project_dir(project_path) if create else existing_project_dir(project_path)
    return os.path.join(base, *parts)

def existing_project_dir(project_path: str) -> str:
    return os.path.join(PROJECTS_ROOT, project_key(normalize_project_path(project_path)))

def project_exists(project_path: str) -> bool:
    return os.path.isdir(existing_project_dir(project_path))

def delete_project_storage(project_path: str) -> bool:
    path = existing_project_dir(project_path)
    with _project_storage_lock():
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path)
    return True

def prune_ephemeral_project_dirs(projects_root: str | None = None) -> int:
    root = projects_root or PROJECTS_ROOT
    if not os.path.isdir(root):
        return 0
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        project_path = _metadata_project_path(path) if os.path.isdir(path) else ""
        if project_path and is_ephemeral_project_path(project_path):
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError:
                pass
    return removed

def _write_project_metadata(path: str, key: str, root: str) -> None:
    meta_path = os.path.join(path, PROJECT_META_FILE)
    payload: dict[str, Any] = {
        "project_key": key,
        "project_path": root,
        "project_name": os.path.basename(root.rstrip(os.sep)) or root,
        "updated_at": time.time(),
    }
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                existing_root = normalize_project_path(str(existing.get("project_path") or ""))
                if existing_root and existing_root != root:
                    raise RuntimeError(f"Project storage collision: {path}")
                payload["created_at"] = (
                    existing.get("created_at")
                    or existing.get("updated_at")
                    or payload["updated_at"]
                )
        except json.JSONDecodeError:
            pass
    payload.setdefault("created_at", payload["updated_at"])
    temp_path = f"{meta_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, meta_path)
