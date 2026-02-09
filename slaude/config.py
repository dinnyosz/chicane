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
    debug: bool = False
    claude_model: str | None = None
    claude_permission_mode: str = "default"

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

        return cls(
            slack_bot_token=bot_token,
            slack_app_token=app_token,
            base_directory=Path(base_dir) if base_dir else None,
            allowed_users=[u.strip() for u in allowed.split(",") if u.strip()],
            debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"),
            claude_model=model,
            claude_permission_mode=perm_mode,
        )
