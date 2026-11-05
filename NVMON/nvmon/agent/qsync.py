"""QSync (Quadro Sync) operations — query, enable, disable.

Wraps `configureQsync2.exe`. Missing exe is handled gracefully: `status()`
returns an empty `QSyncState`, control calls return a failed `CommandResult`
without raising.
"""

from __future__ import annotations

import time

from .. import config, executors, log
from ..parsers import (
    parse_qsync_displays,
    parse_qsync_query_all,
    parse_qsync_query_params,
)
from ..types import CommandResult, QSyncState


def capability() -> dict:
    """Report whether the QSync subsystem is usable.

    Returns a dict for `/health.capabilities.qsync`.
    """
    exe = config.get_qsync_exe()
    if not exe:
        return {
            "available": False,
            "exe": None,
            "reason": config.missing_exe_reason("configureQsync2.exe"),
        }
    return {"available": True, "exe": "configureQsync2.exe"}


def is_available() -> bool:
    return bool(config.get_qsync_exe())


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

# Diagnostic flag set by status() when the exe complains about privileges.
# Read by monitor.py to surface the warning in the agent's /status errors[].
needs_admin_detected: bool = False
_admin_warning_logged = False


def status(*, dry_run: bool = False) -> QSyncState:
    """Query QSync status via `queryAll`.

    Returns an empty QSyncState if the exe is missing or the query fails.
    """
    global needs_admin_detected, _admin_warning_logged
    logger = log.get()

    # INTEGRATION_POINT: backends — dispatch through a backend registry when
    # NVAPI direct-call support lands.
    exe = config.get_qsync_exe()
    if not exe:
        logger.debug("qsync.status: configureQsync2.exe not found — returning empty state")
        return QSyncState()

    result = executors.run(
        exe, ["queryAll"],
        label="qsync-queryAll", dry_run=dry_run, quiet=True,
    )
    if dry_run:
        return QSyncState()

    # Detect the "needs admin privileges" path. configureQsync2.exe exits 0
    # even on this error (its stdout looks superficially like normal output),
    # so the parser would otherwise just fall through to an empty state and
    # the dashboard would say UNLOCKED. We promote it to a hard warning that
    # the monitor copies into /status.errors so the dashboard can show the
    # operator exactly what's wrong.
    combined = (result.stdout or "") + " " + (result.stderr or "")
    if "needs admin" in combined.lower():
        needs_admin_detected = True
        if not _admin_warning_logged:
            logger.error(
                "QSync: configureQsync2.exe reports 'needs admin privileges'. "
                "Restart the agent in an elevated shell, or run install.bat --listener "
                "as Administrator. Until then QSync state will read as UNLOCKED."
            )
            _admin_warning_logged = True
        # Return empty state — the parser would produce one anyway since
        # the actual data lines aren't in the output.
        return QSyncState()
    needs_admin_detected = False

    try:
        return parse_qsync_query_all(result.stdout + "\n" + result.stderr)
    except Exception as exc:
        logger.error(f"qsync queryAll parse failed: {exc}")
        return QSyncState()


def apply_rate_check(state: QSyncState, display_rate_hz: float) -> QSyncState:
    """Downgrade ``synced`` when the displays cannot actually be locked.

    The parser calls a HOUSESYNC server "synced" whenever a signal is
    present (configureQsync2 reports "Is synced: NO" even for a real lock
    in server mode). That also passes a 23.976 Hz wall sitting on a 60 Hz
    house signal — physically impossible to lock, and exactly what the
    dashboard showed as green during the legacy tool 60 Hz switch tests.
    Require the display rate to match the incoming rate within
    ``config.RATE_MATCH_TOLERANCE_HZ``. Mutates and returns ``state``.
    """
    if (
        state.synced
        and state.house_sync_detected
        and state.house_sync_incoming_hz > 0
        and display_rate_hz > 0
        and abs(display_rate_hz - state.house_sync_incoming_hz) > config.RATE_MATCH_TOLERANCE_HZ
    ):
        note = (
            f"rate mismatch: displays {display_rate_hz:g}Hz vs house "
            f"{state.house_sync_incoming_hz:.3f}Hz"
        )
        state.synced = False
        state.lock_source_label = ""
        for g in state.per_gpu_sync:
            g.effective_synced = False
            g.sync_note = note
    return state


def house_sync_matches(profile) -> tuple[bool, str]:
    """Is the incoming house sync at the rate this profile expects?

    A QSync lock is impossible when the house clock runs at a different
    rate than the profile (e.g. generator at 30, profile 60), so the apply
    pipeline fails fast instead of polling for a lock that can't happen.
    No house sync detected, no expectation, or client role → treated as a
    match (nothing to compare).
    """
    q = profile.qsync
    expected = float(getattr(profile.validation, "expected_house_sync_freq", 0.0) or 0.0)
    role = (getattr(q, "role", "server") or "server").lower()
    if not q.enabled or expected <= 0 or role == "client":
        return True, ""
    try:
        state = status()
    except Exception:
        return True, ""
    if not state.house_sync_detected or state.house_sync_incoming_hz <= 0:
        return True, ""
    tolerance = config.rate_tolerance(getattr(profile.validation, "refresh_rate_tolerance", None))
    if abs(state.house_sync_incoming_hz - expected) <= tolerance:
        return True, ""
    return False, (
        f"house sync is {state.house_sync_incoming_hz:.3f}Hz, profile wants {expected:g}Hz "
        f"— waiting for the sync generator to match"
    )


def query_params(*, dry_run: bool = False) -> dict:
    """Query QSync control parameters."""
    exe = config.get_qsync_exe()
    if not exe:
        return {}

    result = executors.run(
        exe, ["queryParams"],
        label="qsync-queryParams", dry_run=dry_run, quiet=True,
    )
    if dry_run:
        return {}

    try:
        return parse_qsync_query_params(result.stdout + "\n" + result.stderr)
    except Exception as exc:
        log.get().debug(f"qsync queryParams parse failed: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Control: enable / disable
# ---------------------------------------------------------------------------

def _discover_displays(*, dry_run: bool = False) -> list[tuple[int, int]]:
    """Run queryAll and parse out every (gpu_index, display_index) tuple.

    This is how the default sync path discovers what to lock to HOUSESYNC
    when the operator hasn't passed an explicit display list or grid index.
    Returns [] on failure — callers fall back to the configureQsync2 default
    of display=0,0.
    """
    exe = config.get_qsync_exe()
    if not exe:
        return []
    if dry_run:
        return []
    result = executors.run(
        exe, ["queryAll"],
        label="qsync-discover-displays", dry_run=False, quiet=True,
    )
    try:
        return parse_qsync_displays(result.stdout)
    except Exception as exc:
        log.get().debug(f"qsync display discovery parse failed: {exc}")
        return []


def enable(
    *,
    source: str = "HOUSESYNC",
    trigger: str = "FALLING",
    grid: int = -1,
    displays: list[tuple[int, int]] | None = None,
    role: str = "server",
    wait: bool = True,
    dry_run: bool = False,
) -> CommandResult:
    """Enable QSync.

    Default behavior (no `grid`, no `displays`): discover every connected
    display via queryAll and pass `display=G,D` for each. The first one
    becomes the server, the rest become clients — that's the non-Mosaic
    "sync everything to HOUSESYNC" path.

    Args:
        source: Sync source ("HOUSESYNC" or "INTERNAL"). Ignored when role="client".
        trigger: Edge to lock to ("FALLING" or "RISING"). Ignored when role="client".
        grid: Mosaic grid index. Default -1 means "no grid; auto-discover".
              Pass `>= 0` only when the caller knows a Mosaic grid exists
              (configureQsync2 silently no-ops if the grid index doesn't
              exist on the host — which is why this used to fail for
              non-Mosaic setups).
        displays: Explicit list of `(gpu_index, display_index)` pairs.
                  When provided, takes precedence over both `grid` and
                  auto-discovery. The first pair is the server (or the
                  primary client display in role="client" mode).
        role: "server" (today's path — this node owns the timing) or
              "client" (this node is daisy-chained downstream of another
              QSync server; configureQsync2 takes `enable client ...` and
              source/trigger are owned upstream).
        wait: If True, poll until sync establishes or timeout.
    """
    exe = config.get_qsync_exe()
    if not exe:
        return CommandResult(
            success=False, return_code=-1,
            stderr=config.missing_exe_reason("configureQsync2.exe"),
        )

    logger = log.get()

    # Build the topology arguments.  Precedence: explicit displays > explicit
    # grid >= 0 > auto-discovered displays > nothing (CLI default = display=0,0).
    topo_args: list[str] = []
    chosen_displays: list[tuple[int, int]] = []
    if displays:
        chosen_displays = list(displays)
    elif grid >= 0:
        topo_args = [f"grid={grid}"]
    else:
        chosen_displays = _discover_displays(dry_run=dry_run)

    if chosen_displays:
        topo_args = [f"display={g},{d}" for g, d in chosen_displays]

    role_arg = role.lower() if isinstance(role, str) else "server"
    if role_arg not in ("server", "client"):
        logger.warning("QSync: unknown role=%r, defaulting to 'server'", role)
        role_arg = "server"

    topo_desc = (
        " ".join(topo_args) if topo_args else "(default: display=0,0)"
    )
    if role_arg == "client":
        # In client mode the upstream timing server owns source/trigger; the
        # CLI rejects them here. We log them at debug for the operator's
        # benefit but do NOT pass them.
        logger.info(f"QSync: enabling client (upstream-driven, {topo_desc})")
    else:
        logger.info(
            f"QSync: enabling server (source={source} trigger={trigger} {topo_desc})"
        )

    args = ["enable", role_arg]
    args.extend(topo_args)
    if role_arg == "server":
        args.extend([f"source={source}", f"trigger={trigger}"])

    result = executors.run(exe, args, label="qsync-enable", dry_run=dry_run)

    if result.success:
        logger.info("QSync: enable command accepted")
        if wait and not dry_run:
            _poll_for_sync(logger, source if role_arg == "server" else "")
    else:
        logger.error(f"QSync enable failed: {executors.failure_detail(result)}")

    return result


def enable_from_profile(profile, *, wait: bool = True, dry_run: bool = False) -> CommandResult:
    """Apply QSync from a profile, forcing the role transition reliably.

    The previous implementation just called ``enable()`` directly, which
    silently no-op'd when configureQsync2.exe rejected the role change
    (most commonly: a node that came up as a daisy-chained client and
    refused to switch to server until QSync was first disabled). This
    bug surfaced on production as "server node restarts as client".

    The new sequence:
        1. Sanity gate (gpu_uuid_pin / sync_card_serial_pin if set).
        2. Force-disable QSync (idempotent — disabling an already-off
           QSync is a no-op).
        3. Settle wait.
        4. Enable with the profile's role/source/trigger/displays.
        5. Verify by reading back; if drift is detected, repeat the
           disable→enable cycle once with a longer settle. After the
           retry, surface a structured failure if drift remains so the
           operator sees exactly which fields didn't take.
    """
    from . import framelock as framelock_mod
    logger = log.get()
    q = profile.qsync

    if not q.enabled:
        logger.info("QSync: disabled in profile, skipping")
        return CommandResult(
            success=True, return_code=0,
            stdout="QSync disabled in profile",
        )

    # ---------- 1. Sanity gate ----------
    gpu_pin = (getattr(q, "gpu_uuid_pin", "") or "").strip()
    serial_pin = (getattr(q, "sync_card_serial_pin", "") or "").strip()
    if (gpu_pin or serial_pin) and not dry_run:
        try:
            fl_pre = framelock_mod.status()
        except Exception as exc:
            logger.warning(f"QSync sanity gate: framelock.status failed: {exc} — skipping check")
            fl_pre = None
        if fl_pre is not None and fl_pre.available:
            if gpu_pin and fl_pre.gpu_uuid and gpu_pin != fl_pre.gpu_uuid:
                msg = (
                    f"QSync apply refused: profile gpu_uuid_pin={gpu_pin!r} "
                    f"does not match live gpu_uuid={fl_pre.gpu_uuid!r}"
                )
                logger.error(msg)
                return CommandResult(success=False, return_code=-2, stderr=msg)
            if serial_pin and fl_pre.sync_card_serial and serial_pin != fl_pre.sync_card_serial:
                msg = (
                    f"QSync apply refused: profile sync_card_serial_pin={serial_pin!r} "
                    f"does not match live serial={fl_pre.sync_card_serial!r}"
                )
                logger.error(msg)
                return CommandResult(success=False, return_code=-2, stderr=msg)

    # ---------- shared apply args ----------
    mosaic_enabled = bool(getattr(profile.mosaic, "enabled", False))
    grid = q.grid if mosaic_enabled else -1

    raw_displays = getattr(q, "displays", None) or []
    displays_arg: list[tuple[int, int]] | None = None
    if raw_displays:
        parsed: list[tuple[int, int]] = []
        for pair in raw_displays:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    parsed.append((int(pair[0]), int(pair[1])))
                except (TypeError, ValueError):
                    continue
        if parsed:
            displays_arg = parsed

    role = getattr(q, "role", "server") or "server"

    def _force_apply(*, extra_settle: float = 0.0) -> CommandResult:
        """One disable→enable cycle. Returns the enable result."""
        # disable() already includes a settle (config.QSYNC_DISABLE_WAIT).
        disable(dry_run=dry_run)
        if extra_settle > 0 and not dry_run:
            executors.sleep(extra_settle, "qsync extra settle (retry)")
        return enable(
            source=q.source,
            trigger=q.trigger,
            grid=grid,
            displays=displays_arg,
            role=role,
            # We handle the post-enable wait ourselves below so we can
            # fold it into the verify loop. The inner enable() should
            # not also poll — that would double the wait.
            wait=False,
            dry_run=dry_run,
        )

    # ---------- 2-4. Disable → enable ----------
    result = _force_apply()
    if not result.success:
        # configureQsync2 rejected the enable outright — no point in retrying.
        return result

    if dry_run:
        return result

    # ---------- 5. Verify, retry once on drift ----------
    drift = _verify_against_profile(profile, settle_seconds=config.QSYNC_POLL_INTERVAL * 2)
    if not drift:
        logger.info("QSync: applied and verified")
        if wait:
            locked = _poll_for_sync(logger, q.source if role == "server" else "")
            if not locked and role == "server":
                # What fixed it by hand every time on SRV01/SRV02: a clean
                # disable + enable with nothing else running (the legacy tool
                # d8f876f). Do that once more before giving up.
                logger.info(f"QSync: not locked to {q.source} — retrying once (disable + enable)")
                result = _force_apply(extra_settle=2.0)
                if result.success:
                    _poll_for_sync(logger, q.source)
        return result

    logger.warning(
        "QSync: drift detected after first apply attempt: %s — retrying with longer settle",
        drift,
    )
    result = _force_apply(extra_settle=max(2.0, config.QSYNC_DISABLE_WAIT))
    if not result.success:
        return result

    drift = _verify_against_profile(profile, settle_seconds=config.QSYNC_POLL_INTERVAL * 3)
    if drift:
        msg = (
            "QSync apply did not converge after retry — drift: "
            + ", ".join(
                f"{k} expected={v.get('expected')!r} actual={v.get('actual')!r}"
                for k, v in drift.items()
            )
        )
        logger.error(msg)
        return CommandResult(
            success=False, return_code=-3,
            stderr=msg,
            stdout=result.stdout or "",
        )

    logger.info("QSync: applied and verified after one retry")
    if wait:
        _poll_for_sync(logger, q.source if role == "server" else "")
    return result


def is_locked_to(source: str) -> bool:
    """True when QSync reports synced AND (if known) to ``source``."""
    try:
        st = status()
    except Exception:
        return False
    return bool(st.synced and (not source or not st.source or st.source.upper() == source.upper()))


def verify_against_profile(profile) -> dict[str, dict]:
    """Public read-only drift check — used by /qsync/verify (preflight).

    Same diff logic as the internal verify in enable_from_profile, but
    settles for 0 seconds since the caller is doing a one-shot poll, not
    waiting on a post-enable transition. Returns the drift dict
    ``{field: {expected, actual}}``; empty dict = no drift.
    """
    return _verify_against_profile(profile, settle_seconds=0.0)


def live_role(state) -> str:
    """Node-level QSync role ("server" / "client") from a parsed QSyncState.

    On a multi-GPU box the sync card's timing GPU reports ``Sync State :
    SERVER`` and every other GPU on the same card reports ``CLIENT`` — those
    are *internal* clients of GPU 0, not evidence that the node is a
    daisy-chain client. So: any SERVER → server; CLIENT only (no SERVER) →
    client; nothing → server (the standalone default). Prefers the parser's
    own ``state.role`` when it has already decided.
    """
    return live_role_evidence(state) or "server"


def live_role_evidence(state) -> str | None:
    """Like ``live_role`` but returns ``None`` when the live state carries no
    role evidence at all — QSync off, or enabled but every GPU reports an
    empty ``Sync State``. Capture uses this to avoid stamping ``server`` on
    a daisy-chain client that merely wasn't locked at capture time
    (RENDER-02, 2026-11-01: captured as server+HOUSESYNC, and the Sync
    button then ran ``enable server`` on a box with no BNC feed).
    """
    parsed = (getattr(state, "role", "") or "").strip().lower()
    if parsed in ("server", "client"):
        return parsed
    states = {
        (getattr(g, "sync_state", "") or "").upper()
        for g in (getattr(state, "per_gpu_sync", None) or [])
    }
    if "SERVER" in states:
        return "server"
    if "CLIENT" in states:
        return "client"
    return None


def _verify_against_profile(profile, *, settle_seconds: float = 2.0) -> dict[str, dict]:
    """Compare live QSync + framelock state to the profile.

    Returns a drift dict shaped as ``{field: {"expected": x, "actual": y}}``.
    Empty dict = everything matches the profile's expectations.

    Settles for ``settle_seconds`` before reading so we don't race with the
    driver's post-enable transition. Reads both configureQsync2 (for role /
    source / trigger) and NVWMI (for the deeper fields, when available).
    """
    if settle_seconds > 0:
        executors.sleep(settle_seconds, "qsync verify settle")

    drift: dict[str, dict] = {}
    q = profile.qsync

    # configureQsync2-side checks (always available when QSync is)
    live = status()
    if live.available:
        # Role: server vs client. SERVER on any GPU wins — the other GPUs on
        # a multi-GPU server report CLIENT internally (see live_role()).
        evidence = live_role_evidence(live)
        actual_role = evidence or "server"
        expected_role = (getattr(q, "role", "server") or "server").lower()
        if evidence is None:
            # Enabled but no GPU reports SERVER/CLIENT: nothing is engaged.
            # Say so instead of "actual=server" (which read as a role flip
            # on an unlocked client).
            drift["role"] = {"expected": expected_role, "actual": "unsynced"}
        elif expected_role != actual_role:
            drift["role"] = {"expected": expected_role, "actual": actual_role}

        # Source — only meaningful for server mode (client inherits upstream).
        if expected_role == "server":
            expected_source = (q.source or "").upper()
            actual_source = (live.source or "").upper()
            # The parser folds "INTERNAL" and "VSYNC" both into "VSYNC" on
            # the live side (legacy bucket). HOUSESYNC is the meaningful
            # check for the daisy-chain bug; we don't drift-flag the
            # internal/vsync ambiguity until the parser splits them.
            if expected_source == "HOUSESYNC" and actual_source not in ("HOUSESYNC", ""):
                drift["source"] = {"expected": expected_source, "actual": actual_source}

    # NVWMI-side checks (only when framelock is available)
    try:
        from . import framelock as framelock_mod
        fl = framelock_mod.status()
        for k, v in framelock_mod.diff_against_profile(profile, fl).items():
            drift[k] = v
    except Exception as exc:
        log.get().debug(f"verify: framelock.status failed: {exc}")

    return drift


def disable(*, dry_run: bool = False) -> CommandResult:
    """Disable QSync."""
    exe = config.get_qsync_exe()
    if not exe:
        return CommandResult(
            success=False, return_code=-1,
            stderr=config.missing_exe_reason("configureQsync2.exe"),
        )

    logger = log.get()
    logger.info("QSync: disabling")

    result = executors.run(exe, ["disable"], label="qsync-disable", dry_run=dry_run)
    executors.sleep(config.QSYNC_DISABLE_WAIT, "qsync disable settle")

    if result.success:
        logger.info("QSync: disabled")
    else:
        logger.warning(f"QSync disable returned error: {executors.failure_detail(result)}")

    return result


def _poll_for_sync(logger, source: str = "") -> bool:
    """Poll until QSync is locked (to ``source`` when given) or timeout.

    A lock to the WRONG source does not count: SRV01 came back from a
    reboot locked to VSYNC/RISING, "enable HOUSESYNC" left it there, and
    the old poll reported "locked after 3s" regardless (the legacy tool dd06cbd).

    Returns True if sync was achieved, False on timeout.
    """
    timeout = config.QSYNC_SYNC_TIMEOUT
    interval = config.QSYNC_POLL_INTERVAL
    elapsed = 0.0
    want = (source or "").upper()

    while elapsed < timeout:
        executors.sleep(interval, "qsync lock poll")
        elapsed += interval

        try:
            state = status()
            have = (state.source or "").upper()
            if state.synced and (not want or not have or have == want):
                logger.info(f"QSync: locked after {elapsed:.0f}s" + (f" ({have})" if have else ""))
                return True
            if state.synced:
                logger.info(
                    f"QSync: locked to {have}, waiting for {want}... ({elapsed:.0f}s/{timeout}s)"
                )
                continue
            logger.info(f"QSync: waiting for lock... ({elapsed:.0f}s/{timeout}s)")
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.debug(f"QSync poll error: {exc}")

    logger.warning(f"QSync: did not lock within {timeout}s (continuing)")
    return False
