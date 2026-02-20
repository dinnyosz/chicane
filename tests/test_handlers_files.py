"""Tests for file download and file attachment handling in messages."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.handlers import _download_files, _process_message, MAX_FILE_SIZE
from tests.conftest import make_event, mock_client, mock_session_info


def _mock_http_session(responses):
    """Build a mock aiohttp.ClientSession that yields *responses* in order.

    Each *response* is a ``(status, data)`` or ``(status, data, content_type)``
    tuple where *data* is the bytes returned by ``resp.read()``.
    """
    resp_iter = iter(responses)

    class _FakeResp:
        def __init__(self, status, data, content_type=None):
            self.status = status
            self._data = data
            self.content_type = content_type or "application/octet-stream"

        async def read(self):
            return self._data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeSession:
        def get(self, url, **kw):
            entry = next(resp_iter)
            status, data = entry[0], entry[1]
            ct = entry[2] if len(entry) > 2 else None
            return _FakeResp(status, data, ct)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    return _FakeSession()


class TestDownloadFiles:
    """Test _download_files helper that downloads Slack file attachments."""

    @pytest.mark.asyncio
    async def test_no_files_returns_empty(self, tmp_path):
        event = {"ts": "1.0", "text": "hello"}
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_files_list_returns_empty(self, tmp_path):
        event = {"ts": "1.0", "text": "hello", "files": []}
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_downloads_file_to_target_dir(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "test.py",
                    "mimetype": "text/x-python",
                    "url_private_download": "https://files.slack.com/test.py",
                    "size": 100,
                }
            ],
        }
        file_content = b"print('hello')"
        mock_sess = _mock_http_session([(200, file_content)])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert len(result) == 1
        name, path, mime = result[0]
        assert name == "test.py"
        assert path.exists()
        assert path.read_bytes() == file_content
        assert mime == "text/x-python"

    @pytest.mark.asyncio
    async def test_skips_file_without_download_url(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "nourl.txt",
                    "mimetype": "text/plain",
                    "size": 10,
                }
            ],
        }
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_oversized_file(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "huge.bin",
                    "mimetype": "application/octet-stream",
                    "url_private_download": "https://files.slack.com/huge.bin",
                    "size": MAX_FILE_SIZE + 1,
                }
            ],
        }
        result = await _download_files(event, "xoxb-token", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_file_on_http_error(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "secret.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/secret.txt",
                    "size": 50,
                }
            ],
        }
        mock_sess = _mock_http_session([(403, b"")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_file_when_response_is_html(self, tmp_path):
        """Slack returns HTML instead of file data when files:read scope is missing."""
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "photo.png",
                    "mimetype": "image/png",
                    "url_private_download": "https://files.slack.com/photo.png",
                    "size": 5000,
                }
            ],
        }
        html_page = b"<!DOCTYPE html><html>login page</html>"
        mock_sess = _mock_http_session([(200, html_page, "text/html")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert result == []
        assert not (tmp_path / "photo.png").exists()

    @pytest.mark.asyncio
    async def test_duplicate_filenames_get_suffixed(self, tmp_path):
        (tmp_path / "report.csv").write_text("existing")

        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "report.csv",
                    "mimetype": "text/csv",
                    "url_private_download": "https://files.slack.com/report.csv",
                    "size": 20,
                }
            ],
        }
        file_content = b"new,data"
        mock_sess = _mock_http_session([(200, file_content)])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert len(result) == 1
        _, path, _ = result[0]
        assert path.name == "report_1.csv"
        assert path.read_bytes() == file_content
        assert (tmp_path / "report.csv").read_text() == "existing"

    @pytest.mark.asyncio
    async def test_skips_file_on_download_exception(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "boom.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/boom.txt",
                    "size": 10,
                }
            ],
        }

        class _ExplodingSession:
            def get(self, url, **kw):
                raise Exception("connection reset")
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=_ExplodingSession()):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert result == []

    @pytest.mark.asyncio
    async def test_downloads_multiple_files(self, tmp_path):
        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "a.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/a.txt",
                    "size": 5,
                },
                {
                    "name": "b.png",
                    "mimetype": "image/png",
                    "url_private_download": "https://files.slack.com/b.png",
                    "size": 10,
                },
            ],
        }
        mock_sess = _mock_http_session([(200, b"aaa"), (200, b"png-data")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", tmp_path)

        assert len(result) == 2
        assert result[0][0] == "a.txt"
        assert result[1][0] == "b.png"
        assert result[1][2] == "image/png"

    @pytest.mark.asyncio
    async def test_creates_target_dir_if_missing(self, tmp_path):
        target = tmp_path / "subdir" / "files"
        assert not target.exists()

        event = {
            "ts": "1.0",
            "files": [
                {
                    "name": "hello.txt",
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/hello.txt",
                    "size": 5,
                }
            ],
        }
        mock_sess = _mock_http_session([(200, b"hello")])

        with patch("chicane.handlers.aiohttp.ClientSession", return_value=mock_sess):
            result = await _download_files(event, "xoxb-token", target)

        assert target.exists()
        assert len(result) == 1


class TestProcessMessageWithFiles:
    """Test that file attachments are downloaded and injected into the prompt."""

    @pytest.mark.asyncio
    async def test_files_appended_to_prompt(self, config, sessions, queue, tmp_path):
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"
        mock_session.cwd = tmp_path

        client = mock_client()

        downloaded = [
            ("screenshot.png", tmp_path / "screenshot.png", "image/png"),
        ]

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=downloaded),
        ):
            event = {
                "ts": "8000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "files": [{"name": "screenshot.png"}],
            }
            await _process_message(event, "what's wrong here?", client, config, sessions, queue)

        assert captured_prompt is not None
        assert "what's wrong here?" in captured_prompt
        assert "Read tool" in captured_prompt
        assert "screenshot.png" in captured_prompt
        assert "Image:" in captured_prompt

    @pytest.mark.asyncio
    async def test_text_files_labeled_as_file(self, config, sessions, queue, tmp_path):
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"
        mock_session.cwd = tmp_path

        client = mock_client()

        downloaded = [
            ("main.py", tmp_path / "main.py", "text/x-python"),
        ]

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=downloaded),
        ):
            event = {
                "ts": "8001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "files": [{"name": "main.py"}],
            }
            await _process_message(event, "review this code", client, config, sessions, queue)

        assert "File:" in captured_prompt
        assert "Image:" not in captured_prompt

    @pytest.mark.asyncio
    async def test_file_only_no_text(self, config, sessions, queue, tmp_path):
        """When a user sends only a file with no text, the prompt should still work."""
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"
        mock_session.cwd = tmp_path

        client = mock_client()

        downloaded = [
            ("error.log", tmp_path / "error.log", "text/plain"),
        ]

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=downloaded),
        ):
            event = {
                "ts": "8002.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
                "files": [{"name": "error.log"}],
            }
            await _process_message(event, "", client, config, sessions, queue)

        assert captured_prompt is not None
        assert "error.log" in captured_prompt
        assert "Read tool" in captured_prompt

    @pytest.mark.asyncio
    async def test_no_files_prompt_unchanged(self, config, sessions, queue):
        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = capturing_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with (
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
            patch("chicane.handlers._download_files", new_callable=AsyncMock, return_value=[]),
        ):
            event = {
                "ts": "8003.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "just text", client, config, sessions, queue)

        assert captured_prompt == "just text"
