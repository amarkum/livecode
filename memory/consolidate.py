from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from livecode.memory.flush import has_markdown_headers, is_no_reply
from livecode.memory.index import embed_missing_chunks, reindex_file, remove_path_chunks
from livecode.memory.storage import (
    memory_lock,
    memory_md_path,
    memory_root,
    read_memory_md,
    sessions_dir,
    write_text_atomic,
)

MIN_HOURS_BETWEEN_RUNS = 24.0
MIN_NEW_SESSION_LOGS = 3
MAX_INPUT_CHARS = 32_000
MAX_MEMORY_WRITE_CHARS = 12_000
LOCK_STALE_SECONDS = 600

_MARKER_NAME = ".consolidated"
_LOCK_NAME = ".consolidating"

CONSOLIDATE_SYSTEM_PROMPT = (
    "You are performing a reflective pass over project memory. Fold the recent "
    "session logs into one durable memory document so future sessions orient fast.\n\n"
    "You will receive the current memory document (may be empty) followed by recent "
    "session logs. Your job:\n\n"
    "1. **Merge** related information into coherent topic sections with `##` headers\n"
    "2. **Resolve contradictions** — if a newer session overrides an older fact, keep "
    "only the current truth\n"
    "3. **Convert** relative dates (\"yesterday\", \"last week\") to absolute dates\n"
    "4. **Discard** ephemera — greetings, tool-output noise, message/'tool result' "
    "counts, session metadata, 'current state' / 'next steps' sections, and generic "
    "user preferences (OS, shell, editor)\n"
    "5. **Preserve** decisions and their rationale, architecture, conventions, and "
    "problem/solution pairs\n"
    "6. **Generalize** task narratives into reusable mechanisms, rules, and root causes\n"
    "7. **Ground** file references in stable repo-relative paths; copy exact identifiers "
    "and numbers only when they appear verbatim in the input\n\n"
    "Respond with a single markdown document (only `##` topic headers, no preamble). "
    "If nothing in the logs is worth persisting, respond with exactly NO_REPLY."
)


def _marker_path(project_path: str) -> str:
    return os.path.join(memory_root(project_path, create=True), _MARKER_NAME)


def _lock_path(project_path: str) -> str:
    return os.path.join(memory_root(project_path, create=True), _LOCK_NAME)


def _last_run_epoch(project_path: str) -> float:
    try:
        with open(_marker_path(project_path), "r", encoding="utf-8") as f:
            return float((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _record_run(project_path: str) -> None:
    try:
        with open(_marker_path(project_path), "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def _session_logs_since(project_path: str, since_epoch: float) -> list[str]:
    root = sessions_dir(project_path, create=False)
    if not os.path.isdir(root):
        return []
    out: list[tuple[float, str]] = []
    for name in os.listdir(root):
        if not name.endswith(".md"):
            continue
        abs_path = os.path.join(root, name)
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            continue
        if mtime > since_epoch:
            out.append((mtime, abs_path))
    out.sort(key=lambda x: x[0])
    return [p for _, p in out]


def _acquire_lock(project_path: str) -> bool:
    path = _lock_path(project_path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(int(time.time())).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > LOCK_STALE_SECONDS:
                os.unlink(path)
                return _acquire_lock(project_path)
        except OSError:
            pass
        return False
    except OSError:
        return False


def _release_lock(project_path: str) -> None:
    try:
        os.unlink(_lock_path(project_path))
    except OSError:
        pass


def _file_stamp(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _build_prompt(project_path: str, log_paths: list[str]) -> tuple[str, list[str], str, dict[str, tuple[int, int] | None]]:
    parts: list[str] = []
    snapshot = read_memory_md(project_path)
    existing = snapshot.strip()
    if existing:
        parts.append("--- Current memory document (merge into this) ---\n\n" + existing)
    used: list[str] = []
    stamps: dict[str, tuple[int, int] | None] = {}
    budget = MAX_INPUT_CHARS - sum(len(p) for p in parts)
    for path in log_paths:
        stamp = _file_stamp(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read().strip()
        except OSError:
            continue
        if not body:
            used.append(path)
            stamps[path] = stamp
            continue
        block = f"--- Session log: {os.path.basename(path)} ---\n\n{body}"
        if len(block) > budget and used:
            break
        parts.append(block)
        used.append(path)
        stamps[path] = stamp
        budget -= len(block)
        if budget <= 0:
            break
    return "\n\n".join(parts), used, snapshot, stamps


def should_consolidate(
    project_path: str,
    *,
    min_hours: float = MIN_HOURS_BETWEEN_RUNS,
    min_new_logs: int = MIN_NEW_SESSION_LOGS,
) -> list[str]:
    last = _last_run_epoch(project_path)
    if time.time() - last < min_hours * 3600.0:
        return []
    logs = _session_logs_since(project_path, last)
    if len(logs) < min_new_logs:
        return []
    return logs


def run_consolidation(
    project_path: str,
    *,
    model: str,
    call_summarize: Callable[[str, list[dict[str, str]]], str],
    min_hours: float = MIN_HOURS_BETWEEN_RUNS,
    min_new_logs: int = MIN_NEW_SESSION_LOGS,
) -> dict[str, Any]:
    logs = should_consolidate(
        project_path, min_hours=min_hours, min_new_logs=min_new_logs
    )
    if not logs:
        return {"status": "skipped", "reason": "gate_closed"}
    if not _acquire_lock(project_path):
        return {"status": "skipped", "reason": "locked"}
    try:
        user_text, used, snapshot, stamps = _build_prompt(project_path, logs)
        if not user_text.strip() or not used:
            _record_run(project_path)
            return {"status": "skipped", "reason": "empty_input"}

        try:
            raw = call_summarize(
                model,
                [
                    {"role": "system", "content": CONSOLIDATE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
            ) or ""
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}

        text = raw.strip()
        if is_no_reply(text) or not has_markdown_headers(text):
            _record_run(project_path)
            return {"status": "skipped", "reason": "no_reply"}
        if len(text) > MAX_MEMORY_WRITE_CHARS:
            text = text[:MAX_MEMORY_WRITE_CHARS].rstrip() + "\n"

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        document = f"<!-- consolidated {stamp} -->\n\n{text}\n"
        md_path = memory_md_path(project_path, create=True)
        removed_logs = 0
        kept_logs = 0
        with memory_lock(project_path):
            current = read_memory_md(project_path)
            if current != snapshot:
                if not current.startswith(snapshot):
                    _record_run(project_path)
                    return {"status": "skipped", "reason": "memory_changed"}
                added = current[len(snapshot):].strip()
                if added:
                    document = document.rstrip() + "\n\n" + added + "\n"
            try:
                write_text_atomic(md_path, document)
            except OSError as exc:
                return {"status": "failed", "error": str(exc)}
            for path in used:
                if _file_stamp(path) != stamps.get(path):
                    kept_logs += 1
                    continue
                try:
                    os.unlink(path)
                    removed_logs += 1
                except OSError:
                    continue
                remove_path_chunks(project_path, f"sessions/{os.path.basename(path)}")

        reindex_file(project_path, md_path, "workspace", os.path.basename(md_path))

        embed_missing_chunks(project_path)
        _record_run(project_path)
        return {
            "status": "written",
            "path": md_path,
            "chars": len(text),
            "logs_consumed": removed_logs,
            "logs_kept": kept_logs,
        }
    finally:
        _release_lock(project_path)


def maybe_consolidate_memory(
    project_path: str,
    *,
    model: str,
    call_summarize: Callable[[str, list[dict[str, str]]], str] | None,
    logger: Any = None,
    min_hours: float | None = None,
) -> dict[str, Any] | None:
    if not call_summarize or not project_path:
        return None
    try:
        result = run_consolidation(
            project_path,
            model=model,
            call_summarize=call_summarize,
            min_hours=MIN_HOURS_BETWEEN_RUNS if min_hours is None else min_hours,
        )
        if logger and result.get("status") == "written":
            logger.info(
                "[LiveCode] memory consolidated: %s log(s) folded into MEMORY.md",
                result.get("logs_consumed", 0),
            )
        return result
    except Exception:
        if logger:
            logger.debug("[LiveCode] memory consolidation skipped", exc_info=True)
        return None
