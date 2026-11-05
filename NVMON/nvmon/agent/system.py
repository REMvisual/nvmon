"""System power actions — restart / shutdown / abort wrappers around
Windows' built-in `shutdown.exe`.

Both restart() and shutdown() queue the operation with a short delay
(default 8s) so the HTTP response returns BEFORE the OS starts the
shutdown sequence. The delay also gives an operator who clicked the
wrong button a window to cancel via `shutdown /a` (or POST /system/abort
on this agent) before the machine actually goes down.

Admin is required: the calling endpoint enforces config.is_admin(), and
shutdown.exe itself rejects non-elevated callers with "Access is denied".

Recovery after restart is handled by the "nvmon Listener" Scheduled Task
installed by install.bat --listener (/SC ONLOGON /RL HIGHEST). The task
re-launches `run_listener.bat` automatically on next logon, so the
agent + launcher come back without operator action.

Recovery after shutdown requires physical power-on or Wake-on-LAN —
this module does NOT attempt WoL or any auto-revive. The dashboard's
two-step confirmation surfaces that asymmetry to the operator.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Literal

from .. import config as cfgmod
from .. import log


# 8 seconds: short enough not to feel like a hang, long enough for the
# HTTP response to return and for an operator to issue `shutdown /a`
# from another shell if they realize they hit the wrong button.
DEFAULT_DELAY_SECONDS = 8


def system_uptime_seconds() -> float | None:
    """Seconds since Windows booted (``GetTickCount64``), or None when the
    value is unavailable (non-Windows dev box, ctypes failure).

    Used by the monitor to tell a real OS boot (uptime of a few minutes)
    from a listener restart hours into the day — only the former should
    replay the active profile (site, 2026-11-01: "Restart all listeners"
    to pick up new code re-ran every node's boot-apply).
    """
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return float(kernel32.GetTickCount64()) / 1000.0
    except Exception:  # noqa: BLE001 — AttributeError off Windows, anything else
        return None


@dataclass
class PowerResult:
    success: bool
    action: str
    delay_seconds: int
    return_code: int
    stdout: str
    stderr: str
    error: str = ""


def _run_shutdown(*flags: str) -> tuple[int, str, str]:
    """Run shutdown.exe with the given flags. Returns (rc, stdout, stderr).

    The call is synchronous but shutdown.exe returns immediately after
    queueing the request with the LSA — the actual OS shutdown happens
    after `/t <delay>`.
    """
    cmd = ["shutdown.exe", *flags]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return -1, "", f"shutdown.exe not found: {exc}"
    except subprocess.TimeoutExpired:
        return -1, "", "shutdown.exe did not return within 10s"


def _power_action(action: Literal["restart", "shutdown"], delay_seconds: int) -> PowerResult:
    delay = max(0, int(delay_seconds))
    flag = "/r" if action == "restart" else "/s"
    comment = f"nvmon: {action} requested via dashboard"
    # /f         force-close apps (don't hang on a stuck child process)
    # /d p:0:0   classify as planned, "other planned" — appears in event log
    # /c "..."   note shown in the shutdown notification + event log
    flags = [flag, "/t", str(delay), "/f", "/d", "p:0:0", "/c", comment]
    log.get().warning(
        f"[system] queuing {action.upper()} in {delay}s "
        f"(shutdown.exe {' '.join(flags)})"
    )
    rc, out, err = _run_shutdown(*flags)
    success = rc == 0
    if not success:
        log.get().error(
            f"[system] shutdown.exe failed (rc={rc}): {err.strip() or out.strip()}"
        )
    return PowerResult(
        success=success,
        action=action,
        delay_seconds=delay,
        return_code=rc,
        stdout=out,
        stderr=err,
        error="" if success else (err.strip() or f"shutdown.exe rc={rc}"),
    )


def restart(delay_seconds: int = DEFAULT_DELAY_SECONDS) -> PowerResult:
    """Queue a Windows restart in `delay_seconds` (default 8)."""
    return _power_action("restart", delay_seconds)


def shutdown(delay_seconds: int = DEFAULT_DELAY_SECONDS) -> PowerResult:
    """Queue a Windows shutdown in `delay_seconds` (default 8)."""
    return _power_action("shutdown", delay_seconds)


def abort() -> PowerResult:
    """Cancel a queued shutdown/restart (`shutdown.exe /a`).

    If nothing is queued, shutdown.exe returns nonzero — we surface that
    as success=False but it's not really an error from the operator's
    point of view.
    """
    log.get().warning("[system] aborting any queued shutdown (shutdown.exe /a)")
    rc, out, err = _run_shutdown("/a")
    success = rc == 0
    return PowerResult(
        success=success,
        action="abort",
        delay_seconds=0,
        return_code=rc,
        stdout=out,
        stderr=err,
        error="" if success else (err.strip() or f"shutdown.exe /a rc={rc}"),
    )


# ---------------------------------------------------------------------------
# Listener self-restart (no OS reboot)
# ---------------------------------------------------------------------------

# Task Scheduler entry created by install.bat --listener. The listener
# self-restart prefers this path because /Run on a user-owned task is UAC-
# free, but we fall back to kill_listener.bat + run_listener.bat when the
# task isn't registered (e.g. the operator launched the listener manually).
LISTENER_TASK_NAME = "nvmon Listener"


def restart_listener(delay_seconds: int = 2) -> PowerResult:
    """Stop and re-launch the listener.

    Spawns bats/_restart_listener_helper.bat detached so the helper
    survives the agent's own taskkill. The bat handles:
      1. timeout to let the HTTP 202 flush
      2. schtasks /End + /Run if the 'nvmon Listener' task is registered
      3. otherwise kill_listener.bat + run_listener.bat
    Everything goes to logs/restart_listener-<stamp>.log so failures are
    diagnosable instead of silent.

    Why a .bat instead of inline PowerShell-via-Popen: the previous
    implementation passed a multi-line PS script through `-Command`, which
    got mangled by Windows argv quoting (embedded "), and the detached
    PS process died before reaching its first Out-File. A real .bat on
    disk has no quoting issues, can be inspected, and writes its log
    reliably.
    """
    delay = max(0, int(delay_seconds))
    logger = log.get()

    base_dir = cfgmod.BASE_DIR
    helper_bat = base_dir / "bats" / "_restart_listener_helper.bat"
    run_bat = base_dir / "run_listener.bat"

    if not helper_bat.is_file():
        msg = (
            f"_restart_listener_helper.bat not found at {helper_bat}. "
            f"Re-deploy NVVMON to this node and bounce the listener once "
            f"manually (kill_listener.bat + run_listener.bat) so the new "
            f"agent code can pick up the helper."
        )
        logger.error(f"[system] restart_listener: {msg}")
        return PowerResult(
            success=False, action="restart_listener", delay_seconds=delay,
            return_code=-1, stdout="", stderr=msg, error=msg,
        )
    if not run_bat.is_file():
        msg = f"run_listener.bat not found at {run_bat}; cannot restart"
        logger.error(f"[system] restart_listener: {msg}")
        return PowerResult(
            success=False, action="restart_listener", delay_seconds=delay,
            return_code=-1, stdout="", stderr=msg, error=msg,
        )

    # Flags: detach fully so the agent's own taskkill doesn't take the
    # helper with it. We DO want a console (cmd.exe needs one) but
    # minimized via `start "" /min` from the helper itself; here we
    # request DETACHED_PROCESS without CREATE_NO_WINDOW (the two are
    # mutually exclusive per CreateProcess docs).
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    logger.warning(
        f"[system] queueing listener self-restart in {delay}s "
        f"(via {helper_bat.name}). Watch logs/restart_listener-*.log for trace."
    )
    try:
        # cmd.exe /c <bat> <delay>. Each arg as a separate list element so
        # subprocess does the Windows argv escaping for us.
        subprocess.Popen(
            ["cmd.exe", "/c", str(helper_bat), str(delay)],
            cwd=str(base_dir),
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return PowerResult(
            success=False, action="restart_listener", delay_seconds=delay,
            return_code=-1, stdout="", stderr=str(exc), error=str(exc),
        )
    except Exception as exc:
        logger.error(f"[system] restart_listener failed to spawn helper: {exc}")
        return PowerResult(
            success=False, action="restart_listener", delay_seconds=delay,
            return_code=-1, stdout="", stderr=str(exc), error=str(exc),
        )

    return PowerResult(
        success=True, action="restart_listener", delay_seconds=delay,
        return_code=0,
        stdout=f"helper spawned; logs/restart_listener-*.log will record the trace",
        stderr="",
    )
