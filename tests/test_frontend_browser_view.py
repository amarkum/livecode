"""The Browser tab's live view: the stage keeps the frame's aspect ratio, and the select tool picks the
innermost element under the pointer from the element map. The front end's own functions, run in node."""
import json
import os
import re
import shutil
import subprocess

import pytest

JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "livecode.js")
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def _fn(name):
    with open(JS) as handle:
        source = handle.read()
    return re.search(r"function %s\(.*?\n\}" % re.escape(name), source, re.S).group(0)


def _run(js):
    done = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip())


@pytest.mark.parametrize("pane, page, natural, expect", [
    # A pane wider than the page: the page at 1:1, never upscaled.
    ((1600, 1000), (1280, 800), (1280, 800), (1280, 800)),
    # A narrow pane: scaled down, aspect kept.
    ((640, 1000), (1280, 800), (1280, 800), (640, 400)),
    # The viewport the UI heard of is stale (a fit resize under way): the frame's own shape wins.
    ((1200, 900), (1280, 800), (900, 1100), (736, 900)),
    # A phone frame in a wide pane.
    ((1400, 900), (390, 844), (780, 1688), (390, 844)),
    # No frame yet: the viewport's shape.
    ((500, 500), (1280, 800), (0, 0), (500, 313)),
])
def test_stage_keeps_the_frames_aspect_ratio(pane, page, natural, expect):
    out = _run(_fn("_livecodeBrowserStageSize") + f"\nconsole.log(JSON.stringify(_livecodeBrowserStageSize({pane[0]}, {pane[1]}, {page[0]}, {page[1]}, {natural[0]}, {natural[1]})));")
    assert (out["width"], out["height"]) == expect
    if natural[0]:
        assert abs(out["width"] / out["height"] - natural[0] / natural[1]) < 0.01
    assert out["scale"] <= 1


def test_pick_the_innermost_element_under_the_pointer():
    elements = [
        [0, 0, 1100, 700, "body", ""],
        [32, 96, 1036, 70, "form.filters", ""],
        [48, 112, 260, 38, "input.search", "Search orders"],
        [32, 190, 330, 76, "article.card", ""],
        [52, 204, 48, 48, "img.avatar", ""],
    ]
    js = _fn("_livecodeBrowserPickElement") + f"\nconst E = {json.dumps(elements)};\nconsole.log(JSON.stringify([_livecodeBrowserPickElement(E, 60, 120), _livecodeBrowserPickElement(E, 60, 220), _livecodeBrowserPickElement(E, 200, 250), _livecodeBrowserPickElement(E, 900, 600), _livecodeBrowserPickElement(E, -5, 10)]));"
    picked = _run(js)
    assert [p and p["label"] for p in picked] == ["input.search", "img.avatar", "article.card", "body", None]
    assert picked[0]["text"] == "Search orders" and picked[0]["rect"] == {"x": 48, "y": 112, "width": 260, "height": 38}
