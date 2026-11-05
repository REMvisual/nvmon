"""Exact display timings via NVAPI custom displays.

Why this module exists (site, 2026-11-02): the RTX PRO Sync card would not
frame-lock any panel running the CTA-861 4K timings whose pixel clock is
exactly 297.000 MHz (4K24 5500x2250, 4K25 5280x2250, 4K30 4400x2250) —
on two driver branches, through NVCP as well as configureQsync2, with one
or two displays — while 23.976 / 29.97 (296.700 MHz), 59.94 (593.400 MHz)
and 60.000 (594.000 MHz) locked at once. A custom 24.000 Hz timing with
the same structure but 5500x2248 (296.740 MHz) locked immediately.

Windows' mode-set APIs can only pick timings the driver already lists, so
to make such a timing exist on every node without hand work in NVIDIA
Control Panel we use the same NVAPI calls NVCP's "Create Custom
Resolution" uses:

    NvAPI_DISP_GetTiming              read the timing a display runs now
    NvAPI_DISP_TryCustomDisplay       apply a timing immediately (trial)
    NvAPI_DISP_SaveCustomDisplay      persist the trial
    NvAPI_DISP_RevertCustomDisplayTrial
    NvAPI_DISP_EnumCustomDisplay / DeleteCustomDisplay

A profile carries the full timing (``display_ids[<id>]["timing"]``), so
the apply reproduces the exact pixel clock and totals, not a rate label.

Non-Windows / no NVAPI: every call returns ``None`` or raises
``TimingUnavailable``; nothing fails at import time.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import log
from .backends.nvapi import NVAPI_OK, NvAPI_Status, NvU32, _NvAPILoader

# --- NVAPI enums ------------------------------------------------------------

NV_TIMING_OVERRIDE_CURRENT = 0
NV_TIMING_OVERRIDE_AUTO = 1
NV_TIMING_OVERRIDE_EDID = 2
NV_TIMING_OVERRIDE_DMT = 3
NV_TIMING_OVERRIDE_DMT_RB = 4
NV_TIMING_OVERRIDE_CVT = 5
NV_TIMING_OVERRIDE_CVT_RB = 6
NV_TIMING_OVERRIDE_GTF = 7
NV_TIMING_OVERRIDE_EIA861 = 8
NV_TIMING_OVERRIDE_ANALOG_TV = 9
NV_TIMING_OVERRIDE_CUST = 10

NV_FORMAT_A8R8G8B8 = 21

# Pixel clocks the RTX PRO Sync will not lock to on site (Hz). Exact match.
POISON_PIXEL_CLOCKS_HZ = (297_000_000,)

# --- structs (nvapi.h) -------------------------------------------------------


class NV_TIMINGEXT(ctypes.Structure):
    _fields_ = [
        ("flag", ctypes.c_uint32),
        ("rr", ctypes.c_uint16),
        ("rrx1k", ctypes.c_uint32),
        ("aspect", ctypes.c_uint32),
        ("rep", ctypes.c_uint16),
        ("status", ctypes.c_uint32),
        ("name", ctypes.c_uint8 * 40),
    ]


class NV_TIMING(ctypes.Structure):
    _fields_ = [
        ("HVisible", ctypes.c_uint16),
        ("HBorder", ctypes.c_uint16),
        ("HFrontPorch", ctypes.c_uint16),
        ("HSyncWidth", ctypes.c_uint16),
        ("HTotal", ctypes.c_uint16),
        ("HSyncPol", ctypes.c_uint8),
        ("VVisible", ctypes.c_uint16),
        ("VBorder", ctypes.c_uint16),
        ("VFrontPorch", ctypes.c_uint16),
        ("VSyncWidth", ctypes.c_uint16),
        ("VTotal", ctypes.c_uint16),
        ("VSyncPol", ctypes.c_uint8),
        ("interlaced", ctypes.c_uint16),
        ("pclk", ctypes.c_uint32),          # in 10 kHz units
        ("etc", NV_TIMINGEXT),
    ]


class NV_TIMING_INPUT(ctypes.Structure):
    # 32 bytes on driver 597.30 (probed on RENDER-01, 2026-11-02): the
    # 24-byte layout from older headers is rejected with
    # NVAPI_INCOMPATIBLE_STRUCT_VERSION; `type` lives at offset 24.
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("rr", ctypes.c_float),
        ("flag", ctypes.c_uint32),          # NV_TIMING_FLAG bitfield (isInterlaced:4 ...)
        ("flag_hi", ctypes.c_uint32),       # second flag word / padding on current drivers
        ("type", ctypes.c_uint32),          # NV_TIMING_OVERRIDE
        ("reserved", ctypes.c_uint32),
    ]


class NV_VIEWPORTF(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float),
                ("w", ctypes.c_float), ("h", ctypes.c_float)]


class NV_CUSTOM_DISPLAY(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("depth", ctypes.c_uint32),
        ("colorFormat", ctypes.c_uint32),   # NV_FORMAT
        ("srcPartition", NV_VIEWPORTF),
        ("xRatio", ctypes.c_float),
        ("yRatio", ctypes.c_float),
        ("timing", NV_TIMING),
        ("flags", ctypes.c_uint32),         # hwModeSetOnly : 1
    ]


# Layout guards — a wrong size means a wrong struct version and NVAPI
# rejects the call (NVAPI_INCOMPATIBLE_STRUCT_VERSION) rather than
# corrupting memory, but assert anyway so a packing surprise is loud.
assert ctypes.sizeof(NV_TIMING) == 96, ctypes.sizeof(NV_TIMING)
assert ctypes.sizeof(NV_TIMING_INPUT) == 32, ctypes.sizeof(NV_TIMING_INPUT)
assert ctypes.sizeof(NV_CUSTOM_DISPLAY) == 144, ctypes.sizeof(NV_CUSTOM_DISPLAY)

NV_TIMING_INPUT_VER = (ctypes.sizeof(NV_TIMING_INPUT) | (1 << 16)) & 0xFFFFFFFF
NV_CUSTOM_DISPLAY_VER = (ctypes.sizeof(NV_CUSTOM_DISPLAY) | (1 << 16)) & 0xFFFFFFFF


class TimingUnavailable(RuntimeError):
    """NVAPI not loaded, function not exported by this driver, or a call failed."""


# --- plain data ----------------------------------------------------------------


@dataclass
class Timing:
    """A complete progressive display timing. ``pclk_hz`` is the pixel clock
    in Hz; the driver stores it in 10 kHz units (``pclk_10khz``)."""
    h_visible: int
    h_front: int
    h_sync: int
    h_total: int
    v_visible: int
    v_front: int
    v_sync: int
    v_total: int
    pclk_hz: int
    h_pol: int = 0          # nvapi.h NV_TIMING: 0 = positive, 1 = negative (CTA 4K is +/+)
    v_pol: int = 0
    interlaced: int = 0
    rr_x1k: int = 0         # vertical rate ×1000 as the driver reports it
    name: str = ""
    source: str = ""        # "nvapi:current" | "designed" | ...
    extra: dict = field(default_factory=dict)

    # -- derived
    @property
    def pclk_10khz(self) -> int:
        return int(round(self.pclk_hz / 10_000))

    @property
    def h_back(self) -> int:
        return self.h_total - self.h_visible - self.h_front - self.h_sync

    @property
    def v_back(self) -> int:
        return self.v_total - self.v_visible - self.v_front - self.v_sync

    @property
    def exact_hz(self) -> float:
        if not (self.h_total and self.v_total and self.pclk_hz):
            return 0.0
        return self.pclk_hz / (self.h_total * self.v_total)

    @property
    def line_khz(self) -> float:
        return self.pclk_hz / self.h_total / 1000.0 if self.h_total else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        d["exact_hz"] = round(self.exact_hz, 6)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Timing":
        keys = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kw = {k: d[k] for k in keys if k in d}
        for k in ("h_visible", "h_front", "h_sync", "h_total", "v_visible", "v_front",
                  "v_sync", "v_total", "pclk_hz", "h_pol", "v_pol", "interlaced", "rr_x1k"):
            if k in kw:
                kw[k] = int(kw[k])
        return cls(**kw)

    def validate(self) -> list[str]:
        p: list[str] = []
        if self.h_visible <= 0 or self.v_visible <= 0:
            p.append("active size must be positive")
        if self.h_back < 0:
            p.append("horizontal total smaller than active + front + sync")
        if self.v_back < 0:
            p.append("vertical total smaller than active + front + sync")
        if self.pclk_hz <= 0:
            p.append("pixel clock must be positive")
        elif self.pclk_hz % 10_000:
            p.append(f"pixel clock {self.pclk_hz} Hz is not on the driver's 10 kHz grid "
                     f"(it would be programmed as {self.pclk_10khz * 10_000} Hz)")
        if self.pclk_hz in POISON_PIXEL_CLOCKS_HZ:
            p.append(f"pixel clock {self.pclk_hz / 1e6:.3f} MHz is one the RTX PRO Sync "
                     f"will not frame-lock on this rig (site 2026-11-02)")
        return p


def _from_nv(t: NV_TIMING, source: str) -> Timing:
    name = bytes(t.etc.name).split(b"\0", 1)[0].decode("ascii", errors="replace")
    return Timing(
        h_visible=t.HVisible, h_front=t.HFrontPorch, h_sync=t.HSyncWidth, h_total=t.HTotal,
        v_visible=t.VVisible, v_front=t.VFrontPorch, v_sync=t.VSyncWidth, v_total=t.VTotal,
        pclk_hz=int(t.pclk) * 10_000, h_pol=int(t.HSyncPol), v_pol=int(t.VSyncPol),
        interlaced=int(t.interlaced), rr_x1k=int(t.etc.rrx1k), name=name, source=source,
        extra={"rr": int(t.etc.rr), "aspect": int(t.etc.aspect), "rep": int(t.etc.rep),
               "status": int(t.etc.status), "flag": int(t.etc.flag),
               "h_border": int(t.HBorder), "v_border": int(t.VBorder)},
    )


def _to_nv(tm: Timing, template: NV_TIMING | None = None) -> NV_TIMING:
    t = NV_TIMING()
    if template is not None:
        ctypes.memmove(ctypes.byref(t), ctypes.byref(template), ctypes.sizeof(NV_TIMING))
    t.HVisible, t.HBorder = tm.h_visible, 0
    t.HFrontPorch, t.HSyncWidth, t.HTotal = tm.h_front, tm.h_sync, tm.h_total
    t.HSyncPol = tm.h_pol
    t.VVisible, t.VBorder = tm.v_visible, 0
    t.VFrontPorch, t.VSyncWidth, t.VTotal = tm.v_front, tm.v_sync, tm.v_total
    t.VSyncPol = tm.v_pol
    t.interlaced = tm.interlaced
    t.pclk = tm.pclk_10khz
    hz = tm.exact_hz
    t.etc.rr = int(round(hz))
    t.etc.rrx1k = int(round(hz * 1000))
    t.etc.rep = 1
    # etc.status / etc.flag: keep whatever the driver's own record for this
    # display carries (passed in as ``template``); a hand-set
    # NV_TIMING_OVERRIDE_CUST was rejected with NVAPI_INVALID_ARGUMENT on 597.30.
    if template is None:
        t.etc.status = 0
        t.etc.flag = 0
    if not t.etc.aspect:
        t.etc.aspect = (16 << 16) | 9
    label = (tm.name or f"nvmon {tm.h_visible}x{tm.v_visible}@{hz:.3f}").encode("ascii", "replace")[:39]
    name = (ctypes.c_uint8 * 40)()
    for i, b in enumerate(label):
        name[i] = b
    t.etc.name = name
    return t


# --- timing design -------------------------------------------------------------

# CTA-861 horizontal structure for 3840x2160 per nominal rate: the proven
# 23.976 / 29.97 / 59.94 / 60 timings on site all use these (front, sync,
# back). 24 and 25 carry the wide porches of 4K24 / 4K25.
_CTA_4K_H = {
    24: (1276, 88, 296),
    25: (1056, 88, 296),
    30: (176, 88, 296),
    48: (1276, 88, 296),
    50: (1056, 88, 296),
    60: (176, 88, 296),
}
_CTA_4K_V = (8, 10, 72)   # front, sync, back → total 2250


def design_timing(width: int, height: int, rate: float, *, v_total: int | None = None,
                  h_total: int | None = None) -> Timing:
    """Design an exact, sync-friendly timing for ``width x height @ rate``.

    Keeps the CTA horizontal structure for the nominal rate (so the panel
    sees a familiar line rate) and picks the vertical total just below the
    CTA 2250 such that (a) the pixel clock is an exact multiple of the
    driver's 10 kHz grid — so the programmed rate is *exactly* ``rate`` —
    and (b) the clock is not one of the poison values (297.000 MHz).
    For 3840x2160 that yields 5500x2245 @ 24 → 296.340 MHz,
    4400x2245 @ 30 → 296.340 MHz, 5280x2245 @ 25 → 296.340 MHz.
    Integer-rate requests only; the 1.001 rates already lock on the
    driver's own timings.
    """
    nominal = int(round(rate))
    if abs(rate - nominal) > 0.001:
        raise ValueError("design_timing is for integer rates; 1.001 rates use the driver's own timing")
    if (width, height) == (3840, 2160) and nominal in _CTA_4K_H:
        hf, hs, hb = _CTA_4K_H[nominal]
    else:
        # CVT-RB style blanking for anything else.
        hf, hs, hb = 48, 32, 80
    vf, vs, vb_min = _CTA_4K_V[0], _CTA_4K_V[1], 20
    ht = int(h_total) if h_total else width + hf + hs + hb
    v_max = height + vf + vs + (_CTA_4K_V[2] if (width, height) == (3840, 2160) else 60)
    v_min = height + vf + vs + vb_min
    candidates = [int(v_total)] if v_total else list(range(v_max, v_min - 1, -1))
    chosen = None
    for vt in candidates:
        pclk = ht * vt * nominal
        if pclk % 10_000 == 0 and pclk not in POISON_PIXEL_CLOCKS_HZ:
            chosen = (vt, pclk)
            break
    if chosen is None:
        raise ValueError(f"no vertical total in [{v_min}, {v_max}] gives an exact 10 kHz clock for "
                         f"{width}x{height}@{nominal} with H total {ht}")
    vt, pclk = chosen
    return Timing(
        h_visible=width, h_front=hf, h_sync=hs, h_total=ht,
        v_visible=height, v_front=vf, v_sync=vs, v_total=vt,
        pclk_hz=pclk, h_pol=0, v_pol=0, interlaced=0, rr_x1k=nominal * 1000,
        name=f"nvmon {width}x{height}@{nominal} {pclk / 1e6:.2f}MHz", source="designed",
    )


# --- NVAPI calls ---------------------------------------------------------------


def is_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return _NvAPILoader().available()
    except Exception:  # noqa: BLE001
        return False


def _fn(name: str, restype, argtypes):
    loader = _NvAPILoader()
    if not loader.available():
        raise TimingUnavailable(f"NVAPI unavailable: {loader.init_error()}")
    fn = loader._get_function(name, restype, argtypes)  # noqa: SLF001 — shared loader
    if fn is None:
        raise TimingUnavailable(f"{name} not exported by this driver")
    return fn


def _status_text(status: int) -> str:
    try:
        return _NvAPILoader().get_error_message(status)
    except Exception:  # noqa: BLE001
        return f"NVAPI status {status}"


def _ids_array(display_ids: list[int]):
    arr = (NvU32 * len(display_ids))(*[int(d) for d in display_ids])
    return arr


def parse_display_id(value: Any) -> int:
    """NVAPI display ids are hex everywhere in nvmon ("80061085", "0x80061085")."""
    if isinstance(value, int):
        return value
    return int(str(value).strip().lower().removeprefix("0x"), 16)


def get_timing(display_id: int, *, width: int = 0, height: int = 0, rate: float = 0.0,
               kind: int = NV_TIMING_OVERRIDE_AUTO) -> Timing:
    """Ask the driver for a timing on ``display_id``.

    On 597.30 ``kind=CURRENT`` returns NVAPI_ERROR; ``AUTO`` (and CVT_RB /
    EIA861) with width/height/rr = 0 returns the driver's timing record for
    the display's active mode — e.g. "HDMI EXT: 3840x2160x23.98/24hz",
    5500x2250, pclk 297.00 MHz, rrx1k 23976 — i.e. the EDID entry that the
    driver labels as both 23.98 and 24. Other kinds return the formula
    timing for ``width x height @ rate``.
    """
    fn = _fn("NvAPI_DISP_GetTiming", NvAPI_Status,
             [NvU32, ctypes.POINTER(NV_TIMING_INPUT), ctypes.POINTER(NV_TIMING)])
    inp = NV_TIMING_INPUT()
    inp.version = NV_TIMING_INPUT_VER
    inp.width, inp.height, inp.rr = int(width), int(height), float(rate)
    inp.flag, inp.flag_hi, inp.type, inp.reserved = 0, 0, int(kind), 0
    out = NV_TIMING()
    status = fn(NvU32(int(display_id)), ctypes.byref(inp), ctypes.byref(out))
    if status != NVAPI_OK:
        raise TimingUnavailable(f"NvAPI_DISP_GetTiming(0x{int(display_id):x}): {_status_text(status)}")
    return _from_nv(out, f"nvapi:kind{kind}")


def current_timing(display_id: int, *, pclk_hz: int = 0, h_total: int = 0, v_total: int = 0) -> Timing:
    """Best available description of what ``display_id`` runs now.

    The exact pixel clock and totals come from Windows CCD (caller passes
    them from ``ccd.describe()``); the porches come from the driver record
    whose totals match — a saved custom timing first, else the EDID/formula
    record from ``get_timing(AUTO)``. Falls back to the AUTO record with the
    CCD numbers overlaid.
    """
    base = None
    try:
        for c in enum_custom(display_id):
            if (not h_total or c.h_total == h_total) and (not v_total or c.v_total == v_total):
                base = c
                break
    except TimingUnavailable:
        base = None
    if base is None:
        base = get_timing(display_id)
    if pclk_hz:
        base.pclk_hz = int(pclk_hz)
    if h_total and base.h_total != h_total:
        base.h_front += int(h_total) - base.h_total   # keep sync/back, absorb in front porch
        base.h_total = int(h_total)
    if v_total and base.v_total != v_total:
        base.v_front += int(v_total) - base.v_total
        base.v_total = int(v_total)
    base.rr_x1k = int(round(base.exact_hz * 1000)) if base.exact_hz else base.rr_x1k
    base.source = "nvapi+ccd"
    return base


def _custom_struct(tm: Timing, template: NV_TIMING | None = None) -> NV_CUSTOM_DISPLAY:
    cd = NV_CUSTOM_DISPLAY()
    cd.version = NV_CUSTOM_DISPLAY_VER
    cd.width, cd.height, cd.depth = tm.h_visible, tm.v_visible, 32
    cd.colorFormat = NV_FORMAT_A8R8G8B8
    cd.srcPartition = NV_VIEWPORTF(0.0, 0.0, 1.0, 1.0)
    cd.xRatio, cd.yRatio = 1.0, 1.0
    cd.timing = _to_nv(tm, template)
    cd.flags = 0
    return cd


def _driver_record(display_id: int) -> NV_TIMING | None:
    """The driver's own NV_TIMING record for the display's active mode
    (``GetTiming`` kind AUTO) — used as the template so etc.status/flag/aspect
    carry the values this driver expects."""
    try:
        gt = _fn("NvAPI_DISP_GetTiming", NvAPI_Status,
                 [NvU32, ctypes.POINTER(NV_TIMING_INPUT), ctypes.POINTER(NV_TIMING)])
        cur = NV_TIMING()
        inp = NV_TIMING_INPUT(version=NV_TIMING_INPUT_VER, type=NV_TIMING_OVERRIDE_AUTO)
        if gt(NvU32(int(display_id)), ctypes.byref(inp), ctypes.byref(cur)) == NVAPI_OK:
            return cur
    except Exception:  # noqa: BLE001
        pass
    return None


def try_custom(display_ids: list[int], tm: Timing) -> None:
    """Apply ``tm`` to the displays NOW as a trial (what NVCP's "Test" does).

    One NVAPI call per display: the two panels sit on different GPUs and a
    single call with both ids was refused (NVAPI_INVALID_ARGUMENT, 597.30).
    Must run in the interactive session (the listener does); from an SSH
    session the same call returns a bare NVAPI_ERROR.
    Raises TimingUnavailable with the NVAPI message on refusal.
    """
    problems = [p for p in tm.validate() if "will not frame-lock" not in p]
    if problems:
        raise ValueError("; ".join(problems))
    fn = _fn("NvAPI_DISP_TryCustomDisplay", NvAPI_Status,
             [ctypes.POINTER(NvU32), NvU32, ctypes.POINTER(NV_CUSTOM_DISPLAY)])
    for d in display_ids:
        cd = _custom_struct(tm, _driver_record(int(d)))
        ids = _ids_array([int(d)])
        status = fn(ids, NvU32(1), ctypes.byref(cd))
        if status != NVAPI_OK:
            raise TimingUnavailable(f"NvAPI_DISP_TryCustomDisplay(0x{int(d):x}): {_status_text(status)}")


def save_custom(display_ids: list[int]) -> str:
    """Persist the live trial. Tries the scope flags in order
    (monitor-only → output-only → both) because the driver answers a bare
    NVAPI_ERROR for some scopes on some nodes (srv5 after a clean driver
    reinstall, 2026-11-02). Returns the scope that worked."""
    fn = _fn("NvAPI_DISP_SaveCustomDisplay", NvAPI_Status,
             [ctypes.POINTER(NvU32), NvU32, NvU32, NvU32])
    ids = _ids_array(display_ids)
    last = ""
    for out_only, mon_only, label in ((0, 1, "monitor"), (1, 0, "output"), (0, 0, "any"), (1, 1, "output+monitor")):
        status = fn(ids, NvU32(len(display_ids)), NvU32(out_only), NvU32(mon_only))
        if status == NVAPI_OK:
            return label
        last = _status_text(status)
    raise TimingUnavailable(f"NvAPI_DISP_SaveCustomDisplay: {last}")


def revert_trial(display_ids: list[int]) -> None:
    fn = _fn("NvAPI_DISP_RevertCustomDisplayTrial", NvAPI_Status, [ctypes.POINTER(NvU32), NvU32])
    ids = _ids_array(display_ids)
    status = fn(ids, NvU32(len(display_ids)))
    if status != NVAPI_OK:
        raise TimingUnavailable(f"NvAPI_DISP_RevertCustomDisplayTrial: {_status_text(status)}")


def enum_custom(display_id: int, limit: int = 32) -> list[Timing]:
    fn = _fn("NvAPI_DISP_EnumCustomDisplay", NvAPI_Status,
             [NvU32, NvU32, ctypes.POINTER(NV_CUSTOM_DISPLAY)])
    out: list[Timing] = []
    for idx in range(limit):
        cd = NV_CUSTOM_DISPLAY()
        cd.version = NV_CUSTOM_DISPLAY_VER
        status = fn(NvU32(int(display_id)), NvU32(idx), ctypes.byref(cd))
        if status != NVAPI_OK:
            break
        out.append(_from_nv(cd.timing, "nvapi:custom"))
    return out


def delete_custom(display_ids: list[int], tm: Timing) -> None:
    fn = _fn("NvAPI_DISP_DeleteCustomDisplay", NvAPI_Status,
             [ctypes.POINTER(NvU32), NvU32, ctypes.POINTER(NV_CUSTOM_DISPLAY)])
    cd = _custom_struct(tm)
    ids = _ids_array(display_ids)
    status = fn(ids, NvU32(len(display_ids)), ctypes.byref(cd))
    if status != NVAPI_OK:
        raise TimingUnavailable(f"NvAPI_DISP_DeleteCustomDisplay: {_status_text(status)}")


def live_timings() -> dict[int, tuple[int, int, int]]:
    """``{nvapi_display_id: (pclk_hz, h_total, v_total)}`` for every active
    panel — the exact numbers Windows CCD reports, joined to NVAPI ids via
    the configureDdisplay table (DisplayId ↔ OS TargetId)."""
    from . import ccd, displays
    by_target: dict[str, int] = {}
    for d in (displays.status().displays or []):
        if d.id and d.os_target_id and d.id.lower() not in ("<none>", "none"):
            try:
                by_target[d.os_target_id.lower().removeprefix("0x")] = parse_display_id(d.id)
            except ValueError:
                continue
    out: dict[int, tuple[int, int, int]] = {}
    for p in ccd.describe():
        did = by_target.get(str(p.get("target_id", "")).lower())
        if did is not None and p.get("pixel_rate_hz"):
            out[did] = (int(p["pixel_rate_hz"]), int(p["h_total"]), int(p["v_total"]))
    return out


def apply_custom_timing(display_ids: list[int], tm: Timing, *, save: bool = True,
                        settle_seconds: float = 3.0, verify=None) -> dict:
    """Trial → verify the driver programmed it → save (or revert).

    ``verify`` returns ``{display_id_int: (pclk_hz, h_total, v_total)}`` for
    the active paths; default ``live_timings()`` (Windows CCD numbers, which
    is what the sync card is really driven by). A trial whose read-back
    differs from the request is reverted and reported, never saved.
    """
    from .. import executors
    logger = log.get()
    verify = verify or live_timings
    logger.info(
        f"Timing: trial {tm.h_visible}x{tm.v_visible} {tm.h_total}x{tm.v_total} "
        f"@ {tm.exact_hz:.6f} Hz, pclk {tm.pclk_hz / 1e6:.3f} MHz on "
        + ", ".join(f"0x{int(d):x}" for d in display_ids)
    )
    # A stored custom timing for the same size and rate but different
    # totals/clock (e.g. one made by hand in NVCP) makes SaveCustomDisplay
    # fail with a bare NVAPI_ERROR (srv5, 2026-11-02: hand-made 5500x2248
    # vs nvmon's 5500x2245). Remove such conflicts first; an identical
    # entry is kept and noted so a later Save failure is not fatal.
    identical: set[int] = set()
    for d in display_ids:
        try:
            existing = enum_custom(int(d))
        except TimingUnavailable:
            existing = []
        for c in existing:
            same_mode = (c.h_visible, c.v_visible) == (tm.h_visible, tm.v_visible) and \
                abs(c.exact_hz - tm.exact_hz) < 0.5
            if not same_mode:
                continue
            if (c.h_total, c.v_total, c.pclk_hz) == (tm.h_total, tm.v_total, tm.pclk_hz):
                identical.add(int(d))
                continue
            logger.info(
                f"Timing: removing conflicting custom timing on 0x{int(d):x}: "
                f"{c.h_total}x{c.v_total} @ {c.pclk_hz / 1e6:.3f} MHz ('{c.name}')"
            )
            try:
                delete_custom([int(d)], c)
            except TimingUnavailable as exc:
                logger.warning(f"Timing: could not remove conflicting custom timing: {exc}")
    try_custom(display_ids, tm)
    executors.sleep(settle_seconds, "custom timing settle")
    result: dict[str, Any] = {"applied": [], "mismatch": [], "saved": False}
    # A large clock change (24 → 60: 296 → 594 MHz) retrains the DP link and
    # the path is briefly absent from QueryDisplayConfig; srv5 2026-11-02
    # reported "not found in CCD read-back" and reverted a good trial. Poll
    # until every display is present and matches, up to ~12 s.
    deadline_polls = 5
    seen: dict[int, tuple[int, int, int]] = {}
    for attempt in range(deadline_polls):
        try:
            seen = verify()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Timing: read-back failed: {exc}")
            seen = {}
        if all(int(d) in seen and seen[int(d)] == (tm.pclk_hz, tm.h_total, tm.v_total) for d in display_ids):
            break
        if attempt < deadline_polls - 1:
            executors.sleep(2.0, "custom timing read-back retry")
    for d in display_ids:
        vals = seen.get(int(d))
        ok = bool(vals and vals[0] == tm.pclk_hz and vals[1] == tm.h_total and vals[2] == tm.v_total)
        detail = (f"pclk {vals[0]} totals {vals[1]}x{vals[2]}" if vals else "not found in CCD read-back")
        (result["applied"] if ok else result["mismatch"]).append({"display": f"0x{int(d):x}", "detail": detail})
    if result["mismatch"]:
        logger.error(f"Timing: driver did not program the requested timing: {result['mismatch']} — reverting")
        try:
            revert_trial(display_ids)
        except TimingUnavailable as exc:
            logger.warning(f"Timing: revert failed: {exc}")
        return result
    if save:
        try:
            scope = save_custom(display_ids)
            result["saved"] = True
            logger.info(f"Timing: custom timing saved (scope: {scope}; persists across reboots)")
        except TimingUnavailable as exc:
            # The trial is live and verified — the panel IS running the exact
            # timing. Persistence is a convenience: boot-apply re-applies the
            # profile (and therefore this trial) on every boot anyway. Report
            # it, do not fail the apply.
            if identical and all(int(d) in identical for d in display_ids):
                result["saved"] = True
                result["note"] = f"identical custom timing already stored; Save said: {exc}"
            else:
                result["saved"] = False
                result["note"] = (f"timing is live but could not be persisted ({exc}); "
                                  f"boot-apply will re-apply it on the next boot")
            logger.warning(f"Timing: {result['note']}")
    return result
