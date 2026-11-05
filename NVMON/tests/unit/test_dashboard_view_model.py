"""Tests for the dashboard view-model: cascade behavior + render contract.

The agent's /status payload is forgiving — DD may be empty while Mosaic +
QSync know the rate. The view-model's job is to give the dashboard a
single source of truth for `displays_count` and `refresh_hz` regardless of
which subsystem reported it. These tests pin that down.
"""

from __future__ import annotations

import pytest

from nvmon.controller.dashboard_unified.view_model import (
    DashboardViewModel,
    _displays_count,
    _filter_validation,
    _natural_key,
    _refresh_hz,
    _qsync_view,
    _dd_view,
    _mosaic_view,
)
from nvmon.controller.registry import NodeRegistry


def test_fix_hints_render_in_header_dropdown_and_refresh_source():
    """v0.9.10: hints from /status.remedies show in the header dropdown with
    a count, and a hidden copy sits inside #fleet for the soft refresh."""
    from nvmon.controller.dashboard_unified import render
    status = _qsync_locked_status(60.0)
    status["remedies"] = [{"id": "link_too_small", "action": "reseat",
                           "title": "M2 cannot reach 12-bit — reseat its DisplayPort at the GPU",
                           "detail": "…", "target": "M2 (82061085)", "evidence": "link HBR3 8.10 x2"}]
    snap = {"nodes": [{"hostname": "NODE-1", "url": "http://10.0.0.11:9998", "reachable": True,
                       "last_seen": 0, "capabilities": {},
                       "health": {"capabilities": {"qsync": {"available": True}}}, "status": status}],
            "last_update": 0, "node_count": 1}
    reg = NodeRegistry(nodes_cfg=[{"hostname": "NODE-1", "url": "http://10.0.0.11:9998"}],
                       groups_cfg=[{"name": "Wall", "nodes": ["NODE-1"]}])
    html = render.render_dashboard(DashboardViewModel().build(snap, reg))
    hdr = html[html.index('id="fix-hints-btn"'):html.index('id="fix-hints-body"')]
    assert "has" in html[html.index('class="btn btn-ghost fix-hints-btn'):html.index('id="fix-hints-btn"') + 80]
    assert "(1)" in hdr
    body = html[html.index('id="fix-hints-body"'):]
    assert "RESEAT" in body[:2000] and "NODE-1 · M2 (82061085)" in body[:2000]
    fleet = html[html.index('id="fleet"'):]
    src = fleet[fleet.index('id="fix-hints-src"'):]
    assert 'data-count="1"' in src[:80] and "link HBR3 8.10 x2" in src[:2000]
    # no hints -> plain button, empty message
    status.pop("remedies")
    html2 = render.render_dashboard(DashboardViewModel().build(snap, reg))
    assert "Nothing stuck that nvmon recognises." in html2 and 'data-count="0"' in html2


def test_list_rows_carry_group_and_live_status_fields():
    """v0.9.8: list view honours the Wall/Ceiling filter and live offline
    flips — the rows need data-group and a status_text span the JS updates."""
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[2] / "nvmon" / "controller" / "dashboard_unified"
           / "templates" / "dashboard.html").read_text(encoding="utf-8")
    row = tpl[tpl.index('<tr class="node-row"'):]
    row = row[:row.index("</tr>")]
    assert 'data-group="{{ n.group }}"' in row
    assert 'data-field="status"' in row and 'data-field="status_text"' in row
    js = (Path(__file__).resolve().parents[2] / "nvmon" / "controller" / "dashboard_unified"
          / "static" / "js" / "nvmon.js").read_text(encoding="utf-8")
    assert "querySelectorAll('.node, .node-row')" in js
    assert ".node-row[data-hostname=" in js[js.index("node_reachable_change', function"):]


def _qsync_locked_status(refresh: float = 23.976) -> dict:
    """A status dict that mirrors a real 2x2 Mosaic + HOUSESYNC node where
    DD has been disabled (so direct_display is empty)."""
    return {
        "hostname": "NODE-1",
        "ip": "10.0.0.11",
        "timestamp": "2026-04-28T12:00:00Z",
        "active_profile": "default",
        "uptime_seconds": 3600,
        "poll_count": 720,
        "direct_display": {"active": False, "display_count": 0, "displays": []},
        "mosaic": {
            "enabled": True, "rows": 2, "cols": 2,
            "per_display_resolution": [3840, 2160],
            "total_resolution": [7680, 4320],
            "refresh_rate": refresh, "output_count": 4,
            "synchronized": True,
            "outputs": [
                {"gpu": 1, "display": d, "display_id": f"0x8206108{d}"}
                for d in range(4)
            ],
        },
        "qsync": {
            "available": True, "synced": True,
            "house_sync_detected": True, "house_sync_incoming_hz": refresh,
            "source": "HOUSESYNC", "trigger": "FALLING",
            "per_gpu_sync": [
                {"gpu": 0, "synced": True, "raw_synced": False,
                 "signal_available": True, "sync_note": "via HOUSESYNC"},
            ],
        },
        "gpu": [{
            "index": 0, "name": "RTX A6000",
            "temperature_c": 62, "power_draw_w": 150.0, "power_limit_w": 300.0,
            "gpu_utilization_pct": 45, "memory_used_mb": 5000, "memory_total_mb": 24576,
            "fan_speed_pct": 30, "pstate": "P2", "driver_version": "595.79",
            "pci_bus_id": "00000000:01:00.0",
            "gpu_clock_mhz": 1500, "mem_clock_mhz": 7000, "video_clock_mhz": 1500,
            "display_active": True, "pcie_gen": 4, "pcie_width": 16,
        }],
        "edid": {"available": True, "registry_overrides": {}, "edid_files": []},
        "output_health": {
            "total_displays": 0, "dd_active_count": 0, "dd_expected_count": 4,
            "all_same_refresh": True, "all_same_resolution": True,
            "refresh_rate_hz": refresh,
        },
        "alerts": [], "errors": [],
    }


def test_displays_count_falls_back_to_mosaic_when_dd_empty() -> None:
    s = _qsync_locked_status()
    assert _displays_count(s) == 4


def test_displays_count_prefers_dd_when_present() -> None:
    s = _qsync_locked_status()
    s["direct_display"] = {
        "active": True, "display_count": 6,
        "displays": [{}] * 6,
    }
    assert _displays_count(s) == 6


def test_displays_count_falls_back_to_qsync_when_no_dd_no_mosaic() -> None:
    s = _qsync_locked_status()
    s["mosaic"] = {"enabled": False, "output_count": 0}
    # Two GPUs in QSync, only one with signal
    s["qsync"]["per_gpu_sync"] = [{"gpu": 0}, {"gpu": 1}]
    assert _displays_count(s) == 2


def test_refresh_hz_uses_output_health_first() -> None:
    s = _qsync_locked_status(refresh=24.0)
    assert _refresh_hz(s) == pytest.approx(24.0)


def test_refresh_hz_falls_back_to_mosaic_then_qsync() -> None:
    s = _qsync_locked_status(refresh=23.976)
    s["output_health"]["refresh_rate_hz"] = 0.0
    assert _refresh_hz(s) == pytest.approx(23.976)  # via mosaic.refresh_rate
    s["mosaic"]["refresh_rate"] = 0.0
    assert _refresh_hz(s) == pytest.approx(23.976)  # via qsync.house_sync_incoming_hz
    s["qsync"]["house_sync_incoming_hz"] = 0.0
    assert _refresh_hz(s) == 0.0


def test_qsync_view_locks_when_house_sync_detected_and_synced() -> None:
    s = _qsync_locked_status()
    v = _qsync_view(s)
    assert v == {"status": "locked", "level": "ok", "available": True}


def test_qsync_view_crit_when_house_only_no_sync() -> None:
    """House-sync detected but QSync not actually engaged is now CRIT (red),
    not warn — the operator explicitly asked for this: "house-sync without
    qsync is just default sync, not real qsync, it should be red"."""
    s = _qsync_locked_status()
    s["qsync"]["synced"] = False
    v = _qsync_view(s)
    assert v["level"] == "crit"
    assert "house" in v["status"].lower()


def test_qsync_view_off_when_subsystem_unavailable() -> None:
    s = _qsync_locked_status()
    s["qsync"]["available"] = False
    assert _qsync_view(s)["level"] == "off"


def test_qsync_view_no_card_when_exe_runs_but_signal_fields_empty() -> None:
    """Tool exe is present but the host has no QSync hardware → exe returns
    minimal output, parser sets available=True but every signal field stays
    zero.  The dashboard must NOT lie and say UNLOCKED — that implies a
    card is there and losing sync.  The user complained about this exact
    case on a Tessera SX40 setup with no QSync card."""
    s = _qsync_locked_status()
    s["qsync"] = {
        "available": True,
        "synced": False,
        "house_sync_detected": False,
        "house_sync_incoming_hz": 0.0,
        "source": "",
        "trigger": "",
        "per_gpu_sync": [],
    }
    v = _qsync_view(s)
    assert v["level"] == "off"
    assert "card" in v["status"].lower()


def test_dd_view_off_when_no_displays() -> None:
    s = _qsync_locked_status()
    assert _dd_view(s)["level"] == "off"


def test_dd_view_warn_when_displays_present_but_inactive() -> None:
    s = _qsync_locked_status()
    s["direct_display"] = {
        "active": False, "display_count": 4, "displays": [{}] * 4,
    }
    v = _dd_view(s)
    assert v["level"] == "warn"


def test_mosaic_view_reports_grid_dimensions() -> None:
    s = _qsync_locked_status()
    assert _mosaic_view(s) == {"status": "2x2", "level": "ok", "available": True}


def test_build_full_context_round_trip() -> None:
    """End-to-end: build a context for one node and verify all expected
    keys are present so the templates won't KeyError at render time."""
    snap = {
        "nodes": [{
            "hostname": "NODE-1",
            "url": "http://10.0.0.11:9998",
            "reachable": True,
            "last_seen": 0,
            "capabilities": {},
            "health": {"capabilities": {"qsync": {"available": True}}},
            "status": _qsync_locked_status(),
        }],
        "last_update": 0, "node_count": 1,
    }
    reg = NodeRegistry(
        nodes_cfg=[{"hostname": "NODE-1", "url": "http://10.0.0.11:9998"}],
        groups_cfg=[{"name": "outputs", "nodes": ["NODE-1"]}],
    )
    vm = DashboardViewModel()
    ctx = vm.build(snap, reg)

    assert ctx["controller"]["nodes_online"] == 1
    assert ctx["controller"]["nodes_offline"] == 0
    assert len(ctx["nodes"]) == 1

    n = ctx["nodes"][0]
    assert n["hostname"] == "NODE-1"
    assert n["online"] is True
    assert n["group"] == "outputs"
    assert n["displays_count"] == 4
    assert n["refresh_hz"] == pytest.approx(23.976)
    assert n["subsystems"]["qsync"]["level"] == "ok"
    # DD is off — but the Mosaic fallback synthesized 4 display rows for
    # the slide-over table.
    assert len(n["displays"]) == 4
    assert n["displays"][0]["resolution"] == [3840, 2160]


# ---------------------------------------------------------------------------
# Chain consistency (site, 2026-11-01): a client is only green when its
# group's timing server is locked to house sync at the same rate.
# ---------------------------------------------------------------------------

def _snap_node(hostname: str, status: dict) -> dict:
    return {
        "hostname": hostname, "url": f"http://{hostname}:9998", "reachable": True,
        "last_seen": 0, "capabilities": {}, "health": {}, "status": status,
    }


def _server_status(hostname: str, rate: float, *, locked: bool = True) -> dict:
    s = _qsync_locked_status(refresh=rate)
    s["hostname"] = hostname
    s["qsync"]["role"] = "server"
    if not locked:
        s["qsync"]["synced"] = False
        s["qsync"]["role"] = ""
        s["qsync"]["per_gpu_sync"] = [{"gpu": 0, "synced": False, "sync_state": ""}]
    return s


def _client_status(hostname: str, rate: float, *, synced: bool = True) -> dict:
    s = _qsync_locked_status(refresh=rate)
    s["hostname"] = hostname
    s["qsync"] = {
        "available": True, "synced": synced, "house_sync_detected": False,
        "house_sync_incoming_hz": 0.0, "source": "VSYNC", "trigger": "FALLING",
        "role": "client" if synced else "",
        "per_gpu_sync": [{"gpu": 0, "synced": synced, "sync_state": "CLIENT" if synced else ""}],
    }
    return s


def _build_wall(server: dict, client: dict, *, grouped: bool = True) -> tuple[dict, dict]:
    snap = {"nodes": [_snap_node("RENDER-01", server), _snap_node("RENDER-02", client)],
            "last_update": 0, "node_count": 2}
    reg = NodeRegistry(
        nodes_cfg=[{"hostname": "RENDER-01", "url": "http://a:9998"},
                   {"hostname": "RENDER-02", "url": "http://b:9998"}],
        groups_cfg=[{"name": "Wall", "nodes": ["RENDER-01", "RENDER-02"]}] if grouped else [],
    )
    ctx = DashboardViewModel().build(snap, reg)
    by = {n["hostname"]: n for n in ctx["nodes"]}
    return by["RENDER-01"], by["RENDER-02"]


def test_chain_ok_when_server_locked_same_rate() -> None:
    srv, cl = _build_wall(_server_status("RENDER-01", 23.976), _client_status("RENDER-02", 23.976))
    assert cl["chain"]["level"] == "ok"
    assert cl["chain"]["upstream"] == "RENDER-01"
    assert cl["subsystems"]["qsync"]["level"] == "ok"
    assert srv["chain"]["upstream"] == ""   # servers have no upstream


def test_chain_warns_when_upstream_not_locked() -> None:
    """NODE-06/07 case: client locked to NODE-05's pulse while NODE-05 sat
    UNLOCKED (house feed moved to 23.976). Must not render green."""
    _, cl = _build_wall(_server_status("RENDER-01", 59.94, locked=False),
                        _client_status("RENDER-02", 59.94))
    assert cl["chain"]["level"] == "warn"
    assert cl["chain"]["upstream"] == "RENDER-01"
    assert cl["chain"]["detail"] == "not synced"
    assert cl["subsystems"]["qsync"]["level"] == "warn"
    assert cl["subsystems"]["qsync"]["status"] == "client · upstream RENDER-01 not synced"


def test_chain_warns_on_rate_mismatch() -> None:
    _, cl = _build_wall(_server_status("RENDER-01", 23.976), _client_status("RENDER-02", 59.94))
    assert cl["chain"]["level"] == "warn"
    assert "rate" in cl["chain"]["detail"]
    assert cl["subsystems"]["qsync"]["level"] == "warn"


def test_chain_flags_23976_client_under_24_server() -> None:
    """site 2026-11-02: hosts at 24.000, clients left at 23.976 — 0.024 Hz
    apart, and the old 0.05 Hz chain tolerance rendered them green."""
    _, cl = _build_wall(_server_status("RENDER-01", 24.0), _client_status("RENDER-02", 23.976))
    assert cl["chain"]["level"] == "warn"
    assert cl["chain"]["detail"] == "rate 23.976 vs 24.000 Hz"
    assert cl["subsystems"]["qsync"]["level"] == "warn"
    # Third-decimal disagreement between the NVIDIA tools is still a match.
    _, cl = _build_wall(_server_status("RENDER-01", 23.976), _client_status("RENDER-02", 23.975))
    assert cl["chain"]["level"] == "ok"


def test_chain_ignored_for_ungrouped_nodes() -> None:
    _, cl = _build_wall(_server_status("RENDER-01", 23.976), _client_status("RENDER-02", 59.94),
                        grouped=False)
    assert cl["chain"] == {"level": "ok", "upstream": "", "detail": ""}
    assert cl["subsystems"]["qsync"]["level"] == "ok"


def test_restore_relevant_only_with_dd_or_mosaic() -> None:
    """Restore buttons are hidden on a desktop-only QSync rig (site)."""
    srv, cl = _build_wall(_server_status("RENDER-01", 23.976), _client_status("RENDER-02", 23.976))
    assert srv["restore_relevant"] is True          # fixture has Mosaic 2x2 on
    desktop = _server_status("RENDER-01", 23.976)
    desktop["mosaic"] = {"enabled": False, "output_count": 0}
    desktop["direct_display"] = {"active": False, "display_count": 2, "displays": [{}, {}]}
    c2 = _client_status("RENDER-02", 23.976)
    c2["mosaic"] = {"enabled": False, "output_count": 0}
    c2["direct_display"] = {"active": False, "display_count": 2, "displays": [{}, {}]}
    snap = {"nodes": [_snap_node("RENDER-01", desktop), _snap_node("RENDER-02", c2)],
            "last_update": 0, "node_count": 2}
    reg = NodeRegistry(
        nodes_cfg=[{"hostname": "RENDER-01", "url": "http://a:9998"},
                   {"hostname": "RENDER-02", "url": "http://b:9998"}],
        groups_cfg=[{"name": "Wall", "nodes": ["RENDER-01", "RENDER-02"]}],
    )
    ctx = DashboardViewModel().build(snap, reg)
    assert all(n["restore_relevant"] is False for n in ctx["nodes"])
    assert ctx["controller"]["restore_relevant"] is False


def test_chain_leaves_unsynced_client_crit() -> None:
    """An unlocked client is already CRIT; the chain pass must not touch it."""
    _, cl = _build_wall(_server_status("RENDER-01", 23.976),
                        _client_status("RENDER-02", 23.976, synced=False))
    assert cl["subsystems"]["qsync"]["level"] == "crit"


def test_build_handles_offline_node() -> None:
    """Offline nodes must not crash the builder — status is None."""
    snap = {
        "nodes": [{
            "hostname": "DEAD-NODE",
            "url": "http://10.0.0.99:9998",
            "reachable": False,
            "last_seen": 1700000000.0,
            "capabilities": {},
            "health": None,
            "status": None,
        }],
        "last_update": 0, "node_count": 1,
    }
    reg = NodeRegistry(
        nodes_cfg=[{"hostname": "DEAD-NODE", "url": "http://10.0.0.99:9998"}],
        groups_cfg=[],
    )
    ctx = DashboardViewModel().build(snap, reg)
    assert ctx["controller"]["nodes_offline"] == 1
    n = ctx["nodes"][0]
    assert n["online"] is False
    assert n["displays_count"] == 0
    assert n["refresh_hz"] == 0.0
    assert n["subsystems"]["qsync"]["level"] == "off"


# ---------------------------------------------------------------------------
# Validation panel filtering
# ---------------------------------------------------------------------------

def test_filter_validation_drops_not_required_rows() -> None:
    """Checks whose detail says 'not required by profile' are noise — they
    pass trivially and clutter the panel with irrelevant green ticks. The
    filter drops them and recomputes all_passed over the kept set."""
    raw = {
        "all_passed": True,
        "checks": {
            "dd_active":     {"passed": True, "detail": "DD not required by profile"},
            "qsync_locked":  {"passed": True, "detail": "QSync not required by profile"},
            "house_sync":    {"passed": True, "detail": "QSync not required by profile"},
            "refresh_rate":  {"passed": True, "detail": "24.000Hz (no profile expectation)"},
            "gpu_health":    {"passed": True, "detail": "1 GPUs, temps 34-34C"},
        },
    }
    out = _filter_validation(raw)
    assert set(out["checks"].keys()) == {"refresh_rate", "gpu_health"}
    assert out["all_passed"] is True


def test_filter_validation_recomputes_all_passed_when_a_check_fails() -> None:
    raw = {
        "all_passed": True,  # stale
        "checks": {
            "dd_active":    {"passed": True, "detail": "DD not required by profile"},
            "refresh_rate": {"passed": False, "detail": "24.000Hz (expected 30.000Hz)"},
        },
    }
    out = _filter_validation(raw)
    assert "dd_active" not in out["checks"]
    assert out["all_passed"] is False


def test_filter_validation_honors_explicit_skipped_flag() -> None:
    """Forward-compatible with a future validator that emits skipped=True
    explicitly instead of a 'not required by profile' detail string."""
    raw = {
        "checks": {
            "dd_active":   {"passed": True, "detail": "ok", "skipped": True},
            "gpu_health":  {"passed": True, "detail": "ok"},
        },
    }
    out = _filter_validation(raw)
    assert list(out["checks"].keys()) == ["gpu_health"]


def test_filter_validation_empty_input_returns_empty_dict() -> None:
    assert _filter_validation({}) == {"checks": {}, "all_passed": False}
    assert _filter_validation(None) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Natural hostname sort
# ---------------------------------------------------------------------------

def test_natural_key_orders_numeric_suffixes_correctly() -> None:
    """NODE-2 must come before NODE-10. Lexical sort would put NODE-10 first."""
    names = ["NODE-1", "NODE-10", "NODE-11", "NODE-2", "NODE-3", "NODE-9"]
    sorted_names = sorted(names, key=_natural_key)
    assert sorted_names == ["NODE-1", "NODE-2", "NODE-3", "NODE-9", "NODE-10", "NODE-11"]


def test_natural_key_handles_mixed_text_and_numeric() -> None:
    """Names without trailing numbers still sort cleanly alongside numbered ones."""
    names = ["render-2", "control", "render-10", "alpha"]
    out = sorted(names, key=_natural_key)
    assert out == ["alpha", "control", "render-2", "render-10"]


def test_natural_key_case_insensitive() -> None:
    assert _natural_key("NODE-1") == _natural_key("node-1")
