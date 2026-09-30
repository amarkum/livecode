"""Reading a request for the final steps it already asks for, and an answer that stops to ask anyway."""
import pytest

from livecode.routing import asks_permission, authorized_final_actions


@pytest.mark.parametrize("text, kinds", [
    ("go to linkedin, in my inbox send a message to Gurubani saying hi", ["send"]),
    ("message Gurubani on linkedin: are we on for Friday", ["send"]),
    ("reply to the last email from Sam with thanks", ["send"]),
    ("fill in the signup form and submit it", ["submit"]),
    ("buy the cheapest usb-c cable on amazon", ["buy"]),
    ("book a table at 8 and confirm it", ["book", "confirm"]),
    ("post this update on linkedin", ["post"]),
    ("draft a message to Gurubani but don't send it", []),
    ("go to linkedin and write a message to Gurubani, don't send it yet", []),
    ("just draft the post for me to review", []),
    ("check out github actions", []),
    ("open youtube and play lofi", []),
    ("refactor this in order to test it", []),
])
def test_authorized_final_actions(text, kinds):
    assert authorized_final_actions(text) == kinds


@pytest.mark.parametrize("answer, verb", [
    ("I've typed the message. Shall I send it?", "send"),
    ("The message is ready. Do you want me to send it now?", "send"),
    ("Shall I send it? Let me know.", "send"),
    ("Should I go ahead?", "go ahead"),
    ("I opened the page. Would you like me to click Submit?", "click"),
    ("Done. Want me to also add tests?", ""),
    ("Sent it to Gurubani.", ""),
    ("Shall I send it? Also, which font do you prefer?", ""),
])
def test_asks_permission(answer, verb):
    assert asks_permission(answer) == verb
