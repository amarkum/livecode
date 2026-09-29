from __future__ import annotations

import json
import os
import time

_CACHE_DIR = os.path.abspath(os.path.join(os.path.expanduser("~"), ".livecode"))
_CACHE_FILE = os.path.join(_CACHE_DIR, "fx_rate_cache.json")
_CACHE_TTL_SECONDS = 24 * 60 * 60
_FALLBACK_USD_TO_INR = 95.0

_memory_cache: dict | None = None


def _read_cache_file() -> dict | None:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("rate"), (int, float)) and "fetched_at" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache_file(data: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _fetch_live_rate() -> float | None:
    try:
        import requests
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=6)
        if resp.status_code != 200:
            return None
        rate = (resp.json() or {}).get("rates", {}).get("INR")
        return float(rate) if rate else None
    except Exception:
        return None


def get_usd_to_inr_rate() -> dict:
    global _memory_cache
    now = time.time()
    if _memory_cache and (now - _memory_cache.get("fetched_at", 0)) < _CACHE_TTL_SECONDS:
        return _memory_cache

    disk = _read_cache_file()
    if disk and (now - disk.get("fetched_at", 0)) < _CACHE_TTL_SECONDS:
        _memory_cache = disk
        return disk

    live_rate = _fetch_live_rate()
    if live_rate:
        result = {"rate": live_rate, "fetched_at": now, "source": "live"}
        _memory_cache = result
        _write_cache_file(result)
        return result

    if disk:
        _memory_cache = disk
        return disk
    if _memory_cache:
        return _memory_cache

    fallback = {"rate": _FALLBACK_USD_TO_INR, "fetched_at": now, "source": "fallback"}
    _memory_cache = fallback
    return fallback
