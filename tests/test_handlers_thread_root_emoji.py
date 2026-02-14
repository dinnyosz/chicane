"""Tests for thread-root emoji state machine.

The thread root message (first message in a thread) carries status emojis
visible from the channel's message list:

  :eyes:             — LLM is actively working
  :white_check_mark: — done, check the thread for results
  :x:                — error occurred
  :octagonal_sign:   — stream was interrupted by user
  :speech_balloon:   — Claude is asking the user a question
  :hourglass:        — message queued behind active stream lock
  :warning:          — completed but permissions were denied

Transitions:
  new message → clear old state, add :eyes:
  completion  → swap :eyes: → :white_check_mark:
  error       → swap :eyes: → :x:
  interrupt   → swap :eyes: → :octagonal_sign:
  question    → add :speech_balloon: (alongside :eyes:)
  queued      → add :hourglass: while waiting for lock
  denials     → add :warning: alongside :white_check_mark:

These reactions only apply when thread_ts != event["ts"] (follow-up messages
in an existing thread). For the first message, the per-message reactions serve
the same purpose.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.handlers import _process_message, _has_question, _text_ends_with_question
from tests.conftest import (
    make_event,
    make_tool_event,
    mock_client,
    mock_session_info,
    tool_block,
)


def _reactions_by_name(client: AsyncMock, method: str, name: str, ts: str) -> int:
    """Count how many times a specific reaction was added/removed on a given ts."""
    mock_method = getattr(client, method)
    return sum(
        1
        for c in mock_method.call_args_list
        if c.kwargs.get("name") == name and c.kwargs.get("timestamp") == ts
    )


# ---------------------------------------------------------------------------
# Core state machine (eyes / checkmark / error / interrupt)
# ---------------------------------------------------------------------------


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
        info = mock_session_info(mock_session)
        # Simulate that checkmark was left from a previous completion
        info.thread_reactions.add("white_check_mark")

        with patch.object(sessions, "get_or_create", return_value=info):
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
        info = mock_session_info(mock_session)
        info.thread_reactions.add("x")

        with patch.object(sessions, "get_or_create", return_value=info):
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
        info = mock_session_info(mock_session)
        info.thread_reactions.add("octagonal_sign")

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "octagonal_sign", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_speech_balloon_removed_on_new_message(self, config, sessions):
        """Previous :speech_balloon: is removed when a new message arrives."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        info = mock_session_info(mock_session)
        info.thread_reactions.add("speech_balloon")

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "answer", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "speech_balloon", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_warning_removed_on_new_message(self, config, sessions):
        """Previous :warning: is removed when a new message arrives."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        info = mock_session_info(mock_session)
        info.thread_reactions.add("warning")

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "retry", client, config, sessions)

        assert _reactions_by_name(client, "reactions_remove", "warning", "1000.0") >= 1



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

    @pytest.mark.asyncio
    async def test_speech_balloon_removed_on_completion(self, config, sessions):
        """If :speech_balloon: was added during stream, it's cleared at completion."""
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("AskUserQuestion",
                           questions=[{"question": "What?", "options": []}])
            )
            yield make_event("result", text="ok")

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
            await _process_message(event, "do", client, config, sessions)

        # speech_balloon should be added during stream then removed at completion
        assert _reactions_by_name(client, "reactions_add", "speech_balloon", "1000.0") >= 1
        assert _reactions_by_name(client, "reactions_remove", "speech_balloon", "1000.0") >= 1


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

        info = mock_session_info(mock_session)
        # Override defaults *after* mock_session_info sets them
        mock_session.was_interrupted = True
        mock_session.interrupt_source = "reaction"

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


# ---------------------------------------------------------------------------
# :speech_balloon: — Claude asks a question (AskUserQuestion tool)
# ---------------------------------------------------------------------------


class TestHasQuestion:
    """Unit tests for the _has_question helper."""

    def test_detects_ask_user_question(self):
        event = make_tool_event(
            tool_block("AskUserQuestion",
                       questions=[{"question": "Pick one?", "options": []}])
        )
        assert _has_question(event) is True

    def test_ignores_other_tools(self):
        event = make_tool_event(tool_block("Read", file_path="/tmp/x"))
        assert _has_question(event) is False

    def test_ignores_text_only(self):
        event = make_event("assistant", text="just text")
        assert _has_question(event) is False


class TestThreadRootSpeechBalloon:
    """AskUserQuestion adds :speech_balloon: to thread root."""

    @pytest.mark.asyncio
    async def test_speech_balloon_added_when_question_asked(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("AskUserQuestion",
                           questions=[{"question": "Which?", "options": []}])
            )
            yield make_event("result", text="ok")

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
            await _process_message(event, "do stuff", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "speech_balloon", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_no_speech_balloon_without_question(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/tmp/x"))
            yield make_event("result", text="ok")

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
            await _process_message(event, "read file", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "speech_balloon", "1000.0") == 0


# ---------------------------------------------------------------------------
# :hourglass: — message queued behind the session lock
# ---------------------------------------------------------------------------


class TestThreadRootHourglass:
    """Hourglass is shown when a message is queued behind the lock."""

    @pytest.mark.asyncio
    async def test_hourglass_added_when_lock_contended(self, config, sessions):
        """When the lock is already held, :hourglass: is added then removed."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        mock_session.was_interrupted = False
        mock_session.is_streaming = False
        mock_session.interrupt_source = None
        mock_session.interrupt = AsyncMock()

        info = mock_session_info(mock_session)

        client = mock_client()

        # Pre-acquire the lock so it appears contended
        await info.lock.acquire()

        # Track when hourglass is added so we can release the lock
        hourglass_added = asyncio.Event()
        original_reactions_add = client.reactions_add

        async def track_hourglass(**kwargs):
            result = await original_reactions_add(**kwargs)
            if kwargs.get("name") == "hourglass":
                hourglass_added.set()
            return result

        client.reactions_add = AsyncMock(side_effect=track_hourglass)

        async def run_process():
            # Use ts == thread_ts to avoid reconnect logic
            event = {
                "ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            with patch.object(sessions, "get_or_create", return_value=info):
                await _process_message(event, "queued msg", client, config, sessions)

        # Start _process_message (will block on lock)
        task = asyncio.create_task(run_process())

        # Wait for hourglass to be added (proves contention was detected)
        await asyncio.wait_for(hourglass_added.wait(), timeout=2.0)

        # Release the lock so it can proceed
        info.lock.release()
        await task

        # Hourglass should have been added then removed
        assert _reactions_by_name(client, "reactions_add", "hourglass", "1000.0") >= 1
        assert _reactions_by_name(client, "reactions_remove", "hourglass", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_no_hourglass_when_lock_free(self, config, sessions):
        """When the lock is free, no :hourglass: is added."""
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
            await _process_message(event, "no wait", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "hourglass", "1000.0") == 0


# ---------------------------------------------------------------------------
# :warning: — permission denials
# ---------------------------------------------------------------------------


class TestThreadRootWarning:
    """Warning emoji added when permissions were denied."""

    @pytest.mark.asyncio
    async def test_warning_added_on_permission_denial(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event(
                "result",
                text="done",
                num_turns=1,
                duration_ms=5000,
                permission_denials=[
                    {"tool_name": "Bash", "reason": "not allowed"},
                ],
            )

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
            await _process_message(event, "run bash", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "warning", "1000.0") >= 1

    @pytest.mark.asyncio
    async def test_no_warning_without_denials(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event(
                "result",
                text="done",
                num_turns=1,
                duration_ms=3000,
            )

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
            await _process_message(event, "ok", client, config, sessions)

        assert _reactions_by_name(client, "reactions_add", "warning", "1000.0") == 0


# ---------------------------------------------------------------------------
# _text_ends_with_question — unit tests
# ---------------------------------------------------------------------------


class TestTextEndsWithQuestion:
    """Unit tests for the _text_ends_with_question helper."""

    def test_simple_question(self):
        assert _text_ends_with_question("What do you think?") is True

    def test_trailing_whitespace(self):
        assert _text_ends_with_question("What do you think?  \n") is True

    def test_not_a_question(self):
        assert _text_ends_with_question("Done — all tests pass.") is False

    def test_empty_string(self):
        assert _text_ends_with_question("") is False

    def test_only_whitespace(self):
        assert _text_ends_with_question("   \n  ") is False

    def test_multiline_ending_with_question(self):
        assert _text_ends_with_question("Here's the result.\n\nShould I continue?") is True

    def test_multiline_not_ending_with_question(self):
        assert _text_ends_with_question("Is this ok?\n\nDone.") is False


# ---------------------------------------------------------------------------
# :speech_balloon: on completion — question text gets balloon, not checkmark
# ---------------------------------------------------------------------------


class TestThreadRootQuestionCompletion:
    """When Claude's final response ends with ?, speech_balloon is set instead of checkmark."""

    @pytest.mark.asyncio
    async def test_speech_balloon_on_question_text(self, config, sessions):
        """Response ending with '?' gets speech_balloon, not white_check_mark."""
        async def fake_stream(prompt):
            yield make_event("assistant", text="Should I proceed with this approach?")
            yield make_event("result", text="Should I proceed with this approach?")

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
            await _process_message(event, "do stuff", client, config, sessions)

        # Should have speech_balloon, NOT white_check_mark
        assert _reactions_by_name(client, "reactions_add", "speech_balloon", "1000.0") >= 1
        assert _reactions_by_name(client, "reactions_add", "white_check_mark", "1000.0") == 0

    @pytest.mark.asyncio
    async def test_checkmark_on_non_question_text(self, config, sessions):
        """Response NOT ending with '?' gets white_check_mark as usual."""
        async def fake_stream(prompt):
            yield make_event("assistant", text="All done.")
            yield make_event("result", text="All done.")

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
            await _process_message(event, "do stuff", client, config, sessions)

        # Should have white_check_mark, NOT speech_balloon
        assert _reactions_by_name(client, "reactions_add", "white_check_mark", "1000.0") >= 1
        assert _reactions_by_name(client, "reactions_add", "speech_balloon", "1000.0") == 0

    @pytest.mark.asyncio
    async def test_speech_balloon_cleared_when_next_msg_arrives(self, config, sessions):
        """When a new message arrives, old speech_balloon is cleared."""
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        info = mock_session_info(mock_session)
        # Simulate speech_balloon from a previous turn
        info.thread_reactions.add("speech_balloon")

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {
                "ts": "2000.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "here's my answer", client, config, sessions)

        # speech_balloon from previous turn should be removed during cleanup
        assert _reactions_by_name(client, "reactions_remove", "speech_balloon", "1000.0") >= 1

