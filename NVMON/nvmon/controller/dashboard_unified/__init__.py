"""
nvmon.controller.dashboard_unified
==================================

Unified real-time dashboard for the nvmon controller.

Architecture
------------
- Templates rendered with Jinja2 (no build step).
- Vanilla JS + HTMX 2.x + Alpine.js 3.x + Chart.js 4.x — all vendored.
- SSE (`/stream`) for live node/alert/heartbeat updates.
- Slide-over drill-down — no separate route, keeps grid context visible.

Public surface
--------------
``DashboardViewModel``  — stateful builder (sparkline buffers, dismissed alerts).
``render_dashboard(ctx)``         — main HTML
``render_node_detail(node)``      — slide-over partial
``render_fanout_modal(...)``      — confirm/progress modal partial
``render_login(error, next_path)`` — login form
``serve_static(rel_path)``        — (mime, bytes) for /static/* requests
``stream_sse_event(name, data)``  — SSE wire-format helper

The controller's ``ControllerServer`` imports these and dispatches HTTP
requests to them; there is no framework abstraction.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

__all__ = [
    "TEMPLATES_DIR",
    "STATIC_DIR",
    "FIXTURES_DIR",
    "DashboardViewModel",
    "render_dashboard",
    "render_node_detail",
    "render_login",
    "render_fanout_modal",
    "serve_static",
    "stream_sse_event",
]

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR    = HERE / "static"
FIXTURES_DIR  = HERE / "fixtures"


# Imports below intentionally deferred until after the module-level
# constants are bound (render.py reads TEMPLATES_DIR).

from .view_model import DashboardViewModel  # noqa: E402
from .render import (  # noqa: E402
    render_dashboard,
    render_node_detail,
    render_login,
    render_fanout_modal,
)


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

# Pre-register MIME types Python's stdlib doesn't always know.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")


def serve_static(rel_path: str) -> tuple[str, bytes] | None:
    """Resolve and read a static file under ``STATIC_DIR``.

    Returns ``(mime_type, body_bytes)`` or ``None`` if the path is outside
    the static dir, missing, or a directory.
    """
    if not rel_path:
        return None
    # Drop leading slashes so Path() doesn't interpret them as absolute.
    cleaned = rel_path.lstrip("/").replace("\\", "/")
    if ".." in cleaned.split("/"):
        return None
    target = (STATIC_DIR / cleaned).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    mime, _ = mimetypes.guess_type(str(target))
    if not mime:
        mime = "application/octet-stream"
    return mime, target.read_bytes()


# ---------------------------------------------------------------------------
# SSE wire helper
# ---------------------------------------------------------------------------

def stream_sse_event(event: str, payload: Any) -> bytes:
    """Format a single SSE event line. Caller writes the bytes to the wire."""
    import json as _json
    body = _json.dumps(payload, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")
