"""What the context estimate counts, what compaction keeps, and when a session is compacted."""
import json

import pytest

from livecode import context, session
from livecode.compaction.intra import (
    command_output_digest,
    compact_tool_payload,
    estimate_messages_tokens,
    estimate_tools_tokens,
)
from livecode.project_store import workspace_state_path


def test_images_count_toward_the_estimate():
    text_only = [{"role": "user", "content": [{"type": "text", "text": "x" * 4000}]}]
    with_image = [{"role": "user", "content": [{"type": "text", "text": "x" * 4000},
                                                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    sized = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:...", "width": 1500, "height": 1500}}]}]
    assert estimate_messages_tokens(text_only) == 1000
    assert estimate_messages_tokens(with_image) == 1000 + 1400
    assert estimate_messages_tokens(sized) == 3000, "a known size is billed by area"


def test_tool_definitions_count_too():
    tools = [{"type": "function", "function": {"name": "grep_repo", "description": "d" * 400, "parameters": {"type": "object"}}}]
    assert estimate_tools_tokens(tools) > 100 and estimate_tools_tokens([]) == 0 and estimate_tools_tokens(None) == 0


def test_a_compacted_command_keeps_its_failure_lines_and_tail():
    output = "\n".join(f"collecting {i}" for i in range(300)) + "\nFAILED tests/test_x.py::test_y - AssertionError: boom\n" + \
        "\n".join(f"more {i}" for i in range(200)) + "\n1 failed, 12 passed in 3.2s"
    fitted = json.loads(compact_tool_payload(json.dumps({"command": "pytest -q", "exit_code": 1, "output": output})))
    assert fitted["exit_code"] == 1
    assert "FAILED tests/test_x.py::test_y" in fitted["output_digest"] and fitted["output_digest"].endswith("1 failed, 12 passed in 3.2s")
    lossy = json.loads(compact_tool_payload(json.dumps({"command": "pytest -q", "exit_code": 1, "output": output}), tier="lossy"))
    assert lossy["exit_code"] == 1 and lossy["output_tail"].endswith("1 failed, 12 passed in 3.2s")


def test_digest_is_the_whole_output_when_short():
    assert command_output_digest("ok\n", head=300, tail=1200, failure_lines=10) == "ok\n"


def test_a_compacted_browser_step_keeps_where_it_ended_up():
    out = json.loads(compact_tool_payload(json.dumps({"success": True, "action": "navigate", "url": "http://localhost:3000/login", "title": "Login", "outline": "x" * 9000})))
    assert out["action"] == "navigate" and out["url"] == "http://localhost:3000/login" and "outline" not in out


def test_the_fallback_stays_valid_json():
    raw = json.dumps({"alpha": "v" * 5000, "beta": [1, 2, 3], "gamma": {"k": "v"}, "delta": 4, "eps": 5, "zeta": 6, "eta": 7})
    for tier in ("fitted", "lossy"):
        parsed = json.loads(compact_tool_payload(raw, tier=tier))
        assert parsed["_compacted"] is True


class _Summariser:
    def __init__(self):
        self.calls = 0

    def __call__(self, model, messages):
        self.calls += 1
        return ("1. Primary request: the user asked for a login page. 2. Key concepts: Flask routes, Jinja templates. "
                "3. Tools: read_repo_file on app.py, edit_file on templates/login.html. 4. Files: app.py, templates/login.html. "
                "5. Errors: none. 6. Problem solving: styling still open. 7. User messages: build a login page; make it blue.")


def _history(state, sid, turns):
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"turn {i}: " + "please " * 40})
        msgs.append({"role": "assistant", "content": f"answer {i}: " + "done " * 60})
    session.append_messages(state, sid, msgs)


def test_session_compaction_measures_the_projected_history_not_the_file(tmp_path):
    state = workspace_state_path(str(tmp_path / "proj"))
    sid = "s1"
    _history(state, sid, 12)
    summariser = _Summariser()
    first = context.maybe_compact_session(state, sid, model="m", call_summarize=summariser, context_window=2000, threshold_ratio=0.5)
    assert first and first["boundary_index"] > 0
    summary_calls = summariser.calls
    assert summary_calls >= 1

    # Nothing new: the projected history (summary + tail) is small, so no second summary of the same messages.
    second = context.maybe_compact_session(state, sid, model="m", call_summarize=summariser, context_window=2000, threshold_ratio=0.5)
    assert second is None and summariser.calls == summary_calls

    # Much more history: compacted again, from the previous boundary onward, with a later boundary.
    _history(state, sid, 14)
    third = context.maybe_compact_session(state, sid, model="m", call_summarize=summariser, context_window=2000, threshold_ratio=0.5)
    assert third and third["boundary_index"] > first["boundary_index"]
    projected = session.get_projected_messages(state, sid, "next question", wrap_query=False)
    assert projected[0]["content"].startswith("[Previous conversation summary")
    assert len(projected) < 20


def test_a_small_session_is_left_alone(tmp_path):
    state = workspace_state_path(str(tmp_path / "proj"))
    _history(state, "s2", 3)
    summariser = _Summariser()
    assert context.maybe_compact_session(state, "s2", model="m", call_summarize=summariser, context_window=200_000, threshold_ratio=0.85) is None
    assert summariser.calls == 0
