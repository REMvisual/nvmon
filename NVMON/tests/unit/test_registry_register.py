"""Unit tests for the push-registration additions to NodeRegistry.

Covers:
  * merge_registered: add new, update existing-registered, skip config-collision,
    upgrade discovery -> registered.
  * remove_registered: refuses non-registered entries.
  * load_registered_from_file: missing/malformed file is non-fatal.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nvmon.controller.registry import NodeEntry, NodeRegistry


def _empty_registry() -> NodeRegistry:
    return NodeRegistry(nodes_cfg=[], groups_cfg=[])


def _config_registry() -> NodeRegistry:
    return NodeRegistry(
        nodes_cfg=[{"hostname": "NODE-CFG", "url": "http://10.0.0.99:9998"}],
        groups_cfg=[],
    )


def test_merge_registered_adds_new() -> None:
    reg = _empty_registry()
    added, updated, skipped = reg.merge_registered([
        {"hostname": "NODE-04", "url": "http://10.0.0.4:9998",
         "machine_id": "mid04", "version": "0.6.19"},
    ])
    assert (added, updated, skipped) == (1, 0, 0)
    n = reg.get_by_hostname("NODE-04")
    assert n is not None
    assert n.source == "registered"
    assert n.machine_id == "mid04"
    assert n.version == "0.6.19"
    assert n.last_register > 0


def test_merge_registered_skips_on_config_collision() -> None:
    reg = _config_registry()
    added, updated, skipped = reg.merge_registered([
        {"hostname": "NODE-CFG", "url": "http://10.0.0.42:9998"},
    ])
    assert (added, updated, skipped) == (0, 0, 1)
    n = reg.get_by_hostname("NODE-CFG")
    assert n is not None
    assert n.source == "config"
    assert n.url == "http://10.0.0.99:9998"


def test_merge_registered_skips_on_config_collision_case_insensitive() -> None:
    # Toml entries are hand-typed; agents report socket.gethostname() which
    # on Windows is usually all-caps. A case-sensitive collision check left
    # 'Node-02' (toml) and 'NODE-02' (/register) as two duplicate cards.
    reg = NodeRegistry(
        nodes_cfg=[{"hostname": "Node-02", "url": "http://10.0.0.2:9998"}],
        groups_cfg=[],
    )
    added, updated, skipped = reg.merge_registered([
        {"hostname": "NODE-02", "url": "http://169.254.200.38:9998",
         "machine_id": "mid02", "version": "0.7.2"},
    ])
    assert (added, updated, skipped) == (0, 0, 1)
    # Config wins — only the toml-cased entry survives, with its URL intact.
    assert reg.count() == 1
    n = reg.get_by_hostname("Node-02")
    assert n is not None
    assert n.source == "config"
    assert n.url == "http://10.0.0.2:9998"


def test_merge_discovered_skips_on_config_collision_case_insensitive() -> None:
    reg = NodeRegistry(
        nodes_cfg=[{"hostname": "Node-02", "url": "http://10.0.0.2:9998"}],
        groups_cfg=[],
    )
    reg.merge_discovered([
        {"hostname": "NODE-02", "url": "http://10.0.0.2:9998"},
    ])
    assert reg.count() == 1
    assert reg.get_by_hostname("Node-02").source == "config"


def test_merge_discovered_does_not_downgrade_registered() -> None:
    # mDNS announce arriving after a /register POST should not flip the
    # entry back to source=discovery or rewrite the registered URL.
    reg = _empty_registry()
    reg.merge_registered([
        {"hostname": "NODE-X", "url": "http://10.0.0.9:9998", "version": "0.7.2"},
    ])
    reg.merge_discovered([
        {"hostname": "NODE-X", "url": "http://10.0.0.9:9998"},
    ])
    n = reg.get_by_hostname("NODE-X")
    assert n is not None
    assert n.source == "registered"
    assert n.version == "0.7.2"


def test_merge_registered_updates_existing_registered() -> None:
    reg = _empty_registry()
    reg.merge_registered([
        {"hostname": "NODE-04", "url": "http://old:9998", "version": "0.6.10"},
    ])
    added, updated, skipped = reg.merge_registered([
        {"hostname": "NODE-04", "url": "http://new:9998", "version": "0.6.19"},
    ])
    assert (added, updated, skipped) == (0, 1, 0)
    n = reg.get_by_hostname("NODE-04")
    assert n is not None
    assert n.url == "http://new:9998"
    assert n.version == "0.6.19"


def test_merge_registered_upgrades_discovery_to_registered() -> None:
    reg = _empty_registry()
    reg.merge_discovered([
        {"hostname": "NODE-D", "url": "http://10.0.0.5:9998"},
    ])
    assert reg.get_by_hostname("NODE-D").source == "discovery"

    added, updated, skipped = reg.merge_registered([
        {"hostname": "NODE-D", "url": "http://10.0.0.5:9998", "version": "0.6.19"},
    ])
    assert (added, updated, skipped) == (0, 1, 0)
    assert reg.get_by_hostname("NODE-D").source == "registered"


def test_merge_registered_drops_blank_rows() -> None:
    reg = _empty_registry()
    added, updated, skipped = reg.merge_registered([
        {"hostname": "", "url": "http://x:9998"},
        {"hostname": "ok", "url": ""},
    ])
    assert added == 0
    assert reg.count() == 0


def test_remove_registered_refuses_config_entries() -> None:
    reg = _config_registry()
    assert reg.remove_registered("NODE-CFG") is False
    assert reg.get_by_hostname("NODE-CFG") is not None


def test_remove_registered_drops_registered_entry() -> None:
    reg = _empty_registry()
    reg.merge_registered([{"hostname": "NODE-R", "url": "http://r:9998"}])
    assert reg.remove_registered("NODE-R") is True
    assert reg.get_by_hostname("NODE-R") is None
    # Idempotent: second call returns False.
    assert reg.remove_registered("NODE-R") is False


def test_load_registered_missing_file_is_noop(tmp_path: Path) -> None:
    reg = _empty_registry()
    n = reg.load_registered_from_file(tmp_path / "does_not_exist.json")
    assert n == 0
    assert reg.count() == 0


def test_load_registered_malformed_json_is_noop(tmp_path: Path) -> None:
    reg = _empty_registry()
    bad = tmp_path / "bad.json"
    bad.write_text("not json{", encoding="utf-8")
    n = reg.load_registered_from_file(bad)
    assert n == 0
    assert reg.count() == 0


def test_load_registered_round_trips(tmp_path: Path) -> None:
    """Persist via merge_registered shape, then load — count matches."""
    file = tmp_path / "registered_nodes.json"
    payload = {
        "nodes": [
            {"hostname": "NODE-04", "url": "http://10.0.0.4:9998",
             "machine_id": "mid04", "version": "0.6.19", "last_register": 12345.0},
            {"hostname": "NODE-05", "url": "http://10.0.0.5:9998"},
        ],
    }
    file.write_text(json.dumps(payload), encoding="utf-8")
    reg = _empty_registry()
    n = reg.load_registered_from_file(file)
    assert n == 2
    n4 = reg.get_by_hostname("NODE-04")
    assert n4 is not None and n4.source == "registered"
    assert n4.last_register == 12345.0


def test_get_registered_filters_by_source() -> None:
    reg = _config_registry()  # has NODE-CFG (source=config)
    reg.merge_discovered([{"hostname": "NODE-D", "url": "http://d:9998"}])
    reg.merge_registered([{"hostname": "NODE-R", "url": "http://r:9998"}])
    names = [n.hostname for n in reg.get_registered()]
    assert names == ["NODE-R"]


def test_to_public_dict_includes_register_fields() -> None:
    reg = _empty_registry()
    reg.merge_registered([
        {"hostname": "NODE-04", "url": "http://r:9998", "version": "0.6.19"},
    ])
    snap = reg.get_by_hostname("NODE-04").to_public_dict()
    assert snap["source"] == "registered"
    assert snap["version"] == "0.6.19"
    assert "last_register" in snap


def test_to_public_dict_omits_register_fields_for_config_entries() -> None:
    reg = _config_registry()
    snap = reg.get_by_hostname("NODE-CFG").to_public_dict()
    assert snap["source"] == "config"
    assert "version" not in snap
    assert "last_register" not in snap


# ---------------------------------------------------------------------------
# evict_stale_registered (auto-eviction sweep)
# ---------------------------------------------------------------------------


def test_evict_stale_registered_drops_old_entries() -> None:
    reg = _empty_registry()
    now = time.time()
    reg.merge_registered([
        {"hostname": "NODE-OLD", "url": "http://old:9998",
         "last_register": now - 7200},  # 2 hours ago
        {"hostname": "NODE-FRESH", "url": "http://fresh:9998",
         "last_register": now - 60},    # 1 minute ago
    ])
    evicted = reg.evict_stale_registered(ttl_seconds=3600)  # 1 hour TTL
    assert evicted == ["NODE-OLD"]
    assert reg.get_by_hostname("NODE-OLD") is None
    assert reg.get_by_hostname("NODE-FRESH") is not None


def test_evict_stale_registered_returns_hostname_list() -> None:
    reg = _empty_registry()
    now = time.time()
    reg.merge_registered([
        {"hostname": f"NODE-{i}", "url": f"http://h{i}:9998",
         "last_register": now - 7200}
        for i in range(3)
    ])
    evicted = reg.evict_stale_registered(ttl_seconds=3600)
    assert sorted(evicted) == ["NODE-0", "NODE-1", "NODE-2"]
    assert reg.count() == 0


def test_evict_stale_registered_refuses_config_entries() -> None:
    reg = _config_registry()  # NODE-CFG is source=config; last_register == 0
    evicted = reg.evict_stale_registered(ttl_seconds=3600)
    assert evicted == []
    assert reg.get_by_hostname("NODE-CFG") is not None


def test_evict_stale_registered_refuses_discovery_entries() -> None:
    reg = _empty_registry()
    reg.merge_discovered([{"hostname": "NODE-D", "url": "http://d:9998"}])
    # Discovery entries have last_register = 0 — would be "stale" if source
    # were not respected.
    evicted = reg.evict_stale_registered(ttl_seconds=3600)
    assert evicted == []
    assert reg.get_by_hostname("NODE-D") is not None


def test_evict_stale_registered_disabled_when_ttl_zero() -> None:
    reg = _empty_registry()
    reg.merge_registered([
        {"hostname": "NODE-OLD", "url": "http://old:9998",
         "last_register": 1.0},  # ancient
    ])
    assert reg.evict_stale_registered(ttl_seconds=0) == []
    assert reg.evict_stale_registered(ttl_seconds=-5) == []
    assert reg.get_by_hostname("NODE-OLD") is not None


def test_evict_stale_registered_keeps_recent() -> None:
    reg = _empty_registry()
    reg.merge_registered([
        {"hostname": "NODE-NEW", "url": "http://new:9998"},
    ])
    # merge_registered defaults last_register to time.time() when not given.
    evicted = reg.evict_stale_registered(ttl_seconds=3600)
    assert evicted == []
    assert reg.get_by_hostname("NODE-NEW") is not None
