"""Continuous monitor — polls subsystems, writes status.json, fires alerts.

Designed to run in a daemon thread spawned by `agent/server.py`. Each tick:

    1. Query GPU / DD / Mosaic / QSync / EDID
    2. Run the validator against the active profile (if any)
    3. Compute deltas vs. the previous poll and emit Alert objects
    4. Write `status.json` atomically

Alert types (per spec): DD Dropout, QSync Lost, House Sync Lost,
GPU Overtemp, GPU Power Limit.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config, executors, log
from ..types import (
    Alert, DDState, GPUInfo, MosaicState, QSyncState,
    OutputHealth, Profile, ValidationResult,
)


class Monitor:
    """Background poller that owns the single `status.json` output."""

    def __init__(
        self,
        *,
        profile_name: str | None = None,
        interval: int | None = None,
        status_file: Path | str | None = None,
    ):
        agent_cfg = config.get_agent_config()
        # Effective in-memory profile name. The agent boot path resolves
        # this from CLI --profile, the active_profile.json snapshot, or
        # default.json (in that order) — see agent/server.py::serve(). All
        # we do here is record what the caller decided. set_profile()
        # only mutates this in-memory value; the snapshot file at
        # BASE_DIR/active_profile.json (managed by profiles.snapshot_active
        # / clear_active) is the boot-apply contract.
        self.profile_name = profile_name or ""
        self.interval = int(interval or agent_cfg["poll_interval_seconds"])
        self.status_file = Path(status_file or agent_cfg["status_file"])
        if not self.status_file.is_absolute():
            self.status_file = config.BASE_DIR / self.status_file

        # Boot-apply gate. Fires once per Monitor lifetime, reading the
        # active_profile.json snapshot. The snapshot is the single source
        # of truth: present → apply if validator drifts; absent → no-op.
        # The legacy nvmon.toml [agent].apply_on_boot key and .active_profile
        # sticky file are no longer consulted.
        self._apply_on_boot_done = False

        self.poll_count = 0
        self._start_time = time.monotonic()
        self._prev_dd: DDState | None = None
        self._prev_qsync: QSyncState | None = None
        self._driver_advisories: list[str] = []
        self._driver_advisories_logged = False

        self._running = False
        self._stop_event = threading.Event()
        self._last_status: dict | None = None
        # v0.9.9: set when boot-apply deferred QSync to the controller's
        # chain sync; reported in /status as `chain_sync`.
        self._chain_sync: dict | None = None
        # v0.9.10: fix hints (agent/remedies.py), re-evaluated every 30 s.
        from .remedies import Tracker as _RemedyTracker
        self._remedy_tracker = _RemedyTracker()
        self._remedies: list[dict] = []
        self._remedies_at = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_thread(self) -> threading.Thread:
        """Spawn a daemon thread running `run()`. Returns the thread."""
        t = threading.Thread(
            target=self.run, name="nvmon-monitor", daemon=True,
        )
        t.start()
        return t

    def run(self) -> None:
        """Blocking poll loop. Stops when `stop()` is called."""
        from . import profiles as profiles_mod

        logger = log.get()
        logger.info(
            f"Monitor starting (interval={self.interval}s, "
            f"profile={self.profile_name or 'none'})"
        )
        if profiles_mod.active_profile_path().is_file():
            logger.info(
                f"Monitor: boot-apply armed — {profiles_mod.ACTIVE_PROFILE_FILE} "
                f"present (idempotent — applies only if validator reports drift)"
            )
        else:
            logger.info(
                f"Monitor: no {profiles_mod.ACTIVE_PROFILE_FILE} snapshot — "
                f"boot-apply disabled until next /apply"
            )
        self._running = True
        self._stop_event.clear()

        # Driver readiness gate. The Scheduled Task fires at logon, often
        # before the display driver has finished enumerating GPUs/displays;
        # polling then produces an empty GPU list, the validator fails on
        # "No GPU data", and boot-apply would push a profile at a driver
        # that isn't up yet. Wait (bounded) for nvidia-smi to answer first.
        try:
            self._wait_for_driver()
        except Exception as exc:  # noqa: BLE001 — never block boot on the gate
            logger.warning(f"driver readiness gate failed: {exc}")

        try:
            while self._running and not self._stop_event.is_set():
                try:
                    data = self.poll_once()
                    # First successful poll has subsystem capability + validation
                    # results in `data`. If a snapshot is on disk, decide
                    # now whether to apply (state diverged) or skip (already
                    # passing). Runs once per agent process lifetime.
                    if not self._apply_on_boot_done and isinstance(data, dict):
                        self._maybe_apply_on_boot(data)
                except Exception as exc:
                    logger.error(f"Monitor poll failed: {exc}", exc_info=True)

                # Interruptible sleep
                if self._stop_event.wait(self.interval):
                    break
        except KeyboardInterrupt:
            logger.info("Monitor interrupted")
        finally:
            self._running = False
            logger.info("Monitor stopped")

    def _wait_for_driver(self) -> bool:
        """Block until nvidia-smi reports at least one GPU, or the timeout.

        Returns True when the driver answered. Skips instantly when
        nvidia-smi isn't installed (dev box) so non-GPU hosts aren't
        delayed. Interruptible by stop().
        """
        from . import gpu as gpu_mod

        logger = log.get()
        if not gpu_mod.capability().get("available"):
            return False
        deadline = time.monotonic() + config.DRIVER_READY_TIMEOUT
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            try:
                if gpu_mod.status():
                    if attempt > 1:
                        logger.info(f"NVIDIA driver ready after {attempt} attempt(s)")
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"driver readiness probe failed: {exc}")
            if time.monotonic() >= deadline:
                logger.warning(
                    f"NVIDIA driver did not answer within {config.DRIVER_READY_TIMEOUT}s "
                    f"— continuing; boot-apply may fail until the driver is up"
                )
                return False
            logger.info(
                f"Waiting for NVIDIA driver (nvidia-smi returned no GPUs, attempt {attempt})..."
            )
            if self._stop_event.wait(config.DRIVER_READY_POLL):
                return False
        return False

    def _maybe_apply_on_boot(self, status: dict) -> None:
        """Run apply_active() once on boot, gated by validator divergence.

        Skip rules (in order):
          - already fired this session → skip.
          - no active_profile.json snapshot → skip silently (this is a
            fresh / restored node, nothing to re-apply).
          - validator reports all_passed=True → state already matches the
            profile, skip the apply (the whole point of this feature).
          - admin not available → can't apply anyway, log + skip.
        Otherwise spawn a background thread that calls profiles.apply_active().
        """
        # Mark done immediately — we either apply or skip, but only one
        # decision per session. Any failure path below also falls through
        # to "done" (we don't retry on every tick).
        self._apply_on_boot_done = True

        from . import profiles as profiles_mod
        logger = log.get()

        if not profiles_mod.active_profile_path().is_file():
            # Fresh node, or last action was /restore — nothing to apply.
            return

        snap_name = profiles_mod.read_active_name() or "(active)"

        # Boot vs listener restart. The Scheduled Task starts the listener
        # at logon, but "Restart listener" (code pickup) and a supervisor
        # respawn start it too — hours into the day, with the driver still
        # holding whatever the last apply set. Replaying the profile then
        # is churn at best (site 2026-11-01: a fleet-wide listener restart
        # re-applied seven profiles against a house feed that had moved).
        # Only a young system counts as a boot.
        from . import system as system_mod
        try:
            window_min = int(config.get_agent_config().get("boot_apply_max_uptime_minutes", 15) or 0)
        except (TypeError, ValueError):
            window_min = 15
        if window_min > 0:
            uptime = system_mod.system_uptime_seconds()
            if uptime is not None and uptime > window_min * 60:
                logger.info(
                    f"apply_on_boot: Windows has been up {uptime / 60:.0f} min "
                    f"(> {window_min}) — listener restart, not a boot; leaving "
                    f"'{snap_name}' alone (use Apply / Re-apply All to push it)"
                )
                return

        if not config.is_admin():
            logger.warning(
                "apply_on_boot: agent is not admin — cannot apply '%s', skipping",
                snap_name,
            )
            return

        # Frame-lock chain member? (v0.9.9) Only when the profile uses QSync
        # as server or client AND the controller says this node's group is a
        # chain (one server + clients). Then boot-apply sets timing/colour,
        # leaves QSync off and the controller starts the chain server-first.
        chain = _chain_membership(profiles_mod, logger)

        validation = (status or {}).get("validation") or {}
        all_passed = bool(validation.get("all_passed"))
        if all_passed:
            logger.info(
                f"apply_on_boot: '{snap_name}' — validator already passes "
                f"(state matches profile), skipping apply"
            )
            return

        if chain:
            self._chain_sync = {"pending": True, "group": chain.get("group"), "role": chain.get("role"),
                                "server": chain.get("server"), "since": time.time()}
            logger.warning(
                f"apply_on_boot: chain member ({chain.get('role')} in '{chain.get('group')}', server "
                f"{chain.get('server')}) — applying '{snap_name}' without QSync; the controller starts "
                f"the chain server-first once every member is up"
            )

            def _bg_chain() -> None:
                try:
                    ok, msg = profiles_mod.apply_active(skip={"qsync_enable"})
                    logger.info(f"apply_on_boot: '{snap_name}' {'applied' if ok else 'apply failed'} "
                                f"(QSync deferred to chain sync) — {msg}")
                    if ok:
                        self.set_profile(snap_name)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"apply_on_boot: apply crashed — {exc}", exc_info=True)
                # Fallback: controller down or the chain never started.
                if self._stop_event.wait(CHAIN_SYNC_FALLBACK_S):
                    return
                if (self._chain_sync or {}).get("pending"):
                    logger.warning(f"apply_on_boot: chain sync did not lock this node within "
                                   f"{CHAIN_SYNC_FALLBACK_S / 60:.0f} min — syncing on its own")
                    ok2, msg2 = _boot_sync_retries(profiles_mod, logger, "QSync not locked (chain sync fallback)")
                    (logger.info if ok2 else logger.error)(f"apply_on_boot: chain fallback — {msg2}")
                    if ok2:
                        self._chain_sync = {**(self._chain_sync or {}), "pending": False, "by": "fallback"}

            threading.Thread(target=_bg_chain, daemon=True, name="nvmon-apply-on-boot").start()
            return

        # Divergent state → apply in a background thread so the monitor
        # loop continues writing status.json. apply_active() loads the
        # snapshot (profile dict + inline EDID bytes) and runs the same
        # pipeline as /apply.
        def _bg() -> None:
            try:
                logger.warning(
                    f"apply_on_boot: state diverges from '{snap_name}' — applying now"
                )
                ok, msg = profiles_mod.apply_active()
                if not ok and "QSync not locked" in (msg or ""):
                    # Whole-rig power-on (site 2026-11-04 cold boot): all seven
                    # nodes ran boot-apply within ~30 s while every sync card
                    # in the daisy chains was still coming up. Colour and
                    # timing landed; the single sync attempt did not. Give the
                    # chain time to settle and retry the sync step only.
                    ok, msg = _boot_sync_retries(profiles_mod, logger, msg)
                if ok:
                    self.set_profile(snap_name)
                    logger.info(f"apply_on_boot: '{snap_name}' applied successfully")
                else:
                    logger.error(f"apply_on_boot: apply failed — {msg}")
            except Exception as exc:
                logger.error(f"apply_on_boot: apply crashed — {exc}", exc_info=True)

        threading.Thread(
            target=_bg, daemon=True, name="nvmon-apply-on-boot",
        ).start()

    def stop(self) -> None:
        """Signal the poll loop to exit."""
        self._running = False
        self._stop_event.set()

    def is_running(self) -> bool:
        return self._running

    def set_profile(self, profile_name: str) -> None:
        """Change the validation profile mid-flight (in-memory only).

        Cross-restart persistence is handled by the active_profile.json
        snapshot, written by profiles.snapshot_active() on /apply and
        cleared by profiles.clear_active() on /restore. This setter is
        the in-memory companion: /status.active_profile reflects it
        immediately, and the validator runs against this name on the
        next poll.
        """
        self.profile_name = profile_name or ""
        log.get().info(f"Monitor: validation profile set to '{self.profile_name}'")

    def last_status(self) -> dict | None:
        """Return the most recently produced status dict (in-memory cache)."""
        return self._last_status

    # ------------------------------------------------------------------
    # Core poll
    # ------------------------------------------------------------------

    def poll_once(self) -> dict:
        """Run a single poll cycle and return the status dict."""
        from . import displays as displays_mod
        from . import edid as edid_mod
        from . import gpu as gpu_mod
        from . import mosaic as mosaic_mod
        from . import profiles as profiles_mod
        from . import qsync as qsync_mod
        from . import validator

        logger = log.get()

        # An apply / restore / boot-apply owns the NVIDIA tools: don't queue
        # our queries behind it (status would lag a minute) or overlap its
        # QSync enable (configureQsync2 hangs for 60s when queryAll runs at
        # the same time — the legacy tool d8f876f). Keep the last status until
        # the job is done; /status reports status_age_s so the dashboard
        # can show "stale Ns" instead of a frozen reading.
        if executors.job_active() and self._last_status is not None:
            return self._last_status

        self.poll_count += 1
        uptime = int(time.monotonic() - self._start_time)

        # Query subsystems — each handler returns a typed empty state on failure.
        try:
            dd_state = displays_mod.status()
        except Exception as exc:
            logger.error(f"DD query failed: {exc}")
            dd_state = DDState()

        # Windows' own view of each path: desktop size vs signal on the wire.
        # This is where "Mosaic says 3840x2160@23.976 but the wall is being
        # sent 10240x5760@60" shows up; the validator's output_signal check
        # reads it. Cheap (one user32 call), never raises.
        try:
            from . import ccd
            dd_state.signals = ccd.describe()
        except Exception as exc:
            logger.debug(f"ccd.describe failed: {exc}")

        try:
            mosaic_state = mosaic_mod.status()
        except Exception as exc:
            logger.error(f"Mosaic query failed: {exc}")
            mosaic_state = MosaicState()

        try:
            qsync_state = qsync_mod.status()
        except Exception as exc:
            logger.error(f"QSync query failed: {exc}")
            qsync_state = QSyncState()

        try:
            gpu_list = gpu_mod.status()
        except Exception as exc:
            logger.error(f"GPU query failed: {exc}")
            gpu_list = []

        try:
            edid_snapshot = edid_mod.status()
        except Exception as exc:
            logger.debug(f"EDID query failed: {exc}")
            edid_snapshot = {"available": False, "registry_overrides": {}, "edid_files": []}

        # Load profile for validation (quietly skip if missing)
        profile: Profile | None = None
        if self.profile_name:
            try:
                profile = profiles_mod.load(self.profile_name)
            except FileNotFoundError:
                logger.debug(f"Monitor: profile '{self.profile_name}' not found")
            except Exception as exc:
                logger.warning(f"Monitor: profile load failed: {exc}")

        # Run validation
        validation = validator.validate_all(
            dd=dd_state, mosaic=mosaic_state, qsync=qsync_state,
            gpus=gpu_list, profile=profile,
        )

        # Generate alerts based on deltas
        alerts = self._check_alerts(dd_state, mosaic_state, qsync_state, gpu_list)

        # Collect errors
        errors: list[str] = []
        if not dd_state.displays and displays_mod.is_available():
            errors.append("No DD display data")

        # The qsync subsystem flags privilege issues here so the dashboard
        # can show "AGENT NOT ADMIN" instead of misleadingly saying UNLOCKED
        # when the actual problem is that configureQsync2.exe refused to run.
        if getattr(qsync_mod, "needs_admin_detected", False):
            errors.append(
                "QSync: configureQsync2.exe needs admin privileges — restart "
                "the agent in an elevated shell"
            )

        # Driver-version + hardware advisories (e.g. Blackwell + sync FW
        # v3.04/v3.06 timing-server bug). Computed once per process from the
        # first GPU read that returns data, then carried on every poll. We
        # also read the live sync-card firmware so the advisory can confirm
        # "this node IS on v3.06" or suppress itself when the FW is clean.
        # framelock.status() is cheap (NVCP topology JSON) → expensive
        # (PowerShell NVWMI fallback), but only fires once per process.
        # Logged once at WARNING so it shows up in the agent log even when
        # nobody is looking at /status.
        advisories_enabled = bool(config.get_agent_config().get("driver_advisories", True))
        if not advisories_enabled:
            self._driver_advisories = []
            self._driver_advisories_logged = True
        if not self._driver_advisories_logged and gpu_list:
            from . import driver_info, framelock as framelock_mod
            sync_fw = ""
            try:
                sync_fw = framelock_mod.status().sync_card_firmware
            except Exception as exc:
                logger.debug(f"framelock.status (advisory probe) failed: {exc}")
            if not sync_fw:
                # Live read unavailable (site: neither NVCP topology JSON nor
                # NVWMI exposed it) — fall back to the operator-declared
                # [agent].sync_card_firmware so the advisory can still say
                # "CONFIRMED on v3.06" or stay silent on a fixed firmware.
                sync_fw = str(config.get_agent_config().get("sync_card_firmware") or "")
                if sync_fw:
                    logger.info(f"sync card firmware from nvmon.toml: v{sync_fw}")
            self._driver_advisories = driver_info.check_known_issues(
                gpu_list, sync_card_firmware=sync_fw,
            )
            for adv in self._driver_advisories:
                logger.warning(f"DRIVER ADVISORY: {adv}")
            self._driver_advisories_logged = True
        # Advisories ride in their own list, NOT in `errors`. They are
        # informational (known driver/firmware interactions), and counting
        # them as agent errors turned every Blackwell node's "Errors: 1"
        # red and put a paragraph of forum text in the slide-over.

        # Build the payload and persist it
        data = _build_status_dict(
            dd=dd_state, mosaic=mosaic_state, qsync=qsync_state, gpus=gpu_list,
            edid=edid_snapshot, profile_name=self.profile_name,
            uptime_seconds=uptime, poll_count=self.poll_count,
            validation=validation, alerts=alerts, errors=errors,
            advisories=list(self._driver_advisories),
        )

        if self._chain_sync is not None:
            cs = self._chain_sync
            if cs.get("pending") and _sync_locked(qsync_state, str(cs.get("role") or "client")):
                cs = {**cs, "pending": False, "locked_at": time.time()}
                self._chain_sync = cs
                logger.info(f"chain sync: locked ({cs.get('role')} in '{cs.get('group')}')")
            data["chain_sync"] = dict(cs)

        # Fix hints: soft known-case suggestions (restart / reseat / re-apply).
        # Not while an apply owns the tools (we only get here when none does),
        # and at most every 30 s — NVAPI reads per display.
        if time.monotonic() - self._remedies_at >= 30.0:
            self._remedies_at = time.monotonic()
            try:
                from . import remedies as remedies_mod
                self._remedies = remedies_mod.collect(dd_state, qsync_state, self._remedy_tracker)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"fix hints failed: {exc}")
        data["remedies"] = list(self._remedies)

        self._last_status = data

        try:
            _atomic_write_json(self.status_file, data)
        except Exception as exc:
            logger.error(f"Failed to write status: {exc}")

        # Log alerts
        for alert in alerts:
            if alert.severity == "CRITICAL":
                logger.critical(f"ALERT [{alert.type}] {alert.message}")
            else:
                logger.warning(f"ALERT [{alert.type}] {alert.message}")

        # Stash deltas for next poll
        self._prev_dd = dd_state
        self._prev_qsync = qsync_state

        return data

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def _check_alerts(
        self,
        dd: DDState,
        mosaic: MosaicState,
        qsync: QSyncState,
        gpus: list[GPUInfo],
    ) -> list[Alert]:
        """Produce the alert list per spec."""
        alerts: list[Alert] = []
        now = datetime.now(timezone.utc).isoformat()

        agent_cfg = config.get_agent_config()
        temp_warning = agent_cfg["gpu"]["temp_warning_c"]
        power_warning_pct = agent_cfg["gpu"]["power_warning_pct"]

        # DD Dropout — previously active, now not
        if self._prev_dd and self._prev_dd.active and not dd.active:
            non_dd = [
                d for d in dd.displays
                if "removed" not in (d.status or "").lower()
            ]
            alerts.append(Alert(
                severity="CRITICAL",
                type="DD Dropout",
                message=f"{len(non_dd)} display(s) left DD mode",
                timestamp=now,
            ))

        # QSync Lost
        if self._prev_qsync and self._prev_qsync.synced and not qsync.synced:
            alerts.append(Alert(
                severity="CRITICAL",
                type="QSync Lost",
                message="QSync lock lost",
                timestamp=now,
            ))

        # House Sync Lost
        if (
            self._prev_qsync and self._prev_qsync.house_sync_detected
            and not qsync.house_sync_detected
        ):
            alerts.append(Alert(
                severity="CRITICAL",
                type="House Sync Lost",
                message="House sync signal lost",
                timestamp=now,
            ))

        # GPU Overtemp
        for g in gpus:
            if g.temperature_c > temp_warning:
                alerts.append(Alert(
                    severity="WARNING",
                    type="GPU Overtemp",
                    message=(
                        f"GPU{g.index} at {g.temperature_c}C "
                        f"(threshold {temp_warning}C)"
                    ),
                    timestamp=now,
                ))

        # GPU Power Limit
        for g in gpus:
            if g.power_limit_w > 0:
                pct = (g.power_draw_w / g.power_limit_w) * 100
                if pct > power_warning_pct:
                    alerts.append(Alert(
                        severity="WARNING",
                        type="GPU Power Limit",
                        message=f"GPU{g.index} at {pct:.0f}% of power limit",
                        timestamp=now,
                    ))

        return alerts


# ---------------------------------------------------------------------------
# status.json construction + atomic write
# ---------------------------------------------------------------------------

def _build_status_dict(
    *,
    dd: DDState,
    mosaic: MosaicState,
    qsync: QSyncState,
    gpus: list[GPUInfo],
    edid: dict,
    profile_name: str,
    uptime_seconds: int,
    poll_count: int,
    validation: ValidationResult | None,
    alerts: list[Alert],
    errors: list[str],
    advisories: list[str] | None = None,
) -> dict:
    """Build the canonical status dict. Shape mirrors nvdd's telemetry minus
    legacy-tool-only fields (backup_status, peer references)."""

    health = _build_output_health(dd, mosaic=mosaic, qsync=qsync)

    data: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": config.get_hostname(),
        "ip": config.get_local_ip(),
        "active_profile": profile_name,
        "uptime_seconds": uptime_seconds,
        "poll_count": poll_count,
        # Informational driver / firmware advisories (v0.8.2). Separate
        # from `errors` so they never count against the node.
        "advisories": list(advisories or []),

        "direct_display": {
            "active": dd.active,
            "display_count": dd.display_count,
            "displays": [
                {
                    "id": d.id,
                    "name": d.name,
                    "status": d.status,
                    "refresh_rate": d.refresh_rate,
                    "refresh_fraction": d.refresh_fraction,
                    "resolution": list(d.resolution),
                    "connector": d.connector,
                    "bits_per_channel": d.bits_per_channel,
                    "color_space": d.color_space,
                    "pixel_encoding": d.pixel_encoding,
                    "gpu_index": d.gpu_index,
                    "display_index": d.display_index,
                    "os_target_id": d.os_target_id,
                }
                for d in dd.displays
            ],
            # Windows' CCD view per active path (v0.8.0): desktop size
            # (source) vs signal on the wire (target). Differs when Windows
            # is scaling the desktop — see validation.checks.output_signal.
            "signals": [
                {
                    "name": str(s.get("name") or ""),
                    "source": list(s["source"]) if s.get("source") else None,
                    "position": list(s["position"]) if s.get("position") else None,
                    "target": list(s["target"]) if s.get("target") else None,
                    "hz": float(s.get("hz") or 0.0),
                }
                for s in (dd.signals or [])
            ],
        },

        "mosaic": {
            "enabled": mosaic.enabled,
            "rows": mosaic.rows,
            "cols": mosaic.cols,
            "per_display_resolution": list(mosaic.per_display_resolution),
            "total_resolution": list(mosaic.total_resolution),
            "refresh_rate": mosaic.refresh_rate,
            "output_count": mosaic.output_count,
            "synchronized": mosaic.synchronized,
            "outputs": [
                {"gpu": o.gpu, "display": o.display, "display_id": o.display_id}
                for o in mosaic.outputs
            ],
        },

        "qsync": {
            "available": qsync.available,
            "synced": qsync.synced,
            "house_sync_detected": qsync.house_sync_detected,
            "house_sync_incoming_hz": qsync.house_sync_incoming_hz,
            "source": qsync.source,
            "trigger": qsync.trigger,
            # Computed fields the dashboard's _qsync_view branches on. Without
            # `role` here, a daisy-chain client gets misclassified as
            # "locked-to-self" because source=VSYNC at the client.
            "role": qsync.role,
            "lock_source_label": qsync.lock_source_label,
            "effective_lock_hz": qsync.effective_lock_hz,
            "per_gpu_sync": [
                {
                    "gpu": g.gpu,
                    "synced": g.effective_synced,
                    "raw_synced": g.synced,
                    "signal_available": g.signal_available,
                    "sync_note": g.sync_note,
                    "sync_state": g.sync_state,
                }
                for g in qsync.per_gpu_sync
            ],
        },

        "gpu": [
            {
                "index": g.index,
                "name": g.name,
                "pci_bus_id": g.pci_bus_id,
                "driver_version": g.driver_version,
                "temperature_c": g.temperature_c,
                "power_draw_w": g.power_draw_w,
                "power_limit_w": g.power_limit_w,
                "gpu_utilization_pct": g.gpu_utilization_pct,
                "memory_used_mb": g.memory_used_mb,
                "memory_total_mb": g.memory_total_mb,
                "gpu_clock_mhz": g.gpu_clock_mhz,
                "mem_clock_mhz": g.mem_clock_mhz,
                "video_clock_mhz": g.video_clock_mhz,
                "pstate": g.pstate,
                "display_active": g.display_active,
                "fan_speed_pct": g.fan_speed_pct,
                "pcie_gen": g.pcie_gen,
                "pcie_width": g.pcie_width,
            }
            for g in gpus
        ],

        "edid": edid,

        "output_health": {
            "total_displays": health.total_displays,
            "dd_active_count": health.dd_active_count,
            "dd_expected_count": health.dd_expected_count,
            "all_same_refresh": health.all_same_refresh,
            "all_same_resolution": health.all_same_resolution,
            "refresh_rate_hz": health.refresh_rate_hz,
        },
    }

    if validation:
        data["validation"] = {
            "all_passed": validation.all_passed,
            "checks": {
                k: {"passed": v.passed, "detail": v.detail}
                for k, v in validation.checks.items()
            },
        }

    data["alerts"] = [
        {
            "severity": a.severity, "type": a.type,
            "message": a.message, "timestamp": a.timestamp,
        }
        for a in alerts
    ]
    data["errors"] = errors

    return data


def _build_output_health(
    dd: DDState,
    *,
    mosaic: MosaicState | None = None,
    qsync: QSyncState | None = None,
) -> OutputHealth:
    """Compute output health summary, using Mosaic/QSync rate as fallback
    when DD displays are in 'Removed' mode (which reports rate=0)."""
    managed = [
        d for d in dd.displays
        if not config.is_excluded_display(d.name, d.resolution)
    ]

    health = OutputHealth()
    health.total_displays = len(managed)
    health.dd_active_count = sum(
        1 for d in managed if "removed" in (d.status or "").lower()
    )
    health.dd_expected_count = 4  # default assumption for 2x2

    rates = [d.refresh_rate for d in managed if d.refresh_rate > 0]
    resolutions = [d.resolution for d in managed if d.resolution != (0, 0)]

    if rates:
        health.refresh_rate_hz = rates[0]
        health.all_same_refresh = all(abs(r - rates[0]) < 0.01 for r in rates)
    elif mosaic and mosaic.refresh_rate > 0:
        health.refresh_rate_hz = mosaic.refresh_rate
        health.all_same_refresh = True
    elif qsync and qsync.house_sync_incoming_hz > 0:
        health.refresh_rate_hz = qsync.house_sync_incoming_hz
        health.all_same_refresh = True

    if resolutions:
        health.all_same_resolution = all(r == resolutions[0] for r in resolutions)

    return health


BOOT_SYNC_RETRIES = 4
BOOT_SYNC_RETRY_DELAY_S = 45.0
CHAIN_SYNC_FALLBACK_S = 480.0
CHAIN_QUERY_TRIES = 6
CHAIN_QUERY_GAP_S = 10.0


def _chain_membership(profiles_mod, logger) -> dict | None:
    """``{group, role, server, clients}`` when this node is in a frame-lock
    chain per the controller, else None. Only asked when the active profile
    uses QSync as server or client and a controller_url is configured; an
    unreachable controller means "not in a chain" (old boot behaviour)."""
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur

    loaded = profiles_mod.load_active()
    if not loaded:
        return None
    profile = loaded[0]
    role = (getattr(profile.qsync, "role", "") or "").lower()
    if not profile.qsync.enabled or role not in ("server", "client"):
        return None
    base = str((config.get_agent_config() or {}).get("controller_url") or "").rstrip("/")
    if not base:
        return None
    host = config.get_hostname()
    url = f"{base}/sync_chain?host={_up.quote(host)}"
    for attempt in range(CHAIN_QUERY_TRIES):
        try:
            with _ur.urlopen(url, timeout=5) as r:
                data = _json.loads(r.read().decode("utf-8", "replace") or "{}")
            if isinstance(data, dict) and data.get("in_chain"):
                return data
            return None
        except Exception as exc:  # noqa: BLE001 — controller may still be booting too
            logger.info(f"apply_on_boot: chain query to {base} failed ({exc}); "
                        f"retry {attempt + 1}/{CHAIN_QUERY_TRIES}")
            time.sleep(CHAIN_QUERY_GAP_S)
    logger.warning("apply_on_boot: controller not reachable — using this node's own boot sync")
    return None


def _boot_sync_retries(profiles_mod, logger, first_msg: str) -> tuple[bool, str]:
    """Retry only the QSync step of a boot-apply, spaced out so the daisy
    chain can settle after a whole-rig power-on. Servers and clients both
    retry; a client locks as soon as its server does. Returns (ok, msg)."""
    from . import qsync as qsync_mod

    loaded = profiles_mod.load_active()
    if not loaded:
        return False, first_msg
    profile = loaded[0]
    if not profile.qsync.enabled:
        return False, first_msg
    role = (getattr(profile.qsync, "role", "server") or "server").lower()
    msg = first_msg
    for attempt in range(1, BOOT_SYNC_RETRIES + 1):
        logger.warning(f"apply_on_boot: QSync not locked — retry {attempt}/{BOOT_SYNC_RETRIES} "
                       f"in {BOOT_SYNC_RETRY_DELAY_S:g}s (sync chain may still be coming up)")
        executors.sleep(BOOT_SYNC_RETRY_DELAY_S, "boot sync retry")
        try:
            st = qsync_mod.status()
            if _sync_locked(st, role):
                return True, f"Profile '{profile.name}' applied (QSync locked after boot retry {attempt})"
            qsync_mod.enable_from_profile(profile)
            st = qsync_mod.status()
        except executors.Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"boot sync retry {attempt} raised: {exc}"
            continue
        if _sync_locked(st, role):
            return True, f"Profile '{profile.name}' applied (QSync locked after boot retry {attempt})"
        msg = f"{first_msg} — still not locked after {attempt} boot retr{'y' if attempt == 1 else 'ies'}"
    if role == "server":
        msg += " — if house sync is present and no GPU becomes SERVER, restart Windows on this node"
    return False, msg


def _sync_locked(st, role: str) -> bool:
    """Same lock criterion as the apply pipeline: synced, or (client) a GPU says synced."""
    if st is None:
        return False
    if getattr(st, "synced", False):
        return True
    return role == "client" and any(getattr(g, "synced", False) for g in (getattr(st, "per_gpu_sync", None) or []))


# Re-export from the shared util module so any code that has historically
# imported `_atomic_write_json` from monitor (status.json writes, tests)
# keeps working. New callers should import from nvmon.util directly.
from ..util import atomic_write_json as _atomic_write_json  # noqa: E402
