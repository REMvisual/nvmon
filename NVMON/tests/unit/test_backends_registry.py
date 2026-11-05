"""Unit tests for the backend registry — no NVIDIA hardware required.

Covers:
- Registration with priority ordering
- Dispatch to highest-priority backend
- Fallback on BackendCallFailed (non-terminal)
- Fallback + blacklist on BackendUnavailable (terminal)
- Fallback + blacklist on BackendStructMismatch
- Skipping backends whose available() returns False
- Fallback on NotImplementedError (read-only backend with no write)
- fallback_chain() correctly reflects blacklisting
"""

from __future__ import annotations

import pytest

from nvmon.agent.backends import (
    BackendCallFailed,
    BackendRegistry,
    BackendStructMismatch,
    BackendUnavailable,
    PRIORITY_NVAPI,
    PRIORITY_NVML,
    PRIORITY_SHELL,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeBackend:
    """Configurable fake backend for registry tests."""

    def __init__(
        self,
        name: str,
        *,
        subsystem: str = "gpu",
        avail: bool = True,
        status_result=None,
        status_exc: Exception | None = None,
        writable: bool = True,
    ) -> None:
        self.name = name
        self.subsystem = subsystem
        self._avail = avail
        self._status_result = status_result
        self._status_exc = status_exc
        self._writable = writable
        self.call_count = 0

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<FakeBackend {self.name}>"

    def available(self) -> bool:
        return self._avail

    def status(self, *args, **kwargs):
        self.call_count += 1
        if self._status_exc is not None:
            raise self._status_exc
        return self._status_result

    def enable(self, *args, **kwargs):
        if not self._writable:
            raise NotImplementedError("read-only fake")
        return f"{self.name}-enabled"


# ctypes registered as a class name so we can assert chain ordering.
class FakeBackendA(FakeBackend):
    pass


class FakeBackendB(FakeBackend):
    pass


class FakeBackendC(FakeBackend):
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_register_and_dispatch_priority_order():
    reg = BackendRegistry()
    high = FakeBackendA("nvapi-like", status_result=["high"])
    low = FakeBackendB("shell-like", status_result=["low"])

    # Register low-priority first to prove sort-by-priority is real.
    reg.register("gpu", low, PRIORITY_SHELL)
    reg.register("gpu", high, PRIORITY_NVAPI)

    result = reg.get("gpu").status()
    assert result == ["high"], "higher-priority (lower number) should win"
    assert high.call_count == 1
    assert low.call_count == 0


def test_fallback_on_backend_call_failed():
    reg = BackendRegistry()
    failing = FakeBackendA(
        "nvapi-broken", status_exc=BackendCallFailed("transient")
    )
    healthy = FakeBackendB("shell-ok", status_result=["fallback"])

    reg.register("gpu", failing, PRIORITY_NVAPI)
    reg.register("gpu", healthy, PRIORITY_SHELL)

    assert reg.get("gpu").status() == ["fallback"]
    assert failing.call_count == 1
    assert healthy.call_count == 1

    # BackendCallFailed is non-terminal — primary NOT blacklisted.
    assert not reg.is_blacklisted("gpu", "FakeBackendA")
    assert reg.fallback_chain("gpu") == ["FakeBackendA", "FakeBackendB"]


def test_fallback_and_blacklist_on_struct_mismatch():
    reg = BackendRegistry()
    bad = FakeBackendA(
        "nvapi-mismatch", status_exc=BackendStructMismatch("V2 rejected")
    )
    good = FakeBackendB("shell-ok", status_result=["ok"])

    reg.register("gpu", bad, PRIORITY_NVAPI)
    reg.register("gpu", good, PRIORITY_SHELL)

    assert reg.get("gpu").status() == ["ok"]
    # Struct mismatch is terminal — must be blacklisted for the session.
    assert reg.is_blacklisted("gpu", "FakeBackendA")
    assert reg.blacklist_reason("gpu", "FakeBackendA").startswith("struct mismatch")

    # Next call must skip the blacklisted backend entirely (no call_count bump).
    pre_count = bad.call_count
    reg.get("gpu").status()
    assert bad.call_count == pre_count, (
        "blacklisted backend must NOT be called again"
    )

    # Chain reflects blacklist.
    assert reg.fallback_chain("gpu") == ["FakeBackendB"]


def test_fallback_and_blacklist_on_backend_unavailable_runtime():
    """BackendUnavailable raised mid-session also blacklists."""
    reg = BackendRegistry()
    dead = FakeBackendA(
        "nvapi-dead", status_exc=BackendUnavailable("driver gone")
    )
    alive = FakeBackendB("shell-ok", status_result=["ok"])

    reg.register("gpu", dead, PRIORITY_NVAPI)
    reg.register("gpu", alive, PRIORITY_SHELL)

    assert reg.get("gpu").status() == ["ok"]
    assert reg.is_blacklisted("gpu", "FakeBackendA")
    assert "unavailable" in reg.blacklist_reason("gpu", "FakeBackendA")


def test_skip_registration_when_available_false():
    reg = BackendRegistry()
    absent = FakeBackendA("nvapi-missing", avail=False)
    present = FakeBackendB("shell", status_result=["only-this"])

    reg.register("gpu", absent, PRIORITY_NVAPI)
    reg.register("gpu", present, PRIORITY_SHELL)

    # absent must NOT appear in the chain.
    assert reg.fallback_chain("gpu") == ["FakeBackendB"]
    assert reg.get("gpu").status() == ["only-this"]


def test_notimplementederror_falls_through():
    """Read-only backends raise NotImplementedError on writes; registry
    must walk to the next backend instead of re-raising."""
    reg = BackendRegistry()
    readonly = FakeBackendA("nvapi-read-only", writable=False)
    writable = FakeBackendB("shell-writable")

    reg.register("qsync", readonly, PRIORITY_NVAPI)
    reg.register("qsync", writable, PRIORITY_SHELL)

    result = reg.get("qsync").enable()
    assert result == "shell-writable-enabled"


def test_all_backends_fail_raises_backend_call_failed():
    reg = BackendRegistry()
    a = FakeBackendA("a", status_exc=BackendCallFailed("boom-a"))
    b = FakeBackendB("b", status_exc=BackendCallFailed("boom-b"))
    reg.register("gpu", a, PRIORITY_NVAPI)
    reg.register("gpu", b, PRIORITY_SHELL)

    with pytest.raises(BackendCallFailed):
        reg.get("gpu").status()


def test_no_backends_raises_backend_unavailable():
    reg = BackendRegistry()
    with pytest.raises(BackendUnavailable):
        reg.get("gpu").status()


def test_unknown_subsystem_rejected():
    reg = BackendRegistry()
    b = FakeBackendA("x")
    with pytest.raises(ValueError):
        reg.register("cpu", b, PRIORITY_SHELL)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        reg.get("cpu")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        reg.fallback_chain("cpu")  # type: ignore[arg-type]


def test_three_tier_fallback_chain_ordering():
    reg = BackendRegistry()
    a = FakeBackendA("nvapi", status_exc=BackendCallFailed("a"))
    b = FakeBackendB("nvml", status_exc=BackendCallFailed("b"))
    c = FakeBackendC("shell", status_result=["final"])

    reg.register("gpu", b, PRIORITY_NVML)
    reg.register("gpu", c, PRIORITY_SHELL)
    reg.register("gpu", a, PRIORITY_NVAPI)

    chain = reg.fallback_chain("gpu")
    assert chain == ["FakeBackendA", "FakeBackendB", "FakeBackendC"]

    assert reg.get("gpu").status() == ["final"]
    assert a.call_count == 1
    assert b.call_count == 1
    assert c.call_count == 1


def test_reset_blacklist_rehabilitates():
    reg = BackendRegistry()
    bad = FakeBackendA("bad", status_exc=BackendStructMismatch("rejected"))
    good = FakeBackendB("good", status_result=["ok"])
    reg.register("gpu", bad, PRIORITY_NVAPI)
    reg.register("gpu", good, PRIORITY_SHELL)

    reg.get("gpu").status()
    assert reg.is_blacklisted("gpu", "FakeBackendA")

    reg.reset_blacklist()
    assert not reg.is_blacklisted("gpu", "FakeBackendA")
    assert reg.fallback_chain("gpu") == ["FakeBackendA", "FakeBackendB"]
