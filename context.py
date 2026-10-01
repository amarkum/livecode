from __future__ import annotations

from typing import Any, Callable

from livecode.compaction.full_replace import apply_full_replace_compaction
from livecode.compaction.inter import count_user_turns, maybe_inter_turn_compact
from livecode.compaction.intra import (
    compact_stale_tool_messages,
    dedupe_stale_file_reads,
    dedupe_stale_grep_results,
    estimate_messages_tokens,
)
from livecode.memory.flush import maybe_flush_session
from livecode.prompts import LIVECODE_AUTO_COMPACT_RATIO, LIVECODE_CONTEXT_WINDOW
from livecode.session import _valid_compaction, load_session, save_compaction

__all__ = [
    "build_turn_activity_summary",
    "compact_stale_tool_messages",
    "dedupe_stale_file_reads",
    "estimate_messages_tokens",
    "maybe_compact_session",
]

def maybe_compact_session(
    project_path: str,
    session_id: str,
    *,
    model: str,
    call_summarize: Callable[[str, list[dict[str, str]]], str],
    context_window: int = LIVECODE_CONTEXT_WINDOW,
    threshold_ratio: float = LIVECODE_AUTO_COMPACT_RATIO,
    force: bool = False,
) -> dict[str, Any] | None:
    """Compacts the session's history when what the model would be sent is near the window.

    The size measured is the projected history (the previous summary plus the messages after its
    boundary), not the raw file, so a session that was compacted once is not summarised from the start
    again on every later turn; the next compaction folds the previous summary and the messages since it."""
    session = load_session(project_path, session_id)
    messages = session.get("messages") or []
    if len(messages) < 4 and not force:
        return None

    if not force and count_user_turns(messages) < 2:
        return None

    compaction = session.get("compaction")
    previous_boundary = 0
    projected: list[dict[str, Any]] = messages
    if _valid_compaction(messages, compaction):
        previous_boundary = int(compaction["boundary_index"])
        projected = [{"role": "user", "content": str(compaction.get("summary") or "")}] + list(messages[previous_boundary:])

    token_est = estimate_messages_tokens(projected)
    threshold = int(context_window * threshold_ratio)
    if not force and token_est < threshold:
        inter = maybe_inter_turn_compact(
            project_path,
            session_id,
            model=model,
            call_summarize=call_summarize,
            context_window=context_window,
            full_threshold_ratio=threshold_ratio,
        )
        return inter or None
    if len(projected) < 2:
        return None

    try:
        maybe_flush_session(
            project_path,
            session_id,
            messages,
            model=model,
            call_summarize=call_summarize,
            force=True,
        )
    except Exception:
        pass

    try:
        summary, boundary, attempts = apply_full_replace_compaction(
            projected,
            call_summarize=call_summarize,
            model=model,
        )
    except ValueError:
        return None
    if previous_boundary:
        # The first projected message stood for everything before the previous boundary.
        boundary = previous_boundary + max(0, boundary - 1)

    return save_compaction(
        project_path,
        session_id,
        boundary_index=boundary,
        summary=summary,
        messages=messages,
        strategy="full_replace",
        attempts=attempts,
    )

def build_turn_activity_summary(tool_events: list[dict[str, Any]]) -> str:
    if not tool_events:
        return ""
    lines: list[str] = []
    for ev in tool_events[-20:]:
        tool = ev.get("tool", "")
        label = ev.get("label", "")
        detail = ev.get("detail", "")
        if tool and label:
            line = f"- {tool}: {label}"
            if detail:
                line += f" ({detail})"
            lines.append(line)
    return "\n".join(lines)
