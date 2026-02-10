# Goose Handoff — hand off a Claude Code session to Slack

Use this skill when the user says "hand this off to Slack", "continue on Slack",
or similar.

## How it works

1. Extract the current session ID by reading the last line of `~/.claude/history.jsonl`:

```bash
SESSION_ID=$(tail -1 ~/.claude/history.jsonl | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['sessionId'])")
```
2. Write a 2-sentence summary of the current session — what you were working on
   and the current state.
3. Run the handoff command:

```bash
{{GOOSE_PATH}} handoff --session-id "$SESSION_ID" --summary "Your summary here"
```

The command resolves the Slack channel from the current working directory
(using the `CHANNEL_DIRS` mapping configured in Goose). If it fails to
resolve, you can pass `--channel <channel-name>` explicitly.

## What happens next

- Goose posts a message to the Slack channel with your summary and session ID.
- When the user replies to that thread in Slack, Goose resumes this exact
  Claude Code session — all prior context is preserved.

## Important

- Always include a meaningful summary so the user knows what was in progress.
- Do NOT continue working after the handoff — the session will be picked up in Slack.
