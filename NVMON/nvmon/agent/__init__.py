"""nvmon agent — per-machine daemon that polls NVIDIA CLI tools and serves
status + control endpoints over HTTP.

Module layout mirrors the subsystems exposed by the agent:

    gpu              — nvidia-smi queries
    displays         — configureDdisplay.exe (Direct Display) queries + control
    mosaic           — configureMosaic.exe queries + control
    qsync            — configureQsync2.exe queries + control
    edid             — Windows registry EDID overrides
    profiles         — JSON profile CRUD + apply pipeline
    validator        — validate live state against a profile
    monitor          — background poll loop, writes status.json, fires alerts
    server           — HTTP daemon (default :9998)
    dashboard_single — single-host HTML dashboard (debug only)
    main             — `nvmon agent` subcommand entrypoint

Every subsystem module exposes a `status()` function that returns a typed
state object even if the underlying exe is missing — callers can always rely
on the return type being present. Whether the subsystem is actually usable is
advertised through `/health.capabilities` on the HTTP surface.
"""

from __future__ import annotations

__all__ = [
    "gpu",
    "displays",
    "mosaic",
    "qsync",
    "edid",
    "profiles",
    "validator",
    "monitor",
    "server",
    "dashboard_single",
    "main",
]
