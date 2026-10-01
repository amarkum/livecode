"""Shared helpers for chat multimodal attachments (Lazie, LiveCode, etc.)."""
from __future__ import annotations

from typing import Any


def build_multimodal_user_content(text: str, attachments: list[dict[str, Any]] | None) -> str | list[dict[str, Any]]:
    """Build message content — multimodal list when images present, string otherwise.

    Handles:
    - Images: added as image_url parts
    - PDF/DOCX: client-extracted text inlined
    - Text files: content inlined
    """
    if not attachments:
        return text

    image_attachments = [a for a in attachments if a.get("type") == "image" and a.get("data")]
    text_attachments = [
        a for a in attachments
        if a.get("type") in ("text", "pdf", "docx") and a.get("content") is not None
    ]

    if text_attachments:
        text_parts = [text]
        for att in text_attachments:
            name = att.get("name", "unknown")
            content = att.get("content", "")
            file_type = att.get("type", "text")

            if file_type == "pdf":
                page_count = att.get("pageCount", "unknown")
                header = f"--- PDF Document: {name} ({page_count} pages) ---"
            elif file_type == "docx":
                header = f"--- Word Document: {name} ---"
            else:
                header = f"--- Attached file: {name} ---"

            trunc_note = ""
            if att.get("truncated"):
                trunc_note = "\n(content truncated)"

            text_parts.append(f"\n\n{header}\n```\n{content}\n```{trunc_note}")
        text = "\n".join(text_parts)

    if not image_attachments:
        return text

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for img in image_attachments:
        content.append({
            "type": "image_url",
            "image_url": {"url": img.get("data"), "detail": "auto"},
        })
    return content


def extract_text_from_user_content(content: str | list[dict[str, Any]]) -> str:
    """Return plain text from a user message (string or multimodal list)."""
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts).strip()


def has_image_attachments(attachments: list[dict[str, Any]] | None) -> bool:
    return any(a.get("type") == "image" and a.get("data") for a in (attachments or []))
