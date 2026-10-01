"""Chats are kept as the transcript the page rendered, and shown from it when they are opened again."""
import os

import pytest

from livecode import session
from livecode.project_store import workspace_state_path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def _start_chat(project, sid="s1"):
    state = workspace_state_path(project)
    session.append_messages(state, sid, [{"role": "user", "content": "make the button blue"},
                                         {"role": "assistant", "content": "Done."}])
    return state


def test_save_and_load_round_trip_atomically(project):
    state = _start_chat(project)
    html = '<div class="chat-row livecode-user-row"><div class="chat-msg user">make the button blue</div></div>'
    assert session.save_transcript(state, "s1", html) is True
    assert session.load_transcript(state, "s1") == html
    sdir = session.session_dir(state, "s1", create=False)
    assert not [n for n in os.listdir(sdir) if n.endswith(".tmp")]


def test_over_40_mb_is_not_written(project, monkeypatch):
    state = _start_chat(project)
    session.save_transcript(state, "s1", "<p>kept</p>")
    monkeypatch.setattr(session, "TRANSCRIPT_MAX_BYTES", 10)
    assert session.save_transcript(state, "s1", "<p>" + "x" * 20 + "</p>") is False
    assert session.load_transcript(state, "s1") == "<p>kept</p>", "an oversize save leaves the last one"


def test_a_missing_transcript_reads_as_empty(project):
    state = _start_chat(project)
    assert session.load_transcript(state, "s1") == ""
    assert session.load_transcript(state, "nope") == ""


def test_the_old_display_records_are_gone():
    for name in ("save_diff_record", "load_diff_records", "save_tool_artifact", "load_tool_artifacts", "format_messages_for_display"):
        assert not hasattr(session, name)
    from livecode import subagent
    assert not hasattr(subagent, "display_subagent_result")


def test_routes_save_then_load_the_transcript(project, app_client):
    _start_chat(project)
    html = '<div class="chat-row livecode-user-row"><div class="chat-msg user">hi</div></div>'
    reply = app_client.post("/livecode/session/transcript", json={"project_path": project, "session_id": "s1", "html": html})
    assert reply.status_code == 200 and reply.get_json()["success"] is True
    loaded = app_client.post("/livecode/session", json={"project_path": project, "session_id": "s1"}).get_json()
    assert loaded["transcript_html"] == html and loaded["message_count"] == 2
    assert "messages" not in loaded


def test_a_chat_from_before_transcripts_reports_its_history(project, app_client):
    _start_chat(project)
    loaded = app_client.post("/livecode/session", json={"project_path": project, "session_id": "s1"}).get_json()
    assert loaded["transcript_html"] == "" and loaded["message_count"] == 2


@pytest.mark.parametrize("body", [
    {"session_id": "s1", "html": "x"},
    {"project_path": "P", "html": "x"},
    {"project_path": "P", "session_id": "s1"},
    {"project_path": "P", "session_id": "s1", "html": 5},
])
def test_project_session_and_html_are_required(project, app_client, body):
    body = {k: (project if v == "P" else v) for k, v in body.items()}
    assert app_client.post("/livecode/session/transcript", json=body).status_code == 400


def test_a_chat_is_only_saved_to_the_workspace_it_belongs_to(project, tmp_path, app_client):
    _start_chat(project)
    other = tmp_path / "other"
    other.mkdir()
    reply = app_client.post("/livecode/session/transcript", json={"project_path": str(other), "session_id": "s1", "html": "<p>x</p>"})
    assert reply.status_code == 400 and "not in that workspace" in reply.get_json()["error"]


def test_an_oversize_save_is_success_false_not_an_error(project, app_client, monkeypatch):
    _start_chat(project)
    monkeypatch.setattr(session, "TRANSCRIPT_MAX_BYTES", 4)
    reply = app_client.post("/livecode/session/transcript", json={"project_path": project, "session_id": "s1", "html": "<p>too long</p>"})
    assert reply.status_code == 200 and reply.get_json() == {"success": False, "reason": "too_large"}
