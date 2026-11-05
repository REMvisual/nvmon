"""Tests for Monitor._maybe_apply_on_boot — the idempotent boot-time
profile apply.

The whole feature exists to skip re-applying when state already matches
the profile, so the test suite focuses on the gate decisions: which
inputs trigger the apply, which inputs skip it, and that the gate fires
exactly once per Monitor lifetime.

In v0.7.3 the boot trigger switched from "[agent].apply_on_boot toml +
.active_profile sticky" to a single self-contained snapshot at
BASE_DIR/active_profile.json. These tests pin the new behavior:
snapshot present → apply_active(); snapshot absent → no-op.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nvmon.agent.monitor import Monitor


def _monitor() -> Monitor:
    """Construct a Monitor with the boot-apply gate armed (not yet fired)."""
    m = Monitor()
    m._apply_on_boot_done = False
    return m


def _passing_status() -> dict:
    return {"validation": {"all_passed": True, "checks": {}}}


def _failing_status() -> dict:
    return {"validation": {"all_passed": False, "checks": {"refresh_rate": {"passed": False}}}}


def test_skips_when_no_snapshot(tmp_path):
    """No snapshot on disk → silent no-op (fresh / restored node)."""
    m = _monitor()
    snap = tmp_path / "active_profile.json"
    with patch("nvmon.agent.profiles.active_profile_path", return_value=snap), \
         patch("nvmon.agent.monitor.config.is_admin", return_value=True), \
         patch("nvmon.agent.profiles.apply_active") as mock_apply, \
         patch("threading.Thread") as mock_thread:
        m._maybe_apply_on_boot(_failing_status())
    mock_apply.assert_not_called()
    mock_thread.assert_not_called()
    assert m._apply_on_boot_done is True


def test_skips_when_validation_already_passes(tmp_path):
    """The whole point of idempotency: a node that came up clean is left alone."""
    m = _monitor()
    snap = tmp_path / "active_profile.json"
    snap.write_text('{"name":"default"}', encoding="utf-8")
    with patch("nvmon.agent.profiles.active_profile_path", return_value=snap), \
         patch("nvmon.agent.profiles.read_active_name", return_value="default"), \
         patch("nvmon.agent.monitor.config.is_admin", return_value=True), \
         patch("nvmon.agent.profiles.apply_active") as mock_apply, \
         patch("threading.Thread") as mock_thread:
        m._maybe_apply_on_boot(_passing_status())
    mock_apply.assert_not_called()
    mock_thread.assert_not_called()
    assert m._apply_on_boot_done is True


def _agent_cfg(window_min: int) -> dict:
    return {"boot_apply_max_uptime_minutes": window_min}


def test_applies_when_validation_fails(tmp_path):
    """Divergent state on a freshly booted box → spawn the apply thread."""
    m = _monitor()
    snap = tmp_path / "active_profile.json"
    snap.write_text('{"name":"default"}', encoding="utf-8")
    with patch("nvmon.agent.profiles.active_profile_path", return_value=snap), \
         patch("nvmon.agent.profiles.read_active_name", return_value="default"), \
         patch("nvmon.agent.monitor.config.is_admin", return_value=True), \
         patch("nvmon.agent.monitor.config.get_agent_config", return_value=_agent_cfg(15)), \
         patch("nvmon.agent.system.system_uptime_seconds", return_value=90.0), \
         patch("threading.Thread") as mock_thread:
        m._maybe_apply_on_boot(_failing_status())
    mock_thread.assert_called_once()
    # Background thread is daemon-named so it doesn't keep the process alive.
    assert mock_thread.call_args.kwargs.get("daemon") is True
    assert m._apply_on_boot_done is True


def test_listener_restart_is_not_a_boot(tmp_path):
    """Regression (site, 2026-11-01): "Restart all listeners" to pick up new
    code re-ran every node's boot-apply. Windows up for hours → the
    snapshot is left alone even though the validator reports drift."""
    m = _monitor()
    snap = tmp_path / "active_profile.json"
    snap.write_text('{"name":"default"}', encoding="utf-8")
    with patch("nvmon.agent.profiles.active_profile_path", return_value=snap), \
         patch("nvmon.agent.profiles.read_active_name", return_value="default"), \
         patch("nvmon.agent.monitor.config.is_admin", return_value=True), \
         patch("nvmon.agent.monitor.config.get_agent_config", return_value=_agent_cfg(15)), \
         patch("nvmon.agent.system.system_uptime_seconds", return_value=5 * 3600.0), \
         patch("threading.Thread") as mock_thread:
        m._maybe_apply_on_boot(_failing_status())
    mock_thread.assert_not_called()
    assert m._apply_on_boot_done is True


def test_uptime_window_zero_always_applies(tmp_path):
    m = _monitor()
    snap = tmp_path / "active_profile.json"
    snap.write_text('{"name":"default"}', encoding="utf-8")
    with patch("nvmon.agent.profiles.active_profile_path", return_value=snap), \
         patch("nvmon.agent.profiles.read_active_name", return_value="default"), \
         patch("nvmon.agent.monitor.config.is_admin", return_value=True), \
         patch("nvmon.agent.monitor.config.get_agent_config", return_value=_agent_cfg(0)), \
         patch("nvmon.agent.system.system_uptime_seconds", return_value=5 * 3600.0), \
         patch("threading.Thread") as mock_thread:
        m._maybe_apply_on_boot(_failing_status())
    mock_thread.assert_called_once()


def test_unknown_uptime_still_applies(tmp_path):
    """Non-Windows / ctypes failure → no uptime → behave as before (apply)."""
    m = _monitor()
    snap = tmp_path / "active_profile.json"
    snap.write_text('{"name":"default"}', encoding="utf-8")
    with patch("nvmon.agent.profiles.active_profile_path", return_value=snap), \
         patch("nvmon.agent.profiles.read_active_name", return_value="default"), \
         patch("nvmon.agent.monitor.config.is_admin", return_value=True), \
         patch("nvmon.agent.monitor.config.get_agent_config", return_value=_agent_cfg(15)), \
         patch("nvmon.agent.system.system_uptime_seconds", return_value=None), \
         patch("threading.Thread") as mock_thread:
        m._maybe_apply_on_boot(_failing_status())
    mock_thread.assert_called_once()


def test_skips_when_agent_not_admin(tmp_path):
    """configureMosaic / configureQsync2 require admin; without it apply
    can't succeed. Skip cleanly with a warning rather than fail loudly."""
    m = _monitor()
    snap = tmp_path / "active_profile.json"
    snap.write_text('{"name":"default"}', encoding="utf-8")
    with patch("nvmon.agent.profiles.active_profile_path", return_value=snap), \
         patch("nvmon.agent.profiles.read_active_name", return_value="default"), \
         patch("nvmon.agent.monitor.config.is_admin", return_value=False), \
         patch("nvmon.agent.monitor.config.get_agent_config", return_value=_agent_cfg(15)), \
         patch("nvmon.agent.system.system_uptime_seconds", return_value=90.0), \
         patch("threading.Thread") as mock_thread:
        m._maybe_apply_on_boot(_failing_status())
    mock_thread.assert_not_called()
    assert m._apply_on_boot_done is True


def test_default_no_snapshot_no_in_memory_profile():
    """Fresh Monitor with no snapshot and no --profile: profile_name is
    empty, the gate flag is unset, run() will short-circuit safely."""
    m = Monitor()
    assert m.profile_name == ""
    assert m._apply_on_boot_done is False
