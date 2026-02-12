"""Tests for chicane.claude."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.claude import ClaudeEvent, ClaudeSession


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
        session = ClaudeSession(allowed_tools=["Bash(npm run *)", "Read"])
        cmd = session._build_command("do stuff")
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "Bash(npm run *)"
        assert cmd[idx + 2] == "Read"

    def test_build_command_no_allowed_tools(self):
        session = ClaudeSession()
        cmd = session._build_command("do stuff")
        assert "--allowedTools" not in cmd


def _make_process_mock(stdout_lines: list[bytes], returncode: int = 0):
    """Create a mock subprocess with the given stdout lines."""
    process = AsyncMock()
    process.returncode = returncode

    async def stdout_iter():
        for line in stdout_lines:
            yield line

    process.stdout = stdout_iter()
    process.stderr = AsyncMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.wait = AsyncMock()
    process.kill = MagicMock()
    return process


class TestClaudeSessionStream:
    """Tests for the stream() async generator."""

    @pytest.mark.asyncio
    async def test_stream_yields_parsed_events(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}).encode() + b"\n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}).encode() + b"\n",
            json.dumps({"type": "result", "result": "done"}).encode() + b"\n",
        ]
        process = _make_process_mock(lines)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            events = [e async for e in session.stream("hello")]

        assert len(events) == 3
        assert events[0].type == "system"
        assert events[1].type == "assistant"
        assert events[1].text == "hi"
        assert events[2].type == "result"
        assert events[2].text == "done"

    @pytest.mark.asyncio
    async def test_stream_captures_session_id(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "captured-id"}).encode() + b"\n",
        ]
        process = _make_process_mock(lines)

        session = ClaudeSession()
        assert session.session_id is None

        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for _ in session.stream("hello"):
                pass

        assert session.session_id == "captured-id"

    @pytest.mark.asyncio
    async def test_stream_skips_empty_lines(self):
        lines = [
            b"\n",
            b"   \n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "only"}]}}).encode() + b"\n",
            b"\n",
        ]
        process = _make_process_mock(lines)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            events = [e async for e in session.stream("hello")]

        assert len(events) == 1
        assert events[0].text == "only"

    @pytest.mark.asyncio
    async def test_stream_skips_invalid_json(self):
        lines = [
            b"not json at all\n",
            json.dumps({"type": "result", "result": "ok"}).encode() + b"\n",
        ]
        process = _make_process_mock(lines)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            events = [e async for e in session.stream("hello")]

        assert len(events) == 1
        assert events[0].type == "result"

    @pytest.mark.asyncio
    async def test_stream_kills_process_on_exception(self):
        async def exploding_stdout():
            yield json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}).encode() + b"\n"
            raise RuntimeError("boom")

        process = AsyncMock()
        process.returncode = 0
        process.stdout = exploding_stdout()
        process.stderr = AsyncMock()
        process.stderr.read = AsyncMock(return_value=b"")
        process.wait = AsyncMock()
        process.kill = MagicMock()

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="boom"):
                async for _ in session.stream("hello"):
                    pass

        process.kill.assert_called()

    @pytest.mark.asyncio
    async def test_stream_logs_nonzero_exit_code(self, caplog):
        lines = [
            json.dumps({"type": "result", "result": "partial"}).encode() + b"\n",
        ]
        process = _make_process_mock(lines, returncode=1)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for _ in session.stream("hello"):
                pass

        assert any("exited with code 1" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_stream_warns_on_no_events(self, caplog):
        process = _make_process_mock([], returncode=0)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            events = [e async for e in session.stream("hello")]

        assert len(events) == 0
        assert any("no events" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_stream_timeout_on_process_wait(self, caplog):
        lines = [
            json.dumps({"type": "result", "result": "ok"}).encode() + b"\n",
        ]
        process = _make_process_mock(lines, returncode=0)
        process.wait = AsyncMock(side_effect=asyncio.TimeoutError)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for _ in session.stream("hello"):
                pass

        process.kill.assert_called()
        assert any("did not exit in time" in r.message for r in caplog.records)


class TestClaudeSessionRun:
    """Tests for the run() convenience method."""

    @pytest.mark.asyncio
    async def test_run_returns_result_text(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}}).encode() + b"\n",
            json.dumps({"type": "result", "result": "final answer"}).encode() + b"\n",
        ]
        process = _make_process_mock(lines)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await session.run("hello")

        assert result == "final answer"

    @pytest.mark.asyncio
    async def test_run_returns_empty_when_no_result(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "just chatting"}]}}).encode() + b"\n",
        ]
        process = _make_process_mock(lines)

        session = ClaudeSession()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await session.run("hello")

        assert result == ""
