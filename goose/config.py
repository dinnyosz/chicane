"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str

    # Optional
    base_directory: Path | None = None
    allowed_users: list[str] = field(default_factory=list)
    channel_dirs: dict[str, str] = field(default_factory=dict)
    debug: bool = False
    claude_model: str | None = None
    claude_permission_mode: str = "default"
    claude_allowed_tools: list[str] = field(default_factory=list)

    def resolve_channel_dir(self, channel_name: str) -> Path | None:
        """Resolve working directory for a channel.

        If the channel is in channel_dirs, use the mapped path (relative to
        base_directory if not absolute). Returns None if the channel isn't
        whitelisted.
        """
        if channel_name not in self.channel_dirs:
            return None

        mapped = self.channel_dirs[channel_name]
        path = Path(mapped)

        if not path.is_absolute() and self.base_directory:
            path = self.base_directory / mapped

        return path

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        app_token = os.environ.get("SLACK_APP_TOKEN", "")

        if not bot_token or not app_token:
            raise ValueError(
                "SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set. "
                "See .env.example for details."
            )

        base_dir = os.environ.get("BASE_DIRECTORY")
        allowed = os.environ.get("ALLOWED_USERS", "")
        model = os.environ.get("CLAUDE_MODEL")
        perm_mode = os.environ.get("CLAUDE_PERMISSION_MODE", "default")
        allowed_tools_raw = os.environ.get("CLAUDE_ALLOWED_TOOLS", "")
        allowed_tools = [t.strip() for t in allowed_tools_raw.split(",") if t.strip()]

        # Parse CHANNEL_DIRS: "magaldi,slack-bot,frontend" or "magaldi=magaldi,web=frontend"
        channel_dirs: dict[str, str] = {}
        raw_dirs = os.environ.get("CHANNEL_DIRS", "")
        for entry in raw_dirs.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if "=" in entry:
                name, path = entry.split("=", 1)
                channel_dirs[name.strip()] = path.strip()
            else:
                # channel name = directory name (relative to BASE_DIRECTORY)
                channel_dirs[entry] = entry

        return cls(
            slack_bot_token=bot_token,
            slack_app_token=app_token,
            base_directory=Path(base_dir) if base_dir else None,
            allowed_users=[u.strip() for u in allowed.split(",") if u.strip()],
            channel_dirs=channel_dirs,
            debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"),
            claude_model=model,
            claude_permission_mode=perm_mode,
            claude_allowed_tools=allowed_tools,
        )
