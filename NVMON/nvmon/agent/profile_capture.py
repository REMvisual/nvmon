"""Capture live subsystem state into a `Profile` and save it.

Used by the agent's ``POST /profile/save`` endpoint and by the CLI
``python -m nvmon save-profile <name>``. The captured profile becomes the
Ground-Truth definition of the node's current display configuration —
suitable for ``/all/apply`` fan-out across the rest of the fleet.

Critical fields the user cares about:
    color space, resolution, timings (refresh fraction), fps (refresh rate),
    output count, GPU/display mapping, QSync source/trigger, EDID overrides.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import config, log
from ..types import (
    MosaicOutput, Profile, ProfileDD, ProfileEDID, ProfileMosaic,
    ProfileQSync, ProfileValidation,
)
from . import displays as displays_mod
from . import edid as edid_mod
from . import framelock as framelock_mod
from . import gpu as gpu_mod
from . import mosaic as mosaic_mod
from . import nvapi as nvapi_mod
from . import profiles as profiles_mod
from . import qsync as qsync_mod


def _role_from_active_profile() -> str | None:
    """QSync role recorded in this node's active-profile snapshot, if any."""
    try:
        loaded = profiles_mod.load_active()
    except Exception as exc:  # pragma: no cover - defensive
        log.get().debug(f"capture: load_active failed: {exc}")
        return None
    if not loaded:
        return None
    profile, _edid = loaded
    role = (getattr(profile.qsync, "role", "") or "").strip().lower()
    return role if role in ("server", "client") else None


def capture_current_state() -> Profile:
    """Snapshot the live state of every subsystem into a `Profile`.

    Each subsystem call falls back gracefully — if Mosaic isn't installed
    we still capture DD + QSync; if QSync isn't installed we still capture
    Mosaic; etc.
    """
    logger = log.get()
    p = Profile()
    p.description = (
        f"Captured from live state at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    # --- Mosaic --------------------------------------------------------
    # DD state is REQUIRED to turn the Mosaic XML's display IDs (e.g.
    # 0x82061085) into real (gpu, display) indices. Without it,
    # _resolve_mosaic_outputs falls back to the (-1,-1) sentinel and the
    # captured profile records garbage outputs.
    dd_state_for_mosaic = None
    try:
        dd_state_for_mosaic = displays_mod.status()
    except Exception as exc:
        logger.debug(f"capture: DD state for mosaic resolution unavailable: {exc}")

    try:
        mos = mosaic_mod.status()
    except Exception as exc:
        logger.warning(f"capture: mosaic.status failed: {exc}")
        mos = None

    # mosaic.status() parses WITHOUT dd_state, so its outputs come back as
    # (-1,-1). When Mosaic is on and we have DD state, re-run the XML query
    # WITH dd_state so the outputs carry real indices (ground truth, not a
    # sentinel). Leaves outputs alone if resolution still fails.
    if mos and mos.enabled and dd_state_for_mosaic is not None:
        try:
            resolved = mosaic_mod.query_current(dd_state=dd_state_for_mosaic)
            if resolved is not None and resolved.outputs:
                mos.outputs = resolved.outputs
        except Exception as exc:
            logger.debug(f"capture: mosaic output re-resolution failed: {exc}")

    # Mosaic is only "really enabled" if the agent reports both the flag
    # AND non-zero rows/cols/resolution. The previous code accepted
    # `mos.enabled=True` alone and patched the topology with
    # `mos.rows or 2`, `width or 3840` etc. — which **invented a 2x2 4K
    # configuration out of thin air** when the live state was malformed
    # (mid-transition, CLI returned partial data, etc.). The user's
    # NODE-01 capture hit this exact bug: live Mosaic was effectively
    # off (rows=0, cols=0) but flagged enabled=True, and the captured
    # profile claimed Mosaic 2x2 @ 3840x2160 @ 23.98Hz. Apply on NODE-10
    # then dutifully ran `configureMosaic.exe set rows=2 cols=2 ...`
    # against single-display hardware → rc=2 → rollback. We refuse to
    # save Mosaic as enabled unless we have a real topology in hand.
    w, h = (mos.per_display_resolution if mos else (0, 0))
    mosaic_really_on = bool(
        mos and mos.enabled and mos.rows and mos.cols and w and h
    )
    if mosaic_really_on:
        p.mosaic = ProfileMosaic(
            enabled=True,
            rows=mos.rows,
            cols=mos.cols,
            width=w,
            height=h,
            # Empty string means "leave as-is" downstream. Never default
            # to 23.976 — that silently retimed 24/25/30/60 rigs.
            refresh_rate=f"{mos.refresh_rate:.3f}" if mos.refresh_rate else "",
            outputs=[
                MosaicOutput(gpu=o.gpu, display=o.display, display_id=o.display_id)
                for o in (mos.outputs or [])
            ],
        )
    else:
        if mos and mos.enabled and not (mos.rows and mos.cols and w and h):
            logger.warning(
                "capture: mosaic.status() reports enabled=True but "
                "rows=%s cols=%s res=%sx%s — saving as disabled to avoid "
                "writing a fabricated topology into the profile.",
                mos.rows, mos.cols, w, h,
            )
        p.mosaic = ProfileMosaic(enabled=False)

    # --- DirectDisplay -------------------------------------------------
    try:
        dd = displays_mod.status()
    except Exception as exc:
        logger.warning(f"capture: displays.status failed: {exc}")
        dd = None

    dd_active = bool(dd and dd.active)
    p.direct_display = ProfileDD(
        enabled=dd_active,
        # If DirectDisplay isn't in use at capture time, mark skip_apply so
        # applying this profile LEAVES DD ALONE instead of actively running
        # `configureDdisplay return-to-desktop` — which needlessly cycles the
        # monitors on a Mosaic-only rig. Capture reproduces what's ON; it does
        # not poke subsystems you don't use. (enabled=false means "force off",
        # skip_apply=true means "don't touch" — see apply pipeline.)
        skip_apply=not dd_active,
        # Most installs use --index 1 for adapter 1; the apply pipeline
        # falls back to all-nvidia if that's wrong.
        mode="index",
        index=1,
    )

    # Capture the per-display info (color space, res, refresh) into
    # display_ids — pure-data passthrough preserved by save/load round-trip.
    if dd and dd.displays:
        p.display_ids = {
            d.id: {
                "name": d.name,
                "gpu_index": d.gpu_index,
                "display_index": d.display_index,
                "os_target_id": d.os_target_id,
                "resolution": list(d.resolution),
                "refresh_rate": d.refresh_rate,
                "refresh_fraction": d.refresh_fraction,
                "color_space": d.color_space,
                "bits_per_channel": d.bits_per_channel,
                "pixel_encoding": d.pixel_encoding,
                "connector": d.connector,
            }
            for d in dd.displays if d.id
        }
        # Exact timing (pixel clock, totals, porches) — the thing the sync
        # card actually sees. Lets an apply reproduce a custom timing on a
        # node that never had it (site 2026-11-02: the CTA 297.000 MHz 4K24/30
        # clocks will not frame-lock; 5500x2245 @ 296.34 MHz does).
        try:
            from . import timing as timing_mod
            if timing_mod.is_available():
                live = timing_mod.live_timings()
                for disp_id, info in p.display_ids.items():
                    try:
                        did = timing_mod.parse_display_id(disp_id)
                    except ValueError:
                        continue
                    if did in live:
                        pclk, ht, vt = live[did]
                        tm = timing_mod.current_timing(did, pclk_hz=pclk, h_total=ht, v_total=vt)
                        info["timing"] = tm.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"capture: timing read skipped: {exc}")

    # --- QSync ---------------------------------------------------------
    try:
        q = qsync_mod.status()
    except Exception as exc:
        logger.warning(f"capture: qsync.status failed: {exc}")
        q = None

    qsync_capable = bool(qsync_mod.is_available())
    if q and q.available:
        trigger = (q.trigger or "FALLING").upper()
        if trigger not in ("FALLING", "RISING", "BOTH"):
            trigger = "FALLING"

        # Detect daisy-chained client mode from the per-GPU Sync State
        # tokens. A node is a client only when NO GPU reports SERVER — on a
        # two-GPU server, GPU 0 is SERVER and GPU 1 is an internal CLIENT of
        # it, which the old "any CLIENT → client" rule misread as a daisy-
        # chain client (captured RENDER-01 as role=client, source="").
        role = qsync_mod.live_role_evidence(q)
        if role is None:
            # No GPU reports SERVER or CLIENT — QSync is off or unlocked
            # right now, so the live state says nothing about the node's
            # place in the chain. Keep the role of the profile this node
            # last applied (a client stays a client) rather than defaulting
            # to server, which would make Sync run `enable server` on a
            # downstream box.
            role = _role_from_active_profile() or "server"
            logger.info(
                f"capture: live QSync state carries no role — using '{role}' "
                f"from the active profile / default"
            )
        captured_displays: list[tuple[int, int]] = []
        # Capture the EXACT (gpu, display) topology as ground truth. The
        # per-GPU sync_state list only carries a GPU index, so it CANNOT
        # represent two displays on one GPU (e.g. (0,0) and (0,1)) — deriving
        # displays from it and hardcoding display=0 dropped the second head
        # and, on some driver builds, let an unresolved (-1,-1) sentinel
        # through. _discover_displays() parses the real "GPU index : N" /
        # "Display index : N" lines from configureQsync2 queryAll, which is
        # what a golden-node capture must record verbatim (nvmon-0ru).
        try:
            captured_displays = qsync_mod._discover_displays()
        except Exception as exc:
            logger.debug(f"capture: qsync display discovery failed: {exc}")
            captured_displays = []
        # Fall back to the per-GPU-with-signal list (display=0) ONLY if
        # discovery yielded nothing — never overwrite real pairs with it, and
        # never let a negative index into the profile.
        if not captured_displays:
            for g in q.per_gpu_sync or []:
                if getattr(g, "signal_available", False) and int(g.gpu) >= 0:
                    captured_displays.append((int(g.gpu), 0))

        # Source semantics: only meaningful for SERVER role. A daisy-chain
        # client doesn't have a "source" — it locks to the upstream node's
        # frame-lock pulse over RJ45. Recording HOUSESYNC on a client (as
        # the previous code did) is misleading and got flagged by the
        # operator. For server, normalize VSYNC -> HOUSESYNC (parser bucket
        # leak) and accept HOUSESYNC / INTERNAL.
        if role == "client":
            source = ""  # not applicable — empty means "from upstream"
        else:
            raw = (q.source or "HOUSESYNC").upper().replace(" ", "")
            if raw == "HOUSESYNC" or "HOUSE" in raw:
                source = "HOUSESYNC"
            elif raw == "INTERNAL":
                source = "INTERNAL"
            elif raw == "VSYNC":
                # Parser bucket — VSYNC is configureQsync2's word for
                # "internal-or-unknown". For a server we fall back to
                # HOUSESYNC since a captured server with house-sync wired
                # is the common case.
                source = "HOUSESYNC"
            else:
                source = "HOUSESYNC"

        # IMPORTANT: enabled reflects the operator's intent for this
        # profile, not the live state at capture time.  If QSync hardware
        # is present (capability available), default enabled=True so that
        # validation flags an unsynced node as a problem — that's the
        # whole point of capturing the GT profile.  The captured live
        # state (sync source/trigger/role) tells apply() which params to use.
        p.qsync = ProfileQSync(
            enabled=qsync_capable,
            source=source,
            trigger=trigger,
            grid=-1,  # auto-discover at apply time (non-Mosaic default)
            role=role,
            displays=captured_displays,
        )
    else:
        # Same idea: if the QSync exe is on this host, the operator clearly
        # plans to use QSync.  Default enabled=True even when current state
        # is empty (e.g. captured while qsync was off in NVIDIA Control
        # Panel) so the resulting profile is "QSync REQUIRED" by default.
        p.qsync = ProfileQSync(
            enabled=qsync_capable,
            source="HOUSESYNC",
            trigger="FALLING",
            grid=-1,  # auto-discover at apply time (non-Mosaic default)
            role="server",
            displays=[],
        )

    # Layer the NVWMI deep state on top — best-effort. If the NVWMI query
    # fails (no powershell, no namespace, parse error) we leave the deep
    # fields as their captured-from-configureQsync2 defaults / empty
    # sentinels. The profile still applies correctly without these; they
    # primarily power drift detection at /apply verify time.
    try:
        fl = framelock_mod.status()
    except Exception as exc:
        logger.debug(f"capture: framelock.status failed: {exc}")
        fl = None
    if fl is not None and fl.available:
        p.qsync.master_display = fl.master_display
        p.qsync.connector_0_direction = fl.connector_0_direction or ""
        p.qsync.connector_1_direction = fl.connector_1_direction or ""
        p.qsync.source_kind = fl.source_kind or ""
        # gpu_uuid_pin and sync_card_serial_pin are intentionally NOT
        # populated automatically — those are sanity gates the operator
        # opts into. Leaving them empty by default keeps profiles portable
        # across nodes with different cards. To hard-pin a profile to one
        # specific box, the operator edits these fields after capture.
    elif fl is not None and fl.error:
        logger.info(f"capture: NVWMI deep state unavailable ({fl.error}) — skipping")

    # Driver version: pulled from gpu.status() (which already reads it via
    # nvidia-smi). Auto-populated on capture so the profile carries the
    # ground-truth driver string. Drift at apply produces a warning, never
    # blocks (driver versions roll forward on patch days).
    try:
        gpus = gpu_mod.status()
    except Exception as exc:
        logger.debug(f"capture: gpu.status failed: {exc}")
        gpus = []
    if gpus:
        first_driver = (gpus[0].driver_version or "").strip()
        if first_driver:
            p.qsync.driver_version_pin = first_driver

    # Windows desktop arrangement — per-display device name, position, size,
    # refresh, primary flag. Captures the exact DEVMODE the system is
    # running so apply_arrangement can reproduce it via ChangeDisplaySettings,
    # including custom timings the EDID didn't advertise. Empty list on
    # non-Windows or when the Win32 calls fail; profile remains valid.
    try:
        p.windows_arrangement = displays_mod.capture_arrangement() or []
    except Exception as exc:
        logger.debug(f"capture: displays.capture_arrangement failed: {exc}")
        p.windows_arrangement = []

    # NVAPI color data — read live color depth / pixel encoding / colorimetry
    # for each display and stash under display_ids[id]['nvapi_color']. Apply
    # uses this on the way back to restore the exact NVCP color settings.
    # Best-effort; NVAPI absent (older driver / non-Windows) is a no-op.
    try:
        nvapi_mod.capture_color_data_for_displays(p)
    except Exception as exc:
        logger.debug(f"capture: nvapi color capture failed: {exc}")

    # --- EDID ----------------------------------------------------------
    # Capture live EDID_OVERRIDE bytes (if any are active) into the
    # profile. We defer writing the .bin file to save_current_state, which
    # has the profile name in hand — capture stashes the bytes on the
    # profile's _captured_edid_bytes attr for the saver to consume.
    try:
        live_overrides = edid_mod.read_active_edid_overrides()
    except Exception as exc:
        logger.debug(f"capture: edid.read_active_edid_overrides failed: {exc}")
        live_overrides = {}

    p.edid = ProfileEDID(file="")
    if live_overrides:
        # If multiple displays carry overrides, group by blob and keep the
        # most common — render rigs typically push the same custom EDID
        # to every monitor. If they actually differ per display, the
        # operator can split the profile or post-edit. We log the case so
        # it's visible.
        blobs: dict[bytes, list[str]] = {}
        for key, blob in live_overrides.items():
            blobs.setdefault(blob, []).append(key)

        winner_blob, winner_keys = max(
            blobs.items(), key=lambda kv: len(kv[1]),
        )
        if len(blobs) > 1:
            losers = sum(len(v) for k, v in blobs.items() if k != winner_blob)
            logger.warning(
                f"capture: {len(blobs)} distinct EDID overrides found; "
                f"saving the most common ({len(winner_keys)} displays), "
                f"{losers} display(s) with other overrides not captured"
            )
        # Stash bytes for save_current_state — needs the profile name to
        # build the file path.
        setattr(p, "_captured_edid_bytes", winner_blob)

    # --- Validation ----------------------------------------------------
    # Lock validation to the rate we ACTUALLY captured. If we have no
    # rate to work with (no displays, no mosaic, no house sync), we
    # store 0.0 — the validator interprets that as "no expectation, skip
    # the check". The legacy 23.976 fallback was a legacy-tool-era default
    # that produced wrong validations on rigs running at 24/25/30/50/60.
    expected_rate = 0.0
    if dd and dd.displays:
        rates = [d.refresh_rate for d in dd.displays if d.refresh_rate > 0]
        if rates:
            # Live displays are the most authoritative source; prefer them
            # over Mosaic's reported rate (which can lag) or QSync's house
            # frequency (which is the input clock, not necessarily what
            # the displays are actually running at).
            expected_rate = rates[0]
    if expected_rate <= 0.0 and mos and mos.refresh_rate > 0:
        expected_rate = float(mos.refresh_rate)
    if expected_rate <= 0.0 and q and q.house_sync_incoming_hz > 0:
        expected_rate = float(q.house_sync_incoming_hz)

    # Tolerance 0.01 Hz (config.RATE_MATCH_TOLERANCE_HZ): 23.976 and 24.000
    # are different timings and must not match each other; the NVIDIA tools
    # only disagree at the third decimal.
    p.validation = ProfileValidation(
        expected_refresh_rate=expected_rate,
        refresh_rate_tolerance=config.RATE_MATCH_TOLERANCE_HZ,
        expected_qsync_locked=qsync_capable,
        expected_house_sync_freq=expected_rate,
    )

    return p


def save_current_state(name: str, *, description: str = "") -> Path:
    """Capture and save the current state under ``profiles/<name>.json``.

    Returns the path of the saved file. Overwrites if the profile already
    exists (atomic write — see profiles.save).
    """
    if not name or not name.strip():
        raise ValueError("Profile name is required")
    profile = capture_current_state()
    profile.name = name.strip()
    if description:
        profile.description = description

    # Persist captured EDID bytes into BASE_DIR/Edids/. If the operator
    # already dropped a matching EDID file in there, reuse it instead of
    # writing a duplicate with the profile name. New files are saved as
    # space-separated hex text (matches the existing Node1-24-Server.txt
    # convention so the folder stays human-diffable).
    captured_bytes = getattr(profile, "_captured_edid_bytes", None)
    if captured_bytes:
        existing_rel = edid_mod.find_edid_file_matching(captured_bytes)
        if existing_rel:
            profile.edid.file = existing_rel
            log.get().info(
                f"capture: EDID override matches existing {existing_rel}"
            )
        else:
            ed_dir = edid_mod.edid_dir()
            ed_dir.mkdir(parents=True, exist_ok=True)
            edid_path = ed_dir / f"{profile.name}.txt"
            edid_path.write_text(
                " ".join(f"{b:02x}" for b in captured_bytes) + "\n",
                encoding="ascii",
            )
            try:
                rel = edid_path.relative_to(profiles_mod.config.BASE_DIR)
                rel_str = rel.as_posix()
            except ValueError:
                rel_str = str(edid_path)
            profile.edid.file = rel_str
            log.get().info(
                f"capture: wrote EDID override "
                f"({len(captured_bytes)} bytes) to {rel_str}"
            )

    return profiles_mod.save(profile)
