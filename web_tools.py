from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import requests

WEB_FETCH_MAX_BYTES = 512 * 1024
WEB_FETCH_DEFAULT_CHARS = 12_000
WEB_FETCH_TIMEOUT = 15
WEB_SEARCH_TIMEOUT = 20
WEB_SEARCH_MAX_RESULTS = 8

_USER_AGENT = "LiveCode/1.0 (LiveCode coding agent)"

class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._chunks.append(text + " ")

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return re.sub(r"[ \t]{2,}", " ", raw).strip()

def _load_web_search_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "provider": (os.environ.get("LIVECODE_WEB_SEARCH_PROVIDER") or "").strip().lower(),
        "tavily_api_key": (os.environ.get("TAVILY_API_KEY") or "").strip(),
        "serper_api_key": (os.environ.get("SERPER_API_KEY") or "").strip(),
    }
    try:
        from app.runtime import CREDENTIALS_FILE, load_livecode_credentials

        creds = load_livecode_credentials() or {}
        livecode = creds.get("livecode") or {}
        if isinstance(livecode, dict):
            if not cfg["provider"]:
                cfg["provider"] = str(livecode.get("web_search_provider") or "").strip().lower()
            if not cfg["tavily_api_key"]:
                cfg["tavily_api_key"] = str(livecode.get("tavily_api_key") or "").strip()
            if not cfg["serper_api_key"]:
                cfg["serper_api_key"] = str(livecode.get("serper_api_key") or "").strip()
    except Exception:
        pass
    return cfg

def web_tools_available() -> bool:
    return True

def web_search_available() -> bool:
    cfg = _load_web_search_config()
    if cfg.get("tavily_api_key") or cfg.get("serper_api_key"):
        return True
    return True

def _hostname_resolves_to_blocked_ip(hostname: str) -> bool:
    if not hostname:
        return True
    host = hostname.strip().lower().rstrip(".")
    if host in ("localhost", "metadata.google.internal"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return True
        if str(ip) == "169.254.169.254":
            return True
    return False

def _validate_fetch_url(url: str) -> tuple[str | None, str | None]:
    raw = (url or "").strip()
    if not raw:
        return None, "url is required"
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http"):
        return None, "Only http and https URLs are allowed"
    if not parsed.netloc:
        return None, "Invalid URL"
    if _hostname_resolves_to_blocked_ip(parsed.hostname or ""):
        return None, "URL host is not allowed (SSRF protection)"
    return raw, None

def _html_to_text(body: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", body)
    return parser.get_text()

def web_fetch(url: str, *, max_chars: int = WEB_FETCH_DEFAULT_CHARS) -> dict[str, Any]:
    safe_url, err = _validate_fetch_url(url)
    if safe_url is None:
        return {"error": err}

    cap = min(max(int(max_chars or WEB_FETCH_DEFAULT_CHARS), 500), 50_000)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain,text/markdown;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(
            safe_url,
            headers=headers,
            timeout=WEB_FETCH_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        final_url = resp.url
        final_host = urlparse(final_url).hostname or ""
        if _hostname_resolves_to_blocked_ip(final_host):
            return {"error": "Redirect target is not allowed (SSRF protection)"}

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > WEB_FETCH_MAX_BYTES:
                break

        raw_bytes = b"".join(chunks)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        charset = resp.encoding or "utf-8"
        try:
            body = raw_bytes.decode(charset, errors="replace")
        except LookupError:
            body = raw_bytes.decode("utf-8", errors="replace")

        if "html" in content_type:
            text = _html_to_text(body)
        else:
            text = body

        truncated = len(text) > cap or total > WEB_FETCH_MAX_BYTES
        if len(text) > cap:
            text = text[:cap] + "\n... [truncated]"

        return {
            "success": True,
            "url": safe_url,
            "final_url": final_url,
            "status_code": resp.status_code,
            "content_type": content_type,
            "content": text,
            "truncated": truncated,
        }
    except requests.Timeout:
        return {"error": f"Request timed out (>{WEB_FETCH_TIMEOUT}s)"}
    except requests.RequestException as exc:
        return {"error": str(exc)}

def _search_tavily(query: str, api_key: str, max_results: int) -> dict[str, Any]:
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": True,
        },
        timeout=WEB_SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in (data.get("results") or [])[:max_results]:
        results.append({
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("content") or "",
        })
    return {
        "success": True,
        "query": query,
        "provider": "tavily",
        "answer": data.get("answer") or "",
        "result_count": len(results),
        "results": results,
    }

def _search_serper(query: str, api_key: str, max_results: int) -> dict[str, Any]:
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "num": max_results},
        timeout=WEB_SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in (data.get("organic") or [])[:max_results]:
        results.append({
            "title": item.get("title") or "",
            "url": item.get("link") or "",
            "snippet": item.get("snippet") or "",
        })
    return {
        "success": True,
        "query": query,
        "provider": "serper",
        "answer": (data.get("answerBox") or {}).get("answer") or "",
        "result_count": len(results),
        "results": results,
    }

def _search_duckduckgo(query: str, max_results: int) -> dict[str, Any]:
    resp = requests.get(
        "https://lite.duckduckgo.com/lite/",
        params={"q": query},
        headers={"User-Agent": _USER_AGENT},
        timeout=WEB_SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.text
    results: list[dict[str, str]] = []
    for match in re.finditer(
        r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
        body,
        re.IGNORECASE,
    ):
        url = html.unescape(match.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        if url and title:
            results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= max_results:
            break

    if not results:
        for match in re.finditer(r'href="(https?://[^"]+)"', body):
            url = html.unescape(match.group(1))
            if "duckduckgo.com" in url:
                continue
            results.append({"title": url, "url": url, "snippet": ""})
            if len(results) >= max_results:
                break

    return {
        "success": True,
        "query": query,
        "provider": "duckduckgo",
        "answer": "",
        "result_count": len(results),
        "results": results,
    }

def web_search(
    query: str,
    *,
    allowed_domains: list[str] | None = None,
    max_results: int = WEB_SEARCH_MAX_RESULTS,
) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}

    cap = min(max(int(max_results or WEB_SEARCH_MAX_RESULTS), 1), 12)
    cfg = _load_web_search_config()
    provider = cfg.get("provider") or ""
    errors: list[str] = []

    try:
        if provider == "serper" and cfg.get("serper_api_key"):
            out = _search_serper(q, cfg["serper_api_key"], cap)
        elif provider == "tavily" and cfg.get("tavily_api_key"):
            out = _search_tavily(q, cfg["tavily_api_key"], cap)
        elif cfg.get("tavily_api_key"):
            out = _search_tavily(q, cfg["tavily_api_key"], cap)
        elif cfg.get("serper_api_key"):
            out = _search_serper(q, cfg["serper_api_key"], cap)
        else:
            out = _search_duckduckgo(q, cap)
    except requests.RequestException as exc:
        errors.append(str(exc))
        try:
            out = _search_duckduckgo(q, cap)
        except requests.RequestException as exc2:
            return {
                "error": f"Web search failed: {exc2}",
                "hint": "Set TAVILY_API_KEY or SERPER_API_KEY in env or credentials.json livecode section.",
            }

    if allowed_domains:
        domains = [d.strip().lower() for d in allowed_domains if d and str(d).strip()]
        filtered = []
        for item in out.get("results") or []:
            host = (urlparse(str(item.get("url") or "")).hostname or "").lower()
            if any(host == d or host.endswith("." + d) for d in domains):
                filtered.append(item)
        out["results"] = filtered[:cap]
        out["result_count"] = len(out["results"])
        out["allowed_domains"] = domains

    if errors:
        out["warnings"] = errors
    return out
