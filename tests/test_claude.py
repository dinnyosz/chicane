"""Tests for chicane.claude."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.claude import (
    ClaudeEvent,
    ClaudeSession,
    _content_blocks_to_dicts,
    _sdk_message_to_raw,
)


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

    def test_num_turns(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "num_turns": 5},
        )
        assert event.num_turns == 5

    def test_num_turns_absent(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result"},
        )
        assert event.num_turns is None

    def test_duration_ms(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "duration_ms": 12000},
        )
        assert event.duration_ms == 12000

    def test_duration_ms_absent(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result"},
        )
        assert event.duration_ms is None

    def test_parent_tool_use_id(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "parent_tool_use_id": "toolu_abc123",
                "message": {"content": []},
            },
        )
        assert event.parent_tool_use_id == "toolu_abc123"

    def test_parent_tool_use_id_absent(self):
        event = ClaudeEvent(
            type="assistant",
            raw={"type": "assistant", "message": {"content": []}},
        )
        assert event.parent_tool_use_id is None

    def test_tool_errors_from_user_event(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "Command failed",
                        },
                        {
                            "type": "tool_result",
                            "is_error": False,
                            "content": "Success",
                        },
                    ]
                },
            },
        )
        errors = event.tool_errors
        assert errors == ["Command failed"]

    def test_tool_errors_with_list_content(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": [{"type": "text", "text": "error part 1"}, {"type": "text", "text": " part 2"}],
                        }
                    ]
                },
            },
        )
        assert event.tool_errors == ["error part 1 part 2"]

    def test_tool_errors_empty_for_non_user_event(self):
        event = ClaudeEvent(
            type="assistant",
            raw={"type": "assistant", "message": {"content": []}},
        )
        assert event.tool_errors == []

    def test_compact_metadata(self):
        event = ClaudeEvent(
            type="system",
            raw={
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {
                    "trigger": "auto",
                    "pre_tokens": 95000,
                },
            },
        )
        assert event.subtype == "compact_boundary"
        assert event.compact_metadata == {"trigger": "auto", "pre_tokens": 95000}

    def test_compact_metadata_absent(self):
        event = ClaudeEvent(
            type="system",
            raw={"type": "system", "subtype": "init", "session_id": "s1"},
        )
        assert event.compact_metadata is None

    def test_permission_denials(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "permission_denials": [
                    {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {}},
                ],
            },
        )
        assert len(event.permission_denials) == 1
        assert event.permission_denials[0]["tool_name"] == "Bash"

    def test_permission_denials_absent(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result"},
        )
        assert event.permission_denials == []

    def test_errors_from_error_result(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error_during_execution",
                "errors": ["Something broke", "Another issue"],
            },
        )
        assert event.errors == ["Something broke", "Another issue"]

    def test_errors_absent(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "subtype": "success"},
        )
        assert event.errors == []

    def test_tool_errors_strips_html_entities_and_xml_tags(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "&lt;tool_use_error&gt;Sibling tool call errored&lt;/tool_use_error&gt;",
                        }
                    ]
                },
            },
        )
        assert event.tool_errors == ["Sibling tool call errored"]

    def test_tool_errors_strips_raw_xml_tags(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "<tool_use_error>Something failed</tool_use_error>",
                        }
                    ]
                },
            },
        )
        assert event.tool_errors == ["Something failed"]

    def test_tool_errors_empty_when_no_errors(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": False,
                            "content": "all good",
                        }
                    ]
                },
            },
        )
        assert event.tool_errors == []


class TestToolUseIds:
    def test_extracts_ids_from_assistant_event(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
                        {"type": "text", "text": "hello"},
                        {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {}},
                    ]
                },
            },
        )
        assert event.tool_use_ids == {"tu_1": "Read", "tu_2": "Bash"}

    def test_empty_for_non_assistant(self):
        event = ClaudeEvent(type="user", raw={"type": "user", "message": {"content": []}})
        assert event.tool_use_ids == {}

    def test_skips_blocks_without_id(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {}},
                    ]
                },
            },
        )
        assert event.tool_use_ids == {}


class TestToolResults:
    def test_tool_results_from_user_event(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "is_error": False, "content": "file contents here"},
                        {"type": "tool_result", "tool_use_id": "tu_2", "is_error": True, "content": "error msg"},
                        {"type": "tool_result", "tool_use_id": "tu_3", "is_error": False, "content": "another result"},
                    ]
                },
            },
        )
        assert event.tool_results == [("tu_1", "file contents here"), ("tu_3", "another result")]

    def test_tool_results_with_list_content(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "is_error": False,
                            "content": [
                                {"type": "text", "text": "part 1"},
                                {"type": "text", "text": " part 2"},
                            ],
                        }
                    ]
                },
            },
        )
        assert event.tool_results == [("tu_1", "part 1 part 2")]

    def test_tool_results_empty_for_non_user_event(self):
        event = ClaudeEvent(
            type="assistant",
            raw={"type": "assistant", "message": {"content": []}},
        )
        assert event.tool_results == []

    def test_tool_results_skips_empty_content(self):
        event = ClaudeEvent(
            type="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "is_error": False, "content": ""},
                    ]
                },
            },
        )
        assert event.tool_results == []


class TestSdkMessageConversion:
    """Test the SDK message → raw dict conversion functions."""

    def test_content_blocks_to_dicts_text(self):
        from claude_agent_sdk import TextBlock
        blocks = [TextBlock(text="hello")]
        result = _content_blocks_to_dicts(blocks)
        assert result == [{"type": "text", "text": "hello"}]

    def test_content_blocks_to_dicts_tool_use(self):
        from claude_agent_sdk import ToolUseBlock
        blocks = [ToolUseBlock(id="tu_1", name="Read", input={"file_path": "/a.py"})]
        result = _content_blocks_to_dicts(blocks)
        assert result == [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/a.py"}}]

    def test_content_blocks_to_dicts_tool_result(self):
        from claude_agent_sdk import ToolResultBlock
        blocks = [ToolResultBlock(tool_use_id="tu_1", content="ok", is_error=False)]
        result = _content_blocks_to_dicts(blocks)
        assert result == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok", "is_error": False}]

    def test_content_blocks_to_dicts_thinking(self):
        from claude_agent_sdk import ThinkingBlock
        blocks = [ThinkingBlock(thinking="let me think", signature="sig123")]
        result = _content_blocks_to_dicts(blocks)
        assert result == [{"type": "thinking", "thinking": "let me think", "signature": "sig123"}]

    def test_sdk_message_to_raw_assistant(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        msg = AssistantMessage(content=[TextBlock(text="hi")], model="opus", parent_tool_use_id=None, error=None)
        raw = _sdk_message_to_raw(msg)
        assert raw["type"] == "assistant"
        assert raw["message"]["content"] == [{"type": "text", "text": "hi"}]

    def test_sdk_message_to_raw_assistant_with_parent(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        msg = AssistantMessage(content=[TextBlock(text="sub")], model="opus", parent_tool_use_id="toolu_abc", error=None)
        raw = _sdk_message_to_raw(msg)
        assert raw["parent_tool_use_id"] == "toolu_abc"

    def test_sdk_message_to_raw_user_string(self):
        from claude_agent_sdk import UserMessage
        msg = UserMessage(content="hello", uuid=None, parent_tool_use_id=None, tool_use_result=None)
        raw = _sdk_message_to_raw(msg)
        assert raw["type"] == "user"
        assert raw["message"]["content"] == [{"type": "text", "text": "hello"}]

    def test_sdk_message_to_raw_system_init(self):
        from claude_agent_sdk import SystemMessage
        msg = SystemMessage(subtype="init", data={"session_id": "s1", "cwd": "/tmp"})
        raw = _sdk_message_to_raw(msg)
        assert raw["type"] == "system"
        assert raw["subtype"] == "init"
        assert raw["session_id"] == "s1"

    def test_sdk_message_to_raw_result(self):
        from claude_agent_sdk import ResultMessage
        msg = ResultMessage(
            subtype="success", duration_ms=5000, duration_api_ms=4000,
            is_error=False, num_turns=3, session_id="s1",
            total_cost_usd=0.05, usage=None, result="done",
        )
        raw = _sdk_message_to_raw(msg)
        assert raw["type"] == "result"
        assert raw["result"] == "done"
        assert raw["num_turns"] == 3
        assert raw["total_cost_usd"] == 0.05


class TestBuildOptions:
    """Test that ClaudeSession._build_options() produces correct ClaudeAgentOptions."""

    def test_basic_options(self):
        session = ClaudeSession(cwd=Path("/tmp/test"))
        opts = session._build_options()
        assert opts.cwd == Path("/tmp/test")
        assert opts.resume is None
        assert opts.model is None
        assert opts.permission_mode is None
        assert opts.system_prompt is None

    def test_with_resume(self):
        session = ClaudeSession(session_id="abc-123")
        opts = session._build_options()
        assert opts.resume == "abc-123"

    def test_with_model(self):
        session = ClaudeSession(model="sonnet")
        opts = session._build_options()
        assert opts.model == "sonnet"

    def test_with_permission_mode(self):
        session = ClaudeSession(permission_mode="bypassPermissions")
        opts = session._build_options()
        assert opts.permission_mode == "bypassPermissions"

    def test_default_permission_not_included(self):
        session = ClaudeSession(permission_mode="default")
        opts = session._build_options()
        assert opts.permission_mode is None

    def test_with_system_prompt(self):
        session = ClaudeSession(system_prompt="You are a Slack bot.")
        opts = session._build_options()
        assert opts.system_prompt == "You are a Slack bot."

    def test_system_prompt_skipped_on_resume(self):
        session = ClaudeSession(system_prompt="You are a Slack bot.", session_id="existing-session")
        opts = session._build_options()
        assert opts.system_prompt is None

    def test_with_allowed_tools(self):
        session = ClaudeSession(allowed_tools=["Bash(npm run *)", "Read"])
        opts = session._build_options()
        assert opts.allowed_tools == ["Bash(npm run *)", "Read"]

    def test_no_allowed_tools(self):
        session = ClaudeSession()
        opts = session._build_options()
        assert opts.allowed_tools == []

    def test_with_max_turns(self):
        session = ClaudeSession(max_turns=25)
        opts = session._build_options()
        assert opts.max_turns == 25

    def test_no_max_turns(self):
        session = ClaudeSession()
        opts = session._build_options()
        assert opts.max_turns is None

    def test_with_max_budget(self):
        session = ClaudeSession(max_budget_usd=5.50)
        opts = session._build_options()
        assert opts.max_budget_usd == 5.50

    def test_no_max_budget(self):
        session = ClaudeSession()
        opts = session._build_options()
        assert opts.max_budget_usd is None


def _mock_sdk_client(messages):
    """Create a mock ClaudeSDKClient that yields the given messages."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.interrupt = MagicMock()

    async def _receive_response():
        for msg in messages:
            yield msg

    client.receive_response = _receive_response
    return client


class TestClaudeSessionStream:
    """Tests for the stream() async generator using the SDK client."""

    @pytest.mark.asyncio
    async def test_stream_yields_parsed_events(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

        messages = [
            SystemMessage(subtype="init", data={"session_id": "s1"}),
            AssistantMessage(content=[TextBlock(text="hi")], model="opus", parent_tool_use_id=None, error=None),
            ResultMessage(subtype="success", duration_ms=1000, duration_api_ms=900,
                          is_error=False, num_turns=1, session_id="s1",
                          total_cost_usd=0.01, usage=None, result="done"),
        ]
        mock_client = _mock_sdk_client(messages)

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=mock_client):
            events = [e async for e in session.stream("hello")]

        assert len(events) == 3
        assert events[0].type == "system"
        assert events[1].type == "assistant"
        assert events[1].text == "hi"
        assert events[2].type == "result"
        assert events[2].text == "done"

    @pytest.mark.asyncio
    async def test_stream_captures_session_id(self):
        from claude_agent_sdk import SystemMessage

        messages = [
            SystemMessage(subtype="init", data={"session_id": "captured-id"}),
        ]
        mock_client = _mock_sdk_client(messages)

        session = ClaudeSession()
        assert session.session_id is None

        with patch.object(session, "_ensure_connected", return_value=mock_client):
            async for _ in session.stream("hello"):
                pass

        assert session.session_id == "captured-id"

    @pytest.mark.asyncio
    async def test_stream_warns_on_no_events(self, caplog):
        mock_client = _mock_sdk_client([])

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=mock_client):
            events = [e async for e in session.stream("hello")]

        assert len(events) == 0
        assert any("no events" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_stream_sets_and_clears_is_streaming(self):
        from claude_agent_sdk import ResultMessage

        messages = [
            ResultMessage(subtype="success", duration_ms=100, duration_api_ms=90,
                          is_error=False, num_turns=1, session_id="s1",
                          total_cost_usd=None, usage=None, result="ok"),
        ]
        mock_client = _mock_sdk_client(messages)

        session = ClaudeSession()
        assert not session.is_streaming

        with patch.object(session, "_ensure_connected", return_value=mock_client):
            async for _ in session.stream("hello"):
                assert session.is_streaming

        assert not session.is_streaming

    @pytest.mark.asyncio
    async def test_stream_clears_is_streaming_on_error(self):
        client = AsyncMock()
        client.query = AsyncMock()

        async def _exploding_response():
            raise RuntimeError("boom")
            yield  # make it an async generator  # noqa: unreachable

        client.receive_response = _exploding_response

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=client):
            with pytest.raises(RuntimeError, match="boom"):
                async for _ in session.stream("hello"):
                    pass

        assert not session.is_streaming

    @pytest.mark.asyncio
    async def test_stream_calls_query_with_prompt(self):
        mock_client = _mock_sdk_client([])

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=mock_client):
            async for _ in session.stream("test prompt"):
                pass

        mock_client.query.assert_awaited_once_with("test prompt")


class TestClaudeSessionRun:
    """Tests for the run() convenience method."""

    @pytest.mark.asyncio
    async def test_run_returns_result_text(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        messages = [
            AssistantMessage(content=[TextBlock(text="thinking")], model="opus", parent_tool_use_id=None, error=None),
            ResultMessage(subtype="success", duration_ms=500, duration_api_ms=400,
                          is_error=False, num_turns=1, session_id="s1",
                          total_cost_usd=0.01, usage=None, result="final answer"),
        ]
        mock_client = _mock_sdk_client(messages)

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=mock_client):
            result = await session.run("hello")

        assert result == "final answer"

    @pytest.mark.asyncio
    async def test_run_returns_empty_when_no_result(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        messages = [
            AssistantMessage(content=[TextBlock(text="just chatting")], model="opus", parent_tool_use_id=None, error=None),
        ]
        mock_client = _mock_sdk_client(messages)

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=mock_client):
            result = await session.run("hello")

        assert result == ""


class TestClaudeSessionDisconnect:
    """Tests for disconnect and kill."""

    @pytest.mark.asyncio
    async def test_disconnect(self):
        session = ClaudeSession()
        session._client = AsyncMock()
        session._connected = True

        await session.disconnect()

        session._client is None
        assert not session._connected

    @pytest.mark.asyncio
    async def test_disconnect_suppresses_anyio_cancel_scope_error(self):
        """Cross-task disconnect (e.g. during shutdown) should not log."""
        session = ClaudeSession()
        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = RuntimeError(
            "Attempted to exit cancel scope in a different task"
        )
        session._client = mock_client
        session._connected = True

        await session.disconnect()  # Should not raise or log

        assert session._client is None
        assert not session._connected

    @pytest.mark.asyncio
    async def test_disconnect_logs_non_cancel_scope_runtime_error(self):
        """Other RuntimeErrors should still be logged."""
        session = ClaudeSession()
        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = RuntimeError("something else")
        session._client = mock_client
        session._connected = True

        await session.disconnect()

        assert session._client is None
        assert not session._connected

    @pytest.mark.asyncio
    async def test_kill_calls_interrupt_then_disconnects(self):
        session = ClaudeSession()
        mock_client = AsyncMock()
        session._client = mock_client
        session._connected = True
        await session.kill()
        mock_client.interrupt.assert_awaited_once()
        mock_client.disconnect.assert_awaited_once()
        assert session._client is None
        assert not session._connected

    @pytest.mark.asyncio
    async def test_kill_noop_when_no_client(self):
        session = ClaudeSession()
        await session.kill()  # Should not raise

    @pytest.mark.asyncio
    async def test_interrupt_when_streaming(self):
        session = ClaudeSession()
        session._client = AsyncMock()
        session._is_streaming = True
        await session.interrupt()
        session._client.interrupt.assert_awaited_once()
        assert session.was_interrupted

    @pytest.mark.asyncio
    async def test_interrupt_noop_when_not_streaming(self):
        session = ClaudeSession()
        session._client = AsyncMock()
        session._is_streaming = False
        await session.interrupt()
        session._client.interrupt.assert_not_awaited()
        assert not session.was_interrupted

    @pytest.mark.asyncio
    async def test_was_interrupted_resets_on_stream(self):
        from claude_agent_sdk import ResultMessage

        messages = [
            ResultMessage(subtype="success", duration_ms=100, duration_api_ms=90,
                          is_error=False, num_turns=1, session_id="s1",
                          total_cost_usd=None, usage=None, result="ok"),
        ]
        mock_client = _mock_sdk_client(messages)

        session = ClaudeSession()
        session._interrupted = True  # Set from a previous interrupt

        with patch.object(session, "_ensure_connected", return_value=mock_client):
            async for _ in session.stream("hello"):
                pass

        # stream() should have reset the flag
        assert not session.was_interrupted


class TestEnsureConnected:
    """Tests for the _ensure_connected method."""

    @pytest.mark.asyncio
    async def test_creates_client_on_first_call(self):
        session = ClaudeSession(cwd=Path("/tmp"))

        with patch("chicane.claude.ClaudeSDKClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value = mock_instance

            client = await session._ensure_connected()

            MockClient.assert_called_once()
            mock_instance.connect.assert_awaited_once()
            assert client is mock_instance
            assert session._connected

    @pytest.mark.asyncio
    async def test_reuses_existing_client(self):
        session = ClaudeSession()
        existing_client = AsyncMock()
        session._client = existing_client
        session._connected = True

        client = await session._ensure_connected()

        assert client is existing_client
        # connect should NOT have been called again
        existing_client.connect.assert_not_awaited()
