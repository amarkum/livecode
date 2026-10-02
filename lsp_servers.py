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
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Any

CONFIG_PATH = os.path.expanduser("~/.livecode/lsp.json")
CACHE_ROOT = os.path.expanduser("~/.livecode/lsp-cache")
COMMAND_MAX_CHARS = 1000

# Where servers usually land when the server process's PATH is narrower than a login shell's.
_EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/homebrew/opt/llvm/bin",
    "/usr/local/opt/llvm/bin",
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
        {"name": "metals", "argv": ["metals"], "install": "brew install metals", "installs": ["brew install metals", "coursier install metals"]},
    ]},
    {"id": "go", "label": "Go", "monaco": ["go"], "extensions": [".go"], "servers": [
        {"name": "gopls", "argv": ["gopls"], "install": "brew install gopls", "installs": ["brew install gopls", "go install golang.org/x/tools/gopls@latest"]},
    ]},
    {"id": "rust", "label": "Rust", "monaco": ["rust"], "extensions": [".rs"], "servers": [
        {"name": "rust-analyzer", "argv": ["rust-analyzer"], "install": "brew install rust-analyzer", "installs": ["brew install rust-analyzer", "rustup component add rust-analyzer"]},
    ]},
    {"id": "cpp", "label": "C / C++ / Objective-C", "monaco": ["c", "cpp", "objective-c"],
     "extensions": [".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".ino", ".m", ".mm"],
     "language_ids": {".c": "c", ".h": "c", ".m": "objective-c", ".mm": "objective-cpp"},
     "default_language_id": "cpp",
     "servers": [{"name": "clangd", "argv": ["clangd", "--background-index"], "install": "brew install llvm"}]},
    {"id": "csharp", "label": "C#", "monaco": ["csharp"], "extensions": [".cs", ".csx"], "servers": [
        {"name": "csharp-ls", "argv": ["csharp-ls"], "install": "dotnet tool install --global csharp-ls"},
        {"name": "OmniSharp", "argv": ["OmniSharp", "-lsp"], "install": "brew install omnisharp/omnisharp-roslyn/omnisharp-mono"},
    ]},
    {"id": "swift", "label": "Swift", "monaco": ["swift"], "extensions": [".swift"], "servers": [
        {"name": "sourcekit-lsp", "argv": ["sourcekit-lsp"], "install": "Comes with Xcode or the Swift toolchain"},
    ]},
    {"id": "ruby", "label": "Ruby", "monaco": ["ruby"], "extensions": [".rb", ".rake", ".gemspec", ".ru"], "servers": [
        {"name": "ruby-lsp", "argv": ["ruby-lsp"], "install": "brew install ruby-lsp", "installs": ["brew install ruby-lsp", "gem install ruby-lsp"]},
        {"name": "solargraph", "argv": ["solargraph", "stdio"], "install": "brew install solargraph", "installs": ["brew install solargraph", "gem install solargraph"]},
    ]},
    {"id": "php", "label": "PHP", "monaco": ["php"], "extensions": [".php", ".phtml"], "servers": [
        {"name": "intelephense", "argv": ["intelephense", "--stdio"], "install": "npm install -g intelephense"},
        {"name": "phpactor", "argv": ["phpactor", "language-server"], "install": "See phpactor.readthedocs.io"},
    ]},
    {"id": "dart", "label": "Dart / Flutter", "monaco": ["dart"], "extensions": [".dart"], "servers": [
        {"name": "dart", "argv": ["dart", "language-server", "--protocol=lsp"], "install": "brew install dart-lang/dart/dart (or install Flutter)"},
    ]},
    {"id": "lua", "label": "Lua", "monaco": ["lua"], "extensions": [".lua"], "servers": [
        {"name": "lua-language-server", "argv": ["lua-language-server"], "install": "brew install lua-language-server"},
    ]},
    {"id": "elixir", "label": "Elixir", "monaco": ["elixir"], "extensions": [".ex", ".exs"], "servers": [
        {"name": "elixir-ls", "argv": ["elixir-ls"], "install": "brew install elixir-ls"},
        {"name": "lexical", "argv": ["lexical"], "install": "See github.com/lexical-lsp/lexical"},
    ]},
    {"id": "haskell", "label": "Haskell", "monaco": [], "extensions": [".hs", ".lhs"], "servers": [
        {"name": "haskell-language-server", "argv": ["haskell-language-server-wrapper", "--lsp"], "install": "brew install haskell-language-server", "installs": ["brew install haskell-language-server", "ghcup install hls"]},
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
            "servers": [{"name": s["name"], "command": " ".join(s["argv"]), "install": s.get("install", ""), **setup_info(s)} for s in lang["servers"]],
            "lint_plugins_missing": lang["id"] == "python" and _pylsp_without_linters(found),
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


# ---------------------------------------------------------------- one-click setup

# Package managers an install hint may start with; anything else (prose, links) is shown, not run.
_INSTALLERS = {
    "pip": "Python's pip", "pipx": "pipx", "npm": "Node.js (npm)", "brew": "Homebrew", "go": "Go",
    "rustup": "rustup", "gem": "Ruby (gem)", "dotnet": ".NET SDK", "coursier": "Coursier", "ghcup": "GHCup",
}
# A missing package manager that Homebrew can install first, so Install still works in one click.
_PREREQS = {
    "npm": ["brew", "install", "node"],
    "go": ["brew", "install", "go"],
    "pipx": ["brew", "install", "pipx"],
    "coursier": ["brew", "install", "coursier"],
    "ghcup": ["brew", "install", "ghcup"],
}
INSTALL_TIMEOUT_S = 1800
_LOG_LINES = 400
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _install_hints(server: dict[str, Any]) -> list[str]:
    return list(server.get("installs") or [server.get("install") or ""])


def _hint_argv(hint: str) -> list[str] | None:
    try:
        argv = shlex.split(str(hint or "").split("(")[0].strip())
    except ValueError:
        return None
    if not argv or argv[0] not in _INSTALLERS:
        return None
    if argv[0] == "pip":
        # Into the interpreter LiveCode runs on, so {python} -m pylsp finds it.
        return [sys.executable, "-m", "pip", "install", "--upgrade", *argv[2:]] if argv[1:2] == ["install"] else None
    return argv


def _tool_present(tool: str) -> bool:
    return os.sep in tool or bool(find_program(tool)) or (tool == "coursier" and bool(find_program("cs")))


def setup_steps(server: dict[str, Any]) -> tuple[list[list[str]], str]:
    """The commands that install a server, in order, and what is missing when none can run here.

    The first install option whose package manager is present wins; otherwise one whose package
    manager Homebrew can install first. ([], "") means it cannot be automated at all."""
    options = [argv for argv in (_hint_argv(h) for h in _install_hints(server)) if argv]
    if not options:
        return [], ""
    for argv in options:
        if _tool_present(argv[0]):
            return [argv], ""
    if find_program("brew"):
        for argv in options:
            if argv[0] in _PREREQS:
                return [list(_PREREQS[argv[0]]), argv], ""
    return [], _INSTALLERS.get(options[0][0], options[0][0])


def setup_command(server: dict[str, Any]) -> list[str] | None:
    """The last (main) install command for a server, or None when it cannot be automated."""
    steps, _ = setup_steps(server)
    return steps[-1] if steps else None


def setup_info(server: dict[str, Any]) -> dict[str, Any]:
    steps, needs = setup_steps(server)
    if not steps and not needs:
        return {"setup": None}
    return {"setup": " && ".join(" ".join(shlex.quote(a) for a in argv) for argv in steps) or None, "setup_needs": needs}


def _pylsp_without_linters(found: list[dict[str, Any]]) -> bool:
    # pylsp runs without pyflakes/pycodestyle but then never reports errors.
    return any(f["name"] == "pylsp" for f in found) and not _module_available("pyflakes")


_LINT_SETUP = [sys.executable, "-m", "pip", "install", "--upgrade", "python-lsp-server[all]"]


def start_install(lang_id: str, server_name: str = "", lint_plugins: bool = False) -> dict[str, Any]:
    lang = language(lang_id)
    if lang is None:
        raise LspSettingsError(f"Unknown language: {lang_id}")
    if lint_plugins:
        if lang["id"] != "python":
            raise LspSettingsError("Linting plugins are for Python.")
        server, steps = {"name": "python-lsp-server[all]"}, [list(_LINT_SETUP)]
    else:
        candidates = [sv for sv in lang["servers"] if setup_steps(sv)[0] or setup_steps(sv)[1]]
        if server_name:
            candidates = [sv for sv in candidates if sv["name"] == server_name] or candidates
        if not candidates:
            raise LspSettingsError(f"{lang['label']} can't be installed automatically: {lang['servers'][0].get('install', '')}")
        # Prefer a server that installs with one command, then one that needs a package manager first.
        ready = [sv for sv in candidates if len(setup_steps(sv)[0]) == 1]
        server = (ready or candidates)[0]
        steps, needs = setup_steps(server)
        if not steps:
            raise LspSettingsError(f"Installing {lang['label']} needs {needs}, which isn't on this machine.")
    steps = [list(argv) for argv in steps]
    for argv in steps:
        if argv[0] == "coursier" and not find_program("coursier"):
            argv[0] = "cs"
    if not lint_plugins and not _enabled(_read(), lang):
        # Installing a language is asking to use it.
        update({"languages": {lang["id"]: {"enabled": True}}})
    with _jobs_lock:
        for job in _jobs.values():
            if job["language"] == lang["id"] and job["status"] == "running":
                return _job_view(job)
        job = {
            "id": uuid.uuid4().hex[:12], "language": lang["id"], "label": lang["label"], "server": server["name"],
            "command": " && ".join(" ".join(shlex.quote(a) for a in argv) for argv in steps), "status": "running", "returncode": None,
            "started": time.time(), "finished": None, "log": deque(maxlen=_LOG_LINES),
        }
        _jobs[job["id"]] = job
    threading.Thread(target=_run_install, args=(job, steps), name=f"lsp-install-{lang['id']}", daemon=True).start()
    return _job_view(job)


def _run_install(job: dict[str, Any], steps: list[list[str]]) -> None:
    env = os.environ.copy()
    env.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
    env.setdefault("NONINTERACTIVE", "1")
    deadline = time.time() + INSTALL_TIMEOUT_S
    code = 0
    for argv in steps:
        env["PATH"] = _search_path()
        job["log"].append("$ " + " ".join(shlex.quote(a) for a in argv))
        try:
            proc = subprocess.Popen(
                [find_program(argv[0]) or argv[0], *argv[1:]],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                cwd=os.path.expanduser("~"), start_new_session=True,
            )
        except OSError as exc:
            job["log"].append(f"Could not start {argv[0]}: {exc.strerror or exc}")
            job.update(status="failed", returncode=-1, finished=time.time())
            return
        timer = threading.Timer(max(1.0, deadline - time.time()), proc.kill)
        timer.daemon = True
        timer.start()
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    job["log"].append(line[:500])
            code = proc.wait()
        finally:
            timer.cancel()
        if time.time() >= deadline:
            job["log"].append(f"Stopped after {INSTALL_TIMEOUT_S // 60} minutes.")
        if code != 0:
            break
    ok = code == 0 and (job["server"] == "python-lsp-server[all]" or bool(resolve(job["language"]).get("argv")))
    if code == 0 and not ok:
        job["log"].append("The installer finished, but the server was not found on LiveCode's search path. "
                          "Set its full path as the command in Settings > Languages.")
    job.update(status="ok" if ok else "failed", returncode=code, finished=time.time())


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {k: (list(v) if isinstance(v, deque) else v) for k, v in job.items()}


def install_status(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(str(job_id or ""))
        return _job_view(job) if job else None
