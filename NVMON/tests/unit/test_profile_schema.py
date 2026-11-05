"""Unit tests for nvmon v2 profile schema + v1->v2 migration.

These tests exercise two things:

1. Every real v1 profile in the legacy tool nvdd profile dir parses cleanly
   through `load_profile_auto` and produces a structurally valid `ProfileV2`.
2. Round-tripping a v2 profile via `to_dict` -> `from_dict` is lossless
   (structural equivalence).

The v1 source profiles live at:
    <legacy-tool-dir>\\Nvidia_tools\\profiles\\*.json
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from nvmon.agent.profile_migrate import (
    detect_version,
    load_profile_auto,
    upgrade_v1_to_v2,
)
from nvmon.agent.profile_schema import (
    APPLY_ORDER,
    SECTION_TO_CAPABILITY,
    DirectDisplaySection,
    EDIDSection,
    GPUSection,
    MosaicGrid,
    MosaicOutputV2,
    MosaicSection,
    ProfileSections,
    ProfileV2,
    QSyncSection,
    ValidationCheckSpec,
    ValidationSection,
    WindowsArrangementSection,
    WindowsDisplaySnapshot,
    applicable_sections,
    load_profile,
    save_profile,
)


V1_PROFILE_DIR = Path(r"<legacy-tool-dir>\Nvidia_tools\profiles")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_v1_profiles() -> list[Path]:
    """Find all v1 profile JSONs in the legacy tool dir. Skip if dir missing."""
    if not V1_PROFILE_DIR.is_dir():
        return []
    return sorted(V1_PROFILE_DIR.glob("*.json"))


V1_PROFILES = _discover_v1_profiles()


# ---------------------------------------------------------------------------
# Fixtures — synthetic v2 profiles for roundtrip tests
# ---------------------------------------------------------------------------

def _full_v2_profile() -> ProfileV2:
    """Construct a ProfileV2 with every section populated, for roundtrip."""
    return ProfileV2(
        version=2,
        name="full-synthetic",
        description="Every section present — roundtrip smoke.",
        tags=["test", "synthetic"],
        created_at="2026-04-17T00:00:00Z",
        created_by="nvmon-test 0.1.0",
        source_host="TEST-HOST",
        sections=ProfileSections(
            gpu=GPUSection(
                temp_warning_c=85,
                power_warning_pct=95,
                expected_gpu_count=2,
                per_gpu_power_limit=[260.0, 260.0],
                expected_driver_version="551.23",
                min_driver_version="546.00",
            ),
            edid=EDIDSection(
                file_path="NoMosaic_2397.txt",
                sha256="deadbeef" * 8,
                apply_to_displays=["0x82061085", "0x82061087"],
                force_redetect=True,
            ),
            direct_display=DirectDisplaySection(
                expected_count=4,
                display_ids=["0x82061085", "0x82061087", "0x82061081", "0x82061083"],
                exclude_prefixes=["DDX-"],
            ),
            mosaic=MosaicSection(
                grids=[
                    MosaicGrid(
                        label="main",
                        rows=2, cols=2,
                        per_display_resolution=(3840, 2160),
                        total_resolution=(7680, 4320),
                        refresh_rate=23.977,
                        outputs=[
                            MosaicOutputV2(gpu=1, display=0, display_id="0x82061085"),
                            MosaicOutputV2(gpu=1, display=1, display_id="0x82061087"),
                            MosaicOutputV2(gpu=1, display=2, display_id="0x82061081"),
                            MosaicOutputV2(gpu=1, display=3, display_id="0x82061083"),
                        ],
                    ),
                    MosaicGrid(
                        label="ddx-backup",
                        rows=1, cols=1,
                        per_display_resolution=(1920, 1080),
                        total_resolution=(1920, 1080),
                        refresh_rate=60.0,
                        outputs=[
                            MosaicOutputV2(gpu=0, display=0, display_id="0x80061082"),
                        ],
                    ),
                ],
                mosaic_command="configureMosaic.exe set rows=2 cols=2 ...",
            ),
            qsync=QSyncSection(
                source="HOUSESYNC",
                trigger="FALLING",
                grid=1,
                expected_sync_rate=23.977,
            ),
            windows_arrangement=WindowsArrangementSection(
                displays=[
                    WindowsDisplaySnapshot(
                        device_name="\\\\.\\DISPLAY5",
                        position=(0, 0),
                        resolution=(1920, 1080),
                        refresh_rate=60,
                        primary=True,
                    ),
                ],
            ),
            validation=ValidationSection(
                checks=[
                    ValidationCheckSpec(
                        name="refresh_rate",
                        type="within_tolerance",
                        expected=23.977,
                        tolerance=0.002,
                    ),
                    ValidationCheckSpec(
                        name="qsync_locked",
                        type="equals",
                        expected=True,
                    ),
                ],
            ),
        ),
    )


# ---------------------------------------------------------------------------
# v1 -> v2 upgrade tests (parametrized over every real v1 profile)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not V1_PROFILES, reason="the legacy tool v1 profile dir not present")
@pytest.mark.parametrize("v1_path", V1_PROFILES, ids=[p.stem for p in V1_PROFILES])
def test_v1_profile_loads_and_upgrades(v1_path: Path) -> None:
    """Every real v1 profile must parse -> upgrade -> be a valid ProfileV2."""
    profile = load_profile_auto(v1_path)

    assert isinstance(profile, ProfileV2)
    assert profile.version == 2
    assert profile.name == json.loads(v1_path.read_text(encoding="utf-8"))["name"]
    # At minimum the section container exists.
    assert isinstance(profile.sections, ProfileSections)


@pytest.mark.skipif(not V1_PROFILES, reason="the legacy tool v1 profile dir not present")
@pytest.mark.parametrize("v1_path", V1_PROFILES, ids=[p.stem for p in V1_PROFILES])
def test_v1_upgrade_respects_enabled_false(v1_path: Path) -> None:
    """`enabled: false` in v1 -> section OMITTED in v2."""
    raw = json.loads(v1_path.read_text(encoding="utf-8"))
    profile = load_profile_auto(v1_path)

    # Mosaic: enabled=false -> section is None UNLESS ddx_grid has outputs
    # (ddx_grid migrates into the mosaic section as a second grid).
    v1_mosaic_enabled = bool(raw.get("mosaic", {}).get("enabled", False))
    v1_ddx_outputs = bool((raw.get("ddx_grid", {}) or {}).get("outputs") or [])
    if not v1_mosaic_enabled and not v1_ddx_outputs:
        assert profile.sections.mosaic is None, (
            f"{v1_path.stem}: mosaic should be omitted when disabled and no ddx_grid"
        )

    # DirectDisplay: enabled=false -> section is None.
    if not raw.get("direct_display", {}).get("enabled", False):
        assert profile.sections.direct_display is None, (
            f"{v1_path.stem}: direct_display should be omitted when disabled"
        )

    # QSync: enabled=false -> section is None.
    if not raw.get("qsync", {}).get("enabled", False):
        assert profile.sections.qsync is None, (
            f"{v1_path.stem}: qsync should be omitted when disabled"
        )

    # EDID: empty file -> section is None.
    if not (raw.get("edid", {}) or {}).get("file", ""):
        assert profile.sections.edid is None, (
            f"{v1_path.stem}: edid should be omitted when file is empty"
        )


@pytest.mark.skipif(not V1_PROFILES, reason="the legacy tool v1 profile dir not present")
@pytest.mark.parametrize("v1_path", V1_PROFILES, ids=[p.stem for p in V1_PROFILES])
def test_v1_roundtrip_structural_equivalence(v1_path: Path) -> None:
    """v1 -> upgrade -> to_dict -> from_dict -> to_dict is stable."""
    profile = load_profile_auto(v1_path)

    d1 = profile.to_dict()
    profile2 = ProfileV2.from_dict(d1)
    d2 = profile2.to_dict()

    assert d1 == d2, (
        f"{v1_path.stem}: to_dict/from_dict roundtrip not structurally equal"
    )


# ---------------------------------------------------------------------------
# Specific upgrade-rule tests (targeted at known v1 profiles)
# ---------------------------------------------------------------------------

def test_mosaic_2397_upgrades_with_both_grids() -> None:
    """mosaic-2397.json has enabled mosaic + ddx_grid outputs -> 2 grids."""
    path = V1_PROFILE_DIR / "mosaic-2397.json"
    if not path.is_file():
        pytest.skip("mosaic-2397.json not present")

    p = load_profile_auto(path)
    assert p.sections.mosaic is not None
    assert len(p.sections.mosaic.grids) == 2
    assert p.sections.mosaic.grids[0].label == "main"
    assert p.sections.mosaic.grids[0].rows == 2
    assert p.sections.mosaic.grids[0].cols == 2
    assert p.sections.mosaic.grids[0].refresh_rate == pytest.approx(23.977)
    assert len(p.sections.mosaic.grids[0].outputs) == 4
    assert p.sections.mosaic.grids[1].label == "ddx-backup"
    assert p.sections.mosaic.grids[1].rows == 1
    assert p.sections.mosaic.grids[1].cols == 1
    assert p.sections.mosaic.mosaic_command.startswith("configureMosaic.exe set")

    # direct_display enabled + display_ids.mosaic_grid is present -> section populated.
    assert p.sections.direct_display is not None
    assert p.sections.direct_display.expected_count == 4
    assert len(p.sections.direct_display.display_ids) == 4

    # QSync enabled -> present.
    assert p.sections.qsync is not None
    assert p.sections.qsync.source == "HOUSESYNC"
    assert p.sections.qsync.grid == 1


def test_desktop_upgrades_to_empty_sections_except_ddx_and_windows() -> None:
    """desktop.json has everything disabled and empty EDID -> minimal v2."""
    path = V1_PROFILE_DIR / "desktop.json"
    if not path.is_file():
        pytest.skip("desktop.json not present")

    p = load_profile_auto(path)
    # All capability sections except mosaic (ddx_grid has outputs!) are omitted.
    assert p.sections.direct_display is None
    assert p.sections.qsync is None
    assert p.sections.edid is None
    # Mosaic IS present because ddx_grid.outputs is non-empty — it becomes a
    # single "ddx-backup" grid.
    assert p.sections.mosaic is not None
    assert len(p.sections.mosaic.grids) == 1
    assert p.sections.mosaic.grids[0].label == "ddx-backup"
    # windows_arrangement is populated.
    assert p.sections.windows_arrangement is not None
    assert len(p.sections.windows_arrangement.displays) >= 1


def test_no_mosaic_2397_preserves_qsync_and_edid_only() -> None:
    """no-mosaic-2397.json: QSync on, EDID set, mosaic disabled."""
    path = V1_PROFILE_DIR / "no-mosaic-2397.json"
    if not path.is_file():
        pytest.skip("no-mosaic-2397.json not present")

    p = load_profile_auto(path)
    assert p.sections.qsync is not None
    assert p.sections.edid is not None
    assert p.sections.edid.file_path == "NoMosaic_2397.txt"
    # ddx_grid has outputs in the v1 file -> mosaic section carries it.
    assert p.sections.mosaic is not None
    assert any(g.label == "ddx-backup" for g in p.sections.mosaic.grids)
    # mosaic enabled=false -> no "main" grid.
    assert not any(g.label == "main" for g in p.sections.mosaic.grids)
    assert p.sections.direct_display is None


# ---------------------------------------------------------------------------
# v2 roundtrip (synthetic full profile)
# ---------------------------------------------------------------------------

def test_v2_full_profile_roundtrip() -> None:
    """A fully-populated v2 profile roundtrips through to_dict/from_dict."""
    p = _full_v2_profile()
    d1 = p.to_dict()
    p2 = ProfileV2.from_dict(copy.deepcopy(d1))
    d2 = p2.to_dict()
    assert d1 == d2


def test_v2_empty_sections_roundtrip() -> None:
    """A profile with zero sections is still legal and stable."""
    p = ProfileV2(name="bare", description="nothing to apply")
    d1 = p.to_dict()
    p2 = ProfileV2.from_dict(d1)
    assert p2.name == "bare"
    assert p2.sections.present() == []
    assert d1 == p2.to_dict()


def test_sections_present_order_matches_apply_order() -> None:
    """`present()` must return section names in canonical APPLY_ORDER."""
    p = _full_v2_profile()
    # Every section is set, so present() should equal APPLY_ORDER exactly.
    assert p.sections.present() == list(APPLY_ORDER)


# ---------------------------------------------------------------------------
# Capability-aware applicability
# ---------------------------------------------------------------------------

def test_applicable_sections_all_available() -> None:
    """When every capability is available, every present section is applicable."""
    p = _full_v2_profile()
    caps = {
        "gpu": {"available": True},
        "edid": {"available": True},
        "displays": {"available": True},
        "mosaic": {"available": True},
        "qsync": {"available": True},
    }
    assert applicable_sections(p, caps) == list(APPLY_ORDER)


def test_applicable_sections_drops_unavailable() -> None:
    """Sections whose capability is unavailable are dropped."""
    p = _full_v2_profile()
    caps = {
        "gpu": {"available": True},
        "edid": {"available": False, "reason": "registry write disabled"},
        "displays": {"available": True},
        "mosaic": {"available": False, "reason": "configureMosaic.exe not found"},
        "qsync": {"available": False, "reason": "configureQsync2.exe not found"},
    }
    applicable = applicable_sections(p, caps)
    assert "gpu" in applicable
    assert "direct_display" in applicable
    assert "windows_arrangement" in applicable  # no capability gate
    assert "validation" in applicable           # no capability gate
    assert "edid" not in applicable
    assert "mosaic" not in applicable
    assert "qsync" not in applicable


def test_applicable_sections_ignores_missing_profile_sections() -> None:
    """Only sections actually on the profile can appear in the result."""
    p = ProfileV2(
        name="gpu-only",
        sections=ProfileSections(
            gpu=GPUSection(expected_gpu_count=2),
        ),
    )
    caps = {
        "gpu": {"available": True},
        "edid": {"available": True},
        "displays": {"available": True},
        "mosaic": {"available": True},
        "qsync": {"available": True},
    }
    assert applicable_sections(p, caps) == ["gpu"]


def test_applicable_sections_empty_capabilities_dict() -> None:
    """An empty caps dict -> only capability-free sections apply."""
    p = _full_v2_profile()
    applicable = applicable_sections(p, {})
    assert applicable == ["windows_arrangement", "validation"]


# ---------------------------------------------------------------------------
# save_profile — subset support
# ---------------------------------------------------------------------------

def test_save_profile_writes_full(tmp_path: Path) -> None:
    p = _full_v2_profile()
    out = tmp_path / "full.json"
    save_profile(p, out)
    assert out.is_file()
    reloaded = load_profile(out)
    assert reloaded.to_dict() == p.to_dict()


def test_save_profile_subset(tmp_path: Path) -> None:
    """save_profile(sections=[...]) writes only the requested sections."""
    p = _full_v2_profile()
    out = tmp_path / "subset.json"
    save_profile(p, out, sections=["gpu", "qsync"])

    raw = json.loads(out.read_text(encoding="utf-8"))
    assert set(raw["sections"].keys()) == {"gpu", "qsync"}


def test_save_profile_subset_all_missing_raises(tmp_path: Path) -> None:
    """Requesting only sections the profile doesn't carry is an error."""
    p = ProfileV2(name="gpu-only", sections=ProfileSections(gpu=GPUSection()))
    out = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        save_profile(p, out, sections=["mosaic", "qsync"])


# ---------------------------------------------------------------------------
# detect_version + round-trip with a fresh v2 file on disk
# ---------------------------------------------------------------------------

def test_detect_version_v1() -> None:
    assert detect_version({"name": "old"}) == 1
    assert detect_version({"name": "old", "version": 1}) == 1


def test_detect_version_v2() -> None:
    assert detect_version({"version": 2, "name": "new"}) == 2
    # Legacy alias `schema_version`.
    assert detect_version({"schema_version": 2, "name": "new"}) == 2


def test_saved_v2_reloads_as_v2(tmp_path: Path) -> None:
    p = _full_v2_profile()
    out = tmp_path / "rt.json"
    save_profile(p, out)
    reloaded = load_profile_auto(out)
    assert reloaded.version == 2
    assert reloaded.to_dict() == p.to_dict()


# ---------------------------------------------------------------------------
# SECTION_TO_CAPABILITY sanity
# ---------------------------------------------------------------------------

def test_section_to_capability_covers_apply_order() -> None:
    """Every section in APPLY_ORDER has an entry in SECTION_TO_CAPABILITY."""
    for name in APPLY_ORDER:
        assert name in SECTION_TO_CAPABILITY
