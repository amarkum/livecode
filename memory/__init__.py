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
    list_memory_files,
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

__all__ = [
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
