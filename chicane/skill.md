# Chicane — bridge Claude Code sessions to Slack

## Handoff

When the user wants to hand off to Slack:

1. Write a 2-sentence summary — what you were working on and the current state.
2. Scan the conversation for **open questions** — unresolved decisions, deferred
   choices, blockers, or anything that needs user input. Format as a numbered list.
3. Call `chicane_handoff` with the summary and questions.
4. Only include `questions` if there are genuinely unresolved items. Don't fabricate.

## Send a message

Call `chicane_send_message` when the user asks you to send something to Slack.

## Setup (`chicane_init`)

Before calling `chicane_init`, ask the user for:
1. **Scope** — `"global"` (~/.claude/skills/) or `"project"` (project-local)
2. **Allowed tools** — whether to add chicane tools to settings.local.json

Do not assume defaults. The user must confirm both choices.
