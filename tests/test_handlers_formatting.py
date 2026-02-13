"""Tests for message formatting: split_message, markdown_to_mrkdwn, completion_summary."""

from chicane.claude import ClaudeEvent
from chicane.handlers import (
    _format_completion_summary,
    _markdown_to_mrkdwn,
)


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
