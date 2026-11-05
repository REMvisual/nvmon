"""Unit tests for nvmon.discovery.

These run entirely offline: we mock ``zeroconf.Zeroconf`` /
``zeroconf.ServiceBrowser`` / ``zeroconf.ServiceInfo`` at the
`nvmon.discovery` import site so no multicast socket is ever opened.

Coverage
--------
* ``announce()`` calls register_service and builds sensible TXT records
* ``Announcer.shutdown()`` emits a goodbye (unregister_service) exactly once
* ``DiscoveryRegistry`` fires ``appeared`` / ``updated`` / ``goodbye`` /
  ``ttl_expired`` callbacks with the right ``DiscoveredNode`` payloads
* Spoof detection logs a warning when hostname re-appears under a new
  machine_id without hard-failing
* ``merge_with_static`` correctly unions static TOML nodes with mDNS nodes,
  dedup'ing by hostname+port, preserving operator IP, tagging source
* ``get_machine_id()`` returns a stable 16-char hex string
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from nvmon import discovery
from nvmon.discovery import (
    DEFAULT_TTL_SECONDS,
    EVENT_APPEARED,
    EVENT_GOODBYE,
    EVENT_TTL_EXPIRED,
    EVENT_UPDATED,
    SERVICE_TYPE,
    Announcer,
    DiscoveredNode,
    DiscoveryRegistry,
    announce,
    get_machine_id,
    merge_with_static,
)


# ---------------------------------------------------------------------------
# Fakes that mimic the parts of zeroconf we actually touch.
# ---------------------------------------------------------------------------

class _FakeServiceInfo:
    """Minimal stand-in for zeroconf.ServiceInfo."""

    def __init__(
        self,
        type_: str,
        name: str,
        port: int = 0,
        properties: dict | None = None,
        parsed_addresses: list[str] | None = None,
        server: str | None = None,
        **_: Any,
    ) -> None:
        self.type = type_
        self.name = name
        self.port = port
        self.properties = properties or {}
        self._parsed_addresses = parsed_addresses or []
        self.server = server
        self.addresses = [
            ipaddress.IPv4Address(a).packed for a in self._parsed_addresses
            if _looks_ipv4(a)
        ]

    @property
    def decoded_properties(self) -> dict[str, str]:
        out = {}
        for k, v in (self.properties or {}).items():
            ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            if v is None:
                out[ks] = ""
            elif isinstance(v, (bytes, bytearray)):
                out[ks] = v.decode(errors="replace")
            else:
                out[ks] = str(v)
        return out

    @property
    def parsed_addresses(self):  # property, not method — matches our code's fallback
        return list(self._parsed_addresses)


def _looks_ipv4(a: str) -> bool:
    try:
        ipaddress.IPv4Address(a)
        return True
    except Exception:
        return False


class _FakeZeroconf:
    """Captures register/update/unregister calls and drives fake events."""

    def __init__(self, *_, **__) -> None:
        self.registered: list[_FakeServiceInfo] = []
        self.updated: list[_FakeServiceInfo] = []
        self.unregistered: list[_FakeServiceInfo] = []
        self.closed = False
        self._listeners: list[Any] = []

    def register_service(self, info: _FakeServiceInfo, **__) -> None:
        self.registered.append(info)

    def update_service(self, info: _FakeServiceInfo) -> None:
        self.updated.append(info)

    def unregister_service(self, info: _FakeServiceInfo) -> None:
        self.unregistered.append(info)

    def close(self) -> None:
        self.closed = True

    def get_service_info(self, type_: str, name: str, timeout: int = 0):
        # Return whatever the current test has staged as "live".
        if self._current is not None and self._current.name == name:
            return self._current
        for info in reversed(self.registered + self.updated):
            if info.name == name:
                return info
        return None

    _current: _FakeServiceInfo | None = None

    # --- test driver helpers ---------------------------------------------

    def _fire(self, listener: Any, kind: str, info: _FakeServiceInfo) -> None:
        # Stage this specific info as "what zeroconf is serving right now",
        # so get_service_info returns THIS one rather than a stale twin with
        # the same instance name.
        self._current = info
        try:
            if kind == "add":
                listener.add_service(self, info.type, info.name)
            elif kind == "update":
                listener.update_service(self, info.type, info.name)
            elif kind == "remove":
                listener.remove_service(self, info.type, info.name)
        finally:
            self._current = None


class _FakeServiceBrowser:
    """Captures the listener so tests can drive events directly."""

    instances: list["_FakeServiceBrowser"] = []

    def __init__(self, zc: _FakeZeroconf, type_: str, listener: Any) -> None:
        self.zc = zc
        self.type_ = type_
        self.listener = listener
        self.cancelled = False
        _FakeServiceBrowser.instances.append(self)

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture(autouse=True)
def _patch_zeroconf(monkeypatch):
    """Replace the symbols `nvmon.discovery` imported from zeroconf.

    We patch the *module-level* bindings (not the library itself) so any
    object the code under test constructs is actually our fake.
    """
    monkeypatch.setattr(discovery, "Zeroconf", _FakeZeroconf)
    monkeypatch.setattr(discovery, "ServiceInfo", _FakeServiceInfo)
    monkeypatch.setattr(discovery, "ServiceBrowser", _FakeServiceBrowser)
    # IPVersion can stay or be None — our code handles both.
    _FakeServiceBrowser.instances.clear()
    yield


# ---------------------------------------------------------------------------
# machine_id
# ---------------------------------------------------------------------------

def test_machine_id_is_stable_hex_16():
    first = get_machine_id()
    second = get_machine_id()
    assert first == second
    assert len(first) == 16
    int(first, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# announce() / Announcer
# ---------------------------------------------------------------------------

def test_announce_builds_expected_txt_and_registers():
    a = announce(
        port=9998,
        role="agent",
        capabilities={
            "gpu": {"available": True},
            "mosaic": {"available": True},
            "qsync": {"available": False},
        },
        machine_id="deadbeef" * 2,
        extra={"read_only": True},
        hostname="render01",
        ip="192.0.2.50",
        version="1.2.3",
    )

    zc: _FakeZeroconf = a._zc  # type: ignore[assignment]
    assert len(zc.registered) == 1
    info = zc.registered[0]

    assert info.type == SERVICE_TYPE
    assert info.name.startswith("render01.")
    assert info.port == 9998
    assert "192.0.2.50" in info.parsed_addresses

    txt = info.decoded_properties
    assert txt["role"] == "agent"
    assert txt["version"] == "1.2.3"
    assert txt["machine_id"] == "deadbeef" * 2
    # caps is comma-joined, only 'available' ones
    caps = set(txt["caps"].split(","))
    assert caps == {"gpu", "mosaic"}
    # extra flows through
    assert txt["read_only"] == "true"
    # started must parse as ISO
    datetime.fromisoformat(txt["started"])

    a.shutdown()


def test_announce_sanitizes_weird_hostname():
    a = announce(
        port=1,
        role="agent",
        capabilities={},
        machine_id="x" * 16,
        hostname="Render Node 01!",  # spaces + punctuation
        ip="127.0.0.1",
    )
    zc: _FakeZeroconf = a._zc  # type: ignore[assignment]
    info = zc.registered[0]
    label = info.name.split(".", 1)[0]
    # No spaces or illegal chars in the DNS label
    for ch in label:
        assert ch.isalnum() or ch in ("-", "_")
    a.shutdown()


def test_announcer_shutdown_is_idempotent_and_sends_goodbye():
    a = announce(
        port=1, role="agent", capabilities={}, machine_id="a" * 16,
        hostname="host", ip="127.0.0.1",
    )
    zc: _FakeZeroconf = a._zc  # type: ignore[assignment]
    a.shutdown()
    a.shutdown()  # second call must be a no-op
    assert len(zc.unregistered) == 1
    assert zc.closed is True


def test_announcer_update_changes_txt():
    a = announce(
        port=1, role="agent",
        capabilities={"gpu": {"available": True}},
        machine_id="a" * 16,
        hostname="host", ip="127.0.0.1",
    )
    zc: _FakeZeroconf = a._zc  # type: ignore[assignment]

    a.update(
        capabilities={
            "gpu": {"available": True},
            "mosaic": {"available": True},
        },
        extra={"read_only": False},
    )

    assert len(zc.updated) == 1
    updated_info = zc.updated[0]
    txt = updated_info.decoded_properties
    assert set(txt["caps"].split(",")) == {"gpu", "mosaic"}
    assert txt["read_only"] == "false"

    a.shutdown()


# ---------------------------------------------------------------------------
# DiscoveryRegistry lifecycle + events
# ---------------------------------------------------------------------------

def _make_info(
    hostname: str = "render01",
    ip: str = "192.0.2.50",
    port: int = 9998,
    machine_id: str = "mid-aaaa",
    caps: str = "gpu,mosaic",
    role: str = "agent",
    version: str = "0.1.0",
) -> _FakeServiceInfo:
    props = {
        b"role": role.encode(),
        b"version": version.encode(),
        b"caps": caps.encode(),
        b"machine_id": machine_id.encode(),
        b"started": b"2026-04-17T00:00:00+00:00",
        b"path": b"",
    }
    return _FakeServiceInfo(
        type_=SERVICE_TYPE,
        name=f"{hostname}.{SERVICE_TYPE}",
        port=port,
        properties=props,
        parsed_addresses=[ip],
    )


def _start_registry() -> tuple[DiscoveryRegistry, _FakeZeroconf, _FakeServiceBrowser, list[tuple[str, DiscoveredNode]]]:
    events: list[tuple[str, DiscoveredNode]] = []
    reg = DiscoveryRegistry()
    reg.on_change(lambda k, n: events.append((k, n)))
    reg.start()
    zc = reg._zc  # type: ignore[assignment]
    browser = _FakeServiceBrowser.instances[-1]
    return reg, zc, browser, events


def test_registry_fires_appeared_then_updated():
    reg, zc, browser, events = _start_registry()
    try:
        info = _make_info()
        zc.registered.append(info)  # so get_service_info can find it

        zc._fire(browser.listener, "add", info)
        assert any(k == EVENT_APPEARED for k, _ in events)

        # Second event for same machine_id == updated, not duplicate-appeared
        zc._fire(browser.listener, "update", info)
        assert [k for k, _ in events].count(EVENT_APPEARED) == 1
        assert any(k == EVENT_UPDATED for k, _ in events)

        nodes = reg.get_all()
        assert len(nodes) == 1
        assert nodes[0].hostname == "render01"
        assert nodes[0].ip == "192.0.2.50"
        assert nodes[0].port == 9998
        assert nodes[0].role == "agent"
        assert nodes[0].caps == ["gpu", "mosaic"]
        assert nodes[0].source == "mdns"
    finally:
        reg.stop()


def test_registry_fires_goodbye_and_drops_node():
    reg, zc, browser, events = _start_registry()
    try:
        info = _make_info()
        zc.registered.append(info)
        zc._fire(browser.listener, "add", info)
        assert reg.get_all(), "node should exist after add"

        zc._fire(browser.listener, "remove", info)
        kinds = [k for k, _ in events]
        assert EVENT_GOODBYE in kinds
        assert reg.get_all() == []
    finally:
        reg.stop()


def test_registry_ttl_eviction_fires_ttl_expired():
    reg, zc, browser, events = _start_registry()
    try:
        info = _make_info()
        zc.registered.append(info)
        zc._fire(browser.listener, "add", info)
        assert reg.get_all()

        # Backdate last_seen beyond EVICTION_FACTOR×TTL so the sweep evicts
        # immediately. The factor was bumped from 2 to 4 to be tolerant of
        # mDNS announcement loss on busy multicast networks (a missed
        # re-announcement at 2×TTL was causing healthy nodes to vanish);
        # 5× gives test margin without hardcoding the constant.
        node = reg.get_all()[0]
        node.last_seen = datetime.now(timezone.utc) - timedelta(
            seconds=DEFAULT_TTL_SECONDS * 5,
        )

        # Don't wait for the background sweeper — call the sweep directly
        # so the test stays sub-second and doesn't depend on timing.
        reg._sweep_once()

        kinds = [k for k, _ in events]
        assert EVENT_TTL_EXPIRED in kinds
        assert reg.get_all() == []
    finally:
        reg.stop()


def test_registry_get_by_role_filters():
    reg, zc, browser, _ = _start_registry()
    try:
        a = _make_info(hostname="agent01", machine_id="mid-agent", role="agent")
        l = _make_info(
            hostname="launcher01", ip="192.0.2.51", port=9996,
            machine_id="mid-launch", role="launcher", caps="apps",
        )
        zc.registered.extend([a, l])
        zc._fire(browser.listener, "add", a)
        zc._fire(browser.listener, "add", l)

        agents = reg.get_by_role("agent")
        launchers = reg.get_by_role("launcher")
        assert {n.hostname for n in agents} == {"agent01"}
        assert {n.hostname for n in launchers} == {"launcher01"}
    finally:
        reg.stop()


# ---------------------------------------------------------------------------
# Spoof detection
# ---------------------------------------------------------------------------

def test_registry_logs_warning_on_identity_conflict(caplog):
    # Ensure the nvmon.discovery logger propagates to caplog's root handler.
    import logging as _logging
    disc_logger = _logging.getLogger("nvmon.discovery")
    disc_logger.setLevel(_logging.DEBUG)
    disc_logger.propagate = True
    caplog.set_level(_logging.WARNING, logger="nvmon.discovery")

    reg, zc, browser, _ = _start_registry()
    try:
        first = _make_info(hostname="shared-host", machine_id="mid-original")
        second = _make_info(hostname="shared-host", machine_id="mid-attacker")
        zc.registered.extend([first, second])

        zc._fire(browser.listener, "add", first)
        zc._fire(browser.listener, "add", second)

        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "identity_conflict" in messages, (
            f"expected identity_conflict warning, got: {messages!r}"
        )
        # Both nodes end up registered — we do NOT hard-fail per spec.
        nodes = reg.get_all()
        ids = {n.machine_id for n in nodes}
        assert "mid-original" in ids
        assert "mid-attacker" in ids
    finally:
        reg.stop()


# ---------------------------------------------------------------------------
# merge_with_static
# ---------------------------------------------------------------------------

def test_merge_static_only():
    static = [
        {"hostname": "node-02", "url": "http://192.0.2.12:9998"},
        {"hostname": "node-04", "url": "http://192.0.2.14:9998"},
    ]
    merged = merge_with_static([], static)
    assert len(merged) == 2
    assert all(n.source == "static" for n in merged)
    assert {n.hostname for n in merged} == {"node-02", "node-04"}
    assert {n.ip for n in merged} == {"192.0.2.12", "192.0.2.14"}
    assert all(n.port == 9998 for n in merged)


def test_merge_mdns_only():
    d = DiscoveredNode(
        hostname="node-03", ip="192.0.2.13", port=9998,
        caps=["gpu"], machine_id="m3",
    )
    merged = merge_with_static([d], [])
    assert len(merged) == 1
    assert merged[0].source == "mdns"


def test_merge_static_plus_mdns_dedups_by_hostname_port():
    static = [{"hostname": "node-02", "url": "http://192.0.2.12:9998"}]
    d = DiscoveredNode(
        hostname="node-02", ip="192.0.2.12", port=9998,
        role="agent", caps=["gpu", "mosaic"], machine_id="mid-node-02",
        version="0.1.0",
    )
    merged = merge_with_static([d], static)
    assert len(merged) == 1
    n = merged[0]
    assert n.source == "static+mdns"
    # mDNS enriches the static row
    assert n.machine_id == "mid-node-02"
    assert n.caps == ["gpu", "mosaic"]
    assert n.version == "0.1.0"
    # Operator's IP is authoritative
    assert n.ip == "192.0.2.12"
    assert n.port == 9998


def test_merge_static_preserves_unmatched_static_entries():
    static = [
        {"hostname": "node-02", "url": "http://192.0.2.12:9998"},
        {"hostname": "cross-subnet", "url": "http://10.20.1.5:9998"},
    ]
    d = DiscoveredNode(
        hostname="node-02", ip="192.0.2.12", port=9998, machine_id="m2",
    )
    merged = merge_with_static([d], static)
    hostnames = {n.hostname: n.source for n in merged}
    assert hostnames == {
        "node-02": "static+mdns",
        "cross-subnet": "static",
    }


def test_merge_handles_malformed_static_rows():
    static = [
        {"hostname": "ok", "url": "http://1.2.3.4:9998"},
        {"hostname": "", "url": ""},       # dropped
        {"url": "http://1.2.3.5:9998"},    # no hostname — uses ip as hostname
        "not a dict",                       # dropped
    ]
    merged = merge_with_static([], static)  # type: ignore[arg-type]
    names = {n.hostname for n in merged}
    assert "ok" in names
    assert "1.2.3.5" in names
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# Smoke: end-to-end event callback shape
# ---------------------------------------------------------------------------

def test_event_callback_receives_discovered_node_instances():
    events: list[tuple[str, DiscoveredNode]] = []
    reg = DiscoveryRegistry()
    reg.on_change(lambda k, n: events.append((k, n)))
    reg.start()
    try:
        zc: _FakeZeroconf = reg._zc  # type: ignore[assignment]
        browser = _FakeServiceBrowser.instances[-1]
        info = _make_info()
        zc.registered.append(info)
        zc._fire(browser.listener, "add", info)

        assert events, "callback never fired"
        kind, node = events[0]
        assert kind == EVENT_APPEARED
        assert isinstance(node, DiscoveredNode)
        assert node.hostname == "render01"
    finally:
        reg.stop()
