"""Standalone host helpers (repo search/read/list, AST symbols, diff HTML)."""
from __future__ import annotations

import difflib
import html
import json
import os
import re
import subprocess


def _grep_command_available():
    import shutil

    if not hasattr(_grep_command_available, "_cached"):
        if shutil.which("rg"):
            _grep_command_available._cached = "rg"
        else:
            _grep_command_available._cached = "git_grep"
    return _grep_command_available._cached


def _repo_grep(repo_path, pattern, glob_filter=None, max_results=60, directory=None):
    import subprocess as _sp

    try:
        cap = min(max(int(max_results or 60), 1), 100)
        engine = _grep_command_available()

        search_root = os.path.abspath(os.path.expanduser(repo_path))
        if directory:
            safe_rel = os.path.normpath(str(directory).lstrip("/"))
            if ".." in safe_rel.split(os.sep):
                return {"error": "Path traversal not allowed"}
            candidate = (
                os.path.join(search_root, safe_rel)
                if safe_rel and safe_rel != "."
                else search_root
            )
            if not os.path.isdir(candidate):
                return {"error": f"Directory not found: {directory}"}
            search_root = candidate

        if engine == "rg":

            cmd = [
                "rg",
                "--no-heading",
                "--line-number",
                "--color",
                "never",
                "-m",
                str(cap),
                "--max-filesize",
                "2M",
            ]
            if glob_filter:
                cmd += ["--glob", glob_filter]
            cmd += ["--", pattern, search_root]
            cwd = None
        else:
            cmd = ["git", "grep", "-n", "--no-color", "-I", "-E", pattern]
            if glob_filter:
                clean = glob_filter.lstrip("*").lstrip("/")
                cmd += ["--", clean]
            if directory:
                safe_rel = os.path.normpath(str(directory).lstrip("/"))
                if safe_rel and safe_rel != ".":
                    cmd += [safe_rel]
            cwd = repo_path

        result = _sp.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)
        lines = result.stdout.strip().splitlines() if result.stdout else []

        matches = []
        total_chars = 0
        char_cap = 12000
        for raw in lines[:cap]:
            parts = raw.split(":", 2)
            if len(parts) < 3:
                continue
            fpath, lineno, content = parts[0], parts[1], parts[2]
            rel = os.path.relpath(fpath, repo_path) if engine == "rg" else fpath
            entry = {"file": rel, "line": int(lineno), "content": content.rstrip()}
            entry_len = len(rel) + len(content) + 20
            if total_chars + entry_len > char_cap:
                break
            matches.append(entry)
            total_chars += entry_len

        out = {
            "success": True,
            "pattern": pattern,
            "match_count": len(matches),
            "truncated": len(lines) > len(matches),
            "matches": matches,
        }
        if directory:
            out["directory"] = str(directory).strip()
        if glob_filter:
            out["glob_filter"] = glob_filter
        return out
    except _sp.TimeoutExpired:
        return {
            "error": "grep timed out (>30s). Try a more specific pattern, directory, or glob filter."
        }
    except Exception as e:
        return {"error": str(e)}




def _repo_read_file(repo_path, file_path, start_line=None, end_line=None):
    try:
        safe_rel = os.path.normpath(file_path).lstrip("/")
        if ".." in safe_rel.split(os.sep):
            return {"error": "Path traversal not allowed"}
        full = os.path.join(repo_path, safe_rel)
        if not os.path.isfile(full):
            return {"error": f"File not found: {safe_rel}"}

        with open(full, "r", errors="replace") as f:
            all_lines = f.readlines()

        total = len(all_lines)
        s = max(1, start_line or 1)
        e = min(total, end_line or (s + 299))
        e = min(e, s + 299)

        numbered = []
        for i in range(s - 1, e):
            numbered.append(f"{i + 1:>5}| {all_lines[i].rstrip()}")

        return {
            "success": True,
            "file": safe_rel,
            "total_lines": total,
            "showing": f"{s}-{e}",
            "content": "\n".join(numbered),
        }
    except Exception as e:
        return {"error": str(e)}




def _repo_list_dir(repo_path, directory=""):
    try:
        safe_rel = os.path.normpath(directory or ".").lstrip("/")
        if ".." in safe_rel.split(os.sep):
            return {"error": "Path traversal not allowed"}
        target = os.path.join(repo_path, safe_rel) if safe_rel != "." else repo_path
        if not os.path.isdir(target):
            return {"error": f"Directory not found: {safe_rel}"}

        dirs, files = [], []
        try:
            entries = sorted(os.listdir(target))
        except PermissionError:
            return {"error": "Permission denied"}

        for name in entries[:200]:
            if name.startswith("."):
                continue
            full = os.path.join(target, name)
            if os.path.isdir(full):
                dirs.append(name + "/")
            else:
                files.append(name)

        return {
            "success": True,
            "directory": safe_rel if safe_rel != "." else "/",
            "dirs": dirs,
            "files": files,
            "total": len(dirs) + len(files),
        }
    except Exception as e:
        return {"error": str(e)}




def _repo_ast_symbols(repo_path, file_path):
    import ast as _ast

    try:
        safe_rel = os.path.normpath(file_path).lstrip("/")
        if ".." in safe_rel.split(os.sep):
            return {"error": "Path traversal not allowed"}
        full = os.path.join(repo_path, safe_rel)
        if not os.path.isfile(full):
            return {"error": f"File not found: {safe_rel}"}
        if not safe_rel.endswith(".py"):
            return {"error": "ast_symbols only works on .py files"}

        with open(full, "r", errors="replace") as f:
            source = f.read()

        try:
            tree = _ast.parse(source, filename=safe_rel)
        except SyntaxError as se:
            return {"error": f"SyntaxError at line {se.lineno}: {se.msg}"}

        classes, functions, imports = [], [], []
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, _ast.ClassDef):
                methods = [
                    {"name": m.name, "line": m.lineno}
                    for m in _ast.iter_child_nodes(node)
                    if isinstance(m, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                ]
                classes.append(
                    {"name": node.name, "line": node.lineno, "methods": methods}
                )
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                functions.append({"name": node.name, "line": node.lineno})
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    imports.append(
                        {
                            "module": alias.name,
                            "alias": alias.asname,
                            "line": node.lineno,
                        }
                    )
            elif isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    imports.append(
                        {
                            "module": f"{mod}.{alias.name}",
                            "alias": alias.asname,
                            "line": node.lineno,
                        }
                    )

        return {
            "success": True,
            "file": safe_rel,
            "classes": classes,
            "functions": functions,
            "imports": imports,
        }
    except Exception as e:
        return {"error": str(e)}


def create_diff_html(
    original_content,
    new_content,
    file_ext,
    csv_delimiter=None,
    max_blocks=50,
    context_lines=0,
):
    import difflib
    import html

    original_lines = original_content.splitlines()
    new_lines = new_content.splitlines()
    ctx_n = max(0, int(context_lines or 0))

    def diff_gutter_bar(kind):
        return f'<span class="diff-line-gutter-bar diff-line-gutter-bar-{kind}" aria-hidden="true"></span>'

    def create_context_line(line_num, content):
        line_num_str = f'<span class="diff-line-number-inline">{line_num}</span>'
        empty_sign = '<span class="diff-line-sign diff-line-sign-empty" aria-hidden="true"></span>'
        content_html = html.escape(content)
        return (
            f'<div class="diff-line-wrapper diff-line-context-wrapper">'
            f'{diff_gutter_bar("context")}{line_num_str}{empty_sign}'
            f'<span class="diff-line-content diff-line-context">{content_html}</span></div>'
        )

    def emit_context_before(orig_start_idx):
        if ctx_n <= 0 or orig_start_idx <= 0:
            return []
        out = []
        start = max(0, orig_start_idx - ctx_n)
        for idx in range(start, orig_start_idx):
            out.append(create_context_line(idx + 1, original_lines[idx]))
        return out

    def emit_context_after(new_end_idx):
        if ctx_n <= 0 or new_end_idx >= len(new_lines):
            return []
        out = []
        end = min(len(new_lines), new_end_idx + ctx_n)
        for idx in range(new_end_idx, end):
            out.append(create_context_line(idx + 1, new_lines[idx]))
        return out

    def create_inline_diff_line(old_line, new_line, line_num=None):
        line_num_str = (
            f'<span class="diff-line-number-inline">{line_num}</span>'
            if line_num
            else '<span class="diff-line-number-inline diff-line-number-empty"></span>'
        )

        if old_line is None and new_line is not None:
            sign_str = '<span class="diff-line-sign diff-line-sign-added">+</span>'
            new_html = html.escape(new_line)
            return f'<div class="diff-line-wrapper diff-line-added-wrapper">{diff_gutter_bar("added")}{line_num_str}{sign_str}<span class="diff-line-content diff-line-added">{new_html}</span></div>'

        if new_line is None and old_line is not None:
            sign_str = '<span class="diff-line-sign diff-line-sign-deleted">-</span>'
            old_html = html.escape(old_line)
            return f'<div class="diff-line-wrapper diff-line-deleted-wrapper">{diff_gutter_bar("deleted")}{line_num_str}{sign_str}<span class="diff-line-content diff-line-deleted">{old_html}</span></div>'

        sign_del = '<span class="diff-line-sign diff-line-sign-deleted">-</span>'
        sign_add = '<span class="diff-line-sign diff-line-sign-added">+</span>'
        old_html = html.escape(old_line)
        new_html = html.escape(new_line)
        return f'<div class="diff-line-wrapper diff-line-changed-wrapper"><div class="diff-line-row diff-line-deleted-row">{diff_gutter_bar("deleted")}{line_num_str}{sign_del}<span class="diff-line-content diff-line-deleted">{old_html}</span></div><div class="diff-line-row diff-line-added-row">{diff_gutter_bar("added")}{line_num_str}{sign_add}<span class="diff-line-content diff-line-added">{new_html}</span></div></div>'

    html_parts = []
    max_lines_to_show = max_blocks

    matcher = difflib.SequenceMatcher(None, original_lines, new_lines, autojunk=False)
    additions = 0
    deletions = 0

    change_blocks = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":

            deleted_block = []
            for idx in range(i1, i2):
                deleted_block.append((idx + 1, original_lines[idx]))
                deletions += 1

            if deleted_block:
                change_blocks.append(
                    {
                        "type": "delete",
                        "start_line": deleted_block[0][0],
                        "end_line": i2,
                        "orig_start_idx": i1,
                        "orig_end_idx": i2,
                        "new_end_idx": j2,
                        "lines": deleted_block,
                    }
                )
        elif tag == "insert":

            inserted_block = []
            for idx in range(j1, j2):

                line_num = i1 + 1 + (idx - j1)
                inserted_block.append((line_num, new_lines[idx]))
                additions += 1

            if inserted_block:
                change_blocks.append(
                    {
                        "type": "insert",
                        "start_line": i1 + 1,
                        "end_line": i1 + 1,
                        "orig_start_idx": i1,
                        "new_end_idx": j2,
                        "lines": inserted_block,
                    }
                )
        elif tag == "replace":

            deleted_block = []
            inserted_block = []

            for idx in range(i1, i2):
                deleted_block.append((idx + 1, original_lines[idx]))
                deletions += 1
            for idx in range(j1, j2):

                line_num = i1 + 1 + (idx - j1)
                inserted_block.append((line_num, new_lines[idx]))
                additions += 1

            if deleted_block or inserted_block:
                change_blocks.append(
                    {
                        "type": "replace",
                        "start_line": deleted_block[0][0] if deleted_block else i1 + 1,
                        "end_line": i2,
                        "orig_start_idx": i1,
                        "orig_end_idx": i2,
                        "new_end_idx": j2,
                        "deleted_lines": deleted_block,
                        "inserted_lines": inserted_block,
                    }
                )

    if not change_blocks:
        html_parts.append(
            '<span style="color: #94a3b8; font-family: monospace;">No changes detected</span><br>'
        )
    else:
        rendered_count = 0
        for block in change_blocks:
            if max_lines_to_show is not None and rendered_count >= max_lines_to_show:
                remaining = len(change_blocks) - change_blocks.index(block)
                html_parts.append(
                    f'<div class="diff-overflow-message">... ({remaining} more change blocks not shown)</div>'
                )
                break

            if block["type"] == "delete":

                orig_start = block.get("orig_start_idx", block["start_line"] - 1)
                new_end = block.get("new_end_idx", block["end_line"])
                html_parts.append(
                    f'<div class="diff-block-wrapper" data-start-line="{block["start_line"]}" data-end-line="{block["end_line"]}">'
                )
                for ctx_line in emit_context_before(orig_start):
                    html_parts.append(ctx_line)
                for line_num, line_content in block["lines"]:
                    html_parts.append(
                        create_inline_diff_line(line_content, None, line_num)
                    )
                    rendered_count += 1
                for ctx_line in emit_context_after(new_end):
                    html_parts.append(ctx_line)
                html_parts.append("</div>")

            elif block["type"] == "insert":

                orig_start = block.get("orig_start_idx", block["start_line"] - 1)
                new_end = block.get("new_end_idx", orig_start + len(block["lines"]))
                html_parts.append(
                    f'<div class="diff-block-wrapper" data-start-line="{block["start_line"]}">'
                )
                for ctx_line in emit_context_before(orig_start):
                    html_parts.append(ctx_line)
                for line_num, line_content in block["lines"]:
                    html_parts.append(
                        create_inline_diff_line(None, line_content, line_num)
                    )
                    rendered_count += 1
                for ctx_line in emit_context_after(new_end):
                    html_parts.append(ctx_line)
                html_parts.append("</div>")

            elif block["type"] == "replace":

                orig_start = block.get("orig_start_idx", block["start_line"] - 1)
                new_end = block.get("new_end_idx", block["end_line"])
                html_parts.append(
                    f'<div class="diff-block-wrapper" data-start-line="{block["start_line"]}" data-end-line="{block["end_line"]}">'
                )
                for ctx_line in emit_context_before(orig_start):
                    html_parts.append(ctx_line)

                if block["deleted_lines"]:
                    for line_num, line_content in block["deleted_lines"]:
                        html_parts.append(
                            create_inline_diff_line(line_content, None, line_num)
                        )
                        rendered_count += 1

                if block["inserted_lines"]:
                    for line_num, line_content in block["inserted_lines"]:
                        html_parts.append(
                            create_inline_diff_line(None, line_content, line_num)
                        )
                        rendered_count += 1
                for ctx_line in emit_context_after(new_end):
                    html_parts.append(ctx_line)
                html_parts.append("</div>")

    diff_html = "".join(html_parts)

    diff = difflib.unified_diff(
        original_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile="Original",
        tofile="Modified",
        lineterm="",
    )
    diff_text = "".join(diff)

    changes_count = len(
        [
            line
            for line in diff_text.split("\n")
            if line.startswith("+") and not line.startswith("+++")
        ]
    )
    deletions_count = len(
        [
            line
            for line in diff_text.split("\n")
            if line.startswith("-") and not line.startswith("---")
        ]
    )

    return diff_html, diff_text, changes_count, deletions_count
