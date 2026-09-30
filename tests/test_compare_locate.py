"""Comparing a running app with screenshots of its design, in a real Chromium: one component at a time
(a card, an input box, a form), found on the page by itself at whatever width the browser has, and the
fix-and-move-on loop: compare, fix, compare again until it matches, then the next element."""
import os
import tempfile

import pytest

from livecode import browser


@pytest.fixture()
def act():
    state = tempfile.mkdtemp(prefix="lc-state-")

    def _act(**args):
        out = browser.agent_action(state, args, session_id="test", resolve_path=lambda p: p if os.path.isfile(p) else None)
        assert not out.get("error"), out.get("error")
        return out
    return _act


@pytest.fixture(scope="module")
def variants(site_dir, site):
    """The app with the design's fixes applied step by step: first the card, then everything."""
    with open(os.path.join(site_dir, "app.html")) as handle:
        app = handle.read()
    card = app.replace("border-radius: 4px; padding: 14px;", "border-radius: 12px; padding: 20px;")
    assert card != app
    everything = (card.replace("border: 1px solid #6b7280; border-radius: 2px;", "border: 1px solid #d1d5db; border-radius: 8px;")
                      .replace("background: #16a34a;", "background: #2563eb;").replace("font-size: 28px;", "font-size: 22px;")
                      .replace(".pill.cancelled { background: #f3f4f6; color: #374151; }", ".pill.cancelled { background: #fee2e2; color: #991b1b; }")
                      .replace('<button type="button" class="primary">Save</button><button type="button" class="secondary">Cancel</button>',
                               '<button type="button" class="secondary">Cancel</button><button type="button" class="primary">Save</button>'))
    assert ".pill.cancelled { background: #fee2e2; color: #991b1b; }" in everything and 'secondary">Cancel</button><button' in everything
    for name, text in (("app_card_fixed.html", card), ("app_fixed.html", everything)):
        with open(os.path.join(site_dir, name), "w") as handle:
            handle.write(text)
    return {"app": site + "/app.html", "card_fixed": site + "/app_card_fixed.html", "fixed": site + "/app_fixed.html"}


def _findings(result, prefix):
    return [f for e in result.get("elements") or [] if e["element"].startswith(prefix) for f in e["findings"]]


@pytest.mark.parametrize("width", [900, 1100, 1280, 1440])
@pytest.mark.parametrize("shot", ["card", "card@2x"])
def test_a_card_screenshot_is_found_at_any_page_width(act, variants, design_shots, width, shot):
    act(action="resize", width=width, height=700)
    act(action="navigate", url=variants["app"])
    result = act(action="compare", reference=design_shots[shot])
    located = result["located"]
    assert result["mode"] == "element" and "article.card" in located["selector"]
    assert located["scale"] == (2.0 if shot.endswith("@2x") else 1.0)
    if width != 1100:
        assert abs(located["resized"]["to"] - 1100) <= 4, "resized to the width the design was made at"
    assert any("padding about 20px in the design (14px here)" in f for f in _findings(result, "article.card"))
    assert any("corner radius about 1" in f for f in _findings(result, "article.card"))
    # The app's own orders are sample data, not findings.
    assert not any("other words" in f or "not in the design" in f for e in result["elements"] for f in e["findings"])


@pytest.mark.parametrize("shot", ["input", "input@2x"])
def test_an_input_box_screenshot(act, variants, design_shots, shot):
    act(action="resize", width=1280, height=700)
    act(action="navigate", url=variants["app"])
    result = act(action="compare", reference=design_shots[shot])
    assert "input.search" in result["located"]["selector"]
    found = _findings(result, "input.search")
    assert any("border colour #6b7280; the design's looks like #D1D5DB" in f for f in found)
    assert any("corner radius about 7px" in f or "corner radius about 8px" in f for f in found)


def test_fix_one_element_then_move_to_the_next(act, variants, design_shots):
    act(action="resize", width=1100, height=700)
    act(action="navigate", url=variants["app"])
    first = act(action="compare", reference=design_shots["card"])
    assert first["verdict"] == "different" and _findings(first, "article.card")
    # The card's CSS fixed: the same screenshot compares with the same card, and it matches now.
    act(action="navigate", url=variants["card_fixed"])
    again = act(action="compare", reference=design_shots["card"])
    assert again["located"]["selector"] == first["located"]["selector"] and again["located"].get("remembered")
    assert again["verdict"] != "different", again.get("summary")
    assert "fixed" in again.get("progress", "")
    # On to the next element: the input box still differs until its CSS is fixed too.
    next_one = act(action="compare", reference=design_shots["input"])
    assert _findings(next_one, "input.search")
    act(action="navigate", url=variants["fixed"])
    assert act(action="compare", reference=design_shots["input"])["verdict"] != "different"
    # And the whole page at the end.
    page = act(action="compare", reference=design_shots["page"], full_page=True)
    assert page["verdict"] != "different", page.get("summary")


def test_a_whole_page_screenshot_is_split_into_sections(act, variants, design_shots):
    act(action="resize", width=1100, height=700)
    act(action="navigate", url=variants["app"])
    result = act(action="compare", reference=design_shots["page"], full_page=True)
    assert "located" not in result or not result["located"], "a whole-page design is not looked for as a part"
    sections = result["sections"]
    assert sections[0]["element"].startswith("article.card ×") and sections[0]["instances"] >= 3
    assert {s["selector"] for s in sections[1:]} >= {"header", "#orders-mfe > form.filters"}
    assert "work on one section at a time" in result["summary"]
    # The app shows 4 orders where the design shows 3: the 4th is data, not a difference.
    assert not any(e["status"] == "missing" for e in result["elements"])


def test_exact_mode_holds_the_sample_data_against_it(act, variants, design_shots):
    act(action="resize", width=1100, height=700)
    act(action="navigate", url=variants["app"])
    result = act(action="compare", reference=design_shots["card"], content="exact")
    assert any("other words" in f for e in result["elements"] for f in e["findings"])


def test_locate_false_compares_the_view(act, variants, design_shots):
    act(action="resize", width=1100, height=700)
    act(action="navigate", url=variants["app"])
    result = act(action="compare", reference=design_shots["card"], locate=False)
    assert result["mode"] == "view" and not result.get("located")


def test_shuffled_statuses_are_matched_by_their_words(act, variants, design_shots):
    """The design shows Paid, Cancelled, Sold on its rows; the app its own orders: Sold, Paid, Cancelled, Due.
    Each status is compared with the design's status of the same words, wherever it is."""
    act(action="resize", width=1100, height=700)
    act(action="navigate", url=variants["app"])
    result = act(action="compare", reference=design_shots["page"], full_page=True)
    pills = next(g for g in result["labels"] if g["label"] == "span.pill")
    assert set(pills["checked"]) == {"Sold", "Paid", "Cancelled"} and pills["not_in_design"] == ["Due"]
    cancelled = _findings(result, "span.pill.cancelled")
    assert any("has background #f3f4f6 here; the design\u2019s \u201cCancelled\u201d has #FEE2E2" in f for f in cancelled)
    assert not _findings(result, "span.pill.sold") and not _findings(result, "span.pill.paid"), "the same status in another row is no difference"
    # Where the design has its "Cancelled": the second card (Wade Warren's), left of the app's (the third).
    element = next(e for e in result["elements"] if e["element"].startswith("span.pill.cancelled"))
    assert element["design_box"]["x"] < element["box"]["x"] - 100


def test_buttons_in_another_order_are_one_finding(act, variants, design_shots):
    act(action="resize", width=1100, height=700)
    act(action="navigate", url=variants["app"])
    result = act(action="compare", reference=design_shots["page"], full_page=True)
    save = _findings(result, "button.primary")
    assert any("the design has them in the order \u201cCancel\u201d, \u201cSave\u201d" in f for f in save)
    assert any("\u201cSave\u201d has background #16a34a here; the design\u2019s \u201cSave\u201d has #2563EB" in f for f in save)
    assert not any("sits" in f for f in save + _findings(result, "button.secondary")), "the swap is not reported as moves"
