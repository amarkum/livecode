# Changelog

Notes for the agent harness: what was added, and which files changed. Newest first.

## Chat fixes and asking before a UI check, September 2026

Ported from the same fixes in IntelIDE.

### Agent

- **The agent asks before checking a UI change in the browser.** After a UI edit the reminder has it ask in the questions card, "Want me to verify the UI change in the browser?" (Yes / No). It opens the browser only on Yes, or without asking when the request already wants it seen (`user_requests_ui_check`: "check it in the browser", "take a screenshot", a URL, a design compare). On Yes it looks at a crop of just the changed element, not a full-page screenshot. No, a skip or a typed answer ends the reminders for the turn, and the answer's tool result tells the model to say the change was not checked. This replaces "UI changes are always checked in the browser" below.

### Chat UI

- **A pinned user message is pushed out by the next one** instead of the next one sliding over it. Natural positions are carried row by row, because a pinned row's `offsetTop` is its stuck position.
- **Questions card:** 10px of bottom padding in the scroll area, so a focused "Other..." row keeps its border; questions fade in only when a question set first appears, not on every click.
- **An empty tail below the last message** (10% of the chat height) whenever the chat has a message, never on the welcome screen. It no longer shrinks to nothing in a long chat (`flex: 0 0 auto`).
- **Images sent with a message open in the image viewer** (zoom cursor; works in restored chats; the click does not expand or collapse a long message).
- **Expanded command output has one scroller**, capped at `min(420px, 60vh)`. The outer body scrolled too, around an inner scroller that blocks scroll chaining, so the last lines of long output were out of reach.
- **The empty editor screen** no longer lists Agent / Explorer / Terminal.

### Files changed

| File | Change |
| --- | --- |
| `harness.py` | `_UiVerify` asks first, reads the answer (`observe_question`), `requested` from the request |
| `prompts.py` | `UI_VERIFY_QUESTION`, `UI_VERIFY_ASK_TEMPLATE`, `UI_VERIFY_DECLINED_NOTE`, crop in `UI_VERIFY_TEMPLATE`, system prompt Verifying paragraph |
| `routing.py` | `user_requests_ui_check` |
| `static/js/livecode.js` | Pinned row push-out and `_livecodeUserRowNaturalTops`, questions fade once, sent-image click handler |
| `static/css/livecode.css` | Chat tail, questions padding and fade, thumbnail cursor, single command-output scroller, empty editor steps removed |
| `templates/sections/ide-editor.html` | Empty editor steps list removed |
| `tests/` | Ask, Yes and No turns; `_UiVerify` answers; `user_requests_ui_check`; prompt templates |

## Design compare, browser behaviour and asking, September 2026

### Design compare

- **Element by element is the default.** `compare` measures every element against its place in the design (position, size, padding, colours, font, radius, border, shadow). `elements: false` gives the old pixel comparison.
- **Layout mode ignores sample data.** Other words, numbers, images and extra copies of a repeated item (a 4th card where the design has 3) are counted in the summary and never a finding. `content: "exact"` or the Compare setting in Settings holds them against the page too.
- **A screenshot of one component is found on the page by itself.** A card, form or input box (1x or 2x) is matched by shape, remembered per screenshot, and compared one to one. When the element is fluid, the browser is resized to the width the design was made at. `locate: false` compares the view instead.
- **Whole-page compares are grouped into sections** (a card, a form, a header, a micro-frontend's root; copies of one component as one), so the agent fixes one section at a time.
- **Status labels are matched by their words**, wherever the design shows them, so a shuffled Sold, Paid, Cancelled is compared word to word. Sibling buttons in another order (Cancel and Save swapped) are one finding.
- **New findings:** padding (inferred from children that moved alike), border colour and corner radius of bordered boxes on their own colour, status colours.
- **Compare board restyled:** light theme, page in colour with numbered markers on the pixel board, larger type, count chips coloured by kind.

### Browser and dev server

- **"Go to this site and do this"** opens the page in the Browser tab by itself and carries out the steps. Known site names work without a domain ("go to amazon"), and "check it in the browser" means the user's own app.
- **UI changes are always checked in the browser.** A turn that edited components, pages, styles or templates cannot finish until it looked at the page after its last edit (at most two reminders; switch off in Settings).
- **New `restart_command` tool** stops a background dev server, frees its port (also for servers started elsewhere), starts it again and waits for its URL.
- **`reload {hard: true}`** clears the cache and service workers. Local pages that refuse the connection or show an error page point at the restart.

### Browser tab's live view

- **The frame is never stretched.** The stage takes the live frame's own aspect ratio (the screencast reports the page's real size, which also covers an attached Chrome), and the picture is drawn with `object-fit: contain`, so a frame that arrives before a fit resize finishes is letterboxed, not distorted. Pointer positions are mapped from the picture as drawn.
- **Selecting elements is instant.** The select tool loads a map of the visible elements once (`/livecode/browser/inspect-map`) and hit-tests it locally as the pointer moves, one test per animation frame; the map refreshes as frames change. The server round trip is only a fallback.

### Asking, and doing what was asked

- **The request's own final steps go through.** "Send Gurubani a message saying …" presses Send without a second yes, for send, post, submit, buy, book and confirm. "Don't send it" and "just draft it" are respected. Subagents get the same go-ahead.
- **"Shall I send it?" is sent back once** to do it instead of stopping.
- **The questions card is offered in every mode**, not only Plan mode. A question with no options is an open one (a text box); `allow_multiple` lets several options be picked. The answer continues the same turn.
- **Plan mode teaches all three question kinds** (one choice, several, open answer).

### Fixed

- Every agent turn crashed while building the system prompt, because literal braces were added to an f-string. A test now guards it.

### Files changed

| File | Change |
|---|---|
| `browser_stream.py` | Frame metadata (page size), real page size for attached Chrome |
| `browser.py` | `frame_size` in state, `inspect_map`; compare rewrite: layout mode, component locator, sections, padding, border, status matching, framing, hard reload, error-page hints, final-action authorisation, board style |
| `harness.py` | Site-visit and go-ahead notes, permission gate, UI-verify tracker and gate, questions card offered in agent mode, subagent go-ahead |
| `routing.py` | Site-visit and named-site detection, own-app detection, authorised final actions, permission-question detection, UI-file detection |
| `prompts.py` | Design-loop scope guidance, ask-or-act guidance, verify and restart guidance, Plan-mode question kinds, gate templates |
| `tools.py` | `restart_command` tool, `locate`, `content` and `hard` parameters, questions card in every mode, browser tool text |
| `bg_commands.py` | `restart`, port discovery and freeing |
| `questions.py` | Questions without options (open answers) |
| `subagent.py` | `restart_command` excluded for scoped writers |
| `routes.py` | `/livecode/browser/inspect-map`; settings comment for the new keys |
| `static/js/livecode.js` | Live view keeps the frame's aspect and maps pointers from the drawn picture, local element hit-testing, Browser tab comes forward on navigate, new Settings rows, open questions in the card |
| `static/css/livecode.css` | `object-fit: contain` on the live frame, "Pick any that apply" hint |
| `README.md` | Feature notes and how to run the tests |
| `tests/` | 124 tests: intent, prompts, agent turns with a scripted model, compare in Chromium, restart, front-end helper, fixtures |

### Settings added (`~/.livecode/browser.json`)

`compare_content` (`layout` or `exact`) and `ui_verify` (on by default).
