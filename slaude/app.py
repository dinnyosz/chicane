"""Slaude application — Slack bot powered by Claude Code."""

import asyncio
import logging
import os
import ssl

import certifi

# Fix macOS Python SSL cert issue
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import Config
from .handlers import register_handlers
from .sessions import SessionStore

logger = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> AsyncApp:
    """Create and configure the Slack Bolt app."""
    if config is None:
        config = Config.from_env()

    app = AsyncApp(token=config.slack_bot_token)
    sessions = SessionStore()

    register_handlers(app, config, sessions)

    # Store references on the app for access elsewhere
    app._slaude_config = config  # type: ignore[attr-defined]
    app._slaude_sessions = sessions  # type: ignore[attr-defined]

    return app


async def start(config: Config | None = None) -> None:
    """Start the bot."""
    if config is None:
        config = Config.from_env()

    logging.basicConfig(
        level=logging.DEBUG if config.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    app = create_app(config)

    sessions = app._slaude_sessions  # type: ignore[attr-defined]
    cleanup_task = asyncio.create_task(_periodic_cleanup(sessions))

    handler = AsyncSocketModeHandler(app, config.slack_app_token)
    logger.info("Starting Slaude...")
    try:
        await handler.start_async()
    finally:
        cleanup_task.cancel()


async def _periodic_cleanup(sessions: SessionStore, interval_hours: int = 1) -> None:
    """Run session cleanup every interval_hours."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        removed = sessions.cleanup(max_age_hours=24)
        if removed:
            logger.info(f"Periodic cleanup removed {removed} stale sessions")
