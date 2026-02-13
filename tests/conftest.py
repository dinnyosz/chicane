"""Shared fixtures and helpers for handler tests."""

import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from chicane.config import Config
from chicane.claude import ClaudeEvent
from chicane.sessions import SessionStore

# Auto-incrementing counter for unique tool_use IDs in tests.
_tool_id_counter = itertools.count(1)


@pytest.fixture
def config():
    return Config(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
    )


@pytest.fixture
def config_restricted():
    return Config(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        allowed_users=["U_ALLOWED"],
    )


@pytest.fixture
def sessions():
    return SessionStore()


def make_event(type: str, text: str = "", **kwargs) -> ClaudeEvent:
    """Helper to create ClaudeEvent instances."""
    if type == "assistant":
        raw = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
            **kwargs,
        }
    elif type == "result":
        raw = {"type": "result", "result": text, **kwargs}
    elif type == "user":
        raw = {"type": "user", **kwargs}
    else:
        raw = {"type": type, **kwargs}
    return ClaudeEvent(type=type, raw=raw)


def make_tool_event(*tool_blocks, text: str = "", parent_tool_use_id=None) -> ClaudeEvent:
    """Create an assistant event with tool_use content blocks."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(tool_blocks)
    raw = {"type": "assistant", "message": {"content": content}}
    if parent_tool_use_id:
        raw["parent_tool_use_id"] = parent_tool_use_id
    return ClaudeEvent(type="assistant", raw=raw)


def tool_block(name: str, id: str | None = None, **inputs) -> dict:
    return {
        "type": "tool_use",
        "id": id or f"tu_{next(_tool_id_counter)}",
        "name": name,
        "input": inputs,
    }


def mock_client():
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "9999.0"}
    client.conversations_info.return_value = {"channel": {"name": "general"}}
    return client


def make_user_event_with_results(results: list[dict]) -> ClaudeEvent:
    """Create a user event with tool_result blocks."""
    return ClaudeEvent(
        type="user",
        raw={
            "type": "user",
            "message": {"content": results},
        },
    )


def capture_app_handlers(mock_app):
    """Set up a mock AsyncApp to capture registered event handlers.

    Returns the handlers dict. Usage::

        handlers = capture_app_handlers(mock_app)
        register_handlers(mock_app, config, sessions)
        mention_handler = handlers["app_mention"]
    """
    handlers: dict[str, AsyncMock] = {}

    def capture_event(event_type):
        def decorator(fn):
            handlers[event_type] = fn
            return fn
        return decorator

    mock_app.event = capture_event
    return handlers
