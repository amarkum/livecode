from __future__ import annotations

from typing import Any, Callable, Iterator, Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self,
        model: str,
        messages: list[dict],
        *,
        prompt_cache_key: str | None = None,
        timeout: int = 30,
    ) -> str: ...

    def stream(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 16_000,
        timeout: int = 60,
    ) -> Iterator[str]: ...

    def complete_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        *,
        max_completion_tokens: int = 16000,
        tool_choice: str = "auto",
        prompt_cache_key: str | None = None,
        on_thought_delta: Callable[[str], None] | None = None,
        on_retry: Callable[[int, int, Exception], None] | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_tool_call_delta: Callable[[dict], None] | None = None,
    ) -> dict[str, Any]: ...

    def supports_model(self, model: str) -> bool: ...

    def pick_model(
        self,
        user_model: str | None = None,
        *,
        content_chars: int = 0,
        file_count: int = 0,
        task: str = "chat",
    ) -> str: ...
