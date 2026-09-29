from __future__ import annotations

from typing import Any, Callable

from livecode.compaction.full_replace import apply_full_replace_compaction, messages_to_compact_text
from livecode.compaction.intra import estimate_messages_tokens
from livecode.prompts import LIVECODE_CONTEXT_WINDOW, LIVECODE_INTER_COMPACT_RATIO
from livecode.session import load_session, save_compaction

# A single agentic turn persists one user message plus many assistant/tool
# messages, so raw message count is a poor "has this conversation actually run
# long enough to compact" signal. Require at least this many completed user
# turns before inter-turn compaction so a brand-new chat whose first turn was
# tool-heavy doesn't get compacted on its second message.
_INTER_MIN_USER_TURNS = 2

def count_user_turns(messages: list[dict[str, Any]]) -> int:
    count = 0
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.startswith("[Turn activity summary]"):
            continue
        count += 1
    return count

def maybe_inter_turn_compact(
    project_path: str,
    session_id: str,
    *,
    model: str,
    call_summarize: Callable[[str, list[dict[str, str]]], str],
    context_window: int = LIVECODE_CONTEXT_WINDOW,
    threshold_ratio: float = LIVECODE_INTER_COMPACT_RATIO,
) -> dict[str, Any] | None:
    session = load_session(project_path, session_id)
    messages = session.get("messages") or []
    if len(messages) < 10:
        return None
    if count_user_turns(messages) < _INTER_MIN_USER_TURNS:
        return None

    token_est = estimate_messages_tokens(messages)
    threshold = int(context_window * threshold_ratio)
    full_threshold = int(context_window * 0.85)
    if token_est < threshold or token_est >= full_threshold:
        return None

    mid = len(messages) // 2
    if mid < 2:
        return None

    try:
        summary, boundary, attempts = apply_full_replace_compaction(
            messages,
            call_summarize=call_summarize,
            model=model,
            keep_recent=len(messages) - mid,
        )
    except ValueError:
        return None

    record = save_compaction(
        project_path,
        session_id,
        boundary_index=boundary,
        summary=summary,
        messages=messages,
        strategy="inter_turn",
        attempts=attempts,
    )
    return record
