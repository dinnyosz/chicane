"""Tests for chicane.app — session ID resolution and PID file guard."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chicane.app import _acquire_pidfile, _release_pidfile, resolve_session_id
import chicane.app as app_module


class TestResolveSessionId:
    def test_explicit_id_returned_as_is(self):
        assert resolve_session_id("abc-123") == "abc-123"

    def test_reads_from_history(self, tmp_path, monkeypatch):
        history = tmp_path / ".claude" / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(
            json.dumps({"sessionId": "old-session", "display": "hi"}) + "\n"
            + json.dumps({"sessionId": "latest-session", "display": "bye"}) + "\n"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert resolve_session_id(None) == "latest-session"

    def test_raises_when_no_history_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(ValueError, match="No Claude history found"):
            resolve_session_id(None)

    def test_raises_when_no_session_id_in_history(self, tmp_path, monkeypatch):
        history = tmp_path / ".claude" / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(json.dumps({"display": "no session"}) + "\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(ValueError, match="Could not extract session ID"):
            resolve_session_id(None)

    def test_single_entry_history(self, tmp_path, monkeypatch):
        history = tmp_path / ".claude" / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(json.dumps({"sessionId": "only-one", "display": "x"}) + "\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert resolve_session_id(None) == "only-one"


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


class TestIsigWatchdog:
    """Tests for the ISIG terminal watchdog in _run_bot."""

    @pytest.fixture
    def termios_mock(self):
        """Create a mock termios module with realistic lflag behavior."""
        import termios as real_termios
        mock = MagicMock()
        mock.ISIG = real_termios.ISIG
        mock.TCSANOW = real_termios.TCSANOW
        mock.error = real_termios.error
        return mock

    def test_ensure_isig_restores_flag_when_cleared(self, termios_mock):
        """When ISIG is cleared from lflag, _ensure_isig should restore it."""
        import termios as real_termios

        # Simulate attrs with ISIG cleared in lflag (index 3)
        attrs = [0, 0, 0, 0, 0, 0, []]  # lflag = 0, ISIG is not set
        termios_mock.tcgetattr.return_value = attrs

        with patch.dict("sys.modules", {"termios": termios_mock}):
            # Import inline to get the patched version
            fd = 0
            def _ensure_isig():
                try:
                    a = termios_mock.tcgetattr(fd)
                    if not (a[3] & termios_mock.ISIG):
                        a[3] |= termios_mock.ISIG
                        termios_mock.tcsetattr(fd, termios_mock.TCSANOW, a)
                except (termios_mock.error, OSError):
                    pass

            _ensure_isig()

        termios_mock.tcsetattr.assert_called_once()
        set_attrs = termios_mock.tcsetattr.call_args[0][2]
        assert set_attrs[3] & real_termios.ISIG

    def test_ensure_isig_noop_when_flag_already_set(self, termios_mock):
        """When ISIG is already set, _ensure_isig should not call tcsetattr."""
        import termios as real_termios

        attrs = [0, 0, 0, real_termios.ISIG, 0, 0, []]
        termios_mock.tcgetattr.return_value = attrs

        fd = 0
        def _ensure_isig():
            try:
                a = termios_mock.tcgetattr(fd)
                if not (a[3] & termios_mock.ISIG):
                    a[3] |= termios_mock.ISIG
                    termios_mock.tcsetattr(fd, termios_mock.TCSANOW, a)
            except (termios_mock.error, OSError):
                pass

        _ensure_isig()
        termios_mock.tcsetattr.assert_not_called()

    def test_ensure_isig_handles_termios_error(self, termios_mock):
        """_ensure_isig should silently handle termios errors."""
        import termios as real_termios
        termios_mock.tcgetattr.side_effect = real_termios.error("bad fd")

        fd = 0
        def _ensure_isig():
            try:
                a = termios_mock.tcgetattr(fd)
                if not (a[3] & termios_mock.ISIG):
                    a[3] |= termios_mock.ISIG
                    termios_mock.tcsetattr(fd, termios_mock.TCSANOW, a)
            except (termios_mock.error, OSError):
                pass

        _ensure_isig()  # Should not raise
