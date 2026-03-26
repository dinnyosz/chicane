"""Tests for image detection and upload in handlers."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.handlers import (
    _collect_image_paths_from_tool_use,
    _extract_image_paths,
    _process_message,
    _upload_image,
    _upload_new_images,
)
from tests.conftest import make_event, make_tool_event, mock_client, mock_session_info, tool_block


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

    def test_ignores_bare_filename_without_path_prefix(self):
        """Bare filenames like 'chart.png' (no ./ or ../ prefix) are not matched."""
        text = "See chart.png for details"
        assert _extract_image_paths(text) == []

    def test_relative_dotslash_resolved_with_cwd(self, tmp_path):
        img = tmp_path / "output" / "graph.jpg"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"img")
        text = "See ./output/graph.jpg"
        result = _extract_image_paths(text, cwd=tmp_path)
        assert len(result) == 1
        assert result[0] == img.resolve()

    def test_relative_dotdotslash_resolved_with_cwd(self, tmp_path):
        sibling = tmp_path / "other-project" / "image.png"
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_bytes(b"img")
        sub = tmp_path / "project"
        sub.mkdir()
        text = "See ../other-project/image.png"
        result = _extract_image_paths(text, cwd=sub)
        assert len(result) == 1
        assert result[0] == sibling.resolve()

    def test_relative_path_without_cwd_ignored(self):
        """Relative paths are skipped when no cwd is provided."""
        text = "See ./output/graph.jpg and ../other/img.png"
        assert _extract_image_paths(text) == []

    def test_relative_nonexistent_ignored(self, tmp_path):
        text = "See ./does-not-exist.png"
        assert _extract_image_paths(text, cwd=tmp_path) == []

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

    def test_absolute_looking_path_resolved_relative_to_cwd(self, tmp_path):
        """Paths like /assets/img.png that look absolute but don't exist
        should be tried relative to cwd (project-relative paths from LLMs)."""
        img = tmp_path / "assets" / "generated" / "portrait.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"img")
        text = "See /assets/generated/portrait.png"
        result = _extract_image_paths(text, cwd=tmp_path)
        assert len(result) == 1
        assert result[0] == img

    def test_absolute_looking_path_no_cwd_ignored(self):
        """Absolute-looking paths that don't exist are still ignored without cwd."""
        text = "See /assets/generated/portrait.png"
        assert _extract_image_paths(text) == []

    def test_absolute_looking_path_not_on_disk_ignored(self, tmp_path):
        """Absolute-looking paths that don't exist even relative to cwd are ignored."""
        text = "See /assets/nonexistent/portrait.png"
        assert _extract_image_paths(text, cwd=tmp_path) == []

    def test_real_absolute_path_not_retried_relative(self, tmp_path):
        """A real absolute path that exists should be used as-is, not resolved
        against cwd (which could produce a wrong match)."""
        img = tmp_path / "real.png"
        img.write_bytes(b"img")
        # Also create a file at the cwd-relative location
        also = tmp_path / "cwd" / str(tmp_path).lstrip("/") / "real.png"
        also.parent.mkdir(parents=True, exist_ok=True)
        also.write_bytes(b"img")
        text = f"See {img}"
        result = _extract_image_paths(text, cwd=tmp_path / "cwd")
        assert result == [img]  # The real absolute path, not the cwd-relative one

    def test_multiple_project_relative_paths(self, tmp_path):
        """Multiple project-relative paths are all resolved against cwd."""
        a = tmp_path / "assets" / "a.png"
        b = tmp_path / "assets" / "b.jpg"
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_bytes(b"img")
        b.write_bytes(b"img")
        text = "Images: /assets/a.png and /assets/b.jpg"
        result = _extract_image_paths(text, cwd=tmp_path)
        assert len(result) == 2
        assert a in result
        assert b in result


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

    def test_bash_tool_bare_filename_not_collected(self):
        """Bash commands with bare filenames (no path prefix) are not matched."""
        event = make_tool_event(
            tool_block("Bash", command="convert input.pdf output.png")
        )
        assert _collect_image_paths_from_tool_use(event) == []

    def test_bash_tool_absolute_path_collected(self):
        """Bash commands with absolute image paths are collected."""
        event = make_tool_event(
            tool_block("Bash", command="python3 -c \"import matplotlib; plt.savefig('/tmp/chart.png')\"")
        )
        assert _collect_image_paths_from_tool_use(event) == ["/tmp/chart.png"]

    def test_bash_tool_multiple_image_paths(self):
        """Bash commands generating multiple images have all paths collected."""
        event = make_tool_event(
            tool_block(
                "Bash",
                command=(
                    "python3 script.py "
                    "--output /tmp/daily.png /tmp/hourly.jpg /tmp/monthly.png"
                ),
            )
        )
        result = _collect_image_paths_from_tool_use(event)
        assert result == ["/tmp/daily.png", "/tmp/hourly.jpg", "/tmp/monthly.png"]

    def test_bash_tool_savefig_pattern(self):
        """Matplotlib savefig() calls in Python one-liners are detected."""
        cmd = (
            "python3 -c \"\n"
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1,2,3])\n"
            "plt.savefig('/tmp/claude_summary_card.png')\n"
            "plt.savefig('/tmp/claude_daily_activity.png')\n"
            "\""
        )
        event = make_tool_event(tool_block("Bash", command=cmd))
        result = _collect_image_paths_from_tool_use(event)
        assert "/tmp/claude_summary_card.png" in result
        assert "/tmp/claude_daily_activity.png" in result

    def test_bash_tool_relative_path_collected(self):
        """Bash commands with relative image paths (./foo.png) are collected."""
        event = make_tool_event(
            tool_block("Bash", command="convert input.pdf ./output/chart.png")
        )
        assert _collect_image_paths_from_tool_use(event) == ["./output/chart.png"]

    def test_bash_tool_non_image_paths_ignored(self):
        """Bash commands with non-image paths are not collected."""
        event = make_tool_event(
            tool_block("Bash", command="python3 /tmp/script.py > /tmp/output.csv")
        )
        assert _collect_image_paths_from_tool_use(event) == []

    def test_bash_tool_mixed_with_write(self):
        """Bash and Write tool_use blocks are both collected."""
        event = make_tool_event(
            tool_block("Write", file_path="/tmp/icon.svg", content="<svg/>"),
            tool_block("Bash", command="python3 -c \"plt.savefig('/tmp/chart.png')\""),
        )
        result = _collect_image_paths_from_tool_use(event)
        assert result == ["/tmp/icon.svg", "/tmp/chart.png"]

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

    @pytest.mark.asyncio
    async def test_handles_slack_request_error(self, tmp_path):
        """SlackRequestError (5xx during upload) is caught gracefully."""
        from slack_sdk.errors import SlackRequestError

        img = tmp_path / "test.png"
        img.write_bytes(b"img")
        client = mock_client()
        client.files_upload_v2.side_effect = SlackRequestError(
            "Failed to upload a file (status: 500, body: None, "
            "filename: test.png, title: test.png)"
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

    @pytest.mark.asyncio
    async def test_uploads_relative_path_with_cwd(self, tmp_path):
        img = tmp_path / "output" / "result.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"img")
        client = mock_client()
        uploaded: set[str] = set()

        await _upload_new_images(
            client, "C1", "1.0", "See ./output/result.png",
            uploaded, cwd=tmp_path,
        )

        client.files_upload_v2.assert_called_once()
        assert str(img.resolve()) in uploaded


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestPostImagesConfig:
    def test_default_true(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        assert config.post_images is True

    def test_enabled(self):
        config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            post_images=True,
        )
        assert config.post_images is True


# ---------------------------------------------------------------------------
# Image upload during text flushes (integration tests via _process_message)
# ---------------------------------------------------------------------------


class TestImageUploadDuringFlush:
    """Verify images mentioned in text are uploaded even when text is flushed mid-stream."""

    @pytest.mark.asyncio
    async def test_image_in_tool_activity_flushed_text_is_uploaded(
        self, config, sessions, queue, tmp_path
    ):
        """Image path in text flushed before tool activities should still be uploaded."""
        img = tmp_path / "diagram.png"
        img.write_bytes(b"\x89PNG")

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            # Text mentioning an image, followed immediately by tool activity
            yield make_event("assistant", text=f"I created {img}")
            yield make_tool_event(tool_block("Read", file_path="/src/b.py"))
            # Tool result
            yield make_event(
                "user",
                message={"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "file contents",
                }]},
            )
            yield make_event("assistant", text="All done.")
            yield make_event("result", text="All done.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "go", client, config, sessions, queue)

        # Image should have been uploaded despite text being flushed before tool activity
        upload_calls = [
            c for c in client.files_upload_v2.call_args_list
            if c.kwargs.get("filename") == "diagram.png"
        ]
        assert len(upload_calls) == 1

    @pytest.mark.asyncio
    async def test_image_in_idle_flushed_text_is_uploaded(
        self, config, sessions, queue, tmp_path
    ):
        """Image path in text that gets idle-flushed should still be uploaded."""
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x89PNG")

        barrier = asyncio.Event()
        text_yielded = asyncio.Event()

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text=f"Here is the chart: {img}")
            text_yielded.set()
            # SDK blocks — idle timer will fire and flush text
            await barrier.wait()
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            task = asyncio.create_task(
                _process_message(event, "go", client, config, sessions, queue)
            )
            await text_yielded.wait()
            # Let idle flush fire (asyncio.sleep is mocked to instant)
            for _ in range(10):
                await asyncio.sleep(0)

            # Image should have been uploaded during idle flush
            upload_calls = [
                c for c in client.files_upload_v2.call_args_list
                if c.kwargs.get("filename") == "chart.png"
            ]
            assert len(upload_calls) == 1

            barrier.set()
            await task

    @pytest.mark.asyncio
    async def test_image_dedup_across_flush_and_final(
        self, config, sessions, queue, tmp_path
    ):
        """Same image path in flushed text and final text should be uploaded only once."""
        img = tmp_path / "result.png"
        img.write_bytes(b"\x89PNG")

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text=f"Created {img}")
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_event(
                "user",
                message={"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "file contents",
                }]},
            )
            # Same image mentioned again in final text
            yield make_event("assistant", text=f"See {img} for the output.")
            yield make_event("result", text=f"See {img} for the output.")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "go", client, config, sessions, queue)

        upload_calls = [
            c for c in client.files_upload_v2.call_args_list
            if c.kwargs.get("filename") == "result.png"
        ]
        assert len(upload_calls) == 1

    @pytest.mark.asyncio
    async def test_no_upload_when_post_images_disabled(
        self, sessions, queue, tmp_path
    ):
        """When post_images=False, no image scanning at flush points."""
        no_img_config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["UHUMAN1"],
            post_images=False,
            rate_limit=10000,
        )
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x89PNG")

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            yield make_event("assistant", text=f"Created {img}")
            yield make_tool_event(tool_block("Read", file_path="/src/a.py"))
            yield make_event(
                "user",
                message={"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "file contents",
                }]},
            )
            yield make_event("result", text="")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "1000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(
                event, "go", client, no_img_config, sessions, queue
            )

        # No image uploads should have happened
        img_uploads = [
            c for c in client.files_upload_v2.call_args_list
            if c.kwargs.get("filename", "").endswith(".png")
        ]
        assert len(img_uploads) == 0
