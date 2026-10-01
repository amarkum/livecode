from __future__ import annotations

import base64
import threading
import time
from typing import Any, Iterator

SCREENCAST_QUALITY = 90
MAX_SCALE = 4
# The "standard" view quality: the view box capped at the viewport's CSS size, lighter JPEGs.
STANDARD_QUALITY = 70
STANDARD_MAX_SCALE = 1
MIN_VIEW = 64
VIEWER_GRACE_S = 5.0
KEEPALIVE_S = 2.0
PUMP_MS = 6
FALLBACK_VIEWPORT = {"width": 1280, "height": 800}
BOUNDARY = "livecodeframe"
_PART_HEAD = f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n\r\n".encode("ascii")


class Stream:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.page: Any = None
        self.cdp: Any = None
        self.cond = threading.Condition()
        self.seq = 0
        self.data: bytes | None = None
        self.viewers = 0
        self.last_viewer = time.time()
        self.box: tuple[int, int] | None = None
        self.quality = SCREENCAST_QUALITY
        self.closed = False
        # The page's CSS size as the last frame had it (the screencast's own word, not the viewport
        # the UI last heard of), so the view can keep the frame's real aspect ratio.
        self.page_size: tuple[int, int] | None = None

    def open_viewer(self) -> None:
        with self.cond:
            self.viewers += 1
            self.last_viewer = time.time()

    def close_viewer(self) -> None:
        with self.cond:
            self.viewers = max(0, self.viewers - 1)
            self.last_viewer = time.time()

    def wait(self, after: int, timeout: float) -> tuple[int, bytes | None]:
        with self.cond:
            if self.seq == after and not self.closed:
                self.cond.wait(timeout)
            return self.seq, self.data

    def _publish(self, data: bytes) -> None:
        with self.cond:
            if data == self.data:
                return
            self.seq += 1
            self.data = data
            self.cond.notify_all()

    def _on_frame(self, cdp: Any, params: dict[str, Any]) -> None:
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        if cdp is not self.cdp:
            return
        try:
            data = base64.b64decode(params["data"])
        except Exception:
            return
        meta = params.get("metadata") or {}
        try:
            width, height = int(round(float(meta.get("deviceWidth") or 0))), int(round(float(meta.get("deviceHeight") or 0)))
            if width > 0 and height > 0:
                self.page_size = (width, height)
        except (TypeError, ValueError):
            pass
        self._publish(data)

    def _quality(self) -> tuple[int, int]:
        sharp = getattr(self.session, "view_sharp", True)
        return (MAX_SCALE, SCREENCAST_QUALITY) if sharp else (STANDARD_MAX_SCALE, STANDARD_QUALITY)

    def _page_css_size(self, page: Any) -> tuple[int, int]:
        """The page's CSS size: the viewport when Playwright sets it; the window's own when it does
        not (the user's attached Chrome), else what the last frame said."""
        size = page.viewport_size
        if size:
            return max(1, int(size["width"])), max(1, int(size["height"]))
        try:
            measured = page.evaluate("() => [window.innerWidth, window.innerHeight]")
            if measured and int(measured[0]) > 0 and int(measured[1]) > 0:
                return int(measured[0]), int(measured[1])
        except Exception:
            pass
        if self.page_size:
            return self.page_size
        return FALLBACK_VIEWPORT["width"], FALLBACK_VIEWPORT["height"]

    def _target_box(self, page: Any) -> tuple[int, int]:
        width, height = self._page_css_size(page)
        scale, _quality = self._quality()
        view = getattr(self.session, "view_box", None)
        if view:
            # Scale the page uniformly to fit the reported view box, so frames keep the page's
            # aspect ratio even when the box is from a pane size the viewport has not caught up with.
            fit = min(max(int(view[0]), MIN_VIEW) / width, max(int(view[1]), MIN_VIEW) / height, scale)
            return max(1, round(width * fit)), max(1, round(height * fit))
        return width * scale, height * scale

    def _fit_screen(self, cdp: Any, page: Any) -> None:
        """Headless Chrome clips screencast frames to its emulated screen, which does not grow
        with the viewport, so a resized page streams cut off and stretched. Make the screen the
        viewport's size. Skipped for the user's own attached Chrome, whose screen is real."""
        if getattr(self.session, "shared", False):
            return
        width, height = self._page_css_size(page)
        try:
            dpr = float(page.evaluate("devicePixelRatio") or 1)
            cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": dpr, "mobile": False,
                "screenWidth": width, "screenHeight": height,
            })
        except Exception:
            pass

    def _start(self, cdp: Any, box: tuple[int, int], page: Any = None) -> None:
        self.quality = self._quality()[1]
        if page is not None:
            self._fit_screen(cdp, page)
        cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": self.quality,
            "maxWidth": box[0],
            "maxHeight": box[1],
            "everyNthFrame": 1,
        })

    def _attach(self, page: Any) -> None:
        self._detach()
        if self.session.shared:
            try:
                page.bring_to_front()
            except Exception:
                pass
        cdp = page.context.new_cdp_session(page)
        cdp.on("Page.screencastFrame", lambda params, cdp=cdp: self._on_frame(cdp, params))
        box = self._target_box(page)
        self.cdp = cdp
        self.page = page
        self.box = box
        try:
            self._start(cdp, box, page)
        except Exception:
            self._detach()
            raise

    def _resize(self, page: Any, box: tuple[int, int]) -> None:
        try:
            self.cdp.send("Page.stopScreencast")
            self._start(self.cdp, box, page)
            self.box = box
        except Exception:
            self._attach(page)

    def _detach(self) -> None:
        cdp, self.cdp = self.cdp, None
        self.page = None
        self.box = None
        if cdp is None:
            return
        try:
            cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            cdp.detach()
        except Exception:
            pass

    def drop_page(self) -> None:
        self._detach()

    def stop(self) -> None:
        self._detach()
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def service(self) -> bool:
        if self.closed:
            return False
        now = time.time()
        if self.viewers <= 0 and now - self.last_viewer > VIEWER_GRACE_S:
            self.stop()
            return False
        session = self.session
        page = session.tabs.get(session.active) if session.context is not None else None
        if page is not None:
            try:
                if page.is_closed():
                    page = None
            except Exception:
                page = None
        if page is None:
            if self.page is not None:
                self._detach()
            return True
        if page is not self.page:
            try:
                self._attach(page)
            except Exception:
                self._detach()
            return True
        try:
            box = self._target_box(page)
            if box != self.box or self._quality()[1] != self.quality:
                self._resize(page, box)
        except Exception:
            self._detach()
        return True


def mjpeg(stream: Stream) -> Iterator[bytes]:
    seq, sent_at, started = -1, 0.0, False
    try:
        while not stream.closed:
            new_seq, data = stream.wait(seq, 1.0)
            if data is None:
                continue
            now = time.time()
            if new_seq == seq and now - sent_at < KEEPALIVE_S:
                continue
            seq, sent_at = new_seq, now
            yield (b"" if started else _PART_HEAD) + data + b"\r\n" + _PART_HEAD
            started = True
    finally:
        stream.close_viewer()
