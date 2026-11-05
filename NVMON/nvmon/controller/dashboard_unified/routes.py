"""
routes.py — canonical list of HTTP/WS routes required from A3's controller.

This module declares *what* the dashboard needs. A3 owns the *how*
(HTTP framework, auth middleware, dispatch). Each entry documents:

    METHOD  PATH                 PURPOSE
    REQUEST BODY / QUERY PARAMS
    RESPONSE SHAPE
    AUTH    cookie | none
    NOTES

The intent is that A3 reads this file (or imports ROUTES) and creates
one handler per entry, wiring each to the Aggregator / FanoutExecutor
passed to ``mount_routes``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

Method = Literal["GET", "POST", "DELETE"]


@dataclass(frozen=True)
class Route:
    method: Method
    path: str
    purpose: str
    auth: Literal["cookie", "none"] = "cookie"
    request: str = ""
    response: str = ""
    notes: str = ""
    content_type: str = "text/html; charset=utf-8"


ROUTES: Sequence[Route] = (

    Route(
        method="GET",
        path="/",
        purpose="Unified dashboard — grid + tabs + alert banner + global action bar.",
        request="(none)",
        response="Rendered templates/dashboard.html with context: "
                 "{controller, groups, nodes, alerts, profiles_union}.",
        notes="If the password cookie is missing, redirect to /login?next=/.",
    ),

    Route(
        method="GET",
        path="/login",
        purpose="Render the one-field password form.",
        auth="none",
        request="query: ?next=<path> (optional)",
        response="Rendered templates/login.html",
        notes="If the password file is missing/empty, the controller runs with "
              "no auth and this route 302-redirects to /.",
    ),

    Route(
        method="POST",
        path="/login",
        purpose="Check password, set non-expiring cookie, redirect to ?next or /.",
        auth="none",
        request="form: password=<str>, next=<str>",
        response="302 to next on success; 401 re-rendering login.html with error on fail.",
        notes="Set cookie HttpOnly; SameSite=Lax; Secure only if request scheme is https.",
        content_type="text/html; charset=utf-8",
    ),

    Route(
        method="POST",
        path="/logout",
        purpose="Clear cookie and redirect to /login.",
        auth="cookie",
        response="302 to /login.",
    ),

    Route(
        method="GET",
        path="/stream",
        purpose="SSE event stream — node updates, alerts, heartbeats.",
        response="text/event-stream — see sse.py for event catalogue.",
        notes="One long-lived connection per browser tab. Auth via cookie. "
              "Set X-Accel-Buffering: no to prevent proxy buffering.",
        content_type="text/event-stream",
    ),

    Route(
        method="GET",
        path="/node/<hostname>/detail",
        purpose="HTMX partial — slide-over drill-down panel for a single node.",
        request="path: hostname=<str>",
        response="Rendered templates/node_detail.html with node dict context.",
        notes="Call aggregator.snapshot() and pull out the one node. "
              "404 if hostname unknown.",
    ),

    Route(
        method="GET",
        path="/history/<hostname>",
        purpose="Time-series history for detail-view charts.",
        request="path: hostname=<str> · query: metric=<str>, window=<10m|1h|6h|24h>",
        response='application/json — {"hostname":..., "metric":..., '
                 '"window_seconds":..., "samples":[{"t":"ISO8601","v":number}]}',
        notes="Defaults: metric=temperature_c, window=1h. "
              "Pulls from the controller's in-memory ring buffer; falls back to "
              "SQLite if configured (see design spec §8).",
        content_type="application/json",
    ),

    Route(
        method="POST",
        path="/fanout/modal",
        purpose="HTMX partial — render the fan-out confirm/progress/summary modal.",
        request='query: action=apply|sync|restore · '
                'body: {"scope":[host,...], "skipped":[{hostname,reason}], '
                '"profiles":[str,...]}',
        response="Rendered templates/fanout_modal.html with action/scope/skipped/profiles.",
        notes="Purely presentational. The client starts the job via /fanout/job.",
    ),

    Route(
        method="POST",
        path="/fanout/job",
        purpose="Create a fan-out job; return {job_id, ws_url}.",
        request='body: {"action":"apply"|"sync"|"restore", "scope":[str,...], '
                '"profile":<str|null>}',
        response='application/json — {"job_id":"...", "ws_url":"/fanout/job/<id>/ws", '
                 '"targets":[...], "skipped":[...]}',
        notes="Calls FanoutExecutor.create_job(...) and returns the id. "
              "The executor starts dispatching to agents immediately; the WS is where "
              "per-node progress streams. 202 Accepted is a reasonable status.",
        content_type="application/json",
    ),

    Route(
        method="GET",
        path="/fanout/job/<job_id>/ws",
        purpose="WebSocket — stream per-node progress messages for this job.",
        request="Upgrade to WebSocket.",
        response="WS frames — see ws_fanout.py for protocol.",
        notes="Short-lived. Server closes after the 'summary' message. "
              "Client may send {\"action\":\"cancel\"} to stop dispatch.",
        content_type="application/json",
    ),

    Route(
        method="DELETE",
        path="/fanout/job/<job_id>",
        purpose="Cancel remaining dispatch for a job.",
        response='application/json — {"cancelled":true, "in_flight":N, "pending":M}',
        notes="Commands already 202-accepted by agents keep running. Surface that "
              "distinction in the UI copy (see spec §12.7).",
        content_type="application/json",
    ),

    Route(
        method="POST",
        path="/alerts/<alert_id>/dismiss",
        purpose="Mark an alert dismissed; return HTMX-swappable empty fragment.",
        request="path: alert_id=<str>",
        response="200 empty HTML (HTMX outerHTML swap on the banner).",
        notes="A fresh occurrence of the same (hostname, type) un-dismisses. "
              "Dismissed alerts stay in the drawer at /alerts as 'dismissed'.",
    ),
)


def route_count() -> int:
    """Return the number of routes this module asks A3 to register."""
    return len(ROUTES)


def summary() -> str:
    """Human-readable table — useful for logging at mount time."""
    lines = ["nvmon dashboard routes required:"]
    for r in ROUTES:
        lines.append(f"  {r.method:6} {r.path:40} · auth={r.auth:6} · {r.purpose}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
    print(f"\nTotal: {route_count()} routes")
