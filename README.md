<p align="center">
  <img src="static/assets/livecode-logo.png" alt="LiveCode" width="96">
</p>

<h1 align="center">LiveCode</h1>

<p align="center">
  A local, open-source AI coding workspace: an editor, a terminal, a real browser and a coding agent in one window.<br>
  Bring your own API key for Gemini, OpenAI, Anthropic, xAI, Mistral, Groq or OpenRouter.
</p>

---

## Features

- **Coding agent** that reads, searches and edits your project, runs commands, and verifies its changes. It has three modes: **Agent** (makes changes), **Plan** (writes a plan for you to approve) and **Ask** (answers without editing).
- **Any provider, one key each.** Add keys in Settings → Models and choose the default provider. **Auto** picks a model for each step:
  - requests with images go to a model that can read them;
  - code changes can go to a code model;
  - hard and quick tasks get different tiers.

  Text-only models still get images: another model describes them in text first.
- **Built-in browser.** The agent can open pages, click, type, take screenshots and read the console and network. Subagents get their own tabs.
  - You can take control and use the page directly: typing, drag, scroll, paste and copy all work.
  - **Design compare** checks a page against a screenshot, a Figma frame or a link, element by element, and keeps fixing until it matches the accuracy you set. It compares layout (where each element sits, its size, colours and type), not a design's sample names, numbers or pictures; switch it to Exact in Settings when you want the content held against the page too.
  - Saying "go to github.com" or "open localhost:3000" opens the page in the Browser tab by itself, and when a change does not show after a reload the agent hard-reloads, then restarts the dev server.
- **Editor and terminal.** A Monaco editor with Python and TypeScript intelligence, an integrated terminal, and background commands for dev servers and watchers.
- **Review and undo.** Pending changes show as diffs, and you can roll back to a checkpoint.
- **Project context.** The agent reads project rules (`AGENTS.md`, `CLAUDE.md`, `.claude/rules/*.md`), keeps a searchable project memory, and connects to MCP servers.
- **Themes:** Dark, Light, Black and Pink.

## Requirements

- Python **3.10 or newer**
- Google Chrome, or Chromium installed through Playwright (used by the built-in browser)
- An API key from at least one supported provider
- macOS or Linux (the terminal uses a PTY)

## Install

### Homebrew (macOS)

```bash
brew install amarkum/livecode/livecode
livecode                       # then open http://localhost:9000
```

To keep it running in the background and start it at login:

```bash
brew services start livecode
```

### From source

```bash
git clone https://github.com/amarkum/livecode.git
cd livecode
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # optional if Google Chrome is installed
```

> The folder must be named `livecode`, because the package imports itself by that name. `git clone` creates it with that name.

## Run

```bash
python3 server.py
```

Open **http://localhost:9000**, then:

1. Open a project folder, or clone or create a new one.
2. Go to **Settings → Models**, add an API key and make that provider the default.
3. Start chatting in the agent panel.

To use a different port, set `LIVECODE_PORT`:

```bash
LIVECODE_PORT=9001 python3 server.py
```

## Configuration

Most settings are in the app, under **Settings** (General, Agent, Models, Rules, MCP, Indexing). They are saved on your machine:

| File | Holds |
| --- | --- |
| `~/.livecode/llm.json` | API keys (saved with `0600` permissions), the default provider, Auto options |
| `~/.livecode/browser.json` | Browser settings: design accuracy, design gate, subagent tabs, view quality, default viewport |
| `~/.livecode/figma.json` | Figma token (optional) |
| `llm/models.yaml` | Provider catalog: which models each provider offers, and whether each model reads images or is tuned for code. LiveCode reloads this file when it changes. |

Useful environment variables:

| Variable | Purpose |
| --- | --- |
| `LIVECODE_PORT` | Server port (default `9000`) |
| `LIVECODE_LOG_LEVEL` | Log level (default `INFO`) |
| `LIVECODE_BROWSER_EXECUTABLE` | Path to a Chrome or Chromium binary for the built-in browser |
| `LIVECODE_BROWSER_CDP_URL` | Attach to your own Chrome, started with `--remote-debugging-port` |
| `FIGMA_TOKEN` | Figma access token for design compares |

### Adding a model or provider

Edit `llm/models.yaml`. A provider either finds the newest models the key can use (`discover:`) or lists a fixed set (`models:`). Each model can be marked:

- `fast: true`: Auto uses it for quick tasks.
- `vision: false`: it cannot read images.
- `code: true`: Auto prefers it for code changes.

The comments at the top of the file explain each option.

## Optional extras

These are included in `requirements.txt`, and LiveCode works without them:

| Package | Enables |
| --- | --- |
| `flask-sock` | The Python language server in the editor, and a faster input channel for the browser |
| `python-lsp-server` | Python completions, hovers and diagnostics |
| `mcp` | Connecting to MCP servers |
| `jsonschema` | Validation of tool arguments |
| `browser-cookie3` | Importing cookies from your own browser so pages open signed in |

## Project layout

| Path | Role |
| --- | --- |
| `server.py` | Entry point: Flask and Socket.IO app |
| `routes.py`, `lsp_routes.py` | HTTP, Socket.IO and language server routes |
| `harness.py` | Agent turn loop, model routing, tool execution |
| `llm/` | Provider clients, router, settings and model catalog |
| `tools.py`, `search_replace.py` | Repository tools: read, search, edit, commands |
| `browser.py`, `browser_stream.py`, `browser_snapshot.py` | Built-in browser, live view, page snapshots |
| `memory/`, `compaction/` | Project memory and context compaction |
| `static/`, `templates/` | Frontend: editor, agent panel, browser pane |

## Contributing

Issues and pull requests are welcome. Please keep changes focused, match the surrounding style, and describe how you tested them.
