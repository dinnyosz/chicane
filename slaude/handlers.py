"""Slack event handlers — routes messages to Claude and streams responses back."""

import logging
import re

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from .claude import ClaudeSession
from .config import Config
from .sessions import SessionStore

logger = logging.getLogger(__name__)

# How often to update the Slack message while streaming (seconds)
STREAM_UPDATE_INTERVAL = 1.5

# Max message length for Slack
SLACK_MAX_LENGTH = 3900


def register_handlers(app: AsyncApp, config: Config, sessions: SessionStore) -> None:
    """Register all Slack event handlers on the app."""
    bot_user_id: str | None = None
    processed_ts: set[str] = set()

    def _mark_processed(ts: str) -> bool:
        """Mark a message as processed. Returns False if already seen."""
        if ts in processed_ts:
            return False
        processed_ts.add(ts)
        # Keep the set bounded
        if len(processed_ts) > 500:
            processed_ts.clear()
        return True

    @app.event("app_mention")
    async def handle_mention(event: dict, client: AsyncWebClient) -> None:
        """Handle @mentions of the bot in channels (from real users)."""
        if not _mark_processed(event["ts"]):
            return
        if _should_ignore(event, config):
            return

        text = re.sub(r"<@[A-Z0-9]+>\s*", "", event.get("text", "")).strip()
        if not text:
            return

        await _process_message(event, text, client, config, sessions)

    @app.event("message")
    async def handle_message(event: dict, client: AsyncWebClient) -> None:
        """Handle DMs and channel messages that mention the bot."""
        nonlocal bot_user_id

        # Skip message subtypes (edits, deletes, etc.)
        if event.get("subtype"):
            return
        if not _mark_processed(event["ts"]):
            return

        channel_type = event.get("channel_type", "")
        text = event.get("text", "").strip()
        if not text:
            return

        # DMs: process everything
        if channel_type == "im":
            if _should_ignore(event, config):
                return
            await _process_message(event, text, client, config, sessions)
            return

        # Channel messages: only process if bot is mentioned
        # (This catches bot-sent mentions that don't trigger app_mention)
        if not bot_user_id:
            auth = await client.auth_test()
            bot_user_id = auth["user_id"]

        if f"<@{bot_user_id}>" in text:
            if _should_ignore(event, config):
                return
            clean_text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
            if clean_text:
                await _process_message(event, clean_text, client, config, sessions)


def _should_ignore(event: dict, config: Config) -> bool:
    """Check if this event should be ignored."""
    user = event.get("user", "")
    if config.allowed_users and user not in config.allowed_users:
        logger.info(f"Ignoring message from non-allowed user: {user}")
        return True
    return False


async def _process_message(
    event: dict,
    prompt: str,
    client: AsyncWebClient,
    config: Config,
    sessions: SessionStore,
) -> None:
    """Process a message by sending it to Claude and streaming the response."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])
    user = event.get("user", "unknown")

    logger.info(f"Processing message from {user} in {channel}: {prompt[:80]}")

    # Add eyes reaction to show we're working on it
    try:
        await client.reactions_add(channel=channel, name="eyes", timestamp=event["ts"])
    except Exception:
        pass  # Reaction may already exist or we lack permission

    # Get or create a Claude session for this thread
    session = sessions.get_or_create(
        thread_ts=thread_ts,
        config=config,
    )

    # Post initial "thinking" message
    result = await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Working on it...",
    )
    message_ts = result["ts"]

    # Stream Claude's response
    full_text = ""
    last_update_len = 0

    try:
        async for event_data in session.stream(prompt):
            if event_data.type == "assistant":
                chunk = event_data.text
                if chunk:
                    full_text += chunk

                    # Update the Slack message periodically (not on every chunk)
                    if len(full_text) - last_update_len > 100:
                        display = _truncate(full_text)
                        await client.chat_update(
                            channel=channel,
                            ts=message_ts,
                            text=display,
                        )
                        last_update_len = len(full_text)

            elif event_data.type == "result":
                full_text = event_data.text or full_text

        # Final update with complete response
        if full_text:
            display = _truncate(full_text)
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                text=display,
            )
        else:
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                text=":warning: Claude returned an empty response.",
            )

        # Swap eyes for checkmark
        try:
            await client.reactions_remove(
                channel=channel, name="eyes", timestamp=event["ts"]
            )
            await client.reactions_add(
                channel=channel, name="white_check_mark", timestamp=event["ts"]
            )
        except Exception:
            pass

    except Exception as exc:
        logger.exception(f"Error processing message: {exc}")
        await client.chat_update(
            channel=channel,
            ts=message_ts,
            text=f":x: Error: {exc}",
        )
        try:
            await client.reactions_remove(
                channel=channel, name="eyes", timestamp=event["ts"]
            )
            await client.reactions_add(
                channel=channel, name="x", timestamp=event["ts"]
            )
        except Exception:
            pass


def _truncate(text: str) -> str:
    """Truncate text to fit Slack's message limit."""
    if len(text) <= SLACK_MAX_LENGTH:
        return text
    return text[:SLACK_MAX_LENGTH] + "\n\n... _(truncated)_"
