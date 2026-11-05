"""NVAPI backends — display, Mosaic, QSync, EDID-read via ``nvapi64.dll``.

NVAPI is Windows-only. The DLL ships with every NVIDIA driver in
``C:\\Windows\\System32\\nvapi64.dll``. We do NOT vendor the NVAPI SDK
headers — all struct definitions in this file are hand-translated from
NVIDIA's publicly documented function signatures (spec section 6.2).

Function access pattern (unique to NVAPI):

    NVAPI exports a single symbol: ``nvapi_QueryInterface(FunctionId)``. It
    returns a function pointer for the requested NVAPI call. Each call has
    a well-known 32-bit ID (published by NVIDIA). We call QueryInterface
    once per function at init time and cache the pointer.

Struct-version pattern:

    Every NVAPI struct's first field is a ``uint32 version`` made by
    ``MAKE_NVAPI_VERSION(sizeof_struct, version_number)`` which is::

        (sizeof_struct) | (version_number << 16)

    NVAPI returns ``NVAPI_INCOMPATIBLE_STRUCT_VERSION`` (-9) when the
    driver rejects the version. We surface that as
    :class:`BackendStructMismatch`, and the registry auto-blacklists the
    (backend, subsystem) pair.

On non-Windows or when ``nvapi64.dll`` isn't present, every backend's
:meth:`available` returns False and registration is silently skipped.

------------------------------------------------------------------------
Scope of implementation (as of v0.2 / this commit)
------------------------------------------------------------------------

The spec (section 3) lists the target function set. This file provides:

    Confirmed signatures (wired up):
      - NvAPI_Initialize                (function-id 0x0150E828)
      - NvAPI_Unload                    (function-id 0xD22BDD7E)
      - NvAPI_EnumPhysicalGPUs          (function-id 0xE5AC921F)
      - NvAPI_GPU_GetFullName           (function-id 0xCEEE8E9F)
      - NvAPI_GPU_GetBusId              (function-id 0x1BE0B8E5)

    TODO (post-wave-2 — needs NVAPI SDK or context7 confirmation):
      - NvAPI_DISP_GetDisplayConfig             (struct: NV_DISPLAYCONFIG_PATH_INFO_V2)
      - NvAPI_DISP_GetDisplayIdByDisplayName
      - NvAPI_GPU_GetConnectedDisplayIds
      - NvAPI_GPU_GetEDID                       (struct: NV_EDID_V3)
      - NvAPI_Mosaic_GetCurrentTopo             (struct: NV_MOSAIC_TOPO_BRIEF + _V2)
      - NvAPI_Mosaic_GetDisplayViewportsByResolution
      - NvAPI_GSync_EnumSyncDevices
      - NvAPI_GSync_GetStatusParameters         (struct: NV_GSYNC_STATUS_PARAMS_V2)
      - NvAPI_GSync_GetSyncStatus               (struct: NV_GSYNC_STATUS_V2)
      - NvAPI_GSync_GetControlParameters

    Each TODO call raises :class:`BackendCallFailed` with a TODO message
    until its signature is confirmed. The registry falls back to the
    shell backend automatically.

Maintainers: when adding a confirmed signature, (a) define the struct
here, (b) register the function-id in ``_FUNCTION_IDS``, (c) wire the
call into the relevant backend's ``status()`` / write method, (d) add a
citation comment pointing at NVIDIA's developer docs.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
from pathlib import Path
from typing import Any

from ... import log
from ...types import DDState, MosaicState, QSyncState
from .exceptions import (
    BackendCallFailed,
    BackendStructMismatch,
    BackendUnavailable,
)


# ---------------------------------------------------------------------------
# NVAPI status codes (subset)
# ---------------------------------------------------------------------------

NVAPI_OK = 0
NVAPI_ERROR = -1
NVAPI_LIBRARY_NOT_FOUND = -2
NVAPI_NO_IMPLEMENTATION = -3
NVAPI_API_NOT_INITIALIZED = -4
NVAPI_INVALID_ARGUMENT = -5
NVAPI_NVIDIA_DEVICE_NOT_FOUND = -6
NVAPI_END_ENUMERATION = -7
NVAPI_INVALID_HANDLE = -8
# nvapi.h: -9 / -10. Verified 2026-11-03 — a 22-byte NV_COLOR_DATA passed to
# NvAPI_Disp_ColorControl on 596.86 is answered with -9. (Was -11 / -12 here,
# so struct mismatches were classified as generic call failures.)
NVAPI_INCOMPATIBLE_STRUCT_VERSION = -9
NVAPI_HANDLE_INVALIDATED = -10
NVAPI_NOT_SUPPORTED = -104


# ---------------------------------------------------------------------------
# NVAPI constants
# ---------------------------------------------------------------------------

NVAPI_MAX_PHYSICAL_GPUS = 64
NVAPI_SHORT_STRING_MAX = 64
NVAPI_GENERIC_STRING_MAX = 4096


# ---------------------------------------------------------------------------
# Handle opaque types — NVAPI uses opaque pointers.
# ---------------------------------------------------------------------------

NvPhysicalGpuHandle = ctypes.c_void_p
NvDisplayHandle = ctypes.c_void_p
NvLogicalGpuHandle = ctypes.c_void_p
NvGSyncDeviceHandle = ctypes.c_void_p
NvU32 = ctypes.c_uint32
NvS32 = ctypes.c_int32
NvU8 = ctypes.c_uint8

NvAPI_Status = ctypes.c_int32


# ---------------------------------------------------------------------------
# Confirmed NVAPI function-id map.
#
# These IDs are stable, widely published, and appear in every community
# NVAPI binding. Do NOT add an ID here unless confirmed from multiple
# sources or directly from NVIDIA's developer site.
# ---------------------------------------------------------------------------

_FUNCTION_IDS: dict[str, int] = {
    "NvAPI_Initialize":          0x0150E828,
    "NvAPI_Unload":              0xD22BDD7E,
    "NvAPI_GetErrorMessage":     0x6C2D048C,
    "NvAPI_EnumPhysicalGPUs":    0xE5AC921F,
    "NvAPI_GPU_GetFullName":     0xCEEE8E9F,
    "NvAPI_GPU_GetBusId":        0x1BE0B8E5,
    # Display timing / custom display (nvapi.h "Display Control" group).
    # Struct signatures: NV_TIMING_INPUT (24 B, v1), NV_TIMING (96 B),
    # NV_CUSTOM_DISPLAY (144 B, v1) — translated in nvmon/agent/timing.py
    # with sizeof() asserts. IDs from nvapi_interface (public), used by
    # every open NVAPI binding. Added 2026-11-02 to push exact custom
    # timings fleet-wide (site: the 297.000 MHz CTA 4K24/25/30 clocks will
    # not frame-lock on RTX PRO Sync; an off-grid clock does).
    "NvAPI_DISP_GetTiming":                 0x175167E9,
    "NvAPI_DISP_EnumCustomDisplay":         0xA2072D59,
    "NvAPI_DISP_TryCustomDisplay":          0x1F7DB630,
    "NvAPI_DISP_SaveCustomDisplay":         0x49882876,
    "NvAPI_DISP_RevertCustomDisplayTrial":  0xCBBD40F0,
    "NvAPI_DISP_DeleteCustomDisplay":       0x552E5B9B,
    # Colour depth / pixel encoding (nvapi.h NvAPI_Disp_ColorControl: one
    # call, GET/SET/IS_SUPPORTED/GET_DEFAULT command byte, NV_COLOR_DATA_V5
    # 24 B) — translated in nvmon/agent/nvapi.py. Probed 2026-11-03 on an
    # RTX PRO 6000 / 596.86: resolves, GET returns the NVCP values.
    "NvAPI_Disp_ColorControl":              0x92F9D80D,
    # DisplayPort link + sink colour capabilities (NV_DISPLAY_PORT_INFO_V2,
    # 44 B). Probed 2026-11-04 on 596.86: resolves, V2 accepted with
    # hNvDisplay=NULL and the NvU32 display id as outputId.
    "NvAPI_GetDisplayPortInfo":             0xC64FF367,
    # TODO: add the following once struct signatures are confirmed.
    # See file docstring. Placeholder IDs intentionally omitted so calls
    # that depend on them stay in the TODO path and fall back to shell.
    #
    # "NvAPI_DISP_GetDisplayConfig":           0x11ABCCF8,
    # "NvAPI_GPU_GetConnectedDisplayIds":      0x0078DBA2,
    # "NvAPI_GPU_GetEDID":                     0x37D32E69,
    # "NvAPI_Mosaic_GetCurrentTopo":           0xEC32944E,
    # "NvAPI_Mosaic_GetDisplayViewportsByResolution": 0xDC6DC8D3,
    # "NvAPI_GSync_EnumSyncDevices":           0xD9639601,
    # "NvAPI_GSync_GetStatusParameters":       0x70D404EC,
    # "NvAPI_GSync_GetSyncStatus":             0xF1F5B434,
}


def _make_nvapi_version(struct_cls: type, version: int) -> int:
    """Compute NVAPI struct version = sizeof(struct) | (version << 16)."""
    return (ctypes.sizeof(struct_cls) | (version << 16)) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Hand-translated NVAPI struct definitions
# ---------------------------------------------------------------------------

class NvAPI_ShortString(ctypes.Structure):
    """NVAPI-typed 64-char short string. Declared as an array wrapper so
    we can ctypes-cast into bytes.
    """
    _fields_ = [("data", ctypes.c_char * NVAPI_SHORT_STRING_MAX)]


# TODO: add NV_DISPLAYCONFIG_PATH_INFO_V2, NV_MOSAIC_TOPO_BRIEF,
# NV_MOSAIC_GRID_TOPO_V2, NV_GSYNC_STATUS_PARAMS_V2, NV_GSYNC_STATUS_V2,
# NV_EDID_V3 once signatures are confirmed from NVAPI SDK. See file
# docstring.


# ---------------------------------------------------------------------------
# Loader — shared across all NVAPI backends
# ---------------------------------------------------------------------------

class _NvAPILoader:
    """Process-wide loader for ``nvapi64.dll``.

    Implements reference-counted init/unload so multiple backend instances
    share one NvAPI session. Thread-safe via `_lock`.
    """

    _instance: "_NvAPILoader | None" = None
    _cls_lock = threading.Lock()

    def __new__(cls) -> "_NvAPILoader":
        with cls._cls_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False  # type: ignore[attr-defined]
                cls._instance._init_error = ""  # type: ignore[attr-defined]
                cls._instance._refcount = 0  # type: ignore[attr-defined]
                cls._instance._dll = None  # type: ignore[attr-defined]
                cls._instance._fn_cache = {}  # type: ignore[attr-defined]
                cls._instance._lock = threading.Lock()  # type: ignore[attr-defined]
            return cls._instance

    # ------------------------------------------------------------------

    def available(self) -> bool:
        """True when ``nvapi64.dll`` was loaded and ``NvAPI_Initialize``
        returned OK. False on non-Windows, missing DLL, or init failure.
        """
        if sys.platform != "win32":
            return False
        with self._lock:
            if not self._initialized:
                self._try_init()
            return self._initialized

    def init_error(self) -> str:
        return self._init_error

    # ------------------------------------------------------------------

    def _try_init(self) -> None:
        """Load nvapi64.dll and call NvAPI_Initialize. Run once per process."""
        if sys.platform != "win32":
            self._init_error = "not running on Windows"
            return

        # Prefer System32 path (always present when driver installed).
        dll_path = Path(r"C:\Windows\System32\nvapi64.dll")
        try:
            if dll_path.is_file():
                self._dll = ctypes.WinDLL(str(dll_path))
            else:
                # Fallback: let Windows resolve from PATH / known folders.
                self._dll = ctypes.WinDLL("nvapi64.dll")
        except OSError as exc:
            self._init_error = f"LoadLibrary nvapi64.dll failed: {exc}"
            log.get().debug(f"NVAPI: {self._init_error}")
            return
        except Exception as exc:
            self._init_error = f"unexpected error loading nvapi64.dll: {exc}"
            log.get().debug(f"NVAPI: {self._init_error}")
            return

        # Resolve NvAPI_QueryInterface — the single entry point.
        try:
            self._query_interface = self._dll.nvapi_QueryInterface
            self._query_interface.restype = ctypes.c_void_p
            self._query_interface.argtypes = [ctypes.c_uint32]
        except (AttributeError, OSError) as exc:
            self._init_error = f"nvapi_QueryInterface lookup failed: {exc}"
            log.get().debug(f"NVAPI: {self._init_error}")
            return

        # Bind NvAPI_Initialize and call it.
        init_fn = self._get_function(
            "NvAPI_Initialize", NvAPI_Status, []
        )
        if init_fn is None:
            self._init_error = "NvAPI_Initialize function pointer not found"
            log.get().debug(f"NVAPI: {self._init_error}")
            return

        try:
            status = init_fn()
        except Exception as exc:
            self._init_error = f"NvAPI_Initialize call raised: {exc}"
            log.get().debug(f"NVAPI: {self._init_error}")
            return

        if status != NVAPI_OK:
            self._init_error = f"NvAPI_Initialize returned {status}"
            log.get().debug(f"NVAPI: {self._init_error}")
            return

        self._initialized = True
        log.get().info("NVAPI: initialized")

    # ------------------------------------------------------------------

    def _get_function(
        self,
        name: str,
        restype: Any,
        argtypes: list,
    ) -> Any:
        """Resolve an NVAPI call by name via ``nvapi_QueryInterface``.

        Returns a ctypes-callable function pointer with the given signature,
        or None if ``name`` isn't in ``_FUNCTION_IDS`` or the driver doesn't
        expose it (QueryInterface returns NULL).
        """
        if name in self._fn_cache:
            return self._fn_cache[name]

        fn_id = _FUNCTION_IDS.get(name)
        if fn_id is None:
            # Intentionally no log — absent IDs are the expected TODO path.
            return None

        ptr = self._query_interface(fn_id)
        if not ptr:
            log.get().debug(
                f"NVAPI: QueryInterface({name}, id=0x{fn_id:08X}) returned NULL"
            )
            self._fn_cache[name] = None
            return None

        proto = ctypes.WINFUNCTYPE(restype, *argtypes)
        fn = proto(ptr)
        self._fn_cache[name] = fn
        return fn

    # ------------------------------------------------------------------

    def get_error_message(self, status: int) -> str:
        """Resolve NVAPI status to a string using NvAPI_GetErrorMessage.

        Falls back to the numeric status if the call isn't available.
        """
        fn = self._get_function(
            "NvAPI_GetErrorMessage",
            NvAPI_Status,
            [NvAPI_Status, ctypes.c_char * NVAPI_SHORT_STRING_MAX],
        )
        if fn is None:
            return f"NVAPI status {status}"
        buf = (ctypes.c_char * NVAPI_SHORT_STRING_MAX)()
        try:
            fn(status, buf)
            return buf.value.decode("utf-8", errors="replace")
        except Exception:
            return f"NVAPI status {status}"

    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Call NvAPI_Unload. Idempotent."""
        if not self._initialized:
            return
        fn = self._get_function("NvAPI_Unload", NvAPI_Status, [])
        if fn is not None:
            try:
                fn()
            except Exception as exc:
                log.get().debug(f"NvAPI_Unload failed (ignored): {exc}")
        self._initialized = False


def _check(loader: "_NvAPILoader", status: int, call_name: str) -> None:
    """Raise the appropriate backend exception for a non-OK status."""
    if status == NVAPI_OK:
        return
    if status == NVAPI_INCOMPATIBLE_STRUCT_VERSION:
        raise BackendStructMismatch(
            f"{call_name}: NVAPI_INCOMPATIBLE_STRUCT_VERSION"
        )
    if status in (NVAPI_API_NOT_INITIALIZED, NVAPI_LIBRARY_NOT_FOUND):
        raise BackendUnavailable(
            f"{call_name}: {loader.get_error_message(status)}"
        )
    if status == NVAPI_NO_IMPLEMENTATION:
        raise BackendUnavailable(
            f"{call_name}: NVAPI_NO_IMPLEMENTATION (driver lacks support)"
        )
    raise BackendCallFailed(
        f"{call_name}: {loader.get_error_message(status)} ({status})"
    )


# ---------------------------------------------------------------------------
# Shared base — tracks per-call session blacklist to avoid re-retrying
# calls that have already been flagged as unavailable on this driver.
# ---------------------------------------------------------------------------

class NVAPIBackend:
    """Base class for all NVAPI-backed subsystems.

    Subclasses set ``subsystem`` and implement the Backend protocol
    (``available``, ``status``, optional writes).
    """

    name = "nvapi"
    subsystem = ""

    def __init__(self) -> None:
        self._loader = _NvAPILoader()
        # Per-backend session blacklist: call names we've seen fail once.
        self._call_blacklist: set[str] = set()
        self._call_lock = threading.Lock()

    def _nvapi_init(self) -> bool:
        """Ensure the loader is initialized. Returns True on success."""
        return self._loader.available()

    def _nvapi_unload(self) -> None:
        """Currently a no-op — loader is shared and shuts down at process
        exit via the module-level atexit registration below."""

    def available(self) -> bool:
        """True when NVAPI can be used at all on this machine."""
        return self._nvapi_init()

    def _is_call_blacklisted(self, name: str) -> bool:
        with self._call_lock:
            return name in self._call_blacklist

    def _blacklist_call(self, name: str, reason: str) -> None:
        with self._call_lock:
            if name not in self._call_blacklist:
                log.get().warning(
                    f"NVAPI call blacklisted for session: {name} — {reason}"
                )
                self._call_blacklist.add(name)


# ---------------------------------------------------------------------------
# Displays backend
# ---------------------------------------------------------------------------

class NVAPIDisplaysBackend(NVAPIBackend):
    """NVAPI display enumeration → :class:`DDState`.

    TODO: this backend needs NV_DISPLAYCONFIG_PATH_INFO_V2 and
    NvAPI_DISP_GetDisplayConfig signatures confirmed before it can
    return useful data. Today it reports available=True (since NVAPI
    itself loaded) but status() raises, falling back to shell.
    """

    subsystem = "displays"

    def status(self, *, dry_run: bool = False) -> DDState:
        if dry_run:
            return DDState()
        if not self._nvapi_init():
            raise BackendUnavailable("NVAPI not initialized")

        call = "NvAPI_DISP_GetDisplayConfig"
        if self._is_call_blacklisted(call):
            raise BackendCallFailed(
                f"{call} blacklisted for session — falling back"
            )

        # TODO: Implement once NV_DISPLAYCONFIG_PATH_INFO_V2 struct is
        # confirmed. Flow:
        #   1. Call GetDisplayConfig with pathInfoCount = 0 to get count.
        #   2. Allocate pathInfo array + target modes.
        #   3. Call GetDisplayConfig with buffers.
        #   4. Walk paths, correlate to connected display IDs, build
        #      DisplayInfo list, return DDState.
        self._blacklist_call(call, "not yet implemented in bindings")
        raise BackendCallFailed(
            f"{call}: not implemented — falling back to shell backend"
        )


# ---------------------------------------------------------------------------
# Mosaic backend
# ---------------------------------------------------------------------------

class NVAPIMosaicBackend(NVAPIBackend):
    """NVAPI Mosaic read (+ future write) → :class:`MosaicState`.

    Writes gated behind ``allow_writes`` (default False). Read-only for
    v0.2 per spec section 10.

    TODO: needs NV_MOSAIC_TOPO_BRIEF + NV_MOSAIC_DISPLAY_SETTING_V2 +
    NV_MOSAIC_GRID_TOPO_V2 signatures.
    """

    subsystem = "mosaic"

    def __init__(self, *, allow_writes: bool = False) -> None:
        super().__init__()
        self.allow_writes = allow_writes

    def status(self, *, dry_run: bool = False) -> MosaicState:
        if dry_run:
            return MosaicState()
        if not self._nvapi_init():
            raise BackendUnavailable("NVAPI not initialized")

        call = "NvAPI_Mosaic_GetCurrentTopo"
        if self._is_call_blacklisted(call):
            raise BackendCallFailed(f"{call} blacklisted — falling back")

        # TODO: Implement GetCurrentTopo. See file docstring.
        self._blacklist_call(call, "not yet implemented in bindings")
        raise BackendCallFailed(
            f"{call}: not implemented — falling back to shell backend"
        )

    def apply(self, **kwargs: Any) -> Any:
        if not self.allow_writes:
            raise NotImplementedError(
                "NVAPIMosaicBackend: writes disabled (allow_writes=False). "
                "Falling back to shell backend."
            )
        # TODO: NvAPI_Mosaic_SetDisplayGrids with
        # NV_MOSAIC_SETDISPLAYTOPO_FLAG_NO_DRIVER_RELOAD.
        raise NotImplementedError(
            "NvAPI_Mosaic_SetDisplayGrids: not yet implemented"
        )

    def disable(self) -> Any:
        if not self.allow_writes:
            raise NotImplementedError(
                "NVAPIMosaicBackend: writes disabled (allow_writes=False)"
            )
        raise NotImplementedError(
            "NvAPI_Mosaic_EnableCurrentTopo(disable): not yet implemented"
        )


# ---------------------------------------------------------------------------
# GSync / QSync backend
# ---------------------------------------------------------------------------

class NVAPIGSyncBackend(NVAPIBackend):
    """NVAPI Quadro Sync → :class:`QSyncState`.

    Read-only in v0.2 per spec. Writes (``enable``, ``disable``) raise
    NotImplementedError when ``allow_writes=False``, so the registry
    falls through to the shell backend.

    TODO: needs NV_GSYNC_STATUS_PARAMS_V2, NV_GSYNC_STATUS_V2 struct
    signatures before read is functional.
    """

    subsystem = "qsync"

    def __init__(self, *, allow_writes: bool = False) -> None:
        super().__init__()
        self.allow_writes = allow_writes

    def status(self, *, dry_run: bool = False) -> QSyncState:
        if dry_run:
            return QSyncState()
        if not self._nvapi_init():
            raise BackendUnavailable("NVAPI not initialized")

        call = "NvAPI_GSync_GetStatusParameters"
        if self._is_call_blacklisted(call):
            raise BackendCallFailed(f"{call} blacklisted — falling back")

        # TODO: Implement. Flow:
        #   1. NvAPI_GSync_EnumSyncDevices → list of NvGSyncDeviceHandle.
        #   2. For each: NvAPI_GSync_GetStatusParameters → refresh rate,
        #      house-sync state, etc.
        #   3. For each sync device's GPUs: NvAPI_GSync_GetSyncStatus →
        #      per-GPU synced / signal_available.
        self._blacklist_call(call, "not yet implemented in bindings")
        raise BackendCallFailed(
            f"{call}: not implemented — falling back to shell backend"
        )

    def enable(self, **kwargs: Any) -> Any:
        if not self.allow_writes:
            raise NotImplementedError(
                "NVAPIGSyncBackend: writes disabled (allow_writes=False). "
                "Falling back to shell backend."
            )
        raise NotImplementedError(
            "NvAPI_GSync_SetControlParameters: not yet implemented"
        )

    def disable(self) -> Any:
        if not self.allow_writes:
            raise NotImplementedError(
                "NVAPIGSyncBackend: writes disabled (allow_writes=False)"
            )
        raise NotImplementedError(
            "NvAPI_GSync_SetControlParameters(disable): not yet implemented"
        )


# ---------------------------------------------------------------------------
# EDID read backend
# ---------------------------------------------------------------------------

class NVAPIEdidBackend(NVAPIBackend):
    """NVAPI EDID read via NvAPI_GPU_GetEDID.

    Read-only by design — per spec section 3.5, the write path stays on
    the registry (shell backend) even when NVAPI is preferred.

    TODO: needs NV_EDID_V3 struct definition confirmed.
    """

    subsystem = "edid"

    def status(self) -> dict:
        if not self._nvapi_init():
            raise BackendUnavailable("NVAPI not initialized")

        call = "NvAPI_GPU_GetEDID"
        if self._is_call_blacklisted(call):
            raise BackendCallFailed(f"{call} blacklisted — falling back")

        # TODO: Implement NvAPI_GPU_GetEDID with NV_EDID_V3 struct.
        self._blacklist_call(call, "not yet implemented in bindings")
        raise BackendCallFailed(
            f"{call}: not implemented — falling back to shell backend"
        )

    def read(self) -> dict:
        """Alias of status() — some callers use read() for consistency
        with the shell backend's EDID API."""
        return self.status()

    def apply(self, **kwargs: Any) -> Any:
        # Intentional: NVAPI write isn't the v0.2 path. Registry will
        # fall through to ShellEdidBackend.apply() which writes to
        # HKLM\...\Device Parameters\EDID_OVERRIDE.
        raise NotImplementedError(
            "NVAPIEdidBackend: write not supported in v0.2 — "
            "registry write stays on shell backend (spec 3.5)."
        )


# ---------------------------------------------------------------------------
# Exit cleanup — unload NVAPI when the process exits.
# ---------------------------------------------------------------------------

import atexit as _atexit


def _shutdown_nvapi_on_exit() -> None:
    loader = _NvAPILoader()
    loader.shutdown()


_atexit.register(_shutdown_nvapi_on_exit)
