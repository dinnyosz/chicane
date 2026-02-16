# Chicane — bridge Claude Code sessions to Slack

Use `chicane_handoff` when the user says "hand this off to Slack", "continue
on Slack", or similar. Use `chicane_send_message` for any project-related
communication — progress updates, completed work, errors, questions, or
anything the team should know about.

## Handoff

1. Write a 2-sentence summary of the current session — what you were working on
   and the current state.
2. Scan the conversation for any **open questions** — unresolved decisions, deferred
   choices, blockers, or anything that needs user input. Format them as a numbered list.
3. Call the `chicane_handoff` MCP tool:

```
chicane_handoff(
    summary="Your 2-sentence summary here",
    questions="❓ Open questions:\n1. Question one\n2. Question two"
)
```

- `session_id` and `channel` are auto-resolved. Only pass them explicitly if
  auto-detection fails.
- Only include `questions` if there are genuinely unresolved items. Don't fabricate.

## Send a message

Use `chicane_send_message` for any project-related communication that doesn't
require a full session handoff. Proactively send messages when:
- You complete a significant piece of work (commits, feature done, bug fixed)
- You encounter errors or blockers the team should know about
- Tests pass or fail after changes
- You have questions or need input

```
chicane_send_message(text="Pushed 3 commits to feature/auth — login flow complete, tests passing.")
```

## After a handoff

Once the handoff is posted, tell the user:

> "Handoff posted to Slack. **You need to close this session** so the bot can
> resume it — the session can only be active in one place at a time. Want me
> to quit now?"

If the user confirms, exit the session. If they decline, remind them that
Slack replies will fail until the session is closed.

## Setup (`chicane_init`)

Before calling `chicane_init`, ask the user for:
1. **Scope** — `"global"` (~/.claude/skills/) or `"project"` (project-local)
2. **Allowed tools** — whether to add chicane tools to settings.local.json

Do not assume defaults. The user must confirm both choices.

## Important

- Always include a meaningful summary so the user knows what was in progress.
- Do NOT continue working after a handoff — the session will be picked up in Slack.
