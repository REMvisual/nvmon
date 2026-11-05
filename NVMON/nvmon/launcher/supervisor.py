"""Per-app supervisor.

Each `AppSupervisor` owns exactly one app config + runtime. It:

    1. Runs the state machine from spec §6.
    2. Walks `pre_launch` hooks in a background thread on launch.
    3. Spawns the process via subprocess.Popen (shell=False, absolute path).
    4. Drains stdout+stderr to a rotated log file.
    5. Polls Popen.poll() every 1s and routes crashes to auto-restart
       with backoff per spec §7.

Threading model:
    - `_supervisor_thread` polls Popen.poll() and drives crash → restart.
    - `_prelaunch_thread` runs pre_launch hooks for a single launch run.
    - `_drain_thread` (from logs.py) drains child stdout.
    - `runtime._lock` protects state + runtime fields.
    - `_cancel_event` is set on kill to abandon in-flight hooks.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import hooks as hooks_mod
from . import logs as logs_mod
from .apps import AppConfig
from .state import (
    AbortRecord,
    AppRuntime,
    AppState,
    StateTransitionError,
    transition,
)


# Settle window: time between Popen returning and state moving from
# STARTING to RUNNING. 2s matches spec §6.
_SETTLE_WINDOW_SECONDS = 2.0

# Polling cadence for Popen.poll() (spec §7).
_POLL_INTERVAL_SECONDS = 1.0

# How long a process must have been running before restart_count resets.
_RESTART_RESET_WINDOW_SECONDS = 300.0


class AppSupervisor:
    """One supervisor per configured app. Owned by the server."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        log_dir: str,
        agent_port: int,
        per_app_log_bytes: int,
        per_app_log_keep: int,
        launcher_is_elevated: bool,
        logger: Any,
    ) -> None:
        self.cfg = cfg
        self.runtime = AppRuntime(name=cfg.name)
        self._log_dir = log_dir
        self._agent_port = agent_port
        self._launcher_is_elevated = launcher_is_elevated
        self._logger = logger

        self._writer = logs_mod.AppLogWriter(
            log_dir, cfg.name,
            max_bytes=per_app_log_bytes,
            keep=per_app_log_keep,
        )

        self._proc: subprocess.Popen[bytes] | None = None
        self._drain_stop: threading.Event | None = None
        self._drain_thread: logs_mod.DrainThread | None = None

        self._supervisor_thread: threading.Thread | None = None
        self._supervisor_stop = threading.Event()
        self._prelaunch_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()

        # Guards "only one launch attempt in flight at a time".
        self._launch_in_flight = False

    # ------------------------------------------------------------------
    # Public API — called from the HTTP layer
    # ------------------------------------------------------------------

    def start_supervisor(self) -> None:
        """Start the background polling thread (idempotent)."""
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._supervisor_stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name=f"sup:{self.cfg.name}",
            daemon=True,
        )
        self._supervisor_thread.start()

    def shutdown(self) -> None:
        """Ask the supervisor to stop and terminate any child process."""
        self._supervisor_stop.set()
        self._cancel_event.set()
        try:
            self._do_kill(graceful=True)
        except Exception:
            pass

    def launch(self) -> tuple[int, dict[str, Any]]:
        """POST /app/<name>/launch — spec §4a.

        Returns (http_code, body). Idempotent: already-running → 200,
        elevation refusal → 409.
        """
        with self.runtime._lock:
            state = self.runtime.state
            if state in (AppState.RUNNING, AppState.STARTING,
                         AppState.PRE_LAUNCH):
                return 200, {
                    "status": "already_running",
                    "state": state.value,
                    "pid": self.runtime.pid,
                }
            if self.cfg.elevated and not self._launcher_is_elevated:
                return 409, {
                    "error": "launcher not elevated; app requires elevation",
                    "hint": "relaunch launcher elevated",
                }
            if state == AppState.PARKED:
                # Operator force-launch; reset counters.
                self.runtime.restart_count = 0
                transition(self.runtime, AppState.STOPPED,
                           logger=self._logger, reason="operator relaunch from parked")
            if self._launch_in_flight:
                return 409, {"error": "launch already in flight"}
            self._launch_in_flight = True
            run_id = _short_uuid()
            self.runtime.run_id = run_id
            self.runtime.last_abort = None
            self._cancel_event.clear()

        # Kick off pre_launch sequence in a background thread.
        self._prelaunch_thread = threading.Thread(
            target=self._prelaunch_and_start,
            name=f"prelaunch:{self.cfg.name}",
            args=(run_id, False),   # is_auto_restart=False
            daemon=True,
        )
        self._prelaunch_thread.start()
        return 202, {"status": "accepted", "run_id": run_id}

    def kill(self) -> tuple[int, dict[str, Any]]:
        """POST /app/<name>/kill. Idempotent."""
        with self.runtime._lock:
            state = self.runtime.state
            if state in (AppState.STOPPED, AppState.PARKED):
                return 200, {"status": "not_running", "state": state.value}

            # Cancel any in-flight pre_launch first.
            self._cancel_event.set()

            if state == AppState.PRE_LAUNCH:
                # pre_launch thread will observe cancel and land in stopped.
                return 200, {"status": "cancelling_pre_launch"}

            if state in (AppState.STARTING, AppState.RUNNING):
                try:
                    transition(self.runtime, AppState.STOPPING,
                               logger=self._logger, reason="operator kill")
                except StateTransitionError:
                    pass

            if state == AppState.RESTARTING:
                try:
                    transition(self.runtime, AppState.STOPPED,
                               logger=self._logger, reason="cancel restart")
                except StateTransitionError:
                    pass
                return 200, {"status": "restart_cancelled"}

        self._do_kill(graceful=True)
        return 200, {"status": "kill_issued"}

    def restart(self) -> tuple[int, dict[str, Any]]:
        """POST /app/<name>/restart — kill then relaunch."""
        self.kill()
        # Give Popen a moment to release; the supervisor loop will finalize
        # the STOPPED transition.
        deadline = time.monotonic() + self.cfg.kill_grace_seconds + 5.0
        while time.monotonic() < deadline:
            with self.runtime._lock:
                if self.runtime.state in (AppState.STOPPED, AppState.PARKED):
                    break
            time.sleep(0.1)
        # Reset restart counter on manual restart (spec §7).
        with self.runtime._lock:
            self.runtime.restart_count = 0
        return self.launch()

    def status(self) -> dict[str, Any]:
        with self.runtime._lock:
            return self.runtime.to_status_dict()

    def logs(self, tail: int) -> str:
        return logs_mod.read_tail(self._log_dir, self.cfg.name, tail)

    # ------------------------------------------------------------------
    # Pre-launch sequence (runs in its own thread)
    # ------------------------------------------------------------------

    def _prelaunch_and_start(self, run_id: str, is_auto_restart: bool) -> None:
        """Walk pre_launch hooks then spawn the process.

        Honours `pre_launch_on_auto_restart` per spec §4a.
        """
        try:
            hooks_to_run: list[hooks_mod.HookConfig] = list(self.cfg.pre_launch)
            if is_auto_restart and not self.cfg.pre_launch_on_auto_restart:
                hooks_to_run = []

            if hooks_to_run:
                with self.runtime._lock:
                    try:
                        transition(
                            self.runtime, AppState.PRE_LAUNCH,
                            logger=self._logger, reason=f"run={run_id}",
                        )
                    except StateTransitionError as e:
                        self._logger.warning(
                            f"[app={self.cfg.name}] pre_launch transition "
                            f"failed: {e}"
                        )
                        self._mark_launch_done()
                        return
                    self.runtime.current_hook_total = len(hooks_to_run)

                ctx = hooks_mod.HookContext(
                    agent_base_url=f"http://127.0.0.1:{self._agent_port}",
                    cancel_event=self._cancel_event,
                    logger=self._logger,
                    app_name=self.cfg.name,
                    run_id=run_id,
                )

                for idx, hook in enumerate(hooks_to_run, start=1):
                    if self._cancel_event.is_set():
                        self._abort_prelaunch(run_id, hook.label, "cancelled")
                        return

                    with self.runtime._lock:
                        self.runtime.current_hook_index = idx
                        self.runtime.current_hook_type = hook.type
                        self.runtime.current_hook_status = "running"
                    ctx.hook_index = idx
                    ctx.hook_total = len(hooks_to_run)

                    self._writer.write_marker(
                        f"pre-launch run={run_id} hook={idx}/{len(hooks_to_run)}"
                        f" type={hook.type} name={hook.label!r} starting"
                    )

                    result = hooks_mod.run_hook(hook, ctx)

                    with self.runtime._lock:
                        self.runtime.current_hook_status = (
                            "ok" if result.ok else "failed"
                        )
                    self._writer.write_marker(
                        f"pre-launch run={run_id} hook={idx}/{len(hooks_to_run)}"
                        f" type={hook.type} "
                        f"{'ok' if result.ok else 'failed'}: {result.detail}"
                    )

                    if result.ok:
                        continue

                    if result.cancelled:
                        self._abort_prelaunch(run_id, hook.label, "cancelled")
                        return

                    if hook.effective_on_fail == "continue":
                        self._logger.warning(
                            f"[app={self.cfg.name}] hook {hook.label!r} "
                            f"failed but required=false, continuing: "
                            f"{result.detail}"
                        )
                        continue

                    # abort (required=true or on_fail=abort, or retry exhausted)
                    reason = (
                        "timeout" if result.timed_out else "pre_launch_failed"
                    )
                    self._abort_prelaunch(
                        run_id, hook.label,
                        f"{reason}: {result.detail}",
                    )
                    return

            # All hooks passed (or none declared). Spawn the process.
            self._spawn()
        finally:
            self._mark_launch_done()

    def _abort_prelaunch(self, run_id: str, hook_label: str, reason: str) -> None:
        with self.runtime._lock:
            self.runtime.last_abort = AbortRecord(
                run_id=run_id, hook=hook_label, reason=reason,
                timestamp=time.time(),
            )
            self.runtime.current_hook_index = None
            self.runtime.current_hook_total = None
            self.runtime.current_hook_type = ""
            self.runtime.current_hook_status = ""
            try:
                transition(
                    self.runtime, AppState.STOPPED,
                    logger=self._logger,
                    reason=f"pre_launch aborted: {reason}",
                )
            except StateTransitionError:
                pass
        self._writer.write_marker(
            f"pre-launch run={run_id} ABORTED hook={hook_label!r} reason={reason}"
        )

    def _mark_launch_done(self) -> None:
        with self.runtime._lock:
            self._launch_in_flight = False

    # ------------------------------------------------------------------
    # Process spawn / kill
    # ------------------------------------------------------------------

    def _spawn(self) -> None:
        """Fork the process. Caller holds no locks."""
        exe = self.cfg.exe_path
        args_list = [exe] + list(self.cfg.args)
        env = dict(os.environ)
        env.update(self.cfg.env)
        cwd = self.cfg.working_dir or None

        # Sanity: absolute path is enforced at config load, but re-check.
        if not Path(exe).is_absolute():
            self._logger.error(
                f"[app={self.cfg.name}] refusing to spawn non-absolute exe: "
                f"{exe!r}"
            )
            self._writer.write_marker(f"spawn refused: non-absolute exe {exe!r}")
            with self.runtime._lock:
                try:
                    transition(self.runtime, AppState.STOPPED,
                               logger=self._logger, reason="bad exe_path")
                except StateTransitionError:
                    pass
            return

        try:
            with self.runtime._lock:
                try:
                    transition(
                        self.runtime, AppState.STARTING,
                        logger=self._logger,
                        reason=f"spawn {Path(exe).name}",
                    )
                except StateTransitionError as e:
                    self._logger.warning(
                        f"[app={self.cfg.name}] starting transition failed: {e}"
                    )
                    return
                self.runtime.current_hook_index = None
                self.runtime.current_hook_total = None
                self.runtime.current_hook_type = ""
                self.runtime.current_hook_status = ""

            self._logger.info(
                f"[app={self.cfg.name}] spawning: {exe!r} args={list(self.cfg.args)}"
            )
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            proc = subprocess.Popen(
                args_list,
                shell=False,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            self._logger.error(
                f"[app={self.cfg.name}] spawn failed: {e}"
            )
            self._writer.write_marker(f"spawn failed: {e}")
            with self.runtime._lock:
                self.runtime.last_exit_code = -1
                try:
                    transition(self.runtime, AppState.CRASHED,
                               logger=self._logger, reason=f"spawn error: {e}")
                except StateTransitionError:
                    try:
                        transition(self.runtime, AppState.STOPPED,
                                   logger=self._logger, reason="spawn error")
                    except StateTransitionError:
                        pass
            return

        self._proc = proc
        with self.runtime._lock:
            self.runtime.pid = proc.pid
            self.runtime.last_started_at = time.time()
            self.runtime.last_exited_at = None
            self.runtime.last_exit_code = None
            self.runtime.next_restart_at = None

        # Start the drain thread for stdout.
        if proc.stdout is not None:
            self._drain_stop = threading.Event()
            self._drain_thread = logs_mod.DrainThread(
                proc.stdout, self._writer, stop_event=self._drain_stop,
            )
            self._drain_thread.start()

        self._writer.write_marker(
            f"spawned pid={proc.pid} exe={Path(exe).name}"
        )

        # Settle window → RUNNING (if still alive).
        time.sleep(_SETTLE_WINDOW_SECONDS)
        with self.runtime._lock:
            if self.runtime.state == AppState.STARTING:
                rc = proc.poll()
                if rc is None:
                    try:
                        transition(
                            self.runtime, AppState.RUNNING,
                            logger=self._logger, reason="settled",
                        )
                    except StateTransitionError:
                        pass
                else:
                    self._logger.warning(
                        f"[app={self.cfg.name}] exited during settle: rc={rc}"
                    )
                    # Let the supervisor loop handle the transition.

    def _do_kill(self, graceful: bool = True) -> None:
        """Terminate the current process if any."""
        proc = self._proc
        if proc is None:
            return

        if proc.poll() is not None:
            return  # already gone

        grace = self.cfg.kill_grace_seconds if graceful else 0

        try:
            proc.terminate()
        except Exception as e:
            self._logger.warning(
                f"[app={self.cfg.name}] terminate() failed: {e}"
            )

        if grace > 0:
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                self._logger.warning(
                    f"[app={self.cfg.name}] grace expired, forcing kill"
                )

        try:
            proc.kill()
        except Exception as e:
            self._logger.warning(
                f"[app={self.cfg.name}] kill() failed: {e}"
            )

    # ------------------------------------------------------------------
    # Supervisor loop
    # ------------------------------------------------------------------

    def _supervisor_loop(self) -> None:
        """Poll Popen.poll(), handle crashes, drive auto-restart."""
        while not self._supervisor_stop.wait(_POLL_INTERVAL_SECONDS):
            try:
                self._supervisor_tick()
            except Exception as e:
                self._logger.error(
                    f"[app={self.cfg.name}] supervisor tick error: {e}"
                )

    def _supervisor_tick(self) -> None:
        proc = self._proc

        with self.runtime._lock:
            state = self.runtime.state
            # Reset restart counter if we've been running long enough.
            if (
                state == AppState.RUNNING
                and self.runtime.last_started_at is not None
                and time.time() - self.runtime.last_started_at
                > _RESTART_RESET_WINDOW_SECONDS
                and self.runtime.restart_count > 0
            ):
                self.runtime.restart_count = 0

        if proc is None:
            return
        rc = proc.poll()
        if rc is None:
            return

        # Process has exited.
        if self._drain_stop is not None:
            self._drain_stop.set()
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2.0)
            self._drain_thread = None

        expected = rc in self.cfg.expected_exit_codes

        with self.runtime._lock:
            self.runtime.last_exit_code = rc
            self.runtime.last_exited_at = time.time()
            self.runtime.pid = None
            prev_state = self.runtime.state

            if prev_state == AppState.STOPPING:
                # Operator-initiated kill — clean landing regardless of exit.
                try:
                    transition(
                        self.runtime, AppState.STOPPED,
                        logger=self._logger,
                        reason=f"stopped rc={rc}",
                    )
                except StateTransitionError:
                    pass
                self._proc = None
                return

            if expected:
                try:
                    # Valid for RUNNING -> STOPPED, STARTING has no direct
                    # transition to STOPPED so route via CRASHED/RUNNING logic
                    # below in that edge case.
                    if prev_state == AppState.RUNNING:
                        transition(
                            self.runtime, AppState.STOPPED,
                            logger=self._logger,
                            reason=f"clean exit rc={rc}",
                        )
                        self._proc = None
                        return
                    # STARTING with expected rc is still weird — treat as crash
                    # so we surface it.
                except StateTransitionError:
                    pass

            # Crashed.
            try:
                transition(
                    self.runtime, AppState.CRASHED,
                    logger=self._logger,
                    reason=f"unexpected rc={rc}",
                )
            except StateTransitionError:
                pass
        self._writer.write_marker(f"exited rc={rc} expected={expected}")
        self._proc = None

        # Decide: auto-restart vs park vs stopped.
        if not self.cfg.auto_restart or expected:
            # No-restart path: if it was truly an expected exit we'd already
            # have returned above. This leaves us in CRASHED — stay there or
            # move to STOPPED.
            if expected:
                with self.runtime._lock:
                    try:
                        transition(self.runtime, AppState.STOPPED,
                                   logger=self._logger, reason="expected exit")
                    except StateTransitionError:
                        pass
            return

        self._schedule_restart()

    def _schedule_restart(self) -> None:
        """Compute backoff for the next restart and fire a timer."""
        backoff_schedule = self.cfg.restart_backoff_seconds or [5]
        with self.runtime._lock:
            self.runtime.restart_count += 1
            idx = min(self.runtime.restart_count - 1, len(backoff_schedule) - 1)
            wait = max(0, int(backoff_schedule[idx]))
            self.runtime.next_restart_at = time.time() + wait

            # Cooldown ceiling (spec §7): after we've hit the final backoff
            # value `crash_cooldown_seconds / final_backoff` times in a row
            # (default 2), park.
            final_backoff = backoff_schedule[-1] or 1
            park_threshold = len(backoff_schedule) + max(
                1, self.cfg.crash_cooldown_seconds // final_backoff
            )
            if self.runtime.restart_count >= park_threshold:
                try:
                    transition(
                        self.runtime, AppState.PARKED,
                        logger=self._logger,
                        reason=(
                            f"parked after {self.runtime.restart_count} "
                            f"consecutive crashes"
                        ),
                    )
                except StateTransitionError:
                    pass
                self._writer.write_marker(
                    f"parked after {self.runtime.restart_count} crashes; "
                    f"operator intervention required"
                )
                return

            try:
                transition(
                    self.runtime, AppState.RESTARTING,
                    logger=self._logger,
                    reason=f"backoff {wait}s",
                )
            except StateTransitionError:
                pass

        self._logger.info(
            f"[app={self.cfg.name}] restart scheduled in {wait}s "
            f"(attempt #{self.runtime.restart_count})"
        )

        # Wait, cancellable.
        cancelled = self._cancel_event.wait(wait)
        with self.runtime._lock:
            if cancelled or self.runtime.state != AppState.RESTARTING:
                # Operator killed between crash and restart — go to stopped.
                try:
                    transition(
                        self.runtime, AppState.STOPPED,
                        logger=self._logger, reason="restart cancelled",
                    )
                except StateTransitionError:
                    pass
                self._launch_in_flight = False
                return
            run_id = _short_uuid()
            self.runtime.run_id = run_id
            # RESTARTING -> PRE_LAUNCH or STARTING — leave to the same
            # prelaunch path; the transitions table permits RESTARTING
            # -> PRE_LAUNCH and -> STARTING.
            self._launch_in_flight = True

        self._prelaunch_thread = threading.Thread(
            target=self._prelaunch_and_start,
            name=f"prelaunch:{self.cfg.name}",
            args=(run_id, True),   # is_auto_restart=True
            daemon=True,
        )
        self._prelaunch_thread.start()


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


__all__ = ["AppSupervisor"]
