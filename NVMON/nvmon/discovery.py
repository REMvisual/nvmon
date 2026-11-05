"""nvmon network auto-discovery — mDNS / DNS-SD via python-zeroconf.

Implements sub-project C of the 2026-04-17 design suite. The controller
previously read a static `[[controller.nodes]]` list from `nvmon.toml`
(or, in legacy the legacy tool `nvdd`, a hardcoded `{IP: NAME}` dict). This module
replaces that with zero-config LAN discovery while still honouring static
TOML entries as a pinned override.

Service type: ``_nvmon._tcp.local.``

TXT records carry identity + capability hints; the controller always falls
back to ``/health`` on the announced host:port as the source of truth for
fan-out decisions (see spec A).

Windows Firewall / Ports
------------------------
mDNS uses UDP port 5353 (multicast 224.0.0.251). On Windows, if the process
binds the multicast socket as SYSTEM (typical for a service), the first-run
firewall prompt never appears and announcements silently fail. The install
script (see sub-project A4 / `install.bat`) owns adding these rules for the
frozen `nvmon.exe`; the required rules are:

    netsh advfirewall firewall add rule name="nvmon mDNS in"  \
        dir=in  action=allow protocol=UDP localport=5353  program="<nvmon.exe>"
    netsh advfirewall firewall add rule name="nvmon mDNS out" \
        dir=out action=allow protocol=UDP remoteport=5353 program="<nvmon.exe>"

This module does not attempt to create the rules itself; it only logs a
clear warning if the multicast socket cannot bind.

Integration notes (for sibling agents — do not edit other agents' files)
------------------------------------------------------------------------
* **Agent (A2 / `nvmon/agent/server.py`)** — on startup, after the HTTP
  server is listening, call::

      announcer = announce(
          port=cfg["port"],                  # 9998
          role="agent",
          capabilities=health_caps,          # matches /health.capabilities
          machine_id=get_machine_id(),
          extra={"read_only": "true"|"false"},
      )

  and on shutdown call ``announcer.shutdown()`` before exiting the HTTP
  server so a zeroconf goodbye packet is sent.

* **Launcher (A / `nvmon/launcher/server.py`)** — same pattern with
  ``role="launcher"`` and ``port`` from the launcher config.

* **Controller (A3 / `nvmon/controller/registry.py`)** — at startup::

      registry = DiscoveryRegistry()
      registry.on_change(self._on_discovery_change)
      registry.start()                            # non-blocking

  On every discovery event or on a periodic tick, merge the live mDNS
  nodes with the static TOML list::

      merged = merge_with_static(
          discovered=registry.get_all(),
          static=get_controller_config()["nodes"],
      )

  then hand `merged` to the health-probe / fan-out pipeline. Remember that
  static entries are never auto-removed; mDNS entries expire per TTL.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

try:
    # zeroconf is a hard runtime dep but we want this module to remain
    # importable (for `nvmon discover --help`, unit tests, etc.) even if
    # someone is poking around without the wheel installed.
    from zeroconf import (
        IPVersion,
        ServiceBrowser,
        ServiceInfo,
        ServiceListener,
        Zeroconf,
    )
except ImportError:  # pragma: no cover - exercised only in broken envs
    IPVersion = None  # type: ignore[assignment]
    ServiceBrowser = None  # type: ignore[assignment]
    ServiceInfo = None  # type: ignore[assignment]
    ServiceListener = object  # type: ignore[assignment,misc]
    Zeroconf = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_TYPE = "_nvmon._tcp.local."


def _registry_key(node: "DiscoveredNode") -> str:
    """Cache key for ``DiscoveryRegistry._nodes_by_id``.

    The agent and the launcher both run on the same physical box and share
    the same ``machine_id``. Keying by ``machine_id`` alone caused the two
    announcements to collide — whichever advertised second silently evicted
    the first from the cache, so a controller polling the survivor's port
    (9996 launcher vs 9998 agent) saw the wrong service and reported the
    node OFFLINE. Folding ``role`` into the key keeps both alive.
    """
    base = node.machine_id or node.key or node.hostname
    role = (node.role or "").lower() or "any"
    return f"{base}::{role}"
DEFAULT_TTL_SECONDS = 120
"""Per spec: TTL 120s, eviction at 2×TTL. zeroconf enforces cache expiry
using the record's own TTL, so we simply pass this value at registration."""

EVICTION_FACTOR = 4
"""Seconds before a silent node (no refresh, no goodbye) is evicted =
TTL_SECONDS * EVICTION_FACTOR. Originally 2 (240s grace). Bumped to 4
(480s = 8min) because on a busy mDNS network with many nodes
(11+ render boxes all multicasting), individual announcements can be
dropped by the switch's IGMP snooping or the OS multicast stack — a
single missed re-announcement at 2x TTL caused legitimate nodes to
disappear from the dashboard and reappear seconds later. The grace
period is purely a UI smoothing knob; the aggregator's per-node
/health poll is the actual liveness signal."""

# Event kinds emitted to on_change callbacks.
EVENT_APPEARED = "appeared"
EVENT_UPDATED = "updated"
EVENT_GOODBYE = "goodbye"
EVENT_TTL_EXPIRED = "ttl_expired"

_log = logging.getLogger("nvmon.discovery")


# ---------------------------------------------------------------------------
# DiscoveredNode
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredNode:
    """Single node, either learned via mDNS or loaded from static TOML.

    Notes on the fields the spec calls out explicitly:

    * ``machine_id`` — opaque identity stamp, stable across agent restarts.
      Controller uses it to detect bounces (same machine_id, newer started)
      vs identity conflicts (same hostname, different machine_id).
    * ``alive`` — True if the node is currently in the registry. Goes False
      when zeroconf fires remove_service or when the 2×TTL sweep evicts
      a silent node. Static entries are always alive.
    * ``source`` — "mdns" or "static" per spec merge semantics.
    """

    hostname: str
    ip: str
    port: int
    role: str = "agent"
    caps: list[str] = field(default_factory=list)
    machine_id: str = ""
    version: str = ""
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    alive: bool = True
    source: str = "mdns"  # "mdns" | "static"
    # Everything else the TXT record happened to carry. Not required for the
    # spec contract but preserved so the controller's /discovery/status can
    # surface it without a re-parse.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable dedup key. hostname+port per spec merge rule."""
        return f"{self.hostname.lower()}:{self.port}"

    def url(self, path: str = "/health") -> str:
        """Convenience — the controller uses this for /health probing."""
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{self.ip}:{self.port}{path}"


# ---------------------------------------------------------------------------
# machine_id
# ---------------------------------------------------------------------------

def get_machine_id() -> str:
    """Return a stable 16-char hex machine identifier.

    On Windows we read ``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``
    via ``winreg``. This value survives reboots, driver reinstalls, and most
    hardware swaps short of a clean OS reinstall — exactly the stability the
    spec wants for identity binding.

    On non-Windows (developer boxes, CI, Linux containers) we fall back to a
    salted hash of ``hostname + MAC`` so tests can still run end-to-end. The
    fallback is deterministic per-box but obviously not cross-OS stable; we
    log the fact so nobody is surprised in prod.
    """
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        # Hash to a short opaque ID — we don't want to leak the raw
        # MachineGuid on the wire in TXT records.
        return hashlib.sha256(str(guid).encode("utf-8")).hexdigest()[:16]
    except (ImportError, OSError, FileNotFoundError):
        # Non-Windows or registry key missing. Fall through to the salted
        # hostname+MAC fallback. We log at DEBUG to avoid spamming devs.
        _log.debug(
            "machine_id: MachineGuid unavailable, using hostname+MAC fallback"
        )

    hostname = socket.gethostname()
    try:
        mac = uuid.getnode()
    except Exception:
        mac = 0
    salt = "nvmon-discovery-v1"
    payload = f"{salt}|{hostname}|{mac:012x}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Announcer (agent / launcher side)
# ---------------------------------------------------------------------------

class Announcer:
    """Handle returned by ``announce()``. Call ``.shutdown()`` to deregister.

    The constructor registers the service synchronously (so a failure surfaces
    immediately at agent startup). ``shutdown()`` calls
    ``Zeroconf.unregister_service()`` which emits a goodbye (TTL=0) packet
    and then closes the socket. Idempotent — safe to call twice.
    """

    def __init__(
        self,
        *,
        zc: "Zeroconf",
        info: "ServiceInfo",
        owns_zc: bool = True,
    ) -> None:
        self._zc = zc
        self._info = info
        self._owns_zc = owns_zc
        self._shutdown = False
        self._lock = threading.Lock()

    @property
    def info(self) -> "ServiceInfo":
        return self._info

    @property
    def instance_name(self) -> str:
        return self._info.name

    def update(
        self,
        *,
        capabilities: dict[str, Any] | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        """Re-advertise with updated TXT (e.g. a subsystem came online).

        Per spec: on any capability change the agent should re-register so
        the controller notices within a few seconds (zeroconf dispatches
        the TXT change as a RecordUpdate event).
        """
        if self._shutdown:
            return
        props = dict(self._info.decoded_properties or {})
        if capabilities is not None:
            props["caps"] = ",".join(_cap_names(capabilities))
        if extra:
            for k, v in extra.items():
                props[k] = _stringify(v)
        # Rebuild the ServiceInfo in place — zeroconf requires a fresh
        # properties payload for update_service.
        self._info.properties = _encode_txt(props)
        try:
            self._zc.update_service(self._info)
        except Exception as exc:
            _log.warning("discovery: update_service failed: %s", exc)

    def shutdown(self) -> None:
        """Send goodbye packet and close the zeroconf instance (if owned)."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        try:
            self._zc.unregister_service(self._info)
            _log.info("discovery: goodbye sent for %s", self._info.name)
        except Exception as exc:
            _log.warning("discovery: unregister_service failed: %s", exc)
        if self._owns_zc:
            try:
                self._zc.close()
            except Exception as exc:
                _log.warning("discovery: zeroconf close failed: %s", exc)

    # Allow use as a context manager for tests / short-lived scripts.
    def __enter__(self) -> "Announcer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()


def announce(
    port: int,
    role: str,
    capabilities: dict[str, Any],
    machine_id: str,
    extra: dict[str, Any] | None = None,
    *,
    service_type: str = SERVICE_TYPE,
    hostname: str | None = None,
    ip: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    zc: "Zeroconf | None" = None,
    version: str = "0.1.0",
) -> Announcer:
    """Register this process with mDNS so controllers can discover it.

    Parameters
    ----------
    port
        The TCP port the service listens on (e.g. 9998 for the agent).
    role
        ``"agent"`` or ``"launcher"`` (or future ``"controller"``).
    capabilities
        The same dict shape as ``/health.capabilities`` on the agent, e.g.
        ``{"gpu": {"available": True}, "mosaic": {"available": False}}``.
        Only subsystem names with ``available: True`` end up in TXT ``caps``.
    machine_id
        Stable opaque ID for this host — normally ``get_machine_id()``.
    extra
        Any additional TXT fields to advertise (e.g. ``{"read_only": "true"}``).
        Values are coerced to str. Keys must already be DNS-TXT-safe.
    service_type
        Override for tests.
    hostname
        Override ``socket.gethostname()`` — useful for tests and containers.
    ip
        Override the announced IP. By default we use
        ``config.get_local_ip()`` (best-effort outbound IP).
    ttl_seconds
        TTL advertised to peers. Spec default is 120s.
    zc
        Pre-built Zeroconf instance for tests / advanced reuse. If None,
        a fresh one is created and owned by the returned ``Announcer``
        (shutdown closes it).
    version
        nvmon version string to stamp in TXT. Override if building a
        release wheel with a real version.

    Integration reminder
    --------------------
    Sibling agent A2 calls this from ``server.py`` right after
    ``http_server.start()``. Sibling agent A for the launcher does the
    same from ``nvmon/launcher/server.py``. Both must keep the returned
    ``Announcer`` alive for the lifetime of the process and call
    ``.shutdown()`` on clean exit (goodbye packet).
    """
    if Zeroconf is None or ServiceInfo is None:
        raise RuntimeError(
            "zeroconf library not installed — pip install -r requirements.txt"
        )

    # Resolve identity.
    host = (hostname or socket.gethostname()).strip() or "unknown-host"
    sanitized = _sanitize_instance_label(host)
    instance_name = f"{sanitized}.{service_type}"

    # Resolve IP. Import lazily so the module doesn't hard-require `config`
    # just to exist (tests can inject ip=).
    if ip is None:
        try:
            from . import config as _config
            ip = _config.get_local_ip()
        except Exception:
            ip = "127.0.0.1"

    # Build TXT. We include the actual resolved `host` here so the
    # receiver uses our self-reported name even when zeroconf renames
    # the instance for collision avoidance (a bouncing listener whose
    # TTL hasn't expired gets a `-2` suffix appended to instance_name;
    # without hostname in the TXT, the controller would record it as
    # 'NODE-01-2' forever instead of the real 'NODE-01').
    #
    # Use `host` (the already-resolved + sanitized string), NOT the raw
    # `hostname` parameter — that param defaults to None for callers that
    # let socket.gethostname() do the work, and str(None) = "None" would
    # poison the TXT record with the literal string "None". v0.6.14 had
    # this bug and produced a 'None' node in every controller's registry.
    cap_names = _cap_names(capabilities)
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    props: dict[str, str] = {
        "role": str(role),
        "version": str(version),
        "caps": ",".join(cap_names),
        "machine_id": str(machine_id),
        "hostname": host,
        "started": started,
        "path": "",  # future-proof per spec
    }
    if extra:
        for k, v in extra.items():
            props[str(k)] = _stringify(v)

    owns_zc = zc is None
    if zc is None:
        try:
            zc = Zeroconf(ip_version=IPVersion.V4Only) if IPVersion else Zeroconf()
        except OSError as exc:
            # Classic symptom of Windows Firewall blocking UDP:5353 on a
            # service-account process — log loudly but re-raise so the
            # caller (agent.server) can decide whether to fall back to
            # HTTP-only mode per spec.
            _log.error(
                "discovery: failed to bind multicast socket — firewall "
                "likely blocking UDP:5353 (see install.bat). %s", exc,
            )
            raise

    info = ServiceInfo(
        type_=service_type,
        name=instance_name,
        port=int(port),
        properties=_encode_txt(props),
        server=f"{sanitized}.local.",
        parsed_addresses=[ip],
    )

    try:
        zc.register_service(info, ttl=ttl_seconds, allow_name_change=True)
    except Exception:
        if owns_zc:
            try:
                zc.close()
            except Exception:
                pass
        raise

    # Detect auto-rename (zeroconf appends -2/-3/... on collision).
    if info.name != instance_name:
        _log.warning(
            "discovery: instance name collision — registered as %s (wanted %s)",
            info.name, instance_name,
        )

    _log.info(
        "discovery: announced instance=%s ip=%s port=%d ttl=%d role=%s caps=[%s]",
        info.name, ip, port, ttl_seconds, role, ",".join(cap_names),
    )
    return Announcer(zc=zc, info=info, owns_zc=owns_zc)


# ---------------------------------------------------------------------------
# DiscoveryRegistry (controller side)
# ---------------------------------------------------------------------------

class DiscoveryRegistry:
    """Controller-side registry. Non-blocking.

    ``start()`` begins a zeroconf ``ServiceBrowser`` in a background thread
    managed by the zeroconf library itself. All callbacks run on zeroconf's
    own thread; ``get_all()``/``get_by_role()`` lock the registry so the
    controller can read safely from any thread.

    Call ``on_change(cb)`` *before* ``start()`` (or any time — new callbacks
    pick up future events only). Callback signature::

        cb(kind: str, node: DiscoveredNode) -> None

    Where ``kind`` is one of ``appeared``, ``updated``, ``goodbye``,
    ``ttl_expired``.

    Integration reminder
    --------------------
    A3's ``nvmon/controller/registry.py`` instantiates one
    ``DiscoveryRegistry`` at controller startup, registers an ``on_change``
    callback to kick a fresh ``/health`` probe whenever ``appeared`` fires
    (per spec: out-of-cycle probe for instant dashboard updates), and
    merges the static TOML nodes via ``merge_with_static(get_all(),
    static_nodes)`` before returning the union to the aggregator.
    """

    def __init__(
        self,
        service_type: str = SERVICE_TYPE,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        zc: "Zeroconf | None" = None,
    ) -> None:
        self.service_type = service_type
        self.ttl_seconds = ttl_seconds

        self._owns_zc = zc is None
        self._zc = zc
        self._browser: "ServiceBrowser | None" = None

        self._nodes_by_id: dict[str, DiscoveredNode] = {}
        # hostname -> first-seen machine_id, for spoof detection.
        self._id_bindings: dict[str, str] = {}
        self._lock = threading.RLock()

        self._callbacks: list[Callable[[str, DiscoveredNode], None]] = []

        self._sweeper: threading.Thread | None = None
        self._sweeper_stop = threading.Event()
        self._started = False

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Begin listening. Returns immediately."""
        if self._started:
            return
        if Zeroconf is None or ServiceBrowser is None:
            raise RuntimeError(
                "zeroconf library not installed — pip install -r requirements.txt"
            )

        if self._zc is None:
            self._zc = Zeroconf(ip_version=IPVersion.V4Only) if IPVersion else Zeroconf()
            self._owns_zc = True

        listener = _RegistryListener(self)
        self._browser = ServiceBrowser(self._zc, self.service_type, listener)

        # Sweep thread: backstop eviction at 2×TTL for nodes that never
        # sent a goodbye. zeroconf's own cache also expires records based
        # on their TTL, but we keep our own sweep so the on_change
        # callback reliably fires `ttl_expired` exactly once per lost node.
        self._sweeper_stop.clear()
        self._sweeper = threading.Thread(
            target=self._sweep_loop, name="nvmon-discovery-sweep", daemon=True,
        )
        self._sweeper.start()
        self._started = True
        _log.info(
            "discovery: registry started, browsing %s", self.service_type,
        )

    def stop(self) -> None:
        """Stop cleanly. Cancels the browser, joins the sweeper."""
        if not self._started:
            return
        self._sweeper_stop.set()
        if self._browser is not None:
            try:
                self._browser.cancel()
            except Exception:
                pass
            self._browser = None
        if self._sweeper is not None:
            self._sweeper.join(timeout=2.0)
            self._sweeper = None
        if self._owns_zc and self._zc is not None:
            try:
                self._zc.close()
            except Exception:
                pass
        self._zc = None
        self._started = False
        _log.info("discovery: registry stopped")

    def __enter__(self) -> "DiscoveryRegistry":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    # -- read API ------------------------------------------------------

    def get_all(self) -> list[DiscoveredNode]:
        """Snapshot of currently-alive discovered nodes."""
        with self._lock:
            return [n for n in self._nodes_by_id.values() if n.alive]

    def get_by_role(self, role: str) -> list[DiscoveredNode]:
        return [n for n in self.get_all() if n.role == role]

    def get(self, machine_id: str, role: str = "agent") -> DiscoveredNode | None:
        """Look up a discovered node by ``(machine_id, role)``. ``role``
        defaults to ``"agent"`` because that's the slot the controller cares
        about; pass another role for the launcher / controller variants."""
        with self._lock:
            for node in self._nodes_by_id.values():
                if node.machine_id == machine_id and (
                    not role or node.role.lower() == role.lower()
                ):
                    return node
            return None

    def on_change(
        self, callback: Callable[[str, DiscoveredNode], None],
    ) -> None:
        self._callbacks.append(callback)

    # -- internal: called by _RegistryListener -------------------------

    def _on_service_event(
        self,
        info: "ServiceInfo | None",
        *,
        removed: bool,
    ) -> None:
        if info is None:
            return
        node = _node_from_info(info)
        if node is None:
            return

        if removed:
            self._handle_goodbye(node)
        else:
            self._handle_appear_or_update(node)

    def _handle_appear_or_update(self, node: DiscoveredNode) -> None:
        with self._lock:
            # The cache key must include `role` so an agent + launcher
            # running on the same physical box (same machine_id) don't
            # collide.  When they did, whichever announced second
            # overwrote the first — the controller would then see the
            # launcher's port (9996) for the agent slot, /health on the
            # wrong port failed, and the dashboard reported OFFLINE.
            cache_key = _registry_key(node)
            existing = self._nodes_by_id.get(cache_key)

            # Spoof / identity conflict detection: same hostname, different
            # machine_id. Don't hard-fail per spec — log a warning.
            prior_id = self._id_bindings.get(node.hostname.lower())
            if node.machine_id and prior_id and prior_id != node.machine_id:
                _log.warning(
                    "discovery: identity_conflict hostname=%s "
                    "old_machine_id=%s new_machine_id=%s — still registering "
                    "but fan-out trust is up to the controller",
                    node.hostname, prior_id, node.machine_id,
                )

            if node.machine_id:
                self._id_bindings[node.hostname.lower()] = node.machine_id

            if existing is None:
                key = cache_key
            else:
                key = cache_key

            if existing is None:
                node.first_seen = node.last_seen
                self._nodes_by_id[key] = node
                self._fire(EVENT_APPEARED, node)
            else:
                # In-place update: preserve first_seen but refresh the rest.
                existing.ip = node.ip
                existing.port = node.port
                existing.role = node.role
                existing.caps = node.caps
                existing.version = node.version
                existing.extra = node.extra
                existing.last_seen = node.last_seen
                existing.alive = True
                # hostname may have changed while machine_id stayed.
                if existing.hostname != node.hostname:
                    _log.info(
                        "discovery: hostname_changed machine_id=%s "
                        "old=%s new=%s",
                        existing.machine_id, existing.hostname, node.hostname,
                    )
                    existing.hostname = node.hostname
                self._fire(EVENT_UPDATED, existing)

    def _handle_goodbye(self, node: DiscoveredNode) -> None:
        with self._lock:
            match = self._find_by_identity(node)
            if match is None:
                return
            match.alive = False
            self._fire(EVENT_GOODBYE, match)
            # Drop immediately on goodbye (spec: "Immediate removal" for
            # mdns-source). Static entries never enter this registry.
            self._nodes_by_id.pop(_registry_key(match), None)

    def _find_by_identity(self, node: DiscoveredNode) -> DiscoveredNode | None:
        cache_key = _registry_key(node)
        if cache_key in self._nodes_by_id:
            return self._nodes_by_id[cache_key]
        # Fallback — same hostname+port (old agent without machine_id).
        for existing in self._nodes_by_id.values():
            if existing.key == node.key:
                return existing
        return None

    def _sweep_loop(self) -> None:
        """Evict silently-gone nodes at 2×TTL."""
        poll = max(5.0, self.ttl_seconds / 4.0)
        while not self._sweeper_stop.wait(timeout=poll):
            self._sweep_once()

    def _sweep_once(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - (
            self.ttl_seconds * EVICTION_FACTOR
        )
        expired: list[DiscoveredNode] = []
        with self._lock:
            for key, node in list(self._nodes_by_id.items()):
                if not node.alive:
                    continue
                if node.last_seen.timestamp() < cutoff:
                    node.alive = False
                    expired.append(node)
                    self._nodes_by_id.pop(key, None)
        for n in expired:
            _log.info(
                "discovery: ttl_expired hostname=%s last_seen_age_s=%.0f",
                n.hostname,
                datetime.now(timezone.utc).timestamp() - n.last_seen.timestamp(),
            )
            self._fire(EVENT_TTL_EXPIRED, n)

    def _fire(self, kind: str, node: DiscoveredNode) -> None:
        for cb in list(self._callbacks):
            try:
                cb(kind, node)
            except Exception as exc:
                _log.warning("discovery: on_change callback raised: %s", exc)


class _RegistryListener(ServiceListener):  # type: ignore[misc,valid-type]
    """Glue between zeroconf's ServiceListener interface and our registry."""

    def __init__(self, registry: DiscoveryRegistry) -> None:
        self._registry = registry

    # zeroconf calls these on its own thread.
    def add_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        self._registry._on_service_event(info, removed=False)

    def update_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        self._registry._on_service_event(info, removed=False)

    def remove_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
        # On goodbye zeroconf has already dropped the record, so
        # get_service_info will typically return a stale copy or None.
        # Either way we just need enough info to match — zeroconf gives
        # us the instance name which encodes the hostname portion.
        info = zc.get_service_info(type_, name, timeout=500)
        if info is None:
            # Synthesize a minimal node keyed by hostname from the name.
            host = _instance_label(name) or name
            info = None
            fake = DiscoveredNode(
                hostname=host,
                ip="",
                port=0,
                alive=False,
            )
            self._registry._handle_goodbye(fake)
            return
        self._registry._on_service_event(info, removed=True)


# ---------------------------------------------------------------------------
# Merge with static TOML
# ---------------------------------------------------------------------------

def merge_with_static(
    discovered: Iterable[DiscoveredNode],
    static: Iterable[dict[str, Any]] | None,
) -> list[DiscoveredNode]:
    """Merge mDNS-discovered nodes with TOML ``[[controller.nodes]]`` list.

    Per spec:
    * Static entries have ``source="static"`` and are never auto-removed.
    * Dedup by ``hostname+port``. If the same key appears in both, we keep
      one entry and flag it as discovered by both mechanisms — the static
      URL wins for hostname/ip (operator-authoritative) while mDNS
      contributes machine_id, version, and cap list.
    * Static entries that weren't rediscovered via mDNS stay in the output
      (so the controller still probes them).
    """
    static_nodes: list[DiscoveredNode] = []
    for raw in static or []:
        node = _node_from_static(raw)
        if node is not None:
            static_nodes.append(node)

    # Index discovered by key for O(1) merge.
    disc_list = list(discovered)
    disc_by_key: dict[str, DiscoveredNode] = {}
    for d in disc_list:
        # Later duplicates overwrite earlier — shouldn't happen in practice.
        disc_by_key[d.key] = d

    merged_by_key: dict[str, DiscoveredNode] = {}

    # Start from static entries (operator-authoritative).
    for s in static_nodes:
        merged = DiscoveredNode(
            hostname=s.hostname,
            ip=s.ip,
            port=s.port,
            role=s.role,
            caps=list(s.caps),
            machine_id=s.machine_id,
            version=s.version,
            first_seen=s.first_seen,
            last_seen=s.last_seen,
            alive=True,
            source="static",
            extra=dict(s.extra),
        )
        d = disc_by_key.get(s.key)
        if d is not None:
            # Enrich with mDNS-only fields but keep the operator's
            # hostname/ip/port.
            if d.machine_id and not merged.machine_id:
                merged.machine_id = d.machine_id
            if d.version and not merged.version:
                merged.version = d.version
            if d.caps and not merged.caps:
                merged.caps = list(d.caps)
            merged.last_seen = max(merged.last_seen, d.last_seen)
            merged.extra = {**d.extra, **merged.extra}
            merged.source = "static+mdns"
        merged_by_key[merged.key] = merged

    # Add purely-mdns nodes that aren't shadowed by static.
    for d in disc_list:
        if d.key in merged_by_key:
            continue
        merged_by_key[d.key] = d

    return list(merged_by_key.values())


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _encode_txt(props: dict[str, str]) -> dict[bytes, bytes]:
    """Zeroconf expects dict[bytes, bytes] for properties."""
    out: dict[bytes, bytes] = {}
    for k, v in props.items():
        kb = k.encode("utf-8") if isinstance(k, str) else bytes(k)
        vb = (v if isinstance(v, (bytes, bytearray)) else str(v).encode("utf-8"))
        out[kb] = bytes(vb)
    return out


def _decode_txt(info: "ServiceInfo") -> dict[str, str]:
    """Flatten zeroconf TXT records into a str->str dict."""
    try:
        decoded = info.decoded_properties or {}
    except AttributeError:
        decoded = {}
    out: dict[str, str] = {}
    for k, v in decoded.items():
        if k is None:
            continue
        out[str(k)] = "" if v is None else str(v)
    return out


def _cap_names(capabilities: dict[str, Any] | None) -> list[str]:
    """Extract the capability names that are currently available.

    Matches `/health.capabilities` shape where each sub-entry is either
    ``{"available": True, ...}`` or, for convenience, a bare truthy value.
    """
    if not capabilities:
        return []
    out: list[str] = []
    for name, val in capabilities.items():
        if isinstance(val, dict):
            if val.get("available"):
                out.append(str(name))
        elif val:
            out.append(str(name))
    return out


def _stringify(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _sanitize_instance_label(hostname: str) -> str:
    """Replace DNS-illegal chars in an mDNS instance label."""
    safe: list[str] = []
    for ch in hostname.strip():
        if ch.isalnum() or ch in ("-", "_"):
            safe.append(ch)
        else:
            safe.append("-")
    result = "".join(safe).strip("-._") or "nvmon-node"
    return result[:63]  # DNS label limit


def _instance_label(full_name: str) -> str:
    """'node-02._nvmon._tcp.local.' -> 'node-02'."""
    if "." not in full_name:
        return full_name
    return full_name.split(".", 1)[0]


def _first_ipv4(info: "ServiceInfo") -> str:
    """Return the first IPv4 address the announcer advertised."""
    try:
        addrs = info.parsed_addresses() if callable(getattr(info, "parsed_addresses", None)) else list(getattr(info, "parsed_addresses", []) or [])
    except Exception:
        addrs = []
    for a in addrs:
        try:
            parsed = ipaddress.ip_address(a)
        except ValueError:
            continue
        if isinstance(parsed, ipaddress.IPv4Address):
            return str(parsed)
    # Fallback: try raw addresses (bytes).
    try:
        raw = getattr(info, "addresses", None) or []
        for b in raw:
            if isinstance(b, (bytes, bytearray)) and len(b) == 4:
                return str(ipaddress.IPv4Address(bytes(b)))
    except Exception:
        pass
    return ""


def _node_from_info(info: "ServiceInfo") -> DiscoveredNode | None:
    """Build a DiscoveredNode from a zeroconf ServiceInfo."""
    txt = _decode_txt(info)
    # Prefer the self-reported hostname from the TXT record. zeroconf
    # appends a `-2`/`-3`/... suffix to the instance_name when it sees a
    # cache collision (typical when a listener bounces faster than its
    # old TTL). The TXT hostname is the listener's own truth and is
    # immune to that rename. Fall back to the parsed instance label for
    # pre-v0.6.14 announcements OR for the v0.6.14-bug case where the
    # TXT contained the literal string "None" (str(None) when the
    # caller didn't pass hostname=). Treat None/none/empty as missing.
    txt_host = (txt.get("hostname") or "").strip()
    if txt_host.lower() in ("none", ""):
        txt_host = ""
    hostname = txt_host or _instance_label(info.name)
    ip = _first_ipv4(info)
    port = int(info.port or 0)
    if not hostname or port == 0:
        return None

    caps_raw = txt.get("caps", "") or ""
    caps = [c for c in (p.strip() for p in caps_raw.split(",")) if c]

    # Strip well-known fields so extra only has the overflow.
    known = {"role", "version", "caps", "machine_id", "hostname", "started", "path"}
    extra = {k: v for k, v in txt.items() if k not in known}

    return DiscoveredNode(
        hostname=hostname,
        ip=ip,
        port=port,
        role=txt.get("role", "agent") or "agent",
        caps=caps,
        machine_id=txt.get("machine_id", "") or "",
        version=txt.get("version", "") or "",
        last_seen=datetime.now(timezone.utc),
        alive=True,
        source="mdns",
        extra=extra,
    )


def _node_from_static(raw: dict[str, Any]) -> DiscoveredNode | None:
    """Accept a ``[[controller.nodes]]`` TOML row and normalize it.

    The A-spec row shape is ``{hostname, url}``. We parse ``url`` for
    ip+port. Unparseable rows are skipped.
    """
    if not isinstance(raw, dict):
        return None
    hostname = str(raw.get("hostname") or "").strip()
    url = str(raw.get("url") or "").strip()
    ip = ""
    port = 0
    if url:
        # Accept "http://1.2.3.4:9998", "1.2.3.4:9998", or "host:port".
        stripped = url.replace("http://", "").replace("https://", "")
        if "/" in stripped:
            stripped = stripped.split("/", 1)[0]
        if ":" in stripped:
            ip_part, port_part = stripped.rsplit(":", 1)
            ip = ip_part
            try:
                port = int(port_part)
            except ValueError:
                port = 0
        else:
            ip = stripped
    # Allow explicit ip/port fields to override the parsed URL.
    ip = str(raw.get("ip") or ip).strip()
    try:
        port = int(raw.get("port") or port)
    except (TypeError, ValueError):
        pass
    role = str(raw.get("role") or "agent").strip() or "agent"
    if not hostname and not ip:
        return None
    if port == 0:
        # Fall back to the default agent port so the controller can still
        # probe; operator will see 0 in the dashboard and fix their TOML.
        try:
            from . import config as _config
            port = _config.DEFAULT_AGENT_PORT
        except Exception:
            port = 9998
    if not hostname:
        hostname = ip
    return DiscoveredNode(
        hostname=hostname,
        ip=ip,
        port=port,
        role=role,
        caps=list(raw.get("caps") or []),
        machine_id=str(raw.get("machine_id") or "").strip(),
        version=str(raw.get("version") or "").strip(),
        alive=True,
        source="static",
    )


__all__ = [
    "SERVICE_TYPE",
    "DEFAULT_TTL_SECONDS",
    "EVENT_APPEARED",
    "EVENT_UPDATED",
    "EVENT_GOODBYE",
    "EVENT_TTL_EXPIRED",
    "DiscoveredNode",
    "Announcer",
    "DiscoveryRegistry",
    "announce",
    "merge_with_static",
    "get_machine_id",
]
