# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Chicane** is a Slack bot that bridges Claude Code sessions into Slack. It connects Slack (via Socket Mode) to the Claude Agent SDK for persistent, multi-turn coding sessions. The killer feature is **session handoff** — passing coding sessions between desktop Claude Code and Slack (and back).

*"When Claude Code can't go straight, take the chicane."*

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run the bot
chicane run                # start the Slack bot
chicane run --detach       # run as daemon (requires LOG_DIR)
chicane setup              # interactive setup wizard
chicane handoff --summary "..." # hand off a session to Slack

# Tests
pytest                              # all tests
pytest tests/test_config.py         # single file
pytest tests/test_config.py::TestConfig::test_from_env_valid  # single test
pytest -k "test_from_env"           # pattern match
```

## Architecture

```
Slack (Socket Mode) → app.py → handlers.py → sessions.py → claude.py → Claude Agent SDK
```

**Core modules:**

- **`app.py`** — CLI entrypoints (`run`, `setup`, `handoff`), AsyncApp creation, signal handling. Stores config and sessions as private attrs on the AsyncApp (`_chicane_config`, `_chicane_sessions`).
- **`config.py`** — Frozen dataclass loaded from env vars. Config file at `platformdirs.user_config_dir("chicane")/.env`. `resolve_channel_dir()` maps Slack channels ↔ working directories. Also manages handoff alias → session_id persistence.
- **`claude.py`** — `ClaudeSession` wraps `ClaudeSDKClient`. The SDK client persists across turns — created once via `_ensure_connected()`, reused for all messages in a thread. `stream(prompt)` calls `client.query()` then iterates `client.receive_response()`, converting SDK message types (`AssistantMessage`, `ResultMessage`, etc.) to `ClaudeEvent` dicts via `_sdk_message_to_raw()`. System prompt sent on first call only. Supports `interrupt()` for cancellation.
- **`handlers.py`** — Registers `app_mention` and `message` event handlers. See [Handler patterns](#handler-patterns) below.
- **`sessions.py`** — `SessionStore` maps `thread_ts → SessionInfo`. One Claude session per Slack thread. Each `SessionInfo` carries a `ClaudeSession`, an `asyncio.Lock` for concurrency, and metadata. Includes `SLACK_SYSTEM_PROMPT` that constrains Claude's Slack behavior. Auto-cleanup of idle sessions (24h).
- **`mcp_server.py`** — FastMCP server exposing `chicane_handoff`, `chicane_send_message`, and `chicane_init` tools. Uses stdio transport. Entry points: `chicane-mcp` / `chicane-mcp-dev`.
- **`setup.py`** — Interactive 16-step wizard using Rich. Saves config progressively after each step so Ctrl+C preserves completed work.
- **`emoji_map.py`** — Custom alias generator producing verb-adjective-noun names (e.g. `dancing-cosmic-falcon`) with emoji mappings for Slack reactions.

**Bundled assets** (`chicane/artifacts/`):

- `skill.md` — Installed by `chicane_init` to teach Claude Code how to use the MCP tools.
- `slack-app-manifest.json` — Slack app manifest used by `chicane setup`.

## Handler Patterns

`handlers.py` (~1000 lines) is the most complex module. Key subsystems:

**Concurrent message handling:** Per-session `asyncio.Lock` serializes streams within a thread. Multiple messages in the same thread queue behind the lock. Different threads run fully concurrently.

**Notification verbosity:** Three levels (minimal/normal/verbose) control what's shown:
- *Always shown:* text responses, completion summaries, permission denials, errors
- *normal+:* tool activities (`:mag: Reading file.py`), tool errors
- *verbose:* tool results/output, compaction notices

**Tool activity tracking:** `_format_tool_activity()` maps 20+ tool types to emoji + one-liner. All activities are posted as thread replies. Tool outputs >500 chars are uploaded as Slack snippets. Git commits detected via regex get `:package:` reactions. Subagent events prefixed with `:arrow_right_hook:`.

**Message flow:** Slack event → dedup → rate limit check → resolve cwd → `SessionStore.get_or_create()` → acquire lock → stream response → `_format_tool_activity()` / text accumulation → split into Slack-safe chunks (3900 char limit) → completion summary.

**Handoff reconnection:** Thread reply → scan thread history for `_(session: alias)_` → look up alias in handoff map → resume session with that ID. Also supports legacy `_(session_id: uuid)_` format.

## Key Flows

- **Handoff (CLI → Slack):** `chicane handoff` → auto-detect session_id from `~/.claude/history.jsonl` → generate memorable alias → save alias→session_id mapping → resolve channel from cwd via `CHANNEL_DIRS` → post message with embedded `_(session: alias)_`
- **Reconnect (Slack picks up):** Thread reply detected → scan thread history for `_(session: alias)_` → look up session_id from alias map → resume with that session

## Tech Stack

- Python 3.11+, async throughout (asyncio, AsyncApp, AsyncWebClient)
- `claude-agent-sdk` for Claude sessions (`ClaudeSDKClient`)
- `slack-bolt[async]` for Slack Socket Mode
- `pytest` + `pytest-asyncio` for tests (class-based test organization, heavy `AsyncMock` usage)
- `mcp` for the MCP server
- `hatchling` build backend
- `platformdirs` for OS-specific config paths, `rich` for terminal UI

## Test Conventions

- Tests live in `tests/` with `conftest.py` providing shared fixtures: `config`, `sessions`, `make_event`, `make_tool_event`, `tool_block`, `mock_client`
- Autouse `_patch_snippet_io` fixture eliminates real I/O and sleeps globally
- Handler tests are split by concern: `test_handlers_concurrency.py`, `test_handlers_notifications.py`, `test_handlers_tool_activity.py`, `test_handlers_process_message.py`, `test_handlers_routing.py`, `test_handlers_formatting.py`, `test_handlers_files.py`, `test_handlers_utils.py`
- Security tests in `test_handlers_security.py` cover access control, rate limiting, error sanitization, file download sanitization, and handoff session persistence
- All Slack API calls use `AsyncMock`. All async tests use `@pytest.mark.asyncio`.

## Key Conventions

- Config is immutable (frozen dataclass). Never mutate — create new instances.
- Deduplication uses separate sets for mentions vs messages to prevent double-processing.
- The `ClaudeEvent` abstraction layer isolates handlers.py from SDK internals — handlers never see SDK types directly.
- Entry points: `chicane` (CLI), `chicane-mcp` / `chicane-mcp-dev` (MCP server).

## Environment Variables

Config is loaded from `.env` in the platform config directory (`~/Library/Application Support/chicane/` on macOS, `~/.config/chicane/` on Linux). Override with `CHICANE_CONFIG_DIR`.

Required: `SLACK_BOT_TOKEN` (xoxb-...), `SLACK_APP_TOKEN` (xapp-...).

Optional: `BASE_DIRECTORY`, `ALLOWED_USERS`, `CHANNEL_DIRS`, `CLAUDE_MODEL`, `CLAUDE_PERMISSION_MODE` (acceptEdits|dontAsk|bypassPermissions), `CLAUDE_ALLOWED_TOOLS`, `CLAUDE_DISALLOWED_TOOLS`, `CLAUDE_SETTING_SOURCES`, `CLAUDE_MAX_TURNS`, `CLAUDE_MAX_BUDGET_USD`, `RATE_LIMIT`, `VERBOSITY` (minimal|normal|verbose), `LOG_DIR`, `LOG_LEVEL` (DEBUG|INFO|WARNING|ERROR).
