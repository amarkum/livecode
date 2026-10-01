from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any

from .gitignore import ProjectIgnore
from .project_store import project_file
from .workspace_config import LivecodeWorkspace, WorkspaceFolder, workspace_folder_aliases, workspace_from_payload, workspace_name_key

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", "target", ".next", ".nuxt", "coverage",
}
SKIP_EXT = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".zip", ".tar", ".gz"}
MAX_FILES = 8000
MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_FILESIZE_RG = "2M"
LAYOUT_MAX_CHARS = 10_000
LAYOUT_MAX_DEPTH = 12
LAYOUT_MAX_DIRS = 2000
GLOB_MAX_RESULTS = 100
FIND_FILES_MAX_RESULTS = 50

def _index_path(project_path: str) -> str:
    path = project_file(project_path, "index", "workspace.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def resolve_safe_path(project_path: str, rel_path: str) -> tuple[str | None, str | None]:
    root = os.path.abspath(os.path.expanduser(project_path))
    if not os.path.isdir(root):
        return None, f"Project path does not exist: {project_path}"
    raw = str(rel_path or "").replace("\\", "/").strip()
    if not raw or raw == ".":
        return root, ""
    expanded = os.path.expanduser(raw)
    full = os.path.abspath(expanded) if os.path.isabs(expanded) else os.path.abspath(os.path.join(root, expanded))
    rel = os.path.relpath(full, root).replace("\\", "/")
    return full, "" if rel == "." else rel


def workspace_folder_prefix(folder: WorkspaceFolder) -> str:
    return str(folder.name or os.path.basename(folder.path) or "workspace").strip()


def workspace_display_path(folder: WorkspaceFolder, rel_path: str = "") -> str:
    prefix = workspace_folder_prefix(folder)
    rel = (rel_path or "").replace("\\", "/").strip("/")
    return f"{prefix}/{rel}" if rel else prefix


def workspace_folder_label(workspace: LivecodeWorkspace, folder: WorkspaceFolder) -> str:
    if len(workspace.folders) <= 1:
        return ""
    return workspace_folder_prefix(folder)


def resolve_workspace_path(
    project_path: str,
    rel_path: str,
    workspace: LivecodeWorkspace | None = None,
) -> dict:
    ws = workspace or workspace_from_payload(project_path)
    raw = str(rel_path or "").strip().replace("\\", "/")
    raw = raw[2:] if raw.startswith("./") else raw
    if not raw or raw in (".", "/"):
        folder = ws.folders[0]
        return {"folder": folder, "root": folder.path, "rel": "", "full": folder.path, "display_path": workspace_display_path(folder, "")}
    clean = raw.lstrip("/")
    parts = [part for part in clean.split("/") if part]
    if not parts:
        folder = ws.folders[0]
        return {"folder": folder, "root": folder.path, "rel": "", "full": folder.path, "display_path": workspace_display_path(folder, "")}

    if len(ws.folders) > 1:
        first = parts[0]
        first_key = workspace_name_key(first)
        named_matches = [folder for folder in ws.folders if workspace_name_key(folder.name) == first_key]
        matches = named_matches
        if not matches:
            matches = [folder for folder in ws.folders if workspace_name_key(os.path.basename(folder.path)) == first_key]
        if len(matches) == 1:
            folder = matches[0]
            rest = "/".join(parts[1:])
            full, err = resolve_safe_path(folder.path, rest)
            if full is None:
                return {"error": err}
            rel = os.path.relpath(full, folder.path).replace("\\", "/")
            rel = "" if rel == "." else rel
            return {"folder": folder, "root": folder.path, "rel": rel, "full": full, "display_path": workspace_display_path(folder, rel)}
        if len(matches) > 1:
            names = ", ".join(workspace_folder_prefix(folder) for folder in matches)
            return {"error": f"Workspace folder name is ambiguous: {first} matches {names}"}

    folder = ws.folders[0]
    is_real_absolute = os.path.isabs(os.path.expanduser(raw))
    full, err = resolve_safe_path(folder.path, raw if is_real_absolute else clean)
    if full is None:
        return {"error": err}
    rel = os.path.relpath(full, folder.path).replace("\\", "/")
    rel = "" if rel == "." else rel
    return {"folder": folder, "root": folder.path, "rel": rel, "full": full, "display_path": workspace_display_path(folder, rel) if len(ws.folders) > 1 else rel}


def workspace_roots(project_path: str, workspace_payload: dict | None = None) -> LivecodeWorkspace:
    return workspace_from_payload(project_path, workspace_payload)


def _stat_mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _load_gitignore_patterns(project_path: str) -> ProjectIgnore:
    return ProjectIgnore(os.path.abspath(os.path.expanduser(project_path)))

def _ignored(name: str, rel: str, gitignore: ProjectIgnore, is_dir: bool = False) -> bool:
    if name.startswith(".") and name not in (".env", ".env.example"):
        return True
    rel = rel.replace("\\", "/")
    for p in rel.split("/"):
        if p in SKIP_DIRS:
            return True
    return gitignore.ignored_entry(rel, is_dir)

def path_blocked_for_edit(project_path: str, safe_rel: str) -> str | None:
    rel = (safe_rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return None
    parts = rel.split("/")
    if any(part == ".." for part in parts):
        return None
    for p in parts:
        if p in SKIP_DIRS:
            return (
                f"Refusing to edit gitignored or excluded path: {rel} "
                f"(excluded directory '{p}')"
            )
    root = os.path.abspath(os.path.expanduser(project_path))
    if _load_gitignore_patterns(root).ignored_path(rel, is_dir=os.path.isdir(os.path.join(root, rel))):
        return f"Refusing to edit gitignored path: {rel}"
    return None

def _dirs_unchanged(root: str, dirs: dict[str, int]) -> bool:
    if not dirs:
        return False
    for rel, mtime in dirs.items():
        if _stat_mtime(os.path.join(root, rel) if rel else root) != mtime:
            return False
    return True

def _gitignore_stamps(root: str, gitignore: ProjectIgnore) -> dict[str, int]:
    return {rel: _stat_mtime(os.path.join(root, rel)) for rel in gitignore.sources()}

def _stamps_unchanged(root: str, stamps: Any) -> bool:
    if not isinstance(stamps, dict) or not stamps:
        return False
    for rel, mtime in stamps.items():
        if _stat_mtime(os.path.join(root, rel)) != mtime:
            return False
    return True

def _rel_dir(root: str, path: str) -> str:
    rel = os.path.relpath(path, root).replace("\\", "/")
    return "" if rel == "." else rel

def _empty_index(root: str) -> dict:
    return {
        "project_path": root,
        "indexed_at": time.time(),
        "from_cache": False,
        "file_count": 0,
        "top_dirs": [],
        "ext_counts": {},
        "sample_files": [],
        "files": [],
        "truncated": False,
        "missing": True,
    }

INDEX_TTL_S = 3.0
_INDEX_LOCK = threading.Lock()
_INDEX_GENERATION = 0
_INDEX_SEEN: dict[str, int] = {}

def invalidate_workspace_index() -> None:
    global _INDEX_GENERATION
    with _INDEX_LOCK:
        _INDEX_GENERATION += 1

def index_generation() -> int:
    return _INDEX_GENERATION

def iter_project_files(root: str, gitignore: ProjectIgnore | None = None) -> Iterator[tuple[str, str, os.DirEntry]]:
    root = os.path.abspath(os.path.expanduser(root))
    rules = gitignore or _load_gitignore_patterns(root)
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        rules.enter_dir(rel_dir)
        try:
            with os.scandir(os.path.join(root, rel_dir) if rel_dir else root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        subdirs: list[str] = []
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and entry.is_symlink() and entry.is_dir():
                    continue
            except OSError:
                continue
            if _ignored(entry.name, rel, rules, is_dir=is_dir):
                continue
            if is_dir:
                subdirs.append(rel)
            else:
                yield rel, entry.name, entry
        stack.extend(reversed(subdirs))

def build_workspace_index(project_path: str, force: bool = False) -> dict:
    root = os.path.abspath(os.path.expanduser(project_path))
    if not os.path.isdir(root):
        return _empty_index(root)
    cache_file = _index_path(root)
    generation = _INDEX_GENERATION
    if not force and os.path.isfile(cache_file):
        try:
            with open(cache_file, "r", errors="replace") as f:
                cached = json.load(f)
            dirs = cached.get("dirs")
            with _INDEX_LOCK:
                seen = _INDEX_SEEN.get(root)
            if (
                cached.get("project_path") == root
                and _stamps_unchanged(root, cached.get("gitignores"))
                and isinstance(dirs, dict)
                and _dirs_unchanged(root, dirs)
                and (seen is None or seen == generation)
            ):
                cached["from_cache"] = True
                with _INDEX_LOCK:
                    _INDEX_SEEN[root] = generation
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    gitignore = _load_gitignore_patterns(root)
    files: list[dict] = []
    ext_counts: dict[str, int] = {}
    top_dirs: list[str] = []
    dir_mtimes: dict[str, int] = {}
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = _rel_dir(root, dirpath)
        gitignore.enter_dir(rel_dir)
        dir_mtimes[rel_dir] = _stat_mtime(dirpath)
        kept: list[str] = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS or d.startswith("."):
                continue
            child = f"{rel_dir}/{d}" if rel_dir else d
            if gitignore.ignored_entry(child, True):
                continue
            kept.append(d)
        dirnames[:] = kept
        if not rel_dir:
            top_dirs = [d + "/" for d in kept][:30]
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _ignored(name, rel, gitignore):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in SKIP_EXT:
                continue
            try:
                size = os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
            if size > MAX_FILE_SIZE:
                continue
            if len(files) >= MAX_FILES:
                truncated = True
                break
            files.append({"rel": rel, "ext": ext, "size": size})
            ext_counts[ext or "(no ext)"] = ext_counts.get(ext or "(no ext)", 0) + 1
        if truncated:
            break

    index = {
        "project_path": root,
        "dirs": dir_mtimes,
        "gitignores": _gitignore_stamps(root, gitignore),
        "indexed_at": time.time(),
        "from_cache": False,
        "file_count": len(files),
        "truncated": truncated,
        "top_dirs": top_dirs,
        "ext_counts": dict(sorted(ext_counts.items(), key=lambda x: -x[1])[:15]),
        "sample_files": [f["rel"] for f in files[:40]],
        "files": files,
    }
    temp_file = f"{cache_file}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_file, "w") as f:
            json.dump(index, f, indent=0)
        os.replace(temp_file, cache_file)
    except OSError:
        try:
            os.remove(temp_file)
        except OSError:
            pass
    with _INDEX_LOCK:
        _INDEX_SEEN[root] = generation
    return index

_LAYOUT_TREE_CACHE: dict[tuple, tuple[dict[str, int], dict[str, int], int, str]] = {}


def build_project_layout_tree(
    project_path: str,
    *,
    max_chars: int = LAYOUT_MAX_CHARS,
    max_depth: int = LAYOUT_MAX_DEPTH,
    max_dirs: int = LAYOUT_MAX_DIRS,
) -> str:
    root = os.path.abspath(os.path.expanduser(project_path))
    if not os.path.isdir(root):
        return ""
    cache_key = (root, max_chars, max_depth, max_dirs)
    generation = _INDEX_GENERATION
    cached = _LAYOUT_TREE_CACHE.get(cache_key)
    if (
        cached is not None
        and cached[2] == generation
        and _stamps_unchanged(root, cached[1])
        and _dirs_unchanged(root, cached[0])
    ):
        return cached[3]
    gitignore = _load_gitignore_patterns(root)
    lines: list[str] = [root]
    chars = len(root)
    dirs_visited = 0
    seen_dirs: dict[str, int] = {}

    def _note(path: str) -> None:
        seen_dirs.setdefault(_rel_dir(root, path), _stat_mtime(path))

    def _walk(dirpath: str, prefix: str, depth: int) -> None:
        nonlocal chars, dirs_visited
        if depth > max_depth or chars >= max_chars or dirs_visited >= max_dirs:
            return
        try:
            names = sorted(os.listdir(dirpath))
        except OSError:
            return
        _note(dirpath)
        gitignore.enter_dir(_rel_dir(root, dirpath))
        dirs: list[str] = []
        files: list[str] = []
        for name in names:
            if name.startswith(".") and name not in (".env", ".env.example"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if os.path.isdir(full):
                if name in SKIP_DIRS:
                    continue
                if _ignored(name, rel, gitignore, True):
                    continue
                dirs.append(name)
            else:
                if _ignored(name, rel, gitignore):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in SKIP_EXT:
                    continue
                files.append(name)

        for name in dirs:
            if dirs_visited >= max_dirs or chars >= max_chars:
                break
            dirs_visited += 1
            line = f"{prefix}{name}/"
            if chars + len(line) + 1 > max_chars:
                return
            lines.append(line)
            chars += len(line) + 1
            subpath = os.path.join(dirpath, name)
            sub_names: list[str] = []
            gitignore.enter_dir(_rel_dir(root, subpath))
            try:
                for sn in os.listdir(subpath):
                    if sn.startswith("."):
                        continue
                    sp = os.path.join(subpath, sn)
                    srel = os.path.relpath(sp, root).replace("\\", "/")
                    if os.path.isdir(sp):
                        if sn in SKIP_DIRS or _ignored(sn, srel, gitignore, True):
                            continue
                        sub_names.append(sn)
                    else:
                        if _ignored(sn, srel, gitignore):
                            continue
                        ext = os.path.splitext(sn)[1].lower()
                        if ext not in SKIP_EXT:
                            sub_names.append(sn)
                _note(subpath)
            except OSError:
                sub_names = []
            if sub_names and depth < max_depth:
                _walk(subpath, prefix + "  ", depth + 1)
            elif len(sub_names) > 0:
                collapsed = f"{prefix}  [+{len(sub_names)} items]"
                if chars + len(collapsed) + 1 <= max_chars:
                    lines.append(collapsed)
                    chars += len(collapsed) + 1

        for name in files:
            if chars >= max_chars:
                break
            line = f"{prefix}{name}"
            if chars + len(line) + 1 > max_chars:
                break
            lines.append(line)
            chars += len(line) + 1

    _walk(root, "  ", 0)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    _LAYOUT_TREE_CACHE[cache_key] = (seen_dirs, _gitignore_stamps(root, gitignore), generation, text)
    return text

def format_project_layout_block(project_path: str, tree: str | None = None) -> str:
    if tree is None:
        tree = build_project_layout_tree(project_path)
    if not tree:
        return ""
    return f"<project_layout>\n{tree}\n</project_layout>"

def index_summary_brief(index: dict, symbol_index: dict | None = None, max_chars: int = 800) -> str:
    if hasattr(index, "stats") and callable(index.stats):
        index = index.stats()
    if not index:
        return "No workspace index."
    lines = [
        f"Files: {index.get('file_count', 0)}",
        f"Top dirs: {', '.join(index.get('top_dirs', [])[:12])}",
    ]
    if symbol_index:
        lines.append(
            f"Symbols: {symbol_index.get('symbol_count', 0)} in "
            f"{symbol_index.get('file_count', 0)} files"
        )
    text = "\n".join(lines)
    return text[:max_chars]

def _ripgrep_available() -> bool:
    return bool(shutil.which("rg"))

def glob_files(
    project_path: str,
    pattern: str,
    *,
    path: str = "",
    max_results: int = GLOB_MAX_RESULTS,
) -> dict:
    root = os.path.abspath(os.path.expanduser(project_path))
    if not os.path.isdir(root):
        return {"error": f"Project path does not exist: {project_path}"}

    pat = (pattern or "").strip()
    if not pat:
        return {"error": "pattern is required"}

    cap = min(max(int(max_results or GLOB_MAX_RESULTS), 1), GLOB_MAX_RESULTS)
    search_root, err = resolve_safe_path(project_path, path or ".")
    if search_root is None:
        return {"error": err}
    if not os.path.isdir(search_root):
        return {"error": f"Directory not found: {path or '/'}"}

    rel_root = os.path.relpath(search_root, root).replace("\\", "/")
    if rel_root == ".":
        rel_root = ""

    entries: list[dict[str, str | float]] = []

    if _ripgrep_available():
        cmd = ["rg", "--files", "-g", pat, "--max-filesize", MAX_FILESIZE_RG]
        cmd.append(search_root)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=None,
            )
            lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
            for line in lines:
                full = line if os.path.isabs(line) else os.path.join(search_root, line)
                if not full.startswith(search_root + os.sep) and full != search_root:
                    continue
                if not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    mtime = 0.0
                entries.append({"path": rel, "mtime": mtime})
        except subprocess.TimeoutExpired:
            return {"error": "glob_files timed out (>30s). Narrow path or pattern."}
        except OSError as exc:
            return {"error": str(exc)}
    else:
        norm_pat = pat.lstrip("/")
        for dirpath, dirnames, filenames in os.walk(search_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace("\\", "/")
                base = os.path.basename(rel)
                if fnmatch.fnmatch(rel, norm_pat) or fnmatch.fnmatch(base, norm_pat):
                    try:
                        mtime = os.path.getmtime(full)
                    except OSError:
                        mtime = 0.0
                    entries.append({"path": rel, "mtime": mtime})

    entries.sort(key=lambda e: float(e.get("mtime") or 0), reverse=True)
    truncated = len(entries) > cap
    kept = entries[:cap]
    paths = [str(e["path"]) for e in kept]
    return {
        "success": True,
        "pattern": pat,
        "path": rel_root or "/",
        "file_count": len(paths),
        "truncated": truncated,
        "files": paths,
    }

def search_file_manifest(
    project_path: str,
    query: str,
    *,
    ext: str = "",
    path_prefix: str = "",
    max_results: int = FIND_FILES_MAX_RESULTS,
) -> dict:
    q = (query or "").strip().lower()
    if not q:
        return {"error": "query is required"}

    cap = min(max(int(max_results or FIND_FILES_MAX_RESULTS), 1), 100)
    ext_norm = (ext or "").strip().lower()
    if ext_norm and not ext_norm.startswith("."):
        ext_norm = f".{ext_norm}"
    prefix = (path_prefix or "").strip().replace("\\", "/").strip("/")
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"

    index = build_workspace_index(project_path)
    files = index.get("files") or []
    matches: list[dict[str, str | int]] = []

    for entry in files:
        rel = str(entry.get("rel") or "")
        rel_lower = rel.lower()
        if prefix and not rel_lower.startswith(prefix.lower()):
            continue
        if ext_norm and not rel_lower.endswith(ext_norm):
            continue
        if q not in rel_lower and q not in os.path.basename(rel_lower):
            continue
        matches.append({
            "path": rel,
            "size": int(entry.get("size") or 0),
            "ext": str(entry.get("ext") or ""),
        })
        if len(matches) >= cap:
            break

    return {
        "success": True,
        "query": query,
        "path_prefix": prefix.rstrip("/") if prefix else "",
        "ext": ext_norm,
        "match_count": len(matches),
        "truncated": len(matches) >= cap,
        "files": matches,
    }

def index_summary_text(index: dict, max_chars: int = 3500, symbol_index: dict | None = None) -> str:
    if not index:
        return "No workspace index available."
    lines = [
        f"Project root: {index.get('project_path', '')}",
        f"Indexed files: {index.get('file_count', 0)}",
        f"Top-level dirs: {', '.join(index.get('top_dirs', [])[:20])}",
        f"File types: {', '.join(f'{k}({v})' for k, v in list(index.get('ext_counts', {}).items())[:10])}",
    ]
    if symbol_index:
        lines.append(f"Symbol index: {symbol_index.get('symbol_count', 0)} symbols in {symbol_index.get('file_count', 0)} files")
        langs = symbol_index.get("languages") or {}
        if langs:
            lines.append("Symbol languages: " + ", ".join(f"{k}({v})" for k, v in list(langs.items())[:8]))
    sample = index.get("sample_files") or []
    if sample:
        lines.append("Sample paths: " + ", ".join(sample[:25]))
    text = "\n".join(lines)
    return text[:max_chars]
