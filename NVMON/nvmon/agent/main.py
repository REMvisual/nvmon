"""`nvmon agent` subcommand entrypoint.

Wired from `nvmon.cli` in Wave 1 post-integration. Standalone-invocable:

    python -m nvmon.agent.main [--port 9998] [--profile P] [--dry-run]

The CLI is intentionally small: port, poll interval, active profile, verbosity.
Heavy lifting lives in `nvmon.agent.server.serve(...)`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .. import __version__, config, log
from . import server


def build_parser() -> argparse.ArgumentParser:
    """Build the `nvmon agent` argument parser."""
    parser = argparse.ArgumentParser(
        prog="nvmon agent",
        description=(
            "nvmon agent — per-machine monitoring + control daemon for "
            "NVIDIA Windows render nodes. Serves HTTP on the configured port "
            "(default 9998)."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="TCP port to bind (overrides [agent].port from nvmon.toml)",
    )
    parser.add_argument(
        "--bind", default="0.0.0.0",
        help="Interface address to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--profile", default=None,
        help="Name of the profile to validate against on each poll",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=None,
        help=(
            "Polling interval in seconds (overrides "
            "[agent].poll_interval_seconds from nvmon.toml)"
        ),
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to nvmon.toml (overrides the default BASE_DIR/nvmon.toml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging on the console",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color in console output",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Initialize the agent, log the planned bind address + capabilities, "
            "then exit without binding the HTTP port. Intended for smoke tests."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"nvmon agent {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse args and start the agent daemon. Returns an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Redirect config path if requested — must happen before log setup so the
    # `log_dir` override takes effect.
    if args.config:
        from pathlib import Path
        new_cfg = Path(args.config)
        if not new_cfg.is_file():
            print(f"ERROR: config file not found: {new_cfg}", file=sys.stderr)
            return 2
        # Monkey-patch the resolved CONFIG_FILE + force-reload the cache.
        config.CONFIG_FILE = new_cfg  # type: ignore[attr-defined]
        config.load_config(force_reload=True)

    logger = log.setup(verbose=args.verbose, no_color=args.no_color)
    logger.info(f"nvmon agent {__version__} starting")

    try:
        return server.serve(
            port=args.port,
            profile_name=args.profile,
            poll_interval=args.poll_interval,
            bind_host=args.bind,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        logger.error(f"Fatal error: {exc}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
