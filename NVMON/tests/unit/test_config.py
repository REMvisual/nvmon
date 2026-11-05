"""Tests for nvmon.config.

These tests exercise the TOML loading and section accessors without requiring
any agent/controller code. They manipulate the cached config by monkeypatching
the module-level CONFIG_FILE / BASE_DIR and calling load_config(force_reload=True).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nvmon import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_TOML = """\
[agent]
port = 12345
poll_interval_seconds = 3
profile_dir = "./my_profiles"
status_file = "./my_status.json"
log_dir = "./my_logs"

[agent.gpu]
temp_warning_c = 75
power_warning_pct = 80

[agent.displays]
excluded_name_prefixes = ["IGN-", "SKIP-"]
excluded_resolutions = [[1920, 1080], [1280, 720]]

[agent.bin]
nvidia_smi = ""
configure_mosaic = ""
configure_ddisplay = ""
configure_qsync2 = ""

[controller]
port = 23456
poll_interval_seconds = 2
fanout_timeout_seconds = 10
password_file = "C:/some/where/controller.password"

[[controller.nodes]]
hostname = "NODE-A"
url = "http://10.0.0.1:9998"

[[controller.nodes]]
hostname = "NODE-B"
url = "http://10.0.0.2:9998"

[[controller.groups]]
name = "group-one"
nodes = ["NODE-A", "NODE-B"]

[launcher]
port = 34567

[[launcher.apps]]
name = "foo"
command = "C:/bin/foo.exe"
"""


INVALID_TOML = """\
[agent
port = "not closed
"""


@pytest.fixture(autouse=True)
def _reset_cached_config():
    """Ensure each test starts with a clean config cache."""
    config._loaded_config = None
    yield
    config._loaded_config = None


def _point_config_to(tmp_path: Path, contents: str | None) -> Path:
    """Write contents to tmp_path/nvmon.toml (or no file if contents is None)
    and monkey-point config.CONFIG_FILE at that path."""
    target = tmp_path / "nvmon.toml"
    if contents is not None:
        target.write_text(contents, encoding="utf-8")
    config.CONFIG_FILE = target  # type: ignore[attr-defined]
    config._loaded_config = None
    return target


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_valid_toml_loads(tmp_path: Path):
    _point_config_to(tmp_path, VALID_TOML)

    cfg = config.load_config(force_reload=True)
    assert isinstance(cfg, dict)
    assert "agent" in cfg
    assert "controller" in cfg
    assert "launcher" in cfg


def test_missing_file_returns_empty_dict(tmp_path: Path):
    # No file written - loader should silently return {}
    _point_config_to(tmp_path, None)

    cfg = config.load_config(force_reload=True)
    assert cfg == {}


def test_invalid_toml_returns_empty_dict(tmp_path: Path):
    # The loader swallows TOMLDecodeError and returns {} so the daemon
    # can still boot with defaults.
    _point_config_to(tmp_path, INVALID_TOML)

    cfg = config.load_config(force_reload=True)
    assert cfg == {}


def test_get_agent_config_with_toml(tmp_path: Path):
    _point_config_to(tmp_path, VALID_TOML)

    agent = config.get_agent_config()
    assert agent["port"] == 12345
    assert agent["poll_interval_seconds"] == 3
    assert agent["profile_dir"] == "./my_profiles"
    assert agent["status_file"] == "./my_status.json"
    assert agent["log_dir"] == "./my_logs"
    assert agent["gpu"]["temp_warning_c"] == 75
    assert agent["gpu"]["power_warning_pct"] == 80
    assert agent["displays"]["excluded_name_prefixes"] == ["IGN-", "SKIP-"]
    assert agent["displays"]["excluded_resolutions"] == [(1920, 1080), (1280, 720)]
    assert agent["bin"] == {
        "nvidia_smi": "",
        "configure_mosaic": "",
        "configure_ddisplay": "",
        "configure_qsync2": "",
    }


def test_get_agent_config_defaults(tmp_path: Path):
    _point_config_to(tmp_path, None)

    agent = config.get_agent_config()
    assert agent["port"] == config.DEFAULT_AGENT_PORT == 9998
    assert agent["poll_interval_seconds"] == config.DEFAULT_POLL_INTERVAL_SECONDS
    assert agent["gpu"]["temp_warning_c"] == config.DEFAULT_GPU_TEMP_WARNING_C
    assert agent["gpu"]["power_warning_pct"] == config.DEFAULT_GPU_POWER_WARNING_PCT
    assert agent["displays"]["excluded_name_prefixes"] == []
    assert agent["displays"]["excluded_resolutions"] == []
    assert isinstance(agent["bin"], dict)


def test_get_controller_config_with_toml(tmp_path: Path):
    _point_config_to(tmp_path, VALID_TOML)

    ctrl = config.get_controller_config()
    assert ctrl["port"] == 23456
    assert ctrl["poll_interval_seconds"] == 2
    assert ctrl["fanout_timeout_seconds"] == 10
    assert ctrl["password_file"] == "C:/some/where/controller.password"

    assert len(ctrl["nodes"]) == 2
    assert ctrl["nodes"][0] == {"hostname": "NODE-A", "url": "http://10.0.0.1:9998"}
    assert ctrl["nodes"][1]["hostname"] == "NODE-B"

    assert len(ctrl["groups"]) == 1
    assert ctrl["groups"][0]["name"] == "group-one"
    assert ctrl["groups"][0]["nodes"] == ["NODE-A", "NODE-B"]


def test_get_controller_config_defaults(tmp_path: Path):
    _point_config_to(tmp_path, None)

    ctrl = config.get_controller_config()
    assert ctrl["port"] == config.DEFAULT_CONTROLLER_PORT == 9997
    assert ctrl["poll_interval_seconds"] == config.DEFAULT_POLL_INTERVAL_SECONDS
    assert ctrl["fanout_timeout_seconds"] == config.DEFAULT_FANOUT_TIMEOUT_SECONDS
    assert ctrl["nodes"] == []
    assert ctrl["groups"] == []
    # password_file should default to something inside SECRETS_DIR
    assert "controller.password" in ctrl["password_file"]


def test_get_launcher_config_with_toml(tmp_path: Path):
    _point_config_to(tmp_path, VALID_TOML)

    launcher = config.get_launcher_config()
    assert launcher["port"] == 34567
    assert isinstance(launcher["apps"], list)
    assert len(launcher["apps"]) == 1
    assert launcher["apps"][0]["name"] == "foo"
    assert launcher["apps"][0]["command"] == "C:/bin/foo.exe"


def test_get_launcher_config_defaults(tmp_path: Path):
    _point_config_to(tmp_path, None)

    launcher = config.get_launcher_config()
    assert launcher["port"] == config.DEFAULT_LAUNCHER_PORT == 9996
    assert launcher["apps"] == []


def test_section_accessors_return_correct_types(tmp_path: Path):
    _point_config_to(tmp_path, VALID_TOML)

    agent = config.get_agent_config()
    ctrl = config.get_controller_config()
    launcher = config.get_launcher_config()

    assert isinstance(agent, dict)
    assert isinstance(ctrl, dict)
    assert isinstance(launcher, dict)

    assert isinstance(agent["port"], int)
    assert isinstance(agent["poll_interval_seconds"], int)
    assert isinstance(agent["profile_dir"], str)
    assert isinstance(agent["displays"]["excluded_name_prefixes"], list)
    assert isinstance(agent["displays"]["excluded_resolutions"], list)
    for res in agent["displays"]["excluded_resolutions"]:
        assert isinstance(res, tuple)
        assert len(res) == 2
        assert all(isinstance(x, int) for x in res)

    assert isinstance(ctrl["nodes"], list)
    assert isinstance(ctrl["groups"], list)
    assert isinstance(launcher["apps"], list)


def test_excluded_resolutions_bad_entries_skipped(tmp_path: Path):
    bad_toml = """\
[agent.displays]
excluded_resolutions = [[1920, 1080], ["not", "a", "pair"], [640, 480]]
"""
    _point_config_to(tmp_path, bad_toml)

    agent = config.get_agent_config()
    res = agent["displays"]["excluded_resolutions"]
    # Only the two valid pairs should survive.
    assert (1920, 1080) in res
    assert (640, 480) in res
    assert len(res) == 2


def test_is_excluded_display_honors_config(tmp_path: Path):
    toml_contents = """\
[agent.displays]
excluded_name_prefixes = ["IGN-"]
excluded_resolutions = [[1920, 1080]]
"""
    _point_config_to(tmp_path, toml_contents)

    assert config.is_excluded_display("IGN-FOO") is True
    assert config.is_excluded_display("OK-FOO") is False
    # Resolution-based exclusion only applies for empty / unknown names.
    assert config.is_excluded_display("", (1920, 1080)) is True
    assert config.is_excluded_display("Unknown", (1920, 1080)) is True
    assert config.is_excluded_display("Has-Name", (1920, 1080)) is False


def test_load_config_caches(tmp_path: Path):
    _point_config_to(tmp_path, VALID_TOML)

    first = config.load_config()
    # Even if we blank the underlying file, cached result should be returned.
    (tmp_path / "nvmon.toml").write_text("", encoding="utf-8")
    second = config.load_config()
    assert first is second

    # force_reload=True should pick up the empty file.
    third = config.load_config(force_reload=True)
    assert third == {}


def test_hostname_and_admin_are_callable():
    # We don't assert the value - only that they run without raising
    # and return the right types.
    name = config.get_hostname()
    assert isinstance(name, str)
    assert name  # non-empty

    ip = config.get_local_ip()
    assert isinstance(ip, str)
    assert ip  # non-empty (at least "127.0.0.1" fallback)

    admin = config.is_admin()
    assert isinstance(admin, bool)


def test_announce_ip_override_wins_over_autodetect(tmp_path: Path):
    """[network].announce_ip pins the IP returned by get_local_ip()."""
    _point_config_to(tmp_path, '[network]\nannounce_ip = "10.42.0.7"\n')
    config.load_config(force_reload=True)
    assert config.get_local_ip() == "10.42.0.7"


def test_announce_ip_blank_falls_through_to_autodetect(tmp_path: Path):
    """Empty announce_ip leaves the auto-detect path intact."""
    _point_config_to(tmp_path, '[network]\nannounce_ip = ""\n')
    config.load_config(force_reload=True)
    ip = config.get_local_ip()
    assert ip
    assert ip != ""  # auto-detected something


def test_no_network_section_falls_through_to_autodetect(tmp_path: Path):
    """Missing [network] section is fine — auto-detect runs as before."""
    _point_config_to(tmp_path, "[agent]\nport = 9998\n")
    config.load_config(force_reload=True)
    ip = config.get_local_ip()
    assert ip


# ---------------------------------------------------------------------------
# [network].subnet — control-net pin (v0.8.2)
# ---------------------------------------------------------------------------

def test_parse_subnet_and_ip_in_subnet():
    net = config.parse_subnet("10.20.30.0/24")
    assert net is not None
    assert config.ip_in_subnet("10.20.30.101", net)
    assert config.ip_in_subnet("10.20.30.101", "10.20.30.0/24")
    assert not config.ip_in_subnet("10.20.40.101", net)      # Content-Net, not Control-Net
    assert not config.ip_in_subnet("not-an-ip", net)
    assert not config.ip_in_subnet("", net)
    # Non-strict: a host address is accepted as its network.
    assert config.parse_subnet("10.20.30.120/24") == net
    # Blank / garbage subnet = no restriction.
    assert config.parse_subnet("") is None
    assert config.parse_subnet("control-net") is None
    assert config.ip_in_subnet("1.2.3.4", "")
    assert config.ip_in_subnet("1.2.3.4", None)


def test_subnet_pin_picks_stage_net_address_over_default_route(tmp_path: Path, monkeypatch):
    """Two-NIC node: default route on Content-Net (10.1.10.x), nvmon pinned to
    Control-Net (10.1.110.x). get_local_ip() must return the Control-Net IP."""
    _point_config_to(tmp_path, '[network]\nsubnet = "10.20.30.0/24"\n')
    config.load_config(force_reload=True)
    monkeypatch.setattr(config, "_local_ipv4_addresses", lambda: ["10.20.40.101", "10.20.30.101"])

    class _FakeSock:
        """UDP connect() trick: always reports the default-route NIC."""
        def __init__(self, *a, **k): pass
        def connect(self, addr): pass
        def getsockname(self): return ("10.20.40.101", 0)
        def close(self): pass

    monkeypatch.setattr(config.socket, "socket", _FakeSock)
    assert config.get_local_ip() == "10.20.30.101"


def test_subnet_pin_uses_routing_probe_when_it_lands_in_subnet(tmp_path: Path, monkeypatch):
    _point_config_to(tmp_path, '[network]\nsubnet = "10.20.30.0/24"\n')
    config.load_config(force_reload=True)
    monkeypatch.setattr(config, "_local_ipv4_addresses", lambda: [])

    class _FakeSock:
        def __init__(self, *a, **k): pass
        def connect(self, addr): self.addr = addr
        def getsockname(self): return ("10.20.30.105", 0)
        def close(self): pass

    monkeypatch.setattr(config.socket, "socket", _FakeSock)
    assert config.get_local_ip() == "10.20.30.105"


def test_subnet_pin_falls_back_when_host_has_no_address_in_it(tmp_path: Path, monkeypatch, caplog):
    """No control-net address at all: warn once, fall through to auto-detect
    so the listener still comes up (a dead agent is worse than a mis-announced one)."""
    _point_config_to(tmp_path, '[network]\nsubnet = "10.20.30.0/24"\n')
    config.load_config(force_reload=True)
    monkeypatch.setattr(config, "_local_ipv4_addresses", lambda: ["10.20.40.101"])
    monkeypatch.setattr(config, "_subnet_miss_warned", False)

    class _FakeSock:
        def __init__(self, *a, **k): pass
        def connect(self, addr): pass
        def getsockname(self): return ("10.20.40.101", 0)
        def close(self): pass

    monkeypatch.setattr(config.socket, "socket", _FakeSock)
    with caplog.at_level("WARNING", logger="nvmon.config"):
        ip = config.get_local_ip()
        ip2 = config.get_local_ip()
    assert ip == "10.20.40.101" == ip2
    warnings = [r for r in caplog.records if "no address in it" in r.message]
    assert len(warnings) == 1  # warned once, not on every call


def test_announce_ip_beats_subnet_pin(tmp_path: Path, monkeypatch):
    _point_config_to(tmp_path, '[network]\nsubnet = "10.20.30.0/24"\nannounce_ip = "192.168.9.9"\n')
    config.load_config(force_reload=True)
    assert config.get_local_ip() == "192.168.9.9"


def test_get_network_config_defaults(tmp_path: Path):
    _point_config_to(tmp_path, "[agent]\nport = 9998\n")
    config.load_config(force_reload=True)
    assert config.get_network_config() == {"announce_ip": "", "subnet": ""}
