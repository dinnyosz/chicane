"""Tests for thread-root emoji state machine.

The thread root message (first message in a thread) carries status emojis
visible from the channel's message list:

  :eyes:             — LLM is actively working
  :white_check_mark: — done, check the thread for results
  :x:                — error occurred
  :octagonal_sign:   — stream was interrupted by user

Transitions:
  new message → clear old state, add :eyes:
  completion  → swap :eyes: → :white_check_mark:
  error       → swap :eyes: → :x:
  interrupt   → swap :eyes: → :octagonal_sign:

These reactions only apply when thread_ts != event["ts"] (follow-up messages
in an existing thread). For the first message, the per-message reactions serve
the same purpose.
"""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from chicane.handlers import _process_message
from tests.conftest import make_event, mock_client, mock_session_info


def _reactions_by_name(client: AsyncMock, method: str, name: str, ts: str) -> int:
    """Count how many times a specific reaction was added/removed on a given ts."""
    mock_method = getattr(client, method)
    return sum(
        1
        for c in mock_method.call_args_list
        if c.kwargs.get("name") == name and c.kwargs.get("timestamp") == ts
    )


class TestThreadRootEyesOnStart:
    """On follow-up messages, :eyes: is added to the thread root."""

    @pytest.mark.asyncio
    async def test_eyes_added_to_thread_root(self, config, sessions):
        """Follow-up message adds :eyes: to thread_ts."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

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
            await _process_message(event, "follow up", client, config, sessions)

        # :eyes: added to both user message and thread root
        assert _reactions_by_name(client, "reactions_add", "eyes", "2000.0") >= 1
        assert _reactions_by_name(client, "reactions_add", "eyes", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_no_duplicate_eyes_on_first_message(self, config, sessions):
        """First message in thread (thread_ts == event ts) — no double :eyes:."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions)

        # :eyes: only added once (to event ts, which IS the thread root)
        assert _reactions_by_name(client, "reactions_add", "eyes", "1000.0") == 1


class TestThreadRootClearsPreviousState:
    """New messages clear previous status emojis from thread root."""

    @pytest.mark.asyncio
    async def test_checkmark_removed_on_new_message(self, config, sessions):
        """Previous :white_check_mark: is removed when a new message arrives."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

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
            await _process_message(event, "follow up", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "white_check_mark", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_error_x_removed_on_new_message(self, config, sessions):
        """Previous :x: is removed when a new message arrives."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

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
            await _process_message(event, "retry", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "x", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_stop_sign_removed_on_new_message(self, config, sessions):
        """Previous :octagonal_sign: is removed when a new message arrives."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

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
            await _process_message(event, "continue", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "octagonal_sign", "1000.0") >= 1


class TestThreadRootCheckmarkOnCompletion:
    """On successful completion, :white_check_mark: is added to thread root."""

    @pytest.mark.asyncio
    async def test_checkmark_added_to_thread_root(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event("result", text="all done")

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
            await _process_message(event, "do it", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "white_check_mark", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_eyes_removed_from_thread_root_on_completion(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event("result", text="done")

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
            await _process_message(event, "go", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "eyes", "1000.0") >= 1


class TestThreadRootErrorState:
    """On errors, :x: is added to thread root."""

    @pytest.mark.asyncio
    async def test_x_added_to_thread_root_on_error(self, config, sessions):
        async def exploding_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            raise RuntimeError("kaboom")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "break", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "x", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_eyes_removed_from_thread_root_on_error(self, config, sessions):
        async def exploding_stream(prompt):
            raise RuntimeError("boom")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "fail", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "eyes", "1000.0") >= 1


class TestThreadRootInterruptState:
    """On user interrupt, :octagonal_sign: replaces :eyes: on thread root."""

    @pytest.mark.asyncio
    async def test_stop_sign_on_thread_root_after_reaction_interrupt(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event("assistant", text="partial")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        mock_session.was_interrupted = True
        mock_session.interrupt_source = "reaction"
        mock_session.is_streaming = False
        mock_session.interrupt = AsyncMock()

        info = MagicMock()
        info.session = mock_session
        import asyncio
        info.lock = asyncio.Lock()

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "work", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "octagonal_sign", "1000.0") >= 1
        assert _reactions_by_name(client, "reactions_remove", "eyes", "1000.0") >= 1


class TestThreadRootFirstMessage:
    """First message in thread — thread_ts == event ts, no separate thread-root reactions."""

    @pytest.mark.asyncio
    async def test_first_message_no_thread_root_remove(self, config, sessions):
        """For the first message, we don't try to remove old reactions from thread root
        (since thread_ts == event ts, the per-message reactions serve as thread root)."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions)

        # Should not try to remove white_check_mark/x/octagonal_sign from thread root
        # separately — only the per-message swap (eyes → checkmark) happens
        removes = [
            c for c in client.reactions_remove.call_args_list
            if c.kwargs.get("timestamp") == "1000.0"
            and c.kwargs.get("name") in ("white_check_mark", "x", "octagonal_sign")
        ]
        # Only the eyes removal at completion should happen, not the cleanup loop
        assert len(removes) == 0


class TestThreadRootReactionFailuresIgnored:
    """Thread-root reaction failures don't break the flow."""

    @pytest.mark.asyncio
    async def test_reaction_api_errors_swallowed(self, config, sessions):
        """If the Slack API rejects reaction calls, processing continues."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        # Make all reaction calls fail
        client.reactions_add.side_effect = Exception("no_permission")
        client.reactions_remove.side_effect = Exception("not_found")

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            # Should complete without raising
            await _process_message(event, "hello", client, config, sessions)

        # The text response should still be posted
        client.chat_update.assert_called()
