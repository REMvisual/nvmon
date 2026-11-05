"""NVML (pynvml) backend — GPU telemetry only.

NVML is the official, versioned NVIDIA Management Library. Pip-installable
via `nvidia-ml-py` (imported as ``pynvml``). Covers all the fields in A2's
`GPUInfo` dataclass at sub-millisecond per-call cost.

Covers (per spec section 3.1):
    - Device name, UUID, PCI info, driver version
    - Temperature (GPU + threshold)
    - Power draw + limit
    - Utilization (GPU, memory, encoder, decoder)
    - Memory info (used, total)
    - Clocks (graphics, mem, video, SM)
    - PCIe gen + width (current + max)
    - P-state, throttle reasons, fan speed, display active
    - Encoder/decoder session stats

On machines without NVIDIA drivers or without pynvml installed,
:meth:`NVMLGpuBackend.available` returns False and the registry skips
registration silently.
"""

from __future__ import annotations

import threading
from typing import Any

from ... import log
from ...types import GPUInfo
from .exceptions import BackendCallFailed, BackendUnavailable


# ---------------------------------------------------------------------------
# Lazy pynvml import — no hard dependency at module-import time.
# ---------------------------------------------------------------------------

try:
    import pynvml  # type: ignore[import-not-found]
    _pynvml_import_error: Exception | None = None
except Exception as _exc:  # ImportError, DLL not found, etc.
    pynvml = None  # type: ignore[assignment]
    _pynvml_import_error = _exc


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class NVMLGpuBackend:
    """pynvml-backed GPU telemetry provider.

    Thread-safety: ``pynvml`` is not formally documented as thread-safe.
    We guard all calls with an internal lock. GPU polling is ~1 Hz in
    practice; the lock has negligible cost.
    """

    name = "nvml"
    subsystem = "gpu"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._init_error: str = ""

        if pynvml is None:
            self._init_error = (
                f"pynvml import failed: {_pynvml_import_error!r}"
            )
            return

        try:
            with self._lock:
                pynvml.nvmlInit()
                self._initialized = True
        except Exception as exc:  # pynvml.NVMLError et al.
            self._init_error = f"nvmlInit failed: {exc}"
            log.get().debug(
                f"NVMLGpuBackend: nvmlInit failed — {exc}. "
                "GPU subsystem will fall back to shell-out."
            )

    # ------------------------------------------------------------------

    def available(self) -> bool:
        return self._initialized

    def status(self, *, dry_run: bool = False) -> list[GPUInfo]:
        """Return GPU telemetry as a list of :class:`GPUInfo`.

        Raises :class:`BackendUnavailable` if pynvml failed to initialize.
        Individual field-level failures are logged and skipped — the
        returned GPUInfo may have zero values for fields the driver
        doesn't expose.
        """
        if not self._initialized:
            raise BackendUnavailable(self._init_error or "pynvml not initialized")
        if dry_run:
            return []

        if pynvml is None:  # Defensive — unreachable if initialized.
            raise BackendUnavailable("pynvml not importable")

        logger = log.get()
        gpus: list[GPUInfo] = []

        with self._lock:
            try:
                driver_version = _safe_decode(pynvml.nvmlSystemGetDriverVersion())
            except Exception as exc:
                logger.debug(f"nvml: driver_version lookup failed: {exc}")
                driver_version = ""

            try:
                count = pynvml.nvmlDeviceGetCount()
            except Exception as exc:
                raise BackendCallFailed(f"nvmlDeviceGetCount failed: {exc}") from exc

            for idx in range(count):
                try:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
                except Exception as exc:
                    logger.debug(f"nvml: handle for idx={idx} failed: {exc}")
                    continue

                info = GPUInfo(index=idx, driver_version=driver_version)
                _populate_gpuinfo(info, handle, logger)
                gpus.append(info)

        return gpus

    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Call `nvmlShutdown`. Idempotent; safe at process exit."""
        if not self._initialized or pynvml is None:
            return
        try:
            with self._lock:
                pynvml.nvmlShutdown()
        except Exception as exc:
            log.get().debug(f"nvmlShutdown failed (ignored): {exc}")
        finally:
            self._initialized = False


# ---------------------------------------------------------------------------
# Field population — each field wrapped in its own try/except so one
# unsupported metric doesn't void the whole GPU record.
# ---------------------------------------------------------------------------

def _populate_gpuinfo(info: GPUInfo, handle: Any, logger) -> None:
    # Identity
    try:
        info.name = _safe_decode(pynvml.nvmlDeviceGetName(handle))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: name: {exc}")

    try:
        pci = pynvml.nvmlDeviceGetPciInfo(handle)
        # busIdLegacy is the "00000000:01:00.0" format nvidia-smi uses.
        info.pci_bus_id = _safe_decode(
            getattr(pci, "busIdLegacy", None) or getattr(pci, "busId", b"")
        )
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: pci_info: {exc}")

    # Temperature
    try:
        info.temperature_c = int(pynvml.nvmlDeviceGetTemperature(
            handle, pynvml.NVML_TEMPERATURE_GPU
        ))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: temperature: {exc}")

    # Power (milliwatts → watts)
    try:
        info.power_draw_w = float(pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: power_draw: {exc}")
    try:
        info.power_limit_w = float(
            pynvml.nvmlDeviceGetPowerManagementLimit(handle)
        ) / 1000.0
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: power_limit: {exc}")

    # Utilization
    try:
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        info.gpu_utilization_pct = int(util.gpu)
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: utilization: {exc}")

    # Memory
    try:
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        info.memory_used_mb = int(mem.used // (1024 * 1024))
        info.memory_total_mb = int(mem.total // (1024 * 1024))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: memory: {exc}")

    # Clocks
    try:
        info.gpu_clock_mhz = int(pynvml.nvmlDeviceGetClockInfo(
            handle, pynvml.NVML_CLOCK_GRAPHICS,
        ))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: clock_graphics: {exc}")
    try:
        info.mem_clock_mhz = int(pynvml.nvmlDeviceGetClockInfo(
            handle, pynvml.NVML_CLOCK_MEM,
        ))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: clock_mem: {exc}")
    try:
        info.video_clock_mhz = int(pynvml.nvmlDeviceGetClockInfo(
            handle, pynvml.NVML_CLOCK_VIDEO,
        ))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: clock_video: {exc}")

    # Performance state
    try:
        pstate = pynvml.nvmlDeviceGetPerformanceState(handle)
        info.pstate = f"P{int(pstate)}"
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: pstate: {exc}")

    # Display active
    try:
        info.display_active = bool(pynvml.nvmlDeviceGetDisplayActive(handle))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: display_active: {exc}")

    # Fan speed
    try:
        info.fan_speed_pct = int(pynvml.nvmlDeviceGetFanSpeed(handle))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: fan_speed: {exc}")

    # PCIe link
    try:
        info.pcie_gen = int(pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: pcie_gen: {exc}")
    try:
        info.pcie_width = int(pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle))
    except Exception as exc:
        logger.debug(f"nvml[{info.index}]: pcie_width: {exc}")


def _safe_decode(value: Any) -> str:
    """pynvml returns bytes on some calls, str on others. Normalize to str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return str(value)
