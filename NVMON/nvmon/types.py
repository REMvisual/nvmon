"""Data types for nvmon — all dataclasses used across modules.

Site-specific fields intentionally omitted:
- No `BackupStatus` dataclass (failover is out of scope).
- No `PeerStatus` dataclass (peer/backup concept is out of scope).
- No `backup_status` field on Profile.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DDStatus(enum.Enum):
    REMOVED_MANAGED = "Removed Managed"
    ON_DESKTOP = "On Desktop"
    UNKNOWN = "Unknown"


class QSyncSource(enum.Enum):
    HOUSESYNC = "HOUSESYNC"
    INTERNAL = "INTERNAL"
    NONE = "NONE"


class QSyncTrigger(enum.Enum):
    FALLING = "FALLING"
    RISING = "RISING"


class AlertSeverity(enum.Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


# ---------------------------------------------------------------------------
# Direct Display
# ---------------------------------------------------------------------------

@dataclass
class DisplayInfo:
    id: str = ""                    # NVAPI ID (e.g. "82061083")
    name: str = ""                  # Physical display name
    status: str = ""                # "Desktop @(X,Y)" or "Removed Managed"
    refresh_rate: float = 0.0
    refresh_fraction: str = ""      # e.g. "23976/1000"
    resolution: tuple[int, int] = (0, 0)
    connector: str = ""             # "DisplayPort", "HDMI", etc.
    bits_per_channel: int = 0
    color_space: str = ""
    pixel_encoding: str = ""
    gpu_index: int = -1             # Adapter index (0-based)
    display_index: int = -1         # NVIDIA display index (from table col 0)
    os_target_id: str = ""          # Windows OS target ID (e.g. "61100")


@dataclass
class DDState:
    active: bool = False
    display_count: int = 0
    displays: list[DisplayInfo] = field(default_factory=list)
    # Windows' view of each active path (from agent/ccd.describe()):
    # [{"name", "source": (w,h), "position": (x,y), "target": (w,h), "hz"}]
    # source = desktop size, target = signal on the wire. target != source
    # means Windows is scaling the desktop (the "wall sent as 10240x5760@60
    # while Mosaic reports 3840x2160@23.976" failure). Filled by the agent
    # monitor, not by parse_dd_status.
    signals: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Mosaic
# ---------------------------------------------------------------------------

@dataclass
class MosaicOutput:
    gpu: int = 0
    display: int = 0
    display_id: str = ""


@dataclass
class MosaicState:
    enabled: bool = False
    rows: int = 0
    cols: int = 0
    per_display_resolution: tuple[int, int] = (0, 0)
    total_resolution: tuple[int, int] = (0, 0)
    refresh_rate: float = 0.0
    output_count: int = 0
    synchronized: bool = False
    outputs: list[MosaicOutput] = field(default_factory=list)


# ---------------------------------------------------------------------------
# QSync
# ---------------------------------------------------------------------------

@dataclass
class GPUSyncInfo:
    gpu: int = 0
    synced: bool = False
    signal_available: bool = False
    effective_synced: bool = False  # True when locked via HOUSESYNC even if raw "Is synced" says NO
    sync_note: str = ""            # e.g. "via HOUSESYNC" or "via GPU 0"
    # Raw "Sync State" token from configureQsync2.exe queryAll, when present.
    # Either "SERVER", "CLIENT", or "" — used by profile capture to decide
    # whether the captured node should be applied as a daisy-chain client
    # (role=client) or a HOUSESYNC server (role=server).
    sync_state: str = ""


@dataclass
class QSyncState:
    available: bool = False
    synced: bool = False
    house_sync_detected: bool = False
    house_sync_incoming_hz: float = 0.0
    source: str = ""
    trigger: str = ""
    per_gpu_sync: list[GPUSyncInfo] = field(default_factory=list)
    # Computed labels for the dashboard card. Both populated by the parser
    # so the frontend doesn't have to re-derive them. Empty strings on
    # nodes where QSync isn't engaged.
    #   "external house sync"     — server, BNC signal in use
    #   "upstream framelock pulse" — client, locked to daisy-chain master
    #   "internal clock"           — locked to own clock (parser bucket
    #                                "VSYNC"/"INTERNAL"); not real fleet sync
    #   ""                         — not locked
    lock_source_label: str = ""
    # The meaningful sync rate from the operator's POV. For a server with
    # HOUSESYNC, this is house_sync_incoming_hz. For a client locked to the
    # upstream framelock pulse, the parser leaves this 0.0 (it doesn't have
    # the display refresh rate handy) — the view-model fills it from the
    # display refresh rate instead. 0.0 means "not applicable".
    effective_lock_hz: float = 0.0
    # Inferred role from the per-GPU sync_state tokens — "server", "client",
    # or "" when QSync isn't engaged. Lets the view-model render LOCKED · ROLE
    # without re-implementing the heuristic.
    role: str = ""


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------

@dataclass
class GPUInfo:
    index: int = 0
    name: str = ""
    pci_bus_id: str = ""
    driver_version: str = ""
    temperature_c: int = 0
    power_draw_w: float = 0.0
    power_limit_w: float = 0.0
    gpu_utilization_pct: int = 0
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    gpu_clock_mhz: int = 0
    mem_clock_mhz: int = 0
    video_clock_mhz: int = 0
    pstate: str = ""
    display_active: bool = False
    fan_speed_pct: int = 0
    pcie_gen: int = 0
    pcie_width: int = 0


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@dataclass
class ProfileMosaic:
    enabled: bool = True
    rows: int = 2
    cols: int = 2
    width: int = 3840
    height: int = 2160
    refresh_rate: str = "23.98"
    outputs: list[MosaicOutput] = field(default_factory=list)
    # When True, the apply pipeline skips this subsystem entirely — neither
    # disable-then-configure nor any other touch. Use it for nodes whose
    # display config differs from the fleet (e.g. NODE-11 at 1080p while
    # the rest are 4K). The standard "default" profile sets it to False;
    # a per-node "node11" profile sets it to True so applying that profile
    # leaves Mosaic alone.
    skip_apply: bool = False


@dataclass
class ProfileDD:
    enabled: bool = True
    mode: str = "index"
    index: int = 1
    # See ProfileMosaic.skip_apply — same idea, applies to DirectDisplay.
    skip_apply: bool = False


@dataclass
class ProfileQSync:
    enabled: bool = True
    source: str = "HOUSESYNC"
    trigger: str = "FALLING"
    # -1 = "no grid; let qsync.enable() auto-discover the connected displays".
    # The previous default of 1 referenced a Mosaic grid that doesn't exist on
    # non-Mosaic rigs, causing configureQsync2.exe to silently no-op.
    grid: int = -1
    # See ProfileMosaic.skip_apply — same idea, applies to QSync.
    skip_apply: bool = False
    # "server" (this node drives or receives house sync) or "client" (this
    # node is downstream of another nvmon node's QSync output, daisy-chain
    # style — configureQsync2.exe gets `enable client ...` and source/trigger
    # are owned by the upstream timing server). Default "server" matches the
    # historical behavior so legacy profiles upgrade cleanly.
    role: str = "server"
    # Explicit (gpu_index, display_index) pairs to lock to the server. Empty
    # list means "auto-discover at apply time" (today's qsync.enable() behavior).
    # When non-empty, the apply path emits one `display=G,D` per pair and skips
    # auto-discovery + grid lookup. Matches the screenshot's "Select displays
    # to lock to the server" panel.
    displays: list[tuple[int, int]] = field(default_factory=list)
    # ----- Deep state captured via NVWMI (read at /profile/save, verified
    #       at /apply). Empty string / empty tuple / None = "no expectation;
    #       don't drift-check this field". configureQsync2.exe doesn't expose
    #       the SET surface for these — they're primarily ground-truth for
    #       drift detection on apply, plus sanity gates that refuse apply if
    #       the live hardware doesn't match the captured profile. Operator
    #       sets the deeper config in NVCP once and we capture+verify it.
    # (gpu_index, display_index) of the framelock timing master display.
    # None = no expectation (typical for a pure client node).
    master_display: tuple[int, int] | None = None
    # RJ45 connector direction on the GSync card. "in" | "out" | "none".
    connector_0_direction: str = ""
    connector_1_direction: str = ""
    # Refines source: distinguishes vsync-from-external-monitor from
    # housesync. "" = no expectation. Values:
    #   housesync, internal, vsync_external, unknown.
    source_kind: str = ""
    # Sanity gate — if non-empty, apply refuses to run when the live GPU's
    # UUID or the sync card's serial don't match. Captures intent: "this
    # profile is for this specific hardware".
    gpu_uuid_pin: str = ""
    sync_card_serial_pin: str = ""
    # NVIDIA driver version captured at profile-save time. Logged at apply
    # for diagnostics and surfaced in drift reports if the live driver
    # differs (warning only, never blocks apply — driver versions roll
    # forward on patch days and we don't want to wedge the fleet over a
    # minor revision). Empty string = no expectation.
    driver_version_pin: str = ""


@dataclass
class ProfileEDID:
    file: str = ""


@dataclass
class ProfileValidation:
    # Defaults are 0.0 / False — meaning "no expectation; skip the
    # check unless capture wrote a real value". The previous default
    # of 23.976 was a legacy-tool-era assumption that leaked into every
    # profile and produced spurious validation failures on rigs running
    # at 24.000, 25, 30, 50, 60, etc.  profile_capture overwrites these
    # with the live rate at capture time.
    expected_refresh_rate: float = 0.0
    # 0.01 Hz: 23.976 is not 24.000 (config.RATE_MATCH_TOLERANCE_HZ). The
    # consumers cap any stored value there via config.rate_tolerance().
    refresh_rate_tolerance: float = 0.01
    expected_qsync_locked: bool = False
    expected_house_sync_freq: float = 0.0


@dataclass
class Profile:
    name: str = ""
    description: str = ""
    mosaic: ProfileMosaic = field(default_factory=ProfileMosaic)
    direct_display: ProfileDD = field(default_factory=ProfileDD)
    qsync: ProfileQSync = field(default_factory=ProfileQSync)
    edid: ProfileEDID = field(default_factory=ProfileEDID)
    validation: ProfileValidation = field(default_factory=ProfileValidation)
    display_ids: dict = field(default_factory=dict)
    windows_arrangement: list[dict] = field(default_factory=list)
    mosaic_command: str = ""


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    severity: str = "WARNING"
    type: str = ""
    message: str = ""
    timestamp: str = ""


# ---------------------------------------------------------------------------
# Output Health
# ---------------------------------------------------------------------------

@dataclass
class OutputHealth:
    total_displays: int = 0
    dd_active_count: int = 0
    dd_expected_count: int = 4
    all_same_refresh: bool = False
    all_same_resolution: bool = False
    refresh_rate_hz: float = 0.0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationCheck:
    passed: bool = False
    detail: str = ""


@dataclass
class ValidationResult:
    all_passed: bool = False
    checks: dict[str, ValidationCheck] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Subsystem / command result
# ---------------------------------------------------------------------------

@dataclass
class SubsystemResult:
    """Result of a subsystem control operation (e.g. enable DD, apply Mosaic)."""
    success: bool = False
    subsystem: str = ""         # "gpu", "displays", "mosaic", "qsync", "edid"
    operation: str = ""         # "enable", "disable", "apply", "query"
    message: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class CommandResult:
    success: bool = False
    return_code: int = -1
    stdout: str = ""
    stderr: str = ""
    command: str = ""
    duration_ms: int = 0
