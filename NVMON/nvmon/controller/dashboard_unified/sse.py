"""
sse.py — SSE (`/stream`) event generator contract for A3.

A3's controller owns the actual HTTP plumbing (chunked response, keep-alive,
proxy-buffering headers). This module documents the *payload* contract: what
events the dashboard expects and what each one's data looks like.

Usage pattern (pseudocode on the controller side)::

    def handle_stream(request):
        yield ("Content-Type: text/event-stream\\n"
               "Cache-Control: no-cache\\n"
               "X-Accel-Buffering: no\\n"
               "\\n")
        for event_name, payload in stream(aggregator):
            yield f"event: {event_name}\\n"
            yield f"data: {json.dumps(payload)}\\n\\n"

One browser = one `/stream` connection. The heartbeat event doubles as
keepalive; emit at least one message every 10 s or proxies will drop the
connection.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Iterator, Literal, Optional


# ---------------------------------------------------------------------------
# Event catalogue — the *only* events the dashboard listens to.
# ---------------------------------------------------------------------------

EVENT_NODE_UPDATE          = "node_update"
EVENT_NODE_REACHABLE_CHANGE = "node_reachable_change"
EVENT_ALERT                = "alert"
EVENT_HEARTBEAT            = "heartbeat"

EVENT_NAMES = (
    EVENT_NODE_UPDATE,
    EVENT_NODE_REACHABLE_CHANGE,
    EVENT_ALERT,
    EVENT_HEARTBEAT,
)


@dataclass
class NodeUpdate:
    """Aggregate refresh for a single node; emitted once per poll cycle per node."""
    hostname: str
    subsystems: dict       # {"dd": {"status":..., "level":"ok|warn|crit"}, ...}
    gpu: list[dict]        # [{"index":0, "temperature_c":..., "power_draw_w":..., "sparkline":[...]}, ...]
    refresh_hz: float
    displays_count: int
    alerts_count: int
    last_poll_ms_ago: int
    active_profile: Optional[str] = None
    timestamp: str = ""    # ISO8601
    status_age_s: float = 0.0   # v0.8.0: age of the agent's snapshot
    job_active: bool = False    # v0.8.0: apply/restore owns the NVIDIA tools
    color_text: str = ""        # v0.9.3: "10-bit Ycc422" (wire format)

    def as_payload(self) -> dict:
        return {
            "hostname": self.hostname,
            "subsystems": self.subsystems,
            "gpu": self.gpu,
            "refresh_hz": self.refresh_hz,
            "displays_count": self.displays_count,
            "color_text": self.color_text,
            "alerts_count": self.alerts_count,
            "last_poll_ms_ago": self.last_poll_ms_ago,
            "active_profile": self.active_profile,
            "timestamp": self.timestamp,
            "status_age_s": self.status_age_s,
            "job_active": self.job_active,
        }


@dataclass
class NodeReachableChange:
    """Emitted only on online↔offline transitions (not on every poll)."""
    hostname: str
    reachable: bool
    last_seen: Optional[str] = None

    def as_payload(self) -> dict:
        return {
            "hostname": self.hostname,
            "reachable": self.reachable,
            "last_seen": self.last_seen,
        }


@dataclass
class AlertEvent:
    """Emitted once per new alert firing; dashboard prepends to banner + drawer."""
    id: str
    hostname: str
    severity: Literal["CRITICAL", "WARNING", "INFO"]
    type: str
    message: str
    timestamp: str

    def as_payload(self) -> dict:
        return {
            "id": self.id,
            "hostname": self.hostname,
            "severity": self.severity,
            "type": self.type,
            "message": self.message,
            "timestamp": self.timestamp,
        }


@dataclass
class Heartbeat:
    """Emitted every ~10 s; keepalive + header-counter refresh."""
    controller_uptime_s: int
    nodes_online: int
    nodes_offline: int

    def as_payload(self) -> dict:
        return {
            "controller_uptime_s": self.controller_uptime_s,
            "nodes_online": self.nodes_online,
            "nodes_offline": self.nodes_offline,
        }


# ---------------------------------------------------------------------------
# Generator contract — synchronous for simplicity; swap to async on the
# controller side if A3 uses aiohttp/starlette. Shape is unchanged either way.
# ---------------------------------------------------------------------------

def stream(controller_state: Any) -> Iterator[tuple[str, dict]]:
    """Yield (event_name, payload) tuples for SSE framing.

    Expected to be called once per connected client. A3 wraps each yielded
    tuple in the SSE wire format (``event:`` + ``data:`` + blank line).

    Parameters
    ----------
    controller_state : Any
        A handle to the controller's aggregator (or equivalent). Must expose:

          * ``subscribe() -> Iterator``
              Iterator of model events (NodeUpdate | NodeReachableChange |
              AlertEvent). Implementation is up to A3 — a ``queue.Queue``
              fed by the aggregator's poll loop is the simplest fit.
          * ``snapshot() -> dict``
              Current aggregate — used to emit an initial burst so a
              late-joining client has correct state before new deltas arrive.

    Notes
    -----
    - Emit an initial ``heartbeat`` immediately on connect, so the browser's
      stream-state indicator flips to ``live`` within ~200 ms.
    - Emit one ``node_update`` per known online node as the opening burst,
      so a reconnecting client re-syncs without a page reload.
    - Thereafter, drain events from ``subscribe()``. If the queue is empty
      for ≥ 10 s, emit a ``heartbeat``.
    - On controller shutdown, break the loop and close the response.
    """
    # --- Opening burst -----------------------------------------------------
    snap = controller_state.snapshot()
    yield EVENT_HEARTBEAT, Heartbeat(
        controller_uptime_s=snap.get("controller", {}).get("uptime_seconds", 0),
        nodes_online=snap.get("controller", {}).get("nodes_online", 0),
        nodes_offline=snap.get("controller", {}).get("nodes_offline", 0),
    ).as_payload()

    for node in snap.get("nodes", []):
        if not node.get("online"):
            continue
        yield EVENT_NODE_UPDATE, NodeUpdate(
            hostname=node["hostname"],
            subsystems=node.get("subsystems", {}),
            gpu=node.get("gpu", []),
            refresh_hz=node.get("refresh_hz", 0.0),
            displays_count=node.get("displays_count", 0),
            alerts_count=node.get("alerts_count", 0),
            last_poll_ms_ago=node.get("last_poll_ms_ago", 0),
            active_profile=node.get("active_profile"),
            status_age_s=float(node.get("status_age_s") or 0.0),
            job_active=bool(node.get("job_active")),
            color_text=str(node.get("color_text") or ""),
        ).as_payload()

    # --- Steady-state drain ------------------------------------------------
    last_hb = time.monotonic()
    for event in controller_state.subscribe():
        if isinstance(event, NodeUpdate):
            yield EVENT_NODE_UPDATE, event.as_payload()
        elif isinstance(event, NodeReachableChange):
            yield EVENT_NODE_REACHABLE_CHANGE, event.as_payload()
        elif isinstance(event, AlertEvent):
            yield EVENT_ALERT, event.as_payload()
        elif isinstance(event, Heartbeat):
            yield EVENT_HEARTBEAT, event.as_payload()
            last_hb = time.monotonic()

        # Emit keepalive heartbeat if the stream has been quiet too long.
        if time.monotonic() - last_hb > 10.0:
            snap = controller_state.snapshot()
            yield EVENT_HEARTBEAT, Heartbeat(
                controller_uptime_s=snap.get("controller", {}).get("uptime_seconds", 0),
                nodes_online=snap.get("controller", {}).get("nodes_online", 0),
                nodes_offline=snap.get("controller", {}).get("nodes_offline", 0),
            ).as_payload()
            last_hb = time.monotonic()


# ---------------------------------------------------------------------------
# Wire helper — pure function, easy to unit-test.
# ---------------------------------------------------------------------------

def format_sse(event: str, payload: dict, retry_ms: int | None = None) -> bytes:
    """Format a single SSE event. Returned bytes are suitable to write to
    the socket directly.

    >>> format_sse("heartbeat", {"nodes_online": 7})
    b'event: heartbeat\\ndata: {"nodes_online": 7}\\n\\n'
    """
    parts: list[str] = []
    if retry_ms is not None:
        parts.append(f"retry: {retry_ms}")
    parts.append(f"event: {event}")
    parts.append(f"data: {json.dumps(payload, separators=(',', ':'))}")
    parts.append("")  # trailing blank line per SSE spec
    parts.append("")
    return "\n".join(parts).encode("utf-8")
