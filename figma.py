from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse, parse_qs
from urllib.request import Request, urlopen

CONFIG_PATH = os.path.expanduser("~/.livecode/figma.json")
MAX_IMAGE_BYTES = 30 * 1024 * 1024
OUTLINE_MAX_LINES = 220
OUTLINE_MAX_CHARS = 14_000


class FigmaError(RuntimeError):
    pass


def api_base() -> str:
    return (os.environ.get("LIVECODE_FIGMA_API") or "https://api.figma.com").rstrip("/")




def token() -> str:
    for name in ("FIGMA_TOKEN", "FIGMA_ACCESS_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            return str((json.load(handle) or {}).get("token") or "").strip()
    except (OSError, ValueError):
        return ""


def token_status() -> dict[str, Any]:
    for name in ("FIGMA_TOKEN", "FIGMA_ACCESS_TOKEN"):
        if os.environ.get(name, "").strip():
            return {"configured": True, "source": "env", "env": name}
    return {"configured": bool(token()), "source": "settings" if token() else ""}


def save_token(value: str) -> dict[str, Any]:
    value = str(value or "").strip()
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not value:
        try:
            os.remove(CONFIG_PATH)
        except OSError:
            pass
        return token_status()
    if not re.match(r"^[\w.:-]{10,200}$", value):
        raise FigmaError("That does not look like a Figma personal access token.")
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"token": value}, handle)
    os.replace(tmp, CONFIG_PATH)
    return token_status()




def is_figma_url(text: str) -> bool:
    host = (urlparse(str(text or "").strip()).hostname or "").lower()
    return host == "figma.com" or host.endswith(".figma.com")


def parse_url(url: str) -> tuple[str, str]:
    parsed = urlparse(str(url or "").strip())
    parts = [p for p in parsed.path.split("/") if p]
    key = ""
    for index, part in enumerate(parts):
        if part in ("file", "design", "proto", "board") and index + 1 < len(parts):
            key = parts[index + 1]
            if index + 3 < len(parts) and parts[index + 2] == "branch":
                key = parts[index + 3]
            break
    if not key:
        raise FigmaError("That Figma link has no file in it. Copy the link to a frame (right-click the frame -> Copy link).")
    node = unquote((parse_qs(parsed.query).get("node-id") or [""])[0]).strip()
    return key, node.replace("-", ":") if node else ""




def _get(path: str, *, timeout: float = 60) -> Any:
    secret = token()
    if not secret:
        raise FigmaError(
            "Figma links need a personal access token (Figma -> Settings -> Security -> Personal access tokens, "
            "file read access). Add it in LiveCode Settings -> Agent -> Browser, or set FIGMA_TOKEN on the LiveCode "
            "server. Or export the frame as a PNG and attach it to the chat."
        )
    request = Request(api_base() + path, headers={"X-Figma-Token": secret, "User-Agent": "LiveCode"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_IMAGE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise FigmaError("Figma refused the token (HTTP %d): it is invalid, expired, or has no access to this file." % exc.code) from exc
        if exc.code == 404:
            raise FigmaError("Figma has no such file or node (HTTP 404). Check the link.") from exc
        if exc.code == 429:
            raise FigmaError("Figma's rate limit was hit (HTTP 429); wait a minute and try again.") from exc
        raise FigmaError(f"Figma answered HTTP {exc.code}.") from exc
    except (URLError, OSError, ValueError) as exc:
        raise FigmaError(f"Could not reach Figma: {str(exc).splitlines()[0][:200]}") from exc
    if len(body) > MAX_IMAGE_BYTES:
        raise FigmaError("Figma's answer is too large.")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise FigmaError("Figma answered with something that is not JSON.") from exc


def fetch_node(file_key: str, node_id: str, depth: int = 6) -> dict[str, Any]:
    if not node_id:
        data = _get(f"/v1/files/{quote(file_key)}?depth=2")
        pages = ((data.get("document") or {}).get("children") or [])
        frames = [child for page in pages[:1] for child in (page.get("children") or [])]
        if not frames:
            raise FigmaError("The file has no frames on its first page; link to a frame instead.")
        node_id = str(frames[0].get("id") or "")
    data = _get(f"/v1/files/{quote(file_key)}/nodes?ids={quote(node_id)}&depth={max(1, min(int(depth), 12))}")
    entry = (data.get("nodes") or {}).get(node_id) or {}
    document = entry.get("document")
    if not document:
        raise FigmaError(f"Figma has no node {node_id} in that file.")
    return document


def render_node(file_key: str, node_id: str, scale: float = 2) -> bytes:
    scale = min(max(float(scale or 2), 0.25), 4)
    data = _get(f"/v1/images/{quote(file_key)}?ids={quote(node_id)}&format=png&scale={scale:g}&use_absolute_bounds=true")
    if data.get("err"):
        raise FigmaError(f"Figma could not render the node: {data.get('err')}")
    url = (data.get("images") or {}).get(node_id)
    if not url:
        raise FigmaError("Figma rendered nothing for that node (it may be hidden or empty).")
    return _download(url)


def _download(url: str, timeout: float = 60) -> bytes:
    if urlparse(url).scheme not in ("https", "http"):
        raise FigmaError("Figma returned an image link that is not http(s).")
    try:
        with urlopen(Request(url, headers={"User-Agent": "LiveCode"}), timeout=timeout) as response:
            body = response.read(MAX_IMAGE_BYTES + 1)
    except HTTPError as exc:
        raise FigmaError(f"Downloading Figma's render failed (HTTP {exc.code}).") from exc
    except (URLError, OSError, ValueError) as exc:
        raise FigmaError(f"Could not download Figma's render: {str(exc).splitlines()[0][:200]}") from exc
    if len(body) > MAX_IMAGE_BYTES:
        raise FigmaError("Figma's render is too large; link to a smaller frame.")
    return body




def _hex(color: dict[str, Any] | None, opacity: float | None = None) -> str:
    if not color:
        return ""
    r, g, b = (round(float(color.get(k, 0)) * 255) for k in ("r", "g", "b"))
    alpha = float(color.get("a", 1)) * (1 if opacity is None else float(opacity))
    text = f"#{r:02x}{g:02x}{b:02x}"
    return text if alpha >= 0.995 else f"{text} {round(alpha * 100)}%"


def _paint(paints: list[dict[str, Any]] | None) -> str:
    for paint in paints or []:
        if paint.get("visible") is False:
            continue
        kind = paint.get("type")
        if kind == "SOLID":
            return _hex(paint.get("color"), paint.get("opacity"))
        if kind and kind.startswith("GRADIENT"):
            stops = ", ".join(_hex(stop.get("color")) for stop in (paint.get("gradientStops") or [])[:3])
            return f"{kind.lower().replace('_', '-')}({stops})"
        if kind == "IMAGE":
            return "image"
    return ""


def _num(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:.1f}"


def node_details(node: dict[str, Any], origin: tuple[float, float] = (0.0, 0.0)) -> dict[str, Any]:
    box = node.get("absoluteBoundingBox") or {}
    out: dict[str, Any] = {
        "id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type"),
    }
    if box:
        out["box"] = {
            "x": round(float(box.get("x", 0)) - origin[0], 1),
            "y": round(float(box.get("y", 0)) - origin[1], 1),
            "width": round(float(box.get("width", 0)), 1),
            "height": round(float(box.get("height", 0)), 1),
        }
    fill = _paint(node.get("fills"))
    if fill:
        out["text_color" if node.get("type") == "TEXT" else "fill"] = fill
    stroke = _paint(node.get("strokes"))
    if stroke and node.get("strokeWeight"):
        out["stroke"] = f"{_num(node['strokeWeight'])}px {stroke}"
    radii = node.get("rectangleCornerRadii")
    if radii and len(set(radii)) > 1:
        out["radius"] = " ".join(_num(r) for r in radii)
    elif node.get("cornerRadius"):
        out["radius"] = _num(node["cornerRadius"])
    if node.get("type") == "TEXT":
        style = node.get("style") or {}
        out["text"] = str(node.get("characters") or "")[:160]
        font = [str(style.get("fontFamily") or "")]
        if style.get("fontWeight"):
            font.append(_num(style["fontWeight"]))
        if style.get("fontSize"):
            font.append(_num(style["fontSize"]) + "px")
        if style.get("lineHeightPx"):
            font.append("/" + _num(style["lineHeightPx"]) + "px")
        out["font"] = " ".join(part for part in font if part)
        if style.get("letterSpacing"):
            out["letter_spacing"] = _num(style["letterSpacing"]) + "px"
        if style.get("textAlignHorizontal") and style.get("textAlignHorizontal") != "LEFT":
            out["align"] = str(style["textAlignHorizontal"]).lower()
    if node.get("layoutMode") and node.get("layoutMode") != "NONE":
        pads = [node.get(k, 0) or 0 for k in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft")]
        out["layout"] = f"{str(node['layoutMode']).lower()} gap {_num(node.get('itemSpacing') or 0)}"
        if any(pads):
            out["padding"] = " ".join(_num(p) for p in pads)
    shadows = [e for e in node.get("effects") or [] if e.get("type") in ("DROP_SHADOW", "INNER_SHADOW") and e.get("visible", True)]
    if shadows:
        e = shadows[0]
        offset = e.get("offset") or {}
        out["shadow"] = f"{_num(offset.get('x', 0))} {_num(offset.get('y', 0))} {_num(e.get('radius', 0))} {_hex(e.get('color'))}"
    if node.get("opacity") is not None and float(node["opacity"]) < 0.995:
        out["opacity"] = round(float(node["opacity"]), 2)
    return out


def outline(root: dict[str, Any], max_depth: int = 6, origin: tuple[float, float] | None = None) -> tuple[str, list[dict[str, Any]]]:
    if origin is None:
        box = root.get("absoluteBoundingBox") or {}
        origin = (float(box.get("x", 0)), float(box.get("y", 0)))
    lines: list[str] = []
    items: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], depth: int) -> None:
        if len(lines) >= OUTLINE_MAX_LINES or node.get("visible") is False:
            return
        info = node_details(node, origin)
        items.append(info)
        b = info.get("box") or {}
        bits = [f"{'  ' * depth}{info.get('name')!s} ({str(info.get('type') or '').lower()})"]
        if b:
            bits.append(f"{_num(b['x'])},{_num(b['y'])} {_num(b['width'])}×{_num(b['height'])}")
        for key in ("text", "font", "text_color", "fill", "stroke", "radius", "layout", "padding", "shadow", "opacity", "letter_spacing", "align"):
            if info.get(key) not in (None, ""):
                value = info[key]
                bits.append(f'"{value}"' if key == "text" else f"{key.replace('_', ' ')} {value}")
        bits.append(f"[{info.get('id')}]")
        lines.append(" · ".join(bits[:1]) + (" " + " · ".join(bits[1:]) if len(bits) > 1 else ""))
        if depth < max_depth:
            for child in node.get("children") or []:
                walk(child, depth + 1)

    walk(root, 0)
    text = "\n".join(lines)
    if len(text) > OUTLINE_MAX_CHARS:
        text = text[:OUTLINE_MAX_CHARS].rstrip() + "\n… (more layers; load a child node for its details)"
    return text, items


def find_child(root: dict[str, Any], wanted: str) -> dict[str, Any] | None:
    wanted = str(wanted or "").strip()
    if not wanted:
        return None
    wanted_id = wanted.replace("-", ":")
    by_name: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.get("id") == wanted_id:
            return node
        if str(node.get("name") or "").strip().lower() == wanted.lower():
            by_name.append(node)
        stack.extend(reversed(node.get("children") or []))
    return by_name[0] if by_name else None
