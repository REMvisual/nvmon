"""nvmon profile migration — v1 (nvdd) -> v2 (nvmon modular) upgrade.

The v1 profile shape (from `<legacy-tool-dir>\\Nvidia_tools\\nvdd`) has
every subsystem block mandatory, with `enabled: false` as the sentinel for
"this box doesn't do that". v2 flips that model: absence == "not applicable".

Key migration semantics (per spec 2026-04-17-B):

- `enabled: false` in v1 -> section OMITTED in v2 (not carried with enabled=False).
  This is the core semantic shift — and it means v1 -> v2 -> v1 is LOSSY for
  disabled blocks, which is fine because v2 doesn't carry a v1 writer.
- `edid.file == ""` -> `sections.edid` omitted.
- v1 `mosaic` (if enabled) -> `sections.mosaic.grids[0]` with label "main".
- v1 `ddx_grid` (if its `outputs` is non-empty) -> appended as `grids[1]` with
  label "ddx-backup". Note: ddx_grid in v1 has no `enabled` flag of its own,
  so presence of outputs is the only signal.
- v1 `windows_arrangement` entries use `{"device", "x", "y", "width", "height",
  "primary"}` — we rewrite to v2 shape `{device_name, position, resolution,
  primary}` and default refresh_rate to 0 (v1 didn't capture it).
- v1 `display_ids` (e.g. ddx + mosaic_grid NVAPI IDs) is DROPPED — the same
  data lives inside grid outputs' `display_id` field.
- v1 `mosaic_command` -> `sections.mosaic.mosaic_command` (only when mosaic
  section is present; drop if mosaic disabled).
- v1 `validation` (flat shape: expected_refresh_rate / qsync_locked / house_sync)
  -> reshaped into v2 `ValidationCheckSpec` list. Preserved as best-effort.

Loading contract:

    load_profile_auto(path) -> ProfileV2
        - Detects v1 vs v2 from `version` / `schema_version` field.
        - v1: upgrades in memory, returns v2 dataclass. Does NOT touch disk.
        - v2: parses directly.

    upgrade_profile_file(path, backup=True) -> Path
        - Rewrites the file as v2 on disk (explicit operator action).
        - Creates `<path>.v1.bak` alongside if `backup=True`.
        - Idempotent — running on a v2 file is a no-op.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .profile_schema import (
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
)


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def detect_version(data: dict[str, Any]) -> int:
    """Return 1 or 2 based on the shape of a loaded profile dict.

    v2 has an explicit `version: 2` (or legacy `schema_version: 2`).
    Everything else is v1.
    """
    v = data.get("version")
    if v is None:
        v = data.get("schema_version")
    if v == 2:
        return 2
    # Unknown non-2 versions fall back to v1 handling — the v1 upgrader is
    # tolerant of missing fields and will produce something sane.
    return 1


# ---------------------------------------------------------------------------
# v1 -> v2 upgrade
# ---------------------------------------------------------------------------

def upgrade_v1_to_v2(v1_dict: dict[str, Any]) -> ProfileV2:
    """Convert a v1 profile dict (parsed JSON) into a ProfileV2 dataclass.

    Tolerant of missing fields. Disabled blocks are OMITTED (not carried
    with enabled=False).
    """
    name = str(v1_dict.get("name", "") or "")
    description = str(v1_dict.get("description", "") or "")

    sections = ProfileSections(
        gpu=None,  # v1 had no gpu block; nothing to upgrade
        edid=_upgrade_edid(v1_dict.get("edid", {}) or {}),
        direct_display=_upgrade_direct_display(
            v1_dict.get("direct_display", {}) or {},
            v1_dict.get("display_ids", {}) or {},
        ),
        mosaic=_upgrade_mosaic(
            v1_dict.get("mosaic", {}) or {},
            v1_dict.get("ddx_grid", {}) or {},
            v1_dict.get("mosaic_command", "") or "",
        ),
        qsync=_upgrade_qsync(v1_dict.get("qsync", {}) or {}),
        windows_arrangement=_upgrade_windows_arrangement(
            v1_dict.get("windows_arrangement", []) or []
        ),
        validation=_upgrade_validation(v1_dict.get("validation", {}) or {}),
    )

    return ProfileV2(
        version=2,
        name=name,
        description=description,
        sections=sections,
    )


def _upgrade_edid(v1_edid: dict[str, Any]) -> EDIDSection | None:
    file_path = str(v1_edid.get("file", "") or "")
    if not file_path:
        # v1 sentinel for "no override" is empty string. Omit in v2.
        return None
    return EDIDSection(
        file_path=file_path,
        sha256="",                 # v1 didn't capture hashes
        apply_to_displays=None,    # None == "all", matching v1 behavior
        force_redetect=False,
    )


def _upgrade_direct_display(
    v1_dd: dict[str, Any],
    v1_display_ids: dict[str, Any],
) -> DirectDisplaySection | None:
    if not v1_dd.get("enabled", False):
        return None
    # v1 `display_ids.mosaic_grid` lists the NVAPI IDs of the DD-bound
    # displays. Copy them if present — otherwise leave empty (DD can use
    # adapter-index defaults).
    mosaic_ids = v1_display_ids.get("mosaic_grid", []) or []
    return DirectDisplaySection(
        expected_count=len(mosaic_ids),
        display_ids=[str(x) for x in mosaic_ids],
        exclude_prefixes=[],
    )


def _upgrade_mosaic(
    v1_mosaic: dict[str, Any],
    v1_ddx_grid: dict[str, Any],
    v1_mosaic_command: str,
) -> MosaicSection | None:
    grids: list[MosaicGrid] = []

    # Main mosaic grid — only when enabled.
    if v1_mosaic.get("enabled", False):
        grids.append(_v1_grid_to_v2(
            v1_mosaic,
            label="main",
        ))

    # DDX backup grid — only when it carries outputs (no `enabled` flag in v1).
    ddx_outputs = v1_ddx_grid.get("outputs", []) or []
    if ddx_outputs:
        grids.append(_v1_grid_to_v2(
            v1_ddx_grid,
            label="ddx-backup",
        ))

    if not grids:
        return None

    return MosaicSection(
        grids=grids,
        mosaic_command=str(v1_mosaic_command or ""),
    )


def _v1_grid_to_v2(v1_grid: dict[str, Any], *, label: str) -> MosaicGrid:
    rows = int(v1_grid.get("rows", 0) or 0)
    cols = int(v1_grid.get("cols", 0) or 0)
    width = int(v1_grid.get("width", 0) or 0)
    height = int(v1_grid.get("height", 0) or 0)
    refresh = _coerce_refresh_rate(v1_grid.get("refresh_rate", 0))

    outputs_in = v1_grid.get("outputs", []) or []
    outputs: list[MosaicOutputV2] = []
    for o in outputs_in:
        outputs.append(MosaicOutputV2(
            gpu=int(o.get("gpu", 0) or 0),
            display=int(o.get("display", 0) or 0),
            display_id=str(o.get("display_id", "") or ""),
        ))

    # v1 stored only per-display resolution (width/height). Total is
    # derived — a reasonable approximation for migration.
    total_w = width * cols if cols > 0 else width
    total_h = height * rows if rows > 0 else height

    return MosaicGrid(
        label=label,
        rows=rows,
        cols=cols,
        per_display_resolution=(width, height),
        total_resolution=(total_w, total_h),
        refresh_rate=refresh,
        outputs=outputs,
    )


def _coerce_refresh_rate(v: Any) -> float:
    """v1 stored refresh_rate as a string (e.g. "23.977"). Normalize to float."""
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _upgrade_qsync(v1_qsync: dict[str, Any]) -> QSyncSection | None:
    if not v1_qsync.get("enabled", False):
        return None
    # v1 grid is 1-based (configureQsync2 CLI convention). v2 dataclass
    # documents it as 0-based index into MosaicSection.grids, but for
    # compatibility with the existing apply pipeline we preserve the raw
    # integer as-is — the apply layer decides indexing semantics. This keeps
    # the migration faithful to the v1 value.
    #
    # role / displays default to server / [] for v1 profiles which had no
    # concept of either. QSyncSection.from_dict applies VSYNC -> HOUSESYNC
    # normalization; we route through it so the migrator inherits the same
    # coercion logic instead of duplicating it.
    return QSyncSection.from_dict({
        "source": v1_qsync.get("source", "HOUSESYNC"),
        "trigger": v1_qsync.get("trigger", "FALLING"),
        "grid": int(v1_qsync.get("grid", 1) or 1),
        "expected_sync_rate": 0.0,
        "role": v1_qsync.get("role", "server"),
        "displays": v1_qsync.get("displays", []) or [],
    })


def _upgrade_windows_arrangement(
    v1_arrangement: list[dict[str, Any]],
) -> WindowsArrangementSection | None:
    if not v1_arrangement:
        return None
    displays: list[WindowsDisplaySnapshot] = []
    for entry in v1_arrangement:
        if not isinstance(entry, dict):
            continue
        displays.append(WindowsDisplaySnapshot(
            device_name=str(entry.get("device", "") or ""),
            position=(int(entry.get("x", 0) or 0), int(entry.get("y", 0) or 0)),
            resolution=(int(entry.get("width", 0) or 0), int(entry.get("height", 0) or 0)),
            refresh_rate=int(entry.get("refresh_rate", 0) or 0),  # v1 rarely captured this
            primary=bool(entry.get("primary", False)),
        ))
    if not displays:
        return None
    return WindowsArrangementSection(displays=displays)


def _upgrade_validation(v1_validation: dict[str, Any]) -> ValidationSection | None:
    """Reshape v1's flat validation block into a list of named checks.

    v1 shape (all four keys always present):
        {
          "expected_refresh_rate": 23.977,
          "refresh_rate_tolerance": 0.002,
          "expected_qsync_locked": true,
          "expected_house_sync_freq": 23.977
        }

    A profile with all zeros/false is effectively "no validation" — we still
    translate it faithfully so operators can see what the source file said.
    Callers that want to prune empty validation blocks can do so at save time.
    """
    if not v1_validation:
        return None

    checks: list[ValidationCheckSpec] = []

    if "expected_refresh_rate" in v1_validation:
        checks.append(ValidationCheckSpec(
            name="refresh_rate",
            type="within_tolerance",
            expected=float(v1_validation.get("expected_refresh_rate", 0.0) or 0.0),
            tolerance=float(v1_validation.get("refresh_rate_tolerance", 0.0) or 0.0),
        ))

    if "expected_qsync_locked" in v1_validation:
        checks.append(ValidationCheckSpec(
            name="qsync_locked",
            type="equals",
            expected=bool(v1_validation.get("expected_qsync_locked", False)),
            tolerance=None,
        ))

    if "expected_house_sync_freq" in v1_validation:
        checks.append(ValidationCheckSpec(
            name="house_sync_freq",
            type="within_tolerance",
            expected=float(v1_validation.get("expected_house_sync_freq", 0.0) or 0.0),
            # Reuse the refresh-rate tolerance for house-sync (v1 didn't have a
            # separate field).
            tolerance=float(v1_validation.get("refresh_rate_tolerance", 0.0) or 0.0),
        ))

    if not checks:
        return None
    return ValidationSection(checks=checks)


# ---------------------------------------------------------------------------
# File-level loaders / upgraders
# ---------------------------------------------------------------------------

def load_profile_auto(path: Path) -> ProfileV2:
    """Load a profile from disk, auto-detecting v1 vs v2.

    - v2 on disk -> parsed directly via `ProfileV2.from_dict`.
    - v1 on disk -> upgraded in memory via `upgrade_v1_to_v2`. File NOT
      rewritten. Call `upgrade_profile_file()` for an explicit on-disk upgrade.

    Raises:
        FileNotFoundError, json.JSONDecodeError, ValueError (unknown version).
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    version = detect_version(data)
    if version == 2:
        return ProfileV2.from_dict(data)
    if version == 1:
        return upgrade_v1_to_v2(data)
    raise ValueError(f"load_profile_auto: unknown profile version {version!r} in {path}")


def upgrade_profile_file(path: Path, *, backup: bool = True) -> Path:
    """Rewrite `path` as v2 on disk.

    Idempotent: a v2 file is left unchanged (no rewrite, no backup created).

    When a v1 file is upgraded:
      - If `backup=True` (default), copies the original to `<path>.v1.bak`
        before overwriting.
      - Writes v2 JSON to `path` with 2-space indent.

    Returns:
        The path written (same as input).

    Raises:
        FileNotFoundError, json.JSONDecodeError.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if detect_version(data) == 2:
        return path  # already v2 — no-op

    profile = upgrade_v1_to_v2(data)

    if backup:
        backup_path = path.with_suffix(path.suffix + ".v1.bak")
        shutil.copy2(str(path), str(backup_path))

    with path.open("w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f, indent=2)

    return path


__all__ = [
    "detect_version",
    "upgrade_v1_to_v2",
    "load_profile_auto",
    "upgrade_profile_file",
]
