"""Tests for chicane.purge — bot message deletion."""

import time
from argparse import Namespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.purge import (
    _cutoff_ts,
    _delete_messages,
    _filter_messages,
    _get_bot_channels,
    _get_bot_user_id,
    _purge_channel,
    _slack_call,
)


BOT_ID = "UBOT123"
CHANNEL_ID = "C001"


def _ts(days_ago: float) -> str:
    """Return a Slack-style timestamp N days in the past."""
    return str(time.time() - days_ago * 86400)


def _msg(user: str, ts: str, *, thread_ts: str | None = None, reply_count: int = 0) -> dict:
    msg = {"user": user, "ts": ts, "text": "hello"}
    if thread_ts:
        msg["thread_ts"] = thread_ts
    if reply_count:
        msg["reply_count"] = reply_count
    return msg


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestGetBotUserId:
    @pytest.mark.asyncio
    async def test_returns_user_id(self):
        client = AsyncMock()
        client.auth_test.return_value = {"user_id": BOT_ID}
        with patch("chicane.purge._API_INTERVAL", 0):
            assert await _get_bot_user_id(client) == BOT_ID


class TestGetBotChannels:
    _empty = {"channels": [], "response_metadata": {"next_cursor": ""}}

    @pytest.mark.asyncio
    async def test_returns_channels(self):
        client = AsyncMock()
        # First type (public_channel) returns results, rest empty
        client.users_conversations.side_effect = [
            {
                "channels": [
                    {"id": "C001", "name": "general"},
                    {"id": "C002", "name": "dev"},
                ],
                "response_metadata": {"next_cursor": ""},
            },
            self._empty, self._empty, self._empty,
        ]
        with patch("chicane.purge._API_INTERVAL", 0):
            result = await _get_bot_channels(client)
        assert len(result) == 2
        assert result[0]["name"] == "general"

    @pytest.mark.asyncio
    async def test_filters_by_channel_name(self):
        client = AsyncMock()
        client.users_conversations.side_effect = [
            {
                "channels": [
                    {"id": "C001", "name": "general"},
                    {"id": "C002", "name": "dev"},
                ],
                "response_metadata": {"next_cursor": ""},
            },
            self._empty, self._empty, self._empty,
        ]
        with patch("chicane.purge._API_INTERVAL", 0):
            result = await _get_bot_channels(client, channel_filter="dev")
        assert len(result) == 1
        assert result[0]["name"] == "dev"

    @pytest.mark.asyncio
    async def test_paginates(self):
        client = AsyncMock()
        client.users_conversations.side_effect = [
            # public_channel: 2 pages
            {
                "channels": [{"id": "C001", "name": "page1"}],
                "response_metadata": {"next_cursor": "cursor1"},
            },
            {
                "channels": [{"id": "C002", "name": "page2"}],
                "response_metadata": {"next_cursor": ""},
            },
            # private_channel, im, mpim: empty
            self._empty, self._empty, self._empty,
        ]
        with patch("chicane.purge._API_INTERVAL", 0):
            result = await _get_bot_channels(client)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_skips_missing_scope(self):
        """Missing scope for a type doesn't block other types."""
        from slack_sdk.errors import SlackApiError

        scope_err = MagicMock()
        scope_err.response.status_code = 200
        scope_err.response.data = {"error": "missing_scope"}

        client = AsyncMock()
        client.users_conversations.side_effect = [
            # public_channel: works
            {
                "channels": [{"id": "C001", "name": "general"}],
                "response_metadata": {"next_cursor": ""},
            },
            # private_channel: works
            self._empty,
            # im: works
            self._empty,
            # mpim: missing scope
            SlackApiError("err", scope_err.response),
        ]
        with patch("chicane.purge._API_INTERVAL", 0):
            result = await _get_bot_channels(client)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Filter bot messages
# ---------------------------------------------------------------------------


class TestFilterMessages:
    def test_filters_by_user_and_age(self):
        old_ts = _ts(45)
        new_ts = _ts(5)
        cutoff = _cutoff_ts(30)
        messages = [
            _msg(BOT_ID, old_ts),
            _msg(BOT_ID, new_ts),
            _msg("UHUMAN", old_ts),
        ]
        result = _filter_messages(messages, BOT_ID, cutoff, False)
        assert len(result) == 1
        assert result[0]["ts"] == old_ts

    def test_skips_non_bot(self):
        result = _filter_messages(
            [_msg("UHUMAN", _ts(45))], BOT_ID, _cutoff_ts(30), False,
        )
        assert len(result) == 0

    def test_delete_all_includes_user_messages(self):
        old_ts = _ts(45)
        cutoff = _cutoff_ts(30)
        messages = [
            _msg(BOT_ID, old_ts),
            _msg("UHUMAN", old_ts),
        ]
        result = _filter_messages(messages, BOT_ID, cutoff, False, delete_all=True)
        assert len(result) == 2

    def test_keep_parent_skips_thread_parent(self):
        parent_ts = _ts(45)
        reply_ts = _ts(44)
        messages = [
            _msg(BOT_ID, parent_ts, thread_ts=parent_ts),
            _msg(BOT_ID, reply_ts, thread_ts=parent_ts),
        ]
        result = _filter_messages(
            messages, BOT_ID, _cutoff_ts(30), True, thread_ts=parent_ts,
        )
        assert len(result) == 1
        assert result[0]["ts"] == reply_ts


# ---------------------------------------------------------------------------
# Delete messages
# ---------------------------------------------------------------------------


class TestDeleteMessages:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_api(self):
        client = AsyncMock()
        messages = [_msg(BOT_ID, _ts(45)), _msg(BOT_ID, _ts(40))]

        deleted, failed = await _delete_messages(
            client, CHANNEL_ID, messages, True,
        )
        assert deleted == 2
        assert failed == 0
        client.chat_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_calls_chat_delete(self):
        client = AsyncMock()
        client.chat_delete.return_value = {"ok": True}
        messages = [_msg(BOT_ID, "1.0"), _msg(BOT_ID, "2.0")]

        with patch("chicane.purge._API_INTERVAL", 0):
            deleted, failed = await _delete_messages(
                client, CHANNEL_ID, messages, False,
            )
        assert deleted == 2
        assert failed == 0
        assert client.chat_delete.call_count == 2

    @pytest.mark.asyncio
    async def test_uses_user_client_for_non_bot_messages(self):
        """User messages are deleted via user_client, bot messages via bot_client."""
        bot_client = AsyncMock()
        bot_client.chat_delete.return_value = {"ok": True}
        user_client = AsyncMock()
        user_client.chat_delete.return_value = {"ok": True}

        messages = [
            _msg(BOT_ID, "1.0"),
            _msg("UHUMAN", "2.0"),
        ]

        with patch("chicane.purge._API_INTERVAL", 0):
            deleted, failed = await _delete_messages(
                bot_client, CHANNEL_ID, messages, False,
                bot_user_id=BOT_ID, user_client=user_client,
            )
        assert deleted == 2
        assert bot_client.chat_delete.call_count == 1
        assert user_client.chat_delete.call_count == 1

    @pytest.mark.asyncio
    async def test_handles_message_not_found(self):
        """Already-deleted messages are silently skipped."""
        client = AsyncMock()
        exc = MagicMock()
        exc.response.status_code = 200
        exc.response.data = {"error": "message_not_found"}

        from slack_sdk.errors import SlackApiError
        client.chat_delete.side_effect = SlackApiError("err", exc.response)

        messages = [_msg(BOT_ID, "1.0")]

        with patch("chicane.purge._API_INTERVAL", 0):
            deleted, failed = await _delete_messages(
                client, CHANNEL_ID, messages, False,
            )
        assert deleted == 0
        assert failed == 0

    @pytest.mark.asyncio
    async def test_handles_rate_limit(self):
        """429 triggers retry after backoff via _slack_call."""
        client = AsyncMock()

        rate_exc = MagicMock()
        rate_exc.response.status_code = 429
        rate_exc.response.headers = {"Retry-After": "0"}
        rate_exc.response.data = {"error": "ratelimited"}

        from slack_sdk.errors import SlackApiError

        client.chat_delete.side_effect = [
            SlackApiError("rate", rate_exc.response),
            {"ok": True},
        ]

        messages = [_msg(BOT_ID, "1.0")]

        with patch("chicane.purge._API_INTERVAL", 0):
            deleted, failed = await _delete_messages(
                client, CHANNEL_ID, messages, False,
            )
        assert deleted == 1
        assert failed == 0
        assert client.chat_delete.call_count == 2


# ---------------------------------------------------------------------------
# _slack_call
# ---------------------------------------------------------------------------


class TestSlackCall:
    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        from slack_sdk.errors import SlackApiError

        rate_exc = MagicMock()
        rate_exc.response.status_code = 429
        rate_exc.response.headers = {"Retry-After": "0"}

        method = AsyncMock(side_effect=[
            SlackApiError("rate", rate_exc.response),
            SlackApiError("rate", rate_exc.response),
            {"ok": True},
        ])

        with patch("chicane.purge._API_INTERVAL", 0):
            result = await _slack_call(method, channel="C001")
        assert result == {"ok": True}
        assert method.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_non_429_errors(self):
        from slack_sdk.errors import SlackApiError

        err = MagicMock()
        err.response.status_code = 400
        err.response.data = {"error": "invalid_auth"}

        method = AsyncMock(side_effect=SlackApiError("bad", err.response))

        with (
            patch("chicane.purge._API_INTERVAL", 0),
            pytest.raises(SlackApiError),
        ):
            await _slack_call(method)


# ---------------------------------------------------------------------------
# Purge channel (scan-delete interleaved)
# ---------------------------------------------------------------------------


class TestPurgeChannel:
    @pytest.mark.asyncio
    async def test_scans_threads_and_deletes_bot_replies(self):
        """Interleaved scan-delete: fetches thread, deletes bot replies immediately."""
        parent_ts = _ts(45)
        reply_ts = _ts(44)

        client = AsyncMock()
        client.conversations_history.return_value = {
            "messages": [
                _msg("UHUMAN", parent_ts, thread_ts=parent_ts, reply_count=1),
            ],
            "response_metadata": {"next_cursor": ""},
        }
        client.conversations_replies.return_value = {
            "messages": [
                _msg("UHUMAN", parent_ts, thread_ts=parent_ts),
                _msg(BOT_ID, reply_ts, thread_ts=parent_ts),
            ],
            "response_metadata": {"next_cursor": ""},
        }
        client.chat_delete.return_value = {"ok": True}

        with patch("chicane.purge._API_INTERVAL", 0):
            scanned, deleted, failed = await _purge_channel(
                client, CHANNEL_ID, "test", BOT_ID,
                _cutoff_ts(30), False, False,
            )
        assert scanned == 1
        assert deleted == 1
        assert failed == 0
        client.chat_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_delete(self):
        """Dry run scans but doesn't call chat.delete."""
        old_ts = _ts(45)
        client = AsyncMock()
        client.conversations_history.return_value = {
            "messages": [_msg(BOT_ID, old_ts)],
            "response_metadata": {"next_cursor": ""},
        }

        with patch("chicane.purge._API_INTERVAL", 0):
            scanned, deleted, failed = await _purge_channel(
                client, CHANNEL_ID, "test", BOT_ID,
                _cutoff_ts(30), False, True,
            )
        assert scanned == 1
        assert deleted == 1
        assert failed == 0
        client.chat_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_keep_parent_with_replies(self):
        """keep_parent preserves top-level messages that have replies."""
        parent_ts = _ts(45)
        client = AsyncMock()
        client.conversations_history.return_value = {
            "messages": [_msg(BOT_ID, parent_ts, reply_count=3)],
            "response_metadata": {"next_cursor": ""},
        }

        with patch("chicane.purge._API_INTERVAL", 0):
            scanned, deleted, failed = await _purge_channel(
                client, CHANNEL_ID, "test", BOT_ID,
                _cutoff_ts(30), True, True,
            )
        assert deleted == 0


# ---------------------------------------------------------------------------
# Full purge flow
# ---------------------------------------------------------------------------


class TestPurgeCommand:
    @pytest.mark.asyncio
    async def test_dry_run_no_deletions(self):
        """End-to-end dry-run produces zero actual API delete calls."""
        from chicane.purge import _purge

        args = Namespace(confirm=False, channel=None, older_than=30, keep_parent=False)

        mock_client = AsyncMock()
        mock_client.auth_test.return_value = {"user_id": BOT_ID}
        mock_client.users_conversations.return_value = {
            "channels": [{"id": "C001", "name": "test"}],
            "response_metadata": {"next_cursor": ""},
        }
        mock_client.conversations_history.return_value = {
            "messages": [_msg(BOT_ID, _ts(45))],
            "response_metadata": {"next_cursor": ""},
        }

        with (
            patch("chicane.purge.Config.from_env") as mock_config,
            patch("chicane.purge.AsyncWebClient", return_value=mock_client),
            patch("chicane.purge.console"),
            patch("chicane.purge._API_INTERVAL", 0),
        ):
            mock_config.return_value = MagicMock(
                slack_bot_token="xoxb-test",
                purge_older_than_days=30,
            )
            await _purge(args)

        mock_client.chat_delete.assert_not_called()
