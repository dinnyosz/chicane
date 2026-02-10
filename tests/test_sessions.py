"""Tests for goose.sessions."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from goose.config import Config
from goose.sessions import SessionStore


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
        session = store.get_or_create("thread-1", config, cwd=Path("/tmp/projects"))
        assert session is not None
        assert session.cwd == Path("/tmp/projects")

    def test_reuse_existing_session(self, store, config):
        s1 = store.get_or_create("thread-1", config)
        s2 = store.get_or_create("thread-1", config)
        assert s1 is s2

    def test_different_threads_different_sessions(self, store, config):
        s1 = store.get_or_create("thread-1", config)
        s2 = store.get_or_create("thread-2", config)
        assert s1 is not s2

    def test_custom_cwd(self, store, config):
        session = store.get_or_create("thread-1", config, cwd=Path("/tmp/other"))
        assert session.cwd == Path("/tmp/other")

    def test_set_cwd(self, store, config):
        session = store.get_or_create("thread-1", config)
        assert store.set_cwd("thread-1", Path("/tmp/new"))
        assert session.cwd == Path("/tmp/new")

    def test_set_cwd_nonexistent_thread(self, store):
        assert store.set_cwd("nope", Path("/tmp")) is False

    def test_remove(self, store, config):
        store.get_or_create("thread-1", config)
        store.remove("thread-1")
        # Next call should create a new session
        s = store.get_or_create("thread-1", config)
        assert s.session_id is None  # Fresh session

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
        session = store.get_or_create("thread-1", config)
        assert str(session.cwd).startswith("/tmp/goose-") or "goose-" in str(session.cwd)

    def test_sessions_include_slack_system_prompt(self, store, config):
        session = store.get_or_create("thread-1", config)
        assert session.system_prompt is not None
        assert "Slack" in session.system_prompt

    def test_sessions_pass_allowed_tools(self, store):
        config_with_tools = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            claude_allowed_tools=["WebFetch", "WebSearch"],
        )
        session = store.get_or_create("thread-1", config_with_tools)
        assert session.allowed_tools == ["WebFetch", "WebSearch"]

    def test_sessions_empty_allowed_tools_by_default(self, store, config):
        session = store.get_or_create("thread-1", config)
        assert session.allowed_tools == []

    def test_create_with_explicit_session_id(self, store, config):
        """When session_id is passed, the ClaudeSession should use --resume."""
        session = store.get_or_create(
            "thread-1", config, session_id="abc-123-def"
        )
        assert session.session_id == "abc-123-def"

    def test_explicit_session_id_not_used_on_reuse(self, store, config):
        """An existing session is returned as-is; session_id doesn't override it."""
        s1 = store.get_or_create("thread-1", config)
        s2 = store.get_or_create(
            "thread-1", config, session_id="should-be-ignored"
        )
        assert s1 is s2
        assert s2.session_id is None  # original session had no id

    def test_session_id_with_cwd(self, store, config):
        """session_id and cwd can be provided together."""
        session = store.get_or_create(
            "thread-1", config, cwd=Path("/tmp/work"), session_id="sess-42"
        )
        assert session.session_id == "sess-42"
        assert session.cwd == Path("/tmp/work")
