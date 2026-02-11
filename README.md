# Chicane

*When Claude Code can't go straight, take the chicane.*

A Slack bridge for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — send coding tasks from Slack, get results back.

## Why "Chicane"?

In motorsport, a **chicane** is a sequence of tight turns that forces drivers off the straight line — an engineered detour that still gets you to the finish. When you can't go straight to your Claude Code session (you're away from your desk, on your phone, or want your team involved), **Chicane** is the engineered path through Slack. The session continues, the context is preserved, you just took a different route.

## How it works

```
Slack (Socket Mode) → Chicane → Claude Code CLI (subprocess)
                    ← streaming JSON ←
```

Each Slack thread gets its own Claude Code session. The session persists for the life of the thread, so follow-up messages have full context. If the bot restarts, it reconnects to existing threads by scanning thread history or resuming via session IDs.

## Prerequisites

- **Python 3.11+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** installed and authenticated (`claude` must be on your PATH)
- A **Slack workspace** where you can create apps

## Installation

```bash
pip install chicane
```

Or install from source:

```bash
git clone https://github.com/dinnyosz/chicane.git
cd chicane
pip install -e .
```

## Setup

The fastest way to get started is the guided setup wizard:

```bash
chicane setup
```

This walks you through creating a Slack app, getting your tokens, and writing the `.env` file — all in one step. Run it again any time to update your config.

Alternatively, you can set things up manually:

### 1. Create a Slack app

Follow the [Slack app setup guide](docs/slack-setup.md) to create and install a Slack app. You'll need the **Bot Token** (`xoxb-...`) and **App-Level Token** (`xapp-...`).

### 2. Configure environment variables

Create a `.env` file in the directory where you'll run Chicane:

```bash
cat <<'EOF' > .env
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
EOF
```

Or, if you cloned the repo: `cp .env.example .env`

See [Configuration reference](#configuration-reference) for all options.

### 3. Start the bot

```bash
chicane run
```

## Usage

- **@mention in a channel** &mdash; `@Chicane refactor the auth module` starts a new session in a thread
- **DM the bot** &mdash; send a message directly, no @mention needed
- **Thread follow-ups** &mdash; reply in the thread to continue the conversation with full context
- **Reconnect after restart** &mdash; Chicane automatically picks up existing threads when it restarts

## Handoff

Chicane supports handing off sessions between Claude Code on your desktop and Slack.

**Desktop to Slack:** From a Claude Code session on your terminal, hand the session off to Slack so you (or your team) can continue it from any device.

```bash
chicane handoff --summary "Refactoring the auth module, tests passing"
```

The session ID is auto-detected from Claude Code's history. The channel is resolved from your current working directory via the `CHANNEL_DIRS` mapping. When someone replies to the handoff message in Slack, Chicane resumes that exact Claude Code session with all prior context.

**Install the handoff skill** to let Claude Code itself trigger handoffs:

```bash
chicane install-skill
```

This installs a Claude Code skill at `~/.claude/skills/chicane-handoff/SKILL.md`. After installing, you can tell Claude Code "hand this off to Slack" and it will do it automatically.

## CLI reference

```
chicane <command> [options]
```

| Command | Description |
|---|---|
| `setup` | Guided setup wizard (creates/updates `.env`) |
| `run` | Start the Slack bot |
| `handoff` | Post a handoff message to Slack |
| `install-skill` | Install the chicane-handoff skill for Claude Code |
| `help` | Show help message |

### `chicane setup`

Interactive setup wizard. Walks through creating a Slack app, collecting tokens, and writing `.env`. If a `.env` already exists, current values are shown as defaults — just press Enter to keep them.

### `chicane run`

Starts the Slack bot. Connects via Socket Mode and listens for messages.

### `chicane handoff`

| Flag | Required | Description |
|---|---|---|
| `--summary` | Yes | Summary text for the handoff message |
| `--session-id` | No | Claude session ID (auto-detected from `~/.claude/history.jsonl` if omitted) |
| `--channel` | No | Slack channel name (auto-resolved from cwd via `CHANNEL_DIRS` if omitted) |
| `--cwd` | No | Working directory to resolve channel from (defaults to `$PWD`) |

### `chicane install-skill`

Installs the handoff skill to `~/.claude/skills/chicane-handoff/SKILL.md`. No flags.

## Configuration reference

All configuration is via environment variables (loaded from `.env`).

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | &mdash; | Slack Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | &mdash; | Slack App-Level Token (`xapp-...`) with `connections:write` scope |
| `BASE_DIRECTORY` | No | &mdash; | Default working directory for Claude sessions |
| `ALLOWED_USERS` | No | (all users) | Comma-separated Slack user IDs that can use the bot |
| `CHANNEL_DIRS` | No | &mdash; | Map channels to directories. Simple: `magaldi,frontend` (name = dir under `BASE_DIRECTORY`). Custom: `web=frontend,infra=/opt/infrastructure` |
| `CLAUDE_MODEL` | No | CLI default | Claude model override (e.g. `sonnet`, `opus`) |
| `CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Permission mode for Claude CLI (`acceptEdits`, `dontAsk`, `bypassPermissions`) |
| `CLAUDE_ALLOWED_TOOLS` | No | &mdash; | Comma-separated tool rules (e.g. `Bash(npm run *),Read`) |
| `LOG_DIR` | No | &mdash; | Directory for log files (required for `--detach` mode) |
| `LOG_LEVEL` | No | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## License

[Apache 2.0](LICENSE)
