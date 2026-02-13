"""Tests for chicane.config."""

import os
import sys
from pathlib import Path

import pytest

from chicane.config import Config, config_dir, env_file


class TestConfig:
    def test_from_env_valid(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        # Clear optional vars
        monkeypatch.delenv("BASE_DIRECTORY", raising=False)
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_DIR", raising=False)

        config = Config.from_env()

        assert config.slack_bot_token == "xoxb-test"
        assert config.slack_app_token == "xapp-test"
        assert config.base_directory is None
        assert config.allowed_users == []
        assert config.log_level == "INFO"
        assert config.log_dir is None
        assert config.claude_allowed_tools == []

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
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("CLAUDE_MODEL", "sonnet")
        monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
        monkeypatch.setenv("LOG_DIR", "/var/log/chicane")
        monkeypatch.setenv("CLAUDE_ALLOWED_TOOLS", "Bash(npm run *), Read, Edit(./src/**)")

        config = Config.from_env()

        assert str(config.base_directory) == "/tmp/projects"
        assert config.allowed_users == ["U123", "U456"]
        assert config.log_level == "DEBUG"
        assert config.claude_model == "sonnet"
        assert config.claude_permission_mode == "bypassPermissions"
        assert config.log_dir == Path("/var/log/chicane")
        assert config.claude_allowed_tools == ["Bash(npm run *)", "Read", "Edit(./src/**)"]

    def test_log_level_variants(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")

        for val, expected in [("debug", "DEBUG"), ("WARNING", "WARNING"), ("error", "ERROR")]:
            monkeypatch.setenv("LOG_LEVEL", val)
            assert Config.from_env().log_level == expected

    def test_log_level_default(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        assert Config.from_env().log_level == "INFO"

    def test_invalid_log_level_rejected(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("LOG_LEVEL", "almafa")

        with pytest.raises(ValueError, match="Invalid LOG_LEVEL"):
            Config.from_env()

    def test_invalid_permission_mode_rejected(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "yolo")

        with pytest.raises(ValueError, match="Invalid CLAUDE_PERMISSION_MODE"):
            Config.from_env()

    def test_empty_allowed_users(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("ALLOWED_USERS", "")

        config = Config.from_env()
        assert config.allowed_users == []

    def test_max_turns_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_MAX_TURNS", "25")

        config = Config.from_env()
        assert config.claude_max_turns == 25

    def test_max_budget_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_MAX_BUDGET_USD", "5.50")

        config = Config.from_env()
        assert config.claude_max_budget_usd == 5.50

    def test_max_turns_and_budget_default_none(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("CLAUDE_MAX_TURNS", raising=False)
        monkeypatch.delenv("CLAUDE_MAX_BUDGET_USD", raising=False)

        config = Config.from_env()
        assert config.claude_max_turns is None
        assert config.claude_max_budget_usd is None

    def test_invalid_max_turns_rejected(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_MAX_TURNS", "0")

        with pytest.raises(ValueError, match="CLAUDE_MAX_TURNS must be a positive"):
            Config.from_env()

    def test_invalid_max_budget_rejected(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_MAX_BUDGET_USD", "-1.0")

        with pytest.raises(ValueError, match="CLAUDE_MAX_BUDGET_USD must be a positive"):
            Config.from_env()


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


class TestVerbosityConfig:
    def test_verbosity_default_normal(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("VERBOSITY", raising=False)
        assert Config.from_env().verbosity == "verbose"

    def test_verbosity_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        for val, expected in [("minimal", "minimal"), ("VERBOSE", "verbose"), ("Normal", "normal")]:
            monkeypatch.setenv("VERBOSITY", val)
            assert Config.from_env().verbosity == expected

    def test_invalid_verbosity_rejected(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("VERBOSITY", "ultra")
        with pytest.raises(ValueError, match="Invalid VERBOSITY"):
            Config.from_env()


class TestConfigDir:
    def test_override_via_env(self, monkeypatch):
        monkeypatch.setenv("CHICANE_CONFIG_DIR", "/custom/path")
        assert config_dir() == Path("/custom/path")

    def test_default_uses_platformdirs(self, monkeypatch):
        monkeypatch.delenv("CHICANE_CONFIG_DIR", raising=False)
        result = config_dir()
        assert result.name == "chicane"
        assert result.is_absolute()

    def test_env_file_inside_config_dir(self, monkeypatch):
        monkeypatch.setenv("CHICANE_CONFIG_DIR", "/custom/path")
        assert env_file() == Path("/custom/path/.env")
