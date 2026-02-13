"""Tests for small utility/helper functions in chicane.handlers."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chicane.claude import ClaudeEvent
from chicane.config import Config
from chicane.handlers import (
    _bot_in_thread,
    _fetch_thread_history,
    _find_session_id_in_thread,
    _has_git_commit,
    _HANDOFF_RE,
    _resolve_channel_cwd,
    _should_ignore,
    _should_show,
    _split_message,
    _summarize_tool_input,
    SLACK_MAX_LENGTH,
)
from tests.conftest import make_tool_event, tool_block


class TestShouldIgnore:
    def test_no_restrictions(self, config):
        event = {"user": "U_ANYONE"}
        assert _should_ignore(event, config) is False

    def test_allowed_user(self, config_restricted):
        event = {"user": "U_ALLOWED"}
        assert _should_ignore(event, config_restricted) is False

    def test_blocked_user(self, config_restricted):
        event = {"user": "U_BLOCKED"}
        assert _should_ignore(event, config_restricted) is True


class TestBotInThread:
    @pytest.mark.asyncio
    async def test_bot_found_in_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "U_BOT"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_HUMAN", "text": "hello"},
                {"user": "U_BOT", "text": "hi there"},
            ]
        }
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is True

    @pytest.mark.asyncio
    async def test_bot_not_in_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "U_BOT"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_HUMAN", "text": "hello"},
                {"user": "U_OTHER", "text": "hi there"},
            ]
        }
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is False

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self):
        client = AsyncMock()
        client.auth_test.side_effect = Exception("API error")
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is False

    @pytest.mark.asyncio
    async def test_empty_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "U_BOT"}
        client.conversations_replies.return_value = {"messages": []}
        assert await _bot_in_thread("1234.5678", "C_CHAN", client) is False


class TestSplitMessage:
    def test_short_text_single_chunk(self):
        assert _split_message("hello") == ["hello"]

    def test_exact_limit_single_chunk(self):
        text = "a" * SLACK_MAX_LENGTH
        assert _split_message(text) == [text]

    def test_long_text_splits_into_chunks(self):
        text = "a" * 8000
        chunks = _split_message(text)
        assert len(chunks) > 1
        reassembled = "".join(chunks)
        assert reassembled == text

    def test_splits_on_newlines(self):
        line = "x" * 100 + "\n"
        text = line * 50  # 5050 chars
        chunks = _split_message(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= SLACK_MAX_LENGTH

    def test_no_content_lost(self):
        text = "line1\nline2\n" * 500
        chunks = _split_message(text)
        reassembled = "\n".join(chunks)
        assert "line1" in reassembled
        assert "line2" in reassembled

    def test_very_long_single_line(self):
        text = "a" * 10000
        chunks = _split_message(text)
        assert len(chunks) > 1
        assert "".join(chunks) == text


class TestFetchThreadHistory:
    @pytest.mark.asyncio
    async def test_formats_conversation_transcript(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "<@UBOT123> hello there"},
                {"user": "UBOT123", "ts": "1001.0", "text": "Hi! How can I help?"},
                {"user": "UHUMAN1", "ts": "1002.0", "text": "follow-up question"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)

        assert result is not None
        assert "[User] hello there" in result
        assert "[Chicane] Hi! How can I help?" in result
        assert "follow-up question" not in result

    @pytest.mark.asyncio
    async def test_excludes_current_message(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "first"},
                {"user": "UBOT123", "ts": "1001.0", "text": "response"},
                {"user": "UHUMAN1", "ts": "1002.0", "text": "this is the new prompt"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)

        assert "this is the new prompt" not in result
        assert "[User] first" in result
        assert "[Chicane] response" in result

    @pytest.mark.asyncio
    async def test_strips_bot_mentions_from_user_messages(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "<@UBOT123> do something"},
                {"user": "UHUMAN1", "ts": "1001.0", "text": "current msg"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1001.0", client)

        assert result is not None
        assert "<@UBOT123>" not in result
        assert "[User] do something" in result

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "only message"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1000.0", client)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        client = AsyncMock()
        client.auth_test.side_effect = Exception("API error")

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_empty_messages(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "hello"},
                {"user": "UHUMAN1", "ts": "1001.0", "text": ""},
                {"user": "UBOT123", "ts": "1002.0", "text": "response"},
                {"user": "UHUMAN1", "ts": "1003.0", "text": "current"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1003.0", client)

        lines = result.split("\n")
        assert len(lines) == 2
        assert "[User] hello" in lines[0]
        assert "[Chicane] response" in lines[1]

    @pytest.mark.asyncio
    async def test_user_message_only_mention_skipped(self):
        """A user message that's only a bot mention with no content should be skipped."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "<@UBOT123>"},
                {"user": "UBOT123", "ts": "1001.0", "text": "response"},
                {"user": "UHUMAN1", "ts": "1002.0", "text": "current"},
            ]
        }

        result = await _fetch_thread_history("C_CHAN", "1000.0", "1002.0", client)

        assert result is not None
        lines = result.split("\n")
        assert len(lines) == 1
        assert "[Chicane] response" in lines[0]


class TestHandoffRegex:
    """Test the _HANDOFF_RE pattern used to extract session_id from prompts."""

    def test_plain_format(self):
        text = "Working on auth feature (session_id: abc-123-def)"
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "abc-123-def"

    def test_slack_italic_format(self):
        text = "Working on auth feature\n\n_(session_id: abc-123-def)_"
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "abc-123-def"

    def test_trailing_whitespace(self):
        text = "Summary text (session_id: aaa-bbb-ccc)  "
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "aaa-bbb-ccc"

    def test_no_match_when_absent(self):
        text = "Just a normal message with no handoff"
        assert _HANDOFF_RE.search(text) is None

    def test_no_match_mid_text(self):
        """session_id pattern must be at the end of the prompt."""
        text = "(session_id: abc-123) and then more text"
        assert _HANDOFF_RE.search(text) is None

    def test_strips_session_id_from_prompt(self):
        """Verify the extraction + stripping logic that _process_message uses."""
        prompt = "Working on auth feature\n\n_(session_id: abc-123-def)_"
        m = _HANDOFF_RE.search(prompt)
        assert m is not None
        cleaned = prompt[: m.start()].rstrip()
        assert cleaned == "Working on auth feature"
        assert m.group(1) == "abc-123-def"

    def test_full_uuid_format(self):
        text = "Summary _(session_id: a1b2c3d4-e5f6-7890-abcd-ef1234567890)_"
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestFindSessionIdInThread:
    """Test scanning thread messages for a handoff session_id."""

    @pytest.mark.asyncio
    async def test_finds_session_id_in_thread(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "start a task"},
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": "Working on auth\n\n_(session_id: abc-123-def)_",
                },
                {"user": "UHUMAN1", "ts": "1002.0", "text": "continue please"},
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result == "abc-123-def"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_session_id(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "hello"},
                {"user": "UBOT123", "ts": "1001.0", "text": "hi there"},
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_first_session_id_found(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": "First handoff _(session_id: aaa-111)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": "Second handoff _(session_id: bbb-222)_",
                },
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result == "aaa-111"

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        client = AsyncMock()
        client.conversations_replies.side_effect = Exception("API error")

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_empty_thread(self):
        client = AsyncMock()
        client.conversations_replies.return_value = {"messages": []}

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result is None


class TestResolveChannelCwd:
    """Test _resolve_channel_cwd function."""

    @pytest.mark.asyncio
    async def test_returns_none_without_channel_dirs(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        client = AsyncMock()
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolves_channel_to_directory(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            base_directory=Path("/projects"),
            channel_dirs={"dev-team": "myproject"},
        )
        client = AsyncMock()
        client.conversations_info.return_value = {
            "channel": {"name": "dev-team"}
        }
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result == Path("/projects/myproject")

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            channel_dirs={"dev-team": "myproject"},
        )
        client = AsyncMock()
        client.conversations_info.side_effect = Exception("API error")
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_channel(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            channel_dirs={"dev-team": "myproject"},
        )
        client = AsyncMock()
        client.conversations_info.return_value = {
            "channel": {"name": "random-channel"}
        }
        result = await _resolve_channel_cwd("C_CHAN", client, config)
        assert result is None


class TestShouldShow:
    """Unit tests for the _should_show helper."""

    def test_verbose_shows_everything(self):
        for event_type in ("tool_activity", "tool_error", "tool_result", "compact_boundary"):
            assert _should_show(event_type, "verbose") is True

    def test_normal_shows_tools_and_errors(self):
        assert _should_show("tool_activity", "normal") is True
        assert _should_show("tool_error", "normal") is True

    def test_normal_hides_results_and_compact(self):
        assert _should_show("tool_result", "normal") is False
        assert _should_show("compact_boundary", "normal") is False

    def test_minimal_hides_everything(self):
        for event_type in ("tool_activity", "tool_error", "tool_result", "compact_boundary"):
            assert _should_show(event_type, "minimal") is False


class TestSubagentPrefix:
    """Test that subagent activities get the hook prefix."""

    def test_parent_tool_use_id_detected(self):
        event = make_tool_event(
            tool_block("Read", file_path="/src/a.py"),
            parent_tool_use_id="toolu_abc123",
        )
        assert event.parent_tool_use_id == "toolu_abc123"

    def test_no_parent_tool_use_id(self):
        event = make_tool_event(
            tool_block("Read", file_path="/src/a.py"),
        )
        assert event.parent_tool_use_id is None


class TestSummarizeToolInput:
    """Test _summarize_tool_input for the catch-all tool display."""

    def test_string_values(self):
        result = _summarize_tool_input({"query": "authentication", "limit": 10})
        assert "  query: `authentication`" in result
        assert "  limit: `10`" in result
        assert "\n" in result

    def test_skips_long_strings(self):
        result = _summarize_tool_input({"data": "x" * 200})
        assert result == ""

    def test_truncates_medium_strings(self):
        val = "a" * 80
        result = _summarize_tool_input({"query": val})
        assert "...`" in result

    def test_skips_nested_objects(self):
        result = _summarize_tool_input({"nested": {"a": 1}, "name": "test"})
        assert "  name: `test`" in result
        assert "nested" not in result

    def test_empty_input(self):
        assert _summarize_tool_input({}) == ""

    def test_bool_values(self):
        result = _summarize_tool_input({"include_tests": True})
        assert "  include_tests: `true`" in result

    def test_respects_max_params(self):
        result = _summarize_tool_input(
            {"a": "short", "b": "another", "c": "more", "d": "extra"},
            max_params=2,
        )
        assert result.count("\n") == 1  # 2 lines = 1 newline


class TestHasGitCommit:
    """Test _has_git_commit detection helper."""

    def test_simple_git_commit(self):
        event = make_tool_event(
            tool_block("Bash", command='git commit -m "fix bug"')
        )
        assert _has_git_commit(event) is True

    def test_git_add_and_commit_chained(self):
        event = make_tool_event(
            tool_block("Bash", command='git add . && git commit -m "feat"')
        )
        assert _has_git_commit(event) is True

    def test_git_commit_amend(self):
        event = make_tool_event(
            tool_block("Bash", command="git commit --amend --no-edit")
        )
        assert _has_git_commit(event) is True

    def test_not_git_commit(self):
        event = make_tool_event(
            tool_block("Bash", command="git status")
        )
        assert _has_git_commit(event) is False

    def test_non_bash_tool(self):
        event = make_tool_event(
            tool_block("Read", file_path="/src/app.py")
        )
        assert _has_git_commit(event) is False

    def test_no_tool_blocks(self):
        raw = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
        event = ClaudeEvent(type="assistant", raw=raw)
        assert _has_git_commit(event) is False

    def test_multiple_blocks_one_is_commit(self):
        event = make_tool_event(
            tool_block("Bash", command="git add ."),
            tool_block("Bash", command='git commit -m "done"'),
        )
        assert _has_git_commit(event) is True
