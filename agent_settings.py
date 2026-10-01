"""Agent, harness, memory and plan settings, saved from LiveCode Settings.

Stored in ~/.livecode/agent.json. Every setting is declared in SCHEMA with its type, default and
allowed range, so the server validates what the Settings page sends and the page can draw each
control from the same description. The harness reads a snapshot() once per turn, so a change applies
to the next turn and a turn never sees half of an update.
"""

from __future__ import annotations

import json
import math
import os
import threading
from typing import Any

CONFIG_PATH = os.path.expanduser("~/.livecode/agent.json")
CUSTOM_INSTRUCTIONS_MAX_CHARS = 8000

# kind: bool | int | float | choice | text. Ranges are inclusive.
SCHEMA: dict[str, dict[str, Any]] = {
    # Harness: turn budget
    "max_iterations": {"kind": "int", "default": 300, "min": 10, "max": 2000, "group": "harness"},
    "read_only_max_iterations": {"kind": "int", "default": 100, "min": 5, "max": 1000, "group": "harness"},
    "subagent_max_iterations": {"kind": "int", "default": 30, "min": 5, "max": 300, "group": "harness"},
    "subagents": {"kind": "bool", "default": True, "group": "harness"},
    "parallel_tools": {"kind": "bool", "default": True, "group": "harness"},
    "max_parallel_writers": {"kind": "int", "default": 6, "min": 1, "max": 16, "group": "harness"},
    # Harness: reliability
    "model_retries": {"kind": "int", "default": 4, "min": 0, "max": 10, "group": "harness"},
    "loop_guard": {"kind": "bool", "default": True, "group": "harness"},
    "loop_hard_stop": {"kind": "int", "default": 10, "min": 4, "max": 50, "group": "harness"},
    "verify_after_edit": {"kind": "bool", "default": True, "group": "harness"},
    "todo_gate": {"kind": "bool", "default": True, "group": "harness"},
    "nudges": {"kind": "bool", "default": True, "group": "harness"},
    "command_timeout_s": {"kind": "int", "default": 600, "min": 10, "max": 3600, "group": "harness"},
    # Harness: context
    "auto_compact_percent": {"kind": "int", "default": 75, "min": 40, "max": 95, "group": "harness"},
    "custom_instructions": {"kind": "text", "default": "", "max_chars": CUSTOM_INSTRUCTIONS_MAX_CHARS, "group": "harness"},
    # Memory
    "memory_enabled": {"kind": "bool", "default": True, "group": "memory"},
    "memory_max_results": {"kind": "int", "default": 6, "min": 1, "max": 20, "group": "memory"},
    "memory_min_score": {"kind": "float", "default": 0.0, "min": 0.0, "max": 1.0, "group": "memory"},
    "memory_autosave": {"kind": "bool", "default": True, "group": "memory"},
    "memory_auto_extract": {"kind": "bool", "default": True, "group": "memory"},
    "memory_consolidate": {"kind": "bool", "default": True, "group": "memory"},
    "memory_consolidate_hours": {"kind": "int", "default": 24, "min": 1, "max": 720, "group": "memory"},
    "memory_agent_writes": {"kind": "bool", "default": True, "group": "memory"},
    # Plan mode
    "plan_ask_questions": {"kind": "bool", "default": True, "group": "plan"},
    "plan_diagrams": {"kind": "bool", "default": True, "group": "plan"},
    "plan_tests_section": {"kind": "bool", "default": True, "group": "plan"},
    "plan_detail": {"kind": "choice", "default": "standard", "choices": ("concise", "standard", "detailed"), "group": "plan"},
    "plan_build_verify": {"kind": "bool", "default": True, "group": "plan"},
}

GROUPS = ("harness", "memory", "plan")

_lock = threading.Lock()
_cache: tuple[tuple[int, int], dict[str, Any]] | None = None


class SettingsError(ValueError):
    pass


def _stamp() -> tuple[int, int]:
    try:
        info = os.stat(CONFIG_PATH)
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return (0, -1)


def _read_saved() -> dict[str, Any]:
    global _cache
    stamp = _stamp()
    cached = _cache
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    _cache = (stamp, data)
    return dict(data)


def _write_saved(data: dict[str, Any]) -> None:
    global _cache
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, CONFIG_PATH)
    _cache = None


def _coerce(key: str, value: Any) -> Any:
    """The validated value for key, or SettingsError naming what is allowed."""
    spec = SCHEMA[key]
    kind = spec["kind"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        word = str(value).strip().lower() if isinstance(value, str) else ""
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
        raise SettingsError(f"{key} is true or false.")
    if kind in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number) or not spec["min"] <= number <= spec["max"]:
            raise SettingsError(f"{key} is a number from {spec['min']:g} to {spec['max']:g}.")
        return int(round(number)) if kind == "int" else round(number, 3)
    if kind == "choice":
        word = str(value or "").strip().lower()
        if word not in spec["choices"]:
            raise SettingsError(f"{key} is one of: {', '.join(spec['choices'])}.")
        return word
    if kind == "text":
        text = str(value or "").replace("\r\n", "\n")
        if len(text) > spec["max_chars"]:
            raise SettingsError(f"{key} is at most {spec['max_chars']:,} characters.")
        return text
    raise SettingsError(f"{key} has an unknown kind.")


def get(key: str) -> Any:
    spec = SCHEMA[key]
    saved = _read_saved()
    if key not in saved:
        return spec["default"]
    try:
        return _coerce(key, saved[key])
    except SettingsError:
        return spec["default"]


def snapshot() -> dict[str, Any]:
    """Every setting's current value; a hand-edited file with a bad value falls back to the default."""
    saved = _read_saved()
    out: dict[str, Any] = {}
    for key, spec in SCHEMA.items():
        if key in saved:
            try:
                out[key] = _coerce(key, saved[key])
                continue
            except SettingsError:
                pass
        out[key] = spec["default"]
    return out


def _schema_for_client() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, spec in SCHEMA.items():
        entry = {k: (list(v) if isinstance(v, tuple) else v) for k, v in spec.items()}
        out[key] = entry
    return out


def status() -> dict[str, Any]:
    return {"settings": snapshot(), "schema": _schema_for_client(), "path": CONFIG_PATH}


def update(data: dict[str, Any]) -> dict[str, Any]:
    """Validates every known key before saving any; unknown keys are ignored."""
    if not isinstance(data, dict):
        raise SettingsError("Send the settings as an object.")
    updates = {key: _coerce(key, value) for key, value in data.items() if key in SCHEMA}
    if updates:
        with _lock:
            saved = _read_saved()
            for key, value in updates.items():
                if value == SCHEMA[key]["default"]:
                    saved.pop(key, None)
                else:
                    saved[key] = value
            _write_saved(saved)
    return status()


def reset(group: str = "") -> dict[str, Any]:
    """Back to defaults: one group's settings, or every setting when group is empty."""
    group = str(group or "").strip().lower()
    if group and group not in GROUPS:
        raise SettingsError(f"group is one of: {', '.join(GROUPS)}.")
    with _lock:
        saved = _read_saved()
        for key, spec in SCHEMA.items():
            if not group or spec.get("group") == group:
                saved.pop(key, None)
        _write_saved(saved)
    return status()
