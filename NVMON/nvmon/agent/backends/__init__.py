"""Pluggable NVIDIA-access backends.

Three layers per subsystem, chosen at call time by priority:

    gpu:      NVMLGpuBackend (20)  →  ShellGpuBackend (100)
    displays: NVAPIDisplaysBackend (10)  →  ShellDisplaysBackend (100)
    mosaic:   NVAPIMosaicBackend (10)    →  ShellMosaicBackend (100)
    qsync:    NVAPIGSyncBackend (10)     →  ShellQSyncBackend (100)
    edid:     NVAPIEdidBackend (10)      →  ShellEdidBackend (100)

Usage::

    from nvmon.agent.backends import registry, get_registry

    reg = get_registry()                      # auto-registers
    gpus = reg.get("gpu").status()            # NVML → shell fallback
    chain = reg.fallback_chain("mosaic")      # inspect active chain

The ``Backend`` protocol requires ``available() -> bool`` and a
subsystem-appropriate ``status()`` method. Writable backends add
``enable()`` / ``disable()`` / ``apply()``. Read-only backends raise
:class:`NotImplementedError` on write methods; the registry walks to the
next backend in the chain.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .exceptions import (
    BackendCallFailed,
    BackendError,
    BackendStructMismatch,
    BackendUnavailable,
)
from .registry import (
    BackendRegistry,
    PRIORITY_NVAPI,
    PRIORITY_NVML,
    PRIORITY_SHELL,
    SUBSYSTEMS,
    get_registry,
    reset_registry,
)


@runtime_checkable
class Backend(Protocol):
    """Structural typing protocol every backend implements.

    Concrete backends aren't required to inherit from this — any class
    with ``available()`` + ``status()`` passes ``isinstance(..., Backend)``
    at runtime due to ``@runtime_checkable``.
    """

    #: One of ``'nvapi' | 'nvml' | 'shell'``.
    name: str
    #: One of ``'gpu' | 'displays' | 'mosaic' | 'qsync' | 'edid'``.
    subsystem: str

    def available(self) -> bool:
        ...

    def status(self, *args, **kwargs):
        ...


__all__ = [
    # Registry
    "BackendRegistry",
    "get_registry",
    "reset_registry",
    # Protocol + constants
    "Backend",
    "SUBSYSTEMS",
    "PRIORITY_NVAPI",
    "PRIORITY_NVML",
    "PRIORITY_SHELL",
    # Exceptions
    "BackendError",
    "BackendUnavailable",
    "BackendCallFailed",
    "BackendStructMismatch",
]
