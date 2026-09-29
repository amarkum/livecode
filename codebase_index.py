from __future__ import annotations

import ast
import json
import os
import re
import tempfile
import threading
import time
import warnings
from typing import Any

from livecode.project_store import project_file, project_key
from livecode.workspace import INDEX_TTL_S, index_generation, iter_project_files, resolve_safe_path

MAX_INDEXABLE_SIZE = 2 * 1024 * 1024
SYMBOL_EXTENSIONS = frozenset({".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})

_managers: dict[str, "CodebaseIndexManager"] = {}
_lock = threading.Lock()

JS_DECL_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?P<kw>function\s*\*?|class|interface|type|enum|const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)"
    r"(?P<rest>.*)$",
    re.MULTILINE,
)
_JS_FUNCTION_VALUE_RE = re.compile(r"^\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)")
_JS_CLASS_VALUE_RE = re.compile(r"^\s*=\s*class\b")
_JS_KIND = {"class": "class", "interface": "interface", "type": "type", "enum": "enum"}

def _project_hash(project_path: str) -> str:
    return project_key(project_path)

def _symbol_cache_path(project_path: str) -> str:
    path = project_file(project_path, "symbols", "symbols.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _symbol_file_mtimes(project_path: str) -> dict[str, int]:
    root = os.path.abspath(os.path.expanduser(project_path))
    mtimes: dict[str, int] = {}
    for rel, name, entry in iter_project_files(root):
        if os.path.splitext(name)[1].lower() not in SYMBOL_EXTENSIONS:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_size <= MAX_INDEXABLE_SIZE:
            mtimes[rel] = st.st_mtime_ns
    return mtimes


_PY_KINDS = {ast.ClassDef: "class", ast.FunctionDef: "function", ast.AsyncFunctionDef: "async_function"}


def _parse_python_symbols(content: str, rel_path: str) -> list[dict[str, Any]]:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=SyntaxWarning)
            tree = ast.parse(content, filename=rel_path)
    except (SyntaxError, ValueError):
        return []
    symbols: list[dict[str, Any]] = []
    stack: list[tuple[ast.AST, tuple[str, ...]]] = [(tree, ())]
    while stack:
        node, scope = stack.pop()
        for child in ast.iter_child_nodes(node):
            kind = _PY_KINDS.get(type(child))
            if kind:
                symbols.append({
                    "name": child.name,
                    "kind": kind,
                    "file": rel_path,
                    "start_line": child.lineno,
                    "end_line": getattr(child, "end_lineno", child.lineno),
                    "parent_scope": ".".join(scope),
                })
                stack.append((child, scope + (child.name,)))
            elif isinstance(child, (ast.stmt, ast.excepthandler)) or type(child).__name__ == "match_case":
                stack.append((child, scope))
    symbols.sort(key=lambda s: (s["start_line"], s["name"]))
    return symbols

_JS_NOT_NAMES = frozenset({"extends", "implements", "from", "of", "in", "as"})


def _parse_js_symbols(content: str, rel_path: str) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    line, counted_to = 1, 0
    for m in JS_DECL_RE.finditer(content):
        if m.group("name") in _JS_NOT_NAMES:
            continue
        kw = m.group("kw").replace(" ", "")
        if kw in ("const", "let", "var"):
            if m.group("indent"):
                continue
            rest = m.group("rest")
            kind = "function" if _JS_FUNCTION_VALUE_RE.match(rest) else ("class" if _JS_CLASS_VALUE_RE.match(rest) else "variable")
        elif kw.startswith("function"):
            kind = "function"
        else:
            kind = _JS_KIND[kw]
        line += content.count("\n", counted_to, m.start())
        counted_to = m.start()
        symbols.append({
            "name": m.group("name"),
            "kind": kind,
            "file": rel_path,
            "start_line": line,
            "end_line": line,
            "parent_scope": "",
        })
    return symbols

def _parse_file_symbols(rel_path: str, content: str) -> list[dict[str, Any]]:
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in (".py", ".pyi"):
        return _parse_python_symbols(content, rel_path)
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return _parse_js_symbols(content, rel_path)
    return []


def _read_symbols(full: str, rel: str) -> list[dict[str, Any]] | None:
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    return _parse_file_symbols(rel, content)


_KIND_RANK = {"class": 0, "interface": 0, "type": 1, "enum": 1, "function": 1, "async_function": 1, "variable": 3}


class CodebaseIndexManager:

    def __init__(self, project_path: str) -> None:
        self.project_path = os.path.abspath(os.path.expanduser(project_path))
        self.symbols: list[dict[str, Any]] = []
        self.file_mtimes: dict[str, int] = {}
        self._by_file: dict[str, list[dict[str, Any]]] = {}
        self.indexed_at = 0.0
        self._scanned_at = 0.0
        self._scanned_generation = -1
        self._loaded_cache = False
        self._mutate_lock = threading.RLock()

    def build(self, *, force: bool = False) -> dict[str, Any]:
        with self._mutate_lock:
            return self._build_locked(force=force)

    def _load_cache(self) -> None:
        self._loaded_cache = True
        try:
            with open(_symbol_cache_path(self.project_path), "r", encoding="utf-8") as f:
                cached = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        mtimes = cached.get("file_mtimes") or {}
        if cached.get("project_path") != self.project_path or cached.get("version") != 2 or not isinstance(mtimes, dict):
            return
        by_file: dict[str, list[dict[str, Any]]] = {}
        for sym in cached.get("symbols") or []:
            by_file.setdefault(str(sym.get("file") or ""), []).append(sym)
        self._by_file = by_file
        self.file_mtimes = {str(k): int(v) for k, v in mtimes.items()}
        self.indexed_at = float(cached.get("indexed_at") or 0)

    def _build_locked(self, *, force: bool) -> dict[str, Any]:
        generation = index_generation()
        fresh = time.monotonic() - self._scanned_at < INDEX_TTL_S and generation == self._scanned_generation
        if not force and fresh and self._scanned_at:
            return self.stats()
        if not self._loaded_cache and not force:
            self._load_cache()
        current = _symbol_file_mtimes(self.project_path)
        by_file = {} if force else dict(self._by_file)
        changed = False
        for rel in list(by_file):
            if rel not in current:
                del by_file[rel]
                changed = True
        for rel, mtime in list(current.items()):
            if not force and rel in by_file and self.file_mtimes.get(rel) == mtime:
                continue
            parsed = _read_symbols(os.path.join(self.project_path, rel), rel)
            if parsed is None:
                current.pop(rel, None)
                by_file.pop(rel, None)
                continue
            by_file[rel] = parsed
            changed = True
        self._scanned_at = time.monotonic()
        self._scanned_generation = generation
        if changed or force or current != self.file_mtimes:
            self._by_file = by_file
            self.file_mtimes = current
            self.symbols = [sym for rel in sorted(by_file) for sym in by_file[rel]]
            self.indexed_at = time.time()
            self._persist()
        elif not self.symbols and by_file:
            self.symbols = [sym for rel in sorted(by_file) for sym in by_file[rel]]
        return self.stats()

    def _persist(self) -> None:
        path = _symbol_cache_path(self.project_path)
        payload = {
            "version": 2,
            "project_path": self.project_path,
            "indexed_at": self.indexed_at,
            "symbols": self.symbols,
            "file_mtimes": self.file_mtimes,
        }
        try:
            fd, tmp = tempfile.mkstemp(prefix=".symbols-", suffix=".tmp", dir=os.path.dirname(path))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def update_file(self, abs_path: str) -> None:
        rel = os.path.relpath(abs_path, self.project_path).replace("\\", "/")
        if rel.startswith("../") or os.path.splitext(rel)[1].lower() not in SYMBOL_EXTENSIONS:
            return
        with self._mutate_lock:
            by_file = dict(self._by_file)
            mtimes = dict(self.file_mtimes)
            parsed = _read_symbols(abs_path, rel) if os.path.isfile(abs_path) else None
            if parsed is None:
                by_file.pop(rel, None)
                mtimes.pop(rel, None)
            else:
                by_file[rel] = parsed
                try:
                    mtimes[rel] = os.stat(abs_path).st_mtime_ns
                except OSError:
                    pass
            self._by_file = by_file
            self.file_mtimes = mtimes
            self.symbols = [sym for key in sorted(by_file) for sym in by_file[key]]
            self._persist()

    def stats(self) -> dict[str, Any]:
        langs: dict[str, int] = {}
        for s in self.symbols:
            ext = os.path.splitext(s.get("file", ""))[1].lower()
            langs[ext or "?"] = langs.get(ext or "?", 0) + 1
        return {
            "symbol_count": len(self.symbols),
            "file_count": len(self.file_mtimes),
            "languages": langs,
            "indexed_at": self.indexed_at,
        }

    def find_symbol(self, name: str, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        needle = (name or "").strip()
        if not needle:
            return []
        scope_needle = ""
        if "." in needle:
            scope_needle, needle = needle.rsplit(".", 1)
        lower = needle.lower()
        scored: list[tuple[tuple, dict[str, Any]]] = []
        for s in self.symbols:
            sym_name = s.get("name", "")
            if lower not in sym_name.lower():
                continue
            if kind and s.get("kind") != kind:
                continue
            if scope_needle and not str(s.get("parent_scope") or "").endswith(scope_needle):
                continue
            if sym_name == needle:
                match = 0
            elif sym_name.lower() == lower:
                match = 1
            elif sym_name.startswith(needle):
                match = 2
            elif sym_name.lower().startswith(lower):
                match = 3
            elif needle in sym_name:
                match = 4
            else:
                match = 5
            key = (match, _KIND_RANK.get(str(s.get("kind")), 2), len(sym_name), s.get("file", ""), s.get("start_line", 0))
            scored.append((key, s))
        scored.sort(key=lambda item: item[0])
        return [s for _key, s in scored[:limit]]

    def find_references(self, name: str, limit: int = 40) -> list[dict[str, Any]]:
        needle = (name or "").strip()
        if not needle:
            return []
        refs: list[dict[str, Any]] = []
        pattern = re.compile(r"\b" + re.escape(needle) + r"\b")
        for rel in list(self.file_mtimes):
            full, _ = resolve_safe_path(self.project_path, rel)
            if not full or not os.path.isfile(full):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            refs.append({"file": rel, "line": i, "text": line.strip()[:120]})
                            if len(refs) >= limit:
                                return refs
            except OSError:
                continue
        return refs

    def list_symbols(self, path: str = "", limit: int = 80) -> list[dict[str, Any]]:
        prefix = (path or "").strip().replace("\\", "/")
        out = []
        for s in self.symbols:
            fp = s.get("file", "")
            if prefix and not fp.startswith(prefix):
                continue
            out.append(s)
            if len(out) >= limit:
                break
        return out

def get_codebase_index(project_path: str, *, force: bool = False) -> CodebaseIndexManager:
    key = _project_hash(project_path)
    with _lock:
        if key not in _managers:
            _managers[key] = CodebaseIndexManager(project_path)
        mgr = _managers[key]
    mgr.build(force=force)
    return mgr
