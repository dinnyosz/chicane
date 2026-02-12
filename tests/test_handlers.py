"""Tests for chicane.handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.claude import ClaudeEvent
from chicane.handlers import (
    _bot_in_thread,
    _download_files,
    _fetch_thread_history,
    _find_session_id_in_thread,
    _HANDOFF_RE,
    _process_message,
    _resolve_channel_cwd,
    _should_ignore,
    _split_message,
    MAX_FILE_SIZE,
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


class TestHandlerRoutingEdgeCases:
    """Test handler routing edge cases: blocked users, empty text, subtypes, DMs."""

    @pytest.fixture
    def app(self):
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
    async def test_mention_ignored_for_blocked_user(self, app, config_restricted, sessions):
        register_handlers(app, config_restricted, sessions)
        mention_handler = self._handlers["app_mention"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2000.0",
                "channel": "C_CHAN",
                "user": "U_BLOCKED",
                "text": "<@UBOT123> help me",
            }
            await mention_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_with_empty_text_ignored(self, app, config, sessions):
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "<@UBOT123>",
            }
            await mention_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_subtype_ignored(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2002.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "edited text",
                "subtype": "message_changed",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_empty_text_ignored(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2003.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_processed(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2004.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "hello in DM",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()
            assert mock_process.call_args[0][1] == "hello in DM"

    @pytest.mark.asyncio
    async def test_dm_blocked_user_ignored(self, app, config_restricted, sessions):
        register_handlers(app, config_restricted, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2005.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "U_BLOCKED",
                "text": "hello in DM",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_set_bounded(self, app, config, sessions):
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock):
            # Process 501 unique events to trigger the set clearing
            for i in range(501):
                event = {
                    "ts": f"{3000 + i}.0",
                    "channel": "C_CHAN",
                    "user": "UHUMAN1",
                    "text": f"<@UBOT123> msg {i}",
                }
                await mention_handler(event=event, client=AsyncMock())

        # After clearing, a previously-seen ts should be processable again
        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "3000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "<@UBOT123> msg 0",
            }
            await mention_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()


class TestProcessMessageEdgeCases:
    """Test _process_message error paths and edge cases."""

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

    def _mock_client(self):
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        return client

    @pytest.mark.asyncio
    async def test_handoff_session_id_extracted(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-123"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session) as mock_create:
            event = {"ts": "5000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(
                event,
                "do stuff (session_id: abc-123)",
                client, config, sessions,
            )
            # Session should be created with the handoff session_id
            assert mock_create.call_args.kwargs["session_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_reaction_add_failure_doesnt_block(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()
        client.reactions_add.side_effect = Exception("permission denied")

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Should still have updated the message despite reaction failure
        client.chat_update.assert_called()

    @pytest.mark.asyncio
    async def test_empty_response_posts_warning(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("system", subtype="init", session_id="s1")
            # No assistant or result text

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        final_update = client.chat_update.call_args_list[-1]
        assert "empty response" in final_update.kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_stream_exception_posts_error(self, config, sessions):
        async def exploding_stream(prompt):
            yield self._make_event("system", subtype="init", session_id="s1")
            raise RuntimeError("stream exploded")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "5003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Error message should be posted
        error_update = client.chat_update.call_args_list[-1]
        assert ":x: Error:" in error_update.kwargs["text"]
        assert "stream exploded" in error_update.kwargs["text"]

    @pytest.mark.asyncio
    async def test_long_response_split_into_chunks(self, config, sessions):
        long_text = "a" * 8000

        async def fake_stream(prompt):
            yield self._make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "5004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # First chunk via chat_update, remaining via chat_postMessage
        assert client.chat_update.called
        assert client.chat_postMessage.call_count >= 2  # initial "working" + at least 1 extra chunk

    @pytest.mark.asyncio
    async def test_periodic_update_during_streaming(self, config, sessions):
        # Generate enough text to trigger a periodic update (>100 chars)
        chunk_text = "x" * 150

        async def fake_stream(prompt):
            yield self._make_event("assistant", text=chunk_text)
            yield self._make_event("result", text=chunk_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "5005.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # chat_update should have been called at least twice:
        # 1. periodic update during streaming (>100 chars)
        # 2. final update with complete text
        assert client.chat_update.call_count >= 2

    @pytest.mark.asyncio
    async def test_reconnect_rebuilds_context(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()
        # _bot_in_thread returns False, _find_session_id_in_thread returns None
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "6000.0", "text": "original question"},
                {"user": "UBOT123", "ts": "6001.0", "text": "original answer"},
            ]
        }
        # For _find_session_id_in_thread fallback
        client.conversations_history.return_value = {"messages": []}

        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield self._make_event("result", text="ok")

        mock_session.stream = capturing_stream

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            # thread_ts != ts → this is a thread reply (reconnect scenario)
            event = {
                "ts": "6002.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "follow up", client, config, sessions)

        # The prompt should contain thread history
        assert captured_prompt is not None
        assert "conversation history" in captured_prompt
        assert "follow up" in captured_prompt

    @pytest.mark.asyncio
    async def test_reconnect_finds_session_id(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-123-def"

        client = self._mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "7000.0",
                    "text": "Handoff _(session_id: abc-123-def)_",
                },
            ]
        }

        with patch.object(sessions, "get_or_create", return_value=mock_session) as mock_create:
            event = {
                "ts": "7001.0",
                "thread_ts": "7000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions)

            # Should have passed the found session_id
            assert mock_create.call_args.kwargs["session_id"] == "abc-123-def"


class TestResolveChannelCwd:
    """Test _resolve_channel_cwd function."""

    @pytest.mark.asyncio
    async def test_returns_none_without_channel_dirs(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        client = AsyncMock()
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolves_channel_to_directory(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            base_directory=Path("/projects"),
            channel_dirs={"dev-team": "myproject"},
        )
        client = AsyncMock()
        client.conversations_info.return_value = {
            "channel": {"name": "dev-team"}
        }
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result == Path("/projects/myproject")

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            channel_dirs={"dev-team": "myproject"},
        )
        client = AsyncMock()
        client.conversations_info.side_effect = Exception("API error")
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_channel(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            channel_dirs={"dev-team": "myproject"},
        )
        client = AsyncMock()
        client.conversations_info.return_value = {
            "channel": {"name": "random-channel"}
        }
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result is None


class TestDownloadFiles:
    """Test _download_files helper that downloads Slack file attachments."""

    @staticmethod
    def _mock_http_session(responses):
        """Build a mock aiohttp.ClientSession that yields *responses* in order.

        Each *response* is a ``(status, data)`` tuple where *data* is the
        bytes returned by ``resp.read()``.  For error scenarios pass only a
        status code and the data will be ignored.

        Usage::

            mock_sess = self._mock_http_session([(200, b"content")])
            with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
                ...
        """
        resp_iter = iter(responses)

        class _FakeResp:
            def __init__(self, status, data):
                self.status = status
                self._data = data

            async def read(self):
                return self._data

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        class _FakeSession:
            def get(self, url, **kw):
                status, data = next(resp_iter)
                return _FakeResp(status, data)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        return _FakeSession()

    @pytest.mark.asyncio
    async def test_no_files_returns_empty(self, tmp_path):
        event = {"ts": "1.0", "text": "hello"}
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_files_list_returns_empty(self, tmp_path):
        event = {"ts": "1.0", "text": "hello", "files": []}
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_downloads_file_to_target_dir(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "test.py",
                    "mimetype": "text/x-python",
                    "url_private_download": "https://files.slack.com/test.py",
                    "size": 100,
                }
            ],
        }
        file_content = b"print('hello')"
        mock_sess = self._mock_http_session([(200, file_content)])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert len(result) == 1
        name, path, mime = result[0]
        assert name == "test.py"
        assert path.exists()
        assert path.read_bytes() == file_content
        assert mime == "text/x-python"

    @pytest.mark.asyncio
    async def test_skips_file_without_download_url(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "nourl.txt",
                    "mimetype": "text/plain",
                    "size": 10,
                }
            ],
        }
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_oversized_file(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "huge.bin",
                    "mimetype": "application/octet-stream",
                    "url_private_download": "https://files.slack.com/huge.bin",
                    "size": MAX_FILE_SIZE + 1,
                }
            ],
        }
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_file_on_http_error(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "secret.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/secret.txt",
                    "size": 50,
                }
            ],
        }
        mock_sess = self._mock_http_session([(403, b"")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert result == []

    @pytest.mark.asyncio
    async def test_duplicate_filenames_get_suffixed(self, tmp_path):
        (tmp_path / "report.csv").write_text("existing")

        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "report.csv",
                    "mimetype": "text/csv",
                    "url_private_download": "https://files.slack.com/report.csv",
                    "size": 20,
                }
            ],
        }
        file_content = b"new,data"
        mock_sess = self._mock_http_session([(200, file_content)])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert len(result) == 1
        _, path, _ = result[0]
        assert path.name == "report_1.csv"
        assert path.read_bytes() == file_content
        assert (tmp_path / "report.csv").read_text() == "existing"

    @pytest.mark.asyncio
    async def test_skips_file_on_download_exception(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "boom.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/boom.txt",
                    "size": 10,
                }
            ],
        }

        class _ExplodingSession:
            def get(self, url, **kw):
                raise Exception("connection reset")
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=_ExplodingSession()):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert result == []

    @pytest.mark.asyncio
    async def test_downloads_multiple_files(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "a.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/a.txt",
                    "size": 5,
                },
                {
                    "name": "b.png",
                    "mimetype": "image/png",
                    "url_private_download": "https://files.slack.com/b.png",
                    "size": 10,
                },
            ],
        }
        mock_sess = self._mock_http_session([(200, b"aaa"), (200, b"png-data")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert len(result) == 2
        assert result[0][0] == "a.txt"
        assert result[1][0] == "b.png"
        assert result[1][2] == "image/png"

    @pytest.mark.asyncio
    async def test_creates_target_dir_if_missing(self, tmp_path):
        target = tmp_path / "subdir" / "files"
        assert not target.exists()

        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "hello.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/hello.txt",
                    "size": 5,
                }
            ],
        }
        mock_sess = self._mock_http_session([(200, b"hello")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", target)

        assert target.exists()
        assert len(result) == 1


class TestProcessMessageWithFiles:
    """Test that file attachments are downloaded and injected into the prompt."""

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

    def _mock_client(self):
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        return client

    @pytest.mark.asyncio
    async def test_files_appended_to_prompt(self, config, sessions, tmp_path):
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"
        mock_session.cwd = tmp_path

        client = self._mock_client()

        downloaded = [
            ("screenshot.png", tmp_path / "screenshot.png", "image/png"),
        ]

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=downloaded),
        ):
            event = {
                "ts": "8000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "files": [{"name": "screenshot.png"}],
            }
            await _process_message(event, "what's wrong here?", client, config, sessions)

        assert captured_prompt is not None
        assert "what's wrong here?" in captured_prompt
        assert "Read tool" in captured_prompt
        assert "screenshot.png" in captured_prompt
        assert "Image:" in captured_prompt

    @pytest.mark.asyncio
    async def test_text_files_labeled_as_file(self, config, sessions, tmp_path):
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"
        mock_session.cwd = tmp_path

        client = self._mock_client()

        downloaded = [
            ("main.py", tmp_path / "main.py", "text/x-python"),
        ]

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=downloaded),
        ):
            event = {
                "ts": "8001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "files": [{"name": "main.py"}],
            }
            await _process_message(event, "review this code", client, config, sessions)

        assert "File:" in captured_prompt
        assert "Image:" not in captured_prompt

    @pytest.mark.asyncio
    async def test_file_only_no_text(self, config, sessions, tmp_path):
        """When a user sends only a file with no text, the prompt should still work."""
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"
        mock_session.cwd = tmp_path

        client = self._mock_client()

        downloaded = [
            ("error.log", tmp_path / "error.log", "text/plain"),
        ]

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=downloaded),
        ):
            event = {
                "ts": "8002.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "files": [{"name": "error.log"}],
            }
            await _process_message(event, "", client, config, sessions)

        assert captured_prompt is not None
        assert "error.log" in captured_prompt
        assert "Read tool" in captured_prompt

    @pytest.mark.asyncio
    async def test_no_files_prompt_unchanged(self, config, sessions):
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=[]),
        ):
            event = {
                "ts": "8003.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "just text", client, config, sessions)

        assert captured_prompt == "just text"


class TestFileShareSubtype:
    """Test that file_share subtype messages are not skipped."""

    @pytest.fixture
    def app(self):
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
    async def test_file_share_subtype_processed_in_dm(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "9000.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "check this file",
                "subtype": "file_share",
                "files": [{"name": "test.py"}],
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_subtypes_still_skipped(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "9001.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "edited",
                "subtype": "message_changed",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_only_no_text_processed_in_dm(self, app, config, sessions):
        """A file upload with no text should still be processed."""
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "9002.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "",
                "subtype": "file_share",
                "files": [{"name": "data.csv"}],
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()
