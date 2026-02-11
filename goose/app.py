"""Goose application — Send coding tasks from Slack, get results back."""

import argparse
import asyncio
import json
import logging
import os
import signal
import shutil
import ssl
import sys
from pathlib import Path

import certifi

# Fix macOS Python SSL cert issue
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import Config
from .handlers import register_handlers
from .sessions import SessionStore

logger = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> AsyncApp:
    """Create and configure the Slack Bolt app."""
    if config is None:
        config = Config.from_env()

    app = AsyncApp(token=config.slack_bot_token)
    sessions = SessionStore()

    register_handlers(app, config, sessions)

    # Store references on the app for access elsewhere
    app._goose_config = config  # type: ignore[attr-defined]
    app._goose_sessions = sessions  # type: ignore[attr-defined]

    return app


async def start(config: Config | None = None) -> None:
    """Start the bot."""
    if config is None:
        config = Config.from_env()

    log_level = getattr(logging, config.log_level, logging.INFO)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_dir:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        log_file = config.log_dir / f"goose-{datetime.now():%Y-%m-%d}.log"
        handlers.append(logging.FileHandler(str(log_file)))

    logging.basicConfig(level=log_level, format=log_format, handlers=handlers)

    app = create_app(config)

    handler = AsyncSocketModeHandler(app, config.slack_app_token)
    logger.info("Starting Goose...")

    await handler.connect_async()
    logger.info("Goose is running. Press Ctrl+C to stop.")

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _handle_signal() -> None:
        if stop.is_set():
            print("\nForce quit.")
            os._exit(0)
        print("\nShutting down... (press Ctrl+C again to force quit)")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    await stop.wait()
    try:
        await asyncio.wait_for(handler.close_async(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("Graceful shutdown timed out, exiting anyway.")
    logger.info("Goodbye.")


# ---------------------------------------------------------------------------
# CLI: goose handoff
# ---------------------------------------------------------------------------


def _resolve_session_id(explicit: str | None) -> str:
    """Return the session ID — use explicit value or auto-detect from history."""
    if explicit:
        return explicit
    history = Path.home() / ".claude" / "history.jsonl"
    if not history.exists():
        print("Error: no Claude history found. Pass --session-id explicitly.", file=sys.stderr)
        sys.exit(1)
    last_line = history.read_text().strip().rsplit("\n", 1)[-1]
    session_id = json.loads(last_line).get("sessionId")
    if not session_id:
        print("Error: could not extract session ID from history. Pass --session-id explicitly.", file=sys.stderr)
        sys.exit(1)
    return session_id


async def _handoff(args: argparse.Namespace) -> None:
    """Post a handoff message to Slack so the session can be resumed."""
    from slack_sdk.web.async_client import AsyncWebClient

    config = Config.from_env()
    args.session_id = _resolve_session_id(args.session_id)

    # Resolve channel name
    channel_name: str | None = args.channel
    if not channel_name:
        cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
        channel_name = config.resolve_dir_channel(cwd)
        if not channel_name:
            print(
                f"Error: could not resolve a Slack channel for {cwd}.\n"
                "Use --channel to specify one explicitly, or configure CHANNEL_DIRS.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Look up channel ID via Slack API
    client = AsyncWebClient(token=config.slack_bot_token)
    channel_id: str | None = None
    cursor: str | None = None
    while True:
        kwargs: dict = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await client.conversations_list(**kwargs)
        for ch in resp.get("channels", []):
            if ch["name"] == channel_name:
                channel_id = ch["id"]
                break
        if channel_id:
            break
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    if not channel_id:
        print(f"Error: channel #{channel_name} not found.", file=sys.stderr)
        sys.exit(1)

    # Build the handoff message
    parts = [args.summary]
    if args.questions:
        parts.append(f"\n{args.questions}")
    parts.append(f"\n_(session_id: {args.session_id})_")
    text = "\n".join(parts)

    await client.chat_postMessage(channel=channel_id, text=text)
    print(f"Handoff posted to #{channel_name}")


def handoff(args: argparse.Namespace) -> None:
    """Sync wrapper for the handoff command."""
    asyncio.run(_handoff(args))


# ---------------------------------------------------------------------------
# CLI: goose install-skill
# ---------------------------------------------------------------------------


def install_skill(args: argparse.Namespace) -> None:
    """Install the goose-handoff skill for Claude Code."""
    # Resolve path to the goose binary
    goose_path = shutil.which("goose")
    if not goose_path:
        # Fallback: use the repo checkout
        goose_path = str(Path(__file__).resolve().parent.parent / "goose")

    # Read the bundled template
    template_path = Path(__file__).resolve().parent / "skill.md"
    if not template_path.exists():
        print(f"Error: skill template not found at {template_path}", file=sys.stderr)
        sys.exit(1)
    template = template_path.read_text()

    # Replace placeholder
    content = template.replace("{{GOOSE_PATH}}", goose_path)

    # Write to ~/.claude/skills/goose-handoff/SKILL.md
    target_dir = Path.home() / ".claude" / "skills" / "goose-handoff"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "SKILL.md"
    target.write_text(content)

    print(f"Installed goose-handoff skill to {target}")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


class _GooseParser(argparse.ArgumentParser):
    """ArgumentParser that shows our help instead of argparse's error message."""

    def error(self, message: str) -> None:
        _print_help()
        sys.exit(2)


def _build_parser() -> argparse.ArgumentParser:
    parser = _GooseParser(
        prog="goose",
        description="Goose — Send coding tasks from Slack, get results back",
    )
    sub = parser.add_subparsers(dest="command")

    # goose run (default)
    run_parser = sub.add_parser("run", help="Start the Slack bot (default)")
    run_parser.add_argument("--detach", action="store_true", help="Run in the background (daemonize)")

    # goose handoff
    ho = sub.add_parser("handoff", help="Post a handoff message to Slack")
    ho.add_argument("--session-id", default=None, help="Claude session ID (auto-detected from history if omitted)")
    ho.add_argument("--summary", required=True, help="Summary text for the handoff message")
    ho.add_argument("--channel", default=None, help="Slack channel name (auto-resolved from cwd if omitted)")
    ho.add_argument("--cwd", default=None, help="Working directory to resolve channel from (defaults to $PWD)")
    ho.add_argument("--questions", default=None, help="Open questions to post as a thread reply")

    # goose setup
    sub.add_parser("setup", help="Guided setup wizard")

    # goose install-skill
    sub.add_parser("install-skill", help="Install the goose-handoff skill for Claude Code")

    # goose help
    sub.add_parser("help", help="Show this help message")

    return parser


def _print_help() -> None:
    print("""Goose — Send coding tasks from Slack, get results back

Usage: goose <command> [options]

Commands:
  setup            Guided setup wizard
  run              Start the Slack bot
  handoff          Post a handoff message to Slack
  install-skill    Install the goose-handoff skill for Claude Code
  help             Show this help message

Examples:
  goose setup                                    Set up Goose
  goose run                                      Start the bot
  goose run --detach                              Start in the background
  goose handoff --summary "..."                  Hand off a session to Slack
  goose install-skill                            Install the handoff skill

Run 'goose <command> --help' for details on a specific command.""")


def _run_detached() -> None:
    """Fork into background and run the bot as a daemon."""
    config = Config.from_env()
    if not config.log_dir:
        print("Error: --detach requires LOG_DIR to be configured.", file=sys.stderr)
        print("Run 'goose setup' to set a log directory.", file=sys.stderr)
        sys.exit(1)

    pid = os.fork()
    if pid > 0:
        # Parent — print PID and exit
        print(f"Goose running in background (PID {pid}). Logs → {config.log_dir}")
        sys.exit(0)

    # Child — detach from terminal
    os.setsid()
    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    asyncio.run(start(config))


def main() -> None:
    """Sync entrypoint for the console script."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        if getattr(args, "detach", False):
            _run_detached()
        else:
            asyncio.run(start())
    elif args.command == "setup":
        from .setup import setup_command
        setup_command(args)
    elif args.command == "handoff":
        handoff(args)
    elif args.command == "install-skill":
        install_skill(args)
    else:
        # No command or 'help' — show help
        _print_help()
