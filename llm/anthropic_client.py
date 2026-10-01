"""Anthropic Messages API client that accepts OpenAI-style messages and tools."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator

import requests

ANTHROPIC_VERSION = "2023-06-01"
_STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls", "refusal": "content_filter"}


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(i.get("text") or "") if isinstance(i, dict) else str(i) for i in content if i)
    return json.dumps(content, ensure_ascii=False, default=str)


def _blocks(content: Any) -> list[dict]:
    if not isinstance(content, list):
        text = _text_of(content)
        return [{"type": "text", "text": text}] if text else []
    out: list[dict] = []
    for item in content:
        if isinstance(item, str):
            if item:
                out.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "image":
            out.append({"type": "image", "source": item.get("source")})
        elif kind in ("image_url", "input_image"):
            value = item.get("image_url")
            url = value.get("url") if isinstance(value, dict) else value
            if not isinstance(url, str) or not url:
                continue
            if url.startswith("data:"):
                header, _, data = url.partition(",")
                mime = header[5:].split(";", 1)[0] or "image/png"
                out.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
            else:
                out.append({"type": "image", "source": {"type": "url", "url": url}})
        elif item.get("text"):
            out.append({"type": "text", "text": str(item["text"])})
    return out


def convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = (msg.get("role") or "").lower()
        if role in ("system", "developer"):
            text = _text_of(msg.get("content"))
            if text.strip():
                system.append(text)
        elif role == "assistant":
            blocks = [b for b in _blocks(msg.get("content")) if b["type"] == "text" and b["text"].strip()]
            for tc in msg.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                if not fn.get("name"):
                    continue
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    args = {"_raw": raw}
                blocks.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{len(out)}",
                               "name": fn["name"], "input": args if isinstance(args, dict) else {"value": args}})
            push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": msg.get("tool_call_id") or "",
                           "content": _text_of(msg.get("content")) or "(empty)"}])
        else:
            push("user", _blocks(msg.get("content")) or [{"type": "text", "text": "(empty)"}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return "\n\n".join(system), out


def convert_tools(tools: list[dict] | None) -> list[dict]:
    out = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) and tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({"name": fn["name"], "description": fn.get("description") or "",
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, get_config: Callable[[], dict[str, str]]):
        self._get_config = get_config

    def _post(self, payload: dict, *, stream: bool, timeout) -> requests.Response:
        cfg = self._get_config()
        if not cfg.get("api_key"):
            raise ValueError("Anthropic API key not set (LiveCode Settings > Models)")
        headers = {"x-api-key": cfg["api_key"], "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        resp = requests.post(f"{cfg.get('base_url') or 'https://api.anthropic.com/v1'}/messages",
                             headers=headers, json=payload, stream=stream, timeout=timeout)
        if resp.status_code != 200:
            raise Exception(f"Anthropic API error: {resp.status_code} - {resp.text[:2000]}")
        return resp

    def _payload(self, model_id, messages, *, max_tokens, temperature=None, tools=None, tool_choice=None, stream=False):
        system, msgs = convert_messages(messages)
        payload: dict[str, Any] = {"model": model_id, "max_tokens": int(max_tokens), "messages": msgs}
        if system:
            payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if temperature is not None:
            payload["temperature"] = temperature
        converted = convert_tools(tools)
        if converted:
            payload["tools"] = converted
            choice = (tool_choice or "auto").lower()
            payload["tool_choice"] = {"type": {"required": "any", "none": "none"}.get(choice, "auto")}
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, model_id: str, messages: list[dict], *, timeout=30, **_: Any) -> str:
        data = self._post(self._payload(model_id, messages, max_tokens=4096, temperature=0.3), stream=False, timeout=timeout).json()
        return "".join(b.get("text") or "" for b in data.get("content") or [] if b.get("type") == "text")

    @staticmethod
    def _events(resp) -> Iterator[dict]:
        for line in resp.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8", "replace")
            if text.startswith("data:"):
                try:
                    yield json.loads(text[5:].strip())
                except json.JSONDecodeError:
                    continue

    def stream(self, model_id: str, messages: list[dict], *, max_tokens: int = 16_000, timeout=60) -> Iterator[str]:
        resp = self._post(self._payload(model_id, messages, max_tokens=max_tokens, temperature=0.7, stream=True), stream=True, timeout=timeout)
        for ev in self._events(resp):
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                yield delta.get("text") or ""

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
        payload = self._payload(model_id, messages, max_tokens=max_completion_tokens, tools=tools, tool_choice=tool_choice, stream=True)
        for attempt in range(3):
            try:
                resp = self._post(payload, stream=True, timeout=(30, 300))
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt == 2:
                    raise
                if on_retry:
                    try:
                        on_retry(attempt, 3, e)
                    except Exception:
                        pass
                time.sleep(2 ** attempt)

        content: list[str] = []
        reasoning: list[str] = []
        blocks: dict[int, dict] = {}
        tool_order: list[int] = []
        usage: dict[str, int] = {}
        finish = None
        for ev in self._events(resp):
            kind = ev.get("type")
            if kind == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                usage["prompt_tokens"] = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                if u.get("cache_read_input_tokens"):
                    usage["cached_tokens"] = int(u["cache_read_input_tokens"])
            elif kind == "content_block_start":
                idx = int(ev.get("index") or 0)
                block = ev.get("content_block") or {}
                blocks[idx] = {"type": block.get("type"), "id": block.get("id"), "name": block.get("name"), "json": ""}
                if block.get("type") == "tool_use":
                    tool_order.append(idx)
                    if on_tool_call_delta:
                        on_tool_call_delta({"index": len(tool_order) - 1, "id": block.get("id"), "type": "function",
                                            "function": {"name": block.get("name"), "arguments": ""}})
            elif kind == "content_block_delta":
                idx = int(ev.get("index") or 0)
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    text = delta.get("text") or ""
                    content.append(text)
                    if on_content_delta:
                        on_content_delta(text)
                elif dtype == "thinking_delta":
                    text = delta.get("thinking") or ""
                    reasoning.append(text)
                    if on_thought_delta:
                        on_thought_delta(text)
                elif dtype == "input_json_delta" and idx in blocks:
                    part = delta.get("partial_json") or ""
                    blocks[idx]["json"] += part
                    if on_tool_call_delta and idx in tool_order:
                        on_tool_call_delta({"index": tool_order.index(idx), "function": {"arguments": part}})
            elif kind == "message_delta":
                stop = (ev.get("delta") or {}).get("stop_reason")
                if stop:
                    finish = _STOP.get(stop, stop)
                u = ev.get("usage") or {}
                if u.get("output_tokens") is not None:
                    usage["completion_tokens"] = int(u["output_tokens"])
            elif kind == "error":
                raise Exception(f"Anthropic stream error: {ev.get('error')}")
        tool_calls = [{"id": blocks[i]["id"], "type": "function",
                       "function": {"name": blocks[i]["name"], "arguments": blocks[i]["json"] or "{}"}} for i in tool_order]
        if usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
            usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        out = {"content": "".join(content), "reasoning_content": "".join(reasoning),
               "tool_calls": tool_calls or None, "finish_reason": finish}
        out.update(usage)
        return out
