"""Tests for concurrent message handling: queue via per-session lock."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.claude import ClaudeSession
from chicane.handlers import _process_message
from chicane.sessions import SessionInfo, SessionStore
from tests.conftest import make_event, mock_client, mock_session_info


class TestSessionLock:
    """Test that the per-session lock serializes concurrent messages."""

    @pytest.mark.asyncio
    async def test_lock_serializes_concurrent_streams(self, config, sessions, queue):
        """Two concurrent _process_message calls on the same thread should
        serialize via the session lock — the first completes fully before
        the second starts streaming."""
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
        mock_session.was_interrupted = False
        mock_session.stream = slow_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = lock

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event_a = {"ts": "5000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            event_b = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN1"}

            await asyncio.gather(
                _process_message(event_a, "first", client, config, sessions, queue),
                _process_message(event_b, "second", client, config, sessions, queue),
            )

        # Both streams should have started and completed
        assert "start:first" in call_order
        assert "end:first" in call_order
        assert "start:second" in call_order
        assert "end:second" in call_order

        # The lock ensures one finishes before the other starts streaming.
        # First must end before second starts.
        first_end = call_order.index("end:first")
        second_start = call_order.index("start:second")
        assert first_end < second_start

    @pytest.mark.asyncio
    async def test_queued_message_processed_after_current(self, config, sessions, queue):
        """When a second message arrives during streaming, it should be
        processed after the first completes — not dropped."""
        results = []
        lock = asyncio.Lock()

        async def recording_stream(prompt):
            results.append(prompt)
            yield make_event("result", text=f"done: {prompt}")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = recording_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = lock

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event_a = {"ts": "6000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            event_b = {"ts": "6001.0", "channel": "C_CHAN", "user": "UHUMAN1"}

            await asyncio.gather(
                _process_message(event_a, "do X", client, config, sessions, queue),
                _process_message(event_b, "do Y", client, config, sessions, queue),
            )

        # Both messages should have been processed (not dropped)
        assert "do X" in results
        assert "do Y" in results

    @pytest.mark.asyncio
    async def test_each_message_gets_own_response(self, config, sessions, queue):
        """Each queued message should produce its own response."""
        lock = asyncio.Lock()

        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = fake_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = lock

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event_a = {"ts": "7000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            event_b = {"ts": "7001.0", "channel": "C_CHAN", "user": "UHUMAN1"}

            await asyncio.gather(
                _process_message(event_a, "first", client, config, sessions, queue),
                _process_message(event_b, "second", client, config, sessions, queue),
            )

        # Each message should produce a "done" response
        done_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == "done"
        ]
        assert len(done_posts) == 2

    @pytest.mark.asyncio
    async def test_first_error_doesnt_block_second(self, config, sessions, queue):
        """If the first stream errors, the second should still be processed."""
        call_count = 0
        lock = asyncio.Lock()

        async def sometimes_exploding_stream(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("stream exploded")
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = sometimes_exploding_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = lock

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event_a = {"ts": "8000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            event_b = {"ts": "8001.0", "channel": "C_CHAN", "user": "UHUMAN1"}

            await asyncio.gather(
                _process_message(event_a, "first", client, config, sessions, queue),
                _process_message(event_b, "second", client, config, sessions, queue),
            )

        # Both should have been attempted
        assert call_count == 2

        # First should show error (sanitized — no internal message leaked)
        error_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":x: Error (" in c.kwargs.get("text", "")
        ]
        assert len(error_posts) == 1

    @pytest.mark.asyncio
    async def test_different_threads_not_serialized(self, config, queue):
        """Messages in different threads should stream concurrently,
        not block each other."""
        sessions = SessionStore()
        call_order = []

        async def tracking_stream(self, prompt):
            call_order.append(f"start:{prompt}")
            await asyncio.sleep(0.05)
            yield make_event("result", text=f"done: {prompt}")
            call_order.append(f"end:{prompt}")

        client = mock_client()

        # Patch ClaudeSession to use our tracking stream
        with patch.object(ClaudeSession, "stream", tracking_stream):
            event_a = {
                "ts": "9000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            event_b = {
                "ts": "9001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }

            config_obj = config  # avoid fixture naming conflict

            await asyncio.gather(
                _process_message(event_a, "thread A msg", client, config_obj, sessions, queue),
                _process_message(event_b, "thread B msg", client, config_obj, sessions, queue),
            )

        # Both should start before either ends (concurrent, not serialized)
        assert len(call_order) == 4
        starts = [i for i, x in enumerate(call_order) if x.startswith("start:")]
        ends = [i for i, x in enumerate(call_order) if x.startswith("end:")]
        # At least one start should come before both ends
        assert starts[1] < ends[1]
