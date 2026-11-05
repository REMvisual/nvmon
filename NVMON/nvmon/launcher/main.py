"""Launcher entrypoint — `python -m nvmon.launcher.main` or via cli.py.

Parses args, loads config, builds supervisors + HTTP server, handles
SIGINT / Ctrl+C cleanly by draining supervisors and stopping children.

Flags:
    --dry-run                Print configured apps + elevation + bind
                             address, exit 0 without binding a port.
    --allow-partial-elevation  Permit non-elevated boot even when some
                               configured apps require elevation
                               (spec §9).
    --verbose                Verbose console logging.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path
from typing import Sequence

from .. import config as _config
from .. import log as _log
from .apps import AppConfigError, LauncherConfig, load_launcher_config
from .server import build_server


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="nvmon-launcher",
        description="nvmon launcher — generic process-launch daemon (:9996)",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="print config summary and exit without binding")
    p.add_argument("--allow-partial-elevation", action="store_true",
                   help="boot non-elevated even when some apps require elevation")
    p.add_argument("--verbose", action="store_true", help="verbose logging")
    p.add_argument("--bind", default=None,
                   help="override bind address host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=None,
                   help="override bind port (default: 9996)")
    return p.parse_args(argv)


def _elevation_preflight(
    cfg: LauncherConfig,
    *,
    is_elevated: bool,
    allow_partial: bool,
    logger,
) -> bool:
    """Return False if we should refuse to boot, True to continue.

    Per spec §9: if any app is elevated: true and we are not elevated,
    refuse to start unless `--allow-partial-elevation` is passed.
    """
    elevated_apps = [a.name for a in cfg.apps if a.elevated]
    if elevated_apps and not is_elevated:
        if not allow_partial:
            logger.error(
                f"refusing to boot: apps require elevation but launcher is "
                f"non-elevated: {elevated_apps}. "
                f"Pass --allow-partial-elevation to override."
            )
            return False
        logger.warning(
            f"non-elevated boot with elevated apps: {elevated_apps}; "
            f"those apps will be refused at launch time (--allow-partial-elevation)"
        )
    non_elev_apps = [a.name for a in cfg.apps if not a.elevated]
    if is_elevated and non_elev_apps:
        logger.warning(
            f"elevation leak: launcher elevated but these apps are "
            f"elevated=false: {non_elev_apps}. Child processes will inherit "
            f"the elevated token."
        )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logger = _log.setup(verbose=args.verbose)

    # Load config
    try:
        cfg = load_launcher_config(force_reload=True)
    except AppConfigError as e:
        logger.error(f"config error: {e}")
        return 2

    if args.bind is not None:
        cfg.bind_host = args.bind
    if args.port is not None:
        cfg.port = args.port

    # Ensure log dir exists
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)

    is_elevated = _config.is_admin()

    if args.dry_run:
        print(f"nvmon launcher --dry-run")
        print(f"  bind:      {cfg.bind_host}:{cfg.port}")
        print(f"  elevated:  {is_elevated}")
        print(f"  log_dir:   {cfg.log_dir}")
        print(f"  agent:     http://127.0.0.1:{cfg.agent_port}")
        print(f"  admin_required_for_control: "
              f"{cfg.admin_required_for_control}")
        print(f"  apps configured ({len(cfg.apps)}):")
        for app in cfg.apps:
            flags = []
            if app.elevated:
                flags.append("elevated")
            if app.auto_restart:
                flags.append("auto_restart")
            if app.pre_launch:
                flags.append(f"pre_launch[{len(app.pre_launch)}]")
            flag_str = f" [{','.join(flags)}]" if flags else ""
            print(f"    - {app.name}{flag_str}")
            print(f"        exe: {app.exe_path}")
        return 0

    if not _elevation_preflight(
        cfg,
        is_elevated=is_elevated,
        allow_partial=args.allow_partial_elevation,
        logger=logger,
    ):
        return 3

    try:
        httpd, registry = build_server(
            cfg, launcher_is_elevated=is_elevated, logger=logger,
        )
    except OSError as e:
        logger.error(f"bind failed on {cfg.bind_host}:{cfg.port} — {e}")
        return 4

    logger.info(
        f"nvmon launcher listening on http://{cfg.bind_host}:{cfg.port} "
        f"(elevated={is_elevated}, apps={len(cfg.apps)})"
    )

    # Auto-announce via mDNS so the controller can discover this launcher.
    announcer = None
    try:
        from .. import discovery as _discovery
        announcer = _discovery.announce(
            port=cfg.port,
            role="launcher",
            capabilities={"launcher": {"available": True}},
            machine_id=_discovery.get_machine_id(),
            extra={"apps": ",".join(app.name for app in cfg.apps)},
        )
        logger.info("mDNS announce active")
    except Exception as exc:
        logger.warning(f"mDNS announce skipped: {exc}")

    stop_event = threading.Event()

    def _handle_signal(signum, frame):  # type: ignore[no-untyped-def]
        logger.info(f"signal {signum} received — shutting down")
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):
        # Not available on all platforms / non-main threads
        pass

    server_thread = threading.Thread(
        target=httpd.serve_forever, name="launcher-http", daemon=True,
    )
    server_thread.start()

    try:
        while not stop_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("draining supervisors…")
        if announcer is not None:
            try:
                announcer.shutdown()
            except Exception:
                pass
        try:
            registry.shutdown_all()
        finally:
            httpd.shutdown()
            httpd.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
