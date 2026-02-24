"""Tests for contextual TodoWrite diff and formatting."""

import pytest

from chicane.handlers import _diff_todos, _format_list, _format_todo_update


def _todo(content: str, status: str = "pending") -> dict:
    """Shorthand for creating a todo dict."""
    return {"content": content, "status": status}


class TestDiffTodos:
    """Test _diff_todos diffing logic."""

    def test_empty_current(self):
        completed, in_progress, added, remaining = _diff_todos(
            [_todo("A", "pending")], []
        )
        assert completed == []
        assert in_progress == []
        assert added == []
        assert remaining == []

    def test_no_previous(self):
        """First call — everything is 'new' but nothing counts as newly_added
        because previous is None (handled by _format_todo_update instead)."""
        completed, in_progress, added, remaining = _diff_todos(
            None,
            [_todo("A", "pending"), _todo("B", "in_progress")],
        )
        # With previous=None, prev_map is empty so no "newly added" detection
        assert completed == []
        assert in_progress == ["B"]
        assert added == []
        assert remaining == ["A", "B"]

    def test_task_completed(self):
        previous = [_todo("A", "in_progress"), _todo("B", "pending")]
        current = [_todo("A", "completed"), _todo("B", "pending")]
        completed, in_progress, added, remaining = _diff_todos(previous, current)
        assert completed == ["A"]
        assert in_progress == []
        assert added == []
        assert remaining == ["B"]

    def test_task_started(self):
        previous = [_todo("A", "completed"), _todo("B", "pending")]
        current = [_todo("A", "completed"), _todo("B", "in_progress")]
        completed, in_progress, added, remaining = _diff_todos(previous, current)
        assert completed == []
        assert in_progress == ["B"]
        assert added == []
        assert remaining == ["B"]

    def test_new_tasks_added(self):
        previous = [_todo("A", "completed")]
        current = [_todo("A", "completed"), _todo("B", "pending"), _todo("C", "pending")]
        completed, in_progress, added, remaining = _diff_todos(previous, current)
        assert completed == []
        assert in_progress == []
        assert added == ["B", "C"]
        assert remaining == ["B", "C"]

    def test_mixed_changes(self):
        """Complete one, start another, add a new one — all in one update."""
        previous = [
            _todo("A", "in_progress"),
            _todo("B", "pending"),
        ]
        current = [
            _todo("A", "completed"),
            _todo("B", "in_progress"),
            _todo("C", "pending"),
        ]
        completed, in_progress, added, remaining = _diff_todos(previous, current)
        assert completed == ["A"]
        assert in_progress == ["B"]
        assert added == ["C"]
        assert remaining == ["B", "C"]

    def test_no_changes(self):
        todos = [_todo("A", "in_progress"), _todo("B", "pending")]
        completed, in_progress, added, remaining = _diff_todos(todos, todos)
        assert completed == []
        assert in_progress == []
        assert added == []
        assert remaining == ["A", "B"]


class TestFormatList:
    """Test _format_list helper."""

    def test_single_item_inline(self):
        assert _format_list("Done", ["A"]) == "Done: A"

    def test_multiple_items_bullets(self):
        assert _format_list("Done", ["A", "B"]) == "Done:\n• A\n• B"

    def test_three_items(self):
        result = _format_list("Remaining", ["A", "B", "C"])
        assert result == "Remaining:\n• A\n• B\n• C"


class TestFormatTodoUpdate:
    """Test _format_todo_update contextual message formatting."""

    def test_empty_current(self):
        assert _format_todo_update(None, []) == ":clipboard: Updating tasks"

    def test_first_call_shows_plan_multiple(self):
        todos = [_todo("A"), _todo("B"), _todo("C")]
        result = _format_todo_update(None, todos)
        assert result == ":clipboard: *Tasks* (3)\n• A\n• B\n• C"

    def test_first_call_shows_plan_single(self):
        todos = [_todo("A")]
        result = _format_todo_update(None, todos)
        assert result == ":clipboard: *Tasks* (1)\nA"

    def test_all_complete(self):
        previous = [_todo("A", "in_progress")]
        current = [_todo("A", "completed")]
        result = _format_todo_update(previous, current)
        assert result == ":white_check_mark: All tasks complete (1/1)"

    def test_all_complete_multiple(self):
        previous = [_todo("A", "completed"), _todo("B", "in_progress")]
        current = [_todo("A", "completed"), _todo("B", "completed")]
        result = _format_todo_update(previous, current)
        assert result == ":white_check_mark: All tasks complete (2/2)"

    def test_task_completed_and_next_started(self):
        previous = [
            _todo("A", "in_progress"),
            _todo("B", "pending"),
            _todo("C", "pending"),
        ]
        current = [
            _todo("A", "completed"),
            _todo("B", "in_progress"),
            _todo("C", "pending"),
        ]
        result = _format_todo_update(previous, current)
        lines = result.split("\n")
        assert ":white_check_mark: A finished" in lines[0]
        assert ":arrows_counterclockwise: B in progress (2/3)" in lines[1]
        # Remaining has 2 items → bullets
        assert "Remaining:" in result
        assert "• B" in result
        assert "• C" in result
        # No Done section — redundant with the checkmark line
        assert "Done:" not in result

    def test_task_started_only(self):
        previous = [_todo("A", "pending"), _todo("B", "pending")]
        current = [_todo("A", "in_progress"), _todo("B", "pending")]
        result = _format_todo_update(previous, current)
        assert ":arrows_counterclockwise: A in progress (1/2)" in result
        # 2 remaining → bullets
        assert "Remaining:\n• A\n• B" in result

    def test_new_tasks_added_multiple(self):
        previous = [_todo("A", "in_progress")]
        current = [
            _todo("A", "in_progress"),
            _todo("B", "pending"),
            _todo("C", "pending"),
        ]
        result = _format_todo_update(previous, current)
        # 2 added → bullets
        assert ":new: Added:\n• B\n• C" in result
        assert "Remaining:" in result

    def test_new_task_added_single(self):
        previous = [_todo("A", "in_progress")]
        current = [
            _todo("A", "in_progress"),
            _todo("B", "pending"),
        ]
        result = _format_todo_update(previous, current)
        # 1 added → inline
        assert ":new: Added: B" in result

    def test_no_changes_shows_current_progress(self):
        """When snapshot is identical, still show current in_progress task."""
        todos = [
            _todo("A", "completed"),
            _todo("B", "in_progress"),
            _todo("C", "pending"),
        ]
        result = _format_todo_update(todos, todos)
        assert ":arrows_counterclockwise: B in progress (2/3)" in result

    def test_no_changes_no_in_progress(self):
        """All pending, no in_progress — show generic progress."""
        todos = [_todo("A", "pending"), _todo("B", "pending")]
        result = _format_todo_update(todos, todos)
        assert ":clipboard: Tasks (0/2)" in result

    def test_no_done_section(self):
        """Done section is never shown — checkmark lines are sufficient."""
        previous = [
            _todo("A", "completed"),
            _todo("B", "in_progress"),
            _todo("C", "pending"),
        ]
        current = [
            _todo("A", "completed"),
            _todo("B", "completed"),
            _todo("C", "in_progress"),
        ]
        result = _format_todo_update(previous, current)
        assert "Done:" not in result
        # But the newly completed task is still announced
        assert ":white_check_mark: B finished" in result
        assert "Remaining: C" in result

    def test_single_task_remaining(self):
        """Single-task list: 'Remaining' is inline for 1 item."""
        previous = [_todo("A", "pending")]
        current = [_todo("A", "in_progress")]
        result = _format_todo_update(previous, current)
        assert ":arrows_counterclockwise: A in progress (1/1)" in result
        assert "Remaining: A" in result

    def test_no_done_section_even_with_multiple_completed(self):
        """Done section never appears, even with many completed tasks."""
        previous = [
            _todo("A", "completed"),
            _todo("B", "completed"),
            _todo("C", "in_progress"),
            _todo("D", "pending"),
        ]
        current = [
            _todo("A", "completed"),
            _todo("B", "completed"),
            _todo("C", "completed"),
            _todo("D", "in_progress"),
        ]
        result = _format_todo_update(previous, current)
        assert "Done:" not in result
        assert ":white_check_mark: C finished" in result
        assert ":arrows_counterclockwise: D in progress (4/4)" in result
