"""Tests for slaude.handlers."""

from pathlib import Path

import pytest

from slaude.config import Config
from slaude.handlers import _should_ignore, _truncate


@pytest.fixture
def config():
    return Config(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
    )


@pytest.fixture
def config_restricted():
    return Config(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        allowed_users=["U_ALLOWED"],
    )


class TestShouldIgnore:
    def test_no_restrictions(self, config):
        event = {"user": "U_ANYONE"}
        assert _should_ignore(event, config) is False

    def test_allowed_user(self, config_restricted):
        event = {"user": "U_ALLOWED"}
        assert _should_ignore(event, config_restricted) is False

    def test_blocked_user(self, config_restricted):
        event = {"user": "U_BLOCKED"}
        assert _should_ignore(event, config_restricted) is True


class TestTruncate:
    def test_short_text(self):
        assert _truncate("hello") == "hello"

    def test_long_text_truncated(self):
        text = "a" * 5000
        result = _truncate(text)
        assert len(result) < 5000
        assert result.endswith("_(truncated)_")

    def test_exact_limit(self):
        text = "a" * 3900
        assert _truncate(text) == text
