"""Controller self-restart (no OS reboot) — the monitor-side twin of
``nvmon/agent/system.py::restart_listener()``.

``POST /system/restart_monitor`` (REM-gated "Restart monitor" in the
dashboard's power menu) calls :func:`restart_monitor`, which spawns
``bats/_restart_monitor_helper.bat`` fully detached and returns at once.
The helper re-spawns itself so it survives the controller's own taskkill,
waits ``delay_seconds`` for the HTTP 202 to flush, runs ``kill_monitor.bat``
(closes the "nvmon - monitor" console and the python child, sweeps :9997)
and then ``run_monitor.bat`` — which comes back minimized and re-checks its
Startup shortcut. Everything the helper does goes to
``logs/restart_monitor-<stamp>.log``.

Why a .bat and not inline PowerShell: same reasons as the listener helper —
no argv quoting games, inspectable on disk, reliable log.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from .. import config as cfgmod
from .. import log

HELPER_NAME = "_restart_monitor_helper.bat"
DEFAULT_DELAY_SECONDS = 2

# CreateProcess flags (Windows). DETACHED_PROCESS and CREATE_NO_WINDOW are
# mutually exclusive; cmd.exe needs a console, the helper minimizes itself.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

# Set by `nvmon app`: its in-process controller is not the monitor box's
# run_monitor.bat controller, so bouncing "the monitor" from it would kill
# the real one (or start run_monitor.bat on a random laptop).
DISABLED_REASON: str | None = None


def restart_monitor(delay_seconds: int = DEFAULT_DELAY_SECONDS, *, open_browser: bool = False) -> dict[str, Any]:
    """Queue a controller bounce. Returns the JSON body for the route:
    ``ok`` False with ``error`` when a required file is missing or the
    helper could not be spawned; ``ok`` True once the helper is running
    (the controller itself dies ``delay_seconds`` later)."""
    try:
        delay = max(0, int(delay_seconds))
    except (TypeError, ValueError):
        delay = DEFAULT_DELAY_SECONDS
    logger = log.get()
    base_dir = cfgmod.BASE_DIR
    helper = base_dir / "bats" / HELPER_NAME
    run_bat = base_dir / "run_monitor.bat"
    kill_bat = base_dir / "kill_monitor.bat"

    result: dict[str, Any] = {
        "ok": False,
        "action": "restart_monitor",
        "delay_seconds": delay,
        "helper": str(helper),
        "log_hint": "logs/restart_monitor-<stamp>.log on the monitor box",
    }
    if DISABLED_REASON:
        result["error"] = DISABLED_REASON
        logger.warning(f"[system] restart_monitor refused: {DISABLED_REASON}")
        return result
    missing = [p.name for p in (helper, run_bat, kill_bat) if not p.is_file()]
    if missing:
        result["error"] = (
            f"missing on the monitor box: {', '.join(missing)} (looked under {base_dir}) — "
            f"re-copy NVMON and restart the controller once by hand"
        )
        logger.error(f"[system] restart_monitor: {result['error']}")
        return result

    args = ["cmd.exe", "/c", str(helper), str(delay), "browser" if open_browser else "nobrowser"]
    popen_kwargs: dict[str, Any] = dict(
        cwd=str(base_dir),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP

    logger.warning(
        f"[system] queueing controller self-restart in {delay}s via {HELPER_NAME} "
        f"(kill_monitor.bat -> run_monitor.bat, minimized). Trace: {result['log_hint']}"
    )
    try:
        subprocess.Popen(args, **popen_kwargs)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"could not spawn {HELPER_NAME}: {exc}"
        logger.error(f"[system] restart_monitor: {result['error']}")
        return result

    result["ok"] = True
    result["message"] = (
        f"controller restarting in {delay}s; run_monitor.bat relaunches it minimized "
        f"({'opens' if open_browser else 'does not open'} a browser)"
    )
    return result
