"""Backend registry — selects and dispatches per-subsystem backends.

Subsystems: ``gpu``, ``displays``, ``mosaic``, ``qsync``, ``edid``.

A backend is any object implementing the `Backend` protocol (see
`__init__.py`). Backends are registered per subsystem with an integer
priority — lower = preferred. The registry walks the chain on every call,
catching `BackendUnavailable` / `BackendCallFailed` / `BackendStructMismatch`
and trying the next backend.

Default fallback chain (lowest-priority wins):

    gpu:      NVAPI(10)  → NVML(20)  → shell(100)
    displays: NVAPI(10)  → shell(100)
    mosaic:   NVAPI(10)  → shell(100)
    qsync:    NVAPI(10)  → shell(100)
    edid:     NVAPI(10)  → shell(100)

The ``shell`` backend is always available (it wraps A's existing CLI code)
and forms the bottom of every chain.

# INTEGRATION_POINT
# ----------------
# A2's subsystem modules (`nvmon/agent/gpu.py`, `mosaic.py`, `qsync.py`,
# `displays.py`, `edid.py`) currently shell-out directly. To use the backend
# registry, they should conditionally dispatch through it:
#
#     from nvmon.agent.backends import registry as _registry
#     if config.prefer_backends():  # [agent] prefer_backends=true
#         return _registry.get_registry().status("gpu")
#     # else fall through to existing shell-out path
#
# A2's toml loader exposes `prefer_backends` under `[agent]`; when True, the
# registry path runs; when False (default), the existing code path runs
# unchanged. This keeps F strictly opt-in as designed in spec section 5.2.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ... import log
from .exceptions import (
    BackendCallFailed,
    BackendStructMismatch,
    BackendUnavailable,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUBSYSTEMS = ("gpu", "displays", "mosaic", "qsync", "edid")

# Priority tiers — lower is preferred.
PRIORITY_NVAPI = 10
PRIORITY_NVML = 20
PRIORITY_SHELL = 100


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class BackendRegistry:
    """Holds backends per subsystem and dispatches calls with fallback.

    Thread-safe (a single :class:`threading.RLock` protects registration +
    blacklist + counter state). Writes are rare (register-at-init, blacklist
    on failure); reads are hot (every status poll).

    Usage::

        reg = BackendRegistry()
        reg.register_all()                   # auto-register shell + nvml + nvapi
        gpus = reg.status("gpu")             # dispatches with fallback
        reg.fallback_chain("mosaic")         # → ["NVAPIMosaicBackend", "ShellMosaicBackend"]
    """

    def __init__(self, *, allow_writes: bool = False) -> None:
        self._backends: dict[str, list[tuple[int, Any]]] = {
            s: [] for s in SUBSYSTEMS
        }
        # Keys: (subsystem, backend_class_name). Values: reason string.
        self._blacklist: dict[tuple[str, str], str] = {}
        self._fail_counts: dict[tuple[str, str], int] = {}
        # When False, write calls on NVAPI backends raise NotImplementedError.
        # The shell backend always honours writes.
        self.allow_writes = allow_writes
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        subsystem: str,
        backend: Any,
        priority: int = PRIORITY_SHELL,
    ) -> None:
        """Register a backend for ``subsystem``.

        ``backend`` must implement the :class:`Backend` protocol. If its
        ``available()`` returns False, registration is silently skipped
        (with a single debug log line) — this is how missing drivers are
        handled without error spam.
        """
        if subsystem not in SUBSYSTEMS:
            raise ValueError(
                f"unknown subsystem {subsystem!r} — "
                f"must be one of {SUBSYSTEMS}"
            )

        logger = log.get()
        name = type(backend).__name__

        # Skip registration when backend reports unavailable.
        try:
            avail = bool(backend.available())
        except Exception as exc:
            logger.debug(
                f"backend {name}: available() raised {type(exc).__name__}: {exc} "
                "— skipping registration"
            )
            return

        if not avail:
            logger.debug(
                f"backend {name}: not available on this machine — "
                f"skipping registration for {subsystem!r}"
            )
            return

        with self._lock:
            self._backends[subsystem].append((priority, backend))
            # Stable sort by priority (lowest first).
            self._backends[subsystem].sort(key=lambda p: p[0])
        logger.info(
            f"backend {name} registered for {subsystem!r} at priority {priority}"
        )

    def register_all(self) -> None:
        """Auto-discover and register every available backend.

        Order of attempts:
        1. NVAPI (`nvapi.py`) — displays, mosaic, qsync, edid (GPU NOT covered
           by NVAPI in our binding set — NVML owns GPU).
        2. NVML  (`nvml.py`)  — gpu.
        3. Shell (`shell.py`) — all subsystems, always available.

        Backends whose ``available()`` returns False are silently skipped.
        """
        # Import lazily to avoid hard dependencies at module-import time.
        from . import nvapi as _nvapi
        from . import nvml as _nvml
        from . import shell as _shell

        # --- NVAPI (priority 10) --------------------------------------
        try:
            disp = _nvapi.NVAPIDisplaysBackend()
            self.register("displays", disp, PRIORITY_NVAPI)
        except Exception as exc:
            log.get().debug(f"NVAPIDisplaysBackend init failed: {exc}")
        try:
            mos = _nvapi.NVAPIMosaicBackend(allow_writes=self.allow_writes)
            self.register("mosaic", mos, PRIORITY_NVAPI)
        except Exception as exc:
            log.get().debug(f"NVAPIMosaicBackend init failed: {exc}")
        try:
            qs = _nvapi.NVAPIGSyncBackend(allow_writes=self.allow_writes)
            self.register("qsync", qs, PRIORITY_NVAPI)
        except Exception as exc:
            log.get().debug(f"NVAPIGSyncBackend init failed: {exc}")
        try:
            ed = _nvapi.NVAPIEdidBackend()
            self.register("edid", ed, PRIORITY_NVAPI)
        except Exception as exc:
            log.get().debug(f"NVAPIEdidBackend init failed: {exc}")

        # --- NVML (priority 20) ---------------------------------------
        try:
            gpu = _nvml.NVMLGpuBackend()
            self.register("gpu", gpu, PRIORITY_NVML)
        except Exception as exc:
            log.get().debug(f"NVMLGpuBackend init failed: {exc}")

        # --- Shell (priority 100, always available) -------------------
        try:
            self.register("gpu", _shell.ShellGpuBackend(), PRIORITY_SHELL)
            self.register("displays", _shell.ShellDisplaysBackend(), PRIORITY_SHELL)
            self.register("mosaic", _shell.ShellMosaicBackend(), PRIORITY_SHELL)
            self.register("qsync", _shell.ShellQSyncBackend(), PRIORITY_SHELL)
            self.register("edid", _shell.ShellEdidBackend(), PRIORITY_SHELL)
        except Exception as exc:
            log.get().error(f"shell backend registration failed: {exc}")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get(self, subsystem: str) -> "_Dispatcher":
        """Return a dispatcher for ``subsystem`` that walks the fallback chain.

        The dispatcher implements the same surface as a single Backend
        (``status()``, ``enable()``, ``disable()``, ``apply()``) and routes
        to the highest-priority still-available backend, falling back on
        :class:`BackendUnavailable` / :class:`BackendCallFailed` /
        :class:`BackendStructMismatch`.
        """
        if subsystem not in SUBSYSTEMS:
            raise ValueError(f"unknown subsystem {subsystem!r}")
        return _Dispatcher(self, subsystem)

    def fallback_chain(self, subsystem: str) -> list[str]:
        """Return the current ordered chain of backend class names for
        ``subsystem`` (blacklisted entries removed)."""
        if subsystem not in SUBSYSTEMS:
            raise ValueError(f"unknown subsystem {subsystem!r}")
        with self._lock:
            return [
                type(b).__name__
                for _prio, b in self._backends[subsystem]
                if (subsystem, type(b).__name__) not in self._blacklist
            ]

    def status_call(self, subsystem: str) -> Any:
        """Shortcut: ``registry.status_call('gpu')`` equivalent to
        ``registry.get('gpu').status()``. Kept for ergonomic parity with
        the integration-point comment above."""
        return self.get(subsystem).status()

    # ------------------------------------------------------------------
    # Blacklist management (internal)
    # ------------------------------------------------------------------

    def _blacklist_pair(
        self, subsystem: str, backend_name: str, reason: str,
    ) -> None:
        """Permanently skip this (subsystem, backend) for the session."""
        with self._lock:
            key = (subsystem, backend_name)
            if key not in self._blacklist:
                self._blacklist[key] = reason
                log.get().warning(
                    f"backend blacklisted: {backend_name} for {subsystem!r} "
                    f"— {reason}"
                )

    def is_blacklisted(self, subsystem: str, backend_name: str) -> bool:
        with self._lock:
            return (subsystem, backend_name) in self._blacklist

    def blacklist_reason(self, subsystem: str, backend_name: str) -> str | None:
        with self._lock:
            return self._blacklist.get((subsystem, backend_name))

    def reset_blacklist(self) -> None:
        """Clear all blacklist entries. Test-only."""
        with self._lock:
            self._blacklist.clear()
            self._fail_counts.clear()

    # ------------------------------------------------------------------
    # Iteration (used by _Dispatcher)
    # ------------------------------------------------------------------

    def _iter_active_backends(self, subsystem: str):
        """Yield (priority, backend) entries not currently blacklisted."""
        with self._lock:
            snapshot = list(self._backends[subsystem])
        for prio, backend in snapshot:
            if self.is_blacklisted(subsystem, type(backend).__name__):
                continue
            yield prio, backend


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class _Dispatcher:
    """Backend-like wrapper that walks the fallback chain for one subsystem.

    Returned by :meth:`BackendRegistry.get`. Not constructed directly.
    """

    def __init__(self, registry: BackendRegistry, subsystem: str) -> None:
        self._registry = registry
        self._subsystem = subsystem

    def status(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("status", *args, **kwargs)

    def enable(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("enable", *args, **kwargs)

    def disable(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("disable", *args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("apply", *args, **kwargs)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        logger = log.get()
        last_exc: Exception | None = None
        chain_tried: list[str] = []

        for _prio, backend in self._registry._iter_active_backends(self._subsystem):
            name = type(backend).__name__
            fn: Callable[..., Any] | None = getattr(backend, method, None)
            if fn is None:
                # Backend doesn't implement this method — try next one.
                continue
            chain_tried.append(name)
            try:
                return fn(*args, **kwargs)
            except BackendStructMismatch as exc:
                # Blacklist this (backend, subsystem) pair for the session.
                self._registry._blacklist_pair(
                    self._subsystem, name, f"struct mismatch: {exc}"
                )
                last_exc = exc
                continue
            except BackendUnavailable as exc:
                # Terminal for this backend — blacklist and move on.
                self._registry._blacklist_pair(
                    self._subsystem, name, f"unavailable: {exc}"
                )
                last_exc = exc
                continue
            except BackendCallFailed as exc:
                logger.debug(
                    f"backend {name}.{method}() failed: {exc} — "
                    "falling back to next backend"
                )
                last_exc = exc
                continue
            except NotImplementedError:
                # Read-only backend rejecting a write — fall through to
                # the next backend (which may be the writable shell path).
                continue

        if last_exc is not None:
            raise BackendCallFailed(
                f"all backends failed for {self._subsystem}.{method} "
                f"(tried: {chain_tried}): {last_exc}"
            ) from last_exc
        raise BackendUnavailable(
            f"no backend available for {self._subsystem}.{method} "
            f"(tried: {chain_tried})"
        )


# ---------------------------------------------------------------------------
# Module-level default registry (optional singleton for A2 integration)
# ---------------------------------------------------------------------------

_default_registry: BackendRegistry | None = None
_default_lock = threading.Lock()


def get_registry(*, allow_writes: bool = False) -> BackendRegistry:
    """Return a lazily-initialized process-wide registry.

    The first call constructs + auto-registers backends; subsequent calls
    return the same instance. A2's subsystem modules should use this for
    the opt-in dispatch path."""
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            reg = BackendRegistry(allow_writes=allow_writes)
            reg.register_all()
            _default_registry = reg
        return _default_registry


def reset_registry() -> None:
    """Test-only: forget the cached default registry."""
    global _default_registry
    with _default_lock:
        _default_registry = None
