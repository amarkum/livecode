"""Runs real agent turns (run_livecode_turn: routing, prompts, gates, tools) against a scripted model.

A script is a list of steps; each step is called with the messages and tool names of that model call
and returns the model's reply: text, tool calls, or both. The classifier's and summariser's calls (made
without tools) get a fixed answer. Every call is recorded, so a test can check what the harness showed
the model (its notes, reminders and tool results) and which tools it offered."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

CLASSIFICATION = {
    "goal_kind": "code_change", "is_actionable": True, "chat_only": False, "complexity": "simple",
    "edit_scope": "single_file", "needs_code_execution": False,
}


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(text: str = "", *tool_calls: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"content": text, "prompt_tokens": 10, "completion_tokens": 5}
    if tool_calls:
        out["tool_calls"] = list(tool_calls)
    return out


@dataclass
class Call:
    messages: list[dict[str, Any]]
    tools: list[str]

    def text(self) -> str:
        parts = []
        for m in self.messages:
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
            parts.append(str(content or ""))
        return "\n".join(parts)

    def tool_results(self) -> list[dict[str, Any]]:
        out = []
        for m in self.messages:
            if m.get("role") == "tool":
                try:
                    out.append(json.loads(m.get("content") or "{}"))
                except (TypeError, ValueError):
                    out.append({"raw": m.get("content")})
        return out


@dataclass(eq=False)
class ScriptedModel:
    steps: list[Callable[[Call], dict[str, Any]]]
    final: str = "Done."
    calls: list[Call] = field(default_factory=list)
    side_calls: int = 0

    def __call__(self, model: str, messages: list, tools: list, **kwargs: Any) -> dict[str, Any]:
        if not tools:
            self.side_calls += 1
            return {"content": json.dumps(CLASSIFICATION)}
        names = [str((t.get("function") or {}).get("name") or "") for t in tools]
        record = Call([dict(m) for m in messages], names)
        self.calls.append(record)
        if len(self.calls) > 40:
            raise RuntimeError("the scripted turn did not end")
        step = self.steps[len(self.calls) - 1] if len(self.calls) <= len(self.steps) else (lambda c: reply(self.final))
        return step(record)


class _Socket:
    def emit(self, *args: Any, **kwargs: Any) -> None:
        pass


def run_turn(project: str, question: str, model: ScriptedModel, *, images: list[str] | None = None,
             session_id: str | None = None, browser: bool = True, mode: str = "agent") -> dict[str, Any]:
    """Runs one turn; returns the final answer and every event the harness streamed."""
    from livecode.harness import run_livecode_turn
    from livecode.host import helpers

    content: Any = question
    if images:
        content = [{"type": "text", "text": question}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
    events: list[dict[str, Any]] = []
    for chunk in run_livecode_turn(
        project, question, [], user_content=content, user_model="test-model", call_with_tools=model,
        is_azure_model=lambda m: True, repo_grep_fn=helpers._repo_grep, repo_read_fn=helpers._repo_read_file,
        repo_list_fn=helpers._repo_list_dir, repo_ast_fn=helpers._repo_ast_symbols,
        create_diff_html_fn=helpers.create_diff_html, execute_command_pty_fn=lambda *a, **k: None,
        socketio=_Socket(), session_id=session_id or f"test-{uuid.uuid4().hex[:8]}",
        enable_browser_tools=browser, supports_images_fn=lambda m: True, mode=mode,
    ):
        if chunk.startswith("data: "):
            try:
                events.append(json.loads(chunk[6:].strip()))
            except ValueError:
                pass
    done = [e for e in events if e.get("done")]
    return {"answer": (done[-1].get("answer") if done else ""), "events": events,
            "errors": [e["error"] for e in events if e.get("error")]}
