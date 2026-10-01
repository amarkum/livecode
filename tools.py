from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import threading
import time

from livecode.browser import (
    AGENT_ACTIONS as BROWSER_AGENT_ACTIONS,
    DEVICE_PRESETS as BROWSER_DEVICE_PRESETS,
    figma_configured as browser_figma_configured,
    playwright_installed as browser_playwright_installed,
)
from livecode.codebase_index import get_codebase_index
from livecode.workspace_config import workspace_folder_aliases, workspace_name_key
from livecode.memory import (
    append_project_memory,
    read_memory_file,
    search_memory,
)
from livecode.search_replace import (
    SearchReplaceParams,
    apply_multi_search_replace,
    apply_search_replace,
    display_text,
    read_text_preserving,
    suggest_similar_filename,
    validate_path_components,
    write_text_atomic,
)
from livecode.pending_changes import has_baseline, record_baseline, record_turn_baseline, snapshot_file
from livecode.subagent import compact_subagent_result, subagent_title
from livecode.workspace import (
    glob_files,
    path_blocked_for_edit,
    resolve_safe_path,
    resolve_workspace_path,
    search_file_manifest,
    workspace_display_path,
    workspace_roots,
)

def _rel_path_or_empty(project_path: str, path: str) -> str:
    root = os.path.abspath(os.path.expanduser(project_path))
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return ""

def _resolve_flexible_project_path(project_path: str, rel_path: str) -> str | None:
    scoped_path, _ = resolve_safe_path(project_path, rel_path)
    if scoped_path and (os.path.exists(scoped_path) or os.path.isdir(os.path.dirname(scoped_path))):
        return scoped_path

    root = os.path.abspath(os.path.expanduser(project_path))
    parts = [p for p in str(rel_path or "").replace("\\", "/").strip("/").split("/") if p]
    root_name = os.path.basename(root)
    for idx, part in enumerate(parts):
        if part != root_name:
            continue
        candidate_rel = "/".join(parts[idx + 1:])
        candidate, _ = resolve_safe_path(project_path, candidate_rel)
        if candidate and (os.path.exists(candidate) or os.path.isdir(os.path.dirname(candidate))):
            return candidate
    return scoped_path

LIVECODE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": "Find symbol definitions by name in the codebase index. Prefer over grep for known identifiers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name or substring; Class.method narrows to a class's method"},
                    "kind": {"type": "string", "description": "Optional: class, function, async_function, interface, type, enum, variable"},
                    "max_results": {"type": "integer"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "Find text references to a symbol name across the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_symbols",
            "description": "List indexed symbols in a file or directory prefix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory prefix"},
                    "max_results": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "Save a durable fact about this project for future sessions (appends to MEMORY.md).",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "Fact to remember"},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "Search project memory (MEMORY.md + past session logs) via FTS + local embeddings. "
                "Use to recall prior decisions, conventions, and debugging notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max hits (default 6)"},
                    "min_score": {"type": "number", "description": "Minimum score (default 0.0)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_get",
            "description": "Read a memory file by relative path under the project memory store (e.g. MEMORY.md or sessions/...).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path under memory/"},
                    "from_line": {"type": "integer", "description": "0-based start line"},
                    "lines": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": (
                "Start a child agent for one focused sub-task; it works on its own and returns its findings. "
                "Call several in one response to run them in parallel: read-only subagents for a broad "
                "investigation, or writers for a large change split into independent parts. A writer "
                "(read_only false) given `files` may edit only those files and runs no commands, so writers "
                "whose files do not overlap run side by side. Give each a self-contained goal: the exact "
                "change, the names and signatures it must use or keep, and what to report back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short label for the chat, 2-5 words (e.g. 'Billing API routes')"},
                    "goal": {"type": "string", "description": "Self-contained task: what to find or change, and what to report back"},
                    "read_only": {"type": "boolean", "description": "Default true (search and read only). Set false for a writer."},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Writers only: the files or folders this subagent owns and may create or edit. "
                            "Writers started in the same response must not share any of them."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find files by glob pattern (e.g. '**/*.tsx', 'src/**/*.py'). "
                "Returns paths sorted by modification time. Use for filename-pattern discovery."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern e.g. '**/*.test.py'"},
                    "path": {"type": "string", "description": "Optional subdirectory to search in"},
                    "max_results": {"type": "integer", "description": "Max files (default 100)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": (
                "Fuzzy search for files by path/name substring using the workspace index. "
                "Use when you know part of a filename but not the exact path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match in file path or name"},
                    "ext": {"type": "string", "description": "Optional extension filter e.g. '.ts'"},
                    "path_prefix": {"type": "string", "description": "Optional directory prefix e.g. 'src/'"},
                    "max_results": {"type": "integer", "description": "Max results (default 50)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_repo",
            "description": (
                "Search project code using regex (ripgrep). Returns matching lines with file paths and "
                "line numbers. Set output_mode to 'files' to list every file that matches (with match "
                "counts) — the way to find all call sites before a refactor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search"},
                    "glob_filter": {"type": "string", "description": "Optional glob e.g. '*.py'"},
                    "directory": {"type": "string", "description": "Optional subdirectory to scope search"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files", "count"],
                        "description": "'content' (default): matching lines. 'files': matching file paths with counts. 'count': totals only.",
                    },
                    "max_results": {"type": "integer", "description": "Max matching lines (default 100, max 400)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_repo_file",
            "description": (
                "Read a file from the project. Returns numbered source lines "
                "(format: LINE_NUMBER| content), up to 1000 lines per call. Pass start_line/end_line "
                "for a window; when the result says there is more, continue from next_start_line. "
                "The LINE_NUMBER| prefix is not part of the file — do not include it in edit_file old_string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative path within project"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_repo_dir",
            "description": "List files and subdirectories in the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Relative directory path, empty for root"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": (
                "Fast git history lookup. Prefer this over run_command for commit history, "
                "file blame context, or searching commits by message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional path filter (file or directory)"},
                    "grep": {"type": "string", "description": "Optional commit message search (--grep)"},
                    "max_count": {"type": "integer", "description": "Max commits (default 25, max 80)"},
                    "since": {"type": "string", "description": "Optional --since date e.g. '2025-01-01'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ast_symbols",
            "description": "Extract Python symbols from a .py file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative path to .py file"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with full content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact string in a file. "
                "read_repo_file prefixes each line with \"LINE_NUMBER| \" — that prefix is not part of "
                "the file: match only what comes after the |, with its exact indentation. "
                "old_string must match exactly one place unless replace_all=true "
                "(handy for renaming an identifier or mirrored STG/DWD blocks). "
                "To create a new file, set old_string to an empty string. "
                "new_string must differ from old_string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact text to find"},
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text (must differ from old_string)",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences of old_string (default false)",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": (
                "Make several exact-string replacements in one file in a single call. Edits apply in "
                "order, each to the result of the previous one, and either all succeed or the file is "
                "left untouched. Each old_string follows the edit_file rules (exact text, exact "
                "indentation, no LINE_NUMBER| prefixes, unique unless replace_all=true)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "description": "Replacements to apply, in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string", "description": "Exact text to find"},
                                "new_string": {"type": "string", "description": "Replacement text"},
                                "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["file_path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command in the project directory (tests, builds, installs, git). It waits "
                "for the command to finish (default timeout 600s; set timeout_seconds up to 3600 for "
                "long builds). For dev servers, watchers, and other processes that keep running, set "
                "background=true: it returns a command_id right away, then use command_status to read "
                "output and kill_command to stop it. stdin is closed, so pass non-interactive flags "
                "(e.g. --yes). Do NOT use for git history — use git_log instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "description": {
                        "type": "string",
                        "description": "Short title for the command shown in the chat, 3-8 words (e.g. 'Run the unit tests').",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Stop the command after this many seconds (default 600, max 3600).",
                    },
                    "background": {
                        "type": "boolean",
                        "description": "Start a long-running process and return immediately with a command_id.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "command_status",
            "description": (
                "Check a background command started with run_command(background=true): whether it is "
                "still running, its exit code, and the latest output. wait_seconds waits for the "
                "command to exit (or for `until` to appear in the output) before answering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command_id": {"type": "string", "description": "The command_id run_command returned"},
                    "wait_seconds": {"type": "integer", "description": "Wait up to this long, 0-120 (default 0)"},
                    "until": {"type": "string", "description": "Optional regex; stop waiting once the output matches"},
                },
                "required": ["command_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_command",
            "description": "Stop a background command started with run_command(background=true).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_id": {"type": "string", "description": "The command_id run_command returned"},
                },
                "required": ["command_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_command",
            "description": (
                "Restart a dev server or watcher: stop the background command (and whatever is still "
                "listening on its port, so a leftover process cannot keep the port), start it again with the "
                "same command, and return the new command_id with its first output. Use it when a code change "
                "does not show after reloading the page (a hard reload included), when the page stops loading, "
                "or when command_status shows the server exited or is stuck. Without command_id, pass command "
                "to start a server that was started outside LiveCode after freeing its port. wait_seconds and "
                "until wait for it to come up (e.g. until: \"localhost:\\d+\")."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command_id": {"type": "string", "description": "The background command to restart (its command_id from run_command)"},
                    "command": {"type": "string", "description": "Without command_id: the command to start (a dev server), after freeing its port"},
                    "port": {"type": "integer", "description": "The port it listens on, when the command or its output does not show it; whatever listens there is stopped first"},
                    "wait_seconds": {"type": "integer", "description": "Wait up to this long for it to come up, 0-120 (default 0)"},
                    "until": {"type": "string", "description": "Optional regex; stop waiting once the output matches (e.g. its URL)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attempt_completion",
            "description": (
                "Finish the turn with your final summary for the user: what changed, how it was "
                "verified, and what is left. Call it only when the whole task is done and verified, "
                "or when you are truly blocked. Replying with the summary directly also ends the turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": "Final summary for the user"},
                },
                "required": ["result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Maintain a live task list for the current work. Call it at the start of any task "
                "that needs 3+ non-trivial steps, then update statuses as you go. Keep exactly one "
                "item 'in_progress'. The loop tracks this list and will not let a multi-step task "
                "end while items are still pending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The task items.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Stable short id, e.g. 'add-helper'"},
                                "content": {"type": "string", "description": "One-line description of the step"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                                },
                            },
                            "required": ["id", "content"],
                        },
                    },
                    "merge": {
                        "type": "boolean",
                        "description": "Merge into the existing list (default true). Pass false to replace it wholesale.",
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_goal",
            "description": (
                "Report progress on the overall objective. Use 'completed: true' with a summary when "
                "the whole request is done, or 'blocked_reason' only after 3+ failed attempts at the "
                "same sub-problem. A bare 'message' just logs progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "completed": {"type": "boolean", "description": "True only when the objective is fully met."},
                    "message": {"type": "string", "description": "Short progress note or completion summary."},
                    "blocked_reason": {"type": "string", "description": "Why you are stuck (failure signal, not success text)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_definition",
            "description": (
                "Jump to the real definition of the symbol at a position in a Python file, using the "
                "language server. Prefer this over find_symbol for .py files — it resolves imports, "
                "methods, and the standard library accurately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Repo-relative path to a .py file"},
                    "line": {"type": "integer", "description": "1-based line number of the symbol"},
                    "character": {"type": "integer", "description": "1-based column of the symbol"},
                },
                "required": ["file_path", "line", "character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_references",
            "description": (
                "Find everywhere the Python symbol at a position is used, using the language server. "
                "More accurate than find_references (which is a text scan) for .py files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Repo-relative path to a .py file"},
                    "line": {"type": "integer", "description": "1-based line number of the symbol"},
                    "character": {"type": "integer", "description": "1-based column of the symbol"},
                },
                "required": ["file_path", "line", "character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_hover",
            "description": "Get the type, signature, and docstring for the Python symbol at a position, using the language server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Repo-relative path to a .py file"},
                    "line": {"type": "integer", "description": "1-based line number of the symbol"},
                    "character": {"type": "integer", "description": "1-based column of the symbol"},
                },
                "required": ["file_path", "line", "character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_diagnostics",
            "description": (
                "Get the language server's diagnostics (errors and warnings) for a Python file. Use it "
                "to check a file compiles cleanly after you edit it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Repo-relative path to a .py file"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_document_symbols",
            "description": "Get the Python language server's outline for a file: classes, functions, methods, and variables with ranges.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Repo-relative path to a .py file"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_completion",
            "description": "Get read-only Python completion suggestions at a position using the language server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Repo-relative path to a .py file"},
                    "line": {"type": "integer", "description": "1-based line number"},
                    "character": {"type": "integer", "description": "1-based column"},
                    "max_results": {"type": "integer", "description": "Maximum suggestions to return (default 40, max 80)"},
                },
                "required": ["file_path", "line", "character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_rename_preview",
            "description": "Preview the Python language server workspace edits for renaming a symbol. This never writes files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Repo-relative path to a .py file"},
                    "line": {"type": "integer", "description": "1-based line number of the symbol"},
                    "character": {"type": "integer", "description": "1-based column of the symbol"},
                    "new_name": {"type": "string", "description": "Candidate new symbol name"},
                },
                "required": ["file_path", "line", "character", "new_name"],
            },
        },
    },
]

WORKSPACE_SCOPED_TOOL_NAMES = {
    "list_symbols",
    "glob_files",
    "find_files",
    "grep_repo",
    "read_repo_file",
    "list_repo_dir",
    "git_log",
    "ast_symbols",
    "write_file",
    "edit_file",
    "multi_edit",
    "run_command",
    "lsp_definition",
    "lsp_references",
    "lsp_hover",
    "lsp_diagnostics",
    "lsp_document_symbols",
    "lsp_completion",
    "lsp_rename_preview",
}

WORKSPACE_ARGUMENT_DESCRIPTION = "Optional workspace folder name for multi-root workspaces. You can also prefix paths as '<workspace>/<path>'."

for _tool in LIVECODE_TOOLS:
    _fn = _tool.get("function", {}) if isinstance(_tool, dict) else {}
    if _fn.get("name") in WORKSPACE_SCOPED_TOOL_NAMES:
        _params = _fn.get("parameters") or {}
        _props = _params.setdefault("properties", {})
        _props.setdefault("workspace", {"type": "string", "description": WORKSPACE_ARGUMENT_DESCRIPTION})

CREATE_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "create_plan",
        "description": (
            "Record the implementation plan while plan mode is active. This is the only write "
            "available in plan mode. Call once with the complete plan; pass plan_file to revise "
            "an existing plan after user feedback instead of creating a second one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short plan title, e.g. 'LiveCode plan mode'",
                },
                "overview": {
                    "type": "string",
                    "description": "One or two sentences on what the plan does, shown on the plan card in chat",
                },
                "plan": {
                    "type": "string",
                    "description": (
                        "Full plan as markdown: a '# title', a short intro on today's code, the "
                        "pattern to reuse, and scope; an optional mermaid diagram; one '##' section "
                        "per area of change with linked files and named symbols; and a closing "
                        "'## Tests' section."
                    ),
                },
                "todos": {
                    "type": "array",
                    "description": (
                        "Ordered implementation to-dos, rendered as '## Task checklist'. The user can "
                        "edit them before building, and Build tracks progress against them."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["content"],
                    },
                },
                "plan_file": {
                    "type": "string",
                    "description": "Existing plan filename to overwrite (from a prior create_plan)",
                },
            },
            "required": ["title", "plan"],
        },
    },
}

ASK_QUESTION_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_question",
        "description": (
            "Ask the user what only they can decide, in a questions card: the turn pauses and continues "
            "with their answers, in this same turn. Give options for a choice (allow_multiple when several "
            "can apply together, else one is picked); a free-text 'Other' row is always added, so do not "
            "include an 'Other' option. Give no options for an open answer (what a message should say, a "
            "name, an address): the card shows a text box. Use it instead of ending your turn with a "
            "question, only for what the request, the code and the page cannot answer, never to confirm "
            "what the user already asked for. Ask everything you need in one call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "1-4 questions, most important first.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Short stable id, e.g. 'storage'"},
                            "prompt": {"type": "string", "description": "The question, one or two sentences"},
                            "options": {
                                "type": "array",
                                "description": "2-5 distinct choices, recommended first; leave out for an open answer (a text box)",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "label": {"type": "string", "description": "Concise choice text"},
                                    },
                                    "required": ["id", "label"],
                                },
                            },
                            "allow_multiple": {
                                "type": "boolean",
                                "description": "True when several options can be picked together.",
                            },
                        },
                        "required": ["id", "prompt"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
}

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
            "description": (
                "Search the web — only when the user explicitly asked for internet research "
                "or enabled the Web toggle. Returns snippets and URLs."
            ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "allowed_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of domains to restrict results",
                },
                "max_results": {"type": "integer", "description": "Max results (default 8)"},
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
            "description": (
                "Fetch readable text from a public URL. Only when the user provided a link "
                "or explicitly asked for web/internet lookup."
            ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "HTTPS or HTTP URL to fetch"},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 12000)"},
            },
            "required": ["url"],
        },
    },
}

BROWSER_TOOL = {
    "type": "function",
    "function": {
        "name": "browser",
        "description": (
            "Drive the built-in browser: a Chromium the user watches in their Browser tab (or their own Chrome, "
            "when they attached it), with this project's cookies and logins. Use it to preview and check the app you "
            "are building (localhost dev servers), to read pages that need JavaScript or a login, and to make a UI "
            "match a design. One action per call; any action can name another tab with tab_id:\n"
            "- navigate {url}: open a URL, host:port or search words; the result includes the page snapshot\n"
            "- tabs: the open tabs; new_tab {url?, background?} (background keeps the current tab in front: a "
            "design or docs page to look at); switch_tab {tab_id}; close_tab {tab_id?}. Every result names the tab it "
            "ran in (tab_id) and lists the tabs again when they changed (a link or popup opened one, the user or another "
            "agent opened or closed one), with tab_note saying what happened and where your actions go now. Refs belong "
            "to the tab of your latest snapshot\n"
            "- snapshot {query?, selector?, max_chars?}: an Overview first (what kind of page it is, whether a popup or "
            "dialog is open and its buttons, blocked or captcha pages, forms with their fields, repeated items such as "
            "result cards), then the interactive elements numbered [ref] (on-screen ones first; shadow DOM and iframes "
            "included; duplicate names carry the item they belong to) and the page's text. It is the way to read a page: "
            "to find something (an item, a price, a listing) read it, click through its links, or pull the data out with "
            "javascript_exec; do not scroll to look for it. query narrows elements and text to a word; selector reads one "
            "part of the page\n"
            "- click {ref | selector | text | x and y}; type {ref | selector, text, submit?}; press {key}\n"
            "- scroll {direction?, amount? | to: top or bottom | to_y | ref | selector | text}: scroll, or bring an "
            "element to the middle of the view. The result says whether the page moved and lists what is now in view. "
            "Scroll only to load lazy content or to show the user a part of the page; if it did not move, is at the end, "
            "or you have scrolled a few times, stop: dismiss a popup, consent, location or login dialog, click a 'Load more' or a "
            "category, or run a script. wait {text | selector | seconds | change}: change waits until the page's content "
            "changes (search results, a menu, a loaded list). Search results and menus often appear a second or two after "
            "typing: if the result is not there yet, wait {change: true} or take a screenshot and look before concluding "
            "there are no matches\n"
            "- resize {device | width, height}: set the viewport, which stays until changed. Devices: desktop-hd "
            "1920×1080, desktop 1440×900, laptop 1280×800, tablet-landscape 1024×768, ipad-air 820×1180, tablet "
            "768×1024, android 412×915, mobile 390×844, iphone-se 375×667; fit gives it back to the Browser tab\n"
            "- screenshot {full_page?}: what a tab looks like (you see the image when your model takes images)\n"
            "- crop {ref | selector | text, padding? | x, y, width, height}: a close-up of one element (whole, even "
            "below the fold) or a region of a tab. crop {image, x, y, width, height}: cut a region out of an image "
            "instead (image: attachment:N, shot:<id>, tab:<id>, an image URL or a project path), e.g. the design's "
            "frame out of a design tool's page, or one component of a design\n"
            "- inspect {ref | selector | text}: an element's box and computed styles (font, colours, padding, radius, "
            "layout, shadow) with an outline of what is inside it; without a target, the page outlined the way a "
            "design tool lists layers. Boxes are page coordinates: CSS px from the page's top-left\n"
            "- compare {reference?, content?, elements?, full_page? | ref | selector | text | x, y, width, height, reference_region?, "
            "reference_scale?}: the view, the page or one element against the design, element by element: every element "
            "of the page (or of the selector) is cropped with its place in the design, lined up on its own and measured, "
            "and each one that differs says what to change: where it sits (px, against its parent), its size, background "
            "and text colour, font size, letter spacing, line height or wrapping, corner radius, shadow, or that it is not "
            "in the design; missing_on_page lists parts of the design the page lacks, and progress what changed since the "
            "last compare. A screenshot of one part of a page (a card, an input box, a form) is found on the page by itself "
            "(located: the element, its selector, similar copies in a list or grid) and compared with that element alone; "
            "a whole-page compare with many differences groups them into sections (a card, a form, a micro-frontend's root; "
            "copies of one component as one) to work through one at a time with compare {selector}. "
            "What an element shows is judged by content: layout (the default) ignores other words, numbers "
            "and images (a design's sample data never matches a running app's: a card's name, price or photo) and counts "
            "them separately; content: \"exact\" holds other text, images and pixels against the page too. Fix findings top "
            "to bottom (a size change moves what follows it). The board numbers them on the design and the page, with a "
            "close-up of each. elements: false compares the two images pixel by pixel instead (for screenshots and "
            "images rather than a page): similarity (the share of the design's content that matches), structure (how alike "
            "the shapes and layout are, 0-100: it stays high when only colours differ), a verdict (identical, nearly "
            "identical, different), the differing areas with the page element at each and their kind: missing (in the "
            "design, not on the page), extra (on the page only), moved (the design's content moved.dx, moved.dy px away), "
            "color (the same shape in another colour: design_color, page_color, delta_e), content (other text or shapes), "
            "size (past the other image's edge) or edges (only outlines differ; minor ones are anti-aliasing or sub-pixel "
            "rendering), and bands of the page that sit higher or lower than the design. "
            "reference: attachment:N (the Nth image the user attached: a screenshot from Figma, Canva or "
            "any design tool), tab:<id> (a tab showing the design, captured now), a design link (a page opens in a "
            "background tab and is captured; an image is downloaded), shot:<id> (an earlier screenshot or crop), "
            "panel (the Browser tab's compare image), or a project image path. Default: this chat's design (what it "
            "last compared with, or the newest attachment). reference_region is the part of the design to use (its "
            "frame in a design tool's page, or where the element is in the design); otherwise an element or region is "
            "compared with the same place in a whole-page design\n"
            "- javascript_exec {script}: run JavaScript in the page; returns the last expression (top-level await works). "
            "Use it to read data (return [...document.querySelectorAll('a')].filter(a => /keyword/i.test(a.textContent))"
            ".map(a => [a.textContent.trim(), a.href])), to click through what a snapshot cannot reach, or to scroll a "
            "container\n"
            "- hover {ref | selector | text}; click also takes button (left, right, middle) and double; drag {ref | selector, "
            "to_ref | to_selector | to_x and to_y}; select {ref | selector, value}: choose a dropdown option; check {ref | "
            "selector, checked?}: tick or untick; upload {ref | selector, files}: project file paths\n"
            "- dialog {accept?, text?}: answer the next alert, confirm or prompt the page opens (call it before the action "
            "that opens it; by default alerts are accepted and confirms and prompts dismissed)\n"
            "- find {text, index?}: search the page's text, highlight and scroll to a match; zoom {level}: 1 is 100%\n"
            "- console {level?, limit?, clear?}: the page's console messages and errors; network {filter?, status?, type?, "
            "limit?}: requests and their status (status: failed, errors or a code); downloads: files the page downloaded. "
            "Results also flag new console errors and downloads\n"
            "- batch {actions: [{action, ...}, ...]}: up to 8 actions in one call, in order, stopping at the first failure; "
            "use it for known sequences (open, type, submit) to save round trips. Any action also takes timeout (seconds)\n"
            "- back, forward, reload {hard?}: hard clears the cache and reloads from the server, for a page that keeps "
            "showing old code after a change. If a hard reload still shows the old code, or the page will not load "
            "(connection refused, a blank or error page), the dev server needs a restart: restart_command\n"
            "Forms that submit, send, buy, apply or post something: fill in only what the user told you or what the page "
            "pre-filled from their own profile; do not answer personal, legal or screening questions (work authorization, "
            "salary, availability, demographics, willingness to relocate) on their behalf, ask them with ask_question. The "
            "final Submit, Send, Apply or Pay button is held back (and Enter in a form whose button is one of them) unless the "
            "user's request already asked for exactly that step (\"send her a message saying …\", \"submit it\"): then it goes "
            "through, so do it and do not ask again. Otherwise ask for a clear yes with ask_question, and pass confirm: true "
            "once they said yes.\n"
            "When actions keep failing or the page will not move, the result carries a screenshot: look at it and act on "
            "what it shows instead of repeating the same call: its pink numbered boxes are refs for click and type (screenshot "
            "{marks: true} draws them on any screenshot), or click x, y, or press Escape.\n"
            "Each screenshot and crop result names its shot:<id>, which later calls can use as a reference or crop "
            "again. Refs come from the latest snapshot and change when the page does. Start a dev server with "
            "run_command (background: true) before opening it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": [a for a in BROWSER_AGENT_ACTIONS if a != "figma"]},
                "url": {"type": "string", "description": "navigate/new_tab: URL, host:port, or search words"},
                "background": {"type": "boolean", "description": "new_tab: open it behind the current tab"},
                "ref": {"type": "integer", "description": "element number from the latest snapshot"},
                "selector": {"type": "string", "description": "CSS selector of an element, when there is no ref"},
                "text": {"type": "string", "description": "type: the text to enter; click/wait/crop/compare/inspect/scroll: visible text of the element"},
                "submit": {"type": "boolean", "description": "type: press Enter afterwards"},
                "key": {"type": "string", "description": "press: a key or chord, e.g. Enter, Escape, ArrowDown, Control+A"},
                "direction": {"type": "string", "enum": ["down", "up", "left", "right"], "description": "scroll direction (default down)"},
                "amount": {"type": "integer", "description": "scroll: pixels (default 600)"},
                "to": {"type": "string", "enum": ["top", "bottom"], "description": "scroll: to the top or the bottom of the page"},
                "to_y": {"type": "number", "description": "scroll: to this page y (CSS px from the top); drag: viewport y to drop at, with to_x"},
                "device": {"type": "string", "enum": [*BROWSER_DEVICE_PRESETS, "fit"], "description": "resize: a named resolution, or fit (the Browser tab's size)"},
                "x": {"type": "number", "description": "click: viewport x read off a screenshot; crop/compare: region x (viewport, or page with full_page; the image's with image)"},
                "y": {"type": "number", "description": "click: viewport y; crop/compare: region y (viewport, or page with full_page; the image's with image)"},
                "width": {"type": "number", "description": "crop/compare: region width; resize: viewport width (CSS px)"},
                "height": {"type": "number", "description": "crop/compare: region height; resize: viewport height (CSS px)"},
                "padding": {"type": "number", "description": "crop/compare: pixels around the element (crop default 8, compare 0)"},
                "image": {"type": "string", "description": "crop: the image to cut from instead of the page: attachment:N, shot:<id>, tab:<id>, an image URL or a project path"},
                "reference": {"type": "string", "description": "compare: the design: attachment:N, tab:<id>, a design link or image URL, shot:<id>, panel, or a project image path (default: this chat's design)"},
                "reference_region": {
                    "type": "object",
                    "description": "compare: the part of the design to use, in its CSS px (for tab:<id>, that tab's page coordinates)",
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "width": {"type": "number"}, "height": {"type": "number"}},
                },
                "reference_scale": {"type": "number", "description": "compare/crop: the design image's scale (2 for a 2x export or a retina screenshot) when it is not 1"},
                "script": {"type": "string", "description": "javascript_exec: the code to run in the page"},
                "full_page": {"type": "boolean", "description": "screenshot/compare: the whole page instead of the view; crop/compare region: x and y are page coordinates"},
                "elements": {"type": "boolean", "description": "compare: element by element (the default), each element cropped and measured against its place in the design; false compares the two images pixel by pixel instead"},
                "locate": {"type": "boolean", "description": "compare: find the element a screenshot of one part of the page shows (a card, a form, an input box) and compare with it (automatic when the design is narrower than the page; true forces it, false compares with the view)"},
                "content": {"type": "string", "enum": ["layout", "exact"], "description": "compare: layout (the default, or the user's setting) measures each element's place, size, colours and type and ignores what it shows (other words, numbers or images: a design's sample data); exact counts other text, images and pixels as differences too"},
                "seconds": {"type": "number", "description": "wait: how long, at most 30"},
                "tab_id": {"type": "string", "description": "the tab to act on, from tabs (default: the current tab); switch_tab/close_tab: the tab"},
                "query": {"type": "string", "description": "snapshot: keep only elements and text lines containing this word"},
                "max_chars": {"type": "integer", "description": "snapshot: text budget (default 12000, at most 40000)"},
                "marks": {"type": "boolean", "description": "screenshot: draw the snapshot refs as numbered boxes on the view"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "click: which mouse button"},
                "double": {"type": "boolean", "description": "click: double click"},
                "to_ref": {"type": "integer", "description": "drag: the ref to drop on"},
                "to_selector": {"type": "string", "description": "drag: the CSS selector to drop on"},
                "to_x": {"type": "number", "description": "drag: viewport x to drop at"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "upload: project file paths"},
                "value": {"type": "string", "description": "select: the option's text or value"},
                "checked": {"type": "boolean", "description": "check: true to tick (default), false to untick"},
                "accept": {"type": "boolean", "description": "dialog: accept (true, default) or dismiss (false) the next dialog"},
                "index": {"type": "integer", "description": "find: which match to show (0 is the first)"},
                "level": {"type": "string", "description": "console: all, warning or error; zoom: 1, 1.5, 150 (percent) …"},
                "limit": {"type": "integer", "description": "console/network: how many recent entries (default 30-40)"},
                "filter": {"type": "string", "description": "network: only URLs containing this"},
                "status": {"type": "string", "description": "network: failed, errors, or a status code such as 404"},
                "type": {"type": "string", "description": "network: resource type such as xhr, fetch, document, script"},
                "clear": {"type": "boolean", "description": "console/network/find: clear the entries or highlights"},
                "hard": {"type": "boolean", "description": "reload: clear the cache first and reload from the server (a page that still shows old code)"},
                "timeout": {"type": "number", "description": "seconds to wait for this action (1-120) instead of the default"},
                "confirm": {"type": "boolean", "description": "click/type: set true only when the user clearly asked for exactly this final step (a Submit, Send, Post, Pay or Apply button is held back otherwise)"},
                "change": {"type": "boolean", "description": "wait: wait until the page's content changes (seconds is the limit, default 8)"},
                "actions": {
                    "type": "array",
                    "description": "batch: the actions to run in order, each an object with action and its arguments",
                    "items": {"type": "object"},
                },
            },
            "required": ["action"],
        },
    },
}

_BROWSER_FIGMA_TEXT = (
    "- figma {url | node, refresh?}: read a Figma frame through Figma's API (a token is configured): its layer outline "
    "(boxes from the frame's top-left, text, fonts, colours, radii, padding, auto-layout) and its image. It becomes "
    "reference \"figma\", and a layer is \"figma:<layer id or name>\" (compare it with the matching element; layers "
    "need no more API calls). Figma limits API calls tightly: a frame is fetched once and reused, so pass refresh only "
    "after the design changed. If the API refuses (a rate limit, no access), the link opens in a background tab "
    "instead: take the design from there as with any design tool's link\n"
)


def browser_tool(figma: bool = False) -> dict:
    if not figma:
        return BROWSER_TOOL
    tool = copy.deepcopy(BROWSER_TOOL)
    function = tool["function"]
    marker = "- javascript_exec {script}"
    function["description"] = function["description"].replace(marker, _BROWSER_FIGMA_TEXT + marker, 1)
    props = function["parameters"]["properties"]
    props["action"]["enum"] = list(BROWSER_AGENT_ACTIONS)
    props["url"] = {"type": "string", "description": "navigate/new_tab: URL, host:port, or search words; figma: the Figma link"}
    props["node"] = {"type": "string", "description": "figma: a layer of the loaded frame (id like 12:34, or its name), or another frame's node id"}
    props["refresh"] = {"type": "boolean", "description": "figma: fetch the frame again (it changed); otherwise the one fetched before is reused"}
    props["reference"] = dict(props["reference"])
    props["reference"]["description"] = ("compare: the design: figma or figma:<layer> (the loaded Figma frame), attachment:N, tab:<id>, "
                                         "a design link or image URL, shot:<id>, panel, or a project image path (default: this chat's design)")
    return tool


CALL_MCP_TOOL = {
    "type": "function",
    "function": {
        "name": "call_mcp_tool",
        "description": (
            "Call any enabled MCP server/tool by explicit server_name and tool_name. "
            "Use this for project-local MCPs and for tools that are connected but not individually exposed "
            "because of model tool-count limits. Mutating or consequential calls require approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "server_name": {
                    "type": "string",
                    "description": "Enabled MCP server name, e.g. github, filesystem, postgres",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Tool name on that server, e.g. force_local_task_run, write_s3_file, find",
                },
                "arguments": {
                    "type": "object",
                    "description": "Tool arguments as a JSON object",
                },
            },
            "required": ["server_name", "tool_name", "arguments"],
        },
    },
}


MAX_OPENAI_TOOL_COUNT = 128
MCP_DYNAMIC_TOOL_BUDGET = 64

STRUCTURED_OUTPUT_TOOL = "structured_output"
STRUCTURED_OUTPUT_MAX_RETRIES = 3
STRUCTURED_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": True,
}

STRUCTURED_OUTPUT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": STRUCTURED_OUTPUT_TOOL,
        "description": (
            "Return final JSON API response example(s) after reading source. "
            "Call exactly once at the end with response payload(s) derived from code."
        ),
        "parameters": STRUCTURED_OUTPUT_SCHEMA,
    },
}

def get_structured_output_tool() -> dict:
    return dict(STRUCTURED_OUTPUT_TOOL_DEF)

def validate_structured_output(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "structured_output payload must be a JSON object"
    try:
        from jsonschema import ValidationError, validate

        validate(instance=data, schema=STRUCTURED_OUTPUT_SCHEMA)
        return True, ""
    except ValidationError as exc:
        return False, str(exc.message)
    except Exception as exc:
        return False, str(exc)

def format_structured_output_answer(data: dict) -> str:
    text = json.dumps(data, indent=2, default=str)
    return f"```json\n{text}\n```"

def _remaining_mcp_tool_budget(current_tool_count: int) -> int:
    return max(0, min(MCP_DYNAMIC_TOOL_BUDGET, MAX_OPENAI_TOOL_COUNT - current_tool_count))

def get_livecode_tools(
    *,
    enable_mcp: bool = False,
    enable_web: bool = False,
    enable_browser: bool = False,
    include_structured_output: bool = False,
    project_path: str = "",
    mcp_servers: list[str] | tuple[str, ...] | None = None,
    workspace_payload: dict | None = None,
    include_mcp_bindings: bool = False,
    mcp_disabled_tools: dict[str, list[str]] | None = None,
) -> list[dict] | tuple[list[dict], dict[str, object]]:
    tools = list(LIVECODE_TOOLS)
    mcp_bindings: dict[str, object] = {}
    if include_structured_output:
        tools.append(get_structured_output_tool())
    if enable_web:
        tools.append(WEB_SEARCH_TOOL)
        tools.append(WEB_FETCH_TOOL)
    if enable_browser and browser_playwright_installed():
        tools.append(browser_tool(figma=browser_figma_configured()))
    if enable_mcp:
        tools.append(CALL_MCP_TOOL)
        dynamic_budget = _remaining_mcp_tool_budget(len(tools))
        if dynamic_budget > 0:
            try:
                from livecode import mcp_bridge
                dynamic_tools, mcp_bindings = mcp_bridge.list_mcp_tools_with_bindings(
                    project_path,
                    mcp_servers,
                    limit=dynamic_budget,
                    workspace_payload=workspace_payload,
                    tool_denylist_by_server=mcp_disabled_tools or None,
                )
                tools.extend(dynamic_tools)
            except Exception:
                pass
    tools = tools[:MAX_OPENAI_TOOL_COUNT]
    if include_mcp_bindings:
        offered = {str((item.get("function") or {}).get("name") or "") for item in tools}
        return tools, {name: binding for name, binding in mcp_bindings.items() if name in offered}
    return tools

READ_ONLY_TOOL_NAMES = frozenset({
    "grep_repo",
    "read_repo_file",
    "list_repo_dir",
    "find_symbol",
    "find_references",
    "list_symbols",
    "glob_files",
    "find_files",
    "web_search",
    "web_fetch",
    "git_log",
    "ast_symbols",
    "call_mcp_tool",
    "memory_search",
    "memory_get",
    "lsp_definition",
    "lsp_references",
    "lsp_hover",
    "lsp_diagnostics",
    "lsp_document_symbols",
    "lsp_completion",
    "lsp_rename_preview",
    "command_status",
})

LIVECODE_MODES = ("agent", "plan", "ask")

MUTATING_TOOL_NAMES = frozenset({"write_file", "edit_file", "multi_edit", "run_command", "kill_command", "restart_command"})
FILE_EDIT_TOOL_NAMES = frozenset({"write_file", "edit_file", "multi_edit"})

PLAN_MODE_REJECTION = (
    "Plan mode is active, so `{tool}` is unavailable. The only write allowed is the "
    "`create_plan` tool. Keep exploring with the read-only tools and record the "
    "approach with create_plan."
)

ASK_MODE_REJECTION = (
    "Ask mode is active, so `{tool}` is unavailable — this is a read-only conversation. "
    "Answer from the code you can read, and tell the user to switch the composer to "
    "Agent mode if they want the change applied."
)

def normalize_mode(mode: str | None) -> str:
    candidate = (mode or "").strip().lower()
    return candidate if candidate in LIVECODE_MODES else "agent"

def mode_rejection_message(mode: str, tool_name: str) -> str:
    if normalize_mode(mode) == "plan":
        return PLAN_MODE_REJECTION.format(tool=tool_name)
    return ASK_MODE_REJECTION.format(tool=tool_name)

def filter_tools_for_mode(tools: list[dict], mode: str | None) -> list[dict]:
    normalized = normalize_mode(mode)
    if normalized == "agent":
        return list(tools)
    allowed = (READ_ONLY_TOOL_NAMES - {"command_status"}) | {"attempt_completion", STRUCTURED_OUTPUT_TOOL, "browser"}
    filtered = []
    for tool in tools:
        name = (tool.get("function") or {}).get("name")
        if name in allowed:
            filtered.append(tool)
            continue
    # The questions card in every mode: a doubt is asked, and answered, within the turn.
    filtered.append(dict(ASK_QUESTION_TOOL))
    if normalized == "plan":
        filtered.append(dict(CREATE_PLAN_TOOL))
    return filtered

_JSON_DEBRIS_RE = re.compile(r'(?:"\s*,\s*"[A-Za-z_]+"\s*:.*|"\s*\}+\s*)$', re.DOTALL)


def _sanitize_shell_command(cmd: str) -> str:
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (cmd or "").strip())
    if cleaned.count('"') % 2 == 1:
        cleaned = _JSON_DEBRIS_RE.sub("", cleaned)
    return cleaned.strip()

INVALID_ARGUMENTS_HINT = (
    "If this was a large write_file, edit_file, or multi_edit call, the output was probably cut "
    "off by the output-token limit: split the change into several smaller calls, or write the "
    "file in parts (write_file for the first part, then edit_file to add the rest)."
)
COMMAND_TIMEOUT_DEFAULT_S = 600
COMMAND_TIMEOUT_MAX_S = 3600


def _loads_tool_arguments(raw: str) -> tuple[dict | None, str]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        try:
            loaded, _end = json.JSONDecoder().raw_decode(raw.lstrip())
        except json.JSONDecodeError:
            return None, f"{exc.msg} at character {exc.pos} of {len(raw)}"
    if not isinstance(loaded, dict):
        return None, "Tool arguments must be a JSON object"
    return loaded, ""


def _coerce_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def parse_livecode_tool_arguments(tool_name: str, raw) -> tuple[dict, str | None]:
    if isinstance(raw, dict):
        parsed: dict = dict(raw)
    elif not (raw or "").strip():
        parsed = {}
    else:
        loaded, problem = _loads_tool_arguments(str(raw))
        if loaded is None:
            if problem == "Tool arguments must be a JSON object":
                return {}, problem
            if tool_name == "run_command":
                return {}, 'Invalid tool arguments — provide only {"command": "..."}'
            return {}, f"The {tool_name} arguments were not valid JSON ({problem}). {INVALID_ARGUMENTS_HINT}"
        parsed = loaded
    parsed.pop("_permission_approved", None)

    if tool_name != "run_command":
        return parsed, None

    cmd = parsed.get("command")
    if not cmd or not str(cmd).strip():
        if parsed:
            return {}, 'Invalid tool arguments — run_command requires {"command": "..."}'
        return {}, "Empty command"
    out = {"command": _sanitize_shell_command(str(cmd))}
    description = parsed.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = " ".join(description.split())[:120]
    workspace = parsed.get("workspace")
    if isinstance(workspace, str) and workspace.strip():
        out["workspace"] = workspace.strip()
    timeout = parsed.get("timeout_seconds")
    try:
        if timeout is not None and str(timeout).strip():
            out["timeout_seconds"] = max(1, min(int(float(timeout)), COMMAND_TIMEOUT_MAX_S))
    except (TypeError, ValueError):
        pass
    if _coerce_bool(parsed.get("background")):
        out["background"] = True
    return out, None

TOOL_RESULT_MAX_CHARS = 16_000
READ_RESULT_MAX_CHARS = 100_000
COMMAND_RESULT_MAX_CHARS = 32_000
GREP_RESULT_MAX_MATCHES = 150

def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_PAGER", "cat")
    env.setdefault("PAGER", "cat")
    env.setdefault("NONINTERACTIVE", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env

def compact_tool_result_for_llm(tool_name: str, result: dict) -> dict:
    if not isinstance(result, dict):
        return {"summary": str(result)[:500]}
    if tool_name == "spawn_subagent":
        return compact_subagent_result(result)
    if result.get("error"):
        out = dict(result)
        if (
            tool_name in ("write_file", "edit_file")
            and out.get("error_kind") == "invalid_input"
            and "missing required argument: file_path" in str(out.get("error") or "").lower()
        ):
            out["recovery_hint"] = (
                "Retry the tool call with a relative file_path. If you do not know the path, "
                "use find_files, glob_files, or grep_repo first; do not repeat write_file/edit_file without file_path."
            )
        return out

    out = dict(result)
    if tool_name == "grep_repo":
        matches = out.get("matches") or []
        if len(matches) > GREP_RESULT_MAX_MATCHES:
            out["matches"] = matches[:GREP_RESULT_MAX_MATCHES]
            out["truncated"] = True
            out["match_count"] = len(matches)
            out["note"] = (
                f"Showing {GREP_RESULT_MAX_MATCHES} of {len(matches)} matches. Narrow the pattern or "
                "directory, or use output_mode='files' to list every matching file."
            )
    elif tool_name == "read_repo_file":
        content = out.get("content") or ""
        if len(content) > READ_RESULT_MAX_CHARS:
            out["content"] = content[:READ_RESULT_MAX_CHARS] + "\n... [truncated]"
            out["truncated"] = True
    elif tool_name in ("run_command", "command_status", "kill_command", "restart_command"):
        output = out.get("output") or ""
        if len(output) > COMMAND_RESULT_MAX_CHARS:
            out["output"] = head_tail_text(output, COMMAND_RESULT_MAX_CHARS)
            out["output_truncated"] = True
    elif tool_name == "git_log":
        commits = out.get("commits") or []
        if len(commits) > 40:
            out["commits"] = commits[:40]
            out["truncated"] = True
    elif tool_name == "call_mcp_tool" or tool_name.startswith("mcp__"):
        data = out.get("data")
        if isinstance(data, (dict, list)):
            raw = json.dumps(data, default=str)
            if len(raw) > TOOL_RESULT_MAX_CHARS:
                out["data"] = raw[:TOOL_RESULT_MAX_CHARS] + "... [truncated]"
                out["truncated"] = True
        elif isinstance(data, str) and len(data) > TOOL_RESULT_MAX_CHARS:
            out["data"] = data[:TOOL_RESULT_MAX_CHARS] + "... [truncated]"
            out["truncated"] = True
    elif tool_name in ("find_symbol", "find_references", "list_symbols"):
        for key in ("symbols", "references", "matches"):
            items = out.get(key) or []
            if len(items) > 80:
                out[key] = items[:80]
                out["truncated"] = True
    elif tool_name in ("glob_files", "find_files"):
        files = out.get("files") or []
        if len(files) > 150:
            out["files"] = files[:150]
            out["truncated"] = True
    elif tool_name == "web_search":
        results = out.get("results") or []
        if len(results) > 8:
            out["results"] = results[:8]
            out["truncated"] = True
    elif tool_name == "web_fetch":
        content = out.get("content") or ""
        if len(content) > TOOL_RESULT_MAX_CHARS:
            out["content"] = content[:TOOL_RESULT_MAX_CHARS] + "\n... [truncated]"
            out["truncated"] = True
    elif tool_name == "browser":
        if out.get("shot_id"):
            out["shot"] = "shot:" + str(out["shot_id"])
        for key in ("shot_id", "shot_url", "storage_key", "width", "height"):
            out.pop(key, None)
    return out

READ_DEFAULT_LINES = 1000
READ_MAX_LINES = 2000
READ_MAX_LINE_CHARS = 2000
READ_MAX_FILE_BYTES = 25 * 1024 * 1024


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _livecode_read_file(root: str, rel_path: str, start_line=None, end_line=None, *, display_path: str = "") -> dict:
    shown = display_path or rel_path
    full, err = resolve_safe_path(root, rel_path)
    if full is None:
        return {"error": err, "error_kind": "invalid_input"}
    if os.path.isdir(full):
        return {
            "error": f"{shown} is a directory. Use list_repo_dir or glob_files to see what it contains.",
            "error_kind": "is_directory",
        }
    if not os.path.isfile(full):
        return {
            "error": f"File not found: {shown}" + suggest_similar_filename(full, rel_path),
            "error_kind": "file_not_found",
        }
    size = os.path.getsize(full)
    if size > READ_MAX_FILE_BYTES:
        return {
            "error": f"{shown} is {size // (1024 * 1024)} MB, too large to read. Use grep_repo to find the part you need.",
            "error_kind": "too_large",
        }
    with open(full, "rb") as f:
        raw = f.read()
    if b"\x00" in raw[:8192]:
        return {"error": f"{shown} looks like a binary file ({size} bytes).", "error_kind": "binary"}
    lines = raw.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    if total == 0:
        return {
            "success": True, "file": shown, "file_path": shown, "content": "",
            "total_lines": 0, "showing": "0-0", "note": "The file is empty.",
        }
    start = _positive_int(start_line) or 1
    if start > total:
        return {
            "error": f"start_line {start} is past the end of {shown} ({total} lines).",
            "error_kind": "invalid_input",
            "total_lines": total,
        }
    requested_end = _positive_int(end_line)
    if requested_end:
        end = min(total, max(start, requested_end), start + READ_MAX_LINES - 1)
    else:
        end = min(total, start + READ_DEFAULT_LINES - 1)
    numbered: list[str] = []
    used = 0
    last = start - 1
    for number in range(start, end + 1):
        line = lines[number - 1]
        if len(line) > READ_MAX_LINE_CHARS:
            line = f"{line[:READ_MAX_LINE_CHARS]} … [line cut at {READ_MAX_LINE_CHARS} of {len(line)} chars]"
        entry = f"{number}| {line}"
        if numbered and used + len(entry) + 1 > READ_RESULT_MAX_CHARS:
            break
        numbered.append(entry)
        used += len(entry) + 1
        last = number
    out = {
        "success": True,
        "file": shown,
        "file_path": shown,
        "content": "\n".join(numbered),
        "total_lines": total,
        "start_line": start,
        "end_line": last,
        "showing": f"{start}-{last}",
    }
    if last < total:
        out["truncated"] = True
        out["next_start_line"] = last + 1
        out["note"] = f"Showing lines {start}-{last} of {total}. Call read_repo_file with start_line={last + 1} to continue."
    return out


def _grep_file_summary(matches: list) -> list[dict]:
    counts: dict[str, int] = {}
    for item in matches:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file") or item.get("path") or "")
        if path:
            counts[path] = counts.get(path, 0) + 1
    return [{"file": path, "matches": count} for path, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _livecode_write_file(project_path: str, file_path: str, content: str, create_diff_html_fn) -> dict:
    if not str(file_path or "").strip():
        return {"error": "Missing required argument: file_path", "error_kind": "invalid_input"}
    full, err = resolve_safe_path(project_path, file_path)
    if full is None:
        return {"error": err, "error_kind": "invalid_input"}
    safe_rel = os.path.relpath(full, os.path.abspath(os.path.expanduser(project_path))).replace("\\", "/")
    path_err = validate_path_components(safe_rel)
    if path_err:
        return {"error": path_err, "error_kind": "filename_too_long"}
    blocked = path_blocked_for_edit(project_path, safe_rel)
    if blocked:
        return {"error": blocked, "error_kind": "invalid_input"}
    original = ""
    if os.path.isfile(full):
        try:
            original = read_text_preserving(full)
        except OSError as e:
            return {"error": str(e), "error_kind": "invalid_input"}
    if "\r\n" in original and "\r\n" not in content:
        content = content.replace("\n", "\r\n")
    if original == content:
        return {
            "success": True,
            "file_path": safe_rel,
            "action": "write_file",
            "diff_html": "",
            "additions": 0,
            "deletions": 0,
            "absolute_path": full,
            "no_changes": True,
            "edits": [],
        }
    try:
        write_text_atomic(full, content)
    except OSError as e:
        return {"error": str(e), "error_kind": "invalid_input"}
    ext = os.path.splitext(safe_rel)[1]
    diff_html, _diff_text, add_count, del_count = create_diff_html_fn(display_text(original), content, ext)
    return {
        "success": True,
        "file_path": safe_rel,
        "action": "write_file",
        "diff_html": diff_html,
        "additions": add_count,
        "deletions": del_count,
        "absolute_path": full,
        "edits": [],
        "is_new_file": not bool(original),
    }

def _livecode_edit_file(
    project_path: str,
    file_path: str,
    old_string: str,
    new_string: str,
    create_diff_html_fn,
    *,
    replace_all: bool = False,
    params: SearchReplaceParams | None = None,
    file_was_read_this_turn: bool | None = None,
) -> dict:
    if not str(file_path or "").strip():
        return {"error": "Missing required argument: file_path", "error_kind": "invalid_input"}
    full, err = resolve_safe_path(project_path, file_path)
    if full is None:
        return {"error": err, "error_kind": "invalid_input"}
    safe_rel = os.path.relpath(full, os.path.abspath(os.path.expanduser(project_path))).replace("\\", "/")
    blocked = path_blocked_for_edit(project_path, safe_rel)
    if blocked:
        return {"error": blocked, "error_kind": "invalid_input"}
    return apply_search_replace(
        full,
        safe_rel,
        old_string,
        new_string,
        create_diff_html_fn,
        replace_all=replace_all,
        params=params or SearchReplaceParams(),
        file_was_read_this_turn=file_was_read_this_turn,
    )

def _livecode_git_log(
    project_path: str,
    path: str | None = None,
    grep: str | None = None,
    max_count: int = 25,
    since: str | None = None,
) -> dict:
    root = os.path.abspath(os.path.expanduser(project_path))
    if not os.path.isdir(root):
        return {"error": "Invalid project path"}
    git_dir = os.path.join(root, ".git")
    if not os.path.isdir(git_dir):
        return {"error": "Not a git repository"}

    cap = min(max(int(max_count or 25), 1), 80)
    cmd = [
        "git", "--no-pager", "log",
        f"-n{cap}",
        "--date=short",
        "--pretty=format:%h %ad %an %s",
    ]
    if since:
        cmd.append(f"--since={since}")
    if grep:
        cmd.append(f"--grep={grep}")
        cmd.append("-i")

    path_args: list[str] = []
    if path:
        full, err = resolve_safe_path(project_path, path)
        if full is None:
            return {"error": err}
        safe_rel = os.path.relpath(full, root).replace("\\", "/")
        path_args = ["--", safe_rel]

    try:
        result = subprocess.run(
            cmd + path_args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return {"error": "git log timed out (>30s). Narrow with path or grep."}
    except OSError as e:
        return {"error": str(e)}

    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    commits = []
    for line in lines:
        m = re.match(r"^(\S+)\s+(\S+)\s+(.+?)\s+(.+)$", line)
        if m:
            commits.append({
                "hash": m.group(1),
                "date": m.group(2),
                "author": m.group(3),
                "subject": m.group(4),
            })
        else:
            commits.append({"raw": line})

    return {
        "success": True,
        "commit_count": len(commits),
        "commits": commits,
        "stderr": (result.stderr or "")[:500] if result.returncode != 0 and not commits else "",
    }

def _emit_command_stream(
    socketio,
    session_id: str | None,
    payload: dict,
    socket_id: str | None = None,
) -> None:
    if not socketio:
        return
    enriched = {**payload, "source": "livecode"}
    if session_id:
        enriched["session_id"] = session_id
    try:
        room = (socket_id or "").strip() or None
        if room:
            socketio.emit("lazie_command_stream", enriched, room=room)
        else:
            socketio.emit("lazie_command_stream", enriched)
    except Exception:
        pass

def _tokenize_shell_segment(segment: str) -> list[str]:
    text = (segment or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()

def _segment_is_git_log(segment: str) -> bool:
    tokens = _tokenize_shell_segment(segment)
    if not tokens:
        return False
    i = 0
    while i < len(tokens):
        if tokens[i].lower() != "git":
            i += 1
            continue
        j = i + 1
        while j < len(tokens):
            tok = tokens[j]
            low = tok.lower()
            if tok in ("-C", "-c") or low in ("--git-dir", "--work-tree"):
                j += 2 if j + 1 < len(tokens) else 1
                continue
            if low.startswith("--git-dir=") or low.startswith("--work-tree="):
                j += 1
                continue
            if tok.startswith("-"):
                j += 1
                continue
            return low == "log"
        return False
    return False

def _command_has_git_log(command: str) -> bool:
    if "git_log" in (command or "").lower():
        return False
    for seg in re.split(r"&&|\|\||;", command or ""):
        if _segment_is_git_log(seg):
            return True
    return False

COMMAND_OUTPUT_HEAD_CHARS = 8_000
COMMAND_ORPHAN_GRACE_S = 3.0
COMMAND_OUTPUT_TAIL_CHARS = 24_000


def head_tail_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 4
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"


class _OutputBuffer:

    def __init__(self, head_chars: int = COMMAND_OUTPUT_HEAD_CHARS, tail_chars: int = COMMAND_OUTPUT_TAIL_CHARS) -> None:
        self.head_chars = head_chars
        self.tail_chars = tail_chars
        self.head = ""
        self.tail: list[str] = []
        self.tail_len = 0
        self.total = 0

    def add(self, chunk: str) -> None:
        self.total += len(chunk)
        if len(self.head) < self.head_chars:
            room = self.head_chars - len(self.head)
            self.head += chunk[:room]
            chunk = chunk[room:]
            if not chunk:
                return
        self.tail.append(chunk)
        self.tail_len += len(chunk)
        while self.tail and self.tail_len - len(self.tail[0]) >= self.tail_chars:
            self.tail_len -= len(self.tail.pop(0))

    def text(self) -> str:
        tail = "".join(self.tail)
        kept = len(self.head) + len(tail)
        if kept >= self.total:
            return self.head + tail
        if len(tail) > self.tail_chars:
            tail = tail[-self.tail_chars:]
        omitted = self.total - len(self.head) - len(tail)
        return f"{self.head}\n... [{omitted} characters omitted] ...\n{tail}"

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + self.tail_len


def _livecode_run_command(
    project_path: str,
    command: str,
    execute_command_pty_fn,
    socketio,
    session_id: str | None = None,
    socket_id: str | None = None,
    *,
    use_streaming: bool = True,
    timeout_seconds: int | None = None,
    background: bool = False,
    cancel_check=None,
) -> dict:
    del execute_command_pty_fn
    root = os.path.abspath(os.path.expanduser(project_path))
    if not os.path.isdir(root):
        return {"error": "Invalid project path"}
    cmd = command.strip()
    if not cmd:
        return {"error": "Empty command"}
    blocked = ["rm -rf /", "mkfs", ":(){ :|:& };:"]
    low = cmd.lower()
    for b in blocked:
        if b in low:
            return {"error": "Command blocked for safety"}

    if _command_has_git_log(cmd):
        segments = re.split(r"(&&|\|\||;)", cmd)
        kept: list[str] = []
        dropped_any = False
        for seg in segments:
            if seg.strip() in ("&&", "||", ";"):
                kept.append(seg)
                continue
            if _segment_is_git_log(seg):
                dropped_any = True
                continue
            kept.append(seg)
        remaining = re.sub(r"^\s*(&&|\|\||;)\s*", "", "".join(kept))
        remaining = re.sub(r"\s*(&&|\|\||;)\s*$", "", remaining)
        remaining = re.sub(r"(&&|\|\||;)\s*(&&|\|\||;)", r"\1", remaining).strip()

        if not dropped_any or not remaining:
            return {
                "error": "Use the git_log tool for commit history instead of run_command git log.",
                "hint": "git_log supports path, grep, max_count, since filters.",
            }
        cmd = remaining
        dropped_git_log_hint = "Dropped a `git log` segment — call the git_log tool separately for commit history."
    else:
        dropped_git_log_hint = None

    if background:
        from livecode.bg_commands import start_background

        out = start_background(cmd, root, _subprocess_env(), session_id)
        if dropped_git_log_hint and isinstance(out, dict):
            out["hint"] = dropped_git_log_hint
        if isinstance(out, dict) and out.get("success"):
            out["hint"] = (
                out.get("hint")
                or "Running in the background. Use command_status to read its output and kill_command to stop it."
            )
        return out

    from livecode.bg_commands import clean_terminal_text, kill_process_tree
    from livecode.interjection import is_cancelled

    if not timeout_seconds:
        from livecode import agent_settings

        timeout_seconds = agent_settings.get("command_timeout_s")
    limit_s = max(1, min(int(timeout_seconds or COMMAND_TIMEOUT_DEFAULT_S), COMMAND_TIMEOUT_MAX_S))
    if cancel_check is None and session_id:
        command_started = time.monotonic()
        cancel_check = lambda: is_cancelled(session_id, since=command_started)
    stream = use_streaming and socketio is not None
    if stream:
        _emit_command_stream(socketio, session_id, {
            "status": "start",
            "command": cmd,
            "command_name": cmd.split()[0] if cmd.split() else cmd[:30],
            "command_index": 1,
            "total_commands": 1,
        }, socket_id=socket_id)
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            bufsize=1,
            env=_subprocess_env(),
            start_new_session=os.name == "posix",
        )
    except OSError as e:
        return {"error": str(e)}

    stop_reason: list[str] = []
    reader_done = threading.Event()

    def _watch() -> None:
        deadline = time.monotonic() + limit_s
        exited_at = None
        while not reader_done.wait(0.2):
            now = time.monotonic()
            if cancel_check is not None and cancel_check():
                stop_reason.append("cancelled")
                kill_process_tree(proc)
                return
            if now >= deadline:
                stop_reason.append("timeout")
                kill_process_tree(proc)
                return
            if proc.poll() is not None:
                exited_at = exited_at or now
                if now - exited_at >= COMMAND_ORPHAN_GRACE_S:
                    stop_reason.append("orphans")
                    kill_process_tree(proc, grace_s=1.0)
                    return

    threading.Thread(target=_watch, name="livecode-command-watch", daemon=True).start()
    buffer = _OutputBuffer()
    assert proc.stdout is not None
    for line in proc.stdout:
        buffer.add(line)
        if stream:
            _emit_command_stream(socketio, session_id, {
                "status": "stream",
                "command": cmd,
                "output": line,
                "command_index": 1,
                "total_commands": 1,
                "was_streaming": True,
            }, socket_id=socket_id)
    reader_done.set()
    proc.wait()
    exit_code = proc.returncode if proc.returncode is not None else 0
    if exit_code < 0:
        exit_code = 128 - exit_code
    if "timeout" in stop_reason:
        exit_code = 124
        buffer.add(f"\n[Command timed out after {limit_s}s and was stopped]")
    elif "cancelled" in stop_reason:
        exit_code = 130
        buffer.add("\n[Stopped by the user]")
    elif "orphans" in stop_reason:
        buffer.add(
            "\n[The command exited but left processes running that held its output open; they "
            "were stopped. Start long-running processes with background=true.]"
        )

    output = clean_terminal_text(buffer.text())
    if stream:
        _emit_command_stream(socketio, session_id, {
            "status": "output",
            "command": cmd,
            "output": output,
            "exit_code": exit_code,
            "command_index": 1,
            "total_commands": 1,
            "was_streaming": True,
        }, socket_id=socket_id)
    out = {
        "success": exit_code == 0,
        "command": cmd,
        "exit_code": exit_code,
        "output": output,
    }
    if buffer.truncated:
        out["output_truncated"] = True
        out["total_output_chars"] = buffer.total
    if "timeout" in stop_reason:
        out["timed_out"] = True
        out["hint"] = (
            f"The command ran longer than {limit_s}s. If it is a server or watcher, start it with "
            "background=true; for a slow build, pass a larger timeout_seconds."
        )
    if "cancelled" in stop_reason:
        out["cancelled"] = True
    if dropped_git_log_hint:
        out["hint"] = dropped_git_log_hint
    return out

def _livecode_call_mcp_tool(
    project_path: str,
    server_name: str,
    tool_name: str,
    arguments: dict,
    *,
    enabled_servers: list[str] | tuple[str, ...] | None = None,
    active_workspace=None,
) -> dict:
    server = str(server_name or "").strip()
    tool = str(tool_name or "").strip()
    if not server or not tool:
        return {"ok": False, "error": "server_name and tool_name are required"}
    if enabled_servers is not None and server not in {str(item) for item in enabled_servers}:
        return {
            "ok": False,
            "blocked": True,
            "reason_code": "disabled_server",
            "error": f"MCP server is not enabled for this turn: {server}",
        }
    try:
        from livecode import mcp_bridge

        available_servers = {cfg.name for cfg in mcp_bridge.load_mcp_servers(
            project_path,
            _workspace_payload_from_active(active_workspace),
        )}
        if server not in available_servers:
            return {
                "ok": False,
                "blocked": True,
                "reason_code": "unknown_server",
                "error": f"MCP server is not configured: {server}",
                "available_servers": sorted(available_servers),
            }
        return mcp_bridge.call_mcp_tool(
            project_path,
            server,
            tool,
            arguments or {},
            workspace_payload=_workspace_payload_from_active(active_workspace),
        )
    except Exception as exc:
        return {"ok": False, "error": f"MCP dispatch failed: {exc}", "server": server, "tool": tool}

def _workspace_result_path(workspace, folder, rel_path: str) -> str:
    clean = str(rel_path or "").replace("\\", "/").strip("/")
    if len(workspace.folders) <= 1:
        return clean
    return workspace_display_path(folder, clean)


def _workspace_selector_matches(workspace, selector: str) -> list:
    key = workspace_name_key(selector)
    name_matches = [folder for folder in workspace.folders if workspace_name_key(folder.name) == key]
    if name_matches:
        return name_matches
    return [folder for folder in workspace.folders if workspace_name_key(os.path.basename(folder.path)) == key]


def _selected_workspace_root(project_path: str, args: dict, workspace=None, path_key: str = "file_path") -> dict:
    ws = workspace or workspace_roots(project_path)
    selector = str((args or {}).get("workspace") or "").strip()
    raw_path = str((args or {}).get(path_key) or "").strip()
    if not selector:
        return resolve_workspace_path(project_path, raw_path, ws)
    matches = _workspace_selector_matches(ws, selector)
    if not matches:
        return {"error": f"Unknown workspace folder: {selector}"}
    if len(matches) > 1:
        names = ", ".join(str(folder.name or os.path.basename(folder.path)) for folder in matches)
        return {"error": f"Workspace folder name is ambiguous: {selector} matches {names}"}
    folder = matches[0]
    full, err = resolve_safe_path(folder.path, raw_path)
    if full is None:
        return {"error": err}
    rel = os.path.relpath(full, folder.path).replace("\\", "/")
    rel = "" if rel == "." else rel
    return {"folder": folder, "root": folder.path, "rel": rel, "full": full, "display_path": workspace_display_path(folder, rel)}


def edit_target_path(project_path: str, args: dict, workspace=None) -> str | None:
    selected = _selected_workspace_root(project_path, args, workspace)
    if selected.get("error"):
        return None
    full = selected.get("full")
    if not full:
        full, _err = resolve_safe_path(selected["root"], selected.get("rel") or "")
    return full or None


def _path_selects_workspace(path: str, workspace) -> bool:
    raw = str(path or "").strip().replace("\\", "/")
    raw = raw[2:] if raw.startswith("./") else raw
    first = raw.lstrip("/").split("/", 1)[0]
    first_key = workspace_name_key(first)
    if not first or len(workspace.folders) <= 1:
        return False
    for folder in workspace.folders:
        aliases = workspace_folder_aliases(folder)
        if first in aliases or first_key in aliases:
            return True
    return False


def _workspace_path_scope(project_path: str, args: dict, workspace, path_key: str = "file_path") -> dict:
    selector = str((args or {}).get("workspace") or "").strip()
    raw_path = str((args or {}).get(path_key) or "").strip()
    selected = _selected_workspace_root(project_path, args, workspace, path_key=path_key)
    return {
        "selected": selected,
        "single_root": bool(selector or _path_selects_workspace(raw_path, workspace)),
    }


def _prefix_workspace_items(items: list, workspace, folder) -> list:
    if len(workspace.folders) <= 1:
        return items
    out = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        next_item = dict(item)
        for key in ("file", "path"):
            if next_item.get(key):
                next_item[key] = workspace_display_path(folder, str(next_item[key]))
        out.append(next_item)
    return out


def _workspace_match_rank(item: dict, query: str = "") -> tuple[int, int, int, str]:
    path = str(item.get("path") or item.get("file") or "").lower()
    name = str(item.get("name") or "").lower()
    needle = str(query or "").strip().lower()
    base = os.path.basename(path)
    exact = 0 if needle and (base == needle or name == needle) else 1
    contains = 0 if needle and (needle in base or needle in name or needle in path) else 1
    depth = path.count("/")
    return exact, contains, depth, path


def _rank_workspace_matches(items: list, query: str = "") -> list:
    if len(items) <= 1:
        return items
    return sorted(items, key=lambda item: _workspace_match_rank(item, query) if isinstance(item, dict) else (1, 1, 999, str(item)))


def _workspace_codebase_indexes(project_path: str, workspace=None):
    ws = workspace or workspace_roots(project_path)
    for folder in ws.folders:
        yield ws, folder, get_codebase_index(folder.path)


def _workspace_payload_from_active(workspace) -> dict:
    return {
        "path": getattr(workspace, "config_path", "") or "",
        "folders": [
            {"name": folder.name, "path": folder.path}
            for folder in getattr(workspace, "folders", ())
        ],
        "settings": dict(getattr(workspace, "settings", {}) or {}),
        "mcpServers": dict(getattr(workspace, "mcp_servers", {}) or {}),
    }


def _normalize_grep_scope(project_path: str, args: dict) -> dict:
    out = dict(args or {})
    directory = str(out.get("directory") or "").strip()
    if not directory:
        return out

    scoped_path = _resolve_flexible_project_path(project_path, directory)
    if not scoped_path:
        return out

    basename = os.path.basename(scoped_path.rstrip(os.sep))
    parent = os.path.dirname(scoped_path.rstrip(os.sep))
    looks_like_file = bool(os.path.splitext(basename)[1])
    if os.path.isfile(scoped_path) or (looks_like_file and os.path.isdir(parent)):
        rel_parent = _rel_path_or_empty(project_path, parent)
        out["directory"] = "" if rel_parent in ("", ".") else rel_parent
        if not str(out.get("glob_filter") or "").strip():
            out["glob_filter"] = basename
        out["_normalized_file_scope"] = True
    elif not os.path.isdir(scoped_path):
        parent_rel = _rel_path_or_empty(project_path, parent)
        if looks_like_file and parent_rel:
            out["directory"] = parent_rel
            if not str(out.get("glob_filter") or "").strip():
                out["glob_filter"] = basename
            out["_normalized_file_scope"] = True
    return out

def _unread_existing_file(selected: dict, files_read_this_turn: set[str] | None) -> dict | None:
    if files_read_this_turn is None:
        return None
    display = selected.get("display_path") or selected.get("rel") or ""
    if display in files_read_this_turn or (selected.get("rel") or "") in files_read_this_turn:
        return None
    target, _err = resolve_safe_path(selected["root"], selected.get("rel") or "")
    try:
        if not target or not os.path.isfile(target) or os.path.getsize(target) == 0:
            return None
    except OSError:
        return None
    return {
        "error": (
            f"{display} already exists and has not been read this turn. Read it with read_repo_file "
            "before overwriting it, or use edit_file/multi_edit for targeted changes."
        ),
        "error_kind": "not_read",
    }


def _mark_known_content(selected: dict, files_read_this_turn: set[str] | None, out) -> None:
    if files_read_this_turn is None or not isinstance(out, dict) or not out.get("success"):
        return
    display = selected.get("display_path") or selected.get("rel") or ""
    if display:
        files_read_this_turn.add(display)


def _edit_baseline_before(
    state_path: str,
    session_id: str | None,
    selected: dict,
    checkpoint: dict | None = None,
) -> tuple[str | None, dict | None]:
    if not session_id:
        return None, None
    try:
        target, _err = resolve_safe_path(selected["root"], selected.get("rel") or "")
        if not target:
            return None, None
        needs_turn_copy = bool(checkpoint and checkpoint.get("turn_id"))
        if has_baseline(state_path, session_id, target) and not needs_turn_copy:
            return None, None
        return target, snapshot_file(target)
    except (OSError, ValueError, KeyError):
        return None, None

def _edit_baseline_after(
    state_path: str,
    session_id: str | None,
    target: str | None,
    before: dict | None,
    out,
    checkpoint: dict | None = None,
) -> None:
    if not target or before is None or not isinstance(out, dict):
        return
    if out.get("error") or out.get("no_changes") or not out.get("success"):
        return
    try:
        record_baseline(state_path, session_id, target, before)
        if checkpoint:
            record_turn_baseline(
                state_path,
                session_id,
                checkpoint.get("turn_id"),
                checkpoint.get("user_index"),
                target,
                before,
            )
    except (OSError, ValueError):
        pass

def dispatch_tool(
    project_path: str,
    name: str,
    args: dict,
    *,
    repo_grep_fn,
    repo_read_fn,
    repo_list_fn,
    repo_ast_fn,
    create_diff_html_fn,
    execute_command_pty_fn,
    socketio=None,
    session_id: str | None = None,
    socket_id: str | None = None,
    subagent_runner=None,
    files_read_this_turn: set[str] | None = None,
    workspace=None,
    mcp_binding=None,
    state_path: str | None = None,
    checkpoint: dict | None = None,
    cancel_check=None,
    browser_agent: dict | None = None,
) -> dict:
    active_workspace = workspace or workspace_roots(project_path)
    durable_path = state_path or project_path
    if name == "find_symbol":
        limit = min(int(args.get("max_results") or 30), 50)
        symbols = []
        for ws, folder, idx in _workspace_codebase_indexes(project_path, active_workspace):
            chunk = idx.find_symbol(args.get("name", ""), kind=args.get("kind"), limit=limit)
            symbols.extend(_prefix_workspace_items(chunk, ws, folder))
        symbols = _rank_workspace_matches(symbols, args.get("name", ""))[:limit]
        return {"success": True, "symbol_count": len(symbols), "symbols": symbols}
    if name == "find_references":
        limit = min(int(args.get("max_results") or 40), 60)
        refs = []
        for ws, folder, idx in _workspace_codebase_indexes(project_path, active_workspace):
            chunk = idx.find_references(args.get("name", ""), limit=limit)
            refs.extend(_prefix_workspace_items(chunk, ws, folder))
        refs = _rank_workspace_matches(refs, args.get("name", ""))[:limit]
        return {"success": True, "reference_count": len(refs), "references": refs}
    if name == "list_symbols":
        limit = min(int(args.get("max_results") or 80), 100)
        symbols = []
        scope = _workspace_path_scope(project_path, args, active_workspace, path_key="path")
        selected = scope["selected"]
        if scope["single_root"] and selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        for ws, folder, idx in _workspace_codebase_indexes(project_path, active_workspace):
            if scope["single_root"] and selected.get("folder") != folder:
                continue
            path = selected.get("rel") if scope["single_root"] and not selected.get("error") else str(args.get("path") or "")
            chunk = idx.list_symbols(str(path or ""), limit=limit)
            symbols.extend(_prefix_workspace_items(chunk, ws, folder))
        symbols = _rank_workspace_matches(symbols)[:limit]
        return {"success": True, "symbol_count": len(symbols), "symbols": symbols}
    if name == "call_mcp_tool":
        return _livecode_call_mcp_tool(
            project_path,
            args.get("server_name"),
            args.get("tool_name"),
            args.get("arguments") or {},
            active_workspace=active_workspace,
        )
    try:
        from livecode import mcp_bridge
        if mcp_bridge.is_mcp_tool_name(name):
            if mcp_binding is None or getattr(mcp_binding, "encoded_name", name) != name:
                return {
                    "ok": False,
                    "blocked": True,
                    "reason_code": "tool_not_offered",
                    "error": "MCP tool was not offered for this turn",
                }
            server_name = str(getattr(mcp_binding, "server_name", ""))
            tool_name = str(getattr(mcp_binding, "tool_name", ""))
            args.pop("_permission_approved", None)
            return mcp_bridge.call_mcp_tool(project_path, server_name, tool_name, args, workspace_payload=_workspace_payload_from_active(active_workspace))
    except Exception as exc:
        return {"ok": False, "error": f"MCP dispatch failed: {exc}"}
    if name == "update_memory":
        note = args.get("note", "")
        memory = append_project_memory(durable_path, note)
        return {"success": True, "memory_chars": len(memory)}
    if name == "memory_search":
        query = str(args.get("query") or "")
        max_results = min(int(args.get("max_results") or 6), 20)
        min_score = float(args.get("min_score") if args.get("min_score") is not None else 0.0)
        hits = search_memory(
            durable_path,
            query,
            max_results=max_results,
            min_score=min_score,
        )
        return {
            "success": True,
            "result_count": len(hits),
            "results": [
                {
                    "path": h.path,
                    "start_line": h.start_line,
                    "end_line": h.end_line,
                    "score": round(h.score, 4),
                    "source": h.source,
                    "snippet": h.snippet[:500],
                }
                for h in hits
            ],
        }
    if name == "memory_get":
        return read_memory_file(
            durable_path,
            str(args.get("path") or ""),
            from_line=int(args.get("from_line") or 0),
            lines=int(args["lines"]) if args.get("lines") is not None else None,
        )
    if name == "spawn_subagent":
        if not subagent_runner:
            return {
                "error": (
                    "Subagent runner not configured; do not retry this tool in the same turn. "
                    "Continue directly or explain the blocker."
                ),
                "retryable": False,
            }
        return subagent_runner(project_path, args, session_id)
    if name == "glob_files":
        limit = min(int(args.get("max_results") or 100), 100)
        results = []
        scope = _workspace_path_scope(project_path, args, active_workspace, path_key="path")
        selected = scope["selected"]
        if scope["single_root"] and selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        for folder in active_workspace.folders:
            if scope["single_root"] and selected.get("folder") != folder:
                continue
            path = selected.get("rel") if scope["single_root"] and not selected.get("error") else str(args.get("path") or "")
            out = glob_files(folder.path, args.get("pattern", ""), path=str(path or ""), max_results=limit)
            files = out.get("files") if isinstance(out, dict) else []
            for item in files or []:
                results.append(workspace_display_path(folder, str(item)) if len(active_workspace.folders) > 1 else item)
        ranked = _rank_workspace_matches([{"path": item} if not isinstance(item, dict) else item for item in results], args.get("pattern", ""))
        files = [item.get("path") if isinstance(item, dict) and set(item.keys()) == {"path"} else item for item in ranked]
        return {"success": True, "pattern": args.get("pattern", ""), "file_count": min(len(files), limit), "files": files[:limit]}
    if name == "find_files":
        limit = min(int(args.get("max_results") or 50), 100)
        results = []
        scope = _workspace_path_scope(project_path, args, active_workspace, path_key="path_prefix")
        selected = scope["selected"]
        if scope["single_root"] and selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        for folder in active_workspace.folders:
            if scope["single_root"] and selected.get("folder") != folder:
                continue
            path_prefix = selected.get("rel") if scope["single_root"] and not selected.get("error") else str(args.get("path_prefix") or "")
            out = search_file_manifest(
                folder.path,
                args.get("query", ""),
                ext=str(args.get("ext") or ""),
                path_prefix=str(path_prefix or ""),
                max_results=limit,
            )
            files = out.get("files") if isinstance(out, dict) else []
            for item in files or []:
                if isinstance(item, dict) and item.get("path"):
                    next_item = dict(item)
                    next_item["path"] = _workspace_result_path(active_workspace, folder, str(item["path"]))
                    results.append(next_item)
                else:
                    results.append(workspace_display_path(folder, str(item)) if len(active_workspace.folders) > 1 else item)
        ranked = _rank_workspace_matches(results, args.get("query", ""))
        return {"success": True, "query": args.get("query", ""), "file_count": min(len(ranked), limit), "files": ranked[:limit]}
    if name == "grep_repo":
        output_mode = str(args.get("output_mode") or "content").strip().lower()
        if output_mode not in ("content", "files", "count"):
            output_mode = "content"
        if output_mode == "content":
            limit = min(_positive_int(args.get("max_results")) or 100, 400)
        else:
            limit = 5000
        scope = _workspace_path_scope(project_path, args, active_workspace, path_key="directory")
        selected = scope["selected"]
        if scope["single_root"] and selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        search_folders = []
        for folder in active_workspace.folders:
            if scope["single_root"] and selected.get("folder") != folder:
                continue
            directory = selected.get("rel") if scope["single_root"] and not selected.get("error") else str(args.get("directory") or "")
            search_folders.append((folder, folder.path, str(directory or "")))
        matches = []
        for folder, root, directory in search_folders:
            grep_args = _normalize_grep_scope(root, {**args, "directory": directory})
            out = repo_grep_fn(
                root,
                grep_args.get("pattern", ""),
                grep_args.get("glob_filter"),
                limit,
                directory=str(grep_args.get("directory") or ""),
            )
            if isinstance(out, dict) and out.get("error") and len(search_folders) == 1:
                return out
            chunk = out.get("matches") if isinstance(out, dict) else []
            matches.extend(_prefix_workspace_items(chunk or [], active_workspace, folder))
        if output_mode != "content":
            files = _grep_file_summary(matches)
            out = {
                "success": True,
                "pattern": args.get("pattern", ""),
                "output_mode": output_mode,
                "match_count": len(matches),
                "file_count": len(files),
            }
            if output_mode == "files":
                out["files"] = files[:1000]
            if len(matches) >= limit:
                out["capped"] = True
                out["note"] = f"Stopped counting at {limit} matches; narrow the pattern or directory for exact totals."
            return out
        matches = _rank_workspace_matches(matches, args.get("pattern", ""))[:limit]
        return {"success": True, "pattern": args.get("pattern", ""), "match_count": len(matches), "matches": matches}
    if name == "read_repo_file":
        selected = _selected_workspace_root(project_path, args, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        try:
            out = _livecode_read_file(
                selected["root"],
                selected.get("rel") or "",
                args.get("start_line"),
                args.get("end_line"),
                display_path=selected.get("display_path") or selected.get("rel") or "",
            )
        except (OSError, ValueError):
            out = repo_read_fn(selected["root"], selected.get("rel") or "", args.get("start_line"), args.get("end_line"))
        if isinstance(out, dict) and len(active_workspace.folders) > 1:
            out = dict(out)
            out["file"] = selected["display_path"]
            out["workspace"] = selected["folder"].name
        if files_read_this_turn is not None and selected.get("display_path"):
            files_read_this_turn.add(selected["display_path"])
        return out
    if name == "list_repo_dir":
        directory = str(args.get("directory") or "").strip()
        if not directory or directory in (".", "/"):
            from livecode.workspace import build_project_layout_tree
            if len(active_workspace.folders) > 1:
                return {
                    "success": True,
                    "directory": "/",
                    "mode": "workspace_tree",
                    "layout_tree": [{"name": folder.name, "type": "dir", "children": build_project_layout_tree(folder.path)} for folder in active_workspace.folders],
                }
            tree = build_project_layout_tree(project_path)
            return {
                "success": True,
                "directory": "/",
                "mode": "tree",
                "layout_tree": tree,
            }
        selected = _selected_workspace_root(project_path, {"file_path": directory, "workspace": args.get("workspace")}, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        out = repo_list_fn(selected["root"], selected.get("rel") or "")
        if isinstance(out, dict) and len(active_workspace.folders) > 1:
            out = dict(out)
            out["directory"] = selected["display_path"]
            out["workspace"] = selected["folder"].name
        return out
    if name == "git_log":
        selected = _selected_workspace_root(project_path, {"file_path": args.get("path") or "", "workspace": args.get("workspace")}, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        return _livecode_git_log(
            selected["root"],
            path=selected.get("rel") or None,
            grep=args.get("grep"),
            max_count=args.get("max_count", 25),
            since=args.get("since"),
        )
    if name == "ast_symbols":
        selected = _selected_workspace_root(project_path, args, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        out = repo_ast_fn(selected["root"], selected.get("rel") or "")
        if isinstance(out, dict) and len(active_workspace.folders) > 1:
            out = dict(out)
            out["file_path"] = selected["display_path"]
            out["workspace"] = selected["folder"].name
            out["workspace_root"] = selected["root"]
            out["relative_path"] = selected.get("rel") or ""
            out["display_path"] = selected["display_path"]
        return out
    if name == "write_file":
        selected = _selected_workspace_root(project_path, args, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        unread = _unread_existing_file(selected, files_read_this_turn)
        if unread:
            return unread
        baseline_target, baseline = _edit_baseline_before(durable_path, session_id, selected, checkpoint)
        out = _livecode_write_file(selected["root"], selected.get("rel") or "", args.get("content", ""), create_diff_html_fn)
        _edit_baseline_after(durable_path, session_id, baseline_target, baseline, out, checkpoint)
        _mark_known_content(selected, files_read_this_turn, out)
        if isinstance(out, dict) and len(active_workspace.folders) > 1:
            out = dict(out)
            out["file_path"] = selected["display_path"]
            out["workspace"] = selected["folder"].name
            out["workspace_root"] = selected["root"]
            out["relative_path"] = selected.get("rel") or ""
            out["display_path"] = selected["display_path"]
        return out
    if name == "edit_file":
        selected = _selected_workspace_root(project_path, args, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        was_read = None
        if files_read_this_turn is not None:
            was_read = selected["display_path"] in files_read_this_turn or selected.get("rel") in files_read_this_turn
        baseline_target, baseline = _edit_baseline_before(durable_path, session_id, selected, checkpoint)
        out = _livecode_edit_file(
            selected["root"],
            selected.get("rel") or "",
            args.get("old_string", ""),
            args.get("new_string", ""),
            create_diff_html_fn,
            replace_all=bool(args.get("replace_all")),
            file_was_read_this_turn=was_read,
        )
        _edit_baseline_after(durable_path, session_id, baseline_target, baseline, out, checkpoint)
        _mark_known_content(selected, files_read_this_turn, out)
        if isinstance(out, dict) and len(active_workspace.folders) > 1:
            out = dict(out)
            out["file_path"] = selected["display_path"]
            out["workspace"] = selected["folder"].name
            out["workspace_root"] = selected["root"]
            out["relative_path"] = selected.get("rel") or ""
            out["display_path"] = selected["display_path"]
        return out
    if name == "multi_edit":
        selected = _selected_workspace_root(project_path, args, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return {"error": "multi_edit needs a non-empty edits list of {old_string, new_string}.", "error_kind": "invalid_input"}
        was_read = None
        if files_read_this_turn is not None:
            was_read = selected["display_path"] in files_read_this_turn or selected.get("rel") in files_read_this_turn
        baseline_target, baseline = _edit_baseline_before(durable_path, session_id, selected, checkpoint)
        full, err = resolve_safe_path(selected["root"], selected.get("rel") or "")
        if full is None:
            return {"error": err, "error_kind": "invalid_input"}
        safe_rel = os.path.relpath(full, os.path.abspath(os.path.expanduser(selected["root"]))).replace("\\", "/")
        blocked = path_blocked_for_edit(selected["root"], safe_rel)
        if blocked:
            return {"error": blocked, "error_kind": "invalid_input"}
        out = apply_multi_search_replace(
            full,
            safe_rel,
            edits,
            create_diff_html_fn,
            file_was_read_this_turn=was_read,
        )
        _edit_baseline_after(durable_path, session_id, baseline_target, baseline, out, checkpoint)
        _mark_known_content(selected, files_read_this_turn, out)
        if isinstance(out, dict) and len(active_workspace.folders) > 1:
            out = dict(out)
            out["file_path"] = selected["display_path"]
            out["workspace"] = selected["folder"].name
            out["workspace_root"] = selected["root"]
            out["relative_path"] = selected.get("rel") or ""
            out["display_path"] = selected["display_path"]
        return out
    if name == "command_status":
        from livecode.bg_commands import wait_for

        wait = args.get("wait_seconds")
        try:
            wait_s = float(wait) if wait is not None else 0.0
        except (TypeError, ValueError):
            wait_s = 0.0
        return wait_for(str(args.get("command_id") or "").strip(), wait_s, str(args.get("until") or ""))
    if name == "restart_command":
        from livecode.bg_commands import restart as restart_background

        selected = _selected_workspace_root(project_path, {"file_path": "", "workspace": args.get("workspace")}, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        try:
            wait_s = float(args.get("wait_seconds") or 0)
        except (TypeError, ValueError):
            wait_s = 0.0
        try:
            port = int(args.get("port") or 0)
        except (TypeError, ValueError):
            return {"error": "port is a number.", "error_kind": "invalid_input"}
        command = str(args.get("command") or "").strip()
        return restart_background(
            str(args.get("command_id") or "").strip(),
            command=command,
            cwd=selected["root"],
            env=_subprocess_env(),
            session_id=session_id,
            port=port,
            wait_seconds=wait_s,
            until=str(args.get("until") or ""),
        )
    if name == "kill_command":
        from livecode.bg_commands import kill as kill_background

        return kill_background(str(args.get("command_id") or "").strip())
    if name == "run_command":
        selected = _selected_workspace_root(project_path, {"file_path": "", "workspace": args.get("workspace")}, active_workspace)
        if selected.get("error"):
            return {"error": selected.get("error"), "error_kind": "invalid_input"}
        out = _livecode_run_command(
            selected["root"],
            args.get("command", ""),
            execute_command_pty_fn,
            socketio,
            session_id,
            socket_id=socket_id,
            timeout_seconds=args.get("timeout_seconds"),
            background=bool(args.get("background")),
            cancel_check=cancel_check,
        )
        if isinstance(out, dict) and len(active_workspace.folders) > 1:
            out = dict(out)
            out["workspace"] = selected["folder"].name
            out["cwd"] = selected["root"]
            if "hint" not in out:
                out["hint"] = "Command ran in the selected workspace root shown by `cwd`. Use the `workspace` argument to target a different folder."
        return out
    if name == "web_search":
        from livecode.web_tools import web_search as _web_search

        domains = args.get("allowed_domains")
        if isinstance(domains, str):
            domains = [domains]
        return _web_search(
            args.get("query", ""),
            allowed_domains=domains if isinstance(domains, list) else None,
            max_results=min(int(args.get("max_results") or 8), 12),
        )
    if name == "web_fetch":
        from livecode.web_tools import web_fetch as _web_fetch

        return _web_fetch(
            args.get("url", ""),
            max_chars=min(int(args.get("max_chars") or 12000), 50000),
        )
    if name == "browser":
        from livecode import browser as _browser

        def _project_image(rel: str) -> str | None:
            selected = _selected_workspace_root(project_path, {"file_path": rel}, active_workspace)
            return None if selected.get("error") else selected.get("full")

        return _browser.agent_action(durable_path, args, session_id=session_id or "", resolve_path=_project_image, agent=browser_agent)
    if name == "create_plan":
        return _livecode_create_plan(project_path, args, session_id)
    if name == "todo_write":
        return _livecode_todo_write(durable_path, session_id, args)
    if name == "update_goal":
        return _livecode_update_goal(durable_path, session_id, args)
    if name in (
        "lsp_definition",
        "lsp_references",
        "lsp_hover",
        "lsp_diagnostics",
        "lsp_document_symbols",
        "lsp_completion",
        "lsp_rename_preview",
    ):
        return _livecode_lsp_query(project_path, name, args, active_workspace)
    if name == "attempt_completion":
        return {"success": True, "completed": True, "result": args.get("result", "")}
    return {"error": f"Unknown tool: {name}"}


_TODO_STATUSES = ("pending", "in_progress", "completed", "cancelled")


def _todo_summary_line(todos: list[dict]) -> str:
    from livecode.reminders import todo_pending_count

    done = sum(1 for t in todos if str((t or {}).get("status")).lower() == "completed")
    pend, prog = todo_pending_count(todos)
    return f"{done} done · {prog} in progress · {pend} pending"


def _livecode_todo_write(project_path: str, session_id: str | None, args: dict) -> dict:
    from livecode.reminders import load_todo_state, save_todo_state

    if not session_id:
        return {"error": "todo_write needs a session", "error_kind": "unavailable"}
    incoming = args.get("todos")
    if not isinstance(incoming, list) or not incoming:
        return {"error": "todos must be a non-empty array", "error_kind": "invalid_input"}
    merge = args.get("merge")
    merge = True if merge is None else bool(merge)

    cleaned: list[dict] = []
    seen: set[str] = set()
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        tid = str(raw.get("id") or raw.get("content") or "").strip()[:80]
        if not tid or tid in seen:
            if tid in seen:
                return {"error": f"duplicate todo id: {tid}", "error_kind": "invalid_input"}
            continue
        seen.add(tid)
        status = str(raw.get("status") or "pending").lower()
        if status not in _TODO_STATUSES:
            status = "pending"
        cleaned.append({
            "id": tid,
            "content": str(raw.get("content") or "").strip()[:400],
            "status": status,
        })

    if merge:
        current = {t["id"]: t for t in load_todo_state(project_path, session_id)}
        for item in cleaned:
            item["content"] = item["content"] or str((current.get(item["id"]) or {}).get("content") or item["id"])
            current[item["id"]] = item
        final = list(current.values())
    else:
        for item in cleaned:
            item["content"] = item["content"] or item["id"]
        final = cleaned

    save_todo_state(project_path, session_id, final)
    return {"success": True, "todos": final, "summary": _todo_summary_line(final)}


def _livecode_update_goal(project_path: str, session_id: str | None, args: dict) -> dict:
    from livecode.reminders import load_goal_state, save_goal_state

    if not session_id:
        return {"error": "update_goal needs a session", "error_kind": "unavailable"}
    goal = load_goal_state(project_path, session_id) or {}
    if not goal.get("text"):
        goal["text"] = str(args.get("message") or "").strip()[:400] or "(unstated)"
    goal.setdefault("updates", [])
    message = str(args.get("message") or "").strip()
    blocked = str(args.get("blocked_reason") or "").strip()
    completed = bool(args.get("completed"))

    if completed:
        goal["status"] = "completed"
        if message:
            goal["updates"].append({"t": time.time(), "message": message})
        status = "completed"
    elif blocked:
        goal["status"] = "blocked"
        goal["attempts_failed"] = int(goal.get("attempts_failed") or 0) + 1
        goal["updates"].append({"t": time.time(), "blocked": blocked})
        status = "blocked"
    else:
        goal["status"] = "active"
        if message:
            goal["updates"].append({"t": time.time(), "message": message})
        status = "active"
    goal["updates"] = goal["updates"][-20:]
    save_goal_state(project_path, session_id, goal)
    return {
        "success": True,
        "status": status,
        "summary": {"completed": "Goal marked complete", "blocked": f"Goal blocked: {blocked[:160]}"}.get(
            status, message[:160] or "Progress logged"
        ),
    }


def _lsp_loc_list(result) -> list[dict]:
    if not result:
        return []
    arr = result if isinstance(result, list) else [result]
    out: list[dict] = []
    for loc in arr:
        if not isinstance(loc, dict):
            continue
        uri = loc.get("uri") or loc.get("targetUri") or ""
        rng = loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        path = uri[7:] if uri.startswith("file://") else uri
        out.append({
            "file": path,
            "line": int(start.get("line", 0)) + 1,
            "character": int(start.get("character", 0)) + 1,
            "end_line": int(end.get("line", start.get("line", 0))) + 1,
        })
    return out


def _lsp_symbol_items(result, parent: str = "") -> list[dict]:
    if not result:
        return []
    out = []
    for item in result if isinstance(result, list) else [result]:
        if not isinstance(item, dict):
            continue
        rng = item.get("selectionRange") or item.get("range") or {}
        start = (rng.get("start") or {}) if isinstance(rng, dict) else {}
        end = (rng.get("end") or {}) if isinstance(rng, dict) else {}
        name = str(item.get("name") or "")[:160]
        entry = {
            "name": name,
            "kind": item.get("kind"),
            "line": int(start.get("line", 0)) + 1,
            "character": int(start.get("character", 0)) + 1,
            "end_line": int(end.get("line", start.get("line", 0))) + 1,
        }
        detail = str(item.get("detail") or "").strip()
        if detail:
            entry["detail"] = detail[:240]
        if parent:
            entry["container_name"] = parent
        out.append(entry)
        children = item.get("children") or []
        if children:
            out.extend(_lsp_symbol_items(children, name or parent))
    return out


def _lsp_completion_items(result, max_results: int) -> list[dict]:
    items = result.get("items") if isinstance(result, dict) else result
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:max_results]:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or "").strip()
        documentation = item.get("documentation")
        if isinstance(documentation, dict):
            documentation = documentation.get("value") or ""
        entry = {
            "label": str(item.get("label") or "")[:160],
            "kind": item.get("kind"),
        }
        if detail:
            entry["detail"] = detail[:240]
        if documentation:
            entry["documentation"] = str(documentation)[:500]
        insert_text = item.get("insertText")
        text_edit = item.get("textEdit")
        if not insert_text and isinstance(text_edit, dict):
            insert_text = text_edit.get("newText")
        if insert_text and str(insert_text) != entry["label"]:
            entry["insert_text"] = str(insert_text)[:240]
        out.append(entry)
    return out


def _lsp_workspace_edit_preview(result) -> dict:
    changes = result.get("changes") if isinstance(result, dict) else None
    document_changes = result.get("documentChanges") if isinstance(result, dict) else None
    files = []
    edit_count = 0
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            path = uri[7:] if str(uri).startswith("file://") else str(uri)
            if not isinstance(edits, list):
                continue
            edit_count += len(edits)
            first = edits[0] if edits else {}
            rng = first.get("range") if isinstance(first, dict) else {}
            start = (rng or {}).get("start") or {}
            files.append({"file": path, "edit_count": len(edits), "first_line": int(start.get("line", 0)) + 1})
    elif isinstance(document_changes, list):
        for change in document_changes:
            if not isinstance(change, dict):
                continue
            text_document = change.get("textDocument") or {}
            edits = change.get("edits") or []
            uri = text_document.get("uri") or change.get("uri") or ""
            path = uri[7:] if str(uri).startswith("file://") else str(uri)
            edit_count += len(edits) if isinstance(edits, list) else 0
            files.append({"file": path, "edit_count": len(edits) if isinstance(edits, list) else 0})
    return {"file_count": len(files), "edit_count": edit_count, "files": files[:40]}


def _livecode_lsp_query(project_path: str, name: str, args: dict, workspace=None) -> dict:
    from livecode import lsp_client

    selected = _selected_workspace_root(project_path, args, workspace)
    if selected.get("error"):
        return {"error": selected.get("error"), "error_kind": "invalid_input"}
    root = selected["root"]
    full = selected["full"]
    display_path = selected.get("display_path") or str(args.get("file_path") or "")

    if name == "lsp_diagnostics":
        diags = lsp_client.diagnostics(root, full, wait_s=3.0)
        if isinstance(diags, dict) and diags.get("error"):
            return diags
        sev = {1: "error", 2: "warning", 3: "info", 4: "hint"}
        items = [
            {
                "severity": sev.get(d.get("severity"), "info"),
                "line": int((d.get("range") or {}).get("start", {}).get("line", 0)) + 1,
                "message": str(d.get("message") or "")[:400],
                "source": d.get("source") or "pylsp",
            }
            for d in diags
        ]
        errors = [i for i in items if i["severity"] == "error"]
        return {"success": True, "file": display_path, "diagnostic_count": len(items),
                "error_count": len(errors), "diagnostics": items[:60]}

    if name == "lsp_document_symbols":
        res = lsp_client.document_symbols(root, full)
        if isinstance(res, dict) and res.get("error"):
            return res
        symbols = _lsp_symbol_items(res)
        return {"success": True, "file": display_path, "symbol_count": len(symbols), "symbols": symbols[:120]}

    try:
        line = max(0, int(args.get("line") or 1) - 1)
        char = max(0, int(args.get("character") or 1) - 1)
    except (TypeError, ValueError):
        return {"error": "line and character must be integers", "error_kind": "invalid_input"}

    if name == "lsp_definition":
        res = lsp_client.definition(root, full, line, char)
        if isinstance(res, dict) and res.get("error"):
            return res
        locs = _lsp_loc_list(res)
        return {"success": True, "definition_count": len(locs), "definitions": locs}
    if name == "lsp_references":
        res = lsp_client.references(root, full, line, char)
        if isinstance(res, dict) and res.get("error"):
            return res
        locs = _lsp_loc_list(res)
        return {"success": True, "reference_count": len(locs), "references": locs}
    if name == "lsp_completion":
        try:
            max_results = min(80, max(1, int(args.get("max_results") or 40)))
        except (TypeError, ValueError):
            return {"error": "max_results must be an integer", "error_kind": "invalid_input"}
        res = lsp_client.completion(root, full, line, char)
        if isinstance(res, dict) and res.get("error"):
            return res
        items = _lsp_completion_items(res, max_results)
        return {"success": True, "completion_count": len(items), "completions": items}
    if name == "lsp_rename_preview":
        new_name = str(args.get("new_name") or "").strip()
        if not new_name:
            return {"error": "new_name is required", "error_kind": "invalid_input"}
        res = lsp_client.rename(root, full, line, char, new_name)
        if isinstance(res, dict) and res.get("error"):
            return res
        preview = _lsp_workspace_edit_preview(res)
        preview.update({"success": True, "preview_only": True, "new_name": new_name})
        return preview
    if name != "lsp_hover":
        return {"error": f"Unsupported LSP tool: {name}", "error_kind": "invalid_input"}
    res = lsp_client.hover(root, full, line, char)
    if isinstance(res, dict) and res.get("error"):
        return res
    contents = (res or {}).get("contents") if isinstance(res, dict) else None
    text = ""
    if isinstance(contents, dict):
        text = contents.get("value") or ""
    elif isinstance(contents, list):
        parts = []
        for c in contents:
            parts.append(c.get("value") if isinstance(c, dict) else str(c))
        text = "\n".join(p for p in parts if p)
    elif isinstance(contents, str):
        text = contents
    return {"success": True, "hover": text[:2000]}

def _render_todo_checklist(todos) -> str:
    if not isinstance(todos, list):
        return ""
    lines = []
    for todo in todos:
        if isinstance(todo, dict):
            content = str(todo.get("content") or "").strip()
        else:
            content = str(todo or "").strip()
        if content:
            lines.append(f"- [ ] {content}")
    if not lines:
        return ""
    return "## Task checklist\n\n" + "\n".join(lines)

def _livecode_create_plan(project_path: str, args: dict, session_id: str | None) -> dict:
    from livecode import plan_store

    title = str(args.get("title") or "").strip()
    body = str(args.get("plan") or "").strip()
    if not body:
        return {"error": "plan is required — pass the full plan markdown"}
    if "## Task checklist" not in body:
        checklist = _render_todo_checklist(args.get("todos"))
        if checklist:
            body = f"{body}\n\n{checklist}"
    try:
        saved = plan_store.write_plan(
            body,
            title=title or "Untitled plan",
            project_path=project_path,
            session_id=session_id or "",
            filename=str(args.get("plan_file") or "").strip(),
            overview=str(args.get("overview") or "").strip() or None,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"Could not write plan: {exc}"}
    return {
        "success": True,
        "plan_file": saved["file"],
        "plan_path": saved["path"],
        "title": saved["title"],
        "overview": saved["overview"],
        "todos": saved["todos"],
        "plan_chars": len(saved["body"]),
    }

def human_tool_label(name: str, args: dict) -> str:
    try:
        from livecode import mcp_bridge
        if mcp_bridge.is_mcp_tool_name(name):
            server_name, tool_name = mcp_bridge.decode_mcp_tool_name(name)
            return f"MCP {server_name}/{tool_name}"
    except Exception:
        pass
    if name == "grep_repo":
        pat = str(args.get("pattern", ""))
        gf = str(args.get("glob_filter") or "").strip()
        directory = str(args.get("directory") or "").strip()
        if directory and gf:
            return f"Grepped `{pat}` in {directory} ({gf})"
        if directory:
            return f"Grepped `{pat}` in {directory}"
        if gf:
            return f"Grepped `{pat}` in {gf}"
        return f"Grepped `{pat}`"
    if name == "glob_files":
        pat = str(args.get("pattern", ""))[:60]
        return f"Glob `{pat}`"
    if name == "find_files":
        q = str(args.get("query", ""))[:50]
        return f"Find files `{q}`"
    if name == "web_search":
        return f"Web search `{str(args.get('query', ''))[:60]}`"
    if name == "web_fetch":
        from urllib.parse import urlparse

        host = urlparse(str(args.get("url") or "")).hostname or "url"
        return f"Fetched {host}"
    if name == "browser":
        from livecode.browser import action_label

        return action_label(args)
    if name == "read_repo_file":
        fp = os.path.basename(str(args.get("file_path", "")))
        start = args.get("start_line")
        end = args.get("end_line")
        if start and end:
            return f"Read {fp} L{start}-{end}"
        if start:
            return f"Read {fp} L{start}+"
        return f"Read {fp}"
    if name == "list_repo_dir":
        directory = str(args.get("directory") or "").strip()
        label = directory if directory else "project"
        return f"Explored {label}"
    if name == "git_log":
        path = str(args.get("path") or "").strip()
        grep = str(args.get("grep") or "").strip()
        if path and grep:
            return f"Git log `{path}` grep `{grep[:40]}`"
        if path:
            return f"Git log `{path}`"
        if grep:
            return f"Git log grep `{grep[:40]}`"
        return "Git log"
    if name == "ast_symbols":
        return f"Explored {os.path.basename(str(args.get('file_path', '')))}"
    if name == "write_file":
        return f"Editing {os.path.basename(str(args.get('file_path', '')))}"
    if name in ("edit_file", "multi_edit"):
        return f"Editing {os.path.basename(str(args.get('file_path', '')))}"
    if name == "run_command":
        description = str(args.get("description") or "").strip()
        if description:
            return description[:72]
        cmd = str(args.get("command", ""))[:72]
        return f"Running `{cmd}`"
    if name == "command_status":
        return f"Checked `{str(args.get('command_id', ''))[:24]}`"
    if name == "restart_command":
        target = str(args.get("command_id") or args.get("command") or "")[:40]
        return f"Restarted `{target}`" if target else "Restarted the server"
    if name == "kill_command":
        return f"Stopped `{str(args.get('command_id', ''))[:24]}`"
    if name == "find_symbol":
        return f"Find symbol `{str(args.get('name', ''))[:40]}`"
    if name == "find_references":
        return f"Find refs `{str(args.get('name', ''))[:40]}`"
    if name == "list_symbols":
        p = str(args.get("path") or "project")
        return f"List symbols in {p[:40]}"
    if name == "update_memory":
        return "Updated memory"
    if name == "memory_search":
        return f"Memory search `{str(args.get('query', ''))[:40]}`"
    if name == "memory_get":
        return f"Read memory `{str(args.get('path', ''))[:40]}`"
    if name == "ask_question":
        count = len(args.get("questions") or []) if isinstance(args.get("questions"), list) else 0
        return "Asking question" if count == 1 else "Asking questions"
    if name == "create_plan":
        return "Creating plan"
    if name == "spawn_subagent":
        return f"Subagent: {subagent_title(args)}"
    if name == "todo_write":
        items = args.get("todos") if isinstance(args.get("todos"), list) else []
        return f"Updated task list ({len(items)} item{'s' if len(items) != 1 else ''})"
    if name == "update_goal":
        if args.get("completed"):
            return "Goal complete"
        if args.get("blocked_reason"):
            return "Goal blocked"
        return "Goal progress"
    if name in ("lsp_definition", "lsp_references", "lsp_hover", "lsp_completion", "lsp_rename_preview"):
        fp = os.path.basename(str(args.get("file_path", "")))
        verb = {
            "lsp_definition": "Definition",
            "lsp_references": "References",
            "lsp_hover": "Hover",
            "lsp_completion": "Completions",
            "lsp_rename_preview": "Rename preview",
        }[name]
        return f"{verb} in {fp}:{args.get('line', '?')}"
    if name == "lsp_diagnostics":
        return f"Diagnostics for {os.path.basename(str(args.get('file_path', '')))}"
    if name == "lsp_document_symbols":
        return f"LSP symbols for {os.path.basename(str(args.get('file_path', '')))}"
    if name == "attempt_completion":
        return "Thought briefly"
    return name
