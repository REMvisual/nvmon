"""Self-host guard for the controller's power fanout.

The controller refuses to restart/shutdown the machine it's running on;
losing the controller mid-operation would orphan the dashboard and
confuse fleet recovery. These tests exercise that guard end-to-end at
the fanout layer:

    * `is_self_host` matches case-insensitively and strips the FQDN.
    * `restart_all` / `shutdown_all` / `abort_all` skip the self-host
      with the canonical SKIP_REASON_SELF reason and never make an HTTP
      call to it.
    * Only-self registry returns a single skipped entry, no HTTP at all.
    * Mixed registry skips self, fans out to the rest.
"""

from __future__ import annotations

from unittest import mock

import pytest

from nvmon.controller import fanout as fanout_mod
from nvmon.controller.registry import NodeEntry, NodeRegistry


# ---------------------------------------------------------------------------
# is_self_host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("own,candidate,expected", [
    ("monitor", "monitor", True),
    ("monitor", "MONITOR", True),                 # case-insensitive
    ("monitor", "monitor.local", True),           # FQDN strip
    ("monitor.lan", "monitor", True),             # both sides FQDN-stripped
    ("monitor", "node1", False),
    ("monitor", "monitor1", False),               # NOT a prefix match
    ("monitor", "", False),                       # empty candidate → False
    ("",        "monitor", False),                # empty self → False
])
def test_is_self_host(own: str, candidate: str, expected: bool) -> None:
    with mock.patch("nvmon.config.get_hostname", return_value=own):
        assert fanout_mod.is_self_host(candidate) is expected


def test_is_self_host_swallows_config_errors() -> None:
    # If get_hostname() blows up for any reason, we MUST fail closed (False)
    # rather than crash the fanout — otherwise a transient config glitch
    # would lock out every power action.
    with mock.patch("nvmon.config.get_hostname", side_effect=RuntimeError("boom")):
        assert fanout_mod.is_self_host("monitor") is False


# ---------------------------------------------------------------------------
# Fanout integration
# ---------------------------------------------------------------------------

def _make_registry(hostnames: list[str]) -> NodeRegistry:
    # Load with no nodes via config, then inject manually so we don't need
    # a real nvmon.toml in the test workspace.
    reg = NodeRegistry(nodes_cfg=[], groups_cfg=[])
    for h in hostnames:
        reg._nodes[h] = NodeEntry(  # noqa: SLF001 — test fixture
            hostname=h,
            url=f"http://{h}:9998",
            reachable=True,
            source="config",
        )
    return reg


def test_restart_all_skips_self_host_and_calls_only_others() -> None:
    reg = _make_registry(["monitor", "node1", "node2"])
    fanout = fanout_mod.FanoutExecutor(reg, timeout_seconds=1)

    # Simulate /system/restart returning 202 on every reachable node.
    fake_call_results = {
        "node1": {"hostname": "node1", "status": "accepted", "http_code": 202},
        "node2": {"hostname": "node2", "status": "accepted", "http_code": 202},
    }
    called_hosts: list[str] = []

    def _fake_call_one(node, method, path, body):
        called_hosts.append(node.hostname)
        assert path == "/system/restart"
        return fake_call_results[node.hostname]

    with mock.patch("nvmon.config.get_hostname", return_value="monitor"), \
         mock.patch.object(fanout_mod.FanoutExecutor, "_call_one", side_effect=_fake_call_one):
        result = fanout.restart_all()

    # The HTTP layer was NEVER asked to contact 'monitor'.
    assert "monitor" not in called_hosts
    assert sorted(called_hosts) == ["node1", "node2"]

    # Tally: 3 requested (registry size), 2 accepted, 1 skipped (self).
    assert result["requested"] == 3
    assert result["succeeded"] == 2
    assert result["skipped"] == 1
    assert result["failed"] == 0

    by_host = {r["hostname"]: r for r in result["results"]}
    assert by_host["monitor"]["status"] == "skipped"
    assert by_host["monitor"]["reason"] == fanout_mod.SKIP_REASON_SELF
    assert by_host["node1"]["status"] == "accepted"
    assert by_host["node2"]["status"] == "accepted"


def test_shutdown_all_with_only_self_makes_no_http_calls() -> None:
    # Single-box deployment: controller and listener share the host. The
    # whole registry is self → no targets → zero HTTP calls.
    reg = _make_registry(["solo"])
    fanout = fanout_mod.FanoutExecutor(reg, timeout_seconds=1)

    with mock.patch("nvmon.config.get_hostname", return_value="solo"), \
         mock.patch.object(fanout_mod.FanoutExecutor, "_call_one") as mock_call:
        result = fanout.shutdown_all()

    mock_call.assert_not_called()
    assert result["requested"] == 1
    assert result["skipped"] == 1
    assert result["succeeded"] == 0
    assert result["results"][0]["reason"] == fanout_mod.SKIP_REASON_SELF


def test_abort_all_also_filters_self_host_for_symmetry() -> None:
    # `shutdown.exe /a` is harmless on the monitor box, but we still
    # surface the self-host as skipped so the operator's result toast
    # tells a consistent story across restart / shutdown / abort.
    reg = _make_registry(["monitor", "node1"])
    fanout = fanout_mod.FanoutExecutor(reg, timeout_seconds=1)

    with mock.patch("nvmon.config.get_hostname", return_value="monitor"), \
         mock.patch.object(
             fanout_mod.FanoutExecutor, "_call_one",
             return_value={"hostname": "node1", "status": "accepted", "http_code": 202},
         ) as mock_call:
        result = fanout.abort_all()

    # Only node1 was contacted.
    assert mock_call.call_count == 1
    assert mock_call.call_args.args[0].hostname == "node1"

    by_host = {r["hostname"]: r for r in result["results"]}
    assert by_host["monitor"]["status"] == "skipped"
    assert by_host["monitor"]["reason"] == fanout_mod.SKIP_REASON_SELF


def test_normalize_host_drops_fqdn_label() -> None:
    assert fanout_mod._normalize_host("Node1.Local") == "node1"
    assert fanout_mod._normalize_host("  HOST  ") == "host"
    assert fanout_mod._normalize_host("") == ""
