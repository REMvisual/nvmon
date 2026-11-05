"""Tests for nvmon.parsers using captured output from real NVIDIA CLI tools.

Fixtures live in tests/fixtures/nvdd_output/:
    dd_status.txt              - configureDdisplay.exe status --all
    mosaic_query.txt           - configureMosaic.exe query (empty/inactive)
    mosaic_query_current.txt   - configureMosaic.exe query current (Mosaic XML)
    qsync_query_all.txt        - configureQsync2.exe queryAll
    nvidia_smi.txt             - nvidia-smi --query-gpu=... --format=csv,noheader,nounits
"""

from __future__ import annotations

import pytest

from nvmon.parsers import (
    parse_dd_status,
    parse_mosaic_query,
    parse_mosaic_xml_full,
    parse_qsync_displays,
    parse_qsync_query_all,
    parse_nvidia_smi_csv,
)
from nvmon.types import DDState, MosaicState, QSyncState, GPUInfo


# ---------------------------------------------------------------------------
# DirectDisplay
# ---------------------------------------------------------------------------

def test_parse_dd_status_returns_ddstate(load_fixture):
    text = load_fixture("dd_status.txt")
    state = parse_dd_status(text)

    assert isinstance(state, DDState)
    assert state.display_count >= 2

    # Expected entries (from the captured fixture):
    #   DDX-101 on adapter 0 (Desktop @(0,0))
    #   HELIOS-DP on adapter 1 (Removed Managed)
    names = {d.name for d in state.displays}
    assert "DDX-101" in names
    assert "HELIOS-DP" in names


def test_parse_dd_status_extracts_adapter_indices(load_fixture):
    text = load_fixture("dd_status.txt")
    state = parse_dd_status(text)

    by_name = {d.name: d for d in state.displays}
    assert by_name["DDX-101"].gpu_index == 0
    assert by_name["HELIOS-DP"].gpu_index == 1


def test_parse_dd_status_extracts_nvapi_ids(load_fixture):
    text = load_fixture("dd_status.txt")
    state = parse_dd_status(text)

    by_name = {d.name: d for d in state.displays}
    assert by_name["DDX-101"].id == "80061082"
    assert by_name["HELIOS-DP"].id == "8206108b"


def test_parse_dd_status_extracts_resolution(load_fixture):
    text = load_fixture("dd_status.txt")
    state = parse_dd_status(text)

    by_name = {d.name: d for d in state.displays}
    assert by_name["DDX-101"].resolution == (1920, 1080)
    assert by_name["HELIOS-DP"].resolution == (7680, 4320)


def test_parse_dd_status_extracts_status_text(load_fixture):
    text = load_fixture("dd_status.txt")
    state = parse_dd_status(text)

    by_name = {d.name: d for d in state.displays}
    assert "Desktop" in by_name["DDX-101"].status
    assert "Removed" in by_name["HELIOS-DP"].status


_DD_STATUS_HEX_TARGET = """\
Displays
  Display | Adapter |   NVAPI   |    OS    |                                    |    Source    |
   Index  |  Index  | DisplayId | TargetId |               Status               |  Resolution  | Display Name
  --------+---------+-----------+----------+------------------------------------+--------------+-------------
        0 |    0    |  80061085 |    11106 |         Desktop @(3840,0)          | 3840 x 2160  | Tessera SX40
        1 |    1    |  82061085 |    f1106 |           Desktop @(0,0)           | 3840 x 2160  | Tessera SX40

QueryDisplayConfig info:
  PathInfos:
    pathInfo 0
      .sourceInfo:
        {00093632-00000000} 0 _INVALID 1 DISPLAYCONFIG_SOURCE_IN_USE
      .targetInfo:
        {00093632-00000000} f1106 2 0 targetAvailable=true
        _OUTPUT_TECHNOLOGY_DISPLAYPORT_EXTERNAL _ROTATION_IDENTITY _SCALING_IDENTITY
        23976/1000=23.976 _SCANLINE_ORDERING_PROGRESSIVE
    pathInfo 1
      .sourceInfo:
        {00100083-00000000} 0 _INVALID 4 DISPLAYCONFIG_SOURCE_IN_USE
      .targetInfo:
        {00100083-00000000} 11106 5 3 targetAvailable=true
        _OUTPUT_TECHNOLOGY_DISPLAYPORT_EXTERNAL _ROTATION_IDENTITY _SCALING_IDENTITY
        59940/1000=59.94 _SCANLINE_ORDERING_PROGRESSIVE

Info from winrt::Windows::Devices::Display::Core
  Displays
    Target[6]
      AdapterRelativeId (targetID)    0x11106
      Monitor
        DisplayAdapterTargetId        0x11106
        DisplayName                   Tessera SX40
    Target[18]
      AdapterRelativeId (targetID)    0xf1106
      Monitor
        DisplayAdapterTargetId        0xf1106
        DisplayName                   Tessera SX40

  Current State
      state.Views()
        View[0]
          Path
            Target
              AdapterRelativeId (targetID)          0xf1106
              Monitor
                DisplayAdapterTargetId              0xf1106
                DisplayName                         Tessera SX40
            WireFormat
              BitsPerChannel                        8
              ColorSpace                            BT2020
              PixelEncoding                         Rgb444
        View[1]
          Path
            Target
              AdapterRelativeId (targetID)          0x11106
              Monitor
                DisplayAdapterTargetId              0x11106
                DisplayName                         Tessera SX40
            WireFormat
              BitsPerChannel                        10
              ColorSpace                            BT709
              PixelEncoding                         Ycc422
"""


def test_parse_dd_status_hex_target_id_gets_refresh_and_color():
    """Regression (RENDER-01, 2026-11-01): the GPU-1 panel's OS target ID
    is hex ("f1106"). The pathInfo regex only accepted decimal, so that
    display reported refresh 0.0 forever — and both panels share the name
    "Tessera SX40", so the WinRT colour block was always credited to the
    first one (second stayed at bpc 0)."""
    state = parse_dd_status(_DD_STATUS_HEX_TARGET)
    by_id = {d.id: d for d in state.displays}
    assert set(by_id) == {"80061085", "82061085"}

    gpu0 = by_id["80061085"]
    assert gpu0.refresh_rate == pytest.approx(59.94)
    assert gpu0.refresh_fraction == "59940/1000"
    assert gpu0.bits_per_channel == 10
    assert gpu0.color_space == "BT709"
    assert gpu0.pixel_encoding == "Ycc422"

    gpu1 = by_id["82061085"]
    assert gpu1.os_target_id == "f1106"
    assert gpu1.refresh_rate == pytest.approx(23.976)
    assert gpu1.refresh_fraction == "23976/1000"
    assert gpu1.bits_per_channel == 8
    assert gpu1.color_space == "BT2020"
    assert gpu1.pixel_encoding == "Rgb444"


def test_parse_dd_status_empty_input():
    state = parse_dd_status("")
    assert isinstance(state, DDState)
    assert state.display_count == 0
    assert state.displays == []


# ---------------------------------------------------------------------------
# Mosaic
# ---------------------------------------------------------------------------

def test_parse_mosaic_query_inactive(load_fixture):
    # mosaic_query.txt is an empty grid set - Mosaic not active.
    text = load_fixture("mosaic_query.txt")
    state = parse_mosaic_query(text)

    assert isinstance(state, MosaicState)
    assert state.enabled is False


def test_parse_mosaic_query_current_active(load_fixture):
    text = load_fixture("mosaic_query_current.txt")
    state = parse_mosaic_query(text)

    assert isinstance(state, MosaicState)
    assert state.enabled is True
    assert state.rows == 2
    assert state.cols == 2
    assert state.per_display_resolution == (3840, 2160)
    assert state.total_resolution == (7680, 4320)
    assert state.refresh_rate == pytest.approx(23.976, abs=0.01)
    assert state.output_count == 4
    assert state.synchronized is True


def test_parse_mosaic_xml_full_returns_secondary_grids(load_fixture):
    text = load_fixture("mosaic_query_current.txt")
    result = parse_mosaic_xml_full(text)

    assert result.state.enabled is True
    # mosaic_query_current.txt has one 1x1 grid + one 2x2 grid.
    assert len(result.secondary_grids) == 1
    sec = result.secondary_grids[0]
    assert sec["rows"] == 1
    assert sec["cols"] == 1
    assert sec["width"] == 1920
    assert sec["height"] == 1080


def test_parse_mosaic_query_empty_returns_disabled():
    state = parse_mosaic_query("")
    assert state.enabled is False


# ---------------------------------------------------------------------------
# QSync
# ---------------------------------------------------------------------------

def test_parse_qsync_query_all_basic(load_fixture):
    text = load_fixture("qsync_query_all.txt")
    state = parse_qsync_query_all(text)

    assert isinstance(state, QSyncState)
    assert state.available is True
    assert state.house_sync_detected is True
    assert state.house_sync_incoming_hz == pytest.approx(23.976, abs=0.01)
    assert state.source.upper().startswith("HOUSE")
    assert state.trigger == "FALLING"


def test_parse_qsync_query_all_has_gpus(load_fixture):
    text = load_fixture("qsync_query_all.txt")
    state = parse_qsync_query_all(text)

    # Fixture has QSync Target #0 (GPU 0) and QSync Target #1 (GPU 1).
    assert len(state.per_gpu_sync) == 2

    gpu_indices = {g.gpu for g in state.per_gpu_sync}
    assert 0 in gpu_indices
    assert 1 in gpu_indices


def test_parse_qsync_query_all_effective_sync_logic(load_fixture):
    """HOUSESYNC mode: GPU with signal_available is effective_synced via HOUSESYNC.
    GPUs without signal_available are effective_synced via the master GPU."""
    text = load_fixture("qsync_query_all.txt")
    state = parse_qsync_query_all(text)

    # In the fixture, GPU 0 has signal_available=YES, GPU 1 has NO.
    # Because source=HOUSESYNC + house_sync_detected, all GPUs should be
    # effective_synced.
    by_gpu = {g.gpu: g for g in state.per_gpu_sync}
    assert by_gpu[0].signal_available is True
    assert by_gpu[0].effective_synced is True
    assert "HOUSESYNC" in by_gpu[0].sync_note

    assert by_gpu[1].signal_available is False
    assert by_gpu[1].effective_synced is True  # locked internally via master


def test_parse_qsync_query_all_synced_top_level(load_fixture):
    text = load_fixture("qsync_query_all.txt")
    state = parse_qsync_query_all(text)

    # Top-level synced should be True when house sync is detected and locked.
    assert state.synced is True


def test_parse_qsync_empty_returns_defaults():
    state = parse_qsync_query_all("")
    assert isinstance(state, QSyncState)
    assert state.available is False


def test_parse_qsync_house_sync_source_variant(load_fixture):
    """Some configureQsync2.exe versions emit "House Sync Source : HOUSE SYNC"
    instead of plain "Source : HOUSE SYNC". The previous parser used
    re.match with the line anchored to start which silently dropped the
    prefixed form, leaving state.source="" and breaking lock detection.
    """
    text = load_fixture("qsync_query_all_house_sync_source.txt")
    state = parse_qsync_query_all(text)

    assert state.house_sync_detected is True
    assert state.source == "HOUSESYNC"
    # GPU 0 has signal_available=YES; combined with HOUSESYNC + house_sync,
    # the system should report synced=True even though "Is synced: NO".
    assert state.synced is True


def test_parse_qsync_sync_state_server_implies_signal_available():
    """A "Sync State : SERVER" line on any display flips the GPU's
    signal_available, even without an explicit "Is sync signal available?"
    line — covers tool versions that drop it."""
    text = """
QSync topology:
\tHouse Sync Signal Present     : YES
\tHouse Sync Source             : HOUSE SYNC
\tQSync Target #0(GPU) NVIDIA RTX A6000:
\t\tGPU index             : 0
\t\tIs synced             : NO
\t\tDisplay 0:
\t\t\tSync State     : SERVER
\t\t\tRefresh Rate   : 23.976 Hz
"""
    state = parse_qsync_query_all(text)
    assert state.house_sync_detected is True
    assert state.source == "HOUSESYNC"
    assert any(g.signal_available for g in state.per_gpu_sync)
    assert state.synced is True


# ---------------------------------------------------------------------------
# QSync display discovery (used by qsync.enable() default path)
# ---------------------------------------------------------------------------

def test_parse_qsync_displays_extracts_all_pairs(load_fixture):
    """The fixture has GPU 0 with 4 displays + GPU 1 with 1 display."""
    text = load_fixture("qsync_query_all.txt")
    pairs = parse_qsync_displays(text)
    assert pairs == [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0)]


def test_parse_qsync_displays_empty_input_returns_empty_list():
    assert parse_qsync_displays("") == []
    assert parse_qsync_displays("   \n\n   ") == []


def test_parse_qsync_displays_ignores_display_lines_outside_gpu_section():
    """A 'Display index : 7' line in a section before any GPU header is
    discarded — discovery only counts displays attached to a known GPU."""
    text = """
QSync topology:
\tHouse Sync Signal Present     : YES
\tDisplay index : 7
\tQSync Target #0(GPU) NVIDIA RTX A6000:
\t\tGPU index             : 0
\t\tDisplay 0:
\t\t\tDisplay index  : 0
"""
    pairs = parse_qsync_displays(text)
    assert pairs == [(0, 0)]


# ---------------------------------------------------------------------------
# nvidia-smi
# ---------------------------------------------------------------------------

def test_parse_nvidia_smi_csv_returns_gpu_list(load_fixture):
    text = load_fixture("nvidia_smi.txt")
    gpus = parse_nvidia_smi_csv(text)

    assert isinstance(gpus, list)
    assert len(gpus) == 2
    assert all(isinstance(g, GPUInfo) for g in gpus)


def test_parse_nvidia_smi_csv_extracts_fields(load_fixture):
    text = load_fixture("nvidia_smi.txt")
    gpus = parse_nvidia_smi_csv(text)

    g0 = gpus[0]
    assert g0.index == 0
    assert "A6000" in g0.name
    assert "41:00.0" in g0.pci_bus_id
    assert g0.driver_version == "573.48"
    assert g0.temperature_c == 69
    assert g0.power_draw_w == pytest.approx(92.0, abs=0.1)
    assert g0.power_limit_w == pytest.approx(300.0, abs=0.1)
    assert g0.memory_total_mb == 49140
    assert g0.pstate == "P0"
    assert g0.display_active is True

    g1 = gpus[1]
    assert g1.index == 1
    assert g1.temperature_c == 68


def test_parse_nvidia_smi_csv_empty_returns_empty_list():
    assert parse_nvidia_smi_csv("") == []


def test_parse_nvidia_smi_csv_ignores_short_rows():
    # Row has too few columns - should be skipped silently.
    text = "0, NVIDIA RTX A6000, 00000000:41:00.0\n"
    assert parse_nvidia_smi_csv(text) == []
