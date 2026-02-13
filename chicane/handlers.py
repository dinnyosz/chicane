"""Slack event handlers — routes messages to Claude and streams responses back."""

import logging
import re
from pathlib import Path

import aiohttp
from platformdirs import user_cache_dir
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from .claude import ClaudeSession
from .config import Config
from .sessions import SessionStore

logger = logging.getLogger(__name__)

# Max message length for Slack
SLACK_MAX_LENGTH = 3900

# Max file size to download from Slack (10 MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# Regex to detect a handoff session_id at the end of a prompt.
# Matches both plain  (session_id: uuid)  and Slack-italicised  _(session_id: uuid)_
_HANDOFF_RE = re.compile(r"_?\(session_id:\s*([a-f0-9\-]+)\)_?\s*$")


def register_handlers(app: AsyncApp, config: Config, sessions: SessionStore) -> None:
    """Register all Slack event handlers on the app."""
    bot_user_id: str | None = None
    processed_ts: set[str] = set()

    def _mark_processed(ts: str) -> bool:
        """Mark a message as processed. Returns False if already seen."""
        if ts in processed_ts:
            return False
        processed_ts.add(ts)
        # Keep the set bounded
        if len(processed_ts) > 500:
            processed_ts.clear()
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

    # Add eyes reaction to show we're working on it
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
    session = sessions.get_or_create(
        thread_ts=thread_ts,
        config=config,
        cwd=cwd,
        session_id=handoff_session_id,
    )

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
    result = await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Working on it...",
    )
    message_ts = result["ts"]

    # Stream Claude's response
    full_text = ""
    event_count = 0
    first_activity = True  # track whether to update placeholder or post new
    result_event = None  # capture result event for completion summary

    try:
        async for event_data in session.stream(prompt):
            event_count += 1
            if event_data.type == "assistant":
                # Flush any accumulated text before posting tool activity
                # so the message order matches Claude Code console.
                activities = _format_tool_activity(event_data)

                # Prefix subagent activities
                if event_data.parent_tool_use_id:
                    activities = [f":arrow_right_hook: {a}" for a in activities]

                if activities and full_text:
                    for chunk in _split_message(full_text):
                        await client.chat_postMessage(
                            channel=channel, thread_ts=thread_ts, text=chunk,
                        )
                    full_text = ""

                # Post tool activity
                for activity in activities:
                    if first_activity:
                        await client.chat_update(
                            channel=channel, ts=message_ts, text=activity,
                        )
                        first_activity = False
                    else:
                        await client.chat_postMessage(
                            channel=channel, thread_ts=thread_ts, text=activity,
                        )

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
                for error_msg in event_data.tool_errors:
                    truncated = (error_msg[:200] + "...") if len(error_msg) > 200 else error_msg
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f":warning: Tool error: {truncated}",
                    )

            elif event_data.type == "system" and event_data.subtype == "compact_boundary":
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

        # Final: send remaining text
        if full_text:
            chunks = _split_message(full_text)
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

        # Post completion summary from result event
        if result_event:
            summary = _format_completion_summary(result_event)
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

        # Swap eyes for checkmark
        try:
            await client.reactions_remove(
                channel=channel, name="eyes", timestamp=event["ts"]
            )
            await client.reactions_add(
                channel=channel, name="white_check_mark", timestamp=event["ts"]
            )
        except Exception:
            pass

    except Exception as exc:
        logger.exception(f"Error processing message: {exc}")
        await client.chat_update(
            channel=channel,
            ts=message_ts,
            text=f":x: Error: {exc}",
        )
        try:
            await client.reactions_remove(
                channel=channel, name="eyes", timestamp=event["ts"]
            )
            await client.reactions_add(
                channel=channel, name="x", timestamp=event["ts"]
            )
        except Exception:
            pass


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
            short_cmd = (cmd[:60] + "...") if len(cmd) > 60 else cmd
            activities.append(f":computer: Running `{short_cmd}`")
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
            if display.startswith("mcp__"):
                parts = display.split("__")
                display = parts[-1] if len(parts) >= 3 else display
            # Split CamelCase then underscores, title-case each word
            display = re.sub(r"([a-z])([A-Z])", r"\1 \2", display)
            display = display.replace("_", " ").strip().title()
            activities.append(f":wrench: {display}")

    return activities


_ERROR_SUBTYPE_LABELS = {
    "error_max_turns": "hit max turns limit",
    "error_during_execution": "error during execution",
    "error_max_budget_usd": "hit budget limit",
    "error_max_structured_output_retries": "structured output validation failed",
}


def _format_completion_summary(event: ClaudeEvent) -> str | None:
    """Format a completion footer from a result event."""
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

    if event.duration_ms is not None:
        secs = event.duration_ms / 1000
        if secs >= 60:
            mins = int(secs // 60)
            remaining = int(secs % 60)
            duration = f"{mins}m{remaining}s"
        else:
            duration = f"{int(secs)}s"
        return f"{emoji} {turns} took {duration}{reason}"
    return f"{emoji} Done — {turns}{reason}"


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
