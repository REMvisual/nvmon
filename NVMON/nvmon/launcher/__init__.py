"""nvmon launcher — generic process-launch daemon.

HTTP on port 9996. Launches, kills, supervises, and auto-restarts
apps declared in `nvmon.toml` under `[[launcher.apps]]`.

Per spec 2026-04-17-E-listener-v2-design.md:
    - Apps are opaque to the launcher; it only launches what is configured.
    - Optional pre-launch hooks call the local nvmon agent on 127.0.0.1:<agent_port>
      to apply profiles, enable QSync, wait for status, etc. Opt-in, per-app.
    - Per-app supervisor polls Popen.poll() every 1s. Crashes route to
      auto-restart with backoff; sustained crashes route to `parked`.
    - No shell expansion, absolute exe_path only, no arbitrary launch paths.

Modules:
    apps       — AppConfig dataclass + TOML loader
    hooks      — HookConfig + 13 hook types + run_hook()
    state      — AppState enum + AppRuntime dataclass
    supervisor — per-app supervisor (spawn, poll, restart)
    logs       — per-app log rotation + tail reader
    server     — HTTP daemon (port 9996)
    main       — entrypoint (also bootable via `python -m nvmon launcher`)
"""

from __future__ import annotations

# Intentionally do NOT eagerly import submodules here — `python -m
# nvmon.launcher.main` would otherwise re-initialise `main` and emit a
# RuntimeWarning about it appearing in sys.modules pre-execution.
# Consumers import submodules directly.

__all__ = [
    "apps",
    "hooks",
    "logs",
    "main",
    "server",
    "state",
    "supervisor",
]
