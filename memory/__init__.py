from __future__ import annotations

from livecode.memory.autosave import (
    extract_real_user_queries,
    generate_metadata_summary,
    maybe_autosave_session,
)
from livecode.memory.consolidate import maybe_consolidate_memory, run_consolidation
from livecode.memory.flush import maybe_flush_session, process_flush_response, run_memory_flush
from livecode.memory.index import (
    SearchResult,
    embed_missing_chunks,
    ensure_index,
    reindex_all,
    reindex_file,
    search_memory,
)
from livecode.memory.inject import (
    MEMORY_CONTEXT_CLOSE,
    MEMORY_CONTEXT_OPEN,
    build_memory_context,
    format_memory_reminder,
    is_greeting,
)
from livecode.memory.storage import (
    append_memory_md,
    editable_memory_rel,
    list_editable_memory,
    list_memory_files,
    read_editable_memory,
    read_memory_file,
    read_memory_md,
    write_session_log,
)

MAX_MEMORY_CHARS = 2048

def load_project_memory(project_path: str) -> str:
    text = read_memory_md(project_path)
    if len(text) > MAX_MEMORY_CHARS:
        return text[-MAX_MEMORY_CHARS:]
    return text

def append_project_memory(project_path: str, note: str) -> str:
    combined = append_memory_md(project_path, note)
    from livecode.memory.storage import memory_md_path

    path = memory_md_path(project_path, create=True)
    reindex_file(project_path, path, "workspace", "MEMORY.md")
    embed_missing_chunks(project_path)
    return combined[-MAX_MEMORY_CHARS:] if len(combined) > MAX_MEMORY_CHARS else combined

def save_memory_file(project_path: str, rel_path: str, content) -> dict:
    """Save a memory file from the Settings view, then refresh its entries in the memory search index."""
    from livecode.memory.storage import save_editable_memory

    result = save_editable_memory(project_path, rel_path, content)
    if result.get("saved"):
        try:
            reindex_file(project_path, result["abs_path"], "workspace" if result["path"] == "MEMORY.md" else "session", result["path"])
        except Exception:
            pass
        result.pop("abs_path", None)
    return result

__all__ = [
    "editable_memory_rel",
    "list_editable_memory",
    "read_editable_memory",
    "save_memory_file",
    "MAX_MEMORY_CHARS",
    "MEMORY_CONTEXT_CLOSE",
    "MEMORY_CONTEXT_OPEN",
    "SearchResult",
    "append_project_memory",
    "build_memory_context",
    "embed_missing_chunks",
    "ensure_index",
    "extract_real_user_queries",
    "format_memory_reminder",
    "generate_metadata_summary",
    "is_greeting",
    "list_memory_files",
    "load_project_memory",
    "maybe_autosave_session",
    "maybe_consolidate_memory",
    "maybe_flush_session",
    "process_flush_response",
    "run_consolidation",
    "read_memory_file",
    "read_memory_md",
    "reindex_all",
    "reindex_file",
    "run_memory_flush",
    "search_memory",
    "write_session_log",
]
