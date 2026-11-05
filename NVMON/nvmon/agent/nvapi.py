"""NVAPI colour control — bits per channel / pixel encoding / colorimetry read+write.

Why this exists
---------------
``configureMosaic.exe``, Windows ``SetDisplayConfig`` and the NVAPI custom
display calls in ``timing.py`` set position, size, timing and refresh — none
of them touches colour depth (8/10/12 bpc), pixel encoding (RGB / YCbCr444 /
YCbCr422) or colorimetry. Those live behind ``NvAPI_Disp_ColorControl``, the
call behind NVIDIA Control Panel's "Use NVIDIA color settings". The driver
re-chooses them per mode: after nvmon programs a new timing a panel comes up
on the driver's default (site 2026-11-03: six of seven Tessera SX40 nodes at
8-bit RGB444 after a "60 - 10b" apply, only the one hand-set node at 10-bit
YCC422). Profiles already carry ``display_ids[<id>].bits_per_channel`` /
``pixel_encoding`` / ``color_space`` (from the Windows wire format); this
module turns them into the matching NVAPI call and verifies the landing.

History: the first version of this module (v0.6–v0.9.0) never worked — it
looked up two non-existent function ids ("NvAPI_DISP_GetColorData" /
"SetColorData"; NVAPI has one call, ``NvAPI_Disp_ColorControl`` 0x92F9D80D,
with a GET/SET command byte), used a 22-byte all-``u8`` struct where
``nvapi.h`` has 4-byte enums (24 bytes; the driver answers
NVAPI_INCOMPATIBLE_STRUCT_VERSION, -9), loaded the DLL by bare name (fails
in the listener's session on the site boxes while the System32 path loads),
and the apply pipeline never called it. All four verified on an RTX PRO 6000
/ 596.86 on 2026-11-03.

Design
------
Shares the process-wide ``_NvAPILoader`` with ``backends/nvapi.py`` and
``timing.py`` (one NvAPI session, System32 path first). Every function
degrades gracefully: missing DLL, missing export, non-OK status — one log
line, ``None`` / ``False`` back. The agent never crashes on NVAPI absence.

Reference (nvapi.h, "Display Control" group)
--------------------------------------------
``NvAPI_Disp_ColorControl(NvU32 displayId, NV_COLOR_DATA *pColorData)``

    NV_COLOR_DATA_V5 {
        NvU32 version; NvU16 size; NvU8 cmd;           // cmd: 1 GET, 2 SET,
        struct {                                        //   3 IS_SUPPORTED, 4 GET_DEFAULT
            NvU8  colorFormat;      // NV_COLOR_FORMAT      RGB / YUV422 / YUV444 / YUV420
            NvU8  colorimetry;      // NV_COLOR_COLORIMETRY RGB / YCC601 / YCC709 / … / BT2020
            NvU8  dynamicRange;     // NV_DYNAMIC_RANGE     VESA (full) / CEA (limited) / AUTO
            NV_BPC bpc;             // enum (4 B)           DEFAULT / 6 / 8 / 10 / 12 / 16
            NV_COLOR_SELECTION_POLICY colorSelectionPolicy;  // USER / BEST_QUALITY / DEFAULT
            NV_DESKTOP_COLOR_DEPTH depth;                     // desktop depth (read-only here)
        } data;
    }                                                   // sizeof 24, version 0x50018

Probed values on this hardware (GET, 8-bit RGB desktop): format 0,
colorimetry 0, dynamicRange 0, bpc 2, policy 1 (BEST_QUALITY), depth 1.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, fields
from typing import Any

from .. import log
from .backends.nvapi import NVAPI_OK, NvAPI_Status, NvU32, _NvAPILoader

# ---------------------------------------------------------------------------
# Constants from nvapi.h
# ---------------------------------------------------------------------------

NVAPI_INCOMPATIBLE_STRUCT_VERSION = -9

# NV_COLOR_CMD
NV_COLOR_CMD_GET = 1
NV_COLOR_CMD_SET = 2
NV_COLOR_CMD_IS_SUPPORTED_COLOR = 3
NV_COLOR_CMD_GET_DEFAULT = 4

# NV_COLOR_FORMAT
NV_COLOR_FORMAT_RGB = 0
NV_COLOR_FORMAT_YUV422 = 1
NV_COLOR_FORMAT_YUV444 = 2
NV_COLOR_FORMAT_YUV420 = 3
NV_COLOR_FORMAT_DEFAULT = 0xFE
NV_COLOR_FORMAT_AUTO = 0xFF

# NV_COLOR_COLORIMETRY
NV_COLORIMETRY_RGB = 0
NV_COLORIMETRY_YCC601 = 1
NV_COLORIMETRY_YCC709 = 2
NV_COLORIMETRY_XVYCC601 = 3
NV_COLORIMETRY_XVYCC709 = 4
NV_COLORIMETRY_SYCC601 = 5
NV_COLORIMETRY_ADOBEYCC601 = 6
NV_COLORIMETRY_ADOBERGB = 7
NV_COLORIMETRY_BT2020RGB = 8
NV_COLORIMETRY_BT2020YCC = 9
NV_COLORIMETRY_BT2020CYCC = 10
NV_COLORIMETRY_DEFAULT = 0xFE
NV_COLORIMETRY_AUTO = 0xFF

# NV_DYNAMIC_RANGE
NV_DYNAMIC_RANGE_VESA = 0      # full range
NV_DYNAMIC_RANGE_CEA = 1       # limited range
NV_DYNAMIC_RANGE_AUTO = 0xFF

# NV_BPC
NV_BPC_DEFAULT = 0
NV_BPC_6 = 1
NV_BPC_8 = 2
NV_BPC_10 = 3
NV_BPC_12 = 4
NV_BPC_16 = 5

# NV_COLOR_SELECTION_POLICY
NV_COLOR_SELECTION_POLICY_USER = 0          # "Use NVIDIA color settings"
NV_COLOR_SELECTION_POLICY_BEST_QUALITY = 1  # "Use default color settings"
NV_COLOR_SELECTION_POLICY_DEFAULT = 0xFF

# NV_DESKTOP_COLOR_DEPTH
NV_DESKTOP_COLOR_DEPTH_DEFAULT = 0
NV_DESKTOP_COLOR_DEPTH_8BPC = 1
NV_DESKTOP_COLOR_DEPTH_10BPC = 2
NV_DESKTOP_COLOR_DEPTH_12BPC = 3

_UNSET = -1  # ColorData sentinel: "profile carries no value; keep the driver's"

FORMAT_NAMES = {
    NV_COLOR_FORMAT_RGB: "RGB444",
    NV_COLOR_FORMAT_YUV422: "YCbCr422",
    NV_COLOR_FORMAT_YUV444: "YCbCr444",
    NV_COLOR_FORMAT_YUV420: "YCbCr420",
    NV_COLOR_FORMAT_DEFAULT: "default",
    NV_COLOR_FORMAT_AUTO: "auto",
}
BPC_BITS = {NV_BPC_6: 6, NV_BPC_8: 8, NV_BPC_10: 10, NV_BPC_12: 12, NV_BPC_16: 16}
COLORIMETRY_NAMES = {
    NV_COLORIMETRY_RGB: "RGB", NV_COLORIMETRY_YCC601: "BT601", NV_COLORIMETRY_YCC709: "BT709",
    NV_COLORIMETRY_XVYCC601: "xvYCC601", NV_COLORIMETRY_XVYCC709: "xvYCC709",
    NV_COLORIMETRY_SYCC601: "sYCC601", NV_COLORIMETRY_ADOBEYCC601: "AdobeYCC601",
    NV_COLORIMETRY_ADOBERGB: "AdobeRGB", NV_COLORIMETRY_BT2020RGB: "BT2020RGB",
    NV_COLORIMETRY_BT2020YCC: "BT2020YCC", NV_COLORIMETRY_BT2020CYCC: "BT2020cYCC",
    NV_COLORIMETRY_DEFAULT: "default", NV_COLORIMETRY_AUTO: "auto",
}
RANGE_NAMES = {NV_DYNAMIC_RANGE_VESA: "full", NV_DYNAMIC_RANGE_CEA: "limited", NV_DYNAMIC_RANGE_AUTO: "auto"}
POLICY_NAMES = {
    NV_COLOR_SELECTION_POLICY_USER: "user",
    NV_COLOR_SELECTION_POLICY_BEST_QUALITY: "best-quality",
    NV_COLOR_SELECTION_POLICY_DEFAULT: "default",
}


# ---------------------------------------------------------------------------
# Structs — hand-translated from nvapi.h; sizeof asserted below
# ---------------------------------------------------------------------------


class _NV_COLOR_DATA_V5_DATA(ctypes.Structure):
    _fields_ = [
        ("colorFormat", ctypes.c_uint8),
        ("colorimetry", ctypes.c_uint8),
        ("dynamicRange", ctypes.c_uint8),
        # (1 byte padding — the enums below are 4-byte C ints)
        ("bpc", ctypes.c_uint32),
        ("colorSelectionPolicy", ctypes.c_uint32),
        ("depth", ctypes.c_uint32),
    ]


class NV_COLOR_DATA_V5(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("size", ctypes.c_uint16),
        ("cmd", ctypes.c_uint8),
        # (1 byte padding — `data` is 4-byte aligned, offset 8)
        ("data", _NV_COLOR_DATA_V5_DATA),
    ]


NV_COLOR_DATA_V5_VER = (ctypes.sizeof(NV_COLOR_DATA_V5) | (5 << 16)) & 0xFFFFFFFF
assert ctypes.sizeof(NV_COLOR_DATA_V5) == 24, ctypes.sizeof(NV_COLOR_DATA_V5)
assert NV_COLOR_DATA_V5.data.offset == 8


class ColorUnavailable(RuntimeError):
    """NVAPI or NvAPI_Disp_ColorControl is not usable on this host."""


# ---------------------------------------------------------------------------
# Loader glue — shared with backends/nvapi.py and timing.py
# ---------------------------------------------------------------------------

_COLOR_CONTROL = "NvAPI_Disp_ColorControl"


def is_available() -> bool:
    """True when nvapi64.dll initialised and the driver exports ColorControl."""
    if sys.platform != "win32":
        return False
    try:
        loader = _NvAPILoader()
        if not loader.available():
            return False
        return _color_control_fn(loader) is not None
    except Exception:  # noqa: BLE001
        return False


def capability() -> dict:
    """``/health.capabilities.color`` shape."""
    if sys.platform != "win32":
        return {"available": False, "exe": None, "reason": "not running on Windows"}
    try:
        loader = _NvAPILoader()
        if not loader.available():
            return {"available": False, "exe": None, "reason": loader.init_error() or "NVAPI unavailable"}
        if _color_control_fn(loader) is None:
            return {"available": False, "exe": "nvapi64.dll",
                    "reason": f"{_COLOR_CONTROL} not exported by this driver"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "exe": None, "reason": str(exc)}
    return {"available": True, "exe": "nvapi64.dll", "api": _COLOR_CONTROL}


def _color_control_fn(loader: "_NvAPILoader | None" = None):
    loader = loader or _NvAPILoader()
    return loader._get_function(  # noqa: SLF001 — shared loader
        _COLOR_CONTROL, NvAPI_Status, [NvU32, ctypes.POINTER(NV_COLOR_DATA_V5)],
    )


def _fn():
    loader = _NvAPILoader()
    if not loader.available():
        raise ColorUnavailable(f"NVAPI unavailable: {loader.init_error()}")
    fn = _color_control_fn(loader)
    if fn is None:
        raise ColorUnavailable(f"{_COLOR_CONTROL} not exported by this driver")
    return fn


def _status_text(status: int) -> str:
    if status == NVAPI_INCOMPATIBLE_STRUCT_VERSION:
        return "NVAPI_INCOMPATIBLE_STRUCT_VERSION"
    try:
        return _NvAPILoader().get_error_message(status)
    except Exception:  # noqa: BLE001
        return f"NVAPI status {status}"


# ---------------------------------------------------------------------------
# Display ID resolution
# ---------------------------------------------------------------------------


def display_id_from_hex(hex_id: str) -> int | None:
    """Coerce a profile's display_ids key (``'80061085'`` / ``'0x80061085'``)
    to the NvU32 display id NVAPI expects. None on parse failure."""
    if not hex_id:
        return None
    s = str(hex_id).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    try:
        return int(s, 16)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ColorData
# ---------------------------------------------------------------------------


@dataclass
class ColorData:
    """One display's colour settings; -1 (``_UNSET``) means "no value".

    Note that 0 is a real value for every field (RGB, BT601→RGB colorimetry,
    full range, policy USER), so "missing" is -1, never falsy.
    """
    color_format: int = _UNSET          # NV_COLOR_FORMAT_*
    colorimetry: int = _UNSET           # NV_COLORIMETRY_*
    dynamic_range: int = _UNSET         # NV_DYNAMIC_RANGE_*
    bpc: int = _UNSET                   # NV_BPC_*
    color_selection_policy: int = _UNSET
    desktop_color_depth: int = _UNSET

    def to_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ColorData":
        cd = cls()
        if not isinstance(data, dict):
            return cd
        for f in fields(cls):
            v = data.get(f.name, None)
            if v is None or v == "":
                continue
            try:
                setattr(cd, f.name, int(v))
            except (TypeError, ValueError):
                continue
        return cd

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) == _UNSET for f in fields(self))

    def bits(self) -> int:
        """Bits per channel as a plain number (0 = default / unknown)."""
        return BPC_BITS.get(self.bpc, 0)

    def describe(self) -> str:
        """``10-bit YCbCr422 BT709 limited (user)`` — for logs and /displays/color."""
        parts: list[str] = []
        if self.bpc != _UNSET:
            parts.append(f"{self.bits()}-bit" if self.bits() else "default-bpc")
        if self.color_format != _UNSET:
            parts.append(FORMAT_NAMES.get(self.color_format, f"format {self.color_format}"))
        if self.colorimetry != _UNSET:
            parts.append(COLORIMETRY_NAMES.get(self.colorimetry, f"colorimetry {self.colorimetry}"))
        if self.dynamic_range != _UNSET:
            parts.append(RANGE_NAMES.get(self.dynamic_range, f"range {self.dynamic_range}"))
        if self.color_selection_policy != _UNSET:
            parts.append(f"({POLICY_NAMES.get(self.color_selection_policy, self.color_selection_policy)})")
        return " ".join(parts) or "(nothing set)"

    def to_public(self) -> dict[str, Any]:
        """Raw enums plus decoded names (``/displays/color``)."""
        d: dict[str, Any] = self.to_dict()
        d.update({
            "bits_per_channel": self.bits(),
            "pixel_encoding": FORMAT_NAMES.get(self.color_format, ""),
            "colorimetry_name": COLORIMETRY_NAMES.get(self.colorimetry, ""),
            "range": RANGE_NAMES.get(self.dynamic_range, ""),
            "policy": POLICY_NAMES.get(self.color_selection_policy, ""),
            "summary": self.describe(),
        })
        return d

    def satisfied_by(self, live: "ColorData") -> bool:
        """True when every field this object specifies matches ``live``.
        Colorimetry / range / policy are only compared when specified; the
        driver normalises them (asked YCC709, reads back the same, but asked
        AUTO reads back a concrete value)."""
        for name in ("color_format", "bpc", "colorimetry", "dynamic_range"):
            want = getattr(self, name)
            if want in (_UNSET, NV_COLOR_FORMAT_AUTO, NV_COLOR_FORMAT_DEFAULT):
                continue
            if getattr(live, name) != want:
                return False
        return True


def _to_struct(cmd: int, data: ColorData | None = None) -> NV_COLOR_DATA_V5:
    cd = NV_COLOR_DATA_V5()
    cd.version = NV_COLOR_DATA_V5_VER
    cd.size = ctypes.sizeof(NV_COLOR_DATA_V5)
    cd.cmd = cmd
    if data is not None:
        cd.data.colorFormat = (NV_COLOR_FORMAT_DEFAULT if data.color_format == _UNSET else data.color_format) & 0xFF
        cd.data.colorimetry = (NV_COLORIMETRY_DEFAULT if data.colorimetry == _UNSET else data.colorimetry) & 0xFF
        cd.data.dynamicRange = (NV_DYNAMIC_RANGE_AUTO if data.dynamic_range == _UNSET else data.dynamic_range) & 0xFF
        cd.data.bpc = NV_BPC_DEFAULT if data.bpc == _UNSET else data.bpc
        cd.data.colorSelectionPolicy = (
            NV_COLOR_SELECTION_POLICY_DEFAULT if data.color_selection_policy == _UNSET
            else data.color_selection_policy
        )
        cd.data.depth = NV_DESKTOP_COLOR_DEPTH_DEFAULT if data.desktop_color_depth == _UNSET else data.desktop_color_depth
    return cd


def _from_struct(cd: NV_COLOR_DATA_V5) -> ColorData:
    d = cd.data
    return ColorData(
        color_format=int(d.colorFormat),
        colorimetry=int(d.colorimetry),
        dynamic_range=int(d.dynamicRange),
        bpc=int(d.bpc),
        color_selection_policy=int(d.colorSelectionPolicy),
        desktop_color_depth=int(d.depth),
    )


def _call(display_id: int, cmd: int, data: ColorData | None = None) -> tuple[int, ColorData]:
    """One NvAPI_Disp_ColorControl round trip. Raises ColorUnavailable only
    when NVAPI itself is missing; a non-OK status is returned, not raised."""
    fn = _fn()
    cd = _to_struct(cmd, data)
    rc = int(fn(NvU32(display_id), ctypes.byref(cd)))
    return rc, _from_struct(cd)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def get_color_data(display_id: int) -> ColorData | None:
    """Live colour settings for one display, or None (logged) on failure."""
    try:
        rc, cd = _call(display_id, NV_COLOR_CMD_GET)
    except ColorUnavailable as exc:
        log.get().debug(f"NVAPI colour: {exc}")
        return None
    if rc != NVAPI_OK:
        log.get().warning(f"NVAPI colour: GET(0x{display_id:08X}) failed: {_status_text(rc)} ({rc})")
        return None
    return cd


def get_default_color_data(display_id: int) -> ColorData | None:
    """What the driver would pick on its own for this display (GET_DEFAULT)."""
    try:
        rc, cd = _call(display_id, NV_COLOR_CMD_GET_DEFAULT)
    except ColorUnavailable:
        return None
    return cd if rc == NVAPI_OK else None


def is_supported(display_id: int, data: ColorData) -> bool | None:
    """IS_SUPPORTED_COLOR for a fully specified request; None when unknown."""
    try:
        rc, _ = _call(display_id, NV_COLOR_CMD_IS_SUPPORTED_COLOR, data)
    except ColorUnavailable:
        return None
    return rc == NVAPI_OK


def _merge_request(wanted: ColorData, live: ColorData | None) -> ColorData:
    """The complete struct to SET: profile values where given, otherwise the
    display's current values (NVAPI wants every field filled)."""
    req = ColorData()
    for f in fields(ColorData):
        w = getattr(wanted, f.name)
        if w != _UNSET:
            setattr(req, f.name, w)
        elif live is not None:
            setattr(req, f.name, getattr(live, f.name))
    # An explicit format or depth is an NVCP "Use NVIDIA color settings"
    # choice. Under BEST_QUALITY the driver keeps picking its own.
    if (wanted.color_format != _UNSET or wanted.bpc != _UNSET) and wanted.color_selection_policy == _UNSET:
        req.color_selection_policy = NV_COLOR_SELECTION_POLICY_USER
    # Switching RGB <-> YCbCr changes the legal range (RGB full, YCbCr
    # limited); let the driver pick unless the profile says.
    if (
        wanted.dynamic_range == _UNSET and wanted.color_format != _UNSET
        and live is not None and live.color_format != wanted.color_format
    ):
        req.dynamic_range = NV_DYNAMIC_RANGE_AUTO
    # Desktop depth is driver-managed; never force it from a profile.
    req.desktop_color_depth = NV_DESKTOP_COLOR_DEPTH_DEFAULT
    return req


def set_color_data(display_id: int, data: ColorData) -> bool:
    """Apply ``data`` to one display and verify the landing.

    Fields left at -1 keep the display's current value. Returns True when
    the SET was accepted and a GET afterwards reports the asked format /
    depth. A rejected request is retried once with colorimetry and range on
    AUTO (the panel may not take the exact colorimetry the profile named).
    """
    logger = log.get()
    if data.is_empty():
        return True
    live = get_color_data(display_id)
    req = _merge_request(data, live)
    # Candidates in order: the exact request, then the same with
    # colorimetry / range left to the driver (only the fields the profile
    # did not pin). Each is asked IS_SUPPORTED first so a panel or link
    # that cannot take e.g. 12-bit says so in one clear line instead of a
    # bare SET failure.
    candidates = [req]
    if data.colorimetry == _UNSET or data.dynamic_range == _UNSET:
        relaxed = ColorData(**req.to_dict())
        if data.colorimetry == _UNSET:
            relaxed.colorimetry = NV_COLORIMETRY_AUTO
        if data.dynamic_range == _UNSET:
            relaxed.dynamic_range = NV_DYNAMIC_RANGE_AUTO
        if relaxed != req:
            candidates.append(relaxed)
    rc = NVAPI_OK
    unsupported: list[str] = []
    try:
        for cand in candidates:
            if is_supported(display_id, cand) is False:
                unsupported.append(cand.describe())
                continue
            rc, _ = _call(display_id, NV_COLOR_CMD_SET, cand)
            if rc == NVAPI_OK:
                break
            logger.info(f"NVAPI colour: SET(0x{display_id:08X}) {cand.describe()} refused ({_status_text(rc)})")
        else:
            if unsupported and len(unsupported) == len(candidates):
                logger.warning(
                    f"NVAPI colour: display 0x{display_id:08X} — the driver reports "
                    f"{' / '.join(unsupported)} as NOT supported on this display/link "
                    f"(panel EDID or DisplayPort bandwidth); keeping "
                    f"{live.describe() if live else 'the current setting'}"
                )
                return False
    except ColorUnavailable as exc:
        logger.warning(f"NVAPI colour: {exc}")
        return False
    if rc != NVAPI_OK:
        logger.warning(f"NVAPI colour: SET(0x{display_id:08X}) {req.describe()} failed: {_status_text(rc)} ({rc})")
        return False
    after = get_color_data(display_id)
    if after is None:
        logger.warning(f"NVAPI colour: SET(0x{display_id:08X}) accepted but the read-back failed")
        return False
    if not data.satisfied_by(after):
        logger.warning(
            f"NVAPI colour: display 0x{display_id:08X} asked {data.describe()}, "
            f"driver landed on {after.describe()}"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Profile strings <-> NVAPI enums
#
# Capture stores what Windows reports on the wire (WinRT WireFormat via
# configureDdisplay): pixel_encoding "Rgb444" / "Ycc422" / "Ycc444",
# bits_per_channel 8 / 10 / 12, color_space "BT709" / "BT2020" / ...
# ---------------------------------------------------------------------------


_PIXEL_ENCODING_TO_FORMAT = {
    "rgb444": NV_COLOR_FORMAT_RGB, "rgb": NV_COLOR_FORMAT_RGB,
    "ycc444": NV_COLOR_FORMAT_YUV444, "ycbcr444": NV_COLOR_FORMAT_YUV444, "yuv444": NV_COLOR_FORMAT_YUV444,
    "ycc422": NV_COLOR_FORMAT_YUV422, "ycbcr422": NV_COLOR_FORMAT_YUV422, "yuv422": NV_COLOR_FORMAT_YUV422,
    "ycc420": NV_COLOR_FORMAT_YUV420, "ycbcr420": NV_COLOR_FORMAT_YUV420, "yuv420": NV_COLOR_FORMAT_YUV420,
}

_BPC_TO_NVAPI = {6: NV_BPC_6, 8: NV_BPC_8, 10: NV_BPC_10, 12: NV_BPC_12, 16: NV_BPC_16}


def color_data_from_strings(
    *,
    pixel_encoding: str = "",
    bits_per_channel: int = 0,
    color_space: str = "",
) -> ColorData:
    """Build a ColorData from the capture strings in display_ids[id].

    Unrecognised values stay -1 (keep the driver's). The colour space is
    mapped only where it is unambiguous: BT709 with a YCbCr encoding is
    YCC709, with RGB it is plain RGB colorimetry; BT2020 likewise.
    ``ProfileDefinedWideColorGamut`` and friends are left alone.
    """
    cd = ColorData()
    pe = (pixel_encoding or "").strip().lower().replace("-", "").replace(" ", "").replace("_", "")
    if pe in _PIXEL_ENCODING_TO_FORMAT:
        cd.color_format = _PIXEL_ENCODING_TO_FORMAT[pe]
    try:
        bits = int(bits_per_channel or 0)
    except (TypeError, ValueError):
        bits = 0
    if bits in _BPC_TO_NVAPI:
        cd.bpc = _BPC_TO_NVAPI[bits]
    cs = (color_space or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    ycc = cd.color_format in (NV_COLOR_FORMAT_YUV422, NV_COLOR_FORMAT_YUV444, NV_COLOR_FORMAT_YUV420)
    if cs.startswith("bt709"):
        cd.colorimetry = NV_COLORIMETRY_YCC709 if ycc else NV_COLORIMETRY_RGB
    elif cs.startswith("bt601"):
        cd.colorimetry = NV_COLORIMETRY_YCC601 if ycc else NV_COLORIMETRY_RGB
    elif cs.startswith("bt2020"):
        cd.colorimetry = NV_COLORIMETRY_BT2020YCC if ycc else NV_COLORIMETRY_BT2020RGB
    elif cs in ("srgb", "rgb"):
        cd.colorimetry = NV_COLORIMETRY_RGB
    return cd


def wanted_color_for_display(info: dict) -> ColorData:
    """The colour a display_ids entry asks for: the ``nvapi_color`` block when
    a post-fix capture wrote one, else the Windows wire-format strings."""
    if isinstance(info.get("nvapi_color"), dict):
        cd = ColorData.from_dict(info["nvapi_color"])
        if not cd.is_empty():
            return cd
    try:
        bits = int(info.get("bits_per_channel", 0) or 0)
    except (TypeError, ValueError):
        bits = 0
    return color_data_from_strings(
        pixel_encoding=str(info.get("pixel_encoding", "") or ""),
        bits_per_channel=bits,
        color_space=str(info.get("color_space", "") or ""),
    )


def plan_color_from_profile(profile) -> dict[str, tuple[ColorData, str]]:
    """``{display_id_hex: (wanted, note)}`` for every display the profile
    gives a colour for. A display with no colour of its own inherits from a
    sibling with the same panel name (profiles captured before the 2026-11-01
    parser fix credited both "Tessera SX40" panels' wire format to the first
    one, leaving the GPU-1 panel at bpc 0 / "")."""
    display_ids = getattr(profile, "display_ids", {}) or {}
    plan: dict[str, tuple[ColorData, str]] = {}
    donors: dict[str, tuple[str, ColorData]] = {}   # panel name -> (hex id, colour)
    for hex_id, info in display_ids.items():
        if not isinstance(info, dict):
            continue
        cd = wanted_color_for_display(info)
        if not cd.is_empty():
            plan[str(hex_id)] = (cd, "")
            donors.setdefault(str(info.get("name", "") or ""), (str(hex_id), cd))
    for hex_id, info in display_ids.items():
        if not isinstance(info, dict) or str(hex_id) in plan:
            continue
        name = str(info.get("name", "") or "")
        donor = donors.get(name) if name else None
        if donor is not None:
            plan[str(hex_id)] = (donor[1], f"inherited from display {donor[0]} (same panel '{name}')")
    return plan


# ---------------------------------------------------------------------------
# Apply / capture pipeline integration
# ---------------------------------------------------------------------------


def apply_color_data_from_profile(profile, *, dry_run: bool = False) -> dict[str, bool]:
    """Per-display SET from the profile's display_ids colour fields.

    Returns ``{display_id_hex: success}``; empty when the profile carries no
    colour or NVAPI is unavailable (both logged). Never raises — a failing
    display is logged and recorded as False so the pipeline can carry on
    and report it.
    """
    logger = log.get()
    results: dict[str, bool] = {}
    plan = plan_color_from_profile(profile)
    if not plan:
        logger.debug("NVAPI colour: profile carries no colour depth / pixel encoding — nothing to apply")
        return results
    if not is_available():
        logger.warning(
            f"NVAPI colour: unavailable on this host ({capability().get('reason', '?')}) — "
            f"colour depth / pixel encoding NOT applied"
        )
        return results

    for hex_id, (wanted, note) in plan.items():
        nvapi_id = display_id_from_hex(hex_id)
        if nvapi_id is None:
            logger.warning(f"NVAPI colour: cannot parse display id {hex_id!r}; skipping")
            results[hex_id] = False
            continue
        suffix = f" [{note}]" if note else ""
        if dry_run:
            logger.info(f"NVAPI colour [dry-run]: display {hex_id} -> {wanted.describe()}{suffix}")
            results[hex_id] = True
            continue
        live = get_color_data(nvapi_id)
        if live is not None and wanted.satisfied_by(live):
            logger.info(f"  Colour: display {hex_id} already {live.describe()}{suffix}")
            results[hex_id] = True
            continue
        have = live.describe() if live is not None else "unknown"
        logger.info(f"  Colour: display {hex_id} {have} -> {wanted.describe()}{suffix}")
        ok = set_color_data(nvapi_id, wanted)
        results[hex_id] = ok
        if not ok:
            # Never leave a refused request half-applied. site 2026-11-04: after
            # refused 12-bit requests the driver sat at "10-bit YCbCr422,
            # colorimetry RGB" while sending 8-bit RGB on the wire with a
            # 4:2:2 infoframe — purple/green on every Tessera. Fall back to the
            # deepest depth the current link offers, same encoding/colorimetry.
            fb = _link_fallback(nvapi_id, wanted)
            if fb is not None:
                logger.warning(f"  Colour: display {hex_id} falling back to {fb.describe()} "
                               f"(the current link cannot carry {wanted.describe()})")
                if set_color_data(nvapi_id, fb):
                    after = get_color_data(nvapi_id)
                    logger.info(f"  Colour: display {hex_id} now {after.describe() if after else fb.describe()} (fallback)")
                else:
                    logger.error(f"  Colour: display {hex_id} fallback {fb.describe()} also refused")
        if ok:
            after = get_color_data(nvapi_id)
            logger.info(f"  Colour: display {hex_id} now {after.describe() if after else wanted.describe()}")
        else:
            logger.error(f"  Colour: display {hex_id} could not be set to {wanted.describe()}")
    return results


def _link_fallback(display_id: int, wanted: ColorData) -> ColorData | None:
    """The same request at the deepest bpc the current DP link offers, or
    None when the link info is unavailable or offers nothing lower."""
    try:
        info = get_displayport_info(display_id)
    except Exception:  # noqa: BLE001
        info = None
    if not info:
        return None
    offered = [b for b in (info.get("current_mode_bpc") or []) if b < (wanted.bits() or 99)]
    if not offered:
        return None
    fb = ColorData(**wanted.to_dict())
    fb.bpc = _BPC_TO_NVAPI[max(offered)]
    return fb


def capture_color_data_for_displays(profile) -> None:
    """Read live colour settings for every display in profile.display_ids and
    stash them under ``display_ids[id]['nvapi_color']``. In-place; best effort.
    """
    if not is_available():
        return
    display_ids = getattr(profile, "display_ids", {}) or {}
    for hex_id, info in display_ids.items():
        if not isinstance(info, dict):
            continue
        nvapi_id = display_id_from_hex(str(hex_id))
        if nvapi_id is None:
            continue
        cd = get_color_data(nvapi_id)
        if cd is not None and not cd.is_empty():
            info["nvapi_color"] = cd.to_dict()


# ---------------------------------------------------------------------------
# DisplayPort link / sink capabilities — NvAPI_GetDisplayPortInfo
#
# The question "can this panel do 12-bit at all?" is answered here, not by
# IS_SUPPORTED_COLOR (which says yes to everything on 597.30). The driver
# reports the trained link (rate × lanes), what the sink's DPCD/EDID allow
# per depth and encoding, and what fits the current mode.
# ---------------------------------------------------------------------------

_DP_INFO = "NvAPI_GetDisplayPortInfo"


class NV_DISPLAY_PORT_INFO_V2(ctypes.Structure):
    """nvapi.h NV_DISPLAY_PORT_INFO_V2: ten NvU32 fields then one NvU32 of
    bitfields (44 bytes, version 0x2002C). Bitfield order below includes the
    two 4:2:0 bits (``isYCrCb420Supported`` after 16BPC,
    ``isYCbCr420SupportedOnCurrentMode`` after 422-on-current-mode); checked
    on 596.86 against an 8-bit DP monitor where the alternative (no 4:2:0
    bits) read "8 bpc not supported on current mode" while running 8 bpc."""
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("dpcd_ver", ctypes.c_uint32),
        ("maxLinkRate", ctypes.c_uint32),
        ("maxLaneCount", ctypes.c_uint32),
        ("curLinkRate", ctypes.c_uint32),
        ("curLaneCount", ctypes.c_uint32),
        ("colorFormat", ctypes.c_uint32),
        ("dynamicRange", ctypes.c_uint32),
        ("colorimetry", ctypes.c_uint32),
        ("bpc", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    ]


NV_DISPLAY_PORT_INFO_V2_VER = (ctypes.sizeof(NV_DISPLAY_PORT_INFO_V2) | (2 << 16)) & 0xFFFFFFFF
assert ctypes.sizeof(NV_DISPLAY_PORT_INFO_V2) == 44

_DP_FLAG_NAMES = (
    "is_dp", "is_internal_dp", "color_ctrl_supported",
    "bpc6", "bpc8", "bpc10", "bpc12", "bpc16",
    "ycbcr420", "ycbcr422", "ycbcr444",
    "rgb444_on_current_mode", "ycbcr444_on_current_mode", "ycbcr422_on_current_mode", "ycbcr420_on_current_mode",
    "bpc6_on_current_mode", "bpc8_on_current_mode", "bpc10_on_current_mode", "bpc12_on_current_mode", "bpc16_on_current_mode",
    "xvycc601", "xvycc709", "sycc601", "adobeycc601", "adobergb", "bt2020rgb", "bt2020ycc", "bt2020cycc",
)
# NV_DP_LINK_RATE is in units of 270 Mbit/s per lane: 0x06 1.62, 0x0A 2.70, 0x14 5.40, 0x1E 8.10.
_DP_LINK_RATE_NAMES = {0x06: "RBR 1.62", 0x0A: "HBR 2.70", 0x14: "HBR2 5.40", 0x1E: "HBR3 8.10"}


def decode_dp_flags(flags: int) -> dict[str, bool]:
    return {name: bool((flags >> i) & 1) for i, name in enumerate(_DP_FLAG_NAMES)}


def get_displayport_info(display_id: int) -> dict[str, Any] | None:
    """Link + capability summary for one display, or None (not DP / call
    unavailable / refused — logged at debug)."""
    try:
        loader = _NvAPILoader()
        if not loader.available():
            return None
        fn = loader._get_function(  # noqa: SLF001 — shared loader
            _DP_INFO, NvAPI_Status,
            [ctypes.c_void_p, NvU32, ctypes.POINTER(NV_DISPLAY_PORT_INFO_V2)],
        )
        if fn is None:
            return None
        info = NV_DISPLAY_PORT_INFO_V2()
        info.version = NV_DISPLAY_PORT_INFO_V2_VER
        rc = int(fn(None, NvU32(display_id), ctypes.byref(info)))
    except Exception as exc:  # noqa: BLE001
        log.get().debug(f"NVAPI dp-info 0x{display_id:08X}: {exc}")
        return None
    if rc != NVAPI_OK:
        log.get().debug(f"NVAPI dp-info 0x{display_id:08X}: {_status_text(rc)} ({rc})")
        return None
    flags = decode_dp_flags(int(info.flags))
    lanes = int(info.curLaneCount)
    rate_gbps = int(info.curLinkRate) * 0.27
    payload_gbps = round(rate_gbps * lanes * 0.8, 2)   # 8b/10b line code
    return {
        "dpcd_version": f"{(info.dpcd_ver >> 4) & 0xF}.{info.dpcd_ver & 0xF}",
        "max_link": f"{_DP_LINK_RATE_NAMES.get(int(info.maxLinkRate), str(info.maxLinkRate))} x{int(info.maxLaneCount)}",
        "current_link": f"{_DP_LINK_RATE_NAMES.get(int(info.curLinkRate), str(info.curLinkRate))} x{lanes}",
        "current_payload_gbps": payload_gbps,
        "current": ColorData(color_format=int(info.colorFormat) if info.colorFormat < 0xFE else _UNSET,
                             dynamic_range=int(info.dynamicRange), colorimetry=int(info.colorimetry),
                             bpc=int(info.bpc)).describe(),
        "sink_supports_bpc": [b for b in (6, 8, 10, 12, 16) if flags[f"bpc{b}"]],
        "sink_supports_encodings": [n for n, k in (("RGB444", None), ("YCbCr444", "ycbcr444"),
                                                   ("YCbCr422", "ycbcr422"), ("YCbCr420", "ycbcr420"))
                                    if k is None or flags[k]],
        "current_mode_bpc": [b for b in (6, 8, 10, 12, 16) if flags[f"bpc{b}_on_current_mode"]],
        "current_mode_encodings": [n for n, k in (("RGB444", "rgb444_on_current_mode"),
                                                  ("YCbCr444", "ycbcr444_on_current_mode"),
                                                  ("YCbCr422", "ycbcr422_on_current_mode"),
                                                  ("YCbCr420", "ycbcr420_on_current_mode")) if flags[k]],
        "flags": flags,
        "raw": {"maxLinkRate": int(info.maxLinkRate), "curLinkRate": int(info.curLinkRate),
                "flags_hex": f"0x{int(info.flags):08x}"},
    }


# Bits per pixel on a DisplayPort main link per (bits per channel, encoding).
# RGB and YCbCr 4:4:4 carry three full samples, 4:2:2 two, 4:2:0 one and a half.
_BPP_FACTORS = (("RGB444", 3.0), ("YCbCr444", 3.0), ("YCbCr422", 2.0), ("YCbCr420", 1.5))


def link_bandwidth_math(pclk_hz: int, payload_gbps: float, *, bpcs=(8, 10, 12)) -> dict[str, Any]:
    """Required main-link payload for the current pixel clock at every depth
    and encoding, against what the trained link carries. Pure arithmetic —
    the operator asked for the numbers so they can be checked by hand:
    ``needed = pixel clock × bits per pixel``; payload = lanes × rate × 0.8
    (8b/10b). DSC would change this and is not modelled."""
    rows = []
    for bpc in bpcs:
        for enc, factor in _BPP_FACTORS:
            bpp = bpc * factor
            needed = pclk_hz * bpp / 1e9
            rows.append({
                "format": f"{bpc}-bit {enc}",
                "bits_per_pixel": bpp,
                "needed_gbps": round(needed, 2),
                "fits": bool(payload_gbps and needed <= payload_gbps),
            })
    return {
        "pixel_clock_mhz": round(pclk_hz / 1e6, 3),
        "link_payload_gbps": payload_gbps,
        "formula": "needed Gbit/s = pixel clock × bits/pixel; payload = lanes × Gbit/s per lane × 0.8 (8b/10b)",
        "formats": rows,
        "fits": [r["format"] for r in rows if r["fits"]],
    }


def describe_displays(
    display_ids: list[tuple[int, str]],
    probe: ColorData | None = None,
) -> list[dict[str, Any]]:
    """``GET /displays/color`` body: live + driver-default colour per display.

    ``probe`` (e.g. 12-bit YCbCr422) is asked IS_SUPPORTED on every display
    without changing anything — the preflight for a new ``- 12b -`` template.
    Fields the probe leaves unset are taken from the display's current
    settings, exactly as the apply would.
    """
    out: list[dict[str, Any]] = []
    pclks: dict[int, int] = {}
    try:
        from . import timing as timing_mod
        if timing_mod.is_available():
            pclks = {d: int(v[0]) for d, v in timing_mod.live_timings().items()}
    except Exception:  # noqa: BLE001
        pclks = {}
    for did, hexid in display_ids:
        entry: dict[str, Any] = {"display": hexid}
        live = get_color_data(did)
        if live is None:
            entry["error"] = "GET failed (see agent log)"
        else:
            entry["current"] = live.to_public()
        default = get_default_color_data(did)
        if default is not None:
            entry["driver_default"] = default.to_public()
        link = get_displayport_info(did)
        if link is not None:
            if pclks.get(did):
                link["bandwidth"] = link_bandwidth_math(pclks[did], float(link.get("current_payload_gbps") or 0))
            entry["link"] = link
        if probe is not None and not probe.is_empty():
            req = _merge_request(probe, live)
            entry["probe"] = {
                "request": req.describe(),
                "supported": is_supported(did, req),
                "already": bool(live is not None and probe.satisfied_by(live)),
            }
        out.append(entry)
    return out


def parse_probe(text: str) -> ColorData:
    """``probe=12`` / ``probe=12,Ycc422`` / ``probe=12,Ycc422,BT709`` ->
    ColorData. Unknown pieces are ignored (left unset)."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    bits = 0
    enc = ""
    cs = ""
    for p in parts:
        if p.isdigit() and not bits:
            bits = int(p)
        elif p.lower().replace("-", "").replace("_", "") in _PIXEL_ENCODING_TO_FORMAT and not enc:
            enc = p
        elif not cs:
            cs = p
    return color_data_from_strings(pixel_encoding=enc, bits_per_channel=bits, color_space=cs)
