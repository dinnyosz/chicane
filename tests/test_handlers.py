"""Tests for slaude.handlers."""

from pathlib import Path

import pytest

from slaude.config import Config
from slaude.handlers import _should_ignore, _split_message, SLACK_MAX_LENGTH


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


class TestSplitMessage:
    def test_short_text_single_chunk(self):
        assert _split_message("hello") == ["hello"]

    def test_exact_limit_single_chunk(self):
        text = "a" * SLACK_MAX_LENGTH
        assert _split_message(text) == [text]

    def test_long_text_splits_into_chunks(self):
        text = "a" * 8000
        chunks = _split_message(text)
        assert len(chunks) > 1
        reassembled = "".join(chunks)
        assert reassembled == text

    def test_splits_on_newlines(self):
        # Build text with lines that total over the limit
        line = "x" * 100 + "\n"
        text = line * 50  # 5050 chars
        chunks = _split_message(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= SLACK_MAX_LENGTH

    def test_no_content_lost(self):
        text = "line1\nline2\n" * 500
        chunks = _split_message(text)
        reassembled = "\n".join(chunks)
        # All original content should be present (minus split newlines)
        assert "line1" in reassembled
        assert "line2" in reassembled

    def test_very_long_single_line(self):
        text = "a" * 10000  # No newlines at all
        chunks = _split_message(text)
        assert len(chunks) > 1
        assert "".join(chunks) == text
