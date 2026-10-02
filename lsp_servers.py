"""Language servers LiveCode can run, per language, and the user's choices for them.

Each language lists candidate servers, best first. The first one found on this machine is used,
unless Settings > Languages gives a command of its own. Choices are saved in ~/.livecode/lsp.json:
{"enabled": bool, "languages": {"<id>": {"enabled": bool, "command": "<argv>"}}}.

Commands may use {python} (LiveCode's interpreter), {project} (the project folder) and {cache}
(a per-project folder for servers like jdtls that need a workspace directory).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
import threading
from typing import Any

CONFIG_PATH = os.path.expanduser("~/.livecode/lsp.json")
CACHE_ROOT = os.path.expanduser("~/.livecode/lsp-cache")
COMMAND_MAX_CHARS = 1000

# Where servers usually land when the server process's PATH is narrower than a login shell's.
_EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/bin",
    "~/go/bin",
    "~/.cargo/bin",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "~/.dotnet/tools",
    "~/.ghcup/bin",
    "~/.local/share/coursier/bin",
    "~/Library/Application Support/Coursier/bin",
    "~/.gem/bin",
    "~/.rbenv/shims",
    "~/.asdf/shims",
    "~/.volta/bin",
    "~/.livecode/venv/bin",
)

# builtin: the editor already has its own intelligence for it, so the server is off unless chosen.
LANGUAGES: list[dict[str, Any]] = [
    {"id": "python", "label": "Python", "monaco": ["python"], "extensions": [".py", ".pyi", ".pyw"], "servers": [
        {"name": "pylsp", "argv": ["{python}", "-m", "pylsp"], "module": "pylsp", "install": "pip install 'python-lsp-server[all]'"},
        {"name": "basedpyright", "argv": ["basedpyright-langserver", "--stdio"], "install": "pip install basedpyright"},
        {"name": "pyright", "argv": ["pyright-langserver", "--stdio"], "install": "npm install -g pyright"},
        {"name": "pylsp (PATH)", "argv": ["pylsp"], "install": "pipx install python-lsp-server"},
    ]},
    {"id": "typescript", "label": "TypeScript / JavaScript", "builtin": True,
     "monaco": ["typescript", "javascript"], "extensions": [".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"],
     "language_ids": {".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "typescriptreact",
                      ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascriptreact"},
     "servers": [{"name": "typescript-language-server", "argv": ["typescript-language-server", "--stdio"],
                  "install": "npm install -g typescript typescript-language-server"}]},
    {"id": "java", "label": "Java", "monaco": ["java"], "extensions": [".java"], "servers": [
        {"name": "jdtls", "argv": ["jdtls", "-data", "{cache}"], "install": "brew install jdtls"},
    ]},
    {"id": "kotlin", "label": "Kotlin", "monaco": ["kotlin"], "extensions": [".kt", ".kts"], "servers": [
        {"name": "kotlin-lsp", "argv": ["kotlin-lsp", "--stdio"], "install": "brew install JetBrains/utils/kotlin-lsp"},
        {"name": "kotlin-language-server", "argv": ["kotlin-language-server"], "install": "brew install kotlin-language-server"},
    ]},
    {"id": "scala", "label": "Scala", "monaco": ["scala"], "extensions": [".scala", ".sc", ".sbt"], "servers": [
        {"name": "metals", "argv": ["metals"], "install": "coursier install metals"},
    ]},
    {"id": "go", "label": "Go", "monaco": ["go"], "extensions": [".go"], "servers": [
        {"name": "gopls", "argv": ["gopls"], "install": "go install golang.org/x/tools/gopls@latest"},
    ]},
    {"id": "rust", "label": "Rust", "monaco": ["rust"], "extensions": [".rs"], "servers": [
        {"name": "rust-analyzer", "argv": ["rust-analyzer"], "install": "rustup component add rust-analyzer"},
    ]},
    {"id": "cpp", "label": "C / C++ / Objective-C", "monaco": ["c", "cpp", "objective-c"],
     "extensions": [".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".ino", ".m", ".mm"],
     "language_ids": {".c": "c", ".h": "c", ".m": "objective-c", ".mm": "objective-cpp"},
     "default_language_id": "cpp",
     "servers": [{"name": "clangd", "argv": ["clangd", "--background-index"], "install": "brew install llvm (or apt install clangd)"}]},
    {"id": "csharp", "label": "C#", "monaco": ["csharp"], "extensions": [".cs", ".csx"], "servers": [
        {"name": "csharp-ls", "argv": ["csharp-ls"], "install": "dotnet tool install --global csharp-ls"},
        {"name": "OmniSharp", "argv": ["OmniSharp", "-lsp"], "install": "brew install omnisharp/omnisharp-roslyn/omnisharp-mono"},
    ]},
    {"id": "swift", "label": "Swift", "monaco": ["swift"], "extensions": [".swift"], "servers": [
        {"name": "sourcekit-lsp", "argv": ["sourcekit-lsp"], "install": "Comes with Xcode or the Swift toolchain"},
    ]},
    {"id": "ruby", "label": "Ruby", "monaco": ["ruby"], "extensions": [".rb", ".rake", ".gemspec", ".ru"], "servers": [
        {"name": "ruby-lsp", "argv": ["ruby-lsp"], "install": "gem install ruby-lsp"},
        {"name": "solargraph", "argv": ["solargraph", "stdio"], "install": "gem install solargraph"},
    ]},
    {"id": "php", "label": "PHP", "monaco": ["php"], "extensions": [".php", ".phtml"], "servers": [
        {"name": "intelephense", "argv": ["intelephense", "--stdio"], "install": "npm install -g intelephense"},
        {"name": "phpactor", "argv": ["phpactor", "language-server"], "install": "brew install phpactor"},
    ]},
    {"id": "dart", "label": "Dart / Flutter", "monaco": ["dart"], "extensions": [".dart"], "servers": [
        {"name": "dart", "argv": ["dart", "language-server", "--protocol=lsp"], "install": "brew install dart (or install Flutter)"},
    ]},
    {"id": "lua", "label": "Lua", "monaco": ["lua"], "extensions": [".lua"], "servers": [
        {"name": "lua-language-server", "argv": ["lua-language-server"], "install": "brew install lua-language-server"},
    ]},
    {"id": "elixir", "label": "Elixir", "monaco": ["elixir"], "extensions": [".ex", ".exs"], "servers": [
        {"name": "elixir-ls", "argv": ["elixir-ls"], "install": "brew install elixir-ls"},
        {"name": "lexical", "argv": ["lexical"], "install": "See github.com/lexical-lsp/lexical"},
    ]},
    {"id": "haskell", "label": "Haskell", "monaco": [], "extensions": [".hs", ".lhs"], "servers": [
        {"name": "haskell-language-server", "argv": ["haskell-language-server-wrapper", "--lsp"], "install": "ghcup install hls"},
    ]},
    {"id": "zig", "label": "Zig", "monaco": [], "extensions": [".zig"], "servers": [
        {"name": "zls", "argv": ["zls"], "install": "brew install zls"},
    ]},
    {"id": "shell", "label": "Shell", "monaco": ["shell"], "extensions": [".sh", ".bash", ".zsh"], "language_id": "shellscript", "servers": [
        {"name": "bash-language-server", "argv": ["bash-language-server", "start"], "install": "npm install -g bash-language-server"},
    ]},
    {"id": "yaml", "label": "YAML", "monaco": ["yaml"], "extensions": [".yaml", ".yml"], "servers": [
        {"name": "yaml-language-server", "argv": ["yaml-language-server", "--stdio"], "install": "npm install -g yaml-language-server"},
    ]},
    {"id": "terraform", "label": "Terraform / HCL", "monaco": ["hcl"], "extensions": [".tf", ".tfvars"], "language_id": "terraform", "servers": [
        {"name": "terraform-ls", "argv": ["terraform-ls", "serve"], "install": "brew install hashicorp/tap/terraform-ls"},
    ]},
    {"id": "dockerfile", "label": "Dockerfile", "monaco": ["dockerfile"], "extensions": [".dockerfile"], "servers": [
        {"name": "docker-langserver", "argv": ["docker-langserver", "--stdio"], "install": "npm install -g dockerfile-language-server-nodejs"},
    ]},
    {"id": "sql", "label": "SQL", "monaco": ["sql", "mysql", "pgsql"], "extensions": [".sql"], "servers": [
        {"name": "sqls", "argv": ["sqls"], "install": "go install github.com/sqls-server/sqls@latest"},
    ]},
    {"id": "html", "label": "HTML", "builtin": True, "monaco": ["html"], "extensions": [".html", ".htm"], "servers": [
        {"name": "vscode-html-language-server", "argv": ["vscode-html-language-server", "--stdio"], "install": "npm install -g vscode-langservers-extracted"},
    ]},
    {"id": "css", "label": "CSS / SCSS / Less", "builtin": True, "monaco": ["css", "scss", "less"], "extensions": [".css", ".scss", ".less"],
     "language_ids": {".scss": "scss", ".less": "less"}, "servers": [
        {"name": "vscode-css-language-server", "argv": ["vscode-css-language-server", "--stdio"], "install": "npm install -g vscode-langservers-extracted"},
    ]},
    {"id": "json", "label": "JSON", "builtin": True, "monaco": ["json"], "extensions": [".json", ".jsonc"], "servers": [
        {"name": "vscode-json-language-server", "argv": ["vscode-json-language-server", "--stdio"], "install": "npm install -g vscode-langservers-extracted"},
    ]},
    {"id": "markdown", "label": "Markdown", "builtin": True, "monaco": ["markdown"], "extensions": [".md", ".markdown"], "servers": [
        {"name": "marksman", "argv": ["marksman", "server"], "install": "brew install marksman"},
    ]},
]

_BY_ID = {lang["id"]: lang for lang in LANGUAGES}
_lock = threading.Lock()


class LspSettingsError(ValueError):
    pass


def language(lang_id: str) -> dict[str, Any] | None:
    return _BY_ID.get(str(lang_id or "").lower())


def language_for_path(path: str) -> dict[str, Any] | None:
    name = os.path.basename(str(path or "")).lower()
    if name == "dockerfile" or name.startswith("dockerfile."):
        return _BY_ID["dockerfile"]
    ext = os.path.splitext(name)[1]
    for lang in LANGUAGES:
        if ext in lang["extensions"]:
            return lang
    return None


def language_id_for_path(lang: dict[str, Any], path: str) -> str:
    """The LSP languageId for a file: per-extension when a server serves several languages."""
    ext = os.path.splitext(str(path or "").lower())[1]
    return (lang.get("language_ids") or {}).get(ext) or lang.get("default_language_id") or lang.get("language_id") or lang["id"]


def _read() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("languages"), dict):
        data["languages"] = {}
    return data


def _write(data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, CONFIG_PATH)


def _search_path() -> str:
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    dirs.append(os.path.dirname(sys.executable))
    for extra in _EXTRA_BIN_DIRS:
        dirs.append(os.path.expanduser(extra))
    seen: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


def find_program(name: str) -> str:
    if not name:
        return ""
    if os.sep in name:
        path = os.path.expanduser(name)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""
    return shutil.which(name, path=_search_path()) or ""


def _module_available(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _server_found(server: dict[str, Any]) -> str:
    if server.get("module"):
        return sys.executable if _module_available(server["module"]) else ""
    return find_program(server["argv"][0])


def _cache_dir(lang_id: str, project: str) -> str:
    digest = hashlib.sha1(os.path.abspath(project or ".").encode("utf-8")).hexdigest()[:12]
    base = os.path.basename(os.path.abspath(project or ".")) or "project"
    path = os.path.join(CACHE_ROOT, lang_id, f"{base}-{digest}")
    os.makedirs(path, exist_ok=True)
    return path


def _expand(argv: list[str], lang_id: str, project: str) -> list[str]:
    out = []
    for arg in argv:
        if "{cache}" in arg:
            arg = arg.replace("{cache}", _cache_dir(lang_id, project))
        out.append(arg.replace("{python}", sys.executable).replace("{project}", project or "."))
    if out and os.sep not in out[0]:
        out[0] = find_program(out[0]) or out[0]
    return out


def _pref(data: dict[str, Any], lang: dict[str, Any]) -> dict[str, Any]:
    raw = data["languages"].get(lang["id"])
    return raw if isinstance(raw, dict) else {}


def _enabled(data: dict[str, Any], lang: dict[str, Any]) -> bool:
    pref = _pref(data, lang)
    if "enabled" in pref:
        return bool(pref["enabled"])
    return not lang.get("builtin")


def master_enabled() -> bool:
    return _read().get("enabled", True) is not False


def resolve(lang_id: str, project: str = "") -> dict[str, Any]:
    """{argv, server, source} for the server to run, or {error} saying why there is none."""
    lang = language(lang_id)
    if lang is None:
        return {"error": f"No language server is known for {lang_id}."}
    data = _read()
    if data.get("enabled", True) is False:
        return {"error": "Language servers are turned off in Settings > Languages."}
    if not _enabled(data, lang):
        return {"error": f"The {lang['label']} language server is turned off in Settings > Languages."}
    command = str(_pref(data, lang).get("command") or "").strip()
    if command:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return {"error": f"The {lang['label']} server command is not valid: {exc}"}
        expanded = _expand(argv, lang["id"], project)
        if not find_program(expanded[0]):
            return {"error": f"{argv[0]} was not found. Check the command in Settings > Languages."}
        return {"argv": expanded, "server": os.path.basename(argv[0]), "source": "custom"}
    for server in lang["servers"]:
        if _server_found(server):
            return {"argv": _expand(server["argv"], lang["id"], project), "server": server["name"], "source": "auto"}
    hint = lang["servers"][0].get("install") or ""
    return {"error": f"No {lang['label']} language server is installed." + (f" Install one with: {hint}" if hint else "")}


def status() -> dict[str, Any]:
    data = _read()
    rows = []
    for lang in LANGUAGES:
        pref = _pref(data, lang)
        command = str(pref.get("command") or "")
        found = []
        for server in lang["servers"]:
            path = _server_found(server)
            if path:
                found.append({"name": server["name"], "path": path})
        active = resolve(lang["id"]) if _enabled(data, lang) and data.get("enabled", True) is not False else {}
        rows.append({
            "id": lang["id"],
            "label": lang["label"],
            "extensions": lang["extensions"],
            "monaco": lang["monaco"],
            "builtin": bool(lang.get("builtin")),
            "enabled": _enabled(data, lang),
            "command": command,
            "servers": [{"name": s["name"], "command": " ".join(s["argv"]), "install": s.get("install", "")} for s in lang["servers"]],
            "found": found,
            "active": active.get("server", ""),
            "active_source": active.get("source", ""),
            "problem": active.get("error", "") if command or found else "",
            "ready": bool(active.get("argv")),
        })
    return {"enabled": data.get("enabled", True) is not False, "languages": rows, "path": CONFIG_PATH}


def client_config() -> dict[str, Any]:
    """What the editor needs: for each language with a runnable server, its Monaco ids and extensions."""
    data = _read()
    out = []
    if data.get("enabled", True) is not False:
        for lang in LANGUAGES:
            if not _enabled(data, lang):
                continue
            active = resolve(lang["id"])
            if not active.get("argv"):
                continue
            out.append({
                "id": lang["id"],
                "label": lang["label"],
                "monaco": lang["monaco"],
                "extensions": lang["extensions"],
                "language_ids": lang.get("language_ids") or {},
                "language_id": lang.get("default_language_id") or lang.get("language_id") or lang["id"],
                "server": active["server"],
            })
    return {"languages": out}


def update(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise LspSettingsError("Send the settings as an object.")
    with _lock:
        data = _read()
        if "enabled" in body:
            data["enabled"] = bool(body["enabled"])
        langs = body.get("languages")
        if langs is not None:
            if not isinstance(langs, dict):
                raise LspSettingsError("languages is an object keyed by language id.")
            for lang_id, change in langs.items():
                lang = language(lang_id)
                if lang is None or not isinstance(change, dict):
                    raise LspSettingsError(f"Unknown language: {lang_id}")
                pref = dict(_pref(data, lang))
                if "enabled" in change:
                    pref["enabled"] = bool(change["enabled"])
                if "command" in change:
                    command = str(change["command"] or "").strip()
                    if len(command) > COMMAND_MAX_CHARS:
                        raise LspSettingsError(f"The command is at most {COMMAND_MAX_CHARS} characters.")
                    if command:
                        try:
                            shlex.split(command)
                        except ValueError as exc:
                            raise LspSettingsError(f"That command is not valid: {exc}") from None
                        pref["command"] = command
                    else:
                        pref.pop("command", None)
                if pref.get("enabled") == (not lang.get("builtin")):
                    pref.pop("enabled", None)
                if pref:
                    data["languages"][lang["id"]] = pref
                else:
                    data["languages"].pop(lang["id"], None)
        _write(data)
    return status()
