"""How a request is read before any model sees it: closing remarks, questions, version bumps, design work."""
import pytest

from livecode.routing import (
    authorized_final_actions,
    heuristic_classification,
    is_acknowledgement,
    is_hard_task,
    is_question,
    needs_codebase_evidence,
    user_requests_design_work,
    user_requests_ui_check,
)


@pytest.mark.parametrize("text", ["thanks, it works", "perfect, looks good now!", "ok great", "Thank you! That fixed it.", "lgtm", "nice, all good"])
def test_closing_remarks_are_chat_only(text):
    assert is_acknowledgement(text)
    cls = heuristic_classification(text, has_prior_turns=True)
    assert cls and cls["chat_only"] and not cls["is_actionable"]
    assert not needs_codebase_evidence(text, has_prior_turns=True)


@pytest.mark.parametrize("text", ["do the same for that one", "now fix it in the other file too", "and make these green"])
def test_real_follow_ups_still_need_the_code(text):
    assert needs_codebase_evidence(text, has_prior_turns=True)


def test_a_question_about_versions_is_not_a_version_bump():
    assert heuristic_classification("what version of node do I need to install?", has_prior_turns=False) is None
    bump = heuristic_classification("bump the version to 2.1.0", has_prior_turns=False)
    assert bump and bump["goal_kind"] == "code_change" and bump["edit_scope"] == "single_line"
    assert heuristic_classification("set version = '1.4.0' in pyproject", has_prior_turns=False)["goal_kind"] == "code_change"


@pytest.mark.parametrize("text, question, hard", [
    ("why is the implementation of foo slow?", True, False),
    ("could you explain how the migration works?", True, False),
    ("refactor the whole codebase to use async", False, True),
    ("Could you refactor the whole codebase to use async?", False, True),
    ("implement the new billing flow across all modules", False, True),
    ("how do I add a route?", True, False),
])
def test_questions_are_not_hard_tasks(text, question, hard):
    assert is_question(text) is question
    assert is_hard_task(text) is hard


@pytest.mark.parametrize("text, design", [
    ("what does this design pattern do?", False),
    ("implement the design system tokens", False),
    ("explain the design decision behind the cache", False),
    ("make the page look exactly like this design", True),
    ("match the Figma mockup", True),
    ("here is the mockup, build the header", True),
])
def test_design_work_is_about_matching_a_design(text, design):
    assert user_requests_design_work(text) is design


@pytest.mark.parametrize("text, check", [
    ("make it blue and check it in the browser", True),
    ("update the header and take a screenshot", True),
    ("dont check the page visually", False),
    ("don't take a screenshot, just change the colour", False),
    ("fix the localhost CORS bug in server.py", False),
    ("make the button blue", False),
])
def test_asking_to_see_the_ui_honours_negation(text, check):
    assert user_requests_ui_check(text) is check


@pytest.mark.parametrize("text", [
    "add a POST /users endpoint",
    "fix the email validation on the checkout page",
    "show the order status",
    "apply the migration and register the route",
    "wire the submit button to the handler",
    "write the tweet component",
    "Don't, under any circumstances, send it",
    "Draft an email to Bob but wait for my OK before sending",
    "add a signup page",
])
def test_coding_vocabulary_authorizes_nothing(text):
    assert authorized_final_actions(text) == []


@pytest.mark.parametrize("text, kinds", [
    ("email Sam about the outage", ["send"]),
    ("text him the address", ["send"]),
    ("order 2 pizzas from dominos", ["buy"]),
    ("pay the invoice on stripe", ["buy"]),
    ("tweet it", ["post"]),
    ("apply for the job on linkedin", ["submit"]),
])
def test_explicit_instructions_do(text, kinds):
    assert authorized_final_actions(text) == kinds
