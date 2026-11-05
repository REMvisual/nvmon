"""Shell-out backends — thin wrappers around A2's existing subsystem modules.

Each class adapts the existing shell-out code in `nvmon.agent.{gpu,displays,
mosaic,qsync,edid}` into the :class:`Backend` protocol expected by the
registry. Same behaviour, typed state objects out, NOT a reimplementation.

The shell backend is always available (it degrades to returning empty state
when the underlying CLI exe is missing) and sits at the bottom of every
fallback chain.
"""

from __future__ import annotations

from typing import Any

from ...types import DDState, GPUInfo, MosaicState, QSyncState
from .. import displays as _displays
from .. import edid as _edid
from .. import gpu as _gpu
from .. import mosaic as _mosaic
from .. import qsync as _qsync


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------

class ShellGpuBackend:
    """Wraps `nvmon.agent.gpu` (nvidia-smi shell-out)."""

    name = "shell"
    subsystem = "gpu"

    def available(self) -> bool:
        # Always "available" — if nvidia-smi is missing, underlying status()
        # returns [] without crashing, which is the documented contract.
        return True

    def status(self, *, dry_run: bool = False) -> list[GPUInfo]:
        return _gpu.status(dry_run=dry_run)

    def quick_info(self, *, dry_run: bool = False) -> list[dict]:
        return _gpu.quick_info(dry_run=dry_run)


# ---------------------------------------------------------------------------
# Displays (Direct Display)
# ---------------------------------------------------------------------------

class ShellDisplaysBackend:
    """Wraps `nvmon.agent.displays` (configureDdisplay.exe)."""

    name = "shell"
    subsystem = "displays"

    def available(self) -> bool:
        return True

    def status(self, *, dry_run: bool = False) -> DDState:
        return _displays.status(dry_run=dry_run)

    def enable(self, *, mode: str = "index", index: int = 1, dry_run: bool = False) -> Any:
        return _displays.enable(mode=mode, index=index, dry_run=dry_run)

    def disable(self, *, dry_run: bool = False) -> Any:
        return _displays.disable(dry_run=dry_run)


# ---------------------------------------------------------------------------
# Mosaic
# ---------------------------------------------------------------------------

class ShellMosaicBackend:
    """Wraps `nvmon.agent.mosaic` (configureMosaic.exe)."""

    name = "shell"
    subsystem = "mosaic"

    def available(self) -> bool:
        return True

    def status(self, *, dry_run: bool = False) -> MosaicState:
        return _mosaic.status(dry_run=dry_run)

    def apply(
        self,
        *,
        rows: int,
        cols: int,
        width: int,
        height: int,
        refresh_rate: str,
        outputs: str,
        dry_run: bool = False,
    ) -> Any:
        return _mosaic.configure(
            rows=rows,
            cols=cols,
            width=width,
            height=height,
            refresh_rate=refresh_rate,
            outputs=outputs,
            dry_run=dry_run,
        )

    def disable(self, *, dry_run: bool = False) -> Any:
        return _mosaic.disable(dry_run=dry_run)


# ---------------------------------------------------------------------------
# QSync
# ---------------------------------------------------------------------------

class ShellQSyncBackend:
    """Wraps `nvmon.agent.qsync` (configureQsync2.exe)."""

    name = "shell"
    subsystem = "qsync"

    def available(self) -> bool:
        return True

    def status(self, *, dry_run: bool = False) -> QSyncState:
        return _qsync.status(dry_run=dry_run)

    def enable(
        self,
        *,
        source: str = "HOUSESYNC",
        trigger: str = "FALLING",
        grid: int = -1,
        wait: bool = True,
        dry_run: bool = False,
    ) -> Any:
        return _qsync.enable(
            source=source, trigger=trigger, grid=grid,
            wait=wait, dry_run=dry_run,
        )

    def disable(self, *, dry_run: bool = False) -> Any:
        return _qsync.disable(dry_run=dry_run)


# ---------------------------------------------------------------------------
# EDID
# ---------------------------------------------------------------------------

class ShellEdidBackend:
    """Wraps `nvmon.agent.edid` (Windows registry reads + writes).

    Note: the "shell" name is retained for consistency with other backends
    even though EDID uses `winreg` rather than a CLI exe. Writes are
    always permitted on this backend (the registry is the only writable
    EDID path in v1 per spec section 3.5).
    """

    name = "shell"
    subsystem = "edid"

    def available(self) -> bool:
        return _edid.is_available()

    def status(self) -> dict:
        return _edid.status()

    def read(self) -> dict[str, bool]:
        """Registry-based EDID discovery (maps to NVAPI read path)."""
        return _edid.check_registry_edid()

    def apply(self, *, monitor: str, instance: str, edid_bytes: bytes) -> bool:
        return _edid.write_registry_edid(monitor, instance, edid_bytes)
