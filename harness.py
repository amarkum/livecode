from __future__ import annotations

import functools
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Generator

from livecode.activity_log import (
    accumulate_usage_by_model,
    describe_iteration_start,
    describe_model_usage,
    describe_tool_result,
    describe_tool_start,
    describe_turn_complete,
    describe_turn_start,
)
from livecode.model_pricing import estimate_usage_cost_usd
from livecode.compaction.full_replace import fit_messages_for_summarizer
from livecode.context import (
    build_turn_activity_summary,
    compact_stale_tool_messages,
    estimate_messages_tokens,
    maybe_compact_session,
)
from livecode.codebase_index import get_codebase_index
from livecode.interjection import drain_interjections, has_pending_interjection, is_cancelled
from livecode.model_limits import max_output_tokens_for_model, working_context_tokens
from livecode.model_stream import StepStream, TurnCancelled, call_host_model
from livecode.memory import (
    build_memory_context,
    maybe_autosave_session,
    maybe_consolidate_memory,
    maybe_flush_session,
)
from livecode.prompts import (
    CLOSURE_ITERATIONS,
    DIRECTORY_DRILL_NUDGE_AFTER,
    DIRECTORY_DRILL_NUDGE_TEMPLATE,
    EXPLORATION_STREAK_NUDGE_AFTER,
    EXPLORATION_STREAK_NUDGE_TEMPLATE,
    EXPLORATION_STREAK_READ_ONLY_NUDGE_TEMPLATE,
    LIVECODE_ASK_MODE_PROMPT,
    LIVECODE_CODEBASE_RECOVERY_PROMPT,
    LIVECODE_IN_TURN_COMPACT_RATIO,
    LIVECODE_PLAN_BUILD_PREFIX,
    LIVECODE_PLAN_MODE_PROMPT,
    LIVECODE_PLAN_REENTRY_REMINDER_TEMPLATE,
    LIVECODE_STRUCTURED_OUTPUT_REMINDER,
    ITERATION_BUDGET_NUDGE_TEMPLATE,
    POST_EDIT_COMPLETION_NUDGE_AFTER,
    POST_EDIT_COMPLETION_NUDGE_TEMPLATE,
    POST_EDIT_DIAGNOSTICS_TEMPLATE,
    LIVECODE_TODO_GATE_ENABLED,
    LIVECODE_TODO_GATE_MAX_FIRES,
    LIVECODE_VERIFY_AFTER_EDIT,
    PARALLEL_AGENTS_HINT,
    DESIGN_GATE_MAX_FIRES,
    DESIGN_GATE_TEMPLATE,
    DESIGN_LOOP_FIGMA_NOTE,
    DESIGN_LOOP_PROMPT,
    DESIGN_RECHECK_TEMPLATE,
    UI_VERIFY_GATE_MAX_FIRES,
    UI_VERIFY_TEMPLATE,
    TODO_GATE_TEMPLATE,
    TODO_NUDGE_AFTER,
    TODO_NUDGE_COOLDOWN,
    TODO_NUDGE_MAX_FIRES,
    TODO_NUDGE_TEMPLATE,
    SEARCH_SCATTER_NUDGE_AFTER,
    SEARCH_SCATTER_NUDGE_TEMPLATE,
    STATIONARITY_HARD_STOP,
    STATIONARITY_NUDGE_AFTER,
    STATIONARITY_NUDGE_TEMPLATE,
    SUBAGENT_MAX_ITERATIONS,
    TEST_FAILURE_NUDGE_AFTER,
    TEST_FAILURE_NUDGE_TEMPLATE,
    EDIT_NO_MATCH_NUDGE_AFTER,
    EDIT_NO_MATCH_NUDGE_TEMPLATE,
    build_subagent_system_prompt,
    build_system_prompt,
    build_turn_context_block,
    iteration_budget_nudge_points,
    max_iterations_for_mode,
)
from livecode.intelligent_classifier import (
    describe_intelligent_classification,
    intelligent_classify_turn,
)
from livecode.routing import (
    get_session_chat_history_for_classify,
    needs_code_change,
    needs_codebase_evidence,
    needs_flagship_edit,
    pick_tool_choice,
    user_requests_browser,
    user_requests_design_work,
    user_requests_mcp_or_tool_use,
    browse_request,
    is_ui_file,
    user_requests_web_lookup,
    wants_structured_json,
)
from livecode.reminders import (
    build_reminder_text,
    clear_compaction_reminder,
    load_goal_state,
    load_todo_state,
    record_compaction_ran,
    record_file_edited,
    save_goal_state,
    save_todo_state,
    todo_pending_count,
)
from livecode.rules import load_workspace_rules_reminder
from livecode.session import (
    count_user_messages,
    get_projected_messages,
    has_valid_compaction,
    load_session,
    sanitize_messages_for_api,
    save_diff_record,
    save_tool_artifact,
)
from livecode.mcp_bridge import MCPToolBinding
from livecode.project_store import workspace_state_identity
from livecode.mcp_policy import (
    MCP_EFFECT_APPROVAL_REQUIRED,
    MCP_EFFECT_BLOCKED,
    MCP_EFFECT_READ,
    MCP_EFFECT_WRITE,
    classify_mcp_tool_effect,
)
from livecode.permissions import SENSITIVE_TOOLS, is_destructive_command
from livecode.subagent import (
    SCOPED_WRITER_EXCLUDED_TOOLS,
    FileScope,
    SubagentProgress,
    is_scoped_writer_call,
    scope_entries,
    subagent_read_only,
    subagent_title,
)
from livecode.browser import (
    action_needs_approval as browser_action_needs_approval,
    design_context as browser_design_context,
    figma_configured as browser_figma_configured,
    agent_tabs_enabled as browser_agent_tabs_enabled,
    design_gate_enabled as browser_design_gate_enabled,
    ui_verify_enabled as browser_ui_verify_enabled,
    playwright_installed as browser_playwright_installed,
    release_agent as browser_release_agent,
    shot_data_url as browser_shot_data_url,
    turn_context as browser_turn_context,
)
from livecode.tools import (
    FILE_EDIT_TOOL_NAMES,
    MUTATING_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    STRUCTURED_OUTPUT_MAX_RETRIES,
    STRUCTURED_OUTPUT_TOOL,
    compact_tool_result_for_llm,
    dispatch_tool,
    edit_target_path,
    filter_tools_for_mode,
    format_structured_output_answer,
    get_livecode_tools,
    human_tool_label,
    mode_rejection_message,
    normalize_mode,
    parse_livecode_tool_arguments,
    validate_structured_output,
)
from livecode.workspace import (
    LAYOUT_MAX_CHARS,
    build_project_layout_tree,
    build_workspace_index,
    format_project_layout_block,
    index_summary_brief,
    invalidate_workspace_index,
    resolve_safe_path,
    workspace_roots,
)



def _log_session_id(session_id: str) -> str:
    sid = str(session_id or "")
    if len(sid) <= 24:
        return sid
    return f"{sid[:12]}…{sid[-8:]}"

_IDE_STEP_RE = re.compile(r"^Step (\d+) of (\d+): calling ([^ ]+)(?: \((.*)\))?")
_IDE_STEP_USED_RE = re.compile(r"^Step \d+ \([^)]*\) used,?\s*")
_IDE_GLYPHS = (
    ("Asking the model", "→"),
    ("Model replied", "←"),
    ("Used ", "$"),
    ("parallel", "∥"),
    ("nudge", "≫"),
    ("final answer", "★"),
    ("Task completed", "■"),
    ("Done:", "✓"),
    ("Read ", "✓"),
    ("Found ", "✓"),
    ("Shell command finished", "✓"),
    ("Failed", "✗"),
    ("Running shell", "›"),
    ("Searching", "◎"),
    ("Reading", "◎"),
    ("Finding", "◎"),
)


_IDE_ANSI = {
    "→": "2",
    "←": "2",
    "$": "33",
    "∥": "36",
    "≫": "35",
    "★": "1;32",
    "■": "32",
    "✓": "32",
    "✗": "31",
    "›": "34",
    "◎": "34",
    "·": "2",
}


def _ide_color_on() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stderr, "isatty", lambda: False)() or getattr(sys.stdout, "isatty", lambda: False)())


def _ide_paint(text: str, code: str) -> str:
    if not _ide_color_on():
        return text
    return f"\033[{code}m{text}\033[0m"


def _ide_glyph(text: str) -> str:
    for prefix, glyph in _IDE_GLYPHS:
        if text.startswith(prefix):
            return glyph
    return "·"


_IDE_USAGE_RE = re.compile(
    r"^Used ([\d,]+) input tokens, ([\d,]+) output tokens(?:, ([\d,]+) cached)?, estimated cost (\$[\d.]+)$"
)


def _ide_short_count(value: Any) -> str:
    n = int(str(value).replace(",", ""))
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


_IDE_TOKEN_NUM_RE = re.compile(r"([\d,]{4,})(?= (?:in|out|cached)\b)")


def _ide_usage_text(text: str) -> str:
    m = _IDE_USAGE_RE.match(text)
    if not m:
        return text
    tin, tout, cached, cost = m.groups()
    total = int(tin.replace(",", "")) + int(tout.replace(",", ""))
    cache = f", {_ide_short_count(cached)} of them cached" if cached else ""
    return f"Used {_ide_short_count(total)} tokens ({_ide_short_count(tin)} read{cache}, {_ide_short_count(tout)} written), about {cost}"


def _ide_child(text: str) -> str:
    glyph = _ide_glyph(text)
    bar = _ide_paint("┃", "2")
    body = _ide_paint(text, "31") if glyph == "✗" else text
    return f"LiveCode  {bar}  {_ide_paint(glyph, _IDE_ANSI.get(glyph, '0'))} {body}"


def _ide_tree_line(text: str) -> str:
    step = _IDE_STEP_RE.match(text)
    if step:
        num, total, model, extra = step.groups()
        reason = (extra or "").replace("auto-routed, reason: ", "").replace("auto-routed", "auto")
        line = f"┏━ Step {num}/{total} ━ {model}"
        line = f"{line} ━ {reason}" if reason else line
        return f"LiveCode  {_ide_paint(line, '1;36')}"
    if text.startswith("Starting LiveCode turn"):
        return f"LiveCode  {_ide_paint('╭─ ' + text, '1')}"
    if text.startswith("Turn complete"):
        head, _, rest = text.partition(". ")
        if rest:
            detail = "  ·  ".join(x.strip(" .") for x in rest.split(". ") if x.strip(" ."))
            detail = _IDE_TOKEN_NUM_RE.sub(lambda m: _ide_short_count(m.group(1)), detail)
            return f"LiveCode  {_ide_paint('╰─ ' + head, '1')}\nLiveCode     {_ide_paint(detail, '2')}"
        return f"LiveCode  {_ide_paint('╰─ ' + text, '1')}"
    return _ide_child(_ide_usage_text(_IDE_STEP_USED_RE.sub("Used ", text)))

def _ide_log(
    logger: Any,
    level: str,
    headline: str,
    *parts: Any,
    sid: str = "",
    exc_info: bool = False,
) -> None:
    if not logger:
        return
    bits = [str(p).strip() for p in parts if p is not None and str(p).strip() != ""]
    head = str(headline).strip()
    msg = _ide_child(head)
    if bits:
        msg += " " + "." * max(3, 30 - len(head)) + " " + "  ·  ".join(bits)
    if sid:
        msg += f"  (session {sid[-8:]})"
    log_fn = getattr(logger, level, None)
    if not callable(log_fn):
        return
    if exc_info:
        log_fn(msg, exc_info=True)
    else:
        log_fn(msg)

def _ide_log_plain(
    logger: Any,
    level: str,
    message: str,
    *,
    sid: str = "",
    exc_info: bool = False,
) -> None:
    if not logger or not str(message or "").strip():
        return
    msg = _ide_tree_line(str(message).strip())
    if sid:
        msg += f"  (session {sid[-8:]})"
    log_fn = getattr(logger, level, None)
    if not callable(log_fn):
        return
    if exc_info:
        log_fn(msg, exc_info=True)
    else:
        log_fn(msg)

def _xml_attr(value: str) -> str:
    return str(value or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _workspace_folder_layout_block(folder, primary: bool, tree: str) -> str:
    primary_value = "true" if primary else "false"
    block = format_project_layout_block(folder.path, tree=tree)
    return f'<workspace_folder name="{_xml_attr(folder.name)}" primary="{primary_value}">\n{block}\n</workspace_folder>'


def _workspace_root_only_tree(folder) -> str:
    return f"{folder.path}\n  ... detailed layout omitted ..."


def _trim_project_layout_tree(tree: str, max_chars: int) -> str:
    safe_limit = max(0, int(max_chars))
    if len(tree) <= safe_limit:
        return tree
    if safe_limit <= 0:
        return ""
    marker = "\n  ... truncated ..."
    if safe_limit <= len(marker):
        return tree[:safe_limit]
    return tree[: safe_limit - len(marker)].rstrip() + marker


def _multi_root_detail_blocks(active_workspace) -> str:
    folders = list(active_workspace.folders)
    minimum_blocks = [_workspace_folder_layout_block(folder, idx == 0, _workspace_root_only_tree(folder)) for idx, folder in enumerate(folders)]
    minimum_detail = "\n\n".join(minimum_blocks)
    if len(minimum_detail) >= LAYOUT_MAX_CHARS:
        names = ", ".join(_xml_attr(folder.name) for folder in folders)
        return _trim_project_layout_tree(f"Detailed layout omitted for workspace folders: {names}", LAYOUT_MAX_CHARS)
    remaining = LAYOUT_MAX_CHARS - len(minimum_detail)
    blocks: list[str] = []
    for idx, folder in enumerate(folders):
        minimum_tree = _workspace_root_only_tree(folder)
        minimum_block = _workspace_folder_layout_block(folder, idx == 0, minimum_tree)
        detail_share = max(0, remaining // max(1, len(folders) - idx))
        tree = build_project_layout_tree(folder.path, max_chars=max(1, len(minimum_tree) + detail_share))
        block = _workspace_folder_layout_block(folder, idx == 0, tree)
        overflow = len(block) - len(minimum_block)
        if overflow > detail_share:
            tree = _trim_project_layout_tree(tree, max(0, len(tree) - (overflow - detail_share)))
            block = _workspace_folder_layout_block(folder, idx == 0, tree)
        blocks.append(block)
        remaining = max(0, remaining - max(0, len(block) - len(minimum_block)))
    return _trim_project_layout_tree("\n\n".join(blocks), LAYOUT_MAX_CHARS)


def _workspace_layout_block(project_path: str, active_workspace, symbol_index: dict) -> str:
    if len(active_workspace.folders) <= 1:
        return format_project_layout_block(project_path)
    lines = ["**Workspace:** multi-root", "", "Folders:"]
    for idx, folder in enumerate(active_workspace.folders, start=1):
        label = " primary" if idx == 1 else ""
        lines.append(f"- {folder.name}{label}: `{folder.path}`")
    lines.append("")
    lines.append("Use workspace-qualified paths when a file is not in the primary folder, e.g. `workspace: backend` and `file_path: src/app.py`, or `backend/src/app.py`.")
    lines.append("")
    return "\n".join(lines) + "\n" + _multi_root_detail_blocks(active_workspace)


def _workspace_index_summary(project_path: str, active_workspace) -> dict:
    if len(active_workspace.folders) <= 1:
        return get_codebase_index(project_path).stats()
    total_symbols = 0
    total_files = 0
    languages: dict[str, int] = {}
    folders = []
    for folder in active_workspace.folders:
        summary = get_codebase_index(folder.path).stats()
        total_symbols += int(summary.get("symbol_count") or 0)
        total_files += int(summary.get("file_count") or 0)
        for lang, count in (summary.get("languages") or {}).items():
            languages[lang] = languages.get(lang, 0) + int(count or 0)
        folders.append({"name": folder.name, "path": folder.path, "summary": summary})
    return {"symbol_count": total_symbols, "file_count": total_files, "languages": languages, "folders": folders}


def _format_token_usage_parts(response: dict | None) -> list[str]:
    if not isinstance(response, dict):
        return []
    parts: list[str] = []
    prompt = response.get("prompt_tokens")
    completion = response.get("completion_tokens")
    total = response.get("total_tokens")
    cached = response.get("cached_tokens")
    if prompt is not None:
        parts.append(f"in={prompt}")
    if completion is not None:
        parts.append(f"out={completion}")
    if total is not None and (prompt is None or completion is None):
        parts.append(f"total={total}")
    if cached is not None:
        parts.append(f"cached={cached}")
    return parts

def _accumulate_token_usage(totals: dict[str, int], response: dict | None) -> None:
    if not isinstance(response, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        val = response.get(key)
        if val is None:
            continue
        try:
            totals[key] = totals.get(key, 0) + int(val)
        except (TypeError, ValueError):
            continue

def _merge_thought_content(response: dict[str, Any]) -> str:
    parts = []
    reasoning = (response.get("reasoning_content") or "").strip()
    content = (response.get("content") or "").strip()
    if reasoning:
        parts.append(reasoning)
    if content:
        parts.append(content)
    return "\n".join(parts)

def _brief_thought_line(text: str, max_len: int = 160) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    line = ""
    for part in raw.splitlines():
        collapsed = " ".join(part.split())
        if collapsed:
            line = collapsed
            break
    if not line:
        return ""
    if len(line) > max_len:
        return line[: max_len - 1].rstrip() + "…"
    return line

def _is_auto_model(user_model: str) -> bool:
    return (user_model or "").strip().lower() in ("", "auto")

def _pick_iteration_model(
    user_model: str,
    classification: dict[str, Any],
    *,
    tool_loop: bool,
    escalate: bool,
    content_chars: int = 0,
    edit_pending: bool = False,
    edit_completed: bool = False,
    needs_flagship: bool = False,
    images: bool = False,
    code: bool = False,
) -> str:
    """images: the messages carry images; code: the turn changes code. Both steer Auto only;
    a model the user picked is kept (the router describes images for a text-only model)."""
    from app.runtime import pick_livecode_auto_model, resolve_livecode_model

    from livecode.llm import settings as llm_settings

    if not _is_auto_model(user_model):
        if llm_settings.find_model(user_model):
            return user_model
        return resolve_livecode_model(user_model)
    return pick_livecode_auto_model(
        classification,
        tool_loop=tool_loop,
        escalate=escalate,
        content_chars=content_chars,
        edit_pending=edit_pending,
        edit_completed=edit_completed,
        needs_flagship_edit=needs_flagship,
        images=images,
        code=code,
    )

def _messages_have_images(messages: list[dict[str, Any]]) -> bool:
    return any(_image_urls_in_content(m.get("content")) for m in messages if isinstance(m, dict))

def _auto_route_reason(
    classification: dict[str, Any],
    *,
    tool_loop: bool,
    escalate: bool,
    content_chars: int,
    edit_pending: bool,
    edit_completed: bool,
    needs_flagship: bool,
    model: str,
    images: bool = False,
    code: bool = False,
) -> str:
    from app.runtime import livecode_auto_model_route_reason

    return livecode_auto_model_route_reason(
        classification,
        tool_loop=tool_loop,
        escalate=escalate,
        content_chars=content_chars,
        edit_pending=edit_pending,
        edit_completed=edit_completed,
        needs_flagship_edit=needs_flagship,
        model=model,
        images=images,
        code=code,
    )

def _classify_turn(
    user_model: str,
    question: str,
    call_with_tools: Callable,
    call_summarize: Callable[[str, list[dict[str, str]]], str] | None,
    *,
    project_path: str = "",
    session_id: str = "",
    logger: Any = None,
) -> tuple[dict[str, Any], bool]:
    from app.runtime import pick_azure_model, resolve_livecode_model

    chat_history = []
    if project_path and session_id:
        chat_history = get_session_chat_history_for_classify(project_path, session_id, question)
    has_prior_turns = len(chat_history) > 0

    route_model = pick_azure_model("gpt-5-mini", task="fast")

    def _call_non_streaming(model_: str, messages_: list, *, prompt_cache_key: str | None = None) -> str:
        if call_summarize:
            try:
                return call_summarize(model_, messages_) or ""
            except TypeError:
                return call_summarize(model_, messages_) or ""
        resp = call_host_model(call_with_tools, model_, messages_, [], prompt_cache_key=prompt_cache_key)
        return (resp or {}).get("content") or ""

    classification = intelligent_classify_turn(
        model=route_model,
        call_non_streaming=_call_non_streaming,
        user_message=question,
        chat_history=chat_history or None,
        has_prior_turns=has_prior_turns,
        logger=logger,
    )
    return classification, has_prior_turns

class _StreakTracker:

    def __init__(self) -> None:
        self.run_len = 0
        self.nudged = False

    def _bump(self) -> int:
        self.run_len += 1
        return self.run_len

    def _reset(self, reset_to: int = 0) -> int:
        self.run_len = reset_to
        self.nudged = False
        return self.run_len

    def _take_nudge(self, nudge_after: int) -> bool:
        fire = self.run_len >= nudge_after and not self.nudged
        self.nudged |= fire
        return fire

_POLLING_TOOLS = frozenset({"command_status"})


class _IdenticalToolCallRun(_StreakTracker):
    def __init__(self) -> None:
        super().__init__()
        self.last_signature: str | None = None
        self.tool_name = ""

    def observe(self, signature: str, tool_name: str) -> int:
        if tool_name in _POLLING_TOOLS:
            return self.run_len
        if self.last_signature == signature:
            self._bump()
        else:
            self._reset(reset_to=1)
            self.last_signature = signature
        self.tool_name = tool_name
        return self.run_len

    def take_nudge(self) -> bool:
        return self._take_nudge(STATIONARITY_NUDGE_AFTER)

    def should_hard_stop(self) -> bool:
        return self.run_len >= STATIONARITY_HARD_STOP

_SEARCH_SCATTER_TOOLS = frozenset({"grep_repo", "glob_files", "find_files"})

_CONTEXT_OVERFLOW_MARKERS = (
    "context_length",
    "context length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length",
)

_READ_ONLY_TOOLS = READ_ONLY_TOOL_NAMES

class _SearchScatterRun(_StreakTracker):

    def observe(self, tool_name: str) -> int:
        if tool_name in _SEARCH_SCATTER_TOOLS:
            self._bump()
        else:
            self._reset()
        return self.run_len

    def take_nudge(self) -> bool:
        return self._take_nudge(SEARCH_SCATTER_NUDGE_AFTER)

class _ExplorationStreakRun(_StreakTracker):

    def observe_iteration(self, tool_names: list[str]) -> int:
        if tool_names and all(name in _READ_ONLY_TOOLS for name in tool_names):
            self._bump()
        else:
            self._reset()
        return self.run_len

    def take_nudge(self) -> bool:
        return self._take_nudge(EXPLORATION_STREAK_NUDGE_AFTER)

class _EditNoMatchRun(_StreakTracker):

    def observe(self, tool_name: str, result: dict) -> int:
        if tool_name in ("edit_file", "multi_edit") and result.get("error_kind") == "no_matches":
            self._bump()
        else:
            self._reset()
        return self.run_len

    def take_nudge(self) -> bool:
        return self._take_nudge(EDIT_NO_MATCH_NUDGE_AFTER)

class _TodoNudgeRun:

    def __init__(self) -> None:
        self.iters_since = 0
        self.fires = 0
        self.last_fire_iter = -100
        self.ever_used = False

    def observe_iteration(self, tool_names: list[str], iteration: int) -> None:
        if "todo_write" in tool_names:
            self.iters_since = 0
            self.ever_used = True
        else:
            self.iters_since += 1

    def take_nudge(self, iteration: int, *, multistep: bool, todo_list_empty: bool) -> bool:
        if not multistep or not todo_list_empty:
            return False
        if self.fires >= TODO_NUDGE_MAX_FIRES:
            return False
        if self.iters_since < TODO_NUDGE_AFTER:
            return False
        if iteration - self.last_fire_iter < TODO_NUDGE_COOLDOWN:
            return False
        self.fires += 1
        self.last_fire_iter = iteration
        return True

class _DesignRounds:

    def __init__(self) -> None:
        self.last: dict[str, Any] | None = None
        self.counts: list[int] = []
        self.fires = 0

    def observe(self, result: Any, iteration: int) -> None:
        if not isinstance(result, dict) or result.get("action") != "compare" or not result.get("success"):
            return
        lines: list[str] = []
        for item in result.get("elements") or []:
            lines.append(f"{item.get('n')}. {item.get('element')}: " + "; ".join((item.get("findings") or [])[:2]))
        for area in result.get("missing_on_page") or []:
            lines.append(f"{area.get('n')}. a part of the design at {round(float(area.get('x') or 0))},{round(float(area.get('y') or 0))} "
                         f"({area.get('width')}×{area.get('height')}) is not on the page")
        background = result.get("background")
        if isinstance(background, dict):
            lines.append(f"the page's background is {background.get('page')}; the design's is {background.get('design')}")
        if result.get("compare") != "elements":
            for area in result.get("differences") or []:
                if not area.get("minor"):
                    lines.append(f"{area.get('kind') or 'a difference'} at {area.get('element') or str(area.get('x')) + ',' + str(area.get('y'))}")
        verdict = str(result.get("verdict") or "")
        self.last = {"iteration": iteration, "verdict": verdict, "lines": lines, "summary": str(result.get("summary") or "")}
        self.counts.append(len(lines) or (1 if verdict == "different" else 0))

    def stalled(self) -> bool:
        return len(self.counts) >= 3 and self.counts[-1] >= self.counts[-3]

    def reminder(self, last_edit_iteration: int | None) -> str:
        if self.last is None or self.fires >= DESIGN_GATE_MAX_FIRES:
            return ""
        if last_edit_iteration is not None and last_edit_iteration > self.last["iteration"]:
            text = DESIGN_RECHECK_TEMPLATE
        elif self.last["verdict"] == "different" and not self.stalled():
            lines = self.last["lines"]
            items = "\n".join(f"- {line}" for line in lines[:8]) + (f"\n- and {len(lines) - 8} more" if len(lines) > 8 else "")
            text = DESIGN_GATE_TEMPLATE.format(state=self.last["summary"] or "The last comparison still shows differences:", items=items)
        else:
            return ""
        self.fires += 1
        return text

_UI_LOOK_ACTIONS = frozenset({"navigate", "reload", "screenshot", "compare", "snapshot", "crop", "inspect", "back", "forward",
                               "new_tab", "switch_tab"})


class _UiVerify:
    """UI code changed in this turn has to be looked at in the browser after its last change."""

    def __init__(self) -> None:
        self.files: list[str] = []
        self.edited_at: int | None = None
        self.looked_at: int | None = None
        self.blocked = False
        self.fires = 0

    def observe_edit(self, path: str, iteration: int) -> None:
        if path and is_ui_file(path):
            if path not in self.files:
                self.files.append(path)
            self.edited_at = iteration

    def observe_browser(self, args: Any, result: Any, iteration: int) -> None:
        if not isinstance(result, dict):
            return
        if result.get("unavailable"):
            self.blocked = True
            return
        if result.get("error"):
            return
        args = args if isinstance(args, dict) else {}
        actions = [str(args.get("action") or "").lower()]
        if actions[0] == "batch":
            actions = [str((a or {}).get("action") or "").lower() for a in (args.get("actions") or []) if isinstance(a, dict)]
        if any(a in _UI_LOOK_ACTIONS for a in actions):
            self.looked_at = iteration

    def reminder(self) -> str:
        if not self.files or self.blocked or self.fires >= UI_VERIFY_GATE_MAX_FIRES or self.edited_at is None:
            return ""
        if self.looked_at is not None and self.looked_at > self.edited_at:
            return ""
        self.fires += 1
        shown = ", ".join(self.files[:4]) + (f" and {len(self.files) - 4} more" if len(self.files) > 4 else "")
        return UI_VERIFY_TEMPLATE.format(files=shown)


def _is_child_directory(parent: str, child: str) -> bool:
    parent_norm = (parent or "").strip().strip("/")
    child_norm = (child or "").strip().strip("/")
    if not child_norm:
        return False
    if not parent_norm:
        return True
    return child_norm.startswith(parent_norm + "/")

class _DirectoryDrillRun(_StreakTracker):

    def __init__(self) -> None:
        super().__init__()
        self.last_dir = ""

    def observe(self, tool_name: str, tool_args: dict) -> int:
        if tool_name != "list_repo_dir":
            self.last_dir = ""
            self._reset()
            return 0
        directory = str(tool_args.get("directory") or "").strip().strip("/")
        if self.last_dir and _is_child_directory(self.last_dir, directory):
            self._bump()
        else:
            self._reset(reset_to=1)
        self.last_dir = directory
        return self.run_len

    def take_nudge(self) -> bool:
        return self._take_nudge(DIRECTORY_DRILL_NUDGE_AFTER)

_TEST_COMMAND_RE = re.compile(
    r"\b(pytest|run_tests|unittest|tox|nox|jest|vitest|mocha|ava|karma|playwright\s+test|cypress\s+run|"
    r"rspec|phpunit|ctest|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|(?:go|cargo|deno|dotnet|mix|swift)\s+test|"
    r"(?:mvn|gradle|gradlew|sbt|make)\s+(?:\S+\s+)*test)\b",
    re.IGNORECASE,
)


def _is_test_command(command: str) -> bool:
    return bool(_TEST_COMMAND_RE.search(command or ""))

def _attempt_completion_only_tools(tools: list[dict]) -> list[dict]:
    return [
        t for t in tools
        if (t.get("function") or {}).get("name") == "attempt_completion"
    ]

def _build_exhaustion_partial_summary(tool_events: list[dict[str, Any]]) -> str:
    activity = build_turn_activity_summary(tool_events)
    lines = [
        "Reached the maximum analysis steps for this turn. Partial progress:",
    ]
    if activity:
        lines.append(activity)
    lines.append("Try a more focused follow-up or continue in a new turn.")
    return "\n".join(lines)

def _complete_text_non_streaming(
    model: str,
    messages: list[dict[str, Any]],
    *,
    call_summarize: Callable[[str, list[dict[str, str]]], str] | None = None,
    call_with_tools: Callable | None = None,
    logger: Any = None,
    session_id: str = "",
    log_label: str = "complete text",
) -> str:
    if call_summarize:
        try:
            text = (call_summarize(model, messages) or "").strip()
            if text:
                return text
        except Exception:
            if logger:
                _ide_log(
                    logger,
                    "exception",
                    f"Failed {log_label} via summarize",
                    sid=_log_session_id(session_id),
                    exc_info=True,
                )
    if call_with_tools:
        try:
            resp = call_host_model(call_with_tools, model, messages, [])
            return ((resp or {}).get("content") or "").strip()
        except Exception:
            if logger:
                _ide_log(
                    logger,
                    "exception",
                    f"Failed {log_label} via tools call",
                    sid=_log_session_id(session_id),
                    exc_info=True,
                )
    return ""

def _run_exhaustion_summarize(
    *,
    summarize_model: str,
    messages: list[dict[str, Any]],
    call_summarize: Callable[[str, list[dict[str, str]]], str] | None,
    call_with_tools: Callable | None,
    tool_events: list[dict[str, Any]],
    logger: Any,
    session_id: str,
) -> str:
    summarize_prompt = (
        "Summarize your findings and answer the original question. Do not call more tools."
    )
    base_msgs = list(messages)
    base_msgs.append({"role": "user", "content": summarize_prompt})
    fitted = fit_messages_for_summarizer(base_msgs)

    if call_summarize:
        try:
            text = (call_summarize(summarize_model, fitted) or "").strip()
            if text:
                return text
        except Exception:
            if logger:
                _ide_log(logger, "exception", "Failed to summarize after max iterations", sid=_log_session_id(session_id), exc_info=True)
        try:
            fitted_short = fit_messages_for_summarizer(base_msgs, max_chars=60_000)
            text = (call_summarize(summarize_model, fitted_short) or "").strip()
            if text:
                return text
        except Exception:
            if logger:
                _ide_log(logger, "exception", "Failed to summarize after max iterations retry", sid=_log_session_id(session_id), exc_info=True)

    text = _complete_text_non_streaming(
        summarize_model,
        fitted,
        call_summarize=None,
        call_with_tools=call_with_tools,
        logger=logger,
        session_id=session_id,
        log_label="exhaustion summary",
    )
    if text:
        return text

    return _build_exhaustion_partial_summary(tool_events)

def _tool_call_signature(tool_name: str, tool_args: dict) -> str:
    try:
        return f"{tool_name}:{json.dumps(tool_args, sort_keys=True, default=str)}"
    except TypeError:
        return f"{tool_name}:{tool_args}"

def _tool_summary(tool_name: str, result: dict) -> tuple[str, bool]:
    if not isinstance(result, dict):
        return "", False
    if result.get("error"):
        err = str(result["error"])
        if tool_name in FILE_EDIT_TOOL_NAMES:
            return err, True
        return err[:200], True
    if tool_name == "grep_repo":
        count = result.get("match_count", 0)
        return f"Found {count} match{'es' if count != 1 else ''}", False
    if tool_name in ("glob_files", "find_files"):
        count = result.get("file_count", result.get("match_count", 0))
        return f"Found {count} file{'s' if count != 1 else ''}", False
    if tool_name == "web_search":
        count = result.get("result_count", 0)
        return f"{count} web result{'s' if count != 1 else ''}", False
    if tool_name == "web_fetch":
        return "Fetched page", False
    if tool_name == "browser":
        return _browser_summary(result), False
    if tool_name == "list_repo_dir":
        count = result.get("total")
        if count is None:
            count = len(result.get("dirs", []) or []) + len(result.get("files", []) or [])
        return f"{count} item{'s' if count != 1 else ''}", False
    if tool_name == "read_repo_file":
        showing = result.get("showing") or result.get("total_lines")
        if showing:
            return f"Read lines {showing}", False
        return "Read file", False
    if tool_name == "run_command":
        if result.get("background"):
            return ("Running in background" if result.get("running") else f"Exit {result.get('exit_code', '?')}"), False
        return f"Exit {result.get('exit_code', '?')}", False
    if tool_name in ("command_status", "kill_command", "restart_command"):
        if result.get("running"):
            return ("Restarted, running" if tool_name == "restart_command" else "Still running"), False
        return f"Exit {result.get('exit_code', '?')}", False
    if tool_name == "multi_edit":
        return result.get("summary") or "Edited file", False
    if tool_name == "ask_question":
        noun = "question" if int(result.get("question_count") or 0) == 1 else "questions"
        return (f"Skipped {noun}" if result.get("skipped") else f"Asked {noun}"), False
    if tool_name == "git_log":
        count = result.get("commit_count", 0)
        return f"{count} commit{'s' if count != 1 else ''}", False
    return "", False

def _browser_summary(result: dict) -> str:
    action = str(result.get("action") or "")
    place = str(result.get("title") or result.get("url") or "").strip()[:80]
    if action in ("navigate", "back", "forward", "reload", "new_tab", "switch_tab"):
        return f"Opened {place}" if place else "Opened page"
    if action == "snapshot":
        count = int(result.get("elements") or 0)
        return f"Read page ({count} element{'s' if count != 1 else ''})"
    if action == "screenshot":
        return "Took screenshot"
    if action == "compare":
        verdict = str(result.get("verdict") or "")
        if result.get("compare") == "elements":
            compared = int(result.get("elements_compared") or 0)
            differ = len(result.get("elements") or []) + len(result.get("missing_on_page") or [])
            return f"Compared {compared} element{'s' if compared != 1 else ''}: " + (f"{differ} differ" if differ else "all match")
        return f"Compared: {result.get('similarity')}% match" + (f", {verdict}" if verdict else "")
    if action == "crop":
        return f"Cropped {result.get('target') or 'region'}"[:120]
    if action == "inspect":
        return f"Inspected {result.get('target') or 'page'}"[:120]
    if action == "resize":
        size = result.get("viewport") or {}
        if result.get("viewport_mode") == "fit" and not result.get("device"):
            return "Browser fits its tab"
        return f"Resized browser to {size.get('width')}×{size.get('height')}" + (f" ({result.get('device')})" if result.get("device") else "")
    if action == "figma":
        if result.get("fallback"):
            return f"Opened the Figma link in tab {result.get('tab_id') or ''}".rstrip()
        design = result.get("design") or {}
        layer = result.get("layer") or {}
        return f"Read Figma layer {layer.get('name')}"[:120] if layer else f"Loaded Figma {design.get('name') or 'design'}"[:120]
    if action == "click":
        return f"Clicked {result.get('clicked') or 'element'}"[:120]
    if action == "type":
        return f"Typed into {result.get('typed_into') or 'element'}"[:120]
    if action == "press":
        return f"Pressed {result.get('pressed') or 'key'}"
    if action == "javascript_exec":
        return "Ran page script"
    if action == "scroll":
        return "Scrolled page"
    if action == "wait":
        return "Waited"
    if action in ("tabs", "close_tab"):
        count = len(result.get("tabs") or [])
        return f"{count} tab{'s' if count != 1 else ''} open"
    return "Used browser"


BROWSER_SCREENSHOT_NOTE = "[Screenshots from your browser tool calls in the previous step]"
BROWSER_IMAGES_PER_STEP = 3
BROWSER_IMAGE_MESSAGES_KEPT = 2


def _image_urls_in_content(content: Any) -> list[str]:
    urls: list[str] = []
    if not isinstance(content, list):
        return urls
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("image_url", "input_image"):
            value = block.get("image_url")
            url = value.get("url") if isinstance(value, dict) else value
            if isinstance(url, str) and url.startswith("data:image/"):
                urls.append(url)
        elif kind == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64" and source.get("data"):
                urls.append(f"data:{source.get('media_type') or 'image/png'};base64,{source['data']}")
    return urls


def _browser_image_caption(shot: dict[str, Any]) -> str:
    action = str(shot.get("action") or "")
    url = shot.get("url") or "the page"
    size = f"{shot.get('width')}x{shot.get('height')}"
    if action == "crop" and shot.get("source") != "image":
        region = shot.get("region") or {}
        return (f"Crop of {shot.get('target') or 'a region'} on {url} ({size} px, from x={region.get('x')}, "
                f"y={region.get('y')} in page coordinates):")
    if action == "compare" and shot.get("compare") == "elements":
        return (f"Element by element comparison board for {url}: the design and the page with each element that differs "
                f"numbered as in elements (red: differs, purple: not in the design, orange: not on the page), then a "
                f"close-up of each (design | page). {shot.get('summary') or ''}")
    if action == "compare":
        return (f"Comparison board for {url}: Reference | Page | Difference (pink marks what differs, numbered as in "
                f"differences), {shot.get('similarity')}% match, {shot.get('verdict') or 'compared'}:")
    if action == "figma":
        if shot.get("fallback"):
            return (f"The Figma link, opened in tab {shot.get('tab_id')} because Figma's API is not available "
                    f"({shot.get('reason') or 'no token'}):")
        design = shot.get("design") or {}
        layer = shot.get("layer") or {}
        what = f"layer {layer.get('name')}" if layer else f"frame {design.get('name')}"
        return f"The Figma design, {what} ({design.get('width')}×{design.get('height')} CSS px frame):"
    if action == "crop" and shot.get("source") == "image":
        region = shot.get("reference_region") or {}
        where = f" x={region.get('x')}, y={region.get('y')}, {region.get('width')}×{region.get('height')}" if region else ""
        scale = f", at {shot.get('reference_scale')}x" if shot.get("reference_scale") else ""
        return f"Cut from {shot.get('reference') or 'the image'}{where}{scale} (shot:{shot.get('shot_id')}):"
    tab = f" (tab {shot.get('tab_id')})" if shot.get("tab_id") else ""
    return f"Screenshot of {url}{tab} ({size} CSS pixels; click x/y use these coordinates):"


def _retire_browser_images(messages: list[dict[str, Any]], keep: int) -> None:
    seen = 0
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list) or not content:
            continue
        head = content[0]
        if not (isinstance(head, dict) and str(head.get("text") or "").startswith(BROWSER_SCREENSHOT_NOTE)):
            continue
        if not any(isinstance(block, dict) and block.get("type") == "image_url" for block in content):
            continue
        seen += 1
        if seen <= keep:
            continue
        messages[index] = {
            **msg,
            "content": [
                {"type": "text", "text": "[An older screenshot was removed to save context.]"}
                if isinstance(block, dict) and block.get("type") == "image_url" else block
                for block in content
            ],
        }


def _tool_call_read_only_for_parallel(tc: dict, enabled_servers: set[str]) -> bool:
    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
    tool_name = fn.get("name", "")
    if tool_name == "spawn_subagent":
        args, error = parse_livecode_tool_arguments(tool_name, fn.get("arguments") or "{}")
        return not error and subagent_read_only(args)
    if tool_name not in _READ_ONLY_TOOLS:
        return False
    if tool_name != "call_mcp_tool":
        return True
    args, error = parse_livecode_tool_arguments(tool_name, fn.get("arguments") or "{}")
    if error:
        return False
    server_name = str(args.get("server_name") or "").strip()
    tool = str(args.get("tool_name") or "").strip()
    if server_name not in enabled_servers or not tool:
        return False
    arguments = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
    return classify_mcp_tool_effect(server_name, tool, arguments).effect == MCP_EFFECT_READ


def _memory_upkeep(state_path: str, session_id: str, history: list, model: str, call_summarize, logger=None) -> None:
    try:
        result = maybe_flush_session(state_path, session_id, history, model=model, call_summarize=call_summarize)
        if logger and result and result.get("status") == "written":
            _ide_log(logger, "info", "memory saved", f"chars={result.get('chars')}", sid=_log_session_id(session_id))
    except Exception:
        if logger:
            _ide_log(logger, "debug", "memory flush skipped", sid=_log_session_id(session_id), exc_info=True)
    try:
        maybe_consolidate_memory(state_path, model=model, call_summarize=call_summarize, logger=logger)
    except Exception:
        if logger:
            _ide_log(logger, "debug", "memory consolidation skipped", sid=_log_session_id(session_id), exc_info=True)


def _launch_memory_upkeep(*args) -> None:
    threading.Thread(target=_memory_upkeep, args=args, name="livecode-memory", daemon=True).start()


MAX_PARALLEL_WRITERS = 6


def _plan_tool_batches(
    tool_calls: list,
    enabled_servers: set[str],
    writer_scope: Callable[[dict], FileScope | None] | None = None,
) -> list[list[dict]]:
    groups: list[list[dict]] = []
    last_kind = ""
    scopes: list[FileScope] = []
    for tc in tool_calls:
        scope = None
        if _tool_call_read_only_for_parallel(tc, enabled_servers):
            kind = "read"
        else:
            scope = writer_scope(tc) if writer_scope else None
            kind = "writers" if scope else ""
        joins = bool(kind) and kind == last_kind and (
            kind == "read"
            or (len(groups[-1]) < MAX_PARALLEL_WRITERS and not any(scope.overlaps(other) for other in scopes))
        )
        if joins:
            groups[-1].append(tc)
        else:
            groups.append([tc])
            scopes = []
        if scope:
            scopes.append(scope)
        last_kind = kind
    return groups


def _narration_repeats_completion(narration: str, tool_calls: list) -> bool:
    if len(tool_calls) != 1:
        return False
    fn = (tool_calls[0] or {}).get("function") or {}
    if fn.get("name") != "attempt_completion":
        return False
    args, _error = parse_livecode_tool_arguments("attempt_completion", fn.get("arguments") or "{}")
    result = " ".join(str(args.get("result") or "").split())
    said = " ".join((narration or "").split())
    if not result or not said:
        return False
    return said in result or result in said or said[:80] == result[:80]


def _subagent_mode(parent_mode: str, args: dict) -> str:
    if parent_mode != "agent":
        return "ask"
    return "ask" if subagent_read_only(args) else "agent"


_SUBAGENT_EXCLUDED_TOOLS = frozenset({"spawn_subagent", "todo_write", "update_goal", "create_plan", "ask_question", "update_memory"})


def _execute_one_tool(
    project_path: str,
    tc: dict,
    iteration: int,
    *,
    repo_grep_fn,
    repo_read_fn,
    repo_list_fn,
    repo_ast_fn,
    create_diff_html_fn,
    execute_command_pty_fn,
    socketio,
    session_id: str = "",
    socket_id: str = "",
    require_permissions: bool = False,
    emit_progress_fn=None,
    subagent_runner=None,
    mode: str = "agent",
    files_read_this_turn: set[str] | None = None,
    workspace=None,
    mcp_servers: list[str] | None = None,
    allowed_mcp_tools: dict[str, MCPToolBinding] | None = None,
    state_path: str | None = None,
    checkpoint: dict | None = None,
    cancel_check: Callable[[], bool] | None = None,
    mcp_disabled_tools: dict[str, list[str]] | None = None,
    browser_agent: dict[str, str] | None = None,
) -> dict[str, Any]:
    fn = tc.get("function", {})
    tool_name = fn.get("name", "")
    tool_args, parse_error = parse_livecode_tool_arguments(
        tool_name, fn.get("arguments") or "{}",
    )
    tool_call_id = tc.get("id", "")

    if parse_error:
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": {"error": parse_error},
            "compacted": {"error": parse_error},
            "iteration": iteration,
        }

    mcp_policy = None
    mcp_annotations: dict[str, Any] = {}
    is_generic_mcp_tool = tool_name == "call_mcp_tool"
    try:
        from livecode import mcp_bridge
        is_dynamic_mcp_tool = mcp_bridge.is_mcp_tool_name(tool_name)
        if is_dynamic_mcp_tool:
            binding = (allowed_mcp_tools or {}).get(tool_name)
            if not binding:
                result = {
                    "error": "MCP tool was not offered for this turn",
                    "blocked": True,
                    "reason_code": "tool_not_offered",
                }
                return {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "result": result,
                    "compacted": result,
                    "iteration": iteration,
                }
            mcp_server_name = binding.server_name
            mcp_original_tool_name = binding.tool_name
            mcp_annotations = dict(binding.annotations)
        elif is_generic_mcp_tool:
            mcp_server_name = str(tool_args.get("server_name") or "")
            mcp_original_tool_name = str(tool_args.get("tool_name") or "")
        else:
            mcp_server_name = ""
            mcp_original_tool_name = ""
    except Exception:
        is_dynamic_mcp_tool = False
        mcp_server_name = ""
        mcp_original_tool_name = ""
    is_any_mcp_tool = is_dynamic_mcp_tool or is_generic_mcp_tool

    if (is_dynamic_mcp_tool or is_generic_mcp_tool) and mcp_servers is not None and mcp_server_name not in {str(server) for server in mcp_servers}:
        result = {
            "error": f"MCP server is not enabled for this turn: {mcp_server_name}",
            "blocked": True,
            "reason_code": "disabled_server",
        }
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": result,
            "compacted": result,
            "iteration": iteration,
        }

    if is_generic_mcp_tool and mcp_original_tool_name in set((mcp_disabled_tools or {}).get(mcp_server_name) or ()):
        result = {
            "error": f"The user turned off the MCP tool {mcp_server_name}/{mcp_original_tool_name}.",
            "blocked": True,
            "reason_code": "disabled_tool",
        }
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": result,
            "compacted": result,
            "iteration": iteration,
        }

    if is_any_mcp_tool:
        mcp_effect_args = tool_args.get("arguments") if is_generic_mcp_tool and isinstance(tool_args.get("arguments"), dict) else tool_args
        mcp_policy = classify_mcp_tool_effect(
            server_name=mcp_server_name,
            tool_name=mcp_original_tool_name,
            arguments=mcp_effect_args,
            annotations=mcp_annotations,
        )

    if mcp_policy is not None and mcp_policy.effect == MCP_EFFECT_BLOCKED:
        result = {
            "error": mcp_policy.reason,
            "blocked": True,
            "reason_code": mcp_policy.reason_code,
        }
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": result,
            "compacted": result,
            "iteration": iteration,
        }

    allowed_in_read_only_mode = tool_name in READ_ONLY_TOOL_NAMES or tool_name in {"attempt_completion", STRUCTURED_OUTPUT_TOOL} or (mode == "plan" and tool_name in {"create_plan", "ask_question"})
    if tool_name == "browser":
        allowed_in_read_only_mode = not browser_action_needs_approval(tool_args)
    mcp_writes_in_read_only_mode = (
        is_any_mcp_tool
        and mcp_policy is not None
        and mcp_policy.effect in {MCP_EFFECT_WRITE, MCP_EFFECT_APPROVAL_REQUIRED}
    )
    if mode != "agent" and (not allowed_in_read_only_mode or tool_name in MUTATING_TOOL_NAMES or is_dynamic_mcp_tool or mcp_writes_in_read_only_mode):
        rejected_name = f"browser {tool_args.get('action')}" if tool_name == "browser" else tool_name
        rejection = mode_rejection_message(mode, rejected_name)
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": {"error": rejection},
            "compacted": {"error": rejection},
            "iteration": iteration,
        }

    needs_permission = require_permissions and (tool_name in SENSITIVE_TOOLS or is_any_mcp_tool)
    if tool_name == "run_command" and is_destructive_command(tool_args.get("command", "")):
        needs_permission = True
    if tool_name == "browser" and require_permissions and browser_action_needs_approval(tool_args):
        needs_permission = True
    if needs_permission:
        from livecode.permissions import create_permission_request, wait_for_permission_result
        request_id = create_permission_request(session_id, tool_name, tool_args)
        if emit_progress_fn:
            emit_progress_fn(
                "permission_request",
                f"Approve {tool_name}?",
                request_id=request_id,
                tool=tool_name,
                args=tool_args,
            )
        permission_result = wait_for_permission_result(request_id)
        if permission_result in {"expired", "missing"}:
            if emit_progress_fn:
                emit_progress_fn(
                    "permission_expired",
                    "Permission request expired",
                    request_id=request_id,
                    tool=tool_name,
                )
            error = "Permission request expired" if permission_result == "expired" else "Permission request no longer exists"
            return {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "result": {"error": error},
                "compacted": {"error": error},
                "iteration": iteration,
            }
        if permission_result != "approved":
            return {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "result": {"error": "User denied permission"},
                "compacted": {"error": "User denied permission"},
                "iteration": iteration,
            }

    if tool_name == "ask_question":
        result = _ask_user_questions(
            tool_args,
            session_id=session_id,
            emit_progress_fn=emit_progress_fn,
            cancel_check=cancel_check,
        )
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": result,
            "compacted": {k: v for k, v in result.items() if k != "questions"},
            "iteration": iteration,
        }

    if tool_name == STRUCTURED_OUTPUT_TOOL:
        valid, err = validate_structured_output(tool_args)
        if valid:
            result = {"valid": True, "data": tool_args}
        else:
            result = {"valid": False, "error": err}
        compacted = compact_tool_result_for_llm(tool_name, result)
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": result,
            "compacted": compacted,
            "iteration": iteration,
        }

    if tool_name == "spawn_subagent" and subagent_runner is not None:
        subagent_runner = functools.partial(subagent_runner, agent_id=tool_call_id)
    result = dispatch_tool(
        project_path,
        tool_name,
        tool_args,
        repo_grep_fn=repo_grep_fn,
        repo_read_fn=repo_read_fn,
        repo_list_fn=repo_list_fn,
        repo_ast_fn=repo_ast_fn,
        create_diff_html_fn=create_diff_html_fn,
        execute_command_pty_fn=execute_command_pty_fn,
        socketio=socketio,
        session_id=session_id,
        socket_id=socket_id,
        subagent_runner=subagent_runner,
        files_read_this_turn=files_read_this_turn,
        workspace=workspace,
        mcp_binding=(allowed_mcp_tools or {}).get(tool_name),
        state_path=state_path,
        checkpoint=checkpoint,
        cancel_check=cancel_check,
        browser_agent=browser_agent,
    )
    if tool_name in MUTATING_TOOL_NAMES:
        invalidate_workspace_index()
    compacted = compact_tool_result_for_llm(tool_name, result)
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_args": tool_args,
        "result": result,
        "compacted": compacted,
        "iteration": iteration,
    }

def _ask_user_questions(
    tool_args: dict,
    *,
    session_id: str,
    emit_progress_fn=None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    from livecode.questions import (
        build_answer_result,
        create_question_request,
        normalize_questions,
        wait_for_question_response,
    )

    questions, problem = normalize_questions(tool_args)
    if problem:
        return {"error": problem, "error_kind": "invalid_input"}
    if not emit_progress_fn:
        return {"error": "Questions cannot be shown in this context", "error_kind": "unavailable"}
    request_id = create_question_request(session_id, questions)
    emit_progress_fn(
        "question_request",
        f"{len(questions)} question{'s' if len(questions) != 1 else ''}",
        request_id=request_id,
        questions=questions,
    )
    response = wait_for_question_response(
        request_id,
        is_cancelled=cancel_check or (lambda: is_cancelled(session_id)),
    )
    if response.get("status") in {"cancelled", "expired", "missing"}:
        emit_progress_fn("question_closed", "", request_id=request_id, reason=response["status"])
    result = build_answer_result(questions, response)
    result["questions"] = questions
    return result


def _seed_build_todos(state_path: str, session_id: str, plan_file: str) -> list[dict]:
    from livecode import plan_store

    try:
        plan_todos = plan_store.read_plan(plan_file)["todos"]
    except (FileNotFoundError, ValueError, OSError):
        return []
    if not plan_todos:
        return []
    current = {str(t.get("id")): t for t in load_todo_state(state_path, session_id) if isinstance(t, dict)}
    seeded = [current.get(t["id"]) or dict(t) for t in plan_todos]
    save_todo_state(state_path, session_id, seeded)
    return seeded

def _approved_plan_block(plan_file: str) -> str:
    from livecode import plan_store

    try:
        plan = plan_store.read_plan(plan_file)
    except (FileNotFoundError, ValueError, OSError):
        return ""
    return (
        f"{LIVECODE_PLAN_BUILD_PREFIX}\n\n"
        f'<approved_plan file="{plan["file"]}" title="{plan["title"]}">\n'
        f"{plan['body']}\n"
        "</approved_plan>"
    )

def _prepend_approved_plan(content: str | list, plan_file: str) -> str | list:
    block = _approved_plan_block(plan_file)
    if not block:
        return content
    if isinstance(content, list):
        out: list = []
        merged = False
        for item in content:
            if not merged and isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                out.append({
                    "type": "text",
                    "text": f"{block}\n\n{text}" if text else block,
                })
                merged = True
            else:
                out.append(item)
        if not merged:
            out.insert(0, {"type": "text", "text": block})
        return out
    text = str(content or "").strip()
    return f"{block}\n\n{text}" if text else block

def run_livecode_turn(
    project_path: str,
    question: str,
    chat_history: list[dict],
    *,
    user_content: str | list | None = None,
    user_model: str,
    call_with_tools: Callable,
    call_streaming: Callable | None = None,
    call_summarize: Callable[[str, list[dict[str, str]]], str] | None = None,
    is_azure_model: Callable,
    repo_grep_fn: Callable,
    repo_read_fn: Callable,
    repo_list_fn: Callable,
    repo_ast_fn: Callable,
    create_diff_html_fn: Callable,
    execute_command_pty_fn: Callable,
    socketio: Any,
    session_id: str,
    socket_id: str = "",
    logger: Any = None,
    force_reindex: bool = False,
    require_permissions: bool = False,
    enable_mcp_tools: bool = False,
    enable_web_tools: bool | None = None,
    enable_browser_tools: bool = False,
    supports_images_fn: Callable[[str], bool] | None = None,
    mcp_servers: list[str] | None = None,
    workspace_payload: dict | None = None,
    mode: str = "agent",
    plan_file: str | None = None,
    cancel_since: float | None = None,
    mcp_disabled_tools: dict[str, list[str]] | None = None,
) -> Generator[str, None, None]:
    del chat_history
    del call_streaming

    if enable_web_tools is None:
        enable_web_tools = False
    if user_requests_web_lookup(question):
        enable_web_tools = True

    mode = normalize_mode(mode)
    turn_started = cancel_since if cancel_since is not None else time.monotonic()

    def _turn_cancelled() -> bool:
        return is_cancelled(session_id, since=turn_started)

    effective_user_content: str | list = user_content if user_content is not None else question
    build_plan_file = plan_file if mode == "agent" and plan_file else ""
    if build_plan_file:
        effective_user_content = _prepend_approved_plan(effective_user_content, plan_file)

    thinking_start: float | None = None
    final_thinking: dict[str, Any] = {}
    active_workspace = workspace_roots(project_path, workspace_payload)
    state_path = workspace_state_identity(active_workspace)
    attached_images: list[str] = []
    design_loop_for_turn = False
    enable_browser_for_turn = False
    browser_tabs_note = ""
    browser_open_note = ""
    if enable_browser_tools:
        attached_images = _image_urls_in_content(effective_user_content)
        if attached_images:
            try:
                from livecode.browser import register_attachments

                register_attachments(state_path, session_id, attached_images)
            except Exception:
                if logger:
                    _ide_log(logger, "debug", "browser reference registration failed", sid=_log_session_id(session_id), exc_info=True)
    try:
        prior_messages = load_session(state_path, session_id).get("messages") or []
    except Exception:
        prior_messages = []
    turn_checkpoint = {"turn_id": uuid.uuid4().hex, "user_index": count_user_messages(prior_messages)}
    project_basename = os.path.basename(os.path.abspath(os.path.expanduser(active_workspace.primary_path)))
    prompt_cache_key = f"livecode:{session_id[:24]}"
    turn_id = uuid.uuid4().hex
    emit_room = (socket_id or "").strip() or None
    tool_events: list[dict[str, Any]] = []
    progress_seq = 0
    turn_persist: list[dict[str, Any]] = [{"role": "user", "content": effective_user_content}]
    context_retried = False
    message_seq_retried = False

    def _persist_msg(msg: dict[str, Any]) -> None:
        if msg.get("internal"):
            return
        turn_persist.append(json.loads(json.dumps(msg, default=str)))

    def _finalize_turn_persist(answer: str) -> list[dict[str, Any]]:
        if answer and answer.strip():
            last = turn_persist[-1] if turn_persist else {}
            if last.get("role") != "assistant" or last.get("content") != answer.strip():
                _persist_msg({"role": "assistant", "content": answer.strip(), **final_thinking})
        msgs = list(turn_persist)
        history: list[dict[str, Any]] = []
        try:
            prior = load_session(state_path, session_id).get("messages") or []
            history = list(prior) + msgs
            maybe_autosave_session(state_path, session_id, history)
        except Exception:
            if logger:
                _ide_log(logger, "debug", "memory autosave skipped", sid=_log_session_id(session_id), exc_info=True)
        if call_summarize and history:
            try:
                upkeep_model = _pick_iteration_model(
                    user_model,
                    classification,
                    tool_loop=False,
                    escalate=False,
                    content_chars=_estimate_content_chars(),
                )
                _launch_memory_upkeep(state_path, session_id, history, upkeep_model, call_summarize, logger)
            except Exception:
                if logger:
                    _ide_log(logger, "debug", "memory upkeep skipped", sid=_log_session_id(session_id), exc_info=True)
        return msgs

    def _start_thinking():
        nonlocal thinking_start
        thinking_start = time.monotonic()
        _emit_progress("agent_thinking", "Thinking")

    def _emit_answer_done(answer: str) -> None:
        _emit_progress("answer_done", answer=answer or "", length=len(answer or ""))

    step_stream: StepStream | None = None

    def _on_model_retry(attempt: int, max_retries: int, error: BaseException) -> None:
        nonlocal thinking_start
        if _turn_cancelled():
            raise TurnCancelled()
        thinking_start = time.monotonic()
        stream = step_stream
        if stream is not None and stream.thinking_ended:
            stream.retract()
            _emit_progress("agent_thinking", "Thinking")
        err_brief = str(error or "").replace("\n", " ")[:120]
        _emit_progress(
            "agent_status",
            "Retrying model connection…",
            attempt=attempt,
            max_retries=max_retries,
            error=err_brief,
        )

    emit_lock = threading.Lock()

    def _emit_progress(progress_type: str, message: str = "", **extra):
        with emit_lock:
            _emit_progress_locked(progress_type, message, **extra)

    def _emit_progress_locked(progress_type: str, message: str = "", **extra):
        nonlocal progress_seq
        progress_seq += 1
        if logger and progress_type not in {"agent_thinking_delta", "answer_delta", "agent_status", "usage"}:
            tool_name = extra.get("tool", "") or ""
            short_message = (message or "").replace("\n", " ")[:120]
            if progress_type == "tool_call":
                plain = extra.get("log_message") or describe_tool_start(
                    tool_name, extra.get("args") or {}, short_message
                )
                if plain:
                    _ide_log_plain(logger, "info", plain)
            elif progress_type == "tool_result":
                plain = extra.get("log_message")
                if not plain:
                    plain = describe_tool_result(
                        tool_name,
                        extra.get("args") or {},
                        extra.get("result") if isinstance(extra.get("result"), dict) else None,
                        short_message,
                    )
                level = "warning" if extra.get("error") else "info"
                _ide_log_plain(logger, level, plain)
            elif progress_type == "permission_request":
                _ide_log_plain(logger, "info", f"Waiting for approval: {short_message or tool_name}")
            elif progress_type == "compaction":
                _ide_log_plain(logger, "info", "Compacting conversation history to free context space")
            elif progress_type == "diff_block":
                _ide_log_plain(
                    logger,
                    "info",
                    f"File change ready: {extra.get('file_name') or tool_name}",
                )
            else:
                _ide_log(logger, "debug", progress_type, short_message or None)
        try:
            payload = {
                "session_id": session_id,
                "status": "progress",
                "type": progress_type,
                "message": message,
                "turn_id": turn_id,
                "seq": progress_seq,
                **extra,
            }
            if emit_room:
                socketio.emit("livecode_progress", payload, room=emit_room)
            else:
                socketio.emit("livecode_progress", payload)
        except Exception:
            if logger:
                _ide_log(logger, "debug", "progress emit failed", progress_type, sid=_log_session_id(session_id), exc_info=True)

    def _emit_complete(answer_len: int, turn_summary: str = ""):
        if logger:
            _ide_log_plain(
                logger,
                "info",
                describe_turn_complete(
                    answer_len,
                    turn_token_totals,
                    turn_usage_by_model,
                    project_basename,
                ),
            )
        turn_cost_usd = sum(
            estimate_usage_cost_usd(
                model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(usage.get("cached_tokens") or 0),
            )
            for model, usage in turn_usage_by_model.items()
        )
        try:
            complete_payload = {
                "session_id": session_id,
                "status": "complete",
                "turn_id": turn_id,
                "seq": progress_seq + 1,
                "cost_usd": turn_cost_usd,
                "prompt_tokens": turn_token_totals.get("prompt_tokens", 0),
                "completion_tokens": turn_token_totals.get("completion_tokens", 0),
                "cached_tokens": turn_token_totals.get("cached_tokens", 0),
            }
            if emit_room:
                socketio.emit("livecode_progress", complete_payload, room=emit_room)
            else:
                socketio.emit("livecode_progress", complete_payload)
        except Exception:
            if logger:
                _ide_log(logger, "debug", "completion emit failed", sid=_log_session_id(session_id), exc_info=True)

    def _emit_diff(result: dict, tool_call_id: str = ""):
        if not result.get("diff_html"):
            return
        file_name = result.get("file_path", "")
        additions = result.get("additions", 0)
        deletions = result.get("deletions", 0)
        if additions == 0 and deletions == 0:
            return
        absolute_path = result.get("absolute_path", "")
        created = bool(result.get("is_new_file"))
        if logger:
            _ide_log(logger, "info", "diff", file_name, f"+{additions}/-{deletions}")
        try:
            _emit_progress(
                "diff_block",
                result.get("diff_html", ""),
                file_name=file_name,
                additions=additions,
                deletions=deletions,
                absolute_path=absolute_path,
                created=created,
            )
        except Exception:
            if logger:
                _ide_log(logger, "debug", "diff emit failed", file_name, sid=_log_session_id(session_id), exc_info=True)
        try:
            save_diff_record(
                state_path,
                session_id,
                tool_call_id,
                file_name=file_name,
                diff_html=result.get("diff_html", ""),
                additions=additions,
                deletions=deletions,
                absolute_path=absolute_path,
                created=created,
            )
        except Exception:
            if logger:
                _ide_log(logger, "exception", "Failed to persist diff artifact", sid=_log_session_id(session_id), exc_info=True)

    allowed_mcp_tools: dict[str, MCPToolBinding] = {}

    def _estimate_content_chars(messages=None):
        chars = len(question)
        if messages:
            chars = max(chars, estimate_messages_tokens(messages) * 4)
        return chars

    def _maybe_session_compact(*, force: bool = False) -> bool:
        if not call_summarize:
            return False
        compact_model = _pick_iteration_model(
            user_model,
            classification,
            tool_loop=False,
            escalate=False,
            content_chars=_estimate_content_chars(),
        )
        record = maybe_compact_session(
            state_path,
            session_id,
            model=compact_model,
            call_summarize=call_summarize,
            force=force,
        )
        if record:
            record_compaction_ran(state_path, session_id)
            _emit_progress(
                "compaction",
                "Compacted conversation history",
                forced=force,
                boundary_index=record.get("boundary_index"),
                strategy=record.get("strategy", "full_replace"),
            )
            return True
        return False

    def _writer_scope_for_args(args: dict) -> FileScope | None:
        entries = scope_entries(args)
        if not entries:
            return None
        return FileScope(entries, lambda raw: edit_target_path(project_path, {"file_path": raw}, active_workspace))

    def _writer_scope_for_call(tc: dict) -> FileScope | None:
        fn = tc.get("function") or {}
        if fn.get("name") != "spawn_subagent" or mode != "agent" or require_permissions:
            return None
        args, error = parse_livecode_tool_arguments("spawn_subagent", fn.get("arguments") or "{}")
        if error or not is_scoped_writer_call(args):
            return None
        return _writer_scope_for_args(args) or None

    usage_lock = threading.Lock()

    def _subagent_runner(proj: str, args: dict, parent_sid: str, agent_id: str = "") -> dict:
        from livecode.subagent import run_subagent_turn

        sub_model = _pick_iteration_model(
            user_model,
            classification,
            tool_loop=True,
            escalate=escalate,
            content_chars=_estimate_content_chars(),
        )

        child_mode = _subagent_mode(mode, args)
        read_only = child_mode != "agent"
        session_id_for_tools = parent_sid or session_id
        title = subagent_title(args)
        progress = SubagentProgress(
            agent_id,
            title,
            emit=lambda payload: _emit_progress("subagent_update", title, **payload),
            model=sub_model,
        )
        scope = None if read_only else _writer_scope_for_args(args)
        if not read_only and scope_entries(args) and not scope:
            error = "None of this writer's files are inside the workspace: " + ", ".join(scope_entries(args)[:6])
            progress.finish("failed", error)
            return {"success": False, "error": error, "result": error, "title": title, "goal": args.get("goal", ""),
                    "read_only": False, **progress.display_fields()}
        excluded_tools = _SUBAGENT_EXCLUDED_TOOLS | (SCOPED_WRITER_EXCLUDED_TOOLS if scope else frozenset())
        browser_agent = {"id": agent_id or uuid.uuid4().hex[:12], "label": title}
        try:
            own_browser_tabs = enable_browser_for_turn and browser_agent_tabs_enabled()
        except Exception:
            own_browser_tabs = False

        def _mini_turn(project_path: str, question: str, session_id: str = "", max_iterations: int = SUBAGENT_MAX_ITERATIONS, **kwargs):
            del kwargs, session_id
            child_messages = [
                {"role": "system", "content": build_subagent_system_prompt(
                    project_path, read_only=read_only, files=scope.entries if scope else None,
                    browser=own_browser_tabs,
                )},
                {"role": "user", "content": question},
            ]
            child_tools_result = get_livecode_tools(
                enable_mcp=enable_mcp_tools,
                enable_web=enable_web_tools,
                enable_browser=enable_browser_for_turn,
                project_path=project_path,
                mcp_servers=mcp_servers,
                workspace_payload=workspace_payload,
                include_mcp_bindings=True,
                mcp_disabled_tools=mcp_disabled_tools,
            )
            if isinstance(child_tools_result, tuple):
                child_tools, child_allowed_mcp_tools = child_tools_result
            else:
                child_tools, child_allowed_mcp_tools = child_tools_result, {}
            child_tools = [
                t for t in filter_tools_for_mode(child_tools, child_mode)
                if (t.get("function") or {}).get("name") not in excluded_tools
            ]
            child_allowed_mcp_tools = {
                name: binding
                for name, binding in child_allowed_mcp_tools.items()
                if any((item.get("function") or {}).get("name") == name for item in child_tools)
            }
            enabled_servers = {str(server) for server in (mcp_servers or []) if str(server).strip()}
            budget = int(working_context_tokens(sub_model) * LIVECODE_IN_TURN_COMPACT_RATIO)
            steps = max(1, min(int(max_iterations or SUBAGENT_MAX_ITERATIONS), SUBAGENT_MAX_ITERATIONS))
            answer = ""
            sub_usage_by_model: dict[str, dict[str, int]] = {}

            def _call(msgs: list, tools_: list) -> dict:
                resp = call_host_model(
                    call_with_tools,
                    sub_model,
                    msgs,
                    tools_,
                    tool_choice="auto" if tools_ else None,
                    prompt_cache_key=f"{prompt_cache_key}:sub",
                    max_completion_tokens=max_output_tokens_for_model(sub_model),
                )
                accumulate_usage_by_model(sub_usage_by_model, sub_model, resp)
                return resp or {}

            for _step in range(steps):
                if _turn_cancelled():
                    answer = answer or "Stopped by the user."
                    break
                child_messages = compact_stale_tool_messages(child_messages, max_input_tokens=budget)
                resp = _call(sanitize_messages_for_api(child_messages), child_tools)
                tcs = resp.get("tool_calls")
                if not tcs:
                    answer = resp.get("content") or answer
                    break
                if _turn_cancelled():
                    answer = answer or "Stopped by the user."
                    break
                child_messages.append({
                    "role": "assistant",
                    "content": resp.get("content") or "",
                    "tool_calls": tcs,
                })
                exec_kwargs = dict(
                    repo_grep_fn=repo_grep_fn,
                    repo_read_fn=repo_read_fn,
                    repo_list_fn=repo_list_fn,
                    repo_ast_fn=repo_ast_fn,
                    create_diff_html_fn=create_diff_html_fn,
                    execute_command_pty_fn=execute_command_pty_fn,
                    socketio=None,
                    session_id=session_id_for_tools,
                    socket_id=emit_room or "",
                    require_permissions=require_permissions,
                    emit_progress_fn=_emit_progress,
                    subagent_runner=None,
                    workspace=active_workspace,
                    mcp_servers=mcp_servers,
                    allowed_mcp_tools=child_allowed_mcp_tools,
                    state_path=state_path,
                    mode=child_mode,
                    files_read_this_turn=files_read_this_turn,
                    checkpoint=turn_checkpoint,
                    cancel_check=_turn_cancelled,
                    mcp_disabled_tools=mcp_disabled_tools,
                    browser_agent=browser_agent,
                )

                def _run_child_call(tc: dict) -> dict:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    call_args, _parse_error = parse_livecode_tool_arguments(name, fn.get("arguments") or "{}")
                    progress.tool_started(name, call_args)
                    if scope and name in FILE_EDIT_TOOL_NAMES and not scope.allows(
                        edit_target_path(project_path, call_args, active_workspace)
                    ):
                        refused = {
                            "error": (
                                f"{call_args.get('file_path') or 'That file'} is not one of your files "
                                f"({', '.join(scope.entries)}); another agent may be editing it. Leave it "
                                "alone and say in your final message what change it needs."
                            ),
                            "error_kind": "not_owned",
                        }
                        item = {"tool_call_id": tc.get("id", ""), "tool_name": name, "tool_args": call_args,
                                "result": refused, "compacted": refused, "iteration": 1}
                    else:
                        item = _execute_one_tool(project_path, tc, 1, **exec_kwargs)
                    progress.tool_finished(item["tool_name"], item.get("tool_args") or {}, item.get("result"))
                    return item

                items = []
                for group in _plan_tool_batches(tcs, enabled_servers):
                    if len(group) > 1:
                        with ThreadPoolExecutor(max_workers=min(len(group), 8)) as pool:
                            items.extend(pool.map(_run_child_call, group))
                    else:
                        items.append(_run_child_call(group[0]))
                finished = False
                for item in items:
                    child_messages.append({
                        "role": "tool",
                        "tool_call_id": item["tool_call_id"],
                        "content": json.dumps(item["compacted"], default=str),
                    })
                    result = item["result"] if isinstance(item.get("result"), dict) else {}
                    if item["tool_name"] == "attempt_completion" and result.get("completed"):
                        answer = result.get("result", "")
                        finished = True
                if finished:
                    break
            else:
                wrap_up = child_messages + [{
                    "role": "user",
                    "content": "You are out of steps. Report your findings now, without calling tools.",
                }]
                try:
                    answer = (_call(sanitize_messages_for_api(wrap_up), []).get("content") or "").strip() or answer
                except Exception:
                    if logger:
                        _ide_log(logger, "debug", "subagent wrap-up failed", exc_info=True)
            yield f"data: {json.dumps({'answer': answer, 'done': True, 'usage_by_model': sub_usage_by_model})}\n\n"

        try:
            sub_result = run_subagent_turn(
                project_path=proj,
                goal=args.get("goal", ""),
                parent_session_id=parent_sid or session_id,
                run_turn_fn=_mini_turn,
                read_only=read_only,
                max_iterations=SUBAGENT_MAX_ITERATIONS,
            )
        finally:
            if enable_browser_for_turn:
                try:
                    browser_release_agent(state_path, session_id_for_tools, browser_agent["id"])
                except Exception:
                    pass
        if _turn_cancelled():
            state = "stopped"
        elif sub_result.get("error"):
            state = "failed"
        else:
            state = "done"
        progress.finish(state, str(sub_result.get("error") or ""))
        sub_result.update({"title": title, **progress.display_fields()})
        if scope:
            sub_result["files"] = scope.entries
        sub_usage = sub_result.pop("usage_by_model", None) or {}
        with usage_lock:
            for sub_model_name, sub_usage_bucket in sub_usage.items():
                parent_bucket = turn_usage_by_model.setdefault(
                    sub_model_name, {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
                )
                for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                    delta = int(sub_usage_bucket.get(key) or 0)
                    parent_bucket[key] = parent_bucket.get(key, 0) + delta
                    turn_token_totals[key] = turn_token_totals.get(key, 0) + delta
        return sub_result

    def _mode_prompt_block() -> str:
        if mode == "ask":
            return LIVECODE_ASK_MODE_PROMPT
        if mode != "plan":
            return ""
        block = LIVECODE_PLAN_MODE_PROMPT
        if plan_file:
            from livecode import plan_store

            try:
                existing = plan_store.read_plan(plan_file)
            except (FileNotFoundError, ValueError, OSError):
                existing = None
            if existing:
                block += "\n\n" + LIVECODE_PLAN_REENTRY_REMINDER_TEMPLATE.format(
                    plan_file=existing["file"],
                    plan_title=existing["title"],
                )
        return block

    prefetched: dict[str, Any] = {}

    def _memory_context_for(query: str) -> str:
        try:
            return build_memory_context(state_path, query, min_score=0.0)
        except Exception:
            if logger:
                _ide_log(logger, "debug", "memory context failed", sid=_log_session_id(session_id), exc_info=True)
            return ""

    def _prefetch_prompt_context() -> None:
        try:
            prefetched["rules"] = load_workspace_rules_reminder(active_workspace)
            prefetched["memory"] = _memory_context_for(question)
            prefetched["layout"] = _workspace_layout_block(
                project_path, active_workspace, _workspace_index_summary(project_path, active_workspace)
            )
        except Exception:
            if logger:
                _ide_log(logger, "debug", "prompt context prefetch failed", sid=_log_session_id(session_id), exc_info=True)

    def _prefetch_mcp_tools() -> None:
        try:
            from livecode import mcp_bridge

            mcp_bridge.list_mcp_tools_with_bindings(
                project_path,
                mcp_servers,
                workspace_payload=workspace_payload,
                tool_denylist_by_server=mcp_disabled_tools or None,
            )
        except Exception:
            if logger:
                _ide_log(logger, "debug", "mcp tool prefetch failed", sid=_log_session_id(session_id), exc_info=True)

    def _parallel_agents_hint() -> str:
        if mode != "agent" or not isinstance(classification, dict):
            return ""
        if classification.get("edit_scope") not in ("multi_file", "bulk") or classification.get("complexity") != "complex":
            return ""
        return PARALLEL_AGENTS_HINT

    def _build_base_messages(brief_summary: str, *, include_layout: bool = True) -> list[dict[str, Any]]:
        rules_reminder = prefetched.pop("rules") if "rules" in prefetched else load_workspace_rules_reminder(active_workspace)
        compacted = has_valid_compaction(state_path, session_id)
        reminders = "\n".join(part for part in (build_reminder_text(state_path, session_id), _parallel_agents_hint(), browser_tabs_note, browser_open_note) if part)
        memory = prefetched.pop("memory") if "memory" in prefetched else _memory_context_for(question)
        prefetched_layout = prefetched.pop("layout", None)
        system_content = build_system_prompt(
            project_path,
            has_project_rules=bool(rules_reminder),
        )
        mode_block = _mode_prompt_block()
        if mode_block:
            system_content = f"{system_content}\n\n{mode_block}"
        if design_loop_for_turn:
            system_content = f"{system_content}\n\n{DESIGN_LOOP_PROMPT}"
            if browser_figma_configured():
                system_content = f"{system_content}\n{DESIGN_LOOP_FIGMA_NOTE}"
        if enable_mcp_for_turn:
            try:
                from livecode import mcp_bridge
                mcp_context = mcp_bridge.connected_mcp_context(
                    project_path, mcp_servers, workspace_payload=workspace_payload, disabled_tools=mcp_disabled_tools
                )
                if mcp_context:
                    system_content = f"{system_content}\n\n{mcp_context}"
            except Exception:
                pass
        projected = get_projected_messages(
            state_path,
            session_id,
            effective_user_content,
            rules_reminder=rules_reminder if compacted else "",
            wrap_query=True,
        )
        clear_compaction_reminder(state_path, session_id)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
        if include_layout and not compacted:
            layout = prefetched_layout if prefetched_layout is not None else _workspace_layout_block(
                project_path, active_workspace, _workspace_index_summary(project_path, active_workspace)
            )
            if layout:
                messages.append({"role": "user", "content": layout})
        if rules_reminder and not compacted:
            messages.append({"role": "user", "content": rules_reminder})

        turn_ctx = build_turn_context_block(
            "" if compacted else brief_summary,
            memory,
            "" if compacted else reminders,
        )
        if turn_ctx and projected:
            messages.extend(projected[:-1])
            messages.append({"role": "user", "content": turn_ctx})
            messages.append(projected[-1])
        else:
            messages.extend(projected)
        return sanitize_messages_for_api(messages)

    if logger:
        _ide_log_plain(
            logger,
            "info",
            describe_turn_start(project_basename, mode, user_model, len(question)),
            sid=_log_session_id(session_id),
        )

    prep_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="livecode-prep")
    classify_future = prep_pool.submit(
        _classify_turn,
        user_model,
        question,
        call_with_tools,
        call_summarize,
        project_path=state_path,
        session_id=session_id,
        logger=logger,
    )
    context_future = prep_pool.submit(_prefetch_prompt_context)
    if enable_mcp_tools and mcp_servers:
        prep_pool.submit(_prefetch_mcp_tools)
    try:
        index = build_workspace_index(project_path, force=force_reindex)
        symbol_mgr = get_codebase_index(project_path)
        symbol_stats = symbol_mgr.stats()
        classification, has_prior_turns = classify_future.result()
        context_future.result()
    finally:
        prep_pool.shutdown(wait=False)
    escalate = False
    stationarity = _IdenticalToolCallRun()
    search_scatter = _SearchScatterRun()
    directory_drill = _DirectoryDrillRun()
    exploration_streak = _ExplorationStreakRun()
    edit_no_match = _EditNoMatchRun()
    todo_nudger = _TodoNudgeRun()
    todo_gate_fires = 0
    design_rounds = _DesignRounds()
    ui_verify = _UiVerify()
    last_diag_sig_by_file: dict[str, str] = {}
    diagnostics_blocked_completion = False
    interjection_extensions = 0
    consecutive_tool_errors = 0
    iteration_budget_nudges_sent: set[int] = set()
    last_edit_iteration: int | None = None
    post_edit_nudged = False
    consecutive_test_failures = 0
    test_failure_nudged = False
    files_read_this_turn: set[str] = set()
    edit_completed_this_turn = False

    try:
        _goal = load_goal_state(state_path, session_id)
        if not _goal.get("text"):
            _seed = (question or "").strip().replace("\n", " ")
            save_goal_state(state_path, session_id, {
                "text": _seed[:400] or "(unstated)",
                "status": "active",
                "updates": [],
            })
    except Exception:
        pass
    turn_token_totals: dict[str, int] = {}
    turn_usage_by_model: dict[str, dict[str, int]] = {}

    if build_plan_file:
        build_todos = _seed_build_todos(state_path, session_id, build_plan_file)
        if build_todos:
            _emit_progress("todo_update", "Task list", tool="todo_write", todos=build_todos, plan_file=build_plan_file)

    if logger:
        _ide_log_plain(
            logger,
            "info",
            f"Intelligent classify: {describe_intelligent_classification(classification)}",
        )
        _ide_log(logger, "debug", "classify detail", classification)

    summary = index_summary_brief(index, symbol_index=symbol_stats)
    file_count = index.get("file_count", 0)
    from_cache = index.get("from_cache", False)
    if logger:
        _ide_log(
            logger,
            "debug" if from_cache else "info",
            "index",
            f"{file_count} files",
            "cache" if from_cache else "built",
        )

    _maybe_session_compact()
    use_structured_output = wants_structured_json(question)
    mcp_tool_intent = user_requests_mcp_or_tool_use(question)
    web_tool_intent = user_requests_web_lookup(question)
    browser_intent = bool(enable_browser_tools) and user_requests_browser(question)
    pure_chat_turn = bool(classification.get("chat_only")) and not use_structured_output and not mcp_tool_intent and not web_tool_intent and not browser_intent
    enable_mcp_for_turn = enable_mcp_tools and (not pure_chat_turn or mcp_tool_intent)
    enable_web_for_turn = enable_web_tools and (not pure_chat_turn or web_tool_intent)
    enable_browser_for_turn = bool(enable_browser_tools) and not pure_chat_turn
    if enable_browser_for_turn and browser_playwright_installed():
        try:
            design_loop_for_turn = user_requests_design_work(question, has_images=bool(attached_images)) or \
                browser_design_context(state_path, session_id)
        except Exception:
            design_loop_for_turn = False
        try:
            browser_tabs_note = browser_turn_context(state_path, session_id)
        except Exception:
            browser_tabs_note = ""
        # "Go to github.com and …", "open localhost:3000", "check it in the browser": the page opens in the
        # user's Browser tab, which the UI brings forward as soon as the browser navigates. Without this the
        # model tends to fetch the page as text, or to open the browser only when told to in so many words.
        wanted = browse_request(question)
        if wanted:
            steps = str(wanted.get("steps") or "")
            then = (f" Then do what they asked there, in the browser, step by step: {steps} (snapshot to find the "
                    "controls, then click and type; read the result off the page, and say what you found).") if steps else \
                " Then read the page with snapshot or a screenshot."
            if wanted.get("app"):
                browser_open_note = (
                    "The user wants to see their app in the built-in browser. Open it there with browser {action: \"navigate\", "
                    "url}: the URL of its dev server (a background command you started prints it: command_status; a tab "
                    "may already show it), or start the dev server first (its script in package.json or the like, with "
                    "run_command background: true) and open the URL it prints." + then +
                    " Do not ask whether to open the browser."
                )
            else:
                browser_open_note = (
                    f"The user asked you to open {wanted['site']}: open it in the built-in browser now, with browser "
                    "{action: \"navigate\", url} (it appears in their Browser tab, which opens by itself)." + then +
                    " Do not fetch it as text with web_fetch instead, and do not ask whether to open the browser."
                )
    messages = _build_base_messages(summary)
    tools_result = get_livecode_tools(
        enable_mcp=enable_mcp_for_turn,
        enable_web=enable_web_for_turn,
        enable_browser=enable_browser_for_turn,
        include_structured_output=use_structured_output,
        project_path=project_path,
        mcp_servers=mcp_servers,
        workspace_payload=workspace_payload,
        include_mcp_bindings=True,
        mcp_disabled_tools=mcp_disabled_tools,
    )
    if isinstance(tools_result, tuple):
        tools, allowed_mcp_tools = tools_result
    else:
        tools, allowed_mcp_tools = tools_result, {}
    tools = filter_tools_for_mode(tools, mode)
    allowed_mcp_tools = {
        name: binding
        for name, binding in allowed_mcp_tools.items()
        if any((item.get("function") or {}).get("name") == name for item in tools)
    }
    if use_structured_output and messages:
        messages[0] = {
            "role": "system",
            "content": messages[0].get("content", "") + "\n\n" + LIVECODE_STRUCTURED_OUTPUT_REMINDER,
        }

    full_answer = ""
    completed = False
    codebase_recovery_used = False
    force_tool_choice_required = False
    structured_output_retries = 0
    max_iterations = max_iterations_for_mode(mode)
    budget_nudge_points = iteration_budget_nudge_points(max_iterations)
    in_turn_squeezes = 0

    def _cancelled_finish() -> Generator[str, None, None]:
        stop_note = "Stopped by the user before finishing."
        if logger:
            _ide_log(logger, "info", "turn stopped by user", sid=_log_session_id(session_id))
        turn_summary = build_turn_activity_summary(tool_events)
        turn_messages = _finalize_turn_persist(stop_note)
        yield f"data: {json.dumps({'done': True, 'cancelled': True, 'answer': stop_note, 'turn_summary': turn_summary, 'turn_messages': turn_messages})}\n\n"
        _emit_complete(len(stop_note), turn_summary)

    try:
        for iteration in range(1, max_iterations + 1):
            if _turn_cancelled():
                yield from _cancelled_finish()
                return
            needs_flagship = needs_flagship_edit(classification)
            code_turn = needs_code_change(question, classification)
            edit_pending = (
                code_turn
                and bool(files_read_this_turn)
                and not edit_completed_this_turn
            )
            content_chars = _estimate_content_chars(messages)
            turn_has_images = _messages_have_images(messages)
            iteration_model = _pick_iteration_model(
                user_model,
                classification,
                tool_loop=True,
                escalate=escalate,
                content_chars=content_chars,
                edit_pending=edit_pending,
                edit_completed=edit_completed_this_turn,
                needs_flagship=needs_flagship,
                images=turn_has_images,
                code=code_turn,
            )
            route_reason = None
            if _is_auto_model(user_model):
                route_reason = _auto_route_reason(
                    classification,
                    tool_loop=True,
                    escalate=escalate,
                    content_chars=content_chars,
                    edit_pending=edit_pending,
                    edit_completed=edit_completed_this_turn,
                    needs_flagship=needs_flagship,
                    model=iteration_model,
                    images=turn_has_images,
                    code=code_turn,
                )

            if stationarity.should_hard_stop():
                full_answer = (
                    f"Stopped after {stationarity.run_len} identical calls to "
                    f"`{stationarity.tool_name}` with the same arguments. "
                    "Try a different approach or ask a more specific question."
                )
                if logger:
                    _ide_log(
                        logger,
                        "info",
                        "nudge stationarity-stop",
                        f"tool={stationarity.tool_name}",
                        f"run={stationarity.run_len}",
                    )
                turn_summary = build_turn_activity_summary(tool_events)
                turn_messages = _finalize_turn_persist(full_answer)
                yield f"data: {json.dumps({'done': True, 'answer': full_answer, 'turn_summary': turn_summary, 'turn_messages': turn_messages})}\n\n"
                _emit_answer_done(full_answer)
                _emit_complete(len(full_answer), turn_summary)
                return

            if stationarity.take_nudge():
                nudge_text = STATIONARITY_NUDGE_TEMPLATE.format(
                    tool_name=stationarity.tool_name,
                    run_len=stationarity.run_len,
                )
                nudge = {"role": "user", "content": nudge_text, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                escalate = True
                if logger:
                    _ide_log(
                        logger,
                        "info",
                        "nudge stationarity",
                        f"tool={stationarity.tool_name}",
                        f"run={stationarity.run_len}",
                    )

            if search_scatter.take_nudge():
                nudge_text = SEARCH_SCATTER_NUDGE_TEMPLATE.format(run_len=search_scatter.run_len)
                nudge = {"role": "user", "content": nudge_text, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                if logger:
                    _ide_log(logger, "info", "nudge search-scatter", f"run={search_scatter.run_len}")

            if directory_drill.take_nudge():
                nudge_text = DIRECTORY_DRILL_NUDGE_TEMPLATE.format(run_len=directory_drill.run_len)
                nudge = {"role": "user", "content": nudge_text, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                if logger:
                    _ide_log(logger, "info", "nudge directory-drill", f"run={directory_drill.run_len}")

            if exploration_streak.take_nudge():
                streak_template = (
                    EXPLORATION_STREAK_NUDGE_TEMPLATE if mode == "agent"
                    else EXPLORATION_STREAK_READ_ONLY_NUDGE_TEMPLATE
                )
                nudge_text = streak_template.format(
                    run_len=exploration_streak.run_len,
                )
                nudge = {"role": "user", "content": nudge_text, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                if logger:
                    _ide_log(logger, "info", "nudge exploration-streak", f"run={exploration_streak.run_len}")

            if edit_no_match.take_nudge():
                nudge_text = EDIT_NO_MATCH_NUDGE_TEMPLATE.format(run_len=edit_no_match.run_len)
                nudge = {"role": "user", "content": nudge_text, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                escalate = True
                if logger:
                    _ide_log(logger, "info", "nudge edit-no-match", f"run={edit_no_match.run_len}")

            if mode == "agent" and todo_nudger.iters_since >= TODO_NUDGE_AFTER:
                _multistep = (
                    needs_code_change(question, classification)
                    or not classification.get("chat_only")
                )
                _todos_empty = _multistep and not load_todo_state(state_path, session_id)
                if todo_nudger.take_nudge(iteration, multistep=_multistep, todo_list_empty=_todos_empty):
                    nudge = {"role": "user", "content": TODO_NUDGE_TEMPLATE, "internal": True}
                    messages.append(nudge)
                    _persist_msg(nudge)
                    if logger:
                        _ide_log(logger, "info", "nudge todo-list", f"iters_since={todo_nudger.iters_since}")

            if iteration in budget_nudge_points and iteration not in iteration_budget_nudges_sent:
                remaining = max_iterations - iteration + 1
                nudge_text = ITERATION_BUDGET_NUDGE_TEMPLATE.format(remaining=remaining)
                nudge = {"role": "user", "content": nudge_text, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                iteration_budget_nudges_sent.add(iteration)
                if logger:
                    _ide_log(
                        logger,
                        "info",
                        "nudge iteration-budget",
                        f"iter={iteration}",
                        f"remaining={remaining}",
                    )

            if (
                last_edit_iteration is not None
                and not post_edit_nudged
                and iteration - last_edit_iteration >= POST_EDIT_COMPLETION_NUDGE_AFTER
            ):
                nudge = {"role": "user", "content": POST_EDIT_COMPLETION_NUDGE_TEMPLATE, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                post_edit_nudged = True
                if logger:
                    _ide_log(logger, "info", "nudge post-edit", f"iter={iteration}")

            if consecutive_test_failures >= TEST_FAILURE_NUDGE_AFTER and not test_failure_nudged:
                nudge = {"role": "user", "content": TEST_FAILURE_NUDGE_TEMPLATE, "internal": True}
                messages.append(nudge)
                _persist_msg(nudge)
                test_failure_nudged = True
                if logger:
                    _ide_log(logger, "info", "nudge test-failure", f"failures={consecutive_test_failures}")

            iteration_tools = tools
            if iteration > max_iterations - CLOSURE_ITERATIONS:
                iteration_tools = _attempt_completion_only_tools(tools)

            if logger:
                _ide_log_plain(
                    logger,
                    "info",
                    describe_iteration_start(
                        iteration,
                        max_iterations,
                        iteration_model,
                        user_model if _is_auto_model(user_model) else None,
                        route_reason=route_reason,
                    ),
                )

            _start_thinking()

            if not is_azure_model(iteration_model):
                err = "No model is set up. Add an API key in LiveCode Settings > Models."
                if logger:
                    _ide_log(logger, "warning", "invalid model", iteration_model, sid=_log_session_id(session_id))
                yield f"data: {json.dumps({'error': err})}\n\n"
                return

            budget = int(working_context_tokens(iteration_model) * LIVECODE_IN_TURN_COMPACT_RATIO)
            messages = compact_stale_tool_messages(messages, max_input_tokens=budget)

            pending = drain_interjections(session_id)
            for interjection in pending:
                user_msg = {"role": "user", "content": f"[User interjection]\n{interjection}"}
                messages.append(user_msg)
                _persist_msg(user_msg)

            if estimate_messages_tokens(messages) > budget:
                messages = compact_stale_tool_messages(messages, max_input_tokens=budget)

            messages = sanitize_messages_for_api(messages)

            tool_choice = pick_tool_choice(
                iteration,
                classification,
                question,
                has_prior_turns=has_prior_turns,
                force_required=force_tool_choice_required,
            )
            force_tool_choice_required = False
            if logger and iteration == 1:
                _ide_log(
                    logger,
                    "debug",
                    "tool_choice",
                    tool_choice,
                    "chat_only" if classification.get("chat_only") else None,
                    "needs_evidence" if needs_codebase_evidence(question, has_prior_turns=has_prior_turns) else None,
                )

            model_call_started = time.monotonic()
            if logger:
                _ide_log_plain(
                    logger,
                    "info",
                    f"Asking the model: {len(messages)} messages, {len(iteration_tools)} tools available"
                    + (" (chat only)" if classification.get("chat_only") else ""),
                )

            step_stream = StepStream(
                _emit_progress,
                started_at=thinking_start or model_call_started,
                cancel_check=_turn_cancelled,
            )
            stop_watchdog = threading.Event()

            def _watchdog(stream: StepStream = step_stream, started: float = model_call_started, iter_no: int = iteration, model_name: str = iteration_model) -> None:
                warn_after_s = 20
                interval_s = 30
                last_status: float | None = None
                while not stop_watchdog.wait(0.1):
                    stream.tick()
                    now = time.monotonic()
                    if now - stream.last_activity < warn_after_s:
                        continue
                    if last_status is not None and now - last_status < interval_s:
                        continue
                    last_status = now
                    elapsed = now - started
                    _emit_progress(
                        "agent_status",
                        f"Still waiting for model response… ({int(elapsed)}s)",
                        elapsed_s=elapsed,
                        iteration=iter_no,
                        model=model_name,
                        phase="model_call",
                    )
                    if logger:
                        _ide_log(
                            logger,
                            "debug",
                            "model call waiting",
                            f"iter={iter_no}",
                            model_name,
                            f"elapsed={elapsed:.1f}s",
                            sid=_log_session_id(session_id),
                        )

            threading.Thread(target=_watchdog, name="livecode-model-watchdog", daemon=True).start()

            try:
                response = call_host_model(
                    call_with_tools,
                    iteration_model,
                    messages,
                    iteration_tools,
                    tool_choice=tool_choice,
                    prompt_cache_key=prompt_cache_key,
                    max_completion_tokens=max_output_tokens_for_model(iteration_model),
                    on_thought_delta=step_stream.on_legacy,
                    on_reasoning_delta=step_stream.on_reasoning,
                    on_content_delta=step_stream.on_content,
                    on_tool_call_delta=step_stream.on_tool_call,
                    on_retry=_on_model_retry,
                ) or {}
            except Exception as api_err:
                if isinstance(api_err, TurnCancelled) or _turn_cancelled():
                    stop_watchdog.set()
                    step_stream.finish()
                    yield from _cancelled_finish()
                    return
                step_stream.retract()
                err_msg = str(api_err)
                if logger:
                    _ide_log(
                        logger,
                        "exception",
                        "API error",
                        iteration_model,
                        err_msg[:200],
                        sid=_log_session_id(session_id),
                        exc_info=True,
                    )
                low_err = err_msg.lower()
                is_tool_seq_err = (
                    "tool_calls" in low_err
                    and "role" in low_err
                    and "tool" in low_err
                )
                if is_tool_seq_err and not message_seq_retried:
                    message_seq_retried = True
                    messages = sanitize_messages_for_api(_build_base_messages(summary) + list(turn_persist[1:]))
                    if logger:
                        _ide_log(logger, "warning", "retry tool-message sequencing", sid=_log_session_id(session_id))
                    continue
                context_overflow = any(marker in low_err for marker in _CONTEXT_OVERFLOW_MARKERS)
                if context_overflow and in_turn_squeezes < 2:
                    in_turn_squeezes += 1
                    squeeze_budget = max(8_000, int(estimate_messages_tokens(messages) * 0.55))
                    messages = compact_stale_tool_messages(
                        messages,
                        max_input_tokens=squeeze_budget,
                        keep_recent_tool_messages=4,
                    )
                    if logger:
                        _ide_log(logger, "warning", "context overflow: squeezed turn", f"budget={squeeze_budget}", sid=_log_session_id(session_id))
                    continue
                if context_overflow and call_summarize and not context_retried:
                    context_retried = True
                    if _maybe_session_compact(force=True):
                        messages = _build_base_messages(summary)
                        progress_note = build_turn_activity_summary(tool_events)
                        if progress_note:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "<system-reminder>\nThe conversation was compacted in the middle of this task. "
                                    f"Work already done this turn:\n{progress_note}\n"
                                    "Continue from there, and re-read files before editing them.\n</system-reminder>"
                                ),
                                "internal": True,
                            })
                        continue
                if context_overflow:
                    msg = "Query requires too much context. Try a more specific question."
                    yield f"data: {json.dumps({'done': True, 'answer': msg})}\n\n"
                    return
                yield f"data: {json.dumps({'error': err_msg})}\n\n"
                return

            finally:
                stop_watchdog.set()
                step_stream.finish()

            if logger:
                elapsed = time.monotonic() - model_call_started
                _resp_tool_calls = len((response or {}).get("tool_calls") or [])
                _resp_chars = len((response or {}).get("content") or "")
                _ide_log_plain(
                    logger,
                    "info",
                    f"Model replied in {elapsed:.1f}s, "
                    + (
                        f"asking to run {_resp_tool_calls} tool{'s' if _resp_tool_calls != 1 else ''}"
                        if _resp_tool_calls
                        else f"answer ready ({_resp_chars:,} characters)"
                    ),
                )

            _accumulate_token_usage(turn_token_totals, response)
            accumulate_usage_by_model(turn_usage_by_model, iteration_model, response)
            step_cost_usd = estimate_usage_cost_usd(
                iteration_model,
                prompt_tokens=int(response.get("prompt_tokens") or 0),
                completion_tokens=int(response.get("completion_tokens") or 0),
                cached_tokens=int(response.get("cached_tokens") or 0),
            )
            if step_cost_usd or response.get("prompt_tokens"):
                _emit_progress(
                    "usage",
                    "",
                    cost_usd=step_cost_usd,
                    prompt_tokens=response.get("prompt_tokens"),
                    completion_tokens=response.get("completion_tokens"),
                    cached_tokens=response.get("cached_tokens"),
                    context_tokens=working_context_tokens(iteration_model),
                )
            if logger:
                _ide_log_plain(
                    logger,
                    "info",
                    describe_model_usage(
                        iteration,
                        iteration_model,
                        prompt_tokens=response.get("prompt_tokens"),
                        completion_tokens=response.get("completion_tokens"),
                        cached_tokens=response.get("cached_tokens"),
                    ),
                )

            if _turn_cancelled():
                yield from _cancelled_finish()
                return

            tool_calls = response.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict) and not tc.get("id"):
                        tc["id"] = f"call_{uuid.uuid4().hex[:24]}"
                thought_content = (response.get("reasoning_content") or "").strip()
                narration = (response.get("content") or "").strip()
                duration_ms = step_stream.thinking_duration_ms()
                duration_s = max(1, duration_ms // 1000)
                final_thinking = {}
                if narration and _narration_repeats_completion(narration, tool_calls):
                    narration = ""
                    step_stream.drop_content()
                if step_stream.thinking_ended:
                    step_stream.close_content(role="narration", text=narration, thought_content=thought_content)
                else:
                    _emit_progress(
                        "agent_thinking_done",
                        f"Thought for {duration_s}s",
                        duration_s=duration_s,
                        duration_ms=duration_ms,
                        thought_content=thought_content,
                        narration=narration,
                    )
                brief = _brief_thought_line(_merge_thought_content(response))
                if logger and brief:
                    _ide_log(logger, "info", brief)
                thinking_start = None
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.get("content") or "",
                    "tool_calls": tool_calls,
                    "thinking_s": duration_s,
                    "thinking_ms": duration_ms,
                }
                reasoning = (response.get("reasoning_content") or "").strip()
                if reasoning:
                    assistant_msg["reasoning_content"] = reasoning
                messages.append(assistant_msg)
                _persist_msg(assistant_msg)

                enabled_server_names = {str(server) for server in (mcp_servers or []) if str(server).strip()}
                tool_groups = _plan_tool_batches(tool_calls, enabled_server_names, writer_scope=_writer_scope_for_call)
                if logger and len(tool_calls) > 1:
                    names = [tc.get("function", {}).get("name") for tc in tool_calls]
                    _ide_log(
                        logger,
                        "info",
                        f"parallel ×{len(tool_calls)}",
                        ",".join(str(n) for n in names if n),
                        "groups " + "+".join(str(len(g)) for g in tool_groups),
                    )

                tool_exec_kwargs = dict(
                    repo_grep_fn=repo_grep_fn,
                    repo_read_fn=repo_read_fn,
                    repo_list_fn=repo_list_fn,
                    repo_ast_fn=repo_ast_fn,
                    create_diff_html_fn=create_diff_html_fn,
                    execute_command_pty_fn=execute_command_pty_fn,
                    socketio=socketio,
                    session_id=session_id,
                    socket_id=emit_room or "",
                    require_permissions=require_permissions,
                    emit_progress_fn=_emit_progress,
                    subagent_runner=_subagent_runner,
                    mode=mode,
                    files_read_this_turn=files_read_this_turn,
                    workspace=active_workspace,
                    mcp_servers=mcp_servers,
                    allowed_mcp_tools=allowed_mcp_tools,
                    state_path=state_path,
                    checkpoint=turn_checkpoint,
                    cancel_check=_turn_cancelled,
                    mcp_disabled_tools=mcp_disabled_tools,
                )

                def _announce_tool_call(tc: dict[str, Any]) -> None:
                    fn = tc.get("function", {})
                    tool_name = fn.get("name", "")
                    tool_args, _parse_error = parse_livecode_tool_arguments(
                        tool_name, fn.get("arguments") or "{}",
                    )
                    label = human_tool_label(tool_name, tool_args)
                    detail = ""
                    if tool_name in FILE_EDIT_TOOL_NAMES and tool_args.get("file_path"):
                        detail = str(tool_args.get("file_path"))
                    elif tool_name == "run_command":
                        detail = "exit pending"
                    tool_events.append({"tool": tool_name, "label": label, "detail": detail})
                    if logger:
                        args_preview = json.dumps(tool_args, default=str)[:300]
                        _ide_log(logger, "debug", f"tool args {tool_name}", f"iter={iteration}", args_preview)
                    _emit_progress("tool_call", label, tool=tool_name, args=tool_args, tool_call_id=tc.get("id", ""))

                edits_to_verify: list[tuple[str, str, dict]] = []
                browser_shots: list[dict[str, Any]] = []
                try:
                    model_reads_images = bool(supports_images_fn and supports_images_fn(iteration_model))
                except Exception:
                    model_reads_images = False

                def _flush_browser_screenshots() -> None:
                    if not browser_shots:
                        return
                    blocks: list[dict[str, Any]] = []
                    for shot in browser_shots[-BROWSER_IMAGES_PER_STEP:]:
                        data_url = browser_shot_data_url(str(shot.get("storage_key") or ""), str(shot.get("shot_id") or ""))
                        if not data_url:
                            continue
                        blocks.append({"type": "text", "text": _browser_image_caption(shot)})
                        blocks.append({"type": "image_url", "image_url": {"url": data_url}})
                    browser_shots.clear()
                    if not blocks:
                        return
                    _retire_browser_images(messages, keep=BROWSER_IMAGE_MESSAGES_KEPT - 1)
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text": BROWSER_SCREENSHOT_NOTE}, *blocks],
                        "internal": True,
                    })

                def _lsp_errors_for_edit(file_path: str, result: dict) -> list[dict] | None:
                    verify_path = str(result.get("relative_path") or file_path or "")
                    if not verify_path.lower().endswith((".py", ".pyi")):
                        return None
                    try:
                        from livecode import lsp_client

                        if not lsp_client.is_available():
                            return None
                        workspace_root = str(result.get("workspace_root") or "")
                        if workspace_root and result.get("relative_path"):
                            full, _rel = resolve_safe_path(workspace_root, str(result.get("relative_path") or ""))
                            lsp_root = workspace_root
                        else:
                            full, _rel = resolve_safe_path(project_path, file_path)
                            lsp_root = project_path
                        if full is None:
                            return None
                        diags = lsp_client.diagnostics(lsp_root, full, wait_s=3.0)
                        if isinstance(diags, dict):
                            return None
                        return [
                            d for d in diags
                            if d.get("severity") == 1 and str(d.get("source") or "").lower() != "pycodestyle"
                        ]
                    except Exception:
                        if logger:
                            _ide_log(logger, "debug", "post-edit lsp verify failed", exc_info=True)
                        return None

                def _flush_post_edit_diagnostics() -> bool:
                    if not LIVECODE_VERIFY_AFTER_EDIT or not edits_to_verify:
                        return False
                    latest: dict[str, tuple[str, dict]] = {}
                    for tool_name, file_path, result in edits_to_verify:
                        if file_path:
                            latest[file_path] = (tool_name, result)
                    edits_to_verify.clear()
                    paths = list(latest)
                    with ThreadPoolExecutor(max_workers=min(len(paths), 6) or 1) as pool:
                        found = list(pool.map(lambda p: _lsp_errors_for_edit(p, latest[p][1]), paths))
                    sections: list[str] = []
                    for file_path, errors in zip(paths, found):
                        if errors is None:
                            continue
                        if not errors:
                            last_diag_sig_by_file.pop(file_path, None)
                            continue
                        sig = "|".join(
                            f"{(d.get('range') or {}).get('start', {}).get('line')}:{d.get('message')}"
                            for d in errors
                        )
                        if last_diag_sig_by_file.get(file_path) == sig:
                            continue
                        last_diag_sig_by_file[file_path] = sig
                        items = "\n".join(
                            f"- L{int((d.get('range') or {}).get('start', {}).get('line', 0)) + 1}: {str(d.get('message') or '')[:200]}"
                            for d in errors[:8]
                        )
                        sections.append(POST_EDIT_DIAGNOSTICS_TEMPLATE.format(
                            tool=latest[file_path][0], file=file_path, count=len(errors), items=items
                        ))
                        if logger:
                            _ide_log(logger, "warning", "post-edit diagnostics", file_path, f"errors={len(errors)}")
                    if not sections:
                        return False
                    note = {"role": "user", "content": "\n\n".join(sections), "internal": True}
                    messages.append(note)
                    _persist_msg(note)
                    return True

                def _finish_tool_item(item: dict[str, Any]) -> str:
                    nonlocal last_edit_iteration, full_answer, completed
                    nonlocal structured_output_retries, consecutive_test_failures
                    nonlocal consecutive_tool_errors, escalate, edit_completed_this_turn

                    tool_name = item["tool_name"]
                    result = item["result"]
                    compacted = item["compacted"]
                    tool_call_id = item["tool_call_id"]
                    tool_args = item.get("tool_args") or {}
                    try:
                        save_tool_artifact(
                            state_path,
                            session_id,
                            tool_call_id,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            result=result,
                            iteration=iteration,
                        )
                    except Exception:
                        if logger:
                            _ide_log(logger, "exception", "Failed to persist tool artifact", sid=_log_session_id(session_id), exc_info=True)

                    stationarity.observe(
                        _tool_call_signature(tool_name, tool_args),
                        tool_name,
                    )
                    search_scatter.observe(tool_name)
                    directory_drill.observe(tool_name, tool_args)
                    edit_no_match.observe(tool_name, result)

                    if tool_name == "browser":
                        design_rounds.observe(result, iteration)
                        ui_verify.observe_browser(tool_args, result, iteration)
                    if tool_name == "browser" and result.get("shot_id"):
                        if model_reads_images:
                            browser_shots.append(result)
                            what = {"compare": "The comparison board", "crop": "The crop", "figma": "The design"}.get(str(result.get("action") or ""), "The screenshot")
                            compacted = {**compacted, "note": f"{what} follows as an image after the tool results."}
                        else:
                            compacted = {**compacted, "note": (
                                "Saved, and shown to the user in the chat. This model can't view images: "
                                "use the snapshot action to read the page."
                            )}

                    if tool_name == "read_repo_file" and result.get("success"):
                        read_fp = result.get("file") or tool_args.get("file_path")
                        if read_fp:
                            files_read_this_turn.add(str(read_fp).replace("\\", "/").lstrip("/"))

                    if tool_name in FILE_EDIT_TOOL_NAMES and result.get("success"):
                        last_edit_iteration = iteration
                        edit_completed_this_turn = True
                        fp = result.get("file_path") or tool_args.get("file_path")
                        if fp:
                            record_file_edited(state_path, session_id, str(fp))
                            ui_verify.observe_edit(str(fp), iteration)
                        _emit_diff(result, tool_call_id)
                        edits_to_verify.append((tool_name, str(fp or ""), result))

                    if tool_name == "spawn_subagent":
                        for changed in result.get("files_changed") or []:
                            fp = str((changed or {}).get("path") or "")
                            if not fp:
                                continue
                            last_edit_iteration = iteration
                            edit_completed_this_turn = True
                            record_file_edited(state_path, session_id, fp)
                            ui_verify.observe_edit(fp, iteration)
                            edits_to_verify.append((tool_name, fp, changed))

                    if tool_name == "create_plan" and result.get("success"):
                        _emit_progress(
                            "plan_created",
                            result.get("title") or "Plan",
                            tool=tool_name,
                            plan_file=result.get("plan_file") or "",
                            plan_title=result.get("title") or "Plan",
                            overview=result.get("overview") or "",
                            todos=result.get("todos") or [],
                        )
                        full_answer = ""
                        completed = True
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps(compacted),
                        }
                        messages.append(tool_msg)
                        _persist_msg(tool_msg)
                        return "break"

                    if tool_name == "todo_write" and result.get("success"):
                        if build_plan_file:
                            try:
                                from livecode import plan_store

                                plan_store.sync_plan_todo_statuses(build_plan_file, result.get("todos") or [])
                            except (FileNotFoundError, ValueError, OSError):
                                if logger:
                                    _ide_log(logger, "debug", "plan todo sync failed", exc_info=True)
                        _emit_progress(
                            "todo_update",
                            result.get("summary") or "Task list updated",
                            tool=tool_name,
                            todos=result.get("todos") or [],
                            plan_file=build_plan_file,
                        )

                    if tool_name == "update_goal" and result.get("success"):
                        if result.get("status") == "blocked":
                            _emit_progress(
                                "goal_blocked",
                                result.get("summary") or "Goal blocked",
                                tool=tool_name,
                            )
                        elif result.get("status") == "completed":
                            _emit_progress(
                                "goal_completed",
                                result.get("summary") or "Goal complete",
                                tool=tool_name,
                            )

                    if tool_name == "attempt_completion" and result.get("completed"):
                        full_answer = result.get("result", "")
                        completed = True
                        _emit_progress(
                            "tool_result",
                            "Task complete",
                            tool=tool_name,
                            success=True,
                            args=tool_args,
                            result=result,
                        )
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps(compacted),
                        }
                        messages.append(tool_msg)
                        _persist_msg(tool_msg)
                        return "break"

                    if tool_name == STRUCTURED_OUTPUT_TOOL:
                        if result.get("valid"):
                            full_answer = format_structured_output_answer(result.get("data") or {})
                            completed = True
                            _emit_progress(
                                "tool_result",
                                "Structured JSON ready",
                                tool=tool_name,
                                success=True,
                                args=tool_args,
                                result=result,
                            )
                        else:
                            structured_output_retries += 1
                            err_text = result.get("error") or "Invalid JSON structure"
                            if structured_output_retries >= STRUCTURED_OUTPUT_MAX_RETRIES:
                                full_answer = (
                                    f"Could not produce valid JSON after "
                                    f"{STRUCTURED_OUTPUT_MAX_RETRIES} attempts: {err_text}"
                                )
                                completed = True
                            _emit_progress(
                                "tool_result",
                                err_text[:120],
                                tool=tool_name,
                                error=not result.get("valid"),
                                success=bool(result.get("valid")),
                                args=tool_args,
                                result=result,
                            )
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps(compacted, default=str),
                        }
                        messages.append(tool_msg)
                        _persist_msg(tool_msg)
                        return "break" if completed else "continue"

                    summary_msg, is_error = _tool_summary(tool_name, result)
                    if tool_name == "run_command":
                        cmd = str(tool_args.get("command") or "")
                        exit_code = result.get("exit_code")
                        if _is_test_command(cmd):
                            if exit_code not in (0, None) and not result.get("error"):
                                consecutive_test_failures += 1
                            else:
                                consecutive_test_failures = 0
                    if is_error:
                        consecutive_tool_errors += 1
                        if consecutive_tool_errors >= 2:
                            escalate = True
                    else:
                        consecutive_tool_errors = 0
                    if logger:
                        _ide_log(
                            logger,
                            "debug",
                            f"tool result {tool_name}",
                            "ok" if not is_error else "error",
                            (summary_msg or "")[:120],
                        )
                    emit_kwargs = {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "error": is_error,
                        "success": not is_error,
                        "error_kind": result.get("error_kind") if is_error else None,
                        "args": tool_args,
                        "result": result,
                    }
                    if is_error and result.get("error"):
                        emit_kwargs["error_full"] = str(result["error"])
                    if result.get("edits") is not None and tool_name in ("edit_file", "multi_edit"):
                        emit_kwargs["edit_count"] = len(result.get("edits") or [])
                    hide_from_activity = is_error and (
                        tool_name in READ_ONLY_TOOL_NAMES
                        or (
                            tool_name == "run_command"
                            and str(result.get("error") or "")
                            == "Use the git_log tool for commit history instead of run_command git log."
                        )
                    )
                    if not hide_from_activity:
                        _emit_progress(
                            "tool_result",
                            summary_msg,
                            **emit_kwargs,
                        )
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(compacted, default=str),
                    }
                    messages.append(tool_msg)
                    _persist_msg(tool_msg)
                    return ""

                def _run_parallel_group(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
                    results_by_id: dict[str, dict[str, Any]] = {}
                    with ThreadPoolExecutor(max_workers=min(len(group), 8)) as pool:
                        futures = {
                            pool.submit(_execute_one_tool, project_path, tc, iteration, **tool_exec_kwargs): tc.get("id", "")
                            for tc in group
                        }
                        for future in as_completed(futures):
                            call_id = futures[future]
                            try:
                                results_by_id[call_id] = future.result()
                            except Exception as exc:
                                results_by_id[call_id] = {
                                    "tool_call_id": call_id,
                                    "tool_name": "unknown",
                                    "tool_args": {},
                                    "result": {"error": str(exc)},
                                    "compacted": {"error": str(exc)},
                                    "iteration": iteration,
                                }
                    return [results_by_id[tc.get("id", "")] for tc in group]

                executed: list[dict[str, Any]] = []
                step_ended = False
                for group_index, group in enumerate(tool_groups):
                    if _turn_cancelled():
                        for later_group in tool_groups[group_index:]:
                            for skipped in later_group:
                                skipped_msg = {
                                    "role": "tool",
                                    "tool_call_id": skipped.get("id", ""),
                                    "content": json.dumps({"error": "Stopped by the user before this ran."}),
                                }
                                messages.append(skipped_msg)
                                _persist_msg(skipped_msg)
                        break
                    for tc in group:
                        _announce_tool_call(tc)
                    if len(group) == 1:
                        items = [_execute_one_tool(project_path, group[0], iteration, **tool_exec_kwargs)]
                    else:
                        items = _run_parallel_group(group)
                    for item in items:
                        executed.append(item)
                        if _finish_tool_item(item) == "break":
                            step_ended = True
                            break
                    if step_ended:
                        break
                if step_ended:
                    answered = {item.get("tool_call_id", "") for item in executed}
                    for tc in tool_calls:
                        if tc.get("id", "") in answered:
                            continue
                        unrun_msg = {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": json.dumps({"error": "Not run: an earlier call in this step ended it."}),
                        }
                        messages.append(unrun_msg)
                        _persist_msg(unrun_msg)

                diagnostics_found = _flush_post_edit_diagnostics()
                _flush_browser_screenshots()

                _iter_tool_names = [item.get("tool_name") or "" for item in executed]
                exploration_streak.observe_iteration(_iter_tool_names)
                todo_nudger.observe_iteration(_iter_tool_names, iteration)

                if completed and diagnostics_found and not diagnostics_blocked_completion:
                    diagnostics_blocked_completion = True
                    completed = False
                    full_answer = ""
                if completed:
                    turn_summary = build_turn_activity_summary(tool_events)
                    turn_messages = _finalize_turn_persist(full_answer)
                    yield f"data: {json.dumps({'done': True, 'answer': full_answer, 'turn_summary': turn_summary, 'turn_messages': turn_messages})}\n\n"
                    _emit_answer_done(full_answer)
                    _emit_complete(len(full_answer), turn_summary)
                    return
                if _turn_cancelled():
                    yield from _cancelled_finish()
                    return
                continue

            duration_ms = step_stream.thinking_duration_ms()
            duration_s = max(1, duration_ms // 1000)
            content = response.get("content") or ""
            reasoning_only = (response.get("reasoning_content") or "").strip()
            final_thinking = {"thinking_s": duration_s, "thinking_ms": duration_ms}
            if reasoning_only:
                final_thinking["reasoning_content"] = reasoning_only
            if not step_stream.thinking_ended:
                _emit_progress(
                    "agent_thinking_done",
                    f"Thought for {duration_s}s",
                    duration_s=duration_s,
                    duration_ms=duration_ms,
                    thought_content=reasoning_only,
                    answer_pending=bool(content and not reasoning_only),
                )
            thinking_start = None

            if (
                iteration == 1
                and not codebase_recovery_used
                and content
                and needs_codebase_evidence(question, has_prior_turns=has_prior_turns)
                and not classification.get("is_meta")
                and not classification.get("chat_only")
            ):
                step_stream.drop_content()
                recovery = {"role": "user", "content": LIVECODE_CODEBASE_RECOVERY_PROMPT, "internal": True}
                messages.append(recovery)
                _persist_msg(recovery)
                codebase_recovery_used = True
                force_tool_choice_required = True
                if logger:
                    _ide_log(logger, "info", "codebase recovery", f"q={len(question)}")
                continue

            if interjection_extensions < 3 and has_pending_interjection(session_id):
                if content.strip():
                    step_stream.close_content(role="narration", text=content, thought_content=reasoning_only)
                    early = {"role": "assistant", "content": content}
                    messages.append(early)
                    _persist_msg(early)
                interjection_extensions += 1
                if logger:
                    _ide_log(logger, "info", "interjection re-entry", f"n={interjection_extensions}")
                continue

            if (
                LIVECODE_TODO_GATE_ENABLED
                and mode == "agent"
                and todo_gate_fires < LIVECODE_TODO_GATE_MAX_FIRES
            ):
                _todos = load_todo_state(state_path, session_id)
                _pending, _in_prog = todo_pending_count(_todos)
                if _pending + _in_prog > 0:
                    if content.strip():
                        step_stream.close_content(role="narration", text=content, thought_content=reasoning_only)
                        early = {"role": "assistant", "content": content}
                        messages.append(early)
                        _persist_msg(early)
                    _open_items = "\n".join(
                        f"- [{t.get('status', 'pending')}] {t.get('content') or t.get('id')}"
                        for t in _todos
                        if str(t.get("status") or "pending").lower() not in ("completed", "cancelled")
                    )
                    gate = {
                        "role": "user",
                        "content": TODO_GATE_TEMPLATE.format(
                            count=_pending + _in_prog, items=_open_items
                        ),
                        "internal": True,
                    }
                    messages.append(gate)
                    _persist_msg(gate)
                    todo_gate_fires += 1
                    force_tool_choice_required = True
                    if logger:
                        _ide_log(
                            logger,
                            "warning",
                            "todo-gate re-entry",
                            f"pending={_pending}",
                            f"in_progress={_in_prog}",
                            f"fire={todo_gate_fires}",
                        )
                    continue

            if (mode == "agent" and enable_browser_for_turn and iteration < max_iterations - CLOSURE_ITERATIONS
                    and any((t.get("function") or {}).get("name") == "browser" for t in tools)
                    and browser_ui_verify_enabled()):
                ui_note = ui_verify.reminder()
                if ui_note:
                    if content.strip():
                        step_stream.close_content(role="narration", text=content, thought_content=reasoning_only)
                        early = {"role": "assistant", "content": content}
                        messages.append(early)
                        _persist_msg(early)
                    gate = {"role": "user", "content": ui_note, "internal": True}
                    messages.append(gate)
                    _persist_msg(gate)
                    force_tool_choice_required = True
                    if logger:
                        _ide_log(logger, "info", "ui-verify re-entry", f"fire={ui_verify.fires}", ", ".join(ui_verify.files[:3]))
                    continue

            if (design_loop_for_turn and mode == "agent" and iteration < max_iterations - CLOSURE_ITERATIONS
                    and browser_design_gate_enabled()):
                design_note = design_rounds.reminder(last_edit_iteration)
                if design_note:
                    if content.strip():
                        step_stream.close_content(role="narration", text=content, thought_content=reasoning_only)
                        early = {"role": "assistant", "content": content}
                        messages.append(early)
                        _persist_msg(early)
                    gate = {"role": "user", "content": design_note, "internal": True}
                    messages.append(gate)
                    _persist_msg(gate)
                    force_tool_choice_required = True
                    if logger:
                        _ide_log(logger, "info", "design-gate re-entry", f"fire={design_rounds.fires}",
                                 f"differences={design_rounds.counts[-1] if design_rounds.counts else 0}")
                    continue

            final_model = _pick_iteration_model(
                user_model,
                classification,
                tool_loop=False,
                escalate=escalate,
                content_chars=_estimate_content_chars(messages),
                images=_messages_have_images(messages),
            )
            if _is_auto_model(user_model) and final_model != user_model:
                if logger:
                    _ide_log(logger, "info", "final answer", final_model)

            if content:
                full_answer = content
            else:
                full_answer = _complete_text_non_streaming(
                    final_model,
                    messages,
                    call_summarize=call_summarize,
                    call_with_tools=call_with_tools,
                    logger=logger,
                    session_id=session_id,
                    log_label="final answer",
                )
            if step_stream.content_started:
                step_stream.close_content(role="answer", text=full_answer, thought_content=reasoning_only)
            turn_summary = build_turn_activity_summary(tool_events)
            turn_messages = _finalize_turn_persist(full_answer)
            yield f"data: {json.dumps({'done': True, 'answer': full_answer, 'turn_summary': turn_summary, 'turn_messages': turn_messages})}\n\n"
            _emit_answer_done(full_answer)
            _emit_complete(len(full_answer), turn_summary)
            return

        summarize_model = _pick_iteration_model(
            user_model,
            classification,
            tool_loop=False,
            escalate=escalate,
            content_chars=_estimate_content_chars(messages),
        )
        _start_thinking()
        _emit_progress("agent_thinking", "Summarizing findings")
        full_answer = _run_exhaustion_summarize(
            summarize_model=summarize_model,
            messages=messages,
            call_summarize=call_summarize,
            call_with_tools=call_with_tools,
            tool_events=tool_events,
            logger=logger,
            session_id=session_id,
        )
        turn_summary = build_turn_activity_summary(tool_events)
        turn_messages = _finalize_turn_persist(full_answer)
        yield f"data: {json.dumps({'done': True, 'answer': full_answer, 'turn_summary': turn_summary, 'turn_messages': turn_messages})}\n\n"
        _emit_answer_done(full_answer)
        _emit_complete(len(full_answer), turn_summary)

    except Exception as e:
        if logger:
            _ide_log(logger, "exception", "Harness error", sid=_log_session_id(session_id), exc_info=True)
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
