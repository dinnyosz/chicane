# Goose

Slack bot powered by Claude Code. Send coding tasks from Slack, get results back.

Goose connects Slack to the [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI via Socket Mode. When you @mention the bot or DM it, Goose spawns a Claude Code subprocess, streams the response back to Slack, and keeps the session alive for follow-up messages in the same thread.

## How it works

```
Slack (Socket Mode) → Goose → Claude Code CLI (subprocess)
                    ← streaming JSON ←
```

Each Slack thread gets its own Claude Code session. The session persists for the life of the thread, so follow-up messages have full context. If the bot restarts, it reconnects to existing threads by scanning thread history or resuming via session IDs.

## Prerequisites

- **Python 3.11+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** installed and authenticated (`claude` must be on your PATH)
- A **Slack workspace** where you can create apps

## Installation

```bash
pip install goose-code
```

Or install from source:

```bash
git clone https://github.com/dinnyosz/goose-code.git
cd goose-code
pip install -e .
```

## Setup

The fastest way to get started is the guided setup wizard:

```bash
goose init
```

This walks you through creating a Slack app, getting your tokens, and writing the `.env` file — all in one step.

Alternatively, you can set things up manually:

### 1. Create a Slack app

Follow the [Slack app setup guide](docs/slack-setup.md) to create and install a Slack app. You'll need the **Bot Token** (`xoxb-...`) and **App-Level Token** (`xapp-...`).

### 2. Configure environment variables

Create a `.env` file in the directory where you'll run Goose:

```bash
cat <<'EOF' > .env
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
EOF
```

Or, if you cloned the repo: `cp .env.example .env`

See [Configuration reference](#configuration-reference) for all options.

### 3. Start the bot

Run `goose run` from the directory containing your `.env` file:

```bash
goose run
```

## Usage

- **@mention in a channel** &mdash; `@Goose refactor the auth module` starts a new session in a thread
- **DM the bot** &mdash; send a message directly, no @mention needed
- **Thread follow-ups** &mdash; reply in the thread to continue the conversation with full context
- **Reconnect after restart** &mdash; Goose automatically picks up existing threads when it restarts

## Handoff

Goose supports handing off sessions between Claude Code on your desktop and Slack.

**Desktop to Slack:** From a Claude Code session on your terminal, hand the session off to Slack so you (or your team) can continue it from any device.

```bash
goose handoff --summary "Refactoring the auth module, tests passing"
```

The session ID is auto-detected from Claude Code's history. The channel is resolved from your current working directory via the `CHANNEL_DIRS` mapping. When someone replies to the handoff message in Slack, Goose resumes that exact Claude Code session with all prior context.

**Install the handoff skill** to let Claude Code itself trigger handoffs:

```bash
goose install-skill
```

This installs a Claude Code skill at `~/.claude/skills/goose-handoff/SKILL.md`. After installing, you can tell Claude Code "hand this off to Slack" and it will do it automatically.

## CLI reference

```
goose <command> [options]
```

| Command | Description |
|---|---|
| `init` | Guided setup wizard (creates `.env`) |
| `run` | Start the Slack bot |
| `handoff` | Post a handoff message to Slack |
| `install-skill` | Install the goose-handoff skill for Claude Code |
| `help` | Show help message |

### `goose init`

Interactive setup wizard. Walks through creating a Slack app, collecting tokens, and writing `.env`.

| Flag | Description |
|---|---|
| `--force` | Overwrite existing `.env` without asking |

### `goose run`

Starts the Slack bot. Connects via Socket Mode and listens for messages.

### `goose handoff`

| Flag | Required | Description |
|---|---|---|
| `--summary` | Yes | Summary text for the handoff message |
| `--session-id` | No | Claude session ID (auto-detected from `~/.claude/history.jsonl` if omitted) |
| `--channel` | No | Slack channel name (auto-resolved from cwd via `CHANNEL_DIRS` if omitted) |
| `--cwd` | No | Working directory to resolve channel from (defaults to `$PWD`) |

### `goose install-skill`

Installs the handoff skill to `~/.claude/skills/goose-handoff/SKILL.md`. No flags.

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
| `CLAUDE_PERMISSION_MODE` | No | `default` | Permission mode for Claude CLI (`default`, `bypassPermissions`, etc.) |
| `DEBUG` | No | `false` | Enable debug logging (`true`, `1`, or `yes`) |

## License

[Apache 2.0](LICENSE)
