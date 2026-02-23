"""Tests for contextual TodoWrite diff and formatting."""

import pytest

from chicane.handlers import _diff_todos, _format_todo_update


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


class TestFormatTodoUpdate:
    """Test _format_todo_update contextual message formatting."""

    def test_empty_current(self):
        assert _format_todo_update(None, []) == ":clipboard: Updating tasks"

    def test_first_call_shows_plan(self):
        todos = [_todo("A"), _todo("B"), _todo("C")]
        result = _format_todo_update(None, todos)
        assert result == ":clipboard: *Tasks* (3)\nA, B, C"

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
        assert ":white_check_mark: A" in lines[0]
        assert ":arrows_counterclockwise: B (2/3)" in lines[1]
        assert "Done: A" in lines[2]
        assert "Remaining: B, C" in lines[3]

    def test_task_started_only(self):
        previous = [_todo("A", "pending"), _todo("B", "pending")]
        current = [_todo("A", "in_progress"), _todo("B", "pending")]
        result = _format_todo_update(previous, current)
        lines = result.split("\n")
        assert ":arrows_counterclockwise: A (1/2)" in lines[0]
        assert "Remaining: A, B" in lines[1]

    def test_new_tasks_added(self):
        previous = [_todo("A", "in_progress")]
        current = [
            _todo("A", "in_progress"),
            _todo("B", "pending"),
            _todo("C", "pending"),
        ]
        result = _format_todo_update(previous, current)
        assert ":new: Added: B, C" in result
        assert "Remaining:" in result

    def test_no_changes_shows_current_progress(self):
        """When snapshot is identical, still show current in_progress task."""
        todos = [
            _todo("A", "completed"),
            _todo("B", "in_progress"),
            _todo("C", "pending"),
        ]
        result = _format_todo_update(todos, todos)
        assert ":arrows_counterclockwise: B (2/3)" in result

    def test_no_changes_no_in_progress(self):
        """All pending, no in_progress — show generic progress."""
        todos = [_todo("A", "pending"), _todo("B", "pending")]
        result = _format_todo_update(todos, todos)
        assert ":clipboard: Tasks (0/2)" in result

    def test_done_line_shows_completed_tasks(self):
        """The 'Done:' line should list all completed tasks."""
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
        assert "Done: A, B" in result
        assert "Remaining: C" in result

    def test_done_line_absent_when_nothing_completed(self):
        """No 'Done:' line when nothing is completed yet."""
        previous = [_todo("A", "pending"), _todo("B", "pending")]
        current = [_todo("A", "in_progress"), _todo("B", "pending")]
        result = _format_todo_update(previous, current)
        assert "Done:" not in result

    def test_single_task_no_remaining(self):
        """Single-task list: no 'Remaining' line when it's in progress."""
        previous = [_todo("A", "pending")]
        current = [_todo("A", "in_progress")]
        result = _format_todo_update(previous, current)
        assert ":arrows_counterclockwise: A (1/1)" in result
        # A is in_progress so it's in remaining, but it's also the only task
        assert "Remaining: A" in result
