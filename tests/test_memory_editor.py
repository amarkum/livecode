"""The Memory tab's backend: which files it may touch, reading and saving them, and the routes."""
import os

import pytest

from livecode.memory import storage
from livecode.memory import list_editable_memory, read_editable_memory, save_memory_file


@pytest.fixture
def project(tmp_path, monkeypatch):
    # Test folders live under the temp dir, which LiveCode treats as an ephemeral workspace that keeps no
    # memory; the ephemeral case has its own test below.
    monkeypatch.setattr(storage, "_skip_workspace_write", lambda path: False)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("demo\n")
    return str(root)


@pytest.mark.parametrize("rel, ok", [
    ("MEMORY.md", True),
    ("/MEMORY.md", True),
    ("sessions/2026-10-01-fix-login-abcd1234.md", True),
    ("sessions/../MEMORY.md", False),
    ("sessions/sub/x.md", False),
    ("sessions/notes.txt", False),
    ("index.sqlite", False),
    ("../../etc/passwd", False),
    ("", False),
])
def test_only_memory_md_and_session_logs_are_editable(rel, ok):
    assert (storage.editable_memory_rel(rel) is not None) is ok


def test_memory_md_is_listed_before_it_exists_and_can_be_written(project):
    files = list_editable_memory(project)
    assert files[0]["path"] == "MEMORY.md" and files[0]["exists"] is False
    assert read_editable_memory(project, "MEMORY.md") == {"path": "MEMORY.md", "content": "", "exists": False}
    result = save_memory_file(project, "MEMORY.md", "## Notes\n\n- Use pnpm.\n")
    assert result["saved"] and "abs_path" not in result
    assert read_editable_memory(project, "MEMORY.md")["content"] == "## Notes\n\n- Use pnpm.\n"
    assert storage.read_memory_md(project) == "## Notes\n\n- Use pnpm.\n", "the agent reads what was saved"


def test_session_logs_are_listed_newest_first_and_editable(project):
    old = storage.write_session_log(project, date="2026-09-01", topic_slug="old", session_id="aaaaaaaa", content="old log")
    new = storage.write_session_log(project, date="2026-10-01", topic_slug="new", session_id="bbbbbbbb", content="new log")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    paths = [f["path"] for f in list_editable_memory(project)]
    assert paths[0] == "MEMORY.md" and paths[1].endswith("new-bbbbbbbb.md") and paths[2].endswith("old-aaaaaaaa.md")
    rel = paths[1]
    assert read_editable_memory(project, rel)["content"] == "new log"
    save_memory_file(project, rel, "edited log")
    assert open(new).read() == "edited log"


def test_unknown_paths_and_non_text_are_refused(project):
    with pytest.raises(ValueError):
        save_memory_file(project, "index.sqlite", "x")
    with pytest.raises(ValueError):
        save_memory_file(project, "MEMORY.md", b"bytes")
    with pytest.raises(ValueError):
        save_memory_file(project, "MEMORY.md", "nul\x00byte")
    with pytest.raises(FileNotFoundError):
        read_editable_memory(project, "sessions/2026-01-01-none-00000000.md")


def test_a_symlink_out_of_the_memory_folder_is_refused(project, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    sessions = storage.sessions_dir(project, create=True)
    os.symlink(outside, os.path.join(sessions, "2026-10-01-link-cccccccc.md"))
    with pytest.raises(ValueError):
        read_editable_memory(project, "sessions/2026-10-01-link-cccccccc.md")


def test_ephemeral_workspaces_keep_no_memory(project, monkeypatch):
    monkeypatch.setattr(storage, "_skip_workspace_write", lambda path: True)
    result = save_memory_file(project, "MEMORY.md", "x")
    assert result["saved"] is False and "temporary" in result["skipped"]


def test_routes(project, app_client, monkeypatch):
    listing = app_client.post("/livecode/memory", json={"project_path": project}).get_json()
    assert listing["success"] and listing["files"][0]["path"] == "MEMORY.md"
    assert app_client.get("/livecode/memory?project_path=" + project).get_json()["success"]
    assert app_client.post("/livecode/memory", json={}).status_code == 400

    saved = app_client.post("/livecode/memory/file", json={"project_path": project, "path": "MEMORY.md", "content": "- hi\n"}).get_json()
    assert saved["success"] and saved["saved"]
    read = app_client.post("/livecode/memory/file", json={"project_path": project, "path": "MEMORY.md"}).get_json()
    assert read["content"] == "- hi\n"

    assert app_client.post("/livecode/memory/file", json={"project_path": project, "path": "index.sqlite"}).status_code == 400
    assert app_client.post("/livecode/memory/file", json={"project_path": project, "path": "MEMORY.md", "content": 5}).status_code == 400

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(storage, "write_text_atomic", boom)
    reply = app_client.post("/livecode/memory/file", json={"project_path": project, "path": "MEMORY.md", "content": "x"})
    assert reply.status_code == 500 and "Permission denied" in reply.get_json()["error"]
