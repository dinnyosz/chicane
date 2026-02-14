"""Tests for message formatting: split_message, markdown_to_mrkdwn, completion_summary."""

from unittest.mock import MagicMock

from chicane.claude import ClaudeEvent
from chicane.handlers import (
    _format_completion_summary,
    _markdown_to_mrkdwn,
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
        assert "&lt;" not in result
