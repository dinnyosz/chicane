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
    _guess_snippet_type,
    _SNIPPET_EXT,
    _has_git_commit,
    _HANDOFF_RE,
    _SESSION_ALIAS_RE,
    _resolve_channel_cwd,
    _should_ignore,
    _should_show,
    _split_message,
    _summarize_tool_input,
    SessionSearchResult,
    SLACK_MAX_LENGTH,
)
from tests.conftest import make_tool_event, tool_block


class TestShouldIgnore:
    def test_empty_allowed_users_blocks_all(self):
        """When allowed_users is empty, all messages are blocked."""
        empty_config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=[],
        )
        event = {"user": "U_ANYONE"}
        assert _should_ignore(event, empty_config) is True

    def test_allowed_user_in_list(self, config):
        """Users in the config's allowed_users list are not ignored."""
        event = {"user": "UHUMAN1"}
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

    def test_match_mid_text(self):
        """session_id pattern matches even when followed by more text."""
        text = "(session_id: abc-123) and then more text"
        m = _HANDOFF_RE.search(text)
        assert m is not None
        assert m.group(1) == "abc-123"

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


class TestSessionAliasRegex:
    """Test the _SESSION_ALIAS_RE pattern used to find session aliases."""

    def test_standard_format(self):
        text = ":sparkles: New session\n_(session: clever-fox-rainbow)_"
        m = _SESSION_ALIAS_RE.search(text)
        assert m is not None
        assert m.group(1) == "clever-fox-rainbow"

    def test_two_word_alias(self):
        text = "_(session: cool-cat)_"
        m = _SESSION_ALIAS_RE.search(text)
        assert m is not None
        assert m.group(1) == "cool-cat"

    def test_match_mid_line(self):
        """Alias followed by more text on the same line should still match."""
        text = "_(session: laughing-frosty-wheel)_ and then response text"
        m = _SESSION_ALIAS_RE.search(text)
        assert m is not None
        assert m.group(1) == "laughing-frosty-wheel"

    def test_match_mid_message(self):
        """Alias in the middle of a multi-line message should match."""
        text = (
            ":sparkles: New session\n"
            "_(session: dancing-cosmic-falcon)_\n\n"
            "Let me look into that.\n"
            ":mag: Reading `file.py`"
        )
        m = _SESSION_ALIAS_RE.search(text)
        assert m is not None
        assert m.group(1) == "dancing-cosmic-falcon"

    def test_no_match_when_absent(self):
        text = "Just a normal message"
        assert _SESSION_ALIAS_RE.search(text) is None

    def test_no_match_single_word(self):
        """Single word (no hyphens) should not match."""
        text = "_(session: singleword)_"
        assert _SESSION_ALIAS_RE.search(text) is None

    def test_without_underscores(self):
        text = "(session: cool-fox-hat)"
        m = _SESSION_ALIAS_RE.search(text)
        assert m is not None
        assert m.group(1) == "cool-fox-hat"


class TestFindSessionIdInThread:
    """Test scanning thread messages for session references."""

    @pytest.mark.asyncio
    async def test_finds_session_id_in_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
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
        assert result.session_id == "abc-123-def"
        assert result.alias is None  # old UUID format has no alias
        assert result.total_found == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_no_session_id(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "hello"},
                {"user": "UBOT123", "ts": "1001.0", "text": "hi there"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id is None
        assert result.alias is None
        assert result.total_found == 0

    @pytest.mark.asyncio
    async def test_returns_last_session_id_found(self):
        """When multiple session_ids exist, the last (most recent) wins."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
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
        assert result.session_id == "bbb-222"
        assert result.total_found == 2
        assert len(result.skipped_aliases) == 1  # aaa-111 skipped

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.side_effect = Exception("API error")
        client.conversations_history.return_value = {"messages": []}

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id is None
        assert result.alias is None

    @pytest.mark.asyncio
    async def test_handles_empty_thread(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {"messages": []}
        client.conversations_history.return_value = {"messages": []}

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id is None
        assert result.alias is None
        assert result.total_found == 0

    @pytest.mark.asyncio
    async def test_returns_alias_when_found_via_alias_format(self, tmp_path):
        """When session is found via _(session: alias)_ format, the alias
        should be returned alongside the session_id."""
        from unittest.mock import patch
        from chicane.config import save_handoff_session

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": ":sparkles: New session\n_(session: clever-fox-rainbow)_",
                },
            ]
        }

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("clever-fox-rainbow", "sess-abc-123")
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id == "sess-abc-123"
        assert result.alias == "clever-fox-rainbow"
        assert result.total_found == 1

    @pytest.mark.asyncio
    async def test_finds_alias_when_response_text_appended(self, tmp_path):
        """Session alias should be found even when the bot appends response
        text after the _(session: alias)_ line in the same message."""
        from unittest.mock import patch
        from chicane.config import save_handoff_session

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": (
                        ":sparkles: New session\n"
                        "_(session: laughing-frosty-wheel)_\n\n"
                        "Let me look into the rate limiting situation.\n\n"
                        ":mag: Searching for `rate_limit`"
                    ),
                },
            ]
        }

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("laughing-frosty-wheel", "sess-lfw-123")
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id == "sess-lfw-123"
        assert result.alias == "laughing-frosty-wheel"
        assert result.total_found == 1

    @pytest.mark.asyncio
    async def test_returns_last_match_in_thread(self, tmp_path):
        """When multiple sessions exist in a thread, the last (most recent)
        should be returned, with the older one tracked as skipped."""
        from unittest.mock import patch
        from chicane.config import save_handoff_session

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": ":sparkles: New session\n_(session: old-dusty-parrot)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": ":sparkles: New session\n_(session: fresh-shiny-eagle)_",
                },
            ]
        }

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("old-dusty-parrot", "sess-old")
            save_handoff_session("fresh-shiny-eagle", "sess-new")
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id == "sess-new"
        assert result.alias == "fresh-shiny-eagle"
        assert result.total_found == 2
        assert "old-dusty-parrot" in result.skipped_aliases

    @pytest.mark.asyncio
    async def test_finds_handoff_and_bot_session_messages(self, tmp_path):
        """Both handoff messages _(session: alias)_ and bot session
        messages _(session: alias)_ should be found — the last one wins."""
        from unittest.mock import patch
        from chicane.config import save_handoff_session

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": "Handoff summary\n_(session: handoff-cool-cat)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": ":arrows_counterclockwise: Continuing session\n_(session: handoff-cool-cat)_",
                },
            ]
        }

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("handoff-cool-cat", "sess-handoff")
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id == "sess-handoff"
        assert result.alias == "handoff-cool-cat"

    @pytest.mark.asyncio
    async def test_unmapped_alias_tracked(self, tmp_path):
        """When an alias is found but can't be mapped, it appears in
        unmapped_aliases and session_id is None."""
        from unittest.mock import patch

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": ":sparkles: New session\n_(session: lost-ghost-cat)_",
                },
            ]
        }

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            # Don't save anything — alias is unmapped
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id is None
        assert result.total_found == 1
        assert "lost-ghost-cat" in result.unmapped_aliases

    @pytest.mark.asyncio
    async def test_fallback_to_older_when_newest_unmapped(self, tmp_path):
        """When the newest alias can't be mapped, fall back to the next
        older one that can."""
        from unittest.mock import patch
        from chicane.config import save_handoff_session

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": ":sparkles: New session\n_(session: old-good-parrot)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": ":sparkles: New session\n_(session: new-lost-eagle)_",
                },
            ]
        }

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("old-good-parrot", "sess-old-good")
            # Don't save new-lost-eagle — it's unmapped
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id == "sess-old-good"
        assert result.alias == "old-good-parrot"
        assert result.total_found == 2
        assert "new-lost-eagle" in result.unmapped_aliases
        assert result.skipped_aliases == []  # old-good-parrot was used, not skipped

    @pytest.mark.asyncio
    async def test_all_unmapped(self, tmp_path):
        """When all aliases are unmapped, session_id is None and all are
        listed in unmapped_aliases."""
        from unittest.mock import patch

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": ":sparkles: New session\n_(session: ghost-one-alpha)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "1001.0",
                    "text": ":sparkles: New session\n_(session: ghost-two-beta)_",
                },
            ]
        }

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id is None
        assert result.total_found == 2
        assert set(result.unmapped_aliases) == {"ghost-one-alpha", "ghost-two-beta"}


    @pytest.mark.asyncio
    async def test_paginates_long_threads(self, tmp_path):
        """Thread scanning should paginate through all replies, not just the first page."""
        from unittest.mock import patch
        from chicane.config import save_handoff_session

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}

        # Page 1: no session refs, has cursor
        page1 = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "start"},
                {"user": "UBOT123", "ts": "1001.0", "text": "working on it"},
            ],
            "response_metadata": {"next_cursor": "page2_cursor"},
        }
        # Page 2: has the session ref, no cursor
        page2 = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1050.0",
                    "text": ":sparkles: New session\n_(session: paginated-cool-fox)_",
                },
            ],
        }
        client.conversations_replies.side_effect = [page1, page2]

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("paginated-cool-fox", "sess-paginated")
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        assert result.session_id == "sess-paginated"
        assert result.alias == "paginated-cool-fox"
        assert client.conversations_replies.call_count == 2
        # Second call should include the cursor
        second_call = client.conversations_replies.call_args_list[1]
        assert second_call.kwargs.get("cursor") == "page2_cursor"

    @pytest.mark.asyncio
    async def test_session_on_first_page_with_more_pages(self, tmp_path):
        """Session found on first page should still scan remaining pages
        to find newer sessions."""
        from unittest.mock import patch
        from chicane.config import save_handoff_session

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}

        page1 = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": ":sparkles: New session\n_(session: old-early-bird)_",
                },
            ],
            "response_metadata": {"next_cursor": "page2_cursor"},
        }
        page2 = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1050.0",
                    "text": ":arrows_counterclockwise: Continuing\n_(session: new-late-owl)_",
                },
            ],
        }
        client.conversations_replies.side_effect = [page1, page2]

        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("old-early-bird", "sess-old")
            save_handoff_session("new-late-owl", "sess-new")
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)

        # Should pick the newer one from page 2
        assert result.session_id == "sess-new"
        assert result.alias == "new-late-owl"
        assert result.total_found == 2
        assert "old-early-bird" in result.skipped_aliases


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

    def test_git_with_flags_between(self):
        """git -C /path commit should match (flags between git and commit)."""
        event = make_tool_event(
            tool_block("Bash", command='git -C /Users/me/project commit -m "fix"')
        )
        assert _has_git_commit(event) is True

    def test_chained_add_and_git_c_commit(self):
        """echo ... && git -C /path add file && git -C /path commit should match."""
        event = make_tool_event(
            tool_block(
                "Bash",
                command='echo "test" > file.txt && git -C /tmp/project add file.txt && git -C /tmp/project commit -m "test: verification"',
            )
        )
        assert _has_git_commit(event) is True

    def test_multiple_blocks_one_is_commit(self):
        event = make_tool_event(
            tool_block("Bash", command="git add ."),
            tool_block("Bash", command='git commit -m "done"'),
        )
        assert _has_git_commit(event) is True


class TestGuessSnippetType:
    """Tests for _guess_snippet_type which prevents Slack 'Binary' classification."""

    def test_diff_output(self):
        text = "diff --git a/foo.py b/foo.py\nindex abc..def 100644\n--- a/foo.py\n+++ b/foo.py\n"
        assert _guess_snippet_type(text) == "diff"

    def test_diff_with_leading_whitespace(self):
        text = "  diff --git a/foo.py b/foo.py\n"
        assert _guess_snippet_type(text) == "diff"

    def test_unified_diff_header(self):
        text = "--- a/foo.py\n+++ b/foo.py\n@@ -1,3 +1,4 @@\n"
        assert _guess_snippet_type(text) == "diff"

    def test_json_output(self):
        text = '{"key": "value", "nested": {"a": 1}}'
        assert _guess_snippet_type(text) == "javascript"

    def test_xml_output(self):
        text = '<?xml version="1.0"?><root></root>'
        assert _guess_snippet_type(text) == "xml"

    def test_html_output(self):
        text = "<html><body>hello</body></html>"
        assert _guess_snippet_type(text) == "xml"

    def test_plain_text_fallback(self):
        text = "just some regular output from a command"
        assert _guess_snippet_type(text) == "text"

    def test_empty_string(self):
        assert _guess_snippet_type("") == "text"

    def test_multiline_plain_text(self):
        text = "line one\nline two\nline three\n"
        assert _guess_snippet_type(text) == "text"


class TestSnippetExt:
    """Tests for _SNIPPET_EXT filename extension mapping."""

    def test_diff_extension(self):
        assert _SNIPPET_EXT["diff"] == ".diff"

    def test_json_extension(self):
        assert _SNIPPET_EXT["javascript"] == ".json"

    def test_text_fallback(self):
        assert _SNIPPET_EXT["text"] == ".txt"

    def test_unknown_type_falls_back(self):
        assert _SNIPPET_EXT.get("unknown_type", ".txt") == ".txt"


class TestSendSnippetFilenameAlignment:
    """Tests that _send_snippet aligns filename extension with snippet_type."""

    @pytest.mark.asyncio
    async def test_diff_snippet_gets_diff_extension(self):
        """When snippet_type='diff', filename should end with .diff."""
        client = AsyncMock()
        client.files_upload_v2 = AsyncMock()

        from chicane.handlers import _send_snippet
        await _send_snippet(client, "C123", "t1", "diff --git a/f b/f\n", snippet_type="diff")

        kwargs = client.files_upload_v2.call_args.kwargs
        assert kwargs["filename"] == "response.diff"
        assert kwargs["snippet_type"] == "diff"

    @pytest.mark.asyncio
    async def test_text_snippet_keeps_txt_extension(self):
        """Plain text keeps .txt extension."""
        client = AsyncMock()
        client.files_upload_v2 = AsyncMock()

        from chicane.handlers import _send_snippet
        await _send_snippet(client, "C123", "t1", "hello world")

        kwargs = client.files_upload_v2.call_args.kwargs
        assert kwargs["filename"] == "response.txt"

    @pytest.mark.asyncio
    async def test_custom_filename_stem_preserved(self):
        """Custom filename stem is preserved, only extension changes."""
        client = AsyncMock()
        client.files_upload_v2 = AsyncMock()

        from chicane.handlers import _send_snippet
        await _send_snippet(client, "C123", "t1", '{"a":1}', filename="output.txt", snippet_type="javascript")

        kwargs = client.files_upload_v2.call_args.kwargs
        assert kwargs["filename"] == "output.json"
