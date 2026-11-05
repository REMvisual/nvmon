"""Controller HTTP server (port 9997).

Routes — see `docs/API.md`:

    GET  /health                         controller identity + node count
    GET  /nodes                          known nodes + last-seen health
    GET  /status                         aggregated fleet status (cached)
    GET  /node/<hostname>/status         pass-through to one node (cached snapshot)
    GET  /launchers                      STUB — E's launcher registry
    GET  /apps                           STUB — E's app registry
    POST /all/apply {profile}            fan out /apply to every node
    POST /all/restore                    fan out /restore
    POST /all/qsync/enable {s,t,g}       fan out /qsync/enable (manual override)
    POST /all/qsync/disable              fan out /qsync/disable
    Sync button: per-node /qsync/enable_from_profile {profile=active_profile}
    Re-apply All: per-node /apply {profile=active_profile} (no body.profile)
    POST /all/restart                    fan out OS restart  (loud log line)
    POST /all/shutdown                   fan out OS shutdown (loud log line)
    POST /all/abort                      cancel queued shutdowns fleet-wide
    POST /group/<name>/apply {profile}   fan out /apply to a group
    POST /node/<hostname>/apply {...}    pass-through to one node
    POST /node/<hostname>/system/restart  per-node restart   (loud log line)
    POST /node/<hostname>/system/shutdown per-node shutdown  (loud log line)
    POST /node/<hostname>/system/abort    per-node abort
    POST /login                          set session cookie
    POST /logout                         clear session cookie
    GET  /                               minimal landing HTML (placeholder)

Auth: when enabled, every route except /health, GET /login, POST /login is
gated by `require_auth`.

# INTEGRATION_POINT: dashboard_unified
# Sub-project D replaces the `GET /` handler below with its unified
# dashboard blueprint. Mount D's renderer in _handle_root(); the JSON
# endpoints are stable and don't need to change.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from .. import config as cfgmod
from .. import log
from .. import util as _util
from . import auth as authmod
from . import dashboard_placeholder
from . import dashboard_unified as dash
from . import selfrestart
from .aggregator import Aggregator
from .fanout import FanoutExecutor
from .registry import NodeRegistry


def persist_registered(registry: NodeRegistry, registered_file: Any) -> None:
    """Write current 'registered' entries to disk. Best-effort.

    Module-level so both the request handler and the background sweep
    thread can call it. ``registered_file`` may be None — in that case
    the call is a no-op (used in tests / when persistence is disabled).
    """
    if registered_file is None:
        return
    from pathlib import Path as _Path
    try:
        entries = [
            {
                "hostname": n.hostname,
                "url": n.url,
                "machine_id": n.machine_id,
                "version": n.version,
                "last_register": n.last_register,
            }
            for n in registry.get_registered()
        ]
        payload = {"nodes": entries, "saved_at": time.time()}
        _util.atomic_write_json(_Path(registered_file), payload)
    except Exception as exc:  # noqa: BLE001 — defensive: callers must not fail
        log.get().warning(
            "[register] failed to persist %s: %s", registered_file, exc,
        )


# Matches /node/<hostname>/status etc. Hostnames: letters, digits, dash, dot, underscore.
_HOSTNAME_RE = r"[A-Za-z0-9._-]+"

# Set by `nvmon app` (desktop widget): fn(cmd, body) -> dict, for POST
# /app/<cmd> window controls. None on the monitor box (routes 404 there).
APP_HOOK = None


def _collect_profiles_union(aggregator: Aggregator) -> list[str]:
    """Best-effort: GET /profiles from each reachable agent, merge into a sorted list.

    Tolerates failures silently — a node we can't reach contributes nothing.
    Used to populate the fan-out modal's profile picker.
    """
    out: set[str] = set()
    snap = aggregator.snapshot() or {}
    for n in snap.get("nodes") or []:
        if not n.get("reachable"):
            continue
        url = (n.get("url") or "").rstrip("/")
        if not url:
            continue
        try:
            req = urllib.request.Request(url + "/profiles", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            names = data.get("profiles") if isinstance(data, dict) else None
            if isinstance(names, list):
                for name in names:
                    if isinstance(name, str) and name.strip():
                        out.add(name.strip())
        except Exception:
            # Silent — this list is best-effort UI sugar.
            continue
    return sorted(out)


def make_handler(
    *,
    registry: NodeRegistry,
    aggregator: Aggregator,
    fanout: FanoutExecutor,
    view_model: "dash.DashboardViewModel | None" = None,
    disc_registry: Any = None,
    registered_file: Any = None,
    chainsync: Any = None,
) -> type[BaseHTTPRequestHandler]:
    """Build a handler class closed over the controller components.

    `disc_registry` is the optional `nvmon.discovery.DiscoveryRegistry` —
    when provided, GET /discovery/raw returns the full mDNS cache so the
    operator can diagnose 'I have 10 listeners on but the dashboard only
    sees 6'. Includes nodes the controller's NodeRegistry deduped away.

    `registered_file` is an optional `pathlib.Path` to the JSON file
    backing push-registered nodes. When set, POST /register persists
    every accepted registration so a controller restart re-loads them.
    """

    def _persist_registered() -> None:
        persist_registered(registry, registered_file)

    # ------------------------------------------------------------------
    # Profile templates (v0.8.2). The {template} value the fleet is on is
    # persisted next to registered_nodes.json so a controller restart keeps
    # Re-apply All on the same set. Falls back to template_default.
    # ------------------------------------------------------------------
    _ACTIVE_TEMPLATE_FILE = cfgmod.SECRETS_DIR / "active_template.json"

    def _load_active_template() -> str:
        try:
            if _ACTIVE_TEMPLATE_FILE.is_file():
                data = json.loads(_ACTIVE_TEMPLATE_FILE.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict):
                    return str(data.get("template") or "").strip()
        except Exception as exc:  # noqa: BLE001 — a bad file must not break the controller
            log.get().warning("[template] could not read %s: %s", _ACTIVE_TEMPLATE_FILE, exc)
        return ""

    def _save_active_template(name: str) -> None:
        try:
            _ACTIVE_TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _ACTIVE_TEMPLATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"template": name, "set_at": time.time()}, indent=2),
                encoding="utf-8",
            )
            tmp.replace(_ACTIVE_TEMPLATE_FILE)
        except Exception as exc:  # noqa: BLE001
            log.get().warning("[template] could not persist %r: %s", name, exc)

    def _current_template() -> str:
        """Last picked template, else [controller].template_default, else ''."""
        return _load_active_template() or str(
            cfgmod.get_controller_config().get("template_default") or ""
        ).strip()

    def _profile_names_for_templates() -> list[str]:
        """Controller's own profiles/ (canonical; dashboard saves land here
        and FreeFileSync mirrors from it) plus whatever online agents list."""
        names: set[str] = set()
        try:
            names.update(p.stem for p in cfgmod.PROFILES_DIR.glob("*.json"))
        except Exception:  # noqa: BLE001
            pass
        try:
            names.update(_collect_profiles_union(aggregator))
        except Exception:  # noqa: BLE001
            pass
        return sorted(names)

    def _templates_payload() -> dict[str, Any]:
        cfg = cfgmod.get_controller_config()
        node_profiles = cfg.get("node_profiles") or {}
        patterns = cfgmod.template_patterns(node_profiles)
        names = _profile_names_for_templates()
        templates = cfgmod.discover_templates(node_profiles, names)
        # Values that exist for SOME pattern but not all — surfaced so the
        # operator can see "30 - SyncHost exists but 30 - SyncClient doesn't".
        seen: set[str] = set()
        for pat in patterns:
            seen.update(cfgmod.discover_templates({"_": pat}, names))
        return {
            "templates": templates,
            "active": _load_active_template(),
            "default": str(cfg.get("template_default") or ""),
            "current": _current_template(),
            "patterns": patterns,
            "roles": {
                pat: sorted(h for h, v in node_profiles.items() if v == pat)
                for pat in patterns
            },
            "missing": sorted(t for t in seen if t not in templates),
        }

    # The view-model is stateful (sparkline ring buffers, dismissed alerts).
    # One instance per controller process — the handler captures it via closure.
    vm = view_model if view_model is not None else dash.DashboardViewModel()

    class ControllerHandler(BaseHTTPRequestHandler):
        server_version = "nvmon-controller/0.1"

        # Quiet the default stderr log — route through our logger.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.get().debug("[http] %s - %s", self.address_string(), format % args)

        # ----------------------------------------------------------------
        # Dispatch
        # ----------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
            try:
                self._dispatch_get()
            except Exception as exc:  # noqa: BLE001 — defensive
                log.get().exception("[controller] GET %s crashed: %s", self.path, exc)
                self._safe_error(500, f"internal error: {exc}")

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._dispatch_post()
            except Exception as exc:  # noqa: BLE001
                log.get().exception("[controller] POST %s crashed: %s", self.path, exc)
                self._safe_error(500, f"internal error: {exc}")

        def do_DELETE(self) -> None:  # noqa: N802
            try:
                self._dispatch_delete()
            except Exception as exc:  # noqa: BLE001
                log.get().exception("[controller] DELETE %s crashed: %s", self.path, exc)
                self._safe_error(500, f"internal error: {exc}")

        def _dispatch_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            raw_path = parsed.path  # keep trailing slash for static lookups

            # Always-open endpoints (no auth gate).
            if path == "/health":
                return self._handle_health()
            if path == "/login":
                return self._handle_login_form(parsed.query)
            # Open like /register: agents ask at boot whether they belong to
            # a frame-lock chain (read-only, no secrets — group/role names).
            if path == "/sync_chain":
                host = (parse_qs(parsed.query).get("host") or [""])[0]
                info = chainsync.chain_for(host) if chainsync is not None else None
                return self._json_response(200, {"in_chain": bool(info), **(info or {})})

            # Static assets are also unauthenticated — they're hashes-of-bytes
            # served from a fixed dir, no leakage risk.
            if raw_path.startswith("/static/"):
                return self._handle_static(raw_path)

            # nvmon app only: its window opens /app/enter?t=<per-launch
            # secret> once and gets the session cookie (no login form).
            if path == "/app/enter" and APP_HOOK is not None:
                token = (parse_qs(parsed.query).get("t") or [""])[0]
                if not authmod.password_ok(token):
                    return self._json_response(403, {"error": "bad app token"})
                cookie = (f"{authmod.SESSION_COOKIE_NAME}={authmod.mint_session_cookie()}; "
                          f"Path=/; HttpOnly; SameSite=Lax")
                return self._redirect("/", extra_headers=[("Set-Cookie", cookie)])

            # Everything else requires auth.
            if not authmod.request_is_authorized(self):
                # Browsers get a login form; API callers get 401 JSON.
                if self._prefers_html():
                    return self._redirect_to_login(path)
                return self._json_response(401, {
                    "error": "authentication required",
                    "hint": "set X-NVMON-Password header or POST /login",
                })

            # Static routes.
            static = {
                "/": self._handle_root,
                "/nodes": self._handle_nodes,
                "/discovery/raw": self._handle_discovery_raw,
                "/status": self._handle_status,
                "/profiles": self._handle_profiles_union,
                "/templates": self._handle_templates,
                "/chains": lambda: self._json_response(
                    200, chainsync.public() if chainsync is not None else {"chains": {}, "error": "chain sync not running"}),
                "/launchers": self._handle_launchers_stub,
                "/apps": self._handle_apps_stub,
                "/logout": self._handle_logout,
                "/stream": self._handle_stream,
                "/logs": self._handle_controller_logs,
            }
            if path in static:
                return static[path]()

            # Dynamic: /node/<hostname>/status
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/status", path)
            if m:
                return self._handle_node_status(m.group(1))

            # Dynamic: /node/<hostname>/detail (HTMX partial)
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/detail", path)
            if m:
                return self._handle_node_detail(m.group(1))

            # Dynamic: /node/<hostname>/logs (proxied tail)
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/logs", path)
            if m:
                return self._handle_node_logs(m.group(1), parsed.query)

            # Dynamic: /node/<hostname>/<subsys>/raw — proxied JSON dump
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/(qsync|displays|mosaic)/raw", path)
            if m:
                return self._handle_node_raw(m.group(1), m.group(2))

            # Dynamic: /node/<hostname>/apply/status — passthrough to agent's
            # /apply/status. Used by the dashboard's apply flow to poll for
            # the truthful per-node outcome (success/"nothing to apply"/error)
            # after the dispatch-only "accepted" toast lands. Without this
            # the modal could only ever show what the agent said when it
            # received the apply, never what the apply pipeline actually did.
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/apply/status", path)
            if m:
                return self._handle_node_passthrough_get(m.group(1), "/apply/status")

            # Dynamic: /node/<hostname>/mosaic/timings[?profile=X] — Mosaic
            # dry run on the agent (configureMosaic test, nothing applied):
            # which rate the wall would actually come up at. Preflight for
            # Apply-All so a bad timing fails in seconds, not after a
            # 2-minute apply that blanks the wall.
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/mosaic/timings", path)
            if m:
                agent_path = "/mosaic/timings" + (f"?{parsed.query}" if parsed.query else "")
                return self._handle_node_passthrough_get(m.group(1), agent_path, timeout=70)

            # Dynamic: /node/<hostname>/displays/signals — Windows CCD view
            # (desktop vs signal on the wire) for the scaled-wall diagnosis.
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/displays/signals", path)
            if m:
                return self._handle_node_passthrough_get(m.group(1), "/displays/signals")

            # Dynamic: /history/<hostname>?metric=...&window=...
            # Stub for now — returns an empty samples array so the slide-over
            # GPU charts stop emitting "history HTTP 404".
            m = re.fullmatch(rf"/history/({_HOSTNAME_RE})", path)
            if m:
                return self._handle_history(m.group(1), parsed.query)

            return self._json_response(404, {"error": "not found", "path": path})

        def _dispatch_post(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            # /login is always reachable.
            if path == "/login":
                return self._handle_login_submit()

            # /register is unauthenticated by design — listeners on the
            # trusted LAN don't carry the controller password; registration
            # IS the bootstrap step for that listener's first contact. The
            # trusted-LAN model already treats agents as peers (their
            # /health, /status etc. are unauth on port 9998).
            if path == "/register":
                body = self._read_json_body()
                return self._handle_register(body)

            if not authmod.request_is_authorized(self):
                return self._json_response(401, {
                    "error": "authentication required",
                    "hint": "set X-NVMON-Password header or POST /login",
                })

            body = self._read_json_body()

            if path == "/logout":
                return self._handle_logout()
            if path == "/all/apply":
                return self._handle_all_apply(body)
            if path == "/all/template":
                return self._handle_all_template(body)
            if path == "/all/restore":
                return self._handle_all_restore(body)
            if path == "/all/qsync/enable":
                return self._handle_all_qsync_enable(body)
            if path == "/all/qsync/disable":
                return self._handle_all_qsync_disable(body)
            if path == "/all/restart":
                return self._handle_all_restart(body)
            if path == "/all/shutdown":
                return self._handle_all_shutdown(body)
            if path == "/all/abort":
                return self._handle_all_abort(body)
            if path == "/fanout/modal":
                return self._handle_fanout_modal(parsed.query, body)
            if path == "/fanout/job":
                return self._handle_fanout_job(body)
            if path == "/poll":
                return self._handle_force_poll()
            if path == "/chains/run":
                # Manual trigger: sequence one chain (or all) now, server first.
                if chainsync is None:
                    return self._json_response(503, {"error": "chain sync not running"})
                want = str((body or {}).get("group") or "").strip()
                chains = chainsync.chains()
                targets = {g: c for g, c in chains.items() if not want or g == want}
                if not targets:
                    return self._json_response(404, {"error": "no such chain", "chains": list(chains)})
                for g, c in targets.items():
                    snaps = {h: aggregator.node_snapshot(h) for h in [c["server"], *c["clients"]]}
                    threading.Thread(target=chainsync.run_chain, args=(g, c, snaps), daemon=True).start()
                return self._json_response(202, {"started": list(targets)})
            if path == "/system/restart_monitor":
                return self._handle_restart_monitor(body)
            # nvmon app only: window controls (drag, pin, opacity, ...).
            if path.startswith("/app/") and APP_HOOK is not None:
                return self._json_response(200, APP_HOOK(path[len("/app/"):], body or {}) or {})

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/ping", path)
            if m:
                return self._handle_node_ping(m.group(1))

            m = re.fullmatch(rf"/group/({_HOSTNAME_RE})/apply", path)
            if m:
                return self._handle_group_apply(m.group(1), body)

            m = re.fullmatch(rf"/group/({_HOSTNAME_RE})/template", path)
            if m:
                return self._handle_group_template(m.group(1), body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/apply", path)
            if m:
                return self._handle_node_apply(m.group(1), body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/restore", path)
            if m:
                return self._handle_node_passthrough(m.group(1), "/restore", body)

            # Cancel the running apply / restore / boot-apply on one node.
            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/apply/cancel", path)
            if m:
                return self._handle_node_passthrough(m.group(1), "/apply/cancel", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/qsync/enable", path)
            if m:
                return self._handle_node_passthrough(m.group(1), "/qsync/enable", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/qsync/disable", path)
            if m:
                return self._handle_node_passthrough(m.group(1), "/qsync/disable", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/dd/enable", path)
            if m:
                return self._handle_node_passthrough(m.group(1), "/dd/enable", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/dd/disable", path)
            if m:
                return self._handle_node_passthrough(m.group(1), "/dd/disable", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/mosaic/disable", path)
            if m:
                return self._handle_node_passthrough(m.group(1), "/mosaic/disable", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/profile/save", path)
            if m:
                return self._handle_node_profile_save(m.group(1), body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/system/restart", path)
            if m:
                return self._handle_node_power(m.group(1), "/system/restart", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/system/restart_listener", path)
            if m:
                # restart_listener doesn't take down the whole box, so route
                # through the regular passthrough instead of _handle_node_power
                # (which carries the self-host guard).
                return self._handle_node_passthrough(m.group(1), "/system/restart_listener", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/system/shutdown", path)
            if m:
                return self._handle_node_power(m.group(1), "/system/shutdown", body)

            m = re.fullmatch(rf"/node/({_HOSTNAME_RE})/system/abort", path)
            if m:
                return self._handle_node_power(m.group(1), "/system/abort", body)

            m = re.fullmatch(r"/alerts/([A-Za-z0-9._:\-]+)/dismiss", path)
            if m:
                return self._handle_alert_dismiss(m.group(1))

            return self._json_response(404, {"error": "not found", "path": path})

        # ----------------------------------------------------------------
        # Always-open routes
        # ----------------------------------------------------------------

        def _handle_health(self) -> None:
            self._json_response(200, {
                "ok": True,
                "service": "nvmon-controller",
                "version": __version__,
                "hostname": cfgmod.get_hostname(),
                "ip": cfgmod.get_local_ip(),
                "node_count": registry.count(),
                "auth_enabled": authmod.is_auth_enabled(),
            })

        # ----------------------------------------------------------------
        # JSON read routes
        # ----------------------------------------------------------------

        def _handle_nodes(self) -> None:
            nodes = [n.to_public_dict() for n in registry.get_all()]
            groups = {
                name: [n.hostname for n in registry.get_by_group(name)]
                for name in registry.group_names()
            }
            self._json_response(200, {
                "nodes": nodes,
                "count": len(nodes),
                "groups": groups,
            })

        def _handle_discovery_raw(self) -> None:
            """Diagnostic dump of the full mDNS discovery cache.

            Use case: the dashboard shows fewer nodes than you expect.
            /nodes shows the deduped/filtered registry; this endpoint
            shows EVERY announcement zeroconf has heard, including ones
            that NodeRegistry collapsed (same hostname) or filtered
            (role!='agent', e.g. launcher announcements). If a listener
            is running but missing from /nodes, look here first. If
            it's missing here too, it's a network / firewall / TTL
            problem, not a controller bug.
            """
            if disc_registry is None:
                self._json_response(200, {
                    "available": False,
                    "reason": "discovery disabled (zeroconf missing or controller built without disc_registry)",
                    "discovered": [],
                })
                return
            try:
                live = disc_registry.get_all()
            except Exception as exc:
                self._json_response(500, {"error": str(exc)})
                return
            payload = []
            for n in live:
                last_seen = getattr(n, "last_seen", None)
                payload.append({
                    "hostname": n.hostname,
                    "ip": n.ip,
                    "port": n.port,
                    "role": n.role,
                    "machine_id": n.machine_id,
                    "version": getattr(n, "version", "") or "",
                    "caps": getattr(n, "caps", []) or [],
                    "alive": bool(getattr(n, "alive", True)),
                    "last_seen": last_seen.isoformat() if last_seen else "",
                    "source": getattr(n, "source", "mdns"),
                })
            agents = [p for p in payload if (p.get("role") or "").lower() == "agent"]
            self._json_response(200, {
                "available": True,
                "discovered_total": len(payload),
                "discovered_agents": len(agents),
                "registered_in_node_registry": registry.count(),
                "discovered": payload,
            })

        def _handle_status(self) -> None:
            self._json_response(200, aggregator.snapshot())

        def _handle_node_status(self, hostname: str) -> None:
            snap = aggregator.node_snapshot(hostname)
            if snap is None:
                self._json_response(404, {"error": "unknown hostname", "hostname": hostname})
                return
            self._json_response(200, snap)

        def _handle_launchers_stub(self) -> None:
            # E's launcher registry lives in a separate module — stubbed here.
            self._json_response(200, {
                "launchers": [],
                "_note": "launcher registry handled by sub-project E",
            })

        def _handle_apps_stub(self) -> None:
            self._json_response(200, {
                "apps": {},
                "_note": "app registry handled by sub-project E",
            })

        # ----------------------------------------------------------------
        # Fan-out routes
        # ----------------------------------------------------------------

        def _handle_all_apply(self, body: dict[str, Any]) -> None:
            profile = str(body.get("profile", "") or "").strip()
            if not profile:
                self._json_response(400, {"error": "missing 'profile' in body"})
                return
            log.get().info("[controller] /all/apply profile=%s", profile)
            self._json_response(200, fanout.apply_all(profile))

        def _handle_all_template(self, body: dict[str, Any]) -> None:
            """POST /all/template {"template": "30"} — the external-system
            entry point. Every known node gets its own role's profile for
            the template (per [controller.node_profiles] patterns); the pick
            is remembered so Re-apply All stays on it. Same response shape
            as /fanout/job (summary + per-node progress)."""
            self._handle_fanout_job({
                "action": "template",
                "template": body.get("template"),
                "scope": [n.hostname for n in registry.get_all()],
                "skip_preflight": body.get("skip_preflight"),
            })

        def _handle_group_template(self, group: str, body: dict[str, Any]) -> None:
            if group not in registry.group_names():
                self._json_response(404, {"error": "unknown group", "group": group})
                return
            self._handle_fanout_job({
                "action": "template",
                "template": body.get("template"),
                "scope": [n.hostname for n in registry.get_by_group(group)],
                "skip_preflight": body.get("skip_preflight"),
            })

        def _handle_all_restore(self, body: dict[str, Any]) -> None:
            log.get().info("[controller] /all/restore")
            self._json_response(200, fanout.restore_all())

        def _handle_all_qsync_enable(self, body: dict[str, Any]) -> None:
            source = str(body.get("source", "") or "").strip() or "HOUSESYNC"
            trigger = str(body.get("trigger", "") or "").strip() or "FALLING"
            # grid defaults to -1 — sentinel for "no grid; let each agent
            # auto-discover its connected displays". An explicit non-negative
            # grid in the body still flows through to the legacy Mosaic-grid
            # path on every agent.
            try:
                grid = int(body.get("grid", -1))
            except (TypeError, ValueError):
                self._json_response(400, {"error": "invalid 'grid' — expected int"})
                return
            log.get().info(
                "[controller] /all/qsync/enable source=%s trigger=%s grid=%s",
                source, trigger, grid,
            )
            self._json_response(200, fanout.qsync_enable_all(
                source=source, trigger=trigger, grid=grid,
            ))

        def _handle_all_qsync_disable(self, body: dict[str, Any]) -> None:
            log.get().info("[controller] /all/qsync/disable")
            self._json_response(200, fanout.qsync_disable_all())

        def _handle_all_restart(self, body: dict[str, Any]) -> None:
            # Loud log line: a fleet-wide restart is a Big Deal and we want
            # an obvious breadcrumb in the controller log even if the
            # dashboard's two-step confirmation is bypassed via curl.
            log.get().warning("[controller] /all/restart fanout — fleet reboot requested")
            self._json_response(200, fanout.restart_all())

        def _handle_all_shutdown(self, body: dict[str, Any]) -> None:
            log.get().warning("[controller] /all/shutdown fanout — fleet power-off requested")
            self._json_response(200, fanout.shutdown_all())

        def _handle_all_abort(self, body: dict[str, Any]) -> None:
            log.get().warning("[controller] /all/abort fanout — cancelling queued shutdowns")
            self._json_response(200, fanout.abort_all())

        def _handle_group_apply(self, group: str, body: dict[str, Any]) -> None:
            profile = str(body.get("profile", "") or "").strip()
            if not profile:
                self._json_response(400, {"error": "missing 'profile' in body"})
                return
            if group not in registry.group_names():
                self._json_response(404, {"error": "unknown group", "group": group})
                return
            log.get().info("[controller] /group/%s/apply profile=%s", group, profile)
            self._json_response(200, fanout.apply_group(group, profile))

        def _handle_node_apply(self, hostname: str, body: dict[str, Any]) -> None:
            profile = str(body.get("profile", "") or "").strip()
            if not profile:
                self._json_response(400, {"error": "missing 'profile' in body"})
                return
            log.get().info(
                "[controller] /node/%s/apply profile=%s",
                hostname, profile,
            )
            self._json_response(200, fanout.node_passthrough(
                hostname,
                method="POST",
                path="/apply",
                body={"profile": profile},
            ))

        # ----------------------------------------------------------------
        # Auth routes
        # ----------------------------------------------------------------

        def _handle_login_form(self, query_string: str) -> None:
            params = parse_qs(query_string or "")
            next_path = (params.get("next") or ["/"])[0] or "/"
            if not authmod.is_auth_enabled():
                # Nothing to log into; send them to root.
                self._redirect(next_path)
                return
            self._html_response(200, dashboard_placeholder.render_login(next_path=next_path))

        def _handle_login_submit(self) -> None:
            # Read form body (urlencoded) or JSON body.
            content_type = (self.headers.get("Content-Type") or "").lower()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""

            password = ""
            next_path = "/"
            if "application/json" in content_type:
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    data = {}
                password = str(data.get("password", "") or "")
                next_path = str(data.get("next", "/") or "/")
            else:
                fields = parse_qs(raw, keep_blank_values=True)
                password = (fields.get("password") or [""])[0]
                next_path = (fields.get("next") or ["/"])[0] or "/"

            if not authmod.is_auth_enabled():
                # No password needed; just send them through.
                self._redirect(next_path)
                return

            if not authmod.password_ok(password):
                log.get().warning(
                    "[auth] failed login attempt from %s",
                    self.address_string(),
                )
                # Browser: re-render the form with an error.
                # API: return 401.
                if "application/json" in content_type:
                    self._json_response(401, {"error": "invalid password"})
                    return
                self._html_response(
                    401,
                    dashboard_placeholder.render_login(
                        error="Invalid password.",
                        next_path=next_path,
                    ),
                )
                return

            cookie_value = authmod.mint_session_cookie()
            # HttpOnly, Path=/, SameSite=Lax. No Expires (clears with browser).
            cookie_header = (
                f"{authmod.SESSION_COOKIE_NAME}={cookie_value}; "
                f"Path=/; HttpOnly; SameSite=Lax"
            )
            if "application/json" in content_type:
                self.send_response(200)
                self.send_header("Set-Cookie", cookie_header)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
                return

            self._redirect(next_path, extra_headers=[("Set-Cookie", cookie_header)])

        def _handle_logout(self) -> None:
            # Clear cookie by sending it expired.
            cookie_header = (
                f"{authmod.SESSION_COOKIE_NAME}=; "
                f"Path=/; HttpOnly; SameSite=Lax; "
                f"Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0"
            )
            if self._prefers_html():
                self._redirect("/login", extra_headers=[("Set-Cookie", cookie_header)])
                return
            self.send_response(200)
            self.send_header("Set-Cookie", cookie_header)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        # ----------------------------------------------------------------
        # Unified dashboard
        # ----------------------------------------------------------------

        def _build_view_context(self) -> dict[str, Any]:
            return vm.build(
                aggregator.snapshot(),
                registry,
                version=__version__,
                auth_enabled=authmod.is_auth_enabled(),
                profiles_union=_collect_profiles_union(aggregator),
                self_hostname=cfgmod.get_hostname(),
            )

        def _handle_root(self) -> None:
            try:
                ctx = self._build_view_context()
                html_str = dash.render_dashboard(ctx)
            except Exception as exc:  # noqa: BLE001 — defensive
                log.get().exception("[controller] dashboard render failed: %s", exc)
                # Fallback to the legacy placeholder so the operator still
                # sees something instead of a blank 500.
                html_str = dashboard_placeholder.render(
                    aggregator.snapshot(),
                    auth_enabled=authmod.is_auth_enabled(),
                )
            self._html_response(200, html_str)

        def _handle_node_detail(self, hostname: str) -> None:
            ctx = self._build_view_context()
            node = vm.find_node(ctx, hostname)
            if node is None:
                self._json_response(404, {"error": "unknown hostname", "hostname": hostname})
                return
            try:
                html_str = dash.render_node_detail(node)
            except Exception as exc:  # noqa: BLE001
                log.get().exception("[controller] node_detail render failed: %s", exc)
                self._json_response(500, {"error": str(exc)})
                return
            self._html_response(200, html_str)

        def _handle_static(self, raw_path: str) -> None:
            # raw_path begins with "/static/"
            rel = raw_path[len("/static/"):]
            result = dash.serve_static(rel)
            if result is None:
                self._json_response(404, {"error": "not found", "path": raw_path})
                return
            mime, body = result
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            # No caching — we ship JS/CSS/template changes often and a
            # browser sitting on yesterday's nvmon.js produces "buttons
            # don't work" reports that are very hard to explain. The
            # dashboard is intranet-only so the bandwidth cost is nil.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)

        def _handle_templates(self) -> None:
            """GET /templates — complete profile sets the fleet can switch to.

            {templates: [...], active, default, current, patterns, roles,
            missing}. A template is listed only when EVERY {template}
            pattern in [controller.node_profiles] has a matching profile
            (e.g. both "30 - SyncHost" and "30 - SyncClient").
            """
            self._json_response(200, _templates_payload())

        def _handle_profiles_union(self) -> None:
            """JSON list of every profile name observed across all online agents."""
            self._json_response(200, {
                "profiles": _collect_profiles_union(aggregator),
            })

        # ----------------------------------------------------------------
        # SSE — degraded but functional. Implements the contract in sse.py:
        #   - opening heartbeat + per-node burst
        #   - steady-state node_update + heartbeat at 5s cadence
        # We poll the aggregator's snapshot rather than subscribing because
        # the aggregator doesn't expose subscribe() yet. This is fine: each
        # poll produces one frame per online node + a heartbeat.
        # ----------------------------------------------------------------

        def _handle_stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            try:
                last_levels: dict[str, dict[str, str]] = {}
                last_reachable: dict[str, bool] = {}
                # Track which hostnames we've already streamed to this client.
                # When the registry gains/loses a node mid-stream (e.g. a
                # listener boots after the dashboard was opened), we emit
                # node_appeared / node_disappeared events so the front-end
                # can reload — otherwise the user has to hit refresh to see
                # the new node card.
                known_hosts: set[str] = set()

                while True:
                    ctx = self._build_view_context()
                    ctrl = ctx["controller"]

                    # Heartbeat
                    self.wfile.write(dash.stream_sse_event("heartbeat", {
                        "controller_uptime_s": ctrl["uptime_seconds"],
                        "nodes_online": ctrl["nodes_online"],
                        "nodes_offline": ctrl["nodes_offline"],
                    }))

                    cur_hosts = {n["hostname"] for n in ctx["nodes"]}
                    appeared = cur_hosts - known_hosts
                    disappeared = known_hosts - cur_hosts
                    if known_hosts:
                        # Skip the initial population — the page already
                        # rendered those nodes server-side. Only fire on
                        # post-open transitions.
                        for h in sorted(appeared):
                            self.wfile.write(dash.stream_sse_event("node_appeared", {
                                "hostname": h,
                            }))
                        for h in sorted(disappeared):
                            self.wfile.write(dash.stream_sse_event("node_disappeared", {
                                "hostname": h,
                            }))
                    known_hosts = cur_hosts

                    for node in ctx["nodes"]:
                        host = node["hostname"]

                        # node_reachable_change — only on transition.
                        prev_reach = last_reachable.get(host)
                        cur_reach = bool(node["online"])
                        if prev_reach is not None and prev_reach != cur_reach:
                            self.wfile.write(dash.stream_sse_event("node_reachable_change", {
                                "hostname": host,
                                "reachable": cur_reach,
                                "last_seen": node.get("last_seen"),
                            }))
                        last_reachable[host] = cur_reach

                        if not cur_reach:
                            continue

                        # node_update — emit each cycle so card stays fresh.
                        self.wfile.write(dash.stream_sse_event("node_update", {
                            "hostname": host,
                            "subsystems": node.get("subsystems") or {},
                            "gpu": [
                                {
                                    "index": g["index"],
                                    "temperature_c": g["temperature_c"],
                                    "power_draw_w": g["power_draw_w"],
                                    "power_limit_w": g["power_limit_w"],
                                    "utilization_pct": g["utilization_pct"],
                                    "sparkline": g.get("sparkline") or [],
                                }
                                for g in node.get("gpu") or []
                            ],
                            "refresh_hz": node.get("refresh_hz", 0.0),
                            "displays_count": node.get("displays_count", 0),
                            "color_text": node.get("color_text", ""),
                            "alerts_count": node.get("alerts_count", 0),
                            "last_poll_ms_ago": node.get("last_poll_ms_ago"),
                            "active_profile": node.get("active_profile"),
                            # v0.8.2: the card's "applying / stale" marker,
                            # job badge, chain line and list-row cells
                            # were server-rendered only — the SSE frame
                            # never carried these, so the tile needed a
                            # manual reload to show a finished apply.
                            "status_age_s": node.get("status_age_s", 0.0),
                            "job_active": bool(node.get("job_active")),
                            "job": node.get("job") or {},
                            "qsync_role": node.get("qsync_role") or "",
                            "chain": node.get("chain") or {},
                            "validation_ok": (
                                bool((node.get("validation") or {}).get("all_passed"))
                                if (node.get("validation") or {}).get("checks") else None
                            ),
                            "errors_count": len(node.get("errors") or []),
                        }))

                    self.wfile.flush()
                    time.sleep(5.0)
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected — normal.
                return
            except Exception as exc:  # noqa: BLE001
                log.get().debug("[controller] /stream ended: %s", exc)

        # ----------------------------------------------------------------
        # Fanout modal / job (HTMX partial + thin job dispatcher)
        # ----------------------------------------------------------------

        def _handle_fanout_modal(self, query: str, body: dict[str, Any]) -> None:
            params = parse_qs(query or "")
            action = (params.get("action") or [""])[0] or "apply"
            scope = body.get("scope") or []
            skipped = body.get("skipped") or []
            profiles = body.get("profiles") or _collect_profiles_union(aggregator)
            html_str = dash.render_fanout_modal(
                action=str(action),
                scope=[str(x) for x in scope if str(x).strip()],
                skipped=[s for s in skipped if isinstance(s, dict)],
                profiles=[str(p) for p in profiles if str(p).strip()],
            )
            self._html_response(200, html_str)

        def _handle_fanout_job(self, body: dict[str, Any]) -> None:
            """Synchronous fan-out runner.

            The dashboard's modal originally expected a WebSocket for
            per-node progress; we don't run a WS server in stdlib, so we
            run the fanout synchronously and return:
                {summary, progress, ws_url:""}
            The client JS keys on ``ws_url == ""`` and renders the summary
            stage directly. ``progress`` is included so the UI can show a
            per-node row even though everything finished server-side.
            """
            action = str(body.get("action", "apply")).strip()
            scope = [str(x) for x in (body.get("scope") or []) if str(x).strip()]
            profile = body.get("profile")

            # Template action (v0.8.2): "switch the scope to profile set X".
            # Validates X against the complete sets on disk, remembers it,
            # then runs as a Re-apply with the {template} patterns resolved
            # to X — so hosts get "X - SyncHost", clients "X - SyncClient".
            template_override = ""
            if action == "template":
                tpl = str(body.get("template") or "").strip()
                payload = _templates_payload()
                if not tpl:
                    self._json_response(400, {"error": "missing 'template' in body", **payload})
                    return
                if not payload["patterns"]:
                    self._json_response(400, {
                        "error": "no {template} patterns in [controller.node_profiles] — "
                                 "templates are disabled on this controller",
                        **payload,
                    })
                    return
                if tpl not in payload["templates"]:
                    self._json_response(400, {
                        "error": (
                            f"unknown or incomplete template {tpl!r} — every pattern "
                            f"needs a profile ({', '.join(payload['patterns'])})"
                        ),
                        **payload,
                    })
                    return
                _save_active_template(tpl)
                log.get().info(
                    "[controller] template -> %r on %d node(s) (was %r)",
                    tpl, len(scope), payload.get("active") or payload.get("default") or "",
                )
                template_override = tpl
                action = "apply"
                profile = None

            from . import fanout as fanout_mod

            results: dict[str, Any]

            # Snapshot active_profile per host once — used by Sync (always)
            # and Re-apply All (when no explicit profile is given). Reading
            # from the aggregator cache means we never block on a fresh poll;
            # if the cache is stale the worst case is "no active profile" =
            # skipped, which the operator sees in the modal summary.
            def _active_profile_map() -> dict[str, str]:
                snap = aggregator.snapshot() or {}
                out: dict[str, str] = {}
                for n in (snap.get("nodes") or []):
                    host = str(n.get("hostname") or "")
                    st = n.get("status") or {}
                    if isinstance(st, dict):
                        ap = st.get("active_profile")
                        if isinstance(ap, str) and ap.strip():
                            out[host] = ap.strip()
                return out

            def _mosaic_preflight(
                nodes: list[Any], profile_for,
            ) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]]]:
                """Dry-run every target's Mosaic timing before blanking a wall.

                Calls ``GET /mosaic/timings?profile=X`` on each node (the
                agent runs ``configureMosaic test`` — nothing is applied) and
                drops nodes whose wall would come up at a different rate than
                the profile asks for (asked 30 → driver picks 29.97; a QSync
                lock can then never happen). Nodes whose profile has Mosaic
                disabled, whose agent predates the endpoint (404), or that
                can't be reached are passed through — the apply itself will
                report those. Returns (kept, skipped_results, report).
                Body ``skip_preflight: true`` bypasses it. (the legacy tool 365e2a6)
                """
                if not nodes or body.get("skip_preflight"):
                    return nodes, [], []
                return fanout_mod.mosaic_preflight(
                    nodes, profile_for, profiles_dir=cfgmod.PROFILES_DIR,
                )

            preflight_report: list[dict[str, Any]] = []

            if action == "apply":
                # Apply-only blocklist: drop hostnames the operator listed
                # in [controller].apply_excluded_hosts before the fan-out.
                # Other actions (sync / restore / restart_listener / power)
                # still hit those nodes — only profile apply is filtered.
                # Match is case-insensitive on the leading DNS label so
                # "NODE-11", "node-11.local" and "Node-11" all hit.
                excluded = set(
                    h.strip().lower().split(".")[0]
                    for h in (cfgmod.get_controller_config().get("apply_excluded_hosts") or [])
                    if isinstance(h, str) and h.strip()
                )
                in_scope_nodes = [
                    n for n in registry.get_all() if n.hostname in scope
                ]
                kept_nodes: list[Any] = []
                excluded_results: list[dict[str, Any]] = []
                for n in in_scope_nodes:
                    if n.hostname.strip().lower().split(".")[0] in excluded:
                        log.get().info(
                            "[controller] /fanout/job apply: excluding %s "
                            "(in apply_excluded_hosts)", n.hostname,
                        )
                        excluded_results.append({
                            "hostname": n.hostname,
                            "status": "skipped",
                            "reason": "excluded from profile apply (apply_excluded_hosts in nvmon.toml)",
                        })
                    else:
                        kept_nodes.append(n)

                if profile:
                    # Explicit-push path: today's behavior — one canonical
                    # profile to every kept node.
                    kept_nodes, pre_skipped, preflight_report = _mosaic_preflight(
                        kept_nodes, lambda n: str(profile),
                    )
                    excluded_results = excluded_results + pre_skipped
                    if kept_nodes:
                        results = fanout._fanout(  # noqa: SLF001
                            kept_nodes,
                            method="POST", path="/apply",
                            body={"profile": str(profile)},
                        )
                        if excluded_results:
                            merged = (results.get("results") or []) + excluded_results
                            merged.sort(key=lambda r: r.get("hostname", ""))
                            results = fanout_mod._tally(merged, requested=len(in_scope_nodes))
                    else:
                        results = fanout_mod._tally(excluded_results, requested=len(in_scope_nodes))
                else:
                    # Re-apply All path: each node re-applies its own
                    # currently-active profile. Lets the fleet hold mixed
                    # profiles and still be re-applied with one click.
                    # Precedence per node: [controller].node_profiles (operator
                    # intent, e.g. 24_Server on the QSync master, 24_Client on
                    # the chain) > the node's currently active profile > skip.
                    # This is what makes the FIRST apply on a mixed fleet one
                    # click instead of one per node.
                    profile_map = _active_profile_map()
                    # {template} entries resolve against the template being
                    # applied (Template action) or the one the fleet is on
                    # (last pick, else template_default). Unresolvable
                    # templated entries drop out and the node falls back to
                    # its active profile, same as an unlisted node.
                    tpl_in_use = template_override or _current_template()
                    configured = cfgmod.resolve_node_profiles(
                        cfgmod.get_controller_config().get("node_profiles") or {},
                        tpl_in_use,
                    )
                    n_cfg = 0
                    for n in kept_nodes:
                        cfg_profile = cfgmod.node_profile_for(n.hostname, configured)
                        if cfg_profile:
                            profile_map[n.hostname] = cfg_profile
                            n_cfg += 1
                    log.get().info(
                        "[controller] /fanout/job re-apply: %d kept nodes, "
                        "%d from node_profiles (template=%r), %d have active_profile",
                        len(kept_nodes), n_cfg, tpl_in_use,
                        sum(1 for n in kept_nodes if profile_map.get(n.hostname)),
                    )
                    kept_nodes, pre_skipped, preflight_report = _mosaic_preflight(
                        kept_nodes, lambda n: profile_map.get(n.hostname) or "",
                    )
                    excluded_results = excluded_results + pre_skipped
                    if kept_nodes:
                        results = fanout._fanout_per_node(  # noqa: SLF001
                            kept_nodes,
                            method="POST", path="/apply",
                            body_for=lambda n: (
                                {"profile": profile_map[n.hostname]}
                                if profile_map.get(n.hostname) else None
                            ),
                            skip_reason="no profile for this node (set [controller.node_profiles] in nvmon.toml, or apply once explicitly)",
                        )
                        if excluded_results:
                            merged = (results.get("results") or []) + excluded_results
                            merged.sort(key=lambda r: r.get("hostname", ""))
                            results = fanout_mod._tally(merged, requested=len(in_scope_nodes))
                    else:
                        results = fanout_mod._tally(excluded_results, requested=len(in_scope_nodes))
            elif action == "restore":
                results = fanout._fanout(  # noqa: SLF001
                    [n for n in registry.get_all() if n.hostname in scope],
                    method="POST", path="/restore", body={},
                )
            elif action == "sync":
                # Per-node profile-aware sync: each node re-applies the
                # QSync section of its own active_profile via the agent's
                # /qsync/enable_from_profile endpoint. This honors the
                # captured role (server vs daisy-chained client) and the
                # captured display selection — fixing the previous behavior
                # where Sync hardcoded HOUSESYNC + FALLING + enable server
                # for every node regardless of what its profile said.
                #
                # We deliberately do NOT pass required_capability=qsync —
                # the pre-skip used to fire whenever the agent reported
                # qsync.available=False (e.g. agent not admin or exe missing),
                # silently dropping the request. We let the agent answer
                # 503 with a readable reason which surfaces in the modal.
                in_scope_nodes = [n for n in registry.get_all() if n.hostname in scope]
                profile_map = _active_profile_map()
                results = fanout._fanout_per_node(  # noqa: SLF001
                    in_scope_nodes,
                    method="POST", path="/qsync/enable_from_profile",
                    body_for=lambda n: (
                        {"profile": profile_map[n.hostname]}
                        if profile_map.get(n.hostname) else None
                    ),
                    skip_reason="no active profile (apply once explicitly first)",
                )
            elif action in ("restart", "shutdown", "abort", "restart_listener"):
                # System actions. restart/shutdown/abort hit Windows
                # shutdown.exe; restart_listener bounces the listener
                # process via the 'nvmon Listener' Scheduled Task — no
                # OS reboot, used to pick up freshly mirrored code.
                # The dashboard's confirm() gates user intent; this
                # layer just fans out. Loud log line either way.
                log.get().warning(
                    "[controller] /fanout/job action=%s scope=%s",
                    action,
                    scope,
                )
                from . import fanout as fanout_mod
                # OS-level actions refuse to hit the monitor box (would
                # orphan the UI mid-operation). restart_listener is safe
                # against self because the controller is a different
                # process — we only need the self-host guard for actions
                # that take down the whole machine.
                in_scope = [n for n in registry.get_all() if n.hostname in scope]
                if action in ("restart", "shutdown"):
                    targets, self_skipped = fanout._split_self(in_scope)  # noqa: SLF001
                else:
                    targets, self_skipped = in_scope, []
                if self_skipped:
                    log.get().warning(
                        "[controller] /fanout/job filtered self-host(s) from %s scope: %s",
                        action,
                        [r["hostname"] for r in self_skipped],
                    )
                if targets:
                    results = fanout._fanout(  # noqa: SLF001
                        targets,
                        method="POST", path=f"/system/{action}", body={},
                    )
                    if self_skipped:
                        merged = (results.get("results") or []) + self_skipped
                        merged.sort(key=lambda r: r.get("hostname", ""))
                        results = fanout_mod._tally(merged, requested=len(in_scope))
                else:
                    results = fanout_mod._tally(self_skipped, requested=len(in_scope))
            else:
                self._json_response(400, {"error": f"unknown action '{action}'"})
                return

            # Translate the per-node fanout result into a progress + summary
            # bundle in the shape the JS handleMessage() expects on the WS.
            per_node_progress: list[dict[str, Any]] = []
            failures: list[dict[str, Any]] = []
            for r in results.get("results") or []:
                state = r.get("status", "failed")
                if state == "accepted":
                    js_state = "done"
                elif state == "skipped":
                    js_state = "skipped"
                elif state == "timed_out":
                    js_state = "failed"
                    failures.append({
                        "hostname": r.get("hostname"),
                        "error": "timed out",
                    })
                else:
                    js_state = "failed"
                    failures.append({
                        "hostname": r.get("hostname"),
                        "error": str(r.get("error") or "failed"),
                    })
                per_node_progress.append({
                    "hostname": r.get("hostname"),
                    "state": js_state,
                    "state_label": state,
                })

            self._json_response(200, {
                "job_id": f"j-{int(time.time() * 1000)}",
                "ws_url": "",  # WS not implemented; client falls through.
                # v0.8.0: Mosaic dry-run results per node (apply only).
                "preflight": preflight_report,
                "targets": [r.get("hostname") for r in results.get("results") or []],
                "skipped": [
                    r for r in results.get("results") or []
                    if r.get("status") == "skipped"
                ],
                "progress": per_node_progress,
                # v0.8.2: the template this fan-out applied ("" for a plain
                # apply / re-apply) — lets an external caller confirm what
                # the fleet was switched to.
                "template": template_override,
                "summary": {
                    "succeeded": results.get("succeeded", 0),
                    "failed": results.get("failed", 0),
                    "skipped": results.get("skipped", 0),
                    "failures": failures,
                    # Hosts whose /apply was accepted — the JS polls these
                    # for the truthful per-node outcome.
                    "accepted_hosts": [
                        r.get("hostname") for r in results.get("results") or []
                        if r.get("status") == "accepted"
                    ],
                },
            })

        def _handle_node_passthrough(
            self, hostname: str, agent_path: str, body: dict[str, Any],
        ) -> None:
            """Generic POST passthrough to one agent. Used by the per-node
            buttons in the slide-over (Restore, Sync, DD enable/disable, etc.).
            """
            self._json_response(200, fanout.node_passthrough(
                hostname, method="POST", path=agent_path, body=body or {},
            ))

        def _handle_node_passthrough_get(
            self, hostname: str, agent_path: str, timeout: float = 5,
        ) -> None:
            """Generic GET passthrough to one agent. Used by the dashboard's
            apply flow to poll `/apply/status` post-dispatch and surface the
            truthful per-node outcome (success/error/in_progress) instead of
            the dispatch-only "accepted" the fanout summary returns.

            Returns the agent's body verbatim on 200; on transport failure or
            non-2xx we return a small JSON envelope the browser can read
            without throwing — the modal interprets a missing body as
            "still in progress, keep polling".
            """
            node = registry.get_by_hostname(hostname)
            if node is None:
                self._json_response(404, {"error": "unknown hostname", "hostname": hostname})
                return
            try:
                url = node.url.rstrip("/") + agent_path
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    code = resp.getcode()
                try:
                    parsed_body: Any = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    parsed_body = raw
                self._json_response(code, parsed_body if isinstance(parsed_body, (dict, list)) else {"raw": parsed_body})
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    raw = ""
                self._json_response(exc.code, {
                    "error": str(exc.reason) if exc.reason else "http error",
                    "body": raw[:500],
                })
            except Exception as exc:  # noqa: BLE001
                self._json_response(502, {"error": f"unreachable: {exc}"})

        def _handle_node_power(
            self, hostname: str, agent_path: str, body: dict[str, Any],
        ) -> None:
            """Per-node /system/{restart,shutdown,abort} — refuses self-host.

            The dashboard already hides the per-node power icon for the
            controller's own card; this is the API-side defense so a
            curl/script can't bypass that hide.
            """
            from . import fanout as fanout_mod
            if fanout_mod.is_self_host(hostname):
                log.get().warning(
                    "[controller] refusing %s on self-host %r",
                    agent_path,
                    hostname,
                )
                # Match the fanout result envelope so the dashboard's
                # toast renderer treats this like a single-node skip.
                self._json_response(403, {
                    "requested": 1,
                    "succeeded": 0,
                    "failed": 0,
                    "skipped": 1,
                    "timed_out": 0,
                    "results": [{
                        "hostname": hostname,
                        "status": "skipped",
                        "reason": fanout_mod.SKIP_REASON_SELF,
                    }],
                    "error": "refusing to power-cycle the monitor machine",
                })
                return
            self._handle_node_passthrough(hostname, agent_path, body)

        def _handle_node_ping(self, hostname: str) -> None:
            """Force-probe one agent's /health right now. Bypasses the
            aggregator's 5s polling cache so the operator can manually
            verify an OFFLINE node has come back without waiting.
            """
            node = registry.get_by_hostname(hostname)
            if node is None:
                self._json_response(404, {"error": "unknown hostname", "hostname": hostname})
                return
            url = node.url.rstrip("/") + "/health"
            t0 = time.time()
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    health = json.loads(raw) if raw else {}
                duration_ms = int((time.time() - t0) * 1000)
                # Refresh the registry so the dashboard flips ONLINE on
                # the next poll cycle even before the next aggregator tick.
                registry.update_health(
                    hostname, reachable=True, health=health, error="",
                )
                self._json_response(200, {
                    "ok": True,
                    "hostname": hostname,
                    "url": url,
                    "duration_ms": duration_ms,
                    "health": health,
                })
            except urllib.error.HTTPError as exc:
                duration_ms = int((time.time() - t0) * 1000)
                err = f"HTTP {exc.code} {exc.reason}"
                registry.update_health(hostname, reachable=False, health=None, error=err)
                self._json_response(200, {
                    "ok": False, "hostname": hostname, "url": url,
                    "duration_ms": duration_ms, "error": err,
                })
            except (urllib.error.URLError, TimeoutError) as exc:
                duration_ms = int((time.time() - t0) * 1000)
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                    err = "timed out after 5s"
                else:
                    err = str(reason).replace("[WinError 10061]", "").strip() or "unreachable"
                registry.update_health(hostname, reachable=False, health=None, error=err)
                self._json_response(200, {
                    "ok": False, "hostname": hostname, "url": url,
                    "duration_ms": duration_ms, "error": err,
                })
            except Exception as exc:  # noqa: BLE001
                duration_ms = int((time.time() - t0) * 1000)
                err = f"unexpected: {exc}"
                registry.update_health(hostname, reachable=False, health=None, error=err)
                self._json_response(200, {
                    "ok": False, "hostname": hostname, "url": url,
                    "duration_ms": duration_ms, "error": err,
                })

        def _handle_restart_monitor(self, body: dict[str, Any]) -> None:
            """POST /system/restart_monitor — bounce THIS controller process
            (no OS reboot, nodes untouched). The REM-gated "Restart monitor"
            menu item. Answers 202 before dying: a detached helper runs
            kill_monitor.bat then run_monitor.bat (minimized) on this box.
            Body: {"delay_seconds": 2, "open_browser": false}.
            """
            body = body or {}
            try:
                delay = int(body.get("delay_seconds", selfrestart.DEFAULT_DELAY_SECONDS))
            except (TypeError, ValueError):
                delay = selfrestart.DEFAULT_DELAY_SECONDS
            open_browser = bool(body.get("open_browser", False))
            log.get().warning("[controller] /system/restart_monitor requested (delay=%ss)", delay)
            result = selfrestart.restart_monitor(delay_seconds=delay, open_browser=open_browser)
            self._json_response(202 if result.get("ok") else 500, result)

        def _handle_force_poll(self) -> None:
            """Trigger an immediate aggregator poll across the entire
            registry. Used by the dashboard header ⟳ button so the operator
            can refresh online/offline state without waiting for the next
            5s tick.
            """
            t0 = time.time()
            try:
                aggregator.poll_once()
            except Exception as exc:  # noqa: BLE001
                self._json_response(500, {
                    "ok": False,
                    "error": f"aggregator poll failed: {exc}",
                })
                return
            duration_ms = int((time.time() - t0) * 1000)
            self._json_response(200, {
                "ok": True,
                "polled": registry.count(),
                "duration_ms": duration_ms,
            })

        def _handle_controller_logs(self) -> None:
            """Plain-text tail of the controller's daily log file.
            Query: ?lines=N (default 200, max 2000)."""
            params = parse_qs(urlparse(self.path).query or "")
            try:
                lines = int((params.get("lines") or ["200"])[0])
            except (TypeError, ValueError):
                lines = 200
            lines = max(1, min(lines, 2000))

            from datetime import datetime as _dt
            today = _dt.now().strftime("%Y-%m-%d")
            log_path = cfgmod.LOGS_DIR / f"nvmon-{today}.log"
            if not log_path.is_file():
                payload = b"(no controller log yet)\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as f:
                    buf = f.readlines()
                tail = "".join(buf[-lines:])
            except Exception as exc:
                self._json_response(500, {"error": str(exc)})
                return

            payload = tail.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _handle_node_raw(self, hostname: str, subsystem: str) -> None:
            """Proxy GET /<subsystem>/raw on the agent. Returns the agent's
            JSON envelope verbatim — {available, command, stdout, stderr,
            parsed, ...}. Used by the 'Show raw' button in the slide-over."""
            node = registry.get_by_hostname(hostname)
            if node is None:
                self._json_response(404, {"error": "unknown hostname", "hostname": hostname})
                return
            url = f"{node.url.rstrip('/')}/{subsystem}/raw"
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    body = json.loads(raw) if raw else {}
            except (urllib.error.URLError, TimeoutError) as exc:
                self._json_response(502, {
                    "error": "agent unreachable",
                    "hostname": hostname,
                    "detail": str(exc),
                })
                return
            except json.JSONDecodeError as exc:
                self._json_response(502, {
                    "error": "agent returned non-JSON",
                    "hostname": hostname,
                    "detail": str(exc),
                })
                return
            self._json_response(200, body)

        def _handle_history(self, hostname: str, query: str) -> None:
            """Per-(host, gpu, metric) time-series history.

            Backed by Aggregator._history which buffers up to 720 samples
            (1 hour at 5s poll). Empty array if we haven't polled this
            host yet, never 404 (so the chart draws the empty state
            cleanly instead of emitting 'history HTTP 404')."""
            node = registry.get_by_hostname(hostname)
            if node is None:
                self._json_response(404, {"error": "unknown hostname", "hostname": hostname})
                return
            params = parse_qs(query or "")
            metric = (params.get("metric") or ["temperature_c"])[0]
            window = (params.get("window") or ["1h"])[0]
            try:
                gpu_index = int((params.get("gpu") or ["0"])[0])
            except (TypeError, ValueError):
                gpu_index = 0
            window_seconds = {
                "10m": 600, "1h": 3600, "6h": 21600, "24h": 86400,
            }.get(window, 3600)
            samples = aggregator.history(
                hostname,
                metric=metric,
                window_seconds=window_seconds,
                gpu_index=gpu_index,
            )
            self._json_response(200, {
                "hostname": hostname,
                "metric": metric,
                "gpu": gpu_index,
                "window_seconds": window_seconds,
                "samples": samples,
            })

        def _handle_node_logs(self, hostname: str, query: str) -> None:
            """Proxy the agent's /logs to the dashboard. Plain text passthrough."""
            node = registry.get_by_hostname(hostname)
            if node is None:
                self._json_response(404, {"error": "unknown hostname", "hostname": hostname})
                return
            url = node.url.rstrip("/") + "/logs"
            if query:
                url += "?" + query
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = resp.read()
                    ct = resp.headers.get("Content-Type", "text/plain; charset=utf-8")
            except (urllib.error.URLError, TimeoutError) as exc:
                self._json_response(502, {
                    "error": "agent unreachable",
                    "hostname": hostname,
                    "detail": str(exc),
                })
                return
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        # ----------------------------------------------------------------
        # Profile save — capture on source, propagate to fleet + controller
        # ----------------------------------------------------------------

        def _handle_node_profile_save(self, hostname: str, body: dict[str, Any]) -> None:
            """Capture on `hostname`, then ship the JSON to every other reachable
            agent and to the controller's local `profiles/` dir.

            Old behaviour was a single agent passthrough — the file only
            existed on the source node, so Apply All would 404 on every
            other agent. The user worked around this by manually copying
            the file across the fleet; this handler does it for them now.
            """
            name = str(body.get("name", "") or "").strip()
            if not name:
                self._json_response(400, {"error": "missing 'name'"})
                return
            description = str(body.get("description", "") or "").strip()

            # 1. Capture on the source node (existing behavior).
            source_result = fanout.node_passthrough(
                hostname,
                method="POST",
                path="/profile/save",
                body={"name": name, "description": description},
            )
            source_entry = (source_result.get("results") or [{}])[0]
            if source_entry.get("status") != "accepted":
                self._json_response(200, source_result)
                return

            # 2. Fetch the captured JSON from the source agent so we ship
            # the same bytes everywhere.
            source_node = registry.get_by_hostname(hostname)
            captured_json: dict[str, Any] | None = None
            if source_node is not None:
                try:
                    url = source_node.url.rstrip("/") + f"/profile?name={urllib.parse.quote(name)}"
                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        raw = resp.read().decode("utf-8", errors="replace")
                    parsed = json.loads(raw) if raw else None
                    if isinstance(parsed, dict):
                        captured_json = parsed
                except Exception as exc:
                    log.get().warning(
                        "[controller] /node/%s/profile/save: failed to fetch "
                        "captured JSON for propagation: %s",
                        hostname, exc,
                    )

            saved_on = [hostname]
            propagate_summary: dict[str, Any] = {
                "requested": 0, "succeeded": 0, "failed": 0,
                "skipped": 0, "timed_out": 0, "results": [],
            }
            controller_saved = False

            if captured_json is not None:
                # 3. Canonical copy on the controller. Best-effort.
                try:
                    ctrl_dir = cfgmod.PROFILES_DIR
                    ctrl_dir.mkdir(parents=True, exist_ok=True)
                    ctrl_path = ctrl_dir / f"{name}.json"
                    tmp = ctrl_path.with_suffix(ctrl_path.suffix + ".tmp")
                    with tmp.open("w", encoding="utf-8") as f:
                        json.dump(captured_json, f, indent=2)
                    tmp.replace(ctrl_path)
                    controller_saved = True
                    log.get().info(
                        "[controller] profile %r saved to controller copy at %s",
                        name, ctrl_path,
                    )
                except Exception as exc:
                    log.get().warning(
                        "[controller] failed to save controller copy of %r: %s",
                        name, exc,
                    )

                # 4. Fan the JSON out to every OTHER reachable agent.
                others = [n for n in registry.get_all() if n.hostname != hostname]
                if others:
                    propagate_summary = fanout._fanout(  # noqa: SLF001
                        others,
                        method="POST",
                        path="/profile/save_json",
                        body={"name": name, "json": captured_json},
                    )
                    for r in (propagate_summary.get("results") or []):
                        if r.get("status") == "accepted":
                            saved_on.append(r.get("hostname"))

            self._json_response(200, {
                "source": source_result,
                "propagated": propagate_summary,
                "controller_saved": controller_saved,
                "saved_on": saved_on,
            })

        # ----------------------------------------------------------------
        # Alert dismissal (HTMX swap — return empty fragment)
        # ----------------------------------------------------------------

        def _handle_alert_dismiss(self, alert_id: str) -> None:
            vm.dismiss_alert(alert_id)
            self._html_response(200, "")

        # ----------------------------------------------------------------
        # Push-registration
        # ----------------------------------------------------------------

        def _dispatch_delete(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            # DELETE /register/<host> is the operator's "remove this node
            # from the dashboard" button. Auth-gated — listeners can only
            # ADD themselves (POST /register), they cannot remove peers.
            if not authmod.request_is_authorized(self):
                return self._json_response(401, {
                    "error": "authentication required",
                    "hint": "set X-NVMON-Password header or POST /login",
                })

            m = re.fullmatch(rf"/register/({_HOSTNAME_RE})", path)
            if m:
                return self._handle_deregister(m.group(1))

            return self._json_response(404, {"error": "not found", "path": path})

        def _handle_register(self, body: dict[str, Any]) -> None:
            """POST /register — listener bootstrap. Persists + live registry."""
            hostname = str(body.get("hostname") or "").strip()
            url = str(body.get("url") or "").strip().rstrip("/")
            if not hostname or not url:
                return self._json_response(400, {
                    "error": "hostname and url are required",
                })

            # Subnet enforcement: with [network].subnet set, a listener may
            # only register an agent URL on that LAN, and only from that
            # LAN. A multi-NIC node whose default route is on another
            # network would otherwise register an address the controller
            # can't reach (or shouldn't use). Static [[controller.nodes]]
            # entries are unaffected — the operator typed those.
            subnet = cfgmod.get_network_config()["subnet"]
            if subnet and cfgmod.parse_subnet(subnet) is not None:
                agent_host = urlparse(url).hostname or ""
                peer_ip = (self.client_address[0] if self.client_address else "") or ""
                bad: list[str] = []
                if not cfgmod.ip_in_subnet(agent_host, subnet):
                    bad.append(f"agent url host {agent_host or '?'}")
                if peer_ip and not cfgmod.ip_in_subnet(peer_ip, subnet):
                    bad.append(f"request source {peer_ip}")
                if bad:
                    log.get().warning(
                        "[register] REJECTED %s: %s outside [network].subnet=%s",
                        hostname, " and ".join(bad), subnet,
                    )
                    return self._json_response(403, {
                        "error": (
                            f"{' and '.join(bad)} is outside the configured "
                            f"nvmon subnet {subnet}. Fix the node's NIC / routing, "
                            f"or set [network].subnet (or announce_ip) in its nvmon.toml."
                        ),
                        "hostname": hostname,
                        "subnet": subnet,
                    })

            entry = {
                "hostname": hostname,
                "url": url,
                "machine_id": str(body.get("machine_id") or "").strip(),
                "version": str(body.get("version") or "").strip(),
                "last_register": time.time(),
            }
            added, updated, skipped = registry.merge_registered([entry])
            _persist_registered()
            outcome = (
                "added" if added else
                "updated" if updated else
                "skipped"  # collided with config — operator wins
            )
            log.get().info(
                "[register] %s %s (%s) v%s",
                outcome, hostname, url, entry["version"] or "?",
            )
            return self._json_response(200, {
                "ok": True,
                "hostname": hostname,
                "outcome": outcome,
                "skipped_reason": "config_wins" if skipped else "",
            })

        def _handle_deregister(self, hostname: str) -> None:
            if not registry.remove_registered(hostname):
                return self._json_response(404, {
                    "error": "not found or not a registered entry",
                    "hostname": hostname,
                })
            _persist_registered()
            return self._json_response(200, {"ok": True, "removed": hostname})

        # ----------------------------------------------------------------
        # Request helpers
        # ----------------------------------------------------------------

        def _read_json_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _prefers_html(self) -> bool:
            accept = (self.headers.get("Accept") or "").lower()
            # If the client explicitly wants JSON, honor it.
            if "application/json" in accept and "text/html" not in accept:
                return False
            return "text/html" in accept or accept == "" or accept == "*/*"

        # ----------------------------------------------------------------
        # Response helpers
        # ----------------------------------------------------------------

        def _json_response(self, status: int, data: Any) -> None:
            payload = json.dumps(data, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _html_response(self, status: int, html_str: str) -> None:
            payload = html_str.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _redirect(self, location: str, *, extra_headers: list[tuple[str, str]] | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", location or "/")
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _redirect_to_login(self, from_path: str) -> None:
            # Minimal query-string escape.
            from urllib.parse import quote
            dest = f"/login?next={quote(from_path or '/', safe='/')}"
            self._redirect(dest)

        def _safe_error(self, status: int, message: str) -> None:
            try:
                self._json_response(status, {"error": message})
            except Exception:  # noqa: BLE001
                # Connection likely dead; nothing more we can do.
                pass

    return ControllerHandler


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

class ControllerServer:
    """Thin wrapper around ThreadingHTTPServer for clean start/stop."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        registry: NodeRegistry,
        aggregator: Aggregator,
        fanout: FanoutExecutor,
        view_model: "dash.DashboardViewModel | None" = None,
        disc_registry: Any = None,
        registered_file: Any = None,
        chainsync: Any = None,
    ) -> None:
        self._bind_host = bind_host
        self._port = int(port)
        self._view_model = view_model or dash.DashboardViewModel()
        handler_cls = make_handler(
            registry=registry,
            aggregator=aggregator,
            fanout=fanout,
            view_model=self._view_model,
            disc_registry=disc_registry,
            registered_file=registered_file,
            chainsync=chainsync,
        )
        self._httpd: ThreadingHTTPServer | None = ThreadingHTTPServer(
            (bind_host, self._port),
            handler_cls,
        )
        self._thread: threading.Thread | None = None

    @property
    def bind_address(self) -> tuple[str, int]:
        return (self._bind_host, self._port)

    @property
    def port(self) -> int:
        """The port actually bound (differs from the requested one for 0)."""
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._port

    def serve_forever_in_thread(self) -> None:
        """Run the HTTP server in a background thread."""
        if self._httpd is None:
            raise RuntimeError("server has been shut down")
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="nvmon-http",
            daemon=True,
        )
        self._thread.start()
        log.get().info(
            "[controller] HTTP server listening on http://%s:%d",
            self._bind_host,
            self._port,
        )

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop the HTTP server and close the socket."""
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception as exc:  # noqa: BLE001
            log.get().warning("[controller] shutdown error: %s", exc)
        finally:
            self._httpd = None
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        log.get().info("[controller] HTTP server stopped")
