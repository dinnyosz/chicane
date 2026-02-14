"""Claude Agent SDK wrapper with streaming support.

Uses the ``claude-agent-sdk`` Python package (``ClaudeSDKClient``) to maintain
a persistent Claude Code session per Slack thread.  Each call to ``stream()``
sends a new user message into the *same* running session, enabling inter-turn
messaging and interruption.
"""

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

logger = logging.getLogger(__name__)

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
    def tool_errors(self) -> list[str]:
        """Extract error messages from tool_result blocks in user events."""
        if self.type != "user":
            return []
        message = self.raw.get("message", {})
        content = message.get("content", [])
        errors = []
        for block in content:
            if block.get("type") == "tool_result" and block.get("is_error"):
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
                        errors.append(text)
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
                text = block.get("content", "")
                if isinstance(text, list):
                    text = "".join(
                        p.get("text", "") for p in text if isinstance(p, dict)
                    )
                if text:
                    results.append((tool_use_id, text))
        return results


class ClaudeSession:
    """Manages a persistent Claude Agent SDK session for a single conversation.

    Unlike the old subprocess-per-message approach, this keeps the SDK client
    alive across multiple ``stream()`` calls.  Each call sends a new user
    message into the running session, enabling inter-turn messaging.
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
        self._client: ClaudeSDKClient | None = None
        self._connected = False
        self._is_streaming = False
        self._interrupted = False
        self._interrupt_source: str | None = None  # "reaction" or "new_message"

    def _build_options(self) -> ClaudeAgentOptions:
        """Build SDK options from session config."""
        opts = ClaudeAgentOptions(
            cwd=self.cwd,
            allowed_tools=list(self.allowed_tools),
            disallowed_tools=list(self.disallowed_tools),
            setting_sources=list(self.setting_sources),
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

        return opts

    async def _ensure_connected(self) -> ClaudeSDKClient:
        """Connect the SDK client if not already connected."""
        if self._client is not None and self._connected:
            return self._client

        opts = self._build_options()
        self._client = ClaudeSDKClient(options=opts)
        await self._client.connect()
        self._connected = True
        logger.info(f"SDK client connected (session_id={self.session_id}, cwd={self.cwd})")
        return self._client

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

        *source* identifies why: ``"reaction"`` (user clicked stop emoji)
        or ``"new_message"`` (new thread reply arrived).
        """
        if self._client and self._is_streaming:
            self._interrupted = True
            self._interrupt_source = source
            await self._client.interrupt()
            logger.info("Interrupted active stream (source=%s)", source)

    async def stream(self, prompt: str) -> AsyncIterator[ClaudeEvent]:
        """Send a message and yield streaming events.

        On the first call this connects the SDK client. On subsequent calls
        it reuses the same client, sending the new prompt into the existing
        conversation.
        """
        client = await self._ensure_connected()
        self._is_streaming = True
        self._interrupted = False
        self._interrupt_source = None

        event_count = 0
        try:
            await client.query(prompt)

            async for msg in client.receive_response():
                event_type = _MSG_TYPE_MAP.get(type(msg), "unknown")
                raw = _sdk_message_to_raw(msg)
                event = ClaudeEvent(type=event_type, raw=raw)
                event_count += 1

                # Capture session_id from init event
                if event.type == "system" and event.subtype == "init":
                    self.session_id = event.session_id
                    logger.info(f"Session started: {self.session_id}")

                yield event
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
        """Disconnect the SDK client."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                logger.debug("Error disconnecting SDK client", exc_info=True)
            self._client = None
            self._connected = False

    async def kill(self) -> None:
        """Kill the active session. For backward compatibility."""
        if self._client:
            try:
                await self._client.interrupt()
            except Exception:
                pass
