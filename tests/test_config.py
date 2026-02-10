"""Tests for goose.config."""

import os
from pathlib import Path

import pytest

from goose.config import Config


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

    def test_allowed_tools_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_ALLOWED_TOOLS", "WebFetch,WebSearch")

        config = Config.from_env()
        assert config.claude_allowed_tools == ["WebFetch", "WebSearch"]

    def test_allowed_tools_empty_by_default(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("CLAUDE_ALLOWED_TOOLS", raising=False)

        config = Config.from_env()
        assert config.claude_allowed_tools == []

    def test_allowed_tools_with_spaces(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_ALLOWED_TOOLS", " WebFetch , WebSearch ")

        config = Config.from_env()
        assert config.claude_allowed_tools == ["WebFetch", "WebSearch"]


class TestChannelDirs:
    def test_simple_channel_dirs(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CHANNEL_DIRS", "magaldi,slack-bot")

        config = Config.from_env()
        assert config.channel_dirs == {"magaldi": "magaldi", "slack-bot": "slack-bot"}

    def test_custom_mapping(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CHANNEL_DIRS", "magaldi,web=frontend")

        config = Config.from_env()
        assert config.channel_dirs == {"magaldi": "magaldi", "web": "frontend"}

    def test_resolve_relative(self):
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=Path("/home/user/code"),
            channel_dirs={"magaldi": "magaldi"},
        )
        assert config.resolve_channel_dir("magaldi") == Path("/home/user/code/magaldi")

    def test_resolve_absolute(self):
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=Path("/home/user/code"),
            channel_dirs={"infra": "/opt/infrastructure"},
        )
        assert config.resolve_channel_dir("infra") == Path("/opt/infrastructure")

    def test_resolve_not_whitelisted(self):
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=Path("/home/user/code"),
            channel_dirs={"magaldi": "magaldi"},
        )
        assert config.resolve_channel_dir("random") is None

    def test_resolve_dir_channel_relative_match(self, tmp_path):
        """Relative mapped dir matches when resolved against base_directory."""
        base = tmp_path / "code"
        project = base / "magaldi"
        project.mkdir(parents=True)

        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=base,
            channel_dirs={"magaldi": "magaldi"},
        )
        assert config.resolve_dir_channel(project) == "magaldi"

    def test_resolve_dir_channel_absolute_match(self, tmp_path):
        """Absolute mapped dir matches directly."""
        project = tmp_path / "infra"
        project.mkdir()

        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=Path("/unused"),
            channel_dirs={"infra": str(project)},
        )
        assert config.resolve_dir_channel(project) == "infra"

    def test_resolve_dir_channel_no_match(self, tmp_path):
        """Returns None when no channel maps to the given directory."""
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=tmp_path,
            channel_dirs={"magaldi": "magaldi"},
        )
        assert config.resolve_dir_channel(tmp_path / "unknown") is None

    def test_resolve_dir_channel_empty_dirs(self, tmp_path):
        """Returns None when channel_dirs is empty."""
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            channel_dirs={},
        )
        assert config.resolve_dir_channel(tmp_path) is None

    def test_empty_channel_dirs(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("CHANNEL_DIRS", raising=False)

        config = Config.from_env()
        assert config.channel_dirs == {}
