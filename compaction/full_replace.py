from __future__ import annotations

import json
import re
from typing import Any, Callable

from livecode.prompts import build_compaction_prompt, build_compaction_prompt_short

MIN_SUMMARY_CHARS = 40
MAX_CAPTURED_SUMMARY_CHARS = 24_000
SUMMARIZER_INPUT_MAX_CHARS = 120_000
TOOL_BODY_TRUNCATE_CHARS = 500

def _truncate_tool_content(msg: dict[str, Any], max_chars: int) -> dict[str, Any]:
    if msg.get("role") != "tool":
        return msg
    content = str(msg.get("content") or "")
    if len(content) <= max_chars:
        return msg
    return {**msg, "content": content[:max_chars] + "...[truncated for summarization]"}

def fit_messages_for_summarizer(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = SUMMARIZER_INPUT_MAX_CHARS,
) -> list[dict[str, Any]]:
    working = list(messages)
    if not messages_to_compact_text(working, max_chars=max_chars).strip():
        return working

    text = messages_to_compact_text(working, max_chars=max_chars)
    if len(text) <= max_chars:
        return working

    target = max(2, len(working) // 2)
    while len(working) > target:
        drop = 0
        while drop < len(working) and working[drop].get("role") == "tool":
            drop += 1
        if drop >= len(working) - 1:
            break
        working = working[drop + 1:]
        text = messages_to_compact_text(working, max_chars=max_chars)
        if len(text) <= max_chars:
            return working

    fitted = []
    for msg in working:
        fitted.append(_truncate_tool_content(msg, TOOL_BODY_TRUNCATE_CHARS))
    text = messages_to_compact_text(fitted, max_chars=max_chars)
    if len(text) <= max_chars:
        return fitted

    emergency = fitted[len(fitted) // 2:]
    if not emergency:
        emergency = fitted[-2:]
    return emergency

def messages_to_compact_text(messages: list[dict[str, Any]], max_chars: int = 120_000) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        line = f"{role.upper()}: {content}"
        if msg.get("tool_calls"):
            line += f"\n[tool_calls: {json.dumps(msg['tool_calls'], default=str)[:2000]}]"
        lines.append(line)
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[truncated for summarization]"
    return text

def align_compaction_boundary(messages: list[dict[str, Any]], boundary: int) -> int:
    n = len(messages)
    boundary = max(0, min(boundary, n))

    while boundary < n and messages[boundary].get("role") == "tool":
        boundary -= 1
        if boundary <= 0:
            return 0

    while (
        boundary > 0
        and boundary < n
        and messages[boundary - 1].get("role") == "assistant"
        and messages[boundary - 1].get("tool_calls")
        and messages[boundary].get("role") == "tool"
    ):
        boundary += 1

    return max(0, min(boundary, n))

def is_degenerate_summary(summary: str) -> bool:
    text = (summary or "").strip()
    if len(text) < MIN_SUMMARY_CHARS:
        return True
    if text.lower() in ("n/a", "none", "no summary", "summary unavailable"):
        return True
    unique = set(text.replace(" ", "").replace("\n", ""))
    if len(unique) <= 2 and len(text) > 100:
        return True
    sections = re.findall(r"^\d+\.\s+\w+", text, re.MULTILINE)
    if len(sections) >= 5 and len(text) < 120:
        return True
    return False

def apply_full_replace_compaction(
    messages: list[dict[str, Any]],
    *,
    call_summarize: Callable[[str, list[dict[str, str]]], str],
    model: str,
    keep_recent: int | None = None,
) -> tuple[str, int, list[dict[str, Any]]]:
    if len(messages) < 2:
        raise ValueError("not enough messages to compact")

    keep = keep_recent if keep_recent is not None else min(6, max(2, len(messages) // 4))
    boundary = align_compaction_boundary(messages, max(0, len(messages) - keep))
    if boundary <= 0:
        raise ValueError("boundary is zero")

    older = fit_messages_for_summarizer(messages[:boundary])
    text = messages_to_compact_text(older)
    if not text.strip():
        raise ValueError("empty compact text")

    attempts: list[dict[str, Any]] = []
    summary = ""
    for attempt, use_short in enumerate((False, True)):
        prompt = build_compaction_prompt_short() if use_short else build_compaction_prompt()
        summary = call_summarize(
            model,
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Summarize this conversation for continuation:\n\n{text}"},
            ],
        )
        summary = (summary or "").strip()[:MAX_CAPTURED_SUMMARY_CHARS]
        degenerate = is_degenerate_summary(summary)
        attempts.append({
            "attempt": attempt + 1,
            "short_prompt": use_short,
            "summary_len": len(summary),
            "degenerate": degenerate,
        })
        if not degenerate:
            break

    if is_degenerate_summary(summary):
        raise ValueError("degenerate compaction summary after retries")

    return summary, boundary, attempts
