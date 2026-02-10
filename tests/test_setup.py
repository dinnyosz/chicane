"""Tests for goose.setup — the setup wizard."""

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from goose.setup import (
    _copy_to_clipboard,
    _load_existing_env,
    _load_manifest,
    _prompt_token,
    _prompt_with_default,
    _step_bot_token,
    _step_app_token,
    _step_optional_settings,
    _write_env,
    setup_command,
)


class TestLoadManifest:
    def test_loads_valid_manifest(self):
        manifest = _load_manifest()
        assert manifest["display_information"]["name"] == "Goose"
        assert "bot" in manifest["oauth_config"]["scopes"]
        assert manifest["settings"]["socket_mode_enabled"] is True


class TestLoadExistingEnv:
    def test_missing_file(self, tmp_path):
        assert _load_existing_env(tmp_path / ".env") == {}

    def test_parses_key_values(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SLACK_BOT_TOKEN=xoxb-123\nSLACK_APP_TOKEN=xapp-456\n")
        result = _load_existing_env(env)
        assert result == {"SLACK_BOT_TOKEN": "xoxb-123", "SLACK_APP_TOKEN": "xapp-456"}

    def test_skips_comments_and_blanks(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# comment\n\nKEY=val\n")
        assert _load_existing_env(env) == {"KEY": "val"}

    def test_handles_value_with_equals(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("KEY=val=ue\n")
        assert _load_existing_env(env) == {"KEY": "val=ue"}


class TestCopyToClipboard:
    def test_success_pbcopy(self):
        with patch("goose.setup.subprocess.run") as mock_run:
            assert _copy_to_clipboard("hello") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["pbcopy"]
            assert args[1]["input"] == b"hello"

    def test_fallback_on_failure(self):
        with patch("goose.setup.subprocess.run", side_effect=FileNotFoundError):
            assert _copy_to_clipboard("hello") is False


class TestPromptWithDefault:
    def test_no_default_empty_input(self):
        with patch("builtins.input", return_value=""):
            assert _prompt_with_default("Label") == ""

    def test_no_default_with_input(self):
        with patch("builtins.input", return_value="new-value"):
            assert _prompt_with_default("Label") == "new-value"

    def test_default_kept_on_empty_input(self):
        with patch("builtins.input", return_value=""):
            assert _prompt_with_default("Label", "old-value") == "old-value"

    def test_default_overridden(self):
        with patch("builtins.input", return_value="new-value"):
            assert _prompt_with_default("Label", "old-value") == "new-value"

    def test_dash_clears_default(self):
        with patch("builtins.input", return_value="-"):
            assert _prompt_with_default("Label", "old-value") == ""

    def test_dash_without_default_is_literal(self):
        with patch("builtins.input", return_value="-"):
            assert _prompt_with_default("Label") == "-"


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

    def test_default_kept_on_empty_input(self):
        with patch("builtins.input", return_value=""):
            result = _prompt_token("Bot Token", "xoxb-", default="xoxb-existing")
            assert result == "xoxb-existing"

    def test_default_overridden_with_valid(self):
        with patch("builtins.input", return_value="xoxb-new"):
            result = _prompt_token("Bot Token", "xoxb-", default="xoxb-old")
            assert result == "xoxb-new"

    def test_default_shown_masked(self):
        with patch("builtins.input", return_value="") as mock_input:
            _prompt_token("Bot Token", "xoxb-", default="xoxb-1234567890")
            prompt_text = mock_input.call_args[0][0]
            assert "xoxb-123...7890" in prompt_text

    def test_short_token_masked_safely(self):
        with patch("builtins.input", return_value="") as mock_input:
            _prompt_token("Bot Token", "xoxb-", default="xoxb-abc")
            prompt_text = mock_input.call_args[0][0]
            assert "xoxb-..." in prompt_text
            # Should not contain the full token
            assert "xoxb-abc]" not in prompt_text


class TestStepBotToken:
    def test_returns_valid_token(self):
        with patch("builtins.input", return_value="xoxb-1234"):
            assert _step_bot_token() == "xoxb-1234"

    def test_keeps_default(self):
        with patch("builtins.input", return_value=""):
            assert _step_bot_token("xoxb-existing") == "xoxb-existing"


class TestStepAppToken:
    def test_returns_valid_token(self):
        with patch("builtins.input", return_value="xapp-5678"):
            assert _step_app_token() == "xapp-5678"

    def test_keeps_default(self):
        with patch("builtins.input", return_value=""):
            assert _step_app_token("xapp-existing") == "xapp-existing"


class TestStepOptionalSettings:
    def test_all_empty_no_defaults(self):
        with patch("builtins.input", return_value=""):
            result = _step_optional_settings({})
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
            result = _step_optional_settings({})
            assert result == {
                "BASE_DIRECTORY": "/home/user/code",
                "ALLOWED_USERS": "U123,U456",
                "CLAUDE_MODEL": "sonnet",
            }

    def test_debug_enabled(self):
        inputs = ["", "", "", "", "", "y"]
        with patch("builtins.input", side_effect=inputs):
            result = _step_optional_settings({})
            assert result == {"DEBUG": "true"}

    def test_defaults_kept_on_enter(self):
        defaults = {
            "BASE_DIRECTORY": "/old/path",
            "ALLOWED_USERS": "U111",
            "CHANNEL_DIRS": "proj1",
            "CLAUDE_MODEL": "opus",
            "CLAUDE_PERMISSION_MODE": "bypassPermissions",
            "DEBUG": "true",
        }
        # All empty inputs — should keep all defaults
        inputs = ["", "", "", "", "", ""]
        with patch("builtins.input", side_effect=inputs):
            result = _step_optional_settings(defaults)
            assert result["BASE_DIRECTORY"] == "/old/path"
            assert result["ALLOWED_USERS"] == "U111"
            assert result["CHANNEL_DIRS"] == "proj1"
            assert result["CLAUDE_MODEL"] == "opus"
            assert result["CLAUDE_PERMISSION_MODE"] == "bypassPermissions"
            assert result["DEBUG"] == "true"

    def test_dash_clears_value(self):
        defaults = {
            "BASE_DIRECTORY": "/old/path",
            "ALLOWED_USERS": "U111",
        }
        inputs = [
            "",     # BASE_DIRECTORY (keep)
            "-",    # ALLOWED_USERS (clear)
            "",     # CHANNEL_DIRS
            "",     # CLAUDE_MODEL
            "",     # CLAUDE_PERMISSION_MODE
            "n",    # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs):
            result = _step_optional_settings(defaults)
            assert result["BASE_DIRECTORY"] == "/old/path"
            assert "ALLOWED_USERS" not in result

    def test_defaults_overridden(self):
        defaults = {
            "BASE_DIRECTORY": "/old/path",
            "CHANNEL_DIRS": "old-proj",
        }
        inputs = [
            "/new/path",    # BASE_DIRECTORY override
            "",             # ALLOWED_USERS (no default, skip)
            "new-proj",     # CHANNEL_DIRS override
            "",             # CLAUDE_MODEL (no default, skip)
            "",             # CLAUDE_PERMISSION_MODE (default "default")
            "n",            # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs):
            result = _step_optional_settings(defaults)
            assert result["BASE_DIRECTORY"] == "/new/path"
            assert result["CHANNEL_DIRS"] == "new-proj"
            assert "ALLOWED_USERS" not in result


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


class TestSetupCommand:
    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace()

    def test_fresh_setup_writes_env(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inputs = [
            "",              # Step 1: Enter
            "xoxb-bot123",   # Bot token
            "xapp-app456",   # App token
            "",              # BASE_DIRECTORY
            "",              # ALLOWED_USERS
            "",              # CHANNEL_DIRS
            "",              # CLAUDE_MODEL
            "",              # CLAUDE_PERMISSION_MODE
            "n",             # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "SLACK_BOT_TOKEN=xoxb-bot123" in content
        assert "SLACK_APP_TOKEN=xapp-app456" in content

    def test_existing_env_tokens_as_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-old\nSLACK_APP_TOKEN=xapp-old\nBASE_DIRECTORY=/old\n"
        )
        # All empty — keep all defaults
        inputs = [
            "",  # Step 1: Enter
            "",  # Bot token (keep xoxb-old)
            "",  # App token (keep xapp-old)
            "",  # BASE_DIRECTORY (keep /old)
            "",  # ALLOWED_USERS
            "",  # CHANNEL_DIRS
            "",  # CLAUDE_MODEL
            "",  # CLAUDE_PERMISSION_MODE
            "",  # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-old" in content
        assert "SLACK_APP_TOKEN=xapp-old" in content
        assert "BASE_DIRECTORY=/old" in content

    def test_existing_env_values_overridden(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-old\nSLACK_APP_TOKEN=xapp-old\nCHANNEL_DIRS=old-proj\n"
        )
        inputs = [
            "",                  # Step 1: Enter
            "xoxb-new",          # Override bot token
            "",                  # Keep app token
            "",                  # BASE_DIRECTORY
            "",                  # ALLOWED_USERS
            "new-proj,extra",    # Override CHANNEL_DIRS
            "",                  # CLAUDE_MODEL
            "",                  # CLAUDE_PERMISSION_MODE
            "n",                 # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-new" in content
        assert "SLACK_APP_TOKEN=xapp-old" in content
        assert "CHANNEL_DIRS=new-proj,extra" in content

    def test_token_validation_reprompts(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        inputs = [
            "",                  # Step 1: Enter
            "bad-bot",           # Invalid
            "xoxb-good",         # Valid
            "not-app",           # Invalid
            "xapp-good",         # Valid
            "",                  # BASE_DIRECTORY
            "",                  # ALLOWED_USERS
            "",                  # CHANNEL_DIRS
            "",                  # CLAUDE_MODEL
            "",                  # CLAUDE_PERMISSION_MODE
            "n",                 # DEBUG
        ]
        with patch("builtins.input", side_effect=inputs), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-good" in content
        assert "SLACK_APP_TOKEN=xapp-good" in content

        captured = capsys.readouterr()
        assert "must start with 'xoxb-'" in captured.out
        assert "must start with 'xapp-'" in captured.out

    def test_ctrl_c_exits_cleanly(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with patch("builtins.input", side_effect=KeyboardInterrupt), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                setup_command(self._make_args())
            assert exc_info.value.code == 130

        captured = capsys.readouterr()
        assert "Aborted" in captured.out
