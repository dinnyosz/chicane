"""Guided setup wizard for Goose (goose setup)."""

import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

console = Console()

_MANIFEST_PATH = Path(__file__).resolve().parent / "slack-app-manifest.json"


def _load_manifest() -> dict:
    """Load the Slack app manifest from the bundled JSON file."""
    return json.loads(_MANIFEST_PATH.read_text())


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
    """Prompt for input, showing and accepting a default value.

    Enter keeps the default. Type '-' to clear it.
    """
    if default:
        val = Prompt.ask(f"  {label}", default=default, console=console)
        if val == "-":
            return ""
        return val
    else:
        val = Prompt.ask(f"  {label}", default="", console=console)
        return val


def _prompt_token(label: str, prefix: str, default: str = "") -> str:
    """Prompt for a token, validating the prefix. Re-prompts on bad input."""
    while True:
        if default:
            if len(default) > 12:
                masked = default[:8] + "..." + default[-4:]
            else:
                masked = default[:5] + "..."
            value = console.input(f"  Paste your {label} \\[{masked}] (Enter to keep): ").strip()
            if not value:
                return default
        else:
            value = console.input(f"  Paste your {label}: ").strip()
        if value.startswith(prefix):
            return value
        console.print(f"  [red]Token must start with '{prefix}'. Please try again.[/red]\n")


def _step_create_app(has_tokens: bool) -> None:
    """Step 1: Print manifest and wait for user to create the app."""
    console.rule("Step 1 of 5: Create Slack App")

    if has_tokens:
        console.print("\n  Slack app already configured.")
        skip = Confirm.ask("  Skip this step?", default=True, console=console)
        if skip:
            return

    manifest_json = json.dumps(_load_manifest(), indent=2)
    copied = _copy_to_clipboard(manifest_json)

    console.print("\n  1. Open https://api.slack.com/apps")
    console.print('  2. Click "Create New App" -> "From a manifest"')
    console.print("  3. Select your workspace")
    console.print("  4. Switch to the JSON tab and paste the manifest")

    if copied:
        console.print("\n  [green]✓[/green] Manifest copied to your clipboard.")
        show = Confirm.ask("\n  Show manifest in console?", default=False, console=console)
        if show:
            console.print()
            console.print_json(manifest_json)
    else:
        console.print()
        console.print_json(manifest_json)

    console.print()
    console.input("  Press Enter when your app is created...")


def _step_bot_token(default: str = "") -> str:
    """Step 2: Get Bot Token."""
    console.rule("Step 2 of 5: Get Bot Token")

    if default:
        console.print("\n  Bot token found in .env. Press Enter to keep it,")
        console.print("  or paste a new one. To generate a new token:")

    console.print("""
  1. In the sidebar, go to "OAuth & Permissions"
  2. Click "Install to Workspace" and approve
  3. Copy the "Bot User OAuth Token" (starts with xoxb-)
""")
    token = _prompt_token("Bot Token", "xoxb-", default)
    console.print("  [green]✓[/green] Saved")
    return token


def _step_app_token(default: str = "") -> str:
    """Step 3: Get App Token."""
    console.rule("Step 3 of 5: Get App Token")

    if default:
        console.print("\n  App token found in .env. Press Enter to keep it,")
        console.print("  or paste a new one. To generate a new token:")

    console.print("""
  1. In the sidebar, go to "Basic Information"
  2. Scroll to "App-Level Tokens" -> "Generate Token and Scopes"
  3. Name it anything (e.g. "goose-socket")
  4. Add the "connections:write" scope
  5. Click "Generate" and copy the token (starts with xapp-)
""")
    token = _prompt_token("App Token", "xapp-", default)
    console.print("  [green]✓[/green] Saved")
    return token


def _step_optional_settings(defaults: dict[str, str]) -> dict[str, str]:
    """Step 4: Prompt for optional settings. Returns non-empty values only."""
    console.rule("Step 4 of 5: Optional Settings")
    console.print("\n  Press Enter to skip (or keep current value). Type '-' to clear a value.\n")

    values: dict[str, str] = {}

    # BASE_DIRECTORY
    console.print("  Base path for channel->directory mappings below.")
    console.print("  e.g. base=/home/user/code + channel 'frontend' = /home/user/code/frontend")
    val = _prompt_with_default(
        "Base directory (e.g. /home/user/code)",
        defaults.get("BASE_DIRECTORY", ""),
    )
    if val:
        values["BASE_DIRECTORY"] = val

    # ALLOWED_USERS
    console.print("  Restrict who can use the bot by Slack member ID.")
    console.print("  (Find yours: Slack profile -> ⋮ menu -> Copy member ID)")
    val = _prompt_with_default(
        "Allowed user IDs, comma-separated (e.g. U01AB2CDE)",
        defaults.get("ALLOWED_USERS", ""),
    )
    if val:
        values["ALLOWED_USERS"] = val

    # CHANNEL_DIRS
    console.print("  Map Slack channels to working directories.")
    console.print("  Simple: channel name = directory name under base directory.")
    console.print("  Custom: channel=path (e.g. web=frontend, infra=/opt/infra)")
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
    debug = Confirm.ask("  Enable debug logging", default=current_debug, console=console)
    if debug:
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
        console.print("\n  [yellow]Aborted.[/yellow]")
        sys.exit(130)


def _run_wizard(args) -> None:
    """Run the interactive wizard steps."""
    env_path = Path(".env")
    existing = _load_existing_env(env_path)

    console.print()
    console.print(Panel("[bold]Goose — Setup Wizard[/bold]"))

    has_tokens = bool(existing.get("SLACK_BOT_TOKEN") and existing.get("SLACK_APP_TOKEN"))

    # Step 1: Create Slack App
    _step_create_app(has_tokens)

    # Step 2: Bot Token
    bot_token = _step_bot_token(existing.get("SLACK_BOT_TOKEN", ""))

    # Step 3: App Token
    app_token = _step_app_token(existing.get("SLACK_APP_TOKEN", ""))

    # Step 4: Optional Settings
    optional = _step_optional_settings(existing)

    # Step 5: Write config
    console.rule("Step 5 of 5: Writing config")
    console.print()

    env_values: dict[str, str] = {
        "SLACK_BOT_TOKEN": bot_token,
        "SLACK_APP_TOKEN": app_token,
    }
    env_values.update(optional)

    _write_env(env_path, env_values)
    console.print(Panel(
        "[green]✓[/green] Wrote .env\n"
        "[green]✓[/green] Next: run [bold]goose run[/bold]\n"
        "[green]✓[/green] Tip: invite @Goose with /invite @Goose"
    ))
