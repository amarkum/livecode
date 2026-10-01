from __future__ import annotations

import json
import re
from typing import Any, Callable, TypedDict

from livecode.prompts import INTELLIGENT_CLASSIFIER_PROMPT
from livecode.routing import (
    bump_for_hard_task,
    get_session_chat_history_for_classify,
    heuristic_classification,
    normalize_livecode_classification,
)

INTELLIGENT_CLASSIFIER_CACHE_KEY = "livecode-intelligent-classify-v1"

GOAL_KINDS = frozenset({"code_change", "analysis", "research", "meta"})
EDIT_SCOPES = frozenset({"none", "single_line", "single_file", "multi_file", "bulk"})


class IntelligentClassification(TypedDict, total=False):
    goal_kind: str
    edit_scope: str
    needs_flagship_model: bool
    is_meta: bool
    is_actionable: bool
    needs_local_save: bool
    expects_bulk_work: bool
    needs_code_execution: bool
    needs_shell: bool
    chat_only: bool
    expects_multi_step: bool
    complexity: str
    is_follow_up: bool
    prior_context_hint: str


DEFAULT_INTELLIGENT_CLASSIFICATION: IntelligentClassification = {
    "goal_kind": "analysis",
    "edit_scope": "none",
    "needs_flagship_model": False,
    "is_meta": False,
    "is_actionable": True,
    "needs_local_save": False,
    "expects_bulk_work": False,
    "needs_code_execution": False,
    "needs_shell": False,
    "chat_only": False,
    "expects_multi_step": False,
    "complexity": "medium",
    "is_follow_up": False,
    "prior_context_hint": "",
}


def parse_classifier_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def normalize_intelligent_classification(parsed: dict[str, Any]) -> IntelligentClassification:
    out: dict[str, Any] = dict(DEFAULT_INTELLIGENT_CLASSIFICATION)
    bool_keys = (
        "needs_flagship_model",
        "is_meta",
        "is_actionable",
        "needs_local_save",
        "expects_bulk_work",
        "needs_code_execution",
        "needs_shell",
        "chat_only",
        "expects_multi_step",
        "is_follow_up",
    )
    for key in bool_keys:
        if key in parsed:
            out[key] = bool(parsed[key])

    raw_goal = str(parsed.get("goal_kind") or "").lower().strip().replace("-", "_")
    if raw_goal in GOAL_KINDS:
        out["goal_kind"] = raw_goal

    raw_scope = str(parsed.get("edit_scope") or "").lower().strip().replace("-", "_")
    if raw_scope in EDIT_SCOPES:
        out["edit_scope"] = raw_scope

    raw_complexity = str(parsed.get("complexity") or "").lower().strip()
    if raw_complexity in ("simple", "medium", "complex"):
        out["complexity"] = raw_complexity

    if "prior_context_hint" in parsed:
        out["prior_context_hint"] = str(parsed.get("prior_context_hint") or "")

    if out.get("is_meta"):
        out["is_actionable"] = False
        out["chat_only"] = True
        out["goal_kind"] = "meta"
        out["edit_scope"] = "none"
        out["needs_flagship_model"] = False

    if out.get("needs_local_save") or out.get("needs_code_execution"):
        out["chat_only"] = False

    if out.get("goal_kind") == "code_change" and out.get("edit_scope") == "none":
        out["edit_scope"] = "single_file"

    if out.get("expects_bulk_work") or out.get("edit_scope") in ("multi_file", "bulk"):
        out["needs_flagship_model"] = True

    if out.get("complexity") == "complex" and out.get("goal_kind") == "code_change":
        out["needs_flagship_model"] = True

    return out


def _normalize_chat_history(
    chat_history: list[dict[str, Any]] | None,
    *,
    max_messages: int = 4,
    max_chars: int = 400,
) -> list[dict[str, Any]]:
    if not chat_history:
        return []
    recent = chat_history[-max_messages:]
    out: list[dict[str, Any]] = []
    for msg in recent:
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if len(text) > max_chars:
            text = text[: max_chars - 3] + "..."
        out.append({"role": msg.get("role") or "user", "content": text})
    return out


def build_classifier_user_content(
    question: str,
    chat_history: list[dict[str, Any]] | None = None,
) -> str:
    recent = _normalize_chat_history(chat_history)
    user_lines = [f"User: {msg['content']}" for msg in recent if msg.get("role") == "user"]
    q = (question or "").strip()
    if q and not any(q.lower() in line.lower() for line in user_lines):
        user_lines.append(f"User: {q}")
    return "Conversation (user messages only):\n" + "\n".join(user_lines or [f"User: {q}"])


def describe_intelligent_classification(classification: dict[str, Any]) -> str:
    return (
        f"goal_kind={classification.get('goal_kind', 'analysis')} "
        f"edit_scope={classification.get('edit_scope', 'none')} "
        f"flagship={bool(classification.get('needs_flagship_model'))} "
        f"complexity={classification.get('complexity', 'medium')} "
        f"multi_step={bool(classification.get('expects_multi_step'))}"
    )


def intelligent_classify_turn(
    *,
    model: str,
    call_non_streaming: Callable[..., str],
    user_message: str,
    chat_history: list[dict[str, Any]] | None = None,
    has_prior_turns: bool = False,
    logger: Any = None,
) -> IntelligentClassification:
    heuristic = heuristic_classification(user_message, has_prior_turns=has_prior_turns)
    if heuristic is not None:
        normalized = normalize_intelligent_classification(
            bump_for_hard_task(
                user_message,
                normalize_livecode_classification(
                    user_message,
                    heuristic,
                    has_prior_turns=has_prior_turns,
                ),
            )
        )
        if logger:
            logger.info(
                "[LiveCode] intelligent classify (heuristic) | %s",
                describe_intelligent_classification(normalized),
            )
        return normalized

    user_content = build_classifier_user_content(user_message, chat_history)
    llm_messages = [
        {"role": "system", "content": INTELLIGENT_CLASSIFIER_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        try:
            response = call_non_streaming(
                model,
                llm_messages,
                prompt_cache_key=INTELLIGENT_CLASSIFIER_CACHE_KEY,
            )
        except TypeError:
            response = call_non_streaming(model, llm_messages)
        parsed = normalize_intelligent_classification(parse_classifier_json(response or ""))
        parsed = normalize_intelligent_classification(
            bump_for_hard_task(
                user_message,
                normalize_livecode_classification(
                    user_message,
                    parsed,
                    has_prior_turns=has_prior_turns,
                ),
            )
        )
        if logger:
            logger.info(
                "[LiveCode] intelligent classify | %s",
                describe_intelligent_classification(parsed),
            )
        return parsed
    except Exception as exc:
        if logger:
            logger.warning(
                "[LiveCode] intelligent classify failed (%s) — using safe defaults",
                exc,
            )
        return DEFAULT_INTELLIGENT_CLASSIFICATION


def build_classifier_chat_history(
    project_path: str,
    session_id: str,
    question: str,
) -> list[dict[str, Any]]:
    return get_session_chat_history_for_classify(project_path, session_id, question)
