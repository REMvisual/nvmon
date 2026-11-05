"""Fake nvmon agent for the Playwright run: reports an apply IN PROGRESS
(step [4/6]) with a stale snapshot, and answers POST /apply/cancel with
200 {"cancelling": true}. Lets the dashboard's job banner, stale marker and
Cancel button be exercised end-to-end without elevating the real agent.
Self-terminates after N seconds (argv[1], default 900)."""
import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 19997
HOST = "FAKE-APPLYING"
started = time.time()
state = {"cancel_requests": 0}


def status_body():
    now = datetime.now(timezone.utc)
    return {
        "timestamp": now.isoformat(),
        "hostname": HOST, "ip": "127.0.0.1", "active_profile": "24_Server",
        "uptime_seconds": int(time.time() - started), "poll_count": 7,
        "status_age_s": 47.0, "job_active": True,
        "job": {"in_progress": True, "action": "apply", "profile": "24_Server",
                "step": "[4/6] Configuring Mosaic", "started_at": now.isoformat(),
                "finished_at": None, "success": None, "message": None, "cancelled": False},
        "direct_display": {"active": False, "display_count": 0, "displays": [], "signals": [
            {"name": "HELIOS", "source": [7680, 4320], "position": [0, 0], "target": [10240, 5760], "hz": 60.0}]},
        "mosaic": {"enabled": True, "rows": 2, "cols": 2, "per_display_resolution": [3840, 2160],
                   "total_resolution": [7680, 4320], "refresh_rate": 23.976, "output_count": 4,
                   "synchronized": True, "outputs": []},
        "qsync": {"available": True, "synced": False, "house_sync_detected": True,
                  "house_sync_incoming_hz": 60.0, "source": "HOUSESYNC", "trigger": "FALLING",
                  "role": "server", "lock_source_label": "", "effective_lock_hz": 0.0, "per_gpu_sync": []},
        "gpu": [{"index": 0, "name": "NVIDIA RTX A6000", "driver_version": "596.86", "temperature_c": 55,
                 "power_draw_w": 120.0, "power_limit_w": 300.0, "gpu_utilization_pct": 40,
                 "memory_used_mb": 1000, "memory_total_mb": 49140}],
        "output_health": {"status": "warn", "refresh_hz": 23.976, "level": "warn"},
        "validation": {"all_passed": False, "checks": {
            "output_signal": {"passed": False, "detail": "desktop 7680x4320 sent as 10240x5760@60Hz (scaled)"},
            "mosaic_config": {"passed": True, "detail": "2x2 @ 3840x2160 @ 23.976Hz"},
            "qsync_locked": {"passed": False, "detail": "QSync not locked (rate mismatch: displays 23.976Hz vs house 60.000Hz)"},
        }},
        "alerts": [], "errors": [],
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if p == "/health":
            return self._json(200, {"status": "ok", "hostname": HOST, "version": "0.8.0", "is_admin": True,
                                    "capabilities": {"gpu": {"available": True}, "mosaic": {"available": True},
                                                     "qsync": {"available": True}, "displays": {"available": True},
                                                     "edid": {"available": True}}})
        if p == "/status":
            return self._json(200, status_body())
        if p == "/apply/status":
            return self._json(200, status_body()["job"])
        if p == "/profiles":
            return self._json(200, {"profiles": ["24_Server"]})
        if p == "/validate":
            return self._json(200, status_body()["validation"])
        return self._json(404, {"error": "nope", "path": p})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if p == "/apply/cancel":
            state["cancel_requests"] += 1
            return self._json(200, {"cancelling": True, "job": status_body()["job"]})
        if p == "/ping":
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "nope", "path": p})


seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 900
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
print("FAKE READY", PORT, flush=True)
time.sleep(seconds)
httpd.shutdown()
print("FAKE EXIT cancel_requests=", state["cancel_requests"], flush=True)
