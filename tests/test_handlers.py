"""Tests for chicane.handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.claude import ClaudeEvent
from chicane.handlers import (
    _bot_in_thread,
    _fetch_thread_history,
    _find_session_id_in_thread,
    _HANDOFF_RE,
    _process_message,
    _should_ignore,
    _split_message,
    register_handlers,
    SLACK_MAX_LENGTH,
)
from chicane.sessions import SessionStore


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
        assert "[Chicane] Hi! How can I help?" in result
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
        assert "[Chicane] response" in result

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
        assert "[Chicane] response" in lines[1]

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
        assert "[Chicane] response" in lines[0]


class TestThreadMentionRouting:
    """Test thread reply routing for both @mentions and plain follow-ups.

    Slack fires 'message.channels' for all channel messages (including thread
    replies) and 'app_mention' only for @mentions.  Plain thread follow-ups
    without an @mention rely solely on the message handler.
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
        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
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

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            # message handler handles it (session exists)
            await message_handler(event=event, client=client)
            assert mock_process.call_count == 1

            # app_mention fires too, but should be deduped
            await mention_handler(event=event, client=client)
            # Should still be 1 — not double-processed
            assert mock_process.call_count == 1


    @pytest.mark.asyncio
    async def test_plain_thread_reply_without_mention_is_processed(
        self, app, config, sessions
    ):
        """A plain thread reply (no @mention) should be processed when
        the bot already has a session for the thread.  This requires
        message.channels to be subscribed in the Slack app manifest."""
        register_handlers(app, config, sessions)

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
            "text": "follow up without mentioning the bot",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await message_handler(event=event, client=client)
            mock_process.assert_called_once()
            # Text should be passed as-is (no mention stripping needed)
            assert mock_process.call_args[0][1] == "follow up without mentioning the bot"

    @pytest.mark.asyncio
    async def test_plain_thread_reply_bot_in_history_is_processed(
        self, app, config, sessions
    ):
        """A plain thread reply should be processed when the bot has posted
        in the thread before (e.g. after a bot restart cleared in-memory sessions)."""
        register_handlers(app, config, sessions)

        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        # Bot HAS posted in this thread before
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "start"},
                {"user": "UBOT123", "ts": "1000.5", "text": "I'm here"},
            ]
        }
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }
        # Also mock conversations_history for _find_session_id_in_thread fallback
        client.conversations_history.return_value = {"messages": []}

        event = {
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "continue working on this",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await message_handler(event=event, client=client)
            mock_process.assert_called_once()


    @pytest.mark.asyncio
    async def test_top_level_mention_not_double_processed(self, app, config, sessions):
        """When a user @mentions the bot in a channel (not in a thread),
        both app_mention and message handlers fire. Only one should process it."""
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> do something",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            # Both handlers fire for the same event
            await mention_handler(event=event, client=client)
            await message_handler(event=event, client=client)
            # Should only be processed once
            assert mock_process.call_count == 1


    @pytest.mark.asyncio
    async def test_top_level_mention_message_first_not_double_processed(
        self, app, config, sessions
    ):
        """When message handler fires BEFORE app_mention for a top-level
        @mention, the message should still only be processed once."""
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> do something",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            # message handler fires first this time
            await message_handler(event=event, client=client)
            # app_mention fires second
            await mention_handler(event=event, client=client)
            # Should only be processed once
            assert mock_process.call_count == 1


class TestHandoffRegex:
    """Test the _HANDOFF_RE pattern used to extract session_id from prompts."""

    def test_plain_format(self):
        text = "Working on auth feature (session_id: abc-123-def)"
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "abc-123-def"

    def test_slack_italic_format(self):
        text = "Working on auth feature\n\n_(session_id: abc-123-def)_"
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "abc-123-def"

    def test_trailing_whitespace(self):
        text = "Summary text (session_id: aaa-bbb-ccc)  "
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "aaa-bbb-ccc"

    def test_no_match_when_absent(self):
        text = "Just a normal message with no handoff"
        assert _HANDOFF_RE.search(text) is None

    def test_no_match_mid_text(self):
        """session_id pattern must be at the end of the prompt."""
        text = "(session_id: abc-123) and then more text"
        assert _HANDOFF_RE.search(text) is None

    def test_strips_session_id_from_prompt(self):
        """Verify the extraction + stripping logic that _process_message uses."""
        prompt = "Working on auth feature\n\n_(session_id: abc-123-def)_"
        m = _HANDOFF_RE.search(prompt)
        assert m is not None
        cleaned = prompt[: m.start()].rstrip()
        assert cleaned == "Working on auth feature"
        assert m.group(1) == "abc-123-def"

    def test_full_uuid_format(self):
        text = "Summary _(session_id: a1b2c3d4-e5f6-7890-abcd-ef1234567890)_"
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestFindSessionIdInThread:
    """Test scanning thread messages for a handoff session_id."""

    @pytest.mark.asyncio
    async def test_finds_session_id_in_thread(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "start a task"},
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": "Working on auth\n\n_(session_id: abc-123-def)_",
                },
                {"user": "UHUMAN1", "ts": "1002.0", "text": "continue please"},
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result == "abc-123-def"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_session_id(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "hello"},
                {"user": "UBOT123", "ts": "1001.0", "text": "hi there"},
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_first_session_id_found(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": "First handoff _(session_id: aaa-111)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": "Second handoff _(session_id: bbb-222)_",
                },
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result == "aaa-111"

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        client = AsyncMock()
        client.conversations_replies.side_effect = Exception("API error")

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_empty_thread(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {"messages": []}

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result is None


class TestProcessMessageFormatting:
    """Test that _process_message preserves newlines from streamed text."""

    @pytest.fixture
    def config(self):
        return Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )

    @pytest.fixture
    def sessions(self):
        return SessionStore()

    def _make_event(self, type: str, text: str = "", **kwargs) -> ClaudeEvent:
        """Helper to create ClaudeEvent instances."""
        if type == "assistant":
            raw = {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
                **kwargs,
            }
        elif type == "result":
            raw = {"type": "result", "result": text, **kwargs}
        else:
            raw = {"type": type, **kwargs}
        return ClaudeEvent(type=type, raw=raw)

    @pytest.mark.asyncio
    async def test_streamed_text_with_newlines_not_overwritten_by_result(
        self, config, sessions
    ):
        """The result event often flattens newlines. Streamed text should win."""
        streamed = "First paragraph.\n\nSecond paragraph.\n\n- bullet 1\n- bullet 2"
        flat_result = "First paragraph. Second paragraph. - bullet 1 - bullet 2"

        async def fake_stream(prompt):
            yield self._make_event("system", subtype="init", session_id="sess-1")
            yield self._make_event("assistant", text=streamed)
            yield self._make_event("result", text=flat_result)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "sess-1"

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {
                "ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions)

        # The final chat_update should use the streamed text (with newlines),
        # NOT the flattened result text.
        final_update = client.chat_update.call_args_list[-1]
        assert "\n\n" in final_update.kwargs["text"]
        assert final_update.kwargs["text"] == streamed

    @pytest.mark.asyncio
    async def test_result_text_used_when_no_streamed_content(
        self, config, sessions
    ):
        """When no assistant events arrive, fall back to result text."""
        result_text = "Fallback result text."

        async def fake_stream(prompt):
            yield self._make_event("system", subtype="init", session_id="sess-2")
            yield self._make_event("result", text=result_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "sess-2"

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {
                "ts": "1001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions)

        final_update = client.chat_update.call_args_list[-1]
        assert final_update.kwargs["text"] == result_text
