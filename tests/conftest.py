"""Shared fixtures and helpers for handler tests."""

import asyncio
import itertools
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.claude import ClaudeEvent
from chicane.sessions import SessionStore
from chicane.slack_queue import SlackMessageQueue

# Auto-incrementing counter for unique tool_use IDs in tests.
_tool_id_counter = itertools.count(1)


@pytest.fixture
def config():
    return Config(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        allowed_users=["UHUMAN1", "U_HUMAN", "U_ALICE", "U_BOB", "U_ALLOWED"],
        rate_limit=10000,  # High limit so tests don't trigger rate limiting
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


@pytest.fixture
def queue():
    """A SlackMessageQueue with zero throttle for tests."""
    return SlackMessageQueue(min_interval=0.0)


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
    # conversations_replies / conversations_history used by reconnect scanning.
    client.conversations_replies.return_value = {"messages": []}
    client.conversations_history.return_value = {"messages": []}
    client.auth_test.return_value = {"user_id": "UBOT123"}
    # Default reactions_get for emoji sync on reconnect.
    client.reactions_get.return_value = {"message": {}}
    # Support snippet uploads via files_upload_v2.
    client.files_upload_v2.return_value = {"ok": True}
    # Keep legacy mocks for any remaining references.
    client.files_getUploadURLExternal.return_value = {
        "upload_url": "https://files.slack.com/upload/v1/fake",
        "file_id": "F_FAKE",
    }
    client.files_completeUploadExternal.return_value = {"ok": True}
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


def mock_session_info(mock_session, thread_ts="1000.0"):
    """Wrap a mock ClaudeSession in a mock SessionInfo with a real asyncio.Lock.

    Since ``SessionStore.get_or_create`` now returns a ``SessionInfo`` object
    (with ``.session`` and ``.lock``), all handler tests that patch
    ``get_or_create`` need to return this wrapper.

    Also configures the mock session with sensible defaults for the interrupt
    mechanism so tests don't fail on the ``was_interrupted`` / ``is_streaming``
    checks introduced by the concurrency control.
    """
    mock_session.was_interrupted = False
    mock_session.is_streaming = False
    mock_session.interrupt_source = None
    mock_session.interrupt = AsyncMock()
    mock_session.disconnect = AsyncMock()
    mock_session._ask_user_callback = None
    info = MagicMock()
    info.session = mock_session
    info.lock = asyncio.Lock()
    info.thread_ts = thread_ts
    info.thread_reactions = set()
    info.session_alias = None
    info.total_requests = 0
    info.total_turns = 0
    info.total_cost_usd = 0.0
    info.total_commits = 0
    info.empty_continue_count = 0
    info.todo_snapshot = None
    info.pending_messages = deque()
    info.pending_question = None
    return info


def _make_fake_http_session():
    """Build a mock aiohttp.ClientSession that accepts PUT for snippet uploads."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()

    class _FakeSession:
        async def put(self, url, **kw):
            return fake_resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    return _FakeSession()


@pytest.fixture(autouse=True)
def _patch_snippet_io():
    """Eliminate real I/O and sleeps from _send_snippet and slack_queue in all tests."""

    async def _instant_sleep(_delay):
        return

    with (
        patch("chicane.handlers.aiohttp.ClientSession", return_value=_make_fake_http_session()),
        patch("chicane.handlers.asyncio.sleep", side_effect=_instant_sleep),
        patch("chicane.slack_queue.asyncio.sleep", side_effect=_instant_sleep),
    ):
        yield


@pytest.fixture(autouse=True)
def _isolate_handoff_map(tmp_path):
    """Redirect handoff_sessions.json to a temp dir so tests never pollute the real file."""
    with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "handoff_sessions.json"):
        yield


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
