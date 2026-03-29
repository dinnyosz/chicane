"""Delete the bot's own Slack messages to reclaim workspace message quota.

Scans channels the bot is a member of, fetches threads, and deletes
bot-authored messages older than a configurable threshold.  Dry-run by
default — pass ``--confirm`` to actually delete.
"""

import asyncio
import logging
import time
from argparse import Namespace
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.table import Table
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .config import Config

logger = logging.getLogger(__name__)

console = Console()

# Slack Tier 3 — ~50 requests per minute for most endpoints.
_API_INTERVAL = 60.0 / 50  # ~1.2s between any API call
_MAX_RETRIES = 5


# ---------------------------------------------------------------------------
# Rate-limit-aware Slack caller
# ---------------------------------------------------------------------------

_last_api_call: float = 0.0


async def _slack_call(method, *args, **kwargs):
    """Call a Slack API method with proactive throttle + 429 retry."""
    global _last_api_call

    # Proactive throttle — never exceed ~50 calls/min across all endpoints
    now = time.monotonic()
    elapsed = now - _last_api_call
    if elapsed < _API_INTERVAL:
        await asyncio.sleep(_API_INTERVAL - elapsed)

    for attempt in range(_MAX_RETRIES):
        try:
            _last_api_call = time.monotonic()
            return await method(*args, **kwargs)
        except SlackApiError as exc:
            if exc.response.status_code == 429:
                retry_after = float(exc.response.headers.get("Retry-After", 2))
                wait = retry_after + attempt  # progressive backoff
                logger.debug(
                    "Rate limited (attempt %d/%d), waiting %.1fs",
                    attempt + 1, _MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                _last_api_call = time.monotonic()
                continue
            raise
    # Final attempt — let it raise
    _last_api_call = time.monotonic()
    return await method(*args, **kwargs)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

async def _get_bot_user_id(client: AsyncWebClient) -> str:
    resp = await _slack_call(client.auth_test)
    return resp["user_id"]


async def _get_bot_channels(
    client: AsyncWebClient,
    channel_filter: str | None = None,
) -> list[dict]:
    """Return channels the bot is a member of."""
    channels: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await _slack_call(client.users_conversations, **kwargs)
        for ch in resp["channels"]:
            if channel_filter and ch["name"] != channel_filter:
                continue
            channels.append({"id": ch["id"], "name": ch["name"]})
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


async def _fetch_thread_replies(
    client: AsyncWebClient,
    channel_id: str,
    thread_ts: str,
) -> list[dict]:
    """Fetch all replies in a thread (including the parent)."""
    messages: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"channel": channel_id, "ts": thread_ts, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await _slack_call(client.conversations_replies, **kwargs)
        messages.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


async def _fetch_channel_messages(
    client: AsyncWebClient,
    channel_id: str,
    oldest_ts: str = "0",
) -> list[dict]:
    """Fetch top-level messages from a channel (no thread replies)."""
    messages: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"channel": channel_id, "oldest": oldest_ts, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await _slack_call(client.conversations_history, **kwargs)
        messages.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


# ---------------------------------------------------------------------------
# Purge logic
# ---------------------------------------------------------------------------

def _cutoff_ts(older_than_days: int) -> float:
    return time.time() - (older_than_days * 86400)


def _filter_bot_messages(
    messages: list[dict],
    bot_user_id: str,
    cutoff: float,
    keep_parent: bool,
    thread_ts: str | None = None,
) -> list[dict]:
    """Return bot-owned messages older than cutoff from a message list."""
    result: list[dict] = []
    for msg in messages:
        if msg.get("user") != bot_user_id:
            continue
        if float(msg.get("ts", "0")) >= cutoff:
            continue
        if keep_parent and thread_ts and msg.get("ts") == thread_ts:
            continue
        result.append(msg)
    return result


async def _delete_messages(
    client: AsyncWebClient,
    channel_id: str,
    messages: list[dict],
    dry_run: bool,
) -> tuple[int, int]:
    """Delete messages, returning (deleted, failed) counts."""
    deleted = 0
    failed = 0
    for msg in messages:
        if dry_run:
            deleted += 1
            continue
        try:
            await _slack_call(client.chat_delete, channel=channel_id, ts=msg["ts"])
            deleted += 1
        except SlackApiError as exc:
            error = getattr(exc.response, "data", {}).get("error", "")
            if error in ("message_not_found", "cant_delete_message"):
                pass
            else:
                failed += 1
                logger.debug("Delete failed for %s: %s", msg["ts"], error)
    return deleted, failed


async def _purge_channel(
    client: AsyncWebClient,
    channel_id: str,
    channel_name: str,
    bot_user_id: str,
    cutoff: float,
    keep_parent: bool,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Scan and delete per-thread in a channel. Returns (scanned, deleted, failed)."""
    top_level = await _fetch_channel_messages(client, channel_id)
    seen_threads: set[str] = set()
    total_deleted = 0
    total_failed = 0

    verb = "Scanning" if dry_run else "Purging"

    with Progress(
        TextColumn(f"[cyan]#{channel_name}[/]"),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"{verb}…", total=len(top_level))

        for msg in top_level:
            thread_ts = msg.get("thread_ts")

            # Threaded message — scan thread, delete immediately
            if thread_ts and thread_ts not in seen_threads:
                seen_threads.add(thread_ts)
                replies = await _fetch_thread_replies(client, channel_id, thread_ts)
                to_delete = _filter_bot_messages(
                    replies, bot_user_id, cutoff, keep_parent, thread_ts,
                )
                if to_delete:
                    deleted, failed = await _delete_messages(
                        client, channel_id, to_delete, dry_run,
                    )
                    total_deleted += deleted
                    total_failed += failed
                progress.update(
                    task,
                    description=f"{verb}… [green]{total_deleted}[/] deleted",
                )
                progress.advance(task)
                continue

            # Non-threaded top-level bot message
            to_delete = _filter_bot_messages(
                [msg], bot_user_id, cutoff, keep_parent,
            )
            # With keep_parent, skip top-level messages that have replies
            if keep_parent and msg.get("reply_count"):
                to_delete = []

            if to_delete:
                deleted, failed = await _delete_messages(
                    client, channel_id, to_delete, dry_run,
                )
                total_deleted += deleted
                total_failed += failed

            progress.update(
                task,
                description=f"{verb}… [green]{total_deleted}[/] deleted",
            )
            progress.advance(task)

    return len(top_level), total_deleted, total_failed


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def _purge(args: Namespace) -> None:
    config = Config.from_env()
    older_than_days = getattr(args, "older_than", None) or config.purge_older_than_days
    dry_run = not getattr(args, "confirm", False)
    channel_filter = getattr(args, "channel", None)
    keep_parent = getattr(args, "keep_parent", False)

    mode = "[yellow bold]DRY RUN[/]" if dry_run else "[red bold]LIVE DELETE[/]"
    console.print(Panel(
        f"Mode: {mode}\n"
        f"Delete messages older than: [cyan]{older_than_days}[/] days\n"
        f"Keep thread parents: [cyan]{keep_parent}[/]\n"
        f"Channel filter: [cyan]{channel_filter or 'all'}[/]",
        title="chicane purge",
    ))

    client = AsyncWebClient(token=config.slack_bot_token)
    bot_user_id = await _get_bot_user_id(client)
    cutoff = _cutoff_ts(older_than_days)
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    console.print(f"Bot user: [cyan]{bot_user_id}[/]  Cutoff: [cyan]{cutoff_dt:%Y-%m-%d %H:%M UTC}[/]\n")

    channels = await _get_bot_channels(client, channel_filter)
    if not channels:
        console.print("[yellow]No channels found.[/]")
        return

    console.print(f"Found [cyan]{len(channels)}[/] channel(s) to scan.\n")

    summary = Table(title="Purge Summary")
    summary.add_column("Channel", style="cyan")
    summary.add_column("Scanned", justify="right")
    summary.add_column("Deleted", justify="right", style="green")
    summary.add_column("Failed", justify="right", style="yellow")

    grand_deleted = 0
    grand_failed = 0

    for ch in channels:
        scanned, deleted, failed = await _purge_channel(
            client, ch["id"], ch["name"],
            bot_user_id, cutoff, keep_parent, dry_run,
        )
        grand_deleted += deleted
        grand_failed += failed
        summary.add_row(ch["name"], str(scanned), str(deleted), str(failed))

    console.print()
    console.print(summary)

    verb = "would be deleted" if dry_run else "deleted"
    console.print(
        f"\n[bold]{grand_deleted}[/] message(s) {verb}"
        + (f", [yellow]{grand_failed}[/] failed" if grand_failed else "")
        + "."
    )
    if dry_run:
        console.print("\n[yellow]Run with --confirm to actually delete messages.[/]")


def purge_command(args: Namespace) -> None:
    """Sync entry point for ``chicane purge``."""
    asyncio.run(_purge(args))
