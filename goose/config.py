"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from platformdirs import user_config_dir


def config_dir() -> Path:
    """Return the Goose configuration directory.

    Override with GOOSE_CONFIG_DIR env var. Platform defaults:
    - macOS: ~/Library/Application Support/goose
    - Linux: ~/.config/goose (or $XDG_CONFIG_HOME/goose)
    - Windows: %APPDATA%/goose
    """
    override = os.environ.get("GOOSE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(user_config_dir("goose", appauthor=False))


def env_file() -> Path:
    """Return the path to the .env configuration file."""
    return config_dir() / ".env"


load_dotenv(env_file())


VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _validate_log_level(value: str) -> str:
    level = value.upper()
    if level not in VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid LOG_LEVEL '{value}'. "
            f"Must be one of: {', '.join(sorted(VALID_LOG_LEVELS))}"
        )
    return level


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str

    # Optional
    base_directory: Path | None = None
    allowed_users: list[str] = field(default_factory=list)
    channel_dirs: dict[str, str] = field(default_factory=dict)
    log_level: str = "INFO"
    log_dir: Path | None = None
    claude_model: str | None = None
    claude_permission_mode: str = "acceptEdits"
    claude_allowed_tools: list[str] = field(default_factory=list)


    def resolve_dir_channel(self, cwd: Path) -> str | None:
        """Reverse lookup: given a directory path, find the Slack channel name.

        Iterates channel_dirs, resolves each mapped path (relative to
        base_directory if needed), and returns the channel name whose
        resolved path matches *cwd*.  Returns None if no match.
        """
        cwd = cwd.resolve()
        for channel_name, mapped in self.channel_dirs.items():
            path = Path(mapped)
            if not path.is_absolute() and self.base_directory:
                path = self.base_directory / mapped
            if path.resolve() == cwd:
                return channel_name
        return None

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
                "Run 'goose setup' to configure."
            )

        base_dir = os.environ.get("BASE_DIRECTORY")
        allowed = os.environ.get("ALLOWED_USERS", "")
        log_dir = os.environ.get("LOG_DIR")
        model = os.environ.get("CLAUDE_MODEL")
        valid_perm_modes = {"acceptEdits", "dontAsk", "bypassPermissions"}
        perm_mode = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
        if perm_mode not in valid_perm_modes:
            raise ValueError(
                f"Invalid CLAUDE_PERMISSION_MODE '{perm_mode}'. "
                f"Must be one of: {', '.join(sorted(valid_perm_modes))}"
            )
        raw_tools = os.environ.get("CLAUDE_ALLOWED_TOOLS", "")
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
            log_level=_validate_log_level(os.environ.get("LOG_LEVEL", "INFO")),
            log_dir=Path(log_dir) if log_dir else None,
            claude_model=model,
            claude_permission_mode=perm_mode,
            claude_allowed_tools=[t.strip() for t in raw_tools.split(",") if t.strip()],
        )
