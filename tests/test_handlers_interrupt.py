"""Tests for stream interrupt: stop emoji reaction + new-message preemption."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.handlers import register_handlers, _process_message
from chicane.sessions import SessionStore
from tests.conftest import (
    capture_app_handlers,
    make_event,
    mock_client,
    mock_session_info,
)


# ---------------------------------------------------------------------------
# Reaction handler tests
# ---------------------------------------------------------------------------


class TestReactionInterrupt:
    """Tests for the reaction_added handler that interrupts active streams."""

    def _setup_reaction_handler(self, config, sessions):
        """Register handlers and return the reaction_added handler."""
        mock_app = MagicMock()
        handlers = capture_app_handlers(mock_app)
        register_handlers(mock_app, config, sessions)
        return handlers["reaction_added"]

    @pytest.mark.asyncio
    async def test_stop_reaction_interrupts_active_stream(self, config, sessions):
        handler = self._setup_reaction_handler(config, sessions)
        client = mock_client()

        # Create a session and register a bot message
        info = sessions.get_or_create("thread-1", config)
        sessions.register_bot_message("bot-msg-1", "thread-1")
        info.session._is_streaming = True
        info.session.interrupt = AsyncMock()

        event = {
            "reaction": "octagonal_sign",
            "item": {"type": "message", "ts": "bot-msg-1", "channel": "C_CHAN"},
            "user": "UHUMAN",
        }
        await handler(event, client)

        info.session.interrupt.assert_awaited_once()
        # Should post an "Interrupted by user" message
        client.chat_postMessage.assert_called_once_with(
            channel="C_CHAN",
            thread_ts="thread-1",
            text=":stop_sign: _Interrupted by user_",
        )

    @pytest.mark.asyncio
    async def test_stop_reaction_ignored_when_not_streaming(self, config, sessions):
        handler = self._setup_reaction_handler(config, sessions)
        client = mock_client()

        info = sessions.get_or_create("thread-1", config)
        sessions.register_bot_message("bot-msg-1", "thread-1")
        info.session._is_streaming = False
        info.session.interrupt = AsyncMock()

        event = {
            "reaction": "octagonal_sign",
            "item": {"type": "message", "ts": "bot-msg-1", "channel": "C_CHAN"},
            "user": "UHUMAN",
        }
        await handler(event, client)

        info.session.interrupt.assert_not_awaited()
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_stop_reaction_ignored(self, config, sessions):
        handler = self._setup_reaction_handler(config, sessions)
        client = mock_client()

        info = sessions.get_or_create("thread-1", config)
        sessions.register_bot_message("bot-msg-1", "thread-1")
        info.session._is_streaming = True
        info.session.interrupt = AsyncMock()

        event = {
            "reaction": "thumbsup",
            "item": {"type": "message", "ts": "bot-msg-1", "channel": "C_CHAN"},
            "user": "UHUMAN",
        }
        await handler(event, client)

        info.session.interrupt.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_on_unknown_message_ignored(self, config, sessions):
        handler = self._setup_reaction_handler(config, sessions)
        client = mock_client()

        event = {
            "reaction": "octagonal_sign",
            "item": {"type": "message", "ts": "unknown-msg", "channel": "C_CHAN"},
            "user": "UHUMAN",
        }
        await handler(event, client)

        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_on_thread_starter(self, config, sessions):
        """Reacting on the thread starter message (which IS the thread_ts) should work."""
        handler = self._setup_reaction_handler(config, sessions)
        client = mock_client()

        info = sessions.get_or_create("thread-1", config)
        info.session._is_streaming = True
        info.session.interrupt = AsyncMock()

        # React on thread-1 itself (not a registered bot message, but the thread_ts)
        event = {
            "reaction": "octagonal_sign",
            "item": {"type": "message", "ts": "thread-1", "channel": "C_CHAN"},
            "user": "UHUMAN",
        }
        await handler(event, client)

        info.session.interrupt.assert_awaited_once()


# ---------------------------------------------------------------------------
# New-message interrupt tests
# ---------------------------------------------------------------------------


class TestNewMessageInterrupt:
    """Tests for new messages interrupting active streams."""

    @pytest.mark.asyncio
    async def test_new_message_interrupts_active_stream(self, config, sessions):
        """When a second message arrives while streaming, it should call interrupt()."""
        stream_started = asyncio.Event()
        interrupt_called = asyncio.Event()

        async def slow_stream(prompt):
            stream_started.set()
            # Wait a bit to simulate a long-running stream
            await asyncio.sleep(0.1)
            yield make_event("result", text=f"result for {prompt}")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = slow_stream

        original_interrupt = AsyncMock()

        async def tracking_interrupt(source="reaction"):
            mock_session.was_interrupted = True
            mock_session.interrupt_source = source
            await original_interrupt()
            interrupt_called.set()

        mock_session.interrupt = tracking_interrupt

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        async def start_first_and_then_second():
            """Start first message, wait for it to begin streaming, then send second."""
            event_a = {"ts": "5000.0", "channel": "C_CHAN", "user": "UHUMAN"}

            async def send_second():
                await stream_started.wait()
                # Now the first message is streaming — mark is_streaming
                mock_session.is_streaming = True
                event_b = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN"}
                with patch.object(sessions, "get_or_create", return_value=info):
                    await _process_message(event_b, "second", client, config, sessions)

            with patch.object(sessions, "get_or_create", return_value=info):
                await asyncio.gather(
                    _process_message(event_a, "first", client, config, sessions),
                    send_second(),
                )

        await start_first_and_then_second()

        # interrupt() should have been called when the second message arrived
        original_interrupt.assert_awaited()


# ---------------------------------------------------------------------------
# Interrupted stream display tests
# ---------------------------------------------------------------------------


class TestInterruptedStreamDisplay:
    """Tests for how interrupted streams are displayed in Slack."""

    @pytest.mark.asyncio
    async def test_interrupted_stream_shows_partial_text(self, config, sessions):
        """Interrupted stream with accumulated text shows text + stop indicator."""
        async def interrupting_stream(prompt):
            yield make_event("assistant", text="Partial response")
            yield make_event("result", text="Partial response")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = True
        mock_session.interrupt_source = "reaction"
        mock_session.stream = interrupting_stream
        mock_session.interrupt = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "do something", client, config, sessions)

        # The placeholder should be updated with partial text + interrupted indicator
        update_calls = client.chat_update.call_args_list
        interrupted_update = [
            c for c in update_calls
            if ":stop_sign: _Interrupted_" in c.kwargs.get("text", "")
        ]
        assert len(interrupted_update) == 1
        assert "Partial response" in interrupted_update[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_interrupted_stream_no_text_shows_indicator(self, config, sessions):
        """Interrupted stream with no text shows just the stop indicator."""
        async def empty_stream(prompt):
            # Only tool activity, no text
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = True
        mock_session.interrupt_source = "reaction"
        mock_session.stream = empty_stream
        mock_session.interrupt = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "2000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "do something", client, config, sessions)

        # Placeholder should be updated to just the interrupted indicator
        update_calls = client.chat_update.call_args_list
        interrupted_update = [
            c for c in update_calls
            if c.kwargs.get("text") == ":stop_sign: _Interrupted_"
        ]
        assert len(interrupted_update) == 1

    @pytest.mark.asyncio
    async def test_interrupted_stream_swaps_eyes_to_stop(self, config, sessions):
        """Interrupted stream should swap eyes reaction to stop sign."""
        async def quick_stream(prompt):
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = True
        mock_session.interrupt_source = "reaction"
        mock_session.stream = quick_stream
        mock_session.interrupt = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "3000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "do something", client, config, sessions)

        # Should remove eyes and add stop sign
        client.reactions_remove.assert_called_with(
            channel="C_CHAN", name="eyes", timestamp="3000.0"
        )
        client.reactions_add.assert_called_with(
            channel="C_CHAN", name="octagonal_sign", timestamp="3000.0"
        )

    @pytest.mark.asyncio
    async def test_interrupted_stream_skips_completion_summary(self, config, sessions):
        """Interrupted stream should NOT post a completion summary."""
        async def stream_with_result(prompt):
            yield make_event("assistant", text="Some text")
            yield make_event("result", text="Some text", num_turns=3, duration_ms=5000)

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = True
        mock_session.interrupt_source = "reaction"
        mock_session.stream = stream_with_result
        mock_session.interrupt = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "4000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "do something", client, config, sessions)

        # No completion summary (checkered_flag) should be posted
        post_calls = client.chat_postMessage.call_args_list
        summary_posts = [
            c for c in post_calls
            if ":checkered_flag:" in c.kwargs.get("text", "")
        ]
        assert len(summary_posts) == 0

        # No checkmark reaction either
        checkmark_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "white_check_mark"
        ]
        assert len(checkmark_calls) == 0

    @pytest.mark.asyncio
    async def test_new_message_interrupt_stays_silent(self, config, sessions):
        """New-message interrupt should NOT post stop indicator or swap reactions."""
        async def some_stream(prompt):
            yield make_event("assistant", text="Partial work")
            yield make_event("result", text="Partial work")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = True
        mock_session.interrupt_source = "new_message"
        mock_session.stream = some_stream
        mock_session.interrupt = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "6000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "do something", client, config, sessions)

        # Should NOT post any ":stop_sign:" messages
        all_texts = [
            c.kwargs.get("text", "")
            for c in client.chat_postMessage.call_args_list
        ] + [
            c.kwargs.get("text", "")
            for c in client.chat_update.call_args_list
        ]
        stop_messages = [t for t in all_texts if ":stop_sign:" in t]
        assert len(stop_messages) == 0

        # Should NOT add octagonal_sign reaction
        stop_reactions = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "octagonal_sign"
        ]
        assert len(stop_reactions) == 0

        # Should still remove eyes
        client.reactions_remove.assert_called_with(
            channel="C_CHAN", name="eyes", timestamp="6000.0"
        )

        # Placeholder should show "New message received" not stop sign
        update_calls = client.chat_update.call_args_list
        forward_update = [
            c for c in update_calls
            if ":fast_forward:" in c.kwargs.get("text", "")
        ]
        assert len(forward_update) == 1
