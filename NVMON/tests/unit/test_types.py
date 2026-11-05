"""Tests for nvmon.types dataclasses.

Verifies construction, defaults, and roundtrip (dataclass -> dict -> dataclass)
behavior. Doesn't exercise the enum values beyond confirming they exist.
"""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass

import pytest

from nvmon import types as T


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

def test_dd_status_enum():
    assert T.DDStatus.REMOVED_MANAGED.value == "Removed Managed"
    assert T.DDStatus.ON_DESKTOP.value == "On Desktop"
    assert T.DDStatus.UNKNOWN.value == "Unknown"


def test_qsync_source_enum():
    assert T.QSyncSource.HOUSESYNC.value == "HOUSESYNC"
    assert T.QSyncSource.INTERNAL.value == "INTERNAL"
    assert T.QSyncSource.NONE.value == "NONE"


def test_qsync_trigger_enum():
    assert T.QSyncTrigger.FALLING.value == "FALLING"
    assert T.QSyncTrigger.RISING.value == "RISING"


def test_alert_severity_enum():
    assert T.AlertSeverity.CRITICAL.value == "CRITICAL"
    assert T.AlertSeverity.WARNING.value == "WARNING"
    assert T.AlertSeverity.INFO.value == "INFO"


# ---------------------------------------------------------------------------
# Defaults - every dataclass should be constructible with no args
# ---------------------------------------------------------------------------

ALL_DATACLASSES = [
    T.DisplayInfo,
    T.DDState,
    T.MosaicOutput,
    T.MosaicState,
    T.GPUSyncInfo,
    T.QSyncState,
    T.GPUInfo,
    T.ProfileMosaic,
    T.ProfileDD,
    T.ProfileQSync,
    T.ProfileEDID,
    T.ProfileValidation,
    T.Profile,
    T.Alert,
    T.OutputHealth,
    T.ValidationCheck,
    T.ValidationResult,
    T.SubsystemResult,
    T.CommandResult,
]


@pytest.mark.parametrize("cls", ALL_DATACLASSES)
def test_dataclass_is_dataclass(cls):
    assert is_dataclass(cls)


@pytest.mark.parametrize("cls", ALL_DATACLASSES)
def test_dataclass_default_construct(cls):
    inst = cls()
    assert inst is not None
    assert is_dataclass(inst)


# ---------------------------------------------------------------------------
# Spot-check specific default values
# ---------------------------------------------------------------------------

def test_display_info_defaults():
    d = T.DisplayInfo()
    assert d.id == ""
    assert d.name == ""
    assert d.resolution == (0, 0)
    assert d.gpu_index == -1
    assert d.display_index == -1


def test_mosaic_state_defaults():
    m = T.MosaicState()
    assert m.enabled is False
    assert m.rows == 0
    assert m.cols == 0
    assert m.outputs == []


def test_qsync_state_defaults():
    q = T.QSyncState()
    assert q.available is False
    assert q.synced is False
    assert q.per_gpu_sync == []


def test_gpu_info_defaults():
    g = T.GPUInfo()
    assert g.index == 0
    assert g.temperature_c == 0
    assert g.memory_total_mb == 0


def test_profile_defaults():
    p = T.Profile()
    assert p.name == ""
    assert isinstance(p.mosaic, T.ProfileMosaic)
    assert isinstance(p.direct_display, T.ProfileDD)
    assert isinstance(p.qsync, T.ProfileQSync)
    assert isinstance(p.edid, T.ProfileEDID)
    assert isinstance(p.validation, T.ProfileValidation)
    # ProfileMosaic defaults
    assert p.mosaic.rows == 2
    assert p.mosaic.cols == 2
    assert p.mosaic.width == 3840
    # ProfileValidation defaults — 0.0 means "no expectation, validator
    # skips the check". The previous default of 23.976 was a legacy-tool-era
    # leak that produced spurious validation failures on rigs running at
    # 24/25/30/50/60. profile_capture writes the live rate over this.
    assert p.validation.expected_refresh_rate == pytest.approx(0.0)
    assert p.validation.refresh_rate_tolerance == pytest.approx(0.01)
    assert p.validation.expected_qsync_locked is False
    # skip_apply defaults — False on every section, so legacy profiles
    # behave exactly as before. Only operators who explicitly set the flag
    # in a per-node profile JSON get the "leave this subsystem alone"
    # behaviour. NODE-11 (1080p in a 4K fleet) is the canonical use case.
    assert p.mosaic.skip_apply is False
    assert p.direct_display.skip_apply is False
    assert p.qsync.skip_apply is False


# ---------------------------------------------------------------------------
# Roundtrip (dataclass -> dict -> reconstructed dataclass)
# ---------------------------------------------------------------------------

def test_display_info_roundtrip():
    original = T.DisplayInfo(
        id="82061085",
        name="HELIOS-DP",
        status="Removed Managed",
        refresh_rate=23.976,
        refresh_fraction="23976/1000",
        resolution=(3840, 2160),
        connector="DisplayPort",
        gpu_index=1,
        display_index=0,
        os_target_id="61400",
    )
    d = asdict(original)
    # asdict converts tuples to lists - turn back to tuple for equality.
    d["resolution"] = tuple(d["resolution"])
    rebuilt = T.DisplayInfo(**d)
    assert rebuilt == original


def test_mosaic_output_roundtrip():
    original = T.MosaicOutput(gpu=1, display=0, display_id="0x82061085")
    rebuilt = T.MosaicOutput(**asdict(original))
    assert rebuilt == original


def test_gpu_info_roundtrip():
    original = T.GPUInfo(
        index=0,
        name="NVIDIA RTX A6000",
        pci_bus_id="00000000:41:00.0",
        temperature_c=69,
        power_draw_w=92.0,
        memory_total_mb=49140,
        pstate="P0",
        display_active=True,
    )
    rebuilt = T.GPUInfo(**asdict(original))
    assert rebuilt == original


def test_qsync_state_roundtrip_preserves_per_gpu():
    g0 = T.GPUSyncInfo(gpu=0, synced=True, signal_available=True,
                      effective_synced=True, sync_note="via HOUSESYNC")
    g1 = T.GPUSyncInfo(gpu=1, synced=False, signal_available=False,
                      effective_synced=True, sync_note="via GPU 0")
    original = T.QSyncState(
        available=True,
        synced=True,
        house_sync_detected=True,
        house_sync_incoming_hz=23.976,
        source="HOUSESYNC",
        trigger="FALLING",
        per_gpu_sync=[g0, g1],
    )
    d = asdict(original)
    # Rebuild per_gpu_sync as GPUSyncInfo instances.
    d["per_gpu_sync"] = [T.GPUSyncInfo(**x) for x in d["per_gpu_sync"]]
    rebuilt = T.QSyncState(**d)
    assert rebuilt == original


def test_profile_roundtrip_preserves_nested_types():
    """A nested Profile rebuild requires wrapping sub-dicts into their dataclasses."""
    mosaic_out = T.MosaicOutput(gpu=1, display=0, display_id="0x82061085")
    original = T.Profile(
        name="test-profile",
        description="A roundtrip test",
        mosaic=T.ProfileMosaic(
            enabled=True, rows=2, cols=2, width=3840, height=2160,
            refresh_rate="23.98", outputs=[mosaic_out],
        ),
        direct_display=T.ProfileDD(enabled=True, mode="index", index=2),
        qsync=T.ProfileQSync(enabled=True, source="HOUSESYNC",
                             trigger="FALLING", grid=1),
        edid=T.ProfileEDID(file="main.bin"),
        validation=T.ProfileValidation(
            expected_refresh_rate=23.976,
            refresh_rate_tolerance=0.002,
            expected_qsync_locked=True,
            expected_house_sync_freq=23.976,
        ),
    )

    d = asdict(original)

    rebuilt = T.Profile(
        name=d["name"],
        description=d["description"],
        mosaic=T.ProfileMosaic(
            **{k: v for k, v in d["mosaic"].items() if k != "outputs"},
            outputs=[T.MosaicOutput(**o) for o in d["mosaic"]["outputs"]],
        ),
        direct_display=T.ProfileDD(**d["direct_display"]),
        qsync=T.ProfileQSync(**d["qsync"]),
        edid=T.ProfileEDID(**d["edid"]),
        validation=T.ProfileValidation(**d["validation"]),
        display_ids=d["display_ids"],
        windows_arrangement=d["windows_arrangement"],
        mosaic_command=d["mosaic_command"],
    )

    assert rebuilt == original


def test_validation_result_roundtrip_with_checks():
    original = T.ValidationResult(
        all_passed=True,
        checks={
            "dd_active": T.ValidationCheck(passed=True, detail="5 displays in DD"),
            "refresh_rate": T.ValidationCheck(passed=False, detail="off by 0.01Hz"),
        },
    )
    # asdict expands nested dataclass values inside dicts too.
    d = asdict(original)
    rebuilt = T.ValidationResult(
        all_passed=d["all_passed"],
        checks={k: T.ValidationCheck(**v) for k, v in d["checks"].items()},
    )
    assert rebuilt == original


def test_command_result_roundtrip():
    original = T.CommandResult(
        success=True, return_code=0,
        stdout="ok", stderr="",
        command="configureMosaic.exe query",
        duration_ms=123,
    )
    rebuilt = T.CommandResult(**asdict(original))
    assert rebuilt == original


def test_subsystem_result_roundtrip():
    original = T.SubsystemResult(
        success=False,
        subsystem="mosaic",
        operation="apply",
        message="failed to apply",
        details={"rc": 1},
    )
    rebuilt = T.SubsystemResult(**asdict(original))
    assert rebuilt == original


# ---------------------------------------------------------------------------
# Field sanity
# ---------------------------------------------------------------------------

def test_no_magicbox_fields_in_profile():
    """Per spec: backup_status / peer fields removed."""
    field_names = {f.name for f in fields(T.Profile)}
    assert "backup_status" not in field_names
    assert "peer" not in field_names
    assert "peer_server" not in field_names


def test_no_magicbox_fields_in_ddstate():
    field_names = {f.name for f in fields(T.DDState)}
    assert "backup_status" not in field_names
