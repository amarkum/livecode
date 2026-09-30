"""The Browser tab comes forward when the agent opens a page: the front end's check, run in node."""
import os
import re
import shutil
import subprocess

import pytest

JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "livecode.js")


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("args, shows", [
    ({"action": "navigate", "url": "https://github.com"}, True),
    ({"action": "switch_tab", "tab_id": "2"}, True),
    ({"action": "new_tab", "url": "https://figma.com"}, True),
    ({"action": "new_tab", "url": "https://figma.com", "background": True}, False),
    ({"action": "batch", "actions": [{"action": "navigate", "url": "x"}, {"action": "click", "text": "Go"}]}, True),
    ({"action": "screenshot"}, False),
    ({"action": "compare"}, False),
    ({"action": "click", "text": "Save"}, False),
])
def test_which_browser_actions_bring_the_browser_tab_forward(args, shows):
    with open(JS) as handle:
        source = handle.read()
    fn = re.search(r"function _livecodeBrowserActionShowsPage\(args\) \{.*?\n\}", source, re.S).group(0)
    import json
    done = subprocess.run(["node", "-e", fn + f"\nconsole.log(JSON.stringify(_livecodeBrowserActionShowsPage({json.dumps(args)})));"],
                          capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == json.dumps(shows)
