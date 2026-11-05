"""Tests for the BASE_DIR/active_profile.json boot-apply contract
(profiles.snapshot_active / clear_active / load_active / read_active_name).

The whole point of this snapshot is to make the listener's boot-apply
self-contained: profile dict + EDID bytes inlined as base64. So the
unit tests focus on round-tripping (snapshot → load returns the same
content), atomicity (tmp + replace), the EDID bundling path, and the
"snapshot absent" no-op cases.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from nvmon import config as cfgmod
from nvmon.agent import profiles


@pytest.fixture
def base_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point BASE_DIR at a tmp dir and seed profiles/."""
    monkeypatch.setattr(cfgmod, "BASE_DIR", tmp_path)
    # Profile loader honors the agent config's profile_dir; redirect it.
    monkeypatch.setattr(
        cfgmod, "get_agent_config",
        lambda: {"profile_dir": str(tmp_path / "profiles")},
    )
    (tmp_path / "profiles").mkdir()
    return tmp_path


def _write_profile_json(base: Path, name: str, payload: dict) -> Path:
    p = base / "profiles" / f"{name}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_snapshot_writes_self_contained_file(base_dir: Path):
    _write_profile_json(base_dir, "default", {
        "name": "default",
        "description": "test",
        "mosaic": {"enabled": True, "rows": 2, "cols": 2,
                   "width": 3840, "height": 2160, "refresh_rate": "23.98",
                   "outputs": []},
        "direct_display": {"enabled": True},
        "qsync": {"enabled": True, "source": "HOUSESYNC", "trigger": "FALLING"},
        "edid": {"file": ""},
        "validation": {},
    })

    out = profiles.snapshot_active("default")
    assert out == base_dir / "active_profile.json"
    assert out.is_file()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == profiles.ACTIVE_PROFILE_SCHEMA
    assert payload["name"] == "default"
    assert payload["edid_b64"] is None
    assert payload["profile"]["mosaic"]["enabled"] is True


def test_snapshot_inlines_edid_bytes(base_dir: Path):
    # Create a referenced EDID file (binary-y bytes; read_edid_file's
    # heuristic falls back to raw on non-ascii content).
    edid_dir = base_dir / "Edids"
    edid_dir.mkdir()
    blob = bytes(range(128))  # 128-byte EDID
    (edid_dir / "panel.bin").write_bytes(blob)

    _write_profile_json(base_dir, "edid_only", {
        "name": "edid_only",
        "edid": {"file": "Edids/panel.bin"},
    })

    profiles.snapshot_active("edid_only")
    payload = json.loads((base_dir / "active_profile.json").read_text("utf-8"))

    assert payload["edid_b64"] is not None
    decoded = base64.b64decode(payload["edid_b64"])
    assert decoded == blob


def test_load_active_round_trip_with_edid(base_dir: Path):
    edid_dir = base_dir / "Edids"
    edid_dir.mkdir()
    blob = b"\x00\x01\x02\x03" * 32  # 128 bytes
    (edid_dir / "panel.bin").write_bytes(blob)

    _write_profile_json(base_dir, "round", {
        "name": "round",
        "qsync": {"enabled": True, "source": "HOUSESYNC", "trigger": "FALLING"},
        "edid": {"file": "Edids/panel.bin"},
    })
    profiles.snapshot_active("round")

    loaded = profiles.load_active()
    assert loaded is not None
    profile, edid_bytes = loaded
    assert profile.name == "round"
    assert profile.qsync.enabled is True
    assert edid_bytes == blob
    # _edid_bytes is stashed on the Profile so _apply_inner uses it directly.
    assert getattr(profile, "_edid_bytes") == blob


def test_load_active_returns_none_when_missing(base_dir: Path):
    assert profiles.load_active() is None


def test_load_active_ignores_malformed_json(base_dir: Path):
    (base_dir / "active_profile.json").write_text("{not json", encoding="utf-8")
    assert profiles.load_active() is None


def test_clear_active_removes_file(base_dir: Path):
    _write_profile_json(base_dir, "default", {"name": "default"})
    profiles.snapshot_active("default")
    assert (base_dir / "active_profile.json").is_file()

    assert profiles.clear_active() is True
    assert not (base_dir / "active_profile.json").is_file()
    # Idempotent — second call returns False, doesn't crash.
    assert profiles.clear_active() is False


def test_read_active_name_returns_snapshot_name(base_dir: Path):
    _write_profile_json(base_dir, "named", {"name": "named"})
    profiles.snapshot_active("named")
    assert profiles.read_active_name() == "named"


def test_read_active_name_empty_when_missing(base_dir: Path):
    assert profiles.read_active_name() == ""


def test_read_active_name_rejects_filesystem_chars(base_dir: Path):
    (base_dir / "active_profile.json").write_text(
        json.dumps({"name": "evil/../boom"}), encoding="utf-8",
    )
    # read_active_name sanitizes — a slash makes the name unsafe to feed
    # back to profile_path() so we treat it as if no name was set.
    assert profiles.read_active_name() == ""


def test_snapshot_atomic_no_partial_file(base_dir: Path, monkeypatch):
    """A failure mid-write must not leave active_profile.json in a torn state."""
    _write_profile_json(base_dir, "default", {"name": "default"})

    # First, write a known-good snapshot so there's a previous version.
    profiles.snapshot_active("default")
    good_bytes = (base_dir / "active_profile.json").read_bytes()

    # Now sabotage tmp.replace() to fail and verify the existing file
    # survives unchanged. We patch json.dump to raise — the .tmp file
    # may exist briefly but the target should not be replaced.
    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(json, "dump", boom)

    with pytest.raises(OSError):
        profiles.snapshot_active("default")

    # Original file is intact.
    assert (base_dir / "active_profile.json").read_bytes() == good_bytes


def test_snapshot_raises_on_unknown_profile(base_dir: Path):
    with pytest.raises(FileNotFoundError):
        profiles.snapshot_active("nonexistent")
