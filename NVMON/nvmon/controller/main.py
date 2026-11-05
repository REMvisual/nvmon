"""Controller entrypoint.

Wires together:
    auth         — loads password file (or runs in no-auth mode)
    NodeRegistry — from nvmon.toml
    Aggregator   — background poll thread
    FanoutExecutor
    ControllerServer — HTTP :9997

Also serves as `python -m nvmon.controller.main`. The user-facing entry is
`python -m nvmon controller` (wired by cli.py when it replaces its stub).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from typing import Sequence

from .. import __version__
from .. import config as cfgmod
from .. import log as logmod
from . import auth as authmod
from .aggregator import Aggregator
from .fanout import FanoutExecutor
from .registry import NodeRegistry
from .server import ControllerServer, persist_registered


REGISTRY_SWEEP_INTERVAL_SECONDS = 300


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nvmon-controller",
        description="nvmon controller — aggregator + fan-out (HTTP :9997)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the port from nvmon.toml",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional explicit path to nvmon.toml (default: BASE_DIR/nvmon.toml)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose/debug logging",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color on console output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print boot summary and exit without binding the socket",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="After the server binds, open the default browser to the dashboard",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"nvmon {__version__}",
    )
    return parser


def _maybe_override_config_file(path: str | None) -> None:
    """If the user passed --config, repoint cfgmod.CONFIG_FILE at it."""
    if not path:
        return
    from pathlib import Path
    p = Path(path).resolve()
    cfgmod.CONFIG_FILE = p  # type: ignore[assignment]
    # Force reload on next access.
    cfgmod.load_config(force_reload=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logger = logmod.setup(verbose=args.verbose, no_color=args.no_color)

    _maybe_override_config_file(args.config)

    ctrl_cfg = cfgmod.get_controller_config()
    port = int(args.port) if args.port is not None else int(ctrl_cfg["port"])

    # --- Auth -------------------------------------------------------------
    authmod.init(ctrl_cfg["password_file"])

    # --- Registry ---------------------------------------------------------
    registry = NodeRegistry(
        nodes_cfg=ctrl_cfg["nodes"],
        groups_cfg=ctrl_cfg["groups"],
    )

    # Push-registered nodes — listeners that POSTed /register in a previous
    # session of this controller. SECRETS_DIR is admin-ACL'd by install.bat
    # alongside the controller password, so the registry file inherits the
    # same protection class. Missing/malformed file is non-fatal: load
    # returns 0 and the controller boots clean.
    registered_file = cfgmod.SECRETS_DIR / "registered_nodes.json"
    try:
        n_loaded = registry.load_registered_from_file(registered_file)
        if n_loaded:
            logger.info(
                "[registry] loaded %d registered node(s) from %s",
                n_loaded, registered_file,
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning("[registry] load %s failed: %s", registered_file, exc)

    # --- Aggregator -------------------------------------------------------
    # Poll timeout is deliberately shorter than the fan-out timeout: a dead
    # node must not stall a 5 s poll cycle for 15 s. Workers scale with the
    # fleet (one per node, capped) so 40 servers are polled as fast as 2.
    poll_workers = int(ctrl_cfg.get("poll_workers", 0) or 0)
    if poll_workers <= 0:
        poll_workers = min(cfgmod.MAX_POLL_WORKERS, max(4, registry.count()))
    aggregator = Aggregator(
        registry,
        poll_interval_seconds=int(ctrl_cfg["poll_interval_seconds"]),
        fanout_timeout_seconds=int(ctrl_cfg.get("poll_timeout_seconds", 5)),
        max_workers=poll_workers,
    )

    # --- Fan-out ----------------------------------------------------------
    fanout = FanoutExecutor(
        registry,
        timeout_seconds=int(ctrl_cfg["fanout_timeout_seconds"]),
    )

    # --- Summary ----------------------------------------------------------
    bind_label = f"http://{args.host}:{port}"
    logger.info("nvmon controller v%s", __version__)
    logger.info("  bind:         %s", bind_label)
    logger.info("  nodes:        %d", registry.count())
    logger.info("  groups:       %d (%s)", len(registry.group_names()),
                ", ".join(registry.group_names()) or "-")
    logger.info("  auth:         %s", "enabled" if authmod.is_auth_enabled() else "DISABLED")
    logger.info("  poll:         every %ds (timeout %ds, %d workers)",
                int(ctrl_cfg["poll_interval_seconds"]),
                int(ctrl_cfg.get("poll_timeout_seconds", 5)), poll_workers)
    logger.info("  fanout:       timeout %ds", int(ctrl_cfg["fanout_timeout_seconds"]))

    if args.dry_run:
        # Summary already logged. Exit cleanly so smoke tests can verify
        # boot without holding a socket.
        print(f"bind: {bind_label}")
        print(f"nodes: {registry.count()}")
        print(f"auth_enabled: {authmod.is_auth_enabled()}")
        print("dry-run: OK")
        return 0

    disc_registry = start_discovery(registry, aggregator, logger)

    # --- Start threads ----------------------------------------------------
    aggregator.start()

    # Chain sync (v0.9.9): starts each frame-lock daisy chain server-first
    # after reboots. Only groups with one server + clients are chains.
    from .chainsync import ChainSync
    chainsync = ChainSync(registry, aggregator, cfgmod.SECRETS_DIR / "sync_chains.json")
    chainsync.start()
    logger.info("  chain sync:   active (%s)", ", ".join(
        f"{g}: {c['server']} -> {', '.join(c['clients'])}" for g, c in chainsync.chains().items()) or
        "no chains known yet — learned from the nodes' active profiles")

    return _serve(args, port, registry, aggregator, fanout, disc_registry,
                  registered_file, chainsync, ctrl_cfg, logger)


def start_discovery(registry: NodeRegistry, aggregator: Aggregator, logger):  # noqa: ANN001
    """Start mDNS discovery and keep ``registry`` in sync with it.

    Returns the DiscoveryRegistry, or None when zeroconf is missing or
    fails to start. Shared by the controller and ``nvmon app --discover``.
    """
    disc_registry = None
    try:
        from .. import discovery as _discovery
        disc_registry = _discovery.DiscoveryRegistry()

        def _sync_discovery_to_registry() -> None:
            """Reconcile registry vs live discovery cache.

            Only ``role=='agent'`` discoveries enter the registry. The
            launcher (role=launcher, port 9996) and any future controller
            announcements share machine_id with the agent; if we registered
            them all, the controller would poll the wrong port for /health
            and surface the node as OFFLINE despite the agent being
            perfectly reachable on 9998. Filtering here is the safety net
            that pairs with discovery's role-aware cache key.
            """
            agents = [
                n for n in disc_registry.get_all()
                if (n.role or "").lower() == "agent"
            ]
            live_hosts = {n.hostname for n in agents}
            registry.merge_discovered([
                {
                    "hostname": n.hostname,
                    "url": f"http://{n.ip}:{n.port}",
                }
                for n in agents
            ])
            # Drop discovery-sourced registry entries no longer in the cache.
            for entry in registry.get_all():
                if entry.source == "discovery" and entry.hostname not in live_hosts:
                    if registry.remove_discovered(entry.hostname):
                        logger.info(
                            "discovery: evicted stale entry %s",
                            entry.hostname,
                        )

        def _on_discovery_change(event: str, node: "_discovery.DiscoveredNode") -> None:
            if event in (
                _discovery.EVENT_APPEARED,
                _discovery.EVENT_UPDATED,
                _discovery.EVENT_GOODBYE,
                _discovery.EVENT_TTL_EXPIRED,
            ):
                _sync_discovery_to_registry()
                # Newly discovered node -> probe its /health right now so
                # the dashboard sees ONLINE immediately instead of waiting
                # up to one aggregator tick (default 5s) for the next poll.
                # Guarded with a flag because the first event may arrive
                # before aggregator.start() runs below.
                if event == _discovery.EVENT_APPEARED and getattr(
                    aggregator, "_thread", None,
                ) is not None:
                    try:
                        aggregator.poll_once()
                    except Exception as poll_exc:
                        logger.debug(
                            "discovery: poll_once after appear failed: %s",
                            poll_exc,
                        )

        disc_registry.on_change(_on_discovery_change)
        disc_registry.start()
        # Give zeroconf a moment to receive the initial round of responses
        # before the aggregator's first poll fires. Without this, listeners
        # that were already running before the controller booted only get
        # picked up on the *next* mDNS announcement (up to 60–120s away),
        # and the operator has to click "reconnect" to make them appear.
        # 1.5s is comfortably longer than typical LAN mDNS latency (10–
        # 100ms) but short enough to not delay startup noticeably.
        time.sleep(1.5)
        logger.info(
            "  discovery:    active (mDNS _nvmon._tcp.local.) — "
            "initial scan: %d node(s)",
            len([n for n in disc_registry.get_all() if (n.role or "").lower() == "agent"]),
        )
    except Exception as exc:
        logger.warning("  discovery:    disabled (%s)", exc)
    return disc_registry


def _serve(args, port, registry, aggregator, fanout, disc_registry,  # noqa: ANN001
           registered_file, chainsync, ctrl_cfg, logger) -> int:
    bind_label = f"http://{args.host}:{port}"
    try:
        server = ControllerServer(
            bind_host=args.host,
            port=port,
            registry=registry,
            aggregator=aggregator,
            fanout=fanout,
            disc_registry=disc_registry,
            registered_file=registered_file,
            chainsync=chainsync,
        )
    except OSError as exc:
        logger.error("[controller] failed to bind %s: %s", bind_label, exc)
        aggregator.stop()
        return 2

    server.serve_forever_in_thread()

    # --- Open browser (host shortcut flow) --------------------------------
    if args.open_browser:
        import threading as _threading
        import time as _time
        import webbrowser as _webbrowser

        def _open_delayed() -> None:
            _time.sleep(1.5)
            url = f"http://localhost:{port}/"
            try:
                _webbrowser.open(url)
                logger.info("[controller] opened browser → %s", url)
            except Exception as exc:
                logger.warning("[controller] could not open browser: %s", exc)

        _threading.Thread(target=_open_delayed, name="open-browser", daemon=True).start()

    # --- Graceful shutdown ------------------------------------------------
    stop_event = threading.Event()

    # --- Background sweep: auto-evict stale registered nodes --------------
    ttl_hours = int(ctrl_cfg.get("registered_node_ttl_hours", 24))
    ttl_seconds = ttl_hours * 3600
    sweep_thread: threading.Thread | None = None
    if ttl_seconds <= 0:
        logger.info("[registry] auto-eviction disabled (ttl=%dh)", ttl_hours)
    else:
        def _sweep_loop() -> None:
            while not stop_event.is_set():
                if stop_event.wait(timeout=REGISTRY_SWEEP_INTERVAL_SECONDS):
                    return
                try:
                    evicted = registry.evict_stale_registered(ttl_seconds)
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.warning("[registry] sweep failed: %s", exc)
                    continue
                if not evicted:
                    continue
                persist_registered(registry, registered_file)
                logger.info(
                    "[registry] auto-evicted %d stale registered node(s): %s (ttl=%dh)",
                    len(evicted), ", ".join(evicted), ttl_hours,
                )

        sweep_thread = threading.Thread(
            target=_sweep_loop,
            name="nvmon-registry-sweep",
            daemon=True,
        )
        sweep_thread.start()
        logger.info(
            "  reg-sweep:    every %ds, ttl=%dh",
            REGISTRY_SWEEP_INTERVAL_SECONDS, ttl_hours,
        )

    def _handle_signal(signum, _frame):  # noqa: ANN001
        logger.info("[controller] caught signal %s — shutting down", signum)
        stop_event.set()

    # SIGINT everywhere; SIGTERM where supported (Windows lacks some).
    try:
        signal.signal(signal.SIGINT, _handle_signal)
    except (ValueError, OSError):
        pass
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, OSError, AttributeError):
        pass

    try:
        while not stop_event.is_set():
            # Block on the event so ctrl-c wakes us immediately.
            if stop_event.wait(timeout=1.0):
                break
    except KeyboardInterrupt:
        logger.info("[controller] KeyboardInterrupt — shutting down")

    try:
        server.shutdown()
    finally:
        aggregator.stop()
        if disc_registry is not None:
            try:
                disc_registry.stop()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
