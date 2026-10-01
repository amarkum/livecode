"""How the agent talks to the user in a turn: it does what the request already asked for without asking
again ("send her a message saying …" ends with the message sent), and asks what is really open in the
questions card, whose answer comes back in the same turn."""
import os
import tempfile
import threading
import time

import pytest

from livecode import questions
from turn_driver import ScriptedModel, call, reply, run_turn


def _project():
    root = tempfile.mkdtemp(prefix="lc-proj-")
    with open(os.path.join(root, "README.md"), "w") as handle:
        handle.write("demo\n")
    return root


def _last(call_):
    return call_.tool_results()[-1]


def _open_and_type(site, text):
    return [
        lambda c: reply("Opening the inbox.", call("browser", action="navigate", url=site + "/inbox.html")),
        lambda c: reply("Opening the conversation.", call("browser", action="click", text="Gurubani Kaur")),
        lambda c: reply("Writing it.", call("browser", action="type", selector="#message", text=text)),
    ]


def test_send_a_message_goes_through_without_asking(site, browser_ready):
    text = "Are we still on for Friday?"
    model = ScriptedModel(_open_and_type(site, text) + [
        lambda c: reply("Sending it.", call("browser", action="click", text="Send")),
        lambda c: reply("Sent Gurubani your message."),
    ])
    out = run_turn(_project(), f"go to {site}/inbox.html and in my inbox send a message to Gurubani saying {text}", model)
    assert not out["errors"]
    note = model.calls[0].text()
    assert "already tells you to send it" in note and "do not ask them to confirm" in note
    sent = _last(model.calls[4])
    assert sent["success"] and sent["clicked"] == "Send" and sent["title"] == "Messaging · sent"


def test_draft_only_holds_the_send_button_back(site, browser_ready):
    model = ScriptedModel(_open_and_type(site, "Hi!") + [
        lambda c: reply("Sending.", call("browser", action="click", text="Send")),
        lambda c: reply("I wrote it and left it unsent."),
    ])
    run_turn(_project(), f"go to {site}/inbox.html and write a message to Gurubani saying hi, but don't send it", model)
    held = _last(model.calls[4])
    assert "Held back" in (held.get("error") or "")
    assert "already tells you to send" not in model.calls[0].text()


def test_shall_i_send_it_is_answered_by_doing_it(site, browser_ready):
    model = ScriptedModel(_open_and_type(site, "See you Friday") + [
        lambda c: reply("I've typed the message to Gurubani. Shall I send it?"),
        lambda c: reply("Sending it.", call("browser", action="click", text="Send")),
        lambda c: reply("Sent."),
    ])
    out = run_turn(_project(), f"go to {site}/inbox.html and send Gurubani a message saying See you Friday", model)
    assert not out["errors"]
    assert "You stopped to ask the user whether to send" in model.calls[4].text()
    assert _last(model.calls[5])["success"] and out["answer"] == "Sent."


def test_should_i_go_ahead_is_not_a_place_to_stop():
    """A check-in in the middle of the very work that was asked for: the request was the go-ahead."""
    model = ScriptedModel([lambda c: reply("I found the helper. Should I go ahead?"), lambda c: reply("Renamed it.")])
    out = run_turn(_project(), "rename the helper in utils.py to clean_name", model)
    text = model.calls[1].text()
    assert "whether to go ahead" in text and "request is the go-ahead" in text
    assert "already asks for it" not in text, "nothing in the request names a send/post/buy step"
    assert out["answer"] == "Renamed it."


def test_a_risky_step_the_user_never_asked_for_is_asked_in_the_card():
    """"Should I proceed?" before dropping a table is the user's call: no invented go-ahead, ask in the card."""
    model = ScriptedModel([
        lambda c: reply("Option B drops the users table and cannot be undone. Should I proceed?"),
        lambda c: reply("Holding off on dropping the table; the index change is in place."),
    ])
    out = run_turn(_project(), "speed up the users query", model)
    text = model.calls[1].text()
    assert "Nothing in the user's request says to" in text and "ask_question" in text
    assert "request is the go-ahead" not in text and "already asks for it" not in text
    assert out["answer"].startswith("Holding off")


def test_an_unauthorized_send_is_asked_not_assumed():
    model = ScriptedModel([
        lambda c: reply("I drafted the reply. Shall I send it?"),
        lambda c: reply("The draft is ready; it has not been sent."),
    ])
    run_turn(_project(), "look at Sam's email and draft a reply", model)
    text = model.calls[1].text()
    assert "whether to send" in text and "Nothing in the user's request says to" in text


def test_an_offer_of_more_work_is_fine():
    model = ScriptedModel([lambda c: reply("Renamed it. Want me to also add tests?")])
    run_turn(_project(), "rename the helper in utils.py to clean_name", model)
    assert len(model.calls) == 1


def _answer_when_asked(answers, seen):
    """Answers the questions card the way the user does, from the browser, when it appears."""
    def run():
        deadline = time.time() + 20
        while time.time() < deadline:
            with questions._LOCK:
                pending = [(rid, q) for rid, q in questions._QUESTIONS.items() if q["response"] is None]
            if pending:
                rid, entry = pending[0]
                seen.append(entry["questions"])
                questions.resolve_question_request(rid, {"answers": answers})
                return
            time.sleep(0.05)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_doubt_is_asked_in_the_card_and_answered_in_the_same_turn():
    ask = call("ask_question", questions=[
        {"id": "who", "prompt": "Which Gurubani do you mean?", "options": [{"id": "kaur", "label": "Gurubani Kaur"}, {"id": "singh", "label": "Gurubani Singh"}]},
        {"id": "text", "prompt": "What should the message say?"},
        {"id": "when", "prompt": "Which days work?", "allow_multiple": True,
         "options": [{"id": "fri", "label": "Friday"}, {"id": "sat", "label": "Saturday"}, {"id": "sun", "label": "Sunday"}]},
    ])
    model = ScriptedModel([lambda c: reply("Two Gurubanis are in your inbox.", ask), lambda c: reply("Sent it to Gurubani Kaur.")])
    seen: list = []
    answerer = _answer_when_asked([
        {"id": "who", "selected": ["kaur"]},
        {"id": "text", "selected": [], "other": "Are we on for Friday?"},
        {"id": "when", "selected": ["fri", "sat"]},
    ], seen)
    out = run_turn(_project(), "send Gurubani a message on linkedin", model)
    answerer.join(timeout=25)
    assert not out["errors"]
    assert "ask_question" in model.calls[0].tools, "the card is offered in agent mode"
    shown = seen[0]
    assert shown[1]["options"] == [] and shown[2]["allow_multiple"] is True
    answers = _last(model.calls[1])["answers"]
    assert answers[0]["selected"] == ["Gurubani Kaur"]
    assert answers[1]["other"] == "Are we on for Friday?"
    assert answers[2]["selected"] == ["Friday", "Saturday"]
    assert len(model.calls) == 2 and out["answer"] == "Sent it to Gurubani Kaur.", "one turn, not a new prompt"


def test_open_questions_need_no_options():
    found, problem = questions.normalize_questions({"questions": [{"id": "text", "prompt": "What should it say?"}]})
    assert not problem and found[0]["options"] == []
    result = questions.build_answer_result(found, {"answers": [{"id": "text", "selected": [], "other": "Hello"}]})
    assert result["answers"][0]["other"] == "Hello"


def test_plan_mode_asks_a_multiple_choice_question_and_plans_with_the_answers():
    """Plan mode: several options that can apply together are asked as one multi-select question, next to a
    single choice and an open one; the plan is written from the answers in the same turn."""
    ask = call("ask_question", questions=[
        {"id": "pages", "prompt": "Which pages should get the new status pills?", "allow_multiple": True,
         "options": [{"id": "orders", "label": "Orders"}, {"id": "invoices", "label": "Invoices"}, {"id": "returns", "label": "Returns"}]},
        {"id": "style", "prompt": "Where should the status colours live?",
         "options": [{"id": "tokens", "label": "Design tokens (recommended)"}, {"id": "css", "label": "Component CSS"}]},
        {"id": "extra", "prompt": "Any status names beyond Paid, Due, Cancelled and Sold?"},
    ])
    plan = call("create_plan", title="Status pills", overview="Colour-coded status pills on the chosen pages.",
                plan="# Status pills\n\nOrders and Returns get pills; colours come from design tokens.\n\n## Tests\n\nA test per status.",
                todos=[{"content": "Add the status tokens"}, {"content": "Use the pill on Orders and Returns"}])
    model = ScriptedModel([lambda c: reply("A few decisions first.", ask), lambda c: reply("Writing the plan.", plan)])
    seen: list = []
    answerer = _answer_when_asked([
        {"id": "pages", "selected": ["orders", "returns", "__freeform_other__"], "other": "Refunds too"},
        {"id": "style", "selected": ["tokens"]},
        {"id": "extra", "selected": [], "other": "Refunded"},
    ], seen)
    out = run_turn(_project(), "plan colour-coded status pills for the order pages", model, mode="plan")
    answerer.join(timeout=25)
    assert not out["errors"]
    first = model.calls[0]
    assert "ask_question" in first.tools and "create_plan" in first.tools and "edit_file" not in first.tools
    assert "allow_multiple: true" in first.text(), "the plan-mode prompt teaches multi-select questions"
    asked = seen[0]
    assert asked[0]["allow_multiple"] is True and asked[1]["allow_multiple"] is False and asked[2]["options"] == []
    answers = _last(model.calls[1])["answers"]
    assert answers[0]["selected"] == ["Orders", "Returns"] and answers[0]["other"] == "Refunds too"
    assert answers[1]["selected"] == ["Design tokens (recommended)"]
    assert answers[2]["other"] == "Refunded"
    assert len(model.calls) == 2, "the plan follows the answers in the same turn"
