"""Mosaic operations — query, configure, disable.

Wraps `configureMosaic.exe`. Missing exe is handled gracefully: `status()`
returns an empty `MosaicState`, control calls return a failed `CommandResult`
without raising.
"""

from __future__ import annotations

from .. import config, executors, log
from ..parsers import parse_mosaic_query, parse_mosaic_xml_full, MosaicXMLResult
from ..types import CommandResult, DDState, MosaicState, Profile
from . import ccd


# Default output mapping when a profile's mosaic.outputs is empty.
# 2x2 grid, adapter 1 (matches nvdd's DD-aware default).
_DEFAULT_OUTPUTS = "out=1,0 out=1,1 out=1,2 out=1,3"


def capability() -> dict:
    """Report whether the Mosaic subsystem is usable.

    Returns a dict for `/health.capabilities.mosaic`.
    """
    exe = config.get_mosaic_exe()
    if not exe:
        return {
            "available": False,
            "exe": None,
            "reason": config.missing_exe_reason("configureMosaic.exe"),
        }
    return {"available": True, "exe": "configureMosaic.exe"}


def is_available() -> bool:
    return bool(config.get_mosaic_exe())


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def status(*, dry_run: bool = False) -> MosaicState:
    """Query current Mosaic state.

    Falls back to `query current` (XML) when text query reports disabled —
    text query can miss Mosaic in DD mode.
    """
    logger = log.get()

    # INTEGRATION_POINT: backends — when a backend registry exists, dispatch
    # through `backends.resolve("mosaic").status()` instead of shelling out.
    exe = config.get_mosaic_exe()
    if not exe:
        logger.debug("mosaic.status: configureMosaic.exe not found — returning empty state")
        return MosaicState()

    result = executors.run(
        exe, ["query"],
        label="mosaic-query", dry_run=dry_run, quiet=True,
    )
    if dry_run:
        return MosaicState()

    try:
        state = parse_mosaic_query(result.stdout + "\n" + result.stderr)
    except Exception as exc:
        logger.error(f"mosaic query parse failed: {exc}")
        state = MosaicState()

    if not state.enabled:
        xml_result = executors.run(
            exe, ["query", "current"],
            label="mosaic-query-current-fallback", dry_run=dry_run, quiet=True,
        )
        try:
            xml_state = parse_mosaic_query(xml_result.stdout + "\n" + xml_result.stderr)
            if xml_state.enabled:
                return xml_state
        except Exception as exc:
            logger.debug(f"mosaic fallback parse failed: {exc}")

    return state


def query_current(
    *, dd_state: DDState | None = None, dry_run: bool = False,
) -> MosaicState:
    """Query current Mosaic XML configuration."""
    logger = log.get()
    exe = config.get_mosaic_exe()
    if not exe:
        logger.debug("mosaic.query_current: configureMosaic.exe not found")
        return MosaicState()

    result = executors.run(
        exe, ["query", "current"],
        label="mosaic-query-current", dry_run=dry_run, quiet=True,
    )
    if dry_run:
        return MosaicState()

    try:
        return parse_mosaic_query(
            result.stdout + "\n" + result.stderr, dd_state=dd_state,
        )
    except Exception as exc:
        logger.error(f"mosaic query-current parse failed: {exc}")
        return MosaicState()


def query_current_full(
    *, dd_state: DDState | None = None, dry_run: bool = False,
) -> MosaicXMLResult:
    """Query Mosaic XML with all grids (primary + secondary 1x1)."""
    logger = log.get()
    exe = config.get_mosaic_exe()
    if not exe:
        logger.debug("mosaic.query_current_full: configureMosaic.exe not found")
        return MosaicXMLResult()

    result = executors.run(
        exe, ["query", "current"],
        label="mosaic-query-current-full", dry_run=dry_run, quiet=True,
    )
    if dry_run:
        return MosaicXMLResult()

    try:
        raw = result.stdout + "\n" + result.stderr
        return parse_mosaic_xml_full(raw, dd_state=dd_state)
    except Exception as exc:
        log.get().error(f"mosaic query-current-full parse failed: {exc}")
        return MosaicXMLResult()


# ---------------------------------------------------------------------------
# Control: configure / disable
# ---------------------------------------------------------------------------

def configure(
    rows: int,
    cols: int,
    width: int,
    height: int,
    refresh_rate: str,
    outputs: str,
    *,
    dry_run: bool = False,
) -> CommandResult:
    """Set Mosaic configuration.

    Args:
        rows: Grid rows (e.g. 2)
        cols: Grid columns (e.g. 2)
        width: Per-display width (e.g. 3840)
        height: Per-display height (e.g. 2160)
        refresh_rate: Refresh rate string (e.g. "23.98")
        outputs: Output mapping (e.g. "out=1,0 out=1,1 out=1,2 out=1,3")
    """
    exe = config.get_mosaic_exe()
    if not exe:
        return CommandResult(
            success=False, return_code=-1,
            stderr=config.missing_exe_reason("configureMosaic.exe"),
        )

    args = [
        "set",
        f"rows={rows}",
        f"cols={cols}",
        f"res={width},{height},{refresh_rate}",
    ]
    for part in outputs.split():
        args.append(part)

    logger = log.get()
    logger.info(
        f"Mosaic: configuring {rows}x{cols} @ {width}x{height},{refresh_rate}Hz"
    )

    result = executors.run(exe, args, label="mosaic-set", dry_run=dry_run)

    if result.success:
        logger.info("Mosaic: configured successfully")
        executors.sleep(config.MOSAIC_STABILIZE_TIME, "mosaic topology stabilization")
    else:
        # configureMosaic reports WHY on stdout (rc=2, empty stderr) —
        # "Mosaic configure failed: " with nothing after it was the smoking
        # gun on SRV01 for a week (the legacy tool 3363a11).
        logger.error(f"Mosaic configure failed: {executors.failure_detail(result)}")

    return result


def test_timings(
    rows: int,
    cols: int,
    *,
    outputs: str = "",
    width: int = 0,
    height: int = 0,
    refresh_rate: str = "",
    dry_run: bool = False,
) -> dict:
    """Dry-run a grid with ``configureMosaic test`` — nothing is applied.

    Returns ``{"command", "success", "valid", "rate_hz", "output"}``.
    ``rate_hz`` is the refresh the driver would actually pick for the
    requested mode (asked 30 → ``rr="29.970000"``), ``valid`` whether the
    exact mode is accepted. Used as a preflight before blanking a wall:
    on SRV02 the Helios panels offered 23.976 / 29.97 / 59.94 / 60 at
    3840x2160 natively, and 24.000 / 30.000 needed custom timings — the
    dry run says so in seconds instead of after a 2-minute apply.
    """
    import re

    exe = config.get_mosaic_exe()
    if not exe:
        return {"command": "", "success": False, "valid": False, "rate_hz": None,
                "output": config.missing_exe_reason("configureMosaic.exe")}

    args = ["test", f"rows={rows}", f"cols={cols}"]
    for part in (outputs or "").split():
        args.append(part)
    if width and height:
        res = f"res={width},{height}"
        if refresh_rate:
            res += f",{refresh_rate}"
        args.append(res)

    result = executors.run(exe, args, label="mosaic-test", dry_run=dry_run, quiet=True)
    output = (result.stdout + "\n" + result.stderr).strip()
    rr = re.search(r'rr="([\d.]+)"', output)
    valid = re.search(r'valid="(\d)"', output)
    return {
        "command": " ".join(args),
        "success": bool(result.success),
        "valid": bool(valid and valid.group(1) == "1"),
        "rate_hz": float(rr.group(1)) if rr else None,
        "output": output,
    }


def test_timings_from_profile(profile: Profile, *, dry_run: bool = False) -> dict:
    """``test_timings`` with exactly the grid + mode a profile would apply."""
    m = profile.mosaic
    outputs = " ".join(f"out={o.gpu},{o.display}" for o in m.outputs) or _DEFAULT_OUTPUTS
    return test_timings(
        m.rows, m.cols, outputs=outputs,
        width=m.width, height=m.height, refresh_rate=str(m.refresh_rate),
        dry_run=dry_run,
    )


def configure_from_profile(profile: Profile, *, dry_run: bool = False) -> CommandResult:
    """Set Mosaic configuration from a Profile."""
    m = profile.mosaic
    outputs = " ".join(f"out={o.gpu},{o.display}" for o in m.outputs)
    if not outputs:
        outputs = _DEFAULT_OUTPUTS

    return configure(
        rows=m.rows,
        cols=m.cols,
        width=m.width,
        height=m.height,
        refresh_rate=m.refresh_rate,
        outputs=outputs,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Per-display mode set (no Mosaic surface)
# ---------------------------------------------------------------------------
#
# For profiles that explicitly disable Mosaic but still need a specific
# resolution + refresh on each display (the user's "no mosaic, no DD, 24Hz"
# case), we use 1x1 Mosaic grids as the underlying mode-set engine.
# NVIDIA represents a non-Mosaic single display as a `<grid rows="1"
# cols="1">` in `configureMosaic.exe query current`, and `set rows=1
# cols=1 ...` is the supported way to drive that grid's display mode via
# the same CLI we already wrap. The resulting state still parses as
# `mosaic.enabled=False` (parsers.py:256 only flags `enabled=True` for
# rows>1 or cols>1), so the profile-level intent ("no mosaic") and the
# live state stay consistent.
#
# ONE `set` call for ALL displays. From the configureMosaic readme: "The
# 'set' command will apply one or more display grids. The list is
# exhaustive, so any display not given in one of the grids will be
# deactivated." The first version of this code issued one `set rows=1
# cols=1 ... out=0,0` per display — on RENDER-01 (two GPUs, one panel
# each) that kicked the GPU-1 panel off the desktop every apply and the
# operator had to re-extend it in Windows Display settings (2026-11-01).
# Every display that should stay lit is now its own grid, joined with
# `nextgrid`, in a single invocation. The first grid becomes the GDI
# primary, so the display that is primary NOW is listed first.

def _grid_plan_from_profile(profile: Profile, live) -> tuple[list[dict], int, int]:
    """Decide the 1x1 grids a per-display mode set must contain.

    Returns ``(grids, n_changed, n_already)``. Each grid is a dict with
    ``id, gpu, display, width, height, rate, changed``. Every display on the
    desktop ends up in the list — profile targets at their target mode, any
    other active display at its current mode — because a display missing
    from the ``set`` is switched off.
    """
    logger = log.get()
    targets = profile.display_ids or {}

    live_by_id: dict[str, object] = {}
    if live is not None and getattr(live, "displays", None):
        for d in live.displays:
            if d.id:
                live_by_id[d.id] = d

    # Fallback rate for a target whose refresh read failed at capture time
    # (the hex-target-ID parser gap left the GPU-1 panel at rr=0.0 in the
    # first site profiles). The profile-wide expected rate is what the
    # operator meant for every panel.
    try:
        fallback_rate = float(profile.validation.expected_refresh_rate or 0.0)
    except (TypeError, ValueError):
        fallback_rate = 0.0
    # 23.976 is NOT "already at 24" — see config.RATE_MATCH_TOLERANCE_HZ.
    rate_tol = config.rate_tolerance(getattr(profile.validation, "refresh_rate_tolerance", None))
    # Exact live timings (pixel clock, totals) for entries that carry a
    # ``timing`` block: same rate on a different pixel clock IS a change.
    live_timing: dict[int, tuple[int, int, int]] = {}
    if any(isinstance(i, dict) and isinstance(i.get("timing"), dict) for i in targets.values()):
        try:
            from . import timing as timing_mod
            live_timing = timing_mod.live_timings()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"set_modes: live timings unavailable: {exc}")

    grids: list[dict] = []
    n_changed = 0
    n_already = 0
    seen: set[str] = set()

    for disp_id, info in targets.items():
        if not isinstance(info, dict):
            continue
        live_d = live_by_id.get(disp_id)

        res = info.get("resolution") or [0, 0]
        try:
            width = int(res[0]) if isinstance(res, (list, tuple)) and len(res) >= 1 else 0
            height = int(res[1]) if isinstance(res, (list, tuple)) and len(res) >= 2 else 0
        except (TypeError, ValueError):
            width, height = 0, 0
        if not (width and height) and live_d is not None:
            width, height = live_d.resolution

        try:
            rate = float(info.get("refresh_rate") or 0.0)
        except (TypeError, ValueError):
            rate = 0.0
        if rate <= 0:
            if fallback_rate > 0:
                rate = fallback_rate
                logger.info(
                    f"set_modes: display {disp_id} has no captured refresh — "
                    f"using the profile's expected rate {rate:.3f}Hz"
                )
            elif live_d is not None and float(live_d.refresh_rate or 0) > 0:
                rate = float(live_d.refresh_rate)

        gpu = int(info.get("gpu_index") if info.get("gpu_index") is not None else -1)
        display = int(info.get("display_index") if info.get("display_index") is not None else -1)
        if (gpu < 0 or display < 0) and live_d is not None:
            gpu, display = int(live_d.gpu_index), int(live_d.display_index)
        if not str(disp_id).strip() and (gpu < 0 or display < 0):
            logger.warning("set_modes: display entry has neither an ID nor GPU/display index — skipped")
            continue

        changed = True
        res_changed = False
        if live_d is not None and rate > 0:
            live_w, live_h = live_d.resolution
            live_rr = float(live_d.refresh_rate or 0)
            res_changed = (live_w, live_h) != (width, height)
            if not res_changed and abs(live_rr - rate) <= rate_tol:
                changed = False
        timing_block = info.get("timing") if isinstance(info.get("timing"), dict) else None
        timing_changed = False
        if timing_block and not changed:
            try:
                want = (int(timing_block.get("pclk_hz") or 0), int(timing_block.get("h_total") or 0),
                        int(timing_block.get("v_total") or 0))
                from . import timing as timing_mod
                have = live_timing.get(timing_mod.parse_display_id(disp_id))
                if all(want) and have is not None and tuple(have) != want:
                    changed = timing_changed = True
                    logger.info(
                        f"Display {disp_id}: rate matches but timing differs — live pclk {have[0]} "
                        f"{have[1]}x{have[2]}, profile pclk {want[0]} {want[1]}x{want[2]}"
                    )
            except (TypeError, ValueError):
                pass
        if changed:
            n_changed += 1
        else:
            n_already += 1
            logger.info(
                f"Display {disp_id}: already at {width}x{height} @ {rate:.3f}Hz"
            )

        target_id = str(info.get("os_target_id") or getattr(live_d, "os_target_id", "") or "").strip()
        grids.append({
            "id": disp_id, "gpu": gpu, "display": display,
            "width": width, "height": height, "rate": rate, "changed": changed,
            # For the exact-refresh path (ccd.set_refresh): Windows target id
            # and the vSync rational Windows reported at capture time.
            "target_id": target_id,
            "fraction": str(info.get("refresh_fraction") or ""),
            "res_changed": res_changed,
            # Exact timing (NVAPI custom display path) when the profile has one.
            "timing": timing_block,
            "timing_changed": timing_changed,
        })
        seen.add(disp_id)

    # Displays the profile never captured (operator/KVM monitor, a head
    # added later) stay at their current mode — but they MUST be in the
    # list or configureMosaic switches them off.
    if live is not None:
        for d in getattr(live, "displays", None) or []:
            if not d.id or d.id in seen:
                continue
            if "desktop" not in (d.status or "").lower():
                continue
            logger.info(
                f"set_modes: keeping {d.name or d.id} (GPU {d.gpu_index} display "
                f"{d.display_index}) at its current mode — not in profile"
            )
            grids.append({
                "id": d.id, "gpu": int(d.gpu_index), "display": int(d.display_index),
                "width": int(d.resolution[0]), "height": int(d.resolution[1]),
                "rate": float(d.refresh_rate or 0), "changed": False,
            })

    # First grid = GDI primary. Keep whichever display is primary now
    # (Windows reports it at the desktop origin); stable sort keeps profile
    # order for the rest.
    def _is_primary(g: dict) -> bool:
        live_d = live_by_id.get(g["id"])
        status = (getattr(live_d, "status", "") or "").replace(" ", "")
        return "@(0,0)" in status

    grids.sort(key=lambda g: 0 if _is_primary(g) else 1)
    return grids, n_changed, n_already


def _grid_args(grids: list[dict]) -> list[str]:
    """Build the ``set`` argument list.

    Displays are addressed by NVAPI display ID (``display=0x82061085``),
    which is what ``query current`` prints and what ``display_ids`` keys
    are. ``out=GPU,OUTPUT`` wants the GPU's *output* index; the DD table's
    display index is a global running number (0, 1) that only coincides
    with it on GPU 0. Verified with ``configureMosaic test`` on RENDER-01
    and -03 (two grids, 23.976, valid=1) on 2026-11-01.
    """
    args = ["set"]
    for i, g in enumerate(grids):
        if i:
            args.append("nextgrid")
        args += ["rows=1", "cols=1"]
        if g["width"] and g["height"]:
            res = f"res={g['width']},{g['height']}"
            if g["rate"] > 0:
                res += f",{g['rate']:.3f}"
            args.append(res)
        did = str(g.get("id") or "").strip().lower()
        if did:
            if not did.startswith("0x"):
                did = "0x" + did
            args.append(f"display={did}")
        else:
            args.append(f"out={g['gpu']},{g['display']}")
    return args


def _set_timing_via_nvapi(grids: list[dict]) -> CommandResult | None:
    """Exact-timing path: every changing display that carries a ``timing``
    block is driven through NVAPI custom displays (TryCustomDisplay →
    verify pixel clock/totals via CCD → SaveCustomDisplay). This both
    creates the timing on a node that never had it and selects it, so the
    sync card sees the exact pixel clock the profile was built with.
    Returns None when no changing display has a timing block (caller falls
    through to the Windows / configureMosaic paths); a failed CommandResult
    when NVAPI refuses or the driver lands elsewhere (the trial is reverted).
    """
    logger = log.get()
    changed = [g for g in grids if g["changed"]]
    if not changed or not all(isinstance(g.get("timing"), dict) for g in changed):
        return None
    try:
        from . import timing as timing_mod
    except Exception:  # noqa: BLE001
        return None
    if not timing_mod.is_available():
        logger.info("Display: profile carries exact timings but NVAPI is unavailable here — using the rate path")
        return None
    report: list[str] = []
    for g in changed:
        try:
            tm = timing_mod.Timing.from_dict(g["timing"])
            did = timing_mod.parse_display_id(g["id"])
        except (TypeError, ValueError) as exc:
            return CommandResult(success=False, return_code=-1,
                                 stderr=f"display {g['id']}: bad timing block in profile ({exc})")
        problems = [p for p in tm.validate() if "will not frame-lock" not in p]
        if problems:
            return CommandResult(success=False, return_code=-1,
                                 stderr=f"display {g['id']}: timing invalid: {'; '.join(problems)}")
        try:
            res = timing_mod.apply_custom_timing([did], tm, save=True)
        except (ValueError, timing_mod.TimingUnavailable) as exc:
            msg = f"display {g['id']}: NVAPI custom timing failed: {exc}"
            logger.error(f"Display: {msg}")
            return CommandResult(success=False, return_code=-1, stderr=msg)
        if res.get("mismatch"):
            msg = (f"display {g['id']}: driver did not program {tm.h_total}x{tm.v_total} @ "
                   f"{tm.pclk_hz / 1e6:.3f} MHz ({res['mismatch']})")
            logger.error(f"Display: {msg}")
            return CommandResult(success=False, return_code=-1, stderr=msg)
        report.append(f"{g['id']}: {tm.h_total}x{tm.v_total} @ {tm.pclk_hz / 1e6:.3f} MHz = {tm.exact_hz:.6f} Hz"
                      + (" (saved)" if res.get("saved") else ""))
    logger.info("Display: exact timings set via NVAPI — " + "; ".join(report))
    return CommandResult(success=True, return_code=0, stdout="NVAPI custom timing: " + "; ".join(report))


def _set_refresh_via_ccd(grids: list[dict], rate_tol: float) -> CommandResult | None:
    """Refresh-only changes go through Windows (``ccd.set_refresh``) first.

    configureMosaic cannot tell 24.000 from 23.976 (it buckets both as
    "24" and always resolves to 23.976 on the site Tessera SX40 panels),
    while Windows picks the exact ``24/1`` vs ``23976/1000`` mode. Used
    when every display that must change keeps its resolution and has a
    Windows target id; anything else (resolution change, no id, non-
    Windows host, Windows refusing the mode) returns ``None`` and the
    caller runs configureMosaic as before.

    Success here only means Windows accepted the request — the caller still
    re-reads the displays and fails the set if the landing rate is wrong.
    """
    logger = log.get()
    changed = [g for g in grids if g["changed"]]
    if not changed or not ccd.is_available():
        return None
    if any(g["res_changed"] or not g.get("target_id") or g["rate"] <= 0 for g in changed):
        return None
    targets: dict[int, list[tuple[int, int]]] = {}
    for g in changed:
        try:
            tid = int(str(g["target_id"]), 16)
        except ValueError:
            return None
        cands = ccd.rational_candidates(g["rate"], g.get("fraction", ""))
        if not cands:
            return None
        targets[tid] = cands
    desc = "; ".join(
        f"display {g['id']} (target 0x{int(str(g['target_id']), 16):x}) → "
        + " | ".join(f"{n}/{d}" for n, d in targets[int(str(g['target_id']), 16)])
        for g in changed
    )
    logger.info(f"Display: setting {len(changed)} refresh rate(s) via Windows SetDisplayConfig — {desc}")
    try:
        got = ccd.set_refresh(targets)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Display: SetDisplayConfig path failed ({exc}) — using configureMosaic")
        return None
    if got is None:
        return None
    report = ", ".join(f"0x{t:x}: {hz:.3f}Hz" for t, hz in got.items())
    return CommandResult(success=True, return_code=0, stdout=f"SetDisplayConfig: {report}")


def set_modes_from_profile(
    profile: Profile,
    *,
    dry_run: bool = False,
) -> tuple[int, int, list[CommandResult]]:
    """Push per-display modes from `profile.display_ids` to live displays.

    Builds ONE ``configureMosaic set`` with a 1x1 grid per display (see the
    module comment above — an unlisted display gets deactivated). Displays
    already at their target (resolution exact, refresh within
    ``config.rate_tolerance()`` — 0.01 Hz, so 23.976 is not "at 24") are
    still listed, at the same mode. Nothing runs when every target already
    matches.

    Returns:
        (n_attempted, n_skipped, results) — `n_attempted` is the number of
        displays whose mode changes, `n_skipped` those already correct,
        `results` holds the single CommandResult when a set was issued.
    """
    from . import displays as displays_mod  # local import: avoid circular
    logger = log.get()

    targets = profile.display_ids or {}
    if not targets:
        return 0, 0, []

    exe = config.get_mosaic_exe()
    if not exe:
        return 0, 0, [CommandResult(
            success=False, return_code=-1, stderr=config.missing_exe_reason("configureMosaic.exe"),
        )]

    # Snapshot live state once.
    try:
        live = displays_mod.status()
    except Exception as exc:
        logger.warning(f"set_modes_from_profile: displays.status failed: {exc}")
        live = None

    grids, n_changed, n_already = _grid_plan_from_profile(profile, live)
    if not grids:
        return 0, 0, []
    if n_changed == 0:
        logger.info("Display modes: every display already at target — nothing to push")
        return 0, n_already, []

    desc = "; ".join(
        f"GPU {g['gpu']} display {g['display']} → {g['width']}x{g['height']}"
        + (f" @ {g['rate']:.3f}Hz" if g["rate"] > 0 else "")
        + ("" if g["changed"] else " (unchanged)")
        for g in grids
    )
    rate_tol = config.rate_tolerance(getattr(profile.validation, "refresh_rate_tolerance", None))

    def _run_mosaic_set() -> CommandResult:
        logger.info(f"Display: setting {len(grids)} display(s) in one call — {desc}")
        return executors.run(exe, _grid_args(grids), label="display-set-mode", dry_run=dry_run)

    def _landing_check(result: CommandResult) -> tuple[CommandResult, bool]:
        """Re-read the displays; (result, landed_ok). Re-attaches dropped
        displays and turns a wrong landing rate into a failed result."""
        logger.info(f"Display: {n_changed} mode(s) set, {len(grids)} display(s) kept active")
        executors.sleep(config.MOSAIC_STABILIZE_TIME, "display mode stabilization")
        if dry_run:
            return result, True
        try:
            after = displays_mod.status()
            wanted = {g["id"] for g in grids}
            lost = [
                d for d in (after.displays or [])
                if d.id in wanted and "desktop" not in (d.status or "").lower()
            ]
            if lost:
                # Belt and braces: a listed display that still dropped off the
                # desktop is re-attached through Windows rather than leaving
                # the operator with a "Show only on 1" wall.
                logger.warning(
                    f"Display: {len(lost)} display(s) left the desktop after the "
                    f"mode set — re-enabling"
                )
                displays_mod.ensure_all_active()
                after = displays_mod.status()
            # The engines answer "success" and then land on whatever timing
            # they matched: configureMosaic picks the NEAREST (asked 24.000 →
            # 23.976 on every site Tessera SX40), Windows with ALLOW_CHANGES
            # substituted 30 Hz for a 60000/1001 request (2026-11-02). A wrong
            # landing rate is a failed set, never a silent "applied".
            after_by_id = {d.id: d for d in (after.displays or []) if d.id}
            # Exact check when the profile carries timings: same rate on the
            # wrong pixel clock (297.000 MHz) is the whole site story.
            exact_live: dict[int, tuple[int, int, int]] = {}
            if any(g.get("timing") for g in grids if g["changed"]):
                try:
                    from . import timing as timing_mod
                    exact_live = timing_mod.live_timings()
                except Exception:  # noqa: BLE001
                    exact_live = {}
            wrong: list[str] = []
            for g in grids:
                if not g["changed"] or g["rate"] <= 0:
                    continue
                d = after_by_id.get(g["id"])
                got = float(getattr(d, "refresh_rate", 0) or 0) if d is not None else 0.0
                if got > 0 and abs(got - g["rate"]) > rate_tol:
                    wrong.append(f"display {g['id']} asked {g['rate']:.3f}Hz, driver picked {got:.3f}Hz")
                    continue
                tb = g.get("timing") or {}
                if tb and exact_live:
                    try:
                        from . import timing as timing_mod
                        want = (int(tb.get("pclk_hz") or 0), int(tb.get("h_total") or 0), int(tb.get("v_total") or 0))
                        have = exact_live.get(timing_mod.parse_display_id(g["id"]))
                    except (TypeError, ValueError):
                        want, have = (0, 0, 0), None
                    if all(want) and have is not None and tuple(have) != want:
                        wrong.append(
                            f"display {g['id']} asked {want[1]}x{want[2]} @ {want[0] / 1e6:.3f} MHz, "
                            f"driver programmed {have[1]}x{have[2]} @ {have[0] / 1e6:.3f} MHz"
                        )
                        continue
                    # Windows/CCD reports the requested timing; the driver's
                    # own mode record (NvAPI_DISP_GetTiming) is what runs.
                    # site 2026-11-04: CCD said 4400x2250 @ 594.000 while the
                    # driver ran 593.410 (59.94) on nodes 2 and 3 — the sync
                    # card measured 59.940 and the chain never locked.
                    if all(want):
                        try:
                            rec = timing_mod.get_timing(timing_mod.parse_display_id(g["id"]))
                            rec_t = (int(getattr(rec, "pclk_hz", 0) or 0), int(getattr(rec, "h_total", 0) or 0),
                                     int(getattr(rec, "v_total", 0) or 0))
                        except Exception:  # noqa: BLE001
                            rec_t = (0, 0, 0)
                        if all(rec_t) and (rec_t[1:] != want[1:] or abs(rec_t[0] - want[0]) > want[0] * 0.0002):
                            wrong.append(
                                f"display {g['id']} asked {want[1]}x{want[2]} @ {want[0] / 1e6:.3f} MHz, the "
                                f"driver's mode record runs {rec_t[1]}x{rec_t[2]} @ {rec_t[0] / 1e6:.3f} MHz "
                                f"({rec_t[0] / (rec_t[1] * rec_t[2]):.3f} Hz)"
                            )
            if wrong:
                msg = (
                    "; ".join(wrong)
                    + " — this timing isn't offered by the panel (check with "
                    "/mosaic/timings; on Tessera panels set the processor's input "
                    "frame rate / EDID to the wanted rate, or add a custom timing)"
                )
                logger.error(f"Display set-mode landed at the wrong rate: {msg}")
                return CommandResult(
                    success=False, return_code=result.return_code,
                    stdout=result.stdout, stderr=msg,
                ), False
        except executors.Cancelled:
            raise
        except Exception as exc:
            logger.debug(f"set_modes: post-set check failed: {exc}")
        return result, True

    result = None
    if not dry_run:
        result = _set_timing_via_nvapi(grids)
        if result is not None and not result.success:
            # NVAPI refused or the driver landed elsewhere. For a timing that
            # the driver also offers natively (e.g. CTA 4K60 at 594 MHz —
            # srv5 2026-11-02) the Windows rate path reaches the same exact
            # timing, so fall through and let the landing check judge pclk +
            # totals. A poison clock never gets that fallback: a 297.000 MHz
            # landing would be the lock failure all over again.
            poison = False
            try:
                from . import timing as timing_mod
                poison = any(int((g.get("timing") or {}).get("pclk_hz") or 0) in timing_mod.POISON_PIXEL_CLOCKS_HZ
                             for g in grids if g["changed"] and g.get("timing"))
            except Exception:  # noqa: BLE001
                poison = False
            if poison:
                logger.error(f"Display set-mode failed: {executors.failure_detail(result)}")
                return n_changed, n_already, [result]
            logger.warning(f"Display: NVAPI timing path failed ({executors.failure_detail(result)}) — "
                           f"falling back to the Windows refresh path")
            result = None
        if result is None:
            result = _set_refresh_via_ccd(grids, rate_tol)
    used_ccd = result is not None
    if result is None:
        result = _run_mosaic_set()
    if result.success:
        result, landed = _landing_check(result)
        if not landed and used_ccd:
            # Windows put a panel on the wrong timing — give the NVIDIA tool
            # one shot at the same request before reporting failure.
            logger.warning("Display: Windows path landed wrong — retrying the set with configureMosaic")
            retry = _run_mosaic_set()
            if retry.success:
                result, _ = _landing_check(retry)
            else:
                logger.error(f"Display set-mode failed: {executors.failure_detail(retry)}")
                result = retry
    else:
        logger.error(f"Display set-mode failed: {executors.failure_detail(result)}")
    return n_changed, n_already, [result]


def disable(*, dry_run: bool = False) -> CommandResult:
    """Disable Mosaic and re-enable individual displays.

    After `mosaic disable` the individual displays that were part of the
    Mosaic surface become detached from the Windows desktop. We call
    `displays.ensure_all_active()` afterward so subsequent operations
    find them.
    """
    exe = config.get_mosaic_exe()
    if not exe:
        return CommandResult(
            success=False, return_code=-1,
            stderr=config.missing_exe_reason("configureMosaic.exe"),
        )

    logger = log.get()

    # Short-circuit if Mosaic is already off
    if not dry_run:
        current = status(dry_run=False)
        if not current.enabled:
            logger.info("Mosaic: already disabled, skipping")
            return CommandResult(success=True, return_code=0, stdout="Already disabled")

    logger.info("Mosaic: disabling")
    result = executors.run(exe, ["disable"], label="mosaic-disable", dry_run=dry_run)
    executors.sleep(config.MOSAIC_DISABLE_WAIT, "mosaic disable settle")

    if result.success:
        logger.info("Mosaic: disabled")
    else:
        logger.warning(
            f"Mosaic disable returned error (may already be disabled): {executors.failure_detail(result)}"
        )

    # Re-enable individual monitors that were part of the Mosaic surface.
    if not dry_run:
        logger.info("Mosaic: re-enabling individual displays after disable")
        try:
            from . import displays
            displays.ensure_all_active()
        except Exception as exc:
            logger.warning(f"Post-disable display reactivation failed: {exc}")

    return result


def test_config(rows: int, cols: int, *, dry_run: bool = False) -> CommandResult:
    """Test which modes are supported for a given grid."""
    exe = config.get_mosaic_exe()
    if not exe:
        return CommandResult(
            success=False, return_code=-1,
            stderr=config.missing_exe_reason("configureMosaic.exe"),
        )

    return executors.run(
        exe, ["test", f"rows={rows}", f"cols={cols}"],
        label="mosaic-test", dry_run=dry_run,
    )
