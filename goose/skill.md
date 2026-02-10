# Goose Handoff — hand off a Claude Code session to Slack

Use this skill when the user says "hand this off to Slack", "continue on Slack",
or similar.

## How it works

1. Write a 2-sentence summary of the current session — what you were working on
   and the current state.
2. Scan the conversation for any **open questions** — unresolved decisions, deferred
   choices, blockers, or anything that needs user input. Format them as a numbered list.
3. Run the handoff command:

```bash
# Without open questions:
{{GOOSE_PATH}} handoff --summary "Your summary here"

# With open questions (included in the same message):
{{GOOSE_PATH}} handoff --summary "Your summary here" --questions "❓ Open questions:
1. Question one
2. Question two"
```

The session ID is auto-detected from Claude Code's history.

The command resolves the Slack channel from the current working directory
(using the `CHANNEL_DIRS` mapping configured in Goose). If it fails to
resolve, you can pass `--channel <channel-name>` explicitly.

## What happens next

- Goose posts a single message to the Slack channel with your summary, open questions,
  and session ID.
- When the user replies to that thread in Slack, Goose resumes this exact
  Claude Code session — all prior context is preserved.

## Important

- Always include a meaningful summary so the user knows what was in progress.
- Only include `--questions` if there are genuinely unresolved items. Don't fabricate questions.
- Do NOT continue working after the handoff — the session will be picked up in Slack.
