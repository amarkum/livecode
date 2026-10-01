from __future__ import annotations

from typing import Any

from livecode.memory.index import SearchResult, ensure_index, search_memory

MEMORY_CONTEXT_OPEN = "<memory-context>"
MEMORY_CONTEXT_CLOSE = "</memory-context>"
SNIPPET_MAX_CHARS = 500
GREETING_FALLBACK_QUERY = "project conventions preferences architecture"

_GREETINGS = frozenset(
    {
        "hi",
        "hey",
        "hello",
        "howdy",
        "continue",
        "start",
        "begin",
        "go",
        "good morning",
        "good afternoon",
        "good evening",
    }
)

def is_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if len(t) < 20 and t.rstrip("!.?") in _GREETINGS:
        return True
    return t in _GREETINGS

def conversation_has_memory_context(messages: list[dict[str, Any]] | None) -> bool:
    if not messages:
        return False
    first = messages[0]
    if first.get("role") != "system":
        return False
    content = first.get("content") or ""
    return MEMORY_CONTEXT_OPEN in content

def format_memory_reminder(results: list[SearchResult]) -> str | None:
    if not results:
        return None
    lines = [MEMORY_CONTEXT_OPEN, "## Relevant Memory from Past Sessions", ""]
    for i, r in enumerate(results):
        snippet = r.snippet
        if len(snippet) > SNIPPET_MAX_CHARS:
            snippet = snippet[:SNIPPET_MAX_CHARS] + "..."
        lines.append(f"### Result {i + 1} (score: {r.score:.2f}, source: {r.source})")
        lines.append(f"**File:** {r.path} (lines {r.start_line}-{r.end_line})")
        lines.append(f"```\n{snippet}\n```")
        lines.append("")
    lines.append(MEMORY_CONTEXT_CLOSE)
    return "\n".join(lines)

def build_memory_context(
    project_path: str,
    query: str,
    *,
    max_results: int = 6,
    min_score: float = 0.0,
    existing_messages: list[dict[str, Any]] | None = None,
) -> str:
    if existing_messages and conversation_has_memory_context(existing_messages):
        return ""
    ensure_index(project_path)
    raw = (query or "").strip()
    search_query = (
        GREETING_FALLBACK_QUERY
        if (not raw or len(raw) < 20 or is_greeting(raw))
        else raw
    )
    results = search_memory(
        project_path,
        search_query,
        max_results=max_results,
        min_score=min_score,
    )
    return format_memory_reminder(results) or ""
