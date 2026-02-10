"""Guided setup wizard for Goose (goose init)."""

import json
import subprocess
import sys
from pathlib import Path


# The Slack app manifest — kept in sync with slack-app-manifest.json
SLACK_MANIFEST = {
    "display_information": {
        "name": "Goose",
        "description": "Claude Code assistant in Slack",
        "background_color": "#D97706",
    },
    "features": {
        "app_home": {
            "home_tab_enabled": False,
            "messages_tab_enabled": True,
            "messages_tab_read_only_enabled": False,
        },
        "bot_user": {"display_name": "Goose", "always_online": True},
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "app_mentions:read",
                "channels:history",
                "channels:read",
                "chat:write",
                "chat:write.public",
                "im:history",
                "im:read",
                "im:write",
                "reactions:read",
                "reactions:write",
                "users:read",
            ]
        }
    },
    "settings": {
        "event_subscriptions": {"bot_events": ["app_mention", "message.im"]},
        "interactivity": {"is_enabled": False},
        "org_deploy_enabled": False,
        "socket_mode_enabled": True,
        "token_rotation_enabled": False,
    },
}


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort copy text to system clipboard. Returns True on success."""
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True, capture_output=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def _prompt_token(label: str, prefix: str) -> str:
    """Prompt for a token, validating the prefix. Re-prompts on bad input."""
    while True:
        value = input(f"  Paste your {label}: ").strip()
        if value.startswith(prefix):
            return value
        print(f"  Token must start with '{prefix}'. Please try again.\n")


def _step_create_app() -> None:
    """Step 1: Print manifest and wait for user to create the app."""
    manifest_json = json.dumps(SLACK_MANIFEST, indent=2)
    copied = _copy_to_clipboard(manifest_json)

    print("""
  Step 1 of 5: Create Slack App

    1. Open https://api.slack.com/apps
    2. Click "Create New App" -> "From a manifest"
    3. Select your workspace
    4. Switch to the JSON tab and paste this manifest:

    ──────────────────────────────────""")
    print(manifest_json)
    print("    ──────────────────────────────────")
    if copied:
        print("\n    (The manifest has been copied to your clipboard.)")
    print()
    input("  Press Enter when your app is created...")


def _step_bot_token() -> str:
    """Step 2: Get Bot Token."""
    print("""
  Step 2 of 5: Get Bot Token

    1. In the sidebar, go to "OAuth & Permissions"
    2. Click "Install to Workspace" and approve
    3. Copy the "Bot User OAuth Token" (starts with xoxb-)
""")
    token = _prompt_token("Bot Token", "xoxb-")
    print("  \u2713 Saved")
    return token


def _step_app_token() -> str:
    """Step 3: Get App Token."""
    print("""
  Step 3 of 5: Get App Token

    1. In the sidebar, go to "Basic Information"
    2. Scroll to "App-Level Tokens" -> "Generate Token and Scopes"
    3. Name it anything (e.g. "goose-socket")
    4. Add the "connections:write" scope
    5. Click "Generate" and copy the token (starts with xapp-)
""")
    token = _prompt_token("App Token", "xapp-")
    print("  \u2713 Saved")
    return token


def _step_optional_settings() -> dict[str, str]:
    """Step 4: Prompt for optional settings. Returns non-empty values only."""
    print("""
  Step 4 of 5: Optional Settings (press Enter to skip)
""")
    values: dict[str, str] = {}

    prompts = [
        ("BASE_DIRECTORY", "Base directory for Claude sessions"),
        ("ALLOWED_USERS", "Allowed Slack user IDs (comma-separated)"),
        ("CHANNEL_DIRS", "Channel->directory mapping (e.g. myproject,web=frontend)"),
        ("CLAUDE_MODEL", "Claude model override (e.g. sonnet, opus)"),
        ("CLAUDE_PERMISSION_MODE", "Claude permission mode"),
    ]

    for key, label in prompts:
        val = input(f"    {label} []: ").strip()
        if val:
            values[key] = val

    debug = input("    Enable debug logging? (y/N): ").strip().lower()
    if debug in ("y", "yes"):
        values["DEBUG"] = "true"

    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Write the .env file with the given values."""
    lines: list[str] = []
    for key, val in values.items():
        lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n")


def init_command(args) -> None:
    """Main init wizard entrypoint."""
    try:
        _run_wizard(args)
    except KeyboardInterrupt:
        print("\n  Aborted.")
        sys.exit(130)


def _run_wizard(args) -> None:
    """Run the interactive wizard steps."""
    env_path = Path(".env")

    if env_path.exists() and not getattr(args, "force", False):
        print(f"  .env already exists at {env_path.resolve()}")
        answer = input("  Overwrite? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print("  Aborted.")
            return

    print()
    print("  Goose — Setup Wizard")
    print("  =====================")

    # Step 1: Create Slack App
    _step_create_app()

    # Step 2: Bot Token
    bot_token = _step_bot_token()

    # Step 3: App Token
    app_token = _step_app_token()

    # Step 4: Optional Settings
    optional = _step_optional_settings()

    # Step 5: Write config
    print("""
  Step 5 of 5: Writing config
""")

    env_values: dict[str, str] = {
        "SLACK_BOT_TOKEN": bot_token,
        "SLACK_APP_TOKEN": app_token,
    }
    env_values.update(optional)

    _write_env(env_path, env_values)
    print(f"    \u2713 Wrote .env")
    print(f"    \u2713 Next: run 'goose run' to start the bot")
    print(f"    \u2713 Tip: invite @Goose to channels with /invite @Goose")
    print()
