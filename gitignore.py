from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    base: str
    negate: bool
    dir_only: bool
    kind: str
    literal: str = ""
    regex: re.Pattern[str] | None = None


def _translate(glob: str) -> str:
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if glob.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = glob.find("]", i + 2)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1:end]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
                continue
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(glob[i + 1]))
            i += 2
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def parse_line(raw: str, base: str = "") -> Rule | None:
    line = raw.rstrip("\r\n")
    stripped = line.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped = stripped[:-1] + " "
    if not stripped or stripped.startswith("#"):
        return None
    negate = stripped.startswith("!")
    if negate:
        stripped = stripped[1:]
    elif stripped[:2] in ("\\#", "\\!"):
        stripped = stripped[1:]
    dir_only = stripped.endswith("/")
    stripped = stripped.rstrip("/")
    anchored = "/" in stripped
    stripped = stripped.lstrip("/")
    if not stripped:
        return None
    if not anchored and not any(ch in stripped for ch in "*?[\\"):
        return Rule(base, negate, dir_only, "name", literal=stripped)
    if not anchored and stripped.startswith("*") and not any(ch in stripped[1:] for ch in "*?[\\"):
        return Rule(base, negate, dir_only, "suffix", literal=stripped[1:])
    try:
        regex = re.compile(("^" if anchored else "^(?:.*/)?") + _translate(stripped) + "$")
    except re.error:
        return None
    return Rule(base, negate, dir_only, "regex", regex=regex)


def parse_rules(text: str, base: str = "") -> list[Rule]:
    rules = []
    for raw in (text or "").splitlines():
        rule = parse_line(raw, base)
        if rule is not None:
            rules.append(rule)
    return rules


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


class ProjectIgnore:

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))
        self._rules: list[Rule] = []
        self._loaded: set[str] = set()
        self._fast: dict[str, tuple] | None = None
        self._dirty = True
        self._add(parse_rules(_read(os.path.join(self.root, ".git", "info", "exclude"))))
        self.enter_dir("")

    def _add(self, rules: list[Rule]) -> None:
        if rules:
            self._rules.extend(rules)
            self._dirty = True

    def enter_dir(self, rel_dir: str) -> None:
        rel_dir = rel_dir.replace("\\", "/").strip("/")
        if rel_dir in self._loaded:
            return
        self._loaded.add(rel_dir)
        self._add(parse_rules(_read(os.path.join(self.root, rel_dir, ".gitignore")), rel_dir))

    def _compile(self) -> None:
        self._dirty = False
        if any(rule.negate for rule in self._rules):
            self._fast = None
            return
        fast: dict[str, tuple[set[str], set[str], list[str], list[str], list[tuple[re.Pattern[str], bool]]]] = {}
        for rule in self._rules:
            names, dir_names, suffixes, dir_suffixes, regexes = fast.setdefault(rule.base, (set(), set(), [], [], []))
            if rule.kind == "name":
                (dir_names if rule.dir_only else names).add(rule.literal)
            elif rule.kind == "suffix":
                (dir_suffixes if rule.dir_only else suffixes).append(rule.literal)
            elif rule.regex is not None:
                regexes.append((rule.regex, rule.dir_only))
        self._fast = {
            base: (names, dir_names, tuple(suffixes), tuple(dir_suffixes), regexes)
            for base, (names, dir_names, suffixes, dir_suffixes, regexes) in fast.items()
        }

    def match(self, rel: str, is_dir: bool) -> bool | None:
        if self._dirty:
            self._compile()
        rel = rel.replace("\\", "/").strip("/")
        name = rel.rsplit("/", 1)[-1]
        if self._fast is not None:
            for base, (names, dir_names, suffixes, dir_suffixes, regexes) in self._fast.items():
                if base:
                    if not rel.startswith(base + "/"):
                        continue
                    sub = rel[len(base) + 1:]
                else:
                    sub = rel
                if name in names or (is_dir and name in dir_names):
                    return True
                if (suffixes and name.endswith(suffixes)) or (is_dir and dir_suffixes and name.endswith(dir_suffixes)):
                    return True
                for regex, dir_only in regexes:
                    if (is_dir or not dir_only) and regex.match(sub):
                        return True
            return None
        verdict: bool | None = None
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.base:
                if not rel.startswith(rule.base + "/"):
                    continue
                sub = rel[len(rule.base) + 1:]
            else:
                sub = rel
            if rule.kind == "name":
                hit = name == rule.literal
            elif rule.kind == "suffix":
                hit = name.endswith(rule.literal)
            else:
                hit = bool(rule.regex and rule.regex.match(sub))
            if hit:
                verdict = not rule.negate
        return verdict

    def sources(self) -> list[str]:
        paths = [".git/info/exclude"]
        for rel_dir in sorted(self._loaded):
            paths.append(f"{rel_dir}/.gitignore" if rel_dir else ".gitignore")
        return paths

    def is_ignored(self, rel: str, is_dir: bool = False) -> bool:
        return self.ignored_path(rel, is_dir)

    def ignored_entry(self, rel: str, is_dir: bool) -> bool:
        return bool(self.match(rel, is_dir))

    def ignored_path(self, rel: str, is_dir: bool = False) -> bool:
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        for k in range(len(parts)):
            self.enter_dir("/".join(parts[:k]))
        for k in range(1, len(parts)):
            if self.match("/".join(parts[:k]), True):
                return True
        return bool(parts) and bool(self.match("/".join(parts), is_dir))
