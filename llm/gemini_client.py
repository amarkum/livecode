from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable

import requests

from .retry import call_with_retries, is_cancel, raise_for_status

LIVECODE_LOGGER = logging.getLogger("LiveCode")
logger = logging.getLogger(__name__)

DEFAULT_GEMINI_HOST = "https://generativelanguage.googleapis.com/v1beta"

# Keep existing UI / harness model ids working by mapping them to Gemini models.
DEFAULT_GEMINI_MODELS: dict[str, str] = {
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
    "gemini-2.0-flash": "gemini-2.0-flash",
    "gpt-5-chat": "gemini-2.5-flash",
    "gpt-5-mini": "gemini-2.0-flash",
    "gpt-5.2": "gemini-2.5-flash",
    "gpt-5.4": "gemini-2.5-pro",
    "gpt-5.5": "gemini-2.5-pro",
}


def _usage_fields(usage: dict | None) -> dict:
    if not isinstance(usage, dict) or not usage:
        return {}
    out: dict = {}
    mapping = (
        ("promptTokenCount", "prompt_tokens"),
        ("candidatesTokenCount", "completion_tokens"),
        ("totalTokenCount", "total_tokens"),
        ("cachedContentTokenCount", "cached_tokens"),
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("cached_tokens", "cached_tokens"),
    )
    for src, dest in mapping:
        val = usage.get(src)
        if val is not None and dest not in out:
            try:
                out[dest] = int(val)
            except (TypeError, ValueError):
                pass
    return out


def _log_token_usage(usage_fields: dict, *, streaming: bool = False) -> None:
    if not usage_fields:
        return
    bits = []
    for key, label in (
        ("prompt_tokens", "in"),
        ("completion_tokens", "out"),
        ("total_tokens", "total"),
        ("cached_tokens", "cached"),
    ):
        val = usage_fields.get(key)
        if val is not None:
            bits.append(f"{label}={val}")
    if not bits:
        return
    kind = "streaming" if streaming else "non-streaming"
    LIVECODE_LOGGER.debug("Gemini tokens (%s): %s", kind, " ".join(bits))


def _tools_read_timeout(default=60):
    raw = (os.environ.get("GEMINI_TOOLS_READ_TIMEOUT") or os.environ.get("AZURE_TOOLS_READ_TIMEOUT") or "").strip()
    if not raw:
        return int(default)
    try:
        return max(15, min(300, int(raw)))
    except (TypeError, ValueError):
        return int(default)


def _response_is_empty(out: dict | None) -> bool:
    if not isinstance(out, dict):
        return True
    content = (out.get("content") or "").strip()
    reasoning = (out.get("reasoning_content") or "").strip()
    tool_calls = out.get("tool_calls")
    has_tools = bool(tool_calls) if tool_calls is not None else False
    return (not content) and (not reasoning) and (not has_tools)


def _raise_for_status(response) -> None:
    # A 429 is left to _with_rate_limit_retry, which knows Gemini's retryDelay and the fallback model.
    if response.status_code == 429:
        raise Exception(f"Gemini API error: {response.status_code} - {response.text}")
    raise_for_status(response, "Gemini")


def _retry_logger(on_retry, kind: str):
    def _log(attempt, total, error):
        logger.warning("Gemini %s tools request failed (%s), attempt %s/%s; retrying", kind, error, attempt + 1, total)
        if on_retry:
            on_retry(attempt + 1, total, error)

    return _log


def _normalize_timeout(timeout) -> Any:
    if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
        return (float(timeout[0]), float(timeout[1]))
    return timeout


def _text_from_openai_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    url = ((item.get("image_url") or {}) if isinstance(item.get("image_url"), dict) else {})
                    parts.append(f"[image: {(url.get('url') if isinstance(url, dict) else item.get('image_url')) or ''}]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _rate_limit_delay(error: Exception) -> float | None:
    """Seconds Gemini asks us to wait for a 429, or None when it is not a rate limit."""
    text = str(error)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text:
        return None
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text) or re.search(r"retry in (\d+(?:\.\d+)?)s", text)
    return float(match.group(1)) if match else 10.0


def _fallback_model() -> str:
    from . import settings as llm_settings

    return str(llm_settings.load().get("gemini_fallback_model") or "").strip()


def _max_rate_limit_wait() -> float:
    try:
        from . import settings as llm_settings

        return max(0.0, float(llm_settings.load().get("rate_limit_max_wait_s") or 30))
    except (TypeError, ValueError):
        return 30.0


def _relax_forced_tool_mode(payload: dict, error: Exception, request_id: str = "") -> bool:
    """Switch a forced tool call (mode ANY) to VALIDATED after Gemini rejects the schema.

    ANY constrains decoding to the union of every tool schema; large tools (e.g. the
    browser tool's many optional properties) exceed Gemini's branching limit.
    VALIDATED keeps schema-checked calls without that constraint.
    """
    config = (payload.get("toolConfig") or {}).get("functionCallingConfig") or {}
    if config.get("mode") != "ANY" or "too much branching" not in str(error):
        return False
    config["mode"] = "VALIDATED"
    LIVECODE_LOGGER.warning("Gemini tools %s: schema too complex for mode ANY; retrying with VALIDATED", request_id)
    return True


def _image_part(item: dict) -> dict | None:
    """OpenAI/Anthropic image block -> Gemini inlineData (or fileData for remote URLs)."""
    kind = item.get("type")
    if kind == "image":
        source = item.get("source") or {}
        if source.get("type") == "base64" and source.get("data"):
            return {"inlineData": {"mimeType": source.get("media_type") or "image/png", "data": source["data"]}}
        return None
    value = item.get("image_url")
    url = value.get("url") if isinstance(value, dict) else value
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        mime = header[5:].split(";", 1)[0] or "image/png"
        return {"inlineData": {"mimeType": mime, "data": data}}
    return {"fileData": {"mimeType": "image/jpeg", "fileUri": url}}


def _parts_from_openai_content(content: Any) -> list[dict]:
    if not isinstance(content, list):
        text = _text_from_openai_content(content)
        return [{"text": text}] if text else []
    parts: list[dict] = []
    for item in content:
        if isinstance(item, str):
            if item:
                parts.append({"text": item})
        elif isinstance(item, dict):
            if item.get("type") in ("image_url", "input_image", "image"):
                part = _image_part(item)
                if part:
                    parts.append(part)
            elif item.get("text"):
                parts.append({"text": str(item["text"])})
    return parts


def _openai_tools_to_gemini(tools: list[dict] | None) -> list[dict]:
    declarations = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict):
            continue
        name = (fn.get("name") or "").strip()
        if not name:
            continue
        decl: dict[str, Any] = {"name": name}
        if fn.get("description"):
            decl["description"] = fn["description"]
        params = fn.get("parameters")
        if isinstance(params, dict) and params:
            decl["parameters"] = params
        declarations.append(decl)
    if not declarations:
        return []
    return [{"functionDeclarations": declarations}]


# Placeholder Google accepts when a function call's real thought signature is unknown.
_SKIP_SIGNATURE = "skip_thought_signature_validator"


def _tool_call_from_part(part: dict, fc: dict) -> dict:
    args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
    tc = {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": fc["name"],
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }
    if part.get("thoughtSignature"):
        tc["thought_signature"] = part["thoughtSignature"]
    return tc


def _openai_messages_to_gemini(messages: list[dict]) -> tuple[dict | None, list[dict]]:
    system_parts: list[str] = []
    contents: list[dict] = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = (msg.get("role") or "").strip().lower()
        if role == "system":
            text = _text_from_openai_content(msg.get("content"))
            if text.strip():
                system_parts.append(text)
            continue

        if role == "tool":
            name = (msg.get("name") or "").strip() or "tool"
            raw = msg.get("content")
            if isinstance(raw, (dict, list)):
                response_payload: Any = raw
            else:
                text = _text_from_openai_content(raw)
                try:
                    response_payload = json.loads(text) if text.strip().startswith(("{", "[")) else {"result": text}
                except json.JSONDecodeError:
                    response_payload = {"result": text}
            part = {"functionResponse": {"name": name, "response": response_payload if isinstance(response_payload, dict) else {"result": response_payload}}}
            if contents and contents[-1].get("role") == "user" and any(
                isinstance(p, dict) and "functionResponse" in p for p in (contents[-1].get("parts") or [])
            ):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
            continue

        if role == "assistant":
            parts: list[dict] = []
            text = _text_from_openai_content(msg.get("content"))
            if text:
                parts.append({"text": text})
            first_call = True
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = (fn.get("name") or "").strip()
                if not name:
                    continue
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
                if not isinstance(args, dict):
                    args = {"value": args}
                call_part: dict[str, Any] = {"functionCall": {"name": name, "args": args}}
                signature = tc.get("thought_signature")
                if not signature and first_call:
                    # Thinking models reject a turn whose first function call has no
                    # signature (e.g. calls made by another provider or saved before
                    # signatures were kept). Google documents this value for that case.
                    signature = _SKIP_SIGNATURE
                if signature:
                    call_part["thoughtSignature"] = signature
                parts.append(call_part)
                first_call = False
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        # user / other → user (images become inlineData parts so vision models see them)
        parts = _parts_from_openai_content(msg.get("content")) or [{"text": ""}]
        contents.append({"role": "user", "parts": parts})

    system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    return system_instruction, contents


def _finish_reason_from_gemini(reason: str | None) -> str | None:
    if not reason:
        return None
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "OTHER": "stop",
    }
    return mapping.get(reason, reason.lower())


def _parse_candidate(candidate: dict | None) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    finish_reason = _finish_reason_from_gemini((candidate or {}).get("finishReason"))

    parts = ((candidate or {}).get("content") or {}).get("parts") or []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought") and part.get("text"):
            reasoning_parts.append(str(part["text"]))
            continue
        if "text" in part and part.get("text") is not None:
            content_parts.append(str(part["text"]))
        fc = part.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            tool_calls.append(_tool_call_from_part(part, fc))

    return {
        "content": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts),
        "tool_calls": tool_calls or None,
        "finish_reason": finish_reason,
    }


def _parse_sse_stream(
    response,
    *,
    on_thought_delta=None,
    on_content_delta=None,
    on_tool_call_delta=None,
    request_id: str = "",
) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    usage_fields: dict = {}
    finish_reason = None
    stream_started = time.monotonic()
    chunk_count = 0
    prefix = f"Gemini tools stream {request_id}".strip()

    for line in response.iter_lines():
        if not line:
            continue
        line_text = line.decode("utf-8")
        if not line_text.startswith("data: "):
            continue
        data_content = line_text[6:].strip()
        if not data_content or data_content == "[DONE]":
            continue
        chunk_count += 1
        try:
            chunk = json.loads(data_content)
        except json.JSONDecodeError:
            continue

        usage_fields.update(_usage_fields(chunk.get("usageMetadata") or {}))
        candidates = chunk.get("candidates") or []
        if not candidates:
            continue
        candidate = candidates[0] or {}
        if candidate.get("finishReason"):
            finish_reason = _finish_reason_from_gemini(candidate.get("finishReason"))

        for part in ((candidate.get("content") or {}).get("parts") or []):
            if not isinstance(part, dict):
                continue
            if part.get("thought") and part.get("text"):
                text = str(part["text"])
                reasoning_parts.append(text)
                if on_thought_delta:
                    on_thought_delta(text)
                continue
            if part.get("text"):
                text = str(part["text"])
                content_parts.append(text)
                if on_content_delta:
                    on_content_delta(text)
                elif on_thought_delta:
                    on_thought_delta(text)
            fc = part.get("functionCall")
            if isinstance(fc, dict) and fc.get("name"):
                tc = _tool_call_from_part(part, fc)
                tool_calls.append(tc)
                if on_tool_call_delta:
                    on_tool_call_delta(
                        {
                            "index": len(tool_calls) - 1,
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": fc["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                    )

    out = {
        "content": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts),
        "tool_calls": tool_calls or None,
        "finish_reason": finish_reason,
    }
    out.update(usage_fields)
    LIVECODE_LOGGER.debug(
        "%s parsed chunks=%d content_len=%d reasoning_len=%d tool_calls=%d usage=%s elapsed=%.2fs",
        prefix,
        chunk_count,
        len(out["content"]),
        len(out["reasoning_content"]),
        len(tool_calls),
        bool(usage_fields),
        time.monotonic() - stream_started,
    )
    return out


class GeminiClient:
    def __init__(
        self,
        *,
        models: dict[str, str] | None = None,
        deployments: dict[str, str] | None = None,
        host: str | None = None,
        api_version: str | None = None,
        get_token: Callable[[], str | None] | None = None,
        fast_model: str = "gemini-2.5-flash",
        large_context_model: str = "gemini-2.5-pro",
        large_content_char_threshold: int = 600_000,
        large_file_count_threshold: int = 40,
        max_input_tokens_for_model: Callable[[str], int] | None = None,
    ):
        # `deployments` kept as a compat alias for the old AzureOpenAIClient constructor.
        raw_models = models or deployments or DEFAULT_GEMINI_MODELS
        self._models = {str(k): str(v) for k, v in raw_models.items()}
        for alias, gemini_id in DEFAULT_GEMINI_MODELS.items():
            self._models.setdefault(alias, gemini_id)
        self._host = (host or DEFAULT_GEMINI_HOST).rstrip("/")
        self._api_version = api_version  # unused; Gemini path embeds version in host
        self._get_token = get_token or (lambda: None)
        self._fast_model = fast_model
        self._large_context_model = large_context_model
        self._large_content_char_threshold = large_content_char_threshold
        self._large_file_count_threshold = large_file_count_threshold
        self._max_input_tokens_for_model = max_input_tokens_for_model

    def supports_model(self, model: str) -> bool:
        key = (model or "").strip()
        if not key:
            return False
        if key in self._models:
            return True
        # Allow bare Gemini model ids even if not listed explicitly.
        return key.startswith("gemini-")

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

    def _resolve_model_id(self, model: str) -> str:
        key = (model or "").strip()
        if key in self._models:
            return self._models[key]
        if key.startswith("gemini-"):
            return key
        raise ValueError(f"Unknown Gemini model: {model}")

    def _api_key(self) -> str | None:
        token = None
        if self._get_token:
            try:
                token = self._get_token()
            except Exception:
                token = None
        if token:
            return str(token).strip() or None
        return None

    def _endpoint(self, model: str, *, stream: bool = False) -> str:
        model_id = self._resolve_model_id(model)
        action = "streamGenerateContent" if stream else "generateContent"
        url = f"{self._host}/models/{model_id}:{action}"
        if stream:
            url += "?alt=sse"
        return url

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }

    def _build_payload(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        system_instruction, contents = _openai_messages_to_gemini(messages)
        payload: dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        gen: dict[str, Any] = {}
        if temperature is not None:
            gen["temperature"] = temperature
        if max_tokens is not None:
            gen["maxOutputTokens"] = int(max_tokens)
        if gen:
            payload["generationConfig"] = gen
        gemini_tools = _openai_tools_to_gemini(tools)
        if gemini_tools:
            payload["tools"] = gemini_tools
            mode = "AUTO"
            choice = (tool_choice or "auto").strip().lower()
            if choice == "none":
                mode = "NONE"
            elif choice == "required":
                mode = "ANY"
            payload["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
        return payload

    def _with_rate_limit_retry(self, call: Callable[[str], Any], model: str) -> Any:
        """Run call(model); on a 429 switch to GEMINI_FALLBACK_MODEL, else wait retryDelay once."""
        try:
            return call(model)
        except Exception as exc:
            delay = _rate_limit_delay(exc)
            if delay is None:
                raise
            fallback = _fallback_model()
            if fallback and fallback != model and self.supports_model(fallback):
                LIVECODE_LOGGER.warning("Gemini rate limit on %s; retrying on fallback %s", model, fallback)
                try:
                    return call(fallback)
                except Exception as fallback_exc:
                    if _rate_limit_delay(fallback_exc) is None:
                        raise
                    exc = fallback_exc
                    delay = _rate_limit_delay(fallback_exc) or delay
            if delay > _max_rate_limit_wait():
                raise exc
            LIVECODE_LOGGER.warning("Gemini rate limit on %s; waiting %.1fs before one retry", model, delay)
            time.sleep(delay + 0.5)
            return call(model)

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> str:
        return self._with_rate_limit_retry(lambda m: self._complete_once(m, messages, **kwargs), model)

    def complete_with_tools(self, model: str, messages: list[dict], tools: list[dict], **kwargs: Any) -> dict[str, Any]:
        return self._with_rate_limit_retry(lambda m: self._complete_with_tools_once(m, messages, tools, **kwargs), model)

    def _complete_once(
        self,
        model: str,
        messages: list[dict],
        *,
        prompt_cache_key: str | None = None,
        timeout: int = 30,
    ) -> str:
        api_key = self._api_key()
        if not api_key:
            raise ValueError("Gemini API key not loaded (add it in LiveCode Settings > Models)")

        endpoint = self._endpoint(model, stream=False)
        headers = self._headers(api_key)
        payload = self._build_payload(messages, temperature=0.3, max_tokens=4096)
        # prompt_cache_key is an Azure concept; ignored for Gemini.

        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=_normalize_timeout(timeout),
        )
        _raise_for_status(response)

        result = response.json()
        usage_fields = _usage_fields(result.get("usageMetadata") or {})
        _log_token_usage(usage_fields, streaming=False)
        candidates = result.get("candidates") or []
        if not candidates:
            raise Exception("Gemini returned invalid response format")
        parsed = _parse_candidate(candidates[0])
        content = parsed.get("content")
        if content is not None:
            return content
        LIVECODE_LOGGER.warning(
            "Gemini response missing text content. Full response: %s",
            json.dumps(result, default=str)[:500],
        )
        raise Exception("Gemini response missing text content")

    def stream(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 16_000,
        timeout: int = 60,
    ):
        api_key = self._api_key()
        if not api_key:
            raise ValueError("Gemini API key not loaded (add it in LiveCode Settings > Models)")

        endpoint = self._endpoint(model, stream=True)
        headers = self._headers(api_key)
        payload = self._build_payload(messages, temperature=0.7, max_tokens=max_tokens)

        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            stream=True,
            timeout=_normalize_timeout(timeout),
        )
        if response.status_code != 200:
            raise Exception(f"Gemini API error: {response.status_code} - {response.text}")

        for line in response.iter_lines():
            if not line:
                continue
            line_text = line.decode("utf-8")
            if not line_text.startswith("data: "):
                continue
            data_content = line_text[6:].strip()
            if not data_content or data_content == "[DONE]":
                continue
            try:
                chunk = json.loads(data_content)
            except json.JSONDecodeError:
                continue
            for candidate in chunk.get("candidates") or []:
                for part in ((candidate.get("content") or {}).get("parts") or []):
                    if isinstance(part, dict) and part.get("text") and not part.get("thought"):
                        yield str(part["text"])

    def _call_blocking(
        self,
        endpoint,
        headers,
        payload,
        connect_timeout=30,
        read_timeout=60,
        max_retries=None,
        on_retry=None,
        request_id: str = "",
    ):
        started = time.monotonic()
        prefix = f"Gemini tools {request_id}".strip()
        LIVECODE_LOGGER.debug(
            "%s blocking_start messages=%d tools=%d timeout=(%s,%s)",
            prefix,
            len((payload.get("contents") or [])),
            len((((payload.get("tools") or [{}])[0]).get("functionDeclarations") or [])),
            connect_timeout,
            read_timeout,
        )

        def _attempt():
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
            _raise_for_status(response)

            result = response.json()
            candidates = result.get("candidates") or []
            if not candidates:
                raise Exception("Gemini returned invalid response format")

            out = _parse_candidate(candidates[0])
            usage_fields = _usage_fields(result.get("usageMetadata") or {})
            out.update(usage_fields)
            _log_token_usage(usage_fields, streaming=False)
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

        return call_with_retries(_attempt, on_retry=_retry_logger(on_retry, "blocking"), retries=max_retries)

    def _call_streaming(
        self,
        endpoint,
        headers,
        payload,
        on_thought_delta,
        connect_timeout=30,
        read_timeout=60,
        max_retries=None,
        on_retry=None,
        request_id: str = "",
        on_content_delta=None,
        on_tool_call_delta=None,
    ):
        started = time.monotonic()
        prefix = f"Gemini tools {request_id}".strip()
        LIVECODE_LOGGER.debug(
            "%s streaming_start messages=%d tools=%d timeout=(%s,%s)",
            prefix,
            len(payload.get("contents") or []),
            len((((payload.get("tools") or [{}])[0]).get("functionDeclarations") or [])),
            connect_timeout,
            read_timeout,
        )

        def _attempt():
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                stream=True,
                timeout=(connect_timeout, read_timeout),
            )
            LIVECODE_LOGGER.debug(
                "%s streaming_http status=%s elapsed=%.2fs",
                prefix,
                response.status_code,
                time.monotonic() - started,
            )
            _raise_for_status(response)
            out = _parse_sse_stream(
                response,
                on_thought_delta=on_thought_delta,
                on_content_delta=on_content_delta,
                on_tool_call_delta=on_tool_call_delta,
                request_id=request_id,
            )
            usage_fields = {
                k: out[k]
                for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")
                if k in out
            }
            _log_token_usage(usage_fields, streaming=True)
            return out

        return call_with_retries(_attempt, on_retry=_retry_logger(on_retry, "streaming"), retries=max_retries)

    def _complete_with_tools_once(
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
        request_id = f"gmt_{uuid.uuid4().hex[:10]}"
        api_key = self._api_key()
        if not api_key:
            raise ValueError("Gemini API key not loaded (add it in LiveCode Settings > Models)")

        cap = min(max(int(max_completion_tokens), 1), 65_536)
        payload = self._build_payload(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=None,
            max_tokens=cap,
        )
        headers = self._headers(api_key)
        connect_timeout, read_timeout = 30, _tools_read_timeout(60)

        LIVECODE_LOGGER.debug(
            "Gemini tools %s prepare model=%s messages=%d tools=%d tool_choice=%s cap=%d timeout=(%s,%s)",
            request_id,
            model,
            len(messages or []),
            len(tools or []),
            tool_choice or "auto",
            cap,
            connect_timeout,
            read_timeout,
        )

        if on_thought_delta or on_content_delta or on_tool_call_delta:
            stream_endpoint = self._endpoint(model, stream=True)
            try:
                out = self._call_streaming(
                    stream_endpoint,
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
                if _response_is_empty(out):
                    LIVECODE_LOGGER.warning(
                        "Gemini tools %s streaming returned empty response; falling back to blocking",
                        request_id,
                    )
                else:
                    return out
            except Exception as e:
                if is_cancel(e) or _rate_limit_delay(e) is not None:
                    raise
                logger.warning("Gemini streaming tools failed (%s); falling back to blocking", e)
                _relax_forced_tool_mode(payload, e, request_id)

        try:
            return self._call_blocking(
                self._endpoint(model, stream=False),
                headers,
                payload,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                on_retry=on_retry,
                request_id=request_id,
            )
        except Exception as e:
            if not _relax_forced_tool_mode(payload, e, request_id):
                raise
            return self._call_blocking(
                self._endpoint(model, stream=False),
                headers,
                payload,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                on_retry=on_retry,
                request_id=request_id,
            )

