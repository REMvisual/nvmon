"""nvmon controller — aggregates multiple agents into one fleet view.

HTTP on port 9997. Reads a static node list from `nvmon.toml`
(`[[controller.nodes]]`), polls each agent's `/health` and `/status` in
parallel, and exposes fan-out control endpoints (`/all/apply`,
`/all/qsync/enable`, ...).

Modules:
    auth         — optional shared-password gate (cookie + header)
    registry     — in-memory node list (later merged with C's discovery)
    aggregator   — background poller, caches per-node health + status
    fanout       — parallel fan-out of control actions with per-node results
    server       — HTTP daemon wiring the above together
    main         — entrypoint (also bootable via `python -m nvmon controller`)
    dashboard_placeholder — minimal landing HTML until D's unified dashboard
"""

from __future__ import annotations

__all__ = [
    "auth",
    "registry",
    "aggregator",
    "fanout",
    "server",
    "main",
    "dashboard_placeholder",
]
