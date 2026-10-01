"""The prompts render: a stray brace in the system prompt's f-string broke every turn once."""
import string

from livecode import prompts


def test_system_prompt_renders_literal_braces():
    text = prompts.build_system_prompt("/tmp/project", has_project_rules=False)
    assert "reload {hard: true}" in text
    assert "restart_command {command_id}" in text
    assert "restart_command" in text and "browser navigate" in text


def test_every_template_formats_with_its_fields():
    for name in dir(prompts):
        if not name.endswith("_TEMPLATE"):
            continue
        template = getattr(prompts, name)
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
        template.format(**{f: "x" for f in fields})


def test_ui_verify_reminder_names_the_files():
    text = prompts.UI_VERIFY_TEMPLATE.format(files="src/components/Button.tsx", why="")
    assert "src/components/Button.tsx" in text and "reload {hard: true}" in text
    assert "crop" in text, "a Yes checks just the changed element"


def test_ui_verify_ask_reminder_asks_the_question_in_the_card():
    text = prompts.UI_VERIFY_ASK_TEMPLATE.format(files="src/components/Button.tsx")
    assert "ask_question" in text and prompts.UI_VERIFY_QUESTION in text


def test_system_prompt_asks_before_verifying_ui():
    text = prompts.build_system_prompt("/tmp/project", has_project_rules=False)
    assert prompts.UI_VERIFY_QUESTION in text


def test_no_hardcoded_python_quality_guidance_or_commit_trailer():
    text = prompts.build_system_prompt("/tmp/project", has_project_rules=False)
    for gone in ("pre-commit", "SonarQube", "ruff", "black --check", "committer@livecode.ai"):
        assert gone not in text
    assert not hasattr(prompts, "LIVECODE_COMPACT_SYSTEM_PROMPT")


def test_ui_checks_crop_the_shared_parent_and_measure():
    for text in (prompts.UI_VERIFY_TEMPLATE.format(files="a.tsx", why=""),
                 prompts.build_system_prompt("/tmp/project", has_project_rules=False)):
        assert "shared parent container" in text and "measurement" in text


def test_git_commits_run_as_written():
    from livecode import tools
    assert not hasattr(tools, "inject_livecode_commit_coauthor")
    spec = next(t for t in tools.LIVECODE_TOOLS if t["function"]["name"] == "run_command")
    assert "Co-authored-by" not in spec["function"]["description"]


READ_ONLY = ["glob_files", "grep_repo", "read_repo_file", "find_symbol", "ask_question", "attempt_completion"]


def test_the_prompt_lists_only_the_tools_offered():
    text = prompts.build_system_prompt("/tmp/project", mode="ask", tool_names=READ_ONLY)
    assert "**Tools this turn:** glob_files, grep_repo, read_repo_file, find_symbol, ask_question, attempt_completion." in text
    for absent in ("**Editing:**", "**Commands:**", "**Verifying:**", "**Parallel agents:**", "**Task list:**", "write_file", "run_command", "spawn_subagent"):
        assert absent not in text, absent
    assert "**Asking:**" in text and "shall I send it" not in text


def test_agent_mode_with_every_tool_has_the_working_sections():
    text = prompts.build_system_prompt("/tmp/project", mode="agent")
    for present in ("**Editing:**", "**Commands:**", "**Verifying:**", "**Checking UI changes:**", "**Parallel agents:**", "**Task list:**", prompts.UI_VERIFY_QUESTION):
        assert present in text, present
    assert "pre-authenticated" not in text and "LiveCode PAT" not in text


def test_no_browser_means_no_browser_sections():
    names = [n for n in prompts._DEFAULT_TOOL_NAMES if n != "browser"]
    text = prompts.build_system_prompt("/tmp/project", mode="agent", tool_names=names)
    assert "**Checking UI changes:**" not in text and "browser navigate" not in text
    with_browser = prompts.build_system_prompt("/tmp/project", mode="agent", tool_names=names + ["browser"])
    assert "**Checking UI changes:**" in with_browser and "**A stale page:**" in with_browser


def test_paragraphs_are_readable():
    text = prompts.build_system_prompt("/tmp/project", mode="agent")
    longest = max(len(p) for p in text.split("\n\n"))
    assert longest < 1500, longest
