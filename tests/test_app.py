"""Tests for chicane.app — session ID resolution."""

import json
from pathlib import Path

import pytest

from chicane.app import _resolve_session_id


class TestResolveSessionId:
    def test_explicit_id_returned_as_is(self):
        assert _resolve_session_id("abc-123") == "abc-123"

    def test_reads_from_history(self, tmp_path, monkeypatch):
        history = tmp_path / ".claude" / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(
            json.dumps({"sessionId": "old-session", "display": "hi"}) + "\n"
            + json.dumps({"sessionId": "latest-session", "display": "bye"}) + "\n"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _resolve_session_id(None) == "latest-session"

    def test_exits_when_no_history_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(SystemExit):
            _resolve_session_id(None)

    def test_exits_when_no_session_id_in_history(self, tmp_path, monkeypatch):
        history = tmp_path / ".claude" / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(json.dumps({"display": "no session"}) + "\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(SystemExit):
            _resolve_session_id(None)

    def test_single_entry_history(self, tmp_path, monkeypatch):
        history = tmp_path / ".claude" / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(json.dumps({"sessionId": "only-one", "display": "x"}) + "\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _resolve_session_id(None) == "only-one"
