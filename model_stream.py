
from __future__ import annotations

import inspect
import json
import re
import threading
import time
import weakref
from typing import Any, Callable

_KNOWN_HOST_KWARGS = frozenset({
    "tool_choice",
    "prompt_cache_key",
    "on_thought_delta",
    "on_retry",
    "max_completion_tokens",
})
_UNEXPECTED_KWARG_RE = re.compile(r"unexpected keyword argument '([^']+)'")

_signature_cache: weakref.WeakKeyDictionary[Callable, frozenset[str] | None] = weakref.WeakKeyDictionary()


class TurnCancelled(Exception):
    pass


def accepted_kwargs(fn: Callable) -> frozenset[str] | None:
    if fn in _signature_cache:
        return _signature_cache[fn]
    names: frozenset[str] | None = None
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        params = None
    if params is not None and not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        names = frozenset(
            p.name for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
    _signature_cache[fn] = names
    return names


def host_streams_separately(fn: Callable) -> bool:
    names = accepted_kwargs(fn)
    return bool(names) and "on_content_delta" in names


def call_host_model(fn: Callable, model: str, messages: list, tools: list, **kwargs: Any) -> dict:
    names = accepted_kwargs(fn)
    allowed = _KNOWN_HOST_KWARGS if names is None else names
    call_kwargs = {k: v for k, v in kwargs.items() if v is not None and k in allowed}
    if "on_content_delta" in call_kwargs:
        call_kwargs.pop("on_thought_delta", None)
    while True:
        try:
            return fn(model, messages, tools, **call_kwargs)
        except TypeError as exc:
            match = _UNEXPECTED_KWARG_RE.search(str(exc))
            if not match or match.group(1) not in call_kwargs:
                raise
            call_kwargs.pop(match.group(1))


_SNIFFED_ARG_FIELDS = ("file_path", "command", "description", "pattern", "query", "goal", "url", "directory", "name")
_ARG_FIELD_RES = {
    field: re.compile(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % field)
    for field in _SNIFFED_ARG_FIELDS
}


def sniff_partial_arguments(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    text = raw or ""
    for field, pattern in _ARG_FIELD_RES.items():
        match = pattern.search(text)
        if not match:
            continue
        try:
            out[field] = json.loads('"' + match.group(1) + '"')
        except ValueError:
            out[field] = match.group(1)
    return out


class StepStream:

    FLUSH_INTERVAL_S = 0.06
    FLUSH_CHARS = 480
    TOOL_PROGRESS_INTERVAL_S = 0.5

    def __init__(
        self,
        emit: Callable[..., None],
        *,
        started_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._cancel_check = cancel_check
        self._lock = threading.RLock()
        self.started_at = started_at if started_at is not None else clock()
        self.last_activity = self.started_at
        self.thinking_ended_at: float | None = None
        self.content_started = False
        self.reasoning_text = ""
        self.content_text = ""
        self._pending_reasoning = ""
        self._pending_content = ""
        self._last_flush = 0.0
        self._tool_calls: dict[int, dict[str, Any]] = {}

    @property
    def thinking_ended(self) -> bool:
        return self.thinking_ended_at is not None

    def thinking_duration_ms(self) -> int:
        end = self.thinking_ended_at if self.thinking_ended_at is not None else self._clock()
        return max(0, int((end - self.started_at) * 1000))

    def pending_tool_calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._tool_calls.items())]


    def _abort_if_cancelled(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise TurnCancelled()

    def on_reasoning(self, delta: Any, *_args: Any, **_kwargs: Any) -> None:
        self._abort_if_cancelled()
        text = _delta_text(delta)
        if not text:
            return
        with self._lock:
            self.last_activity = self._clock()
            self.reasoning_text += text
            if self.thinking_ended:
                return
            self._pending_reasoning += text
            self._maybe_flush()

    def on_content(self, delta: Any, *_args: Any, **_kwargs: Any) -> None:
        self._abort_if_cancelled()
        text = _delta_text(delta)
        if not text:
            return
        with self._lock:
            self.last_activity = self._clock()
            if not self.content_started and not text.strip():
                return
            self._end_thinking()
            self.content_started = True
            self.content_text += text
            self._pending_content += text
            self._maybe_flush()

    def on_legacy(self, delta: Any, *args: Any, **kwargs: Any) -> None:
        kind = str(kwargs.get("kind") or kwargs.get("channel") or "").lower()
        if kind in ("reasoning", "thought", "thinking"):
            self.on_reasoning(delta)
        else:
            self.on_content(delta)

    def on_tool_call(self, *args: Any, **kwargs: Any) -> None:
        self._abort_if_cancelled()
        index, call_id, name, arguments = _tool_delta_parts(args, kwargs)
        if index is None:
            return
        with self._lock:
            now = self._clock()
            self.last_activity = now
            entry = self._tool_calls.setdefault(index, {
                "index": index, "id": "", "name": "", "arguments": "",
                "announced": {}, "last_progress": 0.0,
            })
            if call_id:
                entry["id"] = call_id
            if name and not entry["name"]:
                entry["name"] = name
            if arguments:
                entry["arguments"] += arguments
            if not entry["name"]:
                return
            self._end_thinking()
            self._flush()
            sniffed = sniff_partial_arguments(entry["arguments"])
            fresh = sniffed != entry["announced"]
            due = now - entry["last_progress"] >= self.TOOL_PROGRESS_INTERVAL_S
            if not fresh and not due:
                return
            entry["announced"] = sniffed
            entry["last_progress"] = now
            self._emit(
                "tool_call_pending",
                "",
                index=index,
                tool=entry["name"],
                args=sniffed,
                arg_chars=len(entry["arguments"]),
            )


    def tick(self) -> None:
        with self._lock:
            if self._pending_reasoning or self._pending_content:
                if self._clock() - self._last_flush >= self.FLUSH_INTERVAL_S:
                    self._flush()

    def finish(self) -> None:
        with self._lock:
            self._flush()

    def close_content(self, *, role: str, text: str, thought_content: str = "") -> bool:
        with self._lock:
            self._flush()
            final = (text or "").strip()
            if not self.content_started and not final:
                return False
            self._emit(
                "content_done",
                "",
                role=role,
                text=final,
                thought_content=(thought_content or "").strip(),
                streamed=self.content_started,
            )
            return True

    def drop_content(self) -> None:
        with self._lock:
            self._flush()
            had_content = self.content_started
            self.content_started = False
            self.content_text = ""
            if had_content:
                self._emit("content_retract", "")

    def retract(self) -> None:
        with self._lock:
            had_output = self.content_started or bool(self._tool_calls)
            self._pending_reasoning = ""
            self._pending_content = ""
            self.content_text = ""
            self.reasoning_text = ""
            self.content_started = False
            self._tool_calls.clear()
            self.thinking_ended_at = None
            self.started_at = self._clock()
            self.last_activity = self.started_at
            if had_output:
                self._emit("content_retract", "")


    def _end_thinking(self) -> None:
        if self.thinking_ended:
            return
        self._flush()
        self.thinking_ended_at = self._clock()
        duration_ms = self.thinking_duration_ms()
        self._emit(
            "agent_thinking_done",
            f"Thought for {max(1, duration_ms // 1000)}s",
            live=True,
            duration_s=max(1, duration_ms // 1000),
            duration_ms=duration_ms,
            thought_content=self.reasoning_text.strip(),
        )

    def _maybe_flush(self) -> None:
        size = len(self._pending_reasoning) + len(self._pending_content)
        if size >= self.FLUSH_CHARS or self._clock() - self._last_flush >= self.FLUSH_INTERVAL_S:
            self._flush()

    def _flush(self) -> None:
        if self._pending_reasoning:
            chunk, self._pending_reasoning = self._pending_reasoning, ""
            self._emit("agent_thinking_delta", "", delta=chunk)
        if self._pending_content:
            chunk, self._pending_content = self._pending_content, ""
            self._emit("content_delta", "", delta=chunk)
        self._last_flush = self._clock()


def _delta_text(delta: Any) -> str:
    if delta is None:
        return ""
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        for key in ("text", "content", "delta", "reasoning_content"):
            value = delta.get(key)
            if isinstance(value, str):
                return value
        return ""
    return str(delta)


def _tool_delta_parts(args: tuple, kwargs: dict) -> tuple[int | None, str, str, str]:
    if args and isinstance(args[0], dict):
        payload = args[0]
        fn = payload.get("function") if isinstance(payload.get("function"), dict) else {}
        index = payload.get("index")
        call_id = payload.get("id") or ""
        name = fn.get("name") or payload.get("name") or ""
        arguments = fn.get("arguments") or payload.get("arguments") or ""
    else:
        padded = list(args) + [None] * (4 - len(args))
        index = kwargs.get("index", padded[0])
        call_id = kwargs.get("call_id", kwargs.get("id", padded[1])) or ""
        name = kwargs.get("name", padded[2]) or ""
        arguments = kwargs.get("arguments", kwargs.get("arguments_delta", padded[3])) or ""
    try:
        index = int(index) if index is not None else 0
    except (TypeError, ValueError):
        return None, "", "", ""
    return index, str(call_id), str(name), str(arguments)
