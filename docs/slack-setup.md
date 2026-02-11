# Slack App Setup

Step-by-step guide to create and configure a Slack app for Chicane.

## 1. Create the app

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App**
3. Choose **From a manifest**
4. Select your workspace
5. Switch to **JSON** tab and paste the contents of [`slack-app-manifest.json`](../slack-app-manifest.json) from this repo
6. Click **Create**

## 2. Get your tokens

### Bot Token

1. In the app settings sidebar, go to **OAuth & Permissions**
2. Click **Install to Workspace** and approve the permissions
3. Copy the **Bot User OAuth Token** (`xoxb-...`)

### App-Level Token

1. In the sidebar, go to **Basic Information**
2. Scroll down to **App-Level Tokens**
3. Click **Generate Token and Scopes**
4. Name it anything (e.g. `chicane-socket`)
5. Add the `connections:write` scope
6. Click **Generate**
7. Copy the token (`xapp-...`)

## 3. Verify Socket Mode is enabled

The manifest enables Socket Mode automatically. To verify:

1. In the sidebar, go to **Socket Mode**
2. Confirm the toggle is **on**

## 4. Add tokens to your environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
```

## 5. Invite the bot to channels

The bot can respond to DMs immediately. To use it in channels:

1. Open the channel in Slack
2. Type `/invite @Chicane` (or whatever you named the bot)

The bot will now respond to @mentions in that channel.
