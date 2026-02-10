"""Tests for goose.init — the setup wizard."""

import argparse
from pathlib import Path
from unittest.mock import patch, call

import pytest

from goose.init import (
    _copy_to_clipboard,
    _prompt_token,
    _step_bot_token,
    _step_app_token,
    _step_optional_settings,
    _write_env,
    init_command,
)


class TestCopyToClipboard:
    def test_success_pbcopy(self):
        with patch("goose.init.subprocess.run") as mock_run:
            assert _copy_to_clipboard("hello") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["pbcopy"]
            assert args[1]["input"] == b"hello"

    def test_fallback_on_failure(self):
        with patch("goose.init.subprocess.run", side_effect=FileNotFoundError):
            assert _copy_to_clipboard("hello") is False


class TestPromptToken:
    def test_valid_on_first_try(self):
        with patch("builtins.input", return_value="xoxb-valid"):
            result = _prompt_token("Bot Token", "xoxb-")
            assert result == "xoxb-valid"

    def test_reprompts_on_bad_prefix(self, capsys):
        with patch("builtins.input", side_effect=["bad-token", "xoxb-good"]):
            result = _prompt_token("Bot Token", "xoxb-")
            assert result == "xoxb-good"
            captured = capsys.readouterr()
            assert "must start with 'xoxb-'" in captured.out

    def test_strips_whitespace(self):
        with patch("builtins.input", return_value="  xapp-trimmed  "):
            result = _prompt_token("App Token", "xapp-")
            assert result == "xapp-trimmed"


class TestStepBotToken:
    def test_returns_valid_token(self):
        with patch("builtins.input", return_value="xoxb-1234"):
            token = _step_bot_token()
            assert token == "xoxb-1234"


class TestStepAppToken:
    def test_returns_valid_token(self):
        with patch("builtins.input", return_value="xapp-5678"):
            token = _step_app_token()
            assert token == "xapp-5678"


class TestStepOptionalSettings:
    def test_all_empty(self):
        with patch("builtins.input", return_value=""):
            result = _step_optional_settings()
            assert result == {}

    def test_some_filled(self):
        inputs = [
            "/home/user/code",  # BASE_DIRECTORY
            "U123,U456",        # ALLOWED_USERS
            "",                 # CHANNEL_DIRS
            "sonnet",           # CLAUDE_MODEL
            "",                 # CLAUDE_PERMISSION_MODE
            "n",                # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs):
            result = _step_optional_settings()
            assert result == {
                "BASE_DIRECTORY": "/home/user/code",
                "ALLOWED_USERS": "U123,U456",
                "CLAUDE_MODEL": "sonnet",
            }

    def test_debug_enabled(self):
        inputs = ["", "", "", "", "", "y"]
        with patch("builtins.input", side_effect=inputs):
            result = _step_optional_settings()
            assert result == {"DEBUG": "true"}


class TestWriteEnv:
    def test_writes_key_value_pairs(self, tmp_path):
        env_file = tmp_path / ".env"
        _write_env(env_file, {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
        })
        content = env_file.read_text()
        assert "SLACK_BOT_TOKEN=xoxb-test\n" in content
        assert "SLACK_APP_TOKEN=xapp-test\n" in content

    def test_only_writes_provided_keys(self, tmp_path):
        env_file = tmp_path / ".env"
        _write_env(env_file, {"SLACK_BOT_TOKEN": "xoxb-test"})
        content = env_file.read_text()
        assert "SLACK_BOT_TOKEN=xoxb-test\n" in content
        assert "SLACK_APP_TOKEN" not in content


class TestInitCommand:
    def _make_args(self, force: bool = False) -> argparse.Namespace:
        return argparse.Namespace(force=force)

    def test_writes_env_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inputs = [
            "",              # Step 1: Enter to continue
            "xoxb-bot123",   # Step 2: Bot token
            "xapp-app456",   # Step 3: App token
            "",              # BASE_DIRECTORY
            "",              # ALLOWED_USERS
            "",              # CHANNEL_DIRS
            "",              # CLAUDE_MODEL
            "",              # CLAUDE_PERMISSION_MODE
            "n",             # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.init._copy_to_clipboard", return_value=False):
            init_command(self._make_args())

        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "SLACK_BOT_TOKEN=xoxb-bot123" in content
        assert "SLACK_APP_TOKEN=xapp-app456" in content

    def test_writes_env_with_optional_values(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inputs = [
            "",                  # Step 1: Enter
            "xoxb-bot123",       # Bot token
            "xapp-app456",       # App token
            "/home/user/code",   # BASE_DIRECTORY
            "",                  # ALLOWED_USERS
            "proj1,web=frontend",# CHANNEL_DIRS
            "",                  # CLAUDE_MODEL
            "",                  # CLAUDE_PERMISSION_MODE
            "y",                 # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.init._copy_to_clipboard", return_value=False):
            init_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "BASE_DIRECTORY=/home/user/code" in content
        assert "CHANNEL_DIRS=proj1,web=frontend" in content
        assert "DEBUG=true" in content
        assert "ALLOWED_USERS" not in content

    def test_existing_env_aborts_without_force(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("OLD=stuff\n")

        with patch("builtins.input", return_value="n"):
            init_command(self._make_args(force=False))

        assert (tmp_path / ".env").read_text() == "OLD=stuff\n"
        captured = capsys.readouterr()
        assert "Aborted" in captured.out

    def test_existing_env_overwritten_with_confirmation(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("OLD=stuff\n")

        inputs = [
            "y",             # Overwrite confirmation
            "",              # Step 1: Enter
            "xoxb-new",      # Bot token
            "xapp-new",      # App token
            "",              # BASE_DIRECTORY
            "",              # ALLOWED_USERS
            "",              # CHANNEL_DIRS
            "",              # CLAUDE_MODEL
            "",              # CLAUDE_PERMISSION_MODE
            "n",             # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.init._copy_to_clipboard", return_value=False):
            init_command(self._make_args(force=False))

        content = (tmp_path / ".env").read_text()
        assert "OLD" not in content
        assert "SLACK_BOT_TOKEN=xoxb-new" in content

    def test_force_skips_overwrite_prompt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("OLD=stuff\n")

        inputs = [
            "",              # Step 1: Enter
            "xoxb-forced",   # Bot token
            "xapp-forced",   # App token
            "",              # BASE_DIRECTORY
            "",              # ALLOWED_USERS
            "",              # CHANNEL_DIRS
            "",              # CLAUDE_MODEL
            "",              # CLAUDE_PERMISSION_MODE
            "n",             # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.init._copy_to_clipboard", return_value=False):
            init_command(self._make_args(force=True))

        content = (tmp_path / ".env").read_text()
        assert "OLD" not in content
        assert "SLACK_BOT_TOKEN=xoxb-forced" in content

    def test_token_validation_reprompts(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        inputs = [
            "",                  # Step 1: Enter
            "bad-bot",           # Invalid bot token
            "xoxb-good",         # Valid bot token
            "not-an-app-token",  # Invalid app token
            "xapp-good",         # Valid app token
            "",                  # BASE_DIRECTORY
            "",                  # ALLOWED_USERS
            "",                  # CHANNEL_DIRS
            "",                  # CLAUDE_MODEL
            "",                  # CLAUDE_PERMISSION_MODE
            "n",                 # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.init._copy_to_clipboard", return_value=False):
            init_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-good" in content
        assert "SLACK_APP_TOKEN=xapp-good" in content

        captured = capsys.readouterr()
        assert "must start with 'xoxb-'" in captured.out
        assert "must start with 'xapp-'" in captured.out

    def test_ctrl_c_exits_cleanly(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with patch("builtins.input", side_effect=KeyboardInterrupt), \
             patch("goose.init._copy_to_clipboard", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                init_command(self._make_args())
            assert exc_info.value.code == 130

        captured = capsys.readouterr()
        assert "Aborted" in captured.out
