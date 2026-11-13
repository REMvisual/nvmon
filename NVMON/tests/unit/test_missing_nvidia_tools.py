"""The NVIDIA CLI tools ship in bin\\, but one can go missing. A node
without them must boot, report each subsystem as
unavailable with a message naming the file and where to put it, and never
crash a status poll."""

from __future__ import annotations

import pytest

from nvmon import config
from nvmon.agent import displays, mosaic, qsync

TOOLS = ["configureMosaic.exe", "configureDdisplay.exe", "configureQsync2.exe"]


@pytest.fixture
def no_nvidia_tools(tmp_path, monkeypatch):
    """Empty bin\\ + base dir, nothing on PATH, no toml overrides."""
    monkeypatch.setattr(config, "BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: None)
    real = config.get_agent_config()
    cfg = dict(real)
    cfg["bin"] = {k: "" for k in real["bin"]}
    monkeypatch.setattr(config, "get_agent_config", lambda: cfg)
    monkeypatch.setattr(displays, "_dd_unavailable", False)
    monkeypatch.setattr(displays, "_dd_unavailable_reason", "")
    return tmp_path / "bin"


def test_exe_lookup_returns_empty_when_absent(no_nvidia_tools):
    assert config.get_mosaic_exe() == ""
    assert config.get_dd_exe() == ""
    assert config.get_qsync_exe() == ""


def test_exe_lookup_finds_tool_dropped_in_bin(no_nvidia_tools):
    no_nvidia_tools.mkdir()
    (no_nvidia_tools / "configureMosaic.exe").write_bytes(b"")
    assert config.get_mosaic_exe() == str(no_nvidia_tools / "configureMosaic.exe")


@pytest.mark.parametrize("name", TOOLS)
def test_reason_names_file_and_bin_dir(no_nvidia_tools, name):
    reason = config.missing_exe_reason(name)
    assert reason.startswith(f"{name} not found")
    assert str(no_nvidia_tools) in reason
    assert "NVIDIA" in reason and "Bundled NVIDIA tools" in reason


@pytest.mark.parametrize("module,name", [
    (mosaic, "configureMosaic.exe"),
    (displays, "configureDdisplay.exe"),
    (qsync, "configureQsync2.exe"),
])
def test_capability_unavailable_with_clear_reason(no_nvidia_tools, module, name):
    cap = module.capability()
    assert cap["available"] is False
    assert cap["reason"].startswith(f"{name} not found")
    assert str(no_nvidia_tools) in cap["reason"]


def test_status_polls_do_not_crash(no_nvidia_tools):
    assert mosaic.status() is not None
    assert displays.status() is not None
    assert qsync.status() is not None
    # displays caches the failure; capability must still explain it.
    assert displays.capability()["reason"].startswith("configureDdisplay.exe not found")
