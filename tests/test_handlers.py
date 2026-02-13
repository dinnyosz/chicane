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
    _format_completion_summary,
    _format_tool_activity,
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
    async def test_text_only_response_updates_placeholder(self, config, sessions):
        """When there are no tool calls, the final text updates the placeholder."""
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

        # Only one chat_update: the final text replacing the placeholder
        assert client.chat_update.call_count == 1
        assert client.chat_update.call_args.kwargs["text"] == chunk_text

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


class TestFormatToolActivity:
    """Test _format_tool_activity helper for each tool type."""

    def _make_tool_event(self, *tool_blocks) -> ClaudeEvent:
        """Create an assistant event with tool_use content blocks."""
        content = list(tool_blocks)
        raw = {"type": "assistant", "message": {"content": content}}
        return ClaudeEvent(type="assistant", raw=raw)

    def _tool_block(self, name: str, **inputs) -> dict:
        return {"type": "tool_use", "name": name, "input": inputs}

    def test_read_tool(self):
        event = self._make_tool_event(
            self._tool_block("Read", file_path="/home/user/project/config.py")
        )
        assert _format_tool_activity(event) == [":mag: Reading `config.py`"]

    def test_bash_tool(self):
        event = self._make_tool_event(
            self._tool_block("Bash", command="pytest tests/")
        )
        assert _format_tool_activity(event) == [":computer: Running `pytest tests/`"]

    def test_bash_tool_long_command_truncated(self):
        long_cmd = "a" * 100
        event = self._make_tool_event(
            self._tool_block("Bash", command=long_cmd)
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert result[0].endswith("...`")
        assert len(result[0]) < len(long_cmd) + 30

    def test_edit_tool(self):
        event = self._make_tool_event(
            self._tool_block("Edit", file_path="/src/handlers.py")
        )
        assert _format_tool_activity(event) == [":pencil2: Editing `handlers.py`"]

    def test_write_tool(self):
        event = self._make_tool_event(
            self._tool_block("Write", file_path="/src/new_file.py")
        )
        assert _format_tool_activity(event) == [":pencil2: Writing `new_file.py`"]

    def test_grep_tool(self):
        event = self._make_tool_event(
            self._tool_block("Grep", pattern="download_files")
        )
        assert _format_tool_activity(event) == [":mag: Searching for `download_files`"]

    def test_glob_tool(self):
        event = self._make_tool_event(
            self._tool_block("Glob", pattern="**/*.py")
        )
        assert _format_tool_activity(event) == [":mag: Finding files `**/*.py`"]

    def test_webfetch_tool_with_url(self):
        event = self._make_tool_event(
            self._tool_block("WebFetch", url="https://example.com/api")
        )
        assert _format_tool_activity(event) == [":globe_with_meridians: Fetching `https://example.com/api`"]

    def test_webfetch_tool_no_url(self):
        event = self._make_tool_event(self._tool_block("WebFetch"))
        assert _format_tool_activity(event) == [":globe_with_meridians: Fetching URL"]

    def test_websearch_tool_with_query(self):
        event = self._make_tool_event(
            self._tool_block("WebSearch", query="python asyncio tutorial")
        )
        assert _format_tool_activity(event) == [":globe_with_meridians: Searching web for `python asyncio tutorial`"]

    def test_websearch_tool_no_query(self):
        event = self._make_tool_event(self._tool_block("WebSearch"))
        assert _format_tool_activity(event) == [":globe_with_meridians: Searching web"]

    def test_task_tool_with_details(self):
        event = self._make_tool_event(
            self._tool_block("Task", subagent_type="Explore", description="find auth code")
        )
        assert _format_tool_activity(event) == [":robot_face: Spawning Explore: find auth code"]

    def test_task_tool_subagent_type_only(self):
        event = self._make_tool_event(
            self._tool_block("Task", subagent_type="Bash")
        )
        assert _format_tool_activity(event) == [":robot_face: Spawning Bash"]

    def test_task_tool_no_details(self):
        event = self._make_tool_event(self._tool_block("Task"))
        assert _format_tool_activity(event) == [":robot_face: Spawning subagent"]

    def test_skill_tool_with_name(self):
        event = self._make_tool_event(
            self._tool_block("Skill", skill="commit")
        )
        assert _format_tool_activity(event) == [":zap: Running skill `commit`"]

    def test_skill_tool_no_name(self):
        event = self._make_tool_event(self._tool_block("Skill"))
        assert _format_tool_activity(event) == [":zap: Running skill"]

    def test_notebook_edit_tool(self):
        event = self._make_tool_event(
            self._tool_block("NotebookEdit", notebook_path="/home/user/analysis.ipynb")
        )
        assert _format_tool_activity(event) == [":notebook: Editing notebook `analysis.ipynb`"]

    def test_enter_plan_mode_tool(self):
        event = self._make_tool_event(self._tool_block("EnterPlanMode"))
        assert _format_tool_activity(event) == [":clipboard: Entering plan mode"]

    def test_ask_user_question_tool(self):
        event = self._make_tool_event(self._tool_block("AskUserQuestion"))
        assert _format_tool_activity(event) == [":question: Asking user a question"]

    def test_ask_user_question_with_content(self):
        event = self._make_tool_event(
            self._tool_block(
                "AskUserQuestion",
                questions=[
                    {
                        "question": "Which database should we use?",
                        "header": "Database",
                        "options": [
                            {"label": "PostgreSQL", "description": "Relational, battle-tested"},
                            {"label": "SQLite", "description": "Embedded, zero config"},
                        ],
                        "multiSelect": False,
                    }
                ],
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        text = result[0]
        assert ":question: *Claude is asking:*" in text
        assert "Which database should we use?" in text
        assert "*PostgreSQL*" in text
        assert "Relational, battle-tested" in text
        assert "*SQLite*" in text

    def test_ask_user_question_label_only_option(self):
        event = self._make_tool_event(
            self._tool_block(
                "AskUserQuestion",
                questions=[
                    {
                        "question": "Continue?",
                        "header": "Confirm",
                        "options": [
                            {"label": "Yes"},
                            {"label": "No"},
                        ],
                        "multiSelect": False,
                    }
                ],
            )
        )
        result = _format_tool_activity(event)
        text = result[0]
        assert "*Yes*" in text
        assert "*No*" in text

    def test_todo_write_with_tasks(self):
        event = self._make_tool_event(
            self._tool_block(
                "TodoWrite",
                todos=[
                    {"content": "Set up database", "status": "completed", "activeForm": "Setting up database"},
                    {"content": "Write API endpoints", "status": "in_progress", "activeForm": "Writing API endpoints"},
                    {"content": "Add tests", "status": "pending", "activeForm": "Adding tests"},
                ],
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert ":clipboard: *Tasks*" in result[0]
        assert ":white_check_mark: Set up database" in result[0]
        assert ":arrows_counterclockwise: Write API endpoints" in result[0]
        assert ":white_circle: Add tests" in result[0]

    def test_todo_write_empty_todos(self):
        event = self._make_tool_event(self._tool_block("TodoWrite", todos=[]))
        assert _format_tool_activity(event) == [":clipboard: Updating tasks"]

    def test_todo_write_no_todos_key(self):
        event = self._make_tool_event(self._tool_block("TodoWrite"))
        assert _format_tool_activity(event) == [":clipboard: Updating tasks"]

    def test_unknown_tool_fallback(self):
        event = self._make_tool_event(self._tool_block("CustomTool"))
        assert _format_tool_activity(event) == [":wrench: Custom Tool"]

    def test_unknown_tool_mcp_prefix_stripped_with_server(self):
        event = self._make_tool_event(self._tool_block("mcp__magaldi__search_code"))
        assert _format_tool_activity(event) == [":wrench: magaldi: Search Code"]

    def test_unknown_tool_mcp_deep_prefix(self):
        """MCP name with 4+ parts still extracts the last segment, shows server."""
        event = self._make_tool_event(self._tool_block("mcp__server__ns__find_files"))
        assert _format_tool_activity(event) == [":wrench: server: Find Files"]

    def test_unknown_tool_underscores_to_spaces(self):
        event = self._make_tool_event(self._tool_block("my_custom_tool"))
        assert _format_tool_activity(event) == [":wrench: My Custom Tool"]

    def test_multiple_tool_blocks(self):
        event = self._make_tool_event(
            self._tool_block("Read", file_path="/src/a.py"),
            self._tool_block("Read", file_path="/src/b.py"),
        )
        result = _format_tool_activity(event)
        assert len(result) == 2
        assert result[0] == ":mag: Reading `a.py`"
        assert result[1] == ":mag: Reading `b.py`"

    def test_mixed_text_and_tool_blocks(self):
        """Only tool_use blocks should produce activities; text blocks are ignored."""
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check that."},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
                    ]
                },
            },
        )
        result = _format_tool_activity(event)
        assert result == [":mag: Reading `x.py`"]

    def test_no_tool_blocks_returns_empty(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
        )
        assert _format_tool_activity(event) == []

    def test_empty_content_returns_empty(self):
        event = ClaudeEvent(
            type="assistant",
            raw={"type": "assistant", "message": {"content": []}},
        )
        assert _format_tool_activity(event) == []


class TestToolActivityStreaming:
    """Test that tool activities are posted correctly during streaming."""

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

    def _make_tool_event(self, *tool_blocks, text: str = "") -> ClaudeEvent:
        content = []
        if text:
            content.append({"type": "text", "text": text})
        content.extend(tool_blocks)
        raw = {"type": "assistant", "message": {"content": content}}
        return ClaudeEvent(type="assistant", raw=raw)

    def _tool_block(self, name: str, **inputs) -> dict:
        return {"type": "tool_use", "name": name, "input": inputs}

    def _mock_client(self):
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        return client

    @pytest.mark.asyncio
    async def test_first_tool_activity_updates_placeholder(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_tool_event(
                self._tool_block("Read", file_path="/src/config.py")
            )
            yield self._make_event("assistant", text="Here's the file content.")
            yield self._make_event("result", text="Here's the file content.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "show config", client, config, sessions)

        # First activity should update the placeholder
        first_update = client.chat_update.call_args_list[0]
        assert first_update.kwargs["text"] == ":mag: Reading `config.py`"
        assert first_update.kwargs["ts"] == "9999.0"

    @pytest.mark.asyncio
    async def test_subsequent_activities_posted_as_replies(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_tool_event(
                self._tool_block("Read", file_path="/src/a.py")
            )
            yield self._make_tool_event(
                self._tool_block("Edit", file_path="/src/a.py")
            )
            yield self._make_tool_event(
                self._tool_block("Bash", command="pytest")
            )
            yield self._make_event("assistant", text="Done.")
            yield self._make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "fix tests", client, config, sessions)

        # First activity updates placeholder (chat_update)
        assert client.chat_update.call_args_list[0].kwargs["text"] == ":mag: Reading `a.py`"

        # Subsequent activities + final text as thread replies (chat_postMessage)
        post_calls = client.chat_postMessage.call_args_list
        # post_calls[0] is the initial "Working on it..." placeholder
        # post_calls[1] is 2nd activity
        # post_calls[2] is 3rd activity
        # post_calls[3] is final text
        assert post_calls[1].kwargs["text"] == ":pencil2: Editing `a.py`"
        assert post_calls[2].kwargs["text"] == ":computer: Running `pytest`"
        assert post_calls[3].kwargs["text"] == "Done."

    @pytest.mark.asyncio
    async def test_final_text_as_thread_replies_when_activities_exist(
        self, config, sessions
    ):
        async def fake_stream(prompt):
            yield self._make_tool_event(
                self._tool_block("Read", file_path="/src/x.py")
            )
            yield self._make_event("assistant", text="The answer.")
            yield self._make_event("result", text="The answer.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Final text posted as thread reply (not chat_update)
        post_calls = client.chat_postMessage.call_args_list
        final_post = post_calls[-1]
        assert final_post.kwargs["text"] == "The answer."
        assert final_post.kwargs["thread_ts"] == "1000.0"

    @pytest.mark.asyncio
    async def test_no_activities_updates_placeholder_with_text(self, config, sessions):
        """When there are no tool calls, the response replaces the placeholder."""

        async def fake_stream(prompt):
            yield self._make_event("assistant", text="Quick answer.")
            yield self._make_event("result", text="Quick answer.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Should use chat_update (not a new message) for the final text
        final_update = client.chat_update.call_args_list[-1]
        assert final_update.kwargs["text"] == "Quick answer."

    @pytest.mark.asyncio
    async def test_text_flushed_before_next_tool_activity(self, config, sessions):
        """Text between tool calls is posted before the next activity,
        matching the order seen in Claude Code console."""

        async def fake_stream(prompt):
            yield self._make_tool_event(
                self._tool_block("Read", file_path="/src/a.py")
            )
            yield self._make_event("assistant", text="Looks good, let me edit it.")
            yield self._make_tool_event(
                self._tool_block("Edit", file_path="/src/a.py")
            )
            yield self._make_event("assistant", text="Done editing.")
            yield self._make_event("result", text="Done editing.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "fix it", client, config, sessions)

        # First activity updates placeholder
        assert client.chat_update.call_args_list[0].kwargs["text"] == ":mag: Reading `a.py`"

        # Thread replies should be in order: text → activity → final text
        post_calls = client.chat_postMessage.call_args_list
        # post_calls[0] = "Working on it..." placeholder
        # post_calls[1] = flushed text before 2nd tool
        # post_calls[2] = 2nd tool activity
        # post_calls[3] = final text
        assert post_calls[1].kwargs["text"] == "Looks good, let me edit it."
        assert post_calls[2].kwargs["text"] == ":pencil2: Editing `a.py`"
        assert post_calls[3].kwargs["text"] == "Done editing."

    @pytest.mark.asyncio
    async def test_long_text_with_activities_all_chunks_as_replies(
        self, config, sessions
    ):
        long_text = "a" * 8000

        async def fake_stream(prompt):
            yield self._make_tool_event(
                self._tool_block("Read", file_path="/src/big.py")
            )
            yield self._make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # All text chunks should be thread replies (not chat_update)
        post_calls = client.chat_postMessage.call_args_list
        # post_calls[0] is "Working on it...", rest are activity replies + text chunks
        text_replies = [
            c for c in post_calls[1:]
            if not c.kwargs["text"].startswith(":")
        ]
        assert len(text_replies) >= 2
        reassembled = "".join(c.kwargs["text"] for c in text_replies)
        assert reassembled == long_text


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

        Each *response* is a ``(status, data)`` or ``(status, data, content_type)``
        tuple where *data* is the bytes returned by ``resp.read()``.  For error
        scenarios pass only a status code and the data will be ignored.

        Usage::

            mock_sess = self._mock_http_session([(200, b"content")])
            with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
                ...
        """
        resp_iter = iter(responses)

        class _FakeResp:
            def __init__(self, status, data, content_type=None):
                self.status = status
                self._data = data
                self.content_type = content_type or "application/octet-stream"

            async def read(self):
                return self._data

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        class _FakeSession:
            def get(self, url, **kw):
                entry = next(resp_iter)
                status, data = entry[0], entry[1]
                ct = entry[2] if len(entry) > 2 else None
                return _FakeResp(status, data, ct)

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
    async def test_skips_file_when_response_is_html(self, tmp_path):
        """Slack returns HTML instead of file data when files:read scope is missing."""
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "photo.png",
                    "mimetype": "image/png",
                    "url_private_download": "https://files.slack.com/photo.png",
                    "size": 5000,
                }
            ],
        }
        html_page = b"<!DOCTYPE html><html>login page</html>"
        mock_sess = self._mock_http_session([(200, html_page, "text/html")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert result == []
        assert not (tmp_path / "photo.png").exists()

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


class TestFormatCompletionSummary:
    """Test _format_completion_summary helper."""

    def test_turns_with_duration(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 5,
                "total_cost_usd": 0.03,
                "duration_ms": 12000,
                "is_error": False,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 5 turns took 12s · $0.03"

    def test_single_turn_with_duration(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 1,
                "total_cost_usd": 0.01,
                "duration_ms": 3000,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 1 turn took 3s · $0.01"

    def test_long_duration_shows_minutes(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 27,
                "duration_ms": 125000,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 27 turns took 2m5s"

    def test_error_result(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 2,
                "total_cost_usd": 0.05,
                "duration_ms": 8000,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: 2 turns took 8s · $0.05"

    def test_error_max_turns_subtype(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error_max_turns",
                "num_turns": 10,
                "duration_ms": 30000,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: 10 turns took 30s (hit max turns limit)"

    def test_error_max_budget_subtype(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error_max_budget_usd",
                "num_turns": 5,
                "duration_ms": 60000,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: 5 turns took 1m0s (hit budget limit)"

    def test_error_during_execution_subtype(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error_during_execution",
                "num_turns": 2,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: Done — 2 turns (error during execution)"

    def test_success_subtype_no_reason(self):
        """Success results should not include a reason suffix."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "success",
                "num_turns": 3,
                "duration_ms": 5000,
                "is_error": False,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 3 turns took 5s"
        assert "(" not in result

    def test_turns_without_duration_fallback(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "num_turns": 3},
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: Done — 3 turns"

    def test_cost_displayed_when_present(self):
        """Cost is appended when total_cost_usd > 0 (API users only)."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 8,
                "duration_ms": 45000,
                "total_cost_usd": 1.23,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 8 turns took 45s · $1.23"

    def test_cost_not_displayed_when_zero(self):
        """Zero cost (CLI users) should not show cost."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 3,
                "duration_ms": 5000,
                "total_cost_usd": 0.0,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 3 turns took 5s"
        assert "$" not in result

    def test_cost_not_displayed_when_absent(self):
        """No cost field (CLI users) should not show cost."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 3,
                "duration_ms": 5000,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 3 turns took 5s"
        assert "$" not in result

    def test_no_turns_returns_none(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "total_cost_usd": 0.50, "duration_ms": 125000},
        )
        assert _format_completion_summary(event) is None

    def test_no_fields_returns_none(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result"},
        )
        assert _format_completion_summary(event) is None


class TestToolErrorHandling:
    """Test that tool errors from user events are posted to Slack."""

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
        elif type == "user":
            raw = {"type": "user", **kwargs}
        else:
            raw = {"type": type, **kwargs}
        return ClaudeEvent(type=type, raw=raw)

    def _mock_client(self):
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        return client

    @pytest.mark.asyncio
    async def test_tool_error_posted_as_warning(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event(
                "user",
                message={
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "Command failed: exit code 1",
                        }
                    ]
                },
            )
            yield self._make_event("assistant", text="Got an error.")
            yield self._make_event("result", text="Got an error.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "10000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "run tests", client, config, sessions)

        # Find the warning message
        warning_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "")
        ]
        assert len(warning_calls) == 1
        assert "Command failed: exit code 1" in warning_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_long_tool_error_truncated(self, config, sessions):
        long_error = "x" * 500

        async def fake_stream(prompt):
            yield self._make_event(
                "user",
                message={
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": long_error,
                        }
                    ]
                },
            )
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "10001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do it", client, config, sessions)

        warning_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "")
        ]
        assert len(warning_calls) == 1
        assert warning_calls[0].kwargs["text"].endswith("...")

    @pytest.mark.asyncio
    async def test_non_error_tool_result_ignored(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event(
                "user",
                message={
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": False,
                            "content": "success output",
                        }
                    ]
                },
            )
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "10002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do it", client, config, sessions)

        warning_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "")
        ]
        assert len(warning_calls) == 0


class TestCompletionSummaryPosting:
    """Test that completion summary is posted after streaming."""

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
    async def test_summary_posted_after_response(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("assistant", text="Done!")
            yield self._make_event(
                "result", text="Done!",
                num_turns=3, total_cost_usd=0.02, duration_ms=5000,
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "11000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Last chat_postMessage before reactions should be the summary
        post_calls = client.chat_postMessage.call_args_list
        summary_calls = [
            c for c in post_calls
            if ":checkered_flag:" in c.kwargs.get("text", "")
        ]
        assert len(summary_calls) == 1
        assert "3 turns" in summary_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_no_summary_when_no_result_event(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("assistant", text="Partial response")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "11001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        summary_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":checkered_flag:" in c.kwargs.get("text", "")
        ]
        assert len(summary_calls) == 0


class TestCompactBoundaryNotification:
    """Test that context compaction events notify the user in Slack."""

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
    async def test_auto_compaction_notifies_user(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("assistant", text="Working on it...")
            yield self._make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "auto", "pre_tokens": 95000},
            )
            yield self._make_event("assistant", text="Continuing after compaction.")
            yield self._make_event("result", text="Continuing after compaction.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "12000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "big task", client, config, sessions)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        msg = brain_calls[0].kwargs["text"]
        assert "automatically compacted" in msg
        assert "95,000 tokens" in msg
        assert "earlier messages may be summarized" in msg

    @pytest.mark.asyncio
    async def test_manual_compaction_notifies_user(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "manual", "pre_tokens": 50000},
            )
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "12001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "compact", client, config, sessions)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        assert "manually compacted" in brain_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_compaction_without_pre_tokens(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "auto"},
            )
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "12002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        msg = brain_calls[0].kwargs["text"]
        assert "tokens" not in msg
        assert "earlier messages may be summarized" in msg

    @pytest.mark.asyncio
    async def test_compaction_without_metadata(self, config, sessions):
        """Handle edge case where compact_metadata is missing entirely."""

        async def fake_stream(prompt):
            yield self._make_event(
                "system",
                subtype="compact_boundary",
            )
            yield self._make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "12003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        assert "automatically compacted" in brain_calls[0].kwargs["text"]


class TestPermissionDenialNotification:
    """Test that permission denials from result events are surfaced."""

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
    async def test_denials_posted(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event("assistant", text="I tried but couldn't.")
            yield self._make_event(
                "result", text="I tried but couldn't.",
                num_turns=2, duration_ms=3000,
                permission_denials=[
                    {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "rm -rf /"}},
                    {"tool_name": "Bash", "tool_use_id": "t2", "tool_input": {"command": "sudo reboot"}},
                    {"tool_name": "Write", "tool_use_id": "t3", "tool_input": {"file_path": "/etc/passwd"}},
                ],
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "13000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do it", client, config, sessions)

        denial_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":no_entry_sign:" in c.kwargs.get("text", "")
        ]
        assert len(denial_calls) == 1
        msg = denial_calls[0].kwargs["text"]
        assert "3 tool permissions denied" in msg
        assert "`Bash`" in msg
        assert "`Write`" in msg

    @pytest.mark.asyncio
    async def test_single_denial_singular(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event(
                "result", text="blocked",
                num_turns=1, duration_ms=1000,
                permission_denials=[
                    {"tool_name": "Edit", "tool_use_id": "t1", "tool_input": {}},
                ],
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "13001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "edit it", client, config, sessions)

        denial_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":no_entry_sign:" in c.kwargs.get("text", "")
        ]
        assert len(denial_calls) == 1
        assert "1 tool permission denied" in denial_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_no_denials_no_message(self, config, sessions):
        async def fake_stream(prompt):
            yield self._make_event(
                "result", text="all good",
                num_turns=1, duration_ms=1000,
                permission_denials=[],
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = self._mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session):
            event = {"ts": "13002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions)

        denial_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":no_entry_sign:" in c.kwargs.get("text", "")
        ]
        assert len(denial_calls) == 0


class TestSubagentPrefix:
    """Test that subagent activities get the hook prefix."""

    def _make_tool_event(self, *tool_blocks, parent_tool_use_id=None) -> ClaudeEvent:
        content = list(tool_blocks)
        raw = {"type": "assistant", "message": {"content": content}}
        if parent_tool_use_id:
            raw["parent_tool_use_id"] = parent_tool_use_id
        return ClaudeEvent(type="assistant", raw=raw)

    def _tool_block(self, name: str, **inputs) -> dict:
        return {"type": "tool_use", "name": name, "input": inputs}

    def test_parent_tool_use_id_detected(self):
        event = self._make_tool_event(
            self._tool_block("Read", file_path="/src/a.py"),
            parent_tool_use_id="toolu_abc123",
        )
        assert event.parent_tool_use_id == "toolu_abc123"

    def test_no_parent_tool_use_id(self):
        event = self._make_tool_event(
            self._tool_block("Read", file_path="/src/a.py"),
        )
        assert event.parent_tool_use_id is None


class TestSummarizeToolInput:
    """Test _summarize_tool_input for the catch-all tool display."""

    def test_string_values(self):
        from chicane.handlers import _summarize_tool_input
        result = _summarize_tool_input({"query": "authentication", "limit": 10})
        assert "query: `authentication`" in result
        assert "limit: `10`" in result

    def test_skips_long_strings(self):
        from chicane.handlers import _summarize_tool_input
        result = _summarize_tool_input({"data": "x" * 200})
        assert result == ""

    def test_truncates_medium_strings(self):
        from chicane.handlers import _summarize_tool_input
        val = "a" * 80
        result = _summarize_tool_input({"query": val})
        assert result.endswith("...`")

    def test_skips_nested_objects(self):
        from chicane.handlers import _summarize_tool_input
        result = _summarize_tool_input({"nested": {"a": 1}, "name": "test"})
        assert "name: `test`" in result
        assert "nested: " not in result

    def test_empty_input(self):
        from chicane.handlers import _summarize_tool_input
        assert _summarize_tool_input({}) == ""

    def test_bool_values(self):
        from chicane.handlers import _summarize_tool_input
        result = _summarize_tool_input({"include_tests": True})
        assert "include_tests: `true`" in result

    def test_respects_max_len(self):
        from chicane.handlers import _summarize_tool_input
        result = _summarize_tool_input(
            {"a": "short", "b": "another", "c": "more"},
            max_len=20,
        )
        # Should not include all three
        assert result.count("`") <= 4  # at most 2 values


class TestCatchAllToolDisplay:
    """Test that the catch-all else branch shows args."""

    def test_mcp_tool_with_args(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__magaldi__pattern_search",
                            "input": {"pattern": "def main", "mode": "regexp"},
                        }
                    ]
                },
            },
        )
        activities = _format_tool_activity(event)
        assert len(activities) == 1
        assert "magaldi: Pattern Search" in activities[0]
        assert "pattern: `def main`" in activities[0]

    def test_unknown_tool_no_args(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "SomeNewTool",
                            "input": {},
                        }
                    ]
                },
            },
        )
        activities = _format_tool_activity(event)
        assert activities == [":wrench: Some New Tool"]
