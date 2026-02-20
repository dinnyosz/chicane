"""Tests for _process_message core logic: formatting, error paths, reconnection."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import save_handoff_session, load_handoff_session
from chicane.handlers import _process_message
from tests.conftest import make_event, mock_client, mock_session_info


class TestProcessMessageFormatting:
    """Test that _process_message preserves newlines from streamed text."""

    @pytest.mark.asyncio
    async def test_streamed_text_with_newlines_not_overwritten_by_result(
        self, config, sessions, queue
    ):
        """The result event often flattens newlines. Streamed text should win."""
        streamed = "First paragraph.\n\nSecond paragraph.\n\n- bullet 1\n- bullet 2"
        flat_result = "First paragraph. Second paragraph. - bullet 1 - bullet 2"

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="sess-1")
            yield make_event("assistant", text=streamed)
            yield make_event("result", text=flat_result)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "sess-1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "1000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions, queue)

        # Find the posted text (not the session init or completion summary)
        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "").startswith("First")
        ]
        assert len(text_posts) == 1
        assert "\n\n" in text_posts[0].kwargs["text"]
        # Bullets get converted from - to • by _markdown_to_mrkdwn
        expected = streamed.replace("- bullet", "• bullet")
        assert text_posts[0].kwargs["text"] == expected

    @pytest.mark.asyncio
    async def test_result_text_used_when_no_streamed_content(
        self, config, sessions, queue
    ):
        """When no assistant events arrive, fall back to result text."""
        result_text = "Fallback result text."

        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="sess-2")
            yield make_event("result", text=result_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "sess-2"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "1001.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello", client, config, sessions, queue)

        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == result_text
        ]
        assert len(text_posts) == 1


class TestProcessMessageEdgeCases:
    """Test _process_message error paths and edge cases."""

    @pytest.mark.asyncio
    async def test_handoff_session_id_extracted(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-123"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create:
            event = {"ts": "5000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(
                event,
                "do stuff (session_id: abc-123)",
                client, config, sessions, queue,
            )
            assert mock_create.call_args.kwargs["session_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_reaction_add_failure_doesnt_block(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        client.reactions_add.side_effect = Exception("permission denied")

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5001.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        client.chat_postMessage.assert_called()

    @pytest.mark.asyncio
    async def test_empty_response_posts_warning(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5002.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        warning_posts = [
            c for c in client.chat_postMessage.call_args_list
            if "empty response" in c.kwargs.get("text", "").lower()
        ]
        assert len(warning_posts) == 1

    @pytest.mark.asyncio
    async def test_stream_exception_posts_error(self, config, sessions, queue):
        async def exploding_stream(prompt):
            yield make_event("system", subtype="init", session_id="s1")
            raise RuntimeError("stream exploded")

        mock_session = MagicMock()
        mock_session.stream = exploding_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5003.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        error_posts = [
            c for c in client.chat_postMessage.call_args_list
            if ":x: Error" in c.kwargs.get("text", "")
        ]
        assert len(error_posts) == 1
        error_text = error_posts[0].kwargs["text"]
        assert ":x: Error (RuntimeError)" in error_text
        assert "Check bot logs" in error_text
        # Ensure internal error message is NOT leaked to Slack
        assert "stream exploded" not in error_text

    @pytest.mark.asyncio
    async def test_long_response_uploaded_as_snippet(self, config, sessions, queue):
        """Responses exceeding SNIPPET_THRESHOLD are uploaded as a file snippet."""
        long_text = "a" * 8000

        async def fake_stream(prompt):
            yield make_event("result", text=long_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Snippet uploaded via files_upload_v2
        client.files_upload_v2.assert_called_once()
        upload_kwargs = client.files_upload_v2.call_args.kwargs
        assert upload_kwargs["channel"] == "C_CHAN"

    @pytest.mark.asyncio
    async def test_moderate_response_split_into_chunks(self, config, sessions, queue):
        """Responses between SLACK_MAX_LENGTH and SNIPPET_THRESHOLD still chunk."""
        # 3950 chars: above SLACK_MAX_LENGTH (3900) but below SNIPPET_THRESHOLD (4000)
        text = "a" * 3950

        async def fake_stream(prompt):
            yield make_event("result", text=text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5004.1", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        # Should be split into multiple messages, not uploaded as snippet
        client.files_upload_v2.assert_not_called()
        # At least 2 chat_postMessage calls for the split text
        assert client.chat_postMessage.call_count >= 2

    @pytest.mark.asyncio
    async def test_text_only_response_posted_as_reply(self, config, sessions, queue):
        """When there are no tool calls, the final text is posted as a thread reply."""
        chunk_text = "x" * 150

        async def fake_stream(prompt):
            yield make_event("assistant", text=chunk_text)
            yield make_event("result", text=chunk_text)

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {"ts": "5005.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

        text_posts = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("text", "") == chunk_text
        ]
        assert len(text_posts) == 1
        client.chat_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_rebuilds_context(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "s1"

        client = mock_client()
        client.auth_test.return_value = {"user_id": "UBOT123"}
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "6000.0", "text": "original question"},
                {"user": "UBOT123", "ts": "6001.0", "text": "original answer"},
            ]
        }
        client.conversations_history.return_value = {"messages": []}

        captured_prompt = None

        async def capturing_stream(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            yield make_event("result", text="ok")

        mock_session.stream = capturing_stream

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)):
            event = {
                "ts": "6002.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "follow up", client, config, sessions, queue)

        assert captured_prompt is not None
        assert "conversation history" in captured_prompt
        assert "follow up" in captured_prompt

    @pytest.mark.asyncio
    async def test_reconnect_finds_session_id(self, config, sessions, queue):
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-123-def"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "7000.0",
                    "text": "Handoff _(session_id: abc-123-def)_",
                },
            ]
        }

        with patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create:
            event = {
                "ts": "7001.0",
                "thread_ts": "7000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions, queue)

            assert mock_create.call_args.kwargs["session_id"] == "abc-123-def"

    @pytest.mark.asyncio
    async def test_reconnect_finds_session_alias(self, config, sessions, queue, tmp_path):
        """Reconnect resolves a funky alias to the real session_id."""
        async def fake_stream(prompt):
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "real-uuid-here"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "8000.0",
                    "text": "Handoff _(session: sneaky-octopus-pizza)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("sneaky-octopus-pizza", "real-uuid-here")

            event = {
                "ts": "8001.0",
                "thread_ts": "8000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions, queue)

            assert mock_create.call_args.kwargs["session_id"] == "real-uuid-here"

    @pytest.mark.asyncio
    async def test_reconnect_with_alias_announces_continuing(self, config, sessions, queue, tmp_path):
        """When reconnecting via alias, 'Continuing session' is posted
        with the original alias name."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="real-uuid-here")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "real-uuid-here"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "8000.0",
                    "text": "Handoff _(session: sneaky-octopus-pizza)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
        ):
            save_handoff_session("sneaky-octopus-pizza", "real-uuid-here")

            event = {
                "ts": "8001.0",
                "thread_ts": "8000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "continue", client, config, sessions, queue)

            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "Continuing session" in text
            assert "sneaky-octopus-pizza" in text

    @pytest.mark.asyncio
    async def test_reconnect_finds_bot_session_message(self, config, sessions, queue, tmp_path):
        """The bot's own ':sparkles: New session' message contains
        _(session: alias)_ and should be found on reconnect."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="bot-sess-id")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "bot-sess-id"

        client = mock_client()
        # Thread contains the bot's own session announcement (not a handoff)
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "UHUMAN1", "ts": "7000.0", "text": "hey bot"},
                {
                    "user": "UBOT123",
                    "ts": "7001.0",
                    "text": ":sparkles: New session\n_(session: clever-fox-rainbow)_",
                },
                {"user": "UBOT123", "ts": "7002.0", "text": "Here's the answer"},
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("clever-fox-rainbow", "bot-sess-id")

            event = {
                "ts": "7003.0",
                "thread_ts": "7000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "follow up", client, config, sessions, queue)

            # Should have found the session_id from the bot's own message
            assert mock_create.call_args.kwargs["session_id"] == "bot-sess-id"

            # Should announce "Continuing session" with the alias
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            assert "clever-fox-rainbow" in continuing_posts[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_reconnect_picks_last_session_in_thread(self, config, sessions, queue, tmp_path):
        """When a thread has multiple session aliases (e.g. bot restarted),
        the most recent one should be used."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="second-sess")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "second-sess"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: New session\n_(session: old-dusty-parrot)_",
                },
                {"user": "UBOT123", "ts": "6001.0", "text": "first response"},
                {
                    "user": "UBOT123",
                    "ts": "6002.0",
                    "text": ":sparkles: New session\n_(session: fresh-shiny-eagle)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("old-dusty-parrot", "first-sess")
            save_handoff_session("fresh-shiny-eagle", "second-sess")

            event = {
                "ts": "6003.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "pick up", client, config, sessions, queue)

            # Should have used the LAST session (fresh-shiny-eagle)
            assert mock_create.call_args.kwargs["session_id"] == "second-sess"

            # Should announce continuing with the most recent alias
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "fresh-shiny-eagle" in text
            # Should mention the skipped older session
            assert "old-dusty-parrot" in text

    @pytest.mark.asyncio
    async def test_reconnect_duplicate_alias_not_shown_as_skipped(self, config, sessions, queue, tmp_path):
        """When the same alias appears multiple times in a thread (e.g. from
        the original handoff + a previous reconnect message), the duplicate
        should NOT be displayed as 'skipped older'."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="the-sess")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "the-sess"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: Handoff\n_(session: gardening-ruby-scroll)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "6001.0",
                    "text": ":arrows_counterclockwise: Continuing session _gardening-ruby-scroll_\n_(session: gardening-ruby-scroll)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("gardening-ruby-scroll", "the-sess")

            event = {
                "ts": "6003.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello again", client, config, sessions, queue)

            assert mock_create.call_args.kwargs["session_id"] == "the-sess"

            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "gardening-ruby-scroll" in text
            # The duplicate alias should NOT appear as "skipped older"
            assert "skipped older" not in text

    @pytest.mark.asyncio
    async def test_reconnect_unmapped_alias_warns(self, config, sessions, queue, tmp_path):
        """When reconnecting and the alias can't be mapped, a warning is
        shown and a new session starts."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="brand-new-id")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "brand-new-id"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: New session\n_(session: lost-ghost-cat)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            # Don't save lost-ghost-cat — it's unmapped

            event = {
                "ts": "6001.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "hello again", client, config, sessions, queue)

            # No session_id should be passed (couldn't map)
            assert mock_create.call_args.kwargs.get("session_id") is None

            # Should show warning about unmapped alias
            warning_posts = [
                c for c in client.chat_postMessage.call_args_list
                if "session map lost" in c.kwargs.get("text", "")
            ]
            assert len(warning_posts) == 1
            text = warning_posts[0].kwargs["text"]
            assert "lost-ghost-cat" in text

    @pytest.mark.asyncio
    async def test_reconnect_fallback_to_older_session(self, config, sessions, queue, tmp_path):
        """When the newest alias can't be mapped, fall back to the next
        older one and mention the unmapped one."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="old-good-sess")
            yield make_event("result", text="ok")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "old-good-sess"

        client = mock_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "UBOT123",
                    "ts": "6000.0",
                    "text": ":sparkles: New session\n_(session: old-good-parrot)_",
                },
                {
                    "user": "UBOT123",
                    "ts": "6001.0",
                    "text": ":sparkles: New session\n_(session: new-lost-eagle)_",
                },
            ]
        }

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)) as mock_create,
        ):
            save_handoff_session("old-good-parrot", "old-good-sess")
            # Don't save new-lost-eagle — it's unmapped

            event = {
                "ts": "6002.0",
                "thread_ts": "6000.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event, "pick up", client, config, sessions, queue)

            # Should have fallen back to old-good-parrot
            assert mock_create.call_args.kwargs["session_id"] == "old-good-sess"

            # Should announce continuing AND mention the unmapped one
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "old-good-parrot" in text
            assert "new-lost-eagle" in text
            assert "couldn't map" in text

    @pytest.mark.asyncio
    async def test_new_session_saves_alias_and_announces(self, config, sessions, queue, tmp_path):
        """When a new session starts (init event), an alias is generated,
        saved to disk, and announced as a new session in the thread."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="new-sess-id")
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "new-sess-id"

        client = mock_client()

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
        ):
            event = {"ts": "9000.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event, "hello", client, config, sessions, queue)

            # Should have posted the "New session" announcement
            alias_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":sparkles:" in c.kwargs.get("text", "")
            ]
            assert len(alias_posts) == 1
            alias_text = alias_posts[0].kwargs["text"]
            assert "New session" in alias_text
            # Must contain the scannable (session: alias) format
            m = re.search(r"\(session:\s*([a-z]+(?:-[a-z]+)+)\)", alias_text)
            assert m, f"No scannable session alias found in: {alias_text}"
            alias = m.group(1)

            # Alias should be saved to disk, mapping to the real session_id
            assert load_handoff_session(alias) == "new-sess-id"

    @pytest.mark.asyncio
    async def test_handoff_session_announces_continuing(self, config, sessions, queue, tmp_path):
        """When resuming a handoff session, a 'Continuing session' message
        should be posted with the alias."""
        async def fake_stream(prompt):
            yield make_event("system", subtype="init", session_id="abc-def-123")
            yield make_event("result", text="done")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "abc-def-123"

        client = mock_client()

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=mock_session_info(mock_session)),
        ):
            event = {"ts": "9100.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(
                event,
                "continue (session_id: abc-def-123)",
                client, config, sessions, queue,
            )

            # Should have posted a "Continuing session" announcement
            continuing_posts = [
                c for c in client.chat_postMessage.call_args_list
                if ":arrows_counterclockwise:" in c.kwargs.get("text", "")
            ]
            assert len(continuing_posts) == 1
            text = continuing_posts[0].kwargs["text"]
            assert "Continuing session" in text
            # Must contain the scannable (session: alias) format
            m = re.search(r"\(session:\s*([a-z]+(?:-[a-z]+)+)\)", text)
            assert m, f"No scannable session alias found in: {text}"

    @pytest.mark.asyncio
    async def test_repeated_init_events_do_not_generate_new_alias(
        self, config, sessions, queue, tmp_path
    ):
        """When the SDK emits init on every query(), only the first should
        generate an alias.  Regression test for duplicate session aliases."""

        call_count = 0

        async def fake_stream(prompt):
            nonlocal call_count
            call_count += 1
            yield make_event("system", subtype="init", session_id="same-sess-id")
            yield make_event("result", text=f"response {call_count}")

        mock_session = MagicMock()
        mock_session.stream = fake_stream
        mock_session.session_id = "same-sess-id"

        client = mock_client()
        info = mock_session_info(mock_session)

        with (
            patch("chicane.config._HANDOFF_MAP_FILE", tmp_path / "sessions.json"),
            patch.object(sessions, "get_or_create", return_value=info),
        ):
            # First message — should generate alias
            event1 = {"ts": "9200.0", "channel": "C_CHAN", "user": "UHUMAN1"}
            await _process_message(event1, "hello", client, config, sessions, queue)

            alias_posts_1 = [
                c for c in client.chat_postMessage.call_args_list
                if ":sparkles:" in c.kwargs.get("text", "")
            ]
            assert len(alias_posts_1) == 1
            first_alias = info.session_alias

            client.reset_mock()
            client.chat_postMessage.return_value = {"ts": "9999.0"}

            # Second message in same session — should NOT generate a new alias
            event2 = {
                "ts": "9201.0",
                "thread_ts": "9200.0",
                "channel": "C_CHAN",
                "user": "UHUMAN1",
            }
            await _process_message(event2, "follow up", client, config, sessions, queue)

            alias_posts_2 = [
                c for c in client.chat_postMessage.call_args_list
                if ":sparkles:" in c.kwargs.get("text", "")
            ]
            assert len(alias_posts_2) == 0
            # Alias should not have changed
            assert info.session_alias == first_alias
