"""Tests for goose.claude."""

import pytest

from goose.claude import ClaudeEvent, ClaudeSession


class TestClaudeEvent:
    def test_assistant_text(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world!"},
                    ]
                },
            },
        )
        assert event.text == "Hello world!"

    def test_assistant_mixed_content(self):
        """Text extraction should skip non-text content blocks."""
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {"type": "tool_use", "name": "Read", "id": "123"},
                        {"type": "text", "text": " Done."},
                    ]
                },
            },
        )
        assert event.text == "Let me check. Done."

    def test_result_text(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "success",
                "result": "The answer is 42.",
                "is_error": False,
                "total_cost_usd": 0.05,
            },
        )
        assert event.text == "The answer is 42."
        assert event.is_error is False
        assert event.cost_usd == 0.05

    def test_result_error(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error",
                "result": "Something failed",
                "is_error": True,
            },
        )
        assert event.is_error is True
        assert event.text == "Something failed"

    def test_system_init(self):
        event = ClaudeEvent(
            type="system",
            raw={
                "type": "system",
                "subtype": "init",
                "session_id": "abc-123",
                "cwd": "/tmp",
            },
        )
        assert event.subtype == "init"
        assert event.session_id == "abc-123"
        assert event.text == ""

    def test_empty_content(self):
        event = ClaudeEvent(
            type="assistant",
            raw={"type": "assistant", "message": {"content": []}},
        )
        assert event.text == ""


class TestClaudeSession:
    def test_build_command_basic(self):
        session = ClaudeSession()
        cmd = session._build_command("hello")
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd
        assert cmd[-1] == "hello"

    def test_build_command_with_resume(self):
        session = ClaudeSession(session_id="abc-123")
        cmd = session._build_command("follow up")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "abc-123"

    def test_build_command_with_model(self):
        session = ClaudeSession(model="sonnet")
        cmd = session._build_command("hello")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "sonnet"

    def test_build_command_with_permission_mode(self):
        session = ClaudeSession(permission_mode="bypassPermissions")
        cmd = session._build_command("hello")
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "bypassPermissions"

    def test_build_command_default_permission_not_included(self):
        session = ClaudeSession(permission_mode="default")
        cmd = session._build_command("hello")
        assert "--permission-mode" not in cmd

    def test_build_command_with_system_prompt(self):
        session = ClaudeSession(system_prompt="You are a Slack bot.")
        cmd = session._build_command("hello")
        assert "--append-system-prompt" in cmd
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == "You are a Slack bot."

    def test_build_command_no_system_prompt_by_default(self):
        session = ClaudeSession()
        cmd = session._build_command("hello")
        assert "--append-system-prompt" not in cmd

    def test_system_prompt_skipped_on_resume(self):
        """System prompt should only be sent on the first call, not on resumes."""
        session = ClaudeSession(system_prompt="You are a Slack bot.")
        session.session_id = "existing-session-123"
        cmd = session._build_command("follow up")
        assert "--append-system-prompt" not in cmd

    def test_build_command_with_allowed_tools(self):
        session = ClaudeSession(allowed_tools=["WebFetch", "WebSearch"])
        cmd = session._build_command("hello")
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "WebFetch"
        assert cmd[idx + 2] == "WebSearch"
        # prompt should still be last
        assert cmd[-1] == "hello"

    def test_build_command_no_allowed_tools_by_default(self):
        session = ClaudeSession()
        cmd = session._build_command("hello")
        assert "--allowedTools" not in cmd

    def test_build_command_empty_allowed_tools(self):
        session = ClaudeSession(allowed_tools=[])
        cmd = session._build_command("hello")
        assert "--allowedTools" not in cmd
