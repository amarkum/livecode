"""The harness runs the project's own checks after the agent edits code without checking it, and holds
the turn open while they fail."""
import os
import tempfile

import pytest

from livecode import agent_settings
from livecode.verification import detect_check_command, failure_tail
from turn_driver import ScriptedModel, call, reply, run_turn

MAKEFILE = "test:\n\t@grep -q '#2563eb' src/components/Button.tsx && echo 'colour ok' || (echo 'FAIL: button is not blue'; exit 1)\n"
BUTTON = "export const Button = () => <button style={{ background: '#16a34a' }}>New order</button>;\n"


def _project(files):
    root = tempfile.mkdtemp(prefix="lc-checks-")
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
    return root


def _check_events(out):
    """The progress events the page gets for the automatic check (a terminal card like any run_command)."""
    return [e for e in out["progress"] if e.get("type") in ("tool_call", "tool_result") and str(e.get("tool_call_id", "")).startswith("auto-check")]


def _edit(new):
    return call("edit_file", file_path="src/components/Button.tsx", old_string="#16a34a", new_string=new)


@pytest.mark.parametrize("files, command", [
    ({"package.json": '{"scripts": {"test": "vitest run"}}'}, "npm test --silent"),
    ({"package.json": '{"scripts": {"test": "vitest run"}}', "pnpm-lock.yaml": ""}, "pnpm test"),
    ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}'}, None),
    ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"}, "python -m pytest -q"),
    ({"tests/test_x.py": "def test_x(): pass\n"}, "python -m pytest -q"),
    ({"Cargo.toml": "[package]\nname='x'\n"}, "cargo test -q"),
    ({"go.mod": "module x\n"}, "go test ./..."),
    ({"Makefile": "build:\n\techo\ntest:\n\tpytest\n"}, "make test"),
    ({"package.json": "{}", "tsconfig.json": "{}"}, "npx tsc --noEmit -p ."),
    ({"README.md": "nothing"}, None),
])
def test_detects_the_projects_check_command(files, command):
    found = detect_check_command(_project(files))
    assert (found or {}).get("command") == command


def test_failure_tail_keeps_the_first_error_and_the_end():
    out = "\n".join(f"line {i}" for i in range(500)) + "\nFAILED tests/test_a.py::test_b - AssertionError\n" + "\n".join(f"tail {i}" for i in range(100)) + "\n1 failed"
    tail = failure_tail(out, limit=1500)
    assert "FAILED tests/test_a.py" in tail and tail.endswith("1 failed") and len(tail) <= 1500


def test_a_failing_check_holds_the_turn_until_the_agent_fixes_it():
    project = _project({"Makefile": MAKEFILE, "src/components/Button.tsx": BUTTON})
    model = ScriptedModel([
        lambda c: reply("Changing the colour.", _edit("#ff0000")),        # wrong colour: make test fails
        lambda c: reply("The button is red now."),                        # tries to finish; checks run and fail
        lambda c: reply("Fixing it.", call("edit_file", file_path="src/components/Button.tsx", old_string="#ff0000", new_string="#2563eb")),
        lambda c: reply("The button is blue now."),                       # checks run again and pass
    ], final="Done.")
    out = run_turn(project, "make the button blue", model, browser=False)
    assert not out["errors"]
    reminder = model.calls[2].text()
    assert "project's checks fail after your changes" in reminder and "`make test`" in reminder and "FAIL: button is not blue" in reminder
    events = _check_events(out)
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert events[0]["tool"] == "run_command" and events[0]["args"]["command"] == "make test"
    assert events[1]["result"]["exit_code"] != 0 and events[3]["result"]["exit_code"] == 0
    assert out["answer"].startswith("The button is blue now.") and "Checks: `make test` passed." in out["answer"]
    assert len(model.calls) == 4


def test_no_run_when_the_agent_already_ran_the_tests_after_its_edit():
    project = _project({"Makefile": MAKEFILE, "src/components/Button.tsx": BUTTON})
    model = ScriptedModel([
        lambda c: reply("Changing the colour.", _edit("#2563eb")),
        lambda c: reply("Running the tests.", call("run_command", command="make test")),
        lambda c: reply("Blue, and the tests pass."),
    ])
    out = run_turn(project, "make the button blue", model, browser=False)
    assert not _check_events(out) and len(model.calls) == 3
    assert "Checks:" not in out["answer"]


def test_finishing_after_the_agents_own_failing_test_run_is_held_once():
    project = _project({"Makefile": MAKEFILE, "src/components/Button.tsx": BUTTON})
    model = ScriptedModel([
        lambda c: reply("Changing the colour.", _edit("#ff0000")),
        lambda c: reply("Running the tests.", call("run_command", command="make test")),
        lambda c: reply("Done, the button is red."),                     # the tests it ran failed: held
        lambda c: reply("The test expects blue, which was not what was asked; leaving it red as requested."),
    ])
    out = run_turn(project, "make the button red", model, browser=False)
    assert "last test run after your changes failed" in model.calls[3].text()
    assert out["answer"].startswith("The test expects blue") and len(model.calls) == 4


def test_no_run_for_a_project_without_checks_or_when_switched_off(tmp_path, monkeypatch):
    project = _project({"src/components/Button.tsx": BUTTON})
    model = ScriptedModel([lambda c: reply("Changing it.", _edit("#2563eb")), lambda c: reply("Done.")])
    out = run_turn(project, "make the button blue", model, browser=False)
    assert not _check_events(out) and len(model.calls) == 2

    project = _project({"Makefile": MAKEFILE, "src/components/Button.tsx": BUTTON})
    monkeypatch.setattr(agent_settings, "CONFIG_PATH", str(tmp_path / "agent.json"))
    agent_settings.update({"auto_checks": False})
    model = ScriptedModel([lambda c: reply("Changing it.", _edit("#ff0000")), lambda c: reply("Done.")])
    out = run_turn(project, "make the button red", model, browser=False)
    assert not _check_events(out) and len(model.calls) == 2
