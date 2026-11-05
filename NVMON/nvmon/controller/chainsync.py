"""Chain sync — the controller starts each frame-lock daisy chain in order.

Why (site 2026-11-04): after a whole-rig cold boot and again after a warm
restart, every node's boot-apply enabled QSync within the same ~30 s while
all sync cards in both chains were still coming up. The wall clients never
locked (4 retries over 5 min) and the ceiling server ended in "house sync
present, no GPU became SERVER". Restarting node 5 alone, with its clients
already up, locked in 3 s. A chain has to be started server first, then
clients, after every member is up — only the controller sees the whole chain.

Scope (operator requirement): this ONLY applies to a group that actually is
a chain — exactly one member whose active profile has QSync enabled with
role ``server`` and at least one member with role ``client``. Anything else
(standalone nodes, sync off, two servers, no clients) is not a chain and
keeps the agent's own boot sync.

How:
- Roles are learned from each node's active profile (``GET /profile?name=``
  on the agent), cached per (host, profile) and persisted to
  ``secrets/sync_chains.json`` so a controller restart still knows the
  chains while the nodes are booting.
- An agent in a chain asks ``GET /sync_chain?host=<name>`` at boot (open
  endpoint, read-only). If it is in a chain it applies timing + colour,
  skips QSync and reports ``chain_sync.pending`` in its /status.
- This module's thread runs every ``interval`` s. For a chain with a pending
  member, once every member is online and idle: enable the server, wait for
  its lock, then enable the clients and wait for theirs. Up to
  ``max_runs`` attempts, ``min_gap`` s apart, per pending episode. The
  agent falls back to its own sync if nothing locks it within 8 minutes.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import log

SERVER_LOCK_TIMEOUT_S = 90.0
CLIENT_LOCK_TIMEOUT_S = 90.0
POLL_EVERY_S = 3.0


def _http_json(url: str, *, method: str = "GET", body: dict | None = None, timeout: float = 15.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def is_locked(qsync: dict | None, role: str) -> bool:
    """Same criterion as the agent's apply: synced, or (client) a GPU says synced."""
    if not isinstance(qsync, dict):
        return False
    if qsync.get("synced"):
        return True
    if role == "client":
        return any(isinstance(g, dict) and g.get("synced") for g in (qsync.get("per_gpu_sync") or []))
    return False


def find_chains(groups: dict[str, list[str]], roles: dict[str, str]) -> dict[str, dict[str, Any]]:
    """``{group: {"server": host, "clients": [hosts]}}`` for groups that are
    a chain: exactly one server-role member and at least one client-role
    member. Pure function (tests)."""
    out: dict[str, dict[str, Any]] = {}
    for group, members in groups.items():
        servers = [h for h in members if roles.get(h) == "server"]
        clients = [h for h in members if roles.get(h) == "client"]
        if len(servers) == 1 and clients:
            out[group] = {"group": group, "server": servers[0], "clients": sorted(clients)}
    return out


class ChainSync:
    def __init__(self, registry, aggregator, state_file: Path | str | None, *,
                 interval_s: float = 10.0, min_gap_s: float = 90.0, max_runs: int = 3) -> None:
        self._registry = registry
        self._aggregator = aggregator
        self._state_file = Path(state_file) if state_file else None
        self._interval = max(2.0, float(interval_s))
        self._min_gap = float(min_gap_s)
        self._max_runs = int(max_runs)
        self._lock = threading.RLock()
        self._roles: dict[str, dict[str, str]] = {}      # host -> {"profile", "role"}
        self._runs: dict[str, dict[str, Any]] = {}       # group -> {"count", "last", "running"}
        self._results: dict[str, dict[str, Any]] = {}    # group -> last result
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # --- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._state_file or not self._state_file.is_file():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8") or "{}")
            roles = data.get("roles") if isinstance(data, dict) else None
            if isinstance(roles, dict):
                self._roles = {str(h): dict(v) for h, v in roles.items() if isinstance(v, dict)}
        except Exception as exc:  # noqa: BLE001
            log.get().warning("[chainsync] could not read %s: %s", self._state_file, exc)

    def _save(self) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"roles": self._roles, "saved": time.time()}, indent=2), encoding="utf-8")
            tmp.replace(self._state_file)
        except Exception as exc:  # noqa: BLE001
            log.get().warning("[chainsync] could not write %s: %s", self._state_file, exc)

    # --- roles / chains ------------------------------------------------------

    def _groups(self) -> dict[str, list[str]]:
        return {g: [n.hostname for n in self._registry.get_by_group(g)] for g in self._registry.group_names()}

    def refresh_roles(self) -> None:
        snap = self._aggregator.snapshot() or {}
        changed = False
        for n in snap.get("nodes") or []:
            host = str(n.get("hostname") or "")
            st = n.get("status") if isinstance(n.get("status"), dict) else None
            prof = str((st or {}).get("active_profile") or "").strip()
            url = str(n.get("url") or "").rstrip("/")
            if not host or not prof or not url:
                continue
            with self._lock:
                known = self._roles.get(host)
            if known and known.get("profile") == prof:
                continue
            code, data = _http_json(f"{url}/profile?name={urllib.parse.quote(prof)}", timeout=8)
            if code != 200 or not isinstance(data, dict):
                continue
            q = data.get("qsync") or {}
            role = str(q.get("role") or "server").lower() if q.get("enabled") else "none"
            if role not in ("server", "client"):
                role = "none"
            with self._lock:
                self._roles[host] = {"profile": prof, "role": role}
            changed = True
        if changed:
            with self._lock:
                self._save()

    def chains(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            roles = {h: v.get("role", "none") for h, v in self._roles.items()}
        return find_chains(self._groups(), roles)

    def chain_for(self, host: str) -> dict[str, Any] | None:
        h = (host or "").strip().lower()
        for ch in self.chains().values():
            members = [ch["server"], *ch["clients"]]
            for m in members:
                if m.lower() == h:
                    return {**ch, "role": "server" if m == ch["server"] else "client", "host": m}
        return None

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {"chains": self.chains(), "roles": dict(self._roles),
                    "runs": {g: {k: v for k, v in r.items() if k != "running"} for g, r in self._runs.items()},
                    "results": dict(self._results)}

    # --- loop ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nvmon-chainsync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.get().warning("[chainsync] tick failed: %s", exc)

    def _node(self, host: str) -> dict[str, Any] | None:
        return self._aggregator.node_snapshot(host)

    def tick(self) -> None:
        self.refresh_roles()
        for group, ch in self.chains().items():
            members = [ch["server"], *ch["clients"]]
            snaps = {h: self._node(h) for h in members}
            statuses = {h: (s or {}).get("status") for h, s in snaps.items()}
            pending = [h for h, st in statuses.items()
                       if isinstance(st, dict) and (st.get("chain_sync") or {}).get("pending")]
            run = self._runs.setdefault(group, {"count": 0, "last": 0.0, "running": False})
            if not pending:
                run["count"] = 0
                continue
            if run["running"] or run["count"] >= self._max_runs or time.time() - run["last"] < self._min_gap:
                continue
            offline = [h for h, st in statuses.items() if not isinstance(st, dict)]
            if offline:
                continue                      # wait until every member is up
            if any((st or {}).get("job_active") for st in statuses.values()):
                continue
            busy = []
            for h in members:
                url = str((snaps.get(h) or {}).get("url") or "").rstrip("/")
                code, a = _http_json(f"{url}/apply/status", timeout=6)
                if code == 200 and isinstance(a, dict) and a.get("in_progress"):
                    busy.append(h)
            if busy:
                continue
            run.update(running=True, last=time.time(), count=run["count"] + 1)
            log.get().warning("[chainsync] %s: starting chain sync (run %d/%d) — server %s, clients %s; pending: %s",
                              group, run["count"], self._max_runs, ch["server"], ", ".join(ch["clients"]),
                              ", ".join(pending))
            threading.Thread(target=self._run_chain, args=(group, ch, snaps), daemon=True,
                             name=f"nvmon-chainsync-{group}").start()

    def _enable(self, url: str, profile: str) -> tuple[bool, str]:
        code, data = _http_json(f"{url}/qsync/enable_from_profile", method="POST",
                                body={"profile": profile, "chain_sync": True}, timeout=20)
        if code in (200, 202):
            return True, ""
        return False, f"HTTP {code} {(data or {}).get('error', '')}".strip()

    def _wait_locked(self, url: str, role: str, timeout_s: float) -> bool:
        end = time.time() + timeout_s
        while time.time() < end and not self._stop.is_set():
            time.sleep(POLL_EVERY_S)
            code, q = _http_json(f"{url}/qsync", timeout=15)
            if code == 200 and is_locked(q, role):
                return True
        return False

    def run_chain(self, group: str, ch: dict[str, Any], snaps: dict[str, Any]) -> dict[str, Any]:
        """Server, wait for lock, then clients, wait. Returns the result dict."""
        with self._lock:
            roles = dict(self._roles)
        url = {h: str((snaps.get(h) or {}).get("url") or "").rstrip("/") for h in snaps}
        prof = {h: (roles.get(h) or {}).get("profile", "") for h in snaps}
        t0 = time.time()
        res: dict[str, Any] = {"group": group, "started": t0, "server": ch["server"], "clients": {}, "ok": False}
        srv = ch["server"]
        ok, err = self._enable(url[srv], prof[srv])
        locked = ok and self._wait_locked(url[srv], "server", SERVER_LOCK_TIMEOUT_S)
        res["server_locked"] = bool(locked)
        if not locked:
            res["detail"] = (f"server {srv} did not lock{(' (' + err + ')') if err else ''} — if house sync is "
                             f"present and no GPU becomes SERVER, restart Windows on {srv}")
            log.get().error("[chainsync] %s: %s", group, res["detail"])
        else:
            log.get().info("[chainsync] %s: server %s locked after %.0fs — enabling clients", group, srv, time.time() - t0)

            def one(h: str) -> tuple[str, bool]:
                ok_c, _ = self._enable(url[h], prof[h])
                return h, bool(ok_c and self._wait_locked(url[h], "client", CLIENT_LOCK_TIMEOUT_S))

            with ThreadPoolExecutor(max_workers=max(1, len(ch["clients"]))) as ex:
                for h, ok_c in ex.map(one, ch["clients"]):
                    res["clients"][h] = ok_c
            bad = [h for h, v in res["clients"].items() if not v]
            res["ok"] = not bad
            res["detail"] = ("chain locked" if not bad else f"client(s) not locked: {', '.join(bad)}")
            (log.get().info if not bad else log.get().error)("[chainsync] %s: %s after %.0fs", group,
                                                             res["detail"], time.time() - t0)
        res["seconds"] = round(time.time() - t0, 1)
        with self._lock:
            self._results[group] = res
        return res

    def _run_chain(self, group: str, ch: dict[str, Any], snaps: dict[str, Any]) -> None:
        try:
            self.run_chain(group, ch, snaps)
        finally:
            self._runs.setdefault(group, {})["running"] = False
