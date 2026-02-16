"""Tests for tool activity formatting, streaming, and git commit reactions."""

from unittest.mock import MagicMock, patch

import pytest

from chicane.claude import ClaudeEvent
from chicane.handlers import _format_edit_diff, _format_tool_activity, _has_file_edit, _process_message
from tests.conftest import make_event, make_tool_event, mock_client, mock_session_info, tool_block


class TestFormatToolActivity:
    """Test _format_tool_activity helper for each tool type."""

    # --- Read ---

    def test_read_tool(self):
        event = make_tool_event(
            tool_block("Read", file_path="/home/user/project/config.py")
        )
        assert _format_tool_activity(event) == [":mag: Reading `config.py`"]

    def test_read_tool_with_offset_and_limit(self):
        event = make_tool_event(
            tool_block("Read", file_path="/src/big.py", offset=50, limit=100)
        )
        assert _format_tool_activity(event) == [":mag: Reading `big.py` (lines 50\u2013150)"]

    def test_read_tool_with_offset_only(self):
        event = make_tool_event(
            tool_block("Read", file_path="/src/big.py", offset=200)
        )
        assert _format_tool_activity(event) == [":mag: Reading `big.py` (from line 200)"]

    def test_read_tool_with_limit_only(self):
        event = make_tool_event(
            tool_block("Read", file_path="/src/big.py", limit=50)
        )
        assert _format_tool_activity(event) == [":mag: Reading `big.py` (first 50 lines)"]

    def test_read_tool_with_pdf_pages(self):
        event = make_tool_event(
            tool_block("Read", file_path="/docs/report.pdf", pages="3-5")
        )
        assert _format_tool_activity(event) == [":mag: Reading `report.pdf` (pages 3-5)"]

    # --- Bash ---

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

    def test_bash_tool_with_description(self):
        event = make_tool_event(
            tool_block("Bash", command="npm install", description="Install dependencies")
        )
        assert _format_tool_activity(event) == [":computer: Install dependencies"]

    def test_bash_tool_background(self):
        event = make_tool_event(
            tool_block("Bash", command="pytest -x", run_in_background=True)
        )
        assert _format_tool_activity(event) == [":computer: Running `pytest -x` (background)"]

    def test_bash_tool_description_and_background(self):
        event = make_tool_event(
            tool_block(
                "Bash",
                command="pytest -x",
                description="Run tests",
                run_in_background=True,
            )
        )
        assert _format_tool_activity(event) == [":computer: Run tests (background)"]

    # --- Edit ---

    def test_edit_tool_with_diff(self):
        event = make_tool_event(
            tool_block(
                "Edit",
                file_path="/src/handlers.py",
                old_string="def foo():",
                new_string="def bar():",
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        item = result[0]
        assert isinstance(item, str)
        assert ":pencil2: Editing `handlers.py`" in item
        assert "-def foo():" in item
        assert "+def bar():" in item
        assert "```" in item  # inline code block

    def test_edit_tool_multiline_diff(self):
        event = make_tool_event(
            tool_block(
                "Edit",
                file_path="/src/app.py",
                old_string="x = 1\ny = 2",
                new_string="x = 1\ny = 3\nz = 4",
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        item = result[0]
        assert isinstance(item, str)
        assert "-y = 2" in item
        assert "+y = 3" in item
        assert "+z = 4" in item

    def test_edit_tool_no_strings_fallback(self):
        """Edit with no old/new strings falls back to simple message."""
        event = make_tool_event(
            tool_block("Edit", file_path="/src/handlers.py")
        )
        assert _format_tool_activity(event) == [":pencil2: Editing `handlers.py`"]

    def test_edit_tool_replace_all(self):
        event = make_tool_event(
            tool_block(
                "Edit",
                file_path="/src/config.py",
                old_string="old_name",
                new_string="new_name",
                replace_all=True,
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert "(all occurrences)" in result[0]
        assert ":pencil2: Editing `config.py` (all occurrences)" in result[0]

    # --- Write ---

    def test_write_tool(self):
        event = make_tool_event(
            tool_block("Write", file_path="/src/new_file.py", content="line1\nline2\nline3")
        )
        assert _format_tool_activity(event) == [":pencil2: Writing `new_file.py` (3 lines)"]

    def test_write_tool_single_line(self):
        event = make_tool_event(
            tool_block("Write", file_path="/src/single.txt", content="hello")
        )
        assert _format_tool_activity(event) == [":pencil2: Writing `single.txt` (1 line)"]

    def test_write_tool_no_content(self):
        event = make_tool_event(
            tool_block("Write", file_path="/src/empty.py")
        )
        assert _format_tool_activity(event) == [":pencil2: Writing `empty.py`"]

    # --- Grep ---

    def test_grep_tool(self):
        event = make_tool_event(
            tool_block("Grep", pattern="download_files")
        )
        assert _format_tool_activity(event) == [":mag: Searching for `download_files`"]

    def test_grep_tool_with_glob_filter(self):
        event = make_tool_event(
            tool_block("Grep", pattern="useState", glob="*.tsx")
        )
        assert _format_tool_activity(event) == [
            ":mag: Searching for `useState` in `*.tsx`"
        ]

    def test_grep_tool_with_type_filter(self):
        event = make_tool_event(
            tool_block("Grep", pattern="error", type="py")
        )
        assert _format_tool_activity(event) == [
            ":mag: Searching for `error` (py files)"
        ]

    def test_grep_tool_with_path(self):
        event = make_tool_event(
            tool_block("Grep", pattern="TODO", path="/home/user/project/src")
        )
        assert _format_tool_activity(event) == [
            ":mag: Searching for `TODO` in `src/`"
        ]

    def test_grep_tool_with_glob_and_path(self):
        event = make_tool_event(
            tool_block("Grep", pattern="import", glob="*.py", path="/project/lib")
        )
        assert _format_tool_activity(event) == [
            ":mag: Searching for `import` in `*.py` in `lib/`"
        ]

    # --- Glob ---

    def test_glob_tool(self):
        event = make_tool_event(
            tool_block("Glob", pattern="**/*.py")
        )
        assert _format_tool_activity(event) == [":mag: Finding files `**/*.py`"]

    def test_glob_tool_with_path(self):
        event = make_tool_event(
            tool_block("Glob", pattern="*.test.ts", path="/project/src")
        )
        assert _format_tool_activity(event) == [
            ":mag: Finding files `*.test.ts` in `src/`"
        ]

    # --- WebFetch ---

    def test_webfetch_tool_with_url(self):
        event = make_tool_event(
            tool_block("WebFetch", url="https://example.com/api")
        )
        assert _format_tool_activity(event) == [":globe_with_meridians: Fetching `https://example.com/api`"]

    def test_webfetch_tool_with_url_and_prompt(self):
        event = make_tool_event(
            tool_block(
                "WebFetch",
                url="https://docs.example.com",
                prompt="Find the authentication section",
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert ":globe_with_meridians: Fetching `https://docs.example.com`" in result[0]
        assert "Find the authentication section" in result[0]

    def test_webfetch_tool_long_prompt_truncated(self):
        long_prompt = "A" * 100
        event = make_tool_event(
            tool_block("WebFetch", url="https://example.com", prompt=long_prompt)
        )
        result = _format_tool_activity(event)
        assert "\u2026" in result[0]

    def test_webfetch_tool_no_url(self):
        event = make_tool_event(tool_block("WebFetch"))
        assert _format_tool_activity(event) == [":globe_with_meridians: Fetching URL"]

    # --- WebSearch ---

    def test_websearch_tool_with_query(self):
        event = make_tool_event(
            tool_block("WebSearch", query="python asyncio tutorial")
        )
        assert _format_tool_activity(event) == [":globe_with_meridians: Searching web for `python asyncio tutorial`"]

    def test_websearch_tool_with_allowed_domains(self):
        event = make_tool_event(
            tool_block(
                "WebSearch",
                query="react hooks",
                allowed_domains=["stackoverflow.com", "reactjs.org"],
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert "stackoverflow.com, reactjs.org" in result[0]

    def test_websearch_tool_with_blocked_domains(self):
        event = make_tool_event(
            tool_block(
                "WebSearch",
                query="python tutorial",
                blocked_domains=["w3schools.com"],
            )
        )
        result = _format_tool_activity(event)
        assert len(result) == 1
        assert "excluding w3schools.com" in result[0]

    def test_websearch_tool_no_query(self):
        event = make_tool_event(tool_block("WebSearch"))
        assert _format_tool_activity(event) == [":globe_with_meridians: Searching web"]

    # --- Task ---

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

    def test_task_tool_with_model(self):
        event = make_tool_event(
            tool_block("Task", subagent_type="Explore", description="find tests", model="haiku")
        )
        assert _format_tool_activity(event) == [
            ":robot_face: Spawning Explore: find tests (haiku)"
        ]

    def test_task_tool_no_details(self):
        event = make_tool_event(tool_block("Task"))
        assert _format_tool_activity(event) == [":robot_face: Spawning subagent"]

    # --- Skill ---

    def test_skill_tool_with_name(self):
        event = make_tool_event(
            tool_block("Skill", skill="commit")
        )
        assert _format_tool_activity(event) == [":zap: Running skill `commit`"]

    def test_skill_tool_with_args(self):
        event = make_tool_event(
            tool_block("Skill", skill="commit", args='-m "fix bug"')
        )
        assert _format_tool_activity(event) == [
            ':zap: Running skill `commit` \u2014 `-m "fix bug"`'
        ]

    def test_skill_tool_no_name(self):
        event = make_tool_event(tool_block("Skill"))
        assert _format_tool_activity(event) == [":zap: Running skill"]

    # --- NotebookEdit ---

    def test_notebook_edit_tool(self):
        event = make_tool_event(
            tool_block("NotebookEdit", notebook_path="/home/user/analysis.ipynb")
        )
        assert _format_tool_activity(event) == [":notebook: Editing notebook `analysis.ipynb`"]

    def test_notebook_edit_insert_mode(self):
        event = make_tool_event(
            tool_block(
                "NotebookEdit",
                notebook_path="/nb.ipynb",
                edit_mode="insert",
                cell_type="code",
                cell_number=5,
            )
        )
        assert _format_tool_activity(event) == [
            ":notebook: Inserting into notebook `nb.ipynb` \u2014 code cell #5"
        ]

    def test_notebook_edit_delete_mode(self):
        event = make_tool_event(
            tool_block(
                "NotebookEdit",
                notebook_path="/nb.ipynb",
                edit_mode="delete",
                cell_number=3,
            )
        )
        assert _format_tool_activity(event) == [
            ":notebook: Deleting from notebook `nb.ipynb` \u2014 cell #3"
        ]

    def test_notebook_edit_with_cell_type_only(self):
        event = make_tool_event(
            tool_block(
                "NotebookEdit",
                notebook_path="/nb.ipynb",
                cell_type="markdown",
            )
        )
        assert _format_tool_activity(event) == [
            ":notebook: Editing notebook `nb.ipynb` \u2014 markdown"
        ]

    # --- Plan mode ---

    def test_enter_plan_mode_tool(self):
        event = make_tool_event(tool_block("EnterPlanMode"))
        assert _format_tool_activity(event) == [":clipboard: Entering plan mode"]

    def test_exit_plan_mode_tool(self):
        event = make_tool_event(tool_block("ExitPlanMode"))
        assert _format_tool_activity(event) == [":clipboard: Exiting plan mode"]

    # --- ToolSearch ---

    def test_tool_search_with_query(self):
        event = make_tool_event(
            tool_block("ToolSearch", query="slack")
        )
        assert _format_tool_activity(event) == [":toolbox: Searching for tool `slack`"]

    def test_tool_search_no_query(self):
        event = make_tool_event(tool_block("ToolSearch"))
        assert _format_tool_activity(event) == [":toolbox: Searching for tools"]

    # --- TaskOutput ---

    def test_task_output(self):
        event = make_tool_event(
            tool_block("TaskOutput", task_id="abc123")
        )
        assert _format_tool_activity(event) == [
            ":hourglass_flowing_sand: Waiting for background task"
        ]

    # --- TaskStop ---

    def test_task_stop(self):
        event = make_tool_event(
            tool_block("TaskStop", task_id="abc123")
        )
        assert _format_tool_activity(event) == [
            ":octagonal_sign: Stopping background task"
        ]

    # --- ListMcpResourcesTool ---

    def test_list_mcp_resources_with_server(self):
        event = make_tool_event(
            tool_block("ListMcpResourcesTool", server="magaldi")
        )
        assert _format_tool_activity(event) == [
            ":card_index: Listing MCP resources (magaldi)"
        ]

    def test_list_mcp_resources_no_server(self):
        event = make_tool_event(tool_block("ListMcpResourcesTool"))
        assert _format_tool_activity(event) == [":card_index: Listing MCP resources"]

    # --- ReadMcpResourceTool ---

    def test_read_mcp_resource_with_uri(self):
        event = make_tool_event(
            tool_block("ReadMcpResourceTool", server="magaldi", uri="repo://chicane")
        )
        assert _format_tool_activity(event) == [
            ":card_index: Reading MCP resource `repo://chicane`"
        ]

    def test_read_mcp_resource_no_uri(self):
        event = make_tool_event(tool_block("ReadMcpResourceTool"))
        assert _format_tool_activity(event) == [":card_index: Reading MCP resource"]

    # --- AskUserQuestion ---

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


class TestFormatEditDiff:
    """Test _format_edit_diff returns a compact diff string."""

    def test_simple_replacement(self):
        result = _format_edit_diff("def foo():", "def bar():")
        assert isinstance(result, str)
        # Headers are stripped — only diff body
        assert "---" not in result
        assert "+++" not in result
        assert "@@" not in result
        assert "-def foo():" in result
        assert "+def bar():" in result

    def test_addition_only(self):
        result = _format_edit_diff("import os", "import os\nimport sys")
        assert "+import sys" in result

    def test_context_lines_shown(self):
        result = _format_edit_diff("a\nb\nc", "a\nX\nc")
        assert "-b" in result
        assert "+X" in result
        # Context lines present
        assert " a" in result
        assert " c" in result

    def test_truncation(self):
        old = "\n".join(f"line {i}" for i in range(50))
        new = "\n".join(f"mod {i}" for i in range(50))
        result = _format_edit_diff(old, new, max_lines=10)
        assert "more lines" in result

    def test_empty_strings_returns_empty(self):
        """Identical strings → empty string (nothing to show)."""
        result = _format_edit_diff("", "")
        assert result == ""


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

        # Long text should be uploaded as a snippet via files_upload_v2
        client.files_upload_v2.assert_called_once()
        upload_kwargs = client.files_upload_v2.call_args.kwargs
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


class TestHasFileEdit:
    """Test _has_file_edit helper for detecting file-modifying tools."""

    def test_edit_tool_detected(self):
        event = make_tool_event(tool_block("Edit", file_path="/src/a.py"))
        assert _has_file_edit(event) is True

    def test_write_tool_detected(self):
        event = make_tool_event(tool_block("Write", file_path="/src/b.py"))
        assert _has_file_edit(event) is True

    def test_notebook_edit_detected(self):
        event = make_tool_event(
            tool_block("NotebookEdit", notebook_path="/nb.ipynb")
        )
        assert _has_file_edit(event) is True

    def test_read_not_detected(self):
        event = make_tool_event(tool_block("Read", file_path="/src/a.py"))
        assert _has_file_edit(event) is False

    def test_bash_not_detected(self):
        event = make_tool_event(tool_block("Bash", command="echo hello"))
        assert _has_file_edit(event) is False

    def test_mixed_blocks_detected(self):
        """If any block is a file edit, return True."""
        event = make_tool_event(
            tool_block("Read", file_path="/src/a.py"),
            tool_block("Edit", file_path="/src/a.py"),
        )
        assert _has_file_edit(event) is True

    def test_empty_content(self):
        event = ClaudeEvent(
            type="assistant",
            raw={"type": "assistant", "message": {"content": []}},
        )
        assert _has_file_edit(event) is False


class TestFileChangedReaction:
    """Test that a :pencil2: reaction is added on file edits and removed on commit."""

    @pytest.mark.asyncio
    async def test_file_edit_adds_pencil_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Edit", file_path="/src/app.py")
            )
            yield make_event("assistant", text="Updated.")
            yield make_event("result", text="Updated.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "fix it", client, config, sessions)

        pencil_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_calls) == 1
        assert pencil_calls[0].kwargs["timestamp"] == "1000.0"  # thread_ts

    @pytest.mark.asyncio
    async def test_write_tool_adds_pencil_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Write", file_path="/src/new.py")
            )
            yield make_event("assistant", text="Created.")
            yield make_event("result", text="Created.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "create file", client, config, sessions)

        pencil_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_calls) == 1

    @pytest.mark.asyncio
    async def test_no_file_edit_no_pencil_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Read", file_path="/src/app.py")
            )
            yield make_event("assistant", text="Here it is.")
            yield make_event("result", text="Here it is.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "show file", client, config, sessions)

        pencil_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_calls) == 0

    @pytest.mark.asyncio
    async def test_commit_removes_pencil_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Edit", file_path="/src/app.py")
            )
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "fix bug"')
            )
            yield make_event("assistant", text="Committed.")
            yield make_event("result", text="Committed.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "fix and commit", client, config, sessions)

        # Pencil was added then removed
        pencil_add_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_add_calls) == 1

        pencil_remove_calls = [
            c for c in client.reactions_remove.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_remove_calls) == 1
        assert pencil_remove_calls[0].kwargs["timestamp"] == "1000.0"

    @pytest.mark.asyncio
    async def test_multiple_edits_only_one_pencil_reaction(self, config, sessions):
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Edit", file_path="/src/a.py")
            )
            yield make_tool_event(
                tool_block("Write", file_path="/src/b.py")
            )
            yield make_tool_event(
                tool_block("Edit", file_path="/src/c.py")
            )
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "edit all", client, config, sessions)

        pencil_calls = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_calls) == 1

    @pytest.mark.asyncio
    async def test_commit_without_edits_no_pencil_removal(self, config, sessions):
        """If only a commit happens (no tracked edits), don't try to remove pencil."""
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "amend"')
            )
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "commit it", client, config, sessions)

        pencil_remove_calls = [
            c for c in client.reactions_remove.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_remove_calls) == 0

    @pytest.mark.asyncio
    async def test_edit_after_commit_removes_package_adds_pencil(self, config, sessions):
        """Edit → commit → edit again: package removed, pencil re-added."""
        async def fake_stream(prompt):
            yield make_tool_event(
                tool_block("Edit", file_path="/src/a.py")
            )
            yield make_tool_event(
                tool_block("Bash", command='git commit -m "first"')
            )
            yield make_tool_event(
                tool_block("Edit", file_path="/src/b.py")
            )
            yield make_event("assistant", text="Done.")
            yield make_event("result", text="Done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "edit commit edit", client, config, sessions)

        # Pencil added twice (once per edit cycle)
        pencil_add = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_add) == 2

        # Pencil removed once (by the commit)
        pencil_remove = [
            c for c in client.reactions_remove.call_args_list
            if c.kwargs.get("name") == "pencil2"
        ]
        assert len(pencil_remove) == 1

        # Package added once (by the commit)
        package_add = [
            c for c in client.reactions_add.call_args_list
            if c.kwargs.get("name") == "package"
        ]
        assert len(package_add) == 1

        # Package removed once (by the second edit)
        package_remove = [
            c for c in client.reactions_remove.call_args_list
            if c.kwargs.get("name") == "package"
        ]
        assert len(package_remove) == 1
        assert package_remove[0].kwargs["timestamp"] == "1000.0"

    @pytest.mark.asyncio
    async def test_full_cycle_edit_commit_edit_commit(self, config, sessions):
        """Edit → commit → edit → commit: both cycles complete cleanly."""
        async def fake_stream(prompt):
            yield make_tool_event(tool_block("Edit", file_path="/src/a.py"))
            yield make_tool_event(tool_block("Bash", command='git commit -m "1"'))
            yield make_tool_event(tool_block("Write", file_path="/src/b.py"))
            yield make_tool_event(tool_block("Bash", command='git commit -m "2"'))
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "cycle", client, config, sessions)

        # Pencil added twice, removed twice
        pencil_add = [c for c in client.reactions_add.call_args_list if c.kwargs.get("name") == "pencil2"]
        pencil_remove = [c for c in client.reactions_remove.call_args_list if c.kwargs.get("name") == "pencil2"]
        assert len(pencil_add) == 2
        assert len(pencil_remove) == 2

        # Package added twice (each commit), removed once (second edit)
        package_add = [c for c in client.reactions_add.call_args_list if c.kwargs.get("name") == "package"]
        package_remove = [c for c in client.reactions_remove.call_args_list if c.kwargs.get("name") == "package"]
        assert len(package_add) == 2
        assert len(package_remove) == 1
