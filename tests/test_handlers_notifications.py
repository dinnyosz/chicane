"""Tests for Slack notifications: compaction, permission denials, completion summary posting, verbosity."""

from unittest.mock import MagicMock, patch

import pytest

from chicane.config import Config
from chicane.handlers import _process_message
from chicane.sessions import SessionStore
from chicane.slack_queue import SlackMessageQueue
from tests.conftest import make_event, make_tool_event, make_user_event_with_results, mock_client, mock_session_info, tool_block


class TestCompletionSummaryPosting:
    """Test that completion summary is posted after streaming."""

    @pytest.mark.asyncio
    async def test_summary_posted_after_response(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("assistant", text="Done!")
            yield make_event(
                "result", text="Done!",
                num_turns=3, total_cost_usd=0.02, duration_ms=5000,
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "11000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        post_calls = client.chat_postMessage.call_args_list
        summary_calls = [
            c for c in post_calls
            if ":checkered_flag:" in c.kwargs.get("text", "")
        ]
        assert len(summary_calls) == 1
        assert "3 turns" in summary_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_no_summary_when_no_result_event(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("assistant", text="Partial response")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "11001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        summary_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":checkered_flag:" in c.kwargs.get("text", "")
        ]
        assert len(summary_calls) == 0


class TestCompactBoundaryNotification:
    """Test that context compaction events notify the user in Slack."""

    @pytest.fixture
    def config(self):
        return Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            verbosity="verbose",
        )

    @pytest.mark.asyncio
    async def test_auto_compaction_notifies_user(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("assistant", text="Working on it...")
            yield make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "auto", "pre_tokens": 95000},
            )
            yield make_event("assistant", text="Continuing after compaction.")
            yield make_event("result", text="Continuing after compaction.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "12000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "big task", client, config, sessions, queue)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        msg = brain_calls[0].kwargs["text"]
        assert "automatically compacted" in msg
        assert "95,000 tokens" in msg
        assert "earlier messages may be summarized" in msg

    @pytest.mark.asyncio
    async def test_manual_compaction_notifies_user(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "manual", "pre_tokens": 50000},
            )
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "12001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "compact", client, config, sessions, queue)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        assert "manually compacted" in brain_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_compaction_without_pre_tokens(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "auto"},
            )
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "12002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions, queue)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        msg = brain_calls[0].kwargs["text"]
        assert "tokens" not in msg
        assert "earlier messages may be summarized" in msg

    @pytest.mark.asyncio
    async def test_compaction_without_metadata(self, config, sessions, queue):
        """Handle edge case where compact_metadata is missing entirely."""

        async def fake_stream(prompt):
            yield make_event(
                "system",
                subtype="compact_boundary",
            )
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "12003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions, queue)

        brain_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":brain:" in c.kwargs.get("text", "")
        ]
        assert len(brain_calls) == 1
        assert "automatically compacted" in brain_calls[0].kwargs["text"]


class TestPermissionDenialNotification:
    """Test that permission denials from result events are surfaced."""

    @pytest.mark.asyncio
    async def test_denials_posted(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("assistant", text="I tried but couldn't.")
            yield make_event(
                "result", text="I tried but couldn't.",
                num_turns=2, duration_ms=3000,
                permission_denials=[
                    {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "rm -rf /"}},
                    {"tool_name": "Bash", "tool_use_id": "t2", "tool_input": {"command": "sudo reboot"}},
                    {"tool_name": "Write", "tool_use_id": "t3", "tool_input": {"file_path": "/etc/passwd"}},
                ],
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "13000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do it", client, config, sessions, queue)

        denial_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":no_entry_sign:" in c.kwargs.get("text", "")
        ]
        assert len(denial_calls) == 1
        msg = denial_calls[0].kwargs["text"]
        assert "3 tool permissions denied" in msg
        assert "`Bash`" in msg
        assert "`Write`" in msg

    @pytest.mark.asyncio
    async def test_single_denial_singular(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event(
                "result", text="blocked",
                num_turns=1, duration_ms=1000,
                permission_denials=[
                    {"tool_name": "Edit", "tool_use_id": "t1", "tool_input": {}},
                ],
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "13001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "edit it", client, config, sessions, queue)

        denial_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":no_entry_sign:" in c.kwargs.get("text", "")
        ]
        assert len(denial_calls) == 1
        assert "1 tool permission denied" in denial_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_no_denials_no_message(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event(
                "result", text="all good",
                num_turns=1, duration_ms=1000,
                permission_denials=[],
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "13002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions, queue)

        denial_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":no_entry_sign:" in c.kwargs.get("text", "")
        ]
        assert len(denial_calls) == 0


class TestVerbosityFiltering:
    """Integration tests for verbosity levels in _process_message."""

    @pytest.mark.asyncio
    async def test_minimal_hides_tool_activities(self):
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="minimal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/tmp/test.py"))
            yield make_event("assistant", text="Here is the file content.")
            yield make_event("result", text="Here is the file content.", num_turns=1, duration_ms=5000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "read file", client, config, sessions, queue)

        all_texts = [
            c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list
        ]
        assert not any(":mag:" in t for t in all_texts)
        assert any("Here is the file content." in t for t in all_texts)
        assert any(":checkered_flag:" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_minimal_hides_tool_errors(self):
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="minimal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_user_event_with_results([
                {"type": "tool_result", "is_error": True, "content": "Command failed"},
            ])
            yield make_event("assistant", text="Error occurred.")
            yield make_event("result", text="Error occurred.", num_turns=1, duration_ms=3000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "run cmd", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert not any(":warning: Tool error:" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_normal_shows_tool_activities(self):
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="normal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", file_path="/tmp/test.py"))
            yield make_event("assistant", text="File content.")
            yield make_event("result", text="File content.", num_turns=1, duration_ms=2000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "read file", client, config, sessions, queue)

        all_texts = [
            c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list
        ]
        assert any(":mag:" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_normal_hides_tool_results(self):
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="normal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_user_event_with_results([
                {"type": "tool_result", "is_error": False, "content": "successful output"},
            ])
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do thing", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert not any(":clipboard:" in t and "output" in t.lower() for t in all_texts)

    @pytest.mark.asyncio
    async def test_verbose_shows_non_quiet_tool_results(self):
        """Verbose mode shows tool results for non-quiet tools like Bash."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="verbose")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            # Assistant calls Bash (not a quiet tool)
            yield make_tool_event(tool_block("Bash", id="tu_bash_1", command="echo hello"))
            # User event with the Bash result
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_bash_1", "is_error": False, "content": "hello"},
            ])
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do thing", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert any(":clipboard:" in t and "hello" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_verbose_hides_read_tool_results(self):
        """Even in verbose mode, Read tool output is suppressed."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="verbose")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Read", id="tu_read_1", file_path="/tmp/test.py"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_read_1", "is_error": False, "content": "file contents here"},
            ])
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20007.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "read file", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert not any(":clipboard: Tool output:" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_verbose_long_tool_output_uploaded_as_snippet(self):
        """Long tool output in verbose mode is uploaded as a snippet instead of inline."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="verbose")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        long_output = "x" * 501

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Bash", id="tu_bash_2", command="cat big.log"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_bash_2", "is_error": False, "content": long_output},
            ])
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20008.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "show log", client, config, sessions, queue)

        # Should upload as snippet via files_upload_v2, not post inline
        client.files_upload_v2.assert_called_once()
        upload_kwargs = client.files_upload_v2.call_args.kwargs
        assert upload_kwargs["channel"] == "C_CHAN"

        # No inline tool output messages
        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert not any(":clipboard: Tool output:\n```" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_verbose_short_tool_output_posted_inline(self):
        """Short tool output in verbose mode is still posted as a regular message."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="verbose")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Bash", id="tu_bash_3", command="echo hi"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_bash_3", "is_error": False, "content": "hi"},
            ])
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20009.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "say hi", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        # Should contain a :clipboard: label with the tool output inline
        assert any(":clipboard:" in t and "```\nhi\n```" in t for t in all_texts)
        client.files_getUploadURLExternal.assert_not_called()

    @pytest.mark.asyncio
    async def test_verbose_shows_compact_boundary(self):
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="verbose")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "auto", "pre_tokens": 80000},
            )
            yield make_event("result", text="done", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20005.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert any(":brain:" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_minimal_hides_compact_boundary(self):
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="minimal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_event(
                "system",
                subtype="compact_boundary",
                compact_metadata={"trigger": "auto", "pre_tokens": 80000},
            )
            yield make_event("result", text="done", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "20006.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hi", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert not any(":brain:" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_permission_denials_shown_at_all_levels(self):
        """Permission denials should always be shown regardless of verbosity."""
        for verbosity in ("minimal", "normal", "verbose"):
            config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity=verbosity)
            sessions = SessionStore()
            queue = SlackMessageQueue(min_interval=0.0)

            async def fake_stream(prompt):
                yield make_event("assistant", text="Tried but denied.")
                yield make_event(
                    "result",
                    text="Tried but denied.",
                    num_turns=1,
                    duration_ms=2000,
                    permission_denials=[{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {}}],
                )

            mock_session = MagicMock()
            mock_session.stream = fake_stream
            mock_session.session_id = "s1"
            client = mock_client()

            with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
                event = {"ts": f"2100{verbosity}.0", "channel": "C_CHAN", "user": "UHUMAN1"}
                await _process_message(event, "try bash", client, config, sessions, queue)

            all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
            assert any(":no_entry_sign:" in t for t in all_texts), f"Permission denial not shown at {verbosity}"


class TestTestResultCards:
    """Test that pytest result summary cards are posted for test tool results."""

    @pytest.mark.asyncio
    async def test_pytest_full_summary_shows_card(self):
        """Normal pytest output with summary line produces a test result card."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="normal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        pytest_output = (
            "============================= test session starts "
            "==============================\ncollected 42 items\n"
            "tests/test_foo.py ......  [100%]\n"
            "============================== 42 passed in 3.45s "
            "=============================="
        )

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Bash", id="tu_pytest_1", command="pytest tests/"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_pytest_1", "is_error": False, "content": pytest_output},
            ])
            yield make_event("assistant", text="All tests pass!")
            yield make_event("result", text="All tests pass!", num_turns=2, duration_ms=5000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "40000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "run tests", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        summary_calls = [t for t in all_texts if ":white_check_mark:" in t and "passed" in t]
        assert len(summary_calls) == 1
        assert "42 passed" in summary_calls[0]
        assert "3.45s" in summary_calls[0]

    @pytest.mark.asyncio
    async def test_truncated_pytest_output_shows_card(self):
        """Truncated pytest output (no summary line) still shows a test result card."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="normal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        # Truncated output — has verbose PASSED/FAILED lines but no summary
        truncated_output = (
            "============================= test session starts "
            "==============================\ncollected 42 items\n\n"
            "tests/test_foo.py::test_one PASSED\n"
            "tests/test_foo.py::test_two PASSED\n"
            "tests/test_foo.py::test_three FAILED\n"
            "tests/test_bar.py::test_four PASSED"
        )

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Bash", id="tu_pytest_2", command="pytest tests/ -v"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_pytest_2", "is_error": False, "content": truncated_output},
            ])
            yield make_event("assistant", text="Some tests failed.")
            yield make_event("result", text="Some tests failed.", num_turns=2, duration_ms=5000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "40001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "run tests", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        # Should show failure card from verbose line counting
        summary_calls = [t for t in all_texts if ":x:" in t and "failed" in t]
        assert len(summary_calls) == 1
        assert "3 passed" in summary_calls[0]
        assert "1 failed" in summary_calls[0]

    @pytest.mark.asyncio
    async def test_pytest_short_format_shows_card(self):
        """pytest -q short format (no === wrapping) still shows a card."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="normal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        short_output = (
            "============================= test session starts "
            "==============================\ncollected 42 items\n\n"
            "..........................................\n"
            "42 passed in 3.45s"
        )

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Bash", id="tu_pytest_3", command="pytest tests/ -q"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_pytest_3", "is_error": False, "content": short_output},
            ])
            yield make_event("assistant", text="All pass!")
            yield make_event("result", text="All pass!", num_turns=2, duration_ms=3000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "40002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "run tests", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        summary_calls = [t for t in all_texts if ":white_check_mark:" in t and "passed" in t]
        assert len(summary_calls) == 1
        assert "42 passed" in summary_calls[0]

    @pytest.mark.asyncio
    async def test_no_card_for_non_test_bash(self):
        """Bash tool running non-test commands shouldn't produce test result cards."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="normal")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Bash", id="tu_bash_1", command="echo hello"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_bash_1", "is_error": False, "content": "hello"},
            ])
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.", num_turns=1, duration_ms=1000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "40003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "say hello", client, config, sessions, queue)

        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert not any(":white_check_mark:" in t for t in all_texts)
        assert not any(":x:" in t for t in all_texts)


class TestDiffSnippetUpload:
    """Test that git diff output is always uploaded as a snippet for syntax highlighting."""

    @pytest.mark.asyncio
    async def test_short_diff_uploaded_as_snippet(self):
        """Even short git diff output should be uploaded as a snippet, not inline code."""
        config = Config(slack_bot_token="xoxb-test", slack_app_token="xapp-test", verbosity="verbose")
        sessions = SessionStore()
        queue = SlackMessageQueue(min_interval=0.0)

        short_diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+new line\n"
            " line2\n"
        )

        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Bash", id="tu_diff_1", command="git diff HEAD~1"))
            yield make_user_event_with_results([
                {"type": "tool_result", "tool_use_id": "tu_diff_1", "is_error": False, "content": short_diff},
            ])
            yield make_event("assistant", text="Here's the diff.")
            yield make_event("result", text="Here's the diff.", num_turns=2, duration_ms=2000)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"
        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "50000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "show diff", client, config, sessions, queue)

        # Should upload as snippet, not post inline
        client.files_upload_v2.assert_called_once()
        upload_kwargs = client.files_upload_v2.call_args.kwargs
        assert upload_kwargs["snippet_type"] == "diff"
        assert upload_kwargs["filename"] == "diff.diff"

        # No inline code blocks with diff content
        all_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        assert not any("diff --git" in t for t in all_texts)
