"""Chicane application — When Claude Code can't go straight, take the chicane."""

import argparse
import asyncio
import atexit
import json
import logging
import os
import signal
import sys
from pathlib import Path

import certifi

# Fix macOS Python SSL cert issue
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import Config, config_dir, generate_session_alias, save_handoff_session
from .handlers import register_handlers
from .sessions import SessionStore

logger = logging.getLogger(__name__)

PID_FILE = config_dir() / "chicane.pid"


def save_terminal_state():
    """Save terminal state and register cleanup for restore on any exit.

    Returns the saved state, or None if not a tty / not supported.
    Registers both an atexit handler and a SIGTERM handler so the
    terminal is restored even when the process is killed.
    """
    if not sys.stdin.isatty():
        return None
    try:
        import termios

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)

        def _restore_terminal(*_args):
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except (OSError, termios.error):
                pass

        atexit.register(_restore_terminal)
        signal.signal(signal.SIGTERM, lambda *a: (_restore_terminal(), sys.exit(143)))

        # Fix ISIG right now in case it's already broken.
        attrs = termios.tcgetattr(fd)
        if not (attrs[3] & termios.ISIG):
            attrs[3] |= termios.ISIG
            termios.tcsetattr(fd, termios.TCSANOW, attrs)

        return saved
    except (ImportError, OSError):
        return None


def _acquire_pidfile() -> None:
    """Write our PID to the pidfile, or exit if another instance is running."""
    if PID_FILE.exists():
        try:
            other_pid = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            logger.warning("Corrupt PID file, overwriting.")
        else:
            try:
                os.kill(other_pid, 0)
            except OSError:
                logger.warning("Stale PID file (process %d is dead), overwriting.", other_pid)
            else:
                print(
                    f"Error: Chicane is already running (PID {other_pid}). "
                    f"Kill it first or remove {PID_FILE}",
                    file=sys.stderr,
                )
                sys.exit(1)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _release_pidfile() -> None:
    """Remove the PID file if it still contains our PID."""
    try:
        if PID_FILE.exists() and int(PID_FILE.read_text().strip()) == os.getpid():
            PID_FILE.unlink()
    except (ValueError, OSError):
        pass


def create_app(config: Config | None = None) -> AsyncApp:
    """Create and configure the Slack Bolt app."""
    if config is None:
        config = Config.from_env()

    app = AsyncApp(token=config.slack_bot_token)
    sessions = SessionStore()

    register_handlers(app, config, sessions)

    # Store references on the app for access elsewhere
    app._chicane_config = config  # type: ignore[attr-defined]
    app._chicane_sessions = sessions  # type: ignore[attr-defined]

    return app


async def start(config: Config | None = None) -> None:
    """Start the bot."""
    _acquire_pidfile()
    try:
        if config is None:
            config = Config.from_env()

        log_level = getattr(logging, config.log_level, logging.INFO)
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

        handlers: list[logging.Handler] = [logging.StreamHandler()]
        if config.log_dir:
            config.log_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime
            log_file = config.log_dir / f"chicane-{datetime.now():%Y-%m-%d}.log"
            handlers.append(logging.FileHandler(str(log_file)))

        logging.basicConfig(level=log_level, format=log_format, handlers=handlers)

        # Quiet noisy third-party loggers — their DEBUG output (PING/PONG
        # heartbeats, websocket frames) drowns out useful chicane logs.
        for noisy in ("slack_bolt", "slack_sdk", "aiohttp", "websocket"):
            logging.getLogger(noisy).setLevel(max(log_level, logging.INFO))

        # Security warnings
        if config.claude_permission_mode == "bypassPermissions":
            logger.warning(
                "SECURITY: bypassPermissions is active — Claude has unrestricted "
                "tool access. All allowed users effectively have shell access to "
                "this machine."
            )
        if not config.allowed_users:
            logger.warning(
                "SECURITY: ALLOWED_USERS is not set — the bot will reject all "
                "messages. Configure ALLOWED_USERS or run 'chicane setup'."
            )

        app = create_app(config)
        sessions: SessionStore = app._chicane_sessions  # type: ignore[attr-defined]

        handler = AsyncSocketModeHandler(app, config.slack_app_token)
        logger.info("Starting Chicane...")

        await handler.connect_async()
        logger.info("Chicane is running. Press Ctrl+C to stop.")

        async def _periodic_cleanup() -> None:
            """Clean up expired sessions every hour."""
            while True:
                await asyncio.sleep(3600)
                try:
                    await sessions.cleanup()
                except Exception:
                    logger.warning("Session cleanup failed", exc_info=True)

        cleanup_task = asyncio.ensure_future(_periodic_cleanup())

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        # Save and enforce terminal settings so Ctrl+C always generates
        # SIGINT.  Some terminal emulators (iTerm2 text selection) can
        # leave the tty with isig disabled, making ^C echo literally
        # instead of raising the signal.
        _saved_termios = None
        if sys.stdin.isatty():
            try:
                import termios
                fd = sys.stdin.fileno()
                _saved_termios = termios.tcgetattr(fd)
                # Periodically re-enable ISIG in case something clears it.
                def _ensure_isig() -> None:
                    try:
                        attrs = termios.tcgetattr(fd)
                        if not (attrs[3] & termios.ISIG):
                            attrs[3] |= termios.ISIG
                            termios.tcsetattr(fd, termios.TCSANOW, attrs)
                    except (termios.error, OSError):
                        pass
                _ensure_isig()  # Enforce immediately on startup.

                async def _isig_watchdog() -> None:
                    while not stop.is_set():
                        _ensure_isig()
                        await asyncio.sleep(2)
                isig_task = asyncio.ensure_future(_isig_watchdog())
            except (ImportError, OSError):
                _saved_termios = None

        def _handle_signal() -> None:
            print("\nShutting down... (press Ctrl+C again to force quit)")
            stop.set()
            # Install raw signal handlers for force-quit that bypass the
            # event loop — if the loop is blocked (e.g. closing the
            # websocket), loop-based handlers never fire.
            signal.signal(signal.SIGINT, lambda *_: os._exit(1))
            signal.signal(signal.SIGTERM, lambda *_: os._exit(1))

        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _handle_signal)
        else:
            # Windows: add_signal_handler is not supported. Use a raw
            # signal handler so Ctrl+C still triggers graceful shutdown.
            signal.signal(signal.SIGINT, lambda *_: _handle_signal())
            signal.signal(signal.SIGTERM, lambda *_: _handle_signal())

        await stop.wait()

        # Cancel the watchdog and restore terminal settings with ISIG
        # guaranteed enabled so Ctrl+C works after shutdown.
        if _saved_termios is not None:
            isig_task.cancel()
            try:
                import termios
                restored = list(_saved_termios)
                restored[3] |= termios.ISIG
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, restored)
            except (ImportError, OSError):
                pass
        cleanup_task.cancel()
        await sessions.shutdown()
        try:
            await asyncio.wait_for(handler.close_async(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Graceful shutdown timed out, exiting anyway.")
        logger.info("Goodbye.")
    finally:
        _release_pidfile()


# ---------------------------------------------------------------------------
# CLI: chicane handoff
# ---------------------------------------------------------------------------


def resolve_session_id(explicit: str | None = None) -> str:
    """Return the session ID — use explicit value or auto-detect from history.

    Raises ValueError if auto-detection fails.
    """
    if explicit:
        return explicit
    history = Path.home() / ".claude" / "history.jsonl"
    if not history.exists():
        raise ValueError("No Claude history found. Pass session_id explicitly.")
    last_line = history.read_text().strip().rsplit("\n", 1)[-1]
    session_id = json.loads(last_line).get("sessionId")
    if not session_id:
        raise ValueError(
            "Could not extract session ID from history. Pass session_id explicitly."
        )
    return session_id


async def resolve_channel_id(
    client: "AsyncWebClient", channel_name: str
) -> str | None:
    """Look up a Slack channel ID by name. Returns None if not found."""
    cursor: str | None = None
    while True:
        kwargs: dict = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await client.conversations_list(**kwargs)
        for ch in resp.get("channels", []):
            if ch["name"] == channel_name:
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return None


async def _handoff(args: argparse.Namespace) -> None:
    """Post a handoff message to Slack so the session can be resumed."""
    from slack_sdk.web.async_client import AsyncWebClient

    config = Config.from_env()
    try:
        args.session_id = resolve_session_id(args.session_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

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
    channel_id = await resolve_channel_id(client, channel_name)
    if not channel_id:
        print(f"Error: channel #{channel_name} not found.", file=sys.stderr)
        sys.exit(1)

    # Build the handoff message
    alias = generate_session_alias()
    save_handoff_session(alias, args.session_id)

    parts = [args.summary]
    if args.questions:
        parts.append(f"\n{args.questions}")
    parts.append(f"\n_(session: {alias})_")
    text = "\n".join(parts)

    await client.chat_postMessage(channel=channel_id, text=text)
    print(f"Handoff posted to #{channel_name} ({alias})")


def handoff(args: argparse.Namespace) -> None:
    """Sync wrapper for the handoff command."""
    asyncio.run(_handoff(args))



# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


class _ChicaneParser(argparse.ArgumentParser):
    """ArgumentParser that shows our help instead of argparse's error message."""

    def error(self, message: str) -> None:
        _print_help()
        sys.exit(2)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ChicaneParser(
        prog="chicane",
        description="Chicane — When Claude Code can't go straight, take the chicane",
    )
    sub = parser.add_subparsers(dest="command")

    # chicane run (default)
    run_parser = sub.add_parser("run", help="Start the Slack bot (default)")
    run_parser.add_argument("--detach", action="store_true", help="Run in the background (daemonize)")

    # chicane handoff
    ho = sub.add_parser("handoff", help="Post a handoff message to Slack")
    ho.add_argument("--session-id", default=None, help="Claude session ID (auto-detected from history if omitted)")
    ho.add_argument("--summary", required=True, help="Summary text for the handoff message")
    ho.add_argument("--channel", default=None, help="Slack channel name (auto-resolved from cwd if omitted)")
    ho.add_argument("--cwd", default=None, help="Working directory to resolve channel from (defaults to $PWD)")
    ho.add_argument("--questions", default=None, help="Open questions to post as a thread reply")

    # chicane setup
    sub.add_parser("setup", help="Guided setup wizard")

    # chicane help
    sub.add_parser("help", help="Show this help message")

    return parser


def _print_help() -> None:
    print("""Chicane — When Claude Code can't go straight, take the chicane

Usage: chicane <command> [options]

Commands:
  setup            Guided setup wizard
  run              Start the Slack bot
  handoff          Post a handoff message to Slack
  help             Show this help message

Examples:
  chicane setup                                Set up Chicane
  chicane run                                  Start the bot
  chicane run --detach                          Start in the background
  chicane handoff --summary "..."              Hand off a session to Slack

Run 'chicane <command> --help' for details on a specific command.""")


def _run_detached() -> None:
    """Fork into background and run the bot as a daemon."""
    config = Config.from_env()
    if not config.log_dir:
        print("Error: --detach requires LOG_DIR to be configured.", file=sys.stderr)
        print("Run 'chicane setup' to set a log directory.", file=sys.stderr)
        sys.exit(1)

    pid = os.fork()
    if pid > 0:
        # Parent — print PID and exit
        print(f"Chicane running in background (PID {pid}). Logs → {config.log_dir}")
        sys.exit(0)

    # Child — detach from terminal
    os.setsid()
    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    asyncio.run(start(config))


def main() -> None:
    """Sync entrypoint for the console script."""
    save_terminal_state()
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        if getattr(args, "detach", False):
            _run_detached()
        else:
            try:
                asyncio.run(start())
            except KeyboardInterrupt:
                pass
    elif args.command == "setup":
        from .setup import setup_command
        setup_command(args)
    elif args.command == "handoff":
        handoff(args)
    else:
        # No command or 'help' — show help
        _print_help()
