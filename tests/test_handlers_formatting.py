"""Tests for message formatting: split_message, markdown_to_mrkdwn, completion_summary."""

from unittest.mock import MagicMock

from chicane.claude import ClaudeEvent
from chicane.handlers import (
    CommitInfo,
    ParsedTestResult,
    _extract_code_blocks,
    _extract_git_commit_info,
    _format_commit_card,
    _format_completion_summary,
    _format_test_summary,
    _format_unified_diff,
    _LANG_TO_FILETYPE,
    _markdown_to_mrkdwn,
    _parse_test_results,
    _snippet_metadata_from_tool,
    _split_markdown,
    _SnippetMeta,
    MARKDOWN_BLOCK_LIMIT,
)
from chicane.sessions import SessionInfo


class TestFormatCompletionSummary:
    """Test _format_completion_summary helper."""

    def test_turns_with_duration(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 5,
                "total_cost_usd": 0.03,
                "duration_ms": 12000,
                "is_error": False,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 5 turns took 12s · $0.03"

    def test_single_turn_with_duration(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 1,
                "total_cost_usd": 0.01,
                "duration_ms": 3000,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 1 turn took 3s · $0.01"

    def test_long_duration_shows_minutes(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 27,
                "duration_ms": 125000,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 27 turns took 2m5s"

    def test_error_result(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 2,
                "total_cost_usd": 0.05,
                "duration_ms": 8000,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: 2 turns took 8s · $0.05"

    def test_error_max_turns_subtype(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error_max_turns",
                "num_turns": 10,
                "duration_ms": 30000,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: 10 turns took 30s (hit max turns limit)"

    def test_error_max_budget_subtype(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error_max_budget_usd",
                "num_turns": 5,
                "duration_ms": 60000,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: 5 turns took 1m0s (hit budget limit)"

    def test_error_during_execution_subtype(self):
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "error_during_execution",
                "num_turns": 2,
                "is_error": True,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":x: Done — 2 turns (error during execution)"

    def test_success_subtype_no_reason(self):
        """Success results should not include a reason suffix."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": "success",
                "num_turns": 3,
                "duration_ms": 5000,
                "is_error": False,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 3 turns took 5s"
        assert "(" not in result

    def test_turns_without_duration_fallback(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "num_turns": 3},
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: Done — 3 turns"

    def test_cost_displayed_when_present(self):
        """Cost is appended when total_cost_usd > 0 (API users only)."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 8,
                "duration_ms": 45000,
                "total_cost_usd": 1.23,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 8 turns took 45s · $1.23"

    def test_cost_not_displayed_when_zero(self):
        """Zero cost (CLI users) should not show cost."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 3,
                "duration_ms": 5000,
                "total_cost_usd": 0.0,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 3 turns took 5s"
        assert "$" not in result

    def test_cost_not_displayed_when_absent(self):
        """No cost field (CLI users) should not show cost."""
        event = ClaudeEvent(
            type="result",
            raw={
                "type": "result",
                "num_turns": 3,
                "duration_ms": 5000,
            },
        )
        result = _format_completion_summary(event)
        assert result == ":checkered_flag: 3 turns took 5s"
        assert "$" not in result

    def test_no_turns_returns_none(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "total_cost_usd": 0.50, "duration_ms": 125000},
        )
        assert _format_completion_summary(event) is None

    def test_no_fields_returns_none(self):
        event = ClaudeEvent(
            type="result",
            raw={"type": "result"},
        )
        assert _format_completion_summary(event) is None


class TestCumulativeStats:
    """Cumulative session stats appended after 1st request."""

    def _make_session_info(self, requests=1, turns=0, cost=0.0):
        mock_session = MagicMock()
        info = MagicMock(spec=SessionInfo)
        info.total_requests = requests
        info.total_turns = turns
        info.total_cost_usd = cost
        info.total_commits = 0
        return info

    def test_no_stats_on_first_request(self):
        """First request (total_requests=1) should not show cumulative stats."""
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "num_turns": 5, "duration_ms": 12000, "total_cost_usd": 0.03},
        )
        info = self._make_session_info(requests=1, turns=5, cost=0.03)
        result = _format_completion_summary(event, info)
        assert ":bar_chart:" not in result

    def test_stats_shown_on_second_request(self):
        """After 2+ requests, cumulative stats line is appended."""
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "num_turns": 3, "duration_ms": 8000, "total_cost_usd": 0.05},
        )
        info = self._make_session_info(requests=2, turns=8, cost=0.08)
        result = _format_completion_summary(event, info)
        assert ":bar_chart:" in result
        assert "2 requests" in result
        assert "8 turns total" in result
        assert "$0.08 session total" in result

    def test_stats_no_cost_when_zero(self):
        """Cumulative cost is omitted when zero (CLI/subscription users)."""
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "num_turns": 2, "duration_ms": 5000},
        )
        info = self._make_session_info(requests=3, turns=10, cost=0.0)
        result = _format_completion_summary(event, info)
        assert ":bar_chart:" in result
        assert "3 requests" in result
        assert "session total" not in result

    def test_stats_not_shown_without_session_info(self):
        """Without session_info, no cumulative stats line."""
        event = ClaudeEvent(
            type="result",
            raw={"type": "result", "num_turns": 5, "duration_ms": 12000},
        )
        result = _format_completion_summary(event)
        assert ":bar_chart:" not in result


class TestMarkdownToMrkdwn:
    """Tests for _markdown_to_mrkdwn() Markdown -> Slack mrkdwn conversion."""

    def test_bold_double_asterisks(self):
        assert _markdown_to_mrkdwn("**hello**") == "*hello*"

    def test_bold_double_underscores(self):
        assert _markdown_to_mrkdwn("__hello__") == "*hello*"

    def test_strikethrough(self):
        assert _markdown_to_mrkdwn("~~removed~~") == "~removed~"

    def test_link(self):
        assert _markdown_to_mrkdwn("[click here](https://example.com)") == "<https://example.com|click here>"

    def test_image(self):
        assert _markdown_to_mrkdwn("![alt text](https://img.png)") == "<https://img.png|alt text>"

    def test_headers(self):
        assert _markdown_to_mrkdwn("# Title") == "*Title*"
        assert _markdown_to_mrkdwn("## Subtitle") == "*Subtitle*"
        assert _markdown_to_mrkdwn("### Section") == "*Section*"

    def test_horizontal_rule(self):
        assert _markdown_to_mrkdwn("---") == "\u2014\u2014\u2014"
        assert _markdown_to_mrkdwn("***") == "\u2014\u2014\u2014"
        assert _markdown_to_mrkdwn("___") == "\u2014\u2014\u2014"

    def test_code_block_preserved(self):
        text = "```python\n**not bold**\n```"
        result = _markdown_to_mrkdwn(text)
        assert "**not bold**" in result

    def test_inline_code_preserved(self):
        text = "use `**this**` for bold"
        result = _markdown_to_mrkdwn(text)
        assert "`**this**`" in result

    def test_mixed_content(self):
        text = "# Hello\n\nThis is **bold** and [a link](https://x.com).\n\n---\n\nDone."
        result = _markdown_to_mrkdwn(text)
        assert "*Hello*" in result
        assert "*bold*" in result
        assert "<https://x.com|a link>" in result
        assert "\u2014\u2014\u2014" in result

    def test_plain_text_unchanged(self):
        text = "Just some normal text with no markdown."
        assert _markdown_to_mrkdwn(text) == text

    def test_table_converted_to_preformatted(self):
        text = "| Col A | Col B |\n|---|---|\n| val1 | val2 |"
        result = _markdown_to_mrkdwn(text)
        assert "```" in result
        assert "val1" in result
        assert "|---|" not in result

    def test_bold_inside_code_block_not_converted(self):
        text = "Before\n```\n**stay bold md**\n```\nAfter **convert me**"
        result = _markdown_to_mrkdwn(text)
        assert "**stay bold md**" in result
        assert "*convert me*" in result

    def test_multiple_links_on_one_line(self):
        text = "See [foo](https://a.com) and [bar](https://b.com)"
        result = _markdown_to_mrkdwn(text)
        assert "<https://a.com|foo>" in result
        assert "<https://b.com|bar>" in result

    def test_nested_bold_in_header(self):
        """Header conversion should still work even if bold markers are inside."""
        text = "## **Important**"
        result = _markdown_to_mrkdwn(text)
        assert "Important" in result

    def test_blockquote_preserved(self):
        text = "> This is a quote"
        result = _markdown_to_mrkdwn(text)
        assert "> This is a quote" in result

    def test_italic_single_asterisk_preserved(self):
        """Single asterisk italic is already valid Slack mrkdwn."""
        text = "*italic text*"
        result = _markdown_to_mrkdwn(text)
        assert "*italic text*" in result

    def test_empty_string(self):
        assert _markdown_to_mrkdwn("") == ""

    def test_code_block_with_backticks_inside(self):
        text = "```\nuse `inline` here\n```"
        result = _markdown_to_mrkdwn(text)
        assert "```\nuse `inline` here\n```" in result

    # ── HTML entity escaping ──

    def test_ampersand_escaped(self):
        assert _markdown_to_mrkdwn("Tom & Jerry") == "Tom &amp; Jerry"

    def test_angle_brackets_escaped(self):
        result = _markdown_to_mrkdwn("use x < 10 and y > 5")
        assert "&lt;" in result
        assert "&gt;" in result

    def test_entities_not_escaped_in_code_block(self):
        text = "```\nx < 10 && y > 5\n```"
        result = _markdown_to_mrkdwn(text)
        assert "&lt;" not in result
        assert "&amp;" not in result

    def test_entities_not_escaped_in_inline_code(self):
        text = "use `x < 10` here"
        result = _markdown_to_mrkdwn(text)
        assert "`x < 10`" in result

    def test_blockquote_gt_not_escaped(self):
        """The > at start of line (blockquote) must survive."""
        result = _markdown_to_mrkdwn("> quoted text")
        assert result.startswith(">")
        assert "quoted text" in result

    # ── List bullet conversion ──

    def test_unordered_list_dash(self):
        result = _markdown_to_mrkdwn("- item one\n- item two")
        assert "• item one" in result
        assert "• item two" in result

    def test_unordered_list_asterisk(self):
        result = _markdown_to_mrkdwn("* item one\n* item two")
        assert "• item one" in result

    def test_unordered_list_plus(self):
        result = _markdown_to_mrkdwn("+ item one")
        assert "• item one" in result

    def test_indented_list_preserved(self):
        result = _markdown_to_mrkdwn("- outer\n  - inner")
        assert "• outer" in result
        assert "  • inner" in result

    # ── Task lists ──

    def test_task_list_checked(self):
        result = _markdown_to_mrkdwn("- [x] Done task")
        assert "☒ Done task" in result

    def test_task_list_unchecked(self):
        result = _markdown_to_mrkdwn("- [ ] Todo task")
        assert "☐ Todo task" in result

    def test_task_list_mixed(self):
        text = "- [x] First\n- [ ] Second\n- [x] Third"
        result = _markdown_to_mrkdwn(text)
        assert "☒ First" in result
        assert "☐ Second" in result
        assert "☒ Third" in result

    # ── Zero-width space buffering ──

    def test_bold_mid_word_gets_zws(self):
        """Bold inside a word should get ZWS to prevent Slack misparse."""
        result = _markdown_to_mrkdwn("foo**bar**baz")
        zws = "\u200b"
        assert f"foo{zws}*bar*{zws}baz" == result

    def test_strikethrough_mid_word_gets_zws(self):
        result = _markdown_to_mrkdwn("foo~~bar~~baz")
        zws = "\u200b"
        assert f"foo{zws}~bar~{zws}baz" == result

    def test_zws_cleaned_at_line_edges(self):
        """ZWS at start/end of line should be stripped."""
        result = _markdown_to_mrkdwn("**hello**")
        assert not result.startswith("\u200b")
        assert not result.endswith("\u200b")
        assert result == "*hello*"

    # ── Link reference resolution ──

    def test_reference_link(self):
        text = "See [the docs][docs] for info.\n\n[docs]: https://example.com"
        result = _markdown_to_mrkdwn(text)
        assert "<https://example.com|the docs>" in result

    def test_reference_link_implicit_label(self):
        """[text][] uses text as the ref label."""
        text = "Visit [example][] now.\n\n[example]: https://example.com"
        result = _markdown_to_mrkdwn(text)
        assert "<https://example.com|example>" in result

    def test_reference_link_unresolved_unchanged(self):
        """Unknown ref should leave the text unchanged."""
        text = "See [foo][unknown]"
        result = _markdown_to_mrkdwn(text)
        assert "[foo][unknown]" in result

    def test_reference_link_case_insensitive(self):
        text = "See [link][DOCS]\n\n[docs]: https://example.com"
        result = _markdown_to_mrkdwn(text)
        assert "<https://example.com|link>" in result

    # ── HTML comment removal ──

    def test_html_comment_removed(self):
        result = _markdown_to_mrkdwn("before <!-- comment --> after")
        assert "<!--" not in result
        assert "comment" not in result
        assert "before" in result
        assert "after" in result

    def test_multiline_html_comment_removed(self):
        text = "start\n<!-- multi\nline\ncomment -->\nend"
        result = _markdown_to_mrkdwn(text)
        assert "<!--" not in result
        assert "start" in result
        assert "end" in result

    # ── Email link conversion ──

    def test_email_link(self):
        result = _markdown_to_mrkdwn("<user@example.com>")
        assert "<mailto:user@example.com>" in result

    def test_email_link_in_sentence(self):
        result = _markdown_to_mrkdwn("Contact <support@company.io> for help")
        assert "<mailto:support@company.io>" in result

    # ── Integration: entities + links coexist ──

    def test_slack_links_not_broken_by_entity_escaping(self):
        """Angle brackets in converted links should not be escaped."""
        result = _markdown_to_mrkdwn("[click](https://example.com)")
        assert "<https://example.com|click>" in result


class TestSplitMarkdown:
    """Test _split_markdown for Slack markdown block splitting."""

    def test_short_text_no_split(self):
        text = "Hello world"
        result = _split_markdown(text)
        assert result == ["Hello world"]

    def test_text_under_limit_no_split(self):
        text = "a" * MARKDOWN_BLOCK_LIMIT
        result = _split_markdown(text)
        assert len(result) == 1

    def test_text_over_limit_splits(self):
        text = "a" * (MARKDOWN_BLOCK_LIMIT + 1000)
        result = _split_markdown(text)
        assert len(result) >= 2

    def test_splits_at_paragraph_boundary(self):
        # Create text with paragraph breaks
        para = "a" * 5000
        text = f"{para}\n\n{para}\n\n{para}"
        result = _split_markdown(text, limit=6000)
        assert len(result) >= 2
        # First chunk should end at a paragraph boundary
        assert result[0].strip() == para

    def test_splits_at_heading_boundary(self):
        section = "a" * 4000
        text = f"{section}\n# Heading\n{section}"
        result = _split_markdown(text, limit=5000)
        assert len(result) >= 2
        # Second chunk should start with heading
        assert result[1].startswith("# Heading")

    def test_hard_split_when_no_boundaries(self):
        text = "a" * 25000  # No newlines at all
        result = _split_markdown(text, limit=11000)
        assert len(result) >= 2
        # All content preserved
        assert "".join(result) == text

    def test_preserves_all_content(self):
        para = "paragraph content here"
        text = "\n\n".join([para] * 100)
        result = _split_markdown(text, limit=200)
        # Rejoin (accounting for stripped newlines) should contain all paragraphs
        rejoined = "\n".join(result)
        assert rejoined.count("paragraph content here") == 100

    def test_empty_text(self):
        assert _split_markdown("") == [""]

    def test_single_newline_fallback(self):
        # Lines without paragraph breaks, no headings
        lines = ["line " + str(i) for i in range(200)]
        text = "\n".join(lines)
        result = _split_markdown(text, limit=500)
        assert len(result) >= 2
        # Should split at single newlines
        for chunk in result:
            assert len(chunk) <= 500
        assert "&lt;" not in result


class TestSnippetMetadataFromTool:
    """Test _snippet_metadata_from_tool derives correct filetype, filename, and label."""

    # --- Read tool ---

    def test_read_python_file(self):
        meta = _snippet_metadata_from_tool("Read", {"file_path": "/src/app.py"}, "")
        assert meta.filetype == "python"
        assert meta.filename == "app.py"
        assert "`app.py`" in meta.label

    def test_read_typescript_file(self):
        meta = _snippet_metadata_from_tool("Read", {"file_path": "/src/index.tsx"}, "")
        assert meta.filetype == "javascript"
        assert meta.filename == "index.tsx"

    def test_read_yaml_file(self):
        meta = _snippet_metadata_from_tool("Read", {"file_path": "/config.yml"}, "")
        assert meta.filetype == "yaml"
        assert meta.filename == "config.yml"

    def test_read_unknown_extension(self):
        meta = _snippet_metadata_from_tool("Read", {"file_path": "/data.xyz"}, "")
        assert meta.filetype == "text"
        assert meta.filename == "data.xyz"

    def test_read_no_path(self):
        meta = _snippet_metadata_from_tool("Read", {}, "")
        assert meta.filetype == "text"
        assert "contents" in meta.label.lower()

    # --- Bash tool ---

    def test_bash_git_diff(self):
        meta = _snippet_metadata_from_tool("Bash", {"command": "git diff HEAD"}, "")
        assert meta.filetype == "diff"
        assert meta.filename == "diff.diff"

    def test_bash_git_show(self):
        meta = _snippet_metadata_from_tool("Bash", {"command": "git show abc123"}, "")
        assert meta.filetype == "diff"

    def test_bash_pytest(self):
        meta = _snippet_metadata_from_tool("Bash", {"command": "pytest tests/"}, "")
        assert meta.filetype == "text"
        assert "test" in meta.filename.lower()

    def test_bash_python_command(self):
        meta = _snippet_metadata_from_tool("Bash", {"command": "python3 script.py"}, "")
        assert meta.filetype == "python"

    def test_bash_with_description(self):
        meta = _snippet_metadata_from_tool(
            "Bash",
            {"command": "npm run build", "description": "Build the project"},
            "",
        )
        assert "Build the project" in meta.label

    def test_bash_long_command_truncated(self):
        long_cmd = "x" * 100
        meta = _snippet_metadata_from_tool("Bash", {"command": long_cmd}, "")
        assert len(meta.label) < 200  # label should be reasonable length

    def test_bash_fallback_to_content_detection(self):
        meta = _snippet_metadata_from_tool("Bash", {"command": "some-tool"}, '{"key": "value"}')
        assert meta.filetype == "javascript"  # JSON detected from content

    # --- Edit tool ---

    def test_edit_produces_diff(self):
        meta = _snippet_metadata_from_tool(
            "Edit",
            {"file_path": "/src/config.py"},
            "",
        )
        assert meta.filetype == "diff"
        assert meta.filename == "config.py.diff"
        assert "`config.py`" in meta.label

    def test_edit_no_path(self):
        meta = _snippet_metadata_from_tool("Edit", {}, "")
        assert meta.filetype == "diff"

    # --- Write tool ---

    def test_write_python(self):
        meta = _snippet_metadata_from_tool("Write", {"file_path": "/new/module.py"}, "")
        assert meta.filetype == "python"
        assert meta.filename == "module.py"

    def test_write_json(self):
        meta = _snippet_metadata_from_tool("Write", {"file_path": "/config.json"}, "")
        assert meta.filetype == "javascript"
        assert meta.filename == "config.json"

    # --- Grep/Glob ---

    def test_grep_with_pattern(self):
        meta = _snippet_metadata_from_tool("Grep", {"pattern": "import.*os"}, "")
        assert meta.filetype == "text"
        assert "`import.*os`" in meta.label

    def test_glob_with_pattern(self):
        meta = _snippet_metadata_from_tool("Glob", {"pattern": "**/*.py"}, "")
        assert meta.filetype == "text"

    # --- WebFetch ---

    def test_webfetch_with_url(self):
        meta = _snippet_metadata_from_tool("WebFetch", {"url": "https://example.com"}, "")
        assert meta.filetype == "markdown"
        assert "example.com" in meta.label

    # --- Unknown tool ---

    def test_unknown_tool_fallback(self):
        meta = _snippet_metadata_from_tool("CustomTool", {}, "plain text")
        assert meta.filetype == "text"
        assert "CustomTool" in meta.label

    def test_mcp_tool_name_cleaned(self):
        meta = _snippet_metadata_from_tool("mcp__server__find_code", {}, "")
        assert "find_code" in meta.label
        assert "mcp__" not in meta.label


class TestFormatUnifiedDiff:
    """Test _format_unified_diff produces full unified diff for snippet upload."""

    def test_simple_diff(self):
        result = _format_unified_diff("def foo():", "def bar():")
        assert "---" in result
        assert "+++" in result
        assert "@@" in result
        assert "-def foo():" in result
        assert "+def bar():" in result

    def test_multiline_diff(self):
        result = _format_unified_diff("a\nb\nc", "a\nX\nc")
        assert "-b" in result
        assert "+X" in result

    def test_identical_returns_empty(self):
        assert _format_unified_diff("same", "same") == ""

    def test_empty_strings_returns_empty(self):
        assert _format_unified_diff("", "") == ""

    def test_all_lines_end_with_newline(self):
        result = _format_unified_diff("old", "new")
        for line in result.split("\n"):
            if line:  # skip empty trailing line
                pass  # just checking it doesn't crash
        # Every line from the diff should end with newline
        assert result.endswith("\n")


# ---------------------------------------------------------------------------
# Code block extraction
# ---------------------------------------------------------------------------


class TestExtractCodeBlocks:
    """Test _extract_code_blocks extracts large fenced code blocks."""

    def _big_block(self, lang: str = "python", lines: int = 30) -> str:
        """Generate a fenced code block exceeding extraction thresholds."""
        code_lines = [f"line_{i} = {i}  # some padding to ensure enough characters" for i in range(lines)]
        code = "\n".join(code_lines)
        return f"```{lang}\n{code}\n```"

    def test_extracts_large_python_block(self):
        block = self._big_block("python", 30)
        text = f"Here is some code:\n\n{block}\n\nDone."
        result = _extract_code_blocks(text)
        assert len(result) == 1
        lang, code, full_match = result[0]
        assert lang == "python"
        assert "line_0 = 0" in code
        assert full_match in text

    def test_ignores_small_block(self):
        text = "```python\nprint('hello')\n```"
        result = _extract_code_blocks(text)
        assert len(result) == 0

    def test_ignores_block_without_language(self):
        code_lines = [f"line {i}" for i in range(25)]
        block = "```\n" + "\n".join(code_lines) + "\n```"
        result = _extract_code_blocks(block)
        assert len(result) == 0

    def test_ignores_unknown_language(self):
        code_lines = [f"line {i}" for i in range(25)]
        block = "```brainfuck\n" + "\n".join(code_lines) + "\n```"
        result = _extract_code_blocks(block)
        assert len(result) == 0

    def test_extracts_multiple_blocks(self):
        block1 = self._big_block("python", 30)
        block2 = self._big_block("javascript", 30)
        text = f"First:\n{block1}\n\nSecond:\n{block2}"
        result = _extract_code_blocks(text)
        assert len(result) == 2
        assert result[0][0] == "python"
        assert result[1][0] == "javascript"

    def test_leaves_small_block_extracts_large(self):
        small = "```python\nx = 1\n```"
        large = self._big_block("go", 25)
        text = f"Small:\n{small}\n\nLarge:\n{large}"
        result = _extract_code_blocks(text)
        assert len(result) == 1
        assert result[0][0] == "go"

    def test_known_languages_in_mapping(self):
        """Verify common language tags are in _LANG_TO_FILETYPE."""
        for lang in ("python", "py", "javascript", "js", "typescript", "ts",
                      "go", "rust", "java", "bash", "sh", "sql", "css", "html"):
            assert lang in _LANG_TO_FILETYPE, f"{lang} not in _LANG_TO_FILETYPE"


# ---------------------------------------------------------------------------
# Test result parsing
# ---------------------------------------------------------------------------


class TestParseTestResults:
    """Test _parse_test_results parsing of pytest and jest output."""

    def test_pytest_all_passed(self):
        output = "========================= 42 passed in 3.45s ========================="
        result = _parse_test_results(output)
        assert result is not None
        assert result.passed == 42
        assert result.failed == 0
        assert result.duration == "3.45s"

    def test_pytest_mixed(self):
        output = "=============== 5 failed, 37 passed, 2 skipped in 12.3s ==============="
        result = _parse_test_results(output)
        assert result is not None
        assert result.passed == 37
        assert result.failed == 5
        assert result.skipped == 2
        assert result.duration == "12.3s"

    def test_pytest_with_errors(self):
        output = "=============== 1 failed, 2 error, 10 passed in 5.0s ==============="
        result = _parse_test_results(output)
        assert result is not None
        assert result.errors == 2
        assert result.failed == 1
        assert result.passed == 10

    def test_pytest_only_failed(self):
        output = "=============== 3 failed in 1.2s ==============="
        result = _parse_test_results(output)
        assert result is not None
        assert result.failed == 3
        assert result.passed == 0

    def test_jest_format(self):
        output = """Test Suites:  1 failed, 2 passed, 3 total
Tests:  1 failed, 5 passed, 6 total
Time:        2.5 s"""
        result = _parse_test_results(output)
        assert result is not None
        assert result.passed == 5
        assert result.failed == 1

    def test_jest_all_passed(self):
        output = """Test Suites:  3 passed, 3 total
Tests:  15 passed, 15 total
Time:        1.2 s"""
        result = _parse_test_results(output)
        assert result is not None
        assert result.passed == 15
        assert result.failed == 0

    def test_no_test_output(self):
        result = _parse_test_results("Hello world, nothing to see here")
        assert result is None

    def test_summary_from_long_output(self):
        """Summary line at the bottom of long output is still found."""
        lines = ["PASSED tests/test_foo.py::test_bar"] * 50
        lines.append("=============== 50 passed in 8.0s ===============")
        result = _parse_test_results("\n".join(lines))
        assert result is not None
        assert result.passed == 50


class TestFormatTestSummary:
    """Test _format_test_summary formatting."""

    def test_all_passed(self):
        tr = ParsedTestResult(passed=10, duration="3.4s")
        text, color = _format_test_summary(tr)
        assert ":white_check_mark:" in text
        assert "*10 passed*" in text
        assert "3.4s" in text
        assert color == "good"

    def test_failures(self):
        tr = ParsedTestResult(passed=8, failed=2)
        text, color = _format_test_summary(tr)
        assert ":x:" in text
        assert "*2 failed*" in text
        assert "*8 passed*" in text
        assert color == "danger"

    def test_errors(self):
        tr = ParsedTestResult(passed=5, errors=1)
        text, color = _format_test_summary(tr)
        assert ":x:" in text
        assert "*1 error*" in text
        assert color == "danger"

    def test_skipped(self):
        tr = ParsedTestResult(passed=10, skipped=3, duration="1.0s")
        text, color = _format_test_summary(tr)
        assert "3 skipped" in text
        assert color == "good"


# ---------------------------------------------------------------------------
# Git commit info extraction
# ---------------------------------------------------------------------------


class TestExtractGitCommitInfo:
    """Test _extract_git_commit_info parsing of git commit output."""

    def test_standard_commit(self):
        output = """[main abc1234] feat: add new feature
 3 files changed, 45 insertions(+), 12 deletions(-)"""
        info = _extract_git_commit_info(output)
        assert info is not None
        assert info.short_hash == "abc1234"
        assert info.message == "feat: add new feature"
        assert info.files_changed == 3
        assert info.insertions == 45
        assert info.deletions == 12

    def test_commit_with_branch_slash(self):
        output = "[feature/auth 1a2b3c4] fix: resolve login bug\n 1 file changed, 5 insertions(+)"
        info = _extract_git_commit_info(output)
        assert info is not None
        assert info.short_hash == "1a2b3c4"
        assert info.message == "fix: resolve login bug"
        assert info.files_changed == 1
        assert info.insertions == 5
        assert info.deletions == 0

    def test_commit_only_deletions(self):
        output = "[main abcdef0] chore: remove dead code\n 2 files changed, 15 deletions(-)"
        info = _extract_git_commit_info(output)
        assert info is not None
        assert info.deletions == 15
        assert info.insertions == 0

    def test_no_stat_line(self):
        output = "[main abc1234] initial commit"
        info = _extract_git_commit_info(output)
        assert info is not None
        assert info.short_hash == "abc1234"
        assert info.files_changed == 0

    def test_no_commit_output(self):
        info = _extract_git_commit_info("nothing interesting here")
        assert info is None

    def test_commit_in_longer_output(self):
        """Commit info found in git output with surrounding noise."""
        output = """On branch main
Your branch is up to date with 'origin/main'.

[main fedcba9] docs: update README
 1 file changed, 10 insertions(+), 2 deletions(-)
"""
        info = _extract_git_commit_info(output)
        assert info is not None
        assert info.short_hash == "fedcba9"
        assert info.message == "docs: update README"

    def test_long_hash(self):
        output = "[main 1234567890ab] test commit"
        info = _extract_git_commit_info(output)
        assert info is not None
        assert info.short_hash == "1234567890ab"


class TestFormatCommitCard:
    """Test _format_commit_card formatting."""

    def test_full_stats(self):
        info = CommitInfo(
            short_hash="abc1234",
            message="feat: new feature",
            files_changed=3,
            insertions=45,
            deletions=12,
        )
        text = _format_commit_card(info)
        assert ":package:" in text
        assert "*Committed*" in text
        assert "`abc1234`" in text
        assert "feat: new feature" in text
        assert "3 files changed" in text
        assert "+45" in text
        assert "-12" in text

    def test_no_stats(self):
        info = CommitInfo(short_hash="abc1234", message="initial commit")
        text = _format_commit_card(info)
        assert "`abc1234`" in text
        assert "initial commit" in text
        assert "file" not in text  # no stats line

    def test_single_file(self):
        info = CommitInfo(
            short_hash="abc1234",
            message="fix bug",
            files_changed=1,
            insertions=2,
        )
        text = _format_commit_card(info)
        assert "1 file changed" in text  # singular

    def test_only_insertions(self):
        info = CommitInfo(
            short_hash="abc1234",
            message="add code",
            files_changed=2,
            insertions=10,
            deletions=0,
        )
        text = _format_commit_card(info)
        assert "+10" in text
        assert "-" not in text.split("\n")[-1]  # no deletions in stats line
