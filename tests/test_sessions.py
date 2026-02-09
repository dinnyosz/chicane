"""Tests for slaude.sessions."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from slaude.config import Config
from slaude.sessions import SessionStore


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
    def test_create_new_session(self, store, config):
        session = store.get_or_create("thread-1", config)
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

    def test_falls_back_to_cwd_when_no_base_dir(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        store = SessionStore()
        session = store.get_or_create("thread-1", config)
        assert session.cwd == Path.cwd()
