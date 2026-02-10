"""Tests for goose.setup — the setup wizard."""

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from goose.setup import (
    _copy_to_clipboard,
    _load_existing_env,
    _load_manifest,
    _parse_allowed_tools,
    _parse_allowed_users,
    _parse_channel_dirs,
    _prompt_token,
    _prompt_with_default,
    _serialize_channel_dirs,
    _step_allowed_tools,
    _step_allowed_users,
    _step_bot_token,
    _step_app_token,
    _step_channel_dirs,
    _step_claude_model,
    _step_logging,
    _step_permission_mode,
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


class TestParseChannelDirs:
    def test_empty_string(self):
        assert _parse_channel_dirs("") == {}

    def test_simple_names(self):
        assert _parse_channel_dirs("frontend,backend") == {
            "frontend": "frontend",
            "backend": "backend",
        }

    def test_custom_mappings(self):
        assert _parse_channel_dirs("web=frontend,infra=/opt/infra") == {
            "web": "frontend",
            "infra": "/opt/infra",
        }

    def test_mixed(self):
        assert _parse_channel_dirs("frontend,web=src/web") == {
            "frontend": "frontend",
            "web": "src/web",
        }

    def test_whitespace_handling(self):
        assert _parse_channel_dirs(" frontend , web = src/web ") == {
            "frontend": "frontend",
            "web": "src/web",
        }


class TestSerializeChannelDirs:
    def test_empty(self):
        assert _serialize_channel_dirs({}) == ""

    def test_simple_names(self):
        result = _serialize_channel_dirs({"frontend": "frontend", "backend": "backend"})
        assert result == "frontend,backend"

    def test_custom_mappings(self):
        result = _serialize_channel_dirs({"web": "frontend"})
        assert result == "web=frontend"

    def test_mixed(self):
        result = _serialize_channel_dirs({"frontend": "frontend", "web": "src/web"})
        assert result == "frontend,web=src/web"


class TestParseAllowedUsers:
    def test_empty_string(self):
        assert _parse_allowed_users("") == []

    def test_single_user(self):
        assert _parse_allowed_users("U123") == ["U123"]

    def test_multiple_users(self):
        assert _parse_allowed_users("U123,U456,U789") == ["U123", "U456", "U789"]

    def test_whitespace_handling(self):
        assert _parse_allowed_users(" U123 , U456 ") == ["U123", "U456"]


class TestPromptWithDefault:
    def test_no_default_empty_input(self):
        with patch("goose.setup.Prompt.ask", return_value=""):
            assert _prompt_with_default("Label") == ""

    def test_no_default_with_input(self):
        with patch("goose.setup.Prompt.ask", return_value="new-value"):
            assert _prompt_with_default("Label") == "new-value"

    def test_default_kept_on_empty_input(self):
        with patch("goose.setup.Prompt.ask", return_value="old-value"):
            assert _prompt_with_default("Label", "old-value") == "old-value"

    def test_default_overridden(self):
        with patch("goose.setup.Prompt.ask", return_value="new-value"):
            assert _prompt_with_default("Label", "old-value") == "new-value"

    def test_dash_clears_default(self):
        with patch("goose.setup.Prompt.ask", return_value="-"):
            assert _prompt_with_default("Label", "old-value") == ""

    def test_dash_without_default_is_literal(self):
        with patch("goose.setup.Prompt.ask", return_value="-"):
            assert _prompt_with_default("Label") == "-"


class TestPromptToken:
    def test_valid_on_first_try(self):
        with patch("goose.setup.console.input", return_value="xoxb-valid"):
            result = _prompt_token("Bot Token", "xoxb-")
            assert result == "xoxb-valid"

    def test_reprompts_on_bad_prefix(self):
        with patch("goose.setup.console.input", side_effect=["bad-token", "xoxb-good"]), \
             patch("goose.setup.console.print"):
            result = _prompt_token("Bot Token", "xoxb-")
            assert result == "xoxb-good"

    def test_default_kept_on_empty_input(self):
        with patch("goose.setup.console.input", return_value=""):
            result = _prompt_token("Bot Token", "xoxb-", default="xoxb-existing")
            assert result == "xoxb-existing"

    def test_default_overridden_with_valid(self):
        with patch("goose.setup.console.input", return_value="xoxb-new"):
            result = _prompt_token("Bot Token", "xoxb-", default="xoxb-old")
            assert result == "xoxb-new"

    def test_default_shown_masked(self):
        with patch("goose.setup.console.input", return_value="") as mock_input:
            _prompt_token("Bot Token", "xoxb-", default="xoxb-1234567890")
            prompt_text = mock_input.call_args[0][0]
            assert "xoxb-123...7890" in prompt_text

    def test_short_token_masked_safely(self):
        with patch("goose.setup.console.input", return_value="") as mock_input:
            _prompt_token("Bot Token", "xoxb-", default="xoxb-abc")
            prompt_text = mock_input.call_args[0][0]
            assert "xoxb-..." in prompt_text
            assert "xoxb-abc]" not in prompt_text


class TestStepBotToken:
    def test_returns_valid_token(self):
        with patch("goose.setup.console.input", return_value="xoxb-1234"), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_bot_token() == "xoxb-1234"

    def test_keeps_default(self):
        with patch("goose.setup.console.input", return_value=""), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_bot_token("xoxb-existing") == "xoxb-existing"


class TestStepAppToken:
    def test_returns_valid_token(self):
        with patch("goose.setup.console.input", return_value="xapp-5678"), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_app_token() == "xapp-5678"

    def test_keeps_default(self):
        with patch("goose.setup.console.input", return_value=""), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_app_token("xapp-existing") == "xapp-existing"


class TestStepChannelDirs:
    def test_no_defaults_done_immediately(self):
        prompt_values = ["", "d"]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            base_dir, channel_dirs = _step_channel_dirs({})
            assert base_dir == ""
            assert channel_dirs == ""

    def test_add_one_mapping(self):
        prompt_values = [
            "/home/user/code",  # base dir
            "a",                # add
            "frontend",         # channel name
            "frontend",         # path (default)
            "d",                # done
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            base_dir, channel_dirs = _step_channel_dirs({})
            assert base_dir == "/home/user/code"
            assert channel_dirs == "frontend"

    def test_add_custom_mapping(self):
        prompt_values = [
            "",            # base dir (skip)
            "a",           # add
            "web",         # channel name
            "src/frontend",  # custom path
            "d",           # done
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            _, channel_dirs = _step_channel_dirs({})
            assert channel_dirs == "web=src/frontend"

    def test_add_and_remove(self):
        prompt_values = [
            "",           # base dir
            "a",          # add
            "frontend",   # channel name
            "frontend",   # path
            "a",          # add another
            "backend",    # channel name
            "backend",    # path
            "r",          # remove
            "frontend",   # remove frontend
            "d",          # done
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            _, channel_dirs = _step_channel_dirs({})
            assert channel_dirs == "backend"

    def test_existing_mappings_kept(self):
        defaults = {"CHANNEL_DIRS": "frontend,web=src/web", "BASE_DIRECTORY": "/code"}
        prompt_values = ["/code", "d"]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            base_dir, channel_dirs = _step_channel_dirs(defaults)
            assert base_dir == "/code"
            assert "frontend" in channel_dirs
            assert "web=src/web" in channel_dirs

    def test_remove_nonexistent_channel(self):
        defaults = {"CHANNEL_DIRS": "frontend"}
        prompt_values = [
            "",          # base dir
            "r",         # remove
            "nope",      # doesn't exist
            "d",         # done
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            _, channel_dirs = _step_channel_dirs(defaults)
            assert channel_dirs == "frontend"

    def test_hash_prefix_stripped(self):
        prompt_values = [
            "",            # base dir
            "a",           # add
            "#frontend",   # channel name with #
            "frontend",    # path
            "d",           # done
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            _, channel_dirs = _step_channel_dirs({})
            assert channel_dirs == "frontend"


class TestStepAllowedUsers:
    def test_no_defaults_done_immediately(self):
        with patch("goose.setup.Prompt.ask", side_effect=["d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == ""

    def test_add_one_user(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "U123", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123"

    def test_add_multiple_users(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "U123", "a", "U456", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123,U456"

    def test_add_and_remove(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "U123", "a", "U456", "r", "U123", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U456"

    def test_existing_users_kept(self):
        with patch("goose.setup.Prompt.ask", side_effect=["d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            result = _step_allowed_users({"ALLOWED_USERS": "U111,U222"})
            assert result == "U111,U222"

    def test_duplicate_not_added(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "U123", "a", "U123", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123"

    def test_remove_nonexistent(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "U123", "r", "U999", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123"


class TestStepClaudeModel:
    def test_empty_returns_empty(self):
        with patch("goose.setup.Prompt.ask", return_value=""), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_claude_model() == ""

    def test_returns_value(self):
        with patch("goose.setup.Prompt.ask", return_value="sonnet"), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_claude_model() == "sonnet"

    def test_keeps_default(self):
        with patch("goose.setup.Prompt.ask", return_value="opus"), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_claude_model("opus") == "opus"


class TestStepPermissionMode:
    def test_default_kept(self):
        with patch("goose.setup.Prompt.ask", return_value="acceptEdits"), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_permission_mode() == "acceptEdits"

    def test_valid_mode(self):
        with patch("goose.setup.Prompt.ask", return_value="bypassPermissions"), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_permission_mode() == "bypassPermissions"

    def test_invalid_reprompts(self):
        with patch("goose.setup.Prompt.ask", side_effect=["bogus", "dontAsk"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_permission_mode() == "dontAsk"

    def test_all_valid_modes(self):
        for mode in ("acceptEdits", "dontAsk", "bypassPermissions"):
            with patch("goose.setup.Prompt.ask", return_value=mode), \
                 patch("goose.setup.console.print"), \
                 patch("goose.setup.console.rule"):
                assert _step_permission_mode() == mode


class TestParseAllowedTools:
    def test_empty_string(self):
        assert _parse_allowed_tools("") == []

    def test_single_tool(self):
        assert _parse_allowed_tools("Read") == ["Read"]

    def test_multiple_tools(self):
        assert _parse_allowed_tools("Read,Edit,Bash(npm run *)") == ["Read", "Edit", "Bash(npm run *)"]

    def test_whitespace_handling(self):
        assert _parse_allowed_tools(" Read , Edit ") == ["Read", "Edit"]


class TestStepAllowedTools:
    def test_no_defaults_done_immediately(self):
        with patch("goose.setup.Prompt.ask", side_effect=["d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_allowed_tools() == ""

    def test_add_one_tool(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "Read", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_allowed_tools() == "Read"

    def test_add_multiple_tools(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "Read", "a", "Bash(npm run *)", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_allowed_tools() == "Read,Bash(npm run *)"

    def test_add_and_remove(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "Read", "a", "Edit", "r", "Read", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_allowed_tools() == "Edit"

    def test_existing_tools_kept(self):
        with patch("goose.setup.Prompt.ask", side_effect=["d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_allowed_tools("Read,Edit") == "Read,Edit"

    def test_duplicate_not_added(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "Read", "a", "Read", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_allowed_tools() == "Read"

    def test_remove_nonexistent(self):
        with patch("goose.setup.Prompt.ask", side_effect=["a", "Read", "r", "Edit", "d"]), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            assert _step_allowed_tools() == "Read"


class TestStepLogging:
    def test_all_empty(self):
        with patch("goose.setup.Prompt.ask", return_value=""), \
             patch("goose.setup.Confirm.ask", return_value=False), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            log_file, debug = _step_logging({})
            assert log_file == ""
            assert debug is False

    def test_log_file_set(self):
        with patch("goose.setup.Prompt.ask", return_value="goose.log"), \
             patch("goose.setup.Confirm.ask", return_value=False), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            log_file, debug = _step_logging({})
            assert log_file == "goose.log"
            assert debug is False

    def test_debug_enabled(self):
        with patch("goose.setup.Prompt.ask", return_value=""), \
             patch("goose.setup.Confirm.ask", return_value=True), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            _, debug = _step_logging({})
            assert debug is True

    def test_defaults_kept(self):
        with patch("goose.setup.Prompt.ask", return_value="goose.log"), \
             patch("goose.setup.Confirm.ask", return_value=True), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.rule"):
            log_file, debug = _step_logging({"LOG_FILE": "goose.log", "DEBUG": "true"})
            assert log_file == "goose.log"
            assert debug is True


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
        monkeypatch.setenv("GOOSE_CONFIG_DIR", str(tmp_path))
        # Prompt.ask: base dir, done(channels), done(users), model, permission, done(tools), log_file
        prompt_values = ["", "d", "d", "", "", "d", ""]
        # Confirm.ask: debug=False
        confirm_values = [False]
        # console.input: press Enter (step1), bot token, app token
        input_values = [
            "",              # Step 1: press Enter
            "xoxb-bot123",   # Bot token
            "xapp-app456",   # App token
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.Confirm.ask", side_effect=confirm_values), \
             patch("goose.setup.console.input", side_effect=input_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.print_json"), \
             patch("goose.setup.console.rule"), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        env = tmp_path / ".env"
        assert env.exists()
        content = env.read_text()
        assert "SLACK_BOT_TOKEN=xoxb-bot123" in content
        assert "SLACK_APP_TOKEN=xapp-app456" in content

    def test_existing_env_tokens_as_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOSE_CONFIG_DIR", str(tmp_path))
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-old\nSLACK_APP_TOKEN=xapp-old\nBASE_DIRECTORY=/old\n"
        )
        # Prompt.ask: base dir (keep), done(channels), done(users), model, permission, done(tools), log_file
        prompt_values = ["/old", "d", "d", "", "", "d", ""]
        # Confirm.ask: skip step1=True, debug=False
        confirm_values = [True, False]
        # console.input: bot token (empty=keep), app token (empty=keep)
        input_values = [
            "",    # Bot token (keep default)
            "",    # App token (keep default)
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.Confirm.ask", side_effect=confirm_values), \
             patch("goose.setup.console.input", side_effect=input_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.print_json"), \
             patch("goose.setup.console.rule"), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-old" in content
        assert "SLACK_APP_TOKEN=xapp-old" in content
        assert "BASE_DIRECTORY=/old" in content

    def test_existing_env_values_overridden(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOSE_CONFIG_DIR", str(tmp_path))
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-old\nSLACK_APP_TOKEN=xapp-old\nCHANNEL_DIRS=old-proj\n"
        )
        # Prompt.ask: base dir, add channels, done(users), model, permission, done(tools), log_file
        prompt_values = ["", "a", "new-proj", "new-proj", "a", "extra", "extra", "d", "d", "", "", "d", ""]
        # Confirm.ask: skip step1=True, debug=False
        confirm_values = [True, False]
        # console.input: bot token override, app token keep
        input_values = [
            "xoxb-new",   # Override bot token
            "",            # Keep app token
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.Confirm.ask", side_effect=confirm_values), \
             patch("goose.setup.console.input", side_effect=input_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.print_json"), \
             patch("goose.setup.console.rule"), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-new" in content
        assert "SLACK_APP_TOKEN=xapp-old" in content
        assert "new-proj" in content
        assert "extra" in content

    def test_token_validation_reprompts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOSE_CONFIG_DIR", str(tmp_path))
        # Prompt.ask: base dir, done (channels), done (users), model, permission, done (tools), log_file
        prompt_values = ["", "d", "d", "", "", "d", ""]
        # Confirm.ask: debug=False
        confirm_values = [False]
        # console.input: press Enter (step1), bad bot, good bot, bad app, good app
        input_values = [
            "",              # Step 1: press Enter
            "bad-bot",       # Invalid bot token
            "xoxb-good",    # Valid bot token
            "not-app",       # Invalid app token
            "xapp-good",    # Valid app token
        ]
        with patch("goose.setup.Prompt.ask", side_effect=prompt_values), \
             patch("goose.setup.Confirm.ask", side_effect=confirm_values), \
             patch("goose.setup.console.input", side_effect=input_values), \
             patch("goose.setup.console.print"), \
             patch("goose.setup.console.print_json"), \
             patch("goose.setup.console.rule"), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-good" in content
        assert "SLACK_APP_TOKEN=xapp-good" in content

    def test_ctrl_c_exits_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOSE_CONFIG_DIR", str(tmp_path))
        with patch("goose.setup.console.print") as mock_print, \
             patch("goose.setup.console.input", side_effect=KeyboardInterrupt), \
             patch("goose.setup.console.print_json"), \
             patch("goose.setup.console.rule"), \
             patch("goose.setup._copy_to_clipboard", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                setup_command(self._make_args())
            assert exc_info.value.code == 130

        # Check that Aborted was printed
        abort_calls = [
            call for call in mock_print.call_args_list
            if any("Aborted" in str(arg) for arg in call.args)
        ]
        assert len(abort_calls) > 0
