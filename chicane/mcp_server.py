"""Chicane MCP server — exposes handoff and messaging tools for Claude Code."""

import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .app import resolve_channel_id, resolve_session_id
from .config import Config

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


@mcp.tool()
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

    Args:
        summary: A 2-sentence summary of the current work and state.
        questions: Optional open questions to include in the message.
        session_id: Claude session ID. Auto-detected from history if empty.
        channel: Slack channel name. Auto-resolved from cwd if empty.
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


@mcp.tool()
async def chicane_send_message(
    text: str,
    channel: str = "",
) -> str:
    """Send a message to a Slack channel.

    The channel is resolved from the current working directory via
    CHANNEL_DIRS if not provided. Useful for status updates, notifications,
    or quick pings without a full session handoff.

    Args:
        text: The message content to send.
        channel: Slack channel name. Auto-resolved from cwd if empty.
    """
    try:
        channel_name, channel_id = await _resolve_channel(channel or None)
    except ValueError as exc:
        return f"Error: {exc}"

    client = await _get_client()
    await client.chat_postMessage(channel=channel_id, text=text)

    return f"Message sent to #{channel_name}."


def main() -> None:
    """Entry point for the chicane-mcp console script."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
