"""Guided setup wizard for Goose (goose setup)."""

import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

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


def _parse_channel_dirs(raw: str) -> dict[str, str]:
    """Parse CHANNEL_DIRS string into {channel: path} dict."""
    mappings: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            name, path = entry.split("=", 1)
            mappings[name.strip()] = path.strip()
        else:
            mappings[entry] = entry
    return mappings


def _serialize_channel_dirs(mappings: dict[str, str]) -> str:
    """Serialize {channel: path} dict back to CHANNEL_DIRS string."""
    parts: list[str] = []
    for channel, path in mappings.items():
        if channel == path:
            parts.append(channel)
        else:
            parts.append(f"{channel}={path}")
    return ",".join(parts)


def _parse_allowed_users(raw: str) -> list[str]:
    """Parse ALLOWED_USERS string into a list of user IDs."""
    return [u.strip() for u in raw.split(",") if u.strip()]


def _show_allowed_users(users: list[str]) -> None:
    """Display current allowed users."""
    if not users:
        console.print("  [dim]No user restrictions — all workspace users can use Goose.[/dim]\n")
        return
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Member ID", style="bold")
    for user_id in users:
        table.add_row(user_id)
    console.print(table)
    console.print()


def _show_channel_table(mappings: dict[str, str]) -> None:
    """Display current channel mappings as a Rich table."""
    if not mappings:
        console.print("  [dim]No channel mappings configured.[/dim]\n")
        return
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Channel", style="bold")
    table.add_column("Path")
    for channel, path in mappings.items():
        table.add_row(f"#{channel}", path)
    console.print(table)
    console.print()


def _step_create_app(has_tokens: bool) -> None:
    """Step 1: Print manifest and wait for user to create the app."""
    console.rule("Step 1 of 7: Create Slack App")

    if has_tokens:
        console.print("\n  Tokens found in config — Slack app likely already configured.")
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
    console.rule("Step 2 of 7: Get Bot Token")

    if default:
        console.print("\n  Bot token found in config. Press Enter to keep it,")
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
    console.rule("Step 3 of 7: Get App Token")

    if default:
        console.print("\n  App token found in config. Press Enter to keep it,")
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


def _step_channel_dirs(defaults: dict[str, str]) -> tuple[str, str]:
    """Step 4: Configure base directory and channel mappings interactively.

    Returns (base_directory, channel_dirs_string).
    """
    console.rule("Step 4 of 7: Directory Settings")
    console.print("\n  [yellow]Note:[/yellow] Goose will run Claude Code in these directories remotely.")
    console.print("  Only add directories you trust and are okay to tinker with.\n")

    # Base directory
    console.rule("Base Directory", style="dim")
    console.print("  Root path for relative channel mappings.")
    console.print("  e.g. base=/home/user/code + channel 'frontend' = /home/user/code/frontend")
    base_dir = _prompt_with_default(
        "Base directory",
        defaults.get("BASE_DIRECTORY", ""),
    )

    # Channel mappings
    console.print()
    console.rule("Channel Mappings", style="dim")
    console.print("  Map Slack channels to working directories.")
    console.print("  Each mapping allows Goose to run Claude Code in that directory.\n")

    mappings = _parse_channel_dirs(defaults.get("CHANNEL_DIRS", ""))
    _show_channel_table(mappings)

    while True:
        action = Prompt.ask(
            "  \\[a]dd / \\[r]emove / \\[d]one",
            choices=["a", "r", "d"],
            default="d",
            console=console,
        )
        if action == "d":
            break
        elif action == "a":
            name = Prompt.ask("  Channel name", console=console).strip()
            if not name:
                continue
            name = name.lstrip("#")
            path = Prompt.ask("  Path", default=name, console=console).strip()
            mappings[name] = path
            console.print(f"  [green]✓[/green] Added #{name} → {path}\n")
            _show_channel_table(mappings)
        elif action == "r":
            if not mappings:
                console.print("  [dim]Nothing to remove.[/dim]\n")
                continue
            name = Prompt.ask("  Channel name to remove", console=console).strip().lstrip("#")
            if name in mappings:
                del mappings[name]
                console.print(f"  [green]✓[/green] Removed #{name}\n")
                _show_channel_table(mappings)
            else:
                console.print(f"  [red]Channel #{name} not found.[/red]\n")

    channel_dirs_str = _serialize_channel_dirs(mappings)
    return base_dir, channel_dirs_str


def _step_allowed_users(defaults: dict[str, str]) -> str:
    """Step 5: Configure allowed users interactively. Returns comma-separated IDs or empty."""
    console.rule("Step 5 of 7: Allowed Users")
    console.print("\n  Restrict who can use the bot by Slack member ID.")
    console.print("  (Find yours: Slack profile -> ⋮ menu -> Copy member ID)\n")

    allowed = _parse_allowed_users(defaults.get("ALLOWED_USERS", ""))
    _show_allowed_users(allowed)

    while True:
        action = Prompt.ask(
            "  \\[a]dd / \\[r]emove / \\[d]one",
            choices=["a", "r", "d"],
            default="d",
            console=console,
        )
        if action == "d":
            break
        elif action == "a":
            user_id = Prompt.ask("  Slack member ID", console=console).strip()
            if not user_id:
                continue
            if user_id in allowed:
                console.print(f"  [dim]{user_id} already in list.[/dim]\n")
                continue
            allowed.append(user_id)
            console.print(f"  [green]✓[/green] Added {user_id}\n")
            _show_allowed_users(allowed)
        elif action == "r":
            if not allowed:
                console.print("  [dim]Nothing to remove.[/dim]\n")
                continue
            user_id = Prompt.ask("  Member ID to remove", console=console).strip()
            if user_id in allowed:
                allowed.remove(user_id)
                console.print(f"  [green]✓[/green] Removed {user_id}\n")
                _show_allowed_users(allowed)
            else:
                console.print(f"  [red]{user_id} not found.[/red]\n")

    return ",".join(allowed)


def _step_claude_settings(defaults: dict[str, str]) -> dict[str, str]:
    """Step 6: Configure Claude model, permission mode, and debug. Returns non-empty values."""
    console.rule("Step 6 of 7: Claude Settings")
    console.print("\n  Press Enter to skip (or keep current value). Type '-' to clear.")

    values: dict[str, str] = {}

    console.print("\n  [bold]Model[/bold]")
    console.print("  Override the Claude model used for tasks.")
    console.print("  Options: sonnet, opus, haiku (or any Claude model ID).")
    console.print("  Leave empty to use the Claude CLI default.")
    val = _prompt_with_default(
        "Model",
        defaults.get("CLAUDE_MODEL", ""),
    )
    if val:
        values["CLAUDE_MODEL"] = val

    console.print("\n  [bold]Permission Mode[/bold]")
    console.print("  Controls what Claude Code can do without asking.")
    console.print("  [dim]default[/dim]           — prompts on first use of each tool")
    console.print("  [dim]acceptEdits[/dim]       — auto-accepts file edits, prompts for shell")
    console.print("  [dim]plan[/dim]              — read-only analysis, no modifications")
    console.print("  [dim]dontAsk[/dim]           — auto-denies unless pre-approved via rules")
    console.print("  [dim]bypassPermissions[/dim] — skips all prompts (containers/VMs only)")
    valid_modes = {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
    while True:
        val = _prompt_with_default(
            "Permission mode",
            defaults.get("CLAUDE_PERMISSION_MODE", "acceptEdits"),
        )
        if not val or val in valid_modes:
            break
        console.print(f"  [red]Invalid mode '{val}'. Choose from: {', '.join(sorted(valid_modes))}[/red]\n")
    if val:
        values["CLAUDE_PERMISSION_MODE"] = val

    console.print("\n  [bold]Log File[/bold]")
    console.print("  Write logs to a file instead of console output.")
    console.print("  Useful with --detach mode. Leave empty to log to console only.")
    val = _prompt_with_default(
        "Log file path (e.g. goose.log)",
        defaults.get("LOG_FILE", ""),
    )
    if val:
        values["LOG_FILE"] = val

    console.print("\n  [bold]Debug[/bold]")
    console.print("  Verbose logging (to console, or log file if configured).")
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
    from .config import env_file

    env_path = env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
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

    # Step 4: Directory Settings
    base_dir, channel_dirs = _step_channel_dirs(existing)

    # Step 5: Allowed Users
    allowed_users = _step_allowed_users(existing)

    # Step 6: Claude Settings
    claude_settings = _step_claude_settings(existing)

    # Step 7: Write config
    console.rule("Step 7 of 7: Writing config")
    console.print()

    env_values: dict[str, str] = {
        "SLACK_BOT_TOKEN": bot_token,
        "SLACK_APP_TOKEN": app_token,
    }
    if base_dir:
        env_values["BASE_DIRECTORY"] = base_dir
    if channel_dirs:
        env_values["CHANNEL_DIRS"] = channel_dirs
    if allowed_users:
        env_values["ALLOWED_USERS"] = allowed_users
    env_values.update(claude_settings)

    _write_env(env_path, env_values)
    console.print(Panel(
        f"[green]✓[/green] Wrote {env_path}\n"
        "[green]✓[/green] Next: run [bold]goose run[/bold]\n"
        "[green]✓[/green] Tip: invite @Goose with /invite @Goose"
    ))
