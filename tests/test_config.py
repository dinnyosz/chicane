"""Tests for chicane.config."""

import os
import sys
from pathlib import Path

import pytest

from chicane.config import Config, config_dir, env_file, save_handoff_session


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
        monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "dontAsk")
        monkeypatch.setenv("LOG_DIR", "/var/log/chicane")
        monkeypatch.setenv("CLAUDE_ALLOWED_TOOLS", "Bash(npm run *), Read, Edit(./src/**)")

        config = Config.from_env()

        assert str(config.base_directory) == "/tmp/projects"
        assert config.allowed_users == ["U123", "U456"]
        assert config.log_level == "DEBUG"
        assert config.claude_model == "sonnet"
        assert config.claude_permission_mode == "dontAsk"
        assert config.log_dir == Path("/var/log/chicane")
        assert config.claude_allowed_tools == ["Bash(npm run *)", "Read", "Edit(./src/**)"]

    def test_bypass_permissions_rejected_with_multiple_users(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("ALLOWED_USERS", "U123, U456")
        monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")

        with pytest.raises(ValueError, match="bypassPermissions cannot be used with multiple ALLOWED_USERS"):
            Config.from_env()

    def test_bypass_permissions_allowed_with_single_user(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("ALLOWED_USERS", "U123")
        monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")

        config = Config.from_env()
        assert config.claude_permission_mode == "bypassPermissions"
        assert config.allowed_users == ["U123"]

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


class TestRateLimitConfig:
    def test_rate_limit_default(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("RATE_LIMIT", raising=False)
        assert Config.from_env().rate_limit == 10

    def test_rate_limit_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("RATE_LIMIT", "5")
        assert Config.from_env().rate_limit == 5

    def test_invalid_rate_limit_rejected(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("RATE_LIMIT", "0")
        with pytest.raises(ValueError, match="RATE_LIMIT must be a positive"):
            Config.from_env()


class TestDisallowedToolsConfig:
    def test_disallowed_tools_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_DISALLOWED_TOOLS", "Bash,WebFetch")
        config = Config.from_env()
        assert config.claude_disallowed_tools == ["Bash", "WebFetch"]

    def test_disallowed_tools_default_empty(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("CLAUDE_DISALLOWED_TOOLS", raising=False)
        config = Config.from_env()
        assert config.claude_disallowed_tools == []

    def test_disallowed_tools_whitespace(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_DISALLOWED_TOOLS", " Bash , Edit(./secrets/**) ")
        config = Config.from_env()
        assert config.claude_disallowed_tools == ["Bash", "Edit(./secrets/**)"]


class TestSettingSourcesConfig:
    def test_setting_sources_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,project")
        config = Config.from_env()
        assert config.claude_setting_sources == ["user", "project"]

    def test_setting_sources_default_all(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("CLAUDE_SETTING_SOURCES", raising=False)
        config = Config.from_env()
        assert config.claude_setting_sources == ["user", "project", "local"]

    def test_setting_sources_single(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "local")
        config = Config.from_env()
        assert config.claude_setting_sources == ["local"]

    def test_invalid_setting_source_rejected(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,global")
        with pytest.raises(ValueError, match="Invalid CLAUDE_SETTING_SOURCES"):
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

    def test_resolve_dir_channel_ambiguous_warns(self, tmp_path, caplog):
        """When multiple channels map to the same dir, first wins and a warning is logged."""
        project = tmp_path / "web"
        project.mkdir()
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=tmp_path,
            channel_dirs={"frontend": "web", "design": "web"},
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="chicane.config"):
            result = config.resolve_dir_channel(project)
        assert result == "frontend"
        assert "multiple channels" in caplog.text.lower()
        assert "#frontend" in caplog.text
        assert "#design" in caplog.text

    def test_resolve_traversal_blocked(self):
        """Directory traversal via ../.. should be blocked when base_directory is set."""
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=Path("/home/user/code"),
            channel_dirs={"evil": "../../etc"},
        )
        assert config.resolve_channel_dir("evil") is None

    def test_resolve_traversal_allowed_when_inside_base(self):
        """Paths that resolve inside base_directory should work."""
        config = Config(
            slack_bot_token="t",
            slack_app_token="t",
            base_directory=Path("/home/user/code"),
            channel_dirs={"sub": "sub/../project"},
        )
        result = config.resolve_channel_dir("sub")
        assert result is not None

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


class TestReactToStrangersConfig:
    def test_default_true(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("REACT_TO_STRANGERS", raising=False)
        assert Config.from_env().react_to_strangers is True

    def test_truthy_values(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        for val in ("true", "True", "TRUE", "1", "yes", "Yes"):
            monkeypatch.setenv("REACT_TO_STRANGERS", val)
            assert Config.from_env().react_to_strangers is True, f"Expected True for '{val}'"

    def test_falsy_values(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        for val in ("false", "False", "0", "no", "No"):
            monkeypatch.setenv("REACT_TO_STRANGERS", val)
            assert Config.from_env().react_to_strangers is False, f"Expected False for '{val}'"


class TestPostImagesConfig:
    def test_default_false(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("POST_IMAGES", raising=False)
        assert Config.from_env().post_images is True

    def test_truthy_values(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        for val in ("true", "True", "TRUE", "1", "yes", "Yes"):
            monkeypatch.setenv("POST_IMAGES", val)
            assert Config.from_env().post_images is True, f"Expected True for '{val}'"

    def test_falsy_values(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        for val in ("false", "False", "0", "no", "No"):
            monkeypatch.setenv("POST_IMAGES", val)
            assert Config.from_env().post_images is False, f"Expected False for '{val}'"


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


class TestGenerateSessionAlias:
    def test_returns_hyphenated_alias(self):
        from chicane.config import generate_session_alias
        alias = generate_session_alias()
        parts = alias.split("-")
        assert len(parts) == 3
        assert all(part.isalpha() for part in parts)

    def test_avoids_collision_with_existing(self, tmp_path, monkeypatch):
        from chicane.config import generate_session_alias, save_handoff_session
        from chicane.emoji_map import generate_alias as real_generate
        map_file = tmp_path / "handoff_sessions.json"
        monkeypatch.setattr("chicane.config._HANDOFF_MAP_FILE", map_file)

        # Pre-fill the map with a known alias
        first = generate_session_alias()
        save_handoff_session(first, "existing-session-id")

        # Patch generate_alias to return the colliding name first, then fresh
        call_count = 0

        def _patched():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first  # collision
            return real_generate()

        monkeypatch.setattr("chicane.emoji_map.generate_alias", _patched)
        result = generate_session_alias()
        assert result != first
        assert call_count >= 2

    def test_fallback_after_max_retries(self, tmp_path, monkeypatch):
        from chicane.config import generate_session_alias, save_handoff_session
        map_file = tmp_path / "handoff_sessions.json"
        monkeypatch.setattr("chicane.config._HANDOFF_MAP_FILE", map_file)

        monkeypatch.setattr("chicane.emoji_map.generate_alias", lambda: "same-old-alias")
        save_handoff_session("same-old-alias", "some-id")

        # Should still return after 50 retries (fallback)
        result = generate_session_alias()
        assert result == "same-old-alias"


class TestHandoffFilePermissions:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_save_handoff_session_creates_file_with_0600(self, tmp_path, monkeypatch):
        """save_handoff_session() must create handoff_sessions.json with mode 0o600."""
        map_file = tmp_path / "handoff_sessions.json"
        monkeypatch.setattr("chicane.config._HANDOFF_MAP_FILE", map_file)

        save_handoff_session("test-alias", "test-session-id")

        assert map_file.exists()
        assert map_file.stat().st_mode & 0o777 == 0o600
