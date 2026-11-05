"""Fan-out executor — parallel control actions across agents.

Every public method returns a dict with the spec-mandated shape:

    {
      "requested": int,
      "succeeded": int,
      "failed":    int,
      "skipped":   int,
      "timed_out": int,
      "results": [
        {"hostname": str, "status": str, "http_code"?: int, "error"?: str, ...},
        ...
      ]
    }

**Every targeted node gets an entry in `results`** — no silent drops.

Statuses used in `results[*].status`:
    accepted   — remote returned 2xx
    failed     — remote returned non-2xx (`http_code` + `error`)
    timed_out  — socket / read timeout exceeded
    skipped    — didn't contact the node (capability missing or unreachable)
    unreachable— last known reachability was False AND contact attempt failed

Capability policy:
    When `required_capability` is set and the node's last /health shows
    `capabilities.<cap>.available == false`, we skip the POST entirely
    and record:  {status: "skipped", reason: "<cap> unavailable"}
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .. import config

from .. import log
from .registry import NodeEntry, NodeRegistry


# Capability keys used by subsystem actions. Keep in sync with agent /health.
CAP_DISPLAYS = "displays"
CAP_MOSAIC = "mosaic"
CAP_QSYNC = "qsync"
CAP_GPU = "gpu"
CAP_EDID = "edid"


# Reason string used for nodes filtered out of a power fan-out because they
# ARE the controller machine. Kept as a constant so tests / UI can match on it.
SKIP_REASON_SELF = "monitor (controller self) — refusing to power-cycle"


def _normalize_host(h: str) -> str:
    """Lower-case + strip the first DNS label so 'NODE1.local' == 'node1'."""
    return (h or "").strip().lower().split(".")[0]


def is_self_host(hostname: str) -> bool:
    """True iff `hostname` resolves to the controller's own machine.

    Used to refuse remote restart/shutdown of the monitor box from its
    own dashboard — losing the controller mid-operation would orphan the
    UI and confuse anyone trying to recover the fleet.
    """
    if not hostname:
        return False
    # Lazy import: nvmon.config has no circular dep on fanout, but importing
    # at module level pulls in TOML parsing on `from . import fanout` which
    # we want to keep cheap for tests that monkey-patch hostname.
    from .. import config as cfgmod
    try:
        own = cfgmod.get_hostname()
    except Exception:
        return False
    return _normalize_host(hostname) == _normalize_host(own)


class FanoutExecutor:
    """Runs parallel HTTP calls against a set of agents."""

    def __init__(
        self,
        registry: NodeRegistry,
        *,
        timeout_seconds: int = 15,
        max_workers: int = 16,
    ) -> None:
        self._registry = registry
        self._timeout = max(1, int(timeout_seconds))
        self._max_workers = max(2, int(max_workers))

    # ------------------------------------------------------------------
    # High-level actions
    # ------------------------------------------------------------------

    def apply_all(self, profile: str, nodes: list[NodeEntry] | None = None) -> dict[str, Any]:
        """Fan out `POST /apply {profile}` to every registered (or given) node.

        Apply touches all three subsystems; we don't skip on capability
        because an agent without mosaic can still apply a DD+QSync profile.
        The target agent enforces per-subsystem capability.
        """
        targets = self._registry.get_all() if nodes is None else nodes
        return self._fanout(
            targets,
            method="POST",
            path="/apply",
            body={"profile": profile},
        )

    def apply_group(self, group: str, profile: str) -> dict[str, Any]:
        """Fan out apply to nodes in a named group. Unknown group => empty result."""
        targets = self._registry.get_by_group(group)
        if not targets:
            log.get().warning("[fanout] apply_group: group %r has no members", group)
        return self._fanout(
            targets,
            method="POST",
            path="/apply",
            body={"profile": profile},
        )

    def restore_all(self) -> dict[str, Any]:
        targets = self._registry.get_all()
        return self._fanout(targets, method="POST", path="/restore", body={})

    def qsync_enable_all(
        self,
        *,
        source: str,
        trigger: str,
        grid: int,
    ) -> dict[str, Any]:
        targets = self._registry.get_all()
        # Only forward grid when the caller asked for an explicit Mosaic
        # grid (>= 0). Otherwise omit it so the agent's auto-discover
        # default fires — passing grid=1 to a non-Mosaic agent is what
        # made Sync silently no-op on the user's rig.
        body: dict[str, Any] = {"source": source, "trigger": trigger}
        if grid >= 0:
            body["grid"] = grid
        return self._fanout(
            targets,
            method="POST",
            path="/qsync/enable",
            body=body,
            required_capability=CAP_QSYNC,
        )

    def qsync_disable_all(self) -> dict[str, Any]:
        targets = self._registry.get_all()
        return self._fanout(
            targets,
            method="POST",
            path="/qsync/disable",
            body={},
            required_capability=CAP_QSYNC,
        )

    # ------------------------------------------------------------------
    # System power — every targeted node reboots/shuts-down. Two-step
    # operator confirmation lives in the dashboard JS; this layer just
    # fans out the POST.  No required_capability — the action is always
    # available on Windows so we let the agent answer 403/500 if it's
    # not admin or shutdown.exe rejects the call.
    #
    # Self-host guard: if the controller's own hostname appears in the
    # registry (common when controller + listener share a box), it is
    # filtered out and recorded as `skipped` with reason SKIP_REASON_SELF.
    # The dashboard cannot take down the machine it's running on — that
    # would orphan the UI mid-operation and confuse recovery. Use the
    # OS's own Start menu / `shutdown.exe` if you actually need to
    # restart the monitor box.
    # ------------------------------------------------------------------

    def _split_self(
        self, nodes: list[NodeEntry],
    ) -> tuple[list[NodeEntry], list[dict[str, Any]]]:
        """Partition `nodes` into (targets, self_skipped_results)."""
        targets: list[NodeEntry] = []
        self_skipped: list[dict[str, Any]] = []
        for n in nodes:
            if is_self_host(n.hostname):
                log.get().warning(
                    "[fanout] refusing power action on controller self-host %r",
                    n.hostname,
                )
                self_skipped.append({
                    "hostname": n.hostname,
                    "status": "skipped",
                    "reason": SKIP_REASON_SELF,
                })
            else:
                targets.append(n)
        return targets, self_skipped

    def _power_fanout(self, path: str) -> dict[str, Any]:
        nodes = self._registry.get_all()
        targets, self_skipped = self._split_self(nodes)
        if not targets:
            # Whole registry was self-host (single-node setup w/ controller +
            # listener on the same box). Return a clean tally with the skip.
            return _tally(self_skipped, requested=len(nodes))
        result = self._fanout(targets, method="POST", path=path, body={})
        if self_skipped:
            # Splice in the self-skipped entries and re-tally.
            merged = (result.get("results") or []) + self_skipped
            merged.sort(key=lambda r: r.get("hostname", ""))
            return _tally(merged, requested=len(nodes))
        return result

    def restart_all(self) -> dict[str, Any]:
        return self._power_fanout("/system/restart")

    def shutdown_all(self) -> dict[str, Any]:
        return self._power_fanout("/system/shutdown")

    def abort_all(self) -> dict[str, Any]:
        # Abort is harmless to run on self (cancels a queued shutdown) but we
        # still surface the self-host as skipped for symmetry with restart/shutdown
        # so the operator sees the same picture in the result toast.
        return self._power_fanout("/system/abort")

    def node_passthrough(
        self,
        hostname: str,
        *,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pass-through a single call to one node, wrapped in the standard result shape."""
        node = self._registry.get_by_hostname(hostname)
        if node is None:
            return {
                "requested": 1,
                "succeeded": 0,
                "failed": 1,
                "skipped": 0,
                "timed_out": 0,
                "results": [{
                    "hostname": hostname,
                    "status": "failed",
                    "error": "unknown hostname",
                }],
            }
        return self._fanout([node], method=method.upper(), path=path, body=body or {})

    # ------------------------------------------------------------------
    # Core fan-out engine
    # ------------------------------------------------------------------

    def _fanout(
        self,
        nodes: list[NodeEntry],
        *,
        method: str,
        path: str,
        body: dict[str, Any],
        required_capability: str | None = None,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        if not nodes:
            return _tally(results, requested=0)

        # Plan: which targets get a real POST, which get skipped.
        to_call: list[NodeEntry] = []
        for n in nodes:
            skip_reason = _should_skip(n, required_capability)
            if skip_reason is not None:
                results.append({
                    "hostname": n.hostname,
                    "status": "skipped",
                    "reason": skip_reason,
                })
            else:
                to_call.append(n)

        if to_call:
            workers = min(self._max_workers, max(2, len(to_call)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nvmon-fanout") as ex:
                futures = {
                    ex.submit(
                        self._call_one,
                        node,
                        method,
                        path,
                        body,
                    ): node
                    for node in to_call
                }
                for fut in as_completed(futures):
                    node = futures[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # noqa: BLE001 — defensive
                        log.get().exception(
                            "[fanout] worker for %s crashed: %s",
                            node.hostname,
                            exc,
                        )
                        results.append({
                            "hostname": node.hostname,
                            "status": "failed",
                            "error": f"worker crash: {exc}",
                        })

        # Stable ordering by hostname so responses are diff-friendly.
        results.sort(key=lambda r: r.get("hostname", ""))
        return _tally(results, requested=len(nodes))

    # ------------------------------------------------------------------
    # Per-node fan-out engine — different body per node
    # ------------------------------------------------------------------

    def _fanout_per_node(
        self,
        nodes: list[NodeEntry],
        *,
        method: str,
        path: str,
        body_for,  # Callable[[NodeEntry], dict | None]
        path_for=None,  # Optional Callable[[NodeEntry], str | None]; defaults to `path`
        skip_reason: str = "no active profile",
    ) -> dict[str, Any]:
        """Like ``_fanout`` but the request body (and optionally path) is
        chosen per-node by callback.

        Used by the dashboard's Sync and Re-apply All buttons: each node gets
        a body derived from its own ``status.active_profile``. A ``None``
        return from ``body_for`` (or ``path_for``) marks that node skipped
        with ``reason=skip_reason`` — same shape as the capability skip in
        ``_fanout``.
        """
        results: list[dict[str, Any]] = []
        if not nodes:
            return _tally(results, requested=0)

        to_call: list[tuple[NodeEntry, str, dict[str, Any]]] = []
        for n in nodes:
            this_path = path if path_for is None else path_for(n)
            this_body = body_for(n)
            if this_path is None or this_body is None:
                results.append({
                    "hostname": n.hostname,
                    "status": "skipped",
                    "reason": skip_reason,
                })
                continue
            to_call.append((n, this_path, this_body))

        if to_call:
            workers = min(self._max_workers, max(2, len(to_call)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nvmon-fanout") as ex:
                futures = {
                    ex.submit(
                        self._call_one,
                        node,
                        method,
                        npath,
                        nbody,
                    ): node
                    for (node, npath, nbody) in to_call
                }
                for fut in as_completed(futures):
                    node = futures[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # noqa: BLE001 — defensive
                        log.get().exception(
                            "[fanout] worker for %s crashed: %s",
                            node.hostname,
                            exc,
                        )
                        results.append({
                            "hostname": node.hostname,
                            "status": "failed",
                            "error": f"worker crash: {exc}",
                        })

        results.sort(key=lambda r: r.get("hostname", ""))
        return _tally(results, requested=len(nodes))

    # ------------------------------------------------------------------
    # Per-node HTTP
    # ------------------------------------------------------------------

    def _call_one(
        self,
        node: NodeEntry,
        method: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        url = _join(node.url, path)
        payload = json.dumps(body or {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        req = urllib.request.Request(url, data=payload, method=method, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                http_code = resp.getcode()
                raw = resp.read().decode("utf-8", errors="replace")
            return {
                "hostname": node.hostname,
                "status": "accepted",
                "http_code": http_code,
                "body": _try_parse_json(raw),
            }
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw = ""
            log.get().warning(
                "[fanout] %s %s -> HTTP %s (%s): %s",
                method,
                url,
                exc.code,
                exc.reason,
                raw[:200],
            )
            return {
                "hostname": node.hostname,
                "status": "failed",
                "http_code": exc.code,
                "error": str(exc.reason) if exc.reason else "http error",
                "body": _try_parse_json(raw),
            }
        except (TimeoutError, urllib.error.URLError) as exc:
            # Disambiguate timeout vs other URL errors.
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                log.get().warning("[fanout] %s %s timed out after %ss", method, url, self._timeout)
                return {
                    "hostname": node.hostname,
                    "status": "timed_out",
                    "error": f"timeout after {self._timeout}s",
                }
            # Not reachable at all.
            log.get().warning("[fanout] %s %s unreachable: %s", method, url, reason)
            return {
                "hostname": node.hostname,
                "status": "failed",
                "error": f"unreachable: {reason}",
            }
        except Exception as exc:  # noqa: BLE001 — defensive
            log.get().exception("[fanout] %s %s unexpected error: %s", method, url, exc)
            return {
                "hostname": node.hostname,
                "status": "failed",
                "error": f"unexpected: {exc}",
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mosaic_preflight(
    nodes: list[NodeEntry],
    profile_for,  # Callable[[NodeEntry], str]  -> profile name ('' = none)
    *,
    profiles_dir,
    timeout: float = 75.0,
    max_workers: int = 16,
    opener=None,
) -> tuple[list[NodeEntry], list[dict[str, Any]], list[dict[str, Any]]]:
    """Dry-run every target's Mosaic timing before blanking a wall (v0.8.0).

    Calls ``GET /mosaic/timings?profile=X`` on each node — the agent runs
    ``configureMosaic test``, nothing is applied — and drops nodes whose
    wall would come up at a different rate than the profile asks for
    (asked 30 → the driver picks 29.97; a QSync lock can then never
    happen, and the old flow burned minutes before reporting "applied").
    Nodes whose profile has Mosaic disabled, whose profile JSON isn't on
    the controller, whose agent predates the endpoint (404), or that can't
    be reached are passed through — the apply itself reports those.

    Returns ``(kept, skipped_results, report)``. ``opener`` is injectable
    for tests (``opener(url, timeout) -> dict``). (the legacy tool 365e2a6)
    """
    report: list[dict[str, Any]] = []
    if not nodes:
        return [], [], report

    def _wanted(name: str) -> tuple[float, float]:
        try:
            p = json.loads((profiles_dir / f"{name}.json").read_text(encoding="utf-8-sig"))
            mos = p.get("mosaic") or {}
            if not mos.get("enabled"):
                return 0.0, config.RATE_MATCH_TOLERANCE_HZ
            tol = ((p.get("validation") or {}).get("refresh_rate_tolerance"))
            # Same rule as the agent's apply-time check (config.rate_tolerance):
            # 23.976 does NOT satisfy "24" — the profile may only tighten.
            return float(mos.get("refresh_rate") or 0.0), config.rate_tolerance(tol)
        except Exception:  # noqa: BLE001 — profile not on the controller: can't preflight
            return 0.0, config.RATE_MATCH_TOLERANCE_HZ

    def _default_opener(url: str, t: float) -> dict:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=t) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")

    fetch = opener or _default_opener

    def _one(n: NodeEntry):
        name = profile_for(n) or ""
        if not name:
            return n, None
        wanted, tol = _wanted(name)
        if wanted <= 0:
            return n, None
        url = n.url.rstrip("/") + "/mosaic/timings?" + urllib.parse.urlencode({"profile": name})
        try:
            data = fetch(url, timeout)
        except Exception as exc:  # noqa: BLE001 — old agent / unreachable: let apply decide
            return n, {"hostname": n.hostname, "profile": name, "checked": False,
                       "reason": f"preflight unavailable: {exc}"[:200]}
        rate = data.get("rate_hz") if isinstance(data, dict) else None
        row: dict[str, Any] = {
            "hostname": n.hostname, "profile": name, "checked": True,
            "wanted_hz": wanted, "rate_hz": rate, "valid": bool(isinstance(data, dict) and data.get("valid")),
        }
        if isinstance(rate, (int, float)) and abs(float(rate) - wanted) > tol:
            row["skip"] = (
                f"preflight: wall would come up at {rate:g}Hz, profile wants {wanted:g}Hz "
                f"— add a custom timing in NVIDIA Control Panel or pick a native rate"
            )
        return n, row

    kept: list[NodeEntry] = []
    skipped: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(nodes))),
                            thread_name_prefix="nvmon-preflight") as ex:
        for n, row in ex.map(_one, nodes):
            if row is not None:
                report.append(row)
            if row and row.get("skip"):
                log.get().warning("[fanout] apply preflight skipping %s: %s", n.hostname, row["skip"])
                skipped.append({"hostname": n.hostname, "status": "skipped", "reason": row["skip"]})
            else:
                kept.append(n)
    return kept, skipped, report


def _should_skip(node: NodeEntry, required_capability: str | None) -> str | None:
    """Return a skip reason string, or None to proceed with the call."""
    if required_capability is None:
        return None
    # Use last-known capabilities from aggregator polling. If we've never
    # polled the node we don't skip — we let the HTTP call fail and be
    # reported properly.
    caps = node.capabilities or {}
    cap = caps.get(required_capability)
    if isinstance(cap, dict) and cap.get("available") is False:
        return f"{required_capability} unavailable"
    return None


def _tally(results: list[dict[str, Any]], *, requested: int) -> dict[str, Any]:
    counts = {"succeeded": 0, "failed": 0, "skipped": 0, "timed_out": 0}
    for r in results:
        status = r.get("status")
        if status == "accepted":
            counts["succeeded"] += 1
        elif status == "timed_out":
            counts["timed_out"] += 1
        elif status == "skipped":
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
    return {
        "requested": requested,
        "succeeded": counts["succeeded"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "timed_out": counts["timed_out"],
        "results": results,
    }


def _join(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _try_parse_json(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]  # cap the truncation on weird responses
