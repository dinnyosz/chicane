"""Tests for slaude.config."""

import os

import pytest

from slaude.config import Config


class TestConfig:
    def test_from_env_valid(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        # Clear optional vars
        monkeypatch.delenv("BASE_DIRECTORY", raising=False)
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        monkeypatch.delenv("DEBUG", raising=False)

        config = Config.from_env()

        assert config.slack_bot_token == "xoxb-test"
        assert config.slack_app_token == "xapp-test"
        assert config.base_directory is None
        assert config.allowed_users == []
        assert config.debug is False

    def test_from_env_missing_tokens(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

        with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
            Config.from_env()

    def test_from_env_with_options(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("BASE_DIRECTORY", "/tmp/projects")
        monkeypatch.setenv("ALLOWED_USERS", "U123, U456")
        monkeypatch.setenv("DEBUG", "true")
        monkeypatch.setenv("CLAUDE_MODEL", "sonnet")
        monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")

        config = Config.from_env()

        assert str(config.base_directory) == "/tmp/projects"
        assert config.allowed_users == ["U123", "U456"]
        assert config.debug is True
        assert config.claude_model == "sonnet"
        assert config.claude_permission_mode == "bypassPermissions"

    def test_debug_variants(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")

        for val in ("1", "true", "True", "yes", "YES"):
            monkeypatch.setenv("DEBUG", val)
            assert Config.from_env().debug is True

        for val in ("0", "false", "no", ""):
            monkeypatch.setenv("DEBUG", val)
            assert Config.from_env().debug is False

    def test_empty_allowed_users(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("ALLOWED_USERS", "")

        config = Config.from_env()
        assert config.allowed_users == []
