"""Tests for goose.handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from goose.config import Config
from goose.handlers import (
    _bot_in_thread,
    _fetch_thread_history,
    _should_ignore,
    _split_message,
    register_handlers,
    SLACK_MAX_LENGTH,
)
from goose.sessions import SessionStore


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


class TestShouldIgnore:
    def test_no_restrictions(self, config):
        event = {"user": "U_ANYONE"}
        assert _should_ignore(event, config) is False

    def test_allowed_user(self, config_restricted):
        event = {"user": "U_ALLOWED"}
        assert _should_ignore(event, config_restricted) is False

    def test_blocked_user(self, config_restricted):
        event = {"user": "U_BLOCKED"}
        assert _should_ignore(event, config_restricted) is True


class TestBotInThread:
    @pytest.mark.asyncio
    async def test_bot_found_in_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "U_BOT"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_HUMAN", "text": "hello"},
                {"user": "U_BOT", "text": "hi there"},
            ]
        }
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is True

    @pytest.mark.asyncio
    async def test_bot_not_in_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "U_BOT"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_HUMAN", "text": "hello"},
                {"user": "U_OTHER", "text": "hi there"},
            ]
        }
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is False

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self):
        client = AsyncMock()
        client.auth_test.side_effect = Exception("API error")
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is False

    @pytest.mark.asyncio
    async def test_empty_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "U_BOT"}
        client.conversations_replies.return_value = {"messages": []}
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is False


class TestSplitMessage:
    def test_short_text_single_chunk(self):
        assert _split_message("hello") == ["hello"]

    def test_exact_limit_single_chunk(self):
        text = "a" * SLACK_MAX_LENGTH
        assert _split_message(text) == [text]

    def test_long_text_splits_into_chunks(self):
        text = "a" * 8000
        chunks = _split_message(text)
        assert len(chunks) > 1
        reassembled = "".join(chunks)
        assert reassembled == text

    def test_splits_on_newlines(self):
        # Build text with lines that total over the limit
        line = "x" * 100 + "\n"
        text = line * 50  # 5050 chars
        chunks = _split_message(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= SLACK_MAX_LENGTH

    def test_no_content_lost(self):
        text = "line1\nline2\n" * 500
        chunks = _split_message(text)
        reassembled = "\n".join(chunks)
        # All original content should be present (minus split newlines)
        assert "line1" in reassembled
        assert "line2" in reassembled

    def test_very_long_single_line(self):
        text = "a" * 10000  # No newlines at all
        chunks = _split_message(text)
        assert len(chunks) > 1
        assert "".join(chunks) == text


class TestFetchThreadHistory:
    @pytest.mark.asyncio
    async def test_formats_conversation_transcript(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "<@UBOT123> hello there"},
                {"user": "UBOT123", "ts": "1001.0", "text": "Hi! How can I help?"},
                {"user": "UHUMAN1", "ts": "1002.0", "text": "follow-up question"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)

        assert result is not None
        assert "[User] hello there" in result
        assert "[Goose] Hi! How can I help?" in result
        # Current message should be excluded
        assert "follow-up question" not in result

    @pytest.mark.asyncio
    async def test_excludes_current_message(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "first"},
                {"user": "UBOT123", "ts": "1001.0", "text": "response"},
                {"user": "UHUMAN1", "ts": "1002.0", "text": "this is the new prompt"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)

        assert "this is the new prompt" not in result
        assert "[User] first" in result
        assert "[Goose] response" in result

    @pytest.mark.asyncio
    async def test_strips_bot_mentions_from_user_messages(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "<@UBOT123> do something"},
                {"user": "UHUMAN1", "ts": "1001.0", "text": "current msg"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1001.0", client)

        assert result is not None
        assert "<@UBOT123>" not in result
        assert "[User] do something" in result

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "only message"},
            ]
        }

        # The only message is the current one — nothing to rebuild
        result = await _fetch_thread_history("C_CHAN", "1000.0", "1000.0", client)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        client = AsyncMock()
        client.auth_test.side_effect = Exception("API error")

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)

        assert result is None

    @pytest.mark.asyncio
    async def test_skips_empty_messages(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "hello"},
                {"user": "UHUMAN1", "ts": "1001.0", "text": ""},
                {"user": "UBOT123", "ts": "1002.0", "text": "response"},
                {"user": "UHUMAN1", "ts": "1003.0", "text": "current"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1003.0", client)

        lines = result.split("\n")
        assert len(lines) == 2
        assert "[User] hello" in lines[0]
        assert "[Goose] response" in lines[1]

    @pytest.mark.asyncio
    async def test_user_message_only_mention_skipped(self):
        """A user message that's only a bot mention with no content should be skipped."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "<@UBOT123>"},
                {"user": "UBOT123", "ts": "1001.0", "text": "response"},
                {"user": "UHUMAN1", "ts": "1002.0", "text": "current"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)

        assert result is not None
        lines = result.split("\n")
        assert len(lines) == 1
        assert "[Goose] response" in lines[0]


class TestThreadMentionRouting:
    """Test that @mentioning the bot in a thread it hasn't seen before works.

    Slack fires both 'message' and 'app_mention' events for @mentions.
    The message handler must NOT consume the event when it can't handle it,
    so the app_mention handler can pick it up.
    """

    @pytest.fixture
    def app(self):
        """Create a mock AsyncApp that captures registered handlers."""
        mock_app = MagicMock()
        self._handlers: dict[str, AsyncMock] = {}

        def capture_event(event_type):
            def decorator(fn):
                self._handlers[event_type] = fn
                return fn
            return decorator

        mock_app.event = capture_event
        return mock_app

    @pytest.fixture
    def sessions(self):
        return SessionStore()

    @pytest.mark.asyncio
    async def test_mention_in_unknown_thread_is_processed(self, app, config, sessions):
        """When a user @mentions the bot in a thread it has no session for,
        the app_mention handler should still process the message."""
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        # Bot has NOT posted in this thread before
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "some unrelated message"},
            ]
        }
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> help me",
        }

        # Simulate Slack firing both events for the same message.
        # message handler fires first — it should NOT block the mention handler.
        await message_handler(event=event, client=client)

        # Now the app_mention handler fires
        with patch("goose.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await mention_handler(event=event, client=client)
            # The mention handler should have called _process_message
            mock_process.assert_called_once()
            # The prompt should have the mention stripped
            assert mock_process.call_args[0][1] == "help me"

    @pytest.mark.asyncio
    async def test_thread_followup_in_known_session_prevents_double_processing(
        self, app, config, sessions
    ):
        """When bot already has a session for a thread, the message handler
        processes the event AND blocks app_mention from double-processing."""
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        # Pre-create a session for this thread
        sessions.get_or_create("1000.0", config)

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> follow up",
        }

        with patch("goose.handlers._process_message", new_callable=AsyncMock) as mock_process:
            # message handler handles it (session exists)
            await message_handler(event=event, client=client)
            assert mock_process.call_count == 1

            # app_mention fires too, but should be deduped
            await mention_handler(event=event, client=client)
            # Should still be 1 — not double-processed
            assert mock_process.call_count == 1
