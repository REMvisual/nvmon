"""Direct Display operations — query, enable (remove-from-desktop), disable.

Wraps `configureDdisplay.exe`. Missing exe is handled gracefully — `status()`
returns an empty `DDState`, control calls return a failed `CommandResult`
without raising.

Also exposes Win32-level helpers (`ensure_all_active`, `apply_arrangement`)
for post-Mosaic-disable cleanup, ported from the nvdd reference.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import time

from .. import config, executors, log
from ..parsers import parse_dd_status
from ..types import CommandResult, DDState


# ---------------------------------------------------------------------------
# State flag: DD known-unavailable on this machine. Set once, fast-path forever.
# ---------------------------------------------------------------------------

_dd_unavailable = False
_dd_unavailable_reason = ""


def _mark_unavailable(reason: str) -> None:
    global _dd_unavailable, _dd_unavailable_reason
    _dd_unavailable = True
    _dd_unavailable_reason = reason


def is_available() -> bool:
    """Return True if DirectDisplay queries are believed to work."""
    if _dd_unavailable:
        return False
    return bool(config.get_dd_exe())


def capability() -> dict:
    """Report whether the Direct Display subsystem is usable.

    Returns a dict for `/health.capabilities.displays`.
    """
    if _dd_unavailable:
        return {
            "available": False,
            "exe": "configureDdisplay.exe",
            "reason": _dd_unavailable_reason or "configureDdisplay.exe unavailable",
        }
    exe = config.get_dd_exe()
    if not exe:
        return {
            "available": False,
            "exe": None,
            "reason": config.missing_exe_reason("configureDdisplay.exe"),
        }
    return {"available": True, "exe": "configureDdisplay.exe"}


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def status(*, dry_run: bool = False) -> DDState:
    """Query Direct Display status for all displays.

    Returns an empty DDState if the exe is missing or DD isn't supported
    on this machine. Safe to call repeatedly — after one failure it
    short-circuits to a fast no-op.
    """
    logger = log.get()

    # INTEGRATION_POINT: backends — dispatch through the backend registry
    # when multi-backend support lands (e.g. NVAPI direct, CLI fallback).
    if _dd_unavailable:
        return DDState()

    exe = config.get_dd_exe()
    if not exe:
        logger.warning("configureDdisplay.exe not found — DD queries disabled")
        _mark_unavailable(config.missing_exe_reason("configureDdisplay.exe"))
        return DDState()

    result = executors.run(
        exe, ["status", "--all"],
        label="dd-status", dry_run=dry_run, quiet=True,
    )
    if dry_run:
        return DDState()

    # "OS interface for ddisplay is not available" = machine doesn't support DD
    combined_err = (result.stderr or "").lower()
    if not result.success and "not available" in combined_err:
        logger.warning("DirectDisplay not available on this machine — DD queries disabled")
        _mark_unavailable("OS interface for ddisplay is not available")
        return DDState()

    try:
        return parse_dd_status(result.stdout + "\n" + result.stderr)
    except Exception as exc:
        logger.error(f"DD status parse failed: {exc}")
        return DDState()


# ---------------------------------------------------------------------------
# Control: enable / disable
# ---------------------------------------------------------------------------

def enable(
    *, mode: str = "index", index: int = 1, dry_run: bool = False,
) -> CommandResult:
    """Remove displays from desktop (enter Direct Display mode).

    Args:
        mode: "all-nvidia" or "index" (selects a single adapter by index).
        index: Adapter index for --index mode.
    """
    exe = config.get_dd_exe()
    if not exe:
        return CommandResult(
            success=False, return_code=-1,
            stderr=config.missing_exe_reason("configureDdisplay.exe"),
        )

    logger = log.get()

    args = ["remove-from-desktop"]
    if mode == "all-nvidia":
        args.append("--all-nvidia")
        logger.info("DD: removing all NVIDIA-managed displays from desktop")
    elif mode == "index":
        args.extend(["--index", str(index)])
        logger.info(f"DD: removing adapter index {index} from desktop")
    else:
        args.append("--all-nvidia")
        logger.info("DD: removing displays from desktop (default mode)")

    result = executors.run(exe, args, label="dd-enable", dry_run=dry_run)

    if result.success:
        logger.info("DD: displays removed from desktop")
    else:
        logger.error(f"DD enable failed: {executors.failure_detail(result)}")

    return result


def disable(*, dry_run: bool = False) -> CommandResult:
    """Return displays to desktop (exit Direct Display mode)."""
    exe = config.get_dd_exe()
    if not exe:
        return CommandResult(
            success=False, return_code=-1,
            stderr=config.missing_exe_reason("configureDdisplay.exe"),
        )

    logger = log.get()
    logger.info("DD: returning displays to desktop")

    result = executors.run(
        exe, ["return-to-desktop", "--all-targets", "--force"],
        label="dd-disable", dry_run=dry_run,
    )

    executors.sleep(config.DD_RETURN_WAIT, "DD return settle")

    if result.success:
        logger.info("DD: displays returned to desktop")
    else:
        # This is normal when DD is already off — downgrade to warning.
        logger.warning(
            f"DD return-to-desktop returned error (may already be on desktop): {executors.failure_detail(result)}"
        )

    return result


# ---------------------------------------------------------------------------
# Win32 display enumeration helpers — used during profile apply/restore.
# ---------------------------------------------------------------------------

ENUM_CURRENT_SETTINGS = -1
ENUM_REGISTRY_SETTINGS = -2
CDS_UPDATEREGISTRY = 0x00000001
CDS_NORESET = 0x10000000
CDS_SET_PRIMARY = 0x00000010
DISP_CHANGE_SUCCESSFUL = 0
DM_POSITION = 0x00000020
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004


class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("DeviceName", ctypes.wintypes.WCHAR * 32),
        ("DeviceString", ctypes.wintypes.WCHAR * 128),
        ("StateFlags", ctypes.wintypes.DWORD),
        ("DeviceID", ctypes.wintypes.WCHAR * 128),
        ("DeviceKey", ctypes.wintypes.WCHAR * 128),
    ]


class DEVMODE(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.wintypes.WCHAR * 32),
        ("dmSpecVersion", ctypes.wintypes.WORD),
        ("dmDriverVersion", ctypes.wintypes.WORD),
        ("dmSize", ctypes.wintypes.WORD),
        ("dmDriverExtra", ctypes.wintypes.WORD),
        ("dmFields", ctypes.wintypes.DWORD),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.wintypes.DWORD),
        ("dmDisplayFixedOutput", ctypes.wintypes.DWORD),
        ("dmColor", ctypes.wintypes.SHORT),
        ("dmDuplex", ctypes.wintypes.SHORT),
        ("dmYResolution", ctypes.wintypes.SHORT),
        ("dmTTOption", ctypes.wintypes.SHORT),
        ("dmCollate", ctypes.wintypes.SHORT),
        ("dmFormName", ctypes.wintypes.WCHAR * 32),
        ("dmLogPixels", ctypes.wintypes.WORD),
        ("dmBitsPerPel", ctypes.wintypes.DWORD),
        ("dmPelsWidth", ctypes.wintypes.DWORD),
        ("dmPelsHeight", ctypes.wintypes.DWORD),
        ("dmDisplayFlags", ctypes.wintypes.DWORD),
        ("dmDisplayFrequency", ctypes.wintypes.DWORD),
        ("dmICMMethod", ctypes.wintypes.DWORD),
        ("dmICMIntent", ctypes.wintypes.DWORD),
        ("dmMediaType", ctypes.wintypes.DWORD),
        ("dmDitherType", ctypes.wintypes.DWORD),
        ("dmReserved1", ctypes.wintypes.DWORD),
        ("dmReserved2", ctypes.wintypes.DWORD),
        ("dmPanningWidth", ctypes.wintypes.DWORD),
        ("dmPanningHeight", ctypes.wintypes.DWORD),
    ]


try:
    _user32 = ctypes.windll.user32
except (AttributeError, OSError):
    _user32 = None  # Not on Windows — Win32 helpers become no-ops.


def _get_devmode(device_name: str):
    """Get display settings, trying current first then registry fallback."""
    if _user32 is None:
        return None

    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    if _user32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return dm

    dm2 = DEVMODE()
    dm2.dmSize = ctypes.sizeof(DEVMODE)
    if _user32.EnumDisplaySettingsW(device_name, ENUM_REGISTRY_SETTINGS, ctypes.byref(dm2)):
        return dm2

    return None


def _list_all_adapters() -> list[tuple[str, int]]:
    """Enumerate display adapters. Returns (device_name, state_flags)."""
    if _user32 is None:
        return []

    adapters: list[tuple[str, int]] = []
    idx = 0
    while True:
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        if not _user32.EnumDisplayDevicesW(None, idx, ctypes.byref(dd), 0):
            break
        adapters.append((dd.DeviceName, dd.StateFlags))
        idx += 1
    return adapters


def _mosaic_surface_size() -> tuple[int, int]:
    """Total desktop size of the live Mosaic wall, or (0, 0) when Mosaic is off.

    Cheap: reads the agent monitor's last snapshot when one exists, else
    asks configureMosaic once.
    """
    try:
        from . import mosaic as mosaic_mod
        state = mosaic_mod.status()
        if state.enabled:
            w, h = state.total_resolution
            if not (w and h):
                pw, ph = state.per_display_resolution
                w, h = pw * max(state.cols, 1), ph * max(state.rows, 1)
            return int(w or 0), int(h or 0)
    except Exception as exc:  # noqa: BLE001
        log.get().debug(f"_mosaic_surface_size: {exc}")
    return 0, 0


def _is_mosaic_surface(dm, wall: tuple[int, int]) -> bool:
    """True when this DEVMODE is the combined Mosaic wall.

    nvmon must never write the wall's mode through Windows: a
    ``ChangeDisplaySettingsEx`` without the exact timing makes Windows send
    the panel's EDID default (5120x2880@60 on Helios) and scale the desktop
    to it — saved in the registry, so it survives reboots. Only
    configureMosaic sets the wall's timing (the legacy tool 1170183).
    """
    if dm is None or not wall[0] or not wall[1]:
        return False
    return int(dm.dmPelsWidth) == wall[0] and int(dm.dmPelsHeight) == wall[1]


def _monitor_name(device_name: str) -> str:
    """Friendly monitor name behind an adapter output (e.g. 'DDX-101'), '' if unknown."""
    if _user32 is None:
        return ""
    dd = DISPLAY_DEVICE()
    dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
    if _user32.EnumDisplayDevicesW(device_name, 0, ctypes.byref(dd), 0):
        return dd.DeviceString
    return ""


def make_primary(name_hint: str = "", width: int = 0, height: int = 0) -> bool:
    """Make the operator display (KVM / control monitor) primary at (0,0).

    After a Mosaic rebuild Windows may put the wall at (0,0); DirectDisplay
    cannot take the primary display, so DD enable then fails
    ("sendCmdManageTarget returned error"). Matches by monitor name first
    (``name_hint``, e.g. "DDX"), then by resolution. Moves the layout via
    SetDisplayConfig so no display's mode is re-chosen — the old
    ChangeDisplaySettingsEx move scaled the wall (the legacy tool 5f02b71).

    Returns True when the display is primary afterwards (or already was).
    """
    logger = log.get()
    if _user32 is None:
        return False
    attached = []
    for dev, flags in _list_all_adapters():
        if not flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if _user32.EnumDisplaySettingsW(dev, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
            attached.append((dev, dm))

    target = None
    if name_hint:
        target = next((a for a in attached if name_hint.lower() in _monitor_name(a[0]).lower()), None)
    if target is None and width and height:
        target = next((a for a in attached
                       if a[1].dmPelsWidth == width and a[1].dmPelsHeight == height), None)
    if target is None:
        logger.warning(f"  Operator display ({name_hint or f'{width}x{height}'}) not found — primary unchanged")
        return False

    dev, dm = target
    if dm.dmPositionX == 0 and dm.dmPositionY == 0:
        return True  # already primary (primary is the display at the origin)

    from . import ccd
    logger.info(f"  {dev} ({_monitor_name(dev) or name_hint}) is at ({dm.dmPositionX},{dm.dmPositionY}) "
                f"— moving the layout via SetDisplayConfig so it becomes primary")
    try:
        ok = ccd.make_origin(int(dm.dmPelsWidth), int(dm.dmPelsHeight), name_hint=name_hint)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"  CCD move failed: {exc}")
        ok = False
    if not ok:
        logger.error("  Operator display could not be made primary without touching the wall's "
                     "mode — not moving displays. Fix by hand: Windows Display settings > "
                     "operator display > 'Make this my main display'.")
        return False
    executors.sleep(config.DISPLAY_SETTLE_TIME, "display settle")
    return True


def ensure_all_active() -> list[str]:
    """Enable any connected-but-inactive display adapters.

    Used after Mosaic is disabled — the individual displays get detached from
    the Windows desktop and must be re-enabled before subsequent Mosaic/DD
    operations can find them.

    Returns list of device names that were enabled.
    """
    logger = log.get()

    if _user32 is None:
        logger.debug("ensure_all_active: not running on Windows, skipping")
        return []

    enabled: list[str] = []
    wall = _mosaic_surface_size()

    for device_name, flags in _list_all_adapters():
        if flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue

        dm = _get_devmode(device_name)
        if dm is None:
            logger.debug(f"  {device_name}: no settings found, skipping")
            continue

        if dm.dmPelsWidth == 0 or dm.dmPelsHeight == 0:
            logger.debug(f"  {device_name}: 0x0 resolution, skipping")
            continue

        if _is_mosaic_surface(dm, wall):
            logger.info(f"  {device_name}: Mosaic wall ({dm.dmPelsWidth}x{dm.dmPelsHeight}) — "
                        f"not re-attaching through Windows (configureMosaic owns it)")
            continue

        dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT
        rc = _user32.ChangeDisplaySettingsExW(
            device_name, ctypes.byref(dm), None,
            CDS_UPDATEREGISTRY | CDS_NORESET, None,
        )
        if rc == DISP_CHANGE_SUCCESSFUL:
            logger.info(f"  Enabled display: {device_name} ({dm.dmPelsWidth}x{dm.dmPelsHeight})")
            enabled.append(device_name)
        else:
            logger.warning(f"  Failed to enable {device_name} (error {rc})")

    if enabled:
        _user32.ChangeDisplaySettingsExW(None, None, None, 0, None)
        logger.info(f"  Committed {len(enabled)} display change(s)")
        time.sleep(config.DISPLAY_SETTLE_TIME)
    else:
        logger.debug("  All displays already active")

    return enabled


def capture_arrangement() -> list[dict]:
    """Snapshot the live Windows desktop arrangement.

    Returns the list-of-dicts shape that ``apply_arrangement`` consumes:
    one entry per attached-to-desktop display with
    ``{device, x, y, width, height, refresh_rate, primary}``.
    Empty list when not on Windows or no displays are attached.

    Used by profile_capture so a captured profile can fully restore the
    desktop layout (positions + per-monitor resolution + refresh) without
    relying on NVCP's "Enable resolutions not exposed by the display"
    toggle — the captured DEVMODE carries the exact mode the system was
    running, so re-applying via ChangeDisplaySettingsExW reproduces it.
    """
    if _user32 is None:
        return []

    out: list[dict] = []
    for device_name, flags in _list_all_adapters():
        if not (flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP):
            continue
        dm = _get_devmode(device_name)
        if dm is None:
            continue
        if dm.dmPelsWidth == 0 or dm.dmPelsHeight == 0:
            continue
        out.append({
            "device": device_name,
            "x": int(dm.dmPositionX),
            "y": int(dm.dmPositionY),
            "width": int(dm.dmPelsWidth),
            "height": int(dm.dmPelsHeight),
            "refresh_rate": int(dm.dmDisplayFrequency or 0),
            "primary": bool(flags & DISPLAY_DEVICE_PRIMARY_DEVICE),
        })
    return out


def apply_arrangement(arrangement: list[dict]) -> bool:
    """Apply a Windows display arrangement from a profile.

    Args:
        arrangement: list of dicts with keys:
            device (str), x (int), y (int), width (int), height (int), primary (bool)
    """
    logger = log.get()

    if _user32 is None:
        logger.debug("apply_arrangement: not running on Windows, skipping")
        return False

    if not arrangement:
        logger.warning("  No arrangement data to apply")
        return False

    success = True
    applied = 0
    wall = _mosaic_surface_size()

    for entry in arrangement:
        device = entry.get("device", "")
        if not device:
            continue

        dm = _get_devmode(device)
        if _is_mosaic_surface(dm, wall):
            logger.info(f"  {device}: is the Mosaic wall ({dm.dmPelsWidth}x{dm.dmPelsHeight}) — skipped "
                        f"(only configureMosaic may set its mode)")
            continue
        if dm is None:
            logger.info(f"  {device}: no existing settings, creating from arrangement")
            dm = DEVMODE()
            dm.dmSize = ctypes.sizeof(DEVMODE)

        dm.dmPositionX = entry.get("x", 0)
        dm.dmPositionY = entry.get("y", 0)
        dm.dmPelsWidth = entry.get("width", dm.dmPelsWidth or 3840)
        dm.dmPelsHeight = entry.get("height", dm.dmPelsHeight or 2160)
        dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT

        flags = CDS_UPDATEREGISTRY | CDS_NORESET
        if entry.get("primary", False):
            flags |= CDS_SET_PRIMARY

        rc = _user32.ChangeDisplaySettingsExW(
            device, ctypes.byref(dm), None, flags, None,
        )
        if rc == DISP_CHANGE_SUCCESSFUL:
            primary_tag = " [PRIMARY]" if entry.get("primary") else ""
            logger.info(
                f"  Set {device}: {dm.dmPelsWidth}x{dm.dmPelsHeight} "
                f"@ ({dm.dmPositionX},{dm.dmPositionY}){primary_tag}"
            )
            applied += 1
        else:
            logger.warning(f"  Failed to set {device} (error {rc})")
            success = False

    if applied == 0:
        logger.warning("  No displays were configured")
        return False

    commit = _user32.ChangeDisplaySettingsExW(None, None, None, 0, None)
    if commit != DISP_CHANGE_SUCCESSFUL:
        logger.warning(f"  Commit failed (error {commit})")
        success = False
    else:
        logger.info(f"  Committed {applied} display(s)")

    time.sleep(config.DISPLAY_SETTLE_TIME)
    return success
