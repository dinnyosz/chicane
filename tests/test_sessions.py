"""Tests for chicane.sessions."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from chicane.config import Config
from chicane.sessions import SLACK_SYSTEM_PROMPT, SessionStore


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

    def test_remove(self, store, config):
        store.get_or_create("thread-1", config)
        store.remove("thread-1")
        # Next call should create a new session
        info = store.get_or_create("thread-1", config)
        assert info.session.session_id is None  # Fresh session

    def test_cleanup_old_sessions(self, store, config):
        store.get_or_create("old-thread", config)
        # Manually age the session
        store._sessions["old-thread"].last_used = datetime.now() - timedelta(hours=25)
        store.get_or_create("new-thread", config)

        removed = store.cleanup(max_age_hours=24)
        assert removed == 1
        assert "old-thread" not in store._sessions
        assert "new-thread" in store._sessions

    def test_cleanup_keeps_recent(self, store, config):
        store.get_or_create("thread-1", config)
        removed = store.cleanup(max_age_hours=24)
        assert removed == 0

    def test_falls_back_to_temp_dir_when_no_cwd(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        store = SessionStore()
        info = store.get_or_create("thread-1", config)
        assert str(info.session.cwd).startswith("/tmp/chicane-") or "chicane-" in str(info.session.cwd)

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

    def test_shutdown_kills_all_sessions(self, store, config):
        s1 = store.get_or_create("thread-1", config, cwd=Path("/tmp/a"))
        s2 = store.get_or_create("thread-2", config, cwd=Path("/tmp/b"))
        s1.session.kill = MagicMock()
        s2.session.kill = MagicMock()

        store.shutdown()

        s1.session.kill.assert_called_once()
        s2.session.kill.assert_called_once()

    def test_system_prompt_forbids_terminal_suggestions(self, store, config):
        """System prompt must tell Claude to never suggest local actions."""
        info = store.get_or_create("thread-1", config)
        prompt = info.session.system_prompt
        assert "open a terminal" in prompt.lower()
        assert "start a new Claude Code session" in prompt
        assert "ONLY interact through Slack" in prompt

    def test_system_prompt_forbids_local_workarounds(self, store, config):
        """System prompt must forbid suggesting shell commands for the user."""
        info = store.get_or_create("thread-1", config)
        prompt = info.session.system_prompt
        assert "running shell commands" in prompt.lower()
        assert "host system directly" in prompt.lower()

    def test_system_prompt_forbids_interactive_tools(self, store, config):
        """System prompt must tell Claude not to use blocking interactive tools."""
        info = store.get_or_create("thread-1", config)
        prompt = info.session.system_prompt
        assert "AskUserQuestion" in prompt
        assert "streamed output mode" in prompt.lower()
        assert "will NOT work" in prompt

    def test_shutdown_clears_sessions(self, store, config):
        store.get_or_create("thread-1", config, cwd=Path("/tmp/a"))
        store.get_or_create("thread-2", config, cwd=Path("/tmp/b"))

        store.shutdown()

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
