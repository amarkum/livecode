from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading
import time
from typing import Any

from .session import session_dir

PENDING_INDEX_FILE = "pending_changes.json"
PENDING_BLOB_DIR = "pending_changes"
MAX_BASELINE_BYTES = 4 * 1024 * 1024
MAX_STAT_BYTES = 1024 * 1024

_INDEX_LOCK = threading.RLock()


def _index_path(project_path: str, session_id: str, *, create: bool = False) -> str:
    return os.path.join(session_dir(project_path, session_id, create=create), PENDING_INDEX_FILE)


def _blob_path(project_path: str, session_id: str, blob: str, *, create: bool = False) -> str:
    folder = os.path.join(session_dir(project_path, session_id, create=create), PENDING_BLOB_DIR)
    if create:
        os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, blob)


def _load_index(project_path: str, session_id: str) -> dict[str, Any]:
    try:
        path = _index_path(project_path, session_id)
    except ValueError:
        return {}
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load(project_path: str, session_id: str) -> dict[str, dict[str, Any]]:
    files = _load_index(project_path, session_id).get("files")
    return files if isinstance(files, dict) else {}


def _created_dirs(project_path: str, session_id: str) -> list[str]:
    dirs = _load_index(project_path, session_id).get("created_dirs")
    return [str(d) for d in dirs] if isinstance(dirs, list) else []


def _save(
    project_path: str,
    session_id: str,
    files: dict[str, dict[str, Any]],
    created_dirs: list[str] | None = None,
) -> None:
    path = _index_path(project_path, session_id, create=True)
    dirs = _created_dirs(project_path, session_id) if created_dirs is None else created_dirs
    if not files and not dirs:
        if os.path.isfile(path):
            os.remove(path)
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"files": files, "created_dirs": dirs}, f)
    os.replace(tmp, path)


def _missing_parent_dirs(path: str) -> list[str]:
    out: list[str] = []
    parent = os.path.dirname(os.path.abspath(path))
    while parent and not os.path.exists(parent):
        out.append(parent)
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    return out


def _remove_empty_dirs(dirs: list[str]) -> list[str]:
    left: list[str] = []
    for folder in sorted(set(dirs), key=lambda d: d.count(os.sep), reverse=True):
        try:
            os.rmdir(folder)
        except FileNotFoundError:
            continue
        except OSError:
            left.append(folder)
    return left


def _read_bytes(path: str) -> bytes | None:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_BASELINE_BYTES:
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def snapshot_file(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        missing = _missing_parent_dirs(path)
        return {"existed": False, "missing_dirs": missing} if missing else {"existed": False}
    raw = _read_bytes(path)
    if raw is None:
        return {"existed": True, "untracked": True}
    return {"existed": True, "content": raw}


def has_baseline(project_path: str, session_id: str | None, abs_path: str) -> bool:
    if not session_id or not abs_path:
        return False
    return os.path.abspath(abs_path) in _load(project_path, session_id)


def record_baseline(project_path: str, session_id: str | None, abs_path: str, snapshot: dict[str, Any]) -> None:
    if not session_id or not abs_path or not snapshot:
        return
    key = os.path.abspath(abs_path)
    with _INDEX_LOCK:
        files = _load(project_path, session_id)
        created_dirs = _created_dirs(project_path, session_id)
        for folder in snapshot.get("missing_dirs") or []:
            if folder not in created_dirs:
                created_dirs.append(folder)
        if key in files:
            _save(project_path, session_id, files, created_dirs)
            return
        entry: dict[str, Any] = {"existed": bool(snapshot.get("existed"))}
        if snapshot.get("untracked"):
            entry["untracked"] = True
        elif entry["existed"]:
            blob = hashlib.sha1(key.encode("utf-8")).hexdigest() + ".bin"
            with open(_blob_path(project_path, session_id, blob, create=True), "wb") as f:
                f.write(snapshot.get("content") or b"")
            entry["blob"] = blob
        files[key] = entry
        _save(project_path, session_id, files, created_dirs)


def _baseline_bytes(project_path: str, session_id: str, entry: dict[str, Any]) -> bytes | None:
    if not entry.get("existed"):
        return b""
    blob = entry.get("blob")
    if not blob:
        return None
    return _read_bytes(_blob_path(project_path, session_id, blob))


def _line_stats(before: bytes, after: bytes) -> tuple[int, int]:
    if len(before) > MAX_STAT_BYTES or len(after) > MAX_STAT_BYTES:
        return 0, 0
    a = before.decode("utf-8", errors="replace").splitlines()
    b = after.decode("utf-8", errors="replace").splitlines()
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def list_pending_changes(project_path: str, session_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path, entry in _load(project_path, session_id).items():
        exists_now = os.path.isfile(path)
        before = _baseline_bytes(project_path, session_id, entry)
        now = _read_bytes(path) if exists_now else b""
        if not entry.get("existed") and not exists_now:
            continue
        if before is not None and now is not None and before == now and exists_now == bool(entry.get("existed")):
            continue
        added, removed = _line_stats(before, now) if before is not None and now is not None else (0, 0)
        out.append({
            "path": path,
            "created": not entry.get("existed"),
            "deleted": bool(entry.get("existed")) and not exists_now,
            "additions": added,
            "deletions": removed,
            "can_undo": before is not None,
        })
    out.sort(key=lambda item: item["path"])
    return out


def _select(files: dict[str, dict[str, Any]], paths: list[str] | None) -> list[str]:
    if not paths:
        return list(files.keys())
    wanted = {os.path.abspath(str(p)) for p in paths if p}
    return [p for p in files if p in wanted]


def _drop(project_path: str, session_id: str, files: dict[str, dict[str, Any]], path: str) -> None:
    entry = files.pop(path, None) or {}
    blob = entry.get("blob")
    if blob:
        try:
            os.remove(_blob_path(project_path, session_id, blob))
        except OSError:
            pass


def keep_pending_changes(project_path: str, session_id: str, paths: list[str] | None = None) -> int:
    with _INDEX_LOCK:
        files = _load(project_path, session_id)
        chosen = _select(files, paths)
        for path in chosen:
            _drop(project_path, session_id, files, path)
        created_dirs = [] if not files else None
        _save(project_path, session_id, files, created_dirs)
        return len(chosen)


def _write_back(path: str, raw: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)


def undo_pending_changes(project_path: str, session_id: str, paths: list[str] | None = None) -> dict[str, list[str]]:
    with _INDEX_LOCK:
        files = _load(project_path, session_id)
        restored: list[str] = []
        removed: list[str] = []
        failed: list[str] = []
        for path in _select(files, paths):
            entry = files[path]
            try:
                if entry.get("existed"):
                    raw = _baseline_bytes(project_path, session_id, entry)
                    if raw is None:
                        failed.append(path)
                        continue
                    _write_back(path, raw)
                    restored.append(path)
                else:
                    if os.path.isfile(path):
                        os.remove(path)
                    removed.append(path)
            except OSError:
                failed.append(path)
                continue
            _drop(project_path, session_id, files, path)
        created_dirs = _created_dirs(project_path, session_id)
        if removed and created_dirs:
            created_dirs = _remove_empty_dirs(created_dirs)
        _save(project_path, session_id, files, created_dirs if files else [])
        return {"restored": restored, "removed": removed, "failed": failed}


CHECKPOINT_INDEX_FILE = "checkpoints.json"


def _checkpoint_index_path(project_path: str, session_id: str, *, create: bool = False) -> str:
    return os.path.join(session_dir(project_path, session_id, create=create), CHECKPOINT_INDEX_FILE)


def _load_checkpoints(project_path: str, session_id: str) -> list[dict[str, Any]]:
    try:
        path = _checkpoint_index_path(project_path, session_id)
    except ValueError:
        return []
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    turns = data.get("turns") if isinstance(data, dict) else None
    return turns if isinstance(turns, list) else []


def _save_checkpoints(project_path: str, session_id: str, turns: list[dict[str, Any]]) -> None:
    path = _checkpoint_index_path(project_path, session_id, create=True)
    if not turns:
        if os.path.isfile(path):
            os.remove(path)
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"turns": turns}, f)
    os.replace(tmp, path)


def record_turn_baseline(
    project_path: str,
    session_id: str | None,
    turn_id: str | None,
    user_index: int | None,
    abs_path: str,
    snapshot: dict[str, Any],
) -> None:
    if not session_id or not turn_id or user_index is None or not abs_path or not snapshot:
        return
    key = os.path.abspath(abs_path)
    with _INDEX_LOCK:
        turns = _load_checkpoints(project_path, session_id)
        turn = next((t for t in turns if t.get("turn_id") == turn_id), None)
        if turn is None:
            turn = {"turn_id": turn_id, "user_index": int(user_index), "created_at": time.time(), "files": {}}
            turns.append(turn)
        if key in turn["files"]:
            return
        entry: dict[str, Any] = {"existed": bool(snapshot.get("existed"))}
        if snapshot.get("untracked"):
            entry["untracked"] = True
        elif entry["existed"]:
            blob = "cp_" + hashlib.sha1(f"{turn_id}:{key}".encode("utf-8")).hexdigest() + ".bin"
            with open(_blob_path(project_path, session_id, blob, create=True), "wb") as f:
                f.write(snapshot.get("content") or b"")
            entry["blob"] = blob
        if snapshot.get("missing_dirs"):
            entry["missing_dirs"] = list(snapshot["missing_dirs"])
        turn["files"][key] = entry
        _save_checkpoints(project_path, session_id, turns)


def list_checkpoints(project_path: str, session_id: str) -> list[dict[str, Any]]:
    out = []
    for turn in sorted(_load_checkpoints(project_path, session_id), key=lambda t: int(t.get("user_index") or 0)):
        files = turn.get("files") or {}
        out.append({
            "turn_id": turn.get("turn_id"),
            "user_index": int(turn.get("user_index") or 0),
            "created_at": turn.get("created_at"),
            "files": [{"path": p, "created": not e.get("existed")} for p, e in sorted(files.items())],
        })
    return out


def restore_checkpoint(project_path: str, session_id: str, user_index: int) -> dict[str, list[str]]:
    with _INDEX_LOCK:
        turns = _load_checkpoints(project_path, session_id)
        later = sorted(
            (t for t in turns if int(t.get("user_index") or 0) >= int(user_index)),
            key=lambda t: int(t.get("user_index") or 0),
        )
        earliest: dict[str, dict[str, Any]] = {}
        for turn in later:
            for path, entry in (turn.get("files") or {}).items():
                earliest.setdefault(path, entry)
        restored: list[str] = []
        removed: list[str] = []
        failed: list[str] = []
        dirs_to_prune: list[str] = []
        for path, entry in sorted(earliest.items()):
            try:
                if entry.get("existed"):
                    raw = _baseline_bytes(project_path, session_id, entry)
                    if raw is None:
                        failed.append(path)
                        continue
                    _write_back(path, raw)
                    restored.append(path)
                else:
                    if os.path.isfile(path):
                        os.remove(path)
                    removed.append(path)
                    dirs_to_prune.extend(entry.get("missing_dirs") or [])
            except OSError:
                failed.append(path)
        if dirs_to_prune:
            _remove_empty_dirs(dirs_to_prune)
        if not failed:
            for turn in later:
                for entry in (turn.get("files") or {}).values():
                    blob = entry.get("blob")
                    if blob:
                        try:
                            os.remove(_blob_path(project_path, session_id, blob))
                        except OSError:
                            pass
            _save_checkpoints(project_path, session_id, [t for t in turns if t not in later])
        return {"restored": restored, "removed": removed, "failed": failed}
