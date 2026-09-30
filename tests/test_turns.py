"""Real agent turns (routing, prompts, gates and tools) driven by a scripted model: how LiveCode reacts
when the user says "go to this site and do this", changes UI code, or attaches a component screenshot."""
import base64
import os
import tempfile

import pytest

from turn_driver import ScriptedModel, call, reply, run_turn


def _project(files=None):
    root = tempfile.mkdtemp(prefix="lc-proj-")
    for rel, text in {"README.md": "demo\n", **(files or {})}.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
    return root


def _note(call_):
    return next((line for line in call_.text().splitlines() if "open it in the built-in browser" in line
                 or "see their app in the built-in browser" in line), "")


# ---- "Go to this site and do this" ----------------------------------------------------------------

def test_go_to_a_site_and_do_something_opens_the_browser_and_does_it(site, browser_ready):
    url = site + "/app.html"
    model = ScriptedModel([
        lambda c: reply("Opening it.", call("browser", action="navigate", url=url)),
        lambda c: reply("Clicking it.", call("browser", action="click", text="New order")),
        lambda c: reply("I opened the page and clicked New order."),
    ])
    out = run_turn(_project(), f"go to {url} and click New order", model)
    assert not out["errors"]
    first = model.calls[0]
    assert "browser" in first.tools
    note = _note(first)
    assert url in note and "click New order" in note and "do not ask whether to open the browser" in note
    navigated, clicked = model.calls[1].tool_results()[-1], model.calls[2].tool_results()[-1]
    assert navigated["success"] and navigated["url"] == url
    assert clicked["success"] and clicked["clicked"] == "New order"
    assert out["answer"].startswith("I opened the page")


@pytest.mark.parametrize("question, site, steps", [
    ("go to amazon and search for iphone 16", "amazon.com", "search for iphone 16"),
    ("open youtube and play lofi beats", "youtube.com", "play lofi beats"),
    ("visit https://example.com/pricing, click on Pro and take a screenshot", "https://example.com/pricing", "click on Pro and take a screenshot"),
])
def test_named_sites_get_a_note_with_the_steps(question, site, steps):
    model = ScriptedModel([lambda c: reply("Noted.")])
    run_turn(_project(), question, model)
    first = model.calls[0]
    assert "browser" in first.tools
    note = _note(first)
    assert f"open {site}:" in note and steps in note


def test_check_it_in_the_browser_means_the_users_own_app():
    model = ScriptedModel([lambda c: reply("Noted.")])
    run_turn(_project(), "open the dashboard in the browser and see if the chart loads", model)
    note = _note(model.calls[0])
    assert "see their app in the built-in browser" in note and "see if the chart loads" in note
    assert "command_status" in note and "start the dev server" in note


def test_no_note_and_no_browser_when_the_browser_is_off():
    model = ScriptedModel([lambda c: reply("Noted.")])
    run_turn(_project(), "go to github.com and check the issues", model, browser=False)
    assert "browser" not in model.calls[0].tools and not _note(model.calls[0])


def test_an_ordinary_coding_request_gets_no_browser_note():
    model = ScriptedModel([lambda c: reply("Noted.")])
    run_turn(_project(), "rename the helper in utils.py", model)
    assert not _note(model.calls[0])


# ---- UI changes are checked in the browser -----------------------------------------------------------

BUTTON = "export const Button = () => <button style={{ background: '#16a34a' }}>New order</button>;\n"


def _edit_button():
    return call("edit_file", file_path="src/components/Button.tsx", old_string="#16a34a", new_string="#2563eb")


def _reminders(model):
    return [i for i, c in enumerate(model.calls) if "You changed UI code" in c.text()]


def test_a_ui_change_is_not_finished_until_it_was_looked_at_in_the_browser(site, browser_ready):
    project = _project({"src/components/Button.tsx": BUTTON})
    model = ScriptedModel([
        lambda c: reply("Changing the colour.", _edit_button()),
        lambda c: reply("The button is blue now."),                       # tries to finish without looking
        lambda c: reply("Checking it.", call("browser", action="navigate", url=site + "/app.html")),
        lambda c: reply("Checked it in the browser: the button is blue."),
    ])
    out = run_turn(project, "make the New order button blue", model)
    assert not out["errors"]
    assert "#2563eb" in open(os.path.join(project, "src/components/Button.tsx")).read()
    first = _reminders(model)
    assert first and first[0] == 2, "the finish right after the edit is held back"
    assert "src/components/Button.tsx" in model.calls[2].text()
    assert len(model.calls) == 4, "once it looked, it may finish"
    assert out["answer"].startswith("Checked it in the browser")


def test_no_reminder_when_it_looks_right_after_the_change(site, browser_ready):
    project = _project({"src/components/Button.tsx": BUTTON})
    model = ScriptedModel([
        lambda c: reply("Changing it.", _edit_button()),
        lambda c: reply("Looking.", call("browser", action="navigate", url=site + "/app.html")),
        lambda c: reply("Done, and checked."),
    ])
    run_turn(project, "make the New order button blue", model)
    assert not _reminders(model) and len(model.calls) == 3


def test_no_reminder_for_code_that_is_not_ui():
    project = _project({"src/utils/math.ts": "export const add = (a: number, b: number) => a - b;\n"})
    model = ScriptedModel([
        lambda c: reply("Fixing it.", call("edit_file", file_path="src/utils/math.ts", old_string="a - b", new_string="a + b")),
        lambda c: reply("Fixed add()."),
    ])
    run_turn(project, "fix add in math.ts", model)
    assert not _reminders(model)


def test_the_reminder_gives_up_after_two_tries():
    project = _project({"src/components/Button.tsx": BUTTON})
    model = ScriptedModel([lambda c: reply("Changing it.", _edit_button())], final="There is no dev server here.")
    run_turn(project, "make the New order button blue", model)
    assert len(_reminders(model)) == 2


# ---- A screenshot of one component -----------------------------------------------------------------

def test_a_screenshot_of_a_card_is_compared_with_that_card(site, design_shots):
    with open(design_shots["card@2x"], "rb") as handle:
        image = "data:image/png;base64," + base64.b64encode(handle.read()).decode()
    model = ScriptedModel([
        lambda c: reply("Opening the app.", call("browser", action="navigate", url=site + "/app.html")),
        lambda c: reply("Comparing the card.", call("browser", action="compare")),
    ], final="The card needs 20px padding and a 12px radius.")
    out = run_turn(_project(), "make the order card look like this screenshot", model, images=[image])
    assert not out["errors"]
    system = model.calls[0].messages[0]["content"]
    assert "Decide the scope from what the design shows" in system
    result = model.calls[2].tool_results()[-1]
    assert result["success"] and result["mode"] == "element"
    located = result["located"]
    assert "article.card" in located["selector"] and located["scale"] == 2.0 and located["similar"] >= 3
    card = next(e for e in result["elements"] if e["element"].startswith("article.card"))
    assert any("padding about 20px in the design (14px here)" in f for f in card["findings"])
    assert not any("other words" in f for e in result["elements"] for f in e["findings"])
    # It keeps going until the card matches: finishing now is held back by the design gate.
    assert any("does not match the design yet" in c.text() for c in model.calls[3:])
