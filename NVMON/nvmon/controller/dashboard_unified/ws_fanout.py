"""
ws_fanout.py — WebSocket fan-out progress protocol for A3.

Life-cycle
----------

    ┌─ Operator clicks "Re-apply All" in the header ──┐
    │                                                   │
    │  POST /fanout/job                                 │
    │   ⇐  { "job_id": "...", "ws_url": "/fanout/...",  │
    │       "targets": [...], "skipped": [...] }        │
    │                                                   │
    │  Connect WebSocket(ws_url)                        │
    │                                                   │
    │    ← {"type":"progress", "hostname":"RN-01",      │
    │       "state":"running"}                          │
    │    ← {"type":"progress", "hostname":"RN-01",      │
    │       "state":"done", "duration_ms": 142}         │
    │    ← {"type":"progress", "hostname":"RN-04",      │
    │       "state":"skipped", "reason":"offline"}      │
    │    ← {"type":"summary", "succeeded":6,            │
    │       "failed":1, "skipped":1, "failures":[...]}  │
    │                                                   │
    │  Server closes socket.                            │
    └───────────────────────────────────────────────────┘

State vocabulary
----------------
- ``pending``   queued, not yet dispatched
- ``running``   request in flight to the agent
- ``done``      agent returned 202/200 with success
- ``failed``    agent returned non-success or raised
- ``skipped``   excluded from dispatch (offline, missing capability)

Server-to-client frames
-----------------------
All frames are JSON text. Two ``type`` values:

    progress  — one emission per node per state transition.
                Fields: type, hostname, state, state_label?, duration_ms?,
                        http_code?, reason?, error?

    summary   — exactly one emission at the end of the job.
                Fields: type, succeeded, failed, skipped,
                        failures: [{hostname, error, http_code?}]
                After this frame, server closes the socket.

Client-to-server frames
-----------------------
    {"action": "cancel"}
        Stop dispatching any further pending nodes.
        Commands already accepted (202) by an agent keep running;
        the UI must make this explicit (see spec §12.7).

    ping / pong
        Optional heartbeat; server echoes pong if it receives ping.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Literal, Optional


ProgressState = Literal["pending", "running", "done", "failed", "skipped"]


# ---------------------------------------------------------------------------
# Frame dataclasses — A3 may use these directly or re-implement. The shape
# is the contract; the class is convenience.
# ---------------------------------------------------------------------------

@dataclass
class ProgressFrame:
    hostname: str
    state: ProgressState
    state_label: Optional[str] = None
    duration_ms: Optional[int] = None
    http_code: Optional[int] = None
    reason: Optional[str] = None
    error: Optional[str] = None

    def as_wire(self) -> dict:
        out: dict[str, Any] = {
            "type": "progress",
            "hostname": self.hostname,
            "state": self.state,
        }
        if self.state_label: out["state_label"] = self.state_label
        if self.duration_ms is not None: out["duration_ms"] = self.duration_ms
        if self.http_code   is not None: out["http_code"]   = self.http_code
        if self.reason:      out["reason"] = self.reason
        if self.error:       out["error"]  = self.error
        return out


@dataclass
class SummaryFrame:
    succeeded: int
    failed: int
    skipped: int
    failures: list[dict] = field(default_factory=list)

    def as_wire(self) -> dict:
        return {
            "type": "summary",
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "failures": self.failures,
        }


# ---------------------------------------------------------------------------
# Handler contract — pseudocode of what A3 must implement.
# ---------------------------------------------------------------------------

def fanout_ws_handler(ws: Any, fanout_executor: Any, job_id: str) -> None:
    """Pseudocode for the `/fanout/job/<id>/ws` WebSocket handler.

    A3 replaces this body with the real framework-specific version
    (aiohttp ``WebSocketResponse``, ``websockets`` server, etc.).

    Parameters
    ----------
    ws : Any
        Framework WebSocket handle. Must expose ``send(str)``, ``receive()``,
        and ``close()`` (either sync or async).
    fanout_executor : Any
        Object returned by ``FanoutExecutor``. Must expose::

            def job_stream(job_id: str) -> Iterator[ProgressFrame | SummaryFrame]:
                ...
            def cancel(job_id: str) -> None:
                ...

    Protocol summary
    ----------------
    1. Iterate ``fanout_executor.job_stream(job_id)`` and forward each
       frame as JSON text.
    2. Concurrently read inbound messages; if the client sends
       ``{"action": "cancel"}``, call ``fanout_executor.cancel(job_id)``
       — but continue forwarding remaining completions for in-flight
       requests.
    3. After a ``summary`` frame is forwarded, close the socket (code 1000).

    Concurrency
    -----------
    Two coroutines / threads:
      - reader:   await ws.receive() → dispatch 'cancel' / 'ping'
      - writer:   for frame in executor.job_stream(): ws.send(json.dumps(...))

    Shared state: the ``cancel`` flag is owned by ``fanout_executor``.
    """
    # --- Example loop (blocking pseudocode — replace per framework) -------

    def forward():
        for frame in fanout_executor.job_stream(job_id):
            wire = frame.as_wire() if hasattr(frame, "as_wire") else frame
            ws.send(json.dumps(wire, separators=(",", ":")))
            if wire.get("type") == "summary":
                return

    def read():
        while True:
            msg = ws.receive()
            if msg is None:
                return
            try:
                parsed = json.loads(msg)
            except Exception:
                continue
            if parsed.get("action") == "cancel":
                fanout_executor.cancel(job_id)
            elif parsed.get("action") == "ping":
                ws.send(json.dumps({"type": "pong", "t": time.time()}))

    # A3: run `read` and `forward` concurrently. When forward() completes,
    # close the socket. When read() returns (client disconnected), stop
    # dispatching and let forward drain.
    raise NotImplementedError(
        "fanout_ws_handler is a contract stub — A3's controller wires this "
        "with its chosen WebSocket framework. See docstring for the protocol."
    )


# ---------------------------------------------------------------------------
# Helpers usable by both A3 and tests.
# ---------------------------------------------------------------------------

def encode_frame(frame: ProgressFrame | SummaryFrame) -> str:
    return json.dumps(frame.as_wire(), separators=(",", ":"))


def parse_client_frame(raw: str | bytes) -> dict:
    """Parse an inbound client frame. Returns ``{}`` on malformed input."""
    try:
        return json.loads(raw)
    except Exception:
        return {}
