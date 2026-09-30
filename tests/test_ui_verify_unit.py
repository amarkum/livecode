"""The tracker behind the UI check: when a turn that changed UI code may finish."""
from livecode.harness import _UiVerify


def test_finishing_is_held_back_until_the_page_was_looked_at_after_the_last_change():
    ui = _UiVerify()
    ui.observe_edit("src/components/Card.tsx", 3)
    assert "src/components/Card.tsx" in ui.reminder()
    ui.observe_browser({"action": "navigate", "url": "http://localhost:3000"}, {"success": True}, 5)
    assert ui.reminder() == ""
    ui.observe_edit("src/styles/card.css", 6)
    assert ui.reminder(), "changed again after looking"


def test_a_failed_look_does_not_count_and_an_unavailable_browser_stops_asking():
    ui = _UiVerify()
    ui.observe_edit("src/App.vue", 1)
    ui.observe_browser({"action": "navigate"}, {"error": "net::ERR_CONNECTION_REFUSED"}, 2)
    assert ui.reminder()
    ui.observe_browser({"action": "navigate"}, {"error": "not installed", "unavailable": True}, 3)
    assert ui.reminder() == ""


def test_batches_and_non_ui_files():
    ui = _UiVerify()
    ui.observe_edit("server/api.py", 1)
    assert ui.reminder() == ""
    ui.observe_edit("src/pages/index.tsx", 2)
    ui.observe_browser({"action": "batch", "actions": [{"action": "click", "text": "x"}, {"action": "screenshot"}]}, {"success": True}, 3)
    assert ui.reminder() == ""
