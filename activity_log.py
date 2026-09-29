from __future__ import annotations

import os
import re
from typing import Any

from livecode.model_pricing import estimate_usage_cost_usd, format_token_count, format_usd

def describe_turn_start(
    project: str,
    mode: str,
    user_model: str,
    question_len: int,
) -> str:
    return (
        f"Starting LiveCode turn on {project} "
        f"(mode {mode}, model {user_model}, question {question_len} chars)"
    )

def describe_iteration_start(
    iteration: int,
    max_iterations: int,
    model: str,
    routed_from: str | None,
    route_reason: str | None = None,
) -> str:
    base = f"Step {iteration} of {max_iterations}: calling {model}"
    if routed_from and routed_from.lower() not in ("", "auto") and routed_from != model:
        suffix = f" (routed from {routed_from})"
    elif routed_from and routed_from.lower() in ("", "auto"):
        suffix = " (auto-routed"
        if route_reason:
            suffix += f", reason: {route_reason}"
        suffix += ")"
    else:
        suffix = ""
        if route_reason:
            suffix = f" (reason: {route_reason})"
    return base + suffix

def describe_model_usage(
    iteration: int,
    model: str,
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None = None,
) -> str:
    parts = [f"Step {iteration} ({model}) used"]
    if prompt_tokens is not None:
        parts.append(f"{format_token_count(prompt_tokens)} input tokens")
    if completion_tokens is not None:
        parts.append(f"{format_token_count(completion_tokens)} output tokens")
    if cached_tokens:
        parts.append(f"{format_token_count(cached_tokens)} cached")
    cost = estimate_usage_cost_usd(
        model,
        prompt_tokens=prompt_tokens or 0,
        completion_tokens=completion_tokens or 0,
        cached_tokens=cached_tokens or 0,
    )
    parts.append(f"estimated cost {format_usd(cost)}")
    return ", ".join(parts)

def _short_cmd(command: str, max_len: int = 120) -> str:
    cmd = " ".join(str(command or "").split())
    if len(cmd) <= max_len:
        return cmd
    return cmd[: max_len - 3] + "..."

def _describe_git_command(command: str) -> str | None:
    c = command.lower()
    if "git fetch" in c:
        return "Fetching latest changes from the remote git repository"
    if "git checkout" in c and "git pull" not in c and "git merge" not in c:
        m = re.search(r"git checkout\s+(\S+)", command)
        branch = m.group(1) if m else "another branch"
        return f"Switching git branch to {branch}"
    if "git pull" in c:
        return "Pulling latest commits for the current branch"
    if "git merge" in c:
        return "Merging git branches"
    if "git push" in c:
        if "force" in c or "--force" in c:
            return "Force-pushing git branch to the remote"
        return "Pushing git branch to the remote"
    if "git status" in c:
        return "Checking git working tree status"
    if "git branch" in c:
        return "Listing git branches"
    if "git rev-parse" in c:
        return "Reading git commit identifiers"
    if "git log" in c:
        return "Reading recent git history"
    return None

def describe_tool_start(tool_name: str, args: dict | None, label: str = "") -> str:
    args = args or {}
    name = (tool_name or "").strip()

    if name == "run_command":
        cmd = str(args.get("command") or label or "")
        hint = _describe_git_command(cmd)
        if hint:
            return hint
        return f"Running shell command: {_short_cmd(cmd)}"

    if name == "read_repo_file":
        fp = os.path.basename(str(args.get("file_path") or ""))
        return f"Reading file {fp or 'from project'}"

    if name == "ast_symbols":
        fp = os.path.basename(str(args.get("file_path") or ""))
        return f"Reading symbols in {fp}" if fp else "Reading file structure"

    if name == "grep_repo":
        pat = str(args.get("pattern") or "")[:80]
        return f"Searching codebase for {pat!r}"

    if name == "find_files":
        q = str(args.get("query") or "").strip()
        ext = str(args.get("ext") or "").strip()
        prefix = str(args.get("path_prefix") or "").strip()
        parts = [p for p in [q, (f"ext {ext}" if ext else ""), (f"in {prefix}" if prefix else "")] if p]
        detail = " ".join(parts) if parts else "the repository"
        return f"Finding files in the repository {detail}".rstrip()

    if name == "glob_files":
        pat = str(args.get("pattern") or "").strip()
        directory = str(args.get("path") or "").strip()
        if pat and directory:
            return f"Finding files matching {pat!r} in {directory}"
        if pat:
            return f"Finding files matching {pat!r}"
        return "Finding files in the repository"

    if name == "find_symbol":
        sym = str(args.get("name") or "").strip()
        return f"Finding symbol {sym!r}" if sym else "Finding symbol"

    if name == "find_references":
        sym = str(args.get("name") or "").strip()
        return f"Finding references to {sym!r}" if sym else "Finding references"

    if name == "list_symbols":
        path = str(args.get("path") or "").strip()
        return f"Listing symbols in {path}" if path else "Listing symbols"

    if name == "edit_file":
        fp = os.path.basename(str(args.get("file_path") or ""))
        return f"Editing file {fp or 'in project'}"

    if name == "write_file":
        fp = os.path.basename(str(args.get("file_path") or ""))
        return f"Writing file {fp or 'in project'}"

    if name == "git_log":
        return "Reading git commit history"

    if name == "attempt_completion":
        return ""

    if name == "list_repo_dir":
        directory = str(args.get("directory") or "").strip() or "project root"
        return f"Listing files in {directory}"

    if label:
        return label.replace("`", "")
    return "Working"

def describe_tool_result(
    tool_name: str,
    args: dict | None,
    result: dict | None,
    summary: str = "",
) -> str:
    args = args or {}
    result = result or {}
    name = (tool_name or "").strip()

    if result.get("error"):
        err = str(result["error"]).replace("\n", " ")[:160]
        action = describe_tool_start(name, args, summary)
        hint = str(result.get("recovery_hint") or "").strip()
        if hint:
            return f"Failed: {action}. {err}. Next: {hint}"
        return f"Failed: {action}. {err}"

    if name == "run_command":
        exit_code = result.get("exit_code")
        cmd_hint = _describe_git_command(str(args.get("command") or ""))
        if cmd_hint and exit_code == 0:
            return f"Done: {cmd_hint} (exit {exit_code})"
        if exit_code is not None:
            return f"Shell command finished with exit code {exit_code}"
        return "Shell command finished"

    if name == "attempt_completion":
        return "Task completed"

    if name == "edit_file" and result.get("success"):
        fp = result.get("file_path") or args.get("file_path") or "file"
        return f"Updated file {os.path.basename(str(fp))}"

    if name == "write_file" and result.get("success"):
        fp = result.get("file_path") or args.get("file_path") or "file"
        return f"Wrote file {os.path.basename(str(fp))}"

    if name == "read_repo_file" and result.get("success"):
        fp = result.get("file") or args.get("file_path") or "file"
        return f"Read file {os.path.basename(str(fp))}"

    if name == "git_log" and result.get("success"):
        count = result.get("commit_count")
        if count is not None:
            return f"Loaded {count} git commits"
        return "Loaded git commits"

    if summary:
        return summary
    return describe_tool_start(name, args)

def describe_turn_complete(
    answer_len: int,
    totals: dict[str, int],
    usage_by_model: dict[str, dict[str, int]],
    project: str,
) -> str:
    prompt = totals.get("prompt_tokens", 0)
    completion = totals.get("completion_tokens", 0)
    cached = totals.get("cached_tokens", 0)
    total_cost = 0.0
    model_bits: list[str] = []
    for model, usage in sorted(usage_by_model.items()):
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        cache = int(usage.get("cached_tokens") or 0)
        if not p and not c:
            continue
        cost = estimate_usage_cost_usd(model, prompt_tokens=p, completion_tokens=c, cached_tokens=cache)
        total_cost += cost
        model_bits.append(f"{model} {format_usd(cost)}")

    parts = [
        f"Turn complete on {project}",
        f"answer {answer_len} chars",
        f"tokens {format_token_count(prompt)} in",
        f"{format_token_count(completion)} out",
    ]
    if cached:
        parts.append(f"{format_token_count(cached)} cached")
    parts.append(f"estimated total cost {format_usd(total_cost)}")
    if model_bits:
        parts.append("by model: " + ", ".join(model_bits))
    return ". ".join(parts) + "."

def accumulate_usage_by_model(
    store: dict[str, dict[str, int]],
    model: str,
    response: dict[str, Any] | None,
) -> None:
    if not model or not isinstance(response, dict):
        return
    bucket = store.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
        val = response.get(key)
        if val is None:
            continue
        try:
            bucket[key] = bucket.get(key, 0) + int(val)
        except (TypeError, ValueError):
            continue
