"""OpenAI-compatible chat completions client.

Serves OpenAI plus every provider that speaks the same API: OpenRouter, Groq,
Together, Mistral, DeepSeek, and local open-source runtimes (Ollama, LM Studio).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Iterator

import requests

from .retry import call_with_retries, raise_for_status, stream_error

logger = logging.getLogger(__name__)

_KEEP_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id")


def _uses_completion_tokens(model_id: str) -> bool:
    m = model_id.lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4", "gpt-4.1"))


def _clean_messages(messages: list[dict]) -> list[dict]:
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        clean = {k: msg[k] for k in _KEEP_KEYS if k in msg and msg[k] is not None}
        if clean.get("role") == "assistant" and not clean.get("content") and clean.get("tool_calls"):
            clean["content"] = None
        if clean.get("role") != "tool":
            clean.pop("tool_call_id", None)
        if clean.get("tool_calls"):
            # Drop provider-specific extras such as Gemini's thought_signature.
            clean["tool_calls"] = [
                {k: v for k, v in tc.items() if k in ("id", "type", "function")} if isinstance(tc, dict) else tc
                for tc in clean["tool_calls"]
            ]
        out.append(clean)
    return out


def _usage(usage: dict | None) -> dict:
    if not isinstance(usage, dict):
        return {}
    out = {}
    for src, dest in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
        if usage.get(src) is not None:
            out[dest] = int(usage[src])
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(usage.get("prompt_tokens_details"), dict) else None
    if cached is not None:
        out["cached_tokens"] = int(cached)
    return out


class OpenAICompatClient:
    def __init__(self, provider: str, get_config: Callable[[], dict[str, str]]):
        self.provider = provider
        self._get_config = get_config

    def _request(self, payload: dict, *, stream: bool, timeout) -> requests.Response:
        cfg = self._get_config()
        base = cfg.get("base_url") or ""
        if not base:
            raise ValueError(f"{self.provider}: base URL is not set (LiveCode Settings > Models)")
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        if self.provider == "openrouter":
            headers["X-Title"] = "LiveCode"
        resp = requests.post(f"{base}/chat/completions", headers=headers, json=payload, stream=stream, timeout=timeout)
        raise_for_status(resp, self.provider)
        return resp

    def _payload(self, model_id: str, messages: list[dict], *, max_tokens: int | None, temperature: float | None,
                 tools: list[dict] | None = None, tool_choice: str | None = None, stream: bool = False) -> dict:
        payload: dict[str, Any] = {"model": model_id, "messages": _clean_messages(messages)}
        reasoning = _uses_completion_tokens(model_id) and self.provider == "openai"
        if max_tokens:
            payload["max_completion_tokens" if reasoning else "max_tokens"] = int(max_tokens)
        if temperature is not None and not reasoning:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if stream:
            payload["stream"] = True
            if self.provider in ("openai", "openrouter", "groq", "together", "deepseek", "mistral"):
                payload["stream_options"] = {"include_usage": True}
        return payload

    def complete(self, model_id: str, messages: list[dict], *, timeout=30, **_: Any) -> str:
        payload = self._payload(model_id, messages, max_tokens=4096, temperature=0.3)
        choices = call_with_retries(lambda: self._request(payload, stream=False, timeout=timeout).json(), retries=2).get("choices") or []
        if not choices:
            raise Exception(f"{self.provider} returned no choices")
        return (choices[0].get("message") or {}).get("content") or ""

    def stream(self, model_id: str, messages: list[dict], *, max_tokens: int = 16_000, timeout=60) -> Iterator[str]:
        resp = self._request(self._payload(model_id, messages, max_tokens=max_tokens, temperature=0.7, stream=True), stream=True, timeout=timeout)
        for chunk in self._iter_sse(resp):
            for choice in chunk.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    yield text

    @staticmethod
    def _iter_sse(resp) -> Iterator[dict]:
        for line in resp.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8", "replace")
            if not text.startswith("data:"):
                continue
            data = text[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue

    def complete_with_tools(
        self,
        model_id: str,
        messages: list[dict],
        tools: list[dict],
        *,
        max_completion_tokens: int = 16000,
        tool_choice: str = "auto",
        on_thought_delta: Callable[[str], None] | None = None,
        on_retry: Callable[[int, int, Exception], None] | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_tool_call_delta: Callable[[dict], None] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        payload = self._payload(model_id, messages, max_tokens=max_completion_tokens, temperature=None,
                                tools=tools, tool_choice=tool_choice, stream=True)

        def _attempt() -> dict[str, Any]:
            resp = self._request(payload, stream=True, timeout=(30, 300))
            return self._collect(resp, on_thought_delta, on_content_delta, on_tool_call_delta)

        return call_with_retries(_attempt, on_retry=on_retry)

    def _collect(self, resp, on_thought_delta, on_content_delta, on_tool_call_delta) -> dict[str, Any]:
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        usage: dict = {}
        for chunk in self._iter_sse(resp):
            if chunk.get("error"):
                # OpenRouter and some proxies report upstream failures inside a 200 stream.
                raise stream_error(self.provider, chunk["error"])
            usage.update(_usage(chunk.get("usage")))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                think = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(think, str) and think:
                    reasoning.append(think)
                    if on_thought_delta:
                        on_thought_delta(think)
                if delta.get("content"):
                    content.append(delta["content"])
                    if on_content_delta:
                        on_content_delta(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    acc = calls.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
                    if on_tool_call_delta:
                        on_tool_call_delta({**tc, "index": idx})
        tool_calls = []
        for idx in sorted(calls):
            tc = calls[idx]
            if not tc["function"]["name"]:
                continue
            if not tc["id"]:
                tc["id"] = f"call_{idx}_{int(time.time() * 1000)}"
            tc["function"]["arguments"] = tc["function"]["arguments"] or "{}"
            tool_calls.append(tc)
        out = {"content": "".join(content), "reasoning_content": "".join(reasoning),
               "tool_calls": tool_calls or None, "finish_reason": finish}
        out.update(usage)
        return out
