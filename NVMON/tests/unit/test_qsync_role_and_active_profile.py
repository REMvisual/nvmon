"""Tests for the QSync role/displays + active-profile fanout changes.

Covers:
- ProfileQSync round-trips role + displays through save/load.
- Legacy `source: "VSYNC"` is normalized to `"HOUSESYNC"` on load.
- profile_capture detects role=client from per-GPU sync_state=CLIENT.
- qsync.enable(role="client") emits `enable client` with no source/trigger.
- qsync.enable_from_profile honors profile.qsync.role and profile.qsync.displays.
- v2 schema QSyncSection round-trips role + displays and normalizes VSYNC.
- v1->v2 migrator preserves role/displays defaults and coerces VSYNC.
- controller fanout._fanout_per_node skips nodes when callback returns None.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nvmon import executors
from nvmon.agent import profile_capture, profiles, qsync
from nvmon.agent.profile_migrate import upgrade_v1_to_v2
from nvmon.agent.profile_schema import QSyncSection
from nvmon.types import (
    CommandResult, GPUSyncInfo, Profile, ProfileQSync, QSyncState,
)


# ---------------------------------------------------------------------------
# Round-trip + VSYNC normalization
# ---------------------------------------------------------------------------

def test_profile_qsync_role_displays_round_trip(tmp_path, monkeypatch):
    """A profile saved with role=client + displays=[(0,0)] reloads with the
    same role/displays. Source is normalised to "" for clients on load — a
    daisy-chain client has no source of its own, so a stored HOUSESYNC
    (legacy capture / hand edit) must not survive the round trip."""
    monkeypatch.setattr(profiles.config, "get_agent_config", lambda: {"profile_dir": str(tmp_path)})
    p = Profile(
        name="rt",
        description="round-trip test",
        qsync=ProfileQSync(
            enabled=True, source="HOUSESYNC", trigger="FALLING",
            role="client", displays=[(0, 0)],
        ),
    )
    profiles.save(p)
    loaded = profiles.load("rt")
    assert loaded.qsync.role == "client"
    assert loaded.qsync.displays == [(0, 0)]
    assert loaded.qsync.source == ""


def test_profile_load_client_keeps_empty_source(tmp_path, monkeypatch):
    """Regression (RENDER-02, 2026-11-01): profile_capture writes source=""
    for a client, but load coerced "" -> HOUSESYNC regardless of role, so the
    saved client profile claimed house sync it never receives. Server
    profiles still get the "" -> HOUSESYNC coercion."""
    monkeypatch.setattr(profiles.config, "get_agent_config", lambda: {"profile_dir": str(tmp_path)})
    (tmp_path / "cl.json").write_text(json.dumps({
        "name": "cl", "qsync": {"enabled": True, "role": "client", "source": ""},
    }), encoding="utf-8")
    (tmp_path / "sv.json").write_text(json.dumps({
        "name": "sv", "qsync": {"enabled": True, "role": "server", "source": ""},
    }), encoding="utf-8")
    assert profiles.load("cl").qsync.source == ""
    assert profiles.load("sv").qsync.source == "HOUSESYNC"
    # v2 schema section agrees.
    assert QSyncSection.from_dict({"role": "client", "source": "HOUSESYNC"}).source == ""
    assert QSyncSection.from_dict({"role": "server", "source": ""}).source == "HOUSESYNC"


def test_profile_load_coerces_legacy_vsync_source(tmp_path, monkeypatch, caplog):
    """A v1 file with source=VSYNC must load as HOUSESYNC (not get pushed
    to configureQsync2.exe verbatim, which would fail)."""
    monkeypatch.setattr(profiles.config, "get_agent_config", lambda: {"profile_dir": str(tmp_path)})
    raw = {
        "name": "legacy",
        "qsync": {"enabled": True, "source": "VSYNC", "trigger": "FALLING", "grid": -1},
    }
    (tmp_path / "legacy.json").write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level("WARNING"):
        loaded = profiles.load("legacy")
    assert loaded.qsync.source == "HOUSESYNC"
    assert any("VSYNC" in r.message for r in caplog.records)


def test_profile_load_coerces_unknown_role(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(profiles.config, "get_agent_config", lambda: {"profile_dir": str(tmp_path)})
    raw = {
        "name": "weirdrole",
        "qsync": {"enabled": True, "role": "primary"},
    }
    (tmp_path / "weirdrole.json").write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level("WARNING"):
        loaded = profiles.load("weirdrole")
    assert loaded.qsync.role == "server"


# ---------------------------------------------------------------------------
# Capture: detect client mode from per-GPU sync_state
# ---------------------------------------------------------------------------

def test_capture_detects_client_role(monkeypatch):
    """When queryAll reports a CLIENT sync state, the captured profile
    must record role=client and the locked (gpu, 0) display.

    Client mode: source must be EMPTY (not HOUSESYNC) — the client locks
    to the upstream node's frame-lock pulse, not to house sync directly.
    """
    fake_state = QSyncState(
        available=True,
        synced=True,
        house_sync_detected=True,
        house_sync_incoming_hz=24.0,
        source="HOUSESYNC",
        trigger="FALLING",
        per_gpu_sync=[
            GPUSyncInfo(gpu=0, signal_available=True, sync_state="CLIENT"),
        ],
    )
    monkeypatch.setattr(profile_capture.qsync_mod, "status", lambda: fake_state)
    monkeypatch.setattr(profile_capture.qsync_mod, "is_available", lambda: True)
    # Other subsystems return None — we only care about qsync here.
    monkeypatch.setattr(profile_capture.mosaic_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "capture_arrangement", lambda: [])
    monkeypatch.setattr(profile_capture.edid_mod, "status", lambda: {})
    monkeypatch.setattr(profile_capture.edid_mod, "read_active_edid_overrides", lambda: {})
    # Suppress the topology-JSON lookup and gpu.status calls so this test
    # is hermetic against whatever's at C:\Data on the box.
    from nvmon.agent import framelock as fl_mod
    monkeypatch.setattr(fl_mod, "_read_topology_json", lambda: None)
    monkeypatch.setattr(fl_mod, "_powershell_exe", lambda: None)
    monkeypatch.setattr(profile_capture.gpu_mod, "status", lambda: [])

    p = profile_capture.capture_current_state()
    assert p.qsync.role == "client"
    assert p.qsync.displays == [(0, 0)]
    assert p.qsync.source == ""  # client: source is empty, not HOUSESYNC


def test_capture_detects_server_role_keeps_housesync(monkeypatch):
    """Server mode must KEEP source=HOUSESYNC — that's the meaningful CLI arg."""
    fake_state = QSyncState(
        available=True, synced=True,
        source="HOUSESYNC", trigger="FALLING",
        per_gpu_sync=[GPUSyncInfo(gpu=0, signal_available=True, sync_state="SERVER")],
    )
    monkeypatch.setattr(profile_capture.qsync_mod, "status", lambda: fake_state)
    monkeypatch.setattr(profile_capture.qsync_mod, "is_available", lambda: True)
    monkeypatch.setattr(profile_capture.mosaic_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "capture_arrangement", lambda: [])
    monkeypatch.setattr(profile_capture.edid_mod, "status", lambda: {})
    monkeypatch.setattr(profile_capture.edid_mod, "read_active_edid_overrides", lambda: {})
    from nvmon.agent import framelock as fl_mod
    monkeypatch.setattr(fl_mod, "_read_topology_json", lambda: None)
    monkeypatch.setattr(fl_mod, "_powershell_exe", lambda: None)
    monkeypatch.setattr(profile_capture.gpu_mod, "status", lambda: [])

    p = profile_capture.capture_current_state()
    assert p.qsync.role == "server"
    assert p.qsync.source == "HOUSESYNC"


def test_capture_two_gpu_server_is_server_not_client(monkeypatch):
    """Regression (RENDER-01, 2026-11-01): on a two-GPU server GPU 0
    reports SERVER and GPU 1 reports CLIENT — an *internal* client of GPU 0
    on the same sync card. The old "any CLIENT → client" rule captured the
    node as role=client with source="", so a later apply would have issued
    `enable client` on the house-sync master. SERVER must win.
    """
    fake_state = QSyncState(
        available=True, synced=True, house_sync_detected=True,
        house_sync_incoming_hz=59.941,
        source="HOUSESYNC", trigger="FALLING",
        per_gpu_sync=[
            GPUSyncInfo(gpu=0, signal_available=True, sync_state="SERVER"),
            GPUSyncInfo(gpu=1, signal_available=True, sync_state="CLIENT"),
        ],
    )
    monkeypatch.setattr(profile_capture.qsync_mod, "status", lambda: fake_state)
    monkeypatch.setattr(profile_capture.qsync_mod, "is_available", lambda: True)
    monkeypatch.setattr(profile_capture.qsync_mod, "_discover_displays", lambda: [(0, 0), (1, 0)])
    monkeypatch.setattr(profile_capture.mosaic_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "capture_arrangement", lambda: [])
    monkeypatch.setattr(profile_capture.edid_mod, "status", lambda: {})
    monkeypatch.setattr(profile_capture.edid_mod, "read_active_edid_overrides", lambda: {})
    from nvmon.agent import framelock as fl_mod
    monkeypatch.setattr(fl_mod, "_read_topology_json", lambda: None)
    monkeypatch.setattr(fl_mod, "_powershell_exe", lambda: None)
    monkeypatch.setattr(profile_capture.gpu_mod, "status", lambda: [])

    p = profile_capture.capture_current_state()
    assert p.qsync.role == "server"
    assert p.qsync.source == "HOUSESYNC"
    assert p.qsync.displays == [(0, 0), (1, 0)]


@pytest.mark.parametrize(
    "parsed_role, states, expected",
    [
        ("", ["SERVER", "CLIENT"], "server"),   # two-GPU server, parser undecided
        ("", ["CLIENT", "CLIENT"], "client"),   # two-GPU daisy-chain client
        ("", ["CLIENT"], "client"),
        ("", ["SERVER"], "server"),
        ("", [], "server"),                     # nothing reported -> default
        ("client", ["SERVER"], "client"),       # parser's verdict is trusted
        ("server", ["CLIENT"], "server"),
    ],
)
def test_live_role_server_wins(parsed_role, states, expected):
    st = QSyncState(
        available=True, role=parsed_role,
        per_gpu_sync=[GPUSyncInfo(gpu=i, sync_state=s) for i, s in enumerate(states)],
    )
    assert qsync.live_role(st) == expected


def test_live_role_evidence_none_when_unlocked():
    """QSync enabled but no GPU reports a Sync State → no evidence."""
    st = QSyncState(
        available=True, synced=False, source="VSYNC",
        per_gpu_sync=[GPUSyncInfo(gpu=0, sync_state=""), GPUSyncInfo(gpu=1, sync_state="")],
    )
    assert qsync.live_role_evidence(st) is None
    assert qsync.live_role(st) == "server"   # default unchanged for callers


def _hermetic_capture(monkeypatch, fake_state):
    monkeypatch.setattr(profile_capture.qsync_mod, "status", lambda: fake_state)
    monkeypatch.setattr(profile_capture.qsync_mod, "is_available", lambda: True)
    monkeypatch.setattr(profile_capture.qsync_mod, "_discover_displays", lambda: [(0, 0), (1, 0)])
    monkeypatch.setattr(profile_capture.mosaic_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "capture_arrangement", lambda: [])
    monkeypatch.setattr(profile_capture.edid_mod, "status", lambda: {})
    monkeypatch.setattr(profile_capture.edid_mod, "read_active_edid_overrides", lambda: {})
    from nvmon.agent import framelock as fl_mod
    monkeypatch.setattr(fl_mod, "_read_topology_json", lambda: None)
    monkeypatch.setattr(fl_mod, "_powershell_exe", lambda: None)
    monkeypatch.setattr(profile_capture.gpu_mod, "status", lambda: [])


def test_capture_unlocked_client_keeps_active_profile_role(monkeypatch):
    """Regression (RENDER-02, 2026-11-01): the operator captured the client
    while it was NOT locked (every GPU's Sync State empty). The old rule
    defaulted to role=server + source=HOUSESYNC, and the Sync button then ran
    `enable server HOUSESYNC` on a box with no BNC feed. With no live
    evidence, capture must keep the role of the node's active profile."""
    fake_state = QSyncState(
        available=True, synced=False, house_sync_detected=False,
        house_sync_incoming_hz=23.976, source="VSYNC", trigger="FALLING",
        per_gpu_sync=[
            GPUSyncInfo(gpu=0, signal_available=True, sync_state=""),
            GPUSyncInfo(gpu=1, signal_available=True, sync_state=""),
        ],
    )
    _hermetic_capture(monkeypatch, fake_state)
    active = Profile(name="59.94 - 10b - SyncClient",
                     qsync=ProfileQSync(enabled=True, role="client", source=""))
    monkeypatch.setattr(profile_capture.profiles_mod, "load_active", lambda: (active, None))

    p = profile_capture.capture_current_state()
    assert p.qsync.role == "client"
    assert p.qsync.source == ""


def test_capture_unlocked_without_active_profile_defaults_server(monkeypatch):
    fake_state = QSyncState(
        available=True, synced=False, source="VSYNC", trigger="FALLING",
        per_gpu_sync=[GPUSyncInfo(gpu=0, signal_available=True, sync_state="")],
    )
    _hermetic_capture(monkeypatch, fake_state)
    monkeypatch.setattr(profile_capture.profiles_mod, "load_active", lambda: None)
    p = profile_capture.capture_current_state()
    assert p.qsync.role == "server"


def test_capture_live_server_evidence_beats_active_client_profile(monkeypatch):
    """A node that IS locked as server stays server even if its last applied
    profile said client (operator re-cabled the chain)."""
    fake_state = QSyncState(
        available=True, synced=True, source="HOUSESYNC", trigger="FALLING",
        per_gpu_sync=[GPUSyncInfo(gpu=0, signal_available=True, sync_state="SERVER"),
                      GPUSyncInfo(gpu=1, signal_available=True, sync_state="CLIENT")],
    )
    _hermetic_capture(monkeypatch, fake_state)
    active = Profile(name="x", qsync=ProfileQSync(enabled=True, role="client", source=""))
    monkeypatch.setattr(profile_capture.profiles_mod, "load_active", lambda: (active, None))
    p = profile_capture.capture_current_state()
    assert p.qsync.role == "server"
    assert p.qsync.source == "HOUSESYNC"


def test_capture_defaults_to_server_when_no_client_state(monkeypatch):
    fake_state = QSyncState(
        available=True, synced=True,
        source="HOUSESYNC", trigger="FALLING",
        per_gpu_sync=[GPUSyncInfo(gpu=0, signal_available=True, sync_state="SERVER")],
    )
    monkeypatch.setattr(profile_capture.qsync_mod, "status", lambda: fake_state)
    monkeypatch.setattr(profile_capture.qsync_mod, "is_available", lambda: True)
    monkeypatch.setattr(profile_capture.mosaic_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.edid_mod, "status", lambda: {})
    monkeypatch.setattr(profile_capture.edid_mod, "read_active_edid_overrides", lambda: {})

    p = profile_capture.capture_current_state()
    assert p.qsync.role == "server"
    assert p.qsync.displays == [(0, 0)]


# ---------------------------------------------------------------------------
# Apply: client mode emits `enable client` without source/trigger
# ---------------------------------------------------------------------------

def test_enable_client_omits_source_and_trigger():
    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        return CommandResult(success=True, return_code=0)

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        result = qsync.enable(role="client", displays=[(0, 0)], wait=False)

    assert result.success is True
    # Only the enable call (no discovery — explicit displays bypass it).
    assert len(captured) == 1
    args = captured[0]
    assert args[0:2] == ["enable", "client"]
    assert "display=0,0" in args
    assert not any(a.startswith("source=") for a in args), args
    assert not any(a.startswith("trigger=") for a in args), args


def test_enable_server_keeps_source_and_trigger():
    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        return CommandResult(success=True, return_code=0)

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        qsync.enable(role="server", source="HOUSESYNC", trigger="FALLING",
                     displays=[(0, 0)], wait=False)

    args = captured[0]
    assert args[0:2] == ["enable", "server"]
    assert "source=HOUSESYNC" in args
    assert "trigger=FALLING" in args


def test_enable_from_profile_routes_role_and_displays(monkeypatch):
    """Profile carries role=client + displays=[(0,0)] -> CLI gets `enable client`."""
    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        return CommandResult(success=True, return_code=0)

    p = Profile(
        name="ds",
        qsync=ProfileQSync(
            enabled=True, source="HOUSESYNC", trigger="FALLING",
            role="client", displays=[(0, 0)],
        ),
    )

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run), \
         patch.object(executors, "sleep", lambda *a, **kw: None):
        qsync.enable_from_profile(p, wait=False)

    # New behavior: enable_from_profile does disable -> enable -> verify.
    # Find the enable call (not disable / queryAll / etc.) by looking for
    # the args that start with ["enable", "client"].
    enable_args = next(
        (c for c in captured if len(c) >= 2 and c[0] == "enable" and c[1] == "client"),
        None,
    )
    assert enable_args is not None, f"no enable args captured: {captured}"
    assert "display=0,0" in enable_args
    assert not any(a.startswith("source=") for a in enable_args)
    # Disable must have run before enable (forces role transition).
    disable_idx = next(
        (i for i, c in enumerate(captured) if c == ["disable"]), None,
    )
    enable_idx = captured.index(enable_args)
    assert disable_idx is not None and disable_idx < enable_idx, (
        f"expected disable before enable, got: {captured}"
    )


# ---------------------------------------------------------------------------
# v2 schema: round-trip + VSYNC normalization
# ---------------------------------------------------------------------------

def test_v2_qsync_section_round_trip():
    s = QSyncSection(
        source="HOUSESYNC", trigger="FALLING", grid=0,
        role="client", displays=[(0, 0), (1, 0)],
    )
    d = s.to_dict()
    assert d["role"] == "client"
    assert d["displays"] == [[0, 0], [1, 0]]
    s2 = QSyncSection.from_dict(d)
    assert s2.role == "client"
    assert s2.displays == [(0, 0), (1, 0)]


def test_v2_qsync_section_normalizes_vsync():
    s = QSyncSection.from_dict({"source": "VSYNC", "trigger": "FALLING"})
    assert s.source == "HOUSESYNC"


def test_v1_migrator_normalizes_vsync_and_defaults_role():
    v1 = {
        "name": "legacy",
        "qsync": {
            "enabled": True, "source": "VSYNC", "trigger": "FALLING", "grid": 1,
        },
    }
    p2 = upgrade_v1_to_v2(v1)
    assert p2.sections.qsync is not None
    assert p2.sections.qsync.source == "HOUSESYNC"
    assert p2.sections.qsync.role == "server"
    assert p2.sections.qsync.displays == []


# ---------------------------------------------------------------------------
# Controller per-node fanout
# ---------------------------------------------------------------------------

def test_fanout_per_node_skip_when_callback_returns_none():
    """body_for returning None marks the node skipped with the right reason."""
    from nvmon.controller.fanout import FanoutExecutor
    from nvmon.controller.registry import NodeEntry, NodeRegistry

    reg = NodeRegistry(
        nodes_cfg=[
            {"hostname": "n1", "url": "http://n1:9998"},
            {"hostname": "n2", "url": "http://n2:9998"},
        ],
        groups_cfg=[],
    )
    n1, n2 = reg.get_all()

    ex = FanoutExecutor(reg)
    profile_map = {"n1": "alpha"}

    def body_for(node):
        if profile_map.get(node.hostname):
            return {"profile": profile_map[node.hostname]}
        return None

    posted: list[tuple[str, str, dict]] = []

    def fake_call_one(self, node, method, path, body):
        posted.append((node.hostname, path, body))
        return {"hostname": node.hostname, "status": "accepted", "http_code": 202}

    with patch.object(FanoutExecutor, "_call_one", fake_call_one):
        result = ex._fanout_per_node(
            [n1, n2], method="POST", path="/qsync/enable_from_profile",
            body_for=body_for, skip_reason="no active profile",
        )

    assert result["requested"] == 2
    assert result["succeeded"] == 1
    assert result["skipped"] == 1
    rows = {r["hostname"]: r for r in result["results"]}
    assert rows["n1"]["status"] == "accepted"
    assert rows["n2"]["status"] == "skipped"
    assert "no active profile" in rows["n2"]["reason"]
    # Only the kept node was actually posted to.
    assert len(posted) == 1
    assert posted[0] == ("n1", "/qsync/enable_from_profile", {"profile": "alpha"})
