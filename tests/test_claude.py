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
        assert errors == [("", "Command failed")]

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
        assert event.tool_errors == [("", "error part 1 part 2")]

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
        assert event.tool_errors == [("", "Sibling tool call errored")]

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
        assert event.tool_errors == [("", "Something failed")]

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

    def test_sdk_message_to_raw_user_tool_use_result_merged(self):
        """When SDK delivers tool result via tool_use_result field (e.g. MCP tools),
        it should be merged into the content list as a tool_result block."""
        from claude_agent_sdk import UserMessage
        msg = UserMessage(
            content="",
            uuid=None,
            parent_tool_use_id=None,
            tool_use_result={
                "tool_use_id": "toolu_mcp",
                "content": "search results here",
                "is_error": False,
            },
        )
        raw = _sdk_message_to_raw(msg)
        content = raw["message"]["content"]
        tool_results = [b for b in content if b.get("type") == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "toolu_mcp"
        assert tool_results[0]["content"] == "search results here"
        assert tool_results[0]["is_error"] is False

    def test_sdk_message_to_raw_user_tool_use_result_not_duplicated(self):
        """When content already has tool_result blocks, tool_use_result should not
        be merged (avoid duplicates)."""
        from claude_agent_sdk import ToolResultBlock, UserMessage
        msg = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_abc",
                    content="file updated",
                    is_error=False,
                ),
            ],
            uuid=None,
            parent_tool_use_id=None,
            tool_use_result={
                "tool_use_id": "toolu_abc",
                "content": "file updated",
            },
        )
        raw = _sdk_message_to_raw(msg)
        content = raw["message"]["content"]
        tool_results = [b for b in content if b.get("type") == "tool_result"]
        assert len(tool_results) == 1  # Not duplicated

    def test_sdk_message_to_raw_user_tool_use_result_with_list_content(self):
        """MCP tool results may have list content (e.g. [{'type': 'text', 'text': '...'}])."""
        from claude_agent_sdk import UserMessage
        msg = UserMessage(
            content="",
            uuid=None,
            parent_tool_use_id=None,
            tool_use_result={
                "tool_use_id": "toolu_mcp",
                "content": [{"type": "text", "text": "mcp result"}],
            },
        )
        raw = _sdk_message_to_raw(msg)
        content = raw["message"]["content"]
        tool_results = [b for b in content if b.get("type") == "tool_result"]
        assert len(tool_results) == 1
        # Verify it can be extracted by tool_results property
        event = ClaudeEvent(type="user", raw=raw)
        results = event.tool_results
        assert len(results) == 1
        assert results[0] == ("toolu_mcp", "mcp result")

    def test_sdk_message_to_raw_user_tool_use_result_error(self):
        """MCP tool errors via tool_use_result should be extractable by tool_errors."""
        from claude_agent_sdk import UserMessage
        msg = UserMessage(
            content="",
            uuid=None,
            parent_tool_use_id=None,
            tool_use_result={
                "tool_use_id": "toolu_mcp",
                "content": "Connection refused",
                "is_error": True,
            },
        )
        raw = _sdk_message_to_raw(msg)
        event = ClaudeEvent(type="user", raw=raw)
        assert len(event.tool_errors) == 1
        assert event.tool_errors[0] == ("toolu_mcp", "Connection refused")

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

    def test_hooks_always_present(self):
        """_build_options always includes PreToolUse hooks for plan mode."""
        session = ClaudeSession()
        opts = session._build_options()
        assert opts.hooks is not None
        assert "PreToolUse" in opts.hooks
        matchers = opts.hooks["PreToolUse"]
        assert len(matchers) >= 2  # plan mode hook + dummy hook

    def test_hooks_plan_mode_matcher(self):
        """Plan mode hook targets EnterPlanMode|ExitPlanMode."""
        session = ClaudeSession()
        opts = session._build_options()
        plan_matcher = opts.hooks["PreToolUse"][0]
        assert plan_matcher.matcher == "EnterPlanMode|ExitPlanMode"

    def test_can_use_tool_set_with_callback(self):
        """can_use_tool is set when ask_user_callback is provided."""
        async def fake_callback(questions):
            return {}
        session = ClaudeSession(ask_user_callback=fake_callback)
        opts = session._build_options()
        assert opts.can_use_tool is not None

    def test_can_use_tool_none_without_callback(self):
        """can_use_tool is not set when no ask_user_callback is provided."""
        session = ClaudeSession()
        opts = session._build_options()
        assert opts.can_use_tool is None


class TestAutoApprovePlanMode:
    """Tests for the _auto_approve_plan_mode hook."""

    @pytest.mark.asyncio
    async def test_approves_enter_plan_mode(self):
        from chicane.claude import _auto_approve_plan_mode
        result = await _auto_approve_plan_mode(
            {"hook_event_name": "PreToolUse", "tool_name": "EnterPlanMode", "tool_input": {}},
            "tu_1", None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    @pytest.mark.asyncio
    async def test_approves_exit_plan_mode(self):
        from chicane.claude import _auto_approve_plan_mode
        result = await _auto_approve_plan_mode(
            {"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode", "tool_input": {}},
            "tu_2", None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    @pytest.mark.asyncio
    async def test_ignores_other_tools(self):
        from chicane.claude import _auto_approve_plan_mode
        result = await _auto_approve_plan_mode(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
            "tu_3", None,
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_dummy_hook_returns_continue(self):
        from chicane.claude import _dummy_hook
        result = await _dummy_hook(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
            "tu_4", None,
        )
        assert result == {"continue_": True}


class TestCanUseTool:
    """Tests for the canUseTool callback built by _build_options."""

    @pytest.mark.asyncio
    async def test_ask_user_question_routes_to_callback(self):
        """AskUserQuestion triggers the ask_user_callback."""
        captured_questions = []

        async def fake_callback(questions):
            captured_questions.extend(questions)
            return {"What color?": "Blue"}

        session = ClaudeSession(ask_user_callback=fake_callback)
        opts = session._build_options()

        questions = [{"question": "What color?", "options": [{"label": "Blue"}]}]
        result = await opts.can_use_tool(
            "AskUserQuestion",
            {"questions": questions},
            None,
        )
        assert captured_questions == questions
        assert result.updated_input["answers"] == {"What color?": "Blue"}

    @pytest.mark.asyncio
    async def test_ask_user_question_callback_error_returns_deny(self):
        """If the callback raises, canUseTool returns Deny."""
        async def failing_callback(questions):
            raise RuntimeError("Slack timeout")

        session = ClaudeSession(ask_user_callback=failing_callback)
        opts = session._build_options()

        from claude_agent_sdk.types import PermissionResultDeny
        result = await opts.can_use_tool(
            "AskUserQuestion",
            {"questions": []},
            None,
        )
        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_other_tools_auto_allowed(self):
        """Non-AskUserQuestion tools are auto-allowed."""
        async def fake_callback(questions):
            return {}

        session = ClaudeSession(ask_user_callback=fake_callback)
        opts = session._build_options()

        from claude_agent_sdk.types import PermissionResultAllow
        result = await opts.can_use_tool(
            "Bash",
            {"command": "ls"},
            None,
        )
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input == {"command": "ls"}


def _mock_sdk_client(messages):
    """Create a mock ClaudeSDKClient that yields the given messages.

    Provides ``receive_messages()`` (the streaming-input API used by
    ``ClaudeSession.stream()``) plus the legacy ``receive_response()``
    for any tests that still reference it.
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.interrupt = MagicMock()

    async def _receive_messages():
        for msg in messages:
            yield msg

    async def _receive_response():
        for msg in messages:
            yield msg

    client.receive_messages = _receive_messages
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

        client.receive_messages = _exploding_response

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=client):
            with pytest.raises(RuntimeError, match="boom"):
                async for _ in session.stream("hello"):
                    pass

        assert not session.is_streaming

    @pytest.mark.asyncio
    async def test_stream_pushes_prompt_into_queue(self):
        """stream() pushes the prompt into the message queue (not query())."""
        mock_client = _mock_sdk_client([])

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=mock_client):
            async for _ in session.stream("test prompt"):
                pass

        # The prompt should NOT go through query() — it's pushed into the
        # queue for the streaming input generator to deliver.
        mock_client.query.assert_not_awaited()
        # Queue should be empty after being consumed (or in this mock case,
        # the prompt was put but not consumed by a real generator)
        assert session._message_queue.qsize() == 1  # unconsumed in mock
        assert session._message_queue.get_nowait() == "test prompt"

    @pytest.mark.asyncio
    async def test_stream_skips_message_parse_error(self, caplog):
        """MessageParseError mid-stream is logged and skipped, not fatal."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        from claude_agent_sdk._errors import MessageParseError

        assistant_msg = AssistantMessage(
            content=[TextBlock(text="before error")],
            model="opus", parent_tool_use_id=None, error=None,
        )
        result_msg = ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=90,
            is_error=False, num_turns=1, session_id="s1",
            total_cost_usd=None, usage=None, result="ok",
        )

        # Async generator that raises MessageParseError between valid messages
        client = AsyncMock()
        client.query = AsyncMock()

        call_count = 0

        async def _response_with_parse_error():
            nonlocal call_count
            yield assistant_msg
            call_count += 1
            # Simulate a parse error from an unknown message type
            raise MessageParseError("Unknown message type: rate_limit_event", {})

        client.receive_messages = _response_with_parse_error

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=client):
            events = [e async for e in session.stream("hello")]

        # The valid message before the error should still be yielded
        assert len(events) == 1
        assert events[0].type == "assistant"
        assert events[0].text == "before error"
        assert any("MessageParseError" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_stream_continues_after_message_parse_error(self):
        """Stream continues yielding messages after a MessageParseError."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        from claude_agent_sdk._errors import MessageParseError

        msg1 = AssistantMessage(
            content=[TextBlock(text="before")], model="opus",
            parent_tool_use_id=None, error=None,
        )
        msg2 = ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=90,
            is_error=False, num_turns=1, session_id="s1",
            total_cost_usd=None, usage=None, result="after",
        )

        # msg1 → parse error → msg2 should all flow through
        client = AsyncMock()
        client.query = AsyncMock()

        call_count = 0

        async def _response_with_mid_stream_error():
            nonlocal call_count
            yield msg1
            call_count += 1
            raise MessageParseError("Unknown message type: rate_limit_event", {})

        client.receive_messages = _response_with_mid_stream_error

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=client):
            events = [e async for e in session.stream("hello")]

        # msg1 should be yielded; the error terminates the generator so msg2
        # is never reached — but critically it doesn't crash.
        assert len(events) == 1
        assert events[0].text == "before"
        assert not session.is_streaming


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


class TestStreamingInput:
    """Tests for the streaming input queue and between-turn message delivery."""

    @pytest.mark.asyncio
    async def test_message_generator_yields_formatted_messages(self):
        session = ClaudeSession()
        await session._message_queue.put("hello")
        await session._message_queue.put("world")
        await session._message_queue.put(None)  # sentinel

        gen = session._message_generator()
        messages = []
        async for msg in gen:
            messages.append(msg)

        assert len(messages) == 2
        assert messages[0] == {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
        }
        assert messages[1] == {
            "type": "user",
            "message": {"role": "user", "content": "world"},
        }

    @pytest.mark.asyncio
    async def test_message_generator_stops_on_sentinel(self):
        session = ClaudeSession()
        await session._message_queue.put(None)

        gen = session._message_generator()
        messages = [msg async for msg in gen]
        assert messages == []

    @pytest.mark.asyncio
    async def test_queue_message_puts_into_queue(self):
        session = ClaudeSession()
        await session.queue_message("follow-up")
        assert session._message_queue.qsize() == 1
        assert session._message_queue.get_nowait() == "follow-up"

    @pytest.mark.asyncio
    async def test_stream_continues_when_queue_has_pending_messages(self):
        """When queue_message() is called during streaming, stream() should
        continue past the first ResultMessage to process the next turn."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        assistant1 = AssistantMessage(
            content=[TextBlock(text="working on it")],
            model="opus", parent_tool_use_id=None, error=None,
        )
        result1 = ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=90,
            is_error=False, num_turns=1, session_id="s1",
            total_cost_usd=0.01, usage=None, result="turn 1 done",
        )
        assistant2 = AssistantMessage(
            content=[TextBlock(text="handling follow-up")],
            model="opus", parent_tool_use_id=None, error=None,
        )
        result2 = ResultMessage(
            subtype="success", duration_ms=200, duration_api_ms=180,
            is_error=False, num_turns=2, session_id="s1",
            total_cost_usd=0.02, usage=None, result="turn 2 done",
        )

        session = ClaudeSession()

        client = AsyncMock()
        client.query = AsyncMock()

        async def _two_turn_stream():
            yield assistant1
            # Simulate a follow-up arriving mid-stream
            await session.queue_message("follow-up")
            yield result1
            yield assistant2
            yield result2

        client.receive_messages = _two_turn_stream

        with patch.object(session, "_ensure_connected", return_value=client):
            events = [e async for e in session.stream("initial")]

        # Should see all 4 events — stream continued past first result
        assert len(events) == 4
        assert events[0].type == "assistant"
        assert events[1].type == "result"
        assert events[2].type == "assistant"
        assert events[3].type == "result"

    @pytest.mark.asyncio
    async def test_stream_stops_at_result_when_queue_empty(self):
        """Normal case: stream stops at ResultMessage when no queued messages."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        result = ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=90,
            is_error=False, num_turns=1, session_id="s1",
            total_cost_usd=0.01, usage=None, result="done",
        )
        # This message should never be reached
        extra = AssistantMessage(
            content=[TextBlock(text="should not appear")],
            model="opus", parent_tool_use_id=None, error=None,
        )

        client = AsyncMock()
        client.query = AsyncMock()

        async def _stream_with_extra():
            yield result
            yield extra

        client.receive_messages = _stream_with_extra

        session = ClaudeSession()
        with patch.object(session, "_ensure_connected", return_value=client):
            events = [e async for e in session.stream("hello")]

        # Should stop after the result — the extra message is not yielded
        assert len(events) == 1
        assert events[0].type == "result"

    @pytest.mark.asyncio
    async def test_disconnect_sends_sentinel(self):
        session = ClaudeSession()
        session._client = AsyncMock()
        session._connected = True
        session._generator_started = True

        await session.disconnect()

        # Sentinel should have been sent to stop the generator
        assert not session._generator_started
        # Queue should have the sentinel
        # (in real life the generator would consume it, but here it's unconsumed)

    @pytest.mark.asyncio
    async def test_disconnect_without_generator_skips_sentinel(self):
        session = ClaudeSession()
        session._client = AsyncMock()
        session._connected = True
        session._generator_started = False

        await session.disconnect()

        # Queue should be empty — no sentinel was sent
        assert session._message_queue.empty()

    @pytest.mark.asyncio
    async def test_ensure_connected_sets_generator_started(self):
        session = ClaudeSession(cwd=Path("/tmp"))

        with patch("chicane.claude.ClaudeSDKClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value = mock_instance

            await session._ensure_connected()

            assert session._generator_started


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

    @pytest.mark.asyncio
    async def test_retries_on_timeout_error(self):
        """TimeoutError on connect retries up to max_retries times."""
        session = ClaudeSession(cwd=Path("/tmp"))

        with patch("chicane.claude.ClaudeSDKClient") as MockClient:
            mock_instance = AsyncMock()
            # Fail once with TimeoutError, then succeed
            mock_instance.connect = AsyncMock(
                side_effect=[TimeoutError("deadline exceeded"), None]
            )
            MockClient.return_value = mock_instance

            client = await session._ensure_connected(max_retries=1, base_delay=0.01)

            assert client is mock_instance
            assert session._connected
            assert mock_instance.connect.await_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_sdk_timeout_exception(self):
        """Generic Exception with 'timeout' in message also triggers retry."""
        session = ClaudeSession(cwd=Path("/tmp"))

        with patch("chicane.claude.ClaudeSDKClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock(
                side_effect=[
                    Exception("Control request timeout: initialize"),
                    None,
                ]
            )
            MockClient.return_value = mock_instance

            client = await session._ensure_connected(max_retries=1, base_delay=0.01)

            assert client is mock_instance
            assert session._connected
            assert mock_instance.connect.await_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self):
        """After max_retries timeouts, the exception propagates."""
        session = ClaudeSession(cwd=Path("/tmp"))

        with patch("chicane.claude.ClaudeSDKClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock(
                side_effect=TimeoutError("deadline exceeded")
            )
            MockClient.return_value = mock_instance

            with pytest.raises(TimeoutError, match="deadline exceeded"):
                await session._ensure_connected(max_retries=2, base_delay=0.01)

            # 1 initial + 2 retries = 3 attempts
            assert mock_instance.connect.await_count == 3
            assert not session._connected

    @pytest.mark.asyncio
    async def test_non_timeout_exception_not_retried(self):
        """Non-timeout exceptions propagate immediately without retry."""
        session = ClaudeSession(cwd=Path("/tmp"))

        with patch("chicane.claude.ClaudeSDKClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock(
                side_effect=ValueError("bad config")
            )
            MockClient.return_value = mock_instance

            with pytest.raises(ValueError, match="bad config"):
                await session._ensure_connected(max_retries=2, base_delay=0.01)

            # Should NOT have retried
            assert mock_instance.connect.await_count == 1
