"""View-model builder.

Converts the controller aggregator's raw snapshot
(``{"nodes":[{"hostname":..., "status": <agent /status JSON>, ...}]}``)
into the flat shape the Jinja2 templates expect — see
``fixtures/mock_status.json`` for the canonical contract.

Designed to be tolerant: any missing field in the raw status falls back to a
safe default. The agent's monitor.py already cascades refresh-rate detection
across DD → Mosaic → QSync; the view-model just trusts the resulting
``output_health.refresh_rate_hz`` and provides further fallbacks for
``displays_count`` when DD is absent (mosaic/qsync managed displays).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ... import config
from .. import registry as registry_mod


# Subsystems whose cards are primary by default. Mosaic + DD are secondary —
# the operator opts in via the per-card visibility toggle (persisted to
# localStorage on the client). Server-side default just hides them in the
# initial markup; once toggled on, the JS reveals them.
PRIMARY_SUBSYSTEMS: tuple[str, ...] = ("gpu", "qsync", "edid", "output", "profile")
OPTIONAL_SUBSYSTEMS: tuple[str, ...] = ("dd", "mosaic")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Subsystem level/status — used for the colored dots on each card.
# ---------------------------------------------------------------------------

def _dd_view(status: dict | None) -> dict:
    """Map the DD slice → {status, level} for the dot indicator."""
    if not status:
        return {"status": "unknown", "level": "off", "available": False}
    dd = status.get("direct_display") or {}
    if not isinstance(dd, dict):
        return {"status": "unknown", "level": "off", "available": False}
    if dd.get("active"):
        return {"status": "active", "level": "ok", "available": True}
    cnt = int(dd.get("display_count") or 0)
    if cnt > 0:
        return {"status": "inactive", "level": "warn", "available": True}
    return {"status": "off", "level": "off", "available": False}


def _mosaic_view(status: dict | None) -> dict:
    if not status:
        return {"status": "unknown", "level": "off", "available": False}
    mos = status.get("mosaic") or {}
    if not isinstance(mos, dict):
        return {"status": "unknown", "level": "off", "available": False}
    if mos.get("enabled"):
        rows = int(mos.get("rows") or 0)
        cols = int(mos.get("cols") or 0)
        return {
            "status": f"{rows}x{cols}" if rows and cols else "on",
            "level": "ok" if mos.get("synchronized", True) else "warn",
            "available": True,
        }
    return {"status": "off", "level": "off", "available": False}


def _qsync_view(status: dict | None) -> dict:
    if not status:
        return {"status": "unknown", "level": "off", "available": False}

    # Highest priority: the agent flagged a privilege failure. Showing
    # UNLOCKED in this case is actively misleading — the card may be
    # present and locked, we just couldn't read it. Make the operator's
    # next step obvious.
    errors = status.get("errors") or []
    for err in errors:
        if isinstance(err, str) and "needs admin" in err.lower():
            return {
                "status": "agent not admin",
                "level": "crit",
                "available": False,
            }

    q = status.get("qsync") or {}
    if not isinstance(q, dict):
        return {"status": "unknown", "level": "off", "available": False}
    if not q.get("available"):
        return {"status": "off", "level": "off", "available": False}

    # Discriminator: WHAT is the card locked to?
    # - role=client (parser sets it from per-GPU sync_state=CLIENT) → real
    #                fleet sync via daisy-chain; render OK regardless of
    #                what `source` says (clients legitimately report VSYNC
    #                because they have no BNC).
    # - source=HOUSESYNC + house_sync_detected=True  → master at top of chain (real fleet sync)
    # - source=HOUSESYNC + house_sync_detected=False → daisy-chain slave (locked via RJ45 to
    #                                                  upstream; chain delivers house clock,
    #                                                  this node has no BNC input — legit)
    # - source=VSYNC / INTERNAL with no client-state → locked to OWN INTERNAL CLOCK only.
    #                                                  The card reports synced=True but it's
    #                                                  not part of the daisy chain — the
    #                                                  operator's "node says synced but only
    #                                                  to itself, not the upstream machine"
    #                                                  bug. Render CRIT so it can't masquerade
    #                                                  as legit fleet sync.
    src = str(q.get("source") or "").upper()
    is_internal = src in ("VSYNC", "INTERNAL", "")
    role = str(q.get("role") or "").lower()
    if role == "client" and q.get("synced"):
        return {"status": "client", "level": "ok", "available": True}
    if q.get("synced") and not is_internal and q.get("house_sync_detected"):
        return {"status": "locked", "level": "ok", "available": True}
    if q.get("synced") and not is_internal:
        # source=HOUSESYNC but no BNC → daisy-chain slave. Real fleet sync.
        return {"status": "chain", "level": "ok", "available": True}
    if q.get("synced") and is_internal:
        return {"status": "locked-to-self", "level": "crit", "available": True}
    if q.get("house_sync_detected"):
        # House sync signal is present but QSync isn't actually engaged
        # (no per-GPU lock). Render this as CRIT (red) — the operator
        # asked for this explicitly: "house-sync without qsync is just
        # default sync, not real qsync, so it should be red".
        return {"status": "house-only", "level": "crit", "available": True}
    # exe runs but every signal field is empty → no QSync hardware in this
    # node.  Showing "UNLOCKED" here is misleading (it implies the card is
    # there but losing sync); "no card" is the truthful state.  The
    # operator can confirm via the Diagnose → QSync raw button in the
    # slide-over.
    no_signal = (
        not (q.get("per_gpu_sync") or [])
        and not q.get("house_sync_incoming_hz")
        and not q.get("source")
        and not q.get("trigger")
    )
    if no_signal:
        return {"status": "no card", "level": "off", "available": False}
    return {"status": "unlocked", "level": "crit", "available": True}


# ---------------------------------------------------------------------------
# Display count + refresh fallback
# ---------------------------------------------------------------------------

def _displays_count(status: dict | None) -> int:
    """Count managed displays. Falls back across DD → Mosaic → QSync.

    The agent's DD parser only sees DDC-managed displays; on a node where DD
    is off but Mosaic is enabled, ``mosaic.output_count`` is the source of
    truth (4 for a 2x2 grid). QSync is a last-resort fallback that just
    reflects how many GPUs reported any sync activity.
    """
    if not status:
        return 0
    dd = status.get("direct_display") or {}
    cnt = int(dd.get("display_count") or 0)
    if cnt > 0:
        return cnt
    mos = status.get("mosaic") or {}
    cnt = int(mos.get("output_count") or 0)
    if cnt > 0:
        return cnt
    q = status.get("qsync") or {}
    per_gpu = q.get("per_gpu_sync") or []
    return sum(1 for g in per_gpu if isinstance(g, dict))


def _color_text(status: dict | None) -> str:
    """Live colour depth / pixel encoding, e.g. ``10-bit Ycc422``; mixed
    panels become ``10-bit Ycc422 · 8-bit Rgb444``; '' when Windows did not
    report it. From the DD display list (WinRT wire format) — the same
    numbers the validator's ``color_depth`` check compares. (site
    2026-11-04: operators had no way to see from the fleet view that six
    nodes sat at 8-bit RGB under a ``- 10b -`` profile.)"""
    if not status:
        return ""
    dd = status.get("direct_display") or {}
    seen: list[str] = []
    for d in dd.get("displays") or []:
        if not isinstance(d, dict):
            continue
        try:
            bits = int(d.get("bits_per_channel") or 0)
        except (TypeError, ValueError):
            bits = 0
        enc = str(d.get("pixel_encoding") or "").strip()
        if not bits and not enc:
            continue
        label = (f"{bits}-bit" if bits else "?-bit") + (f" {enc}" if enc else "")
        seen.append(label)
    if not seen:
        return ""
    from collections import Counter
    counter = Counter(seen)
    return " · ".join(label for label, _ in counter.most_common())


def _resolution_text(status: dict | None) -> str:
    """Best-effort human resolution string: '3840x2160', '7680x4320' (mosaic
    total), or '' if unknown.  Falls back DD displays → mosaic per-display →
    mosaic total. The Output card shows this so the operator gets pixel
    dimensions at a glance, not just the displays count."""
    if not status:
        return ""
    dd = status.get("direct_display") or {}
    raw_displays = dd.get("displays") or []
    seen: list[tuple[int, int]] = []
    for d in raw_displays:
        if not isinstance(d, dict):
            continue
        res = d.get("resolution") or [0, 0]
        try:
            w, h = int(res[0] or 0), int(res[1] or 0)
        except (TypeError, ValueError, IndexError):
            continue
        if w and h:
            seen.append((w, h))
    if seen:
        # Most common resolution; if mixed, stitch a "WxH+WxH" label.
        from collections import Counter
        counter = Counter(seen)
        if len(counter) == 1:
            w, h = next(iter(counter))
            return f"{w}x{h}"
        return " · ".join(f"{w}x{h}" for (w, h), _ in counter.most_common())
    mos = status.get("mosaic") or {}
    pdr = mos.get("per_display_resolution") or [0, 0]
    try:
        w, h = int(pdr[0] or 0), int(pdr[1] or 0)
    except (TypeError, ValueError, IndexError):
        w, h = 0, 0
    if w and h:
        return f"{w}x{h}"
    return ""


def _natural_key(s: str) -> tuple:
    """Sort key that puts NODE-2 before NODE-10.

    Splits the string into alternating text + integer chunks and returns a
    tuple of the same. Tuple comparison is element-wise, so as long as the
    surrounding text is identical, the embedded integers compare numerically.
    Non-numeric names sort cleanly against numbered ones because the number
    chunk is missing from both at the same position.
    """
    import re as _re
    parts: list = []
    for chunk in _re.split(r"(\d+)", (s or "").lower()):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((1, int(chunk)))  # 1 = numeric tier
        else:
            parts.append((0, chunk))       # 0 = text tier
    return tuple(parts)


def _filter_validation(validation: dict) -> dict:
    """Drop check rows the profile says aren't applicable.

    A green tick saying "DD not required by profile" or "QSync not required
    by profile" is noise — operators only care about checks that actually
    have an opinion about the live state. We drop those rows AND recompute
    `all_passed` over the remaining set so the panel header agrees with the
    rows shown. The agent still produces every check in /status so
    /validate raw / debugging is unaffected.

    A check is considered "not applicable" when ANY of:
      - its detail string contains "not required by profile"
      - its detail string contains "not in profile" (mosaic emits this)
      - its detail string contains "skipped" (catch-all for future
        validators that say "<X> not in profile — skipped")
      - it has an explicit `skipped: True` flag (forward-compatible with
        a future validator that emits the flag directly)
    """
    if not isinstance(validation, dict):
        return {}
    raw_checks = validation.get("checks") or {}
    if not isinstance(raw_checks, dict):
        return dict(validation)
    kept: dict = {}
    for name, c in raw_checks.items():
        if not isinstance(c, dict):
            continue
        detail = str(c.get("detail") or "").lower()
        if c.get("skipped") is True:
            continue
        if "not required by profile" in detail:
            continue
        if "not in profile" in detail:
            continue
        if "skipped" in detail:
            continue
        kept[name] = c
    out = dict(validation)
    out["checks"] = kept
    out["all_passed"] = bool(kept) and all(bool(c.get("passed")) for c in kept.values())
    return out


def _refresh_hz(status: dict | None) -> float:
    """Refresh rate with cascade. Trusts the agent's output_health
    (which already does DD → Mosaic → QSync) and falls back further if
    output_health is missing entirely."""
    if not status:
        return 0.0
    health = status.get("output_health") or {}
    rate = float(health.get("refresh_rate_hz") or 0.0)
    if rate > 0:
        return rate
    mos = status.get("mosaic") or {}
    rate = float(mos.get("refresh_rate") or 0.0)
    if rate > 0:
        return rate
    q = status.get("qsync") or {}
    return float(q.get("house_sync_incoming_hz") or 0.0)


# ---------------------------------------------------------------------------
# Per-node view
# ---------------------------------------------------------------------------

def _build_node(
    raw: dict[str, Any],
    *,
    group_for: dict[str, str],
    sparkline_buf: dict[str, dict[int, list[int]]],
) -> dict[str, Any]:
    hostname = str(raw.get("hostname") or "")
    reachable = bool(raw.get("reachable"))
    status = raw.get("status") if isinstance(raw.get("status"), dict) else None
    health = raw.get("health") if isinstance(raw.get("health"), dict) else {}

    # Online means: aggregator's last poll succeeded *and* status payload was
    # parseable. A node that returns /health but a 5xx /status is degraded but
    # we still consider it online so the operator sees it.
    online = reachable

    last_seen = raw.get("last_seen")
    last_seen_iso = ""
    if isinstance(last_seen, (int, float)) and last_seen > 0:
        last_seen_iso = datetime.fromtimestamp(
            float(last_seen), tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # GPU array — plus sparkline buffer for last 60 samples per (host, gpu).
    gpus: list[dict[str, Any]] = []
    raw_gpus = (status or {}).get("gpu") or []
    for g in raw_gpus:
        if not isinstance(g, dict):
            continue
        idx = int(g.get("index") or 0)
        temp = int(g.get("temperature_c") or 0)
        host_buf = sparkline_buf.setdefault(hostname, {})
        buf = host_buf.setdefault(idx, [])
        buf.append(temp)
        if len(buf) > 60:
            del buf[: len(buf) - 60]
        gpus.append({
            "index": idx,
            "name": g.get("name", ""),
            "temperature_c": temp,
            "power_draw_w": float(g.get("power_draw_w") or 0.0),
            "power_limit_w": float(g.get("power_limit_w") or 0.0),
            "utilization_pct": int(g.get("gpu_utilization_pct") or 0),
            "memory_used_mb": int(g.get("memory_used_mb") or 0),
            "memory_total_mb": int(g.get("memory_total_mb") or 0),
            "fan_speed_pct": int(g.get("fan_speed_pct") or 0),
            "pstate": g.get("pstate", ""),
            "driver_version": g.get("driver_version", ""),
            "sparkline": list(buf),
        })

    subsystems = {
        "dd": _dd_view(status),
        "mosaic": _mosaic_view(status),
        "qsync": _qsync_view(status),
    }

    poll_count = int((status or {}).get("poll_count") or 0)
    uptime = int((status or {}).get("uptime_seconds") or 0)
    last_poll_ms_ago: int | None
    ts = (status or {}).get("timestamp")
    if isinstance(ts, str) and ts:
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            last_poll_ms_ago = max(0, int(
                (datetime.now(timezone.utc) - t).total_seconds() * 1000
            ))
        except (ValueError, TypeError):
            last_poll_ms_ago = None
    else:
        last_poll_ms_ago = None

    alerts = (status or {}).get("alerts") or []
    alerts_count = len([a for a in alerts if isinstance(a, dict)])

    # Capabilities — pulled from /health.
    caps_raw = health.get("capabilities") if isinstance(health, dict) else {}
    capabilities: dict[str, bool] = {}
    if isinstance(caps_raw, dict):
        for name in ("gpu", "displays", "mosaic", "qsync", "edid"):
            entry = caps_raw.get(name)
            capabilities[name] = bool(
                isinstance(entry, dict) and entry.get("available")
            )

    # Whether the agent process is running elevated. Drives the "AGENT NOT
    # ADMIN" banner on the slide-over — without this the dashboard reports
    # garbage QSync/DD data (the exes refuse to run unprivileged) and the
    # operator has no clue why.
    agent_is_admin: bool | None
    if isinstance(health, dict) and "is_admin" in health:
        agent_is_admin = bool(health.get("is_admin"))
    else:
        agent_is_admin = None  # older agent versions pre-v0.2.0

    # Subnet pin (v0.8.2): /health carries subnet_ok when the node's toml
    # has [network].subnet. False = the listener is announcing an address
    # on some other NIC (e.g. Content-Net instead of Control-Net). None = not
    # configured or older agent.
    agent_subnet_ok: bool | None = None
    agent_ip = ""
    if isinstance(health, dict):
        agent_ip = str(health.get("ip") or "")
        if health.get("subnet_ok") is not None:
            agent_subnet_ok = bool(health.get("subnet_ok"))

    # Surface-level info for the slide-over Displays table. These come from
    # DD's parsed display list; if empty (DD off), fall back to mosaic outputs
    # so the operator at least sees something.
    displays_table: list[dict[str, Any]] = []
    dd_block = (status or {}).get("direct_display") or {}
    raw_displays = dd_block.get("displays") or []
    for d in raw_displays:
        if not isinstance(d, dict):
            continue
        res = d.get("resolution") or [0, 0]
        if not isinstance(res, (list, tuple)) or len(res) < 2:
            res = [0, 0]
        displays_table.append({
            "name": d.get("name", "?"),
            "gpu_index": d.get("gpu_index", -1),
            "display_index": d.get("display_index", -1),
            "resolution": [int(res[0] or 0), int(res[1] or 0)],
            "refresh_rate": float(d.get("refresh_rate") or 0.0),
            "refresh_fraction": d.get("refresh_fraction", ""),
            "color_space": d.get("color_space", ""),
            "bits_per_channel": int(d.get("bits_per_channel") or 0),
            "pixel_encoding": d.get("pixel_encoding", ""),
            "connector": d.get("connector", ""),
            "status": d.get("status", ""),
        })
    if not displays_table:
        # Fallback: synthesize a row per Mosaic output / QSync display so the
        # operator can still see *something*.
        mos = (status or {}).get("mosaic") or {}
        per_disp_res = mos.get("per_display_resolution") or [0, 0]
        rate = float(mos.get("refresh_rate") or 0.0)
        for o in mos.get("outputs") or []:
            if not isinstance(o, dict):
                continue
            displays_table.append({
                "name": o.get("display_id", "?"),
                "gpu_index": o.get("gpu", -1),
                "display_index": o.get("display", -1),
                "resolution": [int(per_disp_res[0] or 0), int(per_disp_res[1] or 0)],
                "refresh_rate": rate,
                "refresh_fraction": "",
                "color_space": "",
                "bits_per_channel": 0,
                "pixel_encoding": "",
                "connector": "",
                "status": "via Mosaic",
            })

    return {
        "hostname": hostname,
        "ip": (status or {}).get("ip", "") or "",
        "url": raw.get("url", ""),
        "reachable": reachable,
        "online": online,
        "group": group_for.get(hostname, ""),
        "active_profile": (status or {}).get("active_profile", "") or None,
        "uptime_seconds": uptime,
        "poll_count": poll_count,
        "last_poll_ms_ago": last_poll_ms_ago,
        "last_seen": last_seen_iso,
        "last_poll_error": str(raw.get("last_poll_error") or ""),
        "subsystems": subsystems,
        "displays_count": _displays_count(status),
        "refresh_hz": _refresh_hz(status),
        "resolution_text": _resolution_text(status),
        "color_text": _color_text(status),
        "gpu": gpus,
        "alerts_count": alerts_count,
        "capabilities": capabilities,
        "displays": displays_table,
        "validation": _filter_validation((status or {}).get("validation", {}) or {}),
        "errors": (status or {}).get("errors", []) or [],
        # Informational driver/firmware advisories (v0.8.2). Rendered muted
        # and collapsed in the slide-over; never counted as errors.
        "advisories": (status or {}).get("advisories", []) or [],
        # v0.9.10 fix hints (agent/remedies.py) for the pop-out panel.
        "remedies": [r for r in ((status or {}).get("remedies") or []) if isinstance(r, dict)] if online else [],
        "agent_is_admin": agent_is_admin,
        "agent_subnet_ok": agent_subnet_ok,
        "agent_ip": agent_ip,
        "agent_subnet": str(health.get("subnet") or "") if isinstance(health, dict) else "",
        # Listener version, surfaced from /health for the per-node card
        # / list cell so the operator can verify a redeploy actually took
        # effect on every node before triggering a fan-out.
        "agent_version": str(health.get("version") or "") if isinstance(health, dict) else "",
        # NVIDIA driver version (live, from gpu.status() on this node — not
        # the profile's pinned value). Surfaced in the list view so an
        # operator can spot a node running an old driver across the fleet.
        "nvidia_driver_version": (
            str(gpus[0].get("driver_version") or "") if gpus else ""
        ),
        # QSync role: "server" / "client" / "" (off). Surfaced in the list
        # view as a column so the operator sees the daisy-chain topology
        # at a glance without opening each card.
        "qsync_role": str(((status or {}).get("qsync") or {}).get("role") or ""),
        # Where the registry learned about this node — "config" / "registered"
        # / "discovery". The slide-over uses it to show a Remove from Registry
        # button only for source=="registered" entries (config wins on
        # collision and discovery is best-effort / can't be operator-removed).
        "source": str(raw.get("source") or ""),
        # v0.8.0: how old the agent's snapshot was when we polled it, and
        # the apply/restore job state. While a job owns the NVIDIA tools the
        # agent keeps its last snapshot, so status_age_s grows — the tile
        # shows "stale Ns" instead of a frozen reading that looks live.
        "status_age_s": _status_age(status),
        "status_stale": _status_age(status) > STALE_AFTER_SECONDS,
        "job_active": bool((status or {}).get("job_active")),
        "job": _job_view((status or {}).get("job")),
        "raw_status": status,  # available for the slide-over deep dive
    }


# A snapshot older than this is shown as stale on the tile. Agents poll
# every 5 s; anything beyond three polls means the monitor is paused (apply
# running) or the subsystem queries are hanging.
STALE_AFTER_SECONDS = 15.0

# A daisy-chain client must run the same rate as its timing server. Same
# tolerance as the validator's refresh check — 0.01 Hz, so a 23.976 client
# under a 24.000 server is flagged (site 2026-11-02).
CHAIN_RATE_TOLERANCE_HZ = config.RATE_MATCH_TOLERANCE_HZ


def _apply_chain_checks(nodes: list[dict[str, Any]]) -> None:
    """Fleet-level consistency for daisy-chain clients (site, 2026-11-01).

    A client's card says "synced" whenever its sync card is locked to the
    pulse on the RJ45 input — which stays true while the group's timing
    server free-runs at the wrong rate. site showed NODE-06/07 green at
    59.94 Hz with NODE-05 UNLOCKED because the SDI feed had moved to
    23.976. The chain is only right when the group's server is locked to
    house sync and runs the same rate as the client.

    For every grouped node with live role ``client`` this attaches
    ``node["chain"] = {level, upstream, detail}`` and, when the chain is
    wrong, downgrades the QSync subsystem to WARN with the reason so the
    card border, the list cell and the slide-over all say why. Ungrouped
    nodes carry an empty chain (no topology to check against).
    """
    by_group: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        n["chain"] = {"level": "ok", "upstream": "", "detail": ""}
        by_group.setdefault(str(n.get("group") or ""), []).append(n)

    for group, members in by_group.items():
        if not group:
            continue
        online = [m for m in members if m.get("online")]
        servers = [m for m in online if (m.get("qsync_role") or "") == "server"]
        if not servers:
            # No live SERVER state. The host is usually still identifiable:
            # the one member that is not a client and is configured for
            # house sync (source stays HOUSESYNC while unlocked).
            servers = [
                m for m in online
                if (m.get("qsync_role") or "") != "client"
                and str((((m.get("raw_status") or {}).get("qsync") or {}).get("source")) or "").upper() == "HOUSESYNC"
            ]
            if len(servers) != 1:
                servers = []

        for m in online:
            if (m.get("qsync_role") or "") != "client":
                continue
            chain = m["chain"]
            # Keep `detail` short — the card renders "↑ <upstream> · <detail>",
            # so the hostname must not be repeated in it.
            if not servers:
                chain.update(level="warn", detail=f"no timing server in {group}")
            else:
                srv = servers[0]
                chain["upstream"] = srv.get("hostname") or ""
                sq = (srv.get("subsystems") or {}).get("qsync") or {}
                srv_rate = float(srv.get("refresh_hz") or 0.0)
                my_rate = float(m.get("refresh_hz") or 0.0)
                if sq.get("status") not in ("locked", "chain"):
                    chain.update(level="warn", detail="not synced")
                elif srv_rate > 0 and my_rate > 0 and abs(srv_rate - my_rate) > CHAIN_RATE_TOLERANCE_HZ:
                    chain.update(level="warn", detail=f"rate {my_rate:.3f} vs {srv_rate:.3f} Hz")
            q = (m.get("subsystems") or {}).get("qsync")
            if chain["level"] != "ok" and isinstance(q, dict) and q.get("level") == "ok":
                q["level"] = "warn"
                q["status"] = (
                    f"client · upstream {chain['upstream']} {chain['detail']}"
                    if chain["upstream"] else f"client · {chain['detail']}"
                )


def _status_age(status: dict | None) -> float:
    try:
        return float((status or {}).get("status_age_s") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _job_view(job: Any) -> dict[str, Any]:
    """Normalise the agent's compact job dict (absent on pre-0.8.0 agents)."""
    if not isinstance(job, dict):
        return {"in_progress": False, "action": "", "profile": "", "step": "",
                "success": None, "message": "", "cancelled": False, "known": False}
    return {
        "in_progress": bool(job.get("in_progress")),
        "action": str(job.get("action") or ""),
        "profile": str(job.get("profile") or ""),
        "step": str(job.get("step") or ""),
        "success": job.get("success"),
        "message": str(job.get("message") or ""),
        "cancelled": bool(job.get("cancelled")),
        "finished_at": str(job.get("finished_at") or ""),
        "known": bool(job.get("action")),
    }


# ---------------------------------------------------------------------------
# Builder + state holder
# ---------------------------------------------------------------------------

class DashboardViewModel:
    """Stateful view-model — keeps GPU sparkline ring buffers across polls.

    One instance per controller process. Thread-safe-by-monotonic-state: each
    `build()` call mutates the buffers under no lock because BaseHTTPServer
    serializes responses per connection and the buffers are only read during
    the same call. If the controller ever switches to async, add a lock.
    """

    def __init__(self) -> None:
        # {hostname: {gpu_index: [last 60 temps]}}
        self._sparklines: dict[str, dict[int, list[int]]] = {}
        self._start_time = time.monotonic()
        self._dismissed_alerts: set[str] = set()

    # --------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------

    def build(
        self,
        snapshot: dict[str, Any],
        registry: registry_mod.NodeRegistry,
        *,
        version: str = "0.1.0",
        auth_enabled: bool = False,
        profiles_union: list[str] | None = None,
        self_hostname: str | None = None,
    ) -> dict[str, Any]:
        """Produce the dashboard context dict (matches templates/dashboard.html)."""

        # Group lookup — flatten members so each hostname maps to *one* group.
        # If a node belongs to multiple groups, the first-declared group in
        # nvmon.toml wins (deterministic, surface that in the data field).
        # Tabs render in declaration order too (registry.group_names()).
        group_for: dict[str, str] = {}
        groups_list: list[dict[str, Any]] = []
        for name in registry.group_names():
            members = registry.get_by_group(name)
            for m in members:
                group_for.setdefault(m.hostname, name)
            groups_list.append({
                "name": name,
                "count": len(members),
                "nodes": [m.hostname for m in members],
            })

        # Normalize the controller's own hostname once so we can flag the
        # self-host card and let the template hide its power icon. We take
        # the first DNS label only — Windows hostnames are case-insensitive
        # and FQDN suffixes vary across networks.
        self_norm = ""
        if self_hostname:
            self_norm = (self_hostname or "").strip().lower().split(".")[0]

        nodes_out: list[dict[str, Any]] = []
        for raw in snapshot.get("nodes") or []:
            if not isinstance(raw, dict):
                continue
            n = _build_node(
                raw,
                group_for=group_for,
                sparkline_buf=self._sparklines,
            )
            host_norm = (n.get("hostname") or "").strip().lower().split(".")[0]
            n["is_self"] = bool(self_norm) and host_norm == self_norm
            # Restore (DD off, Mosaic off, QSync off, profile dropped) only
            # means something on a node that actually runs DD or Mosaic.
            # On a desktop-only QSync rig like site it just kills sync, so
            # the buttons are hidden unless the rig uses those subsystems.
            subs = n.get("subsystems") or {}
            n["restore_relevant"] = bool(
                (subs.get("dd") or {}).get("status") == "active"
                or (subs.get("mosaic") or {}).get("status") not in ("off", "unknown", None)
            )
            nodes_out.append(n)

        # Stable ordering — natural-sorted hostname so NODE-1, NODE-2, ...,
        # NODE-9, NODE-10, NODE-11 land in numeric order rather than the
        # lexical NODE-1, NODE-10, NODE-11, NODE-2. _natural_key splits the
        # hostname into alternating text + integer chunks; comparison is
        # per-chunk so non-numeric names still sort cleanly alongside
        # numbered ones.
        #
        # Group is deliberately NOT part of the key: sorting by group name
        # first put site's "Ceiling" (05-07) above "Wall" (01-04) purely
        # because C < W, which reads as nodes 5,6,7,1,2,3,4 on the wall. The
        # group tabs already filter by group; the All view is rack order.
        nodes_out.sort(key=lambda n: _natural_key(n.get("hostname") or ""))

        # Fleet-level chain consistency (needs every node of a group built).
        _apply_chain_checks(nodes_out)

        # Aggregate alerts across nodes — newest first; surface only the first
        # un-dismissed one as the banner candidate via `alerts[0]`.
        alerts_all: list[dict[str, Any]] = []
        for n in nodes_out:
            for a in (n.get("raw_status") or {}).get("alerts") or []:
                if not isinstance(a, dict):
                    continue
                alert_id = f"{n['hostname']}:{a.get('type','')}:{a.get('timestamp','')}"
                alerts_all.append({
                    "id": alert_id,
                    "hostname": n["hostname"],
                    "severity": a.get("severity", "WARNING"),
                    "type": a.get("type", ""),
                    "message": a.get("message", ""),
                    "timestamp": a.get("timestamp", ""),
                    "dismissed": alert_id in self._dismissed_alerts,
                })
        alerts_all.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        nodes_online = sum(1 for n in nodes_out if n["online"])
        nodes_offline = sum(1 for n in nodes_out if not n["online"])

        return {
            "controller": {
                "version": version,
                "uptime_seconds": int(time.monotonic() - self._start_time),
                "stream_status": "live",
                "last_aggregate": _now_iso(),
                "nodes_online": nodes_online,
                "nodes_offline": nodes_offline,
                "alerts_count": sum(
                    1 for a in alerts_all if not a["dismissed"]
                ),
                "auth_enabled": auth_enabled,
                # Header Restore button only when some node runs DD/Mosaic.
                "restore_relevant": any(n.get("restore_relevant") for n in nodes_out),
            },
            "groups": groups_list,
            "alerts": alerts_all,
            "nodes": nodes_out,
            "profiles_union": profiles_union or [],
        }

    def find_node(
        self, ctx: dict[str, Any], hostname: str,
    ) -> dict[str, Any] | None:
        for n in ctx.get("nodes") or []:
            if n.get("hostname") == hostname:
                return n
        return None

    def dismiss_alert(self, alert_id: str) -> None:
        self._dismissed_alerts.add(alert_id)
