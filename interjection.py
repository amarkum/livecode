from __future__ import annotations

import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_queues: dict[str, list[str]] = defaultdict(list)
_cancelled: dict[str, float] = {}

def _key(session_id: str) -> str:
    return (session_id or "").strip()

def enqueue_interjection(session_id: str, message: str) -> None:
    text = (message or "").strip()
    if not text or not session_id:
        return
    with _lock:
        _queues[_key(session_id)].append(text)

def drain_interjections(session_id: str) -> list[str]:
    with _lock:
        key = _key(session_id)
        items = list(_queues.get(key) or [])
        _queues[key] = []
        return items

def has_pending_interjection(session_id: str) -> bool:
    with _lock:
        return bool(_queues.get(_key(session_id)))

def request_cancel(session_id: str) -> None:
    key = _key(session_id)
    if not key:
        return
    with _lock:
        _cancelled[key] = time.monotonic()

def is_cancelled(session_id: str, since: float | None = None) -> bool:
    with _lock:
        stamp = _cancelled.get(_key(session_id))
    if stamp is None:
        return False
    return since is None or stamp >= since


_turn_locks: dict[str, threading.Lock] = {}


def session_turn_lock(session_id: str) -> threading.Lock:
    with _lock:
        return _turn_locks.setdefault(_key(session_id), threading.Lock())
