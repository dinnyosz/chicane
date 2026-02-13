"""Tests for tool activity formatting, streaming, and git commit reactions."""

from unittest.mock import MagicMock, patch

import pytest

from chicane.claude import ClaudeEvent
from chicane.handlers import _format_tool_activity, _process_message
from tests.conftest import make_event, make_tool_event, mock_client, mock_session_info, tool_block


class TestFormatToolActivity:
    """Test _format_tool_activity helper for each tool type."""

    def test_read_tool(self):
        event = make_tool_event(
            tool_block("Read", file_path="/home/user/project/config.py")
        )
        assert _format_tool_activity(event) == [":mag: Reading `config.py`"]

    def test_bash_tool(self):
        event = make_tool_event(
            tool_block("Bash", command="pytest tests/")
        )
        assert _format_tool_activity(event) == [":computer: Running `pytest tests/`"]

    def test_bash_tool_long_command_not_truncated(self):
        long_cmd = "git diff chicane/handlers.py tests/test_handlers_process_message.py tests/test_handlers_tool_activity.py"
        event = make_tool_event(
            tool_block("Bash", command=long_cmd)
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert result[0] == f":computer: Running `{long_cmd}`"

    def test_edit_tool(self):
        event = make_tool_event(
            tool_block("Edit", file_path="/src/handlers.py")
        )
        assert _format_tool_activity(event) == [":pencil2: Editing `handlers.py`"]

    def test_write_tool(self):
        event = make_tool_event(
            tool_block("Write", file_path="/src/new_file.py")
        )
        assert _format_tool_activity(event) == [":pencil2: Writing `new_file.py`"]

    def test_grep_tool(self):
        event = make_tool_event(
            tool_block("Grep", pattern="download_files")
        )
        assert _format_tool_activity(event) == [":mag: Searching for `download_files`"]

    def test_glob_tool(self):
        event = make_tool_event(
            tool_block("Glob", pattern="**/*.py")
        )
        assert _format_tool_activity(event) == [":mag: Finding files `**/*.py`"]

    def test_webfetch_tool_with_url(self):
        event = make_tool_event(
            tool_block("WebFetch", url="https://example.com/api")
        )
        assert _format_tool_activity(event) == [":globe_with_meridians: Fetching `https://example.com/api`"]

    def test_webfetch_tool_no_url(self):
        event = make_tool_event(tool_block("WebFetch"))
        assert _format_tool_activity(event) == [":globe_with_meridians: Fetching URL"]

    def test_websearch_tool_with_query(self):
        event = make_tool_event(
            tool_block("WebSearch", query="python asyncio tutorial")
        )
        assert _format_tool_activity(event) == [":globe_with_meridians: Searching web for `python asyncio tutorial`"]

    def test_websearch_tool_no_query(self):
        event = make_tool_event(tool_block("WebSearch"))
        assert _format_tool_activity(event) == [":globe_with_meridians: Searching web"]

    def test_task_tool_with_details(self):
        event = make_tool_event(
            tool_block("Task", subagent_type="Explore", description="find auth code")
        )
        assert _format_tool_activity(event) == [":robot_face: Spawning Explore: find auth code"]

    def test_task_tool_subagent_type_only(self):
        event = make_tool_event(
            tool_block("Task", subagent_type="Bash")
        )
        assert _format_tool_activity(event) == [":robot_face: Spawning Bash"]

    def test_task_tool_no_details(self):
        event = make_tool_event(tool_block("Task"))
        assert _format_tool_activity(event) == [":robot_face: Spawning subagent"]

    def test_skill_tool_with_name(self):
        event = make_tool_event(
            tool_block("Skill", skill="commit")
        )
        assert _format_tool_activity(event) == [":zap: Running skill `commit`"]

    def test_skill_tool_no_name(self):
        event = make_tool_event(tool_block("Skill"))
        assert _format_tool_activity(event) == [":zap: Running skill"]

    def test_notebook_edit_tool(self):
        event = make_tool_event(
            tool_block("NotebookEdit", notebook_path="/home/user/analysis.ipynb")
        )
        assert _format_tool_activity(event) == [":notebook: Editing notebook `analysis.ipynb`"]

    def test_enter_plan_mode_tool(self):
        event = make_tool_event(tool_block("EnterPlanMode"))
        assert _format_tool_activity(event) == [":clipboard: Entering plan mode"]

    def test_ask_user_question_tool(self):
        event = make_tool_event(tool_block("AskUserQuestion"))
        assert _format_tool_activity(event) == [":question: Asking user a question"]

    def test_ask_user_question_with_content(self):
        event = make_tool_event(
            tool_block(
                "AskUserQuestion",
                questions=[
                    {
                        "question": "Which database should we use?",
                        "header": "Database",
                        "options": [
                            {"label": "PostgreSQL", "description": "Relational, battle-tested"},
                            {"label": "SQLite", "description": "Embedded, zero config"},
                        ],
                        "multiSelect": False,
                    }
                ],
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        text = result[0]
        assert ":question: *Claude is asking:*" in text
        assert "Which database should we use?" in text
        assert "*PostgreSQL*" in text
        assert "Relational, battle-tested" in text
        assert "*SQLite*" in text

    def test_ask_user_question_label_only_option(self):
        event = make_tool_event(
            tool_block(
                "AskUserQuestion",
                questions=[
                    {
                        "question": "Continue?",
                        "header": "Confirm",
                        "options": [
                            {"label": "Yes"},
                            {"label": "No"},
                        ],
                        "multiSelect": False,
                    }
                ],
            )
        )
        result = _format_tool_activity(event)
        text = result[0]
        assert "*Yes*" in text
        assert "*No*" in text

    def test_todo_write_with_tasks(self):
        event = make_tool_event(
            tool_block(
                "TodoWrite",
                todos=[
                    {"content": "Set up database", "status": "completed", "activeForm": "Setting up database"},
                    {"content": "Write API endpoints", "status": "in_progress", "activeForm": "Writing API endpoints"},
                    {"content": "Add tests", "status": "pending", "activeForm": "Adding tests"},
                ],
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert ":clipboard: *Tasks*" in result[0]
        assert ":white_check_mark: Set up database" in result[0]
        assert ":arrows_counterclockwise: Write API endpoints" in result[0]
        assert ":white_circle: Add tests" in result[0]

    def test_todo_write_empty_todos(self):
        event = make_tool_event(tool_block("TodoWrite", todos=[]))
        assert _format_tool_activity(event) == [":clipboard: Updating tasks"]

    def test_todo_write_no_todos_key(self):
        event = make_tool_event(tool_block("TodoWrite"))
        assert _format_tool_activity(event) == [":clipboard: Updating tasks"]

    def test_unknown_tool_fallback(self):
        event = make_tool_event(tool_block("CustomTool"))
        assert _format_tool_activity(event) == [":wrench: Custom Tool"]

    def test_unknown_tool_mcp_prefix_stripped_with_server(self):
        event = make_tool_event(tool_block("mcp__magaldi__search_code"))
        assert _format_tool_activity(event) == [":wrench: magaldi: Search Code"]

    def test_unknown_tool_mcp_deep_prefix(self):
        """MCP name with 4+ parts still extracts the last segment, shows server."""
        event = make_tool_event(tool_block("mcp__server__ns__find_files"))
        assert _format_tool_activity(event) == [":wrench: server: Find Files"]

    def test_unknown_tool_underscores_to_spaces(self):
        event = make_tool_event(tool_block("my_custom_tool"))
        assert _format_tool_activity(event) == [":wrench: My Custom Tool"]

    def test_multiple_tool_blocks(self):
        event = make_tool_event(
            tool_block("Read", file_path="/src/a.py"),
            tool_block("Read", file_path="/src/b.py"),
        )
        result = _format_tool_activity(event)
        assert len(result) == 2
        assert result[0] == ":mag: Reading `a.py`"
        assert result[1] == ":mag: Reading `b.py`"

    def test_mixed_text_and_tool_blocks(self):
        """Only tool_use blocks should produce activities; text blocks are ignored."""
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check that."},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
                    ]
                },
            },
        )
        result = _format_tool_activity(event)
        assert result == [":mag: Reading `x.py`"]

    def test_no_tool_blocks_returns_empty(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
        )
        assert _format_tool_activity(event) == []

    def test_empty_content_returns_empty(self):
        event = ClaudeEvent(
            type="assistant",
            raw={"type": "assistant", "message": {"content": []}},
        )
        assert _format_tool_activity(event) == []


class TestCatchAllToolDisplay:
    """Test that the catch-all else branch shows args."""

    def test_mcp_tool_with_args(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__magaldi__pattern_search",
                            "input": {"pattern": "def main", "mode": "regexp"},
                        }
                    ]
                },
            },
        )
        activities = _format_tool_activity(event)
        assert len(activities) == 1
        assert "magaldi: Pattern Search" in activities[0]
        assert "  pattern: `def main`" in activities[0]
        assert "  mode: `regexp`" in activities[0]
        assert "\n" in activities[0]

    def test_unknown_tool_no_args(self):
        event = ClaudeEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "SomeNewTool",
                            "input": {},
                        }
                    ]
                },
            },
        )
        activities = _format_tool_activity(event)
        assert activities == [":wrench: Some New Tool"]


class TestToolActivityStreaming:
    """Test that tool activities are posted correctly during streaming."""

    @pytest.mark.asyncio
    async def test_first_tool_activity_updates_placeholder(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Read", file_path="/src/config.py")
            )
            yield make_event("assistant", text="Here's the file content.")
            yield make_event("result", text="Here's the file content.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "show config", client, config, sessions)

        first_update = client.chat_update.call_args_list[0]
        assert first_update.kwargs["text"] == ":mag: Reading `config.py`"
        assert first_update.kwargs["ts"] == "9999.0"

    @pytest.mark.asyncio
    async def test_subsequent_activities_posted_as_replies(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Read", file_path="/src/a.py")
            )
            yield make_tool_event(
                tool_block("Edit", file_path="/src/a.py")
            )
            yield make_tool_event(
                tool_block("Bash", command="pytest")
            )
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "fix tests", client, config, sessions)

        assert client.chat_update.call_args_list[0].kwargs["text"] == ":mag: Reading `a.py`"

        post_calls = client.chat_postMessage.call_args_list
        assert post_calls[1].kwargs["text"] == ":pencil2: Editing `a.py`"
        assert post_calls[2].kwargs["text"] == ":computer: Running `pytest`"
        assert post_calls[3].kwargs["text"] == "Done."

    @pytest.mark.asyncio
    async def test_final_text_as_thread_replies_when_activities_exist(
        self, config, sessions
    ):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Read", file_path="/src/x.py")
            )
            yield make_event("assistant", text="The answer.")
            yield make_event("result", text="The answer.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        post_calls = client.chat_postMessage.call_args_list
        final_post = post_calls[-1]
        assert final_post.kwargs["text"] == "The answer."
        assert final_post.kwargs["thread_ts"] == "1000.0"

    @pytest.mark.asyncio
    async def test_no_activities_updates_placeholder_with_text(self, config, sessions):
        """When there are no tool calls, the response replaces the placeholder."""

        async def fake_stream(prompt):
            yield make_event("assistant", text="Quick answer.")
            yield make_event("result", text="Quick answer.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        final_update = client.chat_update.call_args_list[-1]
        assert final_update.kwargs["text"] == "Quick answer."

    @pytest.mark.asyncio
    async def test_text_flushed_before_next_tool_activity(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Read", file_path="/src/a.py")
            )
            yield make_event("assistant", text="Looks good, let me edit it.")
            yield make_tool_event(
                tool_block("Edit", file_path="/src/a.py")
            )
            yield make_event("assistant", text="Done editing.")
            yield make_event("result", text="Done editing.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "fix it", client, config, sessions)

        assert client.chat_update.call_args_list[0].kwargs["text"] == ":mag: Reading `a.py`"

        post_calls = client.chat_postMessage.call_args_list
        assert post_calls[1].kwargs["text"] == "Looks good, let me edit it."
        assert post_calls[2].kwargs["text"] == ":pencil2: Editing `a.py`"
        assert post_calls[3].kwargs["text"] == "Done editing."

    @pytest.mark.asyncio
    async def test_long_text_with_activities_uploaded_as_snippet(
        self, config, sessions
    ):
        long_text = "a" * 8000

        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Read", file_path="/src/big.py")
            )
            yield make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions)

        # Long text should be uploaded as a snippet, not split into chunks
        client.files_upload_v2.assert_called_once()
        upload_kwargs = client.files_upload_v2.call_args.kwargs
        assert upload_kwargs["content"] == long_text
        assert upload_kwargs["channel"] == "C_CHAN"


class TestToolErrorHandling:
    """Test that tool errors from user events are posted to Slack."""

    @pytest.mark.asyncio
    async def test_tool_error_posted_as_warning(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event(
                "user",
                message={
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "Command failed: exit code 1",
                        }
                    ]
                },
            )
            yield make_event("assistant", text="Got an error.")
            yield make_event("result", text="Got an error.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "10000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "run tests", client, config, sessions)

        warning_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "")
        ]
        assert len(warning_calls) == 1
        assert "Command failed: exit code 1" in warning_calls[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_long_tool_error_truncated(self, config, sessions):
        long_error = "x" * 500

        async def fake_stream(prompt):
            yield make_event(
                "user",
                message={
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": long_error,
                        }
                    ]
                },
            )
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "10001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do it", client, config, sessions)

        warning_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "")
        ]
        assert len(warning_calls) == 1
        assert warning_calls[0].kwargs["text"].endswith("...")

    @pytest.mark.asyncio
    async def test_non_error_tool_result_ignored(self, config, sessions):
        async def fake_stream(prompt):
            yield make_event(
                "user",
                message={
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": False,
                            "content": "success output",
                        }
                    ]
                },
            )
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "10002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "do it", client, config, sessions)

        warning_calls = [
            c for c in client.chat_postMessage.call_args_list
            if ":warning:" in c.kwargs.get("text", "")
        ]
        assert len(warning_calls) == 0


class TestGitCommitReaction:
    """Test that a :package: emoji is added when a git commit happens."""

    @pytest.mark.asyncio
    async def test_git_commit_adds_package_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "feat: add thing"')
            )
            yield make_event("assistant", text="Committed.")
            yield make_event("result", text="Committed.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "commit it", client, config, sessions)

        reaction_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "package"
        ]
        assert len(reaction_calls) == 1
        assert reaction_calls[0].kwargs["timestamp"] == "1000.0"

    @pytest.mark.asyncio
    async def test_no_git_commit_no_package_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Bash", command="pytest tests/")
            )
            yield make_event("assistant", text="Tests pass.")
            yield make_event("result", text="Tests pass.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "run tests", client, config, sessions)

        reaction_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "package"
        ]
        assert len(reaction_calls) == 0

    @pytest.mark.asyncio
    async def test_multiple_commits_only_one_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "first"')
            )
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "second"')
            )
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "commit both", client, config, sessions)

        reaction_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "package"
        ]
        assert len(reaction_calls) == 1
