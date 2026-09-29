"""Routes each request to the provider that owns the model (Gemini, OpenAI, Anthropic, open-source)."""

from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from . import settings as llm_settings
from .anthropic_client import AnthropicClient
from .gemini_client import GeminiClient
from .openai_compat_client import OpenAICompatClient


logger = logging.getLogger(__name__)

_IMAGE_KINDS = ("image_url", "input_image", "image")
_DESCRIBE_PROMPT = (
    "Describe this image for a coding assistant that cannot see it. Transcribe all visible text exactly "
    "(code, error messages, labels, UI copy). Describe the layout from top to bottom and left to right, "
    "with approximate positions and sizes, colours, and anything that looks broken or highlighted. "
    "Be precise and complete; do not guess at what is not visible."
)
_DESCRIPTIONS_KEPT = 256
_FAILED_RETRY_S = 300


def _image_ref(block: Any) -> str:
    value = block.get("image_url") if isinstance(block, dict) else None
    if isinstance(value, dict):
        value = value.get("url")
    return str(value or block.get("url") or block.get("data") or "") if isinstance(block, dict) else ""


def _has_images(messages: list[dict]) -> bool:
    return any(isinstance(b, dict) and b.get("type") in _IMAGE_KINDS
               for m in messages if isinstance(m.get("content"), list) for b in m["content"])


class LLMRouter:
    def __init__(
        self,
        *,
        gemini_models: dict[str, str] | None = None,
        large_content_char_threshold: int = 600_000,
        large_file_count_threshold: int = 40,
        max_input_tokens_for_model: Callable[[str], int] | None = None,
    ):
        self._gemini = GeminiClient(
            models=gemini_models,
            host=llm_settings.PROVIDERS["gemini"]["base_url"],
            get_token=lambda: llm_settings.api_key("gemini"),
        )
        self._anthropic = AnthropicClient(lambda: llm_settings.provider_config("anthropic"))
        self._compat: dict[str, OpenAICompatClient] = {}
        self._large_chars = large_content_char_threshold
        self._large_files = large_file_count_threshold
        self._max_input_tokens_for_model = max_input_tokens_for_model
        self._descriptions: OrderedDict[str, str] = OrderedDict()
        self._descriptions_lock = threading.Lock()
        self._failed: dict[str, float] = {}

    def _compat_client(self, provider: str) -> OpenAICompatClient:
        if provider not in self._compat:
            self._compat[provider] = OpenAICompatClient(provider, lambda p=provider: llm_settings.provider_config(p))
        return self._compat[provider]

    def _resolve(self, model: str) -> tuple[str, str]:
        """Returns (provider, provider-side model id)."""
        key = (model or "").strip()
        if key.lower() == "auto":
            key = ""
        found = llm_settings.find_model(key) if key else None
        if found and llm_settings.is_configured(found["provider"]):
            return found["provider"], found["id"]
        # Auto, or a model whose provider has no key: use the active provider's matching tier.
        role = "large" if any(t in key for t in ("pro", "5.4", "5.5", "opus", "fable", "large")) else "fast"
        fallback = llm_settings.default_model(role)
        if not fallback:
            raise ValueError("No model provider is set up. Add an API key in LiveCode Settings > Models.")
        fb = llm_settings.find_model(fallback)
        return fb["provider"], fb["id"]

    def _client(self, provider: str):
        kind = llm_settings.PROVIDERS[provider]["kind"]
        if kind == "gemini":
            return self._gemini
        if kind == "anthropic":
            return self._anthropic
        return self._compat_client(provider)

    def supports_model(self, model: str) -> bool:
        try:
            self._resolve(model)
            return True
        except ValueError:
            return False

    def reads_images(self, model: str) -> bool:
        """True when the model itself can look at images."""
        try:
            provider, model_id = self._resolve(model)
        except ValueError:
            return False
        found = llm_settings.find_model(f"{provider}:{model_id}")
        return bool(found and found.get("vision"))

    def supports_images(self, model: str) -> bool:
        """True when images can be sent with requests to the model: it reads them, or another
        model can describe them in text for it."""
        return self.reads_images(model) or bool(llm_settings.image_model())

    # --- Images for text-only models -------------------------------------------------------

    def _describe_image(self, ref: str) -> str:
        if not ref:
            return "[An image was attached here.]"
        key = hashlib.sha1(ref.encode("utf-8", "ignore")).hexdigest()
        with self._descriptions_lock:
            if key in self._descriptions:
                self._descriptions.move_to_end(key)
                return self._descriptions[key]
            if time.monotonic() - self._failed.get(key, -_FAILED_RETRY_S) < _FAILED_RETRY_S:
                return "[An image was attached, but no model could describe it.]"
        describers = llm_settings.image_models()
        if not describers:
            return "[An image was attached, but no model that reads images is set up, so it could not be described.]"
        request = [{"role": "user", "content": [
            {"type": "text", "text": _DESCRIBE_PROMPT},
            {"type": "image_url", "image_url": {"url": ref}},
        ]}]
        for describer in describers:
            try:
                text = self.complete(describer, request, timeout=90).strip()
            except Exception as e:
                logger.warning("Could not describe an image with %s: %s", describer, str(e)[:300])
                continue
            if not text:
                continue
            text = f"[Image described by {describer}, because this model cannot view images]\n{text}"
            with self._descriptions_lock:
                self._descriptions[key] = text
                while len(self._descriptions) > _DESCRIPTIONS_KEPT:
                    self._descriptions.popitem(last=False)
            return text
        with self._descriptions_lock:
            self._failed[key] = time.monotonic()
        return "[An image was attached, but no model could describe it.]"

    def _for_model(self, provider: str, model_id: str, messages: list[dict]) -> list[dict]:
        """Messages as this model can take them: images become text descriptions for a
        model that cannot view images. Each image is described once and then reused."""
        if not _has_images(messages):
            return messages
        found = llm_settings.find_model(f"{provider}:{model_id}")
        if found and found.get("vision"):
            return messages
        out = copy.copy(messages)
        for i, msg in enumerate(out):
            content = msg.get("content")
            if not isinstance(content, list) or not any(isinstance(b, dict) and b.get("type") in _IMAGE_KINDS for b in content):
                continue
            blocks = [{"type": "text", "text": self._describe_image(_image_ref(b))}
                      if isinstance(b, dict) and b.get("type") in _IMAGE_KINDS else b for b in content]
            out[i] = {**msg, "content": blocks}
        return out

    def pick_model(self, user_model: str | None = None, *, content_chars: int = 0, file_count: int = 0, task: str = "chat") -> str:
        model = (user_model or "").strip()
        is_auto = not model or model.lower() == "auto"
        if task == "fast" and is_auto:
            return llm_settings.default_model("fast") or model
        wants_large = task == "large" or content_chars > self._large_chars or file_count > self._large_files
        if not wants_large and content_chars > 0 and self._max_input_tokens_for_model and not is_auto:
            limit = self._max_input_tokens_for_model(model)
            wants_large = bool(limit and content_chars > limit * 3)
        if is_auto:
            return llm_settings.default_model("large" if wants_large else "fast") or model
        return model

    def complete(self, model: str, messages: list[dict], *, prompt_cache_key: str | None = None, timeout=30) -> str:
        provider, model_id = self._resolve(model)
        return self._client(provider).complete(model_id, self._for_model(provider, model_id, messages), timeout=timeout)

    def stream(self, model: str, messages: list[dict], *, max_tokens: int = 16_000, timeout=60):
        provider, model_id = self._resolve(model)
        return self._client(provider).stream(model_id, self._for_model(provider, model_id, messages),
                                             max_tokens=max_tokens, timeout=timeout)

    def complete_with_tools(self, model: str, messages: list[dict], tools: list[dict], **kwargs: Any) -> dict[str, Any]:
        provider, model_id = self._resolve(model)
        return self._client(provider).complete_with_tools(model_id, self._for_model(provider, model_id, messages), tools, **kwargs)
