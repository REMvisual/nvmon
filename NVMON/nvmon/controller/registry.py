"""Node registry — in-memory list of known agents.

Sources, in precedence order on hostname collision:

    "config"     — `[[controller.nodes]]` in nvmon.toml. Operator-declared,
                   never overridden by the other two.
    "registered" — push-registered by the listener via POST /register.
                   Persisted to `secrets/registered_nodes.json` so a
                   controller restart still sees them.
    "discovery"  — discovered via mDNS. Best-effort, never persisted.

Each entry is mutated in place by the `Aggregator` as it polls:
    last_seen     — epoch seconds of last successful contact
    last_health   — last `/health` payload (dict) or None
    reachable     — bool (last poll succeeded)
    capabilities  — shortcut: last_health["capabilities"] if present
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import log


@dataclass
class NodeEntry:
    """A single known agent."""
    hostname: str
    url: str
    last_seen: float = 0.0               # epoch seconds; 0 == never
    last_health: dict[str, Any] | None = None
    reachable: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    # Free-form: "config" (nvmon.toml), "registered" (POST /register, persisted
    # in secrets/registered_nodes.json), or "discovery" (mDNS).
    source: str = "config"
    # Bookkeeping for "registered" entries. Set by merge_registered; ignored
    # for other sources.
    machine_id: str = ""
    version: str = ""
    last_register: float = 0.0
    # Last poll outcome — populated by Aggregator. Lets the dashboard
    # answer "WHY is this node offline?" without making the operator
    # tail logs. Stays empty on a successful poll.
    last_poll_error: str = ""
    last_poll_attempt: float = 0.0       # epoch seconds of the last attempt
    # True for [controller].node_ips entries until the agent's /health has
    # told us the real hostname (the entry is then renamed in place).
    pending_hostname: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of this node for HTTP responses."""
        out: dict[str, Any] = {
            "hostname": self.hostname,
            "url": self.url,
            "source": self.source,
            "reachable": self.reachable,
            "last_seen": self.last_seen,
            "last_poll_attempt": self.last_poll_attempt,
            "last_poll_error": self.last_poll_error,
            "capabilities": self.capabilities,
            "health": self.last_health,
        }
        if self.source == "registered":
            out["machine_id"] = self.machine_id
            out["version"] = self.version
            out["last_register"] = self.last_register
        return out


class NodeRegistry:
    """Thread-safe registry of nvmon agents.

    Loaded from `nvmon.toml` on construction. Exposes read accessors plus
    an `update_health()` hook for the aggregator. Hostname is the unique
    key — duplicate entries in config are deduplicated (last wins) and a
    warning is logged.
    """

    def __init__(self, nodes_cfg: list[dict[str, Any]], groups_cfg: list[dict[str, Any]]) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, NodeEntry] = {}
        self._groups: dict[str, list[str]] = {}
        self._load_from_config(nodes_cfg, groups_cfg)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_from_config(
        self,
        nodes_cfg: list[dict[str, Any]],
        groups_cfg: list[dict[str, Any]],
    ) -> None:
        logger = log.get()

        for n in nodes_cfg or []:
            hostname = str(n.get("hostname", "") or "").strip()
            url = str(n.get("url", "") or "").strip().rstrip("/")
            if not hostname or not url:
                logger.warning(
                    "[registry] skipping node with missing hostname or url: %r",
                    n,
                )
                continue
            if hostname in self._nodes:
                logger.warning(
                    "[registry] duplicate node hostname %r in config; last wins",
                    hostname,
                )
            self._nodes[hostname] = NodeEntry(
                hostname=hostname,
                url=url,
                source="config",
                pending_hostname=bool(n.get("pending_hostname", False)),
            )

        for g in groups_cfg or []:
            name = str(g.get("name", "") or "").strip()
            if not name:
                logger.warning("[registry] skipping group with missing name: %r", g)
                continue
            members = [str(x).strip() for x in (g.get("nodes") or []) if str(x).strip()]
            # Warn about dangling references but keep them — hostnames may
            # be filled in later by discovery.
            for m in members:
                if m not in self._nodes:
                    logger.warning(
                        "[registry] group %r references unknown node %r",
                        name,
                        m,
                    )
            self._groups[name] = members

        logger.info(
            "[registry] loaded %d node(s), %d group(s) from config",
            len(self._nodes),
            len(self._groups),
        )

    def _find_existing_ci(self, hostname: str) -> NodeEntry | None:
        """Case-insensitive collision lookup. Caller must hold ``_lock``.

        Hostnames in `[[controller.nodes]]`, in `/register` payloads, and
        in mDNS announces all originate from places that routinely
        disagree on casing — toml is hand-typed; the agent reports
        `socket.gethostname()` (Windows usually returns the OS-recorded
        all-caps form); mDNS is whatever the announcer picked. A naive
        `dict.get(hostname)` collision check left ``"Node-02"`` (toml)
        and ``"NODE-02"`` (POST /register) as two separate dashboard
        cards. Match case-insensitively so config wins regardless of
        how the operator typed it.
        """
        target = hostname.casefold()
        for h, entry in self._nodes.items():
            if h.casefold() == target:
                return entry
        return None

    # ------------------------------------------------------------------
    # Discovery integration (sub-project C)
    # ------------------------------------------------------------------

    def remove_discovered(self, hostname: str) -> bool:
        """Drop a discovery-sourced entry. Refuses to touch config entries.

        Returns True if a discovery entry was removed, False otherwise (entry
        absent or sourced from config). Called by the controller in response
        to mDNS goodbye / ttl_expired events so dead nodes vanish instead of
        sticking around as permanently-OFFLINE cards.
        """
        with self._lock:
            existing = self._nodes.get(hostname)
            if existing is None:
                return False
            if existing.source != "discovery":
                # Operator-declared in nvmon.toml — never auto-evict.
                return False
            del self._nodes[hostname]
            log.get().info(
                "[registry] evicted discovery node %s (source=discovery)",
                hostname,
            )
            return True

    def merge_discovered(self, discovered: list[dict[str, Any]]) -> None:
        """Merge auto-discovered nodes into the registry.

        Rule: config entries win on hostname collision — operator-declared
        nodes are never overridden by mDNS. Discovered nodes that match a
        config hostname are dropped (with a debug log). Hostnames new to
        this registry are added with `source="discovery"`.

        `discovered` entries must have at least `hostname` and `url`.
        Extra keys are ignored.
        """
        logger = log.get()
        added = 0
        skipped = 0
        with self._lock:
            for d in discovered or []:
                hostname = str(d.get("hostname", "") or "").strip()
                url = str(d.get("url", "") or "").strip().rstrip("/")
                if not hostname or not url:
                    continue
                existing = self._find_existing_ci(hostname)
                if existing is not None and existing.source == "config":
                    skipped += 1
                    continue
                if existing is not None and existing.source == "registered":
                    # mDNS is weaker than an explicit /register POST —
                    # don't downgrade source or clobber the registered URL.
                    continue
                if existing is not None and existing.source == "discovery":
                    # Update URL (IP may have changed); keep other fields
                    existing.url = url
                    continue
                self._nodes[hostname] = NodeEntry(
                    hostname=hostname,
                    url=url,
                    source="discovery",
                )
                added += 1
        if added or skipped:
            logger.info(
                "[registry] discovery merge: +%d added, %d skipped (config wins)",
                added, skipped,
            )

    # ------------------------------------------------------------------
    # Push-registration integration
    # ------------------------------------------------------------------

    def merge_registered(self, entries: list[dict[str, Any]]) -> tuple[int, int, int]:
        """Merge push-registered nodes into the registry.

        Hostname collision rules:
            existing source == "config"     -> skip (operator wins)
            existing source == "registered" -> update url/version/last_register
            existing source == "discovery"  -> upgrade to "registered"
            new hostname                    -> add as "registered"

        `entries` shape: [{hostname, url, machine_id?, version?,
                           last_register?}, ...]. Extra keys ignored.

        Returns ``(added, updated, skipped)``.
        """
        logger = log.get()
        added = 0
        updated = 0
        skipped = 0
        with self._lock:
            for e in entries or []:
                hostname = str(e.get("hostname", "") or "").strip()
                url = str(e.get("url", "") or "").strip().rstrip("/")
                if not hostname or not url:
                    continue
                machine_id = str(e.get("machine_id", "") or "").strip()
                version = str(e.get("version", "") or "").strip()
                try:
                    last_register = float(e.get("last_register", 0.0) or 0.0)
                except (TypeError, ValueError):
                    last_register = 0.0
                if last_register <= 0.0:
                    last_register = time.time()

                existing = self._find_existing_ci(hostname)
                if existing is not None and existing.source == "config":
                    skipped += 1
                    continue
                if existing is not None:
                    existing.url = url
                    existing.source = "registered"
                    if machine_id:
                        existing.machine_id = machine_id
                    if version:
                        existing.version = version
                    existing.last_register = last_register
                    updated += 1
                    continue
                self._nodes[hostname] = NodeEntry(
                    hostname=hostname,
                    url=url,
                    source="registered",
                    machine_id=machine_id,
                    version=version,
                    last_register=last_register,
                )
                added += 1
        if added or updated or skipped:
            logger.info(
                "[registry] register merge: +%d added, %d updated, %d skipped (config wins)",
                added, updated, skipped,
            )
        return added, updated, skipped

    def remove_registered(self, hostname: str) -> bool:
        """Drop a registered entry. Refuses to touch config / discovery sources.

        Returns True if a registered entry was removed, False otherwise.
        """
        with self._lock:
            existing = self._nodes.get(hostname)
            if existing is None:
                return False
            if existing.source != "registered":
                return False
            del self._nodes[hostname]
            log.get().info("[registry] removed registered node %s", hostname)
            return True

    def get_registered(self) -> list[NodeEntry]:
        """Return all entries currently sourced from push-registration."""
        with self._lock:
            return [n for n in self._nodes.values() if n.source == "registered"]

    def evict_stale_registered(self, ttl_seconds: float) -> list[str]:
        """Drop registered entries whose last_register is older than ttl_seconds.

        Only touches `source == "registered"` entries — config and discovery
        are left alone (the latter has its own mDNS-driven eviction). Returns
        the list of hostnames evicted so the caller can persist + log.

        ``ttl_seconds <= 0`` disables eviction (returns []).
        """
        if ttl_seconds <= 0:
            return []
        cutoff = time.time() - ttl_seconds
        evicted: list[str] = []
        with self._lock:
            for hostname, node in list(self._nodes.items()):
                if node.source != "registered":
                    continue
                if node.last_register and node.last_register >= cutoff:
                    continue
                del self._nodes[hostname]
                evicted.append(hostname)
        return evicted

    def load_registered_from_file(self, path: Path | str) -> int:
        """Load registered_nodes.json from disk and merge into registry.

        Robust to missing file / malformed JSON: logs a warning and returns 0
        so a corrupted file never blocks controller boot. Returns the number
        of entries successfully merged (added + updated).
        """
        p = Path(path)
        if not p.is_file():
            return 0
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log.get().warning(
                "[registry] failed to load %s: %s — continuing without it",
                p, exc,
            )
            return 0
        if isinstance(data, dict):
            entries = data.get("nodes") or []
        elif isinstance(data, list):
            entries = data
        else:
            entries = []
        if not isinstance(entries, list):
            entries = []
        added, updated, _skipped = self.merge_registered(entries)
        return added + updated

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def get_all(self) -> list[NodeEntry]:
        """Return a stable-ordered list of all known nodes."""
        with self._lock:
            return sorted(self._nodes.values(), key=lambda n: n.hostname)

    def get_by_hostname(self, hostname: str) -> NodeEntry | None:
        with self._lock:
            return self._nodes.get(hostname)

    def get_by_group(self, name: str) -> list[NodeEntry]:
        """Return nodes belonging to a group. Unknown members are skipped."""
        with self._lock:
            members = self._groups.get(name, [])
            return [self._nodes[m] for m in members if m in self._nodes]

    def group_names(self) -> list[str]:
        """Group names in nvmon.toml declaration order.

        Was alphabetical, which rendered site's tabs as Ceiling, Wall — the
        operator declares them top-to-bottom / in rack order and expects
        the dashboard to follow.
        """
        with self._lock:
            return list(self._groups.keys())

    def count(self) -> int:
        with self._lock:
            return len(self._nodes)

    # ------------------------------------------------------------------
    # Write hooks (used by Aggregator)
    # ------------------------------------------------------------------

    def resolve_pending_hostname(self, key: str, real_hostname: str) -> str:
        """Rename an IP-list entry to the hostname its agent reported.

        Returns the key the entry now lives under. No-op when the entry
        isn't pending or the name is unchanged. If another entry already
        exists under ``real_hostname`` (a push-registration or an mDNS
        discovery that arrived first), that weaker entry is dropped — the
        operator-declared IP wins, same rule as [[controller.nodes]].
        """
        real = (real_hostname or "").strip()
        with self._lock:
            node = self._nodes.get(key)
            if node is None or not node.pending_hostname or not real:
                return key
            if real == key:
                node.pending_hostname = False
                return key
            existing = self._find_existing_ci(real)
            if existing is not None and existing is not node:
                if existing.source == "config" and not existing.pending_hostname:
                    # An explicit [[controller.nodes]] block already names
                    # this host — keep that one, drop the IP duplicate.
                    log.get().warning(
                        "[registry] node_ips entry %s is the same host as config node %s — "
                        "dropping the duplicate (remove one from nvmon.toml)",
                        key, existing.hostname,
                    )
                    del self._nodes[key]
                    return existing.hostname
                log.get().info(
                    "[registry] %s entry %s superseded by node_ips entry %s",
                    existing.source, existing.hostname, key,
                )
                del self._nodes[existing.hostname]
            del self._nodes[key]
            node.hostname = real
            node.pending_hostname = False
            self._nodes[real] = node
            for members in self._groups.values():
                for i, m in enumerate(members):
                    if m == key:
                        members[i] = real
            log.get().info("[registry] node_ips entry %s resolved to hostname %s", key, real)
            return real

    def update_health(
        self,
        hostname: str,
        *,
        reachable: bool,
        health: dict[str, Any] | None,
        error: str = "",
    ) -> None:
        """Record the outcome of a `/health` poll for a node.

        ``error`` is the human-readable reason the poll failed (e.g.
        "Connection refused", "Timed out after 5s"); empty on success.
        """
        with self._lock:
            node = self._nodes.get(hostname)
            if node is None:
                return
            node.reachable = reachable
            node.last_health = health
            node.last_poll_attempt = time.time()
            node.last_poll_error = "" if reachable else error
            if health is not None:
                caps = health.get("capabilities") if isinstance(health, dict) else None
                node.capabilities = caps if isinstance(caps, dict) else {}
            if reachable:
                node.last_seen = time.time()
