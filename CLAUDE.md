# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Chicane** is a Slack bot that bridges Claude Code sessions into team chat. It connects Slack (via Socket Mode) to the Claude Code CLI via async subprocess streaming. The killer feature is **session handoff** — passing coding sessions between desktop Claude Code and Slack (and back).

*"When Claude Code can't go straight, take the chicane."*

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run the bot
chicane run                # start the Slack bot
chicane run --detach       # run as daemon (requires LOG_DIR)
chicane setup              # interactive 10-step config wizard
chicane handoff --summary "..." # hand off a session to Slack
chicane install-skill      # install Claude Code handoff skill

# Tests
pytest                              # all tests
pytest tests/test_config.py         # single file
pytest tests/test_config.py::TestConfig::test_from_env_valid  # single test
pytest -k "test_from_env"           # pattern match
```

## Architecture

```
Slack (Socket Mode) → app.py → handlers.py → sessions.py → claude.py → Claude Code CLI subprocess
```

**Seven modules, each with a single responsibility:**

- **`app.py`** — CLI entrypoints (`run`, `setup`, `handoff`, `install-skill`), AsyncApp creation, signal handling, logging setup. Exports `resolve_session_id()` and `resolve_channel_id()` as shared helpers. Stores config and sessions as private attrs on the AsyncApp (`_chicane_config`, `_chicane_sessions`).
- **`config.py`** — Frozen dataclass loaded from env vars. Config file lives at `platformdirs.user_config_dir("chicane")/.env`. Validates tokens, permission modes, log levels. `resolve_channel_dir()` maps Slack channels to working directories.
- **`claude.py`** — `ClaudeSession` wraps a `claude` CLI subprocess with `--print --output-format stream-json --verbose`. `ClaudeEvent` dataclass parses streaming JSON events, extracting text (skipping tool_use blocks), session_id, errors, and cost. System prompt only sent on first call, not on resumes.
- **`handlers.py`** — Registers `app_mention` and `message` event handlers. Core flow in `_process_message()`: dedup → resolve cwd → get/create session → stream response → split into Slack-safe chunks (3900 char limit). Handles reconnection by scanning thread history for handoff session IDs. Adds emoji reactions for visual feedback (eyes → checkmark/x).
- **`mcp_server.py`** — FastMCP server exposing `chicane_handoff` and `chicane_send_message` tools for Claude Code. Uses stdio transport. Entry point: `chicane-mcp = "chicane.mcp_server:main"`.
- **`sessions.py`** — `SessionStore` maps `thread_ts → SessionInfo`. One Claude session per Slack thread. Includes `SLACK_SYSTEM_PROMPT` that tells Claude it's operating via Slack with formatting constraints. Auto-cleanup of idle sessions (24h default).
- **`setup.py`** — 10-step interactive wizard using Rich. Saves config progressively after each step.

**Key flows:**
- **Message flow:** Slack event → handler dedup → `SessionStore.get_or_create()` → `ClaudeSession.stream()` → parse `ClaudeEvent` → update Slack message periodically → split long responses
- **Handoff (CLI → Slack):** `chicane handoff` → auto-detect session_id from `~/.claude/history.jsonl` → resolve channel from cwd via `CHANNEL_DIRS` → post message with embedded `(session_id: uuid)`
- **Reconnect (Slack picks up):** Thread reply detected → scan thread history for `(session_id: uuid)` regex → resume session with `--resume` flag

## Tech Stack

- Python 3.11+, async throughout (asyncio, AsyncApp, AsyncWebClient)
- `slack-bolt[async]` for Slack Socket Mode
- `pytest` + `pytest-asyncio` for tests
- `mcp` (Model Context Protocol) for the MCP server (`chicane-mcp` entry point)
- `hatchling` build backend, entry points: `chicane = "chicane.app:main"`, `chicane-mcp = "chicane.mcp_server:main"`
- `platformdirs` for OS-specific config paths
- `rich` for terminal UI in setup wizard

## Key Conventions

- All Slack API calls are async. Tests use `AsyncMock` and `@pytest.mark.asyncio`.
- Config is immutable (frozen dataclass). Never mutate — create new instances.
- The `SLACK_SYSTEM_PROMPT` in `sessions.py` constrains Claude's behavior for Slack (no markdown headers/tables, 4000 char limit, auto-approve edits).
- Deduplication uses separate sets for mentions vs messages to prevent double-processing from overlapping Slack events.
- The CLI command is `chicane` (pyproject.toml entry point).

## Environment Variables

Required: `SLACK_BOT_TOKEN` (xoxb-...), `SLACK_APP_TOKEN` (xapp-...).
Optional: `BASE_DIRECTORY`, `ALLOWED_USERS`, `CHANNEL_DIRS`, `CLAUDE_MODEL`, `CLAUDE_PERMISSION_MODE` (acceptEdits|dontAsk|bypassPermissions), `CLAUDE_ALLOWED_TOOLS`, `LOG_DIR`, `LOG_LEVEL` (DEBUG|INFO|WARNING|ERROR).
