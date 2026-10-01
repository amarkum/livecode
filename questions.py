from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

# Pending ask_question requests. The harness thread waits on one while the
# browser shows the questions card; /livecode/question resolves it.

_QUESTIONS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_DEFAULT_TIMEOUT_S = 30 * 60
_POLL_S = 0.5

MAX_QUESTIONS = 6
MAX_OPTIONS = 8
FREEFORM_OPTION_ID = "__freeform_other__"


def _is_other_label(label: str) -> bool:
    text = label.strip().lower()
    return text == "other" or text.startswith(("other:", "other -", "other ("))


def normalize_questions(args: dict) -> tuple[list[dict], str]:
    """Clean ask_question arguments into [{id, prompt, options, allow_multiple}]."""
    raw = args.get("questions")
    if not isinstance(raw, list) or not raw:
        return [], "questions must be a non-empty array"
    questions: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw[:MAX_QUESTIONS]):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or item.get("question") or "").strip()
        if not prompt:
            continue
        qid = str(item.get("id") or f"q{index + 1}").strip()[:60] or f"q{index + 1}"
        if qid in seen:
            qid = f"{qid}_{index + 1}"
        seen.add(qid)
        options: list[dict] = []
        option_ids: set[str] = set()
        for opt_index, opt in enumerate(item.get("options") or []):
            if isinstance(opt, dict):
                label = str(opt.get("label") or "").strip()
                oid = str(opt.get("id") or "").strip()[:60]
            else:
                label, oid = str(opt or "").strip(), ""
            # The card always offers a free-text "Other…" row.
            if not label or _is_other_label(label):
                continue
            oid = oid or f"o{opt_index + 1}"
            if oid in option_ids or oid == FREEFORM_OPTION_ID:
                oid = f"o{opt_index + 1}_{len(options)}"
            option_ids.add(oid)
            options.append({"id": oid, "label": label[:300]})
            if len(options) >= MAX_OPTIONS:
                break
        # No options: an open question, answered in the card's text box.
        questions.append({
            "id": qid,
            "prompt": prompt[:1000],
            "options": options,
            "allow_multiple": bool(item.get("allow_multiple")),
        })
    if not questions:
        return [], "questions must include a prompt and options"
    return questions, ""


def create_question_request(session_id: str, questions: list[dict], *, timeout_s: int = _DEFAULT_TIMEOUT_S) -> str:
    request_id = f"q_{uuid.uuid4().hex[:16]}"
    with _LOCK:
        _QUESTIONS[request_id] = {
            "session_id": session_id,
            "questions": questions,
            "event": threading.Event(),
            "response": None,
            "created_at": time.time(),
            "timeout_s": timeout_s,
        }
    return request_id


def resolve_question_request(request_id: str, response: dict) -> bool:
    with _LOCK:
        entry = _QUESTIONS.get(request_id)
        if not entry or entry["response"] is not None:
            return False
        entry["response"] = response
        entry["event"].set()
        return True


def wait_for_question_response(request_id: str, *, is_cancelled: Callable[[], bool] | None = None) -> dict:
    """Block until answered, skipped, cancelled, or timed out.

    Returns the browser's response, or {"status": "cancelled" | "expired" | "missing"}.
    """
    with _LOCK:
        entry = _QUESTIONS.get(request_id)
        if not entry:
            return {"status": "missing"}
        event = entry["event"]
        deadline = time.monotonic() + float(entry["timeout_s"])
    status = "expired"
    while time.monotonic() < deadline:
        if event.wait(_POLL_S):
            status = "resolved"
            break
        if is_cancelled and is_cancelled():
            status = "cancelled"
            break
    with _LOCK:
        entry = _QUESTIONS.pop(request_id, None)
    if status == "resolved" and entry and isinstance(entry.get("response"), dict):
        return entry["response"]
    return {"status": status}


def build_answer_result(questions: list[dict], response: dict) -> dict:
    """Turn the browser response into the tool result the model reads."""
    status = str(response.get("status") or "")
    if status in {"cancelled", "expired", "missing"} or response.get("skipped"):
        skipped_by_user = bool(response.get("skipped"))
        result: dict[str, Any] = {
            "success": True,
            "skipped": True,
            "question_count": len(questions),
            "note": (
                "The user skipped these questions. Proceed with your best judgment and state the "
                "assumptions you made in the plan."
                if skipped_by_user
                else "No answer arrived. Proceed with your best judgment and state your assumptions."
            ),
        }
        if status and not skipped_by_user:
            result["reason"] = status
        return result
    by_id = {
        str(a.get("id") or ""): a
        for a in (response.get("answers") or [])
        if isinstance(a, dict)
    }
    answers: list[dict] = []
    for question in questions:
        picked = by_id.get(question["id"]) or {}
        chosen = [str(x) for x in (picked.get("selected") or []) if x]
        labels = [opt["label"] for opt in question["options"] if opt["id"] in chosen]
        other = str(picked.get("other") or "").strip()[:2000]
        entry: dict[str, Any] = {"question": question["prompt"], "selected": labels}
        if (FREEFORM_OPTION_ID in chosen or not question["options"]) and other:
            entry["other"] = other
        if not labels and "other" not in entry:
            entry["unanswered"] = True
        answers.append(entry)
    result = {
        "success": True,
        "answered": True,
        "question_count": len(questions),
        "answers": answers,
    }
    details = str(response.get("details") or "").strip()[:4000]
    if details:
        result["additional_details"] = details
    return result
