"""Tests for the studio-fleet hardening port (nvmon-9r9).

Every behaviour here was first found on real A6000 / Mosaic / DD / QSync
hardware in the legacy tool nvdd fork (commits cited inline) and then ported.
Tests run against fakes — never against the NVIDIA executables.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nvmon import config, executors
from nvmon.agent import driver_info, mosaic, profiles, qsync, validator
from nvmon.controller.registry import NodeRegistry
from nvmon.parsers import parse_nvidia_smi_csv
from nvmon.types import (
    CommandResult, DDState, DisplayInfo, GPUSyncInfo, MosaicOutput, MosaicState,
    Profile, ProfileMosaic, ProfileQSync, ProfileValidation, QSyncState,
)


class _Logger:
    def info(self, *a, **k): ...
    def warning(self, *a, **k): ...
    def debug(self, *a, **k): ...
    def error(self, *a, **k): ...


# ---------------------------------------------------------------------------
# executors: serialization + cancellation
# ---------------------------------------------------------------------------

def test_configure_exes_are_serialized_nvidia_smi_is_not():
    assert executors._serialized(r"F:\nvmon\bin\configureQsync2.exe")
    assert executors._serialized("configureMosaic.exe")
    assert executors._serialized(r"C:\x\configureDdisplay.exe")
    assert not executors._serialized(r"C:\Windows\System32\nvidia-smi.exe")


def test_hw_lock_blocks_overlapping_configure_calls(monkeypatch):
    """Two threads calling configure* must not run subprocess concurrently
    (configureQsync2 enable hangs under a concurrent queryAll — the legacy tool d8f876f)."""
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **kw):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        with lock:
            active["n"] -= 1
        return _Proc()

    monkeypatch.setattr(executors.subprocess, "run", fake_run)
    threads = [threading.Thread(target=executors.run, args=("configureQsync2.exe", ["queryAll"]))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1


def test_cancel_scope_is_thread_local_and_sleep_is_cancellable():
    executors.CANCEL.clear()
    # Outside a scope: CANCEL set has no effect.
    executors.CANCEL.set()
    executors.sleep(0.01)
    executors.check_cancel()
    executors.CANCEL.clear()

    raised = {}

    def job():
        executors.enter_cancel_scope()
        try:
            executors.sleep(5)
        except executors.Cancelled:
            raised["job"] = True
        finally:
            executors.exit_cancel_scope()

    t = threading.Thread(target=job)
    t.start()
    time.sleep(0.1)
    assert executors.job_active()
    executors.CANCEL.set()
    t.join(timeout=3)
    assert raised.get("job") is True
    assert not executors.job_active()
    executors.CANCEL.clear()


def test_cancelled_is_not_swallowed_by_except_exception():
    assert not issubclass(executors.Cancelled, Exception)


def test_failure_detail_prefers_stderr_then_stdout_then_rc():
    assert executors.failure_detail(CommandResult(success=False, return_code=2, stdout="why", stderr="")) == "why"
    assert executors.failure_detail(CommandResult(success=False, return_code=2, stdout="a", stderr="b")) == "b"
    assert executors.failure_detail(CommandResult(success=False, return_code=2)) == "rc=2"


# ---------------------------------------------------------------------------
# qsync: rate check, house gate, source-aware lock poll
# ---------------------------------------------------------------------------

def _house_state(hz: float, synced: bool = True) -> QSyncState:
    return QSyncState(
        available=True, synced=synced, house_sync_detected=True, house_sync_incoming_hz=hz,
        source="HOUSESYNC", trigger="FALLING",
        per_gpu_sync=[GPUSyncInfo(gpu=0, effective_synced=True, signal_available=True)],
    )


def test_apply_rate_check_downgrades_synced_when_rates_differ():
    """23.976 wall on a 60 Hz house signal is not a lock (the legacy tool 9ebdc20)."""
    st = qsync.apply_rate_check(_house_state(60.0), 23.976)
    assert st.synced is False
    assert st.per_gpu_sync[0].effective_synced is False
    assert "rate mismatch" in st.per_gpu_sync[0].sync_note


def test_apply_rate_check_keeps_real_lock_within_tolerance():
    st = qsync.apply_rate_check(_house_state(23.977), 23.976)
    assert st.synced is True
    assert st.per_gpu_sync[0].effective_synced is True


def test_apply_rate_check_noop_without_house_or_rate():
    st = qsync.apply_rate_check(QSyncState(available=True, synced=True), 24.0)
    assert st.synced is True
    st = qsync.apply_rate_check(_house_state(60.0), 0.0)
    assert st.synced is True


def test_house_sync_gate_fails_fast_on_wrong_clock():
    profile = Profile(
        qsync=ProfileQSync(enabled=True, source="HOUSESYNC"),
        validation=ProfileValidation(expected_house_sync_freq=60.0, refresh_rate_tolerance=0.05),
    )
    with patch.object(qsync, "status", return_value=_house_state(30.0)):
        ok, msg = qsync.house_sync_matches(profile)
    assert ok is False
    assert "30.000Hz" in msg and "60" in msg


def test_house_sync_gate_passes_when_matching_or_unknown():
    profile = Profile(
        qsync=ProfileQSync(enabled=True, source="HOUSESYNC"),
        validation=ProfileValidation(expected_house_sync_freq=23.976),
    )
    with patch.object(qsync, "status", return_value=_house_state(23.977)):
        assert qsync.house_sync_matches(profile) == (True, "")
    with patch.object(qsync, "status", return_value=QSyncState(available=True)):
        assert qsync.house_sync_matches(profile) == (True, "")
    client = Profile(qsync=ProfileQSync(enabled=True, role="client"),
                     validation=ProfileValidation(expected_house_sync_freq=60.0))
    assert qsync.house_sync_matches(client) == (True, "")


def test_poll_for_sync_ignores_lock_to_wrong_source(monkeypatch):
    """Locked to VSYNC when HOUSESYNC was requested must not count (the legacy tool dd06cbd)."""
    monkeypatch.setattr(config, "QSYNC_SYNC_TIMEOUT", 2)
    monkeypatch.setattr(config, "QSYNC_POLL_INTERVAL", 1)
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    wrong = QSyncState(available=True, synced=True, source="VSYNC")
    with patch.object(qsync, "status", return_value=wrong):
        assert qsync._poll_for_sync(_Logger(), "HOUSESYNC") is False
    right = QSyncState(available=True, synced=True, source="HOUSESYNC")
    with patch.object(qsync, "status", return_value=right):
        assert qsync._poll_for_sync(_Logger(), "HOUSESYNC") is True


def test_qsync_sync_timeout_is_20s():
    assert config.QSYNC_SYNC_TIMEOUT == 20


# ---------------------------------------------------------------------------
# validator: mosaic rate, qsync source/trigger, output_signal
# ---------------------------------------------------------------------------

def _mosaic_profile(rate: str = "60") -> Profile:
    return Profile(
        mosaic=ProfileMosaic(enabled=True, rows=2, cols=2, width=3840, height=2160, refresh_rate=rate),
        validation=ProfileValidation(refresh_rate_tolerance=0.05, expected_refresh_rate=float(rate)),
    )


def test_rate_tolerance_profile_may_tighten_never_loosen():
    """config.rate_tolerance(): the single rate-match rule (site 2026-11-02)."""
    assert config.RATE_MATCH_TOLERANCE_HZ == pytest.approx(0.01)
    assert config.rate_tolerance(None) == pytest.approx(0.01)
    assert config.rate_tolerance(0.0) == pytest.approx(0.01)
    assert config.rate_tolerance(0.05) == pytest.approx(0.01)      # legacy profiles: capped
    assert config.rate_tolerance(0.005) == pytest.approx(0.005)    # tighter is honoured
    assert config.rate_tolerance(0.0001) == pytest.approx(0.002)   # floored at tool precision
    assert config.rate_tolerance("junk") == pytest.approx(0.01)
    # The whole point: 23.976 and 24.000 are 0.024 apart and must NOT match.
    assert abs(24.0 - 23.976) > config.rate_tolerance(0.05)


def test_rate_tolerance_validator_fails_23976_under_24_profile():
    """Validator refresh / mosaic / house-sync checks under a 24.000 profile
    whose JSON still says tolerance 0.05 must all fail on 23.976."""
    p = Profile(
        mosaic=ProfileMosaic(enabled=True, rows=2, cols=2, width=3840, height=2160, refresh_rate="24.00"),
        qsync=ProfileQSync(enabled=True, source="HOUSESYNC"),
        validation=ProfileValidation(expected_refresh_rate=24.0, refresh_rate_tolerance=0.05,
                                     expected_house_sync_freq=24.0),
    )
    live = MosaicState(enabled=True, rows=2, cols=2, per_display_resolution=(3840, 2160), refresh_rate=23.976)
    assert validator._check_mosaic(live, p).passed is False
    dd = DDState(active=True, displays=[
        DisplayInfo(id="80061085", name="Tessera SX40", status="Desktop @(0,0)",
                    refresh_rate=23.976, resolution=(3840, 2160), gpu_index=0, display_index=0),
    ], display_count=1)
    assert validator._check_refresh_rate(dd, p, mosaic=MosaicState(enabled=False)).passed is False
    house = _house_state(23.976)
    assert validator._check_house_sync(house, p).passed is False
    # ...and the house-sync apply gate refuses to wait for an impossible lock.
    with patch.object(qsync, "status", return_value=house):
        ok, msg = qsync.house_sync_matches(p)
    assert ok is False and "23.976" in msg and "24" in msg


def test_mosaic_check_fails_on_rate_mismatch():
    """Boot-apply left both walls at 29.97 under a 60 Hz profile because only
    grid + resolution were compared (the legacy tool 5d7ba95)."""
    live = MosaicState(enabled=True, rows=2, cols=2, per_display_resolution=(3840, 2160), refresh_rate=29.97)
    check = validator._check_mosaic(live, _mosaic_profile("60"))
    assert check.passed is False
    assert "29.97" in check.detail and "60" in check.detail


def test_mosaic_check_passes_within_tolerance():
    live = MosaicState(enabled=True, rows=2, cols=2, per_display_resolution=(3840, 2160), refresh_rate=23.976)
    assert validator._check_mosaic(live, _mosaic_profile("23.98")).passed is True


def test_qsync_check_fails_on_wrong_source_and_wrong_trigger():
    profile = Profile(qsync=ProfileQSync(enabled=True, source="HOUSESYNC", trigger="FALLING"))
    wrong_trigger = QSyncState(available=True, synced=True, source="HOUSESYNC", trigger="RISING",
                               per_gpu_sync=[GPUSyncInfo(gpu=0, effective_synced=True)])
    check = validator._check_qsync(wrong_trigger, profile)
    assert check.passed is False and "trigger" in check.detail
    profile_internal = Profile(qsync=ProfileQSync(enabled=True, source="INTERNAL", trigger="FALLING"))
    on_house = QSyncState(available=True, synced=True, source="HOUSESYNC", trigger="FALLING",
                          per_gpu_sync=[GPUSyncInfo(gpu=0, effective_synced=True)])
    check = validator._check_qsync(on_house, profile_internal)
    assert check.passed is False and "wrong source" in check.detail
    good = QSyncState(available=True, synced=True, source="HOUSESYNC", trigger="FALLING",
                      per_gpu_sync=[GPUSyncInfo(gpu=0, effective_synced=True)])
    assert validator._check_qsync(good, profile).passed is True


def test_output_signal_flags_scaled_wall_and_wrong_rate():
    """Mosaic reported 3840x2160@23.976 while Windows sent 10240x5760@60 (the legacy tool 1170183)."""
    dd = DDState(signals=[{"name": "HELIOS", "source": (7680, 4320), "target": (10240, 5760), "hz": 60.0}])
    check = validator._check_output_signal(dd, _mosaic_profile("23.98"))
    assert check.passed is False and "scaled" in check.detail
    dd = DDState(signals=[{"name": "HELIOS", "source": (7680, 4320), "target": (7680, 4320), "hz": 60.0}])
    check = validator._check_output_signal(dd, _mosaic_profile("23.98"))
    assert check.passed is False and "expected 23.98" in check.detail
    dd = DDState(signals=[{"name": "HELIOS", "source": (7680, 4320), "target": (7680, 4320), "hz": 23.976}])
    assert validator._check_output_signal(dd, _mosaic_profile("23.98")).passed is True


def test_output_signal_skips_when_not_reported_or_only_operator_display(monkeypatch):
    assert validator._check_output_signal(DDState(), _mosaic_profile()).passed is True
    monkeypatch.setattr(config, "is_excluded_display", lambda name, res: name.startswith("DDX"))
    dd = DDState(signals=[{"name": "DDX-101", "source": (1920, 1080), "target": (1920, 1080), "hz": 60.0}])
    assert validator._check_output_signal(dd, _mosaic_profile()).passed is True


def test_validate_all_applies_rate_check_before_qsync_check():
    """A 'synced' 23.976 wall on a 60 Hz house signal must fail qsync_locked."""
    profile = Profile(
        mosaic=ProfileMosaic(enabled=True, rows=2, cols=2, width=3840, height=2160, refresh_rate="23.98"),
        qsync=ProfileQSync(enabled=True, source="HOUSESYNC", trigger="FALLING"),
        validation=ProfileValidation(refresh_rate_tolerance=0.05),
    )
    live = MosaicState(enabled=True, rows=2, cols=2, per_display_resolution=(3840, 2160), refresh_rate=23.976)
    result = validator.validate_all(dd=DDState(), mosaic=live, qsync=_house_state(60.0), gpus=[], profile=profile)
    assert result.checks["qsync_locked"].passed is False


# ---------------------------------------------------------------------------
# mosaic: dry-run timings
# ---------------------------------------------------------------------------

_TEST_OUTPUT = '<mode valid="1" width="3840" height="2160" rr="29.970000" />'


def test_test_timings_parses_rate_and_valid():
    captured = {}

    def fake_run(exe, args, **kw):
        captured["args"] = list(args)
        return CommandResult(success=True, return_code=0, stdout=_TEST_OUTPUT, stderr="")

    with patch.object(mosaic.config, "get_mosaic_exe", return_value="configureMosaic.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        out = mosaic.test_timings(2, 2, outputs="out=1,0 out=1,1", width=3840, height=2160, refresh_rate="30")
    assert captured["args"] == ["test", "rows=2", "cols=2", "out=1,0", "out=1,1", "res=3840,2160,30"]
    assert out["rate_hz"] == 29.97 and out["valid"] is True and out["success"] is True


def test_test_timings_from_profile_uses_profile_grid():
    captured = {}

    def fake_run(exe, args, **kw):
        captured["args"] = list(args)
        return CommandResult(success=True, return_code=0, stdout="", stderr="")

    profile = Profile(mosaic=ProfileMosaic(
        enabled=True, rows=1, cols=2, width=1920, height=1080, refresh_rate="60",
        outputs=[MosaicOutput(gpu=0, display=0), MosaicOutput(gpu=0, display=1)],
    ))
    with patch.object(mosaic.config, "get_mosaic_exe", return_value="configureMosaic.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        out = mosaic.test_timings_from_profile(profile)
    assert captured["args"] == ["test", "rows=1", "cols=2", "out=0,0", "out=0,1", "res=1920,1080,60"]
    assert out["rate_hz"] is None and out["valid"] is False


# ---------------------------------------------------------------------------
# profiles: latest request wins + cancel + status
# ---------------------------------------------------------------------------

def test_apply_preempts_running_job_and_reports_cancellation(monkeypatch):
    monkeypatch.setattr(profiles, "_JOB_PREEMPT_TIMEOUT", 5.0)
    monkeypatch.setattr(profiles.config, "is_admin", lambda: True)
    started = threading.Event()

    def slow_pipeline(name, *, dry_run, skip):
        started.set()
        executors.sleep(30, "fake long step")
        return True, "should have been cancelled"

    monkeypatch.setattr(profiles, "_apply_inner", slow_pipeline)
    results = {}

    def first():
        results["first"] = profiles.apply("A")

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(2)
    assert profiles.apply_in_progress()
    assert profiles.get_last_result()["action"] == "apply"
    # Second request: cancels the first, then runs.
    monkeypatch.setattr(profiles, "_apply_inner", lambda name, *, dry_run, skip: (True, f"{name} applied"))
    ok, msg = profiles.apply("B")
    t.join(timeout=5)
    assert ok is True and msg == "B applied"
    assert results["first"][0] is False and "cancelled" in results["first"][1]
    last = profiles.get_last_result()
    assert last["in_progress"] is False and last["profile"] == "B" and last["cancelled"] is False


def test_cancel_current_only_when_running():
    assert profiles.cancel_current() is False
    executors.CANCEL.clear()


def test_apply_status_carries_step(monkeypatch):
    monkeypatch.setattr(profiles.config, "is_admin", lambda: True)
    seen = {}

    def pipeline(name, *, dry_run, skip):
        profiles.set_step("[4/6] Configuring Mosaic")
        seen["step"] = profiles.get_last_result()["step"]
        return True, "ok"

    monkeypatch.setattr(profiles, "_apply_inner", pipeline)
    profiles.apply("X")
    assert seen["step"] == "[4/6] Configuring Mosaic"
    assert profiles.get_last_result()["step"] is None


def test_profile_rate_prefers_mosaic_rate():
    assert profiles._profile_rate(_mosaic_profile("29.97")) == 29.97
    p = Profile(mosaic=ProfileMosaic(enabled=False), validation=ProfileValidation(expected_refresh_rate=24.0))
    assert profiles._profile_rate(p) == 24.0


# ---------------------------------------------------------------------------
# monitor: skip poll while a job owns the hardware
# ---------------------------------------------------------------------------

def test_monitor_returns_last_status_while_job_active(monkeypatch):
    from nvmon.agent.monitor import Monitor
    m = Monitor()
    m._last_status = {"cached": True}
    monkeypatch.setattr(executors, "job_active", lambda: True)
    assert m.poll_once() == {"cached": True}
    assert m.poll_count == 0


# ---------------------------------------------------------------------------
# controller: IP list + poll settings
# ---------------------------------------------------------------------------

def test_node_ip_to_url_forms():
    assert config._node_ip_to_url("10.0.0.5") == "http://10.0.0.5:9998"
    assert config._node_ip_to_url("10.0.0.5:9000") == "http://10.0.0.5:9000"
    assert config._node_ip_to_url("http://10.0.0.5:9998/") == "http://10.0.0.5:9998"
    assert config._node_ip_to_url(' "10.0.0.5" ') == "http://10.0.0.5:9998"
    assert config._node_ip_label("http://10.0.0.5:9998") == "10.0.0.5:9998"


def test_controller_config_node_ips_and_poll_settings(tmp_path: Path):
    toml = tmp_path / "nvmon.toml"
    toml.write_text(
        '[controller]\npoll_timeout_seconds = 3\npoll_workers = 12\n'
        'node_ips = ["10.0.0.11", "10.0.0.12:9998", "10.0.0.13:9998", ""]\n'
        '[[controller.nodes]]\nhostname = "NODE-13"\nurl = "http://10.0.0.13:9998"\n',
        encoding="utf-8",
    )
    saved = config.CONFIG_FILE
    config.CONFIG_FILE = toml  # type: ignore[attr-defined]
    config._loaded_config = None
    try:
        ctrl = config.get_controller_config()
    finally:
        config.CONFIG_FILE = saved  # type: ignore[attr-defined]
        config._loaded_config = None
    hosts = [n["hostname"] for n in ctrl["nodes"]]
    assert hosts == ["NODE-13", "10.0.0.11:9998", "10.0.0.12:9998"]  # .13 duplicate dropped
    assert all(n.get("pending_hostname") for n in ctrl["nodes"][1:])
    assert ctrl["poll_timeout_seconds"] == 3 and ctrl["poll_workers"] == 12


def test_registry_resolves_pending_hostname_and_drops_weaker_duplicate():
    reg = NodeRegistry(
        nodes_cfg=[{"hostname": "10.0.0.11:9998", "url": "http://10.0.0.11:9998", "pending_hostname": True}],
        groups_cfg=[{"name": "wall", "nodes": ["10.0.0.11:9998"]}],
    )
    reg.merge_discovered([{"hostname": "RENDER-01", "url": "http://10.0.0.11:9998"}])
    assert reg.count() == 2
    key = reg.resolve_pending_hostname("10.0.0.11:9998", "RENDER-01")
    assert key == "RENDER-01"
    assert reg.count() == 1
    node = reg.get_by_hostname("RENDER-01")
    assert node is not None and node.source == "config" and node.pending_hostname is False
    assert [n.hostname for n in reg.get_by_group("wall")] == ["RENDER-01"]
    # Idempotent / no-op afterwards
    assert reg.resolve_pending_hostname("RENDER-01", "RENDER-01") == "RENDER-01"


def test_registry_pending_ip_yields_to_explicit_config_node():
    reg = NodeRegistry(
        nodes_cfg=[
            {"hostname": "RENDER-01", "url": "http://10.0.0.11:9998"},
            {"hostname": "10.0.0.11:9998", "url": "http://10.0.0.11:9998", "pending_hostname": True},
        ],
        groups_cfg=[],
    )
    assert reg.resolve_pending_hostname("10.0.0.11:9998", "RENDER-01") == "RENDER-01"
    assert reg.count() == 1 and reg.get_by_hostname("RENDER-01").pending_hostname is False


# ---------------------------------------------------------------------------
# GPU: nvidia-smi parsing on a 4-GPU Blackwell node with N/A fields
# ---------------------------------------------------------------------------

def test_nvidia_smi_blackwell_4gpu_fixture(load_fixture):
    gpus = parse_nvidia_smi_csv(load_fixture("nvidia_smi_blackwell_4gpu.txt"))
    assert [g.index for g in gpus] == [0, 1, 2, 3]
    assert all(driver_info.is_blackwell_pro(g.name) for g in gpus)
    assert gpus[0].driver_version == "596.86" and gpus[0].pcie_gen == 5
    assert gpus[2].fan_speed_pct == 0 and gpus[3].fan_speed_pct == 0   # [N/A] / N/A -> 0
    assert gpus[3].power_draw_w == 0.0                                 # [N/A] -> 0.0
    assert gpus[0].display_active is True and gpus[3].display_active is False
    assert gpus[0].memory_total_mb == 97887
    # Fleet-current driver: no R595 upgrade advisory, but the Blackwell sync advisory.
    msgs = driver_info.check_known_issues(gpus)
    assert any("Timing Server" in m for m in msgs)
    assert not any("older than U7" in m for m in msgs)


def test_util_quick_edit_never_raises():
    from nvmon.util import disable_console_quick_edit
    assert disable_console_quick_edit() in (True, False)


# ---------------------------------------------------------------------------
# controller: Apply-All Mosaic preflight + dashboard job/stale surfacing
# ---------------------------------------------------------------------------

def _preflight_profile(tmp_path: Path, name: str, rate: str, enabled: bool = True) -> Path:
    import json as _json
    (tmp_path / f"{name}.json").write_text(_json.dumps({
        "name": name, "mosaic": {"enabled": enabled, "rows": 2, "cols": 2, "refresh_rate": rate},
        # exact-rate profile (since 2026-11-02 every profile is: a stored
        # 0.05 is capped at config.RATE_MATCH_TOLERANCE_HZ, 23.976 != 24)
        "validation": {"refresh_rate_tolerance": 0.01},
    }), encoding="utf-8")
    return tmp_path


def test_mosaic_preflight_skips_wrong_rate_keeps_matching_and_unreachable(tmp_path: Path):
    from nvmon.controller import fanout as fanout_mod
    from nvmon.controller.registry import NodeEntry
    _preflight_profile(tmp_path, "p30", "30")
    nodes = [NodeEntry("A", "http://a:9998"), NodeEntry("B", "http://b:9998"),
             NodeEntry("C", "http://c:9998"), NodeEntry("D", "http://d:9998")]
    seen = {}

    def opener(url, timeout):
        seen[url.split("//")[1].split(":")[0]] = url
        if "a:" in url:
            return {"rate_hz": 29.97, "valid": True}      # driver picks 29.97 -> skip
        if "b:" in url:
            return {"rate_hz": 30.0, "valid": True}       # exact -> keep
        if "c:" in url:
            raise OSError("HTTP Error 404")               # old agent -> keep
        return {"rate_hz": None, "valid": False}          # unknown -> keep

    kept, skipped, report = fanout_mod.mosaic_preflight(
        nodes, lambda n: "p30", profiles_dir=tmp_path, opener=opener,
    )
    assert [n.hostname for n in kept] == ["B", "C", "D"]
    assert skipped[0]["hostname"] == "A" and "29.97" in skipped[0]["reason"] and "30" in skipped[0]["reason"]
    assert all("profile=p30" in u for u in seen.values())
    by_host = {r["hostname"]: r for r in report}
    assert by_host["C"]["checked"] is False and by_host["B"]["checked"] is True


def test_mosaic_preflight_passes_through_when_profile_unknown_or_mosaic_off(tmp_path: Path):
    from nvmon.controller import fanout as fanout_mod
    from nvmon.controller.registry import NodeEntry
    _preflight_profile(tmp_path, "nomosaic", "60", enabled=False)
    calls = []
    nodes = [NodeEntry("A", "http://a:9998"), NodeEntry("B", "http://b:9998")]
    kept, skipped, report = fanout_mod.mosaic_preflight(
        nodes, lambda n: "nomosaic" if n.hostname == "A" else "missing",
        profiles_dir=tmp_path, opener=lambda u, t: calls.append(u) or {},
    )
    assert [n.hostname for n in kept] == ["A", "B"] and skipped == [] and report == [] and calls == []


def test_view_model_exposes_job_and_stale(monkeypatch):
    from nvmon.controller.dashboard_unified import view_model as vm
    raw = {"hostname": "N1", "reachable": True, "url": "http://n1:9998", "health": {"version": "0.8.0"},
           "status": {"status_age_s": 42.0, "job_active": True,
                      "job": {"in_progress": True, "action": "apply", "profile": "24",
                              "step": "[4/6] Configuring Mosaic"}}}
    node = vm._build_node(raw, group_for={}, sparkline_buf={})
    assert node["status_age_s"] == 42.0 and node["status_stale"] is True and node["job_active"] is True
    assert node["job"]["step"] == "[4/6] Configuring Mosaic" and node["job"]["known"] is True
    # pre-0.8.0 agent: no job dict -> known False, never stale
    old = vm._build_node({"hostname": "N0", "reachable": True, "status": {}}, group_for={}, sparkline_buf={})
    assert old["job"]["known"] is False and old["status_stale"] is False


def test_node_detail_renders_job_banner_with_cancel_and_stale_notice():
    from nvmon.controller.dashboard_unified import view_model as vm, render
    raw = {"hostname": "N1", "reachable": True, "url": "http://n1:9998", "health": {},
           "status": {"status_age_s": 42.0, "job_active": True,
                      "job": {"in_progress": True, "action": "apply", "profile": "24",
                              "step": "[4/6] Configuring Mosaic"}}}
    html = render.render_node_detail(vm._build_node(raw, group_for={}, sparkline_buf={}))
    assert "Applying" in html and "[4/6] Configuring Mosaic" in html
    assert "nvmonCancelApply('N1')" in html
    done = dict(raw); done["status"] = {"status_age_s": 1.0, "job": {
        "in_progress": False, "action": "apply", "profile": "24", "success": False,
        "message": "Mosaic came up at 29.97Hz, profile wants 30Hz", "finished_at": "2026-10-29T00:00:00Z"}}
    html = render.render_node_detail(vm._build_node(done, group_for={}, sparkline_buf={}))
    assert "FAILED" in html and "29.97Hz" in html and "nvmonCancelApply" not in html
    stale = dict(raw); stale["status"] = {"status_age_s": 40.0}
    html = render.render_node_detail(vm._build_node(stale, group_for={}, sparkline_buf={}))
    assert "STALE" in html


def test_sse_node_update_carries_age_and_job_flag():
    from nvmon.controller.dashboard_unified.sse import NodeUpdate
    p = NodeUpdate(hostname="N", subsystems={}, gpu=[], refresh_hz=0.0, displays_count=0,
                   alerts_count=0, last_poll_ms_ago=0, status_age_s=12.5, job_active=True).as_payload()
    assert p["status_age_s"] == 12.5 and p["job_active"] is True


def test_status_dict_carries_ccd_signals():
    from nvmon.agent.monitor import _build_status_dict
    from nvmon.types import ValidationResult
    dd = DDState(signals=[{"name": "HELIOS", "source": (7680, 4320), "position": (0, 0),
                           "target": (10240, 5760), "hz": 60.0}])
    data = _build_status_dict(
        dd=dd, mosaic=MosaicState(), qsync=QSyncState(), gpus=[], edid={},
        profile_name="", uptime_seconds=0, poll_count=1,
        validation=ValidationResult(all_passed=True, checks={}), alerts=[], errors=[],
    )
    sig = data["direct_display"]["signals"]
    assert sig == [{"name": "HELIOS", "source": [7680, 4320], "position": [0, 0],
                    "target": [10240, 5760], "hz": 60.0}]


def test_output_signal_ignores_rate_when_profile_drives_neither_mosaic_nor_dd():
    """QSync-only / desktop-only profile: scaling is still checked, the rate
    verdict is left to refresh_rate (no double failure on a 60 Hz desktop)."""
    from nvmon.types import ProfileDD
    p = Profile(
        mosaic=ProfileMosaic(enabled=False), direct_display=ProfileDD(enabled=False),
        qsync=ProfileQSync(enabled=True),
        validation=ProfileValidation(expected_refresh_rate=24.0, refresh_rate_tolerance=0.05),
    )
    ok = DDState(signals=[{"name": "PA278QV", "source": (2560, 1440), "target": (2560, 1440), "hz": 59.951}])
    assert validator._check_output_signal(ok, p).passed is True
    scaled = DDState(signals=[{"name": "PA278QV", "source": (2560, 1440), "target": (3840, 2160), "hz": 59.951}])
    assert validator._check_output_signal(scaled, p).passed is False
    # Same signal under a Mosaic profile expecting 24 -> rate IS judged.
    assert validator._check_output_signal(ok, _mosaic_profile("24")).passed is False


def test_controller_config_node_profiles_both_spellings(tmp_path: Path):
    toml = tmp_path / "nvmon.toml"
    toml.write_text(
        '[controller]\n'
        '[controller.node_profiles]\n"RENDER-01" = "24_Server"\n"render-02" = "24_Client"\n'
        '[[controller.nodes]]\nhostname = "NODE-11"\nurl = "http://10.0.0.11:9998"\nprofile = "24-Node11"\n'
        '[[controller.nodes]]\nhostname = "NODE-12"\nurl = "http://10.0.0.12:9998"\n',
        encoding="utf-8",
    )
    saved = config.CONFIG_FILE
    config.CONFIG_FILE = toml  # type: ignore[attr-defined]
    config._loaded_config = None
    try:
        ctrl = config.get_controller_config()
    finally:
        config.CONFIG_FILE = saved  # type: ignore[attr-defined]
        config._loaded_config = None
    assert ctrl["node_profiles"] == {"RENDER-01": "24_Server", "render-02": "24_Client", "NODE-11": "24-Node11"}
    m = ctrl["node_profiles"]
    assert config.node_profile_for("render-01", m) == "24_Server"
    assert config.node_profile_for("RENDER-02.studio.lan", m) == "24_Client"
    assert config.node_profile_for("NODE-11", m) == "24-Node11"
    assert config.node_profile_for("NODE-12", m) == ""
    assert config.node_profile_for("", m) == "" and config.node_profile_for("x", {}) == ""
