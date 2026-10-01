"""LiveCode's settings file: what it accepts, what it refuses, and the routes in front of it."""
import json
import os

import pytest

from livecode import browser, settings_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", str(path))
    return path


def test_values_round_trip_and_none_removes(store):
    saved, rejected = settings_store.save_settings({"editorFontSize": 15, "editorWordWrap": False, "terminalCursorStyle": "block", "ratio": 1.5})
    assert not rejected
    assert settings_store.load_settings() == {"editorFontSize": 15, "editorWordWrap": False, "terminalCursorStyle": "block", "ratio": 1.5}
    settings_store.save_settings({"ratio": None})
    assert "ratio" not in settings_store.load_settings()
    assert json.loads(store.read_text())["editorFontSize"] == 15


@pytest.mark.parametrize("key, value, why", [
    ("9lives", 1, "key"),
    ("has-dash", 1, "key"),
    ("x" * 65, 1, "key"),
    ("nested", {"a": 1}, "value"),
    ("listy", [1, 2], "value"),
    ("long", "x" * 501, "500"),
    ("nan", float("nan"), "value"),
])
def test_bad_keys_and_values_are_rejected_and_the_rest_saved(store, key, value, why):
    saved, rejected = settings_store.save_settings({key: value, "fine": True})
    assert key[:80] in rejected and why in rejected[key[:80]]
    assert saved == {"fine": True}


def test_at_most_200_settings(store):
    saved, rejected = settings_store.save_settings({f"k{i}": i for i in range(205)})
    assert len(saved) == 200 and len(rejected) == 5
    saved, rejected = settings_store.save_settings({"k0": "changed"})
    assert not rejected, "an existing key can still change at the cap"


def test_writes_are_atomic_and_leave_no_temp_files(store):
    for i in range(20):
        settings_store.save_settings({"n": i})
    assert sorted(os.listdir(store.parent)) == ["settings.json"]


def test_a_broken_file_reads_as_empty(store):
    store.write_text("{not json")
    assert settings_store.load_settings() == {}


def test_routes_save_reject_and_reset(store, app_client, tmp_path, monkeypatch):
    browser_json = tmp_path / "browser.json"
    browser_json.write_text(json.dumps({"design_accuracy": 70, "ui_verify": False, "cdp_url": "http://127.0.0.1:9222"}))
    monkeypatch.setattr(browser, "BROWSER_SETTINGS_PATH", str(browser_json))
    monkeypatch.setattr(browser, "_settings_cache", None)

    reply = app_client.post("/livecode/settings", json={"settings": {"editorTabSize": 2, "bad-key": 1}}).get_json()
    assert reply["success"] and reply["settings"] == {"editorTabSize": 2} and "bad-key" in reply["rejected"]
    assert app_client.get("/livecode/settings").get_json()["settings"] == {"editorTabSize": 2}
    assert app_client.post("/livecode/settings", json={"settings": "nope"}).status_code == 400

    reply = app_client.post("/livecode/settings/reset").get_json()
    assert reply["success"] and reply["settings"] == {}
    assert settings_store.load_settings() == {}
    left = json.loads(browser_json.read_text())
    assert left == {"cdp_url": "http://127.0.0.1:9222"}, "reset clears preferences and keeps the attached Chrome"
    assert reply["browser"]["design_accuracy"] == browser.DEFAULT_DESIGN_ACCURACY and reply["browser"]["ui_verify"] is True
