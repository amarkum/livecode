"""LiveCode's own preferences (editor, terminal, agent and UI switches), kept in ~/.livecode/settings.json.

Browser, model and MCP settings keep their own files; this holds everything the Settings view used to keep
in the page's localStorage, so it survives a new browser profile and is shared by every window.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from typing import Any

SETTINGS_PATH = os.path.expanduser("~/.livecode/settings.json")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
MAX_SETTINGS = 200
MAX_STRING = 500

_LOCK = threading.Lock()


def _path() -> str:
    return SETTINGS_PATH


def _read() -> dict[str, Any]:
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and KEY_RE.match(k) and _valid_value(v)}


def _write(data: dict[str, Any]) -> None:
    path = _path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _valid_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value) <= MAX_STRING
    return False


def load_settings() -> dict[str, Any]:
    with _LOCK:
        return _read()


def save_settings(updates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge updates into the stored settings. A value of None removes that key.

    Returns the stored settings and, per rejected key, why it was rejected. Valid keys are saved even
    when others in the same call are rejected.
    """
    rejected: dict[str, str] = {}
    if not isinstance(updates, dict):
        return load_settings(), {"": "settings must be an object"}
    with _LOCK:
        data = _read()
        for key, value in updates.items():
            if not isinstance(key, str) or not KEY_RE.match(key):
                rejected[str(key)[:80]] = "key must start with a letter and use only letters, digits and _ (64 at most)"
                continue
            if value is None:
                data.pop(key, None)
                continue
            if not _valid_value(value):
                rejected[key] = (f"strings are limited to {MAX_STRING} characters" if isinstance(value, str)
                                 else "value must be true/false, a number or a string")
                continue
            if key not in data and len(data) >= MAX_SETTINGS:
                rejected[key] = f"at most {MAX_SETTINGS} settings"
                continue
            data[key] = value
        _write(data)
        return dict(data), rejected


def reset_settings() -> dict[str, Any]:
    with _LOCK:
        _write({})
        return {}
