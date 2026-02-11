# Chicane — Implementation Plan

## Goal

Build a Slack bot ("Chicane") that bridges Slack messages to local Claude Code CLI sessions, allowing you to send coding tasks from your phone and have them executed on your machine.

**Stack:** Python + slack-bolt + Claude Code CLI (`claude --print --output-format stream-json`)

---

## Architecture

```
Slack ──Socket Mode──▶ slack-bolt (listener.py)
                            │
                            ▼
                      Bot core (bot.py)
                        │       │
                        ▼       ▼
              Claude CLI    Session/thread
              subprocess     management
                  │
                  ▼
            Stream JSON ──▶ Slack thread replies
```

Key design: **extensible handler pattern** — the bot routes messages through handlers, Claude Code is just one handler. Others can be added (commands, plugins, etc.)

---

## Phase 1: Project Structure & Bot Core ← START HERE

- [x] slack-bolt listener with Socket Mode (exists)
- [ ] Restructure into a proper package:
  ```
  slack-bot/
  ├── chicane/
  │   ├── __init__.py
  │   ├── app.py          # slack-bolt app setup + event routing
  │   ├── claude.py        # Claude CLI subprocess wrapper
  │   ├── handlers.py      # Message handlers (mention, DM)
  │   └── config.py        # Settings from env
  ├── pyproject.toml
  ├── .env.example
  └── plans/
  ```
- [ ] Config module loading from `.env`
- [ ] Basic event routing: @mentions and DMs → handler

---

## Phase 2: Claude Code Integration

- [ ] `claude.py` — spawn `claude --print --output-format stream-json` as subprocess
- [ ] Parse streaming JSON chunks (assistant text, tool use, results)
- [ ] Post initial "thinking..." message in Slack thread, then update it as chunks arrive
- [ ] Handle errors (CLI not found, auth failure, timeout)
- [ ] Session management: map Slack thread_ts → Claude session ID for follow-ups (`--resume`)
- [ ] Working directory: pass `--add-dir` or `cwd` to subprocess

---

## Phase 3: Thread & Session Management

- [ ] Track active sessions: `{thread_ts: {session_id, cwd, started_at}}`
- [ ] Follow-up messages in same thread → `claude --resume <session_id>`
- [ ] Session timeout / cleanup
- [ ] `cwd` command to set working directory per thread/channel

---

## Phase 4: Polish & UX

- [ ] Reaction feedback: 👀 when processing, ✅ when done, ❌ on error
- [ ] Format Claude output for Slack (markdown → mrkdwn, code blocks)
- [ ] Truncate long responses, offer full output as file upload
- [ ] Concurrent request handling (multiple threads at once)

---

## Phase 5: MCP Server Mode

Expose Chicane as an MCP server so any Claude Code session can send Slack messages (e.g. notify you when a long-running task finishes).

- [ ] `chicane/mcp_server.py` — MCP tool definitions (`slack_send_message`, `slack_notify`)
- [ ] Register as an MCP server in `~/.claude/settings.json`
- [ ] Any Claude session can call `mcp__chicane__slack_notify` to ping you

---

## Phase 6: Operational

- [ ] Run as launchd service on macOS
- [ ] Logging with rotation
- [ ] Allowed users/channels config
- [ ] Health check endpoint or status command

---

## Decisions Made

- **Python + slack-bolt** over Node.js (mpociot's project) — stay in Python ecosystem
- **`claude --print --output-format stream-json`** for non-interactive streaming output
- **`--resume`** flag for thread context continuity
- **`--dangerously-skip-permissions`** for headless operation (no TTY for approval prompts)
