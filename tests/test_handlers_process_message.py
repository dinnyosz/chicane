"""Tests for _process_message core logic: formatting, error paths, reconnection."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config, save_handoff_session, load_handoff_session
from chicane.handlers import _process_message
from tests.conftest import make_event, make_tool_event, tool_block, mock_client, mock_session_info


class TestProcessMessageFormatting:
    """Test that _process_message preserves newlines from streamed text."""

    @pytest.mark.asyncio
    async def test_streamed_text_with_newlines_not_overwritten_by_result(
        self, config, sessions, queue
    ):
        """The result event often flattens newlines. Streamed text should win."""
        streamed = "First paragraph.\n\nSecond paragraph.\n\n- bullet 1\n- bullet 2"
        flat_result = "First paragraph. Second paragraph. - bullet 1 - bullet 2"

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="sess-1")
            yield make_event("assistant", text=streamed)
            yield make_event("result", text=flat_result)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "sess-1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions, queue)

        # Find the posted text (not the session init or completion summary)
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "").startswith("First")
        ]
        assert len(text_posts) == 1
        assert "\n\n" in text_posts[0].kwargs["text"]
        # Bullets get converted from - to • by _markdown_to_mrkdwn
        expected = streamed.replace("- bullet", "• bullet")
        assert text_posts[0].kwargs["text"] == expected

    @pytest.mark.asyncio
    async def test_result_text_used_when_no_streamed_content(
        self, config, sessions, queue
    ):
        """When no assistant events arrive, fall back to result text."""
        result_text = "Fallback result text."

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="sess-2")
            yield make_event("result", text=result_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "sess-2"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "1001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions, queue)

        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == result_text
        ]
        assert len(text_posts) == 1


class TestProcessMessageEdgeCases:
    """Test _process_message error paths and edge cases."""

    @pytest.mark.asyncio
    async def test_handoff_session_id_extracted(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-123"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create:
            event = {"ts": "5000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(
                event,
                "do stuff (session_id: abc-123)",
                client, config, sessions, queue,
            )
            assert mock_create.call_args.kwargs["session_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_reaction_add_failure_doesnt_block(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        client.reactions_add.side_effect = Exception("permission denied")

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        client.chat_postMessage.assert_called()

    @pytest.mark.asyncio
    async def test_empty_response_auto_continues(self, config, sessions, queue):
        """First empty response should auto-send 'continue', not warn."""
        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                # First call: empty response
                pass
            else:
                # Retry with "continue": returns text
                yield make_event("assistant", text="Here's the answer")
                yield make_event("result", text="Here's the answer")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should have posted the auto-continue notice
        continue_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "sending `continue`" in c.kwargs.get("text", "")
        ]
        assert len(continue_posts) == 1
        assert "attempt 1/2" in continue_posts[0].kwargs["text"]

        # Should have posted the actual response
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "Here's the answer" in c.kwargs.get("text", "")
        ]
        assert len(text_posts) == 1

        # Counter should be reset after success
        assert si.empty_continue_count == 0

        # Should NOT have posted a warning
        warning_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "") and "empty response" in c.kwargs.get("text", "").lower()
        ]
        assert len(warning_posts) == 0

    @pytest.mark.asyncio
    async def test_empty_response_warns_after_max_retries(self, config, sessions, queue):
        """After 2 failed auto-continues, should warn the user."""
        async def always_empty_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")

        mock_session = MagicMock()
        mock_session.stream = always_empty_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)
        # Simulate already exhausted retries
        si.empty_continue_count = 2

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        warning_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "empty response" in c.kwargs.get("text", "").lower()
            and "2 automatic" in c.kwargs.get("text", "")
        ]
        assert len(warning_posts) == 1

    @pytest.mark.asyncio
    async def test_empty_continue_counter_resets_on_proper_response(self, config, sessions, queue):
        """Counter resets when Claude gives a proper response with text."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text="Got it!")
            yield make_event("result", text="Got it!")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)
        si.empty_continue_count = 1  # Had a previous empty response

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Counter should be reset
        assert si.empty_continue_count == 0

    @pytest.mark.asyncio
    async def test_empty_continue_counter_resets_on_tool_use(self, config, sessions, queue):
        """Counter resets when Claude responds with tool use (no text)."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_tool_event(
                tool_block("ExitPlanMode", plan="# My Plan"),
            )
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)
        si.empty_continue_count = 2  # Was maxed out

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        assert si.empty_continue_count == 0

    @pytest.mark.asyncio
    async def test_empty_continue_reconnects_sdk_client(self, config, sessions, queue):
        """Auto-continue should disconnect and reconnect the SDK client."""
        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                pass  # empty
            else:
                yield make_event("assistant", text="Recovered")
                yield make_event("result", text="Recovered")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # disconnect() should have been called to reset the stuck SDK client
        mock_session.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_continue_second_attempt(self, config, sessions, queue):
        """Second empty response still retries (attempt 2/2)."""
        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            # Always empty

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)
        si.empty_continue_count = 1  # Already had one failed retry

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should have tried auto-continue
        continue_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "sending `continue`" in c.kwargs.get("text", "")
        ]
        assert len(continue_posts) == 1
        assert "attempt 2/2" in continue_posts[0].kwargs["text"]

        # Counter should now be 2 (retry also failed)
        assert si.empty_continue_count == 2

    @pytest.mark.asyncio
    async def test_tool_only_response_no_empty_warning(self, config, sessions, queue):
        """Tool-only responses (e.g. ExitPlanMode) should NOT trigger empty warning."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_tool_event(
                tool_block("ExitPlanMode", plan="# My Plan"),
            )
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5002.1", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "exit plan mode", client, config, sessions, queue)

        warning_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "empty response" in c.kwargs.get("text", "").lower()
        ]
        assert len(warning_posts) == 0

    @pytest.mark.asyncio
    async def test_stream_exception_posts_error(self, config, sessions, queue):
        async def exploding_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            raise RuntimeError("stream exploded")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        error_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":x: Error" in c.kwargs.get("text", "")
        ]
        assert len(error_posts) == 1
        error_text = error_posts[0].kwargs["text"]
        assert ":x: Error (RuntimeError)" in error_text
        assert "Check bot logs" in error_text
        # Ensure internal error message is NOT leaked to Slack
        assert "stream exploded" not in error_text

    @pytest.mark.asyncio
    async def test_timeout_exception_posts_friendly_message(self, config, sessions, queue):
        """Timeout errors show a user-friendly message instead of generic error."""
        async def timeout_stream(prompt):
            raise Exception("Control request timeout: initialize")
            yield  # noqa: unreachable — makes this an async generator

        mock_session = MagicMock()
        mock_session.stream = timeout_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        error_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":x:" in c.kwargs.get("text", "")
        ]
        assert len(error_posts) == 1
        error_text = error_posts[0].kwargs["text"]
        assert "timed out" in error_text
        assert "try again" in error_text.lower()
        # Should NOT show generic "Check bot logs" for timeouts
        assert "Check bot logs" not in error_text

    @pytest.mark.asyncio
    async def test_buffer_overflow_posts_partial_text_and_warning(self, config, sessions, queue):
        """SDK buffer overflow posts partial text and warning (no retry)."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text="Partial text before crash")
            raise Exception(
                "Failed to decode JSON: JSON message exceeded "
                "maximum buffer size of 1048576 bytes..."
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        all_texts = [
            c.kwargs.get("text", "")
            for c in client.chat_postMessage.call_args_list
        ]

        # Partial text from before crash should be posted
        partial_msgs = [t for t in all_texts if "Partial text before crash" in t]
        assert len(partial_msgs) == 1

        # Warning about buffer limit
        warning_msgs = [t for t in all_texts if "buffer limit" in t.lower()]
        assert len(warning_msgs) == 1

        # Should NOT show generic ":x: Error"
        error_msgs = [t for t in all_texts if ":x: Error" in t]
        assert len(error_msgs) == 0

    @pytest.mark.asyncio
    async def test_buffer_overflow_no_partial_text(self, config, sessions, queue):
        """SDK buffer overflow with no partial text still posts warning."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            raise Exception(
                "Failed to decode JSON: JSON message exceeded "
                "maximum buffer size of 1048576 bytes..."
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        all_texts = [
            c.kwargs.get("text", "")
            for c in client.chat_postMessage.call_args_list
        ]

        # Warning about buffer limit
        warning_msgs = [t for t in all_texts if "buffer limit" in t.lower()]
        assert len(warning_msgs) == 1

        # Should NOT show generic ":x: Error"
        error_msgs = [t for t in all_texts if ":x: Error" in t]
        assert len(error_msgs) == 0

    @pytest.mark.asyncio
    async def test_long_response_uses_markdown_block(self, config, sessions, queue):
        """Responses within markdown block limit are posted as markdown blocks."""
        long_text = "a" * 8000

        async def fake_stream(prompt):
            yield make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should be posted via chat_postMessage with markdown blocks
        post_calls = client.chat_postMessage.call_args_list
        md_calls = [c for c in post_calls if c.kwargs.get("blocks")]
        assert len(md_calls) >= 1
        blocks = md_calls[0].kwargs["blocks"]
        assert blocks[0]["type"] == "markdown"

    @pytest.mark.asyncio
    async def test_very_long_response_split_into_multiple_markdown_blocks(self, config, sessions, queue):
        """Very long responses are split into multiple markdown block messages."""
        long_text = "a" * 25000

        async def fake_stream(prompt):
            yield make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should be split into multiple messages with markdown blocks, not a snippet
        client.files_upload_v2.assert_not_called()
        post_calls = client.chat_postMessage.call_args_list
        md_calls = [c for c in post_calls if c.kwargs.get("blocks")]
        assert len(md_calls) >= 3  # 25k / 11k = at least 3 chunks

    @pytest.mark.asyncio
    async def test_moderate_response_uses_single_markdown_block(self, config, sessions, queue):
        """Responses within markdown block limit fit in a single message."""
        # 3950 chars: fits in a single markdown block (limit 11k)
        text = "a" * 3950

        async def fake_stream(prompt):
            yield make_event("result", text=text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.1", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should be a single message with markdown block, not a snippet
        client.files_upload_v2.assert_not_called()
        post_calls = client.chat_postMessage.call_args_list
        md_calls = [c for c in post_calls if c.kwargs.get("blocks")]
        assert len(md_calls) == 1
        assert md_calls[0].kwargs["blocks"][0]["type"] == "markdown"

    @pytest.mark.asyncio
    async def test_markdown_response_split_at_limit(self, config, sessions, queue):
        """Responses exceeding markdown block limit are split into multiple messages."""
        # 15000 chars: above MARKDOWN_BLOCK_LIMIT (11k) but below snippet threshold (22k)
        text = ("a" * 100 + "\n\n") * 150  # ~15300 chars with paragraph breaks

        async def fake_stream(prompt):
            yield make_event("result", text=text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.2", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should be split into 2+ messages, each with markdown blocks
        client.files_upload_v2.assert_not_called()
        post_calls = client.chat_postMessage.call_args_list
        md_calls = [c for c in post_calls if c.kwargs.get("blocks")]
        assert len(md_calls) >= 2
        for call in md_calls:
            assert call.kwargs["blocks"][0]["type"] == "markdown"

    @pytest.mark.asyncio
    async def test_text_only_response_posted_as_reply(self, config, sessions, queue):
        """When there are no tool calls, the final text is posted as a thread reply."""
        chunk_text = "x" * 150

        async def fake_stream(prompt):
            yield make_event("assistant", text=chunk_text)
            yield make_event("result", text=chunk_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5005.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == chunk_text
        ]
        assert len(text_posts) == 1
        client.chat_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_rebuilds_context(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "6000.0", "text": "original question"},
                {"user": "UBOT123", "ts": "6001.0", "text": "original answer"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}

        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield make_event("result", text="ok")

        mock_session.stream = capturing_stream

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "6002.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "follow up", client, config, sessions, queue)

        assert captured_prompt is not None
        assert "conversation history" in captured_prompt
        assert "follow up" in captured_prompt

    @pytest.mark.asyncio
    async def test_reconnect_finds_session_id(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-123-def"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "7000.0",
                    "text": "Handoff _(session_id: abc-123-def)_",
                },
            ]
        }

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create:
            event = {
                "ts": "7001.0",
                "thread_ts": "7000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions, queue)

            assert mock_create.call_args.kwargs["session_id"] == "abc-123-def"

    @pytest.mark.asyncio
    async def test_reconnect_finds_session_alias(self, config, sessions, queue, tmp_path):
        """Reconnect resolves a funky alias to the real session_id."""
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "real-uuid-here"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "8000.0",
                    "text": "Handoff _(session: sneaky-octopus-pizza)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("sneaky-octopus-pizza", "real-uuid-here")

            event = {
                "ts": "8001.0",
                "thread_ts": "8000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions, queue)

            assert mock_create.call_args.kwargs["session_id"] == "real-uuid-here"

    @pytest.mark.asyncio
    async def test_reconnect_with_alias_announces_continuing(self, config, sessions, queue, tmp_path):
        """When reconnecting via alias, 'Continuing session' is posted
        with the original alias name."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="real-uuid-here")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "real-uuid-here"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "8000.0",
                    "text": "Handoff _(session: sneaky-octopus-pizza)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
        ):
            save_handoff_session("sneaky-octopus-pizza", "real-uuid-here")

            event = {
                "ts": "8001.0",
                "thread_ts": "8000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions, queue)

            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "Continuing session" in text
            assert "sneaky-octopus-pizza" in text

    @pytest.mark.asyncio
    async def test_reconnect_finds_bot_session_message(self, config, sessions, queue, tmp_path):
        """The bot's own ':sparkles: New session' message contains
        _(session: alias)_ and should be found on reconnect."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="bot-sess-id")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "bot-sess-id"

        client = mock_client()
        # Thread contains the bot's own session announcement (not a handoff)
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "7000.0", "text": "hey bot"},
                {
                    "user": "UBOT123",
                    "ts": "7001.0",
                    "text": ":sparkles: New session\n_(session: clever-fox-rainbow)_",
                },
                {"user": "UBOT123", "ts": "7002.0", "text": "Here's the answer"},
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("clever-fox-rainbow", "bot-sess-id")

            event = {
                "ts": "7003.0",
                "thread_ts": "7000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "follow up", client, config, sessions, queue)

            # Should have found the session_id from the bot's own message
            assert mock_create.call_args.kwargs["session_id"] == "bot-sess-id"

            # Should announce "Continuing session" with the alias
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            assert "clever-fox-rainbow" in continuing_posts[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_reconnect_picks_last_session_in_thread(self, config, sessions, queue, tmp_path):
        """When a thread has multiple session aliases (e.g. bot restarted),
        the most recent one should be used."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="second-sess")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "second-sess"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: New session\n_(session: old-dusty-parrot)_",
                },
                {"user": "UBOT123", "ts": "6001.0", "text": "first response"},
                {
                    "user": "UBOT123",
                    "ts": "6002.0",
                    "text": ":sparkles: New session\n_(session: fresh-shiny-eagle)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("old-dusty-parrot", "first-sess")
            save_handoff_session("fresh-shiny-eagle", "second-sess")

            event = {
                "ts": "6003.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "pick up", client, config, sessions, queue)

            # Should have used the LAST session (fresh-shiny-eagle)
            assert mock_create.call_args.kwargs["session_id"] == "second-sess"

            # Should announce continuing with the most recent alias
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "fresh-shiny-eagle" in text
            # Should mention the skipped older session
            assert "old-dusty-parrot" in text

    @pytest.mark.asyncio
    async def test_reconnect_duplicate_alias_not_shown_as_skipped(self, config, sessions, queue, tmp_path):
        """When the same alias appears multiple times in a thread (e.g. from
        the original handoff + a previous reconnect message), the duplicate
        should NOT be displayed as 'skipped older'."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="the-sess")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "the-sess"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: Handoff\n_(session: gardening-ruby-scroll)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "6001.0",
                    "text": ":arrows_counterclockwise: Continuing session _gardening-ruby-scroll_\n_(session: gardening-ruby-scroll)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("gardening-ruby-scroll", "the-sess")

            event = {
                "ts": "6003.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello again", client, config, sessions, queue)

            assert mock_create.call_args.kwargs["session_id"] == "the-sess"

            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "gardening-ruby-scroll" in text
            # The duplicate alias should NOT appear as "skipped older"
            assert "skipped older" not in text

    @pytest.mark.asyncio
    async def test_reconnect_unmapped_alias_warns(self, config, sessions, queue, tmp_path):
        """When reconnecting and the alias can't be mapped, a warning is
        shown and a new session starts."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="brand-new-id")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "brand-new-id"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: New session\n_(session: lost-ghost-cat)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            # Don't save lost-ghost-cat — it's unmapped

            event = {
                "ts": "6001.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello again", client, config, sessions, queue)

            # No session_id should be passed (couldn't map)
            assert mock_create.call_args.kwargs.get("session_id") is None

            # Should show warning about unmapped alias
            warning_posts = [
                c for c in client.chat_postMessage.call_args_list
                if "session map lost" in c.kwargs.get("text", "")
            ]
            assert len(warning_posts) == 1
            text = warning_posts[0].kwargs["text"]
            assert "lost-ghost-cat" in text

    @pytest.mark.asyncio
    async def test_reconnect_fallback_to_older_session(self, config, sessions, queue, tmp_path):
        """When the newest alias can't be mapped, fall back to the next
        older one and mention the unmapped one."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="old-good-sess")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "old-good-sess"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: New session\n_(session: old-good-parrot)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "6001.0",
                    "text": ":sparkles: New session\n_(session: new-lost-eagle)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("old-good-parrot", "old-good-sess")
            # Don't save new-lost-eagle — it's unmapped

            event = {
                "ts": "6002.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "pick up", client, config, sessions, queue)

            # Should have fallen back to old-good-parrot
            assert mock_create.call_args.kwargs["session_id"] == "old-good-sess"

            # Should announce continuing AND mention the unmapped one
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "old-good-parrot" in text
            assert "new-lost-eagle" in text
            assert "couldn't map" in text

    @pytest.mark.asyncio
    async def test_new_session_saves_alias_and_announces(self, config, sessions, queue, tmp_path):
        """When a new session starts (init event), an alias is generated,
        saved to disk, and announced as a new session in the thread."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="new-sess-id")
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "new-sess-id"

        client = mock_client()

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
        ):
            event = {"ts": "9000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

            # Should have posted the "New session" announcement
            alias_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":sparkles:" in c.kwargs.get("text", "")
            ]
            assert len(alias_posts) == 1
            alias_text = alias_posts[0].kwargs["text"]
            assert "New session" in alias_text
            # Must contain the scannable (session: alias) format
            m = re.search(r"\(session:\s*([a-z]+(?:-[a-z]+)+)\)", alias_text)
            assert m, f"No scannable session alias found in: {alias_text}"
            alias = m.group(1)

            # Alias should be saved to disk, mapping to the real session_id
            assert load_handoff_session(alias) == "new-sess-id"

    @pytest.mark.asyncio
    async def test_handoff_session_announces_continuing(self, config, sessions, queue, tmp_path):
        """When resuming a handoff session, a 'Continuing session' message
        should be posted with the alias."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="abc-def-123")
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-def-123"

        client = mock_client()

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
        ):
            event = {"ts": "9100.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(
                event,
                "continue (session_id: abc-def-123)",
                client, config, sessions, queue,
            )

            # Should have posted a "Continuing session" announcement
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "Continuing session" in text
            # Must contain the scannable (session: alias) format
            m = re.search(r"\(session:\s*([a-z]+(?:-[a-z]+)+)\)", text)
            assert m, f"No scannable session alias found in: {text}"

    @pytest.mark.asyncio
    async def test_repeated_init_events_do_not_generate_new_alias(
        self, config, sessions, queue, tmp_path
    ):
        """When the SDK emits init on every query(), only the first should
        generate an alias.  Regression test for duplicate session aliases."""

        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="same-sess-id")
            yield make_event("result", text=f"response {call_count}")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "same-sess-id"

        client = mock_client()
        info = mock_session_info(mock_session)

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=info),
        ):
            # First message — should generate alias
            event1 = {"ts": "9200.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event1, "hello", client, config, sessions, queue)

            alias_posts_1 = [
                c for c in client.chat_postMessage.call_args_list
                if ":sparkles:" in c.kwargs.get("text", "")
            ]
            assert len(alias_posts_1) == 1
            first_alias = info.session_alias

            client.reset_mock()
            client.chat_postMessage.return_value = {"ts": "9999.0"}

            # Second message in same session — should NOT generate a new alias
            event2 = {
                "ts": "9201.0",
                "thread_ts": "9200.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event2, "follow up", client, config, sessions, queue)

            alias_posts_2 = [
                c for c in client.chat_postMessage.call_args_list
                if ":sparkles:" in c.kwargs.get("text", "")
            ]
            assert len(alias_posts_2) == 0
            # Alias should not have changed
            assert info.session_alias == first_alias


class TestEmptyContinueRetryEdgeCases:
    """Test edge cases in the empty-continue retry loop."""

    @pytest.mark.asyncio
    async def test_retry_handles_subagent_tool_activities(self, config, sessions, queue):
        """During retry, tool activities with parent_tool_use_id get hook prefix."""
        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                # First: empty
                pass
            else:
                # Retry: subagent activity
                yield make_tool_event(
                    tool_block("Read", file_path="/src/a.py"),
                    parent_tool_use_id="toolu_parent",
                )
                yield make_event("assistant", text="Found it.")
                yield make_event("result", text="Found it.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Subagent activity during retry should have hook prefix
        hook_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":arrow_right_hook:" in c.kwargs.get("text", "")
        ]
        assert len(hook_posts) >= 1

    @pytest.mark.asyncio
    async def test_retry_handles_tool_errors(self, config, sessions, queue):
        """During retry, tool errors in user events are posted as warnings."""
        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                pass
            else:
                yield make_tool_event(tool_block("Bash", id="tu_err", command="bad"))
                yield make_event(
                    "user",
                    message={
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": "tu_err",
                            "is_error": True,
                            "content": "command not found",
                        }]
                    },
                )
                yield make_event("assistant", text="Error occurred.")
                yield make_event("result", text="Error occurred.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        warning_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "") and "command not found" in c.kwargs.get("text", "")
        ]
        assert len(warning_posts) == 1

    @pytest.mark.asyncio
    async def test_retry_result_text_overwrites_shorter_full_text(self, config, sessions, queue):
        """During retry, result_text replaces full_text when it's longer."""
        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                pass
            else:
                yield make_event("assistant", text="Short")
                yield make_event("result", text="Longer result text here")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == "Longer result text here"
        ]
        assert len(text_posts) == 1

    @pytest.mark.asyncio
    async def test_retry_long_response_uses_markdown_block(self, config, sessions, queue):
        """During retry, response within markdown limit uses markdown blocks."""
        call_count = 0
        long_text = "a" * 8000

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                pass
            else:
                yield make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should be posted with markdown blocks, not as a snippet
        post_calls = client.chat_postMessage.call_args_list
        md_calls = [c for c in post_calls if c.kwargs.get("blocks")]
        assert len(md_calls) >= 1
        assert md_calls[0].kwargs["blocks"][0]["type"] == "markdown"

    @pytest.mark.asyncio
    async def test_retry_very_long_response_split_into_markdown_blocks(self, config, sessions, queue):
        """During retry, very long response should be split into markdown blocks."""
        call_count = 0
        long_text = "a" * 25000

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                pass
            else:
                yield make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should be split into markdown blocks, not a snippet
        client.files_upload_v2.assert_not_called()
        post_calls = client.chat_postMessage.call_args_list
        md_calls = [c for c in post_calls if c.kwargs.get("blocks")]
        assert len(md_calls) >= 3

    @pytest.mark.asyncio
    async def test_retry_verbose_tool_results_posted(self, config, sessions, queue):
        """During retry in verbose mode, tool results are posted."""
        verbose_config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["UHUMAN1"],
            rate_limit=10000,
            verbosity="verbose",
        )
        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="s1")
            if call_count == 1:
                pass
            else:
                yield make_tool_event(tool_block("Bash", id="tu_1", command="echo hi"))
                yield make_event(
                    "user",
                    message={
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "is_error": False,
                            "content": "hi",
                        }]
                    },
                )
                yield make_event("assistant", text="Done.")
                yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        si = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=si):
            event = {"ts": "6004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, verbose_config, sessions, queue)

        clipboard_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":clipboard:" in c.kwargs.get("text", "")
        ]
        assert len(clipboard_posts) >= 1


class TestProcessMessageHandoffPrompt:
    """Test empty prompt with handoff session uses special greeting."""

    @pytest.mark.asyncio
    async def test_empty_prompt_with_handoff_uses_greeting(self, config, sessions, queue):
        """When user @mentions bot with no text in a handoff thread, a special
        greeting prompt is sent instead of empty string."""
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield make_event("system", subtype="init", session_id="abc-def-123")
            yield make_event("result", text="Hello!")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "abc-def-123"

        client = mock_client()
        info = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "9500.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(
                event,
                "(session_id: abc-def-123)",
                client, config, sessions, queue,
            )

        assert captured_prompt is not None
        # The prompt should contain the handoff greeting, not be empty
        assert "handed off" in captured_prompt.lower()


class TestGitCommitUserMessageReaction:
    """Test git commit adds :package: to user's message in thread replies."""

    @pytest.mark.asyncio
    async def test_git_commit_adds_package_to_user_message_in_thread(self, config, sessions, queue):
        """Git commit in a thread reply should add :package: to the user's message."""
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "feat: add thing"')
            )
            yield make_event("assistant", text="Committed.")
            yield make_event("result", text="Committed.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "commit it", client, config, sessions, queue)

        # :package: should be added to user's message (ts=2000.0) too
        user_msg_package = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "package" and c.kwargs.get("timestamp") == "2000.0"
        ]
        assert len(user_msg_package) == 1

    @pytest.mark.asyncio
    async def test_git_commit_user_message_reaction_failure_swallowed(self, config, sessions, queue):
        """If adding :package: to user's message fails, it doesn't crash."""
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "fix"')
            )
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        # Make reactions_add fail for user's message but not thread root
        original_add = client.reactions_add

        async def selective_fail(**kwargs):
            if kwargs.get("timestamp") == "2000.0" and kwargs.get("name") == "package":
                raise Exception("already_reacted")
            return await original_add(**kwargs)

        client.reactions_add = AsyncMock(side_effect=selective_fail)

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            # Should not raise
            await _process_message(event, "commit", client, config, sessions, queue)

        # Text response should still be posted
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "Done." in c.kwargs.get("text", "")
        ]
        assert len(text_posts) == 1


class TestUnknownEventType:
    """Test that unknown event types are silently logged."""

    @pytest.mark.asyncio
    async def test_unknown_event_type_does_not_crash(self, config, sessions, queue):
        """An event with an unrecognized type should be logged and skipped."""
        async def fake_stream(prompt):
            yield make_event("unknown_type", subtype="weird")
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "9600.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should still post the result text
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == "done"
        ]
        assert len(text_posts) == 1


class TestStaleSessionContextInjectionFailure:
    """Test that stale session context injection failure is handled."""

    @pytest.mark.asyncio
    async def test_stale_session_run_failure_swallowed(self, config, sessions, queue):
        """When session.run() fails during stale context injection, it doesn't crash."""
        async def fake_stream(prompt):
            # Return a different session_id than requested (stale)
            yield make_event("system", subtype="init", session_id="new-sess-id")
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "new-sess-id"
        mock_session.run = AsyncMock(side_effect=Exception("context injection failed"))

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "7000.0", "text": "original"},
                {"user": "UBOT123", "ts": "7001.0", "text": "response"},
            ]
        }

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "7002.0",
                "thread_ts": "7000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            # Should not raise despite run() failing
            await _process_message(
                event,
                "continue (session_id: old-sess-id)",
                client, config, sessions, queue,
            )

        # Text should still be posted
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == "done"
        ]
        assert len(text_posts) == 1


class TestErrorHandlerEdgeCases:
    """Test error handler double-exception and reaction failure paths."""

    @pytest.mark.asyncio
    async def test_error_post_failure_swallowed(self, config, sessions, queue):
        """When chat_postMessage also fails during error handling, no crash."""
        async def exploding_stream(prompt):
            raise RuntimeError("stream broke")
            yield  # noqa: unreachable

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = mock_client()
        client.chat_postMessage.side_effect = Exception("Slack is down too")

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "9700.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            # Should not raise — double exception is swallowed
            await _process_message(event, "hello", client, config, sessions, queue)

    @pytest.mark.asyncio
    async def test_error_reaction_failure_swallowed(self, config, sessions, queue):
        """When reactions fail during error cleanup, no crash."""
        async def exploding_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            raise RuntimeError("kaboom")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = mock_client()
        client.reactions_remove.side_effect = Exception("rate_limited")
        client.reactions_add.side_effect = Exception("rate_limited")

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "9701.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            # Should not raise — reaction failures are swallowed
            await _process_message(event, "hello", client, config, sessions, queue)
