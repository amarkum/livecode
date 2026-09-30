"""The live view's data: the element map the select tool hit-tests, and the frame size the stream reports."""
import os
import tempfile

import pytest

from livecode import browser, browser_stream


@pytest.fixture()
def act():
    state = tempfile.mkdtemp(prefix="lc-state-")

    def _act(**args):
        out = browser.agent_action(state, args, session_id="view", resolve_path=lambda p: p if os.path.isfile(p) else None)
        assert not out.get("error"), out.get("error")
        return out
    _act.state = state
    return _act


def test_inspect_map_lists_visible_elements_in_viewport_coordinates(act, site, browser_ready):
    act(action="resize", width=1100, height=700)
    act(action="navigate", url=site + "/app.html")
    data = browser.inspect_map(act.state)
    assert data["vw"] == 1100 and data["vh"] == 700 and data["scroll"] == [0, 0]
    labels = [e[4] for e in data["elements"]]
    assert "form.filters" in labels and "input.search" in labels and "article.card" in labels
    cards = [e for e in data["elements"] if e[4] == "article.card"]
    assert len(cards) == 4 and all(e[2] > 200 and 60 <= e[3] <= 120 for e in cards)
    body_index, card_index = labels.index("header"), labels.index("article.card")
    assert body_index < card_index, "parents and earlier content come first"
    search = next(e for e in data["elements"] if e[4] == "input.search")
    assert search[5] == "Search orders"


def test_the_stream_records_the_frames_page_size():
    class Session:
        shared = False
        view_sharp = True
        view_box = None

    stream = browser_stream.Stream(Session())

    class Cdp:
        def send(self, *a, **k):
            pass

    cdp = Cdp()
    stream.cdp = cdp
    stream._on_frame(cdp, {"sessionId": 1, "data": "AAAA", "metadata": {"deviceWidth": 1102.4, "deviceHeight": 720, "pageScaleFactor": 1}})
    assert stream.page_size == (1102, 720)
    stream._on_frame(cdp, {"sessionId": 2, "data": "AAAB", "metadata": {}})
    assert stream.page_size == (1102, 720), "a frame without metadata keeps the last size"


def test_state_reports_the_frame_size_only_for_the_streamed_page():
    class Page:
        pass

    class Stream:
        closed = False
        page = Page()
        page_size = (900, 1100)

    class Session:
        stream = Stream()

    assert browser._frame_size(Session(), Session.stream.page) == {"width": 900, "height": 1100}
    assert browser._frame_size(Session(), Page()) is None
    Session.stream.closed = True
    assert browser._frame_size(Session(), Session.stream.page) is None
