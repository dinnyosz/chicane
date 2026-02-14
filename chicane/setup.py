"""Guided setup wizard for Chicane (chicane setup)."""

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
        console.print("  [dim]No user restrictions — all workspace users can use Chicane.[/dim]\n")
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
    console.rule("Step 1 of 16: Create Slack App")

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
    console.rule("Step 2 of 16: Get Bot Token")

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
    console.rule("Step 3 of 16: Get App Token")

    if default:
        console.print("\n  App token found in config. Press Enter to keep it,")
        console.print("  or paste a new one. To generate a new token:")

    console.print("""
  1. In the sidebar, go to "Basic Information"
  2. Scroll to "App-Level Tokens" -> "Generate Token and Scopes"
  3. Name it anything (e.g. "chicane-socket")
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
    console.rule("Step 4 of 16: Directory Settings")
    console.print("\n  [yellow]Note:[/yellow] Chicane will run Claude Code in these directories remotely.")
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
    console.print("  Each mapping allows Chicane to run Claude Code in that directory.\n")

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
    console.rule("Step 5 of 16: Allowed Users")
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


def _step_claude_model(default: str = "") -> str:
    """Step 6: Configure Claude model."""
    console.rule("Step 6 of 16: Claude Model")
    console.print("\n  Override the Claude model used for tasks.")
    console.print("  Options: sonnet, opus, haiku (or any Claude model ID).")
    console.print("  Leave empty to use the Claude CLI default.")
    return _prompt_with_default("Model", default)


def _step_permission_mode(default: str = "acceptEdits") -> str:
    """Step 7: Configure permission mode."""
    console.rule("Step 7 of 16: Permission Mode")
    console.print("\n  Controls what Claude Code can do autonomously.")
    console.print("  [dim]acceptEdits[/dim]       — auto-accepts file edits, use allowed tools for shell")
    console.print("  [dim]dontAsk[/dim]           — auto-denies everything except allowed tools")
    console.print("  [dim]bypassPermissions[/dim] — auto-approves everything (containers/VMs only)")
    valid_modes = {"acceptEdits", "dontAsk", "bypassPermissions"}
    while True:
        val = _prompt_with_default("Permission mode", default)
        if not val or val in valid_modes:
            if val == "bypassPermissions":
                console.print(Panel(
                    "[bold red]WARNING:[/bold red] bypassPermissions grants Claude "
                    "unrestricted access to all tools including shell commands. "
                    "Anyone who can message this bot will effectively have shell "
                    "access to this machine.\n\n"
                    "Only use this in isolated environments (containers, VMs).",
                    title="Security Warning",
                    border_style="red",
                ))
                if not Confirm.ask("  Continue with bypassPermissions?", default=False, console=console):
                    continue
            return val
        console.print(f"  [red]Invalid mode '{val}'. Choose from: {', '.join(sorted(valid_modes))}[/red]\n")


def _parse_allowed_tools(raw: str) -> list[str]:
    """Parse CLAUDE_ALLOWED_TOOLS string into a list of tool rules."""
    return [t.strip() for t in raw.split(",") if t.strip()]


def _show_allowed_tools(tools: list[str]) -> None:
    """Display current allowed tools."""
    if not tools:
        console.print("  [dim]No pre-approved tools — using your Claude config as-is.[/dim]\n")
        return
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Tool Rule", style="bold")
    for tool in tools:
        table.add_row(tool)
    console.print(table)
    console.print()


def _step_allowed_tools(default: str = "") -> str:
    """Step 8: Configure allowed tools interactively. Returns comma-separated rules."""
    console.rule("Step 8 of 16: Allowed Tools")
    console.print("\n  Pre-approve specific tools so Claude doesn't prompt for them.")
    console.print("  [yellow]Warning:[/yellow] This overrides your Claude settings.json permissions.")
    console.print("  Leave empty to use your existing Claude config as-is.")
    console.print("  Patterns: [dim]Bash(npm run *)[/dim], [dim]Edit(./src/**)[/dim], [dim]Read[/dim], [dim]WebFetch[/dim]\n")

    tools = _parse_allowed_tools(default)
    _show_allowed_tools(tools)

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
            rule = Prompt.ask("  Tool rule", console=console).strip()
            if not rule:
                continue
            if rule in tools:
                console.print(f"  [dim]{rule} already in list.[/dim]\n")
                continue
            tools.append(rule)
            console.print(f"  [green]✓[/green] Added {rule}\n")
            _show_allowed_tools(tools)
        elif action == "r":
            if not tools:
                console.print("  [dim]Nothing to remove.[/dim]\n")
                continue
            rule = Prompt.ask("  Tool rule to remove", console=console).strip()
            if rule in tools:
                tools.remove(rule)
                console.print(f"  [green]✓[/green] Removed {rule}\n")
                _show_allowed_tools(tools)
            else:
                console.print(f"  [red]{rule} not found.[/red]\n")

    return ",".join(tools)


def _show_disallowed_tools(tools: list[str]) -> None:
    """Display current disallowed tools."""
    if not tools:
        console.print("  [dim]No blocked tools — Claude can use any tool allowed by permissions.[/dim]\n")
        return
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Blocked Tool", style="bold")
    for tool in tools:
        table.add_row(tool)
    console.print(table)
    console.print()


def _step_disallowed_tools(default: str = "") -> str:
    """Step 9: Configure disallowed tools interactively. Returns comma-separated rules."""
    console.rule("Step 9 of 16: Disallowed Tools")
    console.print("\n  Block specific tools so Claude cannot use them.")
    console.print("  Complement to allowed tools — these are always denied.")
    console.print("  Patterns: [dim]Bash[/dim], [dim]Edit(./secrets/**)[/dim], [dim]WebFetch[/dim]\n")

    tools = _parse_allowed_tools(default)
    _show_disallowed_tools(tools)

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
            rule = Prompt.ask("  Tool rule", console=console).strip()
            if not rule:
                continue
            if rule in tools:
                console.print(f"  [dim]{rule} already in list.[/dim]\n")
                continue
            tools.append(rule)
            console.print(f"  [green]✓[/green] Added {rule}\n")
            _show_disallowed_tools(tools)
        elif action == "r":
            if not tools:
                console.print("  [dim]Nothing to remove.[/dim]\n")
                continue
            rule = Prompt.ask("  Tool rule to remove", console=console).strip()
            if rule in tools:
                tools.remove(rule)
                console.print(f"  [green]✓[/green] Removed {rule}\n")
                _show_disallowed_tools(tools)
            else:
                console.print(f"  [red]{rule} not found.[/red]\n")

    return ",".join(tools)


def _show_setting_sources(sources: list[str]) -> None:
    """Display current setting sources."""
    if not sources:
        console.print("  [dim]No setting sources — Claude won't load any config files.[/dim]\n")
        return
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Source", style="bold")
    table.add_column("Description")
    descriptions = {
        "user": "~/.claude/settings.json (global user preferences)",
        "project": ".claude/settings.json (project-level, checked into git)",
        "local": ".claude/settings.local.json (local overrides, gitignored)",
    }
    for source in sources:
        table.add_row(source, descriptions.get(source, ""))
    console.print(table)
    console.print()


def _step_setting_sources(default: str = "user,project,local") -> str:
    """Step 10: Configure which Claude config scopes are loaded. Returns comma-separated sources."""
    console.rule("Step 10 of 16: Setting Sources")
    console.print("\n  Which Claude config scopes to load.")
    console.print("  [dim]user[/dim]     — ~/.claude/settings.json (global user preferences)")
    console.print("  [dim]project[/dim]  — .claude/settings.json (project-level, checked into git)")
    console.print("  [dim]local[/dim]    — .claude/settings.local.json (local overrides, gitignored)")
    console.print("  Default loads all three.\n")

    valid = {"user", "project", "local"}
    sources = [s.strip() for s in default.split(",") if s.strip()]
    _show_setting_sources(sources)

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
            source = Prompt.ask("  Source (user/project/local)", console=console).strip()
            if not source:
                continue
            if source not in valid:
                console.print(f"  [red]Invalid source '{source}'. Must be: user, project, or local.[/red]\n")
                continue
            if source in sources:
                console.print(f"  [dim]{source} already in list.[/dim]\n")
                continue
            sources.append(source)
            console.print(f"  [green]✓[/green] Added {source}\n")
            _show_setting_sources(sources)
        elif action == "r":
            if not sources:
                console.print("  [dim]Nothing to remove.[/dim]\n")
                continue
            source = Prompt.ask("  Source to remove", console=console).strip()
            if source in sources:
                sources.remove(source)
                console.print(f"  [green]✓[/green] Removed {source}\n")
                _show_setting_sources(sources)
            else:
                console.print(f"  [red]{source} not found.[/red]\n")

    return ",".join(sources)


def _step_max_turns(default: str = "") -> str:
    """Step 11: Configure max turns per message."""
    console.rule("Step 11 of 16: Max Turns")
    console.print("\n  Maximum number of agentic turns Claude can take per message.")
    console.print("  Each turn is one API call; complex tasks may need 20-50+ turns.")
    console.print("  Leave empty for unlimited (Claude decides when to stop).")
    while True:
        val = _prompt_with_default("Max turns", default)
        if not val:
            return ""
        try:
            n = int(val)
            if n < 1:
                console.print("  [red]Must be a positive integer.[/red]\n")
                continue
            return str(n)
        except ValueError:
            console.print("  [red]Must be a positive integer.[/red]\n")


def _step_max_budget(default: str = "") -> str:
    """Step 12: Configure max budget per message."""
    console.rule("Step 12 of 16: Max Budget")
    console.print("\n  Maximum cost in USD that Claude can spend per message.")
    console.print("  Prevents runaway spending on long-running tasks.")
    console.print("  Leave empty for no budget limit.")
    while True:
        val = _prompt_with_default("Max budget (USD)", default)
        if not val:
            return ""
        try:
            n = float(val)
            if n <= 0:
                console.print("  [red]Must be a positive number.[/red]\n")
                continue
            return val
        except ValueError:
            console.print("  [red]Must be a positive number (e.g. 1.50).[/red]\n")


def _step_rate_limit(default: str = "10") -> str:
    """Step 13 of 16: Configure rate limit."""
    console.rule("Step 13 of 16: Rate Limit")
    console.print("\n  Maximum messages per user per minute.")
    console.print("  Prevents abuse and runaway costs from message floods.")
    console.print("  Default: 10 messages/minute per user.")
    while True:
        val = _prompt_with_default("Rate limit (msgs/min)", default)
        if not val:
            return default
        try:
            n = int(val)
            if n < 1:
                console.print("  [red]Must be a positive integer.[/red]\n")
                continue
            return str(n)
        except ValueError:
            console.print("  [red]Must be a positive integer.[/red]\n")


def _step_logging(defaults: dict[str, str]) -> tuple[str, str]:
    """Step 14: Configure log directory and log level. Returns (log_dir, log_level)."""
    console.rule("Step 14 of 16: Logging")
    from platformdirs import user_log_dir

    default_log_dir = defaults.get("LOG_DIR", "") or user_log_dir("chicane", appauthor=False)
    console.print("\n  [bold]Log Directory[/bold]")
    console.print("  Directory for log files (a new file is created per day).")
    console.print("  Logs go to both console and file. Required for --detach mode.")
    log_dir = _prompt_with_default(
        "Log directory",
        default_log_dir,
    )
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
    console.print("\n  [bold]Log Level[/bold]")
    console.print("  Messages below this level are suppressed.")
    console.print("  DEBUG > INFO > WARNING > ERROR (most → least verbose)")
    while True:
        log_level = _prompt_with_default(
            "Log level",
            defaults.get("LOG_LEVEL", "INFO"),
        ).upper()
        if not log_level:
            log_level = "INFO"
        if log_level in valid_levels:
            break
        console.print(f"  [red]Invalid level. Choose from: {', '.join(sorted(valid_levels))}[/red]\n")
    return log_dir, log_level


def _step_verbosity(default: str = "verbose") -> str:
    """Step 15: Configure verbosity level."""
    console.rule("Step 15 of 16: Verbosity")
    console.print("\n  Controls how much detail is shown in Slack during Claude sessions.")
    console.print("  [dim]minimal[/dim]  — Only final text responses and completion summaries")
    console.print("  [dim]normal[/dim]   — Text + tool call summaries and errors")
    console.print("  [dim]verbose[/dim]  — Text + tool calls + tool outputs/results (default)")
    valid_levels = {"minimal", "normal", "verbose"}
    while True:
        val = _prompt_with_default("Verbosity", default).lower()
        if not val:
            return default
        if val in valid_levels:
            return val
        console.print(f"  [red]Invalid level '{val}'. Choose from: {', '.join(sorted(valid_levels))}[/red]\n")


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
        console.print("\n  [yellow]Aborted.[/yellow] Completed steps were saved.")
        sys.exit(130)


def _run_wizard(args) -> None:
    """Run the interactive wizard steps.

    Config is saved progressively after each step so that a Ctrl+C
    mid-flow preserves everything completed so far.
    """
    from .config import env_file

    env_path = env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_existing_env(env_path)
    env_values: dict[str, str] = dict(existing)

    def _save() -> None:
        _write_env(env_path, env_values)

    console.print()
    console.print(Panel("[bold]Chicane — Setup Wizard[/bold]"))

    has_tokens = bool(existing.get("SLACK_BOT_TOKEN") and existing.get("SLACK_APP_TOKEN"))

    def _set_or_clear(key: str, val: str) -> None:
        if val:
            env_values[key] = val
        else:
            env_values.pop(key, None)

    # Step 1: Create Slack App
    _step_create_app(has_tokens)

    # Step 2: Bot Token
    env_values["SLACK_BOT_TOKEN"] = _step_bot_token(existing.get("SLACK_BOT_TOKEN", ""))
    _save()

    # Step 3: App Token
    env_values["SLACK_APP_TOKEN"] = _step_app_token(existing.get("SLACK_APP_TOKEN", ""))
    _save()

    # Step 4: Directory Settings
    base_dir, channel_dirs = _step_channel_dirs(existing)
    _set_or_clear("BASE_DIRECTORY", base_dir)
    _set_or_clear("CHANNEL_DIRS", channel_dirs)
    _save()

    # Step 5: Allowed Users
    _set_or_clear("ALLOWED_USERS", _step_allowed_users(existing))
    _save()

    # Step 6: Claude Model
    _set_or_clear("CLAUDE_MODEL", _step_claude_model(existing.get("CLAUDE_MODEL", "")))
    _save()

    # Step 7: Permission Mode
    _set_or_clear("CLAUDE_PERMISSION_MODE", _step_permission_mode(existing.get("CLAUDE_PERMISSION_MODE", "acceptEdits")))
    _save()

    # Step 8: Allowed Tools
    _set_or_clear("CLAUDE_ALLOWED_TOOLS", _step_allowed_tools(existing.get("CLAUDE_ALLOWED_TOOLS", "")))
    _save()

    # Step 9: Disallowed Tools
    _set_or_clear("CLAUDE_DISALLOWED_TOOLS", _step_disallowed_tools(existing.get("CLAUDE_DISALLOWED_TOOLS", "")))
    _save()

    # Step 10: Setting Sources
    _set_or_clear("CLAUDE_SETTING_SOURCES", _step_setting_sources(existing.get("CLAUDE_SETTING_SOURCES", "user,project,local")))
    _save()

    # Step 11: Max Turns
    _set_or_clear("CLAUDE_MAX_TURNS", _step_max_turns(existing.get("CLAUDE_MAX_TURNS", "")))
    _save()

    # Step 12: Max Budget
    _set_or_clear("CLAUDE_MAX_BUDGET_USD", _step_max_budget(existing.get("CLAUDE_MAX_BUDGET_USD", "")))
    _save()

    # Step 13: Rate Limit
    _set_or_clear("RATE_LIMIT", _step_rate_limit(existing.get("RATE_LIMIT", "10")))
    _save()

    # Step 14: Logging
    log_dir, log_level = _step_logging(existing)
    _set_or_clear("LOG_DIR", log_dir)
    _set_or_clear("LOG_LEVEL", log_level if log_level != "INFO" else "")
    _save()

    # Step 15: Verbosity
    _set_or_clear("VERBOSITY", _step_verbosity(existing.get("VERBOSITY", "normal")))
    _save()

    # Step 16: Done
    console.rule("Step 16 of 16: Done")
    console.print()
    console.print(Panel(
        f"[green]✓[/green] Config saved to {env_path}\n"
        "[green]✓[/green] Next: run [bold]chicane run[/bold]\n"
        "[green]✓[/green] Tip: invite @Chicane with /invite @Chicane"
    ))
