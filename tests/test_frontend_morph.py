"""The page patches panels in place instead of rebuilding them: focus, typed text, scroll and node identity
survive a re-render. Runs the real functions from livecode.js in headless Chromium."""
import os
import re

import pytest

JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "livecode.js")
CHROMIUM = os.environ.get("LIVECODE_BROWSER_EXECUTABLE") or "/opt/pw-browsers/chromium"


def _function_source(name: str) -> str:
    src = open(JS, encoding="utf-8").read()
    start = src.index("function " + name + "(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(name)


MORPH = "\n".join(_function_source(n) for n in ("_livecodeMorph", "_livecodeMorphKey", "_livecodeMorphCompatible", "_livecodeMorphChildren", "_livecodeMorphNode"))


@pytest.fixture(scope="module")
def page():
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists(CHROMIUM):
        pytest.skip("no Chromium")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        pg = browser.new_page()
        pg.set_content("<!doctype html><html><body><div id='root'></div></body></html>")
        pg.add_script_tag(content=MORPH)
        yield pg
        browser.close()


def test_a_focused_input_keeps_focus_caret_and_typed_text(page):
    page.evaluate("""() => { const r = document.getElementById('root'); r.innerHTML = '<input id="q" value="old"><p>a</p>'; }""")
    page.focus("#q")
    page.keyboard.press("End")
    page.keyboard.type("hello")
    page.evaluate("""() => _livecodeMorph(document.getElementById('root'), '<input id="q" value="server"><p>b</p><span>new</span>')""")
    state = page.evaluate("""() => ({ focused: document.activeElement && document.activeElement.id, value: document.getElementById('q').value,
                                        p: document.querySelector('p').textContent, span: !!document.querySelector('span') })""")
    assert state == {"focused": "q", "value": "oldhello", "p": "b", "span": True}


def test_nodes_are_reused_not_recreated(page):
    page.evaluate("""() => { const r = document.getElementById('root'); r.innerHTML = '<div class="row"><label>x</label><input type="checkbox"></div>'; window._mark = r.querySelector('.row'); window._mark._tag = 1; }""")
    page.evaluate("""() => _livecodeMorph(document.getElementById('root'), '<div class="row is-on"><label>y</label><input type="checkbox" checked></div>')""")
    state = page.evaluate("""() => ({ same: document.querySelector('.row') === window._mark && window._mark._tag === 1,
                                        cls: document.querySelector('.row').className, checked: document.querySelector('input').checked, label: document.querySelector('label').textContent })""")
    assert state == {"same": True, "cls": "row is-on", "checked": True, "label": "y"}


def test_keyed_lists_keep_their_items_when_one_is_removed_or_moved(page):
    page.evaluate("""() => { const r = document.getElementById('root'); r.innerHTML = '<ul><li data-key="a">A</li><li data-key="b">B</li><li data-key="c">C</li></ul>';
        r.querySelectorAll('li').forEach(li => { li._id = li.dataset.key; }); }""")
    page.evaluate("""() => _livecodeMorph(document.getElementById('root'), '<ul><li data-key="c">C2</li><li data-key="a">A</li></ul>')""")
    state = page.evaluate("""() => Array.from(document.querySelectorAll('li')).map(li => [li.dataset.key, li.textContent, li._id])""")
    assert state == [["c", "C2", "c"], ["a", "A", "a"]], "the surviving nodes are the original ones, in the new order"


def test_attributes_dropped_by_the_new_markup_are_removed(page):
    page.evaluate("""() => { document.getElementById('root').innerHTML = '<button disabled title="t" class="x">go</button>'; }""")
    page.evaluate("""() => _livecodeMorph(document.getElementById('root'), '<button class="x y">go</button>')""")
    state = page.evaluate("""() => { const b = document.querySelector('button'); return { disabled: b.hasAttribute('disabled'), title: b.hasAttribute('title'), cls: b.className }; }""")
    assert state == {"disabled": False, "title": False, "cls": "x y"}


def test_a_textarea_being_typed_in_is_left_alone_but_an_idle_one_follows_the_markup(page):
    page.evaluate("""() => { document.getElementById('root').innerHTML = '<textarea id="t">one</textarea><textarea id="u">one</textarea>'; }""")
    page.focus("#t")
    page.keyboard.press("End")
    page.keyboard.type(" typed")
    page.evaluate("""() => _livecodeMorph(document.getElementById('root'), '<textarea id="t">two</textarea><textarea id="u">two</textarea>')""")
    state = page.evaluate("""() => [document.getElementById('t').value, document.getElementById('u').value]""")
    assert state == ["one typed", "two"]


def test_a_scrolled_container_keeps_its_scroll_position(page):
    page.evaluate("""() => { document.getElementById('root').innerHTML = '<div id="s" style="height:60px;overflow:auto">' + Array.from({length: 40}, (_, i) => '<p>row ' + i + '</p>').join('') + '</div>';
        document.getElementById('s').scrollTop = 300; }""")
    page.evaluate("""() => _livecodeMorph(document.getElementById('root'), '<div id="s" style="height:60px;overflow:auto">' + Array.from({length: 40}, (_, i) => '<p>row ' + i + (i === 3 ? ' changed' : '') + '</p>').join('') + '</div>')""")
    state = page.evaluate("""() => ({ top: document.getElementById('s').scrollTop, changed: document.querySelectorAll('p')[3].textContent })""")
    assert state["top"] == 300 and state["changed"] == "row 3 changed"
