"""Tests for SlackMessageQueue throttling and retry logic."""

import asyncio
from time import monotonic
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from chicane.slack_queue import SlackMessageQueue, PostResult, DEFAULT_MIN_INTERVAL


class TestPostResult:
    """PostResult dataclass basics."""

    def test_fields(self):
        r = PostResult(ts="1.0", channel="C1", thread_ts="2.0")
        assert r.ts == "1.0"
        assert r.channel == "C1"
        assert r.thread_ts == "2.0"

    def test_frozen(self):
        r = PostResult(ts="1.0", channel="C1", thread_ts="2.0")
        with pytest.raises(AttributeError):
            r.ts = "999"


class TestEnsureClient:
    """ensure_client binds once, is idempotent."""

    def test_binds_client(self):
        q = SlackMessageQueue()
        client = AsyncMock()
        q.ensure_client(client)
        assert q._client is client

    def test_idempotent(self):
        q = SlackMessageQueue()
        first = AsyncMock()
        second = AsyncMock()
        q.ensure_client(first)
        q.ensure_client(second)
        assert q._client is first  # second call is a no-op

    @pytest.mark.asyncio
    async def test_post_without_client_raises(self):
        q = SlackMessageQueue()
        with pytest.raises(RuntimeError, match="client not bound"):
            await q.post_message("C1", "1.0", "hello")


class TestPostMessage:
    """Core post_message functionality."""

    @pytest.mark.asyncio
    async def test_basic_post_returns_result(self):
        q = SlackMessageQueue(min_interval=0.0)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "42.0"}
        q.ensure_client(client)

        result = await q.post_message("C1", "1.0", "hello")

        assert isinstance(result, PostResult)
        assert result.ts == "42.0"
        assert result.channel == "C1"
        assert result.thread_ts == "1.0"
        client.chat_postMessage.assert_called_once_with(
            channel="C1", thread_ts="1.0", text="hello",
        )

    @pytest.mark.asyncio
    async def test_multiple_posts_same_channel(self):
        q = SlackMessageQueue(min_interval=0.0)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "1.0"}
        q.ensure_client(client)

        r1 = await q.post_message("C1", "1.0", "first")
        r2 = await q.post_message("C1", "1.0", "second")

        assert client.chat_postMessage.call_count == 2
        assert r1.ts == "1.0"
        assert r2.ts == "1.0"


class TestThrottling:
    """Per-channel throttle enforcement."""

    @pytest.mark.asyncio
    async def test_throttle_enforces_interval(self):
        """Second post to same channel waits for min_interval."""
        sleep_delays = []

        async def tracking_sleep(delay):
            sleep_delays.append(delay)

        q = SlackMessageQueue(min_interval=1.0)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "1.0"}
        q.ensure_client(client)

        # Prime the last_post_time so throttle fires
        q._last_post_time["C1"] = monotonic()

        import chicane.slack_queue
        original_sleep = chicane.slack_queue.asyncio.sleep
        chicane.slack_queue.asyncio.sleep = tracking_sleep
        try:
            await q.post_message("C1", "1.0", "throttled")
        finally:
            chicane.slack_queue.asyncio.sleep = original_sleep

        assert len(sleep_delays) == 1
        assert 0 < sleep_delays[0] <= 1.0

    @pytest.mark.asyncio
    async def test_no_throttle_different_channels(self):
        """Posts to different channels should not throttle each other."""
        sleep_delays = []

        async def tracking_sleep(delay):
            sleep_delays.append(delay)

        q = SlackMessageQueue(min_interval=1.0)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "1.0"}
        q.ensure_client(client)

        # Post to C1
        q._last_post_time["C1"] = monotonic()

        import chicane.slack_queue
        original_sleep = chicane.slack_queue.asyncio.sleep
        chicane.slack_queue.asyncio.sleep = tracking_sleep
        try:
            # Post to C2 — should NOT wait
            await q.post_message("C2", "1.0", "no throttle")
        finally:
            chicane.slack_queue.asyncio.sleep = original_sleep

        assert len(sleep_delays) == 0

    @pytest.mark.asyncio
    async def test_no_throttle_when_interval_elapsed(self):
        """No sleep when enough time has passed since last post."""
        sleep_delays = []

        async def tracking_sleep(delay):
            sleep_delays.append(delay)

        q = SlackMessageQueue(min_interval=0.01)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "1.0"}
        q.ensure_client(client)

        # Set last post far in the past
        q._last_post_time["C1"] = monotonic() - 10.0

        import chicane.slack_queue
        original_sleep = chicane.slack_queue.asyncio.sleep
        chicane.slack_queue.asyncio.sleep = tracking_sleep
        try:
            await q.post_message("C1", "1.0", "no wait")
        finally:
            chicane.slack_queue.asyncio.sleep = original_sleep

        assert len(sleep_delays) == 0


class TestRetryOn429:
    """HTTP 429 retry with Retry-After header."""

    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        """Should retry once after sleeping for Retry-After seconds."""
        sleep_delays = []

        async def tracking_sleep(delay):
            sleep_delays.append(delay)

        q = SlackMessageQueue(min_interval=0.0)
        client = AsyncMock()

        # First call raises 429, second succeeds
        error_resp = MagicMock()
        error_resp.status_code = 429
        error_resp.headers = {"Retry-After": "2"}
        error = SlackApiError("rate_limited", response=error_resp)

        success_resp = {"ts": "99.0"}
        client.chat_postMessage.side_effect = [error, success_resp]
        q.ensure_client(client)

        import chicane.slack_queue
        original_sleep = chicane.slack_queue.asyncio.sleep
        chicane.slack_queue.asyncio.sleep = tracking_sleep
        try:
            result = await q.post_message("C1", "1.0", "retry me")
        finally:
            chicane.slack_queue.asyncio.sleep = original_sleep

        assert result.ts == "99.0"
        assert client.chat_postMessage.call_count == 2
        assert 2.0 in sleep_delays

    @pytest.mark.asyncio
    async def test_non_429_error_propagates(self):
        """Non-429 SlackApiError should propagate immediately."""
        q = SlackMessageQueue(min_interval=0.0)
        client = AsyncMock()

        error_resp = MagicMock()
        error_resp.status_code = 500
        error = SlackApiError("server_error", response=error_resp)
        client.chat_postMessage.side_effect = error
        q.ensure_client(client)

        with pytest.raises(SlackApiError):
            await q.post_message("C1", "1.0", "fail")

        assert client.chat_postMessage.call_count == 1

    @pytest.mark.asyncio
    async def test_429_default_retry_after(self):
        """When Retry-After header is missing, default to 1 second."""
        sleep_delays = []

        async def tracking_sleep(delay):
            sleep_delays.append(delay)

        q = SlackMessageQueue(min_interval=0.0)
        client = AsyncMock()

        error_resp = MagicMock()
        error_resp.status_code = 429
        error_resp.headers = {}  # No Retry-After header
        error = SlackApiError("rate_limited", response=error_resp)

        client.chat_postMessage.side_effect = [error, {"ts": "1.0"}]
        q.ensure_client(client)

        import chicane.slack_queue
        original_sleep = chicane.slack_queue.asyncio.sleep
        chicane.slack_queue.asyncio.sleep = tracking_sleep
        try:
            await q.post_message("C1", "1.0", "retry default")
        finally:
            chicane.slack_queue.asyncio.sleep = original_sleep

        assert 1.0 in sleep_delays


class TestConcurrentAccess:
    """Concurrent callers serialize through the lock."""

    @pytest.mark.asyncio
    async def test_concurrent_posts_serialize(self):
        """Multiple concurrent posts should be serialized via the lock."""
        q = SlackMessageQueue(min_interval=0.0)
        call_order = []

        async def slow_post(**kwargs):
            text = kwargs.get("text", "")
            call_order.append(f"start:{text}")
            await asyncio.sleep(0.01)
            call_order.append(f"end:{text}")
            return {"ts": "1.0"}

        client = AsyncMock()
        client.chat_postMessage = slow_post
        q.ensure_client(client)

        await asyncio.gather(
            q.post_message("C1", "1.0", "a"),
            q.post_message("C1", "1.0", "b"),
        )

        # Should be serialized: start:a, end:a, start:b, end:b
        # (order of a/b may swap, but they shouldn't interleave)
        assert len(call_order) == 4
        first_start = call_order[0]
        first_end = call_order[1]
        assert first_start.startswith("start:")
        assert first_end.startswith("end:")
        # Same message for both
        assert first_start.split(":")[1] == first_end.split(":")[1]
