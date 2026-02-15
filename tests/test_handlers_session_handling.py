"""Tests for session handling: hijack prevention, allowed_users filtering,
and stale session detection with context rebuild."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.handlers import (
    _fetch_thread_history,
    _find_session_id_in_thread,
    _process_message,
)
from chicane.sessions import SessionStore
from tests.conftest import make_event, mock_client, mock_session_info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def aiter(items):
    """Helper for async iteration in tests."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# _find_session_id_in_thread — bot-only filtering
# ---------------------------------------------------------------------------


class TestFindSessionIdBotOnly:
    """_find_session_id_in_thread should only trust the bot's own messages."""

    @pytest.mark.asyncio
    async def test_ignores_session_id_from_non_bot_user(self):
        """A session_id posted by a regular user should be ignored."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UHACKER",
                    "ts": "1000.0",
                    "text": "_(session_id: deadbeef-1234-5678-9abc-def012345678)_",
                },
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id is None
        assert result.total_found == 0

    @pytest.mark.asyncio
    async def test_accepts_session_id_from_bot(self):
        """A session_id posted by the bot itself should be accepted."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.5",
                    "text": "_(session_id: deadbeef-1234-5678-9abc-def012345678)_",
                },
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id == "deadbeef-1234-5678-9abc-def012345678"

    @pytest.mark.asyncio
    async def test_mixed_messages_only_trusts_bot(self):
        """When both bot and user post session IDs, only the bot's is used."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UHACKER",
                    "ts": "1000.0",
                    "text": "_(session_id: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "1000.5",
                    "text": "_(session_id: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb)_",
                },
                {
                    "user": "UHACKER",
                    "ts": "1001.0",
                    "text": "_(session_id: cccccccc-cccc-cccc-cccc-cccccccccccc)_",
                },
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        # Should use the bot's session_id, not the hacker's later one
        assert result.session_id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        assert result.total_found == 1

    @pytest.mark.asyncio
    async def test_fallback_thread_starter_also_filtered(self):
        """The fallback path (conversations_history) also filters by bot user."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        # No session refs in replies
        client.conversations_replies.return_value = {"messages": []}
        # Thread starter from a non-bot user
        client.conversations_history.return_value = {
            "messages": [
                {
                    "user": "UHACKER",
                    "ts": "1000.0",
                    "text": "_(session_id: deadbeef-1234-5678-9abc-def012345678)_",
                },
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id is None

    @pytest.mark.asyncio
    async def test_fallback_thread_starter_from_bot_accepted(self):
        """The fallback path accepts session refs from bot messages."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {"messages": []}
        client.conversations_history.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.0",
                    "text": "_(session_id: deadbeef-1234-5678-9abc-def012345678)_",
                },
            ]
        }

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id == "deadbeef-1234-5678-9abc-def012345678"

    @pytest.mark.asyncio
    async def test_alias_from_bot_resolved(self):
        """Session alias from bot's message is resolved via local map."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "1000.5",
                    "text": "_(session: funky-name-here)_",
                },
            ]
        }

        with patch(
            "chicane.handlers.load_handoff_session",
            return_value="resolved-session-id",
        ):
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id == "resolved-session-id"
        assert result.alias == "funky-name-here"

    @pytest.mark.asyncio
    async def test_alias_from_non_bot_ignored(self):
        """Session alias from a regular user should be ignored entirely."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UHACKER",
                    "ts": "1000.5",
                    "text": "_(session: funky-name-here)_",
                },
            ]
        }

        with patch(
            "chicane.handlers.load_handoff_session",
            return_value="resolved-session-id",
        ):
            result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id is None

    @pytest.mark.asyncio
    async def test_auth_failure_returns_empty(self):
        """If we can't determine the bot's user_id, return empty result."""
        client = AsyncMock()
        client.auth_test.side_effect = Exception("API error")

        result = await _find_session_id_in_thread("C_CHAN", "1000.0", client)
        assert result.session_id is None
        assert result.total_found == 0


# ---------------------------------------------------------------------------
# _fetch_thread_history — allowed_users filtering
# ---------------------------------------------------------------------------


class TestFetchThreadHistoryAllowedUsers:
    """_fetch_thread_history should filter out messages from non-allowed users."""

    @pytest.mark.asyncio
    async def test_includes_allowed_user_messages(self):
        """Messages from allowed users are included in the history."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_ALLOWED", "ts": "1000.0", "text": "start"},
                {"user": "UBOT123", "ts": "1000.5", "text": "response"},
            ]
        }

        history = await _fetch_thread_history(
            "C_CHAN", "1000.0", "1001.0", client,
            allowed_users={"U_ALLOWED"},
        )
        assert history is not None
        assert "[User] start" in history
        assert "[Chicane] response" in history

    @pytest.mark.asyncio
    async def test_excludes_non_allowed_user_messages(self):
        """Messages from users not in allowed_users are excluded."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_ALLOWED", "ts": "1000.0", "text": "start"},
                {"user": "U_INTRUDER", "ts": "1000.3", "text": "injected message"},
                {"user": "UBOT123", "ts": "1000.5", "text": "response"},
            ]
        }

        history = await _fetch_thread_history(
            "C_CHAN", "1000.0", "1001.0", client,
            allowed_users={"U_ALLOWED"},
        )
        assert history is not None
        assert "[User] start" in history
        assert "injected message" not in history
        assert "[Chicane] response" in history

    @pytest.mark.asyncio
    async def test_bot_messages_always_included(self):
        """Bot messages are always included regardless of allowed_users."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UBOT123", "ts": "1000.0", "text": "I said something"},
            ]
        }

        history = await _fetch_thread_history(
            "C_CHAN", "1000.0", "1001.0", client,
            allowed_users={"U_ALLOWED"},
        )
        assert history is not None
        assert "[Chicane] I said something" in history

    @pytest.mark.asyncio
    async def test_no_allowed_users_includes_all(self):
        """When allowed_users is None, all messages are included (backwards compat)."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_ANYONE", "ts": "1000.0", "text": "hello"},
                {"user": "UBOT123", "ts": "1000.5", "text": "response"},
            ]
        }

        history = await _fetch_thread_history(
            "C_CHAN", "1000.0", "1001.0", client,
            allowed_users=None,
        )
        assert history is not None
        assert "[User] hello" in history
        assert "[Chicane] response" in history

    @pytest.mark.asyncio
    async def test_current_message_excluded(self):
        """The current message (prompt) should be excluded from history."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_ALLOWED", "ts": "1000.0", "text": "start"},
                {"user": "U_ALLOWED", "ts": "1001.0", "text": "current prompt"},
            ]
        }

        history = await _fetch_thread_history(
            "C_CHAN", "1000.0", "1001.0", client,
            allowed_users={"U_ALLOWED"},
        )
        assert history is not None
        assert "start" in history
        assert "current prompt" not in history

    @pytest.mark.asyncio
    async def test_only_non_allowed_users_returns_none(self):
        """If all non-bot messages are from non-allowed users, return None."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_INTRUDER", "ts": "1000.0", "text": "injected"},
                {"user": "U_OTHER", "ts": "1000.5", "text": "also injected"},
            ]
        }

        history = await _fetch_thread_history(
            "C_CHAN", "1000.0", "1001.0", client,
            allowed_users={"U_ALLOWED"},
        )
        assert history is None

    @pytest.mark.asyncio
    async def test_empty_allowed_users_set_excludes_all_non_bot(self):
        """An empty set means no users are allowed — only bot messages survive."""
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_ANYONE", "ts": "1000.0", "text": "hello"},
                {"user": "UBOT123", "ts": "1000.5", "text": "response"},
            ]
        }

        history = await _fetch_thread_history(
            "C_CHAN", "1000.0", "1001.0", client,
            allowed_users=set(),
        )
        assert history is not None
        assert "hello" not in history
        assert "[Chicane] response" in history


# ---------------------------------------------------------------------------
# Stale session detection and context rebuild
# ---------------------------------------------------------------------------


class TestStaleSessionDetection:
    """When a resumed session is stale (SDK returns different session_id),
    the bot should notify the user and inject thread history."""

    @pytest.mark.asyncio
    async def test_stale_session_notifies_user(self, config, sessions):
        """When the SDK returns a different session_id, a warning is posted."""
        requested_sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        actual_sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        async def fake_stream(prompt):
            yield make_event(
                "system", subtype="init", session_id=actual_sid
            )
            yield make_event("assistant", text="I'm here")
            yield make_event(
                "result", text="done",
                num_turns=1, duration_ms=100, duration_api_ms=80,
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.run = AsyncMock(return_value="OK")
        mock_session.session_id = actual_sid

        client = mock_client()
        # Thread history for the reconnect scan:
        # First call: _find_session_id_in_thread
        # Second call: _fetch_thread_history (pre-fetch)
        # The mock returns the same for all calls.
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UBOT123", "ts": "1000.0",
                 "text": f"_(session_id: {requested_sid})_"},
                {"user": "UHUMAN1", "ts": "1000.5", "text": "do something"},
                {"user": "UBOT123", "ts": "1000.8", "text": "done"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}

        info = mock_session_info(mock_session, thread_ts="1000.0")

        with (
            patch.object(sessions, "get_or_create", return_value=info),
            patch.object(sessions, "has", return_value=False),
        ):
            event = {
                "ts": "1001.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "continue",
            }
            await _process_message(event, "continue", client, config, sessions)

        # Check that a warning about stale session was posted
        post_calls = client.chat_postMessage.call_args_list
        stale_messages = [
            c for c in post_calls
            if "Couldn't restore previous session" in str(c)
        ]
        assert len(stale_messages) >= 1

    @pytest.mark.asyncio
    async def test_stale_session_injects_context(self, config, sessions):
        """When session is stale, thread history is injected via session.run()."""
        requested_sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        actual_sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        async def fake_stream(prompt):
            yield make_event(
                "system", subtype="init", session_id=actual_sid
            )
            yield make_event("assistant", text="I'm here")
            yield make_event(
                "result", text="done",
                num_turns=1, duration_ms=100, duration_api_ms=80,
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.run = AsyncMock(return_value="OK")
        mock_session.session_id = actual_sid

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UBOT123", "ts": "1000.0",
                 "text": f"_(session_id: {requested_sid})_"},
                {"user": "UHUMAN1", "ts": "1000.5", "text": "do something"},
                {"user": "UBOT123", "ts": "1000.8", "text": "done"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}

        info = mock_session_info(mock_session, thread_ts="1000.0")

        with (
            patch.object(sessions, "get_or_create", return_value=info),
            patch.object(sessions, "has", return_value=False),
        ):
            event = {
                "ts": "1001.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "continue",
            }
            await _process_message(event, "continue", client, config, sessions)

        # session.run should have been called with thread history
        mock_session.run.assert_called_once()
        context_arg = mock_session.run.call_args[0][0]
        assert "THREAD HISTORY" in context_arg
        assert "UNTRUSTED DATA" in context_arg

    @pytest.mark.asyncio
    async def test_matching_session_id_no_stale_warning(self, config, sessions):
        """When session_id matches, no stale warning should be posted."""
        session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

        async def fake_stream(prompt):
            yield make_event(
                "system", subtype="init", session_id=session_id
            )
            yield make_event("assistant", text="I'm here")
            yield make_event(
                "result", text="done",
                num_turns=1, duration_ms=100, duration_api_ms=80,
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.run = AsyncMock(return_value="OK")
        mock_session.session_id = session_id

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UBOT123", "ts": "1000.0",
                 "text": f"_(session_id: {session_id})_"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}

        info = mock_session_info(mock_session, thread_ts="1000.0")

        with (
            patch.object(sessions, "get_or_create", return_value=info),
            patch.object(sessions, "has", return_value=False),
        ):
            event = {
                "ts": "1001.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "continue",
            }
            await _process_message(event, "continue", client, config, sessions)

        # No stale warning
        post_calls = client.chat_postMessage.call_args_list
        stale_messages = [
            c for c in post_calls
            if "Couldn't restore" in str(c)
        ]
        assert len(stale_messages) == 0

        # session.run should NOT have been called for context injection
        mock_session.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_session_no_user_history_still_injects_bot_context(
        self, config, sessions
    ):
        """When session is stale and thread has only bot messages (no user
        messages besides current), the bot's own messages still form history
        and get injected."""
        requested_sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        actual_sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        async def fake_stream(prompt):
            yield make_event(
                "system", subtype="init", session_id=actual_sid
            )
            yield make_event("assistant", text="I'm here")
            yield make_event(
                "result", text="done",
                num_turns=1, duration_ms=100, duration_api_ms=80,
            )

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.run = AsyncMock(return_value="OK")
        mock_session.session_id = actual_sid

        client = mock_client()
        # Only the bot's session message — still counts as history
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UBOT123", "ts": "1000.0",
                 "text": f"_(session_id: {requested_sid})_"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}

        info = mock_session_info(mock_session, thread_ts="1000.0")

        with (
            patch.object(sessions, "get_or_create", return_value=info),
            patch.object(sessions, "has", return_value=False),
        ):
            event = {
                "ts": "1001.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "continue",
            }
            await _process_message(event, "continue", client, config, sessions)

        # Bot's own message forms history, so context injection still happens
        mock_session.run.assert_called_once()
        context_arg = mock_session.run.call_args[0][0]
        assert "THREAD HISTORY" in context_arg
