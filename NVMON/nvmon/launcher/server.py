"""HTTP server for the nvmon launcher (port 9996).

Routes (spec §5):
    GET  /health                     — identity + capabilities
    GET  /apps                       — all configured apps + current state
    GET  /app/<name>/status          — one app's state
    GET  /app/<name>/logs?tail=N     — tail captured stdout (N <= 5000)
    POST /app/<name>/launch          — start (202 accepted)
    POST /app/<name>/kill            — graceful terminate
    POST /app/<name>/restart         — kill + relaunch
    POST /reload-config              — re-parse nvmon.toml
    GET  /                           — minimal debug HTML

Admin gating: when `admin_required_for_control=true` (default), every
POST returns 403 unless the launcher process is elevated. Non-elevated
launches of `elevated: true` apps return 409.
"""

from __future__ import annotations

import html
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import config as _config
from .. import __version__
from .apps import (
    AppConfigError,
    LauncherConfig,
    NAME_REGEX,
    load_launcher_config,
)
from .state import AppState
from .supervisor import AppSupervisor


# ---------------------------------------------------------------------------
# Registry — the server's in-memory set of supervisors, keyed by app name.
# ---------------------------------------------------------------------------

class SupervisorRegistry:
    def __init__(
        self, cfg: LauncherConfig, *, launcher_is_elevated: bool, logger: Any,
    ) -> None:
        self._lock = threading.Lock()
        self._cfg = cfg
        self._launcher_is_elevated = launcher_is_elevated
        self._logger = logger
        self._supervisors: dict[str, AppSupervisor] = {}
        for app in cfg.apps:
            sup = AppSupervisor(
                app,
                log_dir=cfg.log_dir,
                agent_port=cfg.agent_port,
                per_app_log_bytes=cfg.per_app_log_bytes,
                per_app_log_keep=cfg.per_app_log_keep,
                launcher_is_elevated=launcher_is_elevated,
                logger=logger,
            )
            sup.start_supervisor()
            self._supervisors[app.name] = sup

    @property
    def cfg(self) -> LauncherConfig:
        return self._cfg

    def names(self) -> list[str]:
        with self._lock:
            return list(self._supervisors.keys())

    def get(self, name: str) -> AppSupervisor | None:
        with self._lock:
            return self._supervisors.get(name)

    def reload(self, new_cfg: LauncherConfig) -> tuple[int, dict[str, Any]]:
        """Diff-based hot reload per spec §10.

        Returns (http_code, body). 409 if a running app would be removed.
        """
        with self._lock:
            old_names = set(self._supervisors.keys())
            new_by_name = {a.name: a for a in new_cfg.apps}
            new_names = set(new_by_name.keys())

            # Refuse if any running-app would be removed
            running_removed: list[str] = []
            for n in old_names - new_names:
                sup = self._supervisors[n]
                st = sup.runtime.state
                if st in (
                    AppState.RUNNING, AppState.STARTING,
                    AppState.PRE_LAUNCH, AppState.STOPPING,
                    AppState.RESTARTING,
                ):
                    running_removed.append(n)
            if running_removed:
                return 409, {
                    "error": "cannot remove running apps",
                    "offending": running_removed,
                }

            added: list[str] = []
            removed: list[str] = []
            modified: list[str] = []

            # Remove stopped/parked apps that vanished from config.
            for n in list(old_names - new_names):
                sup = self._supervisors.pop(n)
                sup.shutdown()
                removed.append(n)

            # Add new apps.
            for n in new_names - old_names:
                app = new_by_name[n]
                sup = AppSupervisor(
                    app,
                    log_dir=new_cfg.log_dir,
                    agent_port=new_cfg.agent_port,
                    per_app_log_bytes=new_cfg.per_app_log_bytes,
                    per_app_log_keep=new_cfg.per_app_log_keep,
                    launcher_is_elevated=self._launcher_is_elevated,
                    logger=self._logger,
                )
                sup.start_supervisor()
                self._supervisors[n] = sup
                added.append(n)

            # Modify existing apps.
            for n in old_names & new_names:
                old_app = self._supervisors[n].cfg
                new_app = new_by_name[n]
                if _apps_equivalent(old_app, new_app):
                    continue
                drift_fields = _drift_fields(old_app, new_app)
                sup = self._supervisors[n]
                # Hot-applicable: auto_restart, restart_backoff_seconds,
                # kill_grace_seconds.
                sup.cfg.auto_restart = new_app.auto_restart
                sup.cfg.restart_backoff_seconds = new_app.restart_backoff_seconds
                sup.cfg.kill_grace_seconds = new_app.kill_grace_seconds
                sup.cfg.crash_cooldown_seconds = new_app.crash_cooldown_seconds
                sup.cfg.expected_exit_codes = new_app.expected_exit_codes
                sup.cfg.pre_launch = new_app.pre_launch
                sup.cfg.pre_launch_on_auto_restart = new_app.pre_launch_on_auto_restart
                sup.cfg.elevated = new_app.elevated
                sup.cfg.kill_by_name = new_app.kill_by_name
                # Drift fields (exe_path, args, working_dir, env) are staged
                # but NOT applied until next launch.
                if drift_fields:
                    sup.runtime.config_drift = True
                else:
                    sup.runtime.config_drift = False
                # We do not apply drift fields live — they take effect on
                # the next launch. Record new values so they're visible
                # when we re-spawn.
                # (Assigning them now means the next _spawn uses new values.)
                sup.cfg.exe_path = new_app.exe_path
                sup.cfg.args = new_app.args
                sup.cfg.working_dir = new_app.working_dir
                sup.cfg.env = new_app.env
                modified.append(n)

            self._cfg = new_cfg
            return 200, {
                "status": "reloaded",
                "added": added,
                "removed": removed,
                "modified": modified,
            }

    def shutdown_all(self) -> None:
        with self._lock:
            for sup in self._supervisors.values():
                sup.shutdown()


def _apps_equivalent(a: Any, b: Any) -> bool:
    """Deep compare of every AppConfig field."""
    for f in (
        "name", "exe_path", "args", "env", "working_dir", "elevated",
        "kill_by_name", "auto_restart", "restart_backoff_seconds",
        "crash_cooldown_seconds", "expected_exit_codes", "kill_grace_seconds",
        "pre_launch_on_auto_restart",
    ):
        if getattr(a, f) != getattr(b, f):
            return False
    # pre_launch: compare serialized form
    if len(a.pre_launch) != len(b.pre_launch):
        return False
    for x, y in zip(a.pre_launch, b.pre_launch):
        if (x.type, x.timeout_seconds, x.required, x.name, x.on_fail,
                x.max_retries, x.params) != (
                y.type, y.timeout_seconds, y.required, y.name, y.on_fail,
                y.max_retries, y.params):
            return False
    return True


def _drift_fields(a: Any, b: Any) -> list[str]:
    """Return the list of next-launch-only fields that have changed."""
    drift = []
    for f in ("exe_path", "args", "env", "working_dir"):
        if getattr(a, f) != getattr(b, f):
            drift.append(f)
    return drift


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class LauncherHTTPServer(ThreadingHTTPServer):
    """Adds a reference to the SupervisorRegistry."""
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type["LauncherHandler"],
        *,
        registry: SupervisorRegistry,
        launcher_is_elevated: bool,
        logger: Any,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.registry = registry
        self.launcher_is_elevated = launcher_is_elevated
        self.nvmon_logger = logger


class LauncherHandler(BaseHTTPRequestHandler):
    """One handler per request. Thread-safe access via registry locks."""

    server_version = f"nvmon-launcher/{__version__}"

    # Silence BaseHTTPRequestHandler's default stderr logging; we use nvmon's.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        server: LauncherHTTPServer = self.server  # type: ignore[assignment]
        server.nvmon_logger.debug(
            f"[http] {self.address_string()} {format % args}"
        )

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        parts = [p for p in path.split("/") if p]

        if path == "/":
            self._serve_root_html()
            return
        if path == "/health":
            self._serve_health()
            return
        if path == "/apps":
            self._serve_apps()
            return
        if len(parts) >= 2 and parts[0] == "app":
            name = parts[1]
            sup = self._get_sup_or_404(name)
            if sup is None:
                return
            if len(parts) == 3 and parts[2] == "status":
                self._json(200, sup.status())
                return
            if len(parts) == 3 and parts[2] == "logs":
                qs = urllib.parse.parse_qs(query or "")
                tail_raw = qs.get("tail", ["200"])[0]
                try:
                    tail = int(tail_raw)
                except ValueError:
                    tail = 200
                body = sup.logs(tail)
                self._plain(200, body)
                return

        self._json(404, {"error": "not found", "path": path})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        server: LauncherHTTPServer = self.server  # type: ignore[assignment]
        registry = server.registry

        # Admin gate
        if (
            registry.cfg.admin_required_for_control
            and not server.launcher_is_elevated
        ):
            self._json(403, {
                "error": "admin required",
                "hint": "relaunch launcher elevated",
            })
            return

        path = self.path.partition("?")[0]
        parts = [p for p in path.split("/") if p]

        if path == "/reload-config":
            try:
                new_cfg = load_launcher_config(force_reload=True)
            except AppConfigError as e:
                self._json(400, {"error": f"config error: {e}"})
                return
            code, body = registry.reload(new_cfg)
            self._json(code, body)
            return

        if len(parts) == 3 and parts[0] == "app":
            name, action = parts[1], parts[2]
            sup = self._get_sup_or_404(name)
            if sup is None:
                return
            if action == "launch":
                code, body = sup.launch()
                self._json(code, body)
                return
            if action == "kill":
                code, body = sup.kill()
                self._json(code, body)
                return
            if action == "restart":
                code, body = sup.restart()
                self._json(code, body)
                return

        self._json(404, {"error": "not found", "path": path})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_sup_or_404(self, name: str) -> AppSupervisor | None:
        server: LauncherHTTPServer = self.server  # type: ignore[assignment]
        if not NAME_REGEX.match(name):
            self._json(400, {"error": "invalid app name", "name": name})
            return None
        sup = server.registry.get(name)
        if sup is None:
            self._json(404, {
                "error": "no such app",
                "known_apps": server.registry.names(),
            })
            return None
        return sup

    def _serve_health(self) -> None:
        server: LauncherHTTPServer = self.server  # type: ignore[assignment]
        cfg = server.registry.cfg
        body = {
            "ok": True,
            "version": __version__,
            "hostname": _config.get_hostname(),
            "ip": _config.get_local_ip(),
            "role": "launcher",
            "elevated": server.launcher_is_elevated,
            "capabilities": {
                "launcher": {
                    "available": True,
                    "apps_configured": server.registry.names(),
                    "admin_required_for_control":
                        cfg.admin_required_for_control,
                },
            },
        }
        self._json(200, body)

    def _serve_apps(self) -> None:
        server: LauncherHTTPServer = self.server  # type: ignore[assignment]
        names = server.registry.names()
        out = []
        for n in names:
            sup = server.registry.get(n)
            if sup is not None:
                out.append(sup.status())
        self._json(200, {"apps": out})

    def _serve_root_html(self) -> None:
        server: LauncherHTTPServer = self.server  # type: ignore[assignment]
        names = server.registry.names()
        rows = []
        for n in names:
            sup = server.registry.get(n)
            if sup is None:
                continue
            st = sup.status()
            state = html.escape(str(st.get("state", "?")))
            pid = html.escape(str(st.get("pid") or "-"))
            name_esc = html.escape(n)
            rows.append(
                f"<tr><td><code>{name_esc}</code></td>"
                f"<td>{state}</td><td>{pid}</td>"
                f"<td>"
                f"<form method='POST' action='/app/{name_esc}/launch' "
                f"style='display:inline'>"
                f"<button>launch</button></form> "
                f"<form method='POST' action='/app/{name_esc}/kill' "
                f"style='display:inline'>"
                f"<button>kill</button></form> "
                f"<form method='POST' action='/app/{name_esc}/restart' "
                f"style='display:inline'>"
                f"<button>restart</button></form> "
                f"<a href='/app/{name_esc}/logs?tail=200'>logs</a> "
                f"<a href='/app/{name_esc}/status'>status</a>"
                f"</td></tr>"
            )
        body = (
            "<!doctype html><meta charset=utf-8>"
            "<title>nvmon launcher</title>"
            "<style>body{font-family:system-ui;margin:2rem}"
            "table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:.4rem .8rem}</style>"
            f"<h1>nvmon launcher :{server.registry.cfg.port}</h1>"
            f"<p>host={html.escape(_config.get_hostname())} "
            f"elevated={server.launcher_is_elevated} "
            f"admin_required={server.registry.cfg.admin_required_for_control}"
            "</p>"
            "<table><thead><tr>"
            "<th>app</th><th>state</th><th>pid</th><th>actions</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
        self._html(200, body)

    def _json(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _plain(self, code: int, body: str) -> None:
        data = body.encode("utf-8", errors="replace")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _html(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# Server lifecycle helpers
# ---------------------------------------------------------------------------

def build_server(
    cfg: LauncherConfig, *, launcher_is_elevated: bool, logger: Any,
) -> tuple[LauncherHTTPServer, SupervisorRegistry]:
    registry = SupervisorRegistry(
        cfg, launcher_is_elevated=launcher_is_elevated, logger=logger,
    )
    httpd = LauncherHTTPServer(
        (cfg.bind_host, cfg.port),
        LauncherHandler,
        registry=registry,
        launcher_is_elevated=launcher_is_elevated,
        logger=logger,
    )
    return httpd, registry


__all__ = [
    "LauncherHTTPServer",
    "LauncherHandler",
    "SupervisorRegistry",
    "build_server",
]
