"""Configuration loaded from environment variables."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from platformdirs import user_config_dir

logger = logging.getLogger(__name__)


def config_dir() -> Path:
    """Return the Chicane configuration directory.

    Override with CHICANE_CONFIG_DIR env var. Platform defaults:
    - macOS: ~/Library/Application Support/chicane
    - Linux: ~/.config/chicane (or $XDG_CONFIG_HOME/chicane)
    - Windows: %APPDATA%/chicane
    """
    override = os.environ.get("CHICANE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(user_config_dir("chicane", appauthor=False))


def env_file() -> Path:
    """Return the path to the .env configuration file."""
    return config_dir() / ".env"


load_dotenv(env_file())


VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
VALID_VERBOSITY_LEVELS = {"minimal", "normal", "verbose"}
VALID_SETTING_SOURCES = {"user", "project", "local"}


def _validate_log_level(value: str) -> str:
    level = value.upper()
    if level not in VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid LOG_LEVEL '{value}'. "
            f"Must be one of: {', '.join(sorted(VALID_LOG_LEVELS))}"
        )
    return level


def _validate_verbosity(value: str) -> str:
    level = value.lower()
    if level not in VALID_VERBOSITY_LEVELS:
        raise ValueError(
            f"Invalid VERBOSITY '{value}'. "
            f"Must be one of: {', '.join(sorted(VALID_VERBOSITY_LEVELS))}"
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
    claude_disallowed_tools: list[str] = field(default_factory=list)
    claude_setting_sources: list[str] = field(default_factory=lambda: ["user", "project", "local"])
    claude_max_turns: int | None = None
    claude_max_budget_usd: float | None = None
    rate_limit: int = 10
    verbosity: str = "verbose"

    def __repr__(self) -> str:
        """Mask sensitive tokens in repr to prevent accidental leakage."""
        def _mask(val: str) -> str:
            if len(val) <= 8:
                return "***"
            return val[:4] + "..." + val[-4:]

        fields = []
        for f in self.__dataclass_fields__:
            val = getattr(self, f)
            if f in ("slack_bot_token", "slack_app_token"):
                val = _mask(val)
            fields.append(f"{f}={val!r}")
        return f"Config({', '.join(fields)})"

    def resolve_dir_channel(self, cwd: Path) -> str | None:
        """Reverse lookup: given a directory path, find the Slack channel name.

        Iterates channel_dirs, resolves each mapped path (relative to
        base_directory if needed), and returns the channel name whose
        resolved path matches *cwd*.  Returns None if no match.

        When multiple channels map to the same directory, the first match
        (by insertion order) is returned and a warning is logged.
        """
        cwd = cwd.resolve()
        matches: list[str] = []
        for channel_name, mapped in self.channel_dirs.items():
            path = Path(mapped)
            if not path.is_absolute() and self.base_directory:
                path = self.base_directory / mapped
            if path.resolve() == cwd:
                matches.append(channel_name)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "Directory %s maps to multiple channels: %s -- using #%s",
                cwd,
                ", ".join(f"#{c}" for c in matches),
                matches[0],
            )
        return matches[0]

    def resolve_channel_dir(self, channel_name: str) -> Path | None:
        """Resolve working directory for a channel.

        If the channel is in channel_dirs, use the mapped path (relative to
        base_directory if not absolute). Returns None if the channel isn't
        whitelisted or if the resolved path escapes base_directory via
        traversal (e.g. ``../../../etc``).
        """
        if channel_name not in self.channel_dirs:
            return None

        mapped = self.channel_dirs[channel_name]
        path = Path(mapped)

        if not path.is_absolute() and self.base_directory:
            path = self.base_directory / mapped

        # Guard against directory traversal: relative paths that resolve
        # outside base_directory via ../ components are blocked.
        # Absolute paths are intentionally configured and always allowed.
        if self.base_directory and not Path(mapped).is_absolute():
            resolved = path.resolve()
            base_resolved = self.base_directory.resolve()
            if not resolved.is_relative_to(base_resolved):
                return None

        return path

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        app_token = os.environ.get("SLACK_APP_TOKEN", "")

        if not bot_token or not app_token:
            raise ValueError(
                "SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set. "
                "Run 'chicane setup' to configure."
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
        allowed_user_list = [u.strip() for u in allowed.split(",") if u.strip()]
        if perm_mode == "bypassPermissions" and len(allowed_user_list) > 1:
            raise ValueError(
                "CLAUDE_PERMISSION_MODE=bypassPermissions cannot be used with "
                "multiple ALLOWED_USERS. bypassPermissions grants unrestricted "
                "shell access -- this is only safe for single-user, isolated "
                "environments. Remove extra users or choose a safer permission mode."
            )
        raw_tools = os.environ.get("CLAUDE_ALLOWED_TOOLS", "")
        raw_disallowed = os.environ.get("CLAUDE_DISALLOWED_TOOLS", "")

        # Parse setting sources
        raw_sources = os.environ.get("CLAUDE_SETTING_SOURCES", "")
        if raw_sources:
            setting_sources = [s.strip() for s in raw_sources.split(",") if s.strip()]
            invalid = set(setting_sources) - VALID_SETTING_SOURCES
            if invalid:
                raise ValueError(
                    f"Invalid CLAUDE_SETTING_SOURCES: {', '.join(sorted(invalid))}. "
                    f"Must be from: {', '.join(sorted(VALID_SETTING_SOURCES))}"
                )
        else:
            setting_sources = ["user", "project", "local"]

        raw_max_turns = os.environ.get("CLAUDE_MAX_TURNS")
        raw_max_budget = os.environ.get("CLAUDE_MAX_BUDGET_USD")
        max_turns: int | None = None
        max_budget: float | None = None
        if raw_max_turns:
            max_turns = int(raw_max_turns)
            if max_turns < 1:
                raise ValueError("CLAUDE_MAX_TURNS must be a positive integer")
        if raw_max_budget:
            max_budget = float(raw_max_budget)
            if max_budget <= 0:
                raise ValueError("CLAUDE_MAX_BUDGET_USD must be a positive number")
        raw_rate_limit = os.environ.get("RATE_LIMIT")
        rate_limit = 10
        if raw_rate_limit:
            rate_limit = int(raw_rate_limit)
            if rate_limit < 1:
                raise ValueError("RATE_LIMIT must be a positive integer")
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

        verbosity = _validate_verbosity(os.environ.get("VERBOSITY", "verbose"))

        return cls(
            slack_bot_token=bot_token,
            slack_app_token=app_token,
            base_directory=Path(base_dir) if base_dir else None,
            allowed_users=allowed_user_list,
            channel_dirs=channel_dirs,
            log_level=_validate_log_level(os.environ.get("LOG_LEVEL", "INFO")),
            log_dir=Path(log_dir) if log_dir else None,
            claude_model=model,
            claude_permission_mode=perm_mode,
            claude_allowed_tools=[t.strip() for t in raw_tools.split(",") if t.strip()],
            claude_disallowed_tools=[t.strip() for t in raw_disallowed.split(",") if t.strip()],
            claude_setting_sources=setting_sources,
            claude_max_turns=max_turns,
            claude_max_budget_usd=max_budget,
            rate_limit=rate_limit,
            verbosity=verbosity,
        )


# ---------------------------------------------------------------------------
# Handoff session aliases — funky names that map to real session IDs
# ---------------------------------------------------------------------------


def generate_session_alias() -> str:
    """Generate a memorable alias like ``dancing-cosmic-falcon``.

    Uses a custom word list (~2M combinations) where every word maps
    1:1 to a standard Slack emoji.  Retries until the alias doesn't
    collide with an existing handoff mapping.
    """
    from .emoji_map import generate_alias

    existing = _load_handoff_map()
    for _ in range(50):
        alias = generate_alias()
        if alias not in existing:
            return alias
    # Extremely unlikely fallback — just return whatever we got
    return alias


# ---------------------------------------------------------------------------
# Handoff session map — persists alias → session_id across restarts
# ---------------------------------------------------------------------------

_HANDOFF_MAP_FILE = config_dir() / "handoff_sessions.json"
_HANDOFF_MAP_MAX = 200


def _load_handoff_map() -> dict[str, str]:
    """Load the handoff alias → session_id map from disk."""
    if not _HANDOFF_MAP_FILE.exists():
        return {}
    try:
        return json.loads(_HANDOFF_MAP_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_handoff_session(alias: str, session_id: str) -> None:
    """Persist a handoff alias → session_id mapping."""
    data = _load_handoff_map()
    data[alias] = session_id
    # Trim oldest entries to keep the file bounded
    if len(data) > _HANDOFF_MAP_MAX:
        keys = list(data.keys())
        for k in keys[: len(data) - _HANDOFF_MAP_MAX]:
            del data[k]
    _HANDOFF_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _HANDOFF_MAP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass  # Windows or restricted filesystem
    tmp.replace(_HANDOFF_MAP_FILE)


def load_handoff_session(alias: str) -> str | None:
    """Look up a handoff session_id by its alias."""
    return _load_handoff_map().get(alias)
