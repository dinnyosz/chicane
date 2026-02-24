"""Tests for AskUserQuestion handling — SDK canUseTool + Slack interception."""

import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.handlers import (
    _format_question_blocks,
    _make_ask_user_callback,
    _parse_question_answer,
)
from chicane.sessions import SessionInfo, SessionStore
from chicane.claude import ClaudeSession
from chicane.slack_queue import SlackMessageQueue


# ---------------------------------------------------------------------------
# Question formatting
# ---------------------------------------------------------------------------


class TestFormatQuestionBlocks:
    """Tests for _format_question_blocks."""

    def test_single_question_formatting(self):
        questions = [
            {
                "question": "Which language?",
                "header": "Language",
                "options": [
                    {"label": "Python", "description": "Dynamic typed"},
                    {"label": "Rust", "description": "Systems language"},
                ],
                "multiSelect": False,
            }
        ]
        text, blocks = _format_question_blocks(questions)
        assert "*Language:*" in text
        assert "Which language?" in text
        assert "1. *Python*" in text
        assert "2. *Rust*" in text
        assert "Reply in this thread" in text
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"

    def test_multi_select_note(self):
        questions = [
            {
                "question": "Which features?",
                "header": "Features",
                "options": [
                    {"label": "Auth", "description": "Authentication"},
                    {"label": "Cache", "description": "Caching layer"},
                ],
                "multiSelect": True,
            }
        ]
        text, _ = _format_question_blocks(questions)
        assert "select multiple" in text

    def test_multiple_questions(self):
        questions = [
            {
                "question": "Q1?",
                "header": "First",
                "options": [{"label": "A", "description": "a"}],
                "multiSelect": False,
            },
            {
                "question": "Q2?",
                "header": "Second",
                "options": [{"label": "B", "description": "b"}],
                "multiSelect": False,
            },
        ]
        text, _ = _format_question_blocks(questions)
        assert "*First:*" in text
        assert "*Second:*" in text


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------


class TestParseQuestionAnswer:
    """Tests for _parse_question_answer."""

    def test_numeric_single_answer(self):
        questions = [
            {
                "question": "Which DB?",
                "options": [
                    {"label": "PostgreSQL"},
                    {"label": "SQLite"},
                ],
            }
        ]
        answers = _parse_question_answer("1", questions)
        assert answers == {"Which DB?": "PostgreSQL"}

    def test_numeric_multi_answer(self):
        questions = [
            {
                "question": "Which features?",
                "options": [
                    {"label": "Auth"},
                    {"label": "Cache"},
                    {"label": "Logging"},
                ],
            }
        ]
        answers = _parse_question_answer("1, 3", questions)
        assert answers == {"Which features?": "Auth, Logging"}

    def test_free_text_answer(self):
        questions = [
            {
                "question": "Which framework?",
                "options": [
                    {"label": "Django"},
                    {"label": "Flask"},
                ],
            }
        ]
        answers = _parse_question_answer("FastAPI", questions)
        assert answers == {"Which framework?": "FastAPI"}

    def test_multi_question_multi_line(self):
        questions = [
            {
                "question": "Q1?",
                "options": [{"label": "A"}, {"label": "B"}],
            },
            {
                "question": "Q2?",
                "options": [{"label": "X"}, {"label": "Y"}],
            },
        ]
        answers = _parse_question_answer("1\n2", questions)
        assert answers == {"Q1?": "A", "Q2?": "Y"}

    def test_fewer_lines_than_questions_reuses_last(self):
        questions = [
            {"question": "Q1?", "options": [{"label": "A"}]},
            {"question": "Q2?", "options": [{"label": "B"}]},
        ]
        answers = _parse_question_answer("custom answer", questions)
        assert answers["Q1?"] == "custom answer"
        assert answers["Q2?"] == "custom answer"

    def test_empty_reply_gives_empty_strings(self):
        questions = [{"question": "Q?", "options": [{"label": "A"}]}]
        answers = _parse_question_answer("", questions)
        assert answers == {"Q?": ""}


# ---------------------------------------------------------------------------
# AskUserQuestion interception in _process_message
# ---------------------------------------------------------------------------


class TestAskUserInterception:
    """Test that pending_question future intercepts incoming messages."""

    @pytest.fixture
    def config(self):
        return Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_users=["UHUMAN"],
        )

    @pytest.fixture
    def sessions(self):
        return SessionStore()

    @pytest.fixture
    def queue(self):
        return SlackMessageQueue(min_interval=0.0)

    @pytest.mark.asyncio
    async def test_message_resolves_pending_question(self, config, sessions, queue):
        """When a question is pending, the next message resolves it."""
        from chicane.handlers import _process_message

        # Create session with a pending question future
        info = sessions.get_or_create("1000.0", config, cwd=Path("/tmp"))
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        info.pending_question = future

        client = AsyncMock()
        client.reactions_add = AsyncMock()
        event = {
            "channel": "C123",
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "user": "UHUMAN",
            "text": "2",
        }

        await _process_message(event, "2", client, config, sessions, queue)

        # The future should be resolved with the user's text
        assert future.done()
        assert future.result() == "2"

        # Should have reacted with checkmark
        client.reactions_add.assert_any_call(
            channel="C123", name="white_check_mark", timestamp="1001.0"
        )

    @pytest.mark.asyncio
    async def test_message_without_pending_question_proceeds_normally(
        self, config, sessions, queue,
    ):
        """When no question is pending, messages go through normal flow."""
        from chicane.handlers import _process_message

        info = sessions.get_or_create("1000.0", config, cwd=Path("/tmp"))
        assert info.pending_question is None

        client = AsyncMock()
        client.reactions_add = AsyncMock()
        client.conversations_info = AsyncMock(return_value={"channel": {"name": "general"}})
        # The rest of _process_message will run — mock the session stream
        info.session.stream = AsyncMock(return_value=AsyncMock(__aiter__=lambda s: s, __anext__=AsyncMock(side_effect=StopAsyncIteration)))

        event = {
            "channel": "C123",
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "user": "UHUMAN",
            "text": "hello",
        }

        # Should NOT set the future — instead proceeds to streaming
        # (will hit some error in the mock, but the point is it didn't
        # short-circuit at the pending_question check)
        try:
            await _process_message(event, "hello", client, config, sessions, queue)
        except Exception:
            pass  # Expected — the stream mock is minimal

        assert info.pending_question is None


# ---------------------------------------------------------------------------
# Callback factory
# ---------------------------------------------------------------------------


class TestMakeAskUserCallback:
    """Tests for _make_ask_user_callback."""

    @pytest.fixture
    def session_info(self):
        session = ClaudeSession(cwd=Path("/tmp"))
        info = MagicMock(spec=SessionInfo)
        info.session = session
        info.thread_ts = "1000.0"
        info.pending_question = None
        return info

    @pytest.fixture
    def queue(self):
        return SlackMessageQueue(min_interval=0.0)

    @pytest.mark.asyncio
    async def test_callback_posts_and_waits(self, session_info, queue):
        """Callback posts question, waits for answer, returns parsed result."""
        client = AsyncMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        queue.ensure_client(client)

        callback = _make_ask_user_callback(
            session_info, client, "C123", "1000.0", queue,
        )

        questions = [
            {
                "question": "Pick a color?",
                "header": "Color",
                "options": [
                    {"label": "Red", "description": "Warm"},
                    {"label": "Blue", "description": "Cool"},
                ],
                "multiSelect": False,
            }
        ]

        # Simulate user answering in a separate task
        async def answer_after_delay():
            await asyncio.sleep(0.05)
            # The callback should have set pending_question by now
            assert session_info.pending_question is not None
            session_info.pending_question.set_result("2")

        asyncio.create_task(answer_after_delay())
        result = await callback(questions)

        assert result == {"Pick a color?": "Blue"}
        # pending_question should be cleared
        assert session_info.pending_question is None

    @pytest.mark.asyncio
    async def test_callback_timeout(self, session_info, queue):
        """Callback raises on timeout."""
        client = AsyncMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        queue.ensure_client(client)

        callback = _make_ask_user_callback(
            session_info, client, "C123", "1000.0", queue,
        )

        questions = [{"question": "Q?", "header": "Q", "options": [], "multiSelect": False}]

        # Patch the timeout to be very short
        with patch("chicane.handlers._ASK_USER_TIMEOUT", 0.05):
            with pytest.raises(RuntimeError, match="Timed out"):
                await callback(questions)

        # Future should be cleaned up
        assert session_info.pending_question is None


# ---------------------------------------------------------------------------
# SessionInfo.pending_question field
# ---------------------------------------------------------------------------


class TestSessionInfoPendingQuestion:
    """Verify SessionInfo has the pending_question field."""

    def test_default_is_none(self):
        session = ClaudeSession(cwd=Path("/tmp"))
        info = SessionInfo(
            session=session,
            thread_ts="1000.0",
            cwd=Path("/tmp"),
        )
        assert info.pending_question is None
