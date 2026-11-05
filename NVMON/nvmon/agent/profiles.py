"""Profile CRUD + apply pipeline.

Profiles are JSON files in `config.PROFILES_DIR`. The loader accepts the v1
nvdd-schema (mosaic / direct_display / qsync / edid / validation blocks). A
follow-up module from sub-project B will handle v2 migration — until then,
any unrecognized top-level field is ignored cleanly (no crash, no warning
spam).

The `apply()` function runs the full state transition end-to-end in the
nvdd-proven order:

    0. Ensure all monitors are active
    1. Return displays to desktop (disable DD)
    2. Disable QSync
    3. Disable Mosaic
    4. Configure Mosaic (or per-display mode set), then colour depth /
       pixel encoding per display via NVAPI (nvapi.py)
    5. Enable DD
    6. Enable QSync (with post-DD settle)

On failure it invokes `_rollback(...)` to restore the previous Mosaic + QSync
+ DD state best-effort.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Serialize apply()/restore() runs. Two near-simultaneous Apply clicks (or a
# fan-out + an on-boot apply) would otherwise interleave their DD/Mosaic/QSync
# shell-outs and produce duplicate, racing log output (see the doubled
# "[1/5] Returning displays to desktop" lines reported in the listener log).
# Held for the full duration of a pipeline; HTTP handlers spawn the worker
# thread and return 202 immediately, so the lock never blocks the request loop.
_apply_lock = threading.Lock()

# Snapshot of the most recent apply()/restore() outcome so /apply/status (and
# the controller's modal) can show truthful per-node results instead of the
# dispatch-only "accepted" we currently surface. Updated by apply() and
# restore() right before they return.
_last_result: dict[str, object] = {
    "in_progress": False,
    "action": None,        # "apply" | "restore" | None
    "profile": None,       # str | None
    "started_at": None,    # ISO8601 str | None
    "finished_at": None,   # ISO8601 str | None
    "success": None,       # bool | None
    "message": None,       # str | None
    "step": None,          # "[4/6] Configuring Mosaic" etc. while in_progress
    "cancelled": False,    # True when the last job was superseded / stopped
}
_last_result_lock = threading.Lock()

# How long a new apply waits for the job it preempted to unwind. A single
# NVIDIA command can take up to 60 s to return, so 90 s covers one in-flight
# call plus the settle sleeps.
_JOB_PREEMPT_TIMEOUT = 90.0


def get_last_result() -> dict[str, object]:
    """Snapshot of the most recent apply/restore outcome — safe to read concurrently."""
    with _last_result_lock:
        return dict(_last_result)


def apply_in_progress() -> bool:
    with _last_result_lock:
        return bool(_last_result.get("in_progress"))


def set_step(step: str) -> None:
    """Record the pipeline's current step for /apply/status."""
    with _last_result_lock:
        if _last_result.get("in_progress"):
            _last_result["step"] = step[:160]


def cancel_current() -> bool:
    """Cancel the running apply / restore / boot-apply.

    The job notices at its next NVIDIA command or settle wait and unwinds
    via ``executors.Cancelled``. Returns True when something was running.
    """
    if not apply_in_progress():
        return False
    log.get().warning("apply/restore cancel requested — the running job will stop at its next step")
    executors.CANCEL.set()
    return True


def _set_in_progress(action: str, profile_name: str | None) -> None:
    with _last_result_lock:
        _last_result.update({
            "in_progress": True,
            "action": action,
            "profile": profile_name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "success": None,
            "message": None,
            "step": "starting",
            "cancelled": False,
        })


def _set_finished(success: bool, message: str, *, cancelled: bool = False) -> None:
    with _last_result_lock:
        _last_result.update({
            "in_progress": False,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "success": success,
            "message": message,
            "step": None,
            "cancelled": cancelled,
        })


def _acquire_job(action: str, profile_name: str | None) -> bool:
    """Take the pipeline lock. Latest request wins.

    If another apply/restore (or the boot-apply) is running, cancel it and
    wait up to ``_JOB_PREEMPT_TIMEOUT`` for it to unwind. The old rule
    ("another apply is in progress — request rejected") meant an operator
    Apply-All right after a fleet reboot was silently dropped on every
    node still boot-applying (the legacy tool 7fedb78).
    """
    if _apply_lock.acquire(blocking=False):
        return True
    running = get_last_result()
    log.get().warning(
        f"{action} '{profile_name}': {running.get('action')} '{running.get('profile')}' "
        f"is still running — cancelling it (latest request wins)"
    )
    executors.CANCEL.set()
    deadline = time.monotonic() + _JOB_PREEMPT_TIMEOUT
    while time.monotonic() < deadline:
        if _apply_lock.acquire(timeout=0.5):
            return True
    log.get().error(f"{action} '{profile_name}': previous job did not stop within "
                    f"{_JOB_PREEMPT_TIMEOUT:.0f}s — request rejected")
    return False


def _run_job(action: str, profile_name: str | None, fn) -> tuple[bool, str]:
    """Run ``fn() -> (ok, msg)`` as THE job: lock held by the caller via
    ``_acquire_job``; sets in_progress, opens the cancel scope, records the
    outcome (including cancellation) and always releases the lock."""
    logger = log.get()
    executors.CANCEL.clear()
    executors.enter_cancel_scope()
    _set_in_progress(action, profile_name)
    cancelled = False
    try:
        ok, msg = fn()
    except executors.Cancelled:
        ok, msg, cancelled = False, f"{action} cancelled (superseded by a newer request)", True
        logger.warning(f"{action} '{profile_name}': {msg}")
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f"{action} crashed: {exc}"
        logger.error(msg, exc_info=True)
    finally:
        executors.exit_cancel_scope()
        _set_finished(ok, msg, cancelled=cancelled)
        _apply_lock.release()
    return ok, msg

from .. import config, executors, log
from ..types import (
    CommandResult, MosaicOutput, MosaicState, QSyncState,
    Profile, ProfileDD, ProfileEDID, ProfileMosaic, ProfileQSync,
    ProfileValidation,
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _profiles_dir() -> Path:
    """Return the resolved profiles directory, creating it if needed."""
    raw = config.get_agent_config()["profile_dir"]
    p = Path(raw)
    if not p.is_absolute():
        p = config.BASE_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def profile_path(name: str) -> Path:
    """Resolve a profile name to its JSON path."""
    if name.endswith(".json"):
        name = name[:-5]
    return _profiles_dir() / f"{name}.json"


# ---------------------------------------------------------------------------
# List / load / save / delete
# ---------------------------------------------------------------------------

def list_profiles() -> list[str]:
    """List available profile names (without .json extension)."""
    d = _profiles_dir()
    return sorted(p.stem for p in d.glob("*.json"))


def load(name: str) -> Profile:
    """Load a profile by name.

    Raises FileNotFoundError if the profile doesn't exist.
    """
    path = profile_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"Profile not found: {path}")

    # utf-8-sig: a profile edited with PowerShell/Notepad arrives with a UTF-8
    # BOM, which json.load rejects ("Unexpected UTF-8 BOM") — every node's
    # apply crashed on the 29.97 pair that way (2026-11-02 17:23).
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)

    return _dict_to_profile(data)


def save(profile: Profile) -> Path:
    """Save a profile to disk. Returns the saved path."""
    if not profile.name:
        raise ValueError("Profile must have a name")

    path = profile_path(profile.name)
    data = _profile_to_dict(profile)

    # Atomic write: .tmp + rename
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)

    log.get().info(f"Profile saved: {path}")
    return path


def delete(name: str) -> bool:
    """Delete a profile by name. Returns True if removed, False if not found."""
    path = profile_path(name)
    if path.is_file():
        path.unlink()
        log.get().info(f"Profile deleted: {path}")
        return True
    return False


def show(name: str) -> dict:
    """Load a profile and return its dict representation."""
    return _profile_to_dict(load(name))


# ---------------------------------------------------------------------------
# Active-profile snapshot — the single local source of truth for boot apply
# ---------------------------------------------------------------------------
#
# `BASE_DIR/active_profile.json` is a self-contained snapshot of the most
# recently dispatched /apply. It bundles the full profile dict AND the EDID
# bytes inline (base64) so the listener's boot-apply needs nothing else:
# not `profiles/<name>.json`, not `Edids/<rel>`, not `nvmon.toml`, not the
# monitor. The file IS the contract.
#
# Lifecycle:
#   /apply <name>  → snapshot_active(name) writes the file
#   /restore       → clear_active() removes the file
#   listener boot  → load_active() reads it and applies if validator drifts
#
# Schema (versioned so we can evolve later):
#   {
#     "schema": 1,
#     "name":   "<profile-name>",
#     "captured_at": "<iso8601 utc>",
#     "profile": { ... full profile dict, same shape as profiles/<name>.json ... },
#     "edid_b64": "<base64 of EDID bytes>" | null
#   }

ACTIVE_PROFILE_FILE = "active_profile.json"
ACTIVE_PROFILE_SCHEMA = 1


def active_profile_path() -> Path:
    """Resolved path of the snapshot file at BASE_DIR/active_profile.json."""
    return config.BASE_DIR / ACTIVE_PROFILE_FILE


def snapshot_active(name: str) -> Path:
    """Capture profile <name> as the local boot-apply contract.

    Reads `profiles/<name>.json`, resolves any referenced EDID file and
    inlines its bytes as base64, writes the result atomically to
    `BASE_DIR/active_profile.json`. The snapshot is fully self-contained —
    a later boot-apply will not need `profiles/` or `Edids/` to be present.

    Raises FileNotFoundError when the named profile doesn't exist.
    """
    profile = load(name)  # raises FileNotFoundError on missing
    edid_bytes: bytes | None = None
    if profile.edid.file:
        edid_path = config.BASE_DIR / profile.edid.file
        if edid_path.is_file():
            try:
                from . import edid as edid_mod
                edid_bytes = edid_mod.read_edid_file(edid_path)
            except Exception as exc:
                # Bundling is best-effort — if the EDID file is unreadable
                # the snapshot still captures the profile dict (with the
                # original file reference); the apply pipeline will surface
                # the error at apply time.
                log.get().warning(
                    "snapshot_active: EDID bundle failed for %s: %s",
                    edid_path, exc,
                )
                edid_bytes = None
        else:
            log.get().warning(
                "snapshot_active: EDID file %s not found — snapshot will "
                "still reference it, but boot-apply may fail if the file "
                "isn't there at boot",
                edid_path,
            )

    payload: dict = {
        "schema": ACTIVE_PROFILE_SCHEMA,
        "name": profile.name or name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "profile": _profile_to_dict(profile),
        "edid_b64": (
            base64.b64encode(edid_bytes).decode("ascii")
            if edid_bytes else None
        ),
    }

    target = active_profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(target)

    log.get().info(
        "active profile snapshot written: %s (name=%s, edid=%s bytes)",
        target.name, payload["name"],
        len(edid_bytes) if edid_bytes else 0,
    )
    return target


def clear_active() -> bool:
    """Remove the active-profile snapshot. Returns True if a file was removed."""
    target = active_profile_path()
    if target.is_file():
        try:
            target.unlink()
            log.get().info("active profile snapshot cleared: %s", target.name)
            return True
        except OSError as exc:
            log.get().warning("clear_active: unlink failed: %s", exc)
            return False
    return False


def load_active() -> tuple[Profile, bytes | None] | None:
    """Load the active-profile snapshot if present.

    Returns (Profile, edid_bytes) on success, None when no snapshot exists
    or the file is unreadable/malformed (logged at WARNING).

    The returned Profile carries `_edid_bytes` set to the resolved bytes
    so `_apply_inner` can write them to the registry without needing the
    original `Edids/<rel>` file to exist on disk.
    """
    target = active_profile_path()
    if not target.is_file():
        return None
    try:
        with target.open("r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.get().warning("load_active: read failed (%s) — ignoring snapshot", exc)
        return None

    if not isinstance(payload, dict):
        log.get().warning("load_active: top-level is not a dict — ignoring snapshot")
        return None

    profile_dict = payload.get("profile")
    if not isinstance(profile_dict, dict):
        log.get().warning("load_active: missing 'profile' dict — ignoring snapshot")
        return None

    try:
        profile = _dict_to_profile(profile_dict)
    except Exception as exc:
        log.get().warning("load_active: profile parse failed: %s", exc)
        return None

    # Snapshot's own name field wins over profile_dict['name'] in case the
    # operator renamed the file mid-edit. Both should agree, but defend.
    snap_name = str(payload.get("name", "") or "").strip()
    if snap_name:
        profile.name = snap_name

    edid_bytes: bytes | None = None
    raw_b64 = payload.get("edid_b64")
    if isinstance(raw_b64, str) and raw_b64:
        try:
            edid_bytes = base64.b64decode(raw_b64, validate=True)
        except Exception as exc:
            log.get().warning("load_active: edid_b64 decode failed: %s", exc)
            edid_bytes = None

    if edid_bytes:
        # Stash on the Profile so _apply_inner can use bytes directly
        # without having to re-read profile.edid.file from disk.
        setattr(profile, "_edid_bytes", edid_bytes)

    return profile, edid_bytes


def read_active_name() -> str:
    """Return the snapshot's profile name, or '' if no snapshot exists.

    Cheap accessor used by the agent boot path and /status — does not
    parse the full profile, just reads the name field.
    """
    target = active_profile_path()
    if not target.is_file():
        return ""
    try:
        with target.open("r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    name = str(payload.get("name", "") or "").strip()
    # Sanity: reject names that wouldn't pass profile_path()
    if not name or any(c in name for c in "\\/:*?\"<>|"):
        return ""
    return name


# ---------------------------------------------------------------------------
# Apply pipeline
# ---------------------------------------------------------------------------

def apply_active(
    *,
    dry_run: bool = False,
    skip: set[str] | None = None,
) -> tuple[bool, str]:
    """Apply the snapshot at BASE_DIR/active_profile.json end-to-end.

    Used by the listener's boot-apply path. Loads the frozen profile +
    inline EDID bytes from the snapshot and runs the same pipeline as
    apply(), bypassing the `profiles/<name>.json` lookup. The snapshot is
    self-contained, so this works even if `profiles/` was wiped, the
    workspace renamed, or `Edids/` mirroring is mid-flight.

    Returns (False, "no active_profile snapshot") when nothing to apply.
    """
    loaded = load_active()
    if loaded is None:
        return False, "no active_profile snapshot"
    profile, _edid_bytes = loaded

    if dry_run:
        return _apply_inner_with_profile(profile, dry_run=True, skip=skip)
    name = profile.name or "(active)"
    if not _acquire_job("boot-apply", name):
        return False, "another apply/restore would not stop — request rejected"
    return _run_job(
        "boot-apply", name,
        lambda: _apply_inner_with_profile(profile, dry_run=False, skip=skip),
    )


def apply(
    profile_name: str,
    *,
    dry_run: bool = False,
    skip: set[str] | None = None,
) -> tuple[bool, str]:
    """Apply a named profile end-to-end.

    Returns (success, message). Rolls back Mosaic/QSync/DD on failure.

    Args:
        profile_name: Profile name (with or without .json).
        dry_run: If True, log intended actions but don't execute.
        skip: Optional set of subsystem names to skip ("mosaic", "dd", "qsync").
    """
    if dry_run:
        return _apply_inner(profile_name, dry_run=True, skip=skip)
    # Serialize apply/restore — latest request wins. The HTTP /apply
    # handler hands the work to a daemon thread and returns 202
    # immediately, so this never blocks the request loop — only the
    # pipelines themselves.
    if not _acquire_job("apply", profile_name):
        return False, "another apply/restore would not stop — request rejected"
    return _run_job(
        "apply", profile_name,
        lambda: _apply_inner(profile_name, dry_run=False, skip=skip),
    )


def _apply_inner(
    profile_name: str,
    *,
    dry_run: bool,
    skip: set[str] | None,
) -> tuple[bool, str]:
    """Name-based apply — load `profiles/<name>.json`, then delegate."""
    logger = log.get()

    if not dry_run and not config.is_admin():
        msg = "Administrator privileges required to apply a profile"
        logger.error(msg)
        return False, msg

    try:
        profile = load(profile_name)
    except FileNotFoundError:
        avail = ", ".join(list_profiles()) or "(none)"
        msg = f"Profile '{profile_name}' not found. Available: {avail}"
        logger.error(msg)
        return False, msg

    return _apply_inner_with_profile(profile, dry_run=dry_run, skip=skip)


def _apply_inner_with_profile(
    profile: Profile,
    *,
    dry_run: bool,
    skip: set[str] | None,
) -> tuple[bool, str]:
    """Profile-object apply. Used by both name-based apply() and
    snapshot-based apply_active() so the pipeline lives in one place."""
    from . import displays as displays_mod
    from . import mosaic as mosaic_mod
    from . import qsync as qsync_mod
    from . import edid as edid_mod
    from . import nvapi as nvapi_mod

    logger = log.get()
    skip = skip or set()

    # Admin gate for real execution
    if not dry_run and not config.is_admin():
        msg = "Administrator privileges required to apply a profile"
        logger.error(msg)
        return False, msg

    logger.info(f"Applying profile: {profile.name} — {profile.description}")

    # Fold profile-level `skip_apply` flags into the runtime skip set so a
    # per-node profile (e.g. NODE-11 at 1080p while the fleet is 4K) can
    # declare "don't touch Mosaic / DD" and have the pipeline leave those
    # subsystems alone. The runtime `skip` parameter (passed by the caller)
    # is unioned in, so both override paths cooperate.
    if getattr(profile.mosaic, "skip_apply", False):
        skip = skip | {"mosaic"}
        logger.info("  [profile] mosaic.skip_apply=true — Mosaic untouched")
    if getattr(profile.direct_display, "skip_apply", False):
        skip = skip | {"dd"}
        logger.info("  [profile] direct_display.skip_apply=true — DD untouched")
    if getattr(profile.qsync, "skip_apply", False):
        skip = skip | {"qsync"}
        logger.info("  [profile] qsync.skip_apply=true — QSync untouched")

    # Shortcut: a profile with NOTHING enabled (Mosaic + DD + QSync all
    # off) has no apply work to do — it's effectively a "go-back-to-desktop"
    # profile. Run restore() instead and say so loudly so the operator
    # doesn't think the apply silently failed (the previous behavior of
    # logging "Applying profile: X" then running restore() looked like a
    # bug from the listener console — the user reported "monitor said
    # Applied but the refresh rate is still wrong").
    #
    # We deliberately do NOT shortcut on "mosaic disabled" alone — a
    # DD-only or QSync-only profile is a real apply pipeline and was
    # being silently dropped to restore() by the previous condition.
    # An EDID-only profile (just `edid.file`, nothing else enabled) is a
    # real apply target — push the override + force redetect, don't drop
    # to restore. display_ids-driven mode set also counts as work.
    nothing_to_apply = (
        not profile.mosaic.enabled
        and not profile.direct_display.enabled
        and not profile.qsync.enabled
        and not profile.edid.file
        and not getattr(profile, "_edid_bytes", None)
        and not profile.display_ids
    )
    if nothing_to_apply and "mosaic" not in skip:
        logger.warning(
            f"Profile '{profile.name}' has nothing enabled "
            f"(mosaic={profile.mosaic.enabled}, dd={profile.direct_display.enabled}, "
            f"qsync={profile.qsync.enabled}) — running restore() instead. "
            f"If you expected this profile to set a specific refresh rate or "
            f"display layout, edit the profile to enable the relevant subsystem."
        )
        # Call _restore_inner to avoid double-locking — we already hold _apply_lock.
        if not dry_run:
            return _restore_inner(default_profile_name=profile.name, dry_run=False)
        return restore(dry_run=True, default_profile_name=profile.name)

    # Honor an explicit `skip_apply` flag on Mosaic for a profile where DD
    # or QSync IS enabled — Mosaic stays untouched, but DD/QSync run.
    # (The previous code above shortcut to restore for any non-Mosaic
    # profile; that was the actual bug.)

    # Step 0: ensure all monitors are active
    logger.info("[0/6] Ensuring all monitors are active")
    set_step("[0/6] Ensuring all monitors are active")
    if not dry_run:
        try:
            displays_mod.ensure_all_active()
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.warning(f"ensure_all_active failed: {exc}")

    # Snapshot for rollback
    prev_mosaic: MosaicState | None = None
    prev_qsync: QSyncState | None = None
    prev_dd_active = False
    if not dry_run:
        prev_dd = None
        try:
            prev_dd = displays_mod.status()
            prev_dd_active = prev_dd.active
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.debug(f"rollback snapshot: dd status failed: {exc}")
        try:
            # dd_state resolves the Mosaic XML's display IDs to out=GPU,OUTPUT
            # — without it every output is (-1,-1) and rollback can never
            # rebuild the previous Mosaic (the legacy tool 9300302 review).
            prev_mosaic = mosaic_mod.query_current(dd_state=prev_dd)
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.debug(f"rollback snapshot: mosaic query failed: {exc}")
        try:
            prev_qsync = qsync_mod.status()
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.debug(f"rollback snapshot: qsync status failed: {exc}")

    # Step 1: EDID write (must precede Mosaic so the driver sees the
    # forced timing before topology is built — see spec 2026-04-17-B,
    # APPLY_ORDER puts edid before direct_display/mosaic). Skip silently
    # when the profile carries no file. Idempotent: apply_edid_to_all_displays
    # only writes when bytes differ, so re-applying the same profile is
    # a no-op (no display blink).
    #
    # Two byte-source paths:
    #   1. Inline bytes: profile carries `_edid_bytes` (set by load_active()
    #      when boot-apply runs from the snapshot). Use them directly — the
    #      original `Edids/<rel>` file might not even exist on this node.
    #   2. File path: profile.edid.file resolved relative to BASE_DIR.
    #      Used by /apply <name> from the monitor where the file IS present.
    inline_edid_bytes: bytes | None = getattr(profile, "_edid_bytes", None)
    has_edid = bool(profile.edid.file or inline_edid_bytes)
    if has_edid and "edid" not in skip:
        edid_bytes: bytes | None = None
        if inline_edid_bytes:
            edid_bytes = inline_edid_bytes
            source = "active_profile snapshot (inline)"
        else:
            edid_path = config.BASE_DIR / profile.edid.file
            if not edid_path.is_file():
                logger.error(f"EDID file not found: {edid_path}")
                return False, f"EDID file not found: {profile.edid.file}"

            try:
                edid_bytes = edid_mod.read_edid_file(edid_path)
            except (OSError, ValueError) as exc:
                logger.error(f"EDID file unreadable ({edid_path}): {exc}")
                return False, f"EDID file unreadable: {exc}"
            source = profile.edid.file

        logger.info(
            f"[EDID] Writing override from {source} "
            f"({len(edid_bytes)} bytes)"
        )
        changed, total, changed_keys = edid_mod.apply_edid_to_all_displays(
            edid_bytes, dry_run=dry_run,
        )
        if total == 0:
            logger.warning("[EDID] No displays found in registry")
        elif changed == 0:
            logger.info(f"[EDID] All {total} displays already have this EDID — skipped")
        else:
            logger.info(
                f"[EDID] Wrote override to {changed}/{total} displays: "
                f"{', '.join(changed_keys)}"
            )
            # Force a desktop redetect so Windows re-reads the override.
            # `displays_mod.disable()` runs `configureDdisplay return-to-desktop
            # --all-targets --force` which is exactly the redetect kick we
            # need; the DD-off step further down (Step 2) would also do
            # this, but if both DD and Mosaic are skipped we still need
            # the kick here.
            if not dry_run and ("dd" in skip and "mosaic" in skip):
                try:
                    displays_mod.disable(dry_run=False)
                except Exception as exc:
                    logger.warning(f"[EDID] redetect kick failed: {exc}")
    elif has_edid and "edid" in skip:
        logger.info("[EDID] SKIPPED (caller override)")

    # Step 2: DD off
    if "dd" not in skip:
        logger.info("[1/6] Returning displays to desktop")
        set_step("[1/6] Returning displays to desktop")
        displays_mod.disable(dry_run=dry_run)
    else:
        logger.info("[1/6] DD disable — SKIPPED (caller override)")

    # Step 3: QSync off
    if "qsync" not in skip:
        logger.info("[2/6] Disabling QSync")
        set_step("[2/6] Disabling QSync")
        qsync_mod.disable(dry_run=dry_run)
    else:
        logger.info("[2/6] QSync disable — SKIPPED (caller override)")

    # Step 4: Mosaic off
    if "mosaic" in skip:
        logger.info("[3/6] Mosaic disable — SKIPPED (caller override)")
    elif (
        not profile.mosaic.enabled
        and prev_mosaic is not None
        and not prev_mosaic.enabled
    ):
        # Profile doesn't use Mosaic and the rollback snapshot (taken at
        # step 0) already says it's off: nothing to disable. Skipping the
        # call saves two configureMosaic queries and a misleading
        # "Disabling Mosaic" line on rigs that never run it (site).
        logger.info("[3/6] Mosaic — not used by this profile and not active, skipping")
    else:
        logger.info("[3/6] Disabling Mosaic")
        set_step("[3/6] Disabling Mosaic")
        mosaic_mod.disable(dry_run=dry_run)

    # Step 5: Mosaic configure (or per-display mode set when Mosaic off)
    # Gate on `profile.mosaic.enabled` the same way DD and QSync gate on
    # theirs. When Mosaic is DISABLED in the profile but the profile has
    # per-display mode targets in `display_ids` (resolution + refresh_rate),
    # we drive each display to its target via `mosaic.set_modes_from_profile`.
    # That uses `configureMosaic.exe set rows=1 cols=1 ...` as the
    # mode-set engine — NVIDIA represents a non-Mosaic single display as
    # a 1x1 grid in `query current`, so this is just "set this display's
    # mode" without making it look like a multi-display Mosaic surface.
    # Result: a "no mosaic, 24Hz" profile actually pushes 24Hz now.
    if "mosaic" in skip:
        logger.info("[4/6] Mosaic configure — SKIPPED (caller override)")
    elif not profile.mosaic.enabled:
        if profile.display_ids:
            logger.info("[4/6] Setting per-display modes (Mosaic disabled)")
            n_attempted, n_skipped, results = mosaic_mod.set_modes_from_profile(
                profile, dry_run=dry_run,
            )
            logger.info(
                f"  Display modes: {n_attempted} pushed, {n_skipped} skipped (already correct)"
            )
            if not dry_run:
                for r in results:
                    if not r.success:
                        logger.error("Display mode-set failed for one or more displays")
                        _rollback(prev_mosaic, prev_qsync, prev_dd_active,
                                  dry_run=dry_run, profile=profile)
                        return False, f"Display mode-set failed: {executors.failure_detail(r)}"
        else:
            logger.info("[4/6] Mosaic disabled in profile, no display_ids — skipping")
    else:
        logger.info("[4/6] Configuring Mosaic")
        set_step("[4/6] Configuring Mosaic")
        result = mosaic_mod.configure_from_profile(profile, dry_run=dry_run)
        if not result.success and not dry_run:
            logger.error("Mosaic configuration failed")
            _rollback(prev_mosaic, prev_qsync, prev_dd_active, dry_run=dry_run, profile=profile)
            return False, f"Mosaic configure failed: {executors.failure_detail(result)}"

        if not dry_run:
            state = mosaic_mod.status()
            if state.enabled:
                logger.info(
                    f"  Mosaic verified: {state.rows}x{state.cols} @ {state.refresh_rate}Hz"
                )
                # configureMosaic picks the nearest timing it has (asked 30 →
                # got 29.97; asked 24 → 23.976 on panels without a true-24
                # timing). A wrong rate can never lock to house sync — roll
                # back now instead of spending minutes on an impossible
                # QSync wait and then reporting "applied" (the legacy tool 14b6dc6).
                wanted = _profile_rate(profile)
                tol = config.rate_tolerance(profile.validation.refresh_rate_tolerance)
                if wanted > 0 and state.refresh_rate > 0 and abs(state.refresh_rate - wanted) > tol:
                    msg = (
                        f"Mosaic came up at {state.refresh_rate}Hz, profile wants {wanted:g}Hz "
                        f"— this timing isn't offered at {profile.mosaic.width}x"
                        f"{profile.mosaic.height} (check /mosaic/timings; a custom "
                        f"timing may be needed in NVCP)"
                    )
                    logger.error(msg)
                    _rollback(prev_mosaic, prev_qsync, prev_dd_active, dry_run=dry_run, profile=profile)
                    return False, msg
            else:
                logger.warning("  Mosaic verification: could not confirm Mosaic is ON")

    # Step 5b: colour depth / pixel encoding per display (NVAPI). Nothing
    # above touches bits per channel or RGB/YCbCr — configureMosaic, Windows
    # SetDisplayConfig and the custom-timing calls all leave colour to the
    # driver, which re-chooses it per mode (site 2026-11-03: six of seven
    # Tessera SX40 nodes came up 8-bit RGB444 after a "60 - 10b" apply; the
    # profiles say 10-bit Ycc422). Runs after the mode set, before DD/QSync
    # — a colour change is a modeset and would drop a fresh lock. Not a hard
    # failure: the wall is up and (below) locked either way; the outcome is
    # logged and carried into the apply message.
    color_note = ""
    if "color" in skip:
        logger.info("[4/6] Colour depth / pixel encoding — SKIPPED (caller override)")
    elif profile.display_ids:
        logger.info("[4/6] Applying colour depth / pixel encoding")
        set_step("[4/6] Applying colour depth / pixel encoding")
        try:
            color_results = nvapi_mod.apply_color_data_from_profile(profile, dry_run=dry_run)
        except executors.Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"  Colour apply raised: {exc}")
            color_results = {}
        failed = sorted(k for k, ok in color_results.items() if not ok)
        # v0.9.6 retrain cycle disabled 2026-11-04: on the rig the link stayed
        # HBR3 x2 after the 60->30->60 cycle, the driver dropped the panels back
        # to 10-bit with colorimetry RGB, and the wall flashed purple/green
        # through the extra mode sets. The lane count is fixed at link
        # training (hotplug), not at mode set. Kept for reference only.
        if False and failed and not dry_run and not profile.mosaic.enabled:  # noqa: SIM223
            # The DP link is trained at mode-set time for the depth active
            # then; a colour change does not retrain it. site 2026-11-04: at
            # 4K60 the 10-bit mode set left HBR3 x2 (12.96 Gbit/s) and 12-bit
            # was refused, while a fresh link (replug) trained HBR2 x4 and
            # took 12-bit at 60 through the same adapter and matrix.
            try:
                retry, err = _colour_link_retrain_cycle(profile, failed, mosaic_mod, nvapi_mod)
            except executors.Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                retry, err = {}, f"colour link retrain raised: {exc}"
            if err:
                logger.error(f"  {err}")
                _rollback(prev_mosaic, prev_qsync, prev_dd_active, dry_run=dry_run, profile=profile)
                return False, err
            if retry:
                color_results.update(retry)
                failed = sorted(k for k, ok in color_results.items() if not ok)
        if color_results and not failed:
            logger.info(f"  Colour verified on {len(color_results)} display(s)")
        elif failed:
            logger.error(f"  Colour NOT applied on display(s) {', '.join(failed)} — see lines above")
            color_note = f" — colour depth/encoding not applied on display(s) {', '.join(failed)}"

    # Before DD: the operator display (KVM / control monitor) must be
    # primary. After a Mosaic rebuild Windows can make the wall primary at
    # (0,0), and DirectDisplay cannot take the primary display — DD enable
    # then fails with "sendCmdManageTarget returned error". Only attempted
    # when the operator told us what that display is called
    # ([agent.displays].excluded_name_prefixes), and moved via
    # SetDisplayConfig so no mode is re-chosen (the legacy tool 6aefc9c/5f02b71).
    if not dry_run and "dd" not in skip and profile.direct_display.enabled:
        prefixes = [p for p in config.get_agent_config()["displays"]["excluded_name_prefixes"] if p]
        if prefixes:
            set_step("[4/6] Making operator display primary before DD")
            if not displays_mod.make_primary(name_hint=prefixes[0]):
                msg = (
                    f"operator display '{prefixes[0]}' is not primary after the Mosaic set "
                    f"— not entering DirectDisplay (DD cannot take the primary display)"
                )
                logger.error(msg)
                _rollback(prev_mosaic, prev_qsync, prev_dd_active, dry_run=dry_run, profile=profile)
                return False, msg

    # Step 6: DD enable (if profile requests it)
    if "dd" in skip:
        logger.info("[5/6] DD enable — SKIPPED (caller override)")
    elif profile.direct_display.enabled:
        logger.info("[5/6] Entering Direct Display mode")
        set_step("[5/6] Entering Direct Display mode")
        dd_result = displays_mod.enable(
            mode=profile.direct_display.mode,
            index=profile.direct_display.index,
            dry_run=dry_run,
        )
        if not dd_result.success and not dry_run:
            logger.error("DD enable failed")
            _rollback(prev_mosaic, prev_qsync, prev_dd_active, dry_run=dry_run, profile=profile)
            return False, f"DD enable failed: {executors.failure_detail(dd_result)}"

        if not dry_run:
            dd_state = displays_mod.status()
            if dd_state.active:
                logger.info(f"  DD verified: {dd_state.display_count} displays in DD mode")
            else:
                logger.warning("  DD verification: not all displays in DD mode")
    else:
        logger.info("[5/6] DD disabled in profile, skipping")

    # DD-to-QSync settle
    if (
        "dd" not in skip and "qsync" not in skip
        and profile.direct_display.enabled and profile.qsync.enabled
        and not dry_run
    ):
        logger.info(f"  DD-to-QSync settle ({config.DD_TO_QSYNC_SETTLE}s)")
        executors.sleep(config.DD_TO_QSYNC_SETTLE, "DD-to-QSync settle")

    # Step 7: QSync enable
    if "qsync" in skip:
        logger.info("[6/6] QSync enable — SKIPPED (caller override)")
    elif "qsync_enable" in skip:
        # Chain member at boot (v0.9.9): QSync was disabled in step 2 like
        # any apply; the controller enables the chain server-first once
        # every member is up (nvmon/controller/chainsync.py).
        logger.info("[6/6] QSync enable — deferred to the controller's chain sync")
    elif profile.qsync.enabled:
        # House-sync gate: a lock is impossible when the incoming clock runs
        # at a different rate than the profile (generator at 30, profile
        # 60). Fail now with a clear message instead of burning minutes on
        # lock retries and then reporting "applied" (the legacy tool 7fedb78).
        if not dry_run:
            house_ok, house_msg = qsync_mod.house_sync_matches(profile)
            if not house_ok:
                msg = f"QSync not enabled — {house_msg}"
                logger.error(f"[6/6] {msg}")
                return False, msg
        logger.info("[6/6] Enabling QSync")
        set_step("[6/6] Enabling QSync")
        qsync_mod.enable_from_profile(profile, dry_run=dry_run)

        if not dry_run:
            qsync_state = qsync_mod.status()
            display_rate = _profile_rate(profile)
            if display_rate > 0:
                qsync_mod.apply_rate_check(qsync_state, display_rate)
            role = (getattr(profile.qsync, "role", "server") or "server").lower()
            # A client counts as locked only when the card says synced — a
            # GPU with "Is synced : YES". "Sync State : CLIENT" is just the
            # configured role (site 2026-11-02: every client CLIENT + "Is
            # synced : NO" while both servers were UNSYNCED, and this step
            # reported "applied" / "locked after 3s").
            if qsync_state.synced or (role == "client" and any(
                g.synced for g in qsync_state.per_gpu_sync
            )):
                logger.info(
                    f"  QSync verified: locked, house_sync={qsync_state.house_sync_detected}"
                )
            else:
                # enable_from_profile already waited QSYNC_SYNC_TIMEOUT + one
                # retry. Report the truth — "applied" with no lock is the lie
                # the dashboard used to tell (the legacy tool f091cd1).
                note = ""
                if qsync_state.per_gpu_sync and qsync_state.per_gpu_sync[0].sync_note:
                    note = f" ({qsync_state.per_gpu_sync[0].sync_note})"
                # Server, house sync present at the right rate, the enable
                # was accepted, yet no GPU reports SERVER or CLIENT: the
                # card/driver is wedged. RENDER-01 sat in exactly this
                # state at 23.976 (2026-11-01) through every GPU/trigger/
                # source combination; a Windows restart cleared it and the
                # same command then locked in 20 s. Say so instead of
                # letting the operator re-Sync for an hour.
                if (
                    role == "server"
                    and qsync_state.house_sync_detected
                    and qsync_state.per_gpu_sync
                    and not any((g.sync_state or "").strip() for g in qsync_state.per_gpu_sync)
                ):
                    note += (
                        " — house sync is present and the card accepted the enable, but no GPU "
                        "became SERVER; if a re-Sync does not fix it, restart Windows on this node"
                    )
                msg = (
                    f"Profile '{profile.name}' applied at {display_rate:g}Hz but QSync not "
                    f"locked (house sync {qsync_state.house_sync_incoming_hz:.3f}Hz, "
                    f"source={qsync_state.source or 'none'}){note}"
                )
                logger.error(msg)
                return False, msg
    else:
        logger.info("[6/6] QSync disabled in profile, skipping")

    logger.info(f"Profile '{profile.name}' applied successfully{color_note}")
    return True, f"Profile '{profile.name}' applied{color_note}"


LINK_RETRAIN_RATE_HZ = 30.0


def _colour_link_retrain_cycle(profile: Profile, failed: list[str], mosaic_mod, nvapi_mod) -> tuple[dict[str, bool], str]:
    """Get a colour depth the sink supports but the current DP link was not
    trained for: drop the panels to a light mode (same resolution, 30 Hz
    designed timing), set the colour there, then set the profile's timing
    again so the driver trains the link for the new depth, then verify.

    Only runs when every refused display's sink lists the wanted bpc and the
    current mode does not (i.e. it is a link-size problem, not a panel one).
    Returns ``(results_by_display, error)``; error is set only when the
    final mode set back to the profile's timing failed.
    """
    import copy

    from . import timing as timing_mod

    logger = log.get()
    plan = nvapi_mod.plan_color_from_profile(profile)
    for hex_id in failed:
        wanted = plan.get(hex_id, (None, ""))[0]
        did = nvapi_mod.display_id_from_hex(hex_id)
        info = nvapi_mod.get_displayport_info(did) if did is not None else None
        bits = wanted.bits() if wanted is not None else 0
        if not (info and bits and bits in (info.get("sink_supports_bpc") or [])
                and bits not in (info.get("current_mode_bpc") or [])):
            logger.info(f"  Colour: display {hex_id} — not a link-size limit (sink/link info: "
                        f"{(info or {}).get('sink_supports_bpc')} / {(info or {}).get('current_mode_bpc')}); no retrain")
            return {}, ""
    if _profile_rate(profile) and _profile_rate(profile) <= LINK_RETRAIN_RATE_HZ + 0.01:
        return {}, ""

    light = copy.deepcopy(profile)
    for info in (light.display_ids or {}).values():
        if not isinstance(info, dict):
            continue
        res = info.get("resolution") or [0, 0]
        try:
            w, h = int(res[0]), int(res[1])
            info["timing"] = timing_mod.design_timing(w, h, LINK_RETRAIN_RATE_HZ).to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"  Colour: no light timing for {res}: {exc}; no retrain")
            return {}, ""
        info["refresh_rate"] = LINK_RETRAIN_RATE_HZ
        info["refresh_fraction"] = f"{int(LINK_RETRAIN_RATE_HZ)}/1"
    light.validation.expected_refresh_rate = LINK_RETRAIN_RATE_HZ

    set_step("[4/6] Colour: retraining DisplayPort link (via 30 Hz)")
    logger.info(f"  Colour: display(s) {', '.join(failed)} — link too small for the depth at this rate; "
                f"setting colour at {LINK_RETRAIN_RATE_HZ:g} Hz, then the target timing again")
    _, _, res_light = mosaic_mod.set_modes_from_profile(light)
    if any(not r.success for r in res_light):
        logger.warning("  Colour: light mode set failed; restoring target timing without the retrain")
    nvapi_mod.apply_color_data_from_profile(profile)
    _, _, res_back = mosaic_mod.set_modes_from_profile(profile)
    if any(not r.success for r in res_back):
        bad = next(r for r in res_back if not r.success)
        return {}, f"Display mode-set back to the profile timing failed after the colour retrain: {executors.failure_detail(bad)}"
    return nvapi_mod.apply_color_data_from_profile(profile), ""


def _profile_rate(profile: Profile) -> float:
    """The display rate a profile asks for: Mosaic rate when Mosaic is on,
    else validation.expected_refresh_rate (0 = no expectation)."""
    if profile.mosaic.enabled:
        try:
            return float(profile.mosaic.refresh_rate)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(profile.validation.expected_refresh_rate or 0.0)
    except (TypeError, ValueError):
        return 0.0


def restore(
    *,
    dry_run: bool = False,
    default_profile_name: str | None = None,
) -> tuple[bool, str]:
    """Restore to desktop — disable DD, QSync, Mosaic, re-enable monitors.

    If a default profile with a `windows_arrangement` is available, apply it.
    """
    if dry_run:
        return _restore_inner(default_profile_name=default_profile_name, dry_run=True)
    if not _acquire_job("restore", default_profile_name):
        return False, "another apply/restore would not stop — request rejected"
    return _run_job(
        "restore", default_profile_name,
        lambda: _restore_inner(default_profile_name=default_profile_name, dry_run=False),
    )


def _restore_inner(
    *,
    default_profile_name: str | None,
    dry_run: bool,
) -> tuple[bool, str]:
    from . import displays as displays_mod
    from . import mosaic as mosaic_mod
    from . import qsync as qsync_mod

    logger = log.get()

    if not dry_run and not config.is_admin():
        msg = "Administrator privileges required to restore"
        logger.error(msg)
        return False, msg

    logger.info("Restoring displays to desktop")

    logger.info("[1/5] Returning displays to desktop")
    displays_mod.disable(dry_run=dry_run)

    logger.info("[2/5] Disabling QSync")
    qsync_mod.disable(dry_run=dry_run)

    logger.info("[3/5] Disabling Mosaic")
    mosaic_mod.disable(dry_run=dry_run)

    # Optional arrangement from a named profile
    default_profile: Profile | None = None
    if default_profile_name and not dry_run:
        try:
            default_profile = load(default_profile_name)
        except FileNotFoundError:
            logger.debug(f"restore: profile '{default_profile_name}' not found, no arrangement")

    logger.info("[4/5] Enabling all monitors")
    if not dry_run:
        try:
            displays_mod.ensure_all_active()
        except Exception as exc:
            logger.warning(f"ensure_all_active failed: {exc}")

    logger.info("[5/5] Restoring display arrangement")
    if not dry_run and default_profile and default_profile.windows_arrangement:
        try:
            displays_mod.apply_arrangement(default_profile.windows_arrangement)
            logger.info("  Arrangement applied from profile")
        except Exception as exc:
            logger.warning(f"apply_arrangement failed: {exc}")
    else:
        logger.info("  No arrangement data, skipping")

    return True, "Restore complete"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def _rollback(
    prev_mosaic: MosaicState | None,
    prev_qsync: QSyncState | None,
    prev_dd_active: bool,
    *,
    dry_run: bool,
    profile: Profile | None = None,
) -> None:
    """Best-effort restore of previous Mosaic → DD → QSync state after failure.

    Order matters (the legacy tool 9300302 / 54b3492): DD on a single panel is
    wrong, so DD is only restored when the Mosaic surface came back; QSync
    likewise. Outputs the XML couldn't resolve (gpu/display = -1) are
    never sent to configureMosaic — they always fail; the target profile's
    outputs are used instead because a node's profiles share the same
    physical outputs at every rate.
    """
    from . import displays as displays_mod
    from . import mosaic as mosaic_mod
    from . import qsync as qsync_mod

    logger = log.get()
    logger.info("--- ROLLING BACK to previous state ---")
    set_step("Rolling back to previous state")

    mosaic_restored = not (prev_mosaic and prev_mosaic.enabled)
    if prev_mosaic and prev_mosaic.enabled and not dry_run:
        logger.info(
            f"Rollback: restoring Mosaic {prev_mosaic.rows}x{prev_mosaic.cols} "
            f"@ {prev_mosaic.refresh_rate}Hz"
        )
        w, h = prev_mosaic.per_display_resolution
        prev_outs = list(prev_mosaic.outputs)
        unresolved = any(o.gpu < 0 or o.display < 0 for o in prev_outs)
        if (unresolved or not prev_outs) and profile is not None and profile.mosaic.outputs:
            prev_outs = list(profile.mosaic.outputs)
            unresolved = any(o.gpu < 0 or o.display < 0 for o in prev_outs)
        if unresolved:
            logger.error("Rollback: Mosaic restore skipped — previous outputs unknown (gpu/display -1)")
        else:
            outputs = " ".join(f"out={o.gpu},{o.display}" for o in prev_outs)
            if not outputs:
                outputs = "out=1,0 out=1,1 out=1,2 out=1,3"
            rate_str = str(prev_mosaic.refresh_rate) if prev_mosaic.refresh_rate else "23.98"
            try:
                result = mosaic_mod.configure(
                    rows=prev_mosaic.rows, cols=prev_mosaic.cols,
                    width=w or 3840, height=h or 2160,
                    refresh_rate=rate_str, outputs=outputs,
                )
                mosaic_restored = bool(result.success)
                if not result.success:
                    logger.error(f"Rollback Mosaic restore failed: {executors.failure_detail(result)}")
            except executors.Cancelled:
                raise
            except Exception as exc:
                logger.error(f"Rollback Mosaic restore failed: {exc}")

    if prev_dd_active and not dry_run and not mosaic_restored:
        logger.error("Rollback: DirectDisplay restore skipped — Mosaic was not restored")
    elif prev_dd_active and not dry_run:
        logger.info("Rollback: restoring DirectDisplay mode")
        try:
            displays_mod.enable()
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.error(f"Rollback DD restore failed: {exc}")

    if prev_qsync and prev_qsync.available and prev_qsync.source and not dry_run and mosaic_restored:
        logger.info(
            f"Rollback: restoring QSync (source={prev_qsync.source}, "
            f"trigger={prev_qsync.trigger})"
        )
        try:
            qsync_mod.enable(
                source=prev_qsync.source or "HOUSESYNC",
                trigger=prev_qsync.trigger or "FALLING",
                wait=False,
            )
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.error(f"Rollback QSync restore failed: {exc}")

    logger.info("--- Rollback complete ---")


# ---------------------------------------------------------------------------
# v1 schema (nvdd-compatible) serialization
# ---------------------------------------------------------------------------

def _dict_to_profile(data: dict) -> Profile:
    """Convert a JSON dict (v1 nvdd schema) to a Profile dataclass.

    Unknown fields (including the nvdd-specific `ddx_grid`) are preserved on
    a `_extras` attribute so B's v2 migrator can recover them if desired.
    """
    p = Profile()
    p.name = data.get("name", "")
    p.description = data.get("description", "")

    # Mosaic
    md = data.get("mosaic", {}) or {}
    p.mosaic = ProfileMosaic(
        enabled=md.get("enabled", True),
        rows=md.get("rows", 2),
        cols=md.get("cols", 2),
        width=md.get("width", 3840),
        height=md.get("height", 2160),
        refresh_rate=str(md.get("refresh_rate", "23.98")),
        outputs=[
            MosaicOutput(
                gpu=o.get("gpu", 0),
                display=o.get("display", i),
                display_id=o.get("display_id", ""),
            )
            for i, o in enumerate(md.get("outputs", []) or [])
        ],
        skip_apply=bool(md.get("skip_apply", False)),
    )

    # If mosaic_layout.grid present, use its order to reindex outputs
    layout_data = data.get("mosaic_layout", {})
    if isinstance(layout_data, dict) and "grid" in layout_data:
        grid = layout_data["grid"]
        ordered_pairs = []
        for row in grid or []:
            for cell in row or []:
                parts = str(cell).split(":")
                if len(parts) == 2:
                    try:
                        ordered_pairs.append((int(parts[0]), int(parts[1])))
                    except ValueError:
                        continue
        lookup = {(o.gpu, o.display): o for o in p.mosaic.outputs}
        reordered = [lookup[pair] for pair in ordered_pairs if pair in lookup]
        if len(reordered) == len(p.mosaic.outputs):
            p.mosaic.outputs = reordered

    # DirectDisplay
    dd = data.get("direct_display", {}) or {}
    p.direct_display = ProfileDD(
        enabled=dd.get("enabled", True),
        mode=dd.get("mode", "index"),
        index=dd.get("index", 1),
        skip_apply=bool(dd.get("skip_apply", False)),
    )

    # QSync
    qs = data.get("qsync", {}) or {}
    # Source normalization: legacy captures wrote "VSYNC" (a parser-internal
    # bucket) into the JSON. The configureQsync2.exe CLI doesn't accept that
    # — only HOUSESYNC and INTERNAL are valid for `enable server source=...`.
    # Coerce VSYNC -> HOUSESYNC (the daisy-chain client + standalone-with-
    # house-sync cases both want HOUSESYNC; INTERNAL is the rare "no external
    # sync" path which capture wouldn't have produced anyway).
    raw_role = str(qs.get("role", "server") or "server").lower()
    if raw_role not in ("server", "client"):
        log.get().warning(
            "profile load: unknown qsync.role=%r — coercing to 'server'",
            qs.get("role"),
        )
        raw_role = "server"

    raw_source = str(qs.get("source", "HOUSESYNC") or "HOUSESYNC").upper().replace(" ", "")
    if raw_role == "client" and raw_source in ("", "VSYNC", "INTERNAL", "HOUSESYNC"):
        # A daisy-chain client has no source of its own — it locks to the
        # upstream framelock pulse over RJ45 and its card reports VSYNC
        # because there is no BNC. profile_capture writes "" for clients;
        # coercing that to HOUSESYNC (as the server path below does) made
        # every client profile claim house sync it never receives.
        normalized_source = ""
    elif raw_source in ("VSYNC", ""):
        if raw_source == "VSYNC":
            log.get().warning(
                "profile load: coercing legacy qsync.source=VSYNC -> HOUSESYNC "
                "(VSYNC is a parser bucket, not a valid CLI source)."
            )
        normalized_source = "HOUSESYNC"
    elif raw_source in ("HOUSESYNC", "INTERNAL"):
        normalized_source = raw_source
    else:
        log.get().warning(
            "profile load: unknown qsync.source=%r — coercing to HOUSESYNC",
            qs.get("source"),
        )
        normalized_source = "HOUSESYNC"

    raw_displays = qs.get("displays", []) or []
    parsed_displays: list[tuple[int, int]] = []
    if isinstance(raw_displays, list):
        for pair in raw_displays:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    parsed_displays.append((int(pair[0]), int(pair[1])))
                except (TypeError, ValueError):
                    continue

    raw_master = qs.get("master_display", None)
    parsed_master: tuple[int, int] | None = None
    if isinstance(raw_master, (list, tuple)) and len(raw_master) == 2:
        try:
            parsed_master = (int(raw_master[0]), int(raw_master[1]))
        except (TypeError, ValueError):
            parsed_master = None

    p.qsync = ProfileQSync(
        enabled=qs.get("enabled", True),
        source=normalized_source,
        trigger=qs.get("trigger", "FALLING"),
        grid=qs.get("grid", -1),
        skip_apply=bool(qs.get("skip_apply", False)),
        role=raw_role,
        displays=parsed_displays,
        master_display=parsed_master,
        connector_0_direction=str(qs.get("connector_0_direction", "") or ""),
        connector_1_direction=str(qs.get("connector_1_direction", "") or ""),
        source_kind=str(qs.get("source_kind", "") or ""),
        gpu_uuid_pin=str(qs.get("gpu_uuid_pin", "") or ""),
        sync_card_serial_pin=str(qs.get("sync_card_serial_pin", "") or ""),
        driver_version_pin=str(qs.get("driver_version_pin", "") or ""),
    )

    # EDID
    ed = data.get("edid", {}) or {}
    p.edid = ProfileEDID(file=ed.get("file", ""))

    # Validation
    vd = data.get("validation", {}) or {}
    p.validation = ProfileValidation(
        # Defaults of 0.0 / False mean "no expectation". The legacy
        # 23.976 default was a legacy-tool-era leak; rigs running at 24, 25,
        # 30, 50, 60 etc. were getting spurious validation failures
        # before the operator captured a profile. With 0.0 the validator
        # now no-ops the check unless the profile carries a real value
        # written by profile_capture from the live rate.
        expected_refresh_rate=float(vd.get("expected_refresh_rate", 0.0)),
        refresh_rate_tolerance=float(vd.get("refresh_rate_tolerance", 0.05)),
        expected_qsync_locked=bool(vd.get("expected_qsync_locked", False)),
        expected_house_sync_freq=float(vd.get("expected_house_sync_freq", 0.0)),
    )

    # Passthrough fields
    p.display_ids = data.get("display_ids", {}) or {}
    p.windows_arrangement = data.get("windows_arrangement", []) or []
    p.mosaic_command = data.get("mosaic_command", "")

    # Preserve unknown top-level keys (e.g. nvdd's ddx_grid) for B's v2 migration.
    known = {
        "name", "description", "mosaic", "mosaic_layout",
        "direct_display", "qsync", "edid", "validation",
        "display_ids", "windows_arrangement", "mosaic_command",
    }
    extras = {k: v for k, v in data.items() if k not in known}
    if extras:
        # Attach as a plain attribute; the Profile dataclass ignores unknown
        # attrs at serialization time.
        setattr(p, "_extras", extras)

    return p


def _profile_to_dict(p: Profile) -> dict:
    """Convert a Profile dataclass back to a JSON-serializable dict."""
    d: dict = {
        "name": p.name,
        "description": p.description,
        "mosaic": {
            "enabled": p.mosaic.enabled,
            "rows": p.mosaic.rows,
            "cols": p.mosaic.cols,
            "width": p.mosaic.width,
            "height": p.mosaic.height,
            "refresh_rate": p.mosaic.refresh_rate,
            "outputs": [
                {"gpu": o.gpu, "display": o.display, "display_id": o.display_id}
                for o in p.mosaic.outputs
            ],
            "skip_apply": p.mosaic.skip_apply,
        },
        "direct_display": {
            "enabled": p.direct_display.enabled,
            "mode": p.direct_display.mode,
            "index": p.direct_display.index,
            "skip_apply": p.direct_display.skip_apply,
        },
        "qsync": {
            "enabled": p.qsync.enabled,
            "source": p.qsync.source,
            "trigger": p.qsync.trigger,
            "grid": p.qsync.grid,
            "skip_apply": p.qsync.skip_apply,
            "role": p.qsync.role,
            "displays": [list(pair) for pair in p.qsync.displays],
            "master_display": (
                list(p.qsync.master_display) if p.qsync.master_display else None
            ),
            "connector_0_direction": p.qsync.connector_0_direction,
            "connector_1_direction": p.qsync.connector_1_direction,
            "source_kind": p.qsync.source_kind,
            "gpu_uuid_pin": p.qsync.gpu_uuid_pin,
            "sync_card_serial_pin": p.qsync.sync_card_serial_pin,
            "driver_version_pin": p.qsync.driver_version_pin,
        },
        "edid": {"file": p.edid.file},
        "validation": {
            "expected_refresh_rate": p.validation.expected_refresh_rate,
            "refresh_rate_tolerance": p.validation.refresh_rate_tolerance,
            "expected_qsync_locked": p.validation.expected_qsync_locked,
            "expected_house_sync_freq": p.validation.expected_house_sync_freq,
        },
    }

    # Mosaic layout visual helper
    if (
        p.mosaic.enabled and p.mosaic.outputs
        and p.mosaic.rows > 0 and p.mosaic.cols > 0
    ):
        grid = []
        for r in range(p.mosaic.rows):
            row = []
            for c in range(p.mosaic.cols):
                idx = r * p.mosaic.cols + c
                if idx < len(p.mosaic.outputs):
                    o = p.mosaic.outputs[idx]
                    row.append(f"{o.gpu}:{o.display}")
            grid.append(row)
        d["mosaic_layout"] = {
            "help": (
                "Mosaic grid layout. Each cell = 'gpu:display'. "
                "Swap entries to rearrange."
            ),
            "grid": grid,
        }

    if p.display_ids:
        d["display_ids"] = p.display_ids
    if p.windows_arrangement:
        d["windows_arrangement"] = p.windows_arrangement
    if p.mosaic_command:
        d["mosaic_command"] = p.mosaic_command

    # Restore any extras captured on load (keeps v1 round-trip lossless).
    extras = getattr(p, "_extras", None)
    if isinstance(extras, dict):
        for k, v in extras.items():
            d.setdefault(k, v)

    return d


