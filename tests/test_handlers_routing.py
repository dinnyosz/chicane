"""Tests for handler routing: event dispatch, dedup, DMs, file_share."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.handlers import register_handlers, _process_message
from chicane.sessions import SessionStore
from tests.conftest import capture_app_handlers, mock_client


class TestThreadMentionRouting:
    """Test thread reply routing for both @mentions and plain follow-ups."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_mention_in_unknown_thread_is_processed(self, app, config, sessions):
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "some unrelated message"},
            ]
        }
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> help me",
        }

        await message_handler(event=event, client=client)

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await mention_handler(event=event, client=client)
            mock_process.assert_called_once()
            assert mock_process.call_args[0][1] == "help me"

    @pytest.mark.asyncio
    async def test_thread_followup_in_known_session_prevents_double_processing(
        self, app, config, sessions
    ):
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        sessions.get_or_create("1000.0", config)

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> follow up",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await message_handler(event=event, client=client)
            assert mock_process.call_count == 1

            await mention_handler(event=event, client=client)
            assert mock_process.call_count == 1

    @pytest.mark.asyncio
    async def test_plain_thread_reply_without_mention_is_processed(
        self, app, config, sessions
    ):
        register_handlers(app, config, sessions)

        message_handler = self._handlers["message"]
        sessions.get_or_create("1000.0", config)

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "follow up without mentioning the bot",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await message_handler(event=event, client=client)
            mock_process.assert_called_once()
            assert mock_process.call_args[0][1] == "follow up without mentioning the bot"

    @pytest.mark.asyncio
    async def test_plain_thread_reply_bot_in_history_is_processed(
        self, app, config, sessions
    ):
        register_handlers(app, config, sessions)

        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "1000.0", "text": "start"},
                {"user": "UBOT123", "ts": "1000.5", "text": "I'm here"},
            ]
        }
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }
        client.conversations_history.return_value = {"messages": []}

        event = {
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "continue working on this",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await message_handler(event=event, client=client)
            mock_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_top_level_mention_not_double_processed(self, app, config, sessions):
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> do something",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await mention_handler(event=event, client=client)
            await message_handler(event=event, client=client)
            assert mock_process.call_count == 1

    @pytest.mark.asyncio
    async def test_top_level_mention_message_first_not_double_processed(
        self, app, config, sessions
    ):
        register_handlers(app, config, sessions)

        mention_handler = self._handlers["app_mention"]
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {
            "channel": {"name": "general"}
        }

        event = {
            "ts": "1001.0",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "user": "UHUMAN1",
            "text": "<@UBOT123> do something",
        }

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            await message_handler(event=event, client=client)
            await mention_handler(event=event, client=client)
            assert mock_process.call_count == 1


class TestHandlerRoutingEdgeCases:
    """Test handler routing edge cases: blocked users, empty text, subtypes, DMs."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_mention_ignored_for_blocked_user(self, app, config_restricted, sessions):
        register_handlers(app, config_restricted, sessions)
        mention_handler = self._handlers["app_mention"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2000.0",
                "channel": "C_CHAN",
                "user": "U_BLOCKED",
                "text": "<@UBOT123> help me",
            }
            await mention_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_with_empty_text_ignored_top_level(self, app, config, sessions):
        """Top-level empty @mention (no thread) should be ignored."""
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "<@UBOT123>",
            }
            await mention_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_mention_in_thread_reply_is_processed(self, app, config, sessions):
        """Empty @mention in a thread reply should still be processed (handoff pickup)."""
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2001.5",
                "thread_ts": "2000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "<@UBOT123>",
            }
            await mention_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()
            # Prompt should be empty string after stripping the @mention
            assert mock_process.call_args[0][1] == ""

    @pytest.mark.asyncio
    async def test_message_subtype_ignored(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2002.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "edited text",
                "subtype": "message_changed",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_empty_text_ignored(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2003.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_processed(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2004.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "hello in DM",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()
            assert mock_process.call_args[0][1] == "hello in DM"

    @pytest.mark.asyncio
    async def test_dm_blocked_user_ignored(self, app, config_restricted, sessions):
        register_handlers(app, config_restricted, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "2005.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "U_BLOCKED",
                "text": "hello in DM",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_set_bounded(self, app, config, sessions):
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock):
            for i in range(501):
                event = {
                    "ts": f"{3000 + i}.0",
                    "channel": "C_CHAN",
                    "user": "UHUMAN1",
                    "text": f"<@UBOT123> msg {i}",
                }
                await mention_handler(event=event, client=AsyncMock())

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "3000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "text": "<@UBOT123> msg 0",
            }
            await mention_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()


class TestDMRoutingEdgeCases:
    """Test DM-specific routing: dedup and rate limiting."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_dm_duplicate_message_ignored(self, app, config, sessions):
        """Duplicate DM is deduplicated via _mark_processed."""
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "4000.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "hello in DM",
            }
            await message_handler(event=event, client=AsyncMock())
            await message_handler(event=event, client=AsyncMock())
            assert mock_process.call_count == 1

    @pytest.mark.asyncio
    async def test_dm_rate_limited_user_blocked(self, app, sessions):
        """Rate-limited user in DM is blocked."""
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["UHUMAN1"],
            rate_limit=1,
        )
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "9999.0"}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event1 = {
                "ts": "4001.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "first DM",
            }
            await message_handler(event=event1, client=client)

            event2 = {
                "ts": "4002.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "second DM",
            }
            await message_handler(event=event2, client=client)
            assert mock_process.call_count == 1

    @pytest.mark.asyncio
    async def test_dm_blocked_user_via_should_ignore(self, app, config_restricted, sessions):
        """Blocked user in DM hits _should_ignore path."""
        register_handlers(app, config_restricted, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "4003.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "U_BLOCKED",
                "text": "hello",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()


class TestChannelThreadRoutingEdgeCases:
    """Test channel thread routing: dedup, ignore, rate-limit for thread follow-ups."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_thread_followup_duplicate_ignored(self, app, config, sessions):
        """Duplicate thread follow-up is deduplicated."""
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]
        sessions.get_or_create("1000.0", config)

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "1001.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "follow up",
            }
            await message_handler(event=event, client=client)
            await message_handler(event=event, client=client)
            assert mock_process.call_count == 1

    @pytest.mark.asyncio
    async def test_thread_followup_blocked_user_ignored(self, app, config_restricted, sessions):
        """Blocked user in thread follow-up hits _should_ignore."""
        register_handlers(app, config_restricted, sessions)
        message_handler = self._handlers["message"]
        sessions.get_or_create("1000.0", config_restricted)

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "1001.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "U_BLOCKED",
                "text": "hello",
            }
            await message_handler(event=event, client=client)
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_followup_rate_limited(self, app, sessions):
        """Rate-limited user in thread follow-up is blocked."""
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["UHUMAN1"],
            rate_limit=1,
        )
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]
        sessions.get_or_create("1000.0", config)

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event1 = {
                "ts": "1001.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "first",
            }
            await message_handler(event=event1, client=client)

            event2 = {
                "ts": "1002.0",
                "thread_ts": "1000.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "second",
            }
            await message_handler(event=event2, client=client)
            assert mock_process.call_count == 1


class TestBotMentionRoutingEdgeCases:
    """Test bot-mention routing in channels (no thread, bot detected via auth_test)."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_bot_mention_duplicate_ignored(self, app, config, sessions):
        """Duplicate bot mention in channel is deduplicated."""
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "5000.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "<@UBOT123> do something",
            }
            await message_handler(event=event, client=client)
            await message_handler(event=event, client=client)
            assert mock_process.call_count == 1

    @pytest.mark.asyncio
    async def test_bot_mention_blocked_user_ignored(self, app, config_restricted, sessions):
        """Blocked user's bot mention is ignored."""
        register_handlers(app, config_restricted, sessions)
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "5001.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "U_BLOCKED",
                "text": "<@UBOT123> help",
            }
            await message_handler(event=event, client=client)
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_mention_rate_limited(self, app, sessions):
        """Rate-limited user's bot mention is blocked."""
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["UHUMAN1"],
            rate_limit=1,
        )
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.chat_postMessage.return_value = {"ts": "9999.0"}
        client.conversations_info.return_value = {"channel": {"name": "general"}}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event1 = {
                "ts": "5002.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "<@UBOT123> first",
            }
            await message_handler(event=event1, client=client)

            event2 = {
                "ts": "5003.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "<@UBOT123> second",
            }
            await message_handler(event=event2, client=client)
            assert mock_process.call_count == 1


class TestReactionHandlerEdgeCases:
    """Edge cases for the reaction_added handler."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_reaction_on_non_message_item_ignored(self, app, config, sessions):
        """Reaction on non-message item type is ignored."""
        from chicane.handlers import register_handlers
        register_handlers(app, config, sessions)
        reaction_handler = self._handlers["reaction_added"]

        client = AsyncMock()

        event = {
            "reaction": "octagonal_sign",
            "item": {"type": "file", "ts": "1000.0", "channel": "C_CHAN"},
            "user": "UHUMAN1",
        }
        await reaction_handler(event, client)
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_on_session_returns_none(self, app, config, sessions):
        """Reaction where sessions.get() returns None is ignored."""
        from chicane.handlers import register_handlers
        register_handlers(app, config, sessions)
        reaction_handler = self._handlers["reaction_added"]

        # Register a message but don't create a session for it
        # (thread_for_message returns None, sessions.has returns False)
        client = AsyncMock()

        event = {
            "reaction": "octagonal_sign",
            "item": {"type": "message", "ts": "9999.0", "channel": "C_CHAN"},
            "user": "UHUMAN1",
        }
        await reaction_handler(event, client)
        client.chat_postMessage.assert_not_called()


class TestFileShareSubtype:
    """Test that file_share subtype messages are not skipped."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_file_share_subtype_processed_in_dm(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "9000.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "check this file",
                "subtype": "file_share",
                "files": [{"name": "test.py"}],
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_subtypes_still_skipped(self, app, config, sessions):
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "9001.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "edited",
                "subtype": "message_changed",
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_only_no_text_processed_in_dm(self, app, config, sessions):
        """A file upload with no text should still be processed."""
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "9002.0",
                "channel": "D_DM",
                "channel_type": "im",
                "user": "UHUMAN1",
                "text": "",
                "subtype": "file_share",
                "files": [{"name": "data.csv"}],
            }
            await message_handler(event=event, client=AsyncMock())
            mock_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_file_only_with_mention_processed_in_channel(self, app, config, sessions):
        """@mention + file with no other text in a channel should be processed."""
        register_handlers(app, config, sessions)
        message_handler = self._handlers["message"]

        client = AsyncMock()
        client.auth_test.return_value = {"user_id": "UBOT123"}

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            event = {
                "ts": "9003.0",
                "channel": "C_CHAN",
                "channel_type": "channel",
                "user": "UHUMAN1",
                "text": "<@UBOT123>",
                "subtype": "file_share",
                "files": [{"name": "screenshot.png"}],
            }
            await message_handler(event=event, client=client)
            mock_process.assert_called_once()
            # Prompt should be empty string (mention stripped), files handled by _process_message
            call_args = mock_process.call_args
            assert call_args[0][1] == ""  # prompt arg
