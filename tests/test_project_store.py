"""Project storage uses the hashed project key only; old slug-keyed folders are left where they are."""
import json
import os

from livecode import project_store


def test_project_dir_uses_the_hashed_key(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "PROJECTS_ROOT", str(tmp_path / "projects"))
    root = tmp_path / "app"
    root.mkdir()
    path = project_store.project_dir(str(root))
    assert os.path.basename(path) == project_store.project_key(str(root))
    assert project_store.existing_project_dir(str(root)) == path
    meta = json.loads(open(os.path.join(path, project_store.PROJECT_META_FILE)).read())
    assert os.path.realpath(meta["project_path"]) == os.path.realpath(str(root))


def test_an_old_slug_folder_is_not_migrated(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(project_store, "PROJECTS_ROOT", str(projects))
    root = tmp_path / "app"
    root.mkdir()
    slug = projects / project_store.path_to_project_slug(project_store.normalize_project_path(str(root)))
    (slug / "sessions").mkdir(parents=True)
    (slug / project_store.PROJECT_META_FILE).write_text(json.dumps({"project_path": str(root)}))
    path = project_store.project_dir(str(root))
    assert path != str(slug) and slug.is_dir(), "the old folder stays, untouched"
    assert not os.path.exists(os.path.join(path, "sessions"))


def test_workspace_state_is_the_primary_folder(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    payload = {"folders": [{"path": str(a)}, {"path": str(b)}]}
    assert project_store.workspace_state_path(str(a), payload) == project_store.normalize_project_path(str(a))
    assert not hasattr(project_store, "adopt_legacy_workspace_storage")
