"""Tests for handler routing: event dispatch, dedup, DMs, file_share."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.handlers import register_handlers, _process_message
from chicane.sessions import SessionStore
from tests.conftest import capture_app_handlers


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
    async def test_mention_with_empty_text_ignored(self, app, config, sessions):
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
