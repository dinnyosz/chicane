# Slaude Setup Guide

**Slaude** (Slack + Claude) is your always-available coding assistant. She bridges Slack and your local Claude Code instances, so you can send her tasks from your phone and she'll handle them on your machine.

Based on **mpociot/claude-code-slack-bot**.

---

## What Slaude Can Do

| Feature | Support |
|---------|---------|
| Send prompts from Slack | ✅ |
| Receive responses in Slack | ✅ |
| She asks clarifying questions | ✅ |
| Thread context maintained | ✅ |
| Streaming responses | ✅ |
| Working directory selection | ✅ |
| Tool permission prompts in Slack | ❌ (she auto-handles these) |
| Visible terminal session | ❌ (she works headless) |

---

## Prerequisites

- [ ] Node.js 18+
- [ ] Claude Code CLI installed and authenticated (`claude` command works)
- [ ] Slack workspace with admin permissions
- [ ] Git

---

## Step 1: Clone and Install

```bash
git clone https://github.com/mpociot/claude-code-slack-bot.git
cd claude-code-slack-bot
npm install
```

---

## Step 2: Create Slack App

### Option A: Using App Manifest (Recommended)

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From an app manifest**
3. Select your workspace
4. Paste contents of `slack-app-manifest.json` from the repo
5. Click **Create**

### Option B: Manual Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch**
3. Name it "Slaude", select workspace

#### Configure OAuth Scopes

Go to **OAuth & Permissions** → **Bot Token Scopes**, add:

- `app_mentions:read`
- `channels:history`
- `channels:read`
- `chat:write`
- `chat:write.public`
- `im:history`
- `im:read`
- `im:write`
- `users:read`
- `reactions:read`
- `reactions:write`

#### Enable Socket Mode

1. Go to **Settings** → **Socket Mode**
2. Toggle **Enable Socket Mode** ON
3. Create App-Level Token with `connections:write` scope
4. Copy the token (starts with `xapp-`)

#### Configure Event Subscriptions

Go to **Event Subscriptions**:

1. Toggle **Enable Events** ON
2. Under **Subscribe to bot events**, add:
   - `app_mention`
   - `message.im`

#### Enable DMs

Go to **App Home**:

1. Enable **Messages Tab**
2. Check "Allow users to send Slash commands and messages from the messages tab"

---

## Step 3: Get Credentials

Collect these three values:

| Credential | Location | Format |
|------------|----------|--------|
| Bot Token | OAuth & Permissions → Bot User OAuth Token | `xoxb-...` |
| App Token | Basic Information → App-Level Tokens | `xapp-...` |
| Signing Secret | Basic Information → Signing Secret | hex string |

---

## Step 4: Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required - Slack credentials
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
SLACK_SIGNING_SECRET=your-signing-secret

# Optional - Base directory for relative paths
BASE_DIRECTORY=/home/youruser/projects/

# Optional - Claude Code auth (only if not using Claude subscription)
# ANTHROPIC_API_KEY=sk-ant-...
# CLAUDE_CODE_USE_BEDROCK=1
# CLAUDE_CODE_USE_VERTEX=1

# Optional - Debug mode
# DEBUG=true
```

---

## Step 5: Install App to Workspace

1. Go to **OAuth & Permissions**
2. Click **Install to Workspace**
3. Authorize the app

---

## Step 6: Run the Bot

### Development (with hot reload)

```bash
npm run dev
```

### Production

```bash
npm run build
npm run prod
```

You should see output indicating successful connection.

---

## Usage

### Set Working Directory

Before sending tasks, set the working directory:

```
cwd backend
```

With `BASE_DIRECTORY=/home/user/projects/`, this resolves to `/home/user/projects/backend`

Or use absolute paths:

```
cwd /home/user/projects/my-app
```

Check current directory:

```
cwd
```

### Directory Scoping

| Context | Behavior |
|---------|----------|
| Direct Messages | Directory persists for entire conversation |
| Channels | Default directory per channel (prompted when bot joins) |
| Threads | Can override channel default within a thread |

### Send Tasks

**In channels** (mention her):

```
@Slaude fix the login bug in auth.ts
```

**In DMs** (no mention needed):

```
add unit tests for the user service
```

### Conversation Flow

Slaude's clarifying questions appear in the thread:

```
You:    @Slaude add caching to the API
Slaude: What caching strategy do you prefer - Redis, in-memory, or file-based?
You:    Redis
Slaude: Got it, implementing Redis caching...
```

---

## Optional: MCP Servers

If you want additional MCP tools available in bot sessions, create `mcp-servers.json`:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token"
      }
    }
  }
}
```

Commands:
- `mcp` - List configured servers
- `mcp reload` - Reload configuration

**Note:** Your existing Claude Code MCP config (`~/.claude/settings.json`) should work automatically.

---

## Running as a Service

### Linux (systemd)

Create `/etc/systemd/system/slaude.service`:

```ini
[Unit]
Description=Slaude - Claude Code Slack Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/claude-code-slack-bot
ExecStart=/usr/bin/npm run prod
Restart=on-failure
RestartSec=10
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable slaude
sudo systemctl start slaude
```

### macOS (launchd)

Create `~/Library/LaunchAgents/com.slaude.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.slaude</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/npm</string>
        <string>run</string>
        <string>prod</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/claude-code-slack-bot</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/slaude.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/slaude.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.slaude.plist
```

---

## Project Directory Configuration

Example setup with multiple projects:

```
/home/user/projects/
├── backend/          # cwd backend
├── frontend/         # cwd frontend
├── api-gateway/      # cwd api-gateway
├── shared-libs/      # cwd shared-libs
└── infrastructure/   # cwd infrastructure
```

Set `BASE_DIRECTORY=/home/user/projects/` in `.env`, then:

```
cwd backend
@Slaude add rate limiting to the auth endpoints
```

---

## Troubleshooting

### Slaude not responding

1. Check she's running (`npm run dev`)
2. Verify all env vars are set
3. Ensure she's invited to the channel
4. Check Slack app permissions

### Connection errors

1. Verify Socket Mode is enabled
2. Check App Token hasn't expired
3. Confirm `connections:write` scope on App Token

### Authentication errors

1. Verify Claude Code CLI works locally (`claude --version`)
2. If using API key, check it's valid
3. Check tokens haven't been revoked

### Working directory issues

1. Ensure `BASE_DIRECTORY` path exists
2. Check permissions on target directories
3. Use absolute paths to debug

### Debug mode

Set `DEBUG=true` in `.env` for verbose logging.

---

## Quick Reference

| Action | Command |
|--------|---------|
| Set directory (relative) | `cwd myproject` |
| Set directory (absolute) | `cwd /full/path/to/project` |
| Check current directory | `cwd` or `get directory` |
| List MCP servers | `mcp` |
| Reload MCP config | `mcp reload` |
| Ask Slaude (channel) | `@Slaude your request` |
| Ask Slaude (DM) | Just type your request |

---

## Links

- **Repo:** https://github.com/mpociot/claude-code-slack-bot
- **Slack API:** https://api.slack.com/apps
- **Claude Code Docs:** https://docs.anthropic.com/claude-code
