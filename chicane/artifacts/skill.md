# Chicane — Slack bridge for Claude Code

Three tools: `chicane_handoff`, `chicane_send_message`, `chicane_init`.

## `chicane_handoff`

Hand off the current session to Slack. Before calling:

1. Summarize the session in 2 sentences: what was being worked on, and where it stands.
2. Review the conversation for unresolved items — open decisions, blockers, questions needing input. Only include genuine items, never fabricate.

Pass the summary and any open questions to the tool. Session ID and channel are auto-resolved.

## `chicane_send_message`

Send a message to a Slack channel. The channel is auto-resolved from the current working directory via `CHANNEL_DIRS`, so this only works when the cwd maps to a configured channel. Use when the user asks to communicate something to Slack.

## `chicane_init`

Install the Chicane skill and optionally auto-allow tools. This is a two-phase tool:

1. **Call without parameters** — returns the setup options to present to the user.
2. **Present the options** — ask the user to choose scope, allowed tools, and MCP server name.
3. **Call again with all parameters** — performs the installation with the user's choices.
