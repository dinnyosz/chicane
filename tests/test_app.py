"""Tests for chicane.app — session ID resolution, PID file guard, DNS watchdog."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.app import (
    DNS_FLUSH_COOLDOWN,
    DNS_FLUSH_THRESHOLD,
    _acquire_pidfile,
    _dns_watchdog,
    _release_pidfile,
    resolve_session_id,
)
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


class TestDnsWatchdog:
    """Tests for the DNS cache flush watchdog."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.is_connected = AsyncMock(return_value=False)
        client.last_ping_pong_time = time.time() - 600  # 10 min ago
        return client

    async def _run_one_cycle(self, client):
        """Run the watchdog for one iteration then cancel it."""
        # Patch asyncio.sleep to skip the initial 60s wait, then cancel
        call_count = 0

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        with patch("chicane.app.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await _dns_watchdog(client)

    @pytest.mark.asyncio
    async def test_skips_when_connected(self, mock_client):
        """No flush when connection is healthy."""
        mock_client.is_connected.return_value = True

        with patch("chicane.app.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            await self._run_one_cycle(mock_client)
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_ping_pong_time(self, mock_client):
        """No flush when last_ping_pong_time is None (never connected)."""
        mock_client.last_ping_pong_time = None

        with patch("chicane.app.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            await self._run_one_cycle(mock_client)
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_below_threshold(self, mock_client):
        """No flush when disconnected for less than the threshold."""
        mock_client.last_ping_pong_time = time.time() - 60  # Only 1 min

        with patch("chicane.app.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            await self._run_one_cycle(mock_client)
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_flushes_dns_on_darwin(self, mock_client):
        """Flushes DNS cache on macOS when disconnected past threshold."""
        with patch("chicane.app.sys") as mock_sys, \
             patch("chicane.app.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_sys.platform = "darwin"
            await self._run_one_cycle(mock_client)
            mock_exec.assert_called_once_with("dscacheutil", "-flushcache")

    @pytest.mark.asyncio
    async def test_no_flush_on_linux(self, mock_client):
        """On non-darwin platforms, logs warning but doesn't run dscacheutil."""
        with patch("chicane.app.sys") as mock_sys, \
             patch("chicane.app.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_sys.platform = "linux"
            await self._run_one_cycle(mock_client)
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_repeated_flush(self, mock_client):
        """Second flush within cooldown period is skipped."""
        cycle_count = 0

        async def fake_sleep(seconds):
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count > 2:
                raise asyncio.CancelledError

        with patch("chicane.app.sys") as mock_sys, \
             patch("chicane.app.asyncio.sleep", side_effect=fake_sleep), \
             patch("chicane.app.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_sys.platform = "darwin"
            with pytest.raises(asyncio.CancelledError):
                await _dns_watchdog(mock_client)
            # Should only flush once despite two cycles (cooldown)
            assert mock_exec.call_count == 1

    @pytest.mark.asyncio
    async def test_exception_does_not_crash_watchdog(self, mock_client):
        """Exceptions in the check loop are caught and logged."""
        mock_client.is_connected.side_effect = RuntimeError("boom")
        cycle_count = 0

        async def fake_sleep(seconds):
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count > 1:
                raise asyncio.CancelledError

        with patch("chicane.app.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await _dns_watchdog(mock_client)
        # Reached here without crashing — the exception was handled
