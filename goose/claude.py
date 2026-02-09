"""Claude Code CLI subprocess wrapper with streaming JSON support."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class ClaudeEvent:
    """A parsed event from Claude's stream-json output."""

    type: str  # "system", "assistant", "result"
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


class ClaudeSession:
    """Manages a Claude Code CLI subprocess for a single conversation."""

    def __init__(
        self,
        cwd: Path | None = None,
        session_id: str | None = None,
        model: str | None = None,
        permission_mode: str = "default",
        system_prompt: str | None = None,
    ):
        self.cwd = cwd or Path.cwd()
        self.session_id = session_id
        self.model = model
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt

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

        if self.system_prompt:
            cmd.extend(["--append-system-prompt", self.system_prompt])

        cmd.append(prompt)
        return cmd

    async def stream(self, prompt: str) -> AsyncIterator[ClaudeEvent]:
        """Run a prompt through Claude and yield streaming events."""
        cmd = self._build_command(prompt)

        logger.info(f"Running: {' '.join(cmd[:6])}...")
        logger.debug(f"Full command: {cmd}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
        )

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

                # Capture session_id from init event
                if event.type == "system" and event.subtype == "init":
                    self.session_id = event.session_id
                    logger.info(f"Session started: {self.session_id}")

                yield event

        except Exception:
            process.kill()
            raise
        finally:
            await process.wait()

            if process.returncode and process.returncode != 0:
                stderr = ""
                if process.stderr:
                    stderr = (await process.stderr.read()).decode()
                logger.error(
                    f"Claude exited with code {process.returncode}: {stderr}"
                )

    async def run(self, prompt: str) -> str:
        """Run a prompt and return the final result text."""
        result_text = ""
        async for event in self.stream(prompt):
            if event.type == "result":
                result_text = event.text
        return result_text
