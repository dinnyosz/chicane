"""Claude Code CLI subprocess wrapper with streaming JSON support."""

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class ClaudeEvent:
    """A parsed event from Claude's stream-json output."""

    type: str  # "system", "assistant", "user", "result", "stream_event"
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
    """Manages a Claude Code CLI subprocess for a single conversation."""

    def __init__(
        self,
        cwd: Path | None = None,
        session_id: str | None = None,
        model: str | None = None,
        permission_mode: str = "default",
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ):
        self.cwd = cwd or Path.cwd()
        self.session_id = session_id
        self.model = model
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools or []
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self._process: asyncio.subprocess.Process | None = None

    def kill(self) -> None:
        """Kill the active subprocess if any."""
        if self._process and self._process.returncode is None:
            self._process.kill()

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [
            "claude",
            "--print",
            "--output-format", "stream-json",
            "--verbose",
        ]

        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        if self.model:
            cmd.extend(["--model", self.model])

        if self.permission_mode != "default":
            cmd.extend(["--permission-mode", self.permission_mode])

        if self.allowed_tools:
            cmd.append("--allowedTools")
            cmd.extend(self.allowed_tools)

        if self.max_turns is not None:
            cmd.extend(["--max-turns", str(self.max_turns)])

        if self.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(self.max_budget_usd)])

        # Only send system prompt on the first invocation — resumed sessions
        # already have it, so resending wastes tokens.
        if self.system_prompt and not self.session_id:
            cmd.extend(["--append-system-prompt", self.system_prompt])

        cmd.append(prompt)
        return cmd

    async def stream(self, prompt: str) -> AsyncIterator[ClaudeEvent]:
        """Run a prompt through Claude and yield streaming events."""
        cmd = self._build_command(prompt)

        logger.debug(f"Running: {' '.join(cmd[:6])}...")
        logger.debug(f"Full command: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            limit=1024 * 1024,  # 1 MiB – Claude stream-json lines can exceed the 64 KiB default
        )
        self._process = process

        event_count = 0
        try:
            async for line in process.stdout:
                line = line.decode().strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Skipping non-JSON line: {line[:100]}")
                    continue

                event = ClaudeEvent(type=data.get("type", "unknown"), raw=data)
                event_count += 1

                # Capture session_id from init event
                if event.type == "system" and event.subtype == "init":
                    self.session_id = event.session_id
                    logger.info(f"Session started: {self.session_id}")

                yield event

        finally:
            if process.returncode is None:
                process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Claude subprocess did not exit in time.")
            self._process = None

            stderr = ""
            if process.stderr:
                stderr = (await process.stderr.read()).decode()

            if process.returncode and process.returncode != 0:
                logger.error(
                    f"Claude exited with code {process.returncode}: {stderr}"
                )
            elif event_count == 0:
                logger.warning(
                    f"Claude produced no events. exit={process.returncode} "
                    f"stderr={stderr[:500] if stderr else '(empty)'}"
                )

    async def run(self, prompt: str) -> str:
        """Run a prompt and return the final result text."""
        result_text = ""
        async for event in self.stream(prompt):
            if event.type == "result":
                result_text = event.text
        return result_text
