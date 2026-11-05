"""Per-app state machine.

States (spec §6):
    stopped     — no process
    pre_launch  — running pre_launch hooks
    starting    — Popen fired, < 2s since spawn
    running     — alive past the settle window
    stopping    — kill issued, awaiting graceful exit
    crashed     — exited unexpectedly
    restarting  — in auto-restart backoff
    parked      — crash-cooldown ceiling hit, operator intervention required

All transitions emit a single log line with `[app=<name>] <from> -> <to>`.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Any


class AppState(enum.Enum):
    STOPPED = "stopped"
    PRE_LAUNCH = "pre_launch"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"
    RESTARTING = "restarting"
    PARKED = "parked"


# Valid transitions per spec §6. Everything else is a hard error in the
# supervisor. We allow stopped→stopped and running→running as no-ops since
# they're produced by idempotent launch/kill.
_TRANSITIONS: dict[AppState, set[AppState]] = {
    AppState.STOPPED: {
        AppState.PRE_LAUNCH,
        AppState.STARTING,
        AppState.STOPPED,
    },
    AppState.PRE_LAUNCH: {
        AppState.STARTING,
        AppState.STOPPED,   # aborted by hook failure or kill
    },
    AppState.STARTING: {
        AppState.RUNNING,
        AppState.CRASHED,   # process died during settle window
        AppState.STOPPING,
    },
    AppState.RUNNING: {
        AppState.STOPPING,
        AppState.CRASHED,
        AppState.STOPPED,   # clean exit with expected exit code
        AppState.RUNNING,
    },
    AppState.STOPPING: {
        AppState.STOPPED,
        AppState.CRASHED,   # unexpected exit code during graceful stop
    },
    AppState.CRASHED: {
        AppState.RESTARTING,
        AppState.PARKED,
        AppState.STOPPED,   # operator chose not to restart
    },
    AppState.RESTARTING: {
        AppState.PRE_LAUNCH,
        AppState.STARTING,
        AppState.STOPPED,   # restart cancelled by operator kill
        AppState.PARKED,
    },
    AppState.PARKED: {
        AppState.STOPPED,   # operator forced restart
        AppState.PRE_LAUNCH,
        AppState.STARTING,
    },
}


@dataclass
class AbortRecord:
    run_id: str = ""
    hook: str = ""
    reason: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "hook": self.hook,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


@dataclass
class AppRuntime:
    """Mutable per-app runtime state. Guarded by a supervisor's lock."""
    name: str
    state: AppState = AppState.STOPPED
    pid: int | None = None
    restart_count: int = 0
    last_exit_code: int | None = None
    last_started_at: float | None = None
    last_exited_at: float | None = None
    next_restart_at: float | None = None
    current_hook_index: int | None = None
    current_hook_total: int | None = None
    current_hook_type: str = ""
    current_hook_status: str = ""
    run_id: str = ""
    last_abort: AbortRecord | None = None
    config_drift: bool = False
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    @property
    def uptime_seconds(self) -> float:
        if self.state != AppState.RUNNING or self.last_started_at is None:
            return 0.0
        return time.time() - self.last_started_at

    def to_status_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "state": self.state.value,
            "pid": self.pid,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "restart_count": self.restart_count,
            "last_exit_code": self.last_exit_code,
            "last_started_at": self.last_started_at,
            "last_exited_at": self.last_exited_at,
            "next_restart_at": self.next_restart_at,
            "config_drift": self.config_drift,
        }
        if self.state == AppState.PRE_LAUNCH:
            out["current_hook"] = {
                "index": self.current_hook_index,
                "total": self.current_hook_total,
                "type": self.current_hook_type,
                "status": self.current_hook_status,
                "run_id": self.run_id,
            }
        if self.last_abort is not None:
            out["last_abort"] = self.last_abort.to_dict()
        return out


class StateTransitionError(RuntimeError):
    """Attempted an illegal state transition."""


def transition(
    runtime: AppRuntime,
    new_state: AppState,
    *,
    logger: Any = None,
    reason: str = "",
) -> None:
    """Transition `runtime.state` to `new_state`. Caller must hold runtime._lock.

    Raises StateTransitionError if the transition isn't allowed by §6.
    """
    current = runtime.state
    allowed = _TRANSITIONS.get(current, set())
    if new_state not in allowed:
        raise StateTransitionError(
            f"illegal transition for app {runtime.name!r}: "
            f"{current.value} -> {new_state.value}"
        )
    runtime.state = new_state
    if logger is not None:
        suffix = f" ({reason})" if reason else ""
        logger.info(
            f"[app={runtime.name}] {current.value} -> {new_state.value}{suffix}"
        )


__all__ = [
    "AbortRecord",
    "AppRuntime",
    "AppState",
    "StateTransitionError",
    "transition",
]
