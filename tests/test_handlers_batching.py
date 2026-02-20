"""Tests for tool activity batching in _process_message."""

from unittest.mock import MagicMock, patch

import pytest

from chicane.handlers import _process_message, _MAX_ACTIVITY_BATCH
from chicane.slack_queue import SlackMessageQueue
from tests.conftest import (
    make_event,
    make_tool_event,
    mock_client,
    mock_session_info,
    tool_block,
)


class TestFirstActivityImmediate:
    """First tool activity in a sequence must be posted immediately."""

    @pytest.mark.asyncio
    async def test_first_activity_posted_immediately(self, config, sessions, queue):
        """The very first tool activity should be a separate message."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_event("result", text="done", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions, queue)

        texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        # First activity should be its own message
        assert any(t == ":mag: Reading `a.py`" for t in texts)

    @pytest.mark.asyncio
    async def test_second_activity_batched(self, config, sessions, queue):
        """Second and subsequent activities in same sequence should be batched."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_tool_event(tool_block("Read", file_path="/src/b.py"))
            yield make_event("result", text="done", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "2.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions, queue)

        texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]

        # First activity standalone
        assert ":mag: Reading `a.py`" in texts
        # Second activity should be flushed (possibly with others) but not standalone
        # (it arrives in the flush before result, so it'll be its own message since
        # it's the only pending activity)
        all_text = "\n".join(texts)
        assert ":mag: Reading `b.py`" in all_text


class TestFlushOnEventTransition:
    """Activities are flushed when a non-activity event arrives."""

    @pytest.mark.asyncio
    async def test_activities_flushed_before_text(self, config, sessions, queue):
        """Pending activities are flushed before text response is posted."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_tool_event(tool_block("Edit", file_path="/src/a.py"))
            yield make_event("assistant", text="I edited the file.")
            yield make_event("result", text="I edited the file.", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "3.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "edit it", client, config, sessions, queue)

        texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        all_text = "\n".join(texts)
        # Both activities should appear before the text
        assert ":mag: Reading `a.py`" in all_text
        assert ":pencil2: Editing `a.py`" in all_text
        assert "I edited the file." in all_text

    @pytest.mark.asyncio
    async def test_activities_flushed_on_result(self, config, sessions, queue):
        """Pending activities are flushed when result event arrives."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/x.py"))
            yield make_tool_event(tool_block("Glob", pattern="*.py"))
            yield make_event("result", text="found files", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "4.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "find py", client, config, sessions, queue)

        all_text = "\n".join(c.kwargs["text"] for c in client.chat_postMessage.call_args_list)
        assert ":mag: Reading `x.py`" in all_text
        assert ":mag: Finding files `*.py`" in all_text


class TestTextBreaksSequence:
    """Text between tool activities resets the first_activity_posted flag."""

    @pytest.mark.asyncio
    async def test_text_resets_first_activity_flag(self, config, sessions, queue):
        """After text, the next tool activity should be posted immediately again."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_event("assistant", text="Let me edit.")
            yield make_tool_event(tool_block("Edit", file_path="/src/a.py"))
            yield make_event("assistant", text="Done editing.")
            yield make_event("result", text="Done editing.", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "fix", client, config, sessions, queue)

        texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        # Both activities should be in separate messages (each is first in its sequence)
        assert ":mag: Reading `a.py`" in texts
        assert ":pencil2: Editing `a.py`" in texts


class TestBatchSizeLimit:
    """Batch is flushed when it reaches _MAX_ACTIVITY_BATCH."""

    @pytest.mark.asyncio
    async def test_flush_at_max_batch(self, config, sessions, queue):
        """Activities should be flushed when reaching the batch limit."""
        # Create enough tool events to exceed _MAX_ACTIVITY_BATCH
        async def fake_stream(prompt):
            # First one is posted immediately
            yield make_tool_event(tool_block("Read", file_path="/src/first.py"))
            # Next _MAX_ACTIVITY_BATCH will fill the batch and trigger a flush
            for i in range(_MAX_ACTIVITY_BATCH + 2):
                yield make_tool_event(tool_block("Read", file_path=f"/src/file{i}.py"))
            yield make_event("result", text="done", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "6.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "read all", client, config, sessions, queue)

        # All activities should appear in the posted messages
        all_text = "\n".join(c.kwargs["text"] for c in client.chat_postMessage.call_args_list)
        assert ":mag: Reading `first.py`" in all_text
        assert ":mag: Reading `file0.py`" in all_text
        # The batch should have been flushed at least once mid-stream
        # (not all accumulated to the end)
        post_count = client.chat_postMessage.call_count
        # At minimum: 1 (first immediate) + 1 (batch flush) + 1 (remainder flush) + 1 (completion)
        assert post_count >= 3


class TestMinimalVerbositySkipsBatching:
    """In minimal mode, tool activities aren't shown at all."""

    @pytest.mark.asyncio
    async def test_minimal_no_activities_posted(self):
        from chicane.config import Config
        from chicane.sessions import SessionStore

        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="minimal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_tool_event(tool_block("Edit", file_path="/src/a.py"))
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "7.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "fix", client, config, sessions, queue)

        all_text = "\n".join(c.kwargs["text"] for c in client.chat_postMessage.call_args_list)
        # No tool activity indicators
        assert ":mag:" not in all_text
        assert ":pencil2:" not in all_text
        # But response text should still be posted
        assert "Done." in all_text


class TestMessageSplitting:
    """Batched activities that exceed Slack's char limit are split."""

    @pytest.mark.asyncio
    async def test_long_batch_respects_split(self, config, sessions, queue):
        """A large batch of activities is split into Slack-safe chunks."""
        async def fake_stream(prompt):
            # First activity posted immediately
            yield make_tool_event(tool_block("Read", file_path="/src/first.py"))
            # Generate enough activities to exceed 3900 chars when combined
            for i in range(60):
                long_name = f"/src/{'x' * 50}_{i}.py"
                yield make_tool_event(tool_block("Read", file_path=long_name))
            yield make_event("result", text="done", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "8.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "read many", client, config, sessions, queue)

        # Verify no single message exceeds the limit
        for call in client.chat_postMessage.call_args_list:
            text = call.kwargs.get("text", "")
            assert len(text) <= 4000, f"Message too long: {len(text)} chars"
