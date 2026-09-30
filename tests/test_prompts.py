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
    text = prompts.UI_VERIFY_TEMPLATE.format(files="src/components/Button.tsx")
    assert "src/components/Button.tsx" in text and "reload {hard: true}" in text
