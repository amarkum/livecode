from __future__ import annotations

import json
import re
from typing import Any

from livecode.prompts import LIVECODE_CONTEXT_WINDOW, LIVECODE_IN_TURN_COMPACT_RATIO, LIVECODE_KEEP_RECENT_TOOL_MSGS

def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            parts.append(json.dumps(tool_calls, default=str))
    return sum(len(p) for p in parts) // 4

def compact_tool_payload(raw: str, *, tier: str = "fitted") -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        if tier == "lossy":
            return raw[:200] + "...[compacted]" if len(raw) > 200 else raw
        if len(raw) > 500:
            return raw[:500] + "...[compacted]"
        return raw

    if not isinstance(parsed, dict):
        limit = 200 if tier == "lossy" else 500
        return raw[:limit] + "...[compacted]" if len(raw) > limit else raw

    if parsed.get("error"):
        err_len = 120 if tier == "lossy" else 300
        return json.dumps({"_compacted": True, "error": str(parsed["error"])[:err_len]}, default=str)

    if tier == "lossy":
        if "match_count" in parsed:
            return json.dumps({"_compacted": True, "match_count": parsed.get("match_count")}, default=str)
        if parsed.get("file_path"):
            return json.dumps({
                "_compacted": True,
                "file_path": parsed.get("file_path"),
                "action": parsed.get("action"),
            }, default=str)
        return json.dumps({"_compacted": True}, default=str)

    if "match_count" in parsed and "matches" in parsed:
        return json.dumps({
            "_compacted": True,
            "match_count": parsed.get("match_count"),
            "pattern": (parsed.get("pattern") or parsed.get("query") or "")[:80],
        }, default=str)

    if parsed.get("files") and isinstance(parsed.get("files"), list):
        return json.dumps({
            "_compacted": True,
            "file_count": parsed.get("file_count", parsed.get("match_count", len(parsed.get("files") or []))),
            "query": (parsed.get("query") or parsed.get("pattern") or "")[:80],
        }, default=str)

    if parsed.get("results") and isinstance(parsed.get("results"), list):
        return json.dumps({
            "_compacted": True,
            "result_count": parsed.get("result_count", len(parsed.get("results") or [])),
            "query": (parsed.get("query") or "")[:80],
        }, default=str)

    if parsed.get("final_url") or (parsed.get("url") and parsed.get("content") is not None):
        return json.dumps({
            "_compacted": True,
            "url": (parsed.get("final_url") or parsed.get("url") or "")[:120],
            "summary": f"fetched {len(str(parsed.get('content') or ''))} chars",
        }, default=str)

    if "entries" in parsed:
        return json.dumps({
            "_compacted": True,
            "entry_count": len(parsed.get("entries") or []),
        }, default=str)

    if parsed.get("file_path") or parsed.get("content"):
        fp = parsed.get("file_path") or parsed.get("file") or ""
        content = parsed.get("content", "")
        preview_len = 80 if tier == "lossy" else 200
        preview = str(content)[:preview_len] if content else ""
        slim = {
            "_compacted": True,
            "file_path": fp,
            "summary": f"read {len(str(content))} chars" if content else "read file",
            "preview": preview,
        }
        if content and parsed.get("showing"):
            slim["showing"] = parsed.get("showing")
            slim["note"] = "Content dropped to save context; call read_repo_file again if you need these lines."
        return json.dumps(slim, default=str)

    if "command" in parsed or "exit_code" in parsed:
        output = str(parsed.get("output") or "")
        out_len = 100 if tier == "lossy" else 300
        return json.dumps({
            "_compacted": True,
            "command": (parsed.get("command") or "")[:120],
            "exit_code": parsed.get("exit_code"),
            "output_preview": output[:out_len],
        }, default=str)

    if parsed.get("success") is not None or parsed.get("completed"):
        return json.dumps({
            "_compacted": True,
            "success": parsed.get("success"),
            "file_path": parsed.get("file_path"),
            "action": parsed.get("action"),
        }, default=str)

    slim = {k: parsed[k] for k in list(parsed.keys())[:6]}
    slim["_compacted"] = True
    limit = 300 if tier == "lossy" else 800
    return json.dumps(slim, default=str)[:limit]

_SHOWING_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)")


def _read_span(parsed: dict[str, Any]) -> tuple[str, int, int] | None:
    if not isinstance(parsed, dict) or parsed.get("_compacted") or "content" not in parsed:
        return None
    path = parsed.get("file_path") or parsed.get("file")
    if not path:
        return None
    start = parsed.get("start_line")
    end = parsed.get("end_line")
    if start is None or end is None:
        match = _SHOWING_RE.match(str(parsed.get("showing") or ""))
        if match:
            start, end = int(match.group(1)), int(match.group(2))
        else:
            start, end = 1, int(parsed.get("total_lines") or 10**9)
    try:
        return str(path), int(start), int(end)
    except (TypeError, ValueError):
        return None


def dedupe_stale_file_reads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(messages)
    spans: list[tuple[int, str, int, int]] = []
    for i, msg in enumerate(out):
        if msg.get("role") != "tool":
            continue
        try:
            parsed = json.loads(msg.get("content") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        span = _read_span(parsed)
        if span:
            spans.append((i, *span))
    for pos, (i, path, start, end) in enumerate(spans):
        covered = any(
            later_path == path and later_start <= start and later_end >= end
            for _j, later_path, later_start, later_end in spans[pos + 1:]
        )
        if covered:
            raw = out[i].get("content") or ""
            out[i] = {**out[i], "content": compact_tool_payload(raw, tier="fitted")}
    return out


def dedupe_stale_grep_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(messages)
    latest_by_key: dict[str, int] = {}

    for i, msg in enumerate(out):
        if msg.get("role") != "tool":
            continue
        raw = msg.get("content") or ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "match_count" not in parsed and "matches" not in parsed:
            continue
        key = f"{parsed.get('pattern', '')}|{parsed.get('glob_filter', '')}"
        if key.strip("|"):
            latest_by_key[key] = i

    for i, msg in enumerate(out):
        if msg.get("role") != "tool" or i in latest_by_key.values():
            continue
        raw = msg.get("content") or ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        key = f"{parsed.get('pattern', '')}|{parsed.get('glob_filter', '')}"
        if key.strip("|") and key in latest_by_key and latest_by_key[key] != i:
            out[i] = {**msg, "content": compact_tool_payload(raw, tier="fitted")}

    return out

_LARGE_ARGUMENT_FIELDS = ("content", "new_string", "old_string", "edits", "plan", "result")
_ARGUMENT_KEEP_CHARS = 400
KEEP_RECENT_ASSISTANT_MSGS = 6


def _message_chars(msg: dict[str, Any]) -> int:
    content = msg.get("content")
    total = 0
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        total += sum(len(str(block.get("text") or "")) for block in content if isinstance(block, dict))
    if msg.get("tool_calls"):
        total += len(json.dumps(msg["tool_calls"], default=str))
    return total


def compact_tool_call_arguments(msg: dict[str, Any]) -> dict[str, Any]:
    calls = msg.get("tool_calls")
    if not calls:
        return msg
    changed = False
    new_calls = []
    for call in calls:
        fn = dict(call.get("function") or {})
        raw = fn.get("arguments")
        if not isinstance(raw, str) or len(raw) <= _ARGUMENT_KEEP_CHARS * 2:
            new_calls.append(call)
            continue
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            new_calls.append(call)
            continue
        if not isinstance(args, dict):
            new_calls.append(call)
            continue
        for key in _LARGE_ARGUMENT_FIELDS:
            value = args.get(key)
            text = value if isinstance(value, str) else json.dumps(value, default=str) if value is not None else ""
            if len(text) > _ARGUMENT_KEEP_CHARS:
                args[key] = f"[{len(text)} chars from an earlier step, omitted to save context]"
                changed = True
        fn["arguments"] = json.dumps(args, default=str)
        new_calls.append({**call, "function": fn})
    return {**msg, "tool_calls": new_calls} if changed else msg


def _compact_oldest_first(
    messages: list[dict[str, Any]],
    budget: int,
    keep_recent_tool_messages: int,
) -> list[dict[str, Any]]:
    out = list(messages)
    total_chars = sum(_message_chars(m) for m in out)
    budget_chars = budget * 4
    if total_chars <= budget_chars:
        return out
    tool_indices = [i for i, msg in enumerate(out) if msg.get("role") == "tool"]
    stale_tools = tool_indices[:-keep_recent_tool_messages] if keep_recent_tool_messages else tool_indices
    if len(tool_indices) <= keep_recent_tool_messages:
        stale_tools = []
    assistant_indices = [i for i, msg in enumerate(out) if msg.get("role") == "assistant" and msg.get("tool_calls")]
    stale_assistants = assistant_indices[:-KEEP_RECENT_ASSISTANT_MSGS] if len(assistant_indices) > KEEP_RECENT_ASSISTANT_MSGS else []
    candidates = sorted(set(stale_tools) | set(stale_assistants))
    for tier in ("fitted", "lossy"):
        for idx in candidates:
            before = _message_chars(out[idx])
            if out[idx].get("role") == "tool":
                raw = out[idx].get("content") or ""
                out[idx] = {**out[idx], "content": compact_tool_payload(raw, tier=tier)}
            elif tier == "fitted":
                out[idx] = compact_tool_call_arguments(out[idx])
            total_chars -= before - _message_chars(out[idx])
            if total_chars <= budget_chars:
                return out
    return out


COMPACT_TARGET_RATIO = 0.7


def compact_stale_tool_messages(
    messages: list[dict[str, Any]],
    *,
    max_input_tokens: int | None = None,
    keep_recent_tool_messages: int = LIVECODE_KEEP_RECENT_TOOL_MSGS,
) -> list[dict[str, Any]]:
    budget = max_input_tokens or int(LIVECODE_CONTEXT_WINDOW * LIVECODE_IN_TURN_COMPACT_RATIO)
    if estimate_messages_tokens(messages) <= budget:
        return messages
    target = max(1, int(budget * COMPACT_TARGET_RATIO))
    out = dedupe_stale_grep_results(dedupe_stale_file_reads(messages))
    if estimate_messages_tokens(out) <= target:
        return out
    out = _compact_oldest_first(out, target, keep_recent_tool_messages)
    if estimate_messages_tokens(out) > target and keep_recent_tool_messages > 2:
        out = _compact_oldest_first(out, target, 2)
    return out
