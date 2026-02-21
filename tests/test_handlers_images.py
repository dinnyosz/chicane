"""Tests for image detection and upload in handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.handlers import (
    _collect_image_paths_from_tool_use,
    _extract_image_paths,
    _upload_image,
    _upload_new_images,
)
from tests.conftest import make_event, make_tool_event, mock_client, tool_block


# ---------------------------------------------------------------------------
# _extract_image_paths
# ---------------------------------------------------------------------------


class TestExtractImagePaths:
    def test_absolute_png_path(self, tmp_path):
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x89PNG")
        text = f"I created the chart at {img}"
        assert _extract_image_paths(text) == [img]

    def test_multiple_images(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.jpg"
        a.write_bytes(b"img")
        b.write_bytes(b"img")
        text = f"See {a} and also {b} for details."
        result = _extract_image_paths(text)
        assert result == [a, b]

    def test_deduplicates_same_path(self, tmp_path):
        img = tmp_path / "dup.png"
        img.write_bytes(b"img")
        text = f"First: {img}\nAgain: {img}"
        assert len(_extract_image_paths(text)) == 1

    def test_ignores_relative_paths(self):
        text = "See chart.png and ./output/graph.jpg"
        assert _extract_image_paths(text) == []

    def test_ignores_nonexistent_files(self):
        text = "See /tmp/nonexistent_abc123_image.png"
        assert _extract_image_paths(text) == []

    def test_various_extensions(self, tmp_path):
        extensions = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff"]
        expected = []
        parts = []
        for ext in extensions:
            f = tmp_path / f"file{ext}"
            f.write_bytes(b"data")
            expected.append(f)
            parts.append(str(f))
        text = " ".join(parts)
        assert _extract_image_paths(text) == expected

    def test_case_insensitive_extension(self, tmp_path):
        img = tmp_path / "photo.PNG"
        img.write_bytes(b"img")
        text = f"Image at {img}"
        assert _extract_image_paths(text) == [img]

    def test_non_image_extension_ignored(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_bytes(b"data")
        text = f"File at {f}"
        assert _extract_image_paths(text) == []

    def test_path_with_spaces_not_matched(self, tmp_path):
        """Paths with spaces are not matched — file paths should not contain spaces."""
        d = tmp_path / "my project"
        d.mkdir()
        img = d / "screenshot.png"
        img.write_bytes(b"img")
        text = f"See {img}"
        assert _extract_image_paths(text) == []

    def test_path_with_hyphens_and_dots(self, tmp_path):
        img = tmp_path / "my-chart.v2.png"
        img.write_bytes(b"img")
        text = f"Created {img}"
        assert _extract_image_paths(text) == [img]


# ---------------------------------------------------------------------------
# _collect_image_paths_from_tool_use
# ---------------------------------------------------------------------------


class TestCollectImagePathsFromToolUse:
    def test_write_tool_with_image_path(self):
        event = make_tool_event(
            tool_block("Write", file_path="/tmp/chart.png", content="<svg>...</svg>")
        )
        assert _collect_image_paths_from_tool_use(event) == ["/tmp/chart.png"]

    def test_write_tool_with_non_image_path(self):
        event = make_tool_event(
            tool_block("Write", file_path="/tmp/data.py", content="print('hi')")
        )
        assert _collect_image_paths_from_tool_use(event) == []

    def test_notebook_edit_with_image_path(self):
        event = make_tool_event(
            tool_block("NotebookEdit", notebook_path="/tmp/output.png", new_source="...")
        )
        assert _collect_image_paths_from_tool_use(event) == ["/tmp/output.png"]

    def test_multiple_write_tools(self):
        event = make_tool_event(
            tool_block("Write", file_path="/tmp/a.png", content="a"),
            tool_block("Write", file_path="/tmp/b.jpg", content="b"),
            tool_block("Write", file_path="/tmp/c.py", content="c"),
        )
        result = _collect_image_paths_from_tool_use(event)
        assert result == ["/tmp/a.png", "/tmp/b.jpg"]

    def test_non_assistant_event_returns_empty(self):
        event = make_event("user")
        assert _collect_image_paths_from_tool_use(event) == []

    def test_bash_tool_not_collected(self):
        """Bash tool doesn't have a predictable file_path — not collected here."""
        event = make_tool_event(
            tool_block("Bash", command="convert input.pdf output.png")
        )
        assert _collect_image_paths_from_tool_use(event) == []

    def test_case_insensitive_extension(self):
        event = make_tool_event(
            tool_block("Write", file_path="/tmp/PHOTO.JPG", content="data")
        )
        assert _collect_image_paths_from_tool_use(event) == ["/tmp/PHOTO.JPG"]


# ---------------------------------------------------------------------------
# _upload_image
# ---------------------------------------------------------------------------


class TestUploadImage:
    @pytest.mark.asyncio
    async def test_calls_files_upload_v2(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(b"img")
        client = mock_client()

        await _upload_image(client, "C123", "1000.0", img)

        client.files_upload_v2.assert_called_once_with(
            file=str(img),
            filename="test.png",
            title="test.png",
            channel="C123",
            thread_ts="1000.0",
            initial_comment=":frame_with_picture: `test.png`",
        )

    @pytest.mark.asyncio
    async def test_handles_upload_failure(self, tmp_path):
        from slack_sdk.errors import SlackApiError

        img = tmp_path / "test.png"
        img.write_bytes(b"img")
        client = mock_client()
        client.files_upload_v2.side_effect = SlackApiError(
            message="upload_error", response=MagicMock(status_code=500)
        )
        queue = MagicMock()
        queue.post_message = AsyncMock()

        await _upload_image(client, "C123", "1000.0", img, queue)

        queue.post_message.assert_called_once_with(
            "C123", "1000.0",
            ":frame_with_picture: `test.png` (upload failed)",
        )


# ---------------------------------------------------------------------------
# _upload_new_images
# ---------------------------------------------------------------------------


class TestUploadNewImages:
    @pytest.mark.asyncio
    async def test_uploads_images_from_text(self, tmp_path):
        img = tmp_path / "result.png"
        img.write_bytes(b"img")
        client = mock_client()
        uploaded: set[str] = set()

        await _upload_new_images(client, "C1", "1.0", f"See {img}", uploaded)

        client.files_upload_v2.assert_called_once()
        assert str(img) in uploaded

    @pytest.mark.asyncio
    async def test_skips_already_uploaded(self, tmp_path):
        img = tmp_path / "result.png"
        img.write_bytes(b"img")
        client = mock_client()
        uploaded = {str(img)}

        await _upload_new_images(client, "C1", "1.0", f"See {img}", uploaded)

        client.files_upload_v2.assert_not_called()

    @pytest.mark.asyncio
    async def test_uploads_multiple_images(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.jpg"
        a.write_bytes(b"img")
        b.write_bytes(b"img")
        client = mock_client()
        uploaded: set[str] = set()

        await _upload_new_images(
            client, "C1", "1.0", f"First {a} then {b}", uploaded,
        )

        assert client.files_upload_v2.call_count == 2
        assert str(a) in uploaded
        assert str(b) in uploaded

    @pytest.mark.asyncio
    async def test_no_images_in_text(self):
        client = mock_client()
        uploaded: set[str] = set()

        await _upload_new_images(
            client, "C1", "1.0", "No images here.", uploaded,
        )

        client.files_upload_v2.assert_not_called()
        assert len(uploaded) == 0


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestPostImagesConfig:
    def test_default_false(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        assert config.post_images is False

    def test_enabled(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            post_images=True,
        )
        assert config.post_images is True
