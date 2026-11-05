"""Windows CCD (QueryDisplayConfig / SetDisplayConfig) helpers.

Two jobs, both ported from the legacy tool nvdd fork after a week of broken
LED walls (mb-5it, 2026-10-06/07):

``describe()``
    One record per active display path with the **desktop** size (source
    mode) and the **signal on the wire** (target mode: active size +
    vertical sync). When target != source, Windows is scaling the desktop —
    e.g. a 7680x4320 Mosaic desktop sent as 10240x5760@60 because Windows
    re-picked the panel's EDID-preferred timing. configureMosaic still
    reports the Mosaic as 3840x2160@23.976 in that state, so this is the
    only place the scaling shows up. The validator's ``output_signal``
    check reads this.

``make_origin()``
    Put one display at (0,0) = primary **without re-choosing any mode**.
    ``ChangeDisplaySettingsEx`` re-commits each display from a DEVMODE and
    lets Windows pick the monitor's preferred timing; ``SetDisplayConfig``
    with the complete current configuration and only the source positions
    shifted keeps every target mode (verified by hand on SRV02: wall stayed
    7680x4320@60). This is what Windows' own "Make this my main display"
    does. Needed because DirectDisplay cannot take the primary display.

Non-Windows hosts: every call returns an empty/False result; nothing raises
at import time.
"""

from __future__ import annotations

import ctypes
import sys

from .. import log

QDC_ONLY_ACTIVE_PATHS = 0x2
SDC_VALIDATE = 0x40
SDC_APPLY = 0x80
SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x20
SDC_SAVE_TO_DATABASE = 0x200
SDC_ALLOW_CHANGES = 0x400
SCANLINE_PROGRESSIVE = 1
MODE_SOURCE = 1
MODE_TARGET = 2
DEVICE_INFO_GET_TARGET_NAME = 2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
_INVALID_IDX = 0xFFFFFFFF

_IS_WINDOWS = sys.platform.startswith("win")

if _IS_WINDOWS:
    import ctypes.wintypes as wt

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

    class RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]

    class REGION(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_uint32), ("cy", ctypes.c_uint32)]

    class PATH_SOURCE_INFO(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32),
                    ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]

    class PATH_TARGET_INFO(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                    ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                    ("scaling", ctypes.c_uint32), ("refreshRate", RATIONAL),
                    ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wt.BOOL),
                    ("statusFlags", ctypes.c_uint32)]

    class PATH_INFO(ctypes.Structure):
        _fields_ = [("sourceInfo", PATH_SOURCE_INFO), ("targetInfo", PATH_TARGET_INFO),
                    ("flags", ctypes.c_uint32)]

    class VIDEO_SIGNAL_INFO(ctypes.Structure):
        _fields_ = [("pixelRate", ctypes.c_uint64), ("hSyncFreq", RATIONAL), ("vSyncFreq", RATIONAL),
                    ("activeSize", REGION), ("totalSize", REGION),
                    ("videoStandard", ctypes.c_uint32), ("scanLineOrdering", ctypes.c_uint32)]

    class SOURCE_MODE(ctypes.Structure):
        _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                    ("pixelFormat", ctypes.c_uint32), ("position", wt.POINTL)]

    class _MODE_UNION(ctypes.Union):
        _fields_ = [("targetMode", VIDEO_SIGNAL_INFO), ("sourceMode", SOURCE_MODE),
                    ("_pad", ctypes.c_byte * 48)]

    class MODE_INFO(ctypes.Structure):
        _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32),
                    ("adapterId", LUID), ("u", _MODE_UNION)]

    class DEVICE_INFO_HEADER(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32),
                    ("adapterId", LUID), ("id", ctypes.c_uint32)]

    class TARGET_DEVICE_NAME(ctypes.Structure):
        _fields_ = [("header", DEVICE_INFO_HEADER), ("flags", ctypes.c_uint32),
                    ("outputTechnology", ctypes.c_uint32), ("edidManufactureId", ctypes.c_uint16),
                    ("edidProductCodeId", ctypes.c_uint16), ("connectorInstance", ctypes.c_uint32),
                    ("monitorFriendlyDeviceName", wt.WCHAR * 64), ("monitorDevicePath", wt.WCHAR * 128)]

    # Layout checks against the Win32 headers — a wrong size would silently
    # corrupt the config we hand back to SetDisplayConfig.
    assert ctypes.sizeof(PATH_INFO) == 72, ctypes.sizeof(PATH_INFO)
    assert ctypes.sizeof(MODE_INFO) == 64, ctypes.sizeof(MODE_INFO)
    assert ctypes.sizeof(TARGET_DEVICE_NAME) == 420, ctypes.sizeof(TARGET_DEVICE_NAME)

    def _user32():
        return ctypes.WinDLL("user32", use_last_error=True)


def is_available() -> bool:
    return _IS_WINDOWS


def query():
    """Active paths + modes as ctypes arrays: (paths, n_paths, modes, n_modes)."""
    if not _IS_WINDOWS:
        raise OSError("CCD is Windows-only")
    u = _user32()
    for _ in range(3):  # the config can change between the size query and the query
        n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
        rc = u.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes))
        if rc != ERROR_SUCCESS:
            raise OSError(f"GetDisplayConfigBufferSizes failed ({rc})")
        paths = (PATH_INFO * n_paths.value)()
        modes = (MODE_INFO * n_modes.value)()
        rc = u.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                  ctypes.byref(n_modes), modes, None)
        if rc == ERROR_SUCCESS:
            return paths, n_paths.value, modes, n_modes.value
        if rc != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(f"QueryDisplayConfig failed ({rc})")
    raise OSError("QueryDisplayConfig kept changing size")


def _target_name(path) -> str:
    t = TARGET_DEVICE_NAME()
    t.header.type = DEVICE_INFO_GET_TARGET_NAME
    t.header.size = ctypes.sizeof(TARGET_DEVICE_NAME)
    t.header.adapterId = path.targetInfo.adapterId
    t.header.id = path.targetInfo.id
    if _user32().DisplayConfigGetDeviceInfo(ctypes.byref(t)) == ERROR_SUCCESS:
        return t.monitorFriendlyDeviceName
    return ""


def describe() -> list[dict]:
    """One dict per active path.

    ``{"name", "source": (w,h)|None, "position": (x,y)|None,
       "target": (w,h)|None, "hz": float}``

    ``source`` is the desktop size, ``target`` the signal actually sent.
    Returns ``[]`` on non-Windows hosts or when the query fails (logged at
    DEBUG — this runs every poll, don't spam).
    """
    if not _IS_WINDOWS:
        return []
    try:
        paths, n_paths, modes, _ = query()
    except Exception as exc:  # noqa: BLE001
        log.get().debug(f"ccd.describe failed: {exc}")
        return []
    out: list[dict] = []
    for i in range(n_paths):
        p = paths[i]
        d: dict = {"name": _target_name(p), "target_id": f"{int(p.targetInfo.id):x}",
                   "source": None, "position": None, "target": None, "hz": 0.0,
                   # Exact timing the driver programs — what the RTX PRO Sync
                   # card actually compares against the house signal (±50 ppm,
                   # RTX PRO Sync User Guide v09 ch. 9). "hz" is the label.
                   "vsync": "", "pixel_rate_hz": 0, "h_total": 0, "v_total": 0, "exact_hz": 0.0}
        si, ti = p.sourceInfo.modeInfoIdx, p.targetInfo.modeInfoIdx
        if si != _INVALID_IDX and modes[si].infoType == MODE_SOURCE:
            sm = modes[si].u.sourceMode
            d["source"] = (int(sm.width), int(sm.height))
            d["position"] = (int(sm.position.x), int(sm.position.y))
        if ti != _INVALID_IDX and modes[ti].infoType == MODE_TARGET:
            tm = modes[ti].u.targetMode
            d["target"] = (int(tm.activeSize.cx), int(tm.activeSize.cy))
            v = tm.vSyncFreq
            d["hz"] = round(v.Numerator / v.Denominator, 3) if v.Denominator else 0.0
            d["vsync"] = f"{int(v.Numerator)}/{int(v.Denominator)}"
            d["pixel_rate_hz"] = int(tm.pixelRate)
            d["h_total"], d["v_total"] = int(tm.totalSize.cx), int(tm.totalSize.cy)
            if d["pixel_rate_hz"] and d["h_total"] and d["v_total"]:
                d["exact_hz"] = round(d["pixel_rate_hz"] / (d["h_total"] * d["v_total"]), 6)
        out.append(d)
    return out


def ppm(display_hz: float, reference_hz: float) -> float:
    """Timing error in parts per million, per the RTX PRO Sync User Guide:
    PPM = abs(Display - Reference) / Reference * 1e6. The card locks only
    within ±50 ppm."""
    if not display_hz or not reference_hz:
        return 0.0
    return abs(display_hz - reference_hz) / reference_hz * 1e6


def rational_candidates(rate: float, fraction: str = "") -> list[tuple[int, int]]:
    """Every spelling of a refresh rate Windows might list, best first.

    The NVIDIA driver labels the 1.001 rates as milli-hertz over 1000 on
    the site panels (``59940/1000``, ``23976/1000``) — asking for
    ``60000/1001`` fails strict validation and, with SDC_ALLOW_CHANGES, left
    a host panel at 30 Hz (2026-11-02 16:54). Order: the fraction captured
    from this very system (when it agrees with ``rate``), then mHz/1000,
    then N*1000/1001, then N/1 for integer rates.
    """
    try:
        rate = float(rate or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    out: list[tuple[int, int]] = []

    def _add(n: int, d: int) -> None:
        if n > 0 and d > 0 and (n, d) not in out:
            out.append((int(n), int(d)))

    if fraction and "/" in str(fraction):
        try:
            n, d = (int(x.strip()) for x in str(fraction).split("/", 1))
            if n > 0 and d > 0 and (rate <= 0 or abs(n / d - rate) < 0.01):
                _add(n, d)
                if rate <= 0:
                    rate = n / d
        except ValueError:
            pass
    if rate <= 0:
        return out
    whole = round(rate)
    if abs(rate - whole) < 0.001:
        _add(int(whole), 1)
        _add(int(whole) * 1000, 1000)
    else:
        _add(int(round(rate * 1000)), 1000)
        ntsc = round(rate * 1.001)
        if abs(ntsc * 1000 / 1001 - rate) < 0.01:
            _add(int(ntsc) * 1000, 1001)
    return out


def rational_for(rate: float, fraction: str = "") -> tuple[int, int]:
    """Best single spelling of a refresh rate (first of ``rational_candidates``)."""
    c = rational_candidates(rate, fraction)
    return c[0] if c else (0, 0)


def _set_display_config(n_paths, paths, n_modes, modes, flags) -> int:
    """Thin wrapper so tests can intercept the Win32 call."""
    return int(_user32().SetDisplayConfig(n_paths, paths, n_modes, modes, flags))


def set_refresh(
    targets: dict[int, tuple[int, int] | list[tuple[int, int]]],
) -> dict[int, float] | None:
    """Change ONLY the vertical refresh of active display paths.

    ``targets`` maps a Windows OS target id (the ``TargetId`` column of
    configureDdisplay, e.g. ``0x11106``) to the wanted vSync rational, or
    to a list of acceptable spellings of it (see ``rational_candidates``).
    For each matching path the target mode index is dropped and the
    rational is written into ``targetInfo.refreshRate`` — the documented
    way to ask Windows for a specific timing from the monitor's mode list
    (this is what the Settings app does). Desktop size, position and every
    other display are handed back unchanged.

    Why this exists: configureMosaic buckets 24.000 and 23.976 into one
    "24" and always resolves it to 23.976 on the site Tessera SX40 panels
    (2026-11-02), while Windows reports and distinguishes ``24/1`` and
    ``23976/1000`` exactly.

    Each spelling is validated STRICTLY (no SDC_ALLOW_CHANGES) and the
    first one Windows accepts for every target is applied; SDC_ALLOW_CHANGES
    is a last resort because it let Windows substitute a 30 Hz mode for a
    rejected 60000/1001 request.

    Returns ``{target_id: hz_after}`` read back from Windows after the
    apply, or ``None`` when CCD is unavailable, a target is not an active
    path, or Windows rejected the configuration (logged) — the caller falls
    back to configureMosaic in that case.
    """
    logger = log.get()
    if not _IS_WINDOWS or not targets:
        return None
    try:
        paths, n_paths, modes, n_modes = query()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"  CCD: QueryDisplayConfig failed: {exc}")
        return None

    # Normalise to per-target candidate lists.
    cands: dict[int, list[tuple[int, int]]] = {}
    for tid, spec in targets.items():
        lst = list(spec) if spec and isinstance(spec, list) else [spec]  # type: ignore[list-item]
        lst = [(int(n), int(d)) for n, d in lst if n and d and int(n) > 0 and int(d) > 0]
        if lst:
            cands[int(tid)] = lst
    path_idx: dict[int, int] = {}
    for i in range(n_paths):
        tid = int(paths[i].targetInfo.id)
        if tid in cands:
            path_idx[tid] = i
    missing = sorted(set(cands) - set(path_idx))
    if missing:
        logger.warning(
            "  CCD: target(s) " + ", ".join(f"0x{t:x}" for t in missing)
            + " are not active display paths — leaving the mode set to configureMosaic"
        )
        return None

    def _stage(choice: dict[int, tuple[int, int]]) -> None:
        for tid, (num, den) in choice.items():
            t = paths[path_idx[tid]].targetInfo
            t.modeInfoIdx = _INVALID_IDX
            t.refreshRate.Numerator = num
            t.refreshRate.Denominator = den
            t.scanLineOrdering = SCANLINE_PROGRESSIVE

    base = SDC_USE_SUPPLIED_DISPLAY_CONFIG
    n_rounds = max(len(v) for v in cands.values())
    hit: dict[int, tuple[int, int]] | None = None
    for k in range(n_rounds):
        choice = {tid: lst[min(k, len(lst) - 1)] for tid, lst in cands.items()}
        _stage(choice)
        rc = _set_display_config(n_paths, paths, n_modes, modes, SDC_VALIDATE | base)
        if rc == ERROR_SUCCESS:
            hit = choice
            break
        logger.debug("  CCD: strict validate rc=%s for %s" % (
            rc, ", ".join(f"0x{t:x}={n}/{d}" for t, (n, d) in choice.items())))
    if hit is None:
        # Last resort: first spelling, let Windows adjust — the caller
        # re-reads the landing rate and fails the set if it is wrong.
        hit = {tid: lst[0] for tid, lst in cands.items()}
        _stage(hit)
        base |= SDC_ALLOW_CHANGES
        rc = _set_display_config(n_paths, paths, n_modes, modes, SDC_VALIDATE | base)
        if rc != ERROR_SUCCESS:
            logger.warning(f"  CCD: Windows rejected every spelling of the requested refresh "
                           f"(validate rc={rc}) — falling back to configureMosaic")
            return None
        logger.warning("  CCD: no spelling passed strict validation; applying with SDC_ALLOW_CHANGES")
    wanted = ", ".join(f"0x{t:x} → {n}/{d} ({n / d:.3f} Hz)" for t, (n, d) in hit.items())
    logger.info(f"  CCD: SetDisplayConfig refresh {wanted}")
    rc = _set_display_config(n_paths, paths, n_modes, modes, SDC_APPLY | SDC_SAVE_TO_DATABASE | base)
    if rc != ERROR_SUCCESS:
        logger.error(f"  CCD: SetDisplayConfig apply failed (rc={rc})")
        return None

    out: dict[int, float] = {}
    try:
        paths, n_paths, modes, _ = query()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"  CCD: re-query after apply failed: {exc}")
        return {t: 0.0 for t in hit}
    for i in range(n_paths):
        p = paths[i]
        tid = int(p.targetInfo.id)
        if tid not in hit:
            continue
        ti = p.targetInfo.modeInfoIdx
        hz = 0.0
        if ti != _INVALID_IDX and modes[ti].infoType == MODE_TARGET:
            v = modes[ti].u.targetMode.vSyncFreq
            hz = v.Numerator / v.Denominator if v.Denominator else 0.0
        if hz <= 0:
            r = p.targetInfo.refreshRate
            hz = r.Numerator / r.Denominator if r.Denominator else 0.0
        out[tid] = round(hz, 3)
    for t in hit:
        out.setdefault(t, 0.0)
    return out


def make_origin(width: int, height: int, name_hint: str = "") -> bool:
    """Put the display matching ``name_hint`` (else ``width x height``) at (0,0).

    Shifts every source position by the same offset and hands Windows back
    the complete configuration — no mode is re-chosen, so nothing gets
    scaled. Verifies afterwards that every path's signal is unchanged.
    """
    logger = log.get()
    if not _IS_WINDOWS:
        return False
    paths, n_paths, modes, n_modes = query()

    target_src = None
    for i in range(n_paths):
        p = paths[i]
        si = p.sourceInfo.modeInfoIdx
        if si == _INVALID_IDX or modes[si].infoType != MODE_SOURCE:
            continue
        sm = modes[si].u.sourceMode
        if name_hint and name_hint.lower() in _target_name(p).lower():
            target_src = si
            break
        if target_src is None and (sm.width, sm.height) == (width, height):
            target_src = si
    if target_src is None:
        logger.warning(f"  CCD: operator display {width}x{height} ({name_hint}) not found — primary unchanged")
        return False

    pos = modes[target_src].u.sourceMode.position
    dx, dy = pos.x, pos.y
    if dx == 0 and dy == 0:
        return True

    before = [(d["name"], d["target"], d["hz"]) for d in describe()]
    for j in range(n_modes):
        if modes[j].infoType == MODE_SOURCE:
            modes[j].u.sourceMode.position.x -= dx
            modes[j].u.sourceMode.position.y -= dy

    flags = SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_SAVE_TO_DATABASE  # no SDC_ALLOW_CHANGES
    rc = _user32().SetDisplayConfig(n_paths, paths, n_modes, modes, flags)
    if rc != ERROR_SUCCESS:
        logger.error(f"  CCD: SetDisplayConfig failed ({rc}) — layout unchanged")
        return False

    after = [(d["name"], d["target"], d["hz"]) for d in describe()]
    if sorted(map(str, before)) != sorted(map(str, after)):
        logger.error(f"  CCD: display signal changed while moving the layout! before={before} after={after}")
        return False
    logger.info(f"  Made {name_hint or f'{width}x{height}'} primary at (0,0) via SetDisplayConfig "
                f"(shifted layout by {-dx},{-dy}; signals unchanged)")
    return True
