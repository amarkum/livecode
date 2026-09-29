"""Standalone stand-in for the host app's runtime: Gemini model routing and limits."""
from __future__ import annotations

import os

FAST_MODEL = os.environ.get("GEMINI_FAST_MODEL") or "gemini-2.5-flash"
COMPLEX_MODEL = os.environ.get("GEMINI_LARGE_CONTEXT_MODEL") or "gemini-2.5-pro"
LARGE_CONTEXT_MODEL = COMPLEX_MODEL
LARGE_CONTENT_CHAR_THRESHOLD = 600_000
LARGE_FILE_COUNT_THRESHOLD = 40

MODEL_LIMITS = {
    "gemini-2.5-flash": {"context_window": 1_048_576, "max_output_tokens": 65_536},
    "gemini-2.5-pro": {"context_window": 1_048_576, "max_output_tokens": 65_536},
    "gemini-2.0-flash": {"context_window": 1_048_576, "max_output_tokens": 8_192},
}

CREDENTIALS_FILE = os.path.join(os.path.expanduser("~"), ".livecode", "credentials.json")


def load_livecode_credentials() -> dict:
    return {}


def model_supports_multimodal(model) -> bool:
    return True


def get_model_max_tokens(model=None) -> dict:
    from livecode.model_limits import context_window_for_model, max_output_tokens_for_model

    limits = MODEL_LIMITS.get((model or "").strip()) or {
        "context_window": context_window_for_model(model or ""),
        "max_output_tokens": max_output_tokens_for_model(model or "") or 32_000,
    }
    reserved = min(limits["max_output_tokens"], 32_000)
    return {
        "context_window": limits["context_window"],
        "max_output_tokens": limits["max_output_tokens"],
        "reserved_for_output": reserved,
        "max_input_tokens": limits["context_window"] - reserved,
    }


def _auto(role: str, *, images: bool = False, code: bool = False) -> str:
    from livecode.llm import settings as llm_settings

    return llm_settings.default_model(role, images=images, code=code) or (COMPLEX_MODEL if role == "large" else FAST_MODEL)


def resolve_livecode_model(model=None, *, content_chars=0, file_count=0, task="chat"):
    m = (model or "").strip() or "auto"
    if m == "auto":
        return pick_azure_model(m, content_chars=content_chars, file_count=file_count, task=task)
    return m


def pick_azure_model(user_model=None, *, content_chars=0, file_count=0, task="chat"):
    model = (user_model or "").strip() or "auto"
    if task == "large" or content_chars > LARGE_CONTENT_CHAR_THRESHOLD or file_count > LARGE_FILE_COUNT_THRESHOLD:
        return _auto("large")
    if task == "fast" or model == "auto":
        return _auto("fast")
    return model


def pick_livecode_auto_model(
    classification: dict,
    *,
    tool_loop: bool,
    escalate: bool,
    content_chars: int = 0,
    edit_pending: bool = False,
    edit_completed: bool = False,
    needs_flagship_edit: bool = False,
    images: bool = False,
    code: bool = False,
) -> str:
    """images: the request carries images (needs a model that reads them).
    code: the turn changes code (a code model is preferred when there are no images)."""
    role = "fast"
    if content_chars > LARGE_CONTENT_CHAR_THRESHOLD or escalate:
        role = "large"
    elif edit_pending and needs_flagship_edit and tool_loop:
        role = "large"
    elif str((classification or {}).get("complexity") or "medium") == "complex" and not tool_loop:
        role = "large"
    return _auto(role, images=images, code=code and not images)


def livecode_auto_model_route_reason(
    classification: dict,
    *,
    tool_loop: bool,
    escalate: bool,
    content_chars: int = 0,
    edit_pending: bool = False,
    edit_completed: bool = False,
    needs_flagship_edit: bool = False,
    model: str = "",
    images: bool = False,
    code: bool = False,
) -> str:
    if content_chars > LARGE_CONTENT_CHAR_THRESHOLD:
        reason = "large context"
    elif escalate:
        reason = "escalation"
    elif edit_pending and needs_flagship_edit and tool_loop:
        reason = "edit pending flagship"
    elif str((classification or {}).get("complexity") or "medium") == "complex" and not tool_loop:
        reason = "complex final answer"
    else:
        reason = "default tier"
    if images:
        return reason + ", reads images"
    if code:
        return reason + ", code change"
    return reason
