"""Tests for chicane.sessions."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.sessions import _build_system_prompt, _run_pre_cleanup, SessionStore


@pytest.fixture
def config():
    return Config(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        base_directory=Path("/tmp/projects"),
    )


@pytest.fixture
def store():
    return SessionStore()


class TestSessionStore:
    def test_create_new_session_with_cwd(self, store, config):
        info = store.get_or_create("thread-1", config, cwd=Path("/tmp/projects"))
        assert info is not None
        assert info.session.cwd == Path("/tmp/projects")

    def test_reuse_existing_session(self, store, config):
        s1 = store.get_or_create("thread-1", config)
        s2 = store.get_or_create("thread-1", config)
        assert s1 is s2

    def test_different_threads_different_sessions(self, store, config):
        s1 = store.get_or_create("thread-1", config)
        s2 = store.get_or_create("thread-2", config)
        assert s1 is not s2

    def test_custom_cwd(self, store, config):
        info = store.get_or_create("thread-1", config, cwd=Path("/tmp/other"))
        assert info.session.cwd == Path("/tmp/other")

    def test_set_cwd(self, store, config):
        info = store.get_or_create("thread-1", config)
        assert store.set_cwd("thread-1", Path("/tmp/new"))
        assert info.session.cwd == Path("/tmp/new")

    def test_set_cwd_nonexistent_thread(self, store):
        assert store.set_cwd("nope", Path("/tmp")) is False

    @pytest.mark.asyncio
    async def test_remove(self, store, config):
        from unittest.mock import AsyncMock
        info = store.get_or_create("thread-1", config)
        info.session.disconnect = AsyncMock()
        await store.remove("thread-1")
        info.session.disconnect.assert_awaited_once()
        # Next call should create a new session
        info2 = store.get_or_create("thread-1", config)
        assert info2.session.session_id is None  # Fresh session

    @pytest.mark.asyncio
    async def test_cleanup_old_sessions(self, store, config):
        from unittest.mock import AsyncMock
        store.get_or_create("old-thread", config)
        store._sessions["old-thread"].session.disconnect = AsyncMock()
        # Manually age the session
        store._sessions["old-thread"].last_used = datetime.now() - timedelta(hours=25)
        store.get_or_create("new-thread", config)

        removed = await store.cleanup(max_age_hours=24)
        assert removed == 1
        assert "old-thread" not in store._sessions
        assert "new-thread" in store._sessions

    @pytest.mark.asyncio
    async def test_cleanup_keeps_recent(self, store, config):
        store.get_or_create("thread-1", config)
        removed = await store.cleanup(max_age_hours=24)
        assert removed == 0

    @pytest.mark.asyncio
    async def test_cleanup_skips_streaming_sessions(self, store, config):
        """cleanup() must skip sessions where is_streaming is True."""
        from unittest.mock import AsyncMock

        # Create two sessions and age them both past the threshold
        info_streaming = store.get_or_create("streaming-thread", config, cwd=Path("/tmp/s"))
        info_idle = store.get_or_create("idle-thread", config, cwd=Path("/tmp/i"))

        info_streaming.session.disconnect = AsyncMock()
        info_idle.session.disconnect = AsyncMock()

        # Age both sessions to 48 hours ago
        aged = datetime.now() - timedelta(hours=48)
        info_streaming.last_used = aged
        info_idle.last_used = aged

        # Set the internal _is_streaming flag (backing the is_streaming property)
        info_streaming.session._is_streaming = True
        info_idle.session._is_streaming = False

        removed = await store.cleanup(max_age_hours=24)

        assert removed == 1
        assert "streaming-thread" in store._sessions
        assert "idle-thread" not in store._sessions
        info_idle.session.disconnect.assert_awaited_once()
        info_streaming.session.disconnect.assert_not_awaited()

    def test_falls_back_to_temp_dir_when_no_cwd(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        store = SessionStore()
        info = store.get_or_create("thread-1", config)
        assert str(info.session.cwd).startswith("/tmp/chicane-") or "chicane-" in str(info.session.cwd)
        assert info.is_temp_dir is True

    def test_explicit_cwd_not_temp(self, store, config):
        info = store.get_or_create("thread-1", config, cwd=Path("/tmp/projects"))
        assert info.is_temp_dir is False

    @pytest.mark.asyncio
    async def test_remove_cleans_up_temp_dir(self):
        from unittest.mock import AsyncMock
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        store = SessionStore()
        info = store.get_or_create("thread-1", config)
        info.session.disconnect = AsyncMock()
        temp_path = info.cwd
        assert temp_path.exists()

        await store.remove("thread-1")
        assert not temp_path.exists()

    @pytest.mark.asyncio
    async def test_remove_preserves_non_temp_dir(self, store, config, tmp_path):
        from unittest.mock import AsyncMock
        work_dir = tmp_path / "myproject"
        work_dir.mkdir()
        info = store.get_or_create("thread-1", config, cwd=work_dir)
        info.session.disconnect = AsyncMock()

        await store.remove("thread-1")
        assert work_dir.exists()  # Should NOT be deleted

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up_temp_dirs(self):
        from unittest.mock import AsyncMock
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        store = SessionStore()
        info = store.get_or_create("thread-1", config)
        info.session.disconnect = AsyncMock()
        temp_path = info.cwd

        await store.shutdown()
        assert not temp_path.exists()

    def test_sessions_include_slack_system_prompt(self, store, config):
        info = store.get_or_create("thread-1", config)
        assert info.session.system_prompt is not None
        assert "Slack" in info.session.system_prompt

    def test_create_with_explicit_session_id(self, store, config):
        """When session_id is passed, the ClaudeSession should use --resume."""
        info = store.get_or_create(
            "thread-1", config, session_id="abc-123-def"
        )
        assert info.session.session_id == "abc-123-def"

    def test_explicit_session_id_not_used_on_reuse(self, store, config):
        """An existing session is returned as-is; session_id doesn't override it."""
        s1 = store.get_or_create("thread-1", config)
        s2 = store.get_or_create(
            "thread-1", config, session_id="should-be-ignored"
        )
        assert s1 is s2
        assert s2.session.session_id is None  # original session had no id

    def test_session_id_with_cwd(self, store, config):
        """session_id and cwd can be provided together."""
        info = store.get_or_create(
            "thread-1", config, cwd=Path("/tmp/work"), session_id="sess-42"
        )
        assert info.session.session_id == "sess-42"
        assert info.session.cwd == Path("/tmp/work")

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_all_sessions(self, store, config):
        from unittest.mock import AsyncMock
        s1 = store.get_or_create("thread-1", config, cwd=Path("/tmp/a"))
        s2 = store.get_or_create("thread-2", config, cwd=Path("/tmp/b"))
        s1.session.disconnect = AsyncMock()
        s2.session.disconnect = AsyncMock()

        await store.shutdown()

        s1.session.disconnect.assert_awaited_once()
        s2.session.disconnect.assert_awaited_once()

    def test_system_prompt_forbids_terminal_suggestions(self, store, config):
        """System prompt must tell Claude to never suggest local actions."""
        info = store.get_or_create("thread-1", config)
        prompt = info.session.system_prompt
        assert "open a terminal" in prompt.lower()
        assert "ONLY interact via Slack" in prompt

    def test_system_prompt_mentions_interactive_tools(self, store, config):
        """System prompt must mention interactive tools and how they're handled."""
        info = store.get_or_create("thread-1", config)
        prompt = info.session.system_prompt
        assert "AskUserQuestion" in prompt
        assert "streamed output mode" in prompt.lower()
        assert "supported" in prompt.lower()

    @pytest.mark.asyncio
    async def test_shutdown_clears_sessions(self, store, config):
        store.get_or_create("thread-1", config, cwd=Path("/tmp/a"))
        store.get_or_create("thread-2", config, cwd=Path("/tmp/b"))

        await store.shutdown()

        assert len(store._sessions) == 0
        assert not store.has("thread-1")
        assert not store.has("thread-2")

    def test_session_info_has_lock(self, store, config):
        """Each SessionInfo should have an asyncio.Lock for concurrency control."""
        import asyncio

        info = store.get_or_create("thread-1", config)
        assert isinstance(info.lock, asyncio.Lock)

    def test_different_threads_have_different_locks(self, store, config):
        """Each thread's SessionInfo should have its own lock."""
        s1 = store.get_or_create("thread-1", config)
        s2 = store.get_or_create("thread-2", config)
        assert s1.lock is not s2.lock

    def test_get_returns_session(self, store, config):
        info = store.get_or_create("thread-1", config)
        assert store.get("thread-1") is info

    def test_get_returns_none_for_unknown(self, store):
        assert store.get("nonexistent") is None

    def test_register_and_lookup_bot_message(self, store, config):
        store.get_or_create("thread-1", config)
        store.register_bot_message("msg-1", "thread-1")
        store.register_bot_message("msg-2", "thread-1")
        assert store.thread_for_message("msg-1") == "thread-1"
        assert store.thread_for_message("msg-2") == "thread-1"
        assert store.thread_for_message("msg-unknown") is None

    @pytest.mark.asyncio
    async def test_remove_cleans_up_message_entries(self, store, config):
        from unittest.mock import AsyncMock
        info = store.get_or_create("thread-1", config)
        info.session.disconnect = AsyncMock()
        store.register_bot_message("msg-1", "thread-1")
        store.register_bot_message("msg-2", "thread-1")
        await store.remove("thread-1")
        assert store.thread_for_message("msg-1") is None
        assert store.thread_for_message("msg-2") is None

    @pytest.mark.asyncio
    async def test_cleanup_removes_orphaned_message_entries(self, store, config):
        from unittest.mock import AsyncMock
        store.get_or_create("old-thread", config)
        store._sessions["old-thread"].session.disconnect = AsyncMock()
        store.register_bot_message("msg-old", "old-thread")
        store._sessions["old-thread"].last_used = datetime.now() - timedelta(hours=25)

        store.get_or_create("new-thread", config)
        store.register_bot_message("msg-new", "new-thread")

        await store.cleanup(max_age_hours=24)
        assert store.thread_for_message("msg-old") is None
        assert store.thread_for_message("msg-new") == "new-thread"

    @pytest.mark.asyncio
    async def test_shutdown_clears_message_entries(self, store, config):
        from unittest.mock import AsyncMock
        store.get_or_create("thread-1", config, cwd=Path("/tmp/a"))
        store.register_bot_message("msg-1", "thread-1")
        store._sessions["thread-1"].session.disconnect = AsyncMock()

        await store.shutdown()

        assert store.thread_for_message("msg-1") is None


class TestBuildSystemPrompt:
    """Tests for _build_system_prompt verbosity adaptation."""

    def test_minimal_hides_tool_calls(self):
        prompt = _build_system_prompt("minimal")
        assert "NOT shown" in prompt
        assert "paste it in your reply" in prompt

    def test_normal_shows_activity_not_output(self):
        prompt = _build_system_prompt("normal")
        assert "tool activity indicators" in prompt
        assert "NOT tool output" in prompt

    def test_verbose_shows_everything(self):
        prompt = _build_system_prompt("verbose")
        assert "tool activity indicators" in prompt
        assert "tool output" in prompt
        assert "don't need to repeat" in prompt.lower()

    def test_default_is_verbose(self):
        assert _build_system_prompt() == _build_system_prompt("verbose")

    def test_unknown_verbosity_falls_back_to_verbose(self):
        assert _build_system_prompt("unknown") == _build_system_prompt("verbose")

    def test_all_levels_include_core_sections(self):
        for level in ("minimal", "normal", "verbose"):
            prompt = _build_system_prompt(level)
            assert "Chicane" in prompt
            assert "Slack" in prompt
            assert "SECURITY" in prompt
            assert "SAFETY" in prompt

    def test_verbosity_passed_from_config(self):
        """Config verbosity should flow through to the system prompt."""
        for level in ("minimal", "normal", "verbose"):
            config = Config(
                slack_bot_token="xoxb-test",
                slack_app_token="xapp-test",
                verbosity=level,
            )
            store = SessionStore()
            info = store.get_or_create("thread-1", config, cwd=Path("/tmp/test"))
            expected = _build_system_prompt(level)
            assert info.session.system_prompt == expected


class TestPreCleanupCommand:
    """Tests for running a pre-cleanup command before closing idle sessions."""

    @pytest.fixture
    def config_with_command(self):
        return Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            base_directory=Path("/tmp/projects"),
            session_cleanup_command="run /context-summary",
        )

    @pytest.fixture
    def config_no_command(self):
        return Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            base_directory=Path("/tmp/projects"),
        )

    @pytest.mark.asyncio
    async def test_cleanup_runs_command_before_disconnect(self, config_with_command):
        store = SessionStore()
        info = store.get_or_create("old-thread", config_with_command, cwd=Path("/tmp/a"))
        info.session.disconnect = AsyncMock()
        info.channel = "C_TEST"
        info.last_used = datetime.now() - timedelta(hours=25)

        async def fake_stream(prompt):
            assert prompt == "run /context-summary"
            for item in []:
                yield item

        info.session.stream = fake_stream

        mock_client = AsyncMock()
        removed = await store.cleanup(
            max_age_hours=24,
            config=config_with_command,
            client=mock_client,
        )

        assert removed == 1
        info.session.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_without_command_skips_stream(self, config_no_command):
        store = SessionStore()
        info = store.get_or_create("old-thread", config_no_command, cwd=Path("/tmp/a"))
        info.session.disconnect = AsyncMock()
        info.session.stream = AsyncMock()
        info.last_used = datetime.now() - timedelta(hours=25)

        removed = await store.cleanup(max_age_hours=24, config=config_no_command)

        assert removed == 1
        info.session.stream.assert_not_awaited()
        info.session.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_command_error_still_disconnects(self, config_with_command):
        store = SessionStore()
        info = store.get_or_create("old-thread", config_with_command, cwd=Path("/tmp/a"))
        info.session.disconnect = AsyncMock()
        info.channel = "C_TEST"
        info.last_used = datetime.now() - timedelta(hours=25)

        async def exploding_stream(prompt):
            raise RuntimeError("SDK crash")
            # Make it a generator
            yield  # pragma: no cover

        info.session.stream = exploding_stream

        mock_client = AsyncMock()
        removed = await store.cleanup(
            max_age_hours=24,
            config=config_with_command,
            client=mock_client,
        )

        assert removed == 1
        info.session.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_command_timeout_still_disconnects(self, config_with_command):
        store = SessionStore()
        info = store.get_or_create("old-thread", config_with_command, cwd=Path("/tmp/a"))
        info.session.disconnect = AsyncMock()
        info.channel = "C_TEST"
        info.last_used = datetime.now() - timedelta(hours=25)

        async def slow_stream(prompt):
            await asyncio.sleep(9999)
            yield  # pragma: no cover

        info.session.stream = slow_stream

        mock_client = AsyncMock()
        with patch("chicane.sessions._CLEANUP_COMMAND_TIMEOUT", 0.01):
            removed = await store.cleanup(
                max_age_hours=24,
                config=config_with_command,
                client=mock_client,
            )

        assert removed == 1
        info.session.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_posts_notifications(self, config_with_command):
        store = SessionStore()
        info = store.get_or_create("old-thread", config_with_command, cwd=Path("/tmp/a"))
        info.session.disconnect = AsyncMock()
        info.channel = "C_TEST"
        info.last_used = datetime.now() - timedelta(hours=25)

        async def fake_stream(prompt):
            for item in []:
                yield item

        info.session.stream = fake_stream

        mock_client = AsyncMock()
        await store.cleanup(
            max_age_hours=24,
            config=config_with_command,
            client=mock_client,
        )

        # Should post: pre-cleanup notice + session closed notice
        assert mock_client.chat_postMessage.call_count == 2
        calls = mock_client.chat_postMessage.call_args_list
        assert ":broom:" in calls[0].kwargs["text"]
        assert ":wave:" in calls[1].kwargs["text"]

    @pytest.mark.asyncio
    async def test_cleanup_no_channel_skips_notifications(self, config_with_command):
        store = SessionStore()
        info = store.get_or_create("old-thread", config_with_command, cwd=Path("/tmp/a"))
        info.session.disconnect = AsyncMock()
        # channel is None (default)
        info.last_used = datetime.now() - timedelta(hours=25)

        async def fake_stream(prompt):
            for item in []:
                yield item

        info.session.stream = fake_stream

        mock_client = AsyncMock()
        removed = await store.cleanup(
            max_age_hours=24,
            config=config_with_command,
            client=mock_client,
        )

        assert removed == 1
        mock_client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_no_config_skips_command(self):
        """Backward compatibility: cleanup() without config just disconnects."""
        store = SessionStore()
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            base_directory=Path("/tmp/projects"),
        )
        info = store.get_or_create("old-thread", config, cwd=Path("/tmp/a"))
        info.session.disconnect = AsyncMock()
        info.session.stream = AsyncMock()
        info.last_used = datetime.now() - timedelta(hours=25)

        removed = await store.cleanup(max_age_hours=24)

        assert removed == 1
        info.session.stream.assert_not_awaited()
        info.session.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_pre_cleanup_noop_when_no_command(self, config_no_command):
        """_run_pre_cleanup does nothing when session_cleanup_command is empty."""
        info = MagicMock()
        info.session.stream = AsyncMock()
        await _run_pre_cleanup(info, "thread-1", config_no_command, None)
        info.session.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_pre_cleanup_noop_when_no_config(self):
        """_run_pre_cleanup does nothing when config is None."""
        info = MagicMock()
        info.session.stream = AsyncMock()
        await _run_pre_cleanup(info, "thread-1", None, None)
        info.session.stream.assert_not_called()
