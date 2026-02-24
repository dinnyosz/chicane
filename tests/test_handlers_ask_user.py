"""Tests for AskUserQuestion handling — SDK canUseTool + Slack interception."""

import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chicane.config import Config
from chicane.handlers import (
    _format_question_blocks,
    _format_single_question,
    _make_ask_user_callback,
    _parse_question_answer,
    _parse_single_answer,
)
from chicane.sessions import SessionInfo, SessionStore
from chicane.claude import ClaudeSession
from chicane.slack_queue import SlackMessageQueue

# Save reference before conftest patches asyncio.sleep globally.
_real_sleep = asyncio.sleep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_future(info, prev=None, max_iters=500):
    """Wait until session_info.pending_question holds a *new*, non-done future.

    Uses the *real* asyncio.sleep (saved before conftest patches it) so that
    the polling loop actually yields to the event loop.
    """
    for _ in range(max_iters):
        f = info.pending_question
        if f is not None and f is not prev and not f.done():
            return f
        await _real_sleep(0.005)
    raise AssertionError("Timed out waiting for new pending_question future")


def _make_test_client():
    """Build a mock Slack client with chat_postMessage returning a valid ts."""
    client = AsyncMock()
    client.reactions_add = AsyncMock()
    client.reactions_remove = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "9999.0"})
    return client


# ---------------------------------------------------------------------------
# Single-question formatting
# ---------------------------------------------------------------------------


class TestFormatSingleQuestion:
    """Tests for _format_single_question."""

    def test_basic_question(self):
        q = {
            "question": "Which language?",
            "header": "Language",
            "options": [
                {"label": "Python", "description": "Dynamic typed"},
                {"label": "Rust", "description": "Systems language"},
            ],
            "multiSelect": False,
        }
        text, blocks = _format_single_question(q, 1, 1)
        assert "*Language:*" in text
        assert "Which language?" in text
        assert "1. *Python*" in text
        assert "2. *Rust*" in text
        assert "Reply in this thread" in text
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"

    def test_index_prefix_when_multiple(self):
        q = {"question": "Q1?", "header": "H", "options": [], "multiSelect": False}
        text, _ = _format_single_question(q, 2, 3)
        assert "(2/3)" in text

    def test_no_prefix_when_single(self):
        q = {"question": "Q?", "header": "H", "options": [], "multiSelect": False}
        text, _ = _format_single_question(q, 1, 1)
        assert "(1/1)" not in text

    def test_multi_select_note(self):
        q = {
            "question": "Which features?",
            "header": "Features",
            "options": [
                {"label": "Auth", "description": "Authentication"},
                {"label": "Cache", "description": "Caching layer"},
            ],
            "multiSelect": True,
        }
        text, _ = _format_single_question(q, 1, 1)
        assert "select multiple" in text


# ---------------------------------------------------------------------------
# Single-answer parsing
# ---------------------------------------------------------------------------


class TestParseSingleAnswer:
    """Tests for _parse_single_answer."""

    def test_numeric_single(self):
        q = {"options": [{"label": "PostgreSQL"}, {"label": "SQLite"}]}
        assert _parse_single_answer("1", q) == "PostgreSQL"

    def test_numeric_second_option(self):
        q = {"options": [{"label": "PostgreSQL"}, {"label": "SQLite"}]}
        assert _parse_single_answer("2", q) == "SQLite"

    def test_numeric_multi_select(self):
        q = {"options": [{"label": "Auth"}, {"label": "Cache"}, {"label": "Logging"}]}
        assert _parse_single_answer("1, 3", q) == "Auth, Logging"

    def test_free_text(self):
        q = {"options": [{"label": "Django"}, {"label": "Flask"}]}
        assert _parse_single_answer("FastAPI", q) == "FastAPI"

    def test_empty_reply(self):
        q = {"options": [{"label": "A"}]}
        assert _parse_single_answer("", q) == ""

    def test_out_of_range_index_falls_through(self):
        q = {"options": [{"label": "A"}]}
        assert _parse_single_answer("5", q) == "5"

    def test_no_options(self):
        q = {"options": []}
        assert _parse_single_answer("anything", q) == "anything"


# ---------------------------------------------------------------------------
# Legacy question formatting (backward compat)
# ---------------------------------------------------------------------------


class TestFormatQuestionBlocks:
    """Tests for _format_question_blocks (legacy wrapper)."""

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
# Legacy answer parsing (backward compat)
# ---------------------------------------------------------------------------


class TestParseQuestionAnswer:
    """Tests for _parse_question_answer (legacy wrapper)."""

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

        assert future.done()
        assert future.result() == "2"

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
        info.session.stream = AsyncMock(return_value=AsyncMock(__aiter__=lambda s: s, __anext__=AsyncMock(side_effect=StopAsyncIteration)))

        event = {
            "channel": "C123",
            "ts": "1001.0",
            "thread_ts": "1000.0",
            "user": "UHUMAN",
            "text": "hello",
        }

        try:
            await _process_message(event, "hello", client, config, sessions, queue)
        except Exception:
            pass

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
    async def test_callback_single_question(self, session_info, queue):
        """Callback posts one question, waits for answer, returns parsed result."""
        client = _make_test_client()
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

        async def answer():
            f = await _wait_for_future(session_info)
            f.set_result("2")

        asyncio.create_task(answer())
        result = await callback(questions)

        assert result == {"Pick a color?": "Blue"}
        assert session_info.pending_question is None

    @pytest.mark.asyncio
    async def test_callback_multiple_questions_sequential(self, session_info, queue):
        """Callback posts each question one at a time, collecting answers."""
        client = _make_test_client()
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
            },
            {
                "question": "Pick a size?",
                "header": "Size",
                "options": [
                    {"label": "Small", "description": "Compact"},
                    {"label": "Large", "description": "Spacious"},
                ],
                "multiSelect": False,
            },
        ]

        async def answer():
            f = await _wait_for_future(session_info)
            f.set_result("1")  # Red
            f = await _wait_for_future(session_info, prev=f)
            f.set_result("2")  # Large

        asyncio.create_task(answer())
        result = await callback(questions)

        assert result == {"Pick a color?": "Red", "Pick a size?": "Large"}
        assert session_info.pending_question is None

    @pytest.mark.asyncio
    async def test_callback_three_questions_mixed_answers(self, session_info, queue):
        """Three questions: numeric, free-text, and numeric answers."""
        client = _make_test_client()
        queue.ensure_client(client)

        callback = _make_ask_user_callback(
            session_info, client, "C123", "1000.0", queue,
        )

        questions = [
            {
                "question": "Language?",
                "header": "Lang",
                "options": [{"label": "Python"}, {"label": "Rust"}],
                "multiSelect": False,
            },
            {
                "question": "Framework?",
                "header": "FW",
                "options": [{"label": "Django"}, {"label": "Flask"}],
                "multiSelect": False,
            },
            {
                "question": "Database?",
                "header": "DB",
                "options": [{"label": "PostgreSQL"}, {"label": "SQLite"}],
                "multiSelect": False,
            },
        ]

        async def answer():
            f = await _wait_for_future(session_info)
            f.set_result("1")  # Python
            f = await _wait_for_future(session_info, prev=f)
            f.set_result("FastAPI")  # free-text
            f = await _wait_for_future(session_info, prev=f)
            f.set_result("2")  # SQLite

        asyncio.create_task(answer())
        result = await callback(questions)

        assert result == {
            "Language?": "Python",
            "Framework?": "FastAPI",
            "Database?": "SQLite",
        }

    @pytest.mark.asyncio
    async def test_callback_posts_with_index_prefix(self, session_info, queue):
        """When multiple questions, each post includes (i/total) prefix."""
        client = _make_test_client()

        posted_texts = []

        async def capture_post(channel, thread_ts, text, **kwargs):
            posted_texts.append(text)

        queue.post_message = capture_post
        queue.ensure_client(client)

        callback = _make_ask_user_callback(
            session_info, client, "C123", "1000.0", queue,
        )

        questions = [
            {"question": "Q1?", "header": "First", "options": [], "multiSelect": False},
            {"question": "Q2?", "header": "Second", "options": [], "multiSelect": False},
        ]

        async def answer():
            f = await _wait_for_future(session_info)
            f.set_result("a")
            f = await _wait_for_future(session_info, prev=f)
            f.set_result("b")

        asyncio.create_task(answer())
        await callback(questions)

        assert len(posted_texts) == 2
        assert "(1/2)" in posted_texts[0]
        assert "(2/2)" in posted_texts[1]

    @pytest.mark.asyncio
    async def test_callback_timeout(self, session_info, queue):
        """Callback raises on timeout."""
        client = _make_test_client()
        queue.ensure_client(client)

        callback = _make_ask_user_callback(
            session_info, client, "C123", "1000.0", queue,
        )

        questions = [{"question": "Q?", "header": "Q", "options": [], "multiSelect": False}]

        with patch("chicane.handlers._ASK_USER_TIMEOUT", 0.05):
            with pytest.raises(RuntimeError, match="Timed out"):
                await callback(questions)

        assert session_info.pending_question is None

    @pytest.mark.asyncio
    async def test_callback_adds_and_removes_speech_balloon(self, session_info, queue):
        """Speech balloon reaction is added at start and removed at end."""
        client = _make_test_client()
        queue.ensure_client(client)

        callback = _make_ask_user_callback(
            session_info, client, "C123", "1000.0", queue,
        )

        questions = [{"question": "Q?", "header": "Q", "options": [{"label": "A"}], "multiSelect": False}]

        async def answer():
            f = await _wait_for_future(session_info)
            f.set_result("1")

        asyncio.create_task(answer())
        await callback(questions)

        client.reactions_add.assert_any_call(
            channel="C123", name="speech_balloon", timestamp="1000.0",
        )
        client.reactions_remove.assert_any_call(
            channel="C123", name="speech_balloon", timestamp="1000.0",
        )


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
