"""Claude Agent SDK wrapper with streaming support.

Uses the ``claude-agent-sdk`` Python package (``ClaudeSDKClient``) to maintain
a persistent Claude Code session per Slack thread.  Each call to ``stream()``
sends a new user message into the *same* running session, enabling inter-turn
messaging and interruption.

Streaming input mode
--------------------
Messages are delivered via an ``asyncio.Queue`` feeding an async generator
passed to ``client.connect(prompt=generator)``.  The SDK runs the generator
as a concurrent background task, picking up new messages *between* agentic
turns — exactly like interactive Claude Code handles queued user input.
This means a user's follow-up sent mid-stream arrives at the next turn
boundary rather than waiting until the entire stream completes.
"""

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)

# Tools that need special handling in headless/Slack mode.
# EnterPlanMode and ExitPlanMode are auto-approved via PreToolUse hook.
# AskUserQuestion is routed through canUseTool to present questions in Slack.
_PLAN_MODE_TOOLS = frozenset({"EnterPlanMode", "ExitPlanMode"})


async def _auto_approve_plan_mode(input_data: dict, tool_use_id: str | None, context) -> dict:
    """PreToolUse hook that auto-approves plan mode tools.

    In headless/Slack mode there's no terminal UI to click approve, so
    EnterPlanMode and ExitPlanMode must be allowed programmatically.
    """
    tool_name = input_data.get("tool_name", "")
    if tool_name in _PLAN_MODE_TOOLS:
        logger.debug("Auto-approving %s via PreToolUse hook", tool_name)
        return {
            "hookSpecificOutput": {
                "hookEventName": input_data["hook_event_name"],
                "permissionDecision": "allow",
                "permissionDecisionReason": f"{tool_name} auto-approved in Slack mode",
            }
        }
    return {}


def _make_pre_compact_hook(event_queue: asyncio.Queue):
    """Create a PreCompact hook that pushes a synthetic event onto *event_queue*.

    The hook fires *before* compaction starts, giving handlers.py a chance to
    notify the user immediately rather than leaving them staring at silence
    until the ``compact_boundary`` SystemMessage arrives afterward.
    """

    async def _pre_compact_hook(input_data: dict, tool_use_id: str | None, context) -> dict:
        trigger = input_data.get("trigger", "auto")
        logger.info("PreCompact hook fired (trigger=%s)", trigger)
        await event_queue.put(
            ClaudeEvent(
                type="system",
                raw={"type": "system", "subtype": "pre_compact", "trigger": trigger},
            )
        )
        return {}

    return _pre_compact_hook


async def _dummy_hook(input_data: dict, tool_use_id: str | None, context) -> dict:
    """No-op PreToolUse hook required by the Python SDK.

    The SDK needs at least one catch-all PreToolUse hook that returns
    ``{"continue_": True}`` to keep the stream open when ``can_use_tool``
    is configured.  Without this, the stream closes before the permission
    callback can be invoked.
    """
    return {"continue_": True}


# Type alias for the async callback that posts AskUserQuestion to Slack
# and returns the user's answers.
# Signature: (questions: list[dict]) -> dict[str, str]  (question_text -> answer)
AskUserCallback = Callable[[list[dict]], Awaitable[dict[str, str]]]


# SDK message type → ClaudeEvent type string
_MSG_TYPE_MAP = {
    AssistantMessage: "assistant",
    UserMessage: "user",
    SystemMessage: "system",
    ResultMessage: "result",
}


def _content_blocks_to_dicts(
    blocks: list[TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock],
) -> list[dict]:
    """Convert SDK content blocks to the dict format ClaudeEvent expects."""
    result = []
    for block in blocks:
        if isinstance(block, TextBlock):
            result.append({"type": "text", "text": block.text})
        elif isinstance(block, ThinkingBlock):
            result.append(
                {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
            )
        elif isinstance(block, ToolUseBlock):
            result.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            result.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error or False,
                }
            )
    return result


def _sdk_message_to_raw(msg) -> dict:
    """Convert an SDK message to the raw dict format ClaudeEvent expects."""
    if isinstance(msg, AssistantMessage):
        raw = {
            "type": "assistant",
            "message": {"content": _content_blocks_to_dicts(msg.content)},
        }
        if msg.parent_tool_use_id:
            raw["parent_tool_use_id"] = msg.parent_tool_use_id
        return raw

    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            content = [{"type": "text", "text": msg.content}]
        else:
            content = _content_blocks_to_dicts(msg.content)

        # SDK may deliver tool results via `tool_use_result` instead of
        # (or in addition to) the content list.  Merge them so handlers
        # always find tool_result blocks in the standard location.
        has_tool_results = any(
            b.get("type") == "tool_result" for b in content
        )
        if not has_tool_results and msg.tool_use_result:
            tur = msg.tool_use_result
            content.append({
                "type": "tool_result",
                "tool_use_id": tur.get("tool_use_id", ""),
                "content": tur.get("content"),
                "is_error": tur.get("is_error", False),
            })


        raw: dict = {"type": "user", "message": {"content": content}}
        if msg.parent_tool_use_id:
            raw["parent_tool_use_id"] = msg.parent_tool_use_id
        return raw

    if isinstance(msg, SystemMessage):
        raw = {"type": "system", "subtype": msg.subtype, **msg.data}
        if msg.subtype == "init":
            raw["session_id"] = msg.data.get("session_id")
        return raw

    if isinstance(msg, ResultMessage):
        return {
            "type": "result",
            "subtype": msg.subtype,
            "result": msg.result or "",
            "is_error": msg.is_error,
            "num_turns": msg.num_turns,
            "duration_ms": msg.duration_ms,
            "duration_api_ms": msg.duration_api_ms,
            "total_cost_usd": msg.total_cost_usd,
            "session_id": msg.session_id,
        }

    return {"type": "unknown"}


@dataclass
class ClaudeEvent:
    """A parsed event from Claude's streaming output.

    This is the common interface used by handlers.py.  The ``raw`` dict
    mirrors the old stream-json format so all existing property accessors
    continue to work unchanged.
    """

    type: str  # "system", "assistant", "user", "result"
    raw: dict = field(repr=False)

    @property
    def subtype(self) -> str | None:
        return self.raw.get("subtype")

    @property
    def session_id(self) -> str | None:
        return self.raw.get("session_id")

    @property
    def text(self) -> str:
        """Extract text content from assistant messages."""
        if self.type == "assistant":
            message = self.raw.get("message", {})
            parts = message.get("content", [])
            return "".join(
                p.get("text", "") for p in parts if p.get("type") == "text"
            )
        if self.type == "result":
            return self.raw.get("result", "")
        return ""

    @property
    def is_error(self) -> bool:
        return self.raw.get("is_error", False)

    @property
    def cost_usd(self) -> float | None:
        return self.raw.get("total_cost_usd")

    @property
    def num_turns(self) -> int | None:
        return self.raw.get("num_turns")

    @property
    def duration_ms(self) -> int | None:
        return self.raw.get("duration_ms")

    @property
    def permission_denials(self) -> list[dict]:
        """Permission denials from a result event."""
        return self.raw.get("permission_denials", [])

    @property
    def errors(self) -> list[str]:
        """Error messages from error result events."""
        return self.raw.get("errors", [])

    @property
    def compact_metadata(self) -> dict | None:
        """Compaction info when subtype is 'compact_boundary'."""
        return self.raw.get("compact_metadata")

    @property
    def parent_tool_use_id(self) -> str | None:
        """Non-null when this event originates from a subagent."""
        return self.raw.get("parent_tool_use_id")

    @property
    def tool_errors(self) -> list[tuple[str, str]]:
        """Extract (tool_use_id, error_msg) from tool_result blocks in user events."""
        if self.type != "user":
            return []
        message = self.raw.get("message", {})
        content = message.get("content", [])
        errors: list[tuple[str, str]] = []
        for block in content:
            if block.get("type") == "tool_result" and block.get("is_error"):
                tool_use_id = block.get("tool_use_id", "")
                text = block.get("content", "")
                if isinstance(text, list):
                    text = "".join(
                        p.get("text", "") for p in text if isinstance(p, dict)
                    )
                if text:
                    # Unescape HTML entities (&lt; → <) and strip XML-like
                    # wrapper tags (e.g. <tool_use_error>...</tool_use_error>)
                    # so Slack output is clean.
                    text = html.unescape(text)
                    text = re.sub(r"</?[a-z_]+>", "", text).strip()
                    if text:
                        errors.append((tool_use_id, text))
        return errors

    @property
    def tool_use_ids(self) -> dict[str, str]:
        """Map tool_use_id -> tool_name from tool_use blocks in assistant events."""
        if self.type != "assistant":
            return {}
        message = self.raw.get("message", {})
        content = message.get("content", [])
        return {
            block["id"]: block.get("name", "unknown")
            for block in content
            if block.get("type") == "tool_use" and "id" in block
        }

    @property
    def tool_use_inputs(self) -> dict[str, dict]:
        """Map tool_use_id -> input dict from tool_use blocks in assistant events."""
        if self.type != "assistant":
            return {}
        message = self.raw.get("message", {})
        content = message.get("content", [])
        return {
            block["id"]: block.get("input", {})
            for block in content
            if block.get("type") == "tool_use" and "id" in block
        }

    @property
    def tool_results(self) -> list[tuple[str, str]]:
        """Extract (tool_use_id, text) from successful tool_result blocks.

        Returns tuples so callers can correlate results back to the tool
        that produced them via the tool_use_id.
        """
        if self.type != "user":
            return []
        message = self.raw.get("message", {})
        content = message.get("content", [])
        results = []
        for block in content:
            if block.get("type") == "tool_result" and not block.get("is_error"):
                tool_use_id = block.get("tool_use_id", "")
                text = block.get("content") or ""
                if isinstance(text, list):
                    text = "".join(
                        p.get("text", "") for p in text if isinstance(p, dict)
                    )
                if text:
                    results.append((tool_use_id, text))
        return results


class ClaudeSession:
    """Manages a persistent Claude Agent SDK session for a single conversation.

    Uses the SDK's **streaming input mode**: an ``asyncio.Queue`` feeds an
    async generator that is passed to ``client.query()`` once.  Subsequent
    messages are pushed into the queue and the SDK picks them up between
    agentic turns — exactly like interactive Claude Code handles queued
    user input.
    """

    def __init__(
        self,
        cwd: Path | None = None,
        session_id: str | None = None,
        model: str | None = None,
        permission_mode: str = "default",
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        setting_sources: list[str] | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        ask_user_callback: AskUserCallback | None = None,
    ):
        self.cwd = cwd or Path.cwd()
        self.session_id = session_id
        self.model = model
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools or []
        self.disallowed_tools = disallowed_tools or []
        self.setting_sources = setting_sources or ["user", "project", "local"]
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self._ask_user_callback = ask_user_callback
        self._client: ClaudeSDKClient | None = None
        self._connected = False
        self._is_streaming = False
        self._interrupted = False
        self._interrupt_source: str | None = None  # "reaction" or "new_message"
        # Streaming input queue — messages pushed here are yielded by
        # _message_generator() and picked up by the SDK between turns.
        self._message_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._generator_started = False
        # Count of between-turn messages queued via queue_message() during
        # an active stream.  Checked at ResultMessage to decide whether to
        # continue iterating for the next turn.
        self._between_turn_pending = 0
        # Queue for synthetic events emitted by hooks (e.g. PreCompact).
        # The stream() loop drains this between SDK messages.
        self._synthetic_events: asyncio.Queue[ClaudeEvent] = asyncio.Queue()

    async def _message_generator(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator that feeds the SDK's streaming input.

        Blocks on ``_message_queue.get()`` and yields each message in the
        format expected by the SDK transport.  A ``None`` sentinel stops
        the generator (used during ``disconnect()``).
        """
        while True:
            prompt = await self._message_queue.get()
            if prompt is None:  # sentinel — time to shut down
                return
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }

    def _build_options(self) -> ClaudeAgentOptions:
        """Build SDK options from session config."""
        opts = ClaudeAgentOptions(
            cwd=self.cwd,
            allowed_tools=list(self.allowed_tools),
            disallowed_tools=list(self.disallowed_tools),
            setting_sources=list(self.setting_sources),
            max_buffer_size=100_000_000,  # 100MB — SDK default is 1MB
        )

        if self.session_id:
            opts.resume = self.session_id

        if self.model:
            opts.model = self.model

        if self.permission_mode and self.permission_mode != "default":
            opts.permission_mode = self.permission_mode

        if self.max_turns is not None:
            opts.max_turns = self.max_turns

        if self.max_budget_usd is not None:
            opts.max_budget_usd = self.max_budget_usd

        # Only send system prompt on the first invocation — resumed sessions
        # already have it, so resending wastes tokens.
        if self.system_prompt and not self.session_id:
            opts.system_prompt = self.system_prompt

        # --- Headless hooks & permission handling ---
        # Auto-approve EnterPlanMode/ExitPlanMode (no terminal UI to click).
        # The dummy hook is required by the Python SDK to keep the stream
        # open when can_use_tool is set (see SDK docs).
        hooks: dict[str, list] = {
            "PreToolUse": [
                HookMatcher(
                    matcher="EnterPlanMode|ExitPlanMode",
                    hooks=[_auto_approve_plan_mode],
                ),
                HookMatcher(matcher=None, hooks=[_dummy_hook]),
            ],
            "PreCompact": [
                HookMatcher(
                    matcher=None,
                    hooks=[_make_pre_compact_hook(self._synthetic_events)],
                ),
            ],
        }
        opts.hooks = hooks

        # Route AskUserQuestion through canUseTool so we can present
        # questions in Slack and wait for the user's reply.
        if self._ask_user_callback:
            callback = self._ask_user_callback

            async def _can_use_tool(
                tool_name: str,
                input_data: dict,
                context: ToolPermissionContext,
            ) -> PermissionResultAllow | PermissionResultDeny:
                if tool_name == "AskUserQuestion":
                    questions = input_data.get("questions", [])
                    try:
                        answers = await callback(questions)
                    except Exception:
                        logger.exception("AskUserQuestion callback failed")
                        return PermissionResultDeny(
                            message="Failed to collect user answers via Slack."
                        )
                    return PermissionResultAllow(
                        updated_input={
                            "questions": questions,
                            "answers": answers,
                        }
                    )
                # Everything else: allow (permission mode handles the rest)
                return PermissionResultAllow(updated_input=input_data)

            opts.can_use_tool = _can_use_tool

        return opts

    async def _ensure_connected(
        self, *, max_retries: int = 2, base_delay: float = 2.0,
    ) -> ClaudeSDKClient:
        """Connect the SDK client if not already connected.

        Retries on timeout errors (SDK ``initialize`` handshake can be slow
        on cold starts).  Other exceptions propagate immediately.
        """
        if self._client is not None and self._connected:
            return self._client

        last_exc: Exception | None = None
        for attempt in range(1 + max_retries):
            opts = self._build_options()
            client = ClaudeSDKClient(options=opts)
            try:
                await client.connect(prompt=self._message_generator())
            except (TimeoutError, asyncio.TimeoutError) as exc:
                last_exc = exc
                self._client = None
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "SDK connect timeout (attempt %d/%d), retrying in %.1fs...",
                        attempt + 1, 1 + max_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception as exc:
                # Check if the cause is a timeout wrapped in a generic Exception
                if "timeout" in str(exc).lower() and attempt < max_retries:
                    last_exc = exc
                    self._client = None
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "SDK connect timeout (attempt %d/%d), retrying in %.1fs...",
                        attempt + 1, 1 + max_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Don't leave a half-initialised client reference
                self._client = None
                raise

            self._client = client
            self._connected = True
            self._generator_started = True
            if attempt > 0:
                logger.info(
                    "SDK client connected after %d retries (session_id=%s, cwd=%s)",
                    attempt, self.session_id, self.cwd,
                )
            else:
                logger.info(
                    "SDK client connected (session_id=%s, cwd=%s)",
                    self.session_id, self.cwd,
                )
            return self._client

        # Shouldn't reach here, but just in case
        raise last_exc or Exception("SDK connect failed after retries")

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def was_interrupted(self) -> bool:
        return self._interrupted

    @property
    def interrupt_source(self) -> str | None:
        """Why the stream was interrupted: "reaction" or "new_message"."""
        return self._interrupt_source

    async def interrupt(self, source: str = "reaction") -> None:
        """Interrupt the current stream (sends interrupt signal via SDK).

        *source* identifies why the stream was interrupted (e.g.
        ``"reaction"`` when the user clicks the stop emoji).

        Note: new-message interrupts have been replaced by message
        queueing in handlers.py — messages arriving during an active
        stream are queued and drained after the stream completes.
        """
        if self._client and self._is_streaming:
            self._interrupted = True
            self._interrupt_source = source
            await self._client.interrupt()
            logger.info("Interrupted active stream (source=%s)", source)

    async def queue_message(self, prompt: str) -> None:
        """Queue a message for between-turn delivery during active streaming.

        The SDK picks up queued messages at the next turn boundary — after
        the current tool execution finishes but before the next one starts.
        """
        self._between_turn_pending += 1
        await self._message_queue.put(prompt)
        logger.info("Queued between-turn message (%d chars, %d pending)",
                     len(prompt), self._between_turn_pending)

    async def stream(self, prompt: str) -> AsyncIterator[ClaudeEvent]:
        """Send a message and yield streaming events.

        On the first call this connects the SDK client.  On subsequent calls
        it reuses the same client, pushing the new prompt into the streaming
        input queue so the SDK picks it up between turns.
        """
        client = await self._ensure_connected()
        self._is_streaming = True
        self._interrupted = False
        self._interrupt_source = None
        self._between_turn_pending = 0

        # Push prompt into the generator — the SDK picks it up between turns
        await self._message_queue.put(prompt)

        event_count = 0
        try:
            # Manual iteration so we can catch MessageParseError per-message
            # and continue the stream.  The SDK raises this for message types
            # it doesn't recognise yet (e.g. rate_limit_event) which would
            # otherwise kill the entire stream.
            response_iter = client.receive_messages().__aiter__()
            while True:
                try:
                    msg = await response_iter.__anext__()
                except StopAsyncIteration:
                    break
                except MessageParseError as exc:
                    logger.warning("SDK MessageParseError (skipped): %s", exc)
                    continue

                # Drain any synthetic events pushed by hooks (e.g. PreCompact)
                # before yielding the SDK message.  This ensures the
                # "compacting…" notification reaches Slack *before* the
                # post-compaction boundary event.
                while not self._synthetic_events.empty():
                    synthetic = self._synthetic_events.get_nowait()
                    event_count += 1
                    yield synthetic

                event_type = _MSG_TYPE_MAP.get(type(msg), "unknown")
                raw = _sdk_message_to_raw(msg)
                event = ClaudeEvent(type=event_type, raw=raw)
                event_count += 1

                # Capture session_id from init event
                if event.type == "system" and event.subtype == "init":
                    self.session_id = event.session_id
                    logger.info(f"Session started: {self.session_id}")

                yield event

                # Stop at ResultMessage — like receive_response() does,
                # but only when no between-turn messages are waiting.
                # If the user pushed a follow-up via queue_message(),
                # the SDK will deliver it as the next turn and we keep
                # iterating.
                if isinstance(msg, ResultMessage):
                    if self._between_turn_pending <= 0:
                        break
                    # Consume one pending count and continue
                    self._between_turn_pending -= 1
                    logger.info("Result received but %d between-turn messages pending; continuing",
                                self._between_turn_pending)
        finally:
            self._is_streaming = False
            if event_count == 0:
                logger.warning(
                    f"Claude produced no events. session_id={self.session_id}"
                )

    async def run(self, prompt: str) -> str:
        """Run a prompt and return the final result text."""
        result_text = ""
        async for event in self.stream(prompt):
            if event.type == "result":
                result_text = event.text
        return result_text

    async def disconnect(self) -> None:
        """Disconnect the SDK client.

        Sends a sentinel to stop the streaming input generator before
        tearing down the SDK connection.

        The SDK uses anyio task groups internally.  When ``disconnect()``
        is called from a *different* asyncio task than the one that created
        the connection (e.g. during shutdown), anyio raises
        ``RuntimeError("Attempted to exit cancel scope in a different task
        …")``.  This is harmless — the subprocess is cleaned up when the
        event loop exits — so we suppress it silently.
        """
        if self._generator_started:
            await self._message_queue.put(None)  # stop the generator
            self._generator_started = False
        if self._client:
            try:
                await self._client.disconnect()
            except RuntimeError as exc:
                if "cancel scope" in str(exc):
                    # Expected during cross-task shutdown; nothing to do.
                    pass
                else:
                    logger.debug(
                        "Error disconnecting SDK client", exc_info=True
                    )
            except Exception:
                logger.debug("Error disconnecting SDK client", exc_info=True)
            self._client = None
            self._connected = False
            self._is_streaming = False

    async def kill(self) -> None:
        """Kill the active session and clean up.

        Interrupts first, then fully disconnects so a subsequent
        ``_ensure_connected()`` creates a fresh client.
        """
        if self._client:
            try:
                await self._client.interrupt()
            except Exception:
                pass
            await self.disconnect()
