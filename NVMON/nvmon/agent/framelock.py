"""Framelock / GSync deep state via NVWMI (Windows-only, read-only).

What this module is for
-----------------------
``configureQsync2.exe queryAll`` exposes the *control* surface — source
(HOUSESYNC/INTERNAL), trigger edge, server vs client mode, per-GPU sync
state. It does NOT expose the deeper genlock detail an operator can see in
NVIDIA Control Panel: which RJ45 connector is wired IN vs OUT, which
display on the GPU is the framelock master vs slave, the GSync card serial,
the GPU UUID, etc. That detail lives in NVWMI under the
``ROOT\\CIMV2\\NV`` namespace, accessible from PowerShell via
``Get-CimInstance``.

Rather than add a Python WMI/pywin32 dependency, we shell out to PowerShell
through the same ``executors.run`` pipeline the rest of the agent uses,
ask for ``ConvertTo-Json`` output, and ``json.loads`` the result. Stdlib
only, no new deps. Per CLAUDE.md.

Surface
-------
``capability()``  — is PowerShell + the NVWMI namespace usable on this box.
``status()``      — typed snapshot. Empty/unavailable returns a
                    ``FramelockState`` with ``available=False`` rather
                    than raising; matches the "graceful degradation"
                    convention every other subsystem follows.

This module is **read-only**. Writes still go through configureQsync2.exe;
NVWMI is the diagnostic / verification surface.

Scope of fields captured (v1)
-----------------------------
- ``gpu_uuid``               — for sanity gate on apply.
- ``sync_card_serial``       — for sanity gate on apply.
- ``master_display``         — (gpu_index, display_index) of the framelock
                               timing master, when the GSync card reports
                               one. ``None`` when no master is set
                               (e.g. a pure client node receiving via
                               daisy-chain).
- ``connector_0_direction``  — ``"in"`` | ``"out"`` | ``"none"`` for the
                               first RJ45 port on the GSync card.
- ``connector_1_direction``  — same for the second port.
- ``source_kind``            — ``"housesync"`` | ``"internal"`` |
                               ``"vsync_external"`` | ``"unknown"``.
                               Refines configureQsync2's coarser source
                               value with whether vsync is sourced from
                               an external monitor vs internal generator.

Out of scope for v1 (left for a follow-up): per-display master/slave list,
video mode (TTL etc.), polarity beyond what configureQsync2's trigger
already covers.

Operator note
-------------
NVWMI class names and property names vary across driver versions. The
parser below is intentionally forgiving — every field falls back to a
sentinel when missing, and we log a single debug line per absent class
so an operator can see what their driver is exposing. After the first
real-hardware run, expect to refine the field-name mappings (search for
``# FIELD-MAP`` in this file).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from typing import Any

from .. import config, executors, log

# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def _powershell_exe() -> str | None:
    """Resolve a usable PowerShell executable, preferring pwsh (cross-version
    compatible) but falling back to Windows PowerShell 5.1. Returns None when
    neither is on PATH — agent runs without framelock features in that case."""
    for candidate in ("pwsh.exe", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def capability() -> dict:
    """Report whether this subsystem can read NVWMI on the local box.

    Mirrors the shape used by displays / mosaic / qsync / edid: the agent
    boots even when this is unavailable; downstream consumers gate on
    ``available``.
    """
    ps = _powershell_exe()
    if not ps:
        return {
            "available": False,
            "exe": None,
            "reason": "powershell.exe / pwsh.exe not on PATH",
        }
    # We don't actually pre-flight the namespace here — that would mean
    # spawning a PowerShell on every /health call. Just confirm the
    # transport exists; status() handles namespace-missing as empty state.
    return {"available": True, "exe": ps}


def is_available() -> bool:
    return _powershell_exe() is not None


# ---------------------------------------------------------------------------
# Typed snapshot
# ---------------------------------------------------------------------------


@dataclass
class FramelockState:
    """Read-only snapshot of NVWMI Gsync / framelock state.

    Every field defaults to a "no information" sentinel so callers can
    treat absence and unavailability uniformly.
    """
    available: bool = False
    gpu_uuid: str = ""
    sync_card_serial: str = ""
    sync_card_firmware: str = ""          # e.g. "3.06" — empty when not exposed
    master_display: tuple[int, int] | None = None
    connector_0_direction: str = "none"   # "in" | "out" | "none"
    connector_1_direction: str = "none"
    source_kind: str = "unknown"          # housesync|internal|vsync_external|unknown
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "available": self.available,
            "gpu_uuid": self.gpu_uuid,
            "sync_card_serial": self.sync_card_serial,
            "sync_card_firmware": self.sync_card_firmware,
            "master_display": list(self.master_display) if self.master_display else None,
            "connector_0_direction": self.connector_0_direction,
            "connector_1_direction": self.connector_1_direction,
            "source_kind": self.source_kind,
        }
        if self.error:
            out["error"] = self.error
        return out


# ---------------------------------------------------------------------------
# PowerShell snippet — single-shot read of every NVWMI class we care about
# ---------------------------------------------------------------------------

# Why one big snippet instead of one PowerShell call per class: each
# ``Get-CimInstance`` startup costs ~150-300ms. Bundling them keeps the
# whole status read under a second on a normal box. ``-ErrorAction
# SilentlyContinue`` per class means a missing namespace key doesn't
# poison the whole read — we just get fewer keys in the JSON.

_PS_QUERY = r"""
$ErrorActionPreference = 'Continue'
$out = @{}
foreach ($cls in @('Gsync','GsyncTopology','FrameLockDisplayConfig','Display','Topology')) {
  try {
    $rows = Get-CimInstance -Namespace 'ROOT\CIMV2\NV' -ClassName $cls `
              -ErrorAction Stop |
            Select-Object -Property * -ExcludeProperty PSComputerName,CimClass,CimInstanceProperties,CimSystemProperties
    if ($null -ne $rows) {
      $out[$cls] = @($rows)
    } else {
      $out[$cls] = @()
    }
  } catch {
    $out[$cls] = @{ '__error' = $_.Exception.Message }
  }
}
$out | ConvertTo-Json -Depth 6 -Compress
"""


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status(*, dry_run: bool = False, timeout_seconds: int = 8) -> FramelockState:
    """Read framelock state, preferring NVCP's saved topology JSON when
    available, falling back to NVWMI via PowerShell.

    The NVCP ``View System Topology → Save Logfile`` button produces a
    structured JSON the driver itself emits — every field we want
    (Framelock 0/1 direction, GPU serial, sync mode, server/client role,
    timing source description) is present in stable shapes. That's a much
    more reliable source than raw NVWMI, whose property names drift
    across driver releases. If the operator drops that file at any of the
    known locations (see ``_TOPOLOGY_PATHS``), we read it directly. If
    no file is present, we fall back to the NVWMI query.

    Always returns a ``FramelockState``. ``available=False`` when neither
    source produced data.
    """
    logger = log.get()

    # ---- 1. NVCP topology JSON, if present ----
    topo = _read_topology_json()
    if topo is not None:
        state = _parse_topology_json(topo)
        if state.available:
            logger.debug(f"framelock.status: using NVCP topology JSON ({state.raw.get('_source_path', 'unknown')})")
            return state

    # ---- 2. Fall back to NVWMI via PowerShell ----
    state = FramelockState()

    ps = _powershell_exe()
    if not ps:
        state.error = "powershell not on PATH"
        return state

    if dry_run:
        return state

    # Pass the snippet on stdin via -Command to avoid PowerShell-quoting
    # nightmares on the agent's argv. We bundle it as a single -Command
    # arg, escape-prefixed minimally because the snippet uses single quotes
    # for class names (no $ interpolation issues).
    args = [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-Command", _PS_QUERY,
    ]
    try:
        result = executors.run(
            ps, args, label="framelock-nvwmi-query",
            dry_run=False, quiet=True, timeout=timeout_seconds,
        )
    except TypeError:
        # Older executors.run signatures may not accept timeout — fall back.
        result = executors.run(
            ps, args, label="framelock-nvwmi-query",
            dry_run=False, quiet=True,
        )
    except Exception as exc:
        state.error = f"powershell invocation failed: {exc}"
        logger.debug(state.error)
        return state

    raw_stdout = (result.stdout or "").strip()
    if not raw_stdout:
        state.error = (result.stderr or "no NVWMI output").strip()[:200]
        logger.debug("framelock.status: empty NVWMI output (%s)", state.error)
        return state

    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        state.error = f"NVWMI JSON parse failed: {exc}"
        logger.debug("%s — first 200 bytes: %s", state.error, raw_stdout[:200])
        return state

    state.raw = parsed if isinstance(parsed, dict) else {}
    _populate_typed_fields(state)
    state.available = bool(state.raw)
    return state


def _populate_typed_fields(state: FramelockState) -> None:
    """Pull our typed fields out of the raw NVWMI JSON.

    FIELD-MAP: NVIDIA's NVWMI schema uses different property names across
    driver releases. Each lookup tolerates absence and tries a few common
    variants. After the first real-hardware capture, refine the variants
    list to match what the box actually emits.
    """

    def _first(d: dict[str, Any] | list, *names: str) -> Any:
        if isinstance(d, dict):
            for n in names:
                if n in d and d[n] not in (None, ""):
                    return d[n]
        return None

    raw = state.raw

    # Gsync class — typically one row per GSync card. We take the first.
    gsync_rows = raw.get("Gsync") or []
    if isinstance(gsync_rows, list) and gsync_rows:
        g = gsync_rows[0]
        if isinstance(g, dict):
            serial = _first(g, "SerialNumber", "BoardSerialNumber", "SyncCardSerial")
            if serial is not None:
                state.sync_card_serial = str(serial)
            # Firmware version. NVWMI Gsync property names drift across
            # driver releases; the most common are FirmwareVersion,
            # FirmwareRevision, FwVersion, and a Major/Minor pair on
            # older schemas. Try the flat strings first, then assemble
            # from the pair as a fallback. Surfacing this matters
            # because RTX PRO Sync FW v3.04/v3.06 has the documented
            # "Timing Server" silently-fails-to-apply bug on Blackwell;
            # when an operator sees source drift after apply, this is
            # the field that confirms whether they're on the buggy FW.
            fw = _first(g, "FirmwareVersion", "FirmwareRevision", "FwVersion", "Firmware")
            if fw is not None:
                state.sync_card_firmware = str(fw).strip()
            else:
                fw_major = _first(g, "FirmwareMajor", "FirmwareVersionMajor")
                fw_minor = _first(g, "FirmwareMinor", "FirmwareVersionMinor")
                if fw_major is not None and fw_minor is not None:
                    state.sync_card_firmware = f"{fw_major}.{fw_minor}"
            # source kind: NVWMI Gsync class often has a "SyncSource" or
            # "SignalSource" enum. We coerce to our coarser bucket.
            src = _first(g, "SyncSource", "SignalSource", "Source")
            if src is not None:
                state.source_kind = _coerce_source_kind(str(src))
            # connector directions
            c0 = _first(g, "Connector0Direction", "Port0Direction", "RJ45Port0")
            c1 = _first(g, "Connector1Direction", "Port1Direction", "RJ45Port1")
            if c0 is not None:
                state.connector_0_direction = _coerce_direction(str(c0))
            if c1 is not None:
                state.connector_1_direction = _coerce_direction(str(c1))

    # FrameLockDisplayConfig — per-display flags. Master is the row with
    # IsMaster / Master / Role=master.
    fld = raw.get("FrameLockDisplayConfig") or []
    if isinstance(fld, list):
        for row in fld:
            if not isinstance(row, dict):
                continue
            is_master = _first(row, "IsMaster", "Master", "MasterFlag")
            role_str = _first(row, "Role")
            is_master_bool = (
                bool(is_master) if isinstance(is_master, (bool, int)) else
                (str(role_str).lower() == "master" if role_str is not None else False)
            )
            if is_master_bool:
                gpu_idx = _first(row, "GpuIndex", "GPU", "GpuId")
                disp_idx = _first(row, "DisplayIndex", "Display", "DisplayId")
                try:
                    state.master_display = (int(gpu_idx), int(disp_idx))
                    break
                except (TypeError, ValueError):
                    continue

    # GPU UUID — Display class or a separate GPU class. Try Display first.
    displays = raw.get("Display") or []
    if isinstance(displays, list):
        for row in displays:
            if not isinstance(row, dict):
                continue
            uuid = _first(row, "GpuUUID", "GpuUuid", "UUID")
            if uuid:
                state.gpu_uuid = str(uuid)
                break


# ---------------------------------------------------------------------------
# NVCP topology JSON — preferred source when present
# ---------------------------------------------------------------------------

# Paths to look for the NVCP "Save Logfile" output. C:\Data is the default
# the operator dropped files at; BASE_DIR\nvcp_topology.json is a
# convention that works regardless of where nvmon is installed. First match
# wins. Add more here as needed without changing the parser.
_TOPOLOGY_PATHS = (
    r"C:\Data\nvcp_topology.json",
    r"C:\Data\ViewSystemTopology.json",
)


def _read_topology_json() -> dict | None:
    """Find and parse the NVCP topology JSON, if any. Best effort."""
    import os
    from pathlib import Path

    candidates = list(_TOPOLOGY_PATHS)
    # Also check BASE_DIR/nvcp_topology.json so the operator can drop it
    # alongside the agent without touching system paths.
    try:
        candidates.append(str(config.BASE_DIR / "nvcp_topology.json"))
    except Exception:
        pass

    for path_str in candidates:
        try:
            p = Path(path_str)
            if not p.is_file():
                continue
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Tag the source path so debug logs can point at it.
                data["_source_path"] = str(p)
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _parse_topology_json(topo: dict) -> FramelockState:
    """Map an NVCP topology dump into a FramelockState.

    NVCP exports two views ("Mosaic Displays" and "Displays and graphics
    Cards") containing the same System dict. We read whichever's present
    and pull the RTX PRO Sync (server) / RTX PRO Sync (client) block —
    those keys carry both the role (in the suffix) and all the genlock
    detail we want.
    """
    state = FramelockState()
    state.raw = topo

    # Find a "System" dict — NVCP wraps it under either of two top-level
    # keys depending on the export view.
    sys_dict: dict | None = None
    if isinstance(topo.get("Displays and graphics Cards"), dict):
        sys_dict = topo["Displays and graphics Cards"].get("System")
    if sys_dict is None and isinstance(topo.get("Mosaic Displays"), list):
        first = topo["Mosaic Displays"][0] if topo["Mosaic Displays"] else None
        if isinstance(first, dict):
            sys_dict = first.get("System")
    if not isinstance(sys_dict, dict):
        return state

    # Locate the RTX PRO Sync block — its key carries the role in parens.
    sync_block: dict | None = None
    sync_role: str = ""
    for k, v in sys_dict.items():
        if not isinstance(v, dict):
            continue
        kl = k.lower()
        if "sync" in kl and ("server" in kl or "client" in kl):
            sync_block = v
            sync_role = "client" if "client" in kl else "server"
            break
    if sync_block is None:
        return state

    state.available = True

    # Connector directions — NVCP says "In" / "Out" verbatim.
    fl0 = str(sync_block.get("Framelock 0", "") or "").strip().lower()
    fl1 = str(sync_block.get("Framelock 1", "") or "").strip().lower()
    if fl0 in ("in", "out"):
        state.connector_0_direction = fl0
    if fl1 in ("in", "out"):
        state.connector_1_direction = fl1

    # source_kind from the Timing string of the active display + role.
    # "locked to the house sync signal" -> housesync
    # "locked to the frame lock sync pulse" -> upstream / not housesync
    # NVCP also surfaces "External sync signal: Present (In use)" which
    # we use as a corroborating signal for housesync on the server.
    ext_sig = str(sync_block.get("External sync signal", "") or "").lower()
    timing_str = ""
    # The GPU sub-dict's Tessera/<display name> entry has a Timing field.
    for v in sync_block.values():
        if not isinstance(v, dict):
            continue
        for vv in v.values():
            if isinstance(vv, dict) and "Timing" in vv:
                timing_str = str(vv.get("Timing") or "").lower()
                break
        if timing_str:
            break
    if "house sync" in timing_str or "in use" in ext_sig:
        state.source_kind = "housesync"
    elif "frame lock" in timing_str or "framelock" in timing_str:
        state.source_kind = "frame_lock_pulse" if sync_role == "client" else "internal"

    # GPU serial — NVCP exports it under the GPU model name dict.
    for k, v in sync_block.items():
        if not isinstance(v, dict):
            continue
        serial = v.get("Serial Number")
        if serial:
            state.sync_card_serial = str(serial).strip()
            break

    # Sync card firmware version. NVCP topology JSON exports this on
    # the sync block itself (top-level), not nested under a GPU. Field
    # name has shifted across driver releases ("Firmware Version",
    # "Firmware version", or just "Firmware") — try them all.
    for k in ("Firmware Version", "Firmware version", "Firmware", "FW Version"):
        v = sync_block.get(k)
        if v:
            state.sync_card_firmware = str(v).strip()
            break

    # master_display: NVCP doesn't directly expose a per-display master
    # index in the topology JSON the same way NVWMI's
    # FrameLockDisplayConfig.IsMaster does. For a server with a single
    # connected display, (0, 0) is the natural master. For multi-display
    # we'd need a richer source — leave as None until then.
    if sync_role == "server":
        # Walk the GPU's display children; if exactly one has Display
        # state == "Server", that's the master.
        for gv in sync_block.values():
            if not isinstance(gv, dict):
                continue
            for dk, dv in gv.items():
                if isinstance(dv, dict) and str(dv.get("Display state", "")).lower() == "server":
                    state.master_display = (0, 0)
                    break

    return state


def _coerce_direction(raw: str) -> str:
    s = raw.strip().lower()
    if "in" in s and "none" not in s:
        return "in"
    if "out" in s:
        return "out"
    return "none"


def _coerce_source_kind(raw: str) -> str:
    s = raw.strip().lower()
    if "house" in s:
        return "housesync"
    if "internal" in s:
        return "internal"
    if "external" in s and ("monitor" in s or "display" in s or "vsync" in s):
        return "vsync_external"
    if "vsync" in s:
        return "vsync_external"
    return "unknown"


# ---------------------------------------------------------------------------
# Drift detection helpers (used by qsync.enable_from_profile to verify apply)
# ---------------------------------------------------------------------------


def diff_against_profile(profile, framelock_state: FramelockState) -> dict[str, dict[str, Any]]:
    """Compare a captured profile to a live FramelockState.

    Returns a drift dict shaped as ``{field_name: {"expected": x, "actual": y}}``.
    Empty dict means everything matches. Only checks fields the profile
    explicitly carries (empty-string / None / empty-list values mean
    "no expectation; skip").
    """
    drift: dict[str, dict[str, Any]] = {}
    q = getattr(profile, "qsync", None)
    if q is None:
        return drift

    gpu_uuid_pin = getattr(q, "gpu_uuid_pin", "") or ""
    if gpu_uuid_pin and framelock_state.gpu_uuid and gpu_uuid_pin != framelock_state.gpu_uuid:
        drift["gpu_uuid"] = {"expected": gpu_uuid_pin, "actual": framelock_state.gpu_uuid}

    serial_pin = getattr(q, "sync_card_serial_pin", "") or ""
    if serial_pin and framelock_state.sync_card_serial and serial_pin != framelock_state.sync_card_serial:
        drift["sync_card_serial"] = {"expected": serial_pin, "actual": framelock_state.sync_card_serial}

    master_expected = getattr(q, "master_display", None)
    if master_expected and framelock_state.master_display:
        # Normalize tuple-ness — JSON round-trip may produce list.
        exp_t = tuple(master_expected) if not isinstance(master_expected, tuple) else master_expected
        if exp_t != framelock_state.master_display:
            drift["master_display"] = {"expected": list(exp_t), "actual": list(framelock_state.master_display)}

    c0 = getattr(q, "connector_0_direction", "") or ""
    if c0 and c0 != framelock_state.connector_0_direction:
        drift["connector_0_direction"] = {"expected": c0, "actual": framelock_state.connector_0_direction}

    c1 = getattr(q, "connector_1_direction", "") or ""
    if c1 and c1 != framelock_state.connector_1_direction:
        drift["connector_1_direction"] = {"expected": c1, "actual": framelock_state.connector_1_direction}

    sk = getattr(q, "source_kind", "") or ""
    if sk and sk != "unknown" and sk != framelock_state.source_kind and framelock_state.source_kind != "unknown":
        drift["source_kind"] = {"expected": sk, "actual": framelock_state.source_kind}

    return drift
