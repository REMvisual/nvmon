"""Tests for the framelock NVWMI module + the new validator checks.

Covers:
- framelock.capability() reports unavailable when PowerShell is missing.
- framelock.status() returns FramelockState(available=False) cleanly when
  PowerShell isn't present (no crash, no exception).
- diff_against_profile flags drift on the deep fields.
- validator's qsync_role check catches role mismatch.
- validator skips mosaic check entirely when profile says mosaic disabled.
- enable_from_profile sanity gate refuses apply when gpu_uuid_pin
  doesn't match live state.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from nvmon import executors
from nvmon.agent import framelock, qsync, validator
from nvmon.agent.framelock import FramelockState, diff_against_profile
from nvmon.types import (
    CommandResult, DDState, GPUInfo, GPUSyncInfo, MosaicState,
    Profile, ProfileMosaic, ProfileQSync, QSyncState,
)


# ---------------------------------------------------------------------------
# framelock module
# ---------------------------------------------------------------------------

def test_framelock_capability_no_powershell(monkeypatch):
    monkeypatch.setattr(framelock, "_powershell_exe", lambda: None)
    cap = framelock.capability()
    assert cap["available"] is False
    assert "powershell" in (cap.get("reason") or "").lower()


def test_framelock_status_no_powershell_returns_empty(monkeypatch):
    monkeypatch.setattr(framelock, "_powershell_exe", lambda: None)
    monkeypatch.setattr(framelock, "_read_topology_json", lambda: None)
    state = framelock.status()
    assert state.available is False
    assert state.gpu_uuid == ""
    assert state.master_display is None


def test_framelock_status_parses_json(monkeypatch):
    """When PowerShell returns a valid JSON blob, the typed fields populate."""
    fake_json = json.dumps({
        "Gsync": [{
            "SerialNumber": "A6KPC-12345",
            "SyncSource": "HouseSync",
            "Connector0Direction": "In",
            "Connector1Direction": "Out",
        }],
        "FrameLockDisplayConfig": [
            {"IsMaster": True, "GpuIndex": 0, "DisplayIndex": 0},
            {"IsMaster": False, "GpuIndex": 0, "DisplayIndex": 1},
        ],
        "Display": [
            {"GpuUUID": "GPU-abcd-1234"},
        ],
    })

    def fake_run(*a, **kw):
        return CommandResult(success=True, return_code=0, stdout=fake_json, stderr="")

    # Skip the topology-JSON path so we test the NVWMI fallback specifically.
    monkeypatch.setattr(framelock, "_read_topology_json", lambda: None)
    monkeypatch.setattr(framelock, "_powershell_exe", lambda: "powershell.exe")
    monkeypatch.setattr(executors, "run", fake_run)

    state = framelock.status()
    assert state.available is True
    assert state.gpu_uuid == "GPU-abcd-1234"
    assert state.sync_card_serial == "A6KPC-12345"
    assert state.master_display == (0, 0)
    assert state.connector_0_direction == "in"
    assert state.connector_1_direction == "out"
    assert state.source_kind == "housesync"


def test_framelock_status_handles_garbage_output(monkeypatch):
    def fake_run(*a, **kw):
        return CommandResult(
            success=True, return_code=0,
            stdout="not json", stderr="",
        )

    monkeypatch.setattr(framelock, "_read_topology_json", lambda: None)
    monkeypatch.setattr(framelock, "_powershell_exe", lambda: "powershell.exe")
    monkeypatch.setattr(executors, "run", fake_run)
    state = framelock.status()
    assert state.available is False
    assert "parse failed" in state.error.lower()


def test_diff_against_profile_flags_master_drift():
    profile = Profile(
        qsync=ProfileQSync(
            enabled=True, master_display=(0, 0),
        ),
    )
    live = FramelockState(available=True, master_display=(1, 0))
    drift = diff_against_profile(profile, live)
    assert "master_display" in drift
    assert drift["master_display"]["expected"] == [0, 0]
    assert drift["master_display"]["actual"] == [1, 0]


def test_diff_against_profile_no_drift_when_match():
    profile = Profile(
        qsync=ProfileQSync(
            enabled=True, master_display=(0, 0),
            gpu_uuid_pin="GPU-X", sync_card_serial_pin="A6K",
        ),
    )
    live = FramelockState(
        available=True, master_display=(0, 0),
        gpu_uuid="GPU-X", sync_card_serial="A6K",
    )
    assert diff_against_profile(profile, live) == {}


# ---------------------------------------------------------------------------
# validator strengthening
# ---------------------------------------------------------------------------

def test_parser_marks_client_synced_from_sync_state():
    """A queryAll dump where Sync State : CLIENT appears must produce
    state.synced=True and role='client', overriding the misleading
    'Is synced : NO' line that configureQsync2 emits for clients."""
    from nvmon.parsers import parse_qsync_query_all

    output = (
        "Found one local server running at Frequency: 24.000 Hz\n"
        "House Sync Signal Present : NO\n"
        "Source : VSYNC\n"
        "QSync Target #0(GPU) NVIDIA RTX A6000:\n"
        "    GPU index             : 0\n"
        "    Is synced             : NO\n"
        "    Is sync signal available? : YES\n"
        "    Display 0:\n"
        "        Sync State : CLIENT\n"
        "        Refresh Rate   : 24.000 Hz\n"
        "QSync control params:\n"
        "    source        : VSYNC\n"
        "    trigger       : FALLING\n"
    )
    state = parse_qsync_query_all(output)
    assert state.synced is True
    assert state.role == "client"
    assert state.lock_source_label == "upstream framelock pulse"
    # effective_lock_hz left 0.0 — view-model fills it from display refresh
    assert state.effective_lock_hz == 0.0
    assert state.per_gpu_sync[0].sync_state == "CLIENT"
    assert state.per_gpu_sync[0].effective_synced is True


def test_parser_client_state_alone_is_not_a_lock():
    """site 2026-11-02: every client read "Sync State : CLIENT" with
    "Is synced : NO" and "Is sync signal available? : NO" while both timing
    servers were UNSYNCED. CLIENT is the configured role; without synced or
    a reported pulse the node is NOT locked (and must not go green)."""
    from nvmon.parsers import parse_qsync_query_all

    output = (
        "House Sync Signal Present : NO\n"
        "QSync Target #0(GPU) NVIDIA RTX PRO 6000:\n"
        "    GPU index             : 0\n"
        "    Is synced             : NO\n"
        "    Is sync signal available? : NO\n"
        "    Display 0:\n"
        "        Sync State : CLIENT\n"
        "        Refresh Rate   : 23.976 Hz\n"
        "QSync Target #1(GPU) NVIDIA RTX PRO 6000:\n"
        "    GPU index             : 1\n"
        "    Is synced             : NO\n"
        "    Is sync signal available? : NO\n"
        "    Display 0:\n"
        "        Sync State : CLIENT\n"
        "        Refresh Rate   : 23.976 Hz\n"
        "QSync control params:\n"
        "    source        : VSYNC\n"
        "    trigger       : FALLING\n"
    )
    state = parse_qsync_query_all(output)
    assert state.synced is False
    assert all(g.sync_state == "CLIENT" for g in state.per_gpu_sync)   # role still captured
    assert all(g.effective_synced is False for g in state.per_gpu_sync)
    assert all(g.signal_available is False for g in state.per_gpu_sync)
    # ...and the validator agrees for a client profile.
    profile = Profile(qsync=ProfileQSync(enabled=True, role="client"))
    assert validator._check_qsync(state, profile).passed is False
    # A CLIENT GPU that says "Is synced : YES" IS locked (srv1 after-reboot dump).
    locked = parse_qsync_query_all(output.replace("Is synced             : NO", "Is synced             : YES", 1))
    assert locked.synced is True and locked.role == "client"


def test_parser_server_with_housesync_populates_lock_label():
    from nvmon.parsers import parse_qsync_query_all

    output = (
        "Found one local server running at Frequency: 24.000 Hz\n"
        "House Sync Signal Present : YES\n"
        "House Sync Signal Rate : 24.000\n"
        "Source : HOUSE SYNC\n"
        "QSync Target #0(GPU):\n"
        "    GPU index : 0\n"
        "    Is synced : NO\n"
        "    Is sync signal available? : YES\n"
        "    Display 0:\n"
        "        Sync State : SERVER\n"
        "        Refresh Rate : 24.000 Hz\n"
    )
    state = parse_qsync_query_all(output)
    assert state.synced is True
    assert state.role == "server"
    assert state.lock_source_label == "external house sync"
    assert abs(state.effective_lock_hz - 24.0) < 0.01


def test_validator_qsync_client_passes_with_sync_state_only(monkeypatch):
    """For role=client, the qsync_locked check must pass purely on
    per-GPU sync_state=CLIENT, regardless of source/trigger expectations."""
    profile = Profile(
        qsync=ProfileQSync(enabled=True, role="client", source="HOUSESYNC", trigger="FALLING"),
    )
    qstate = QSyncState(
        available=True,
        synced=True,  # parser now sets this for client-locked
        source="VSYNC",  # configureQsync2 typically reports VSYNC for clients
        trigger="FALLING",
        per_gpu_sync=[GPUSyncInfo(gpu=0, signal_available=True, sync_state="CLIENT")],
    )
    check = validator._check_qsync(qstate, profile)
    assert check.passed is True
    assert "client" in check.detail.lower()


def test_validator_house_sync_skipped_for_clients():
    profile = Profile(qsync=ProfileQSync(enabled=True, role="client"))
    qstate = QSyncState(available=True, house_sync_detected=False, house_sync_incoming_hz=0.0)
    check = validator._check_house_sync(qstate, profile)
    assert check.passed is True
    assert "client" in check.detail.lower()


def test_validator_qsync_role_passes_when_match():
    profile = Profile(qsync=ProfileQSync(enabled=True, role="client"))
    qstate = QSyncState(
        available=True, synced=True,
        per_gpu_sync=[GPUSyncInfo(gpu=0, sync_state="CLIENT", signal_available=True)],
    )
    check = validator._check_qsync_role(qstate, profile)
    assert check.passed is True
    assert "client" in check.detail


def test_validator_qsync_role_fails_on_drift():
    """The bug the user hit: server profile, but live state says CLIENT."""
    profile = Profile(qsync=ProfileQSync(enabled=True, role="server"))
    qstate = QSyncState(
        available=True, synced=True,
        per_gpu_sync=[GPUSyncInfo(gpu=0, sync_state="CLIENT", signal_available=True)],
    )
    check = validator._check_qsync_role(qstate, profile)
    assert check.passed is False
    assert "server" in check.detail
    assert "client" in check.detail.lower()


def test_validator_qsync_role_skips_when_qsync_disabled():
    profile = Profile(qsync=ProfileQSync(enabled=False))
    check = validator._check_qsync_role(QSyncState(), profile)
    assert check.passed is True


def test_validator_mosaic_skipped_when_profile_disables_it_even_if_live_on():
    """User's request: when profile says mosaic off, don't drift-check it."""
    profile = Profile(mosaic=ProfileMosaic(enabled=False))
    live_mosaic_on = MosaicState(enabled=True, rows=2, cols=2)
    check = validator._check_mosaic(live_mosaic_on, profile)
    assert check.passed is True
    assert "skipped" in check.detail.lower()


def test_validator_mosaic_skipped_when_profile_disables_and_live_off():
    profile = Profile(mosaic=ProfileMosaic(enabled=False))
    check = validator._check_mosaic(MosaicState(enabled=False), profile)
    assert check.passed is True


# ---------------------------------------------------------------------------
# Sanity gate at apply
# ---------------------------------------------------------------------------

def test_enable_from_profile_refuses_on_gpu_uuid_mismatch(monkeypatch):
    """When the profile pins a GPU UUID that differs from live, apply refuses."""
    profile = Profile(
        qsync=ProfileQSync(
            enabled=True, role="server", source="HOUSESYNC", trigger="FALLING",
            gpu_uuid_pin="GPU-X-EXPECTED",
        ),
    )

    fake_live = FramelockState(
        available=True,
        gpu_uuid="GPU-Y-LIVE",  # different — should refuse
        sync_card_serial="A6K",
    )

    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        return CommandResult(success=True, return_code=0)

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run), \
         patch.object(executors, "sleep", lambda *a, **kw: None), \
         patch.object(framelock, "status", lambda **kw: fake_live):
        result = qsync.enable_from_profile(profile, wait=False)

    assert result.success is False
    assert "gpu_uuid_pin" in (result.stderr or "").lower() or "uuid" in (result.stderr or "").lower()
    # configureQsync2 must NOT have been invoked at all — sanity gate fires first.
    assert all(c[0] != "enable" for c in captured), f"sanity gate did not block apply: {captured}"


def test_topology_json_parser_server():
    """NVCP topology dump for a server node yields the right typed fields."""
    from nvmon.agent.framelock import _parse_topology_json

    topo = {
        "Displays and graphics Cards": {
            "System": {
                "Driver version": "595.97",
                "RTX PRO Sync (server)": {
                    "Framelock 0": "Out",
                    "Framelock 1": "Out",
                    "External sync signal": "Present (In use)",
                    "NVIDIA RTX PRO 6000": {
                        "Serial Number": "1000000000001",
                        "Tessera SX40": {
                            "Display state": "Server",
                            "Timing": "The display is locked to the house sync signal",
                        },
                    },
                },
            },
        },
    }
    state = _parse_topology_json(topo)
    assert state.available is True
    assert state.connector_0_direction == "out"
    assert state.connector_1_direction == "out"
    assert state.source_kind == "housesync"
    assert state.sync_card_serial == "1000000000001"
    assert state.master_display == (0, 0)


def test_topology_json_parser_client():
    from nvmon.agent.framelock import _parse_topology_json

    topo = {
        "Mosaic Displays": [{
            "System": {
                "Driver version": "595.97",
                "RTX PRO Sync (client)": {
                    "Framelock 0": "Out",
                    "Framelock 1": "In",
                    "External sync signal": "Not present",
                    "NVIDIA RTX PRO 6000": {
                        "Serial Number": "1000000000002",
                        "Tessera SX40": {
                            "Display state": "Client",
                            "Timing": "The display is locked to the frame lock sync pulse",
                        },
                    },
                },
            },
        }],
    }
    state = _parse_topology_json(topo)
    assert state.available is True
    assert state.connector_0_direction == "out"
    assert state.connector_1_direction == "in"
    assert state.source_kind == "frame_lock_pulse"
    assert state.sync_card_serial == "1000000000002"


def test_capture_client_source_is_empty_not_housesync(monkeypatch):
    """Client mode must not record source=HOUSESYNC — clients lock to the
    upstream frame-lock pulse, not to house sync directly."""
    from nvmon.agent import profile_capture as pc

    fake_state = QSyncState(
        available=True, synced=True,
        source="HOUSESYNC",  # what configureQsync2 reports for clients
        trigger="FALLING",
        per_gpu_sync=[GPUSyncInfo(gpu=0, signal_available=True, sync_state="CLIENT")],
    )
    monkeypatch.setattr(pc.qsync_mod, "status", lambda: fake_state)
    monkeypatch.setattr(pc.qsync_mod, "is_available", lambda: True)
    monkeypatch.setattr(pc.mosaic_mod, "status", lambda: None)
    monkeypatch.setattr(pc.displays_mod, "status", lambda: None)
    monkeypatch.setattr(pc.displays_mod, "capture_arrangement", lambda: [])
    monkeypatch.setattr(pc.edid_mod, "status", lambda: {})
    monkeypatch.setattr(pc.edid_mod, "read_active_edid_overrides", lambda: {})
    monkeypatch.setattr(pc.framelock_mod, "status", lambda: FramelockState())
    monkeypatch.setattr(pc.gpu_mod, "status", lambda: [])

    p = pc.capture_current_state()
    assert p.qsync.role == "client"
    # Critical: source must be empty for clients.
    assert p.qsync.source == ""


def test_monitor_takes_explicit_profile_name(monkeypatch, tmp_path):
    """Monitor.__init__ records the caller-supplied profile name verbatim.
    Resolution from the active_profile.json snapshot lives in
    agent/server.py::serve() — Monitor itself just records what it's told.
    """
    from nvmon.agent.monitor import Monitor
    from nvmon import config as cfgmod

    monkeypatch.setattr(cfgmod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "get_agent_config", lambda: {
        "poll_interval_seconds": 5,
        "status_file": str(tmp_path / "status.json"),
    })

    m = Monitor(profile_name="server-master")
    assert m.profile_name == "server-master"


def test_monitor_default_profile_name_empty(monkeypatch, tmp_path):
    """Without an explicit name the field starts empty — server.py's
    boot path is responsible for resolving from the snapshot or default."""
    from nvmon.agent.monitor import Monitor
    from nvmon import config as cfgmod

    monkeypatch.setattr(cfgmod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "get_agent_config", lambda: {
        "poll_interval_seconds": 5,
        "status_file": str(tmp_path / "status.json"),
    })

    m = Monitor()
    assert m.profile_name == ""


def test_enable_from_profile_does_disable_then_enable(monkeypatch):
    """Force-apply: even if QSync is already in some role, the new path
    disables first to break any stuck state, then enables with the profile's
    role. This is the production fix for 'server node restarts as client'.
    """
    profile = Profile(
        qsync=ProfileQSync(
            enabled=True, role="server", source="HOUSESYNC", trigger="FALLING",
            displays=[(0, 0)],
        ),
    )

    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        # queryAll is called by the verify path — return a SERVER state so verify passes
        if kw.get("label") in ("qsync-queryAll",):
            return CommandResult(
                success=True, return_code=0,
                stdout=(
                    "Found one local server running at Frequency: 24.000 Hz\n"
                    "House Sync Signal Present : YES\n"
                    "Source : HOUSE SYNC\n"
                    "QSync Target #0(GPU) NVIDIA RTX A6000:\n"
                    "    GPU index : 0\n"
                    "    Display 0:\n"
                    "        Sync State : SERVER\n"
                    "        Refresh Rate : 24.000 Hz\n"
                ),
                stderr="",
            )
        return CommandResult(success=True, return_code=0)

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run), \
         patch.object(executors, "sleep", lambda *a, **kw: None):
        result = qsync.enable_from_profile(profile, wait=False)

    assert result.success is True
    # disable was called
    assert ["disable"] in captured, f"expected disable call: {captured}"
    # enable server was called after disable
    enable_call = next(
        (c for c in captured if len(c) >= 2 and c[0] == "enable" and c[1] == "server"),
        None,
    )
    assert enable_call is not None, f"no enable server call: {captured}"
    disable_idx = captured.index(["disable"])
    enable_idx = captured.index(enable_call)
    assert disable_idx < enable_idx


# ---------------------------------------------------------------------------
# No-profile state: DD / Mosaic checks must not paint red Xs (site, 2026-11-01)
# ---------------------------------------------------------------------------

def test_dd_and_mosaic_checks_skip_when_no_profile_loaded():
    """A desktop-only node with no active profile used to fail dd_active
    ("0/2 displays in DD mode") and mosaic_config ("Mosaic is not enabled")
    because the legacy default assumed every rig runs DD + Mosaic. With no
    profile there is no expectation: both pass with a "skipped" detail so
    the dashboard's _filter_validation drops the rows."""
    dd = validator._check_dd_active(DDState(), None)
    assert dd.passed is True
    assert "skipped" in dd.detail.lower()

    ms = validator._check_mosaic(MosaicState(enabled=False), None)
    assert ms.passed is True
    assert "skipped" in ms.detail.lower()


def test_dd_and_mosaic_checks_still_enforce_when_profile_expects_them():
    """Guard against over-correcting: a profile that DOES expect Mosaic / DD
    must still fail on a node where they are off."""
    p = Profile(name="wall", mosaic=ProfileMosaic(enabled=True, rows=2, cols=2, width=3840, height=2160))
    p.direct_display.enabled = True
    ms = validator._check_mosaic(MosaicState(enabled=False), p)
    assert ms.passed is False
    dd = validator._check_dd_active(DDState(), p)
    assert dd.passed is False
