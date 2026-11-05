"""Output parsers for NVIDIA CLI tools.

Parses output from:
- configureMosaic.exe query / query current  (XML-ish + text)
- configureDdisplay.exe status --all          (text blocks)
- configureQsync2.exe queryAll               (key=value pairs)
- nvidia-smi --query-gpu=... --format=csv    (CSV)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import config
from .types import (
    DDState, DisplayInfo, MosaicState, MosaicOutput,
    QSyncState, GPUSyncInfo, GPUInfo,
)


# ============================================================================
# Mosaic parser
# ============================================================================

def _resolve_mosaic_outputs(
    display_ids: list[str], dd_state: DDState | None,
) -> list[MosaicOutput]:
    """Resolve Mosaic XML display IDs to GPU/display indices using DD state.

    The Mosaic XML only gives us display IDs (e.g. "0x82061085"). The DD status
    table has the mapping from NVAPI ID -> (gpu_index, display_index).
    """
    if not dd_state or not dd_state.displays:
        # No DD state available — return with display_id only, gpu/display unknown
        return [MosaicOutput(gpu=-1, display=-1, display_id=did) for did in display_ids]

    # Build lookup: normalized NVAPI ID -> DisplayInfo
    lookup: dict[str, DisplayInfo] = {}
    for d in dd_state.displays:
        # Normalize: DD status uses bare hex (e.g. "82061085"),
        # Mosaic XML uses "0x82061085" — strip prefix and compare lowercase
        norm_id = d.id.lower().lstrip("0x")
        lookup[norm_id] = d

    outputs = []
    for did in display_ids:
        norm_did = did.lower().lstrip("0x")
        if norm_did in lookup:
            d = lookup[norm_did]
            outputs.append(MosaicOutput(
                gpu=d.gpu_index,
                display=d.display_index,
                display_id=did,
            ))
        else:
            outputs.append(MosaicOutput(gpu=-1, display=-1, display_id=did))

    return outputs


def parse_mosaic_query(
    output: str, *, dd_state: DDState | None = None,
) -> MosaicState:
    """Parse configureMosaic.exe query output.

    The output typically looks like:
        Mosaic is ON
        Topology: 2 x 2
        Display Grid:
          Grid[0]: ...
          Resolution: 3840x2160
          Refresh Rate: 23.98 Hz
          ...
    Or XML from 'query current':
        <MosaicConfig>
          <Rows>2</Rows>
          <Cols>2</Cols>
          ...
        </MosaicConfig>

    We handle both text and XML formats.

    Args:
        output: Raw stdout/stderr from configureMosaic.exe.
        dd_state: Optional DD state for resolving GPU/display indices from display IDs.
    """
    state = MosaicState()

    if not output or not output.strip():
        return state

    # Try XML parse first (query current output)
    if "<" in output and ">" in output:
        xml_state = _parse_mosaic_xml(output, dd_state=dd_state)
        if xml_state.enabled:
            # Clean up temp attribute if present
            if hasattr(xml_state, "_secondary_grids"):
                del xml_state._secondary_grids
            return xml_state

    # Text-based parse
    lines = output.strip().splitlines()

    for line in lines:
        line_stripped = line.strip()

        # Check if Mosaic is ON or Active
        if re.search(r"Mosaic\s+(is\s+)?ON", line_stripped, re.IGNORECASE):
            state.enabled = True
        elif re.search(r"Mosaic\s+(is\s+)?OFF", line_stripped, re.IGNORECASE):
            state.enabled = False
            return state
        elif re.search(r"No\s+Mosaic", line_stripped, re.IGNORECASE):
            state.enabled = False
            return state

        # Topology: 2 x 2
        m = re.search(r"(\d+)\s*x\s*(\d+)", line_stripped)
        if m and "topology" in line_stripped.lower():
            state.rows = int(m.group(1))
            state.cols = int(m.group(2))

        # Resolution: 3840x2160
        m = re.search(r"Resolution[:\s]+(\d+)\s*x\s*(\d+)", line_stripped, re.IGNORECASE)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            state.per_display_resolution = (w, h)

        # Refresh Rate: 23.98 Hz
        m = re.search(r"Refresh\s*Rate[:\s]+([\d.]+)", line_stripped, re.IGNORECASE)
        if m:
            state.refresh_rate = float(m.group(1))

        # Rows/Cols explicit
        m = re.match(r"rows\s*=\s*(\d+)", line_stripped, re.IGNORECASE)
        if m:
            state.rows = int(m.group(1))
        m = re.match(r"cols\s*=\s*(\d+)", line_stripped, re.IGNORECASE)
        if m:
            state.cols = int(m.group(1))

    # Compute totals
    if state.rows and state.cols:
        state.output_count = state.rows * state.cols
        w, h = state.per_display_resolution
        if w and h:
            state.total_resolution = (w * state.cols, h * state.rows)

    return state


@dataclass
class MosaicXMLResult:
    """Full result from parsing Mosaic XML, including all grids."""
    state: MosaicState = field(default_factory=MosaicState)
    secondary_grids: list[dict] = field(default_factory=list)  # 1x1 grids


def _parse_mosaic_xml(
    output: str, *, dd_state: DDState | None = None,
) -> MosaicState:
    """Parse Mosaic XML from 'query current'.

    Real XML format:
        <query version="2.4.9">
          <grids>
            <grid rows="2" columns="2" synchronized="1">
              <displaymode width="3840" height="2160" bpp="32" freq="23" rr="24.000000" />
              <position x="0" y="0" />
              <displays>
                <display displayid="0x82061085" ... />
                <display displayid="0x82061087" ... />
                <display displayid="0x82061081" ... />
                <display displayid="0x82061083" ... />
              </displays>
            </grid>
          </grids>
        </query>

    Key: rows/columns/width/height are ATTRIBUTES, not text elements.
    A true Mosaic has a grid with rows>1 or columns>1.
    Extended desktop shows multiple 1x1 grids.
    """
    state = MosaicState()

    try:
        # Extract XML portion
        xml_start = output.find("<?xml")
        if xml_start < 0:
            xml_start = output.find("<query")
        if xml_start < 0:
            xml_start = output.find("<")
        xml_end = output.rfind(">") + 1
        if xml_start < 0 or xml_end <= xml_start:
            return state

        xml_str = output[xml_start:xml_end]
        root = ET.fromstring(xml_str)

        # Find all <grid> elements
        grids = list(root.iter("grid"))
        if not grids:
            return state

        # Find the largest grid (the Mosaic surface) — skip 1x1 grids
        best_grid = None
        best_displays = 0
        all_outputs: list[MosaicOutput] = []
        secondary_grids: list[dict] = []

        for grid in grids:
            rows = int(grid.get("rows", "1"))
            cols = int(grid.get("columns", "1"))
            num_displays = rows * cols

            # Parse display mode
            dm = grid.find("displaymode")
            width = int(dm.get("width", "0")) if dm is not None else 0
            height = int(dm.get("height", "0")) if dm is not None else 0
            rr = float(dm.get("rr", "0")) if dm is not None else 0.0

            # Parse display IDs
            displays_elem = grid.find("displays")
            display_ids = []
            if displays_elem is not None:
                for d in displays_elem.findall("display"):
                    did = d.get("displayid", "")
                    display_ids.append(did)

            if num_displays > best_displays and (rows > 1 or cols > 1):
                best_grid = grid
                best_displays = num_displays
                state.rows = rows
                state.cols = cols
                state.per_display_resolution = (width, height)
                state.refresh_rate = rr
                state.output_count = len(display_ids) if display_ids else num_displays

                # Resolve outputs with DD cross-reference
                all_outputs = _resolve_mosaic_outputs(display_ids, dd_state)
            else:
                # Track 1x1 grids as secondary
                resolved = _resolve_mosaic_outputs(display_ids, dd_state)
                secondary_grids.append({
                    "rows": rows,
                    "cols": cols,
                    "width": width,
                    "height": height,
                    "refresh_rate": rr,
                    "display_ids": display_ids,
                    "outputs": resolved,
                })

        if best_grid is not None and (state.rows > 1 or state.cols > 1):
            # True Mosaic surface found
            state.enabled = True
            state.outputs = all_outputs
            state.synchronized = best_grid.get("synchronized", "0") == "1"
            w, h = state.per_display_resolution
            if w and h:
                state.total_resolution = (w * state.cols, h * state.rows)
        else:
            # Only 1x1 grids — extended desktop, not Mosaic
            state.enabled = False

            # Still report what we found for informational purposes.
            # Use the first grid with a large display mode for info.
            for grid in grids:
                dm = grid.find("displaymode")
                if dm is not None:
                    w = int(dm.get("width", "0"))
                    h = int(dm.get("height", "0"))
                    if w >= 3840:  # Skip small secondary displays
                        state.per_display_resolution = (w, h)
                        state.refresh_rate = float(dm.get("rr", "0"))
                        break

        # Stash secondary grids on the state for profile save to use
        state._secondary_grids = secondary_grids

    except ET.ParseError:
        pass

    return state


def parse_mosaic_xml_full(
    output: str, *, dd_state: DDState | None = None,
) -> MosaicXMLResult:
    """Parse Mosaic XML and return full result including secondary grids."""
    state = _parse_mosaic_xml(output, dd_state=dd_state)
    secondary = getattr(state, "_secondary_grids", [])
    # Clean up the temporary attribute
    if hasattr(state, "_secondary_grids"):
        del state._secondary_grids
    return MosaicXMLResult(state=state, secondary_grids=secondary)


# ============================================================================
# DirectDisplay parser
# ============================================================================

def parse_dd_status(output: str) -> DDState:
    """Parse configureDdisplay.exe status --all output.

    Real output has TWO sections we need:

    1. Displays TABLE (fixed-width columns, 0-based adapter index):
       Display | Adapter | NVAPI     | OS       |      Status        | Source       | Display Name
       --------+---------+-----------+----------+--------------------+--------------+-------------
             0 |    0    |  80061082 |    41101 |   Desktop @(0,0)   | 1920 x 1080  | DP-1
             1 |    1    |  8206108b |    61400 |  Removed, Managed  | 7680 x 4320  | HELIOS-DP

       Adapter Index matches nvidia-smi GPU index (0-based).
       Status field: "Desktop @(X,Y)" or "Removed Managed" or "Removed Not Managed"

    2. PathInfo blocks with precise refresh rates:
       pathInfo 1
         ...
         {xxx} 61100 ...
         23976/1000=23.976 _SCANLINE_ORDERING_PROGRESSIVE
    """
    state = DDState()

    if not output or not output.strip():
        return state

    lines = output.strip().splitlines()

    # --- Phase 1: Parse the FIRST Displays table only ---
    # The output has multiple "Displays" sections (WinRT etc) — we want only the first.
    displays_by_target: dict[str, DisplayInfo] = {}
    displays_by_nvapi: dict[str, DisplayInfo] = {}
    found_first_displays_table = False
    in_display_section = False
    separator_count = 0
    data_started = False

    for line in lines:
        stripped = line.strip()

        # Detect the first "Displays" section header (standalone word)
        if stripped == "Displays" and not found_first_displays_table:
            in_display_section = True
            found_first_displays_table = True
            separator_count = 0
            data_started = False
            continue

        if not in_display_section:
            continue

        # Count separator lines (---+--- pattern) — data starts after the first one
        if re.match(r"^[-+\s]+$", stripped) and "+" in stripped:
            separator_count += 1
            if separator_count >= 1:
                data_started = True
            continue

        # Skip header lines (contain column names)
        if not data_started:
            continue

        # End of table on blank line or non-pipe line
        if not stripped or "|" not in stripped:
            in_display_section = False
            continue

        # Parse table data rows — columns are pipe-separated
        parts = [p.strip() for p in stripped.split("|")]
        if len(parts) >= 7:
            try:
                display_idx = parts[0].strip()
                adapter_idx = parts[1].strip()
                nvapi_id = parts[2].strip()
                os_target_id = parts[3].strip()
                status = parts[4].strip()
                source_res = parts[5].strip()
                display_name = parts[6].strip()

                d = DisplayInfo()
                d.id = nvapi_id
                d.name = display_name
                d.status = status
                d.gpu_index = int(adapter_idx) if adapter_idx.isdigit() else -1
                d.display_index = int(display_idx) if display_idx.isdigit() else -1
                d.os_target_id = os_target_id

                # Parse source resolution
                m = re.search(r"(\d+)\s*x\s*(\d+)", source_res)
                if m:
                    d.resolution = (int(m.group(1)), int(m.group(2)))

                d.connector = "DisplayPort"

                # OS target IDs are HEX (the second GPU on RENDER-01 is
                # "f1106"); key them case-insensitively so the pathInfo /
                # WinRT lookups below find them.
                displays_by_target[os_target_id.lower()] = d
                displays_by_nvapi[nvapi_id] = d
            except (ValueError, IndexError):
                pass

    # --- Phase 2: Parse pathInfo blocks for precise refresh rates + color info ---
    current_target_id = ""
    for line in lines:
        stripped = line.strip()

        # Match target info line with OS target ID
        # e.g. "{00073379-00000000} 61100 5 3 targetAvailable=true"
        # The ID is hex — "{00093632-00000000} f1106 2 0 targetAvailable=true"
        # on a second GPU. The old `\d+` never matched it, so that display
        # reported refresh 0.0 / fraction "" forever (and profile capture
        # recorded an incomplete target for it).
        m = re.match(r"\{[^}]+\}\s+([0-9A-Fa-f]+)\s+", stripped)
        if m:
            current_target_id = m.group(1).lower()

        # Match refresh rate line: "23976/1000=23.976" or "60/1=60"
        m = re.search(r"(\d+)/(\d+)=([\d.]+)", stripped)
        if m and current_target_id:
            num, den = int(m.group(1)), int(m.group(2))
            rate_float = float(m.group(3))

            if current_target_id in displays_by_target:
                d = displays_by_target[current_target_id]
                d.refresh_rate = rate_float
                d.refresh_fraction = f"{num}/{den}"

        # Match technology line for connector type
        if "_OUTPUT_TECHNOLOGY_" in stripped and current_target_id in displays_by_target:
            d = displays_by_target[current_target_id]
            if "DISPLAYPORT" in stripped:
                d.connector = "DisplayPort"
            elif "HDMI" in stripped:
                d.connector = "HDMI"
            elif "DVI" in stripped:
                d.connector = "DVI"

    # --- Phase 3: Parse WinRT Display Core State for color info ---
    # Prefer the target ID ("AdapterRelativeId (targetID) 0xf1106" /
    # "DisplayAdapterTargetId 0xf1106") to pick the display; it is unique.
    # DisplayName is only a fallback when the name is unique — a wall of
    # identical panels ("Tessera SX40" x2) otherwise routes every view's
    # WireFormat to the first display and leaves the rest at bpc 0.
    name_counts: dict[str, int] = {}
    for d in displays_by_nvapi.values():
        name_counts[d.name] = name_counts.get(d.name, 0) + 1
    current_display: DisplayInfo | None = None
    for line in lines:
        stripped = line.strip()

        m = re.match(
            r"(?:AdapterRelativeId \(targetID\)|DisplayAdapterTargetId)\s+0x([0-9A-Fa-f]+)",
            stripped,
        )
        if m:
            hit = displays_by_target.get(m.group(1).lower())
            if hit is not None:
                current_display = hit
            continue

        # Match DisplayName in WinRT section (unique names only)
        m = re.match(r"DisplayName\s+(.*)", stripped)
        if m:
            name = m.group(1).strip()
            if name_counts.get(name) == 1:
                for d in displays_by_nvapi.values():
                    if d.name == name:
                        current_display = d
                        break

        if current_display:
            m = re.match(r"BitsPerChannel\s+(\d+)", stripped)
            if m:
                current_display.bits_per_channel = int(m.group(1))

            m = re.match(r"ColorSpace\s+(\S+)", stripped)
            if m:
                current_display.color_space = m.group(1)

            m = re.match(r"PixelEncoding\s+(\S+)", stripped)
            if m:
                current_display.pixel_encoding = m.group(1)

    # Build state from parsed displays
    state.displays = list(displays_by_target.values())
    state.display_count = len(state.displays)

    # DD is active when managed displays show "Removed" (not "Desktop").
    # Exclusion rules come from [agent.displays] in nvmon.toml.
    managed = [d for d in state.displays if not config.is_excluded_display(d.name, d.resolution)]
    if managed:
        state.active = all("removed" in d.status.lower() for d in managed)
    else:
        state.active = False

    return state


# ============================================================================
# QSync parser
# ============================================================================

def parse_qsync_query_all(output: str) -> QSyncState:
    """Parse configureQsync2.exe queryAll output.

    Real output format (indented, colon-separated):
        Found one local server running at Frequency: 23.976 Hz
        House Sync Signal Present     : YES
        House Sync Signal Rate        : 23.977
        House Sync Trigger           : FALLING EDGE
        Source                        : HOUSE SYNC
        QSync Target #0(GPU) NVIDIA RTX A6000:
            GPU index             : 0
            Is synced             : NO
            Is sync signal available? : YES
            Display 0:
                Refresh Rate   : 23.975 Hz
        QSync control params:
            source        : HOUSESYNC
            trigger       : FALLING
    """
    state = QSyncState()

    if not output or not output.strip():
        return state

    state.available = True

    lines = output.strip().splitlines()
    gpu_sections: list[GPUSyncInfo] = []
    current_gpu: GPUSyncInfo | None = None
    in_control_params = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # --- Top-level frequency ---
        m = re.search(r"Frequency:\s*([\d.]+)\s*Hz", stripped, re.IGNORECASE)
        if m and not current_gpu:
            try:
                state.house_sync_incoming_hz = float(m.group(1))
            except ValueError:
                pass

        # --- House Sync Signal Present ---
        m = re.match(r"House\s+Sync\s+Signal\s+Present\s*:\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            state.house_sync_detected = m.group(1).upper() == "YES"

        # --- House Sync Signal Rate ---
        m = re.match(r"House\s+Sync\s+Signal\s+Rate\s*:\s*([\d.]+)", stripped, re.IGNORECASE)
        if m:
            try:
                state.house_sync_incoming_hz = float(m.group(1))
            except ValueError:
                pass

        # --- House Sync Trigger ---
        m = re.match(r"House\s+Sync\s+Trigger\s*:\s*(.+)", stripped, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if "FALLING" in val.upper():
                state.trigger = "FALLING"
            elif "RISING" in val.upper():
                state.trigger = "RISING"
            elif "BOTH" in val.upper():
                state.trigger = "BOTH"

        # --- Source (top-level) ---
        # configureQsync2.exe emits this line two ways across versions:
        #   "Source                : HOUSE SYNC"
        #   "House Sync Source     : HOUSE SYNC"
        # The optional "House Sync " prefix here covers both.  Without it,
        # newer-tool nodes report state.source="" and `housesync_locked`
        # evaluates False even while NVIDIA Control Panel shows the lock —
        # exactly the "QSync says off when in fact synced" bug.
        m = re.match(r"(?:House\s+Sync\s+)?Source\s*:\s*(.+)", stripped, re.IGNORECASE)
        if m and not current_gpu:
            val = m.group(1).strip().upper()
            if "HOUSE" in val:
                state.source = "HOUSESYNC"
            elif "VSYNC" in val or "INTERNAL" in val:
                state.source = "VSYNC"
            else:
                state.source = val

        # --- QSync Target (GPU section header) ---
        m = re.match(r"QSync\s+Target\s+#(\d+)\(GPU\)\s*(.*?):", stripped, re.IGNORECASE)
        if m:
            if current_gpu is not None:
                gpu_sections.append(current_gpu)
            current_gpu = GPUSyncInfo()
            in_control_params = False

        # --- GPU index (inside GPU section) ---
        m = re.match(r"GPU\s+index\s*:\s*(\d+)", stripped, re.IGNORECASE)
        if m and current_gpu is not None:
            current_gpu.gpu = int(m.group(1))

        # --- Is synced ---
        m = re.match(r"Is\s+synced\s*:\s*(\S+)", stripped, re.IGNORECASE)
        if m and current_gpu is not None:
            current_gpu.synced = m.group(1).upper() in ("YES", "TRUE", "1")

        # --- Is sync signal available ---
        m = re.match(r"Is\s+sync\s+signal\s+available\??\s*:\s*(\S+)", stripped, re.IGNORECASE)
        if m and current_gpu is not None:
            current_gpu.signal_available = m.group(1).upper() in ("YES", "TRUE", "1")

        # --- Per-display "Sync State : SERVER|CLIENT" — the ROLE the card
        #     is running, not proof of a lock. site 2026-11-02: every client
        #     showed "Sync State : CLIENT" with "Is synced : NO" and "Is
        #     sync signal available? : NO" while both timing servers were
        #     UNSYNCED — nothing was locked, yet this line used to force
        #     signal_available=True and the whole fleet went green. A real
        #     lock shows "Is synced : YES" on the CLIENT GPU (srv1 after-
        #     reboot dump, profiles/_backup/srv1-queryAll.after-reboot.txt).
        m = re.match(r"Sync\s+State\s*:\s*(\S+)", stripped, re.IGNORECASE)
        if m and current_gpu is not None:
            sync_state = m.group(1).upper()
            if sync_state in ("SERVER", "CLIENT"):
                # Surface SERVER/CLIENT so profile capture can record role.
                current_gpu.sync_state = sync_state
            if sync_state == "SERVER":
                # A display only reads SERVER while the card is actually
                # serving (an un-engaged server reads UNSYNCED — srv1,
                # 2026-11-01/02), so SERVER does imply the signal; covers
                # tool versions that drop the "Is sync signal available?" line.
                current_gpu.signal_available = True

        # --- Control params section ---
        if re.match(r"QSync\s+control\s+params", stripped, re.IGNORECASE):
            if current_gpu is not None:
                gpu_sections.append(current_gpu)
                current_gpu = None
            in_control_params = True

        # --- source / trigger in control params ---
        if in_control_params:
            m = re.match(r"source\s*:\s*(\S+)", stripped, re.IGNORECASE)
            if m:
                state.source = m.group(1).strip().upper()
            m = re.match(r"trigger\s*:\s*(\S+)", stripped, re.IGNORECASE)
            if m:
                state.trigger = m.group(1).strip().upper()

    # Don't forget the last GPU
    if current_gpu is not None:
        gpu_sections.append(current_gpu)

    state.per_gpu_sync = [g for g in gpu_sections if g.gpu >= 0]

    # Overall sync determination:
    # The CLI reports "Is synced: NO" for sync SERVER mode even when
    # displays are locked to house sync (NVIDIA Control Panel confirms lock).
    # So we use a broader check: any of the following is treated as locked.
    #   1. house_sync_detected + source=HOUSESYNC + at least one GPU has signal
    #   2. source=HOUSESYNC and at least one display reports SERVER/CLIENT
    #      sync state (we set signal_available=True when we see those above)
    #   3. house_sync_detected alone with at least one signal_available — covers
    #      tool versions that drop the "Source" line entirely.
    has_signal = any(g.signal_available for g in state.per_gpu_sync)
    has_client_state = any(g.sync_state == "CLIENT" for g in state.per_gpu_sync)
    has_server_state = any(g.sync_state == "SERVER" for g in state.per_gpu_sync)
    housesync_locked = (
        state.source in ("HOUSESYNC", "HOUSE SYNC")
        and has_signal
    ) or (
        state.house_sync_detected
        and has_signal
    )

    if housesync_locked:
        state.synced = True
        state.role = "client" if has_client_state and not has_server_state else "server"
        state.lock_source_label = "external house sync"
        state.effective_lock_hz = state.house_sync_incoming_hz
        # Compute effective per-GPU sync status.
        # In server/HOUSESYNC mode, "Is synced: NO" is misleading — the GPU
        # with signal_available is locked directly to house sync, and other
        # GPUs are locked internally via that GPU.
        signal_gpu = next(
            (g.gpu for g in state.per_gpu_sync if g.signal_available), None
        )
        for g in state.per_gpu_sync:
            if g.signal_available:
                g.effective_synced = True
                g.sync_note = "via HOUSESYNC"
            else:
                g.effective_synced = True
                g.sync_note = f"via GPU {signal_gpu}" if signal_gpu is not None else "via master"
    elif has_client_state and any(g.synced or g.signal_available for g in state.per_gpu_sync):
        # Daisy-chained client: the upstream framelock pulse arrives over
        # RJ45. "Sync State : CLIENT" alone is only the configured role —
        # the lock is real when a GPU also says "Is synced : YES" (or the
        # pulse is reported available). Source typically reports as
        # VSYNC/INTERNAL on the client (no BNC) — expected, not a problem.
        state.synced = True
        state.role = "client"
        state.lock_source_label = "upstream framelock pulse"
        # The pulse rate equals the display refresh; the parser doesn't
        # have that handy here, so leave 0.0 and let the view-model fill
        # it from dd.refresh_rate.
        state.effective_lock_hz = 0.0
        for g in state.per_gpu_sync:
            if g.synced or g.signal_available:
                g.effective_synced = True
                g.sync_note = "via framelock pulse"
            else:
                g.effective_synced = g.synced
    else:
        # Fall back to per-GPU "Is synced" field. This branch is the
        # "node is configured as a server but no signal yet" path or the
        # genuinely-unlocked case.
        synced_gpus = [g for g in state.per_gpu_sync if g.signal_available]
        if synced_gpus:
            state.synced = all(g.synced for g in synced_gpus)
        elif state.per_gpu_sync:
            state.synced = any(g.synced for g in state.per_gpu_sync)
        for g in state.per_gpu_sync:
            g.effective_synced = g.synced
        if state.synced:
            # Locked to own internal clock — operator-visible warning sign
            # (it'll show up in the dashboard's "locked-to-self" CRIT path).
            state.role = "server"
            state.lock_source_label = "internal clock"
            state.effective_lock_hz = 0.0
        else:
            state.role = ""
            state.lock_source_label = ""
            state.effective_lock_hz = 0.0

    return state


def parse_qsync_query_params(output: str) -> dict:
    """Parse configureQsync2.exe queryParams output into a simple dict."""
    result: dict[str, str] = {}
    if not output:
        return result

    for line in output.strip().splitlines():
        line_stripped = line.strip()
        # Handle both "key : value" and "key=value" formats
        m = re.match(r"(.+?)\s*:\s*(.+)", line_stripped)
        if not m:
            m = re.match(r"(\w+)\s*=\s*(.+)", line_stripped)
        if m:
            key = m.group(1).strip()
            result[key] = m.group(2).strip()

    return result


def parse_qsync_displays(output: str) -> list[tuple[int, int]]:
    """Extract every (gpu_index, display_index) tuple from queryAll output.

    Used by qsync.enable() to build `display=G,D` args dynamically — the
    non-Mosaic default sync path. The list is ordered as it appears in
    the output (first tuple = first display on first GPU = becomes the
    server when passed to configureQsync2.exe enable server).
    """
    pairs: list[tuple[int, int]] = []
    if not output or not output.strip():
        return pairs

    current_gpu: int | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # New GPU section resets the indent context.
        m = re.match(r"QSync\s+Target\s+#(\d+)\(GPU\)", stripped, re.IGNORECASE)
        if m:
            current_gpu = None  # wait for the explicit "GPU index : N" line
            continue
        m = re.match(r"GPU\s+index\s*:\s*(\d+)", stripped, re.IGNORECASE)
        if m:
            current_gpu = int(m.group(1))
            continue
        # Per-display "Display index : N" line. Only counts inside a GPU section.
        m = re.match(r"Display\s+index\s*:\s*(\d+)", stripped, re.IGNORECASE)
        if m and current_gpu is not None:
            pairs.append((current_gpu, int(m.group(1))))

    return pairs


# ============================================================================
# nvidia-smi parser
# ============================================================================

# Field order must match the query string in gpu.py
_SMI_FIELDS = [
    "index", "name", "pci_bus_id", "driver_version", "temperature_gpu",
    "power_draw", "power_limit", "utilization_gpu", "memory_used", "memory_total",
    "clocks_graphics", "clocks_memory", "clocks_video",
    "pstate", "display_active", "fan_speed", "pcie_gen", "pcie_width",
]


def parse_nvidia_smi_csv(output: str) -> list[GPUInfo]:
    """Parse nvidia-smi CSV output (noheader, nounits).

    Each line is one GPU with comma-separated values.
    """
    gpus: list[GPUInfo] = []
    if not output or not output.strip():
        return gpus

    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 10:
            continue

        def _int(s: str, default: int = 0) -> int:
            try:
                return int(float(s))
            except (ValueError, TypeError):
                return default

        def _float(s: str, default: float = 0.0) -> float:
            try:
                return float(s)
            except (ValueError, TypeError):
                return default

        def _get(idx: int) -> str:
            return parts[idx].strip() if idx < len(parts) else ""

        gpu = GPUInfo(
            index=_int(_get(0)),
            name=_get(1),
            pci_bus_id=_get(2),
            driver_version=_get(3),
            temperature_c=_int(_get(4)),
            power_draw_w=_float(_get(5)),
            power_limit_w=_float(_get(6)),
            gpu_utilization_pct=_int(_get(7)),
            memory_used_mb=_int(_get(8)),
            memory_total_mb=_int(_get(9)),
            gpu_clock_mhz=_int(_get(10)),
            mem_clock_mhz=_int(_get(11)),
            video_clock_mhz=_int(_get(12)),
            pstate=_get(13),
            display_active=_get(14).lower() in ("enabled", "true", "1", "yes"),
            fan_speed_pct=_int(_get(15)),
            pcie_gen=_int(_get(16)),
            pcie_width=_int(_get(17)),
        )
        gpus.append(gpu)

    return gpus
