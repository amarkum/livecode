from __future__ import annotations

import os
import re

# Input-token window and output-token ceiling per model family. Deployment names
# vary ("gpt-5-mini", "prod-gpt5", ...), so families match anywhere in the name.
# First match wins, so the more specific families come first.
_MODEL_FAMILIES: tuple[tuple[re.Pattern[str], int, int | None], ...] = (
    (re.compile(r"gpt-?5"), 272_000, 128_000),
    (re.compile(r"gpt-?4\.1"), 1_000_000, 32_768),
    (re.compile(r"(?<![a-z0-9])o[134](?:-mini|-pro)?(?![a-z0-9])"), 200_000, 100_000),
    (re.compile(r"gpt-?4o"), 128_000, 16_384),
    (re.compile(r"gpt-?oss"), 128_000, 32_768),
    (re.compile(r"gpt-?4"), 128_000, 4_096),
    (re.compile(r"gpt-?3\.?5"), 16_000, 4_096),
    (re.compile(r"grok-?4"), 256_000, 64_000),
    (re.compile(r"grok"), 131_072, 32_768),
    (re.compile(r"claude"), 200_000, 64_000),
    (re.compile(r"gemini"), 1_000_000, 65_536),
    (re.compile(r"deepseek"), 128_000, 32_768),
)

DEFAULT_CONTEXT_WINDOW = 128_000
# The loop never plans for more than this many input tokens, even on
# million-token models: every step resends the whole conversation.
DEFAULT_WORKING_CONTEXT_CAP = 200_000
# Room for big write_file/edit_file calls without asking for the whole
# output ceiling (Azure counts the requested maximum against rate limits).
DEFAULT_MAX_OUTPUT_TOKENS = 32_768


def _env_int(name: str) -> int | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _family(model: str) -> tuple[int, int | None] | None:
    name = (model or "").strip().lower().replace(" ", "")
    if not name:
        return None
    for pattern, window, max_output in _MODEL_FAMILIES:
        if pattern.search(name):
            return window, max_output
    return None


def context_window_for_model(model: str) -> int:
    family = _family(model)
    return family[0] if family else DEFAULT_CONTEXT_WINDOW


def working_context_tokens(model: str) -> int:
    """Input tokens the loop may use for ``model`` before compacting."""
    cap = _env_int("LIVECODE_CONTEXT_TOKENS") or DEFAULT_WORKING_CONTEXT_CAP
    return min(context_window_for_model(model), cap)


def max_output_tokens_for_model(model: str) -> int | None:
    """Completion budget to request, or None to keep the host's default."""
    family = _family(model)
    if not family or not family[1]:
        return None
    wanted = _env_int("LIVECODE_MAX_OUTPUT_TOKENS") or DEFAULT_MAX_OUTPUT_TOKENS
    return min(wanted, family[1])
