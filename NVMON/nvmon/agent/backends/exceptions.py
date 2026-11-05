"""Exceptions raised by backends.

The registry catches these and walks the fallback chain (or blacklists the
call) depending on exception type. Callers outside the backends package
should not need to catch these directly — the registry's `.status()` /
`.enable()` / etc. dispatcher handles fallback and re-raises only when all
backends fail.

Hierarchy:

    BackendError
        BackendUnavailable          — backend cannot initialize (missing DLL,
                                      driver not present, init call failed).
                                      Registry should skip this backend for
                                      the rest of the session.
        BackendCallFailed           — a single call failed at runtime. Registry
                                      walks to next backend but keeps the
                                      current one available for future calls.
        BackendStructMismatch       — NVAPI returned INCOMPATIBLE_STRUCT_VERSION
                                      for a specific call. Registry blacklists
                                      that (backend, subsystem) pair for the
                                      session, then walks to the next backend.
"""

from __future__ import annotations


class BackendError(Exception):
    """Base class for backend failures."""


class BackendUnavailable(BackendError):
    """Backend cannot be used at all (missing driver, missing DLL, init failed).

    Raised from `available()` callers and any backend operation that hits a
    terminal-level failure. The registry removes the backend from the
    fallback chain for the session when this is raised.
    """


class BackendCallFailed(BackendError):
    """A specific backend call failed at runtime.

    Non-terminal — the backend stays registered; the dispatcher just walks
    to the next one for this particular call. The same backend may succeed
    on a later call (e.g. transient driver state).
    """


class BackendStructMismatch(BackendError):
    """NVAPI returned NVAPI_INCOMPATIBLE_STRUCT_VERSION (or similar).

    The registry permanently blacklists the specific (backend, subsystem)
    pair for the session — re-trying with the same struct version will
    keep failing. Caller walks to the next backend in the chain.
    """
