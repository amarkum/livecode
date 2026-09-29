from __future__ import annotations

import os
import re
from typing import Any

from livecode.workspace import build_workspace_index, resolve_safe_path, resolve_workspace_path, search_file_manifest, SKIP_DIRS
from livecode.project_store import workspace_state_identity
from livecode.workspace_config import workspace_from_payload, workspace_name_key

CHAT_ATTACHMENT_MAX_CHARS = 40_000
FOLDER_LISTING_MAX_CHARS = 10_000

CHAT_TRANSCRIPT_MAX_CHARS = 12_000
CHAT_TRANSCRIPT_TOTAL_MAX_CHARS = 24_000

def _workspace_from_optional_payload(project_path: str, workspace_payload: dict[str, Any] | None):
    if not workspace_payload:
        return None
    return workspace_from_payload(project_path, workspace_payload)


def _resolve_context_path(project_path: str, repo_path: str, workspace_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    workspace = _workspace_from_optional_payload(project_path, workspace_payload)
    if workspace is not None:
        return resolve_workspace_path(project_path, repo_path, workspace)
    full, safe_rel = resolve_safe_path(project_path, repo_path)
    if not full:
        return {"error": str(safe_rel or "Invalid path")}
    rel = os.path.relpath(full, project_path).replace("\\", "/")
    rel = "" if rel == "." else rel
    return {"folder": None, "root": project_path, "rel": rel, "full": full, "display_path": rel}


def _read_repo_file_content(project_path: str, repo_path: str, workspace_payload: dict[str, Any] | None = None) -> tuple[str, bool]:
    selected = _resolve_context_path(project_path, repo_path, workspace_payload)
    full = selected.get("full")
    safe_rel = selected.get("display_path") or selected.get("rel") or repo_path
    if selected.get("error") or not full:
        return (str(selected.get("error") or "Invalid path"), False)
    if not os.path.isfile(full):
        return (f"File not found: {safe_rel}", False)
    try:
        with open(full, "r", errors="replace") as f:
            content = f.read()
    except OSError as exc:
        return (f"Could not read {safe_rel}: {exc}", False)
    truncated = False
    if len(content) > CHAT_ATTACHMENT_MAX_CHARS:
        content = content[:CHAT_ATTACHMENT_MAX_CHARS]
        truncated = True
    return content, truncated

def _build_folder_listing(project_path: str, repo_path: str, workspace_payload: dict[str, Any] | None = None) -> tuple[str, bool]:
    selected = _resolve_context_path(project_path, repo_path, workspace_payload)
    if selected.get("error"):
        return str(selected.get("error") or "Invalid path"), False
    root = selected.get("root") or project_path
    prefix = (selected.get("rel") or "").strip().replace("\\", "/").strip("/")
    display_prefix = (selected.get("display_path") or prefix).strip("/")
    index = build_workspace_index(root)
    all_files = [str(entry.get("rel") or "") for entry in (index.get("files") or [])]

    matched_files: list[str] = []
    dirs: set[str] = set()
    if prefix:
        dirs.add(prefix)
        prefix_slash = prefix + "/"
        for rel in all_files:
            if rel == prefix or rel.startswith(prefix_slash):
                matched_files.append(rel)
                parts = rel.split("/")
                for i in range(len(parts) - 1):
                    d = "/".join(parts[: i + 1])
                    if d.startswith(prefix) or d == prefix:
                        dirs.add(d)
    else:
        for rel in all_files:
            matched_files.append(rel)
            parts = rel.split("/")
            for i in range(len(parts) - 1):
                dirs.add("/".join(parts[: i + 1]))
        for top in index.get("top_dirs") or []:
            dirs.add(str(top).rstrip("/"))

    def display_child(path: str) -> str:
        rel = str(path or "").strip("/")
        if not display_prefix or display_prefix == prefix:
            return rel
        if not prefix:
            return f"{display_prefix}/{rel}" if rel else display_prefix
        if rel == prefix:
            return display_prefix
        prefix_slash = prefix + "/"
        if rel.startswith(prefix_slash):
            suffix = rel[len(prefix_slash):].strip("/")
            return f"{display_prefix}/{suffix}" if suffix else display_prefix
        return f"{display_prefix}/{rel}" if rel else display_prefix

    label = display_prefix or "/"
    lines = [
        f"Folder: {label}",
        "",
        "Directories:",
    ]
    for d in sorted(dirs)[:300]:
        lines.append(f"  {display_child(d)}/")
    lines.extend(["", "Files:"])
    for rel in sorted(matched_files)[:800]:
        lines.append(f"  {display_child(rel)}")

    text = "\n".join(lines)
    truncated = False
    if len(text) > FOLDER_LISTING_MAX_CHARS:
        text = text[:FOLDER_LISTING_MAX_CHARS] + "\n...(truncated)"
        truncated = True
    return text, truncated

def expand_repo_context_attachments(
    project_path: str,
    attachments: list[dict[str, Any]] | None,
    workspace_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    chat_budget_left = CHAT_TRANSCRIPT_TOTAL_MAX_CHARS
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        att_type = str(att.get("type") or "")
        repo_path = str(att.get("repo_path") or "").strip().replace("\\", "/").lstrip("/")
        name = str(att.get("name") or os.path.basename(repo_path) or repo_path or "context")

        if att_type == "repo_file":
            if not repo_path:
                continue
            content, truncated = _read_repo_file_content(project_path, repo_path, workspace_payload)
            out: dict[str, Any] = {
                "name": name,
                "type": "text",
                "content": content,
                "size": len(content),
            }
            if truncated:
                out["truncated"] = True
            expanded.append(out)
            continue

        if att_type == "repo_folder":
            content, truncated = _build_folder_listing(project_path, repo_path, workspace_payload)
            out = {
                "name": name,
                "type": "text",
                "content": content,
                "size": len(content),
            }
            if truncated:
                out["truncated"] = True
            expanded.append(out)
            continue

        if att_type == "chat":
            from livecode.session import session_transcript_text

            sess_id = str(att.get("session_id") or "").strip()
            if not sess_id:
                continue
            label = name if name and name != "context" else f"chat {sess_id[:8]}"
            per_chat_cap = min(CHAT_TRANSCRIPT_MAX_CHARS, max(chat_budget_left, 0))
            if per_chat_cap <= 0:
                expanded.append({
                    "name": f"Previous chat — {label}",
                    "type": "text",
                    "content": "(Not included — combined chat-context limit reached.)",
                    "size": 0,
                })
                continue
            transcript_state_path = project_path
            if workspace_payload:
                transcript_state_path = workspace_state_identity(
                    _workspace_from_optional_payload(project_path, workspace_payload)
                )
            content = session_transcript_text(
                transcript_state_path, sess_id, max_chars=per_chat_cap
            )
            if not content:
                content = "(No transcript is available for the referenced chat.)"
            chat_budget_left -= len(content)
            out = {
                "name": f"Previous chat — {label}",
                "type": "text",
                "content": content,
                "size": len(content),
            }
            if content.endswith("(transcript truncated)"):
                out["truncated"] = True
            expanded.append(out)
            continue

        expanded.append(att)
    return expanded

def list_context_directory(
    project_path: str,
    directory: str = "",
    *,
    limit: int = 50,
    workspace_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cap = min(max(int(limit or 50), 1), 100)
    workspace = _workspace_from_optional_payload(project_path, workspace_payload)
    if workspace is not None and len(workspace.folders) > 1 and not str(directory or "").strip():
        results = [
            {"kind": "folder", "path": folder.name, "name": folder.name}
            for folder in workspace.folders[:cap]
        ]
        return {"success": True, "query": "", "directory": "", "results": results}
    selected = _resolve_context_path(project_path, directory or "", workspace_payload)
    full = selected.get("full")
    if selected.get("error") or not full:
        return {"success": False, "error": str(selected.get("error") or "Invalid path"), "results": []}
    display_prefix = (selected.get("display_path") or selected.get("rel") or "").replace("\\", "/").strip("/")
    if workspace is not None and len(workspace.folders) == 1:
        display_prefix = (selected.get("rel") or "").replace("\\", "/").strip("/")
    if not os.path.isdir(full):
        return {
            "success": True,
            "query": "",
            "directory": display_prefix,
            "results": [],
        }

    rel_prefix = (selected.get("rel") or "").replace("\\", "/").strip("/")
    dirs: list[dict[str, str]] = []
    files: list[dict[str, str]] = []
    try:
        entries = sorted(os.listdir(full), key=lambda n: n.lower())
    except OSError as exc:
        return {"success": False, "error": str(exc), "results": []}

    for name in entries:
        if name in SKIP_DIRS:
            continue
        child_full = os.path.join(full, name)
        child_rel = f"{rel_prefix}/{name}".strip("/") if rel_prefix else name
        child_path = f"{display_prefix}/{name}".strip("/") if display_prefix else child_rel
        if os.path.isdir(child_full):
            dirs.append({"kind": "folder", "path": child_path, "name": name})
        elif os.path.isfile(child_full):
            files.append({"kind": "file", "path": child_path, "name": name})

    results = (dirs + files)[:cap]
    return {
        "success": True,
        "query": "",
        "directory": display_prefix,
        "results": results,
    }

def search_context_targets(
    project_path: str,
    query: str = "",
    *,
    limit: int = 20,
    directory: str | None = None,
    workspace_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cap = min(max(int(limit or 20), 1), 50)
    q = (query or "").strip().lower()

    if not q and directory is not None:
        payload = list_context_directory(project_path, directory, limit=cap, workspace_payload=workspace_payload)
        payload["query"] = query
        return payload

    workspace = _workspace_from_optional_payload(project_path, workspace_payload)
    folders = workspace.folders if workspace is not None else []
    roots = [(folder.path, folder.name if workspace is not None and len(workspace.folders) > 1 else "") for folder in folders]
    if not roots:
        roots = [(project_path, "")]
    results: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    def _display_path(prefix: str, rel: str) -> str:
        rel = rel.replace("\\", "/").strip("/")
        return f"{prefix}/{rel}".strip("/") if prefix else rel

    def _add(kind: str, path: str) -> None:
        path = path.replace("\\", "/").strip("/")
        key = f"{kind}:{path}"
        if key in seen_paths or len(results) >= cap:
            return
        seen_paths.add(key)
        display = os.path.basename(path) if path else "/"
        results.append({
            "kind": kind,
            "path": path,
            "name": display or path,
        })

    for root, prefix in roots:
        if len(results) >= cap:
            break
        index = build_workspace_index(root)
        if not q:
            for top in (index.get("top_dirs") or [])[:12]:
                _add("folder", _display_path(prefix, str(top).rstrip("/")))
            for rel in (index.get("sample_files") or [])[:12]:
                _add("file", _display_path(prefix, str(rel)))
            continue

        folder_candidates: set[str] = set()
        for top in index.get("top_dirs") or []:
            folder_candidates.add(str(top).rstrip("/"))
        for entry in index.get("files") or []:
            rel = str(entry.get("rel") or "")
            parts = rel.split("/")
            for i in range(len(parts) - 1):
                folder_candidates.add("/".join(parts[: i + 1]))

        for folder in sorted(folder_candidates, key=lambda p: (p.count("/"), p.lower())):
            folder_lower = folder.lower()
            base = os.path.basename(folder_lower)
            display_path = _display_path(prefix, folder)
            if q in folder_lower or q in base or q in display_path.lower():
                _add("folder", display_path)
            if len(results) >= cap:
                break

        if len(results) >= cap:
            break
        file_search = search_file_manifest(root, query, max_results=cap)
        for entry in file_search.get("files") or []:
            rel = str(entry.get("path") or "")
            if rel:
                _add("file", _display_path(prefix, rel))
            if len(results) >= cap:
                break

    return {"success": True, "query": query, "results": results[:cap]}

def _parse_skill_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in ("name", "description", "argument-hint") and value:
            fields[key] = value
    return fields

SLASH_COMMAND_MAX_CHARS = 20_000
_SLASH_RE = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_.:/-]*)[ \t]*(.*)", re.DOTALL)
_SLASH_NAME_SEG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

def _command_search_roots(project_path: str) -> list[str]:
    roots = [os.path.join(project_path, ".claude", "commands")]
    home = os.path.expanduser("~")
    if home and home != "~":
        roots.append(os.path.join(home, ".claude", "commands"))
    return roots

def _iter_markdown_files(root: str):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fname in sorted(filenames):
            if fname.endswith(".md"):
                yield os.path.join(dirpath, fname)

def _command_name_for(root: str, path: str) -> str:
    """`.claude/commands/frontend/review.md` -> `frontend:review`."""
    rel = os.path.relpath(path, root)
    rel = rel[: -len(".md")] if rel.endswith(".md") else rel
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    return ":".join(parts)

def _split_command_name(name: str) -> list[str] | None:
    segs = [s for s in re.split(r"[:/]", name.strip()) if s]
    if not segs or any(not _SLASH_NAME_SEG.match(s) for s in segs):
        return None
    return segs

def _collect_project_skills(project_path: str, prefix: str = "", include_user_commands: bool = True) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen_names: set[str] = set()

    def _add(name: str, description: str, kind: str, source_path: str) -> None:
        name = (name or "").strip()
        display_name = f"{prefix}::{name}" if prefix else name
        if not name or display_name in seen_names:
            return
        seen_names.add(display_name)
        results.append({
            "name": display_name,
            "description": (description or "").strip(),
            "kind": kind,
            "source_workspace": prefix,
            "source_path": source_path,
        })

    skills_dir = os.path.join(project_path, ".claude", "skills")
    if os.path.isdir(skills_dir):
        for entry in sorted(os.listdir(skills_dir)):
            skill_md = os.path.join(skills_dir, entry, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            try:
                with open(skill_md, "r", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            fields = _parse_skill_frontmatter(content)
            rel = os.path.relpath(skill_md, project_path).replace("\\", "/")
            _add(fields.get("name") or entry, fields.get("description", ""), "skill", rel)

    command_roots = _command_search_roots(project_path) if include_user_commands else [os.path.join(project_path, ".claude", "commands")]
    for root in command_roots:
        for command_path in _iter_markdown_files(root):
            try:
                with open(command_path, "r", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            fields = _parse_skill_frontmatter(content)
            description = fields.get("description", "")
            if not description:
                for line in _strip_frontmatter(content).splitlines():
                    if line.strip():
                        description = line.strip()
                        break
            hint = fields.get("argument-hint", "")
            if hint:
                description = f"{description}  ·  {hint}" if description else hint
            rel = os.path.relpath(command_path, project_path).replace("\\", "/") if os.path.isabs(command_path) else command_path
            _add(_command_name_for(root, command_path), description, "command", rel)

    return results


def list_project_skills(project_path: str) -> list[dict[str, str]]:
    return _collect_project_skills(project_path)


def list_workspace_skills(project_path: str, workspace_payload: dict[str, Any] | None = None) -> list[dict[str, str]]:
    workspace = _workspace_from_optional_payload(project_path, workspace_payload)
    if workspace is None or len(workspace.folders) <= 1:
        return list_project_skills(project_path)
    results: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, folder in enumerate(workspace.folders):
        prefix = "" if index == 0 else folder.name
        for item in _collect_project_skills(folder.path, prefix, include_user_commands=index == 0):
            name = item.get("name", "")
            if name in seen_names:
                continue
            seen_names.add(name)
            results.append(item)
    return results

def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text

def _find_slash_command_file(project_path: str, name: str) -> tuple[str, str] | None:
    """Return ``(kind, abspath)`` for a command/skill invoked as ``/name``."""
    segs = _split_command_name(name)
    if not segs:
        return None
    target = ":".join(segs).lower()

    for root in _command_search_roots(project_path):
        direct = os.path.join(root, *segs) + ".md"
        if os.path.isfile(direct):
            return ("command", direct)

    if len(segs) == 1:
        skill_md = os.path.join(project_path, ".claude", "skills", segs[0], "SKILL.md")
        if os.path.isfile(skill_md):
            return ("skill", skill_md)

    for root in _command_search_roots(project_path):
        for path in _iter_markdown_files(root):
            try:
                with open(path, "r", errors="replace") as f:
                    head = f.read(2000)
            except OSError:
                continue
            fm_name = (_parse_skill_frontmatter(head).get("name") or "").strip().lower()
            if fm_name and fm_name == target:
                return ("command", path)

    skills_dir = os.path.join(project_path, ".claude", "skills")
    if len(segs) == 1 and os.path.isdir(skills_dir):
        for entry in sorted(os.listdir(skills_dir)):
            path = os.path.join(skills_dir, entry, "SKILL.md")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", errors="replace") as f:
                    head = f.read(2000)
            except OSError:
                continue
            fm_name = (_parse_skill_frontmatter(head).get("name") or entry).strip().lower()
            if fm_name == target:
                return ("skill", path)

    return None

def _workspace_command_target(project_path: str, name: str, workspace_payload: dict[str, Any] | None) -> tuple[str, str, str]:
    if "::" not in name or not workspace_payload:
        return project_path, name, ""
    selector, command_name = name.split("::", 1)
    workspace = _workspace_from_optional_payload(project_path, workspace_payload)
    if workspace is None or not command_name:
        return project_path, name, ""
    selector_key = workspace_name_key(selector)
    matches = [folder for folder in workspace.folders if workspace_name_key(folder.name) == selector_key]
    if not matches:
        matches = [folder for folder in workspace.folders if workspace_name_key(os.path.basename(folder.path)) == selector_key]
    if len(matches) != 1:
        return project_path, name, ""
    return matches[0].path, command_name, matches[0].name


def _apply_command_arguments(body: str, args: str) -> str:
    parts = args.split()
    out = body
    if "$ARGUMENTS" in out:
        out = out.replace("$ARGUMENTS", args)
    for i in range(1, 10):
        token = f"${i}"
        if token in out:
            out = out.replace(token, parts[i - 1] if i - 1 < len(parts) else "")
    had_placeholder = ("$ARGUMENTS" in body) or any(f"${i}" in body for i in range(1, 10))
    if args and not had_placeholder:
        out = f"{out}\n\n---\nArguments: {args}"
    return out

def resolve_slash_command(project_path: str, text: str, workspace_payload: dict[str, Any] | None = None) -> tuple[str, dict[str, str] | None]:
    """Expand a leading ``/name`` in ``text`` into the matching command body.

    Returns ``(expanded_text, info)`` when ``text`` starts with a slash token
    that resolves to a command or skill, otherwise ``(text, None)`` so ordinary
    messages (and unknown ``/tokens``) pass through untouched.
    """
    raw = text or ""
    if not raw.lstrip().startswith("/"):
        return (raw, None)
    match = _SLASH_RE.match(raw.lstrip())
    if not match:
        return (raw, None)
    name = match.group(1).rstrip(":/")
    args = match.group(2).replace(" ", " ").strip()
    command_root, command_name, folder_name = _workspace_command_target(project_path, name, workspace_payload)
    found = _find_slash_command_file(command_root, command_name)
    if not found:
        return (raw, None)
    kind, path = found
    try:
        with open(path, "r", errors="replace") as f:
            body = f.read(SLASH_COMMAND_MAX_CHARS + 1)
    except OSError:
        return (raw, None)
    truncated = len(body) > SLASH_COMMAND_MAX_CHARS
    body = _strip_frontmatter(body[:SLASH_COMMAND_MAX_CHARS]).strip()
    if not body:
        return (raw, None)

    expanded = _apply_command_arguments(body, args)
    if truncated:
        expanded += "\n\n[Command file truncated]"

    try:
        rel = os.path.relpath(path, command_root)
    except ValueError:
        rel = path
    if rel.startswith(".."):
        rel = path
    display_path = f"{folder_name}/{rel}" if folder_name else rel
    header = f"<!-- Expanded from the /{name} {kind} ({display_path}) -->\n"
    return (header + expanded, {"name": name, "kind": kind, "path": display_path})
