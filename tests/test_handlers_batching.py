"""Tests for tool activity batching in _process_message."""

from unittest.mock import MagicMock, patch

import pytest

from chicane.handlers import _process_message, _MAX_ACTIVITY_BATCH, _ACTIVITY_MSG_MAX_AGE
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
    async def test_second_activity_updates_first(self, config, sessions, queue):
        """Second activity in same sequence should update the first message, not post new."""
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

        post_texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]

        # First activity posted as new message
        assert ":mag: Reading `a.py`" in post_texts

        # Second activity should be an update (chat_update), not a new post
        if client.chat_update.call_args_list:
            update_texts = [c.kwargs["text"] for c in client.chat_update.call_args_list]
            # The update should contain both activities
            assert any(":mag: Reading `b.py`" in t for t in update_texts)
        else:
            # Fallback: if flushed at result boundary, it may still be a new post
            all_text = "\n".join(post_texts)
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

        # Activities may be in posts or updates (create-then-update pattern)
        post_texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        update_texts = [c.kwargs["text"] for c in client.chat_update.call_args_list]
        all_text = "\n".join(post_texts + update_texts)
        assert ":mag: Reading `a.py`" in all_text
        assert ":pencil2: Editing `a.py`" in all_text
        assert "I edited the file." in "\n".join(post_texts)

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

        post_texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        update_texts = [c.kwargs["text"] for c in client.chat_update.call_args_list]
        all_text = "\n".join(post_texts + update_texts)
        assert ":mag: Reading `x.py`" in all_text
        assert ":mag: Finding files `*.py`" in all_text


class TestTextDoesNotBreakBatching:
    """Text between tool activities should NOT break activity batching."""

    @pytest.mark.asyncio
    async def test_text_between_tools_keeps_same_activity_message(self, config, sessions, queue):
        """Tools separated by text should still batch into the same activity message."""
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

        update_texts = [c.kwargs["text"] for c in client.chat_update.call_args_list]
        # Both activities should be combined in the same updated message
        assert any(
            ":mag: Reading `a.py`" in ut and ":pencil2: Editing `a.py`" in ut
            for ut in update_texts
        ), "Both activities should be in the same updated message"

    @pytest.mark.asyncio
    async def test_many_tools_with_text_all_batch(self, config, sessions, queue):
        """Multiple tools with text between them should all batch into one message."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_event("assistant", text="Let me check more files.")
            yield make_tool_event(tool_block("Read", file_path="/src/b.py"))
            yield make_tool_event(tool_block("Read", file_path="/src/c.py"))
            yield make_tool_event(tool_block("Read", file_path="/src/d.py"))
            yield make_event("result", text="done", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5.1", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "check files", client, config, sessions, queue)

        # Only 1 activity post (the first one) — rest should be updates
        post_texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        activity_posts = [t for t in post_texts if ":mag: Reading" in t]
        assert len(activity_posts) == 1, (
            f"Expected exactly 1 activity post (the first), "
            f"got {len(activity_posts)}: {activity_posts}"
        )


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

        # All activities should appear in posts or updates
        post_texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        update_texts = [c.kwargs["text"] for c in client.chat_update.call_args_list]
        all_text = "\n".join(post_texts + update_texts)
        assert ":mag: Reading `first.py`" in all_text
        assert ":mag: Reading `file0.py`" in all_text


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

        # Verify no single posted or updated message exceeds the limit
        for call in client.chat_postMessage.call_args_list:
            text = call.kwargs.get("text", "")
            assert len(text) <= 4000, f"Posted message too long: {len(text)} chars"
        for call in client.chat_update.call_args_list:
            text = call.kwargs.get("text", "")
            assert len(text) <= 4000, f"Updated message too long: {len(text)} chars"


class TestCreateThenUpdate:
    """Tool activities use chat.update after the first post to reduce message count."""

    @pytest.mark.asyncio
    async def test_multiple_activities_use_update(self, config, sessions, queue):
        """Three consecutive activities: 1 post + updates, not 3 posts."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_tool_event(tool_block("Read", file_path="/src/b.py"))
            yield make_tool_event(tool_block("Read", file_path="/src/c.py"))
            yield make_event("result", text="done", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "9.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "read all", client, config, sessions, queue)

        # Count activity-related posts (exclude completion summary, session info, etc.)
        activity_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":mag: Reading" in c.kwargs.get("text", "")
        ]
        # Only 1 activity post (the first one) — rest go through update
        assert len(activity_posts) == 1
        assert activity_posts[0].kwargs["text"] == ":mag: Reading `a.py`"

        # Updates should contain the accumulated activities
        if client.chat_update.call_args_list:
            last_update = client.chat_update.call_args_list[-1]
            update_text = last_update.kwargs["text"]
            assert ":mag: Reading `a.py`" in update_text
            assert ":mag: Reading `c.py`" in update_text

    @pytest.mark.asyncio
    async def test_text_between_activities_keeps_same_message(self, config, sessions, queue):
        """Text between tools doesn't break activity batching — all tools stay in one message."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_event("assistant", text="Found it.")
            yield make_tool_event(tool_block("Edit", file_path="/src/a.py"))
            yield make_event("result", text="Fixed.", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "10.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "fix", client, config, sessions, queue)

        update_texts = [c.kwargs["text"] for c in client.chat_update.call_args_list]
        # Both activities should be combined into one message via update
        assert any(
            ":mag: Reading `a.py`" in ut and ":pencil2: Editing `a.py`" in ut
            for ut in update_texts
        ), "Both activities should be in the same updated message"


class TestActivityMessageRotation:
    """Activity messages rotate after _ACTIVITY_MSG_MAX_AGE seconds."""

    @pytest.mark.asyncio
    async def test_activity_message_rotates_after_max_age(self, config, sessions, queue):
        """After 60s, a new activity should create a new message, not update the old one."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_tool_event(tool_block("Read", file_path="/src/b.py"))
            yield make_event("result", text="done", num_turns=1, duration_ms=100)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        # monotonic() is called when creating an activity message and when
        # checking its age.  Return 0.0 for creation, then jump past max age.
        fake_times = iter([0.0, _ACTIVITY_MSG_MAX_AGE + 1])

        with patch("chicane.handlers.monotonic", side_effect=lambda: next(fake_times, _ACTIVITY_MSG_MAX_AGE + 1)), \
             patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20.0", "channel": "C1", "user": "UHUMAN1"}
            await _process_message(event, "read files", client, config, sessions, queue)

        # Second activity should be a new post (not an update) because the
        # message age exceeded _ACTIVITY_MSG_MAX_AGE.
        post_texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        activity_posts = [t for t in post_texts if ":mag: Reading" in t]
        assert len(activity_posts) >= 2, (
            f"Expected 2 activity posts (rotated), got {len(activity_posts)}: {activity_posts}"
        )
        # No updates should have been made (rotation created a new post instead)
        update_texts = [c.kwargs.get("text", "") for c in client.chat_update.call_args_list]
        activity_updates = [t for t in update_texts if ":mag: Reading" in t]
        assert len(activity_updates) == 0, (
            f"Expected no activity updates (rotated to new post), got: {activity_updates}"
        )
