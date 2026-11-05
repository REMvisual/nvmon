"""GPU health telemetry via nvidia-smi.

Shells out to `nvidia-smi --query-gpu=... --format=csv`. Missing exe is
handled gracefully — `capability()` returns `available=False` and `status()`
returns an empty list without raising.
"""

from __future__ import annotations

from .. import config, executors, log
from ..parsers import parse_nvidia_smi_csv
from ..types import GPUInfo


# The query string must match the field order declared in parsers._SMI_FIELDS.
# INTEGRATION_POINT: backends — once a backend registry exists (NVAPI/NVML),
# dispatch `status()` through `backends.resolve("gpu").status()` and keep this
# shell-out as the default CLI backend.
_QUERY_FIELDS = (
    "index,name,pci.bus_id,driver_version,temperature.gpu,"
    "power.draw,power.limit,utilization.gpu,memory.used,memory.total,"
    "clocks.current.graphics,clocks.current.memory,clocks.current.video,"
    "pstate,display_active,fan.speed,pcie.link.gen.current,pcie.link.width.current"
)


def capability() -> dict:
    """Report whether the GPU subsystem is usable on this machine.

    Returns a dict that slots directly into `/health.capabilities.gpu`.
    """
    exe = config.get_nvidia_smi()
    if not exe:
        return {
            "available": False,
            "exe": None,
            "reason": "nvidia-smi not found on PATH or in configured location",
        }
    return {"available": True, "exe": "nvidia-smi"}


def status(*, dry_run: bool = False) -> list[GPUInfo]:
    """Return GPU telemetry as a list of GPUInfo.

    If nvidia-smi is missing or fails, returns `[]` and logs a warning once.
    Never raises — callers can treat empty == "unavailable".
    """
    logger = log.get()
    exe = config.get_nvidia_smi()
    if not exe:
        logger.debug("gpu.status: nvidia-smi not found — returning empty GPU list")
        return []

    # INTEGRATION_POINT: backends
    result = executors.run(
        exe,
        [f"--query-gpu={_QUERY_FIELDS}", "--format=csv,noheader,nounits"],
        label="nvidia-smi",
        dry_run=dry_run,
        quiet=True,
    )

    if dry_run:
        return []

    if not result.success:
        logger.warning(f"nvidia-smi query failed (rc={result.return_code}): {result.stderr}")
        return []

    try:
        return parse_nvidia_smi_csv(result.stdout)
    except Exception as exc:
        logger.error(f"nvidia-smi parse failed: {exc}")
        return []


def quick_info(*, dry_run: bool = False) -> list[dict]:
    """Quick GPU inventory — just index/name/bus/driver."""
    exe = config.get_nvidia_smi()
    if not exe:
        return []

    result = executors.run(
        exe,
        ["--query-gpu=index,name,pci.bus_id,driver_version", "--format=csv,noheader"],
        label="nvidia-smi-quick",
        dry_run=dry_run,
        quiet=True,
    )

    if dry_run or not result.success:
        return []

    gpus: list[dict] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            gpus.append({
                "index": parts[0],
                "name": parts[1],
                "pci_bus_id": parts[2],
                "driver_version": parts[3],
            })
    return gpus
