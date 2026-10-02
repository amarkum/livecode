from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from typing import Any

from .project_store import project_file

CHAT_HISTORY_FILE = "chat_history.jsonl"
SUMMARY_FILE = "summary.json"
COMPACTION_FILE = "compaction.json"
CHECKPOINTS_DIR = "compaction_checkpoints"
# The chat as the user saw it: the rendered transcript the page sends after each turn. Loading a chat shows
# this, exactly; the message history (chat_history.jsonl) is what the model reads.
TRANSCRIPT_FILE = "transcript.html"
TRANSCRIPT_MAX_BYTES = 40 * 1024 * 1024

def session_dir(project_path: str, session_id: str, *, create: bool = True) -> str:
    safe_id = "".join(c for c in (session_id or "") if c.isalnum() or c in ("_", "-"))[:128]
    if not safe_id:
        raise ValueError("session_id required")
    path = project_file(project_path, "sessions", safe_id, create=create)
    if create:
        os.makedirs(path, exist_ok=True)
    return path

def _chat_history_path(project_path: str, session_id: str) -> str:
    return os.path.join(session_dir(project_path, session_id), CHAT_HISTORY_FILE)

def _summary_path(project_path: str, session_id: str) -> str:
    return os.path.join(session_dir(project_path, session_id), SUMMARY_FILE)

def _compaction_path(project_path: str, session_id: str) -> str:
    return os.path.join(session_dir(project_path, session_id), COMPACTION_FILE)

def _read_jsonl(path: str) -> list[dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    messages: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages

def _count_jsonl_lines(path: str) -> int:
    if not os.path.isfile(path):
        return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count

_APPEND_LOCK = threading.Lock()


def _append_jsonl(path: str, record: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, default=str) + "\n"
    with _APPEND_LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(line)

def _canonical_prefix_hash(messages: list[dict[str, Any]], boundary_index: int) -> str:
    prefix = messages[:boundary_index]
    payload = json.dumps(prefix, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()

_SESSION_CACHE: dict[tuple[str, str], tuple[tuple, dict[str, Any]]] = {}


def _session_disk_signature(sdir: str) -> tuple:
    sig: list = []
    for name in (CHAT_HISTORY_FILE, SUMMARY_FILE, COMPACTION_FILE):
        try:
            st = os.stat(os.path.join(sdir, name))
            sig.append((name, int(st.st_mtime_ns), st.st_size))
        except OSError:
            sig.append((name, 0, -1))
    return tuple(sig)


def load_session(project_path: str, session_id: str) -> dict[str, Any]:
    sdir = session_dir(project_path, session_id, create=False)

    cache_key = (project_path, session_id)
    signature = _session_disk_signature(sdir)
    cached = _SESSION_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        base = cached[1]
        return {**base, "messages": list(base["messages"])}

    summary_path = os.path.join(sdir, SUMMARY_FILE)
    summary: dict[str, Any] = {}
    if os.path.isfile(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except (json.JSONDecodeError, OSError):
            summary = {}

    messages = _read_jsonl(os.path.join(sdir, CHAT_HISTORY_FILE))
    compaction: dict[str, Any] | None = None
    cpath = os.path.join(sdir, COMPACTION_FILE)
    if os.path.isfile(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                compaction = json.load(f)
        except (json.JSONDecodeError, OSError):
            compaction = None

    result = {
        "session_id": session_id,
        "project_path": project_path,
        "session_dir": sdir,
        "summary": summary,
        "messages": messages,
        "compaction": compaction,
    }
    _SESSION_CACHE[cache_key] = (signature, result)
    if len(_SESSION_CACHE) > 64:
        _SESSION_CACHE.pop(next(iter(_SESSION_CACHE)), None)
    return {**result, "messages": list(result["messages"])}

def save_transcript(project_path: str, session_id: str, html: str) -> bool:
    """Write the rendered transcript atomically. Over 40 MB it is not written and this returns False."""
    data = (html or "").encode("utf-8")
    if len(data) > TRANSCRIPT_MAX_BYTES:
        return False
    path = os.path.join(session_dir(project_path, session_id), TRANSCRIPT_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".transcript-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def load_transcript(project_path: str, session_id: str) -> str:
    path = os.path.join(session_dir(project_path, session_id, create=False), TRANSCRIPT_FILE)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def session_exists(project_path: str, session_id: str) -> bool:
    sdir = session_dir(project_path, session_id, create=False)
    return any(os.path.isfile(os.path.join(sdir, name)) for name in (CHAT_HISTORY_FILE, SUMMARY_FILE, TRANSCRIPT_FILE))


def _load_compaction(project_path: str, session_id: str) -> dict[str, Any] | None:
    path = _compaction_path(project_path, session_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

def _strip_display_metadata(msg: dict[str, Any]) -> dict[str, Any]:
    drop = {"display", "reasoning_content", "internal", "thinking_s", "thinking_ms"}
    if not any(k in msg for k in drop):
        return msg
    return {k: v for k, v in msg.items() if k not in drop}

def _sanitize_display_payload(display: dict[str, Any] | None) -> dict[str, Any] | None:
    if not display or not isinstance(display, dict):
        return None
    out: dict[str, Any] = {
        "text": str(display.get("text") or ""),
        "segments": list(display.get("segments") or []),
        "attachments": [],
    }
    build = display.get("plan_build")
    if isinstance(build, dict) and build.get("file"):
        out["plan_build"] = {"file": str(build.get("file"))[:200], "title": str(build.get("title") or "")[:200]}
    for att in (display.get("attachments") or [])[:10]:
        if not isinstance(att, dict):
            continue
        clean: dict[str, Any] = {
            "id": str(att.get("id") or ""),
            "name": str(att.get("name") or ""),
            "type": str(att.get("type") or "file"),
        }
        if att.get("size") is not None:
            clean["size"] = att.get("size")
        if clean["type"] in ("repo_file", "repo_folder") and att.get("repo_path"):
            clean["repo_path"] = str(att.get("repo_path") or "")
        if clean["type"] == "chat" and att.get("session_id"):
            clean["session_id"] = str(att.get("session_id") or "")
        data = att.get("data")
        if clean["type"] == "image" and isinstance(data, str) and data.startswith("data:"):
            if len(data) <= 500_000:
                clean["data"] = data
        out["attachments"].append(clean)
    return out

def sanitize_messages_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    i = 0
    n = len(messages)

    while i < n:
        msg = messages[i]
        role = msg.get("role")

        if role == "tool":
            prev = out[-1] if out else None
            if not prev or prev.get("role") != "assistant" or not prev.get("tool_calls"):
                i += 1
                continue
            expected_ids = {tc.get("id") for tc in prev.get("tool_calls") or []}
            if msg.get("tool_call_id") not in expected_ids:
                i += 1
                continue
            out.append(_strip_display_metadata(msg))
            i += 1
            continue

        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = msg.get("tool_calls") or []
            expected_ids = {tc.get("id") for tc in tool_calls}
            j = i + 1
            matched: list[dict[str, Any]] = []
            while j < n and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id")
                if tid in expected_ids:
                    matched.append(_strip_display_metadata(messages[j]))
                j += 1
            if not matched:
                slim = _strip_display_metadata({k: v for k, v in msg.items() if k != "tool_calls"})
                if slim.get("content") or slim.get("role"):
                    out.append(slim)
            else:
                answered = {m.get("tool_call_id") for m in matched}
                kept = [tc for tc in tool_calls if tc.get("id") in answered]
                out.append(_strip_display_metadata({**msg, "tool_calls": kept}))
                out.extend(matched)
            i = j
            continue

        out.append(_strip_display_metadata(msg))
        i += 1

    return out

def _valid_compaction(messages: list[dict], compaction: dict[str, Any] | None) -> bool:
    if not compaction or not compaction.get("summary"):
        return False
    boundary = int(compaction.get("boundary_index", 0))
    if boundary < 0 or boundary > len(messages):
        return False
    expected = compaction.get("prefix_hash")
    if not expected:
        return False
    return _canonical_prefix_hash(messages, boundary) == expected

def has_valid_compaction(project_path: str, session_id: str) -> bool:
    session = load_session(project_path, session_id)
    messages = session.get("messages") or []
    return _valid_compaction(messages, session.get("compaction"))

def get_projected_messages(
    project_path: str,
    session_id: str,
    current_question: str | list,
    *,
    rules_reminder: str = "",
    wrap_query: bool = True,
) -> list[dict[str, Any]]:
    from livecode.prompts import wrap_user_content

    session = load_session(project_path, session_id)
    messages = list(session.get("messages") or [])
    compaction = session.get("compaction")

    projected: list[dict[str, Any]] = []
    if _valid_compaction(messages, compaction):
        summary = str(compaction.get("summary", "")).strip()
        boundary = int(compaction["boundary_index"])
        if summary:
            projected.append({
                "role": "user",
                "content": (
                    "[Previous conversation summary — continue from this context]\n\n"
                    + summary
                ),
            })
        if rules_reminder:
            projected.append({"role": "user", "content": rules_reminder})
        projected.extend(sanitize_messages_for_api(messages[boundary:]))
    else:
        projected.extend(sanitize_messages_for_api(messages))

    question_content = wrap_user_content(current_question) if wrap_query else current_question
    projected.append({"role": "user", "content": question_content})
    return projected

def _message_content_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p.strip() for p in parts if p and str(p).strip()).strip()
    return str(raw).strip()

def _first_user_title(messages: list[dict[str, Any]] | None) -> str | None:
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        display = msg.get("display") or {}
        display_text = str(display.get("text") or "").strip()
        text = display_text or _message_content_text(msg.get("content"))
        if not text:
            continue
        if text.startswith("[Turn activity summary]") or text.startswith("[Previous conversation summary"):
            continue
        return text[:120]
    return None

def session_transcript_text(
    project_path: str,
    session_id: str,
    *,
    max_chars: int = 12_000,
) -> str:
    try:
        session = load_session(project_path, session_id)
    except Exception:
        return ""
    lines: list[str] = []
    for msg in session.get("messages") or []:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        display = msg.get("display") or {}
        text = str(display.get("text") or "").strip() or _message_content_text(msg.get("content"))
        if not text:
            continue
        if text.startswith("[Turn activity summary]") or text.startswith("[Previous conversation summary"):
            continue
        lines.append(f"{'User' if role == 'user' else 'Assistant'}: {text}")
    transcript = "\n\n".join(lines).strip()
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars].rstrip() + "\n\n…(transcript truncated)"
    return transcript

def append_messages(
    project_path: str,
    session_id: str,
    new_messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    title: str | None = None,
) -> None:
    if not new_messages:
        return
    path = _chat_history_path(project_path, session_id)
    for msg in new_messages:
        _append_jsonl(path, msg)

    summary_path = _summary_path(project_path, session_id)
    summary: dict[str, Any] = {}
    if os.path.isfile(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except (json.JSONDecodeError, OSError):
            summary = {}

    now = time.time()
    if not summary.get("created_at"):
        summary["created_at"] = now
    summary["updated_at"] = now
    summary["session_id"] = session_id
    summary["project_path"] = os.path.abspath(os.path.expanduser(project_path))
    summary["message_count"] = len(_read_jsonl(path))
    if model:
        summary["model"] = model
    effective_title = (title or "").strip()
    if not effective_title and not summary.get("title"):
        effective_title = (_first_user_title(new_messages) or "").strip()
    if effective_title and not summary.get("title"):
        summary["title"] = effective_title[:200]

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

def save_compaction(
    project_path: str,
    session_id: str,
    *,
    boundary_index: int,
    summary: str,
    messages: list[dict[str, Any]] | None = None,
    strategy: str = "full_replace",
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if messages is None:
        messages = _read_jsonl(_chat_history_path(project_path, session_id))

    record: dict[str, Any] = {
        "boundary_index": boundary_index,
        "summary": summary.strip(),
        "prefix_hash": _canonical_prefix_hash(messages, boundary_index),
        "compacted_at": time.time(),
        "strategy": strategy,
    }
    if attempts:
        record["attempts"] = attempts

    sdir = session_dir(project_path, session_id)
    cpath = os.path.join(sdir, COMPACTION_FILE)
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)

    checkpoints = os.path.join(sdir, CHECKPOINTS_DIR)
    os.makedirs(checkpoints, exist_ok=True)
    ts = int(time.time() * 1000)
    checkpoint_path = os.path.join(checkpoints, f"{ts}.json")
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump({"compaction": record, "message_count": len(messages)}, f, indent=2)

    summary_path = _summary_path(project_path, session_id)
    meta: dict[str, Any] = {}
    if os.path.isfile(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta["compaction_count"] = int(meta.get("compaction_count", 0)) + 1
    meta["last_compacted_at"] = record["compacted_at"]
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return record

def append_turn_summary(
    project_path: str,
    session_id: str,
    question: str,
    answer: str,
    turn_summary: str | None = None,
    *,
    model: str | None = None,
    title: str | None = None,
    display: dict[str, Any] | None = None,
) -> None:
    user_msg: dict[str, Any] = {"role": "user", "content": question}
    clean_display = _sanitize_display_payload(display)
    if clean_display:
        user_msg["display"] = clean_display
    to_append: list[dict[str, Any]] = [
        user_msg,
    ]
    if turn_summary and turn_summary.strip():
        to_append.append({
            "role": "user",
            "content": f"[Turn activity summary]\n{turn_summary.strip()}",
        })
    if answer and answer.strip():
        to_append.append({"role": "assistant", "content": answer.strip()})
    append_messages(
        project_path,
        session_id,
        to_append,
        model=model,
        title=(title or question[:120]),
    )

def append_turn_messages(
    project_path: str,
    session_id: str,
    turn_messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    title: str | None = None,
) -> None:
    durable = [m for m in (turn_messages or []) if not m.get("internal")]
    if not durable:
        return
    append_messages(
        project_path,
        session_id,
        durable,
        model=model,
        title=(title or _first_user_title(turn_messages) or "")[:200] or None,
    )

def set_session_title(
    project_path: str,
    session_id: str,
    title: str,
    *,
    overwrite: bool = False,
) -> None:
    clean = (title or "").strip()[:200]
    if not clean:
        return
    sdir = session_dir(project_path, session_id)
    summary_path = _summary_path(project_path, session_id)
    summary: dict[str, Any] = {}
    if os.path.isfile(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except (json.JSONDecodeError, OSError):
            summary = {}
    if summary.get("title") and not overwrite:
        return
    now = time.time()
    if not summary.get("created_at"):
        summary["created_at"] = now
    summary["updated_at"] = now
    summary["session_id"] = session_id
    summary["project_path"] = os.path.abspath(os.path.expanduser(project_path))
    summary["title"] = clean
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

def list_sessions(project_path: str, *, limit: int = 30) -> list[dict[str, Any]]:
    base = project_file(project_path, "sessions", create=False)
    if not os.path.isdir(base):
        return []

    sessions: list[dict[str, Any]] = []
    for name in os.listdir(base):
        try:
            sdir = os.path.join(base, name)
            if not os.path.isdir(sdir):
                continue
            history_path = os.path.join(sdir, CHAT_HISTORY_FILE)
            summary_path = os.path.join(sdir, SUMMARY_FILE)
            meta: dict[str, Any] = {"session_id": name}
            if os.path.isfile(summary_path):
                try:
                    with open(summary_path, "r", encoding="utf-8") as f:
                        meta.update(json.load(f))
                except (json.JSONDecodeError, OSError):
                    pass
                meta["session_id"] = name  # the folder is the session, whatever an old summary says
            meta.setdefault("updated_at", os.path.getmtime(sdir))
            message_count = _count_jsonl_lines(history_path)
            meta["message_count"] = message_count
            if not message_count and not meta.get("title"):
                continue
            if not meta.get("title"):
                preview = _first_user_title(_read_jsonl(history_path))
                if preview:
                    meta["first_user_preview"] = preview[:120]
            sessions.append(meta)
        except Exception:
            continue

    sessions.sort(key=lambda s: float(s.get("updated_at") or 0), reverse=True)
    return sessions[:limit]

def delete_session(project_path: str, session_id: str) -> bool:
    safe_id = "".join(c for c in (session_id or "") if c.isalnum() or c in ("_", "-"))[:128]
    if not safe_id:
        return False
    path = project_file(project_path, "sessions", safe_id, create=False)
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path)
    return True

def rename_session(project_path: str, session_id: str, title: str) -> bool:
    clean_title = (title or "").strip()[:200]
    if not clean_title:
        raise ValueError("title required")
    sdir = session_dir(project_path, session_id)
    if not os.path.isdir(sdir):
        return False
    summary_path = _summary_path(project_path, session_id)
    summary: dict[str, Any] = {}
    if os.path.isfile(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except (json.JSONDecodeError, OSError):
            summary = {}
    summary["title"] = clean_title
    summary["session_id"] = session_id
    summary["updated_at"] = time.time()
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return True

def fork_session(project_path: str, session_id: str, new_session_id: str, *, title: str = "") -> dict[str, Any]:
    import shutil
    src = session_dir(project_path, session_id)
    dst = session_dir(project_path, new_session_id)
    if os.path.isdir(dst) and os.listdir(dst):
        raise ValueError(f"Target session already exists: {new_session_id}")
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        sp = os.path.join(src, name)
        if os.path.isfile(sp):
            shutil.copy2(sp, os.path.join(dst, name))
    # The copy is its own chat: its summary names it, and it sorts as just made.
    summary_path = os.path.join(dst, SUMMARY_FILE)
    summary: dict[str, Any] = {}
    if os.path.isfile(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except (json.JSONDecodeError, OSError):
            summary = {}
    summary["session_id"] = new_session_id
    summary["updated_at"] = time.time()
    if title.strip():
        summary["title"] = title.strip()[:200]
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return load_session(project_path, new_session_id)


def session_markdown(project_path: str, session_id: str) -> str:
    """The chat's user and assistant messages as Markdown, for copying out."""
    try:
        session = load_session(project_path, session_id)
    except Exception:
        return ""
    title = str((session.get("summary") or {}).get("title") or "").strip()
    parts: list[str] = [f"# {title}"] if title else []
    for msg in session.get("messages") or []:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        display = msg.get("display") or {}
        text = str(display.get("text") or "").strip() or _message_content_text(msg.get("content"))
        if not text or text.startswith("[Turn activity summary]") or text.startswith("[Previous conversation summary"):
            continue
        parts.append(f"## {'You' if role == 'user' else 'Assistant'}\n\n{text}")
    return "\n\n".join(parts).strip() + "\n"

def is_user_turn_message(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "user" or msg.get("internal"):
        return False
    content = msg.get("content")
    text = content if isinstance(content, str) else " ".join(
        str(b.get("text") or "") for b in (content or []) if isinstance(b, dict)
    )
    return not text.lstrip().startswith("[User interjection]")


def count_user_messages(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if is_user_turn_message(m))


def message_index_for_user_turn(messages: list[dict[str, Any]], user_index: int) -> int | None:
    seen = 0
    for i, msg in enumerate(messages):
        if is_user_turn_message(msg):
            if seen == user_index:
                return i
            seen += 1
    return None


def rewind_to_message(
    project_path: str,
    session_id: str,
    message_index: int,
) -> list[dict[str, Any]]:
    path = _chat_history_path(project_path, session_id)
    messages = _read_jsonl(path)
    if message_index < 0 or message_index > len(messages):
        raise ValueError(f"message_index out of range: {message_index}")
    kept = messages[:message_index]
    with open(path, "w", encoding="utf-8") as f:
        for msg in kept:
            f.write(json.dumps(msg, default=str) + "\n")
    cpath = _compaction_path(project_path, session_id)
    if os.path.isfile(cpath):
        try:
            os.remove(cpath)
        except OSError:
            pass
    return kept
