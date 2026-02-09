#!/usr/bin/env python3
"""Slack WebSocket listener using Socket Mode."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MESSAGES_FILE = Path(__file__).parent / "messages.json"
MAX_MESSAGES = 100

app = App(token=os.environ["SLACK_BOT_TOKEN"])


def load_messages() -> list[dict]:
    """Load existing messages from file."""
    if MESSAGES_FILE.exists():
        try:
            return json.loads(MESSAGES_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_message(message: dict) -> None:
    """Append message to file, keeping last MAX_MESSAGES."""
    messages = load_messages()
    messages.append(message)
    messages = messages[-MAX_MESSAGES:]
    MESSAGES_FILE.write_text(json.dumps(messages, indent=2))


@app.event("message")
def handle_message(event: dict, say) -> None:
    """Handle incoming messages."""
    # Skip bot messages and message edits
    if event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
        return

    message = {
        "ts": event.get("ts"),
        "channel": event.get("channel"),
        "user": event.get("user"),
        "text": event.get("text", ""),
        "thread_ts": event.get("thread_ts"),
        "received_at": datetime.now().isoformat(),
    }

    save_message(message)
    logger.info(f"Message from {message['user']} in {message['channel']}")


@app.event("app_mention")
def handle_mention(event: dict, say) -> None:
    """Handle @mentions of the bot."""
    message = {
        "ts": event.get("ts"),
        "channel": event.get("channel"),
        "user": event.get("user"),
        "text": event.get("text", ""),
        "thread_ts": event.get("thread_ts"),
        "is_mention": True,
        "received_at": datetime.now().isoformat(),
    }

    save_message(message)
    logger.info(f"Mention from {message['user']} in {message['channel']}")


def main() -> None:
    """Start the Socket Mode handler."""
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("Starting Slack listener...")
    handler.start()


if __name__ == "__main__":
    main()
