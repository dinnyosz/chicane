"""Tests for the idle flush timer — pending output (text/activities) is auto-posted when the SDK blocks."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from chicane.handlers import _process_message, _IDLE_FLUSH_DELAY
from tests.conftest import make_event, make_tool_event, tool_block, mock_client, mock_session_info


class TestIdleFlush:
    """Verify that pending output (text and activities) is auto-flushed after a delay."""

    @pytest.mark.asyncio
    async def test_text_flushed_during_long_sdk_block(self, config, sessions, queue):
        """Text accumulated before a long SDK pause should be posted by the timer.

        Simulates the real scenario: Claude emits text + tool_use in one assistant
        event, then the SDK blocks for a long time while the tool runs.
        """
        barrier = asyncio.Event()
        text_yielded = asyncio.Event()

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            # Text arrives *before* the tool use, so it accumulates in full_text
            yield make_event("assistant", text="Now the big one — tree.py:")
            text_yielded.set()
            # SDK blocks here while the tool runs (we simulate with a barrier)
            await barrier.wait()
            # Tool activity arrives after the block
            yield make_tool_event(tool_block("Write", file_path="/src/tree.py"))
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            task = asyncio.create_task(
                _process_message(event, "refactor", client, config, sessions, queue)
            )
            # Wait until the text event has been yielded and processed
            await text_yielded.wait()
            # asyncio.sleep is mocked to be instant in tests, so the timer
            # fires as soon as we yield control to the event loop.
            # Give multiple iterations for the timer task to run and post.
            for _ in range(10):
                await asyncio.sleep(0)

            # The text should have been auto-flushed by the timer
            text_posts = [
                c for c in client.chat_postMessage.call_args_list
                if "big one" in c.kwargs.get("text", "")
            ]
            assert len(text_posts) == 1, (
                f"Expected timed flush to post text, but got {len(text_posts)} posts. "
                f"All calls: {[c.kwargs.get('text', '')[:60] for c in client.chat_postMessage.call_args_list]}"
            )

            # Unblock the stream so the test completes
            barrier.set()
            await task

    @pytest.mark.asyncio
    async def test_timer_cancelled_on_normal_text_flush(self, config, sessions, queue):
        """When text is flushed normally (tool activity arrives), no duplicate post."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text="Checking the file...")
            # Tool activity arrives quickly — text should be flushed normally
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "check", client, config, sessions, queue)

        # Text "Checking the file..." should appear exactly once (not duplicated)
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "Checking the file" in c.kwargs.get("text", "")
        ]
        assert len(text_posts) == 1

    @pytest.mark.asyncio
    async def test_timer_cancelled_on_result_event(self, config, sessions, queue):
        """Timer is cancelled when result event arrives — no duplicate text post."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text="Here is my analysis.")
            yield make_event("result", text="Here is my analysis.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "analyze", client, config, sessions, queue)

        # "Here is my analysis" should appear exactly once
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "analysis" in c.kwargs.get("text", "")
        ]
        assert len(text_posts) == 1

    @pytest.mark.asyncio
    async def test_timer_cancelled_on_error(self, config, sessions, queue):
        """Timer is properly cancelled when an error occurs during streaming."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text="Starting work...")
            raise RuntimeError("SDK crashed")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "work", client, config, sessions, queue)

        # Should not see duplicate text — error handler should cancel timer
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "Starting work" in c.kwargs.get("text", "")
        ]
        # Text may or may not have been flushed by timer before the error,
        # but it should NOT appear more than once
        assert len(text_posts) <= 1

    @pytest.mark.asyncio
    async def test_idle_flush_delay_constant_exists(self):
        """Verify the constant is defined and reasonable."""
        assert _IDLE_FLUSH_DELAY > 0
        assert _IDLE_FLUSH_DELAY <= 30  # not too long

    @pytest.mark.asyncio
    async def test_multiple_text_chunks_accumulated_before_flush(
        self, config, sessions, queue
    ):
        """Multiple text chunks should be concatenated and flushed as one message."""
        barrier = asyncio.Event()
        text_yielded = asyncio.Event()

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text="Part one. ")
            yield make_event("assistant", text="Part two.")
            text_yielded.set()
            # SDK blocks
            await barrier.wait()
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            task = asyncio.create_task(
                _process_message(event, "go", client, config, sessions, queue)
            )
            await text_yielded.wait()
            for _ in range(10):
                await asyncio.sleep(0)

            # Both parts should be in a single post
            text_posts = [
                c for c in client.chat_postMessage.call_args_list
                if "Part one" in c.kwargs.get("text", "")
            ]
            assert len(text_posts) == 1
            assert "Part two" in text_posts[0].kwargs["text"]

            barrier.set()
            await task

    @pytest.mark.asyncio
    async def test_timer_resets_on_new_text(self, config, sessions, queue):
        """Each new text chunk should reset the flush timer (no premature flush)."""
        barrier = asyncio.Event()
        text_received = asyncio.Event()

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text="First ")
            # Since asyncio.sleep is mocked to 0 in tests, the timer fires
            # instantly. But the timer is reset by each new text chunk.
            yield make_event("assistant", text="Second")
            text_received.set()
            await barrier.wait()
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            task = asyncio.create_task(
                _process_message(event, "go", client, config, sessions, queue)
            )
            await text_received.wait()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            # Both "First " and "Second" should be in the flushed text,
            # proving the timer reset properly concatenated them.
            text_posts = [
                c for c in client.chat_postMessage.call_args_list
                if "First " in c.kwargs.get("text", "")
                and "Second" in c.kwargs.get("text", "")
            ]
            assert len(text_posts) >= 1

            barrier.set()
            await task

    @pytest.mark.asyncio
    async def test_pending_activities_flushed_during_long_sdk_block(
        self, config, sessions, queue
    ):
        """Batched tool activities should be auto-flushed when the SDK blocks."""
        barrier = asyncio.Event()
        tools_yielded = asyncio.Event()

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            # First tool posts immediately, second goes into pending batch
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_tool_event(tool_block("Read", file_path="/src/b.py"))
            tools_yielded.set()
            # SDK blocks while Claude thinks
            await barrier.wait()
            yield make_event("assistant", text="Done reviewing.")
            yield make_event("result", text="Done reviewing.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            task = asyncio.create_task(
                _process_message(event, "review", client, config, sessions, queue)
            )
            await tools_yielded.wait()
            for _ in range(10):
                await asyncio.sleep(0)

            # The second tool activity (b.py) should have been flushed by the timer
            activity_posts = [
                c for c in client.chat_postMessage.call_args_list
                if "b.py" in c.kwargs.get("text", "")
            ]
            assert len(activity_posts) >= 1, (
                f"Expected idle flush to post batched activity for b.py. "
                f"All calls: {[c.kwargs.get('text', '')[:80] for c in client.chat_postMessage.call_args_list]}"
            )

            barrier.set()
            await task
