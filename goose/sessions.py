"""Session management — maps Slack threads to Claude sessions."""

import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .claude import ClaudeSession
from .config import Config

logger = logging.getLogger(__name__)

SLACK_SYSTEM_PROMPT = """\
You are operating as a Slack bot. Your responses are sent directly to a Slack \
channel or thread. Keep these rules in mind:

- The user interacts with you ONLY through Slack messages. They cannot see your \
tool calls, file reads, or terminal output. If they ask to see file contents, \
you MUST include the content in your response text.
- Format responses for Slack: use *bold*, _italic_, `code`, and ```code blocks```. \
Slack does NOT support markdown headers (#), tables, or HTML.
- Keep responses concise. Slack messages have a ~4000 char limit per message. \
For very long content, summarize and offer to show specific sections.
- Never ask the user to "approve" or "confirm" something in a terminal — they \
have no terminal access. Just proceed with the task.
- When you create or modify files, briefly confirm what you did. Don't ask for \
permission first — the user already asked you to do it by sending the message.
"""


@dataclass
class SessionInfo:
    """Metadata about an active Claude session tied to a Slack thread."""

    session: ClaudeSession
    thread_ts: str
    cwd: Path
    created_at: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)

    def touch(self) -> None:
        self.last_used = datetime.now()


class SessionStore:
    """Thread-safe store mapping Slack thread_ts to Claude sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}

    def get_or_create(
        self,
        thread_ts: str,
        config: Config,
        cwd: Path | None = None,
    ) -> ClaudeSession:
        """Get existing session for a thread or create a new one."""
        if thread_ts in self._sessions:
            info = self._sessions[thread_ts]
            info.touch()
            logger.debug(f"Reusing session for thread {thread_ts}")
            return info.session

        if cwd:
            work_dir = cwd
        else:
            # Fallback: random temp directory so Claude doesn't run in the project dir
            work_dir = Path(tempfile.mkdtemp(prefix="goose-"))

        session = ClaudeSession(
            cwd=work_dir,
            model=config.claude_model,
            permission_mode=config.claude_permission_mode,
            system_prompt=SLACK_SYSTEM_PROMPT,
        )

        self._sessions[thread_ts] = SessionInfo(
            session=session,
            thread_ts=thread_ts,
            cwd=work_dir,
        )

        logger.info(f"New session for thread {thread_ts} (cwd={work_dir})")
        return session

    def has(self, thread_ts: str) -> bool:
        """Check if a session exists for this thread."""
        return thread_ts in self._sessions

    def set_cwd(self, thread_ts: str, cwd: Path) -> bool:
        """Update the working directory for a thread's session."""
        if thread_ts in self._sessions:
            info = self._sessions[thread_ts]
            info.cwd = cwd
            info.session.cwd = cwd
            return True
        return False

    def remove(self, thread_ts: str) -> None:
        """Remove a session."""
        self._sessions.pop(thread_ts, None)

    def cleanup(self, max_age_hours: int = 24) -> int:
        """Remove sessions older than max_age_hours. Returns count removed."""
        now = datetime.now()
        expired = [
            ts
            for ts, info in self._sessions.items()
            if (now - info.last_used).total_seconds() > max_age_hours * 3600
        ]
        for ts in expired:
            del self._sessions[ts]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired sessions")
        return len(expired)
