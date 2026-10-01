from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Callable

import requests

LIVECODE_LOGGER = logging.getLogger("LiveCode")
logger = logging.getLogger(__name__)


def _azure_usage_fields(usage: dict | None) -> dict:
    if not isinstance(usage, dict) or not usage:
        return {}
    out: dict = {}
    for src, dest in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ):
        val = usage.get(src)
        if val is not None and dest not in out:
            try:
                out[dest] = int(val)
            except (TypeError, ValueError):
                pass
    details = (
        usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    )
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if cached is not None:
            try:
                out["cached_tokens"] = int(cached)
            except (TypeError, ValueError):
                pass
    return out


def _log_azure_token_usage(usage_fields: dict, *, streaming: bool = False) -> None:
    if not usage_fields:
        return
    prompt = usage_fields.get("prompt_tokens")
    completion = usage_fields.get("completion_tokens")
    total = usage_fields.get("total_tokens")
    cached = usage_fields.get("cached_tokens")
    bits = []
    if prompt is not None:
        bits.append(f"in={prompt}")
    if completion is not None:
        bits.append(f"out={completion}")
    if total is not None:
        bits.append(f"total={total}")
    if cached is not None:
        bits.append(f"cached={cached}")
    if not bits:
        return
    kind = "streaming" if streaming else "non-streaming"
    LIVECODE_LOGGER.debug("Azure OpenAI tokens (%s): %s", kind, " ".join(bits))


def _apply_streaming_tool_call_delta(tool_call_acc, tc_delta):
    idx = int(tc_delta.get("index") or 0)
    if idx not in tool_call_acc:
        tool_call_acc[idx] = {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        }
    entry = tool_call_acc[idx]
    if tc_delta.get("id"):
        entry["id"] += tc_delta["id"]
    func = tc_delta.get("function") or {}
    if func.get("name"):
        entry["function"]["name"] += func["name"]
    if func.get("arguments"):
        entry["function"]["arguments"] += func["arguments"]


def _tool_calls_from_stream_acc(tool_call_acc):
    if not tool_call_acc:
        return None
    out = []
    for idx in sorted(tool_call_acc):
        entry = tool_call_acc[idx]
        fn = entry.get("function") or {}
        if not entry.get("id") and not fn.get("name"):
            continue
        out.append(entry)
    return out or None


def _parse_azure_sse_tool_stream(
    response,
    on_thought_delta=None,
    on_content_delta=None,
    request_id: str = "",
    on_tool_call_delta=None,
):
    content_parts = []
    reasoning_parts = []
    tool_call_acc = {}
    usage_fields: dict = {}
    stream_started = time.monotonic()
    first_sse_seen = False
    first_reasoning_seen = False
    first_content_seen = False
    first_tool_seen = False
    usage_seen = False
    chunk_count = 0
    finish_reason = None
    prefix = f"Azure tools stream {request_id}".strip()

    for line in response.iter_lines():
        if not line:
            continue
        line_text = line.decode("utf-8")
        if not line_text.startswith("data: "):
            continue
        chunk_count += 1
        if not first_sse_seen:
            first_sse_seen = True
            LIVECODE_LOGGER.debug(
                "%s first_sse elapsed=%.2fs", prefix, time.monotonic() - stream_started
            )
        data_content = line_text[6:]
        if data_content.strip() == "[DONE]":
            LIVECODE_LOGGER.debug(
                "%s done_marker chunks=%d elapsed=%.2fs",
                prefix,
                chunk_count,
                time.monotonic() - stream_started,
            )
            break
        try:
            chunk = json.loads(data_content)
        except json.JSONDecodeError:
            continue

        chunk_usage = _azure_usage_fields(chunk.get("usage") or {})
        if chunk_usage:
            usage_fields.update(chunk_usage)
            if not usage_seen:
                usage_seen = True
                LIVECODE_LOGGER.debug(
                    "%s usage_chunk chunks=%d elapsed=%.2fs",
                    prefix,
                    chunk_count,
                    time.monotonic() - stream_started,
                )

        choices = chunk.get("choices") or []
        if not choices:
            continue
        if choices[0].get("finish_reason"):
            finish_reason = choices[0]["finish_reason"]
        delta = choices[0].get("delta") or {}

        reasoning_text = delta.get("reasoning_content") or ""
        if reasoning_text:
            reasoning_parts.append(reasoning_text)
            if not first_reasoning_seen:
                first_reasoning_seen = True
                LIVECODE_LOGGER.debug(
                    "%s first_reasoning_delta elapsed=%.2fs",
                    prefix,
                    time.monotonic() - stream_started,
                )
            if on_thought_delta:
                on_thought_delta(reasoning_text)

        content_text = delta.get("content") or ""
        if content_text:
            content_parts.append(content_text)
            if not first_content_seen:
                first_content_seen = True
                LIVECODE_LOGGER.debug(
                    "%s first_content_delta elapsed=%.2fs",
                    prefix,
                    time.monotonic() - stream_started,
                )
            if on_content_delta:
                on_content_delta(content_text)
            elif on_thought_delta:
                on_thought_delta(content_text)

        tool_deltas = delta.get("tool_calls") or []
        if tool_deltas and not first_tool_seen:
            first_tool_seen = True
            LIVECODE_LOGGER.debug(
                "%s first_tool_delta elapsed=%.2fs",
                prefix,
                time.monotonic() - stream_started,
            )
        for tc_delta in tool_deltas:
            _apply_streaming_tool_call_delta(tool_call_acc, tc_delta)
            if on_tool_call_delta:
                on_tool_call_delta(tc_delta)

    tool_calls = _tool_calls_from_stream_acc(tool_call_acc)
    out = {
        "content": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
    }
    out.update(usage_fields)
    LIVECODE_LOGGER.debug(
        "%s parsed chunks=%d content_len=%d reasoning_len=%d tool_calls=%d usage=%s elapsed=%.2fs",
        prefix,
        chunk_count,
        len(out["content"]),
        len(out["reasoning_content"]),
        len(tool_calls or []),
        bool(usage_fields),
        time.monotonic() - stream_started,
    )
    return out


def _azure_tools_read_timeout(default=60):
    raw = (os.environ.get("AZURE_TOOLS_READ_TIMEOUT") or "").strip()
    if not raw:
        return int(default)
    try:
        return max(15, min(300, int(raw)))
    except (TypeError, ValueError):
        return int(default)


def _azure_tools_response_is_empty(out: dict | None) -> bool:
    if not isinstance(out, dict):
        return True
    content = (out.get("content") or "").strip()
    reasoning = (out.get("reasoning_content") or "").strip()
    tool_calls = out.get("tool_calls")
    has_tools = bool(tool_calls) if tool_calls is not None else False
    return (not content) and (not reasoning) and (not has_tools)


def _notify_azure_tools_retry(on_retry, attempt, max_retries, error):
    if not on_retry:
        return
    try:
        on_retry(attempt + 1, max_retries, error)
    except Exception:
        logger.debug("Azure tools on_retry callback failed", exc_info=True)


class AzureOpenAIClient:
    def __init__(
        self,
        *,
        deployments: dict[str, str],
        host: str,
        api_version: str,
        get_token: Callable[[], str | None],
        fast_model: str,
        large_context_model: str,
        large_content_char_threshold: int = 600_000,
        large_file_count_threshold: int = 40,
        max_input_tokens_for_model: Callable[[str], int] | None = None,
    ):
        self._deployments = deployments
        self._host = host
        self._api_version = api_version
        self._get_token = get_token
        self._fast_model = fast_model
        self._large_context_model = large_context_model
        self._large_content_char_threshold = large_content_char_threshold
        self._large_file_count_threshold = large_file_count_threshold
        self._max_input_tokens_for_model = max_input_tokens_for_model

    def supports_model(self, model: str) -> bool:
        return model in self._deployments

    def pick_model(
        self,
        user_model: str | None = None,
        *,
        content_chars: int = 0,
        file_count: int = 0,
        task: str = "chat",
    ) -> str:
        model = (user_model or "").strip() or self._fast_model
        if not self.supports_model(model):
            return model
        if task == "fast":
            return self._fast_model
        if (
            task == "large"
            or content_chars > self._large_content_char_threshold
            or file_count > self._large_file_count_threshold
        ):
            return self._large_context_model
        if content_chars > 0 and self._max_input_tokens_for_model:
            max_input_tokens = self._max_input_tokens_for_model(model)
            if max_input_tokens and content_chars > max_input_tokens * 3:
                return self._large_context_model
        return model

    def _resolve_endpoint_and_token(self, model: str) -> tuple[str, str | None]:
        if model not in self._deployments:
            raise ValueError(f"Unknown Azure GPT model: {model}")
        deployment = self._deployments[model]
        endpoint = f"{self._host}/openai/deployments/{deployment}/chat/completions?api-version={self._api_version}"
        return endpoint, self._get_token()

    @staticmethod
    def _build_payload(model, messages, stream=False, temperature=None, max_tokens=None):
        payload = {
            "messages": messages,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_completion_tokens"] = max_tokens
        return payload

    def complete(
        self,
        model: str,
        messages: list[dict],
        *,
        prompt_cache_key: str | None = None,
        timeout: int = 30,
    ) -> str:
        endpoint, token = self._resolve_endpoint_and_token(model)
        if not endpoint:
            raise ValueError(f"Azure OpenAI endpoint not configured for model: {model}")
        if not token:
            raise ValueError(f"Service token not loaded for model: {model}")

        headers = {"Content-Type": "application/json", "api-key": token}
        payload = self._build_payload(
            model, messages, stream=False, temperature=0.3, max_tokens=4096
        )
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key

        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)

        if response.status_code != 200:
            raise Exception(
                f"Azure OpenAI API error: {response.status_code} - {response.text}"
            )

        result = response.json()
        usage_fields = _azure_usage_fields(result.get("usage") or {})
        _log_azure_token_usage(usage_fields, streaming=False)
        if "choices" in result and len(result["choices"]) > 0:
            message = result["choices"][0].get("message", {})
            content = message.get("content")
            if content is not None:
                return content
            LIVECODE_LOGGER.warning(
                f"Azure OpenAI response missing 'content' key. Full response: {json.dumps(result, default=str)[:500]}"
            )
            raise Exception("Azure OpenAI response missing 'content' key")
        raise Exception("Azure OpenAI returned invalid response format")

    def stream(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 16_000,
        timeout: int = 60,
    ):
        endpoint, token = self._resolve_endpoint_and_token(model)
        if not endpoint:
            raise ValueError(f"Azure OpenAI endpoint not configured for model: {model}")
        if not token:
            raise ValueError(f"Service token not loaded for model: {model}")

        headers = {"Content-Type": "application/json", "api-key": token}
        payload = self._build_payload(
            model, messages, stream=True, temperature=0.7, max_tokens=max_tokens
        )

        response = requests.post(
            endpoint, headers=headers, json=payload, stream=True, timeout=timeout
        )

        if response.status_code != 200:
            raise Exception(
                f"Azure OpenAI API error: {response.status_code} - {response.text}"
            )

        for line in response.iter_lines():
            if not line:
                continue
            line_text = line.decode("utf-8")
            if not line_text.startswith("data: "):
                continue
            data_content = line_text[6:]
            if data_content.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_content)
                if "choices" in chunk and len(chunk["choices"]) > 0:
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        yield delta["content"]
            except json.JSONDecodeError:
                continue

    def _call_with_tools_blocking(
        self,
        endpoint,
        headers,
        payload,
        connect_timeout=30,
        read_timeout=60,
        max_retries=3,
        on_retry=None,
        request_id: str = "",
    ):
        started = time.monotonic()
        prefix = f"Azure tools {request_id}".strip()

        for attempt in range(max_retries):
            try:
                if attempt == 0:
                    LIVECODE_LOGGER.debug(
                        "%s blocking_start messages=%d tools=%d tool_choice=%s timeout=(%s,%s) prompt_cache=%s",
                        prefix,
                        len(payload.get("messages") or []),
                        len(payload.get("tools") or []),
                        payload.get("tool_choice"),
                        connect_timeout,
                        read_timeout,
                        bool(payload.get("prompt_cache_key")),
                    )

                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=(connect_timeout, read_timeout),
                )
                LIVECODE_LOGGER.debug(
                    "%s blocking_http status=%s elapsed=%.2fs",
                    prefix,
                    response.status_code,
                    time.monotonic() - started,
                )
                if response.status_code != 200:
                    raise Exception(
                        f"Azure OpenAI API error: {response.status_code} - {response.text}"
                    )

                result = response.json()

                if "choices" not in result or len(result["choices"]) == 0:
                    raise Exception("Azure OpenAI returned invalid response format")

                choice = result["choices"][0]
                message = choice.get("message", {})

                out = {
                    "content": message.get("content", "") or "",
                    "reasoning_content": message.get("reasoning_content", "") or "",
                    "tool_calls": message.get("tool_calls", None),
                    "finish_reason": choice.get("finish_reason"),
                }
                usage_fields = _azure_usage_fields(result.get("usage") or {})
                out.update(usage_fields)
                _log_azure_token_usage(usage_fields, streaming=False)
                LIVECODE_LOGGER.debug(
                    "%s blocking_parsed content_len=%d reasoning_len=%d tool_calls=%d usage=%s elapsed=%.2fs",
                    prefix,
                    len(out.get("content") or ""),
                    len(out.get("reasoning_content") or ""),
                    len(out.get("tool_calls") or []),
                    bool(usage_fields),
                    time.monotonic() - started,
                )
                return out
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        "Azure OpenAI tools request failed (%s), attempt %s/%s; retrying",
                        e,
                        attempt + 1,
                        max_retries,
                    )
                    _notify_azure_tools_retry(on_retry, attempt, max_retries, e)
                    time.sleep(2**attempt)
                    continue
                raise

    def _call_with_tools_streaming(
        self,
        endpoint,
        headers,
        payload,
        on_thought_delta,
        connect_timeout=30,
        read_timeout=60,
        max_retries=3,
        on_retry=None,
        request_id: str = "",
        on_content_delta=None,
        on_tool_call_delta=None,
    ):
        started = time.monotonic()
        prefix = f"Azure tools {request_id}".strip()

        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}

        for attempt in range(max_retries):
            try:
                if attempt == 0:
                    LIVECODE_LOGGER.debug(
                        "%s streaming_start messages=%d tools=%d tool_choice=%s timeout=(%s,%s) prompt_cache=%s",
                        prefix,
                        len(payload.get("messages") or []),
                        len(payload.get("tools") or []),
                        payload.get("tool_choice"),
                        connect_timeout,
                        read_timeout,
                        bool(payload.get("prompt_cache_key")),
                    )

                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=stream_payload,
                    stream=True,
                    timeout=(connect_timeout, read_timeout),
                )
                LIVECODE_LOGGER.debug(
                    "%s streaming_http status=%s elapsed=%.2fs",
                    prefix,
                    response.status_code,
                    time.monotonic() - started,
                )
                if response.status_code != 200:
                    raise Exception(
                        f"Azure OpenAI API error: {response.status_code} - {response.text}"
                    )
                out = _parse_azure_sse_tool_stream(
                    response,
                    on_thought_delta=on_thought_delta,
                    on_content_delta=on_content_delta,
                    request_id=request_id,
                    on_tool_call_delta=on_tool_call_delta,
                )
                usage_fields = {
                    k: out[k]
                    for k in (
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "cached_tokens",
                    )
                    if k in out
                }
                _log_azure_token_usage(usage_fields, streaming=True)
                return out
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        "Azure OpenAI streaming tools request failed (%s), attempt %s/%s; retrying",
                        e,
                        attempt + 1,
                        max_retries,
                    )
                    _notify_azure_tools_retry(on_retry, attempt, max_retries, e)
                    time.sleep(2**attempt)
                    continue
                raise

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
    ) -> dict[str, Any]:
        request_id = f"azt_{uuid.uuid4().hex[:10]}"
        endpoint, token = self._resolve_endpoint_and_token(model)

        if not endpoint:
            raise ValueError(f"Azure OpenAI endpoint not configured for model: {model}")
        if not token:
            raise ValueError(f"Service token not loaded for model: {model}")

        headers = {
            "Content-Type": "application/json",
            "api-key": token,
        }

        cap = min(max(int(max_completion_tokens), 1), 32_000)
        payload = self._build_payload(
            model, messages, stream=False, temperature=None, max_tokens=cap
        )
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key

        connect_timeout, read_timeout = 30, _azure_tools_read_timeout(60)

        LIVECODE_LOGGER.debug(
            "Azure tools %s prepare model=%s messages=%d tools=%d tool_choice=%s cap=%d timeout=(%s,%s) prompt_cache=%s",
            request_id,
            model,
            len(messages or []),
            len(tools or []),
            payload.get("tool_choice"),
            cap,
            connect_timeout,
            read_timeout,
            bool(prompt_cache_key),
        )

        if on_thought_delta or on_content_delta or on_tool_call_delta:
            try:
                out = self._call_with_tools_streaming(
                    endpoint,
                    headers,
                    payload,
                    on_thought_delta,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    on_retry=on_retry,
                    request_id=request_id,
                    on_content_delta=on_content_delta,
                    on_tool_call_delta=on_tool_call_delta,
                )
                if _azure_tools_response_is_empty(out):
                    LIVECODE_LOGGER.warning(
                        "Azure tools %s streaming returned empty response; falling back to blocking",
                        request_id,
                    )
                else:
                    return out
            except Exception as e:
                logger.warning(
                    "Azure streaming tools failed (%s); falling back to blocking", e
                )

        return self._call_with_tools_blocking(
            endpoint,
            headers,
            payload,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            on_retry=on_retry,
            request_id=request_id,
        )
