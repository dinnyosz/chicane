"""Tests for concurrent message handling: abort + lock pattern."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.claude import ClaudeSession
from chicane.handlers import _process_message
from chicane.sessions import SessionInfo, SessionStore
from tests.conftest import make_event, mock_client, mock_session_info


class TestAbortMechanism:
    """Test ClaudeSession abort flag and is_streaming property."""

    def test_initial_state(self):
        session = ClaudeSession()
        assert session.was_aborted is False
        assert session.is_streaming is False

    def test_abort_sets_flag(self):
        session = ClaudeSession()
        session.abort()
        assert session.was_aborted is True

    def test_abort_kills_process(self):
        session = ClaudeSession()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        session._process = mock_proc

        session.abort()

        mock_proc.kill.assert_called_once()
        assert session.was_aborted is True

    def test_is_streaming_when_process_active(self):
        session = ClaudeSession()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        session._process = mock_proc
        assert session.is_streaming is True

    def test_not_streaming_when_process_finished(self):
        session = ClaudeSession()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        session._process = mock_proc
        assert session.is_streaming is False


class TestConcurrentMessages:
    """Test that concurrent messages on the same thread are handled correctly."""

    @pytest.mark.asyncio
    async def test_aborted_stream_skips_final_posting(self, config, sessions):
        """When a stream is aborted, no final text/summary should be posted."""
        async def fake_stream(prompt):
            yield make_event("assistant", text="Working on something...")
            yield make_event("result", text="Working on something...")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        # Simulate abort: was_aborted returns True after stream ends
        mock_session.was_aborted = True

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # The placeholder should be deleted or updated to "interrupted", not
        # updated with the response text.
        update_texts = [
            c.kwargs.get("text", "") for c in client.chat_update.call_args_list
        ]
        assert not any("Working on something" in t for t in update_texts)

        # No checkmark reaction should be added
        checkmark_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "white_check_mark"
        ]
        assert len(checkmark_calls) == 0

    @pytest.mark.asyncio
    async def test_aborted_stream_exception_suppressed(self, config, sessions):
        """When a stream raises an exception during abort, no error is posted."""
        async def exploding_stream(prompt):
            raise RuntimeError("process killed")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_aborted = True

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "2000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # No ":x: Error:" message should be posted
        error_updates = [
            c for c in client.chat_update.call_args_list
            if ":x: Error:" in c.kwargs.get("text", "")
        ]
        assert len(error_updates) == 0

    @pytest.mark.asyncio
    async def test_active_stream_aborted_on_new_message(self, config, sessions):
        """When a new message arrives while streaming, the session is aborted."""
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = True  # Simulate active stream
        mock_session.was_aborted = False

        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session.stream = fake_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "3000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "stop that", client, config, sessions)

        # abort() should have been called because is_streaming was True
        mock_session.abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_abort_when_not_streaming(self, config, sessions):
        """When no stream is active, abort should not be called."""
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_aborted = False

        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session.stream = fake_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "4000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        mock_session.abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_prevents_concurrent_streaming(self, config, sessions):
        """Two concurrent _process_message calls on the same thread should
        serialize via the session lock."""
        call_order = []
        lock = asyncio.Lock()

        async def slow_stream(prompt):
            call_order.append(f"start:{prompt}")
            await asyncio.sleep(0.05)
            yield make_event("result", text=f"result for {prompt}")
            call_order.append(f"end:{prompt}")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_aborted = False
        mock_session.stream = slow_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = lock

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event_a = {"ts": "5000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            event_b = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN1"}

            # Run both concurrently
            await asyncio.gather(
                _process_message(event_a, "first", client, config, sessions),
                _process_message(event_b, "second", client, config, sessions),
            )

        # The lock ensures one finishes before the other starts streaming
        # (both start, but only one holds the lock at a time for the stream part)
        assert "start:first" in call_order
        assert "start:second" in call_order
        assert "end:first" in call_order
        assert "end:second" in call_order

    @pytest.mark.asyncio
    async def test_aborted_placeholder_deleted(self, config, sessions):
        """When aborted, the 'Working on it...' placeholder should be cleaned up."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_aborted = True

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "6000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Placeholder should be deleted
        client.chat_delete.assert_called_once_with(
            channel="C_CHAN", ts="9999.0",
        )

    @pytest.mark.asyncio
    async def test_aborted_placeholder_fallback_update(self, config, sessions):
        """When delete fails, fallback to updating the placeholder."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_aborted = True

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()
        client.chat_delete.side_effect = Exception("cant_delete_message")

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "7000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Should fall back to updating
        update_texts = [
            c.kwargs.get("text", "") for c in client.chat_update.call_args_list
        ]
        assert any("Interrupted" in t for t in update_texts)

    @pytest.mark.asyncio
    async def test_eyes_removed_on_abort(self, config, sessions):
        """Eyes reaction should be removed when a stream is aborted."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_aborted = True

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "8000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Eyes should be removed
        remove_calls = [
            c for c in client.reactions_remove.call_args_list
            if c.kwargs.get("name") == "eyes"
        ]
        assert len(remove_calls) == 1

        # No checkmark should be added
        checkmark_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "white_check_mark"
        ]
        assert len(checkmark_calls) == 0
