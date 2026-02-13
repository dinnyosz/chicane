"""Tests for _process_message core logic: formatting, error paths, reconnection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.handlers import _process_message
from tests.conftest import make_event, mock_client, mock_session_info


class TestProcessMessageFormatting:
    """Test that _process_message preserves newlines from streamed text."""

    @pytest.mark.asyncio
    async def test_streamed_text_with_newlines_not_overwritten_by_result(
        self, config, sessions
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
            await _process_message(event, "hello", client, config, sessions)

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
            await _process_message(event, "hello", client, config, sessions)

        final_update = client.chat_update.call_args_list[-1]
        assert final_update.kwargs["text"] == result_text


class TestProcessMessageEdgeCases:
    """Test _process_message error paths and edge cases."""

    @pytest.mark.asyncio
    async def test_handoff_session_id_extracted(self, config, sessions):
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
                client, config, sessions,
            )
            assert mock_create.call_args.kwargs["session_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_reaction_add_failure_doesnt_block(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        client.reactions_add.side_effect = Exception("permission denied")

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        client.chat_update.assert_called()

    @pytest.mark.asyncio
    async def test_empty_response_posts_warning(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        final_update = client.chat_update.call_args_list[-1]
        assert "empty response" in final_update.kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_stream_exception_posts_error(self, config, sessions):
        async def exploding_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            raise RuntimeError("stream exploded")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        error_update = client.chat_update.call_args_list[-1]
        assert ":x: Error:" in error_update.kwargs["text"]
        assert "stream exploded" in error_update.kwargs["text"]

    @pytest.mark.asyncio
    async def test_long_response_uploaded_as_snippet(self, config, sessions):
        """Responses exceeding SNIPPET_THRESHOLD are uploaded as a file snippet."""
        long_text = "a" * 8000

        async def fake_stream(prompt):
            yield make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Placeholder updated to indicate snippet
        assert client.chat_update.called
        update_text = client.chat_update.call_args.kwargs["text"]
        assert "snippet" in update_text.lower()

        # Snippet uploaded via files_upload_v2
        client.files_upload_v2.assert_called_once()
        upload_kwargs = client.files_upload_v2.call_args.kwargs
        assert upload_kwargs["content"] == long_text
        assert upload_kwargs["channel"] == "C_CHAN"

    @pytest.mark.asyncio
    async def test_moderate_response_split_into_chunks(self, config, sessions):
        """Responses between SLACK_MAX_LENGTH and SNIPPET_THRESHOLD still chunk."""
        # 3950 chars: above SLACK_MAX_LENGTH (3900) but below SNIPPET_THRESHOLD (4000)
        text = "a" * 3950

        async def fake_stream(prompt):
            yield make_event("result", text=text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.1", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Should be split into 2 messages, not uploaded as snippet
        assert client.chat_update.called
        client.files_upload_v2.assert_not_called()
        assert client.chat_postMessage.call_count >= 2

    @pytest.mark.asyncio
    async def test_text_only_response_updates_placeholder(self, config, sessions):
        """When there are no tool calls, the final text updates the placeholder."""
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
            await _process_message(event, "hello", client, config, sessions)

        assert client.chat_update.call_count == 1
        assert client.chat_update.call_args.kwargs["text"] == chunk_text

    @pytest.mark.asyncio
    async def test_reconnect_rebuilds_context(self, config, sessions):
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
            await _process_message(event, "follow up", client, config, sessions)

        assert captured_prompt is not None
        assert "conversation history" in captured_prompt
        assert "follow up" in captured_prompt

    @pytest.mark.asyncio
    async def test_reconnect_finds_session_id(self, config, sessions):
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
            await _process_message(event, "continue", client, config, sessions)

            assert mock_create.call_args.kwargs["session_id"] == "abc-123-def"
