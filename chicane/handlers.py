"""Slack event handlers — routes messages to Claude and streams responses back."""

import asyncio
import logging
import re
from pathlib import Path

import aiohttp
from slack_sdk.errors import SlackApiError
from platformdirs import user_cache_dir
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from .config import Config
from .sessions import SessionInfo, SessionStore

logger = logging.getLogger(__name__)

# Max message length for Slack
SLACK_MAX_LENGTH = 3900


# ---------------------------------------------------------------------------
# Thread-root reaction helpers — track state in SessionInfo to skip no-ops
# ---------------------------------------------------------------------------


async def _add_thread_reaction(
    client: AsyncWebClient,
    channel: str,
    session_info: SessionInfo,
    name: str,
) -> None:
    """Add a reaction to the thread root, skipping if already present."""
    if name in session_info.thread_reactions:
        return
    try:
        await client.reactions_add(
            channel=channel, name=name, timestamp=session_info.thread_ts,
        )
        session_info.thread_reactions.add(name)
    except Exception:
        pass


async def _remove_thread_reaction(
    client: AsyncWebClient,
    channel: str,
    session_info: SessionInfo,
    name: str,
) -> None:
    """Remove a reaction from the thread root, skipping if not present."""
    if name not in session_info.thread_reactions:
        return
    try:
        await client.reactions_remove(
            channel=channel, name=name, timestamp=session_info.thread_ts,
        )
        session_info.thread_reactions.discard(name)
    except Exception:
        session_info.thread_reactions.discard(name)

# Threshold above which we upload a snippet instead of splitting into
# multiple messages.  Set slightly above SLACK_MAX_LENGTH so short
# overflows still get a simple two-message split.
SNIPPET_THRESHOLD = 4000

# Max file size to download from Slack (10 MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# Regex to detect a handoff session_id at the end of a prompt.
# Matches both plain  (session_id: uuid)  and Slack-italicised  _(session_id: uuid)_
_HANDOFF_RE = re.compile(r"_?\(session_id:\s*([a-f0-9\-]+)\)_?\s*$")

# Tools whose output is too noisy for Slack (file contents).
# Their tool_result blocks are silently dropped even in verbose mode.
_QUIET_TOOLS = frozenset({"Read"})


def _should_show(event_type: str, verbosity: str) -> bool:
    """Check whether an event type should be displayed at the given verbosity level.

    Event types: tool_activity, tool_error, tool_result, compact_boundary.
    Text, completion summary, permission denials, and empty warnings are always shown.
    """
    if verbosity == "verbose":
        return True
    if verbosity == "normal":
        return event_type in ("tool_activity", "tool_error")
    # minimal
    return False


def register_handlers(app: AsyncApp, config: Config, sessions: SessionStore) -> None:
    """Register all Slack event handlers on the app."""
    bot_user_id: str | None = None
    processed_ts: dict[str, None] = {}  # ordered dict (insertion order) for LRU eviction

    def _mark_processed(ts: str) -> bool:
        """Mark a message as processed. Returns False if already seen."""
        if ts in processed_ts:
            return False
        processed_ts[ts] = None
        # Keep bounded: evict oldest half when limit is reached
        if len(processed_ts) > 500:
            keys = list(processed_ts)
            for k in keys[: len(keys) // 2]:
                del processed_ts[k]
        return True

    @app.event("app_mention")
    async def handle_mention(event: dict, client: AsyncWebClient) -> None:
        """Handle @mentions of the bot in channels."""
        if not _mark_processed(event["ts"]):
            return

        if _should_ignore(event, config):
            return

        text = re.sub(r"<@[A-Z0-9]+>\s*", "", event.get("text", "")).strip()
        if not text and not event.get("files"):
            return

        await _process_message(event, text or "", client, config, sessions)

    @app.event("message")
    async def handle_message(event: dict, client: AsyncWebClient) -> None:
        """Handle DMs and thread follow-ups."""
        nonlocal bot_user_id

        # Skip message subtypes (edits, deletes, etc.) but allow file_share
        # so users can send files with accompanying text.
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return
        # Fast reject if already handled by app_mention or a prior delivery
        if event["ts"] in processed_ts:
            return

        channel_type = event.get("channel_type", "")
        text = event.get("text", "").strip()
        has_files = bool(event.get("files"))
        if not text and not has_files:
            return

        # DMs: process everything
        if channel_type == "im":
            if not _mark_processed(event["ts"]):
                return
            if _should_ignore(event, config):
                return
            await _process_message(event, text, client, config, sessions)
            return

        # Channel thread follow-ups: respond if it's a reply in a thread
        # that Chicane already has a session for, OR if the bot previously
        # posted in the thread (survives bot restarts)
        thread_ts = event.get("thread_ts")
        if thread_ts:
            is_chicane_thread = sessions.has(thread_ts)
            if not is_chicane_thread:
                logger.debug(f"No session for thread {thread_ts}, checking Slack history")
                is_chicane_thread = await _bot_in_thread(
                    thread_ts, event["channel"], client
                )
                logger.debug(f"Bot in thread {thread_ts}: {is_chicane_thread}")
            if is_chicane_thread:
                if not _mark_processed(event["ts"]):
                    return
                if _should_ignore(event, config):
                    return
                await _process_message(event, text, client, config, sessions)
                return
            # Not a Chicane thread — don't claim the ts so app_mention
            # can still handle @mentions in unknown threads.
            return

        # Channel messages with @mention from bots (app_mention doesn't fire for bots).
        if not bot_user_id:
            auth = await client.auth_test()
            bot_user_id = auth["user_id"]

        if f"<@{bot_user_id}>" in text:
            if not _mark_processed(event["ts"]):
                return
            if _should_ignore(event, config):
                return
            clean_text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
            if clean_text:
                await _process_message(event, clean_text, client, config, sessions)

    @app.event("reaction_added")
    async def handle_reaction(event: dict, client: AsyncWebClient) -> None:
        """Handle :octagonal_sign: reactions to interrupt active streams."""
        if event.get("reaction") != "octagonal_sign":
            return

        item = event.get("item", {})
        if item.get("type") != "message":
            return

        item_ts = item.get("ts", "")
        item_channel = item.get("channel", "")

        # Find the thread this message belongs to
        thread_ts = sessions.thread_for_message(item_ts)
        if not thread_ts and sessions.has(item_ts):
            # The reacted message IS the thread starter
            thread_ts = item_ts

        if not thread_ts:
            return

        session_info = sessions.get(thread_ts)
        if not session_info:
            return

        if session_info.session.is_streaming:
            await session_info.session.interrupt()
            await client.chat_postMessage(
                channel=item_channel,
                thread_ts=thread_ts,
                text=":stop_sign: _Interrupted by user_",
            )


def _should_ignore(event: dict, config: Config) -> bool:
    """Check if this event should be ignored."""
    user = event.get("user", "")
    if config.allowed_users and user not in config.allowed_users:
        logger.info(f"Ignoring message from non-allowed user: {user}")
        return True
    return False


async def _process_message(
    event: dict,
    prompt: str,
    client: AsyncWebClient,
    config: Config,
    sessions: SessionStore,
) -> None:
    """Process a message by sending it to Claude and streaming the response."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])
    user = event.get("user", "unknown")

    # Check for a handoff session_id embedded in the prompt
    handoff_session_id: str | None = None
    handoff_match = _HANDOFF_RE.search(prompt)
    if handoff_match:
        handoff_session_id = handoff_match.group(1)
        prompt = prompt[: handoff_match.start()].rstrip()
        logger.info(f"Handoff detected — resuming session {handoff_session_id}")

    logger.debug(f"Processing message from {user} in {channel}: {prompt[:80]}")

    # Add eyes reaction to user's message to show we've seen it
    try:
        await client.reactions_add(channel=channel, name="eyes", timestamp=event["ts"])
    except Exception:
        pass  # Reaction may already exist or we lack permission

    # Resolve working directory from channel name
    cwd = await _resolve_channel_cwd(channel, client, config)

    # Detect reconnect: session doesn't exist yet AND this is a thread reply
    # Skip reconnect when this is a handoff — the resumed session already has context.
    is_reconnect = (
        not handoff_session_id
        and not sessions.has(thread_ts)
        and thread_ts != event["ts"]
    )

    # On reconnect, try to find a session_id in the thread first so we can
    # resume the original Claude session instead of rebuilding from history.
    if is_reconnect and not handoff_session_id:
        found_id = await _find_session_id_in_thread(channel, thread_ts, client)
        if found_id:
            handoff_session_id = found_id
            is_reconnect = False
            logger.info(
                f"Reconnect: found session_id {found_id} in thread {thread_ts}"
            )

    # Get or create a Claude session for this thread
    session_info = sessions.get_or_create(
        thread_ts=thread_ts,
        config=config,
        cwd=cwd,
        session_id=handoff_session_id,
    )
    session = session_info.session

    # Thread-root status: clear any previous state → add eyes so the channel
    # list shows this thread is actively being worked on.
    if thread_ts != event["ts"]:
        for old_emoji in ("white_check_mark", "x", "octagonal_sign",
                         "speech_balloon", "warning", "hourglass"):
            await _remove_thread_reaction(client, channel, session_info, old_emoji)
        await _add_thread_reaction(client, channel, session_info, "eyes")

    # On reconnect (no session_id found), fall back to rebuilding context
    # from Slack thread history.
    if is_reconnect:
        history = await _fetch_thread_history(channel, thread_ts, event["ts"], client)
        if history:
            prompt = (
                "Here is the conversation history from this Slack thread:\n\n"
                f"{history}\n\n"
                "---\n"
                f"Now respond to the latest message:\n{prompt}"
            )

    # Download file attachments to a cache directory outside the git worktree
    attachments_dir = Path(user_cache_dir("chicane", appauthor=False)) / "attachments" / thread_ts
    downloaded_files = await _download_files(event, config.slack_bot_token, attachments_dir)
    if downloaded_files:
        refs = []
        for name, path, mime in downloaded_files:
            if mime.startswith("image/"):
                refs.append(f"- Image: {path} (original name: {name})")
            else:
                refs.append(f"- File: {path} (original name: {name})")
        file_note = (
            "\n\nThe user attached files. "
            "Use the Read tool to inspect them:\n" + "\n".join(refs)
        )
        prompt = (prompt + file_note) if prompt else file_note.lstrip()

    # Post initial "thinking" message
    try:
        result = await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass_flowing_sand: Working on it...",
        )
        message_ts = result["ts"]
        sessions.register_bot_message(message_ts, thread_ts)
    except Exception:
        logger.exception("Failed to post placeholder message")
        return

    # If another stream is active for this thread, interrupt it so we
    # can acquire the lock quickly instead of waiting minutes.
    queued = session_info.lock.locked()
    if session_info.session.is_streaming:
        logger.info(f"New message in {thread_ts} — interrupting active stream")
        await session_info.session.interrupt(source="new_message")

    # Show hourglass on thread root while waiting for the lock
    if queued:
        await _add_thread_reaction(client, channel, session_info, "hourglass")

    # Stream Claude's response — hold the session lock so only one
    # _process_message streams at a time per thread.
    async with session_info.lock:
        # Clear hourglass now that we have the lock
        if queued:
            await _remove_thread_reaction(client, channel, session_info, "hourglass")
        full_text = ""
        event_count = 0
        first_activity = True  # track whether to update placeholder or post new
        result_event = None  # capture result event for completion summary
        git_committed = False  # track whether a git commit happened (since last edit)
        files_changed = False  # track uncommitted file changes
        tool_id_to_name: dict[str, str] = {}  # tool_use_id → tool name

        try:
            async for event_data in session.stream(prompt):
                event_count += 1
                if event_data.type == "assistant":
                    # Track tool_use_id → tool name so we can filter results later
                    tool_id_to_name.update(event_data.tool_use_ids)

                    # Flush any accumulated text before posting tool activity
                    # so the message order matches Claude Code console.
                    activities = _format_tool_activity(event_data)

                    # Prefix subagent activities
                    if event_data.parent_tool_use_id:
                        activities = [f":arrow_right_hook: {a}" for a in activities]

                    show_activities = _should_show("tool_activity", config.verbosity)

                    if show_activities and activities and full_text:
                        for chunk in _split_message(_markdown_to_mrkdwn(full_text)):
                            r = await client.chat_postMessage(
                                channel=channel, thread_ts=thread_ts, text=chunk,
                            )
                            sessions.register_bot_message(r["ts"], thread_ts)
                        full_text = ""

                    # Post tool activity
                    if show_activities:
                        for activity in activities:
                            if first_activity:
                                await client.chat_update(
                                    channel=channel, ts=message_ts, text=activity,
                                )
                                first_activity = False
                            else:
                                r = await client.chat_postMessage(
                                    channel=channel, thread_ts=thread_ts, text=activity,
                                )
                                sessions.register_bot_message(r["ts"], thread_ts)

                    # Detect file edits — add pencil reaction to thread root
                    if not files_changed and _has_file_edit(event_data):
                        files_changed = True
                        await _add_thread_reaction(client, channel, session_info, "pencil2")
                        # Remove package if editing after a commit (uncommitted changes again)
                        if git_committed:
                            git_committed = False
                            await _remove_thread_reaction(client, channel, session_info, "package")

                    # Detect git commits from Bash tool use
                    if _has_git_commit(event_data):
                        # Remove pencil reaction — changes are committed
                        if files_changed:
                            files_changed = False
                            await _remove_thread_reaction(client, channel, session_info, "pencil2")
                        # Add package reaction (only if not already showing)
                        if not git_committed:
                            git_committed = True
                            session_info.total_commits += 1
                            await _add_thread_reaction(client, channel, session_info, "package")
                            # Also add to user's message (not tracked)
                            if event["ts"] != thread_ts:
                                try:
                                    await client.reactions_add(
                                        channel=channel, name="package", timestamp=event["ts"],
                                    )
                                except Exception:
                                    pass

                    # Detect AskUserQuestion — add speech balloon to thread root
                    if _has_question(event_data):
                        await _add_thread_reaction(client, channel, session_info, "speech_balloon")

                    # Accumulate text
                    chunk = event_data.text
                    if chunk:
                        full_text += chunk

                elif event_data.type == "result":
                    result_event = event_data
                    # Prefer whichever is longer — streamed text preserves
                    # formatting but could miss chunks; result blob is
                    # complete but may flatten newlines.
                    result_text = event_data.text or ""
                    if len(result_text) > len(full_text):
                        full_text = result_text

                elif event_data.type == "user":
                    # Check for tool errors in user events (tool results)
                    if _should_show("tool_error", config.verbosity):
                        for error_msg in event_data.tool_errors:
                            truncated = (error_msg[:200] + "...") if len(error_msg) > 200 else error_msg
                            await client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts,
                                text=f":warning: Tool error: {truncated}",
                            )

                    # Show tool outputs in verbose mode (skip noisy tools)
                    if _should_show("tool_result", config.verbosity):
                        for tool_use_id, result_text in event_data.tool_results:
                            tool_name = tool_id_to_name.get(tool_use_id, "")
                            if tool_name in _QUIET_TOOLS:
                                continue
                            # Upload long tool output as a snippet (>500 chars)
                            if len(result_text) > 500:
                                await _send_snippet(
                                    client, channel, thread_ts,
                                    result_text,
                                    initial_comment=":clipboard: Tool output (uploaded as snippet):",
                                )
                            else:
                                wrapped = f":clipboard: Tool output:\n```\n{result_text}\n```"
                                await client.chat_postMessage(
                                    channel=channel,
                                    thread_ts=thread_ts,
                                    text=wrapped,
                                )

                elif event_data.type == "system" and event_data.subtype == "compact_boundary":
                    if _should_show("compact_boundary", config.verbosity):
                        meta = event_data.compact_metadata or {}
                        trigger = meta.get("trigger", "auto")
                        pre_tokens = meta.get("pre_tokens")
                        if trigger == "auto":
                            note = ":brain: Context was automatically compacted"
                        else:
                            note = ":brain: Context was manually compacted"
                        if pre_tokens:
                            note += f" ({pre_tokens:,} tokens before)"
                        note += " — earlier messages may be summarized"
                        await client.chat_postMessage(
                            channel=channel, thread_ts=thread_ts, text=note,
                        )

                else:
                    logger.debug(f"Event type={event_data.type} subtype={event_data.subtype}")

            # Handle interrupted stream — skip normal completion flow
            if session.was_interrupted:
                if session.interrupt_source == "new_message":
                    # New message will process next — post partial text, then note
                    if full_text:
                        mrkdwn = _markdown_to_mrkdwn(full_text)
                        if first_activity:
                            await client.chat_update(
                                channel=channel, ts=message_ts, text=mrkdwn[:SLACK_MAX_LENGTH],
                            )
                        else:
                            for chunk in _split_message(mrkdwn):
                                await client.chat_postMessage(
                                    channel=channel, thread_ts=thread_ts, text=chunk,
                                )
                    await client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text=":bulb: _Thought added_",
                    )
                    try:
                        await client.reactions_remove(
                            channel=channel, name="eyes", timestamp=event["ts"]
                        )
                    except Exception:
                        pass
                    # Thread-root eyes will be re-added by the new message's
                    # _process_message call, so no action needed here.
                else:
                    # Reaction interrupt — show partial text + stop indicator
                    if full_text:
                        mrkdwn = _markdown_to_mrkdwn(full_text)
                        if first_activity:
                            await client.chat_update(
                                channel=channel, ts=message_ts,
                                text=mrkdwn[:SLACK_MAX_LENGTH] + "\n\n:stop_sign: _Interrupted_",
                            )
                        else:
                            for chunk in _split_message(mrkdwn):
                                await client.chat_postMessage(
                                    channel=channel, thread_ts=thread_ts, text=chunk,
                                )
                            await client.chat_postMessage(
                                channel=channel, thread_ts=thread_ts,
                                text=":stop_sign: _Interrupted_",
                            )
                    else:
                        await client.chat_update(
                            channel=channel, ts=message_ts,
                            text=":stop_sign: _Interrupted_",
                        )
                    # Swap eyes → stop sign reaction on user's message
                    try:
                        await client.reactions_remove(
                            channel=channel, name="eyes", timestamp=event["ts"]
                        )
                        await client.reactions_add(
                            channel=channel, name="octagonal_sign", timestamp=event["ts"]
                        )
                    except Exception:
                        pass
                    # Thread-root: swap eyes/speech_balloon → stop sign
                    if thread_ts != event["ts"]:
                        for int_emoji in ("eyes", "speech_balloon"):
                            await _remove_thread_reaction(client, channel, session_info, int_emoji)
                        await _add_thread_reaction(client, channel, session_info, "octagonal_sign")
                return

            # Final: send remaining text
            if full_text:
                mrkdwn = _markdown_to_mrkdwn(full_text)

                if len(mrkdwn) > SNIPPET_THRESHOLD:
                    # Long output → upload as a snippet file
                    if first_activity:
                        await client.chat_update(
                            channel=channel, ts=message_ts,
                            text=":page_facing_up: Response uploaded as snippet (too long for a message).",
                        )
                    await _send_snippet(client, channel, thread_ts, mrkdwn)
                else:
                    chunks = _split_message(mrkdwn)
                    if first_activity:
                        # No tool activities were posted — update the placeholder
                        await client.chat_update(
                            channel=channel, ts=message_ts, text=chunks[0],
                        )
                        for chunk in chunks[1:]:
                            await client.chat_postMessage(
                                channel=channel, thread_ts=thread_ts, text=chunk,
                            )
                    else:
                        # Tool activities were posted — send text as thread replies
                        for chunk in chunks:
                            await client.chat_postMessage(
                                channel=channel, thread_ts=thread_ts, text=chunk,
                            )
            else:
                logger.warning(
                    f"Empty response from Claude: {event_count} events received, "
                    f"session_id={session.session_id}, "
                    f"handoff={handoff_session_id}, reconnect={is_reconnect}"
                )
                msg = (
                    ":warning: Claude returned an empty response. "
                    "This usually means the session is still active in a terminal. "
                    "Please close that Claude Code session first (type `/exit` or quit), "
                    "then try again."
                )
                await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=msg,
                )

            # Update cumulative session stats and post completion summary
            if result_event:
                session_info.total_requests += 1
                if result_event.num_turns is not None:
                    session_info.total_turns += result_event.num_turns
                if result_event.cost_usd is not None:
                    session_info.total_cost_usd += result_event.cost_usd

                summary = _format_completion_summary(result_event, session_info)
                if summary:
                    await client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts, text=summary,
                    )

                # Surface permission denials so users know why tools were blocked
                denials = result_event.permission_denials
                if denials:
                    names = sorted({d.get("tool_name", "unknown") for d in denials})
                    note = (
                        f":no_entry_sign: {len(denials)} tool permission"
                        f"{'s' if len(denials) != 1 else ''}"
                        f" denied: {', '.join(f'`{n}`' for n in names)}"
                    )
                    await client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts, text=note,
                    )
                    # Thread-root: :warning: signals "completed but something was blocked"
                    await _add_thread_reaction(client, channel, session_info, "warning")

            # Swap eyes for checkmark on the user's message
            try:
                await client.reactions_remove(
                    channel=channel, name="eyes", timestamp=event["ts"]
                )
                await client.reactions_add(
                    channel=channel, name="white_check_mark", timestamp=event["ts"]
                )
            except Exception:
                pass

            # Thread-root status: swap eyes → checkmark (or speech_balloon
            # if the response ends with a question needing user attention).
            if thread_ts != event["ts"]:
                ends_with_question = _text_ends_with_question(full_text)
                await _remove_thread_reaction(client, channel, session_info, "eyes")
                if ends_with_question:
                    # Keep/add speech_balloon — user needs to respond
                    await _add_thread_reaction(client, channel, session_info, "speech_balloon")
                else:
                    # Normal completion — checkmark
                    await _remove_thread_reaction(client, channel, session_info, "speech_balloon")
                    await _add_thread_reaction(client, channel, session_info, "white_check_mark")

        except Exception as exc:
            logger.exception(f"Error processing message: {exc}")
            try:
                await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=f":x: Error: {exc}",
                )
            except Exception:
                logger.debug("Failed to post error message to Slack", exc_info=True)
            # Error reactions on the user's message
            try:
                await client.reactions_remove(
                    channel=channel, name="eyes", timestamp=event["ts"]
                )
                await client.reactions_add(
                    channel=channel, name="x", timestamp=event["ts"]
                )
            except Exception:
                pass
            # Thread-root status: swap eyes/speech_balloon → x on error
            if thread_ts != event["ts"]:
                for err_emoji in ("eyes", "speech_balloon"):
                    await _remove_thread_reaction(client, channel, session_info, err_emoji)
                await _add_thread_reaction(client, channel, session_info, "x")


async def _fetch_thread_history(
    channel: str,
    thread_ts: str,
    current_ts: str,
    client: AsyncWebClient,
) -> str | None:
    """Fetch thread history from Slack and format as a conversation transcript.

    Used on reconnect to rebuild context for a new Claude session.
    Excludes the current message (which will be sent as the actual prompt).
    """
    try:
        auth = await client.auth_test()
        bot_id = auth["user_id"]

        replies = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=100
        )
        messages = replies.get("messages", [])

        lines = []
        for msg in messages:
            # Skip the current message — it becomes the prompt
            if msg.get("ts") == current_ts:
                continue

            text = msg.get("text", "").strip()
            if not text:
                continue

            if msg.get("user") == bot_id:
                lines.append(f"[Chicane] {text}")
            else:
                # Strip bot mentions from user messages
                clean = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
                if clean:
                    lines.append(f"[User] {clean}")

        if not lines:
            return None

        logger.debug(
            f"Rebuilt {len(lines)} messages of thread history for {thread_ts}"
        )
        return "\n".join(lines)

    except Exception:
        logger.exception(f"Failed to fetch thread history for {thread_ts}")
        return None


async def _find_session_id_in_thread(
    channel: str,
    thread_ts: str,
    client: AsyncWebClient,
) -> str | None:
    """Scan thread messages for a handoff session_id.

    Returns the session_id if found in any message, otherwise None.
    Checks both thread replies and the thread starter message explicitly.
    Used on reconnect to resume the original Claude session instead of
    rebuilding context from scratch.
    """
    try:
        replies = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=100
        )
        for msg in replies.get("messages", []):
            text = msg.get("text", "")
            m = _HANDOFF_RE.search(text)
            if m:
                logger.debug(f"Found session_id in thread reply: {m.group(1)}")
                return m.group(1)
    except Exception:
        logger.warning(
            f"Could not scan thread {thread_ts} for session_id", exc_info=True
        )

    # Fallback: fetch the thread starter message directly.
    # conversations_replies should include it, but fetch it explicitly
    # in case the thread is new or the API didn't return it above.
    try:
        resp = await client.conversations_history(
            channel=channel, latest=thread_ts, inclusive=True, limit=1
        )
        for msg in resp.get("messages", []):
            text = msg.get("text", "")
            m = _HANDOFF_RE.search(text)
            if m:
                logger.debug(
                    f"Found session_id in thread starter message: {m.group(1)}"
                )
                return m.group(1)
    except Exception:
        logger.warning(
            f"Could not fetch thread starter {thread_ts} for session_id",
            exc_info=True,
        )

    logger.debug(f"No session_id found in thread {thread_ts}")
    return None


async def _bot_in_thread(
    thread_ts: str,
    channel: str,
    client: AsyncWebClient,
) -> bool:
    """Check if the bot has previously posted in this thread."""
    try:
        auth = await client.auth_test()
        bot_id = auth["user_id"]
        replies = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=50
        )
        for msg in replies.get("messages", []):
            if msg.get("user") == bot_id:
                return True
    except Exception:
        logger.warning(f"Could not check thread history for {thread_ts}", exc_info=True)
    return False


async def _resolve_channel_cwd(
    channel_id: str,
    client: AsyncWebClient,
    config: Config,
) -> Path | None:
    """Resolve working directory based on channel name.

    Looks up the channel name, checks if it's whitelisted in CHANNEL_DIRS,
    and returns the mapped directory path. Returns None to use default.
    """
    if not config.channel_dirs:
        return None

    try:
        info = await client.conversations_info(channel=channel_id)
        channel_name = info["channel"]["name"]
    except Exception:
        return None

    resolved = config.resolve_channel_dir(channel_name)
    if resolved:
        logger.debug(f"Channel #{channel_name} → cwd {resolved}")
    return resolved


async def _download_files(
    event: dict,
    token: str,
    target_dir: Path,
) -> list[tuple[str, Path, str]]:
    """Download Slack file attachments to *target_dir*.

    Returns a list of ``(original_name, local_path, mimetype)`` tuples for
    every file successfully downloaded.  Skips files that are too large,
    lack a download URL, or fail to download.
    """
    files = event.get("files", [])
    if not files:
        return []

    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[tuple[str, Path, str]] = []

    async with aiohttp.ClientSession() as http:
        for f in files:
            name = f.get("name", "attachment")
            mimetype = f.get("mimetype", "application/octet-stream")
            url = f.get("url_private_download")
            size = f.get("size", 0)

            if not url:
                logger.warning(f"File {name} has no download URL, skipping")
                continue
            if size > MAX_FILE_SIZE:
                logger.warning(
                    f"File {name} too large ({size} bytes > {MAX_FILE_SIZE}), skipping"
                )
                continue

            try:
                async with http.get(
                    url, headers={"Authorization": f"Bearer {token}"}
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"Failed to download {name}: HTTP {resp.status}"
                        )
                        continue
                    content_type = resp.content_type or ""
                    if content_type.startswith("text/html"):
                        logger.warning(
                            f"File {name} returned HTML instead of file data "
                            f"(missing files:read scope?), skipping"
                        )
                        continue
                    data = await resp.read()

                local_path = target_dir / name
                counter = 1
                while local_path.exists():
                    stem = Path(name).stem
                    suffix = Path(name).suffix
                    local_path = target_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                local_path.write_bytes(data)
                downloaded.append((name, local_path, mimetype))
                logger.info(f"Downloaded {name} ({len(data)} bytes) → {local_path}")
            except Exception:
                logger.exception(f"Failed to download file {name}")

    return downloaded


def _has_git_commit(event: ClaudeEvent) -> bool:
    """Check if an assistant event contains a Bash tool_use running git commit."""
    message = event.raw.get("message", {})
    for block in message.get("content", []):
        if block.get("type") != "tool_use":
            continue
        logger.debug("_has_git_commit checking tool_use block: name=%s input=%s", block.get("name"), block.get("input"))
        if block.get("name") != "Bash":
            continue
        cmd = block.get("input", {}).get("command", "")
        if re.search(r"\bgit\b.*\bcommit\b", cmd):
            logger.debug("_has_git_commit matched git commit in: %s", cmd)
            return True
    return False


# Tools that modify files on disk.
_FILE_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


def _has_file_edit(event: ClaudeEvent) -> bool:
    """Check if an assistant event contains a tool_use that modifies files."""
    message = event.raw.get("message", {})
    for block in message.get("content", []):
        if block.get("type") != "tool_use":
            continue
        if block.get("name") in _FILE_EDIT_TOOLS:
            return True
    return False


def _has_question(event: ClaudeEvent) -> bool:
    """Check if an assistant event contains an AskUserQuestion tool_use."""
    message = event.raw.get("message", {})
    for block in message.get("content", []):
        if block.get("type") != "tool_use":
            continue
        if block.get("name") == "AskUserQuestion":
            return True
    return False


def _text_ends_with_question(text: str) -> bool:
    """Check if the final non-empty line of text ends with a question mark."""
    if not text:
        return False
    # Strip trailing whitespace/newlines and check last character
    stripped = text.rstrip()
    if not stripped:
        return False
    # Check last meaningful character (ignore trailing punctuation like ')' after '?')
    return stripped.endswith("?")


def _summarize_tool_input(tool_input: dict, max_params: int = 6) -> str:
    """Build a multi-line summary of tool input args for display.

    Picks short scalar values (strings, numbers, bools) and formats
    each as its own line. Skips large blobs, nested objects, and
    internal-looking keys.
    """
    lines: list[str] = []
    for key, val in tool_input.items():
        if len(lines) >= max_params:
            break
        if isinstance(val, str):
            if not val or len(val) > 120:
                continue
            snippet = val if len(val) <= 60 else val[:57] + "..."
        elif isinstance(val, bool):
            snippet = str(val).lower()
        elif isinstance(val, (int, float)):
            snippet = str(val)
        else:
            continue
        lines.append(f"  {key}: `{snippet}`")
    return "\n".join(lines)


def _format_tool_activity(event: ClaudeEvent) -> list[str]:
    """Extract tool_use blocks from an assistant event and return human-readable one-liners."""
    message = event.raw.get("message", {})
    content = message.get("content", [])

    activities = []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        tool_name = block.get("name", "unknown")
        tool_input = block.get("input", {})

        if tool_name == "Read":
            file_path = tool_input.get("file_path", "")
            basename = Path(file_path).name if file_path else "file"
            activities.append(f":mag: Reading `{basename}`")
        elif tool_name == "Bash":
            cmd = tool_input.get("command", "")
            activities.append(f":computer: Running `{cmd}`")
        elif tool_name == "Edit":
            file_path = tool_input.get("file_path", "")
            basename = Path(file_path).name if file_path else "file"
            activities.append(f":pencil2: Editing `{basename}`")
        elif tool_name == "Write":
            file_path = tool_input.get("file_path", "")
            basename = Path(file_path).name if file_path else "file"
            activities.append(f":pencil2: Writing `{basename}`")
        elif tool_name == "Grep":
            pattern = tool_input.get("pattern", "")
            activities.append(f":mag: Searching for `{pattern}`")
        elif tool_name == "Glob":
            pattern = tool_input.get("pattern", "")
            activities.append(f":mag: Finding files `{pattern}`")
        elif tool_name == "WebFetch":
            url = tool_input.get("url", "")
            if url:
                activities.append(f":globe_with_meridians: Fetching `{url}`")
            else:
                activities.append(":globe_with_meridians: Fetching URL")
        elif tool_name == "WebSearch":
            query = tool_input.get("query", "")
            if query:
                activities.append(f":globe_with_meridians: Searching web for `{query}`")
            else:
                activities.append(":globe_with_meridians: Searching web")
        elif tool_name == "Task":
            subagent_type = tool_input.get("subagent_type", "")
            description = tool_input.get("description", "")
            if subagent_type or description:
                parts = [p for p in [subagent_type, description] if p]
                activities.append(f":robot_face: Spawning {': '.join(parts)}")
            else:
                activities.append(":robot_face: Spawning subagent")
        elif tool_name == "Skill":
            skill = tool_input.get("skill", "")
            if skill:
                activities.append(f":zap: Running skill `{skill}`")
            else:
                activities.append(":zap: Running skill")
        elif tool_name == "NotebookEdit":
            notebook_path = tool_input.get("notebook_path", "")
            basename = Path(notebook_path).name if notebook_path else "notebook"
            activities.append(f":notebook: Editing notebook `{basename}`")
        elif tool_name == "EnterPlanMode":
            activities.append(":clipboard: Entering plan mode")
        elif tool_name == "TodoWrite":
            todos = tool_input.get("todos", [])
            if todos:
                _STATUS_EMOJI = {
                    "completed": ":white_check_mark:",
                    "in_progress": ":arrows_counterclockwise:",
                    "pending": ":white_circle:",
                }
                lines = [":clipboard: *Tasks*"]
                for todo in todos:
                    status = todo.get("status", "pending")
                    emoji = _STATUS_EMOJI.get(status, ":white_circle:")
                    label = todo.get("content", "?")
                    lines.append(f"{emoji} {label}")
                activities.append("\n".join(lines))
            else:
                activities.append(":clipboard: Updating tasks")
        elif tool_name == "AskUserQuestion":
            questions = tool_input.get("questions", [])
            if questions:
                lines = [":question: *Claude is asking:*"]
                for q in questions:
                    text = q.get("question", "")
                    if text:
                        lines.append(f"  {text}")
                    options = q.get("options", [])
                    for opt in options:
                        label = opt.get("label", "")
                        desc = opt.get("description", "")
                        if label and desc:
                            lines.append(f"    • *{label}* — {desc}")
                        elif label:
                            lines.append(f"    • *{label}*")
                activities.append("\n".join(lines))
            else:
                activities.append(":question: Asking user a question")
        else:
            # Clean up tool names for display: strip MCP prefixes
            # (mcp__server__tool → Tool), split snake/camel case.
            display = tool_name
            server_prefix = ""
            if display.startswith("mcp__"):
                parts = display.split("__")
                if len(parts) >= 3:
                    server_prefix = parts[1]
                    display = parts[-1]
            # Split CamelCase then underscores, title-case each word
            display = re.sub(r"([a-z])([A-Z])", r"\1 \2", display)
            display = display.replace("_", " ").strip().title()

            # Summarize input args: each param on its own line
            arg_summary = _summarize_tool_input(tool_input)
            label = f"{server_prefix}: {display}" if server_prefix else display
            if arg_summary:
                activities.append(f":wrench: {label}\n{arg_summary}")
            else:
                activities.append(f":wrench: {label}")

    return activities


_ERROR_SUBTYPE_LABELS = {
    "error_max_turns": "hit max turns limit",
    "error_during_execution": "error during execution",
    "error_max_budget_usd": "hit budget limit",
    "error_max_structured_output_retries": "structured output validation failed",
}


def _format_completion_summary(
    event: ClaudeEvent,
    session_info: SessionInfo | None = None,
) -> str | None:
    """Format a completion footer from a result event.

    When *session_info* is provided and the session has handled more than one
    request, a cumulative stats line is appended (total turns, cost, requests).
    """
    if event.num_turns is None:
        return None
    turns = f"{event.num_turns} turn{'s' if event.num_turns != 1 else ''}"
    emoji = ":checkered_flag:" if not event.is_error else ":x:"

    # Build error reason suffix for non-success results
    reason = ""
    if event.is_error and event.subtype:
        label = _ERROR_SUBTYPE_LABELS.get(event.subtype)
        if label:
            reason = f" ({label})"

    # Cost reporting (tracked for all users — subscription and API)
    cost = ""
    if event.cost_usd is not None and event.cost_usd > 0:
        cost = f" · ${event.cost_usd:.2f}"

    if event.duration_ms is not None:
        secs = event.duration_ms / 1000
        if secs >= 60:
            mins = int(secs // 60)
            remaining = int(secs % 60)
            duration = f"{mins}m{remaining}s"
        else:
            duration = f"{int(secs)}s"
        line = f"{emoji} {turns} took {duration}{reason}{cost}"
    else:
        line = f"{emoji} Done — {turns}{reason}{cost}"

    # Append cumulative session stats after the 1st request
    if session_info and session_info.total_requests > 1:
        parts = [f"{session_info.total_requests} requests"]
        parts.append(f"{session_info.total_turns} turns total")
        if session_info.total_cost_usd > 0:
            parts.append(f"${session_info.total_cost_usd:.2f} session total")
        line += f"\n:bar_chart: {' · '.join(parts)}"

    return line


def _markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format.

    Preserves code blocks (``` and inline `), then converts:
    - **bold** / __bold__  →  *bold*  (with zero-width space buffering)
    - *italic* / _italic_  →  _italic_  (already valid)
    - ~~strikethrough~~    →  ~strikethrough~  (with zero-width space buffering)
    - [text](url)          →  <url|text>
    - [text][ref]          →  <url|text>  (reference-style links)
    - ![alt](url)          →  <url|alt>
    - <user@example.com>   →  <mailto:user@example.com>
    - # / ## / ### headers →  *Header* (bold line)
    - > blockquotes        →  > (already valid in Slack)
    - Markdown tables      →  preformatted text
    - Horizontal rules     →  ———
    - - / * / + list items →  • list items
    - - [x] / - [ ]        →  ☒ / ☐ (task lists)
    - <!-- comments -->    →  removed
    - HTML entities         →  & < > escaped outside code
    """
    # Zero-width space for buffering formatting markers in ambiguous contexts
    _ZWS = "\u200b"

    # ── Collect link reference definitions before any processing ──
    # [ref]: url or [ref]: url "title"
    _ref_defs: dict[str, str] = {}
    def _collect_ref(m: re.Match) -> str:
        label = m.group(1).lower()
        url = m.group(2)
        _ref_defs[label] = url
        return ""  # Remove the definition line
    text = re.sub(
        r"^\[([^\]]+)\]:\s+(\S+)(?:\s+[\"'(].*[\"')])?\s*$",
        _collect_ref, text, flags=re.MULTILINE,
    )

    # ── Protect code blocks and inline code from conversion ──
    placeholders: list[str] = []

    def _protect(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00PROTECTED{len(placeholders) - 1}\x00"

    # Protect fenced code blocks first, then inline code
    text = re.sub(r"```[\s\S]*?```", _protect, text)
    text = re.sub(r"`[^`\n]+`", _protect, text)

    # ── Remove HTML comments ──
    text = re.sub(r"<!--[\s\S]*?-->", "", text)

    # ── Escape HTML entities (& < >) ──
    # Must happen before we insert our own angle-bracket links.
    # Preserve > at start of line (blockquotes) and existing Slack-style
    # angle-bracket constructs like <url|text> or <@U123>.
    text = text.replace("&", "&amp;")
    # Escape < except when it starts a Slack link/mention pattern or protected placeholder
    text = re.sub(r"<(?![\x00!@#])", "&lt;", text)
    # Escape > except at start of line (blockquotes)
    text = re.sub(r"(?<!^)>", "&gt;", text, flags=re.MULTILINE)
    # Restore > in blockquotes — already preserved by the negative lookbehind above

    # ── Tables → preformatted text ──
    def _convert_table(m: re.Match) -> str:
        table_text = m.group(0)
        lines = [
            line for line in table_text.split("\n")
            if not re.match(r"^\|[\s\-:|]+\|$", line.strip())
        ]
        return "```\n" + "\n".join(lines) + "\n```"

    text = re.sub(r"(?:^\|.+\|\n?){2,}", _convert_table, text, flags=re.MULTILINE)

    # ── Images: ![alt](url) → <url|alt> ──
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"<\2|\1>", text)

    # ── Inline links: [text](url) → <url|text> ──
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # ── Reference links: [text][ref] → <url|text> ──
    def _resolve_ref_link(m: re.Match) -> str:
        link_text = m.group(1)
        ref_label = (m.group(2) or link_text).lower()
        url = _ref_defs.get(ref_label)
        if url:
            return f"<{url}|{link_text}>"
        return m.group(0)  # Leave unchanged if ref not found

    text = re.sub(r"\[([^\]]+)\]\[([^\]]*)\]", _resolve_ref_link, text)

    # ── Email links: <user@example.com> → <mailto:user@example.com> ──
    text = re.sub(
        r"&lt;([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})&gt;",
        r"<mailto:\1>", text,
    )

    # ── Bold: **text** or __text__ → *text* with ZWS buffering ──
    text = re.sub(r"\*\*(.+?)\*\*", rf"{_ZWS}*\1*{_ZWS}", text)
    text = re.sub(r"__(.+?)__", rf"{_ZWS}*\1*{_ZWS}", text)

    # ── Strikethrough: ~~text~~ → ~text~ with ZWS buffering ──
    text = re.sub(r"~~(.+?)~~", rf"{_ZWS}~\1~{_ZWS}", text)

    # ── Task lists: - [x] → ☒, - [ ] → ☐ (before bullet conversion) ──
    text = re.sub(r"^(\s*)[-*+]\s+\[x\]\s+", r"\1☒ ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s*)[-*+]\s+\[ \]\s+", r"\1☐ ", text, flags=re.MULTILINE)

    # ── List bullets: - / * / + items → • items ──
    text = re.sub(r"^(\s*)[-*+](\s+)", r"\1•\2", text, flags=re.MULTILINE)

    # ── Headers: # Header → *Header* ──
    text = re.sub(r"^#{1,6}\s+(.+)$", rf"*\1*", text, flags=re.MULTILINE)

    # ── Horizontal rules: --- or *** or ___ → ——— ──
    text = re.sub(r"^[\-\*_]{3,}\s*$", "———", text, flags=re.MULTILINE)

    # ── Restore protected code blocks ──
    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        return placeholders[idx]

    text = re.sub(r"\x00PROTECTED(\d+)\x00", _restore, text)

    # ── Clean up redundant ZWS (at start/end of line, doubled) ──
    text = re.sub(rf"{_ZWS}{{2,}}", _ZWS, text)
    text = re.sub(rf"^{_ZWS}|{_ZWS}$", "", text, flags=re.MULTILINE)

    return text


async def _send_snippet(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    text: str,
    initial_comment: str = "",
    *,
    _max_attempts: int = 2,
    _retry_delay: float = 2.0,
    _step_delay: float = 0.5,
) -> None:
    """Upload *text* as a Slack snippet file in the thread.

    Uses the three-step Slack upload flow (``getUploadURLExternal`` →
    PUT content → ``completeUploadExternal``).  A short delay is inserted
    between each step to avoid racing Slack's backend, and the whole
    sequence is retried once on failure before falling back to split
    messages.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, _max_attempts + 1):
        try:
            # Step 1 – obtain an upload URL and file id.
            size = len(text.encode("utf-8"))
            url_resp = await client.files_getUploadURLExternal(
                filename="response.md",
                length=size,
            )
            upload_url: str = url_resp["upload_url"]
            file_id: str = url_resp["file_id"]

            await asyncio.sleep(_step_delay)

            # Step 2 – upload the content to the URL Slack gave us.
            async with aiohttp.ClientSession() as http:
                put_resp = await http.put(
                    upload_url,
                    data=text.encode("utf-8"),
                    headers={"Content-Type": "text/markdown"},
                )
                put_resp.raise_for_status()

            await asyncio.sleep(_step_delay)

            # Step 3 – finalise the upload and share it in the thread.
            await client.files_completeUploadExternal(
                files=[{"id": file_id, "title": "Full response"}],
                channel_id=channel,
                thread_ts=thread_ts,
                initial_comment=initial_comment or None,
            )
            return  # success
        except (SlackApiError, aiohttp.ClientError, KeyError) as exc:
            last_exc = exc
            if attempt < _max_attempts:
                logger.info(
                    "Snippet upload attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt, _max_attempts, exc, _retry_delay,
                )
                await asyncio.sleep(_retry_delay)

    # All attempts exhausted – fall back to split messages.
    logger.warning(
        "Snippet upload failed after %d attempts, falling back to split messages",
        _max_attempts,
        exc_info=last_exc,
    )
    if initial_comment:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=initial_comment,
        )
    for chunk in _split_message(text):
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=chunk,
        )


def _split_message(text: str) -> list[str]:
    """Split text into chunks that fit Slack's message limit.

    Tries to split on newlines to avoid breaking mid-line.
    """
    if len(text) <= SLACK_MAX_LENGTH:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= SLACK_MAX_LENGTH:
            chunks.append(remaining)
            break

        # Find a good split point: last newline before the limit
        split_at = remaining.rfind("\n", 0, SLACK_MAX_LENGTH)
        if split_at < SLACK_MAX_LENGTH // 2:
            # No good newline found — split at limit
            split_at = SLACK_MAX_LENGTH

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    return chunks
