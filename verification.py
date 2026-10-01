"""The command a project uses to check itself, so the harness can run it after the agent's edits.

Nothing here runs anything: detect_check_command reads the project's manifests and returns the one command
worth running (its tests, or a type check when it has no tests), or None when the project has no obvious
check. The harness runs it once before a turn ends, when the agent changed code and did not run the checks
itself.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

_NPM_PLACEHOLDER = re.compile(r"no test specified", re.IGNORECASE)
_MAKE_TARGET = re.compile(r"^(test|tests|check)\s*:", re.MULTILINE)
_PYTEST_FILE = re.compile(r"^(test_.*\.py|.*_test\.py)$")


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _exists(root: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(root, name)) for name in names)


def _has_python_tests(root: str) -> bool:
    for name in ("pytest.ini", "conftest.py", "tox.ini", "setup.cfg", "pyproject.toml"):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        if name in ("pytest.ini", "conftest.py"):
            return True
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        if "pytest" in text:
            return True
    for folder in ("tests", "test"):
        path = os.path.join(root, folder)
        if not os.path.isdir(path):
            continue
        try:
            names = os.listdir(path)
        except OSError:
            continue
        if any(_PYTEST_FILE.match(n) for n in names) or "conftest.py" in names:
            return True
    return False


def _python_runner(root: str) -> str:
    if _exists(root, "uv.lock"):
        return "uv run pytest -q"
    if _exists(root, "poetry.lock"):
        return "poetry run pytest -q"
    for venv in (".venv", "venv"):
        python = os.path.join(root, venv, "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")
        if os.path.isfile(python):
            return f'"{python}" -m pytest -q' if " " in python else f"{python} -m pytest -q"
    return "python -m pytest -q"


def _node_runner(root: str) -> str:
    if _exists(root, "pnpm-lock.yaml"):
        return "pnpm"
    if _exists(root, "yarn.lock"):
        return "yarn"
    if _exists(root, "bun.lockb", "bun.lock"):
        return "bun"
    return "npm"


def detect_check_command(root: str) -> dict[str, Any] | None:
    """{"command", "label", "source"} for the project at root, or None.

    The tests come first (a package.json test script, pytest, cargo, go, make test, rspec, mix, composer,
    gradle, maven); a TypeScript project without tests gets a type check instead."""
    root = os.path.abspath(os.path.expanduser(root or ""))
    if not os.path.isdir(root):
        return None

    package = _read_json(os.path.join(root, "package.json"))
    if package is not None:
        scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
        test = str(scripts.get("test") or "").strip()
        if test and not _NPM_PLACEHOLDER.search(test):
            runner = _node_runner(root)
            command = {"npm": "npm test --silent", "pnpm": "pnpm test", "yarn": "yarn test", "bun": "bun run test"}[runner]
            return {"command": command, "label": "Run the project's tests", "source": "package.json"}

    if _has_python_tests(root):
        return {"command": _python_runner(root), "label": "Run the project's tests", "source": "pytest"}
    if _exists(root, "Cargo.toml"):
        return {"command": "cargo test -q", "label": "Run the project's tests", "source": "Cargo.toml"}
    if _exists(root, "go.mod"):
        return {"command": "go test ./...", "label": "Run the project's tests", "source": "go.mod"}
    makefile = os.path.join(root, "Makefile")
    if os.path.isfile(makefile):
        try:
            with open(makefile, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            text = ""
        target = _MAKE_TARGET.search(text)
        if target:
            return {"command": f"make {target.group(1)}", "label": "Run the project's checks", "source": "Makefile"}
    if _exists(root, "Gemfile") and os.path.isdir(os.path.join(root, "spec")):
        return {"command": "bundle exec rspec", "label": "Run the project's tests", "source": "Gemfile"}
    if _exists(root, "mix.exs"):
        return {"command": "mix test", "label": "Run the project's tests", "source": "mix.exs"}
    composer = _read_json(os.path.join(root, "composer.json"))
    if composer and isinstance(composer.get("scripts"), dict) and composer["scripts"].get("test"):
        return {"command": "composer test", "label": "Run the project's tests", "source": "composer.json"}
    if _exists(root, "gradlew"):
        return {"command": "./gradlew test -q", "label": "Run the project's tests", "source": "gradlew"}
    if _exists(root, "pom.xml"):
        return {"command": "mvn -q test", "label": "Run the project's tests", "source": "pom.xml"}

    if package is not None and _exists(root, "tsconfig.json"):
        runner = _node_runner(root)
        exec_ = {"npm": "npx", "pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}[runner]
        return {"command": f"{exec_} tsc --noEmit -p .", "label": "Type-check the project", "source": "tsconfig.json"}
    return None


def failure_tail(output: str, limit: int = 3000) -> str:
    """The part of a failing run worth showing the model: the first error lines and the end of the output."""
    text = str(output or "").strip()
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    marked = [i for i, line in enumerate(lines)
              if re.search(r"\b(error|fail|failed|failure|exception|traceback|assert|panic|✗|FAIL)\b", line, re.IGNORECASE)]
    head: list[str] = []
    if marked:
        first = marked[0]
        head = lines[max(0, first - 2):first + 12]
    tail_budget = max(800, limit - sum(len(l) + 1 for l in head) - 20)
    tail = text[-tail_budget:]
    joined = ("\n".join(head) + "\n…\n" if head else "…\n") + tail
    return joined[-limit:] if len(joined) > limit else joined
