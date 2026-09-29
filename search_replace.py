from __future__ import annotations

import difflib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from typing import Callable

ERROR_MULTIPLE_MATCHES = (
    "The string to replace was found multiple times in the file. "
    "Use replace_all to replace all occurrences, or include more context to only edit one occurrence."
)
ERROR_NO_MATCHES_BASE = (
    "The string to replace was not found in the file, use the read_repo_file tool to see the "
    "correct string."
)
ERROR_USER_EDIT_HINT = " The user may have changed the file since you last read it."
ERROR_UNREAD_HINT = (
    " You have not read this file this turn — call read_repo_file first."
)
ERROR_NO_MATCHES = ERROR_NO_MATCHES_BASE + ERROR_USER_EDIT_HINT
ERROR_SAME_STRING = "Old string and new string are the same"
ERROR_FILE_ALREADY_EXISTS = (
    "File already exists and is not empty. An empty old_string is only allowed "
    "when creating a new file or when the file is empty."
)
ERROR_WHITESPACE_HINT = (
    " If a nearest-match line is shown, copy its exact leading whitespace into old_string "
    "(sibling files may differ by a few spaces)."
)
ERROR_NO_MATCHES_RECOVERY_HINT = (
    "Re-read the target line range with read_repo_file, then retry edit_file using an exact "
    "old_string copied from that file. Do not reuse snippets from sibling files or include "
    "LINE_NUMBER| prefixes."
)

NAME_MAX = 255
CONTEXT_LINES = 3

_READ_LINE_PREFIX_RE = re.compile(r"^(?:\s*\d+\|\s|\d+→)")

_NEAREST_HINT_MAX = 200

@dataclass
class SearchReplaceParams:

    empty_old_string_does_not_override: bool = False
    include_user_edit_hint: bool = True

@dataclass(frozen=True)
class LineRange:

    start_line: int
    end_line: int

def truncate_str_with_marker(s: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    if max_len == 1:
        return "…"
    return s[: max_len - 1] + "…"

def find_match_positions(text: str, old_string: str) -> list[int]:
    if not old_string:
        return []
    positions: list[int] = []
    start = 0
    while True:
        idx = text.find(old_string, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + len(old_string)
    return positions

def replace_using_positions(
    text: str,
    positions: list[int],
    old_string: str,
    new_string: str,
) -> str:
    if not positions:
        return text
    parts: list[str] = []
    last_end = 0
    for pos in positions:
        parts.append(text[last_end:pos])
        parts.append(new_string)
        last_end = pos + len(old_string)
    parts.append(text[last_end:])
    return "".join(parts)

def strip_read_line_prefixes(s: str) -> str:
    if not s:
        return s
    lines = s.split("\n")
    stripped = [_READ_LINE_PREFIX_RE.sub("", line) for line in lines]
    return "\n".join(stripped)

def compute_line_range(text: str, start_pos: int, inserted_text: str) -> LineRange:
    start_line = text[:start_pos].count("\n")
    if inserted_text.endswith("\n"):
        lines_in_inserted = inserted_text.count("\n")
    elif inserted_text:
        lines_in_inserted = inserted_text.count("\n") + 1
    else:
        lines_in_inserted = 1
    end_line = start_line + lines_in_inserted - 1
    return LineRange(start_line=start_line, end_line=end_line)

def render_snippet(
    new_text: str,
    new_string: str,
    start_pos: int,
    context_size: int = CONTEXT_LINES,
) -> tuple[str, str, str]:
    line_range = compute_line_range(new_text, start_pos, new_string)
    lines = new_text.splitlines(keepends=True)
    if not lines and new_text == "":
        lines = [""]
    total = len(lines)
    start_line = line_range.start_line
    end_line = min(line_range.end_line, max(0, total - 1))
    snippet_start = max(0, start_line - context_size)
    snippet_end = min(total - 1, end_line + context_size) if total else 0

    before_context = "".join(lines[snippet_start:start_line]) if snippet_start < start_line else ""
    after_context = (
        "".join(lines[end_line + 1 : snippet_end + 1]) if end_line < snippet_end else ""
    )
    snippet_parts: list[str] = []
    for i in range(snippet_start, snippet_end + 1 if total else 0):
        snippet_parts.append(f"{i + 1}→{lines[i]}")
    return "".join(snippet_parts), before_context, after_context

def build_edit_details(
    new_text: str,
    old_string: str,
    new_string: str,
    new_positions: list[int],
    context_lines: int = CONTEXT_LINES,
) -> list[dict]:
    details: list[dict] = []
    for start_pos in new_positions:
        _snippet, context_before, context_after = render_snippet(
            new_text, new_string, start_pos, context_lines
        )
        line_range = compute_line_range(new_text, start_pos, new_string)
        line_start = new_text.rfind("\n", 0, start_pos)
        line_start = 0 if line_start < 0 else line_start + 1
        line_prefix = new_text[line_start:start_pos]
        details.append({
            "old_string": old_string,
            "old_line": line_range.start_line + 1,
            "new_string": new_string,
            "new_line": line_range.start_line + 1,
            "context_before": context_before,
            "context_after": context_after,
            "line_prefix": line_prefix,
        })
    return details

def read_text_preserving(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="surrogateescape")


def display_text(text: str) -> str:
    return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


_TEMP_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_temp_beside(directory: str) -> tuple[int, str]:
    for _ in range(100):
        tmp = os.path.join(directory, f".livecode-{uuid.uuid4().hex[:12]}.tmp")
        try:
            return os.open(tmp, _TEMP_OPEN_FLAGS, 0o666), tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create a temporary file in {directory}")


def write_text_atomic(path: str, text: str, *, new_file_mode: int | None = None) -> None:
    target = os.path.realpath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        st = os.stat(target)
    except FileNotFoundError:
        st = None
    data = text.encode("utf-8", errors="surrogateescape")
    fd, tmp = _open_temp_beside(directory)
    try:
        mode = stat.S_IMODE(st.st_mode) if st is not None else new_file_mode
        if mode is not None:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            else:
                os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if st is not None and hasattr(os, "chown"):
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_CLOSEST_BLOCK_MIN_LINES = 2
_CLOSEST_BLOCK_MIN_RATIO = 0.6
_CLOSEST_BLOCK_MAX_LINES = 40
_CLOSEST_BLOCK_MAX_CHARS = 4000
_CLOSEST_BLOCK_MAX_FILE_LINES = 50_000


def find_closest_block(file_text: str, old_string: str) -> tuple[int, int, float] | None:
    old_lines = old_string.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    count = len(old_lines)
    if count < _CLOSEST_BLOCK_MIN_LINES:
        return None
    file_lines = file_text.split("\n")
    if not file_lines or len(file_lines) > _CLOSEST_BLOCK_MAX_FILE_LINES:
        return None
    positions: dict[str, list[int]] = {}
    for idx, line in enumerate(file_lines):
        key = line.strip()
        if key:
            positions.setdefault(key, []).append(idx)
    votes: dict[int, int] = {}
    for k, line in enumerate(old_lines):
        key = line.strip()
        if len(key) < 3:
            continue
        hits = positions.get(key, [])
        if len(hits) > 50:
            continue
        for idx in hits:
            start = idx - k
            if 0 <= start < len(file_lines):
                votes[start] = votes.get(start, 0) + 1
    candidates = sorted(votes, key=lambda s: -votes[s])[:8]
    if not candidates:
        first = next((line.strip() for line in old_lines if line.strip()), "")
        stripped = [line.strip() for line in file_lines]
        close = difflib.get_close_matches(first, stripped, n=3, cutoff=0.6) if first else []
        offset = next((k for k, line in enumerate(old_lines) if line.strip()), 0)
        for text in close:
            for idx in positions.get(text, [])[:3]:
                if idx - offset >= 0:
                    candidates.append(idx - offset)
    best: tuple[int, int, float] | None = None
    target = "\n".join(line.strip() for line in old_lines)
    for start in candidates:
        end = min(len(file_lines), start + count) - 1
        window = "\n".join(line.strip() for line in file_lines[start:end + 1])
        ratio = difflib.SequenceMatcher(None, target, window, autojunk=False).ratio()
        if best is None or ratio > best[2]:
            best = (start, end, ratio)
    if best is None or best[2] < _CLOSEST_BLOCK_MIN_RATIO:
        return None
    return best


def build_closest_block_hint(file_text: str, old_string: str) -> str:
    found = find_closest_block(file_text, old_string)
    if not found:
        return ""
    start, end, ratio = found
    lines = file_text.split("\n")
    end = min(end, start + _CLOSEST_BLOCK_MAX_LINES - 1)
    body = "\n".join(f"{n + 1}| {lines[n]}" for n in range(start, end + 1))
    if len(body) > _CLOSEST_BLOCK_MAX_CHARS:
        body = body[:_CLOSEST_BLOCK_MAX_CHARS] + "\n…"
    return (
        f"\n\nClosest match in the file, lines {start + 1}-{end + 1} ({int(ratio * 100)}% similar):\n"
        f"{display_text(body)}\n"
        "Copy old_string from these lines exactly (without the `N| ` prefixes)."
    )


def build_nearest_match_hint(file_text: str, old_string: str) -> str:
    first_line = (old_string.split("\n", 1)[0] if old_string else "") or ""
    tokens = first_line.split()
    if not tokens:
        return ""
    keyword = max(tokens, key=len)
    if not keyword:
        return ""
    for i, line in enumerate(file_text.splitlines()):
        if keyword in line:
            full = f"\n\nNearest match: line {i + 1}: {line.rstrip()}"
            return truncate_str_with_marker(full, _NEAREST_HINT_MAX)
    return ""

def build_no_match_message(
    file_text: str,
    old_string: str,
    *,
    include_user_edit_hint: bool = True,
    file_was_read_this_turn: bool | None = None,
) -> str:
    msg = ERROR_NO_MATCHES_BASE
    if include_user_edit_hint:
        msg += ERROR_USER_EDIT_HINT
    block = build_closest_block_hint(file_text, old_string)
    if block:
        msg += block
    else:
        nearest = build_nearest_match_hint(file_text, old_string)
        msg += display_text(nearest)
        if nearest:
            msg += ERROR_WHITESPACE_HINT
    if file_was_read_this_turn is False:
        msg += ERROR_UNREAD_HINT
    return msg

def validate_path_components(safe_rel: str) -> str | None:
    if not safe_rel:
        return None
    for part in safe_rel.replace("\\", "/").split("/"):
        if part and len(part) > NAME_MAX:
            return (
                f"Error: file name exceeds the {NAME_MAX}-character limit. "
                "Please use a shorter file name."
            )
    return None

def suggest_similar_filename(full_path: str, safe_rel: str) -> str:
    parent = os.path.dirname(full_path) or "."
    target = os.path.basename(safe_rel).lower()
    if not target or not os.path.isdir(parent):
        return ""
    try:
        names = os.listdir(parent)
    except OSError:
        return ""
    best = None
    best_score = 0
    for name in names:
        if not os.path.isfile(os.path.join(parent, name)):
            continue
        n = name.lower()
        if n == target:
            continue
        score = 0
        if n.startswith(target[:3]) or target.startswith(n[:3]):
            score += 2
        shared = set(n) & set(target)
        score += len(shared)
        if abs(len(n) - len(target)) <= 2:
            score += 1
        if score > best_score:
            best_score = score
            best = name
    if best and best_score >= 4:
        parent_rel = os.path.dirname(safe_rel).replace("\\", "/")
        suggestion = f"{parent_rel}/{best}" if parent_rel else best
        return f" Did you mean: {suggestion}?"
    return ""

def _error(kind: str, message: str) -> dict:
    return {"error": message, "error_kind": kind}

def _success_payload(
    *,
    safe_rel: str,
    full_path: str,
    diff_html: str,
    add_count: int,
    del_count: int,
    edits: list[dict],
    is_new_file: bool,
    replace_count: int,
) -> dict:
    if is_new_file:
        summary = f"File {safe_rel} created successfully."
    elif replace_count > 1:
        summary = f"All {replace_count} occurrences in {safe_rel} were successfully replaced."
    else:
        summary = f"File {safe_rel} updated successfully."
    return {
        "success": True,
        "file_path": safe_rel,
        "action": "edit_file",
        "diff_html": diff_html,
        "additions": add_count,
        "deletions": del_count,
        "absolute_path": full_path,
        "edits": edits,
        "is_new_file": is_new_file,
        "summary": summary,
    }

FUZZY_MIN_LINES = 2
FUZZY_MIN_CHARS = 24
MATCH_MODE_NOTES = {
    "trailing_whitespace": "Matched after ignoring trailing whitespace differences.",
    "indentation": "Matched after ignoring indentation differences; the replacement was re-indented to fit the file.",
}


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def find_whitespace_tolerant_span(text: str, old_string: str) -> tuple[int, int, str, str, str] | None:
    old_lines = old_string.split("\n")
    ends_with_newline = old_string.endswith("\n")
    if ends_with_newline:
        old_lines = old_lines[:-1]
    meaningful = [line for line in old_lines if line.strip()]
    if not meaningful or (len(meaningful) < FUZZY_MIN_LINES and len(old_string.strip()) < FUZZY_MIN_CHARS):
        return None
    text_lines = text.split("\n")
    offsets: list[int] = []
    pos = 0
    for line in text_lines:
        offsets.append(pos)
        pos += len(line) + 1
    count = len(old_lines)
    for mode, norm in (("trailing_whitespace", str.rstrip), ("indentation", str.strip)):
        target = [norm(line) for line in old_lines]
        hits: list[int] = []
        for i in range(len(text_lines) - count + 1):
            if norm(text_lines[i]) != target[0]:
                continue
            if all(norm(text_lines[i + k]) == target[k] for k in range(1, count)):
                hits.append(i)
                if len(hits) > 1:
                    return None
        if len(hits) == 1:
            first = hits[0]
            last = first + count - 1
            start = offsets[first]
            end = offsets[last] + len(text_lines[last])
            if ends_with_newline and end < len(text):
                end += 1
            old_first = next((line for line in old_lines if line.strip()), "")
            file_first = next((text_lines[first + k] for k, line in enumerate(old_lines) if line.strip()), "")
            return start, end, mode, _leading_ws(old_first), _leading_ws(file_first)
    return None


def reindent_block(block: str, old_indent: str, file_indent: str) -> str:
    if old_indent == file_indent:
        return block
    out: list[str] = []
    for line in block.split("\n"):
        if line.strip() and line.startswith(old_indent):
            out.append(file_indent + line[len(old_indent):])
        else:
            out.append(line)
    return "\n".join(out)


def _locate(text: str, old_string: str, *, allow_fuzzy: bool) -> tuple[list[int], str, str, dict]:
    positions = find_match_positions(text, old_string)
    if positions:
        return positions, old_string, "exact", {}
    stripped = strip_read_line_prefixes(old_string)
    if stripped != old_string and stripped:
        positions = find_match_positions(text, stripped)
        if positions:
            return positions, stripped, "exact", {}
    if allow_fuzzy:
        for candidate in (old_string, stripped):
            if not candidate:
                continue
            span = find_whitespace_tolerant_span(text, candidate)
            if span:
                start, end, mode, old_indent, file_indent = span
                return [start], text[start:end], mode, {
                    "old_indent": old_indent,
                    "file_indent": file_indent,
                }
    return [], stripped or old_string, "none", {}


def apply_search_replace(
    full_path: str,
    safe_rel: str,
    old_string: str,
    new_string: str,
    create_diff_html_fn: Callable,
    *,
    replace_all: bool = False,
    params: SearchReplaceParams | None = None,
    file_was_read_this_turn: bool | None = None,
) -> dict:
    cfg = params or SearchReplaceParams()

    path_err = validate_path_components(safe_rel)
    if path_err:
        return _error("filename_too_long", path_err)

    if os.path.isdir(full_path):
        return _error("invalid_input", f"Error: {safe_rel} is a directory, not a file.")

    if old_string == new_string:
        return _error("invalid_input", ERROR_SAME_STRING)

    if not old_string:
        original = ""
        existed = os.path.isfile(full_path)
        if existed:
            try:
                original = read_text_preserving(full_path)
            except OSError as e:
                return _error("invalid_input", str(e))
        if (
            cfg.empty_old_string_does_not_override
            and existed
            and original
        ):
            return _error("file_already_exists", ERROR_FILE_ALREADY_EXISTS)
        try:
            write_text_atomic(full_path, new_string)
        except OSError as e:
            return _error("invalid_input", str(e))
        ext = os.path.splitext(safe_rel)[1]
        diff_html, _diff_text, add_count, del_count = create_diff_html_fn(display_text(original), new_string, ext)
        is_new = not existed or not original
        edits = [{
            "old_string": "",
            "old_line": 1,
            "new_string": new_string,
            "new_line": 1,
            "context_before": "",
            "context_after": "",
            "line_prefix": "",
        }]
        return _success_payload(
            safe_rel=safe_rel,
            full_path=full_path,
            diff_html=diff_html,
            add_count=add_count,
            del_count=del_count,
            edits=edits,
            is_new_file=is_new,
            replace_count=1,
        )

    if not os.path.isfile(full_path):
        msg = f"File not found: {safe_rel}"
        msg += suggest_similar_filename(full_path, safe_rel)
        return _error("file_not_found", msg)

    try:
        original = read_text_preserving(full_path)
    except OSError as e:
        return _error("invalid_input", str(e))

    has_crlf = "\r\n" in original
    match_text = original.replace("\r\n", "\n") if has_crlf else original

    positions, search_string, match_mode, fuzzy = _locate(match_text, old_string, allow_fuzzy=not replace_all)
    if match_mode == "indentation":
        new_string = reindent_block(new_string, fuzzy["old_indent"], fuzzy["file_indent"])

    if not positions:
        out = _error(
            "no_matches",
            build_no_match_message(
                match_text,
                search_string,
                include_user_edit_hint=cfg.include_user_edit_hint,
                file_was_read_this_turn=file_was_read_this_turn,
            ),
        )
        out["recovery_hint"] = ERROR_NO_MATCHES_RECOVERY_HINT
        return out

    if len(positions) > 1 and not replace_all:
        return _error("multiple_matches", ERROR_MULTIPLE_MATCHES)

    new_content = replace_using_positions(match_text, positions, search_string, new_string)
    new_positions: list[int] = []
    offset = 0
    delta = len(new_string) - len(search_string)
    for pos in positions:
        new_positions.append(pos + offset)
        offset += delta

    write_text = new_content.replace("\n", "\r\n") if has_crlf else new_content

    try:
        write_text_atomic(full_path, write_text)
    except OSError as e:
        return _error("invalid_input", str(e))

    edits = build_edit_details(new_content, search_string, new_string, new_positions)
    for detail in edits:
        for key in ("old_string", "context_before", "context_after", "line_prefix"):
            detail[key] = display_text(detail[key])
    ext = os.path.splitext(safe_rel)[1]
    diff_html, _diff_text, add_count, del_count = create_diff_html_fn(display_text(original), display_text(write_text), ext)
    payload = _success_payload(
        safe_rel=safe_rel,
        full_path=full_path,
        diff_html=diff_html,
        add_count=add_count,
        del_count=del_count,
        edits=edits,
        is_new_file=False,
        replace_count=len(positions),
    )
    if match_mode in MATCH_MODE_NOTES:
        payload["match_mode"] = match_mode
        payload["summary"] = f"{payload['summary']} {MATCH_MODE_NOTES[match_mode]}"
    return payload


def apply_multi_search_replace(
    full_path: str,
    safe_rel: str,
    edits: list,
    create_diff_html_fn: Callable,
    *,
    file_was_read_this_turn: bool | None = None,
) -> dict:
    path_err = validate_path_components(safe_rel)
    if path_err:
        return _error("filename_too_long", path_err)
    if os.path.isdir(full_path):
        return _error("invalid_input", f"Error: {safe_rel} is a directory, not a file.")
    existed = os.path.isfile(full_path)
    original = ""
    if existed:
        try:
            original = read_text_preserving(full_path)
        except OSError as e:
            return _error("invalid_input", str(e))
    has_crlf = "\r\n" in original
    text = original.replace("\r\n", "\n") if has_crlf else original
    total = len(edits)
    details: list[dict] = []
    notes: list[str] = []
    replacements = 0
    for index, edit in enumerate(edits, start=1):
        label = f"Edit {index} of {total}"
        if not isinstance(edit, dict):
            return _error("invalid_input", f"{label} must be an object with old_string and new_string.")
        old_string = str(edit.get("old_string") or "")
        new_string = str(edit.get("new_string") or "")
        replace_all = bool(edit.get("replace_all"))
        if old_string == new_string:
            return _error("invalid_input", f"{label}: {ERROR_SAME_STRING}")
        if not old_string:
            if index == 1 and not text:
                text = new_string
                replacements += 1
                continue
            return _error("invalid_input", f"{label}: an empty old_string is only allowed as the first edit of a new or empty file.")
        if not existed and index == 1:
            msg = f"File not found: {safe_rel}" + suggest_similar_filename(full_path, safe_rel)
            return _error("file_not_found", msg)
        positions, search_string, match_mode, fuzzy = _locate(text, old_string, allow_fuzzy=not replace_all)
        if not positions:
            out = _error(
                "no_matches",
                f"{label}: " + build_no_match_message(
                    text,
                    search_string,
                    file_was_read_this_turn=file_was_read_this_turn,
                ) + " No edits were applied.",
            )
            out["recovery_hint"] = ERROR_NO_MATCHES_RECOVERY_HINT
            out["failed_edit"] = index
            return out
        if len(positions) > 1 and not replace_all:
            out = _error("multiple_matches", f"{label}: {ERROR_MULTIPLE_MATCHES} No edits were applied.")
            out["failed_edit"] = index
            return out
        if match_mode == "indentation":
            new_string = reindent_block(new_string, fuzzy["old_indent"], fuzzy["file_indent"])
        if match_mode in MATCH_MODE_NOTES:
            notes.append(f"{label}: {MATCH_MODE_NOTES[match_mode]}")
        text = replace_using_positions(text, positions, search_string, new_string)
        replacements += len(positions)
        details.append({"old_string": display_text(search_string), "new_string": new_string, "occurrences": len(positions)})

    write_text = text.replace("\n", "\r\n") if has_crlf else text
    if write_text == original:
        return _error("invalid_input", "The edits leave the file unchanged.")
    try:
        write_text_atomic(full_path, write_text)
    except OSError as e:
        return _error("invalid_input", str(e))
    ext = os.path.splitext(safe_rel)[1]
    diff_html, _diff_text, add_count, del_count = create_diff_html_fn(display_text(original), display_text(write_text), ext)
    summary = f"Applied {total} edit{'s' if total != 1 else ''} to {safe_rel}."
    if notes:
        summary += " " + " ".join(notes)
    return {
        "success": True,
        "file_path": safe_rel,
        "action": "multi_edit",
        "diff_html": diff_html,
        "additions": add_count,
        "deletions": del_count,
        "absolute_path": full_path,
        "edits": details,
        "edit_count": total,
        "replacements": replacements,
        "is_new_file": not existed or not original,
        "summary": summary,
    }
