"""Which model-call failures are retried, and how long the waits are."""
import requests

from livecode.llm import retry


class _Resp:
    def __init__(self, status, headers=None, text="boom"):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


def _raises(status, headers=None):
    try:
        retry.raise_for_status(_Resp(status, headers), "prov")
    except Exception as exc:
        return exc
    return None


def test_transient_statuses_are_retryable_and_a_conflict_is_not():
    for status in (429, 500, 502, 503, 504, 529, 408):
        assert isinstance(_raises(status), retry.RetryableHTTPError), status
    for status in (400, 401, 403, 404, 409, 422):
        exc = _raises(status)
        assert exc is not None and not retry.is_retryable(exc), status
    assert _raises(200) is None


def test_retry_after_is_honoured():
    exc = _raises(429, {"retry-after": "7"})
    assert exc.retry_after == 7.0
    wait = retry.backoff_seconds(0, exc)
    assert 7.0 <= wait <= 7.5
    assert retry.backoff_seconds(3) <= retry.MAX_BACKOFF_S * 1.5


def test_stream_errors_are_classified_by_type_then_by_clear_wording():
    assert isinstance(retry.stream_error("p", {"type": "overloaded_error", "message": "x"}), retry.RetryableStreamError)
    assert not isinstance(retry.stream_error("p", {"type": "invalid_request_error", "message": "timeout field is wrong"}), retry.RetryableStreamError), \
        "a typed error is judged by its type, not by words in its message"
    assert isinstance(retry.stream_error("p", "The server is overloaded, please try again"), retry.RetryableStreamError)
    assert not isinstance(retry.stream_error("p", "Invalid API key"), retry.RetryableStreamError)


def test_network_failures_retry_and_cancellation_never_does():
    assert retry.is_retryable(requests.exceptions.ConnectionError())
    assert retry.is_retryable(requests.exceptions.Timeout())

    class TurnCancelled(Exception):
        pass
    assert not retry.is_retryable(TurnCancelled())


def test_call_with_retries_stops_at_the_limit(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda s: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise retry.RetryableHTTPError("busy", 503)
        return "ok"
    assert retry.call_with_retries(flaky, retries=4) == "ok" and len(attempts) == 3
    attempts.clear()
    try:
        retry.call_with_retries(lambda: (_ for _ in ()).throw(retry.RetryableHTTPError("busy", 503)), retries=2)
    except retry.RetryableHTTPError:
        pass
    else:
        raise AssertionError("should raise after the retries are spent")
