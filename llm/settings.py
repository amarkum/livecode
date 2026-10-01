"""LLM provider keys and models, saved from LiveCode Settings.

Stored in ~/.livecode/llm.json with 0600 permissions. One provider is
active; its strongest models are discovered from the provider when the key is saved.
Providers and model lists come from models.yaml next to this file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

import requests
import yaml

CONFIG_PATH = os.path.expanduser("~/.livecode/llm.json")
CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.yaml")
_LOCK = threading.Lock()
_REFRESH_AFTER_S = 24 * 3600
logger = logging.getLogger(__name__)

# Filled from models.yaml; updated in place so importers keep the same objects.
PROVIDERS: dict[str, dict[str, Any]] = {}
# Fixed model lists (providers without discovery), best first: {id, label, fast, vision, code}.
PREFERRED_MODELS: dict[str, list[dict[str, Any]]] = {}
# Models added to a discovered list when the key can use them, e.g. text-only coding models.
EXTRA_MODELS: dict[str, list[dict[str, Any]]] = {}
FALLBACK_MODELS: dict[str, list[str]] = {}
_BEST_ORDER: dict[str, list[str]] = {"large": [], "fast": []}
MAX_MODELS = 4
_catalog_mtime: float | None = None
_DISCOVER_RULES = {"gemini", "openai", "anthropic", "xai", "openrouter"}


def _catalog_model(raw: dict[str, Any]) -> dict[str, Any]:
    """A models.yaml model entry. Models read images unless marked vision: false."""
    mid = str(raw["id"])
    return {"id": mid, "label": str(raw.get("label") or mid), "fast": bool(raw.get("fast")),
            "vision": raw.get("vision") is not False, "code": bool(raw.get("code"))}


def _load_catalog() -> None:
    """Reads models.yaml when it changed. A broken edit keeps the last good catalog."""
    global MAX_MODELS, _catalog_mtime
    try:
        mtime = os.path.getmtime(CATALOG_PATH)
    except OSError:
        if _catalog_mtime is None:
            raise
        return
    if mtime == _catalog_mtime:
        return
    try:
        with open(CATALOG_PATH, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle) or {}
        providers, preferred, fallback, extra = {}, {}, {}, {}
        for pid, raw in (doc.get("providers") or {}).items():
            pid = str(pid)
            if not isinstance(raw, dict) or not raw.get("base_url") or raw.get("kind") not in ("gemini", "anthropic", "openai"):
                raise ValueError(f"provider {pid!r} needs kind (gemini, anthropic or openai) and base_url")
            discover = raw.get("discover")
            if discover is not None and discover not in _DISCOVER_RULES:
                raise ValueError(f"provider {pid!r}: unknown discover rules {discover!r}")
            if discover is None and not raw.get("models"):
                raise ValueError(f"provider {pid!r} needs discover or models")
            providers[pid] = {
                "label": str(raw.get("label") or pid), "kind": raw["kind"],
                "base_url": str(raw["base_url"]).rstrip("/"),
                "key_hint": str(raw.get("key_hint") or ""), "key_url": str(raw.get("key_url") or ""),
                "discover": discover, "min_version": float(raw.get("min_version") or 0),
                "vendors": {str(k): str(v) for k, v in (raw.get("vendors") or {}).items()},
            }
            if raw.get("models"):
                preferred[pid] = [_catalog_model(m) for m in raw["models"]]
                fallback[pid] = [m["id"] for m in preferred[pid]]
            else:
                fallback[pid] = [str(x) for x in raw.get("fallback") or []]
            if raw.get("extra_models"):
                extra[pid] = [_catalog_model(m) for m in raw["extra_models"]]
        if not providers:
            raise ValueError("no providers listed")
        order = doc.get("best_auto_order") or {}
        best = {role: [p for p in (order.get(role) or providers) if p in providers] for role in ("large", "fast")}
    except Exception as e:
        if _catalog_mtime is None:
            raise
        logger.warning("Ignoring invalid %s: %s", CATALOG_PATH, e)
        _catalog_mtime = mtime
        return
    for target, value in ((PROVIDERS, providers), (PREFERRED_MODELS, preferred), (EXTRA_MODELS, extra),
                          (FALLBACK_MODELS, fallback), (_BEST_ORDER, best)):
        target.clear()
        target.update(value)
    MAX_MODELS = max(1, int(doc.get("max_models") or 4))
    _catalog_mtime = mtime


_FAST_TIERS = {"flash", "flash-lite", "mini", "haiku", "fast"}
_TIER_RANK = {"pro": 0, "flash": 1, "flash-lite": 2, "": 0, "mini": 1, "fast": 1, "fable": 0, "opus": 1, "sonnet": 2, "haiku": 3}

_GEMINI_RE = re.compile(r"^gemini-(\d+(?:\.\d+)?)-(pro|flash-lite|flash)(-preview)?$")
_OPENAI_RE = re.compile(r"^gpt-(\d+(?:\.\d+)?)(?:-(mini))?$")
_CLAUDE_RE = re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:[-.](\d{1,2}))?(?:-(\d{8}))?$")
_GROK_RE = re.compile(r"^grok-(\d+)(?:[-.](\d))?(?:-(fast|mini))?(?:-reasoning)?(?:-\d{4})?$")


def _parse(rules: str, model_id: str, min_version: float = 0) -> dict[str, Any] | None:
    """Version, tier and label for a flagship chat model id; None for anything else."""
    info = None
    if rules == "gemini":
        m = _GEMINI_RE.match(model_id)
        if m:
            tier, preview = m.group(2), bool(m.group(3))
            info = {"version": float(m.group(1)), "tier": tier, "stable": not preview,
                    "label": f"Gemini {m.group(1)} {tier.title()}" + (" (preview)" if preview else "")}
    elif rules == "openai":
        m = _OPENAI_RE.match(model_id)
        if m:
            tier = m.group(2) or ""
            info = {"version": float(m.group(1)), "tier": tier, "stable": True,
                    "label": f"GPT-{m.group(1)}" + (" mini" if tier else "")}
    elif rules == "anthropic":
        m = _CLAUDE_RE.match(model_id)
        if m:
            family, major, minor = m.group(1), m.group(2), m.group(3)
            version_text = f"{major}.{minor}" if minor else major
            info = {"version": float(version_text), "tier": family, "stable": True,
                    "label": f"Claude {family.title()} {version_text}"}
    elif rules == "xai":
        m = _GROK_RE.match(model_id)
        if m:
            version_text = f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)
            tier = m.group(3) or ""
            info = {"version": float(version_text), "tier": tier, "stable": True,
                    "label": f"Grok {version_text}" + (f" {tier.title()}" if tier else "")}
    if info and info["version"] < min_version:
        return None
    return info


def _rank(provider: str, ids: list[str]) -> list[dict[str, Any]]:
    meta = PROVIDERS.get(provider) or {}
    if provider in PREFERRED_MODELS:
        have = set(ids)
        out = [dict(m, tier="fast" if m["fast"] else "") for m in PREFERRED_MODELS[provider] if m["id"] in have]
        top = out[:MAX_MODELS]
        # Keep one fast model for Auto's quick tasks even when the list is cut short.
        quick = next((m for m in out if m["fast"]), None)
        if quick and quick not in top:
            top[-1] = quick
        return top
    if meta.get("discover") == "openrouter":
        vendors = meta.get("vendors") or {}
        by_vendor: dict[str, list[str]] = {}
        for mid in ids:
            vendor, _, rest = mid.partition("/")
            if vendor in vendors:
                by_vendor.setdefault(vendor, []).append(rest)
        strong, fast = [], []
        for vendor, sub in vendors.items():
            ranked = _rank(sub, by_vendor.get(vendor, []))
            top = next((m for m in ranked if m["tier"] not in _FAST_TIERS), None)
            if top:
                strong.append(dict(top, id=f"{vendor}/{top['id']}"))
            quick = next((m for m in ranked if m["tier"] in _FAST_TIERS), None)
            if quick and not fast:
                fast.append(dict(quick, id=f"{vendor}/{quick['id']}"))
        return strong[:MAX_MODELS] + fast
    best: dict[tuple[float, str], dict[str, Any]] = {}
    for model_id in ids:
        info = _parse(str(meta.get("discover") or ""), model_id, meta.get("min_version") or 0)
        if not info:
            continue
        key = (info["version"], info["tier"])
        current = best.get(key)
        # Prefer stable over preview, and the undated alias over a dated snapshot.
        if current is None or (info["stable"], -len(model_id)) > (current["stable"], -len(current["id"])):
            best[key] = dict(info, id=model_id)
    # Take turns across tiers (strongest first), newest version each turn, so the list mixes
    # e.g. Pro, Flash and Flash-Lite instead of filling up with Flash versions.
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for m in sorted(best.values(), key=lambda m: -m["version"]):
        by_tier.setdefault(m["tier"], []).append(m)
    tiers = sorted(by_tier.values(), key=lambda ms: (_TIER_RANK.get(ms[0]["tier"], 9), -ms[0]["version"]))
    ranked = [ms[i] for i in range(max(map(len, tiers), default=0)) for ms in tiers if i < len(ms)]
    return ranked[:MAX_MODELS]


def _fetch_model_ids(provider: str, key: str) -> list[str]:
    """Model ids the key can use. Raises PermissionError for a rejected key."""
    meta = PROVIDERS[provider]
    base = meta["base_url"]
    if meta["kind"] == "gemini":
        resp = requests.get(f"{base}/models", params={"key": key, "pageSize": 1000}, timeout=15)
    elif meta["kind"] == "anthropic":
        resp = requests.get(f"{base}/models", params={"limit": 1000},
                            headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=15)
    else:
        if meta.get("discover") == "openrouter":
            # The model list is public, so check the key separately.
            check = requests.get(f"{base}/key", headers={"Authorization": f"Bearer {key}"}, timeout=15)
            if check.status_code in (401, 403):
                raise PermissionError(f"{meta['label']} rejected this API key.")
        resp = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
    if resp.status_code in (400, 401, 403):
        raise PermissionError(f"{PROVIDERS[provider]['label']} rejected this API key.")
    resp.raise_for_status()
    body = resp.json()
    if meta["kind"] == "gemini":
        return [str(m.get("name", "")).removeprefix("models/") for m in body.get("models") or []
                if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    return [str(m.get("id", "")) for m in body.get("data") or []]


def _read() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load() -> dict[str, Any]:
    _load_catalog()
    with _LOCK:
        return _read()


def _saved(provider: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return ((data if data is not None else load()).get("providers") or {}).get(provider) or {}


def provider_config(provider: str) -> dict[str, str]:
    return {"api_key": str(_saved(provider).get("api_key") or "").strip(),
            "base_url": PROVIDERS.get(provider, {}).get("base_url", "")}


def api_key(provider: str) -> str:
    return provider_config(provider)["api_key"]


def is_configured(provider: str) -> bool:
    return provider in PROVIDERS and bool(api_key(provider))


def active_provider(data: dict[str, Any] | None = None) -> str | None:
    data = data if data is not None else load()
    chosen = str(data.get("provider") or "")
    if chosen in PROVIDERS and str(_saved(chosen, data).get("api_key") or "").strip():
        return chosen
    for pid in PROVIDERS:
        if str(_saved(pid, data).get("api_key") or "").strip():
            return pid
    return chosen if chosen in PROVIDERS else None


def provider_models(provider: str, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The models the chat menu offers for a provider. vision: reads images; code: tuned for code."""
    fetched = _saved(provider, data).get("models")
    ids = fetched if isinstance(fetched, list) and fetched else FALLBACK_MODELS.get(provider, [])
    ranked = _rank(provider, ids)
    have = set(ids)
    ranked += [dict(m, tier="fast" if m["fast"] else "") for m in EXTRA_MODELS.get(provider, [])
               if m["id"] in have and all(r["id"] != m["id"] for r in ranked)]
    return [{"id": m["id"], "provider": provider, "label": m["label"], "vision": m.get("vision", True),
             "code": bool(m.get("code")), "fast": m["tier"] in _FAST_TIERS} for m in ranked]


_NAME_PREFIXES = {"gemini": ("gemini-",), "anthropic": ("claude-",), "openai": ("gpt-", "o1", "o3", "o4", "chatgpt-"),
                  "xai": ("grok-",), "mistral": ("mistral-", "pixtral-")}


def infer_provider(model_id: str) -> str | None:
    m = (model_id or "").strip().lower()
    for provider, prefixes in _NAME_PREFIXES.items():
        if provider in PROVIDERS and m.startswith(prefixes):
            return provider
    for catalog in (PREFERRED_MODELS, EXTRA_MODELS):
        for provider, models in catalog.items():
            if any(m["id"] == model_id for m in models):
                return provider
    return None


def find_model(model: str) -> dict[str, Any] | None:
    """Accepts "provider:model" or a bare model id of a provider that has a key."""
    key = (model or "").strip()
    if not key:
        return None
    provider, sep, rest = key.partition(":")
    if sep and provider in PROVIDERS:
        key = rest
    else:
        active = active_provider()
        if active and any(m["id"] == key for m in provider_models(active)):
            provider = active
        else:
            provider = infer_provider(key) or ""
    if provider not in PROVIDERS:
        return None
    for m in provider_models(provider):
        if m["id"] == key:
            return m
    listed = next((m for m in PREFERRED_MODELS.get(provider, []) + EXTRA_MODELS.get(provider, []) if m["id"] == key), None)
    if listed:
        return {"id": key, "provider": provider, "label": listed["label"], "vision": listed["vision"],
                "code": listed["code"], "fast": listed["fast"]}
    info = _parse(str((PROVIDERS.get(provider) or {}).get("discover") or ""), key)
    return {"id": key, "provider": provider, "label": info["label"] if info else key,
            "vision": True, "code": False, "fast": False}


def _role_model(provider: str, role: str, *, images: bool = False, code: bool = False) -> str | None:
    """Auto's pick within one provider. images: must read images. code: prefer a code model."""
    models = provider_models(provider)
    if images:
        models = [m for m in models if m["vision"]]
    if not models:
        return None
    fast = role == "fast"
    if code:
        coders = [m for m in models if m["code"]]
        pick = next((m for m in coders if m["fast"] == fast), None) or (coders[0] if coders else None)
        if pick:
            return pick["id"]
    general = [m for m in models if not m["code"]] or models
    wanted = [m for m in general if m["fast"] == fast]
    return (wanted or general)[0]["id"]


def best_auto(data: dict[str, Any] | None = None) -> bool:
    return bool((data if data is not None else load()).get("best_auto"))


def default_model(role: str = "fast", *, images: bool = False, code: bool = False) -> str | None:
    """Auto's model for a task. images: the request carries images, so the model must read them
    (another provider with a key is used when the default one has no such model)."""
    role = "fast" if role == "fast" else "large"
    data = load()
    if best_auto(data):
        for provider in _BEST_ORDER[role]:
            if is_configured(provider):
                model = _role_model(provider, role, images=images, code=code)
                if model:
                    return f"{provider}:{model}"
        return None
    provider = active_provider(data)
    if not provider or not is_configured(provider):
        return None
    model = _role_model(provider, role, images=images, code=code)
    if model or not images:
        return model
    for other in _BEST_ORDER[role]:
        if other != provider and is_configured(other):
            model = _role_model(other, role, images=True)
            if model:
                return f"{other}:{model}"
    return _role_model(provider, role, code=code)


def image_models() -> list[str]:
    """Quick models that read images ("provider:model"), best first: the default provider's, then
    other providers with a key. Used to describe images for text-only models."""
    first = default_model("fast", images=True)
    if first and ":" not in first:
        first = f"{active_provider()}:{first}"
    out = [first] if first else []
    for provider in _BEST_ORDER["fast"]:
        if is_configured(provider):
            model = _role_model(provider, "fast", images=True)
            if model and f"{provider}:{model}" not in out:
                out.append(f"{provider}:{model}")
    return out


def image_model() -> str | None:
    models = image_models()
    return models[0] if models else None


def _refresh(provider: str) -> None:
    key = api_key(provider)
    if not key:
        return
    try:
        ids = _fetch_model_ids(provider, key)
    except Exception:
        ids = None
    with _LOCK:
        data = _read()
        cfg = (data.get("providers") or {}).get(provider)
        if not cfg or cfg.get("api_key") != key:
            return
        if ids:
            cfg["models"] = ids
        cfg["checked_at"] = time.time()
        _write(data)


def status() -> dict[str, Any]:
    data = load()
    active = active_provider(data)
    providers = []
    for pid, meta in PROVIDERS.items():
        cfg = _saved(pid, data)
        key = str(cfg.get("api_key") or "")
        if key and time.time() - float(cfg.get("checked_at") or 0) > _REFRESH_AFTER_S:
            threading.Thread(target=_refresh, args=(pid,), daemon=True).start()
        providers.append({
            "id": pid, "label": meta["label"], "configured": bool(key),
            "key_preview": ("…" + key[-4:]) if len(key) >= 8 else ("set" if key else ""),
            "key_hint": meta["key_hint"], "key_url": meta["key_url"],
        })
    models = [dict(m, value=m["id"]) for m in provider_models(active, data)] if active else []
    return {"provider": active or "", "best_auto": best_auto(data), "providers": providers, "models": models,
            "fast_model": default_model("fast") or "", "large_model": default_model("large") or "",
            "code_model": default_model("fast", code=True) or "", "image_model": image_model() or ""}


def update(payload: dict[str, Any]) -> dict[str, Any]:
    pid = str(payload.get("provider") or "").strip()
    if pid and pid not in PROVIDERS:
        raise ValueError(f"Unknown provider: {pid}")
    new_key = str(payload.get("api_key") or "").strip() if "api_key" in payload else None
    fetched: list[str] | None = None
    if pid and new_key:
        try:
            fetched = _fetch_model_ids(pid, new_key)
        except PermissionError as e:
            raise ValueError(str(e)) from e
        except Exception:
            fetched = None
    with _LOCK:
        data = _read()
        providers = data.setdefault("providers", {})
        default = str(data.get("provider") or "")
        has_key = lambda p: bool(str((providers.get(p) or {}).get("api_key") or "").strip())
        if pid and payload.get("clear"):
            providers.pop(pid, None)
            if default == pid:
                # The default lost its key: move to another provider that has one.
                data["provider"] = next((p for p in PROVIDERS if has_key(p)), "")
        elif pid and new_key:
            providers[pid] = {"api_key": new_key, "checked_at": time.time(), **({"models": fetched} if fetched else {})}
            # Saving a key only changes the default when the current default cannot be used.
            if not has_key(default):
                data["provider"] = pid
        elif pid:
            if not has_key(pid):
                raise ValueError(f"Add an API key for {PROVIDERS[pid]['label']} before making it the default.")
            data["provider"] = pid
        if "best_auto" in payload:
            data["best_auto"] = bool(payload.get("best_auto"))
        _write(data)
    return status()


_load_catalog()
