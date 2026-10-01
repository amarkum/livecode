"""Retrying model calls on transient failures.

A rate limit, an overloaded or failing server, a timeout or a dropped stream is retried with
jittered exponential backoff (or the server's Retry-After). Everything else, and a cancelled
turn, is raised at once. The retry count comes from Settings > Harness > Model retries.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, TypeVar

import requests

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
MAX_BACKOFF_S = 30.0
DEFAULT_RETRIES = 4
_RETRYABLE_STREAM_ERRORS = ("overloaded", "rate_limit", "internal_server_error", "api_error", "timeout", "unavailable")

T = TypeVar("T")


class RetryableHTTPError(Exception):
    """A provider answered with a status worth retrying."""

    def __init__(self, message: str, status: int, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RetryableStreamError(Exception):
    """A provider reported a transient error partway through a streamed reply."""


def is_cancel(exc: BaseException) -> bool:
    # Matched by name so llm/ does not import the harness.
    return type(exc).__name__ == "TurnCancelled"


def _retry_after(resp: requests.Response) -> float | None:
    for header in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset"):
        raw = (resp.headers.get(header) or "").strip().lower()
        if not raw:
            continue
        try:
            if raw.endswith("ms"):
                return float(raw[:-2]) / 1000.0
            return float(raw.rstrip("s"))
        except ValueError:
            continue
    return None


def raise_for_status(resp: requests.Response, provider: str) -> None:
    if resp.status_code == 200:
        return
    message = f"{provider} API error: {resp.status_code} - {resp.text[:2000]}"
    if resp.status_code in RETRYABLE_STATUS:
        raise RetryableHTTPError(message, resp.status_code, _retry_after(resp))
    raise Exception(message)


def stream_error(provider: str, error: Any) -> Exception:
    """The exception for an error event inside a stream: retryable when it is transient."""
    text = str(error)
    kind = str(error.get("type") or "") if isinstance(error, dict) else ""
    if any(word in (kind or text).lower() for word in _RETRYABLE_STREAM_ERRORS):
        return RetryableStreamError(f"{provider} stream error: {text[:500]}")
    return Exception(f"{provider} stream error: {text[:2000]}")


def is_retryable(exc: BaseException) -> bool:
    if is_cancel(exc):
        return False
    return isinstance(exc, (
        RetryableHTTPError,
        RetryableStreamError,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    ))


def configured_retries() -> int:
    try:
        from livecode import agent_settings

        return int(agent_settings.get("model_retries"))
    except Exception:
        return DEFAULT_RETRIES


def backoff_seconds(attempt: int, exc: BaseException | None = None) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None and retry_after >= 0:
        return min(MAX_BACKOFF_S * 2, float(retry_after) + random.uniform(0, 0.5))
    return min(MAX_BACKOFF_S, 2.0 ** attempt) * random.uniform(0.5, 1.5)


def call_with_retries(
    fn: Callable[[], T],
    *,
    on_retry: Callable[[int, int, Exception], None] | None = None,
    retries: int | None = None,
) -> T:
    """Runs fn, retrying transient failures. on_retry(attempt, total, error) runs before each wait;
    a TurnCancelled it raises stops the retries."""
    retries = configured_retries() if retries is None else max(0, int(retries))
    total = retries + 1
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if not is_retryable(exc) or attempt >= retries:
                raise
            if on_retry:
                try:
                    on_retry(attempt, total, exc)
                except Exception as cb_exc:
                    if is_cancel(cb_exc):
                        raise
            time.sleep(backoff_seconds(attempt, exc))
            attempt += 1
