"""Sub-CLI: ``python -m nvmon discover``.

Lists currently-advertised ``_nvmon._tcp.local.`` services for a fixed
window and prints a table. First-line diagnostic tool ("why isn't node-05
showing up?"). Does not start a full controller — just a zeroconf browser
in the foreground.

Entry point is :func:`main`; cli.py will wire it up later. Until then,
you can run::

    python -m nvmon.discovery_cli [--timeout 5] [--service-type ...]

Spec reference: sub-project C, "Observability → CLI: nvmon discover --debug".
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Sequence

from .discovery import (
    DEFAULT_TTL_SECONDS,
    DiscoveredNode,
    DiscoveryRegistry,
    EVENT_APPEARED,
    EVENT_GOODBYE,
    EVENT_TTL_EXPIRED,
    EVENT_UPDATED,
    SERVICE_TYPE,
)


def _fmt_ts(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M:%S")


def _fmt_table(nodes: list[DiscoveredNode]) -> str:
    if not nodes:
        return "(no nvmon services found)"

    headers = ["HOSTNAME", "IP", "PORT", "ROLE", "VERSION", "CAPS", "MACHINE_ID", "SEEN"]
    rows: list[list[str]] = []
    for n in sorted(nodes, key=lambda x: x.hostname.lower()):
        rows.append([
            n.hostname or "-",
            n.ip or "-",
            str(n.port),
            n.role or "-",
            n.version or "-",
            ",".join(n.caps) if n.caps else "-",
            (n.machine_id[:12] + "...") if len(n.machine_id) > 12 else (n.machine_id or "-"),
            _fmt_ts(n.last_seen),
        ])

    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def _row(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines = [_row(headers), _row(["-" * w for w in widths])]
    for r in rows:
        lines.append(_row(r))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns process exit code.

    Flags match the spec's ``nvmon discover --debug`` shape.
    """
    parser = argparse.ArgumentParser(
        prog="nvmon discover",
        description="List nvmon services advertised on the LAN via mDNS.",
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0,
        help="Seconds to listen before printing (default: 5).",
    )
    parser.add_argument(
        "--service-type", default=SERVICE_TYPE,
        help=f"Override service type (default: {SERVICE_TYPE}).",
    )
    parser.add_argument(
        "--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS,
        help="Eviction TTL (default: 120).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every appear/update/goodbye event as it happens.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    print(
        f"Browsing {args.service_type} for {args.timeout:.1f}s ..."
        " (Ctrl+C to stop early)",
        file=sys.stderr,
    )

    registry = DiscoveryRegistry(
        service_type=args.service_type,
        ttl_seconds=args.ttl_seconds,
    )

    def _on_event(kind: str, node: DiscoveredNode) -> None:
        if not args.verbose:
            return
        now = datetime.now(timezone.utc)
        caps = ",".join(node.caps) if node.caps else "-"
        print(
            f"{_fmt_ts(now)} [{kind:<12}] "
            f"{node.hostname} {node.ip}:{node.port} role={node.role} "
            f"caps=[{caps}] machine_id={node.machine_id[:12]}",
            file=sys.stderr,
        )

    registry.on_change(_on_event)

    try:
        registry.start()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        # Poll with small granularity so Ctrl+C is snappy.
        end = time.monotonic() + max(0.0, args.timeout)
        while time.monotonic() < end:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n(interrupted)", file=sys.stderr)
    finally:
        nodes = registry.get_all()
        registry.stop()

    print(_fmt_table(nodes))

    # Exit 0 if we found anything, 1 otherwise. Scriptable.
    return 0 if nodes else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
