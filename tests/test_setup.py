"""Tests for chicane.setup — the setup wizard."""

import argparse
import signal
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from chicane.app import save_terminal_state
from chicane.setup import (
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
    _step_disallowed_tools,
    _step_logging,
    _step_max_budget,
    _step_max_turns,
    _step_permission_mode,
    _step_setting_sources,
    _step_verbosity,
    _write_env,
    setup_command,
)


class TestSaveTerminalState:
    def test_saves_and_fixes_isig_when_disabled(self):
        """save_terminal_state enables ISIG and registers cleanup."""
        import termios

        saved_attrs = [0, 0, 0, termios.ISIG, 0, 0, []]
        broken_attrs = [0, 0, 0, 0, 0, 0, []]
        # First call returns saved state, second returns broken state for fix
        with patch("sys.stdin") as mock_stdin, \
             patch("termios.tcgetattr", side_effect=[saved_attrs, broken_attrs]), \
             patch("termios.tcsetattr") as mock_set, \
             patch("atexit.register") as mock_atexit, \
             patch("chicane.app.signal.signal"):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            result = save_terminal_state()
            assert result == saved_attrs
            mock_atexit.assert_called_once()
            # tcsetattr called to fix ISIG
            mock_set.assert_called_once()
            set_attrs = mock_set.call_args[0][2]
            assert set_attrs[3] & termios.ISIG

    def test_skips_fix_when_isig_already_set(self):
        """save_terminal_state doesn't touch termios if ISIG is fine."""
        import termios

        good_attrs = [0, 0, 0, termios.ISIG, 0, 0, []]
        with patch("sys.stdin") as mock_stdin, \
             patch("termios.tcgetattr", return_value=good_attrs), \
             patch("termios.tcsetattr") as mock_set, \
             patch("atexit.register"), \
             patch("chicane.app.signal.signal"):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            result = save_terminal_state()
            assert result == good_attrs
            mock_set.assert_not_called()

    def test_returns_none_when_not_a_tty(self):
        """save_terminal_state returns None when stdin is not a tty."""
        with patch("sys.stdin") as mock_stdin, \
             patch("termios.tcgetattr") as mock_get:
            mock_stdin.isatty.return_value = False
            result = save_terminal_state()
            assert result is None
            mock_get.assert_not_called()

    def test_handles_oserror_gracefully(self):
        """save_terminal_state returns None on OSError."""
        with patch("sys.stdin") as mock_stdin, \
             patch("termios.tcgetattr", side_effect=OSError):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            result = save_terminal_state()
            assert result is None

    def test_registers_sigterm_handler(self):
        """save_terminal_state installs a SIGTERM handler."""
        import termios

        attrs = [0, 0, 0, termios.ISIG, 0, 0, []]
        with patch("sys.stdin") as mock_stdin, \
             patch("termios.tcgetattr", return_value=attrs), \
             patch("termios.tcsetattr"), \
             patch("atexit.register"), \
             patch("chicane.app.signal.signal") as mock_signal:
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            save_terminal_state()
            mock_signal.assert_called_once()
            assert mock_signal.call_args[0][0] == signal.SIGTERM


class TestLoadManifest:
    def test_loads_valid_manifest(self):
        manifest = _load_manifest()
        assert manifest["display_information"]["name"] == "Chicane"
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
        with patch("chicane.setup.subprocess.run") as mock_run:
            assert _copy_to_clipboard("hello") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["pbcopy"]
            assert args[1]["input"] == b"hello"

    def test_fallback_on_failure(self):
        with patch("chicane.setup.subprocess.run", side_effect=FileNotFoundError):
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
        with patch("chicane.setup.Prompt.ask", return_value=""):
            assert _prompt_with_default("Label") == ""

    def test_no_default_with_input(self):
        with patch("chicane.setup.Prompt.ask", return_value="new-value"):
            assert _prompt_with_default("Label") == "new-value"

    def test_default_kept_on_empty_input(self):
        with patch("chicane.setup.Prompt.ask", return_value="old-value"):
            assert _prompt_with_default("Label", "old-value") == "old-value"

    def test_default_overridden(self):
        with patch("chicane.setup.Prompt.ask", return_value="new-value"):
            assert _prompt_with_default("Label", "old-value") == "new-value"

    def test_dash_clears_default(self):
        with patch("chicane.setup.Prompt.ask", return_value="-"):
            assert _prompt_with_default("Label", "old-value") == ""

    def test_dash_without_default_is_literal(self):
        with patch("chicane.setup.Prompt.ask", return_value="-"):
            assert _prompt_with_default("Label") == "-"


class TestPromptToken:
    def test_valid_on_first_try(self):
        with patch("chicane.setup.console.input", return_value="xoxb-valid"):
            result = _prompt_token("Bot Token", "xoxb-")
            assert result == "xoxb-valid"

    def test_reprompts_on_bad_prefix(self):
        with patch("chicane.setup.console.input", side_effect=["bad-token", "xoxb-good"]), \
             patch("chicane.setup.console.print"):
            result = _prompt_token("Bot Token", "xoxb-")
            assert result == "xoxb-good"

    def test_default_kept_on_empty_input(self):
        with patch("chicane.setup.console.input", return_value=""):
            result = _prompt_token("Bot Token", "xoxb-", default="xoxb-existing")
            assert result == "xoxb-existing"

    def test_default_overridden_with_valid(self):
        with patch("chicane.setup.console.input", return_value="xoxb-new"):
            result = _prompt_token("Bot Token", "xoxb-", default="xoxb-old")
            assert result == "xoxb-new"

    def test_default_shown_masked(self):
        with patch("chicane.setup.console.input", return_value="") as mock_input:
            _prompt_token("Bot Token", "xoxb-", default="xoxb-1234567890")
            prompt_text = mock_input.call_args[0][0]
            assert "xoxb-123...7890" in prompt_text

    def test_short_token_masked_safely(self):
        with patch("chicane.setup.console.input", return_value="") as mock_input:
            _prompt_token("Bot Token", "xoxb-", default="xoxb-abc")
            prompt_text = mock_input.call_args[0][0]
            assert "xoxb-..." in prompt_text
            assert "xoxb-abc]" not in prompt_text


class TestStepBotToken:
    def test_returns_valid_token(self):
        with patch("chicane.setup.console.input", return_value="xoxb-1234"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_bot_token() == "xoxb-1234"

    def test_keeps_default(self):
        with patch("chicane.setup.console.input", return_value=""), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_bot_token("xoxb-existing") == "xoxb-existing"


class TestStepAppToken:
    def test_returns_valid_token(self):
        with patch("chicane.setup.console.input", return_value="xapp-5678"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_app_token() == "xapp-5678"

    def test_keeps_default(self):
        with patch("chicane.setup.console.input", return_value=""), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_app_token("xapp-existing") == "xapp-existing"


class TestStepChannelDirs:
    def test_no_defaults_done_immediately(self):
        prompt_values = ["", "d"]
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
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
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
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
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
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
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            _, channel_dirs = _step_channel_dirs({})
            assert channel_dirs == "backend"

    def test_existing_mappings_kept(self):
        defaults = {"CHANNEL_DIRS": "frontend,web=src/web", "BASE_DIRECTORY": "/code"}
        prompt_values = ["/code", "d"]
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
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
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
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
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            _, channel_dirs = _step_channel_dirs({})
            assert channel_dirs == "frontend"


class TestStepAllowedUsers:
    def test_no_defaults_done_immediately(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == ""

    def test_add_one_user(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "U123", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123"

    def test_add_multiple_users(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "U123", "a", "U456", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123,U456"

    def test_add_and_remove(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "U123", "a", "U456", "r", "U123", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U456"

    def test_existing_users_kept(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            result = _step_allowed_users({"ALLOWED_USERS": "U111,U222"})
            assert result == "U111,U222"

    def test_duplicate_not_added(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "U123", "a", "U123", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123"

    def test_remove_nonexistent(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "U123", "r", "U999", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            result = _step_allowed_users({})
            assert result == "U123"


class TestStepClaudeModel:
    def test_empty_returns_empty(self):
        with patch("chicane.setup.Prompt.ask", return_value=""), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_claude_model() == ""

    def test_returns_value(self):
        with patch("chicane.setup.Prompt.ask", return_value="sonnet"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_claude_model() == "sonnet"

    def test_keeps_default(self):
        with patch("chicane.setup.Prompt.ask", return_value="opus"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_claude_model("opus") == "opus"


class TestStepPermissionMode:
    def test_default_kept(self):
        with patch("chicane.setup.Prompt.ask", return_value="acceptEdits"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_permission_mode() == "acceptEdits"

    def test_valid_mode(self):
        with patch("chicane.setup.Prompt.ask", return_value="bypassPermissions"), \
             patch("chicane.setup.Confirm.ask", return_value=True), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_permission_mode() == "bypassPermissions"

    def test_invalid_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["bogus", "dontAsk"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_permission_mode() == "dontAsk"

    def test_all_valid_modes(self):
        for mode in ("acceptEdits", "dontAsk", "bypassPermissions"):
            with patch("chicane.setup.Prompt.ask", return_value=mode), \
                 patch("chicane.setup.Confirm.ask", return_value=True), \
                 patch("chicane.setup.console.print"), \
                 patch("chicane.setup.console.rule"):
                assert _step_permission_mode() == mode

    def test_bypass_declined_reprompts(self):
        """Declining bypassPermissions confirmation re-prompts."""
        with patch("chicane.setup.Prompt.ask", side_effect=["bypassPermissions", "acceptEdits"]), \
             patch("chicane.setup.Confirm.ask", return_value=False), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_permission_mode() == "acceptEdits"


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
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_allowed_tools() == ""

    def test_add_one_tool(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Read", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_allowed_tools() == "Read"

    def test_add_multiple_tools(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Read", "a", "Bash(npm run *)", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_allowed_tools() == "Read,Bash(npm run *)"

    def test_add_and_remove(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Read", "a", "Edit", "r", "Read", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_allowed_tools() == "Edit"

    def test_existing_tools_kept(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_allowed_tools("Read,Edit") == "Read,Edit"

    def test_duplicate_not_added(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Read", "a", "Read", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_allowed_tools() == "Read"

    def test_remove_nonexistent(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Read", "r", "Edit", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_allowed_tools() == "Read"


class TestStepDisallowedTools:
    def test_no_defaults_done_immediately(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_disallowed_tools() == ""

    def test_add_one_tool(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Bash", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_disallowed_tools() == "Bash"

    def test_add_multiple_tools(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Bash", "a", "WebFetch", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_disallowed_tools() == "Bash,WebFetch"

    def test_add_and_remove(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Bash", "a", "Edit", "r", "Bash", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_disallowed_tools() == "Edit"

    def test_existing_tools_kept(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_disallowed_tools("Bash,WebFetch") == "Bash,WebFetch"

    def test_duplicate_not_added(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Bash", "a", "Bash", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_disallowed_tools() == "Bash"

    def test_remove_nonexistent(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "Bash", "r", "Edit", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_disallowed_tools() == "Bash"


class TestStepSettingSources:
    def test_default_all_sources(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_setting_sources() == "user,project,local"

    def test_remove_one_source(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["r", "local", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_setting_sources() == "user,project"

    def test_remove_all_and_add_one(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["r", "user", "r", "project", "r", "local", "a", "project", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_setting_sources() == "project"

    def test_invalid_source_rejected(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "global", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_setting_sources() == "user,project,local"

    def test_duplicate_not_added(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["a", "user", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_setting_sources() == "user,project,local"

    def test_existing_value_kept(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_setting_sources("user,project") == "user,project"

    def test_remove_nonexistent(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["r", "nope", "d"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_setting_sources() == "user,project,local"


class TestStepMaxTurns:
    def test_empty_returns_empty(self):
        with patch("chicane.setup.Prompt.ask", return_value=""), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_turns() == ""

    def test_valid_integer(self):
        with patch("chicane.setup.Prompt.ask", return_value="50"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_turns() == "50"

    def test_keeps_default(self):
        with patch("chicane.setup.Prompt.ask", return_value="25"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_turns("25") == "25"

    def test_invalid_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["abc", "10"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_turns() == "10"

    def test_zero_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["0", "5"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_turns() == "5"

    def test_negative_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["-3", "1"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_turns() == "1"

    def test_empty_clears_existing(self):
        """Empty input when there's an existing value clears it."""
        with patch("chicane.setup.Prompt.ask", return_value="-"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_turns("25") == ""


class TestStepMaxBudget:
    def test_empty_returns_empty(self):
        with patch("chicane.setup.Prompt.ask", return_value=""), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget() == ""

    def test_valid_float(self):
        with patch("chicane.setup.Prompt.ask", return_value="1.50"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget() == "1.50"

    def test_valid_integer(self):
        with patch("chicane.setup.Prompt.ask", return_value="5"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget() == "5"

    def test_keeps_default(self):
        with patch("chicane.setup.Prompt.ask", return_value="2.00"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget("2.00") == "2.00"

    def test_invalid_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["abc", "3.50"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget() == "3.50"

    def test_zero_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["0", "1.00"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget() == "1.00"

    def test_negative_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["-5", "0.50"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget() == "0.50"

    def test_empty_clears_existing(self):
        """Dash clears existing default."""
        with patch("chicane.setup.Prompt.ask", return_value="-"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_max_budget("2.00") == ""


class TestStepLogging:
    def test_accepts_suggested_default(self):
        """Accepting the platformdirs default by pressing Enter."""
        from platformdirs import user_log_dir
        expected = user_log_dir("chicane", appauthor=False)
        with patch("chicane.setup.Prompt.ask", side_effect=[expected, "INFO"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            log_dir, log_level = _step_logging({})
            assert log_dir == expected
            assert log_level == "INFO"

    def test_log_dir_overridden(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["/var/log/chicane", "INFO"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            log_dir, log_level = _step_logging({})
            assert log_dir == "/var/log/chicane"
            assert log_level == "INFO"

    def test_log_dir_cleared_with_dash(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["-", "INFO"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            log_dir, _ = _step_logging({"LOG_DIR": "/old/path"})
            assert log_dir == ""

    def test_debug_level(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["", "DEBUG"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            _, log_level = _step_logging({})
            assert log_level == "DEBUG"

    def test_case_insensitive(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["", "warning"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            _, log_level = _step_logging({})
            assert log_level == "WARNING"

    def test_invalid_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["", "almafa", "ERROR"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            _, log_level = _step_logging({})
            assert log_level == "ERROR"

    def test_defaults_kept(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["/var/log/chicane", "DEBUG"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            log_dir, log_level = _step_logging({"LOG_DIR": "/var/log/chicane", "LOG_LEVEL": "DEBUG"})
            assert log_dir == "/var/log/chicane"
            assert log_level == "DEBUG"


class TestStepVerbosity:
    def test_default_normal(self):
        with patch("chicane.setup.Prompt.ask", return_value="normal"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_verbosity() == "normal"

    def test_minimal(self):
        with patch("chicane.setup.Prompt.ask", return_value="minimal"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_verbosity() == "minimal"

    def test_verbose(self):
        with patch("chicane.setup.Prompt.ask", return_value="verbose"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_verbosity() == "verbose"

    def test_invalid_reprompts(self):
        with patch("chicane.setup.Prompt.ask", side_effect=["bogus", "minimal"]), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_verbosity() == "minimal"

    def test_case_insensitive(self):
        with patch("chicane.setup.Prompt.ask", return_value="VERBOSE"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_verbosity() == "verbose"

    def test_empty_returns_verbose(self):
        with patch("chicane.setup.Prompt.ask", return_value=""), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_verbosity() == "verbose"

    def test_keeps_default(self):
        with patch("chicane.setup.Prompt.ask", return_value="verbose"), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.rule"):
            assert _step_verbosity("verbose") == "verbose"


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

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_creates_file_with_0600(self, tmp_path):
        """_write_env() must create the .env file with mode 0o600."""
        env_file = tmp_path / ".env"
        _write_env(env_file, {"SLACK_BOT_TOKEN": "xoxb-test"})
        assert env_file.stat().st_mode & 0o777 == 0o600


class TestSetupCommand:
    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace()

    def test_fresh_setup_writes_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHICANE_CONFIG_DIR", str(tmp_path))
        # Prompt.ask: base dir, done(channels), done(users), model, permission, done(tools), done(disallowed), done(sources), max_turns, max_budget, rate_limit, log_dir, log_level, verbosity
        prompt_values = ["", "d", "d", "", "", "d", "d", "d", "", "", "10", "", "INFO", "normal"]
        # console.input: press Enter (step1), bot token, app token
        input_values = [
            "",              # Step 1: press Enter
            "xoxb-bot123",   # Bot token
            "xapp-app456",   # App token
        ]
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.input", side_effect=input_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.print_json"), \
             patch("chicane.setup.console.rule"), \
             patch("chicane.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        env = tmp_path / ".env"
        assert env.exists()
        content = env.read_text()
        assert "SLACK_BOT_TOKEN=xoxb-bot123" in content
        assert "SLACK_APP_TOKEN=xapp-app456" in content

    def test_existing_env_tokens_as_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHICANE_CONFIG_DIR", str(tmp_path))
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-old\nSLACK_APP_TOKEN=xapp-old\nBASE_DIRECTORY=/old\n"
        )
        # Prompt.ask: base dir (keep), done(channels), done(users), model, permission, done(tools), done(disallowed), done(sources), max_turns, max_budget, rate_limit, log_dir, log_level, verbosity
        prompt_values = ["/old", "d", "d", "", "", "d", "d", "d", "", "", "10", "", "INFO", "normal"]
        # Confirm.ask: skip step1=True
        confirm_values = [True]
        # console.input: bot token (empty=keep), app token (empty=keep)
        input_values = [
            "",    # Bot token (keep default)
            "",    # App token (keep default)
        ]
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.Confirm.ask", side_effect=confirm_values), \
             patch("chicane.setup.console.input", side_effect=input_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.print_json"), \
             patch("chicane.setup.console.rule"), \
             patch("chicane.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-old" in content
        assert "SLACK_APP_TOKEN=xapp-old" in content
        assert "BASE_DIRECTORY=/old" in content

    def test_existing_env_values_overridden(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHICANE_CONFIG_DIR", str(tmp_path))
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-old\nSLACK_APP_TOKEN=xapp-old\nCHANNEL_DIRS=old-proj\n"
        )
        # Prompt.ask: base dir, add channels, done(users), model, permission, done(tools), done(disallowed), done(sources), max_turns, max_budget, rate_limit, log_dir, log_level, verbosity
        prompt_values = ["", "a", "new-proj", "new-proj", "a", "extra", "extra", "d", "d", "", "", "d", "d", "d", "", "", "10", "", "INFO", "normal"]
        # Confirm.ask: skip step1=True
        confirm_values = [True]
        # console.input: bot token override, app token keep
        input_values = [
            "xoxb-new",   # Override bot token
            "",            # Keep app token
        ]
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.Confirm.ask", side_effect=confirm_values), \
             patch("chicane.setup.console.input", side_effect=input_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.print_json"), \
             patch("chicane.setup.console.rule"), \
             patch("chicane.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-new" in content
        assert "SLACK_APP_TOKEN=xapp-old" in content
        assert "new-proj" in content
        assert "extra" in content

    def test_token_validation_reprompts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHICANE_CONFIG_DIR", str(tmp_path))
        # Prompt.ask: base dir, done (channels), done (users), model, permission, done (tools), done(disallowed), done(sources), max_turns, max_budget, rate_limit, log_dir, log_level, verbosity
        prompt_values = ["", "d", "d", "", "", "d", "d", "d", "", "", "10", "", "INFO", "normal"]
        # console.input: press Enter (step1), bad bot, good bot, bad app, good app
        input_values = [
            "",              # Step 1: press Enter
            "bad-bot",       # Invalid bot token
            "xoxb-good",    # Valid bot token
            "not-app",       # Invalid app token
            "xapp-good",    # Valid app token
        ]
        with patch("chicane.setup.Prompt.ask", side_effect=prompt_values), \
             patch("chicane.setup.console.input", side_effect=input_values), \
             patch("chicane.setup.console.print"), \
             patch("chicane.setup.console.print_json"), \
             patch("chicane.setup.console.rule"), \
             patch("chicane.setup._copy_to_clipboard", return_value=False):
            setup_command(self._make_args())

        content = (tmp_path / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-good" in content
        assert "SLACK_APP_TOKEN=xapp-good" in content

    def test_ctrl_c_exits_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHICANE_CONFIG_DIR", str(tmp_path))
        with patch("chicane.setup.console.print") as mock_print, \
             patch("chicane.setup.console.input", side_effect=KeyboardInterrupt), \
             patch("chicane.setup.console.print_json"), \
             patch("chicane.setup.console.rule"), \
             patch("chicane.setup._copy_to_clipboard", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                setup_command(self._make_args())
            assert exc_info.value.code == 130

        # Check that Aborted was printed
        abort_calls = [
            call for call in mock_print.call_args_list
            if any("Aborted" in str(arg) for arg in call.args)
        ]
        assert len(abort_calls) > 0
