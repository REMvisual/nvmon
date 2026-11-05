"""Subprocess wrapper for NVIDIA CLI tools.

Two fleet-hardening rules live here (both ported from the legacy tool nvdd
fork after they were verified on real A6000 / Mosaic / QSync hardware):

1. **One NVIDIA configure* call at a time.** ``configureQsync2.exe enable``
   hangs (60 s timeouts) when the monitor thread's ``queryAll`` overlaps
   it, and ``configureMosaic set`` fails rc=2 under a concurrent query.
   Every ``configure*.exe`` invocation takes ``_HW_LOCK``; ``nvidia-smi``
   is left unserialized because it never contends with the display tools.

2. **Cancellable jobs.** An apply / restore / boot-apply runs inside a
   *cancel scope* (``enter_cancel_scope`` / ``exit_cancel_scope``). When a
   newer request wants the hardware (latest-request-wins) it sets
   ``CANCEL``; the running job notices at its next ``run()`` or ``sleep()``
   and unwinds via :class:`Cancelled`. The scope is thread-local, so the
   monitor thread's own queries are never interrupted.
"""

from __future__ import annotations

import subprocess
import threading
import time

from . import log
from .types import CommandResult


# --- Serialization ------------------------------------------------------------
# RLock so a configure* call made while the same thread already holds the lock
# (nested helper → run) doesn't deadlock.
_HW_LOCK = threading.RLock()


def _serialized(exe: str) -> bool:
    """True for the NVIDIA display tools that must never overlap."""
    return "configure" in (exe or "").lower()


# --- Job cancellation ---------------------------------------------------------
CANCEL = threading.Event()
_scope = threading.local()
_jobs_lock = threading.Lock()
_jobs_active = 0


class Cancelled(BaseException):
    """The running apply / restore / boot-apply was cancelled.

    BaseException on purpose: the pipelines wrap steps in ``except
    Exception`` blocks that must not swallow a cancellation.
    """


def job_active() -> bool:
    """True while an apply / restore / boot-apply owns the NVIDIA tools.

    The agent monitor skips its poll (and keeps the last status) while this
    is True so its queries don't queue behind, or overlap with, the job.
    """
    return _jobs_active > 0


def enter_cancel_scope() -> None:
    global _jobs_active
    _scope.active = True
    with _jobs_lock:
        _jobs_active += 1


def exit_cancel_scope() -> None:
    global _jobs_active
    _scope.active = False
    with _jobs_lock:
        _jobs_active = max(0, _jobs_active - 1)


def in_cancel_scope() -> bool:
    return bool(getattr(_scope, "active", False))


def check_cancel() -> None:
    """Raise :class:`Cancelled` if this thread's job has been cancelled."""
    if getattr(_scope, "active", False) and CANCEL.is_set():
        raise Cancelled("cancelled (superseded or stopped)")


def run(
    exe: str,
    args: list[str],
    *,
    timeout: int = 60,
    dry_run: bool = False,
    label: str = "",
    quiet: bool = False,
) -> CommandResult:
    """Run an NVIDIA CLI executable and capture output.

    Args:
        exe: Path to the executable.
        args: Command-line arguments.
        timeout: Max seconds to wait.
        dry_run: If True, log the command but don't execute.
        label: Human-readable label for logging.
        quiet: If True, suppress failure warnings (caller handles logging).

    Returns:
        CommandResult with stdout, stderr, return code, etc.
    """
    check_cancel()
    logger = log.get()
    cmd = [exe] + args
    cmd_str = " ".join(cmd)

    if label:
        logger.debug(f"[{label}] {cmd_str}")
    else:
        logger.debug(f"exec: {cmd_str}")

    if dry_run:
        logger.info(f"[DRY RUN] Would execute: {cmd_str}")
        return CommandResult(
            success=True,
            return_code=0,
            stdout="[dry-run]",
            stderr="",
            command=cmd_str,
            duration_ms=0,
        )

    start = time.monotonic()
    serialize = _serialized(exe)
    try:
        if serialize:
            _HW_LOCK.acquire()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        finally:
            if serialize:
                _HW_LOCK.release()
        elapsed_ms = int((time.monotonic() - start) * 1000)

        # Some NVIDIA tools (configureQsync2.exe) always exit with rc=1
        # but produce valid output on stdout. Treat as success if we got output.
        success = proc.returncode == 0
        if not success and "qsync" in label.lower():
            combined = proc.stdout + proc.stderr
            if combined and ("QSync" in combined or "Found" in combined):
                success = True

        result = CommandResult(
            success=success,
            return_code=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            command=cmd_str,
            duration_ms=elapsed_ms,
        )

        if not result.success and not quiet:
            logger.warning(f"Command failed (rc={proc.returncode}): {cmd_str}")
            # configureMosaic / configureDdisplay put the reason on STDOUT
            # (stderr is usually empty on rc=2) — log whichever carries it.
            detail = result.stderr or result.stdout
            if detail:
                logger.warning(f"  output: {detail[-600:]}")

        return result

    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(f"Command timed out after {timeout}s: {cmd_str}")
        return CommandResult(
            success=False,
            return_code=-1,
            stdout="",
            stderr=f"Timed out after {timeout}s",
            command=cmd_str,
            duration_ms=elapsed_ms,
        )
    except FileNotFoundError:
        logger.error(f"Executable not found: {exe}")
        return CommandResult(
            success=False,
            return_code=-1,
            stdout="",
            stderr=f"Executable not found: {exe}",
            command=cmd_str,
            duration_ms=0,
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(f"Error running {cmd_str}: {e}")
        return CommandResult(
            success=False,
            return_code=-1,
            stdout="",
            stderr=str(e),
            command=cmd_str,
            duration_ms=elapsed_ms,
        )


def failure_detail(result: CommandResult, limit: int = 600) -> str:
    """Human-readable reason a command failed — stderr, else stdout tail.

    NVIDIA's display tools report errors on stdout with an empty stderr, so
    ``result.stderr`` alone is usually blank in a failure message.
    """
    text = (result.stderr or result.stdout or "").strip()
    if not text:
        return f"rc={result.return_code}"
    return text[-limit:]


def sleep(seconds: float, label: str = "") -> None:
    """Sleep with optional logging. Cancellable inside a job scope."""
    logger = log.get()
    if label:
        logger.debug(f"Waiting {seconds}s ({label})...")
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        check_cancel()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.25, left))
