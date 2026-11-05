"""Profile templates (v0.8.2): {template} in [controller.node_profiles].

Covers the pure config helpers the controller's Template button and
POST /all/template build on:
- template_patterns() — distinct templated values
- resolve_node_profiles() — token substitution / drop-when-blank
- discover_templates() — only COMPLETE sets are offered, natural order
"""

from __future__ import annotations

from pathlib import Path

from nvmon import config

SITE_MAP = {
    "RENDER-01": "{template} - SyncHost",
    "RENDER-02": "{template} - SyncClient",
    "RENDER-03": "{template} - SyncClient",
    "RENDER-05": "{template} - SyncHost",
    "RENDER-11": "1080p-odd-box",          # non-templated entry
}


def test_template_patterns_distinct_and_sorted():
    assert config.template_patterns(SITE_MAP) == ["{template} - SyncClient", "{template} - SyncHost"]
    assert config.template_patterns({"A": "plain"}) == []
    assert config.template_patterns({}) == []


def test_resolve_node_profiles_substitutes_and_keeps_plain_entries():
    out = config.resolve_node_profiles(SITE_MAP, "59.94 - 10b")
    assert out["RENDER-01"] == "59.94 - 10b - SyncHost"
    assert out["RENDER-02"] == "59.94 - 10b - SyncClient"
    assert out["RENDER-05"] == "59.94 - 10b - SyncHost"
    assert out["RENDER-11"] == "1080p-odd-box"
    # node_profile_for works on the resolved map exactly as before.
    assert config.node_profile_for("render-03.lan", out) == "59.94 - 10b - SyncClient"


def test_resolve_node_profiles_blank_template_drops_templated_entries():
    out = config.resolve_node_profiles(SITE_MAP, "")
    assert "RENDER-01" not in out          # falls back to active profile downstream
    assert out == {"RENDER-11": "1080p-odd-box"}


def test_discover_templates_only_complete_sets_natural_order():
    names = [
        "59.94 - 10b - SyncHost", "59.94 - 10b - SyncClient",
        "30 - SyncHost", "30 - SyncClient",
        "24 - SyncHost",                      # no client -> incomplete, excluded
        "25 - SyncClient",                    # no host   -> incomplete, excluded
        "default",                            # unrelated
    ]
    assert config.discover_templates(SITE_MAP, names) == ["30", "59.94 - 10b"]


def test_discover_templates_case_insensitive_and_whole_name():
    names = ["30 - synchost", "30 - SYNCCLIENT", "x30 - SyncHost - y", "30 - SyncClient"]
    assert config.discover_templates(SITE_MAP, names) == ["30"]


def test_discover_templates_no_patterns_returns_empty():
    assert config.discover_templates({"A": "plain"}, ["plain", "30 - SyncHost"]) == []


def test_controller_config_reads_template_default(tmp_path: Path):
    toml = tmp_path / "nvmon.toml"
    toml.write_text(
        '[controller]\ntemplate_default = "59.94 - 10b"\n'
        '[controller.node_profiles]\n"N-01" = "{template} - SyncHost"\n',
        encoding="utf-8",
    )
    saved = config.CONFIG_FILE
    try:
        config.CONFIG_FILE = toml  # type: ignore[attr-defined]
        config._loaded_config = None
        ctrl = config.get_controller_config()
    finally:
        config.CONFIG_FILE = saved  # type: ignore[attr-defined]
        config._loaded_config = None
    assert ctrl["template_default"] == "59.94 - 10b"
    assert ctrl["node_profiles"] == {"N-01": "{template} - SyncHost"}
    resolved = config.resolve_node_profiles(ctrl["node_profiles"], ctrl["template_default"])
    assert resolved == {"N-01": "59.94 - 10b - SyncHost"}
