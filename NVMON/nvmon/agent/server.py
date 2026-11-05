"""HTTP daemon — `nvmon agent` listens on `agent.port` (default 9998).

Routes (read-only unless noted):

    GET  /health                    — identity + capabilities
    GET  /status                    — full state snapshot
    GET  /gpu                       — GPU slice
    GET  /displays                  — DD slice
    GET  /mosaic                    — Mosaic slice
    GET  /qsync                     — QSync slice
    GET  /edid                      — EDID snapshot
    GET  /profiles                  — list profile names
    GET  /validate                  — run validator against active profile
    GET  /                          — single-host HTML dashboard (debug)

    POST /apply    {"profile": ...} — apply a profile (async, 202)
    POST /restore                   — restore to desktop (async, 202)
    POST /dd/enable                 — remove displays from desktop
    POST /dd/disable                — return displays to desktop
    POST /qsync/enable              — {source?, trigger?, grid?, displays?, role?}
                                       grid omitted -> auto-discover displays
                                       displays: [[gpu,disp], ...] explicit
                                       role: "server" (default) | "client"
    POST /qsync/enable_from_profile — {"profile": "<name>"}; loads the profile
                                       and dispatches the QSync apply path
                                       (honors captured role/displays/source,
                                       disable→enable→verify→retry-on-drift)
    POST /qsync/verify              — {"profile": "<name>"}; read-only drift
                                       check vs profile. Returns {drift, ok}.
    POST /qsync/disable
    POST /mosaic/disable
    POST /system/restart            — queue OS restart (delay_seconds=8) — 202
    POST /system/restart_listener   — relaunch the listener via Scheduled Task,
                                       no OS reboot. delay_seconds default 2.
    POST /system/shutdown           — queue OS shutdown (delay_seconds=8) — 202
    POST /system/abort              — cancel a queued shutdown/restart

Every POST is gated behind `config.is_admin()` — non-admin gets 403.
Every subsystem route returns 503 if the subsystem's capability reports
`available=False`.

The monitor runs in a daemon thread spawned at boot. The HTTP handler reads
from the monitor's in-memory cache when available, falls back to on-demand
subsystem queries otherwise.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from .. import config, log
from .. import __version__
from . import (
    dashboard_single, displays, edid, gpu, mosaic, monitor as monitor_mod,
    nvapi as nvapi_mod, profile_capture, profiles, qsync,
    system as system_mod, validator,
)


# ---------------------------------------------------------------------------
# Capability assembly
# ---------------------------------------------------------------------------

def _build_capabilities() -> dict:
    """Compose the `/health.capabilities` dict from every subsystem."""
    return {
        "gpu": gpu.capability(),
        "displays": displays.capability(),
        "mosaic": mosaic.capability(),
        "qsync": qsync.capability(),
        "edid": edid.capability(),
        "color": nvapi_mod.capability(),
    }


# ---------------------------------------------------------------------------
# Threading HTTP server — one thread per request so slow callers (e.g. the
# controller doing a fan-out) don't block the dashboard.
# ---------------------------------------------------------------------------

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class AgentHandler(BaseHTTPRequestHandler):
    """nvmon agent request handler."""

    # Populated by `serve()` at boot.
    monitor: monitor_mod.Monitor | None = None
    bound_port: int = 0

    # --- logging override: route HTTP access logs through our logger ---

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        try:
            log.get().debug(f"HTTP {self.address_string()} - {format % args}")
        except Exception:
            pass

    # --- dispatch ---

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/")

        if path in ("", "/", "/dashboard"):
            # Locked-down agent: the full HTML dashboard is gone — the
            # controller is now the canonical UI. Anyone who hits the bare
            # agent URL gets a 1-line stub pointing at the controller and
            # the JSON endpoints (still useful for debugging/curl).
            return self._handle_locked_dashboard()

        routes = {
            "/health": self._handle_health,
            "/status": self._handle_status,
            "/gpu": self._handle_gpu,
            "/displays": self._handle_displays,
            "/displays/raw": self._handle_displays_raw,
            "/mosaic": self._handle_mosaic,
            "/mosaic/raw": self._handle_mosaic_raw,
            "/qsync": self._handle_qsync,
            "/qsync/raw": self._handle_qsync_raw,
            "/edid": self._handle_edid,
            "/edid/raw": self._handle_edid_raw,
            "/profiles": self._handle_profiles,
            "/profile": self._handle_profile_get,
            "/validate": self._handle_validate,
            "/apply/status": self._handle_apply_status,
            "/mosaic/timings": self._handle_mosaic_timings,
            "/displays/signals": self._handle_displays_signals,
            "/displays/color": self._handle_displays_color,
            "/timing": self._handle_timing,
            "/logs": self._handle_logs,
        }
        handler = routes.get(path)
        if handler is None:
            # Distinguish "wrong method" from "no such endpoint" so the
            # operator hitting /qsync/enable in a browser doesn't get
            # confused by a 404 -- they get a clear "POST only" message.
            post_only = {
                "/apply", "/apply/cancel", "/restore",
                "/dd/enable", "/dd/disable",
                "/qsync/enable", "/qsync/disable",
                "/qsync/enable_from_profile",
                "/qsync/verify",
                "/mosaic/disable",
                "/profile/save",
                "/system/restart", "/system/restart_listener",
                "/system/shutdown", "/system/abort",
            }
            if path in post_only:
                return self._json_response(405, {
                    "error": "method not allowed",
                    "path": path,
                    "hint": f"this is a POST-only endpoint; "
                            f"use: curl -X POST http://<host>:9998{path}",
                })
            return self._json_response(404, {"error": "Not found", "path": path})
        return handler()

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")

        # Body parse (JSON, best-effort)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        body_data: dict = {}
        if body:
            try:
                body_data = json.loads(body)
                if not isinstance(body_data, dict):
                    body_data = {}
            except json.JSONDecodeError:
                body_data = {}

        routes = {
            "/apply": self._handle_apply,
            "/apply/cancel": self._handle_apply_cancel,
            "/restore": self._handle_restore,
            "/dd/enable": self._handle_dd_enable,
            "/dd/disable": self._handle_dd_disable,
            "/qsync/enable": self._handle_qsync_enable,
            "/qsync/enable_from_profile": self._handle_qsync_enable_from_profile,
            "/qsync/verify": self._handle_qsync_verify,
            "/qsync/disable": self._handle_qsync_disable,
            "/mosaic/disable": self._handle_mosaic_disable,
            "/profile/save": self._handle_profile_save,
            "/profile/save_json": self._handle_profile_save_json,
            "/timing/custom": self._handle_timing_custom,
            "/timing/revert": self._handle_timing_revert,
            "/system/restart": self._handle_system_restart,
            "/system/restart_listener": self._handle_system_restart_listener,
            "/system/shutdown": self._handle_system_shutdown,
            "/system/abort": self._handle_system_abort,
        }
        handler = routes.get(path)
        if handler is None:
            return self._json_response(404, {"error": "Not found", "path": path})
        return handler(body_data)

    # --- GET handlers ---

    def _handle_health(self) -> None:
        ip = config.get_local_ip()
        subnet = config.get_network_config()["subnet"]
        # subnet_ok: None = no [network].subnet configured (no opinion);
        # True = the IP we announce sits inside the pinned subnet; False =
        # this box has no address on the nvmon LAN and is announcing some
        # other NIC. The dashboard raises a banner on False.
        subnet_ok: bool | None = None
        if subnet:
            subnet_ok = bool(config.parse_subnet(subnet)) and config.ip_in_subnet(ip, subnet)
        self._json_response(200, {
            "ok": True,
            "version": __version__,
            "hostname": config.get_hostname(),
            "ip": ip,
            "is_admin": config.is_admin(),
            "subnet": subnet,
            "subnet_ok": subnet_ok,
            "capabilities": _build_capabilities(),
        })

    def _handle_status(self) -> None:
        """Full status. Prefer the monitor's in-memory snapshot; otherwise
        fall back to a fresh poll."""
        data = None
        if self.monitor is not None:
            data = self.monitor.last_status()

        if data is None:
            # Fresh on-demand build
            try:
                data = self.monitor.poll_once() if self.monitor else _build_adhoc_status()
            except Exception as exc:
                return self._json_response(500, {"error": str(exc)})

        # How old this snapshot is. During an apply the monitor keeps its
        # last snapshot (it must not overlap the apply's NVIDIA calls), so
        # the dashboard needs this to show "stale Ns" instead of a frozen
        # reading that looks live.
        data = dict(data)
        job = profiles.get_last_result()
        data["status_age_s"] = _status_age_seconds(data.get("timestamp"))
        data["job_active"] = bool(job.get("in_progress"))
        # Compact apply/restore job state so the controller dashboard can
        # show "Applying X — [4/6] Configuring Mosaic" + Cancel, and the
        # last outcome, without a second round-trip per node.
        data["job"] = {k: job.get(k) for k in (
            "in_progress", "action", "profile", "step", "started_at",
            "finished_at", "success", "message", "cancelled",
        )}
        self._json_response(200, data)

    def _handle_gpu(self) -> None:
        cap = gpu.capability()
        if not cap["available"]:
            return self._json_response(503, {
                "error": "gpu subsystem unavailable",
                "reason": cap.get("reason", ""),
            })
        gpus = gpu.status()
        self._json_response(200, {
            "available": True,
            "count": len(gpus),
            "gpus": [_gpu_to_dict(g) for g in gpus],
        })

    def _handle_displays(self) -> None:
        cap = displays.capability()
        if not cap["available"]:
            return self._json_response(503, {
                "error": "displays subsystem unavailable",
                "reason": cap.get("reason", ""),
            })
        state = displays.status()
        self._json_response(200, _dd_to_dict(state))

    def _handle_mosaic(self) -> None:
        cap = mosaic.capability()
        if not cap["available"]:
            return self._json_response(503, {
                "error": "mosaic subsystem unavailable",
                "reason": cap.get("reason", ""),
            })
        state = mosaic.status()
        self._json_response(200, _mosaic_to_dict(state))

    def _handle_qsync(self) -> None:
        cap = qsync.capability()
        if not cap["available"]:
            return self._json_response(503, {
                "error": "qsync subsystem unavailable",
                "reason": cap.get("reason", ""),
            })
        state = qsync.status()
        self._json_response(200, _qsync_to_dict(state))

    def _handle_edid(self) -> None:
        cap = edid.capability()
        if not cap["available"]:
            return self._json_response(503, {
                "error": "edid subsystem unavailable",
                "reason": cap.get("reason", ""),
            })
        self._json_response(200, edid.status())

    def _handle_edid_raw(self) -> None:
        """GET /edid/raw — every panel EDID Windows has on file, decoded
        (manufacturer, name, preferred mode, bit depth, encodings, CTA
        flags). ``?blob=1`` adds the bytes as base64; ``?name=BRT`` keeps
        only instances whose registry id or monitor name contains it."""
        query = parse_qs(urlparse(self.path).query)
        want_blob = (query.get("blob", ["0"])[0] or "0") not in ("0", "", "false")
        name_filter = (query.get("name", [""])[0] or "").strip().lower()
        try:
            rows = edid.describe_active_edids(include_blob=want_blob)
        except Exception as exc:  # noqa: BLE001
            log.get().error(f"/edid/raw failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": str(exc)})
        if name_filter:
            rows = [r for r in rows if name_filter in (r.get("instance", "") + " " + r.get("monitor_name", "")).lower()]
        self._json_response(200, {"count": len(rows), "edids": rows})

    def _handle_profiles(self) -> None:
        self._json_response(200, {"profiles": profiles.list_profiles()})

    def _handle_profile_get(self) -> None:
        """GET /profile?name=<name> — return the raw JSON of a saved profile.

        Used by the controller during "Save as profile" to read back the
        captured JSON from the source node and propagate the same content
        to every other agent. Read-only; no admin gate.
        """
        from urllib.parse import parse_qs
        query = urlparse(self.path).query
        name = (parse_qs(query).get("name") or [""])[0].strip()
        if not name:
            return self._json_response(400, {"error": "missing 'name' query param"})
        try:
            data = profiles.show(name)
        except FileNotFoundError:
            return self._json_response(404, {
                "error": f"Profile '{name}' not found",
                "available": profiles.list_profiles(),
            })
        except Exception as exc:
            log.get().error(f"/profile read failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": str(exc)})
        self._json_response(200, data)

    def _handle_apply_status(self) -> None:
        """Most recent apply/restore outcome.

        Polled by the controller after dispatching /apply so the dashboard
        modal can show truthful per-node results (success / "nothing to
        apply" / "another apply in progress" / specific subsystem failure)
        instead of just the dispatch-only `accepted` it used to surface.
        """
        self._json_response(200, profiles.get_last_result())

    def _handle_mosaic_timings(self) -> None:
        """Dry-run a Mosaic grid with ``configureMosaic test`` (nothing applied).

            GET /mosaic/timings                      -> the active profile's grid + mode
            GET /mosaic/timings?profile=X            -> that profile's grid + mode
            GET /mosaic/timings?rows=2&cols=2&res=3840,2160,30

        Returns {command, success, valid, rate_hz, output}. ``rate_hz`` is
        the rate the driver would actually pick (asked 30 -> 29.97 on
        panels without a true-30 timing). The controller calls this on
        every target before an Apply-All so a wall that would come up at
        the wrong rate fails the preflight in seconds, before anything
        is blanked.
        """
        from . import mosaic as mosaic_mod

        query = parse_qs(urlparse(self.path).query)
        name = (query.get("profile", [""])[0] or "").strip()
        if not name and not query.get("rows"):
            name = self.monitor.profile_name if self.monitor else ""
        try:
            if name:
                try:
                    profile = profiles.load(name)
                except FileNotFoundError:
                    return self._json_response(404, {"error": f"Profile '{name}' not found"})
                data = mosaic_mod.test_timings_from_profile(profile)
                data["profile"] = name
            else:
                rows = int(query.get("rows", ["1"])[0])
                cols = int(query.get("cols", ["1"])[0])
                res = (query.get("res", [""])[0] or "").split(",")
                width = int(res[0]) if len(res) >= 2 else 0
                height = int(res[1]) if len(res) >= 2 else 0
                rate = res[2] if len(res) >= 3 else ""
                data = mosaic_mod.test_timings(
                    rows, cols, outputs=query.get("out", [""])[0].replace(";", " "),
                    width=width, height=height, refresh_rate=rate,
                )
        except Exception as exc:
            log.get().error(f"/mosaic/timings failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": str(exc)})
        self._json_response(200, data)

    def _handle_displays_signals(self) -> None:
        """Windows CCD view of every active path: desktop vs signal on the wire."""
        from . import ccd
        self._json_response(200, {"signals": ccd.describe()})

    def _handle_displays_color(self) -> None:
        """GET /displays/color — NVAPI colour depth / pixel encoding /
        colorimetry per display (what NVCP "Use NVIDIA color settings"
        shows) plus the driver's own default for that display.

            GET /displays/color                      -> current + default
            GET /displays/color?probe=12             -> also: is 12-bit (current
                                                        encoding) supported?
            GET /displays/color?probe=12,Ycc422,BT709
        Nothing is changed by a probe (IS_SUPPORTED_COLOR only).
        """
        query = parse_qs(urlparse(self.path).query)
        probe_text = (query.get("probe", [""])[0] or "").strip()
        payload: dict = {"available": nvapi_mod.is_available(), "displays": []}
        if probe_text:
            payload["probe"] = probe_text
        if not payload["available"]:
            payload["error"] = nvapi_mod.capability().get("reason", "NVAPI unavailable on this host")
            return self._json_response(200, payload)
        try:
            probe = nvapi_mod.parse_probe(probe_text) if probe_text else None
            payload["displays"] = nvapi_mod.describe_displays(self._timing_display_ids(), probe=probe)
        except Exception as exc:  # noqa: BLE001
            log.get().error(f"/displays/color failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": str(exc)})
        self._json_response(200, payload)

    # --- exact timings (NVAPI custom displays) ---------------------------

    def _timing_display_ids(self, body: dict | None = None) -> list[tuple[int, str]]:
        """(display_id_int, hex) for the requested displays, default: every
        display configureDdisplay lists with an NVAPI id."""
        from . import timing as timing_mod
        wanted = (body or {}).get("displays") if body else None
        if isinstance(wanted, list) and wanted:
            return [(timing_mod.parse_display_id(w), f"{timing_mod.parse_display_id(w):x}") for w in wanted]
        out: list[tuple[int, str]] = []
        for d in (displays.status().displays or []):
            if d.id and d.id.lower() not in ("", "<none>", "none"):
                try:
                    out.append((timing_mod.parse_display_id(d.id), d.id.lower()))
                except ValueError:
                    continue
        return out

    def _handle_timing(self) -> None:
        """GET /timing — the exact timing every panel runs now (NvAPI_DISP_GetTiming),
        the custom timings stored for it, and nvmon's designed sync-friendly
        timings for the integer rates at that resolution.

            GET /timing            -> all displays
            GET /timing?rates=24,30 -> designs for those rates (default 24,25,30)
        """
        from . import timing as timing_mod
        query = parse_qs(urlparse(self.path).query)
        rates_q = (query.get("rates", ["24,25,30"])[0] or "").split(",")
        payload: dict = {"available": timing_mod.is_available(), "displays": [], "designs": {}}
        if not payload["available"]:
            payload["error"] = "NVAPI unavailable on this host"
            return self._json_response(200, payload)
        res_seen: set[tuple[int, int]] = set()
        try:
            live = timing_mod.live_timings()
        except Exception:  # noqa: BLE001
            live = {}
        for did, hexid in self._timing_display_ids():
            entry: dict = {"display": hexid}
            try:
                pclk, ht, vt = live.get(did, (0, 0, 0))
                cur = timing_mod.current_timing(did, pclk_hz=pclk, h_total=ht, v_total=vt)
                entry["current"] = cur.to_dict()
                entry["current"]["validate"] = cur.validate()
                entry["driver_record"] = timing_mod.get_timing(did).to_dict()
                res_seen.add((cur.h_visible, cur.v_visible))
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)
            try:
                entry["customs"] = [t.to_dict() for t in timing_mod.enum_custom(did)]
            except Exception as exc:  # noqa: BLE001
                entry["customs_error"] = str(exc)
            payload["displays"].append(entry)
        for (w, h) in sorted(res_seen) or [(3840, 2160)]:
            for r in rates_q:
                try:
                    rate = float(r)
                    payload["designs"][f"{w}x{h}@{rate:g}"] = timing_mod.design_timing(w, h, rate).to_dict()
                except ValueError as exc:
                    payload["designs"][f"{w}x{h}@{r}"] = {"error": str(exc)}
        self._json_response(200, payload)

    def _handle_timing_custom(self, body: dict) -> None:
        """POST /timing/custom — create and apply an exact timing (NVCP "Create
        Custom Resolution", programmatically).

        Body: {"displays": ["80061085", ...]   (default: all panels)
               "rate": 24                        -> nvmon designs the timing, or
               "timing": {h_total, v_total, pclk_hz, ...} -> use exactly this,
               "save": true}                     (false = trial only, auto-revert on mismatch)
        Applies immediately via NvAPI_DISP_TryCustomDisplay, reads back the
        programmed timing, saves it when it matches, reverts when it does not.
        """
        from . import timing as timing_mod
        if not self._require_admin():
            return
        if not timing_mod.is_available():
            return self._json_response(503, {"error": "NVAPI unavailable on this host"})
        ids = self._timing_display_ids(body)
        if not ids:
            return self._json_response(400, {"error": "no displays"})
        try:
            if isinstance(body.get("timing"), dict):
                tm = timing_mod.Timing.from_dict(body["timing"])
                tm.source = tm.source or "operator"
            else:
                rate = float(body.get("rate") or 0)
                if rate <= 0:
                    return self._json_response(400, {"error": "give 'rate' or 'timing'"})
                first = timing_mod.get_timing(ids[0][0])
                tm = timing_mod.design_timing(first.h_visible, first.v_visible, rate,
                                              v_total=body.get("v_total"), h_total=body.get("h_total"))
        except (ValueError, timing_mod.TimingUnavailable) as exc:
            return self._json_response(400, {"error": str(exc)})
        save = bool(body.get("save", True))
        log.get().info(f"/timing/custom: {tm.to_dict()} on {[h for _, h in ids]} save={save}")
        try:
            result = timing_mod.apply_custom_timing([d for d, _ in ids], tm, save=save)
        except (ValueError, timing_mod.TimingUnavailable) as exc:
            log.get().error(f"/timing/custom failed: {exc}")
            return self._json_response(500, {"error": str(exc), "timing": tm.to_dict()})
        after = []
        for did, hexid in ids:
            try:
                after.append({"display": hexid, **timing_mod.get_timing(did).to_dict()})
            except Exception as exc:  # noqa: BLE001
                after.append({"display": hexid, "error": str(exc)})
        code = 200 if not result.get("mismatch") else 409
        self._json_response(code, {"requested": tm.to_dict(), "result": result, "after": after})

    def _handle_timing_revert(self, body: dict) -> None:
        """POST /timing/revert — undo a custom-display trial on the given displays."""
        from . import timing as timing_mod
        if not self._require_admin():
            return
        ids = self._timing_display_ids(body)
        try:
            timing_mod.revert_trial([d for d, _ in ids])
        except timing_mod.TimingUnavailable as exc:
            return self._json_response(500, {"error": str(exc)})
        self._json_response(200, {"reverted": [h for _, h in ids]})

    def _handle_validate(self) -> None:
        profile = None
        name = self.monitor.profile_name if self.monitor else ""
        if name:
            try:
                profile = profiles.load(name)
            except FileNotFoundError:
                profile = None

        dd_state = displays.status()
        mosaic_state = mosaic.status()
        qsync_state = qsync.status()
        gpu_list = gpu.status()

        result = validator.validate_all(
            dd=dd_state, mosaic=mosaic_state, qsync=qsync_state,
            gpus=gpu_list, profile=profile,
        )
        self._json_response(200, {
            "active_profile": name,
            "all_passed": result.all_passed,
            "checks": {
                k: {"passed": v.passed, "detail": v.detail}
                for k, v in result.checks.items()
            },
        })

    def _handle_locked_dashboard(self) -> None:
        """Stub page for direct agent visits.

        Lists the JSON endpoints the agent serves and points at the
        controller for the unified dashboard. No telemetry leaks here —
        the JSON endpoints (/status, /gpu, etc.) are still open per the
        trusted-LAN threat model, but the operator UX lives on the
        controller. The user explicitly asked for this lockdown so a
        casual browser at IP:9998 doesn't see all the data.
        """
        host = config.get_hostname()
        ip = config.get_local_ip()
        is_admin = config.is_admin()
        admin_banner = (
            "<div style='margin-bottom:14px;padding:10px 14px;background:#3d1a1a;"
            "border-left:3px solid #ff4444;color:#ffaaaa;font-size:13px;font-family:Consolas,monospace;'>"
            "&#9888; AGENT IS NOT RUNNING AS ADMINISTRATOR &mdash; QSync and DD "
            "queries will fail. Restart via run_listener.bat (auto-elevates)."
            "</div>"
            if not is_admin else ""
        )
        # Favicon inlined as a data URL — the agent has no static-file route,
        # and adding one just for a 600-byte SVG would balloon the surface area.
        # Source of truth: NVMON/nvmon/controller/dashboard_unified/static/img/nvmon-favicon.svg
        favicon_svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 180 180'>"
            "<polygon points='90,28 144,59 144,121 90,152 36,121 36,59' "
            "fill='%230A0A12' stroke='%234A90E2' stroke-width='6' stroke-linejoin='round'/>"
            "<g fill='none' stroke='%234A90E2' stroke-width='3' stroke-linejoin='round'>"
            "<path d='M 90 90 L 71 45 L 49 63 Z'/>"
            "<path d='M 90 90 L 43 76 L 43 104 Z'/>"
            "<path d='M 90 90 L 71 135 L 49 117 Z'/>"
            "</g>"
            "<path d='M 90 90 L 138 70 L 138 110 Z' fill='%232DD4BF' fill-opacity='0.6'/>"
            "<circle cx='90' cy='90' r='9' fill='%23FFFFFF'/>"
            "</svg>"
        )
        favicon_href = "data:image/svg+xml;utf8," + favicon_svg
        body = (
            "<!doctype html>\n"
            "<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>nvmon agent</title>"
            f"<link rel=\"icon\" type=\"image/svg+xml\" href=\"{favicon_href}\">"
            "<style>"
            "body{font-family:system-ui,Segoe UI,sans-serif;background:#0a0a0a;color:#ddd;"
            "margin:0;padding:32px;line-height:1.5;}"
            "h1{color:#76b900;margin:0 0 6px 0;font-size:18px;}"
            ".sub{color:#888;font-size:13px;margin-bottom:18px;font-family:Consolas,monospace;}"
            "code{background:#1a1a1a;padding:2px 6px;border-radius:3px;color:#9cf;}"
            "ul{font-size:12px;font-family:Consolas,monospace;color:#aaa;}"
            "li{margin-bottom:3px;}"
            ".note{margin-top:18px;padding:10px 14px;background:#152a05;border-left:3px solid #76b900;"
            "font-size:13px;}"
            "</style></head><body>"
            f"<h1>nvmon agent <span style=\"color:#888;font-size:13px;font-weight:400;\">v{__version__}</span></h1>"
            f"<div class=\"sub\">{host} &middot; {ip}:{self.bound_port} &middot; "
            f"admin: {'YES' if is_admin else 'NO'}</div>"
            f"{admin_banner}"
            "<div class=\"note\">"
            "This is a JSON-only nvmon agent. The full dashboard lives on the "
            "controller (see <code>nvmon.toml</code> for the address)."
            "</div>"
            "<p style=\"font-size:13px;color:#888;margin-top:18px;\">JSON endpoints (curl or HTTP client only):</p>"
            "<ul>"
            "<li><code>GET /health</code> &mdash; identity + capabilities</li>"
            "<li><code>GET /status</code> &mdash; full snapshot</li>"
            "<li><code>GET /gpu</code> &middot; <code>/displays</code> &middot; "
            "<code>/mosaic</code> &middot; <code>/qsync</code> &middot; "
            "<code>/edid</code></li>"
            "<li><code>GET /profiles</code> &middot; <code>/validate</code> &middot; "
            "<code>/logs</code></li>"
            "<li><code>POST /apply {profile}</code> &middot; <code>/restore</code> &middot; "
            "<code>/profile/save {name}</code></li>"
            "<li><code>POST /dd/enable</code> &middot; <code>/dd/disable</code> &middot; "
            "<code>/qsync/enable</code> &middot; "
            "<code>/qsync/enable_from_profile {profile}</code> &middot; "
            "<code>/qsync/verify {profile}</code> &middot; "
            "<code>/qsync/disable</code> &middot; "
            "<code>/mosaic/disable</code></li>"
            "</ul>"
            "</body></html>"
        )
        self._html_response(200, body)

    # ---- Raw CLI dumps ----------------------------------------------------
    # Re-run the underlying NVIDIA CLI command and return both the raw
    # stdout/stderr AND what our parser made of it.  Used to diagnose
    # cases where the parser disagrees with the NVIDIA Control Panel —
    # the operator pulls the raw dump, sees the actual output format the
    # tool emits, and we can update the parser to match.
    # ----------------------------------------------------------------------

    def _qsync_raw_payload(self) -> dict:
        from .. import config as _cfg, executors as _ex
        from ..parsers import parse_qsync_query_all
        exe = _cfg.get_qsync_exe()
        if not exe:
            return {
                "available": False,
                "reason": _cfg.missing_exe_reason("configureQsync2.exe"),
                "command": "", "stdout": "", "stderr": "",
                "return_code": -1, "duration_ms": 0,
                "parsed": None,
            }
        result = _ex.run(exe, ["queryAll"], label="qsync-queryAll-raw", quiet=True)
        try:
            parsed = parse_qsync_query_all(result.stdout + "\n" + result.stderr)
            parsed_view = _qsync_to_dict(parsed)
        except Exception as exc:
            parsed_view = {"_parse_error": str(exc)}
        return {
            "available": True,
            "command": result.command,
            "return_code": result.return_code,
            "duration_ms": result.duration_ms,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "parsed": parsed_view,
        }

    def _displays_raw_payload(self) -> dict:
        from .. import config as _cfg, executors as _ex
        from ..parsers import parse_dd_status
        exe = _cfg.get_dd_exe()
        if not exe:
            return {
                "available": False,
                "reason": _cfg.missing_exe_reason("configureDdisplay.exe"),
                "command": "", "stdout": "", "stderr": "",
                "return_code": -1, "duration_ms": 0,
                "parsed": None,
            }
        result = _ex.run(exe, ["status", "--all"], label="dd-status-raw", quiet=True)
        try:
            parsed = parse_dd_status(result.stdout + "\n" + result.stderr)
            parsed_view = _dd_to_dict(parsed)
        except Exception as exc:
            parsed_view = {"_parse_error": str(exc)}
        return {
            "available": True,
            "command": result.command,
            "return_code": result.return_code,
            "duration_ms": result.duration_ms,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "parsed": parsed_view,
        }

    def _mosaic_raw_payload(self) -> dict:
        from .. import config as _cfg, executors as _ex
        from ..parsers import parse_mosaic_query
        exe = _cfg.get_mosaic_exe()
        if not exe:
            return {
                "available": False,
                "reason": _cfg.missing_exe_reason("configureMosaic.exe"),
                "command": "", "stdout": "", "stderr": "",
                "return_code": -1, "duration_ms": 0,
                "parsed": None,
            }
        # Run both 'query' and 'query current' so the operator sees what
        # the parser fed off — falls back to XML when the text query is empty.
        r1 = _ex.run(exe, ["query"], label="mosaic-query-raw", quiet=True)
        r2 = _ex.run(exe, ["query", "current"], label="mosaic-query-current-raw", quiet=True)
        try:
            parsed = parse_mosaic_query(r1.stdout + "\n" + r1.stderr)
            if not parsed.enabled:
                parsed = parse_mosaic_query(r2.stdout + "\n" + r2.stderr)
            parsed_view = _mosaic_to_dict(parsed)
        except Exception as exc:
            parsed_view = {"_parse_error": str(exc)}
        return {
            "available": True,
            "command": r1.command,
            "return_code": r1.return_code,
            "duration_ms": r1.duration_ms,
            "stdout": r1.stdout,
            "stderr": r1.stderr,
            "command_xml": r2.command,
            "stdout_xml": r2.stdout,
            "stderr_xml": r2.stderr,
            "parsed": parsed_view,
        }

    def _handle_qsync_raw(self) -> None:
        try:
            self._json_response(200, self._qsync_raw_payload())
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    def _handle_displays_raw(self) -> None:
        try:
            self._json_response(200, self._displays_raw_payload())
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    def _handle_mosaic_raw(self) -> None:
        try:
            self._json_response(200, self._mosaic_raw_payload())
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    def _handle_logs(self) -> None:
        """Tail the agent's daily log file. Plain text. Best-effort.

        Query: ?lines=N (default 200, max 2000).
        """
        from urllib.parse import parse_qs as _pq
        params = _pq(urlparse(self.path).query or "")
        try:
            lines = int((params.get("lines") or ["200"])[0])
        except (TypeError, ValueError):
            lines = 200
        lines = max(1, min(lines, 2000))

        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        log_path = config.LOGS_DIR / f"nvmon-{today}.log"
        if not log_path.is_file():
            self._html_response(200, "(no log file yet)")
            return
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                # Read all then tail — log files here are bounded by daily rollover.
                buf = f.readlines()
            tail = "".join(buf[-lines:])
        except Exception as exc:
            return self._json_response(500, {"error": str(exc)})

        payload = tail.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    # --- POST handlers ---

    def _handle_apply(self, body: dict) -> None:
        if not self._require_admin():
            return
        profile_name = str(body.get("profile", "")).strip()
        if not profile_name:
            return self._json_response(400, {"error": "Missing 'profile' in request body"})

        # Check profile exists before accepting
        if profile_name not in profiles.list_profiles():
            return self._json_response(404, {
                "error": f"Profile '{profile_name}' not found",
                "available": profiles.list_profiles(),
            })

        # Capture operator INTENT immediately — write the active_profile
        # snapshot (full profile dict + EDID bytes inlined) before the
        # pipeline runs. Why: the snapshot IS the boot-apply contract.
        # Writing it on entry means: regardless of whether THIS apply
        # takes, the next listener boot reads the snapshot, the validator
        # checks for drift, and the pipeline self-heals to the operator's
        # last expressed intent. A partial failure / rollback / transient
        # hardware glitch no longer loses the choice across a reboot.
        try:
            profiles.snapshot_active(profile_name)
        except Exception as exc:
            # Snapshot failure is non-fatal for the running apply, but we
            # log it loudly — the operator will discover that boot-apply
            # is broken only on the next listener restart, much later.
            log.get().error(
                f"/apply: snapshot_active({profile_name!r}) failed: {exc}",
                exc_info=True,
            )

        # Update in-memory state too so /status.active_profile is right
        # immediately (the validator runs against this on the next poll).
        if self.monitor is not None:
            self.monitor.set_profile(profile_name)

        # Fire-and-forget background apply; caller polls /validate or /status
        def _bg() -> None:
            try:
                success, msg = profiles.apply(profile_name)
                log.get().info(f"/apply background result: {msg}")
            except Exception as exc:
                log.get().error(f"/apply background error: {exc}", exc_info=True)

        threading.Thread(target=_bg, daemon=True, name="nvmon-apply").start()
        self._json_response(202, {
            "status": "accepted", "profile": profile_name,
            "preempting": profiles.get_last_result().get("in_progress", False),
        })

    def _handle_apply_cancel(self, body: dict) -> None:
        """Cancel the running apply / restore / boot-apply at its next wait
        or command. 200 when something was running, 409 otherwise."""
        if not self._require_admin():
            return
        cancelling = profiles.cancel_current()
        self._json_response(200 if cancelling else 409, {
            "cancelling": cancelling,
            "job": profiles.get_last_result(),
        })

    def _handle_restore(self, body: dict) -> None:
        if not self._require_admin():
            return

        # Clear the boot-apply contract immediately. Restore means "go
        # back to desktop and STAY there across a reboot" — the operator
        # is explicitly walking away from any previously-applied profile.
        # If we left active_profile.json in place, the next listener
        # boot would re-apply it via _maybe_apply_on_boot and undo the
        # restore. Doing this on entry (not on success) so even a partial
        # restore doesn't leave a poisoned snapshot behind.
        try:
            profiles.clear_active()
        except Exception as exc:
            log.get().warning(f"/restore: clear_active failed: {exc}")

        if self.monitor is not None:
            # In-memory only — there is no "desktop" profile JSON, so the
            # validator stops checking against any profile until the next
            # /apply. /status.active_profile reads "" until then.
            self.monitor.set_profile("")

        def _bg() -> None:
            try:
                success, msg = profiles.restore()
                log.get().info(f"/restore background result: {msg}")
            except Exception as exc:
                log.get().error(f"/restore background error: {exc}", exc_info=True)

        threading.Thread(target=_bg, daemon=True, name="nvmon-restore").start()
        self._json_response(202, {"status": "accepted", "action": "restore"})

    # Subsystem control endpoints all run in a background thread so the
    # HTTP handler returns 202 immediately. Why: a synchronous shell-out
    # to configureQsync2.exe + queryAll can take several seconds, longer
    # than the controller's 15s fanout timeout in pathological cases.
    # While the handler is blocked, the next aggregator /health poll on
    # the same agent also queues behind it — the dashboard then flickers
    # OFFLINE for the operator. Async pattern (see _handle_apply) avoids
    # all of that. The operator polls /status / /validate to see results.

    def _handle_dd_enable(self, body: dict) -> None:
        if not self._subsystem_available(displays.capability(), "displays"):
            return
        if not self._require_admin():
            return
        mode = str(body.get("mode", "index"))
        try:
            index = int(body.get("index", 1))
        except (TypeError, ValueError):
            index = 1

        def _bg() -> None:
            try:
                result = displays.enable(mode=mode, index=index)
                log.get().info(f"/dd/enable background result: success={result.success}")
            except Exception as exc:
                log.get().error(f"/dd/enable background error: {exc}", exc_info=True)

        threading.Thread(target=_bg, daemon=True, name="nvmon-dd-enable").start()
        self._json_response(202, {"status": "accepted", "action": "dd_enable"})

    def _handle_dd_disable(self, body: dict) -> None:
        if not self._subsystem_available(displays.capability(), "displays"):
            return
        if not self._require_admin():
            return

        def _bg() -> None:
            try:
                result = displays.disable()
                log.get().info(f"/dd/disable background result: success={result.success}")
            except Exception as exc:
                log.get().error(f"/dd/disable background error: {exc}", exc_info=True)

        threading.Thread(target=_bg, daemon=True, name="nvmon-dd-disable").start()
        self._json_response(202, {"status": "accepted", "action": "dd_disable"})

    def _handle_qsync_enable(self, body: dict) -> None:
        if not self._subsystem_available(qsync.capability(), "qsync"):
            return
        if not self._require_admin():
            return
        source = str(body.get("source", "HOUSESYNC"))
        trigger = str(body.get("trigger", "FALLING"))
        # grid defaults to -1 (sentinel = "no grid; auto-discover displays").
        # The dashboard "Sync All" button posts an empty body, so this is the
        # path that actually fires. Only an explicit non-negative grid in the
        # payload selects the legacy Mosaic-grid path.
        try:
            grid = int(body.get("grid", -1))
        except (TypeError, ValueError):
            grid = -1
        # Optional explicit display list — payload form: [[gpu, display], ...]
        # Takes precedence over grid + auto-discovery. Reserved for the future
        # "manual setup" UI (nvmon-0ru).
        raw_displays = body.get("displays")
        displays_arg: list[tuple[int, int]] | None = None
        if isinstance(raw_displays, list):
            parsed: list[tuple[int, int]] = []
            for pair in raw_displays:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    try:
                        parsed.append((int(pair[0]), int(pair[1])))
                    except (TypeError, ValueError):
                        continue
            if parsed:
                displays_arg = parsed

        def _bg() -> None:
            try:
                result = qsync.enable(
                    source=source, trigger=trigger,
                    grid=grid, displays=displays_arg, wait=False,
                )
                log.get().info(f"/qsync/enable background result: success={result.success}")
            except Exception as exc:
                log.get().error(f"/qsync/enable background error: {exc}", exc_info=True)

        threading.Thread(target=_bg, daemon=True, name="nvmon-qsync-enable").start()
        self._json_response(202, {"status": "accepted", "action": "qsync_enable"})

    def _handle_qsync_enable_from_profile(self, body: dict) -> None:
        """Apply just the QSync section of a named profile.

        Used by the controller's Sync button — the controller looks up each
        node's status.active_profile and POSTs that name here. The agent
        loads the profile and dispatches to qsync.enable_from_profile, which
        honors the captured role/displays/source/trigger. This is the only
        profile-aware sync path; /qsync/enable remains the manual override
        with explicit source/trigger/grid/displays in the body.
        """
        if not self._subsystem_available(qsync.capability(), "qsync"):
            return
        if not self._require_admin():
            return
        profile_name = str(body.get("profile", "")).strip()
        if not profile_name:
            return self._json_response(400, {
                "error": "Missing 'profile' in request body",
            })
        if profile_name not in profiles.list_profiles():
            return self._json_response(404, {
                "error": "profile_not_found",
                "profile": profile_name,
                "available": profiles.list_profiles(),
            })

        try:
            profile_obj = profiles.load(profile_name)
        except Exception as exc:
            log.get().error(f"/qsync/enable_from_profile load failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": f"profile load failed: {exc}"})

        # Tracked as a job (action="sync") so /apply/status, the dashboard
        # job badge and the fan-out outcome poll show the REAL result. Until
        # v0.8.2 this ran untracked: the operator saw "accepted" on the
        # listener and nothing else while the card never locked (site srv1).
        if not profiles._acquire_job("sync", profile_name):  # noqa: SLF001
            return self._json_response(409, {
                "error": "another apply/restore/sync would not stop — request rejected",
                "profile": profile_name,
            })

        def _job() -> tuple[bool, str]:
            result = qsync.enable_from_profile(profile_obj, wait=False)
            detail = (result.stderr or result.stdout or "").strip()
            if result.success:
                return True, f"QSync synced from '{profile_name}'"
            return False, f"QSync not locked from '{profile_name}'" + (f": {detail}" if detail else "")

        def _bg() -> None:
            try:
                ok, msg = profiles._run_job("sync", profile_name, _job)  # noqa: SLF001
                log.get().info(
                    f"/qsync/enable_from_profile background result: "
                    f"profile={profile_name} success={ok} — {msg}"
                )
            except Exception as exc:
                log.get().error(
                    f"/qsync/enable_from_profile background error: {exc}",
                    exc_info=True,
                )

        threading.Thread(
            target=_bg, daemon=True, name="nvmon-qsync-enable-from-profile",
        ).start()
        self._json_response(202, {
            "status": "accepted",
            "action": "qsync_enable_from_profile",
            "profile": profile_name,
            "hint": "poll GET /apply/status for the outcome (action=sync)",
        })

    def _handle_qsync_verify(self, body: dict) -> None:
        """Read-only preflight: compare live state to a named profile.

        Returns {drift: {field: {expected, actual}}}. Empty drift = state
        matches the profile. Used by the dashboard to surface "this node
        has drifted" before the operator hits Re-apply or Sync.

        No admin gate — this only reads state.
        """
        if not self._subsystem_available(qsync.capability(), "qsync"):
            return
        profile_name = str(body.get("profile", "")).strip()
        if not profile_name:
            return self._json_response(400, {"error": "Missing 'profile' in request body"})
        if profile_name not in profiles.list_profiles():
            return self._json_response(404, {
                "error": "profile_not_found",
                "profile": profile_name,
                "available": profiles.list_profiles(),
            })
        try:
            profile_obj = profiles.load(profile_name)
        except Exception as exc:
            log.get().error(f"/qsync/verify load failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": f"profile load failed: {exc}"})
        try:
            drift = qsync.verify_against_profile(profile_obj)
        except Exception as exc:
            log.get().error(f"/qsync/verify check failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": f"verify failed: {exc}"})
        self._json_response(200, {
            "profile": profile_name,
            "drift": drift,
            "ok": not drift,
        })

    def _handle_qsync_disable(self, body: dict) -> None:
        if not self._subsystem_available(qsync.capability(), "qsync"):
            return
        if not self._require_admin():
            return

        def _bg() -> None:
            try:
                result = qsync.disable()
                log.get().info(f"/qsync/disable background result: success={result.success}")
            except Exception as exc:
                log.get().error(f"/qsync/disable background error: {exc}", exc_info=True)

        threading.Thread(target=_bg, daemon=True, name="nvmon-qsync-disable").start()
        self._json_response(202, {"status": "accepted", "action": "qsync_disable"})

    def _handle_mosaic_disable(self, body: dict) -> None:
        if not self._subsystem_available(mosaic.capability(), "mosaic"):
            return
        if not self._require_admin():
            return

        def _bg() -> None:
            try:
                result = mosaic.disable()
                log.get().info(f"/mosaic/disable background result: success={result.success}")
            except Exception as exc:
                log.get().error(f"/mosaic/disable background error: {exc}", exc_info=True)

        threading.Thread(target=_bg, daemon=True, name="nvmon-mosaic-disable").start()
        self._json_response(202, {"status": "accepted", "action": "mosaic_disable"})

    def _handle_profile_save(self, body: dict) -> None:
        """Capture the live state into a Profile JSON.

        Body:
            {"name": "<filename>", "description": "<optional>"}
        Returns:
            200 {"saved": <path>, "name": <name>}
            400 if name missing
            500 on capture failure
        Note: capturing is read-only — no admin gate. Anyone on the trusted
        LAN can ask the agent to write a profile to its own profiles/ dir.
        """
        name = str(body.get("name", "") or "").strip()
        if not name:
            return self._json_response(400, {
                "error": "Missing 'name' in request body",
            })
        description = str(body.get("description", "") or "").strip()
        try:
            path = profile_capture.save_current_state(name, description=description)
        except ValueError as exc:
            return self._json_response(400, {"error": str(exc)})
        except Exception as exc:
            log.get().error(f"/profile/save failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": str(exc)})

        return self._json_response(200, {
            "saved": str(path),
            "name": name,
            "profiles": profiles.list_profiles(),
        })

    def _handle_profile_save_json(self, body: dict) -> None:
        """Write a profile JSON to disk WITHOUT capturing live state.

        Used by the controller to propagate a profile saved on one node
        to every other agent. Body shape:
            {"name": "<filename>", "json": {<profile dict>}}
        Returns 200 with the saved path on success.

        Read-only against the live hardware — no admin gate. The trusted-
        LAN threat model means anyone on the rendering subnet can push a
        profile file to an agent. The downstream apply step still gates
        on admin.
        """
        name = str(body.get("name", "") or "").strip()
        if not name:
            return self._json_response(400, {"error": "Missing 'name' in request body"})
        data = body.get("json")
        if not isinstance(data, dict):
            return self._json_response(400, {"error": "Missing or non-dict 'json' in request body"})

        # Force the profile's `name` field to match the filename so the
        # apply pipeline finds it under the right key.
        try:
            data = dict(data)
            data["name"] = name
            profile = profiles._dict_to_profile(data)  # noqa: SLF001 — internal helper, fine here
            path = profiles.save(profile)
        except ValueError as exc:
            return self._json_response(400, {"error": str(exc)})
        except Exception as exc:
            log.get().error(f"/profile/save_json failed: {exc}", exc_info=True)
            return self._json_response(500, {"error": str(exc)})

        return self._json_response(200, {
            "saved": str(path),
            "name": name,
            "profiles": profiles.list_profiles(),
        })

    # --- System power handlers ---------------------------------------
    # These shell out to Windows' shutdown.exe with a small delay so the
    # HTTP response returns BEFORE the OS starts shutting down. Admin is
    # required (config.is_admin() and shutdown.exe both enforce this).
    # Recovery after restart is handled by the "nvmon Listener" Scheduled
    # Task installed by install.bat --listener (/SC ONLOGON /RL HIGHEST).

    def _coerce_delay(self, body: dict) -> int:
        try:
            return int(body.get("delay_seconds", system_mod.DEFAULT_DELAY_SECONDS))
        except (TypeError, ValueError):
            return system_mod.DEFAULT_DELAY_SECONDS

    def _system_response(self, result: system_mod.PowerResult) -> None:
        # 202 accepted on success (shutdown queued, OS will act after the delay);
        # 500 on shutdown.exe failure.
        code = 202 if result.success else 500
        self._json_response(code, {
            "success": result.success,
            "action": result.action,
            "delay_seconds": result.delay_seconds,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.error,
        })

    def _handle_system_restart(self, body: dict) -> None:
        if not self._require_admin():
            return
        result = system_mod.restart(delay_seconds=self._coerce_delay(body))
        self._system_response(result)

    def _handle_system_shutdown(self, body: dict) -> None:
        if not self._require_admin():
            return
        result = system_mod.shutdown(delay_seconds=self._coerce_delay(body))
        self._system_response(result)

    def _handle_system_abort(self, body: dict) -> None:
        if not self._require_admin():
            return
        self._system_response(system_mod.abort())

    def _handle_system_restart_listener(self, body: dict) -> None:
        if not self._require_admin():
            return
        # Default 2s delay so the HTTP response returns before the
        # listener is taskkilled. Operator can override via body.delay_seconds.
        try:
            delay = int(body.get("delay_seconds", 2))
        except (TypeError, ValueError):
            delay = 2
        self._system_response(system_mod.restart_listener(delay_seconds=delay))

    # --- gates + response helpers ---

    def _require_admin(self) -> bool:
        if config.is_admin():
            return True
        self._json_response(403, {
            "error": "Administrator privileges required",
            "hint": "Restart nvmon agent in an elevated shell to allow control operations.",
        })
        return False

    def _subsystem_available(self, cap: dict, name: str) -> bool:
        if cap.get("available"):
            return True
        self._json_response(503, {
            "error": f"{name} subsystem unavailable",
            "reason": cap.get("reason", ""),
        })
        return False

    def _control_response(self, result) -> None:
        code = 200 if result.success else 500
        self._json_response(code, {
            "success": result.success,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
        })

    def _html_response(self, status_code: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _json_response(self, status_code: int, data) -> None:
        payload = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NVMON-Password")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:  # noqa: N802 — stdlib naming
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NVMON-Password")
        self.end_headers()


# ---------------------------------------------------------------------------
# Ad-hoc status builder (used before the monitor has produced its first poll)
# ---------------------------------------------------------------------------

def _status_age_seconds(timestamp: object) -> float:
    """Seconds since an ISO-8601 UTC ``timestamp`` (0.0 when unparseable)."""
    if not isinstance(timestamp, str) or not timestamp:
        return 0.0
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return round(max(0.0, (datetime.now(timezone.utc) - ts).total_seconds()), 1)
    except Exception:
        return 0.0


def _build_adhoc_status() -> dict:
    """Build a status dict directly from subsystem queries (no monitor)."""
    dd_state = displays.status()
    mosaic_state = mosaic.status()
    qsync_state = qsync.status()
    gpu_list = gpu.status()
    edid_snapshot = edid.status()
    result = validator.validate_all(
        dd=dd_state, mosaic=mosaic_state, qsync=qsync_state,
        gpus=gpu_list, profile=None,
    )
    return monitor_mod._build_status_dict(  # reuse the canonical builder
        dd=dd_state, mosaic=mosaic_state, qsync=qsync_state, gpus=gpu_list,
        edid=edid_snapshot, profile_name="",
        uptime_seconds=0, poll_count=0,
        validation=result, alerts=[], errors=[],
    )


# ---------------------------------------------------------------------------
# Serialization helpers shared by GET handlers
# ---------------------------------------------------------------------------

def _dd_to_dict(state) -> dict:
    return {
        "available": displays.is_available(),
        "active": state.active,
        "display_count": state.display_count,
        "displays": [
            {
                "id": d.id, "name": d.name, "status": d.status,
                "refresh_rate": d.refresh_rate, "refresh_fraction": d.refresh_fraction,
                "resolution": list(d.resolution),
                "connector": d.connector,
                "bits_per_channel": d.bits_per_channel,
                "color_space": d.color_space,
                "pixel_encoding": d.pixel_encoding,
                "gpu_index": d.gpu_index,
                "display_index": d.display_index,
                "os_target_id": d.os_target_id,
            }
            for d in state.displays
        ],
    }


def _mosaic_to_dict(state) -> dict:
    return {
        "available": mosaic.is_available(),
        "enabled": state.enabled,
        "rows": state.rows, "cols": state.cols,
        "per_display_resolution": list(state.per_display_resolution),
        "total_resolution": list(state.total_resolution),
        "refresh_rate": state.refresh_rate,
        "output_count": state.output_count,
        "synchronized": state.synchronized,
        "outputs": [
            {"gpu": o.gpu, "display": o.display, "display_id": o.display_id}
            for o in state.outputs
        ],
    }


def _qsync_to_dict(state) -> dict:
    return {
        "available": state.available,
        "synced": state.synced,
        "house_sync_detected": state.house_sync_detected,
        "house_sync_incoming_hz": state.house_sync_incoming_hz,
        "source": state.source,
        "trigger": state.trigger,
        "role": state.role,
        "lock_source_label": state.lock_source_label,
        "effective_lock_hz": state.effective_lock_hz,
        "per_gpu_sync": [
            {
                "gpu": g.gpu,
                "synced": g.effective_synced,
                "raw_synced": g.synced,
                "signal_available": g.signal_available,
                "sync_note": g.sync_note,
                "sync_state": g.sync_state,
            }
            for g in state.per_gpu_sync
        ],
    }


def _gpu_to_dict(g) -> dict:
    return {
        "index": g.index, "name": g.name,
        "pci_bus_id": g.pci_bus_id, "driver_version": g.driver_version,
        "temperature_c": g.temperature_c,
        "power_draw_w": g.power_draw_w, "power_limit_w": g.power_limit_w,
        "gpu_utilization_pct": g.gpu_utilization_pct,
        "memory_used_mb": g.memory_used_mb, "memory_total_mb": g.memory_total_mb,
        "gpu_clock_mhz": g.gpu_clock_mhz,
        "mem_clock_mhz": g.mem_clock_mhz,
        "video_clock_mhz": g.video_clock_mhz,
        "pstate": g.pstate,
        "display_active": g.display_active,
        "fan_speed_pct": g.fan_speed_pct,
        "pcie_gen": g.pcie_gen, "pcie_width": g.pcie_width,
    }


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def serve(
    *,
    port: int | None = None,
    profile_name: str | None = None,
    poll_interval: int | None = None,
    bind_host: str = "0.0.0.0",
    dry_run: bool = False,
) -> int:
    """Start the HTTP daemon with the integrated monitor.

    Returns an int suitable as a process exit code.

    Args:
        port: override for `[agent].port`.
        profile_name: profile to validate against (defaults to none).
        poll_interval: override for `[agent].poll_interval_seconds`.
        bind_host: interface to bind; defaults to all interfaces.
        dry_run: if True, initialize then shut down immediately after logging
            the planned bind address. Intended for smoke tests.
    """
    logger = log.get()
    # A click in the console window would otherwise freeze every log write
    # (QuickEdit selection) and with it the monitor thread + POST handlers.
    from ..util import disable_console_quick_edit
    disable_console_quick_edit()
    agent_cfg = config.get_agent_config()
    actual_port = int(port if port is not None else agent_cfg["port"])
    actual_interval = int(
        poll_interval if poll_interval is not None
        else agent_cfg["poll_interval_seconds"]
    )

    logger.info(
        f"nvmon agent {__version__} — binding {bind_host}:{actual_port} "
        f"(poll_interval={actual_interval}s)"
    )

    capabilities = _build_capabilities()
    for name, cap in capabilities.items():
        if cap.get("available"):
            logger.info(f"  capability {name}: available ({cap.get('exe', '')})")
        else:
            logger.warning(
                f"  capability {name}: unavailable — {cap.get('reason', '')}"
            )

    if not config.is_admin():
        logger.warning(
            "Agent running WITHOUT administrator — read-only endpoints will work, "
            "but POST control operations will return 403."
        )

    if dry_run:
        logger.info(f"[dry-run] Would bind http://{bind_host}:{actual_port}")
        logger.info("[dry-run] Monitor + HTTP server skipped")
        return 0

    # Effective profile resolution priority (most → least):
    #   1. CLI --profile / explicit arg (operator override)
    #   2. active_profile.json snapshot — the boot-apply contract written
    #      by the most recent /apply on this listener. Survives restarts;
    #      a node that was running "24" keeps reporting "24" after a
    #      bounce instead of silently reverting to the auto-default.
    #   3. profiles/default.json auto-pick — first-boot fallback for a
    #      node that has never been /apply'd.
    effective_profile = profile_name
    if not effective_profile:
        snap_name = profiles.read_active_name()
        if snap_name:
            effective_profile = snap_name
            logger.info(
                f"Monitor: restoring active profile '{snap_name}' from "
                f"{profiles.ACTIVE_PROFILE_FILE} (last applied on this listener)"
            )
    if not effective_profile:
        if "default" in profiles.list_profiles():
            effective_profile = "default"
            logger.info(
                "Monitor: no --profile or active_profile snapshot, "
                "auto-selecting 'default' (profiles/default.json found)"
            )

    # Spin up the monitor before the HTTP server so the first request can
    # read a populated cache if the poll finishes quickly enough.
    mon = monitor_mod.Monitor(profile_name=effective_profile, interval=actual_interval)
    AgentHandler.monitor = mon
    mon.start_thread()

    # Bind HTTP
    try:
        httpd = _ThreadingHTTPServer((bind_host, actual_port), AgentHandler)
    except OSError as exc:
        logger.error(f"Failed to bind {bind_host}:{actual_port}: {exc}")
        logger.error("Is another nvmon agent already running? Try a different port.")
        mon.stop()
        return 2

    AgentHandler.bound_port = actual_port

    # Auto-announce via mDNS so controllers can discover this agent.
    # Graceful fallback: if zeroconf is missing or announce fails, log and continue.
    announcer = None
    try:
        from .. import discovery as _discovery
        announcer = _discovery.announce(
            port=actual_port,
            role="agent",
            capabilities=capabilities,
            machine_id=_discovery.get_machine_id(),
            version=__version__,
        )
        logger.info("mDNS announce active (_nvmon._tcp.local.)")
    except Exception as exc:
        logger.warning(f"mDNS announce skipped: {exc}")

    # Push-registration: tell the configured controller "I exist", with retry
    # backoff and a fixed-interval heartbeat. Reliable TCP path that doesn't
    # depend on multicast surviving the LAN. Empty controller_url -> disabled,
    # the agent falls back to mDNS / static [[controller.nodes]] only.
    agent_cfg = config.get_agent_config()
    controller_url = agent_cfg.get("controller_url", "")
    registration_client = None
    if controller_url:
        try:
            from . import registration as _registration
            from .. import discovery as _discovery_mod
            registration_client = _registration.RegistrationClient(
                controller_url=controller_url,
                hostname=config.get_hostname(),
                agent_url=f"http://{config.get_local_ip()}:{actual_port}",
                machine_id=_discovery_mod.get_machine_id(),
                version=__version__,
                capabilities=capabilities,
                interval_seconds=int(agent_cfg.get("register_interval_seconds", 60)),
            )
            registration_client.start()
            logger.info(f"push-registration started (controller={controller_url})")
        except Exception as exc:
            logger.warning(f"push-registration failed to start: {exc}")
            registration_client = None

    logger.info(f"Server ready at http://localhost:{actual_port}/")
    logger.info("Endpoints: /health /status /gpu /displays /mosaic /qsync /profiles /validate")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
    finally:
        if registration_client is not None:
            try:
                registration_client.stop()
            except Exception:
                pass
        if announcer is not None:
            try:
                announcer.shutdown()
            except Exception:
                pass
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        mon.stop()
        logger.info("nvmon agent stopped")

    return 0


def wait_for_first_poll(timeout_seconds: float = 30.0) -> bool:
    """Block until the monitor has written its first status, or timeout.

    Intended for smoke tests — not called automatically.
    """
    mon = AgentHandler.monitor
    if mon is None:
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if mon.last_status() is not None:
            return True
        time.sleep(0.2)
    return False
