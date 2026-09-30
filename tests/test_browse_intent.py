"""How LiveCode reads "go to this site and do this", and which files count as UI code."""
import pytest

from livecode.routing import browse_request, is_ui_file, user_requests_browser, user_requests_site_visit


@pytest.mark.parametrize("text, site, steps", [
    ("go to github.com and check the latest issues on amarkum/livecode", "github.com", "check the latest issues on amarkum/livecode"),
    ("open localhost:3000/login and sign in with the test user", "localhost:3000/login", "sign in with the test user"),
    ("visit https://example.com/pricing, click on Pro and take a screenshot", "https://example.com/pricing", "click on Pro and take a screenshot"),
    ("navigate to http://127.0.0.1:8000/admin", "http://127.0.0.1:8000/admin", ""),
    ("test the app at localhost:5173 and fill the signup form", "localhost:5173", "fill the signup form"),
    ("look at my-app.vercel.app", "my-app.vercel.app", ""),
    ("open the browser and go to google.com", "google.com", ""),
    ("go to amazon and search for iphone 16", "amazon.com", "search for iphone 16"),
    ("open youtube and play lofi beats", "youtube.com", "play lofi beats"),
    ("head over to hacker news and summarise the top 3 stories", "news.ycombinator.com", "summarise the top 3 stories"),
    ("go to google", "google.com", ""),
    ("open localhost:3000.", "localhost:3000", ""),
])
def test_named_sites_and_their_steps(text, site, steps):
    found = browse_request(text)
    assert found and not found["app"]
    assert found["site"] == site
    assert found["steps"] == steps
    assert user_requests_site_visit(text) == site
    assert user_requests_browser(text)


@pytest.mark.parametrize("text, steps", [
    ("change the button colour and check it in the browser", ""),
    ("open my app", ""),
    ("preview the site after the change", ""),
    ("can you open the dashboard in the browser and see if the chart loads", "see if the chart loads"),
    ("go to the site and click on login", "click on login"),
])
def test_the_users_own_app(text, steps):
    found = browse_request(text)
    assert found and found["app"] and found["site"] == ""
    assert found["steps"] == steps
    assert user_requests_browser(text)


@pytest.mark.parametrize("text", [
    "check out github actions config",  # git, not a website
    "open the file App.tsx",
    "can you open settings.json",
    "fix the bug in utils.py",
    "what does google do",
    "run npm test",
    "load index.html",
    "update package.json version to 2.0",
])
def test_not_a_site_visit(text):
    assert browse_request(text) is None
    assert user_requests_site_visit(text) == ""


@pytest.mark.parametrize("path, ui", [
    ("src/components/Button.tsx", True),
    ("src/pages/index.vue", True),
    ("app/styles/main.scss", True),
    ("templates/base.html", True),
    ("src/components/card/Card.module.css", True),
    ("packages/orders-mfe/src/features/list/List.ts", True),
    ("src/views/Home.js", True),
    ("src/utils/math.ts", False),
    ("server/api.py", False),
    ("src/components/Button.test.tsx", False),
    ("src/components/Button.stories.tsx", False),
    ("vite.config.ts", False),
    ("tailwind.config.js", False),
    ("node_modules/react/index.js", False),
    ("README.md", False),
])
def test_ui_files(path, ui):
    assert is_ui_file(path) is ui


def test_requests_that_already_ask_to_see_the_ui():
    from livecode.routing import user_requests_ui_check
    assert user_requests_ui_check("make the button blue and check it in the browser")
    assert user_requests_ui_check("fix the card padding, then verify how it looks")
    assert user_requests_ui_check("update the header and take a screenshot")
    assert not user_requests_ui_check("make the button blue")
    assert not user_requests_ui_check("rename the Card component to Tile")
