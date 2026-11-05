"""nvmon listener — single-process supervisor for agent + launcher.

Runs both daemons as child processes attached to this terminal. Output
interleaves into one console; closing the window or pressing Ctrl-C
stops both children cleanly.

Usage:
    python -m nvmon listener
    python -m nvmon listener --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import Sequence

from . import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nvmon listener",
        description="Supervise agent (:9998) + launcher (:9996) in one console.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be supervised, then exit.",
    )
    args = parser.parse_args(argv)

    # A click in this console window starts a QuickEdit selection and Windows
    # then blocks every write to the console until Esc/Enter — the agent and
    # launcher inherit this console, so their log lines block too, status
    # goes stale and POSTs hang while GETs keep answering. Seen on a render
    # node after someone clicked the listener window (the legacy tool 5d8efd4).
    from .util import disable_console_quick_edit
    disable_console_quick_edit()

    print(f"nvmon listener {__version__}")
    print("  agent     -> http://localhost:9998")
    print("  launcher  -> http://localhost:9996")
    print("  Ctrl-C or close this window to stop both.")

    if args.dry_run:
        print("[listener] dry-run: not spawning children.")
        return 0

    py = sys.executable
    children: list[tuple[str, subprocess.Popen]] = []

    # Spawn both as console-attached children. They inherit this stdout/stderr,
    # so output interleaves naturally. Ctrl-C in this console fires a CTRL_C
    # event on the whole process group — both children get it for free.
    for name in ("agent", "launcher"):
        p = subprocess.Popen([py, "-m", "nvmon", name])
        print(f"[listener] {name} started (pid {p.pid})")
        children.append((name, p))

    exit_code = 0
    try:
        while True:
            time.sleep(1.0)
            for name, p in children:
                rc = p.poll()
                if rc is not None:
                    print(f"[listener] {name} exited (code {rc}) — shutting down peer")
                    exit_code = rc or 0
                    raise _PeerDied()
    except KeyboardInterrupt:
        print("\n[listener] Ctrl-C — stopping children")
    except _PeerDied:
        pass
    finally:
        for name, p in children:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(f"[listener] {name} did not exit on terminate — killing")
                    p.kill()
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
        print("[listener] both stopped.")

    return exit_code


class _PeerDied(Exception):
    pass


if __name__ == "__main__":
    sys.exit(main())
