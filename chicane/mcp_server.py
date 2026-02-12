"""Chicane MCP server — exposes handoff and messaging tools for Claude Code."""

import json
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .app import resolve_channel_id, resolve_session_id
from .config import Config

# Shared annotation presets.
_SLACK_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_LOCAL_IDEMPOTENT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# MCP uses stdio for JSON-RPC — all logging must go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
logger = logging.getLogger(__name__)

mcp = FastMCP("chicane")

# Lazy-loaded singletons (avoid import-time side effects).
_config: Config | None = None
_client = None  # AsyncWebClient


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


async def _get_client():
    global _client
    if _client is None:
        from slack_sdk.web.async_client import AsyncWebClient

        _client = AsyncWebClient(token=_get_config().slack_bot_token)
    return _client


async def _resolve_channel(channel: str | None, cwd: Path | None = None) -> tuple[str, str]:
    """Resolve channel name and ID.

    Returns (channel_name, channel_id).
    Raises ValueError on failure.
    """
    config = _get_config()
    channel_name = channel
    if not channel_name:
        working_dir = cwd or Path.cwd()
        channel_name = config.resolve_dir_channel(working_dir.resolve())
        if not channel_name:
            raise ValueError(
                f"Could not resolve a Slack channel for {working_dir}. "
                "Pass channel explicitly, or configure CHANNEL_DIRS."
            )

    client = await _get_client()
    channel_id = await resolve_channel_id(client, channel_name)
    if not channel_id:
        raise ValueError(f"Channel #{channel_name} not found in Slack.")

    return channel_name, channel_id


@mcp.tool(annotations=_SLACK_ANNOTATIONS)
async def chicane_handoff(
    summary: str,
    questions: str = "",
    session_id: str = "",
    channel: str = "",
) -> str:
    """Hand off the current Claude Code session to Slack.

    Posts a handoff message so the session can be resumed by the Chicane
    Slack bot. The session ID is auto-detected from Claude Code history
    if not provided. The channel is resolved from the current working
    directory via CHANNEL_DIRS if not provided.
    """
    try:
        sid = resolve_session_id(session_id or None)
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        channel_name, channel_id = await _resolve_channel(channel or None)
    except ValueError as exc:
        return f"Error: {exc}"

    parts = [summary]
    if questions:
        parts.append(f"\n{questions}")
    parts.append(f"\n_(session_id: {sid})_")
    text = "\n".join(parts)

    client = await _get_client()
    await client.chat_postMessage(channel=channel_id, text=text)

    return (
        f"Handoff posted to #{channel_name}. "
        "The user should close this session so the bot can resume it — "
        "the session can only be active in one place at a time."
    )


@mcp.tool(annotations=_SLACK_ANNOTATIONS)
async def chicane_send_message(
    text: str,
    channel: str = "",
) -> str:
    """Send a message to a Slack channel.

    The channel is resolved from the current working directory via
    CHANNEL_DIRS if not provided. Useful for status updates, notifications,
    or quick pings without a full session handoff.
    """
    try:
        channel_name, channel_id = await _resolve_channel(channel or None)
    except ValueError as exc:
        return f"Error: {exc}"

    client = await _get_client()
    await client.chat_postMessage(channel=channel_id, text=text)

    return f"Message sent to #{channel_name}."


_SKILL_TEMPLATE: str | None = None


def _get_skill_content() -> str:
    """Read and cache the bundled skill.md template."""
    global _SKILL_TEMPLATE
    if _SKILL_TEMPLATE is None:
        template_path = Path(__file__).resolve().parent / "skill.md"
        _SKILL_TEMPLATE = template_path.read_text()
    return _SKILL_TEMPLATE


_TOOL_NAMES = ["chicane_handoff", "chicane_send_message", "chicane_init"]


def _add_allowed_tools(settings_path: Path, mcp_server_name: str) -> list[str]:
    """Add chicane MCP tools to a settings.local.json file.

    Returns the list of tool entries that were added (skips duplicates).
    """
    if settings_path.exists():
        data = json.loads(settings_path.read_text())
    else:
        data = {}

    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    added = []
    for tool in _TOOL_NAMES:
        entry = f"mcp__{mcp_server_name}__{tool}"
        if entry not in allow:
            allow.append(entry)
            added.append(entry)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return added


@mcp.tool(annotations=_LOCAL_IDEMPOTENT_ANNOTATIONS)
async def chicane_init(
    scope: str = "global",
    project_root: str = "",
    add_allowed_tools: bool = False,
    mcp_server_name: str = "chicane-dev",
) -> str:
    """Set up Chicane for Claude Code.

    Installs the handoff skill (SKILL.md) and optionally adds chicane
    tools to the allowed tools list in settings.local.json so they
    run without permission prompts.
    """
    content = _get_skill_content()

    if scope == "project":
        if not project_root:
            return "Error: project_root is required when scope is 'project'."
        base = Path(project_root)
    elif scope == "global":
        base = Path.home()
    else:
        return f"Error: scope must be 'global' or 'project', got '{scope}'."

    # Install skill
    skill_dir = base / ".claude" / "skills" / "chicane"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_target = skill_dir / "SKILL.md"
    skill_target.write_text(content)

    parts = [f"Installed chicane skill to {skill_target}"]

    # Optionally add allowed tools
    if add_allowed_tools:
        settings_path = base / ".claude" / "settings.local.json"
        added = _add_allowed_tools(settings_path, mcp_server_name)
        if added:
            parts.append(f"Added {len(added)} tool(s) to {settings_path}: {', '.join(added)}")
            parts.append("NOTE: Restart Claude Code for allowed tools changes to take effect.")
        else:
            parts.append(f"All tools already in {settings_path}")

    return "\n".join(parts)


def main() -> None:
    """Entry point for the chicane-mcp console script."""
    try:
        _get_config()
    except ValueError as exc:
        logger.error("Chicane MCP server cannot start: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
