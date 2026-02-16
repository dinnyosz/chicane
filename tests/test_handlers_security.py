"""Tests for security-related handler behavior."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config, save_handoff_session, load_handoff_session
from chicane.handlers import register_handlers, _should_ignore, _download_files
from chicane.sessions import SessionStore
from tests.conftest import (
    capture_app_handlers,
    make_event,
    mock_client,
    mock_session_info,
)


class TestShouldIgnore:
    """Tests for _should_ignore access control."""

    def test_blocks_when_allowed_users_empty(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=[],
        )
        event = {"user": "U_ANYONE"}
        assert _should_ignore(event, config) is True

    def test_blocks_unauthorized_user(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_ALLOWED"],
        )
        event = {"user": "U_BLOCKED"}
        assert _should_ignore(event, config) is True

    def test_allows_authorized_user(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_ALLOWED"],
        )
        event = {"user": "U_ALLOWED"}
        assert _should_ignore(event, config) is False


class TestErrorSanitization:
    """Tests that error messages don't leak internal details to Slack."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_exception_type_shown_not_message(self, app, sessions):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_HUMAN"],
        )
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        client = mock_client()

        mock_session = AsyncMock()

        async def _failing_stream(prompt):
            raise RuntimeError("secret internal path /etc/passwd")
            yield  # makes this an async generator  # noqa: unreachable

        mock_session.stream = _failing_stream
        info = mock_session_info(mock_session)

        with patch.object(sessions, "get_or_create", return_value=info):
            event = {
                "ts": "1000.0",
                "channel": "C_CHAN",
                "user": "U_HUMAN",
                "text": "<@UBOT> do something",
            }
            await mention_handler(event=event, client=client)

        # The error message posted to Slack should NOT contain the internal details
        error_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":x: Error" in c.kwargs.get("text", "")
        ]
        assert len(error_posts) == 1
        error_msg = error_posts[0].kwargs["text"]
        assert "secret internal path" not in error_msg
        assert "/etc/passwd" not in error_msg
        assert "RuntimeError" in error_msg
        assert "Check bot logs" in error_msg


class TestRateLimiting:
    """Tests for per-user rate limiting."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_after_threshold(self, app, sessions):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_HUMAN"],
            rate_limit=3,
        )
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        client = mock_client()

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            # Send 3 messages (within limit)
            for i in range(3):
                event = {
                    "ts": f"{5000 + i}.0",
                    "channel": "C_CHAN",
                    "user": "U_HUMAN",
                    "text": f"<@UBOT> msg {i}",
                }
                await mention_handler(event=event, client=client)

            assert mock_process.call_count == 3

            # 4th message should be rate limited
            event = {
                "ts": "5003.0",
                "channel": "C_CHAN",
                "user": "U_HUMAN",
                "text": "<@UBOT> msg 3",
            }
            await mention_handler(event=event, client=client)
            # Still 3 — the 4th was blocked
            assert mock_process.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_adds_reaction(self, app, sessions):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_HUMAN"],
            rate_limit=1,
        )
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        client = mock_client()

        with patch("chicane.handlers._process_message", new_callable=AsyncMock):
            # First message succeeds
            event1 = {
                "ts": "6000.0",
                "channel": "C_CHAN",
                "user": "U_HUMAN",
                "text": "<@UBOT> first",
            }
            await mention_handler(event=event1, client=client)

            # Second message is rate-limited — should add no_entry_sign
            event2 = {
                "ts": "6001.0",
                "channel": "C_CHAN",
                "user": "U_HUMAN",
                "text": "<@UBOT> second",
            }
            await mention_handler(event=event2, client=client)

        # Check that no_entry_sign reaction was added
        add_calls = client.reactions_add.call_args_list
        no_entry_calls = [
            c for c in add_calls
            if c.kwargs.get("name") == "no_entry_sign"
        ]
        assert len(no_entry_calls) >= 1

    @pytest.mark.asyncio
    async def test_rate_limit_per_user(self, app, sessions):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_ALICE", "U_BOB"],
            rate_limit=1,
        )
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        client = mock_client()

        with patch("chicane.handlers._process_message", new_callable=AsyncMock) as mock_process:
            # Alice's first message
            await mention_handler(
                event={"ts": "7000.0", "channel": "C", "user": "U_ALICE", "text": "<@U> hi"},
                client=client,
            )
            # Bob's first message — should NOT be rate limited by Alice's count
            await mention_handler(
                event={"ts": "7001.0", "channel": "C", "user": "U_BOB", "text": "<@U> hi"},
                client=client,
            )
            assert mock_process.call_count == 2


class TestReconnectHistorySanitization:
    """Tests that reconnect history includes untrusted data boundary markers."""

    @pytest.fixture
    def app(self):
        mock_app = MagicMock()
        self._handlers = capture_app_handlers(mock_app)
        return mock_app

    @pytest.mark.asyncio
    async def test_reconnect_prompt_has_boundary_markers(self, app, sessions):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_HUMAN"],
        )
        register_handlers(app, config, sessions)
        mention_handler = self._handlers["app_mention"]

        client = mock_client()
        # Simulate reconnect: thread_ts != ts, no existing session
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U_HUMAN", "ts": "1000.0", "text": "start"},
                {"user": "UBOT123", "ts": "1000.5", "text": "response"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}
        client.auth_test.return_value = {"user_id": "UBOT123"}

        mock_session = AsyncMock()
        mock_session.stream.return_value = aiter([
            make_event("result", "done", num_turns=1, duration_ms=100, duration_api_ms=80),
        ])
        info = mock_session_info(mock_session, thread_ts="1000.0")

        with patch.object(sessions, "get_or_create", return_value=info):
            with patch.object(sessions, "has", return_value=False):
                event = {
                    "ts": "1001.0",
                    "thread_ts": "1000.0",
                    "channel": "C_CHAN",
                    "user": "U_HUMAN",
                    "text": "<@UBOT> continue",
                }
                await mention_handler(event=event, client=client)

        # Check the prompt passed to session.stream
        if mock_session.stream.called:
            prompt = mock_session.stream.call_args[0][0]
            assert "UNTRUSTED DATA" in prompt
            assert "BEGIN THREAD HISTORY" in prompt
            assert "END THREAD HISTORY" in prompt


class TestSecurityLogging:
    """Tests that security events are logged to chicane.security logger."""

    def test_blocked_user_logged(self, caplog):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["U_ALLOWED"],
        )
        with caplog.at_level("WARNING", logger="chicane.security"):
            _should_ignore({"user": "U_BLOCKED"}, config)
        assert "BLOCKED" in caplog.text
        assert "U_BLOCKED" in caplog.text

    def test_empty_allowed_users_logged(self, caplog):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=[],
        )
        with caplog.at_level("WARNING", logger="chicane.security"):
            _should_ignore({"user": "U_ANYONE"}, config)
        assert "BLOCKED" in caplog.text
        assert "ALLOWED_USERS" in caplog.text


class TestFileDownloadSanitization:
    """Tests that file downloads sanitize filenames to prevent path traversal."""

    @staticmethod
    def _make_fake_download(data: bytes = b"safe content", content_type: str = "text/plain"):
        """Create fake aiohttp session that returns given data for downloads."""
        class _FakeResp:
            status = 200
            async def read(self):
                return data
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
        _FakeResp.content_type = content_type

        class _FakeHttp:
            def get(self, url, **kw):
                return _FakeResp()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        return _FakeHttp()

    @pytest.mark.asyncio
    async def test_traversal_filename_stripped(self, tmp_path):
        """A filename like ../../etc/passwd should be sanitized to 'passwd'."""
        event = {
            "files": [{
                "name": "../../etc/passwd",
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/fake",
                "size": 100,
            }],
        }

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=self._make_fake_download()):
            result = await _download_files(event, "xoxb-test", tmp_path)

        assert len(result) == 1
        name, local_path, mime = result[0]
        # Path should be inside tmp_path, not traversed
        assert local_path.parent == tmp_path
        assert local_path.name == "passwd"
        assert not str(local_path).startswith("/etc")

    @pytest.mark.asyncio
    async def test_empty_filename_gets_default(self, tmp_path):
        """A filename that resolves to empty after sanitization gets a default."""
        event = {
            "files": [{
                "name": "../../",
                "mimetype": "application/octet-stream",
                "url_private_download": "https://files.slack.com/fake",
                "size": 100,
            }],
        }

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=self._make_fake_download(b"data", "application/octet-stream")):
            result = await _download_files(event, "xoxb-test", tmp_path)

        assert len(result) == 1
        _, local_path, _ = result[0]
        assert local_path.name == "attachment"


class TestHandoffSessionMap:
    """Tests for persistent handoff session_id storage."""

    def test_save_and_load(self, tmp_path):
        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            save_handoff_session("1000.0", "abc-def-123")
            assert load_handoff_session("1000.0") == "abc-def-123"
            assert load_handoff_session("9999.0") is None

    def test_load_missing_file(self, tmp_path):
        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "nope.json"):
            assert load_handoff_session("1000.0") is None

    def test_trims_old_entries(self, tmp_path):
        with patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"):
            with patch("chicane.config._HANDOFF_MAP_MAX", 5):
                for i in range(10):
                    save_handoff_session(f"{i}.0", f"sess-{i}")
                # Only last 5 should remain
                assert load_handoff_session("0.0") is None
                assert load_handoff_session("9.0") == "sess-9"


# Helper for async iteration in tests
async def aiter(items):
    for item in items:
        yield item
