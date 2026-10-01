from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .workspace_config import LivecodeWorkspace

AGENT_FILE_NAMES = ("AGENTS.md", "Agents.md", "CLAUDE.md", "Claude.md", "AGENT.md")
RULES_SUBDIRS = (".claude/rules",)
RULE_FILE_MAX_CHARS = 20_000
WORKSPACE_RULES_MAX_CHARS = 60_000

@dataclass
class RuleConfig:
    file_name: str
    file_path: str
    content: str
    depth: int

def _find_git_root(start: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return os.path.abspath(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None

def _collect_dirs_chain(project_path: str) -> list[tuple[str, int]]:
    root = os.path.abspath(os.path.expanduser(project_path))
    git_root = _find_git_root(root) or root
    chain: list[tuple[str, int]] = []
    seen: set[str] = set()
    current = root
    depth = 0
    while True:
        if current in seen:
            break
        seen.add(current)
        chain.append((current, depth))
        if current == git_root:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        depth += 1
    ordered: list[tuple[str, int]] = []
    if git_root in seen:
        rel_parts: list[str] = []
        try:
            rel_parts = os.path.relpath(root, git_root).split(os.sep)
            if rel_parts == ["."]:
                rel_parts = []
        except ValueError:
            rel_parts = []
        path = git_root
        for i, part in enumerate(rel_parts):
            path = os.path.join(path, part)
            ordered.append((path, i + 1))
        if not rel_parts:
            ordered = [(git_root, 0)]
    else:
        ordered = sorted(chain, key=lambda x: x[1])
    return ordered

def _read_rule_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(RULE_FILE_MAX_CHARS + 1).strip()
    except OSError:
        return None
    if len(content) > RULE_FILE_MAX_CHARS:
        return content[:RULE_FILE_MAX_CHARS].rstrip() + "\n[truncated]"
    return content


def discover_project_rules(project_path: str) -> list[RuleConfig]:
    configs: dict[str, RuleConfig] = {}
    seen_named_paths: set[str] = set()
    for directory, depth in _collect_dirs_chain(project_path):
        for name in AGENT_FILE_NAMES:
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            real = os.path.normcase(os.path.realpath(path))
            if real in seen_named_paths:
                continue
            seen_named_paths.add(real)
            content = _read_rule_file(path)
            if content is None:
                continue
            if content:
                key = f"named:{real}"
                prev = configs.get(key)
                if not prev or depth >= prev.depth:
                    configs[key] = RuleConfig(name, path, content, depth)
        for subdir in RULES_SUBDIRS:
            rules_dir = os.path.join(directory, subdir)
            if not os.path.isdir(rules_dir):
                continue
            try:
                entries = sorted(os.listdir(rules_dir))
            except OSError:
                continue
            for entry in entries:
                if not entry.lower().endswith(".md"):
                    continue
                path = os.path.join(rules_dir, entry)
                if not os.path.isfile(path):
                    continue
                content = _read_rule_file(path)
                if content is None:
                    continue
                if content:
                    key = f"rule:{path}"
                    prev = configs.get(key)
                    if not prev or depth >= prev.depth:
                        configs[key] = RuleConfig(entry, path, content, depth)
    return sorted(configs.values(), key=lambda c: (c.depth, c.file_path))

def format_rules_reminder(configs: list[RuleConfig]) -> str:
    if not configs:
        return ""
    parts: list[str] = [
        "<system-reminder>",
        "The following project instructions apply. Follow them when relevant.",
        "",
    ]
    for cfg in configs:
        rel_hint = cfg.file_path
        parts.append(f"### {cfg.file_name} ({rel_hint})")
        parts.append(cfg.content)
        parts.append("")
    parts.append("</system-reminder>")
    return _truncate_rules_reminder("\n".join(parts).strip(), WORKSPACE_RULES_MAX_CHARS)


def _append_rules_block(parts: list[str], block: list[str], max_chars: int) -> bool:
    candidate = "\n".join([*parts, *block, "</system-reminder>"]).strip()
    if len(candidate) > max_chars:
        return False
    parts.extend(block)
    return True


def _truncate_rules_reminder(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    closing = "\n[truncated]\n</system-reminder>"
    return text[: max(0, max_chars - len(closing))].rstrip() + closing


def load_rules_reminder(project_path: str) -> str:
    return format_rules_reminder(discover_project_rules(project_path))


def load_workspace_rules_reminder(workspace: LivecodeWorkspace) -> str:
    header = [
        "<system-reminder>",
        "The following project instructions apply. Follow them when relevant.",
        "",
    ]
    parts = list(header)
    seen_roots: set[str] = set()
    seen_files: set[str] = set()
    added = 0
    for folder in workspace.folders:
        real_root = os.path.normcase(os.path.realpath(folder.path))
        if real_root in seen_roots:
            continue
        seen_roots.add(real_root)
        folder_configs: list[RuleConfig] = []
        for cfg in discover_project_rules(folder.path):
            real_file = os.path.normcase(os.path.realpath(cfg.file_path))
            if real_file in seen_files:
                continue
            seen_files.add(real_file)
            folder_configs.append(cfg)
        if not folder_configs:
            continue
        block = [f"### Workspace folder: {folder.name} ({folder.path})", ""]
        for cfg in folder_configs:
            block.append(f"#### {cfg.file_name} ({cfg.file_path})")
            block.append(cfg.content)
            block.append("")
        if not _append_rules_block(parts, block, WORKSPACE_RULES_MAX_CHARS):
            if added == 0:
                return format_rules_reminder(folder_configs)
            parts.append("[truncated]")
            break
        added += len(folder_configs)
    if not added:
        return ""
    parts.append("</system-reminder>")
    return "\n".join(parts).strip()
