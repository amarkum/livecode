# Changelog

Notes for the agent harness: what was added, and which files changed. Newest first.

## Harness without patchy edges, and a page that does not flicker, October 2026

### Harness

- **No invented go-aheads.** An answer that ends by asking whether to send, post, submit, book or buy is held: when the request authorised that step the agent is told to do it, when the step is routine (a rename, a dependency, a check-in) it is told to carry on, otherwise it must ask in the questions card (`ASK_IN_CARD_TEMPLATE`, `GO_AHEAD_TEMPLATE`, `_asked_step_authorized`, `_asked_step_is_routine` in `harness.py`).
- **Every finish goes through the gates.** `attempt_completion` and a plain text answer take the same `_closing_gate`: to-do list, permission, UI check, design match, failing tests, then the project's checks. A stationarity hard stop ends the turn with its reason in the answer.
- **Checks run after edits** (`verification.py`, `detect_check_command`): a project with `npm test`, `pytest`, `cargo test`, `go test`, `make test` or `tsc` gets its check run when the agent finishes after editing code without testing. The run appears as a command card (`tool_call_id` `auto-check-N`), a failure's tail goes back to the model and holds the turn, twice at most (`AUTO_CHECK_MAX_RUNS`). Settings > Harness > Run the project's checks (`auto_checks`) turns it off.
- **One reminder a step.** The nudges are one if/elif chain by priority: loop guard, budget, failed edit, failing tests, exploration streak (a late variant when the turn is long), scattered searches, directory drilling, finish after editing, to-do list.
- **Context measured properly.** Images count as 1,400 tokens each, the tool schemas count against the budget, the budget comes from the model's real working window (`working_context_tokens`). Compaction keeps the failure lines and tail of command output (`command_output_digest`) and browser steps keep action, URL, title and verdict. Session compaction measures the projected history and compacts at a threshold above the in-turn one.
- **Prompt built from the tools offered** (`build_system_prompt(..., mode, tool_names)`): sections appear only for tools the turn has; the compact prompt is gone.
- **Routing with fewer false positives** (`routing.py`): closing remarks ("thanks, that fixed it") are chat only, questions are not hard tasks, a version question is not a version bump, "dont check the page" is honoured, URLs quoted as evidence ("the request to localhost:8000 returns 500") do not open the browser, and coding vocabulary ("add a POST endpoint", "wire the submit button") authorises nothing.
- **Retries:** 409 is not retried; 408, 425, 429, 5xx and 529 are, as are stream drops.

### Page

- **In-place patching** (`_livecodeMorph`): the settings page, questions card, chat tabs, project tabs, queue bar and changes list are patched node by node instead of rebuilt, keyed by `data-key` or `id`, so focus, typed text, scroll position, open menus and running spinners survive a re-render. New nodes are adopted, not cloned, so listeners the renderer attached stay.
- **Chat:** a finished streamed answer is rendered off screen and patched onto the streamed nodes; command output is written once per frame and only follows its tail while the reader has not scrolled up; pinned user messages move in the same frame as the scroll with their row gaps cached; collapse measurement reads every row before writing; the transcript snapshot is written when the browser is idle and re-parsed only when there is an error row to strip; the thinking label writes only when it changed.
- **Browser tab:** the last frame stays on screen when the stream closes, the stage follows a pane drag at once and only the viewport resize waits, a changed frame size is re-fitted on the next poll, the agent pill keeps showing for eight seconds between steps and lost its entrance animation and blur.
- **Settings:** the search filters a beat after the last keystroke; a reply to an earlier save no longer flips a switch toggled while it was in flight; editor and terminal settings re-apply only when one of theirs changed.
- **Less work on the main thread:** the file tree redraws only when a silent refresh found a change; the toast has a class and is re-attached if it was detached; the body observer highlights scripts only when a script block was added; the MCP tool chip renders the settings page once; `transition: all` became the properties that change; the plan view's theme watcher starts from the right theme name; `static/assets/monaco-themes.json` ships (empty) so the page no longer 404s on load.
- **Tests:** `tests/test_frontend_morph.py` (Chromium), `test_auto_checks.py`, `test_compaction.py`, `test_retry.py`, `test_classifier.py`, and the turn driver records the page's progress events.

## Merged with main's settings and harness work, October 2026

Main was rewritten to its own history with a large Settings and harness commit. Both sides are merged here:

- **Settings UI:** main's pages are kept (General, Appearance, Agent, Harness, Plan mode, Memory, Browser, with search, backup and reset). This branch's Editor, Browser, Design and Memory tabs were dropped for them; its Terminal tab stays, and main's Design checks gain the Compare and UI check rows. Preferences are stored in `~/.livecode/settings.json`, which Reset all also clears.
- **Memory:** main's Memory page and routes, plus this branch's read and save of MEMORY.md and session logs on `/livecode/memory/file` when a path is given.
- **Chrome:** main's probe, detect and launch, with one launcher underneath that reuses a running Chrome or starts one it closes on disconnect.
- **Harness:** main's retries, streaming, settings-disabled tools and parse-error guard, together with this branch's gates, transcripts and removals.

The section below describes this branch's own Settings tabs; where it differs, this note wins.

## Settings, memory, transcripts and Chrome launch, October 2026

Ported from IntelIDE's change list, saved in `docs/ports/intelide-2026-10.md`. Its Files/S3 and Workbench shell sections have no counterpart in LiveCode and were not ported.

### Settings backed by a file

- **`settings_store.py`** keeps LiveCode's preferences in `~/.livecode/settings.json`. Keys match `^[A-Za-z][A-Za-z0-9_]{0,63}$`, at most 200 of them; values are bool, number or string, strings up to 500 characters. Writes are atomic (temp file and `os.replace`) under a lock.
- **Routes:** `GET/POST /livecode/settings` (`{settings: {key: value | null}}`, null removes; rejected keys come back in `rejected`) and `POST /livecode/settings/reset`, which also clears the browser and design preferences through `reset_browser_settings()` and keeps an attached Chrome.
- **The page** loads settings from the server, mirrors them to localStorage, and moves values from before the server kept them. Panel sizes (`explorerWidth`, `terminalHeight`, `chatPanelWidth`) are settings too.
- **The view** is renamed Settings and regrouped: Editor (font size, tab size, word wrap, line numbers, minimap; applied live), Terminal (font size, cursor, blink, scrollback; applied live), Browser and Design (the rows that were under Agent > Browser), Memory, and Reset all under General. Messages point to "Settings > Browser".

### Rules

- The rules list is deduplicated by file identity (`st_dev`, `st_ino`) instead of the real path, in `rules.py` (`file_identity`) and the rules route, so a symlink or hard link to a rule file shows once.

### Memory tab

- `memory/storage.py` gains `editable_memory_rel` (only `MEMORY.md` and `sessions/*.md`), `list_editable_memory`, `read_editable_memory` and `save_editable_memory`: per-project lock, atomic writes, nothing written for ephemeral workspaces. `memory.save_memory_file` also refreshes the search index.
- **Routes:** `GET/POST /livecode/memory` (listing) and `POST /livecode/memory/file` (reads without `content`, saves with it). 400 for other paths or non-text content, 404 for a missing log, 500 with a readable message on OS errors.

### Chats saved as the rendered transcript

- Each session keeps `transcript.html`, the HTML the page rendered (40 MB cap, written atomically). Loading a chat returns `transcript_html` and `message_count`.
- **Route:** `POST /livecode/session/transcript` with `project_path`, `session_id` and `html`. 400 when the chat is not in that workspace; over the cap it answers `success: false, reason: "too_large"`, not an error. The page sends the transcript after each turn, only for the open workspace.
- **Removed:** `save_diff_record`, `load_diff_records`, `save_tool_artifact`, `load_tool_artifacts`, `format_messages_for_display` and their helpers in `session.py`, their callers in `harness.py`, `display_subagent_result` in `subagent.py`, and the page's JSON-history renderer. A chat saved before transcripts shows a note that its history is still there for the agent.
- **Chat:** the questions card shows one question at a time (Next, then Continue); a finished command's long output fades before the card folds; the view stays pinned to the bottom while a reply streams and unpins only when the reader scrolls up.

### Browser

- **`launch_chrome_and_attach(port=9222)`** finds Chrome on mac, Windows or Linux (or `LIVECODE_CHROME_EXECUTABLE`), starts it with `--remote-debugging-port` and a profile at `~/.livecode/chrome-debug-profile` (log in `chrome-debug.log`), waits up to 12 s for the port, then attaches. Ports 1024 to 65535; refused when `LIVECODE_BROWSER_CDP_URL` is set. Turning the connection off closes a Chrome LiveCode started.
- `connection_status()` reports `managed_launch`, `pid`, `profile_dir` and `port`. `_browser_connection_update()` handles launch, attach and disconnect for the HTTP route and a new Socket.IO pair, `livecode_browser_connection` and `livecode_browser_connection_result`.
- **UI:** a Launch Chrome button in Settings > Browser; in the Browser tab an engine chip (Chromium, Chrome, Chrome launched), a resolution chip that always shows the page size, and a theme-aware stage background.

### Prompts and tools

- The hardcoded Python-quality guidance (pre-commit, ruff, black, SonarQube) is gone from `prompts.py`.
- `git commit` runs as written: the `Co-authored-by: LiveCode` trailer is no longer injected (`tools.py`), and the tool description and prompt no longer mention it.
- **`UI_VERIFY_CROP_GUIDE`:** when checking UI, crop the shared parent container so neighbouring elements show together, and back the crop with a measurement.

### UI polish

- The mode chip is tinted per mode (Agent neutral, Plan amber, Ask blue with a chat-bubble icon).
- Dividers have wider grab areas.
- The terminal has inner padding and an ANSI palette per light or dark theme.
- The design accuracy slider has a clear accent in the dark themes.

### Legacy storage removed

- `project_store.py` no longer migrates old slug-keyed project folders or adopts old multi-folder workspace storage. `project_dir`, `existing_project_dir` and `workspace_state_path` use the hashed key only. **Impact:** project folders that were never migrated, and chats that exist only as message JSON, are not migrated or shown any more. Their files stay on disk, and the agent still reads a chat's history.

### Files changed

| File | Change |
| --- | --- |
| `settings_store.py` | New: the settings file |
| `routes.py` | Settings, memory and transcript routes; connection update and Socket.IO pair; rules by file identity |
| `rules.py` | `file_identity` |
| `memory/storage.py`, `memory/__init__.py` | Editable memory files |
| `session.py`, `subagent.py`, `harness.py` | Transcript save/load; display records removed |
| `browser.py`, `browser_snapshot.py` | Chrome launch, managed status, `reset_browser_settings`, "Settings > Browser" |
| `prompts.py`, `tools.py` | Quality guidance and trailer removed; crop guide |
| `project_store.py` | Legacy migration removed |
| `static/js/livecode.js`, `static/css/livecode.css` | Settings tabs and server-backed settings, Memory tab, transcripts, questions one at a time, pinned scroll, command fade, Chrome launch, chips, mode chip, panel sizes, dividers, terminal |
| `README.md` | Settings tabs, new files and variables |
| `tests/` | `test_settings_store.py`, `test_memory_editor.py`, `test_rules.py`, `test_transcript.py`, `test_chrome_launch.py`, `test_project_store.py`, prompt tests; `app_client` fixture |

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
