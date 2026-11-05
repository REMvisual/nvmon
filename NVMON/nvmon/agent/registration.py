"""Push-registration client.

Each agent POSTs `/register` to its configured controller on boot, with
exponential backoff until the first success, then heartbeats on a fixed
interval. Reliable TCP unicast path that doesn't depend on multicast
surviving the LAN — pairs with the existing mDNS announce as
belt-and-suspenders.

Empty `controller_url` -> registration is disabled and `start()` is a
no-op. The registration thread is a daemon so it never blocks process
exit; `stop()` sets a stop event and joins for clean shutdown.

Stdlib only. The enclosed Python on render nodes deliberately doesn't
ship `requests`; we use `urllib.request` directly.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .. import log


_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_REQUEST_TIMEOUT = 10.0


class RegistrationClient:
    """Daemon-thread client that POSTs `/register` to a controller.

    Lifecycle:
        client = RegistrationClient(...)
        client.start()         # non-blocking; spawns daemon thread
        ...
        client.stop()          # signal + join (called from agent shutdown)

    Thread loop:
        - POST until first success with exponential backoff (1, 2, 4, ..., 60s)
        - On success, sleep `interval_seconds` then heartbeat
        - On heartbeat failure, fall back to backoff loop until success again
    """

    def __init__(
        self,
        controller_url: str,
        hostname: str,
        agent_url: str,
        machine_id: str,
        version: str,
        capabilities: dict[str, Any],
        interval_seconds: int = 60,
    ) -> None:
        self.controller_url = controller_url.rstrip("/")
        self.hostname = hostname
        self.agent_url = agent_url
        self.machine_id = machine_id
        self.version = version
        self.capabilities = capabilities or {}
        self.interval_seconds = max(5, int(interval_seconds))

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered_once = False

    def start(self) -> None:
        if not self.controller_url:
            log.get().info("[register] disabled (controller_url empty)")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="nvmon-register",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def _payload(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "url": self.agent_url,
            "machine_id": self.machine_id,
            "version": self.version,
            "capabilities": self.capabilities,
        }

    def _post_once(self) -> tuple[bool, str]:
        """One POST attempt. Returns (ok, error_msg)."""
        url = f"{self.controller_url}/register"
        body = json.dumps(self._payload()).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                if 200 <= resp.status < 300:
                    return True, ""
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 — defensive
            return False, f"unexpected: {exc}"

    def _run(self) -> None:
        logger = log.get()
        backoff = _INITIAL_BACKOFF

        while not self._stop_event.is_set():
            ok, err = self._post_once()
            if ok:
                if not self._registered_once:
                    logger.info(
                        "[register] success: %s -> %s",
                        self.hostname, self.controller_url,
                    )
                    self._registered_once = True
                else:
                    logger.debug(
                        "[register] heartbeat ok: %s", self.controller_url,
                    )
                backoff = _INITIAL_BACKOFF
                # Wait the full heartbeat interval, but wake immediately on stop.
                if self._stop_event.wait(timeout=self.interval_seconds):
                    return
                continue

            # Failure path. First-failure logs at warning; subsequent at debug
            # to avoid log spam during a long outage.
            if not self._registered_once:
                logger.warning(
                    "[register] %s -> %s failed: %s (retry in %.0fs)",
                    self.hostname, self.controller_url, err, backoff,
                )
            else:
                logger.warning(
                    "[register] heartbeat to %s failed: %s (retry in %.0fs)",
                    self.controller_url, err, backoff,
                )
            if self._stop_event.wait(timeout=backoff):
                return
            backoff = min(backoff * 2.0, _MAX_BACKOFF)
