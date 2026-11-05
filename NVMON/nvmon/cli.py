"""Top-level CLI dispatcher.

Each subcommand delegates to its module's `main(argv)` with the remaining
argv after the subcommand name. This lets each module own its own argparse
surface without coupling.

Usage:
    python -m nvmon agent [--port 9998] [--dry-run] ...
    python -m nvmon controller [--port 9997] [--dry-run] ...
    python -m nvmon launcher [--port 9996] [--dry-run] ...
    python -m nvmon discover [--timeout 5] ...
    python -m nvmon version
"""

from __future__ import annotations

import sys
from typing import Sequence

from . import __version__


def _run_agent(argv: Sequence[str]) -> int:
    from .agent.main import main as _main
    return _main(list(argv))


def _run_controller(argv: Sequence[str]) -> int:
    from .controller.main import main as _main
    return _main(list(argv))


def _run_launcher(argv: Sequence[str]) -> int:
    from .launcher.main import main as _main
    return _main(list(argv))


def _run_listener(argv: Sequence[str]) -> int:
    from .listener import main as _main
    return _main(list(argv))


def _run_app(argv: Sequence[str]) -> int:
    from .app import main as _main
    return _main(list(argv))


def _run_discover(argv: Sequence[str]) -> int:
    from .discovery_cli import main as _main
    return _main(list(argv))


def _run_version(argv: Sequence[str]) -> int:
    print(f"nvmon {__version__}")
    return 0


def _run_save_profile(argv: Sequence[str]) -> int:
    """Capture the local node's live state and save as a profile.

    Usage: python -m nvmon save-profile <name> [--description "..."]
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="nvmon save-profile",
        description="Capture live GPU/display/QSync state into profiles/<name>.json",
    )
    parser.add_argument("name", help="Profile filename stem (without .json)")
    parser.add_argument("--description", default="", help="Free-form description")
    args = parser.parse_args(list(argv))

    from .agent import profile_capture
    try:
        path = profile_capture.save_current_state(
            args.name, description=args.description,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"capture failed: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {path}")
    return 0


_DISPATCH = {
    "agent": _run_agent,
    "controller": _run_controller,
    "launcher": _run_launcher,
    "listener": _run_listener,
    "app": _run_app,
    "discover": _run_discover,
    "save-profile": _run_save_profile,
    "version": _run_version,
}


def _print_usage() -> None:
    print(f"nvmon {__version__}")
    print()
    print("Usage: python -m nvmon <command> [options]")
    print()
    print("Commands:")
    print("  agent          Run the per-machine agent daemon (:9998)")
    print("  controller     Run the controller + unified dashboard (:9997)")
    print("  launcher       Run the process-launch daemon (:9996)")
    print("  listener       Run agent + launcher together in one console")
    print("  app            Desktop app: local controller + dashboard window + floating bubble")
    print("  discover       Browse nvmon services on the local network")
    print("  save-profile   Snapshot live state into profiles/<name>.json")
    print("  version        Print version and exit")
    print()
    print("Run `python -m nvmon <command> --help` for command-specific options.")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help"):
        _print_usage()
        return 0

    if args[0] in ("-V", "--version"):
        print(f"nvmon {__version__}")
        return 0

    cmd, rest = args[0], args[1:]
    handler = _DISPATCH.get(cmd)
    if handler is None:
        print(f"nvmon: unknown command: {cmd}", file=sys.stderr)
        print("Run `python -m nvmon --help` for usage.", file=sys.stderr)
        return 2

    return int(handler(rest) or 0)


if __name__ == "__main__":
    sys.exit(main())
