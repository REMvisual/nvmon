"""Tests for EDID capture-and-apply as a first-class subsystem (nvmon-jil).

Covers:
- read_edid_file auto-detects hex-text vs raw binary.
- find_edid_file_matching matches by content not by name.
- apply_edid_to_all_displays is idempotent (skips when bytes match) and
  writes only when bytes differ.
- save_current_state captures the live EDID_OVERRIDE blob into
  ``Edids/<profile>.txt`` (hex text) and points the profile at it.
- save_current_state reuses an existing matching EDID file rather than
  writing a duplicate.
- _apply_inner short-circuit no longer drops EDID-only profiles to restore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nvmon import config
from nvmon.agent import edid as edid_mod
from nvmon.agent import profile_capture, profiles
from nvmon.types import Profile, ProfileEDID


# ---------------------------------------------------------------------------
# read_edid_file
# ---------------------------------------------------------------------------

def test_read_edid_file_hex_text_round_trip(tmp_path: Path) -> None:
    raw = bytes(range(64)) + bytes([0xff] * 64)
    p = tmp_path / "hex.txt"
    p.write_text(" ".join(f"{b:02x}" for b in raw) + "\n", encoding="ascii")
    assert edid_mod.read_edid_file(p) == raw


def test_read_edid_file_hex_text_with_newlines(tmp_path: Path) -> None:
    raw = b"\x00\xff\xff\xff" * 32
    hex_text = " ".join(f"{b:02x}" for b in raw)
    # Break across lines like a real dump might.
    chunks = [hex_text[i:i + 48] for i in range(0, len(hex_text), 48)]
    p = tmp_path / "wrapped.txt"
    p.write_text("\n".join(chunks) + "\n", encoding="ascii")
    assert edid_mod.read_edid_file(p) == raw


def test_read_edid_file_raw_binary(tmp_path: Path) -> None:
    raw = bytes([0x00, 0xff, 0xfe, 0x80, 0x10, 0x99]) + bytes(range(20))
    p = tmp_path / "raw.bin"
    p.write_bytes(raw)
    assert edid_mod.read_edid_file(p) == raw


def test_read_edid_file_empty_raises(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    with pytest.raises(ValueError):
        edid_mod.read_edid_file(p)


def test_read_edid_file_real_workspace_file() -> None:
    """The shipped Edids/Node1-24-Server.txt should parse to ~256 bytes."""
    p = config.BASE_DIR / "Edids" / "Node1-24-Server.txt"
    if not p.is_file():
        pytest.skip("workspace EDID sample not present")
    blob = edid_mod.read_edid_file(p)
    # EDID 1.4 base block is 128 bytes; 1.4 with one extension is 256.
    # The shipped file is 256 bytes.
    assert len(blob) == 256
    assert blob[:8] == b"\x00\xff\xff\xff\xff\xff\xff\x00"  # EDID magic


# ---------------------------------------------------------------------------
# parse_edid
# ---------------------------------------------------------------------------

def test_parse_edid_rejects_non_edid_blob() -> None:
    out = edid_mod.parse_edid(b"\x00" * 128)
    assert out["valid"] is False
    assert out["size_bytes"] == 128


def test_parse_edid_rejects_short_blob() -> None:
    out = edid_mod.parse_edid(b"\x00\xff\xff\xff\xff\xff\xff\x00short")
    assert out["valid"] is False


def test_parse_edid_real_node1_server_sample() -> None:
    """Node1-24-Server.txt is a Tessera SX40 EDID at 24 Hz."""
    p = config.BASE_DIR / "Edids" / "Node1-24-Server.txt"
    if not p.is_file():
        pytest.skip("workspace EDID sample not present")
    parsed = edid_mod.parse_edid(edid_mod.read_edid_file(p))
    assert parsed["valid"] is True
    assert parsed["size_bytes"] == 256
    assert parsed["monitor_name"] == "Tessera SX40"
    assert parsed["edid_version"] == "1.3"
    assert parsed["extension_count"] == 1
    # The preferred timing in the base block is the 60 Hz native mode
    # (2160x3024). The 24 Hz mode operators run at lives in the CTA-861
    # extension block, not the base DTD — so don't confuse the file name
    # with the EDID's advertised preferred timing.
    assert "preferred_mode" in parsed
    assert parsed["preferred_width"] == 2160
    assert parsed["preferred_height"] == 3024
    assert abs(parsed["preferred_refresh_hz"] - 60.0) < 0.5


def test_parse_edid_real_node2_client_sample() -> None:
    p = config.BASE_DIR / "Edids" / "Node2-24-Client.txt"
    if not p.is_file():
        pytest.skip("workspace EDID sample not present")
    parsed = edid_mod.parse_edid(edid_mod.read_edid_file(p))
    assert parsed["valid"] is True
    assert parsed["monitor_name"] == "Tessera SX40"
    assert parsed["edid_version"] == "1.3"
    # Year/week are real values, not zero.
    assert parsed["year_of_manufacture"] >= 1990
    assert 0 <= parsed["week_of_manufacture"] <= 54


# ---------------------------------------------------------------------------
# status() with active overrides — surfaces parsed details + source file
# ---------------------------------------------------------------------------

def test_status_active_details_includes_parsed_edid_and_source(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    ed_dir = tmp_path / "Edids"
    ed_dir.mkdir()

    # Use a real EDID sample so parse_edid yields real data — fall back
    # to a synthetic minimum-EDID if the workspace sample isn't there.
    real = config.BASE_DIR / "Edids" / "Node1-24-Server.txt"
    if real.is_file():
        # need to copy under the patched BASE_DIR so find_edid_file_matching
        # finds it under the patched path
        sample_path = ed_dir / "Node1-24-Server.txt"
        sample_path.write_text(
            (config.BASE_DIR.parent.parent / "Edids" / "Node1-24-Server.txt").read_text()
            if False else  # avoid the round-trip; just use the real bytes
            "",
        )
        # Simpler: just re-load the real file (BASE_DIR was patched, but we
        # need the original path).
        import nvmon.config as _real_config_mod
        # _real_config_mod.BASE_DIR is now the patched tmp_path — read the
        # raw original via Path resolution instead.
        original = Path(__file__).resolve().parents[2] / "Edids" / "Node1-24-Server.txt"
        sample_path.write_text(original.read_text(), encoding="ascii")
        blob = edid_mod.read_edid_file(sample_path)
    else:
        # Build a minimal-but-valid EDID 1.3 blob: header + zeros + checksum.
        blob = bytes([0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x00])
        blob += bytes(120)  # filler so total len == 128
        # Fix checksum so byte 127 makes the block sum mod 256 == 0.
        chk = (-sum(blob[:127])) & 0xFF
        blob = blob[:127] + bytes([chk])
        sample_path = ed_dir / "synthetic.txt"
        sample_path.write_bytes(blob)

    # Stub winreg-backed reads: pretend two displays carry this override.
    monkeypatch.setattr(
        edid_mod, "winreg", object(),  # truthy; we override the readers below
    )
    monkeypatch.setattr(
        edid_mod, "check_registry_edid",
        lambda: {"MON1\\INST_A": True, "MON1\\INST_B": True},
    )
    monkeypatch.setattr(
        edid_mod, "read_active_edid_overrides",
        lambda: {"MON1\\INST_A": blob, "MON1\\INST_B": blob},
    )

    snap = edid_mod.status()
    assert snap["available"] is True
    details = snap["active_details"]
    assert details is not None
    assert details["valid"] is True
    assert details["applied_to"] == ["MON1\\INST_A", "MON1\\INST_B"]
    assert details["distinct_blobs"] == 1
    # source_file should match the file we just wrote into Edids/.
    assert details["source_file"] is not None
    assert details["source_file"].startswith("Edids/")


def test_status_active_details_none_when_no_overrides(monkeypatch) -> None:
    monkeypatch.setattr(edid_mod, "winreg", object())
    monkeypatch.setattr(edid_mod, "check_registry_edid", lambda: {})
    monkeypatch.setattr(edid_mod, "read_active_edid_overrides", lambda: {})
    monkeypatch.setattr(edid_mod, "list_edid_files", lambda *a, **k: [])
    snap = edid_mod.status()
    assert snap["active_details"] is None


# ---------------------------------------------------------------------------
# find_edid_file_matching
# ---------------------------------------------------------------------------

def test_find_edid_file_matching_by_content(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    ed_dir = tmp_path / "Edids"
    ed_dir.mkdir()
    blob = bytes(range(64)) * 4
    (ed_dir / "match.txt").write_text(
        " ".join(f"{b:02x}" for b in blob), encoding="ascii",
    )
    (ed_dir / "other.txt").write_text(
        " ".join(f"{b:02x}" for b in bytes([0xaa] * 256)), encoding="ascii",
    )
    assert edid_mod.find_edid_file_matching(blob) == "Edids/match.txt"
    assert edid_mod.find_edid_file_matching(b"\x12\x34" * 64) is None


# ---------------------------------------------------------------------------
# apply_edid_to_all_displays — fake winreg
# ---------------------------------------------------------------------------

class _FakeWinreg:
    """Minimal winreg stand-in for apply_edid_to_all_displays.

    Models a registry tree of:
        SYSTEM\\...\\DISPLAY\\<m>\\<i>\\Device Parameters\\EDID_OVERRIDE
    Tracks writes so tests can assert exact behaviour.
    """
    HKEY_LOCAL_MACHINE = object()
    KEY_SET_VALUE = 0x2
    REG_BINARY = 3

    def __init__(self, layout: dict[str, dict[str, bytes | None]]):
        # layout: {"MON1\\INST_A": {"EDID_OVERRIDE": b"..."} or {} (no override)}
        self._layout = layout
        # Track writes per "<monitor>\\<instance>" -> bytes.
        self.writes: dict[str, bytes] = {}

    _ROOT = "SYSTEM\\CurrentControlSet\\Enum\\DISPLAY"

    def OpenKey(self, root, path, _reserved=0, _access=0):
        # Recognise the three depths we need: DISPLAY root, monitor key,
        # and Device Parameters leaf.
        if path == self._ROOT:
            return _DisplayRootHandle(self)
        if path.endswith("\\Device Parameters"):
            mid = path[len(self._ROOT) + 1:-len("\\Device Parameters")]
            return _ParamsHandle(self, mid)
        if path.startswith(self._ROOT + "\\"):
            tail = path[len(self._ROOT) + 1:]
            if "\\" not in tail:
                return _MonitorHandle(self, tail)
        raise OSError(f"unexpected path: {path!r}")

    def EnumKey(self, handle, idx):
        return handle.enum_key(idx)

    def QueryValueEx(self, handle, name):
        return handle.query_value(name)

    def SetValueEx(self, handle, name, _reserved, kind, value):
        handle.set_value(name, value)


class _DisplayRootHandle:
    def __init__(self, fr):
        self._fr = fr
        self._monitors = sorted({k.split("\\", 1)[0] for k in fr._layout})

    def enum_key(self, idx):
        if idx >= len(self._monitors):
            raise OSError("no more keys")
        return self._monitors[idx]

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _MonitorHandle:
    def __init__(self, fr, monitor):
        self._fr = fr
        self._instances = sorted(
            k.split("\\", 1)[1] for k in fr._layout if k.startswith(monitor + "\\")
        )

    def enum_key(self, idx):
        if idx >= len(self._instances):
            raise OSError("no more keys")
        return self._instances[idx]

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _ParamsHandle:
    def __init__(self, fr, mid):
        self._fr = fr
        self._mid = mid  # "<monitor>\\<instance>"

    def query_value(self, name):
        if name != "EDID_OVERRIDE":
            raise FileNotFoundError(name)
        existing = self._fr._layout.get(self._mid, {}).get("EDID_OVERRIDE")
        if existing is None:
            raise FileNotFoundError(name)
        # Pretend writes happened on this handle too (simulates re-read).
        if self._mid in self._fr.writes:
            return (self._fr.writes[self._mid], _FakeWinreg.REG_BINARY)
        return (existing, _FakeWinreg.REG_BINARY)

    def set_value(self, name, value):
        assert name == "EDID_OVERRIDE"
        self._fr.writes[self._mid] = bytes(value)

    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_apply_edid_to_all_displays_writes_when_bytes_differ(monkeypatch) -> None:
    fake = _FakeWinreg(layout={
        "MON1\\INST_A": {"EDID_OVERRIDE": b"\x00" * 256},
        "MON1\\INST_B": {"EDID_OVERRIDE": b"\xff" * 256},
        "MON2\\INST_C": {},  # no existing override
    })
    monkeypatch.setattr(edid_mod, "winreg", fake)

    target = b"\xab" * 256
    changed, total, keys = edid_mod.apply_edid_to_all_displays(target)
    assert total == 3
    assert changed == 3
    assert set(keys) == {"MON1\\INST_A", "MON1\\INST_B", "MON2\\INST_C"}
    assert all(v == target for v in fake.writes.values())


def test_apply_edid_to_all_displays_idempotent_when_bytes_match(monkeypatch) -> None:
    target = b"\xab" * 256
    fake = _FakeWinreg(layout={
        "MON1\\INST_A": {"EDID_OVERRIDE": target},
        "MON1\\INST_B": {"EDID_OVERRIDE": target},
    })
    monkeypatch.setattr(edid_mod, "winreg", fake)

    changed, total, keys = edid_mod.apply_edid_to_all_displays(target)
    assert total == 2
    assert changed == 0
    assert keys == []
    assert fake.writes == {}


def test_apply_edid_to_all_displays_dry_run_does_not_write(monkeypatch) -> None:
    fake = _FakeWinreg(layout={
        "MON1\\INST_A": {"EDID_OVERRIDE": b"\x00" * 256},
    })
    monkeypatch.setattr(edid_mod, "winreg", fake)

    changed, total, keys = edid_mod.apply_edid_to_all_displays(
        b"\xab" * 256, dry_run=True,
    )
    assert (changed, total) == (1, 1)
    assert keys == ["MON1\\INST_A"]
    assert fake.writes == {}  # dry run wrote nothing


def test_apply_edid_rejects_empty_blob() -> None:
    with pytest.raises(ValueError):
        edid_mod.apply_edid_to_all_displays(b"")


# ---------------------------------------------------------------------------
# Capture round-trip — save_current_state writes the EDID file
# ---------------------------------------------------------------------------

def _stub_subsystems(monkeypatch) -> None:
    """Quiet every subsystem so we can isolate the EDID path."""
    monkeypatch.setattr(profile_capture.mosaic_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.displays_mod, "status", lambda: None)
    monkeypatch.setattr(
        profile_capture.displays_mod, "capture_arrangement", lambda: [],
    )
    monkeypatch.setattr(profile_capture.qsync_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.qsync_mod, "is_available", lambda: False)
    monkeypatch.setattr(profile_capture.framelock_mod, "status", lambda: None)
    monkeypatch.setattr(profile_capture.gpu_mod, "status", lambda: [])
    monkeypatch.setattr(
        profile_capture.nvapi_mod, "capture_color_data_for_displays",
        lambda *_a, **_k: None,
    )


def test_save_current_state_writes_captured_edid_file(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(
        config, "_loaded_config",
        {"agent": {"profile_dir": str(tmp_path / "profiles")}},
    )
    _stub_subsystems(monkeypatch)
    blob = bytes(range(128)) + bytes(reversed(range(128)))
    monkeypatch.setattr(
        edid_mod, "read_active_edid_overrides",
        lambda: {"MON1\\INST_A": blob, "MON1\\INST_B": blob},
    )

    path = profile_capture.save_current_state("EDIDONLY")

    assert path.is_file()
    edid_path = tmp_path / "Edids" / "EDIDONLY.txt"
    assert edid_path.is_file()
    # Round-trip — the on-disk hex must decode back to the captured blob.
    assert edid_mod.read_edid_file(edid_path) == blob

    # And the saved profile must point at that file.
    saved = profiles.load("EDIDONLY")
    assert saved.edid.file == "Edids/EDIDONLY.txt"


def test_save_current_state_reuses_matching_existing_file(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(
        config, "_loaded_config",
        {"agent": {"profile_dir": str(tmp_path / "profiles")}},
    )
    _stub_subsystems(monkeypatch)

    # Operator dropped a hand-curated EDID file into Edids/ already.
    blob = bytes([0x55, 0xaa] * 128)
    ed_dir = tmp_path / "Edids"
    ed_dir.mkdir()
    existing = ed_dir / "Node5-24-Server.txt"
    existing.write_text(" ".join(f"{b:02x}" for b in blob), encoding="ascii")

    monkeypatch.setattr(
        edid_mod, "read_active_edid_overrides",
        lambda: {"MON1\\INST_A": blob},
    )

    profile_capture.save_current_state("CAPTURE_NAME")

    # Must NOT have created Edids/CAPTURE_NAME.txt — should reuse the
    # existing match instead.
    assert not (ed_dir / "CAPTURE_NAME.txt").exists()
    saved = profiles.load("CAPTURE_NAME")
    assert saved.edid.file == "Edids/Node5-24-Server.txt"


def test_save_current_state_no_overrides_leaves_edid_empty(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(
        config, "_loaded_config",
        {"agent": {"profile_dir": str(tmp_path / "profiles")}},
    )
    _stub_subsystems(monkeypatch)
    monkeypatch.setattr(edid_mod, "read_active_edid_overrides", lambda: {})

    profile_capture.save_current_state("NOEDID")
    saved = profiles.load("NOEDID")
    assert saved.edid.file == ""
    # Edids/ directory was not auto-created.
    assert not (tmp_path / "Edids" / "NOEDID.txt").exists()


# ---------------------------------------------------------------------------
# Apply pipeline — EDID-only profile is no longer dropped to restore
# ---------------------------------------------------------------------------

def test_edid_only_profile_is_not_nothing_to_apply(
    monkeypatch, tmp_path: Path,
) -> None:
    """An EDID-only profile (no Mosaic/DD/QSync) must still be applied."""
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(
        config, "_loaded_config",
        {"agent": {"profile_dir": str(tmp_path / "profiles")}},
    )
    monkeypatch.setattr(config, "is_admin", lambda: False)

    p = Profile()
    p.name = "edidonly"
    p.edid = ProfileEDID(file="Edids/edidonly.txt")
    profiles.save(p)

    # admin gate kicks in before any apply work — it should fail there,
    # NOT short-circuit to restore("nothing to apply").
    ok, msg = profiles._apply_inner("edidonly", dry_run=False, skip=set())
    assert not ok
    assert "Administrator" in msg or "admin" in msg
