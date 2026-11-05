"""Aggregator — background polling of every known agent.

Runs a small thread pool that hits each node's `/health` and `/status`
endpoints in parallel on a fixed interval. The HTTP server reads the
cached snapshot and never blocks on a slow node.

Threaded model: we use `concurrent.futures.ThreadPoolExecutor` because
the workload is pure urllib I/O and the rest of the controller HTTP
server is thread-based (`BaseHTTPServer`). An asyncio loop would be
overkill and would require its own thread anyway — keep one model.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .. import log
from .registry import NodeEntry, NodeRegistry


# How many GPU samples we keep per (host, gpu, metric). 720 = 1 hour at
# the default 5-second poll. Tunable later if we need 6h/24h windows.
HISTORY_SAMPLES = 720


class Aggregator:
    """Periodically polls every registered agent and caches the results."""

    def __init__(
        self,
        registry: NodeRegistry,
        *,
        poll_interval_seconds: int = 5,
        fanout_timeout_seconds: int = 15,
        max_workers: int = 16,
    ) -> None:
        self._registry = registry
        self._poll_interval = max(1, int(poll_interval_seconds))
        self._timeout = max(1, int(fanout_timeout_seconds))
        self._max_workers = max(2, int(max_workers))

        self._lock = threading.RLock()
        self._node_status: dict[str, dict[str, Any] | None] = {}
        self._last_update: float = 0.0

        # GPU telemetry ring buffer per (host, gpu_index, metric). Each
        # entry is a deque of (epoch_seconds, value). Powers /history
        # responses; without this the slide-over charts stayed empty.
        self._history: dict[tuple[str, int, str], deque[tuple[float, float]]] = {}

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background poll thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="nvmon-aggregator",
            daemon=True,
        )
        self._thread.start()
        log.get().info(
            "[aggregator] started — interval=%ds timeout=%ds nodes=%d",
            self._poll_interval,
            self._timeout,
            self._registry.count(),
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the background thread to stop and wait briefly."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        log.get().info("[aggregator] stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        # First tick immediately so the dashboard has data on the first request.
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — defensive
                log.get().exception("[aggregator] poll cycle failed: %s", exc)
            # Interruptible sleep.
            self._stop.wait(self._poll_interval)

    def poll_once(self) -> None:
        """Poll every registered node in parallel. Called by `_run()`."""
        nodes = self._registry.get_all()
        if not nodes:
            with self._lock:
                self._last_update = time.time()
            return

        workers = min(self._max_workers, max(2, len(nodes)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nvmon-poll") as ex:
            futures = {ex.submit(self._poll_node, n): n for n in nodes}
            for fut in as_completed(futures):
                node = futures[fut]
                error = ""
                try:
                    health, status, error = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.get().debug(
                        "[aggregator] poll future for %s raised: %s",
                        node.hostname,
                        exc,
                    )
                    health, status = None, None
                    error = f"poll worker crashed: {exc}"

                key = node.hostname
                # IP-list entry: adopt the hostname the agent reports so the
                # card, groups, apply_excluded_hosts and per-node routes all
                # use the real name from the first successful poll on.
                if node.pending_hostname and isinstance(health, dict):
                    reported = str(health.get("hostname", "") or "").strip()
                    if reported:
                        new_key = self._registry.resolve_pending_hostname(key, reported)
                        if new_key != key:
                            with self._lock:
                                self._node_status.pop(key, None)
                                for hk in [k for k in self._history if k[0] == key]:
                                    self._history[(new_key, hk[1], hk[2])] = self._history.pop(hk)
                            key = new_key

                reachable = health is not None
                self._registry.update_health(
                    key,
                    reachable=reachable,
                    health=health,
                    error=error,
                )
                with self._lock:
                    self._node_status[key] = status
                    self._record_history(key, status)

        with self._lock:
            self._last_update = time.time()

    def _record_history(self, hostname: str, status: dict[str, Any] | None) -> None:
        """Append the current GPU samples to the per-metric ring buffer.
        Called under self._lock from poll_once. Cheap enough to do every
        tick — 720 samples * a few floats per (host, gpu, metric)."""
        if not status:
            return
        now = time.time()
        gpus = status.get("gpu") or []
        if not isinstance(gpus, list):
            return
        for g in gpus:
            if not isinstance(g, dict):
                continue
            try:
                idx = int(g.get("index") or 0)
            except (TypeError, ValueError):
                continue
            for metric in ("temperature_c", "power_draw_w", "gpu_utilization_pct"):
                v = g.get(metric)
                if v is None:
                    continue
                try:
                    value = float(v)
                except (TypeError, ValueError):
                    continue
                key = (hostname, idx, metric)
                buf = self._history.get(key)
                if buf is None:
                    buf = deque(maxlen=HISTORY_SAMPLES)
                    self._history[key] = buf
                buf.append((now, value))

    # ------------------------------------------------------------------
    # Per-node work
    # ------------------------------------------------------------------

    def _poll_node(
        self, node: NodeEntry,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """Fetch `/health` and `/status` from a single node.

        Returns ``(health, status, error)``. ``health``/``status`` may be
        None on failure. ``error`` is the human-readable failure reason
        when ``health`` is None — used to populate the dashboard slide-over
        so the operator can see WHY a node is OFFLINE.
        """
        health, error = _http_get_json(
            _join(node.url, "/health"),
            timeout=self._timeout,
        )
        if health is None:
            return None, None, error

        status, _ = _http_get_json(
            _join(node.url, "/status"),
            timeout=self._timeout,
        )
        return health, status, ""

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the aggregated state."""
        with self._lock:
            last_update = self._last_update
            status_by_host = dict(self._node_status)

        nodes_out: list[dict[str, Any]] = []
        for node in self._registry.get_all():
            entry = node.to_public_dict()
            entry["status"] = status_by_host.get(node.hostname)
            nodes_out.append(entry)

        return {
            "nodes": nodes_out,
            "last_update": last_update,
            "node_count": len(nodes_out),
        }

    def history(
        self, hostname: str, *, metric: str = "temperature_c",
        window_seconds: int = 3600, gpu_index: int = 0,
    ) -> list[dict[str, Any]]:
        """Return up to ``window_seconds`` worth of (t, v) samples for one
        (hostname, gpu_index, metric). Used by the controller's /history
        endpoint to feed the slide-over Chart.js plots."""
        from datetime import datetime, timezone
        cutoff = time.time() - max(60, int(window_seconds))
        with self._lock:
            buf = self._history.get((hostname, int(gpu_index), str(metric)))
            samples = list(buf) if buf else []
        out: list[dict[str, Any]] = []
        for ts, v in samples:
            if ts < cutoff:
                continue
            out.append({
                "t": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "v": v,
            })
        return out

    def node_snapshot(self, hostname: str) -> dict[str, Any] | None:
        """Return the cached state for a single node, or None if unknown."""
        node = self._registry.get_by_hostname(hostname)
        if node is None:
            return None
        with self._lock:
            status = self._node_status.get(hostname)
        entry = node.to_public_dict()
        entry["status"] = status
        return entry


# ---------------------------------------------------------------------------
# HTTP helpers (module-private — mirrors nvdd/peer_client.py pattern)
# ---------------------------------------------------------------------------

def _join(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _http_get_json(
    url: str, *, timeout: float,
) -> tuple[dict[str, Any] | None, str]:
    """GET a URL, parse as JSON. Returns ``(parsed_or_None, error_string)``.

    On success ``error_string`` is empty. On failure it carries a short
    human-readable reason ("Connection refused", "Timed out", "DNS failure",
    etc.) which the dashboard renders in the OFFLINE node slide-over.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed, ""
        return {"_raw": parsed}, ""
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code} {exc.reason}"
        log.get().debug("[aggregator] GET %s failed: %s", url, msg)
        return None, msg
    except urllib.error.URLError as exc:
        # urllib wraps the underlying socket error in `reason`.
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            msg = f"timed out after {int(timeout)}s"
        else:
            # Strip the typical "<urlopen error [WinError 10061]>" wrapper to
            # leave just the human bit (e.g. "Connection refused").
            text = str(reason)
            for prefix in ("[WinError 10061]", "[WinError 10060]", "[WinError 10065]"):
                text = text.replace(prefix, "").strip()
            msg = text or "unreachable"
        log.get().debug("[aggregator] GET %s failed: %s", url, msg)
        return None, msg
    except TimeoutError:
        msg = f"timed out after {int(timeout)}s"
        log.get().debug("[aggregator] GET %s timed out", url)
        return None, msg
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"non-JSON response: {exc}"
        log.get().debug("[aggregator] GET %s returned non-JSON: %s", url, exc)
        return None, msg
    except Exception as exc:  # noqa: BLE001
        log.get().debug("[aggregator] GET %s unexpected error: %s", url, exc)
        return None, f"unexpected: {exc}"
