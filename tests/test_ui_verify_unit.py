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


def _answer(ui, selected=None, other=None, skipped=False):
    args = {"questions": [{"id": "v", "prompt": "Want me to verify the UI change in the browser?",
                           "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]}]}
    if skipped:
        return ui.observe_question(args, {"success": True, "skipped": True})
    entry = {"question": args["questions"][0]["prompt"], "selected": selected or []}
    if other:
        entry["other"] = other
    return ui.observe_question(args, {"success": True, "answered": True, "answers": [entry]})


def test_without_a_request_to_see_it_the_agent_asks_first():
    ui = _UiVerify()
    ui.observe_edit("src/components/Card.tsx", 1)
    text = ui.reminder()
    assert "ask_question" in text and "Want me to verify the UI change in the browser?" in text


def test_yes_sends_it_to_the_browser_for_a_crop_of_the_change():
    ui = _UiVerify()
    ui.observe_edit("src/components/Card.tsx", 1)
    ui.reminder()
    assert _answer(ui, ["Yes"]) == ""
    text = ui.reminder()
    assert "crop" in text and "the user asked you to" in text
    ui.observe_browser({"action": "crop", "selector": ".card"}, {"success": True}, 3)
    assert ui.reminder() == ""


def test_no_skip_or_a_typed_answer_stops_the_reminders_and_says_so():
    for kwargs in ({"selected": ["No"]}, {"skipped": True}, {"selected": [], "other": "later maybe"}):
        ui = _UiVerify()
        ui.observe_edit("src/components/Card.tsx", 1)
        ui.reminder()
        note = _answer(ui, **kwargs)
        assert "not checked in the browser" in note, kwargs
        assert ui.reminder() == "", kwargs


def test_other_questions_leave_it_alone():
    ui = _UiVerify()
    ui.observe_edit("src/components/Card.tsx", 1)
    args = {"questions": [{"id": "c", "prompt": "Which colour?", "options": [{"id": "r", "label": "Red"}]}]}
    assert ui.observe_question(args, {"success": True, "skipped": True}) == ""
    assert "ask_question" in ui.reminder()


def test_a_request_that_asks_to_see_it_goes_straight_to_the_browser():
    ui = _UiVerify(requested=True)
    ui.observe_edit("src/components/Card.tsx", 1)
    text = ui.reminder()
    assert "ask_question" not in text and "crop" in text
