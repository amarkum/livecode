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
MEMORY_INDEX_PREFIX = "index.sqlite"
_PROJECT_STORE_LOCK = threading.RLock()
_LEGACY_ADOPTION_SEEN: set[tuple[str, str, str]] = set()
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

def _legacy_project_key(project_path: str) -> str:
    return path_to_project_slug(normalize_project_path(project_path))

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
    identity = normalize_project_path(declared_primary_path(project_path, workspace_payload))
    if isinstance(workspace_payload, dict):
        raw_folders = workspace_payload.get("folders")
        if isinstance(raw_folders, list) and len(raw_folders) > 1:
            adopt_legacy_workspace_storage(identity, [
                item.get("path") if isinstance(item, dict) else item
                for item in raw_folders
            ])
    return identity

def _legacy_workspace_marker(folder_paths: Any) -> str:
    paths = sorted({
        normalize_project_path(str(path))
        for path in folder_paths
        if isinstance(path, str) and path.strip()
    })
    if len(paths) < 2:
        return ""
    digest = hashlib.sha256(json.dumps(paths, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return f"workspace::{digest}"

def _legacy_workspace_dirs(marker: str) -> list[str]:
    if not marker or not os.path.isdir(PROJECTS_ROOT):
        return []
    found: list[str] = []
    for name in sorted(os.listdir(PROJECTS_ROOT)):
        path = os.path.join(PROJECTS_ROOT, name)
        if os.path.isdir(path) and os.path.basename(_metadata_project_path(path)) == marker:
            found.append(path)
    return found

def _move_missing_entries(source_dir: str, target_dir: str, prefix: str) -> list[str]:
    moved: list[str] = []
    if not os.path.isdir(source_dir):
        return moved
    os.makedirs(target_dir, exist_ok=True)
    for name in sorted(os.listdir(source_dir)):
        if name.startswith(MEMORY_INDEX_PREFIX):
            continue
        destination = os.path.join(target_dir, name)
        if os.path.exists(destination):
            continue
        try:
            shutil.move(os.path.join(source_dir, name), destination)
        except OSError:
            continue
        moved.append(f"{prefix}/{name}")
    return moved

def _adopt_legacy_dir(legacy: str, target: str) -> list[str]:
    moved = _move_missing_entries(os.path.join(legacy, "sessions"), os.path.join(target, "sessions"), "sessions")
    moved += _move_missing_entries(os.path.join(legacy, "memory"), os.path.join(target, "memory"), "memory")
    moved += _move_missing_entries(
        os.path.join(legacy, "memory", "sessions"),
        os.path.join(target, "memory", "sessions"),
        "memory/sessions",
    )
    return moved

def _legacy_dir_is_spent(legacy: str) -> bool:
    for dirpath, _dirnames, filenames in os.walk(legacy):
        for name in filenames:
            if dirpath == legacy and name == PROJECT_META_FILE:
                continue
            if name.startswith(MEMORY_INDEX_PREFIX) and os.path.basename(dirpath) == "memory":
                continue
            return False
    return True

def adopt_legacy_workspace_storage(identity_path: str, folder_paths: Any) -> list[str]:
    identity = normalize_project_path(identity_path)
    marker = _legacy_workspace_marker(list(folder_paths or ()))
    if not marker:
        return []
    seen_key = (PROJECTS_ROOT, identity, marker)
    with _PROJECT_STORE_LOCK:
        if seen_key in _LEGACY_ADOPTION_SEEN:
            return []
        _LEGACY_ADOPTION_SEEN.add(seen_key)
        legacy_dirs = _legacy_workspace_dirs(marker)
    if not legacy_dirs:
        return []
    target = project_dir(identity)
    moved: list[str] = []
    with _project_storage_lock():
        for legacy in legacy_dirs:
            moved.extend(_adopt_legacy_dir(legacy, target))
            if _legacy_dir_is_spent(legacy):
                shutil.rmtree(legacy, ignore_errors=True)
    return moved

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

def _storage_candidates(root: str) -> tuple[str, str]:
    return os.path.join(PROJECTS_ROOT, project_key(root)), os.path.join(PROJECTS_ROOT, _legacy_project_key(root))

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

def _migrate_legacy_storage(root: str, target: str, legacy: str) -> None:
    if os.path.exists(target) or not os.path.isdir(legacy) or _metadata_project_path(legacy) != root:
        return
    try:
        os.replace(legacy, target)
    except OSError:
        shutil.copytree(legacy, target, dirs_exist_ok=False)
        shutil.rmtree(legacy)

def project_dir(project_path: str) -> str:
    root = normalize_project_path(project_path)
    path, legacy = _storage_candidates(root)
    with _project_storage_lock():
        _migrate_legacy_storage(root, path, legacy)
        os.makedirs(path, exist_ok=True)
        _write_project_metadata(path, project_key(root), root)
        return path

def project_file(project_path: str, *parts: str, create: bool = True) -> str:
    base = project_dir(project_path) if create else existing_project_dir(project_path)
    return os.path.join(base, *parts)

def existing_project_dir(project_path: str) -> str:
    root = normalize_project_path(project_path)
    path, legacy = _storage_candidates(root)
    if os.path.isdir(path):
        return path
    if os.path.isdir(legacy) and _metadata_project_path(legacy) == root:
        return legacy
    return path

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
