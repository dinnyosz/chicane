"""Throttled Slack message queue — prevents hitting platform rate limits.

Slack's ``chat.postMessage`` allows roughly 1 message per second per channel
with short burst tolerance.  This module wraps outbound posting through a
per-channel throttle so we never exceed that limit, even when Claude is
firing off many tool calls in quick succession.
"""

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

# Default minimum seconds between chat_postMessage calls per channel.
DEFAULT_MIN_INTERVAL = 1.0


@dataclass(frozen=True)
class PostResult:
    """Result of a queued message post."""

    ts: str
    channel: str
    thread_ts: str


class SlackMessageQueue:
    """Per-channel throttle for ``chat.postMessage`` calls.

    Usage::

        queue = SlackMessageQueue()

        # In a handler (once client is available):
        queue.ensure_client(client)
        result = await queue.post_message(channel, thread_ts, text)
        print(result.ts)  # Slack message timestamp

    The queue is *awaitable*, not background-worker based.  Each
    ``post_message`` call blocks until the message is actually posted,
    which naturally preserves ordering within a single coroutine.
    Concurrent callers serialize via an internal lock.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL) -> None:
        self._client: AsyncWebClient | None = None
        self._min_interval = min_interval
        self._last_post_time: dict[str, float] = {}  # channel → monotonic time
        self._lock = asyncio.Lock()

    def ensure_client(self, client: AsyncWebClient) -> None:
        """Bind the Slack client (idempotent)."""
        if self._client is None:
            self._client = client

    async def post_message(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        *,
        blocks: list[dict] | None = None,
    ) -> PostResult:
        """Post a message, throttling to respect Slack's rate limit.

        Blocks until the message is posted and returns a :class:`PostResult`
        with the Slack timestamp.

        If *blocks* is provided, the message is sent with Block Kit blocks
        and *text* serves as the notification/accessibility fallback.
        """
        if self._client is None:
            raise RuntimeError("SlackMessageQueue: client not bound — call ensure_client() first")

        async with self._lock:
            await self._throttle(channel)
            result = await self._post_with_retry(channel, thread_ts, text, blocks=blocks)
            self._last_post_time[channel] = monotonic()
            return result

    async def _throttle(self, channel: str) -> None:
        """Sleep if needed to maintain minimum interval for this channel."""
        last = self._last_post_time.get(channel, 0.0)
        elapsed = monotonic() - last
        if elapsed < self._min_interval:
            delay = self._min_interval - elapsed
            logger.debug("Throttling channel %s for %.2fs", channel, delay)
            await asyncio.sleep(delay)

    async def _post_with_retry(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        *,
        blocks: list[dict] | None = None,
    ) -> PostResult:
        """Post message, retrying once on HTTP 429."""
        kwargs: dict = dict(channel=channel, thread_ts=thread_ts, text=text)
        if blocks:
            kwargs["blocks"] = blocks
        try:
            resp = await self._client.chat_postMessage(**kwargs)
            return PostResult(ts=resp["ts"], channel=channel, thread_ts=thread_ts)
        except SlackApiError as exc:
            if exc.response.status_code == 429:
                retry_after = float(
                    exc.response.headers.get("Retry-After", 1)
                )
                logger.warning(
                    "Slack rate limited (429) on channel %s, retrying after %.1fs",
                    channel, retry_after,
                )
                await asyncio.sleep(retry_after)
                resp = await self._client.chat_postMessage(**kwargs)
                return PostResult(ts=resp["ts"], channel=channel, thread_ts=thread_ts)
            raise
