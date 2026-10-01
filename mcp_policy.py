from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

MCP_EFFECT_READ = "read"
MCP_EFFECT_WRITE = "write"
MCP_EFFECT_APPROVAL_REQUIRED = "approval_required"
MCP_EFFECT_BLOCKED = "blocked"

MCP_REASON_DESTRUCTIVE = "destructive"
MCP_REASON_EXTERNAL_COMMUNICATION = "external_communication"
MCP_REASON_PRODUCTION_CHANGE = "production_change"
MCP_REASON_FINANCIAL_ACTION = "financial_action"
MCP_REASON_PROTECTED_BRANCH = "protected_branch_change"
MCP_REASON_BULK_MUTATION = "bulk_mutation"
MCP_REASON_POLICY_OVERRIDE = "policy_override"
MCP_REASON_DEFAULT_WRITE = "default_write"
MCP_REASON_READ_ONLY = "read_only"

MCPEffect = Literal["read", "write", "approval_required", "blocked"]


@dataclass(frozen=True)
class MCPToolPolicy:
    effect: MCPEffect
    reason_code: str
    reason: str


_APPROVAL_REQUIRED_EXACT = {
    ("github", "merge_pull_request"),
    ("github", "delete_branch"),
    ("github", "approve_pull_request"),
}

_BLOCKED_EXACT: set[tuple[str, str]] = set()


_APPROVAL_TOKENS = {
    "delete",
    "purge",
    "destroy",
    "drop",
    "truncate",
    "terminate",
    "force_push",
    "force",
    "merge",
    "approve",
    "deploy",
    "publish",
    "release",
    "promote",
    "send",
    "notify",
    "charge",
    "purchase",
}

_MUTATION_TOKENS = {
    "add",
    "apply",
    "assign",
    "cancel",
    "change",
    "close",
    "create",
    "disable",
    "enable",
    "insert",
    "modify",
    "move",
    "patch",
    "post",
    "put",
    "remove",
    "rename",
    "replace",
    "reset",
    "restart",
    "revoke",
    "run",
    "set",
    "start",
    "stop",
    "submit",
    "update",
    "upload",
    "write",
}

_READ_VERB_TOKENS = {"get", "list", "search", "find", "read", "view", "fetch", "query", "describe", "browse", "head"}
_DESTRUCTIVE_MODES = {"delete", "purge", "destroy", "drop", "truncate", "terminate", "hard", "force"}
_PRODUCTION_VALUES = {"prod", "production"}
_SHARED_TARGETS = {"prod", "production", "staging", "stage", "shared", "release"}
_PROTECTED_BRANCHES = {"main", "master", "production", "prod", "release"}
_BULK_VALUES = {"*", "all", "any", "bulk", "everything", "unbounded"}
_DESCRIPTIVE_TOKENS = {"deleted", "deployment", "release", "status", "notes", "notification", "settings", "approved", "reviews", "records"}
_EXTERNAL_RECIPIENT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SQL_COMMENT_RE = re.compile(r"--.*?$|/\*.*?\*/", re.S | re.M)
_SQL_STRING_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", re.S)
_SQL_DESTRUCTIVE_RE = re.compile(r"\b(drop|truncate|delete\s+from|alter\s+table|update\s+\w+\s+set)\b", re.I)


def classify_mcp_tool_effect(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    annotations: dict[str, Any] | None = None,
) -> MCPToolPolicy:
    key = (str(server_name or ""), str(tool_name or ""))
    normalized_args = arguments if isinstance(arguments, dict) else {}
    normalized_annotations = annotations if isinstance(annotations, dict) else {}
    if key in _BLOCKED_EXACT:
        return MCPToolPolicy(MCP_EFFECT_BLOCKED, MCP_REASON_POLICY_OVERRIDE, "This MCP tool is blocked by policy")
    if key in _APPROVAL_REQUIRED_EXACT:
        return MCPToolPolicy(MCP_EFFECT_APPROVAL_REQUIRED, MCP_REASON_POLICY_OVERRIDE, "This MCP tool requires approval by policy")
    argument_reason = _argument_policy_reason(str(tool_name or ""), normalized_args)
    if argument_reason:
        return MCPToolPolicy(MCP_EFFECT_APPROVAL_REQUIRED, argument_reason[0], argument_reason[1])
    token_reason = _token_policy_reason(str(tool_name or ""))
    if token_reason:
        return MCPToolPolicy(MCP_EFFECT_APPROVAL_REQUIRED, token_reason[0], token_reason[1])
    if normalized_annotations.get("destructiveHint") is True:
        return MCPToolPolicy(MCP_EFFECT_APPROVAL_REQUIRED, MCP_REASON_DESTRUCTIVE, "MCP tool declares a destructive effect")
    name_tokens = set(_tool_name_tokens(str(tool_name or "")))
    if name_tokens & _READ_VERB_TOKENS and not name_tokens & (_MUTATION_TOKENS | _APPROVAL_TOKENS):
        return MCPToolPolicy(MCP_EFFECT_READ, MCP_REASON_READ_ONLY, "MCP tool name indicates a read-only lookup")
    return MCPToolPolicy(MCP_EFFECT_WRITE, MCP_REASON_DEFAULT_WRITE, "Unknown MCP tool defaults to write")


def _token_policy_reason(tool_name: str) -> tuple[str, str] | None:
    tokens = _tool_name_tokens(tool_name)
    token_set = set(tokens)
    single_tokens = {token for token in tokens if "_" not in token}
    if single_tokens and single_tokens.issubset(_DESCRIPTIVE_TOKENS | {"get", "list", "show", "read", "fetch", "query", "describe"}):
        return None
    if "force" in token_set and "push" in token_set:
        return MCP_REASON_PROTECTED_BRANCH, "MCP tool name indicates force push"
    if "production" in token_set or "prod" in token_set:
        if token_set & {"deploy", "publish", "release", "promote", "modify", "update", "set"}:
            return MCP_REASON_PRODUCTION_CHANGE, "MCP tool name indicates production change"
    for token in tokens:
        if token in _APPROVAL_TOKENS:
            return _reason_for_token(token), f"MCP tool name indicates consequential action: {token}"
    return None


def _tool_name_tokens(tool_name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(tool_name or ""))
    raw = re.split(r"[^A-Za-z0-9]+", spaced)
    words = [part.lower() for part in raw if part]
    tokens = list(words)
    tokens.extend(f"{words[idx]}_{words[idx + 1]}" for idx in range(len(words) - 1))
    return tokens


def _argument_policy_reason(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str] | None:
    flattened = list(_flatten_values(arguments))
    lowered_values = {value.lower() for value in flattened}
    action_tokens = set(_tool_name_tokens(tool_name))
    if _truthy_argument(arguments, {"force", "hard"}):
        return MCP_REASON_DESTRUCTIVE, "Arguments request force or hard execution"
    if lowered_values & _DESTRUCTIVE_MODES:
        return MCP_REASON_DESTRUCTIVE, "Arguments request a destructive mode"
    if lowered_values & _PRODUCTION_VALUES:
        return MCP_REASON_PRODUCTION_CHANGE, "Arguments target production"
    if lowered_values & _SHARED_TARGETS and action_tokens & {"deploy", "publish", "release", "promote"}:
        return MCP_REASON_PRODUCTION_CHANGE, "Arguments target a shared release environment"
    if action_tokens & {"send", "post", "notify"} and any(_EXTERNAL_RECIPIENT_RE.match(value) for value in flattened):
        return MCP_REASON_EXTERNAL_COMMUNICATION, "Arguments include an external recipient"
    if lowered_values & _BULK_VALUES and action_tokens & _MUTATION_TOKENS:
        return MCP_REASON_BULK_MUTATION, "Arguments request a bulk mutation"
    if lowered_values & _PROTECTED_BRANCHES and action_tokens & {"merge", "push", "delete", "force_push", "force"}:
        return MCP_REASON_PROTECTED_BRANCH, "Arguments target a protected branch"
    for value in flattened:
        if _contains_destructive_sql(value):
            return MCP_REASON_DESTRUCTIVE, "Arguments contain destructive SQL"
    return None


def _truthy_argument(arguments: dict[str, Any], names: set[str]) -> bool:
    for key, value in arguments.items():
        if str(key).lower() in names and value is True:
            return True
        if isinstance(value, dict) and _truthy_argument(value, names):
            return True
    return False


def _flatten_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            values.extend(_flatten_values(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_flatten_values(item))
    elif value is not None:
        values.append(str(value))
    return values


def _contains_destructive_sql(value: str) -> bool:
    text = str(value or "")
    if not text:
        return False
    without_comments = _SQL_COMMENT_RE.sub(" ", text)
    normalized = _SQL_STRING_RE.sub(" ", without_comments)
    return bool(_SQL_DESTRUCTIVE_RE.search(normalized))


def _reason_for_token(token: str) -> str:
    if token in {"send", "notify"}:
        return MCP_REASON_EXTERNAL_COMMUNICATION
    if token in {"deploy", "publish", "release", "promote"}:
        return MCP_REASON_PRODUCTION_CHANGE
    if token in {"charge", "purchase"}:
        return MCP_REASON_FINANCIAL_ACTION
    if token in {"force", "force_push", "merge", "approve"}:
        return MCP_REASON_PROTECTED_BRANCH
    return MCP_REASON_DESTRUCTIVE
