"""Tests for chicane.app — session ID resolution and PID file guard."""

import json
import os
from pathlib import Path

import pytest

from chicane.app import _acquire_pidfile, _release_pidfile, _resolve_session_id
import chicane.app as app_module


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


class TestPidFile:
    @pytest.fixture(autouse=True)
    def _use_tmp_pidfile(self, tmp_path, monkeypatch):
        self.pidfile = tmp_path / "chicane.pid"
        monkeypatch.setattr(app_module, "PID_FILE", self.pidfile)

    def test_acquire_creates_pidfile(self):
        _acquire_pidfile()
        assert self.pidfile.exists()
        assert int(self.pidfile.read_text().strip()) == os.getpid()

    def test_acquire_fails_if_alive(self):
        # Write our own PID — guaranteed alive
        self.pidfile.write_text(str(os.getpid()))
        with pytest.raises(SystemExit):
            _acquire_pidfile()

    def test_acquire_overwrites_stale_pid(self):
        # PID 2**22 is almost certainly not running
        self.pidfile.write_text("4194304")
        _acquire_pidfile()
        assert int(self.pidfile.read_text().strip()) == os.getpid()

    def test_acquire_handles_corrupt_pidfile(self):
        self.pidfile.write_text("not-a-number\n")
        _acquire_pidfile()
        assert int(self.pidfile.read_text().strip()) == os.getpid()

    def test_release_removes_pidfile(self):
        self.pidfile.write_text(str(os.getpid()))
        _release_pidfile()
        assert not self.pidfile.exists()

    def test_release_noop_if_different_pid(self):
        self.pidfile.write_text("99999")
        _release_pidfile()
        assert self.pidfile.exists()
        assert self.pidfile.read_text().strip() == "99999"
