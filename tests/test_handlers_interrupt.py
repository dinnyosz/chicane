"""Tests for stream interrupt: stop emoji reaction + new-message queueing."""

import asyncio
from collections import deque
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

    def _setup_reaction_handler(self, config, sessions, queue):
        """Register handlers and return the reaction_added handler."""
        mock_app = MagicMock()
        handlers = capture_app_handlers(mock_app)
        register_handlers(mock_app, config, sessions)
        return handlers["reaction_added"]

    @pytest.mark.asyncio
    async def test_stop_reaction_interrupts_active_stream(self, config, sessions, queue):
        handler = self._setup_reaction_handler(config, sessions, queue)
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
    async def test_stop_reaction_ignored_when_not_streaming(self, config, sessions, queue):
        handler = self._setup_reaction_handler(config, sessions, queue)
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
    async def test_non_stop_reaction_ignored(self, config, sessions, queue):
        handler = self._setup_reaction_handler(config, sessions, queue)
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
    async def test_reaction_on_unknown_message_ignored(self, config, sessions, queue):
        handler = self._setup_reaction_handler(config, sessions, queue)
        client = mock_client()

        event = {
            "reaction": "octagonal_sign",
            "item": {"type": "message", "ts": "unknown-msg", "channel": "C_CHAN"},
            "user": "UHUMAN",
        }
        await handler(event, client)

        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_on_thread_starter(self, config, sessions, queue):
        """Reacting on the thread starter message (which IS the thread_ts) should work."""
        handler = self._setup_reaction_handler(config, sessions, queue)
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


class TestNewMessageQueue:
    """Tests for new messages being queued behind active streams."""

    @pytest.mark.asyncio
    async def test_new_message_queued_when_streaming(self, config, sessions, queue):
        """When a second message arrives while streaming, it should be sent via
        queue_message() for between-turn delivery."""
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = True  # Stream is active
        mock_session.was_interrupted = False
        mock_session.interrupt = AsyncMock()
        mock_session.queue_message = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        event = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN"}
        with patch.object(sessions, "get_or_create", return_value=info):
            await _process_message(event, "second message", client, config, sessions, queue)

        # interrupt() should NOT have been called
        mock_session.interrupt.assert_not_awaited()

        # Message should have been delivered via queue_message for between-turn delivery
        mock_session.queue_message.assert_awaited_once_with("second message")
        # NOT added to pending_messages — between-turn delivery only
        assert len(info.pending_messages) == 0

    @pytest.mark.asyncio
    async def test_queued_message_gets_speech_balloon(self, config, sessions, queue):
        """Queued messages should get speech_balloon reaction (eyes swapped out)."""
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = True
        mock_session.was_interrupted = False
        mock_session.queue_message = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN"}
        with patch.object(sessions, "get_or_create", return_value=info):
            await _process_message(event, "queued msg", client, config, sessions, queue)

        # eyes added first, then removed (queue path), then speech_balloon added
        add_calls = client.reactions_add.call_args_list
        speech_calls = [c for c in add_calls if c.kwargs.get("name") == "speech_balloon"]
        assert len(speech_calls) == 1

    @pytest.mark.asyncio
    async def test_multiple_messages_queue_in_order(self, config, sessions, queue):
        """Multiple messages during streaming should all be sent via queue_message()."""
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = True
        mock_session.was_interrupted = False
        mock_session.queue_message = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        for i, msg in enumerate(["first", "second", "third"]):
            event = {"ts": f"600{i}.0", "channel": "C_CHAN", "user": "UHUMAN"}
            with patch.object(sessions, "get_or_create", return_value=info):
                await _process_message(event, msg, client, config, sessions, queue)

        assert mock_session.queue_message.await_count == 3
        queued = [c.args[0] for c in mock_session.queue_message.call_args_list]
        assert queued == ["first", "second", "third"]
        # NOT added to pending_messages — between-turn delivery only
        assert len(info.pending_messages) == 0


# ---------------------------------------------------------------------------
# Interrupted stream display tests
# ---------------------------------------------------------------------------


class TestInterruptedStreamDisplay:
    """Tests for how interrupted streams are displayed in Slack."""

    @pytest.mark.asyncio
    async def test_interrupted_stream_shows_partial_text(self, config, sessions, queue):
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
            await _process_message(event, "do something", client, config, sessions, queue)

        # Partial text and stop indicator should be posted as thread replies
        post_calls = client.chat_postMessage.call_args_list
        text_posts = [
            c for c in post_calls
            if "Partial response" in c.kwargs.get("text", "")
        ]
        assert len(text_posts) == 1
        stop_posts = [
            c for c in post_calls
            if c.kwargs.get("text") == ":stop_sign: _Interrupted_"
        ]
        assert len(stop_posts) == 1

    @pytest.mark.asyncio
    async def test_interrupted_stream_no_text_shows_indicator(self, config, sessions, queue):
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
            await _process_message(event, "do something", client, config, sessions, queue)

        # Stop indicator should be posted as a thread reply
        post_calls = client.chat_postMessage.call_args_list
        stop_posts = [
            c for c in post_calls
            if c.kwargs.get("text") == ":stop_sign: _Interrupted_"
        ]
        assert len(stop_posts) == 1

    @pytest.mark.asyncio
    async def test_interrupted_stream_swaps_eyes_to_stop(self, config, sessions, queue):
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
            await _process_message(event, "do something", client, config, sessions, queue)

        # Should remove eyes and add stop sign
        client.reactions_remove.assert_called_with(
            channel="C_CHAN", name="eyes", timestamp="3000.0"
        )
        client.reactions_add.assert_called_with(
            channel="C_CHAN", name="octagonal_sign", timestamp="3000.0"
        )

    @pytest.mark.asyncio
    async def test_interrupted_stream_skips_completion_summary(self, config, sessions, queue):
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
            await _process_message(event, "do something", client, config, sessions, queue)

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
    async def test_queued_message_does_not_trigger_interrupt_display(self, config, sessions, queue):
        """Messages queued during streaming should not show any interrupt indicators."""
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = True
        mock_session.was_interrupted = False
        mock_session.queue_message = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "6000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "do something", client, config, sessions, queue)

        # Should NOT post any ":stop_sign:" or ":bulb:" messages
        all_texts = [
            c.kwargs.get("text", "")
            for c in client.chat_postMessage.call_args_list
        ]
        stop_messages = [t for t in all_texts if ":stop_sign:" in t]
        assert len(stop_messages) == 0
        bulb_messages = [t for t in all_texts if ":bulb:" in t]
        assert len(bulb_messages) == 0

        # Message should have been delivered via queue_message
        mock_session.queue_message.assert_awaited_once_with("do something")


class TestInterruptExceptionHandling:
    """Test that exception handling during interrupt reaction swaps doesn't crash."""

    @pytest.mark.asyncio
    async def test_queued_message_reaction_remove_failure(self, config, sessions, queue):
        """Queueing a message: reactions_remove failure is swallowed."""
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = True
        mock_session.was_interrupted = False
        mock_session.queue_message = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()
        client.reactions_remove.side_effect = Exception("not_found")

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "7000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            # Should not raise despite reactions_remove failing
            await _process_message(event, "queued msg", client, config, sessions, queue)

        # Message should still be delivered via queue_message
        mock_session.queue_message.assert_awaited_once_with("queued msg")
        # speech_balloon should still be attempted
        speech_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "speech_balloon"
        ]
        assert len(speech_calls) == 1

    @pytest.mark.asyncio
    async def test_reaction_interrupt_reaction_swap_failure(self, config, sessions, queue):
        """Reaction interrupt: reactions_remove/add failure is swallowed."""
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
        client.reactions_remove.side_effect = Exception("not_found")
        client.reactions_add.side_effect = Exception("already_reacted")

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "7001.0", "channel": "C_CHAN", "user": "UHUMAN"}
            # Should not raise
            await _process_message(event, "work", client, config, sessions, queue)

        # Should still post the stop indicator
        stop_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":stop_sign: _Interrupted_" in c.kwargs.get("text", "")
        ]
        assert len(stop_posts) == 1


# ---------------------------------------------------------------------------
# Queue drain tests
# ---------------------------------------------------------------------------


class TestQueueDrain:
    """Tests for draining queued messages after stream completion."""

    @pytest.mark.asyncio
    async def test_queued_message_drained_after_completion(self, config, sessions, queue):
        """A message queued during streaming should be processed after the stream ends."""
        prompts_seen = []

        async def recording_stream(prompt):
            prompts_seen.append(prompt)
            yield make_event("result", text=f"done: {prompt}")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = recording_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        # Pre-queue a message as if it arrived during an active stream
        queued_event = {"ts": "9001.0", "channel": "C_CHAN", "user": "UHUMAN"}
        info.pending_messages.append({
            "event": queued_event,
            "prompt": "follow-up question",
            "client": client,
        })

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "9000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "initial question", client, config, sessions, queue)

        # Both the initial and queued messages should have been streamed
        assert "initial question" in prompts_seen
        assert "follow-up question" in prompts_seen
        assert prompts_seen.index("initial question") < prompts_seen.index("follow-up question")

    @pytest.mark.asyncio
    async def test_multiple_queued_messages_drained_in_order(self, config, sessions, queue):
        """Multiple queued messages should drain in FIFO order."""
        prompts_seen = []

        async def recording_stream(prompt):
            prompts_seen.append(prompt)
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = recording_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        # Pre-queue two messages
        for i, msg in enumerate(["second", "third"]):
            info.pending_messages.append({
                "event": {"ts": f"900{i+1}.0", "channel": "C_CHAN", "user": "UHUMAN"},
                "prompt": msg,
                "client": client,
            })

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "9000.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "first", client, config, sessions, queue)

        assert prompts_seen == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_queued_message_speech_balloon_removed(self, config, sessions, queue):
        """When a queued message is drained, its speech_balloon reaction should be removed."""
        async def fast_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = fast_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        queued_event = {"ts": "9010.0", "channel": "C_CHAN", "user": "UHUMAN"}
        info.pending_messages.append({
            "event": queued_event,
            "prompt": "queued",
            "client": client,
        })

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "9009.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "initial", client, config, sessions, queue)

        # speech_balloon should have been removed from the queued message
        remove_calls = [
            c for c in client.reactions_remove.call_args_list
            if c.kwargs.get("name") == "speech_balloon"
            and c.kwargs.get("timestamp") == "9010.0"
        ]
        assert len(remove_calls) == 1

    @pytest.mark.asyncio
    async def test_queue_empty_after_drain(self, config, sessions, queue):
        """After draining, the pending_messages queue should be empty."""
        async def fast_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.is_streaming = False
        mock_session.was_interrupted = False
        mock_session.stream = fast_stream

        info = MagicMock()
        info.session = mock_session
        info.lock = asyncio.Lock()
        info.pending_messages = deque()

        client = mock_client()

        info.pending_messages.append({
            "event": {"ts": "9021.0", "channel": "C_CHAN", "user": "UHUMAN"},
            "prompt": "queued",
            "client": client,
        })

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {"ts": "9020.0", "channel": "C_CHAN", "user": "UHUMAN"}
            await _process_message(event, "initial", client, config, sessions, queue)

        assert len(info.pending_messages) == 0
