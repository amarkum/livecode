from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

LIVECODE_WORKSPACE_SUFFIXES = (".livecode-workspace.json", ".livecode-workspace", ".code-workspace")


class WorkspaceError(ValueError):
    def __init__(self, message: str, missing: Any = (), remaining: int = 0):
        super().__init__(message)
        self.missing = tuple(missing)
        self.remaining = remaining


@dataclass(frozen=True)
class WorkspaceFolder:
    name: str
    path: str


@dataclass(frozen=True)
class LivecodeWorkspace:
    primary_path: str
    folders: tuple[WorkspaceFolder, ...]
    settings: dict[str, Any]
    mcp_servers: dict[str, Any]
    config_path: str = ""
    missing: tuple[WorkspaceFolder, ...] = ()


def is_livecode_workspace_file(path: str) -> bool:
    value = str(path or "").strip().lower()
    return any(value.endswith(suffix) for suffix in LIVECODE_WORKSPACE_SUFFIXES)


def _path_basename(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


def _safe_name(path: str, explicit: Any = None) -> str:
    name = str(explicit or "").strip()
    if name and name not in {".", ".."} and "/" not in name and "\\" not in name:
        return name
    fallback = os.path.basename(os.path.abspath(os.path.expanduser(path)))
    return _path_basename(path) or fallback or "workspace"


def workspace_name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def workspace_folder_aliases(folder: WorkspaceFolder) -> tuple[str, ...]:
    aliases = []
    for raw in (folder.name, os.path.basename(folder.path)):
        value = str(raw or "").strip()
        if not value:
            continue
        for alias in (value, workspace_name_key(value)):
            if alias and alias not in aliases:
                aliases.append(alias)
    return tuple(aliases)


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def validate_workspace_folders(folders: list[WorkspaceFolder]) -> None:
    names: dict[str, str] = {}
    paths: set[str] = set()
    for folder in folders:
        path_key = _real(folder.path)
        if path_key in paths:
            raise ValueError(f"Duplicate workspace folder path: {folder.path}")
        paths.add(path_key)
        name = str(folder.name or "").strip()
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError(f"Invalid workspace folder name: {folder.name}")
        key = workspace_name_key(name)
        if not key:
            raise ValueError(f"Invalid workspace folder name: {folder.name}")
        if key in names:
            raise ValueError(f"Duplicate workspace folder name: {folder.name}")
        names[key] = folder.path


def _validate_unique_aliases(folders: list[WorkspaceFolder]) -> None:
    validate_workspace_folders(folders)


def _parse_folder(raw: Any, base_dir: Path | None = None) -> WorkspaceFolder | None:
    if isinstance(raw, str):
        path = raw.strip()
        name = ""
    elif isinstance(raw, dict):
        path = str(raw.get("path") or "").strip()
        name = str(raw.get("name") or "").strip()
    else:
        return None
    if not path:
        return None
    expanded_user = os.path.expanduser(path)
    if os.path.isabs(expanded_user) or base_dir is None:
        expanded = os.path.abspath(expanded_user)
    else:
        expanded = os.path.abspath(str(base_dir / expanded_user))
    return WorkspaceFolder(name=_safe_name(expanded, name), path=expanded)


def _folder_problem(folder: WorkspaceFolder) -> str:
    if os.path.isdir(folder.path):
        return ""
    return "not a directory" if os.path.exists(folder.path) else "not found"


def _partition_folders(folders: list[WorkspaceFolder]) -> tuple[list[WorkspaceFolder], list[tuple[int, WorkspaceFolder, str]]]:
    present: list[WorkspaceFolder] = []
    absent: list[tuple[int, WorkspaceFolder, str]] = []
    for index, folder in enumerate(folders):
        problem = _folder_problem(folder)
        if problem:
            absent.append((index, folder, problem))
        else:
            present.append(folder)
    return present, absent


def _missing_error(absent: list[tuple[int, WorkspaceFolder, str]], remaining: int, message: str) -> WorkspaceError:
    return WorkspaceError(message, missing=[folder for _, folder, _ in absent], remaining=remaining)


def _unique_folder_name(base: str, used: set[str]) -> str:
    name = base
    index = 2
    while workspace_name_key(name) in used:
        name = f"{base}-{index}"
        index += 1
    used.add(workspace_name_key(name))
    return name


def check_workspace_folders(raw_folders: Any) -> tuple[list[WorkspaceFolder], list[dict[str, str]]]:
    present: list[WorkspaceFolder] = []
    missing: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    used_names: set[str] = set()
    for raw in raw_folders if isinstance(raw_folders, list) else []:
        folder = _parse_folder(raw)
        if folder is None:
            continue
        path_key = _real(folder.path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        problem = _folder_problem(folder)
        if problem:
            missing.append({"name": folder.name, "path": folder.path, "reason": problem})
            continue
        present.append(WorkspaceFolder(name=_unique_folder_name(folder.name, used_names), path=folder.path))
    return present, missing


def merge_workspace_entries(existing: Any, folders: list[WorkspaceFolder], base_dir: str) -> list[Any]:
    preserved: dict[str, tuple[Any, WorkspaceFolder]] = {}
    for entry in existing if isinstance(existing, list) else []:
        parsed = _parse_folder(entry, Path(base_dir))
        if parsed is not None:
            preserved[_real(parsed.path)] = (entry, parsed)
    merged: list[Any] = []
    for folder in folders:
        match = preserved.get(_real(folder.path))
        if match is not None and match[1].name == folder.name:
            merged.append(match[0])
        else:
            merged.append({"name": folder.name, "path": folder.path})
    return merged


def _payload_dicts(settings: Any, mcp_servers: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(settings, dict):
        raise ValueError("workspace.settings must be an object")
    if not isinstance(mcp_servers, dict):
        raise ValueError("workspace.mcpServers must be an object")
    return dict(settings), dict(mcp_servers)


def _declared_payload(project_path: str, workspace_payload: Any) -> tuple[list[WorkspaceFolder], str, dict[str, Any], dict[str, Any]]:
    if not isinstance(workspace_payload, dict):
        raise ValueError("workspace must be an object")
    raw_folders = workspace_payload.get("folders")
    if not isinstance(raw_folders, list) or not raw_folders:
        raise ValueError("workspace.folders must be a non-empty array")
    folders: list[WorkspaceFolder] = []
    for index, raw in enumerate(raw_folders):
        folder = _parse_folder(raw)
        if folder is None:
            raise ValueError(f"Invalid workspace folder at index {index}")
        folders.append(folder)
    validate_workspace_folders(folders)
    primary = _real(os.path.expanduser(project_path))
    first = _real(folders[0].path)
    config_path = str(workspace_payload.get("path") or "").strip()
    config = _real(os.path.expanduser(config_path)) if config_path else ""
    if primary != first and primary != config:
        raise ValueError(
            "project_path must match the first workspace folder or workspace config path "
            f"(project_path={project_path}, first folder={folders[0].path})"
        )
    settings, mcp_servers = _payload_dicts(workspace_payload.get("settings", {}), workspace_payload.get("mcpServers", {}))
    return folders, config_path, settings, mcp_servers


def workspace_from_payload(
    project_path: str,
    workspace_payload: dict[str, Any] | None = None,
    *,
    strict: bool = False,
) -> LivecodeWorkspace:
    if workspace_payload is None:
        return load_livecode_workspace(project_path)
    folders, config_path, settings, mcp_servers = _declared_payload(project_path, workspace_payload)
    present, absent = _partition_folders(folders)
    if absent:
        first_index, first_folder, first_problem = absent[0]
        if strict:
            raise _missing_error(
                absent,
                len(present),
                f"Invalid workspace folder at index {first_index}: {first_folder.path} ({first_problem})",
            )
        if first_index == 0:
            state = "not found" if first_problem == "not found" else "is not a directory"
            raise _missing_error(
                absent,
                len(present),
                f"Primary workspace folder {state}: {first_folder.path}",
            )
    return LivecodeWorkspace(
        primary_path=present[0].path,
        folders=tuple(present),
        settings=settings,
        mcp_servers=mcp_servers,
        config_path=config_path,
        missing=tuple(folder for _, folder, _ in absent),
    )


def declared_primary_path(project_path: str, workspace_payload: dict[str, Any] | None = None) -> str:
    raw_path = str(project_path or "").strip()
    if not raw_path:
        raise ValueError("project_path required")
    if workspace_payload is not None:
        folders, _config_path, _settings, _mcp_servers = _declared_payload(raw_path, workspace_payload)
        return folders[0].path
    expanded = Path(os.path.abspath(os.path.expanduser(raw_path)))
    if expanded.is_file() and is_livecode_workspace_file(str(expanded)):
        data = _read_workspace_file(expanded)
        raw_folders = data.get("folders")
        first = _parse_folder(raw_folders[0], expanded.parent) if isinstance(raw_folders, list) and raw_folders else None
        if first is None:
            raise ValueError(f"Workspace has no valid folders: {raw_path}")
        return first.path
    return str(expanded)


def _read_workspace_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"Unable to read workspace file: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid workspace JSON: {path}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Workspace document must be an object: {path}")
    return data


def load_livecode_workspace(path_or_project: str, *, allow_partial: bool = False) -> LivecodeWorkspace:
    raw_path = str(path_or_project or "").strip()
    expanded = Path(os.path.abspath(os.path.expanduser(raw_path)))
    if expanded.is_file() and is_livecode_workspace_file(str(expanded)):
        data = _read_workspace_file(expanded)
        raw_folders = data.get("folders")
        if not isinstance(raw_folders, list) or not raw_folders:
            raise ValueError(f"Workspace has no valid folders: {path_or_project}")
        parsed_folders: list[WorkspaceFolder] = []
        for index, item in enumerate(raw_folders):
            folder = _parse_folder(item, expanded.parent)
            if folder is None:
                raise ValueError(f"Invalid workspace folder at index {index}: {path_or_project}")
            parsed_folders.append(folder)
        validate_workspace_folders(parsed_folders)
        present, absent = _partition_folders(parsed_folders)
        if absent and not allow_partial:
            first_index, first_folder, first_problem = absent[0]
            raise _missing_error(
                absent,
                len(present),
                f"Invalid workspace folder at index {first_index}: {first_folder.path} ({first_problem}) in {path_or_project}",
            )
        if not present:
            raise _missing_error(absent, 0, f"Workspace has no valid folders: {path_or_project}")
        settings, mcp_servers = _payload_dicts(data.get("settings", {}), data.get("mcpServers", {}))
        return LivecodeWorkspace(
            primary_path=present[0].path,
            folders=tuple(present),
            settings=settings,
            mcp_servers=mcp_servers,
            config_path=str(expanded),
            missing=tuple(folder for _, folder, _ in absent),
        )
    project = str(expanded)
    if not os.path.isdir(project):
        raise _missing_error(
            [(0, WorkspaceFolder(name=_safe_name(project), path=project), "not found")],
            0,
            f"Project path not found: {path_or_project}",
        )
    folder = WorkspaceFolder(name=_safe_name(project), path=project)
    return LivecodeWorkspace(
        primary_path=project,
        folders=(folder,),
        settings={},
        mcp_servers={},
    )
