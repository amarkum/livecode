"""Project rules are listed once per file, however the file is reached."""
import os

from livecode.rules import discover_project_rules, file_identity


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# Rules\n\nUse tabs.\n")
    return root


def test_a_symlinked_agent_file_is_listed_once(tmp_path):
    root = _project(tmp_path)
    os.symlink(root / "AGENTS.md", root / "CLAUDE.md")
    names = [r.file_name for r in discover_project_rules(str(root))]
    assert len(names) == 1


def test_a_hard_link_to_the_same_rule_is_listed_once(tmp_path):
    root = _project(tmp_path)
    os.link(root / "AGENTS.md", root / "CLAUDE.md")
    assert len(discover_project_rules(str(root))) == 1
    assert file_identity(str(root / "AGENTS.md")) == file_identity(str(root / "CLAUDE.md"))


def test_rules_dir_entries_linked_to_each_other_are_listed_once(tmp_path):
    root = _project(tmp_path)
    (root / ".claude" / "rules" / "style.md").write_text("Prefer small functions.\n")
    os.link(root / ".claude" / "rules" / "style.md", root / ".claude" / "rules" / "style-copy.md")
    files = [r.file_path for r in discover_project_rules(str(root))]
    assert sum(1 for f in files if os.sep + os.path.join(".claude", "rules") + os.sep in f) == 1


def test_different_files_with_the_same_text_both_count(tmp_path):
    root = _project(tmp_path)
    (root / "CLAUDE.md").write_text("# Rules\n\nUse tabs.\n")
    assert len(discover_project_rules(str(root))) == 2


def test_the_rules_route_lists_a_linked_file_once(tmp_path, app_client):
    root = _project(tmp_path)
    os.link(root / "AGENTS.md", root / "CLAUDE.md")
    reply = app_client.post("/livecode/rules", json={"project_path": str(root)}).get_json()
    assert reply["success"] and len(reply["files"]) == 1
