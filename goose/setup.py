"""Guided setup wizard for Goose (goose setup)."""

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


def _load_existing_env(path: Path) -> dict[str, str]:
    """Parse an existing .env file into a dict. Returns empty dict if missing."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip()
    return values


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort copy text to system clipboard. Returns True on success."""
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True, capture_output=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def _prompt_with_default(label: str, default: str = "") -> str:
    """Prompt for input, showing and accepting a default value."""
    if default:
        val = input(f"    {label} [{default}]: ").strip()
        return val if val else default
    else:
        return input(f"    {label} []: ").strip()


def _prompt_token(label: str, prefix: str, default: str = "") -> str:
    """Prompt for a token, validating the prefix. Re-prompts on bad input."""
    while True:
        if default:
            masked = default[:8] + "..." + default[-4:]
            value = input(f"  Paste your {label} [{masked}]: ").strip()
            if not value:
                return default
        else:
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
    input("  Press Enter when your app is created (or to skip if already done)...")


def _step_bot_token(default: str = "") -> str:
    """Step 2: Get Bot Token."""
    print("""
  Step 2 of 5: Get Bot Token

    1. In the sidebar, go to "OAuth & Permissions"
    2. Click "Install to Workspace" and approve
    3. Copy the "Bot User OAuth Token" (starts with xoxb-)
""")
    token = _prompt_token("Bot Token", "xoxb-", default)
    print("  \u2713 Saved")
    return token


def _step_app_token(default: str = "") -> str:
    """Step 3: Get App Token."""
    print("""
  Step 3 of 5: Get App Token

    1. In the sidebar, go to "Basic Information"
    2. Scroll to "App-Level Tokens" -> "Generate Token and Scopes"
    3. Name it anything (e.g. "goose-socket")
    4. Add the "connections:write" scope
    5. Click "Generate" and copy the token (starts with xapp-)
""")
    token = _prompt_token("App Token", "xapp-", default)
    print("  \u2713 Saved")
    return token


def _step_optional_settings(defaults: dict[str, str]) -> dict[str, str]:
    """Step 4: Prompt for optional settings. Returns non-empty values only."""
    print("""
  Step 4 of 5: Optional Settings (press Enter to skip or keep current value)
""")
    values: dict[str, str] = {}

    # BASE_DIRECTORY
    val = _prompt_with_default(
        "Base directory for Claude sessions",
        defaults.get("BASE_DIRECTORY", ""),
    )
    if val:
        values["BASE_DIRECTORY"] = val

    # ALLOWED_USERS
    print("    Restrict who can use the bot by Slack member ID.")
    print("    (Find yours: Slack profile -> \u22ee menu -> Copy member ID)")
    val = _prompt_with_default(
        "Allowed user IDs, comma-separated (e.g. U01AB2CDE)",
        defaults.get("ALLOWED_USERS", ""),
    )
    if val:
        values["ALLOWED_USERS"] = val

    # CHANNEL_DIRS
    print("    Map Slack channels to working directories.")
    print("    Simple: channel name = directory name under base directory.")
    print("    Custom: channel=path (e.g. web=frontend, infra=/opt/infra)")
    val = _prompt_with_default(
        "Channel mappings, comma-separated",
        defaults.get("CHANNEL_DIRS", ""),
    )
    if val:
        values["CHANNEL_DIRS"] = val

    # CLAUDE_MODEL
    val = _prompt_with_default(
        "Claude model override (e.g. sonnet, opus)",
        defaults.get("CLAUDE_MODEL", ""),
    )
    if val:
        values["CLAUDE_MODEL"] = val

    # CLAUDE_PERMISSION_MODE
    val = _prompt_with_default(
        "Claude permission mode",
        defaults.get("CLAUDE_PERMISSION_MODE", "default"),
    )
    if val and val != "default":
        values["CLAUDE_PERMISSION_MODE"] = val

    # DEBUG
    current_debug = defaults.get("DEBUG", "").lower() in ("1", "true", "yes")
    debug_default = "Y/n" if current_debug else "y/N"
    debug = input(f"    Enable debug logging? ({debug_default}): ").strip().lower()
    if debug:
        if debug in ("y", "yes"):
            values["DEBUG"] = "true"
    elif current_debug:
        values["DEBUG"] = "true"

    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Write the .env file with the given values."""
    lines: list[str] = []
    for key, val in values.items():
        lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n")


def setup_command(args) -> None:
    """Main setup wizard entrypoint."""
    try:
        _run_wizard(args)
    except KeyboardInterrupt:
        print("\n  Aborted.")
        sys.exit(130)


def _run_wizard(args) -> None:
    """Run the interactive wizard steps."""
    env_path = Path(".env")
    existing = _load_existing_env(env_path)

    print()
    print("  Goose \u2014 Setup Wizard")
    print("  =====================")

    if existing:
        print(f"\n  Found existing .env \u2014 current values shown as defaults.")

    # Step 1: Create Slack App
    _step_create_app()

    # Step 2: Bot Token
    bot_token = _step_bot_token(existing.get("SLACK_BOT_TOKEN", ""))

    # Step 3: App Token
    app_token = _step_app_token(existing.get("SLACK_APP_TOKEN", ""))

    # Step 4: Optional Settings
    optional = _step_optional_settings(existing)

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
