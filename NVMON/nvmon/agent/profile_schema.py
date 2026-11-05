"""nvmon profile schema v2 — modular, capability-aware profile dataclasses.

A v2 profile describes a node's desired state as a set of OPTIONAL sections.
Unlike v1, every section (gpu, edid, direct_display, mosaic, qsync,
windows_arrangement, validation) is independent — a profile can carry just
one section ("save me only the GPU baseline"), any combo, or all of them.

Design contract:
- `ProfileV2` is the top-level shape. `version` is always 2.
- `ProfileSections` holds every section as Optional — absence of a section
  means "this profile doesn't touch that subsystem".
- Every dataclass has a `to_dict()` and `from_dict()` pair. `to_dict()` emits
  a plain JSON-safe dict; `from_dict()` tolerates missing keys (fills with
  defaults) so the loader survives forward-compatible field additions.
- No I/O in this module. `load`/`save` live alongside `profile_migrate`'s
  upgrade helpers and the agent's `profiles.py` (A2 territory).

Apply order (deterministic, used by `APPLY_ORDER` + agent apply pipeline):

    gpu -> edid -> direct_display -> mosaic -> qsync
        -> windows_arrangement -> validation

Order rationale (see spec 2026-04-17-B):
- `gpu` is a pre-flight assertion. Fail fast if hardware mismatches.
- `edid` must precede Mosaic so the driver sees the forced mode.
- `direct_display` must precede Mosaic — DD removal changes the output pool.
- `mosaic` blows away the Windows desktop arrangement, so it comes before
  `windows_arrangement` which restores it.
- `qsync` binds to a Mosaic grid, so it runs after Mosaic.
- `validation` is read-only and independent — runs last.

Capability mapping (SECTION_TO_CAPABILITY): tells the agent/controller which
`/health.capabilities` key each section depends on. `windows_arrangement`
depends on nothing (Win32 API is always there); `validation` maps to None
because its own deps depend on what it checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Apply order + capability mapping (stable, referenced by controller fan-out)
# ---------------------------------------------------------------------------

#: Canonical apply order. Agents MUST iterate sections in this order.
APPLY_ORDER: tuple[str, ...] = (
    "gpu",
    "edid",
    "direct_display",
    "mosaic",
    "qsync",
    "windows_arrangement",
    "validation",
)

#: Section -> required `/health.capabilities` key. None means "always available".
SECTION_TO_CAPABILITY: dict[str, Optional[str]] = {
    "gpu": "gpu",
    "edid": "edid",
    "direct_display": "displays",
    "mosaic": "mosaic",
    "qsync": "qsync",
    "windows_arrangement": None,   # Win32 API — always present
    "validation": None,            # depends on sections it touches (caller's problem)
}


# ---------------------------------------------------------------------------
# Per-section dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GPUSection:
    """Preflight assertions for the GPU subsystem.

    Entirely optional at the field level — a profile can carry just
    `expected_gpu_count`, or just a temperature threshold, etc.

    Fields:
        temp_warning_c: Per-GPU warning threshold (degrees C).
        power_warning_pct: Per-GPU warning threshold (% of power limit).
        expected_gpu_count: Assert this many GPUs are present. -1 = no assert.
        per_gpu_power_limit: Optional per-GPU power limit in watts. Same length
            as expected_gpu_count when used. Empty = no per-GPU assertion.
        expected_driver_version: Exact driver version string to match.
        min_driver_version: Minimum acceptable driver version.
    """
    temp_warning_c: Optional[int] = None
    power_warning_pct: Optional[int] = None
    expected_gpu_count: int = -1
    per_gpu_power_limit: list[float] = field(default_factory=list)
    expected_driver_version: str = ""
    min_driver_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GPUSection":
        return cls(
            temp_warning_c=_opt_int(data.get("temp_warning_c")),
            power_warning_pct=_opt_int(data.get("power_warning_pct")),
            expected_gpu_count=int(data.get("expected_gpu_count", -1)),
            per_gpu_power_limit=[float(x) for x in (data.get("per_gpu_power_limit") or [])],
            expected_driver_version=str(data.get("expected_driver_version", "") or ""),
            min_driver_version=str(data.get("min_driver_version", "") or ""),
        )


@dataclass
class EDIDSection:
    """EDID override — file on disk applied to specified displays.

    Fields:
        file_path: Path to the EDID file, relative to `profile_dir/edid/` or
            absolute. Empty string is illegal — omit the whole section instead.
        sha256: Optional integrity hash of the EDID file. Empty = skip check.
        apply_to_displays: List of NVAPI display IDs to apply the override to.
            None means "apply to all displays" (see spec: apply_to.mode=="all").
        force_redetect: If True, force a desktop redetect cycle after writing.
    """
    file_path: str = ""
    sha256: str = ""
    apply_to_displays: Optional[list[str]] = None
    force_redetect: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "file_path": self.file_path,
            "sha256": self.sha256,
            "force_redetect": self.force_redetect,
        }
        # None means "all" — preserve the distinction from [] ("explicitly none")
        if self.apply_to_displays is None:
            d["apply_to_displays"] = None
        else:
            d["apply_to_displays"] = list(self.apply_to_displays)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EDIDSection":
        raw_apply = data.get("apply_to_displays", None)
        apply_to: Optional[list[str]]
        if raw_apply is None:
            apply_to = None
        else:
            apply_to = [str(x) for x in raw_apply]
        return cls(
            file_path=str(data.get("file_path", "") or ""),
            sha256=str(data.get("sha256", "") or ""),
            apply_to_displays=apply_to,
            force_redetect=bool(data.get("force_redetect", False)),
        )


@dataclass
class DirectDisplaySection:
    """Direct Display subsystem — which adapters/displays go managed off desktop.

    Fields:
        expected_count: Number of displays expected to be in managed state
            after apply. Used as a sanity check. 0 = no assertion.
        display_ids: Specific NVAPI display IDs to manage. Empty list = use
            adapter-level defaults (all on selected adapter).
        exclude_prefixes: Display name prefixes to exclude from management
            (e.g. ["DDX-"] to skip a backup display named "DDX-MONITOR").
    """
    expected_count: int = 0
    display_ids: list[str] = field(default_factory=list)
    exclude_prefixes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_count": self.expected_count,
            "display_ids": list(self.display_ids),
            "exclude_prefixes": list(self.exclude_prefixes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DirectDisplaySection":
        return cls(
            expected_count=int(data.get("expected_count", 0)),
            display_ids=[str(x) for x in (data.get("display_ids") or [])],
            exclude_prefixes=[str(x) for x in (data.get("exclude_prefixes") or [])],
        )


@dataclass
class MosaicOutputV2:
    """One physical output slot in a Mosaic grid.

    Fields:
        gpu: GPU adapter index (0-based).
        display: NVIDIA display index on that adapter.
        display_id: NVAPI display ID string (e.g. "0x82061085"). May be "" if
            not yet resolved at capture time.
    """
    gpu: int = 0
    display: int = 0
    display_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"gpu": self.gpu, "display": self.display, "display_id": self.display_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MosaicOutputV2":
        return cls(
            gpu=int(data.get("gpu", 0)),
            display=int(data.get("display", 0)),
            display_id=str(data.get("display_id", "") or ""),
        )


@dataclass
class MosaicGrid:
    """One Mosaic grid. Multiple grids allowed (configureMosaic nextgrid).

    Fields:
        label: Free-form grid name (e.g. "main", "ddx-backup"). Informational.
        rows, cols: Grid shape.
        per_display_resolution: Per-output resolution in pixels (w, h).
        total_resolution: Full composed surface resolution (w, h). Typically
            per_display_resolution * (cols, rows), but we store it explicitly
            because Mosaic can set a non-trivial total via bezel/overlap.
        refresh_rate: Refresh rate as a float (e.g. 23.977).
        outputs: Row-major list of MosaicOutputV2 — length == rows * cols.
    """
    label: str = ""
    rows: int = 0
    cols: int = 0
    per_display_resolution: tuple[int, int] = (0, 0)
    total_resolution: tuple[int, int] = (0, 0)
    refresh_rate: float = 0.0
    outputs: list[MosaicOutputV2] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "rows": self.rows,
            "cols": self.cols,
            "per_display_resolution": list(self.per_display_resolution),
            "total_resolution": list(self.total_resolution),
            "refresh_rate": self.refresh_rate,
            "outputs": [o.to_dict() for o in self.outputs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MosaicGrid":
        pdr = data.get("per_display_resolution", [0, 0]) or [0, 0]
        tr = data.get("total_resolution", [0, 0]) or [0, 0]
        return cls(
            label=str(data.get("label", "") or ""),
            rows=int(data.get("rows", 0)),
            cols=int(data.get("cols", 0)),
            per_display_resolution=(int(pdr[0]), int(pdr[1])) if len(pdr) >= 2 else (0, 0),
            total_resolution=(int(tr[0]), int(tr[1])) if len(tr) >= 2 else (0, 0),
            refresh_rate=float(data.get("refresh_rate", 0.0)),
            outputs=[MosaicOutputV2.from_dict(o) for o in (data.get("outputs") or [])],
        )


@dataclass
class MosaicSection:
    """Mosaic subsystem — one or more grids + optional raw command.

    `grids` is a list because `configureMosaic.exe` supports a second grid
    via `nextgrid` (used in legacy-tool-style setups for the DDX backup).
    Most profiles will carry exactly one grid.

    Fields:
        grids: Ordered list of MosaicGrid. Minimum one when section present.
        mosaic_command: Optional captured `configureMosaic.exe set ...` command
            string (informational — not required for apply, which regenerates
            it from the structured grids).
    """
    grids: list[MosaicGrid] = field(default_factory=list)
    mosaic_command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "grids": [g.to_dict() for g in self.grids],
            "mosaic_command": self.mosaic_command,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MosaicSection":
        return cls(
            grids=[MosaicGrid.from_dict(g) for g in (data.get("grids") or [])],
            mosaic_command=str(data.get("mosaic_command", "") or ""),
        )


@dataclass
class QSyncSection:
    """QSync (frame-lock / genlock) subsystem.

    Fields:
        source: "HOUSESYNC" | "INTERNAL" | "NONE". Ignored for role="client".
        trigger: "FALLING" | "RISING". Ignored for role="client".
        grid: Which Mosaic grid index (0-based in MosaicSection.grids) to
            bind the sync to. Integer.
        expected_sync_rate: Expected incoming house-sync frequency (Hz) for
            validation. 0.0 = no assertion.
        role: "server" — this node owns the timing (drives or receives house
            sync directly). "client" — this node is daisy-chained downstream
            of another QSync server; configureQsync2 takes `enable client`
            and source/trigger are owned upstream. Default "server" preserves
            historical behavior for legacy profiles.
        displays: Explicit (gpu_index, display_index) pairs to lock to the
            server. Empty = auto-discover at apply time.
    """
    source: str = "HOUSESYNC"
    trigger: str = "FALLING"
    grid: int = 0
    expected_sync_rate: float = 0.0
    role: str = "server"
    displays: list[tuple[int, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "trigger": self.trigger,
            "grid": self.grid,
            "expected_sync_rate": self.expected_sync_rate,
            "role": self.role,
            "displays": [list(p) for p in self.displays],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QSyncSection":
        # Source normalization: legacy captures wrote "VSYNC" (a parser-internal
        # bucket). configureQsync2.exe doesn't accept that — only HOUSESYNC and
        # INTERNAL are valid. Coerce VSYNC -> HOUSESYNC; warn so the operator
        # knows their profile carried a stale value.
        raw_role = str(data.get("role", "server") or "server").lower()
        if raw_role not in ("server", "client"):
            try:
                from .. import log as _log
                _log.get().warning(
                    "QSyncSection: unknown role=%r — coercing to 'server'",
                    data.get("role"),
                )
            except Exception:
                pass
            raw_role = "server"

        raw_source = str(data.get("source", "HOUSESYNC") or "HOUSESYNC").upper().replace(" ", "")
        if raw_role == "client" and raw_source in ("", "VSYNC", "INTERNAL", "HOUSESYNC", "NONE"):
            # Daisy-chain client: no source of its own (locks to the upstream
            # framelock pulse; the card reports VSYNC because there is no
            # BNC). Keep "" — never let a client profile claim HOUSESYNC.
            normalized_source = ""
        elif raw_source == "VSYNC":
            try:
                from .. import log as _log
                _log.get().warning(
                    "QSyncSection: coercing legacy source=VSYNC -> HOUSESYNC "
                    "(VSYNC is a parser bucket, not a valid CLI source)."
                )
            except Exception:
                pass
            normalized_source = "HOUSESYNC"
        elif raw_source in ("HOUSESYNC", "INTERNAL", "NONE"):
            normalized_source = raw_source
        else:
            try:
                from .. import log as _log
                _log.get().warning(
                    "QSyncSection: unknown source=%r — coercing to HOUSESYNC",
                    data.get("source"),
                )
            except Exception:
                pass
            normalized_source = "HOUSESYNC"

        raw_displays = data.get("displays", []) or []
        parsed_displays: list[tuple[int, int]] = []
        if isinstance(raw_displays, list):
            for pair in raw_displays:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    try:
                        parsed_displays.append((int(pair[0]), int(pair[1])))
                    except (TypeError, ValueError):
                        continue

        return cls(
            source=normalized_source,
            trigger=str(data.get("trigger", "FALLING") or "FALLING"),
            grid=int(data.get("grid", 0)),
            expected_sync_rate=float(data.get("expected_sync_rate", 0.0)),
            role=raw_role,
            displays=parsed_displays,
        )


@dataclass
class WindowsDisplaySnapshot:
    """One entry in a Windows display arrangement snapshot.

    Captured from `EnumDisplaySettingsEx` / `ChangeDisplaySettingsEx`.

    Fields:
        device_name: Win32 device name (e.g. "\\\\.\\DISPLAY5").
        position: (x, y) top-left corner in the virtual desktop.
        resolution: (width, height) pixels.
        refresh_rate: Refresh rate in Hz (integer; Win32 reports whole Hz).
        primary: True if this is the Windows primary display.
    """
    device_name: str = ""
    position: tuple[int, int] = (0, 0)
    resolution: tuple[int, int] = (0, 0)
    refresh_rate: int = 0
    primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_name": self.device_name,
            "position": list(self.position),
            "resolution": list(self.resolution),
            "refresh_rate": self.refresh_rate,
            "primary": self.primary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowsDisplaySnapshot":
        pos = data.get("position", [0, 0]) or [0, 0]
        res = data.get("resolution", [0, 0]) or [0, 0]
        return cls(
            device_name=str(data.get("device_name", "") or ""),
            position=(int(pos[0]), int(pos[1])) if len(pos) >= 2 else (0, 0),
            resolution=(int(res[0]), int(res[1])) if len(res) >= 2 else (0, 0),
            refresh_rate=int(data.get("refresh_rate", 0) or 0),
            primary=bool(data.get("primary", False)),
        )


@dataclass
class WindowsArrangementSection:
    """Windows desktop arrangement — per-display position, mode, primary flag.

    Stored as a list of snapshots (one per display). Applied via
    `ChangeDisplaySettingsEx` after Mosaic has set topology.
    """
    displays: list[WindowsDisplaySnapshot] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # Emits just the list — WindowsArrangement is really just a list at
        # the JSON level; the dataclass wrapper exists for typing.
        return {"displays": [d.to_dict() for d in self.displays]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowsArrangementSection":
        # Accept either {"displays": [...]} or a raw list (friendly for v1
        # upgrade and manual-edit workflows).
        if isinstance(data, list):
            entries = data
        else:
            entries = data.get("displays", []) or []
        return cls(displays=[WindowsDisplaySnapshot.from_dict(e) for e in entries])


@dataclass
class ValidationCheckSpec:
    """One named validation check.

    Fields:
        name: Free-form identifier (e.g. "refresh_rate", "qsync_locked").
        type: Check type ("equals", "within_tolerance", "truthy", "count").
        expected: Expected value (type depends on `type`).
        tolerance: Optional tolerance for "within_tolerance" checks.
    """
    name: str = ""
    type: str = "equals"
    expected: Any = None
    tolerance: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "expected": self.expected,
        }
        if self.tolerance is not None:
            d["tolerance"] = self.tolerance
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationCheckSpec":
        tol = data.get("tolerance", None)
        return cls(
            name=str(data.get("name", "") or ""),
            type=str(data.get("type", "equals") or "equals"),
            expected=data.get("expected", None),
            tolerance=None if tol is None else float(tol),
        )


@dataclass
class ValidationSection:
    """Post-apply validation — list of named checks.

    Each check is an independent assertion. `all_passed` semantics live in the
    validator module, not here.
    """
    checks: list[ValidationCheckSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"checks": [c.to_dict() for c in self.checks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationSection":
        # Accept list-of-checks OR dict-of-checks (v1-style flat shape).
        if isinstance(data, list):
            return cls(checks=[ValidationCheckSpec.from_dict(c) for c in data])
        raw_checks = data.get("checks", [])
        if isinstance(raw_checks, list):
            return cls(checks=[ValidationCheckSpec.from_dict(c) for c in raw_checks])
        return cls()


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------

@dataclass
class ProfileSections:
    """Container for all optional sections of a v2 profile.

    Every field is Optional. Absence == "this profile doesn't touch that
    subsystem". Presence == "apply/validate this section".
    """
    gpu: Optional[GPUSection] = None
    edid: Optional[EDIDSection] = None
    direct_display: Optional[DirectDisplaySection] = None
    mosaic: Optional[MosaicSection] = None
    qsync: Optional[QSyncSection] = None
    windows_arrangement: Optional[WindowsArrangementSection] = None
    validation: Optional[ValidationSection] = None

    def present(self) -> list[str]:
        """Return the list of section names that are actually set."""
        return [
            name for name in APPLY_ORDER
            if getattr(self, name) is not None
        ]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in APPLY_ORDER:
            val = getattr(self, name)
            if val is None:
                continue
            out[name] = val.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileSections":
        if not isinstance(data, dict):
            return cls()
        return cls(
            gpu=(GPUSection.from_dict(data["gpu"]) if "gpu" in data and data["gpu"] is not None else None),
            edid=(EDIDSection.from_dict(data["edid"]) if "edid" in data and data["edid"] is not None else None),
            direct_display=(
                DirectDisplaySection.from_dict(data["direct_display"])
                if "direct_display" in data and data["direct_display"] is not None else None
            ),
            mosaic=(MosaicSection.from_dict(data["mosaic"]) if "mosaic" in data and data["mosaic"] is not None else None),
            qsync=(QSyncSection.from_dict(data["qsync"]) if "qsync" in data and data["qsync"] is not None else None),
            windows_arrangement=(
                WindowsArrangementSection.from_dict(data["windows_arrangement"])
                if "windows_arrangement" in data and data["windows_arrangement"] is not None else None
            ),
            validation=(
                ValidationSection.from_dict(data["validation"])
                if "validation" in data and data["validation"] is not None else None
            ),
        )


@dataclass
class ProfileV2:
    """Top-level v2 profile.

    Fields:
        version: Always 2. The loader uses this (plus absence) to dispatch
            v1 upgrade path vs direct v2 parse.
        name: Profile identifier.
        description: Human-readable summary.
        sections: ProfileSections — every capability-bound section.
        tags: Free-form tags for filtering in the dashboard.
        created_at: ISO8601 timestamp string (informational).
        created_by: Tool that wrote it (e.g. "nvmon 0.1.0").
        source_host: Hostname of the node the profile was captured from.
    """
    version: int = 2
    name: str = ""
    description: str = ""
    sections: ProfileSections = field(default_factory=ProfileSections)
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    created_by: str = ""
    source_host: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "sections": self.sections.to_dict(),
        }
        # Only emit optional metadata when populated — keeps JSON clean.
        if self.tags:
            d["tags"] = list(self.tags)
        if self.created_at:
            d["created_at"] = self.created_at
        if self.created_by:
            d["created_by"] = self.created_by
        if self.source_host:
            d["source_host"] = self.source_host
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileV2":
        return cls(
            version=int(data.get("version", 2)),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            sections=ProfileSections.from_dict(data.get("sections", {}) or {}),
            tags=[str(t) for t in (data.get("tags") or [])],
            created_at=str(data.get("created_at", "") or ""),
            created_by=str(data.get("created_by", "") or ""),
            source_host=str(data.get("source_host", "") or ""),
        )


# ---------------------------------------------------------------------------
# Helpers — save (subset), applicability, JSON I/O
# ---------------------------------------------------------------------------

def save_profile(
    profile: ProfileV2,
    path: Path,
    sections: Optional[list[str]] = None,
) -> Path:
    """Serialize a ProfileV2 to JSON on disk.

    Args:
        profile: The profile to save.
        path: Destination file (overwritten).
        sections: If given, only write these section names. Other sections
            are omitted from the output. Unknown names are silently dropped
            with no error — we don't want a typo to blow up a save.

    Returns:
        The path written.

    Raises:
        ValueError: If `sections` is given but none of the requested sections
            are actually present on the profile (nothing to save).
        OSError: On disk write failure.
    """
    import json

    data = profile.to_dict()

    if sections is not None:
        wanted = set(sections)
        all_sections = data.get("sections", {}) or {}
        filtered = {k: v for k, v in all_sections.items() if k in wanted}
        if not filtered:
            raise ValueError(
                f"save_profile: none of the requested sections are present on "
                f"the profile (requested={sorted(wanted)}, "
                f"present={sorted(all_sections.keys())})"
            )
        data["sections"] = filtered

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def load_profile(path: Path) -> ProfileV2:
    """Load a v2 profile from disk.

    For v1-detection + auto-upgrade, callers should use
    `profile_migrate.load_profile_auto(path)` instead. This loader is strict:
    it expects `version: 2` at the top level.

    Raises:
        FileNotFoundError, ValueError (on unknown version), json.JSONDecodeError
    """
    import json

    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    version = data.get("version")
    if version != 2:
        raise ValueError(
            f"load_profile: expected version 2, got {version!r}. "
            f"Use profile_migrate.load_profile_auto() for v1 files."
        )
    return ProfileV2.from_dict(data)


def applicable_sections(
    profile: ProfileV2,
    capabilities: dict[str, Any],
) -> list[str]:
    """Given a node's capabilities, return sections of `profile` that CAN run.

    Sections that the profile doesn't carry are never returned. Sections that
    don't map to any capability (windows_arrangement, validation) are always
    returned when present.

    Args:
        profile: The profile to filter.
        capabilities: The node's `/health.capabilities` dict. Expected shape:
            {"gpu": {"available": bool, ...}, "mosaic": {...}, ...}.
            Missing keys are treated as unavailable.

    Returns:
        Section names in canonical APPLY_ORDER.
    """
    result: list[str] = []
    for name in APPLY_ORDER:
        if getattr(profile.sections, name) is None:
            continue
        cap_key = SECTION_TO_CAPABILITY.get(name)
        if cap_key is None:
            # No capability gate — always applicable when present.
            result.append(name)
            continue
        cap = capabilities.get(cap_key) if isinstance(capabilities, dict) else None
        if isinstance(cap, dict) and cap.get("available"):
            result.append(name)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _opt_int(v: Any) -> Optional[int]:
    """Coerce to int, preserving None (so Optional fields stay Optional)."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Generic shallow dataclass -> dict emitter used by simple sections.

    Converts tuples to lists (for JSON friendliness) and recursively handles
    nested dataclasses. Skips None values on Optional fields.
    """
    if not is_dataclass(obj):
        raise TypeError(f"_dataclass_to_dict: not a dataclass: {type(obj)!r}")
    out: dict[str, Any] = {}
    for f in fields(obj):
        val = getattr(obj, f.name)
        if val is None:
            out[f.name] = None
        elif isinstance(val, tuple):
            out[f.name] = list(val)
        elif is_dataclass(val) and hasattr(val, "to_dict"):
            out[f.name] = val.to_dict()
        elif isinstance(val, list):
            out[f.name] = [
                item.to_dict() if is_dataclass(item) and hasattr(item, "to_dict") else item
                for item in val
            ]
        else:
            out[f.name] = val
    return out


__all__ = [
    "APPLY_ORDER",
    "SECTION_TO_CAPABILITY",
    "GPUSection",
    "EDIDSection",
    "DirectDisplaySection",
    "MosaicOutputV2",
    "MosaicGrid",
    "MosaicSection",
    "QSyncSection",
    "WindowsDisplaySnapshot",
    "WindowsArrangementSection",
    "ValidationCheckSpec",
    "ValidationSection",
    "ProfileSections",
    "ProfileV2",
    "save_profile",
    "load_profile",
    "applicable_sections",
]
