"""v0.9.3 — colour depth visible and verified: validator row, fleet-view
text, EDID capability decode."""

from __future__ import annotations

from nvmon.agent import edid, validator
from nvmon.controller.dashboard_unified.view_model import _color_text
from nvmon.types import DDState, DisplayInfo, Profile


def _profile(bits: int = 10) -> Profile:
    return Profile(
        name=f"60 - {bits}b - SyncHost",
        display_ids={
            "80061085": {"name": "Tessera SX40", "bits_per_channel": bits, "pixel_encoding": "Ycc422", "color_space": "BT709"},
            "82061085": {"name": "Tessera SX40", "bits_per_channel": bits, "pixel_encoding": "Ycc422", "color_space": "BT709"},
        },
    )


def _dd(bits: int = 10, enc: str = "Ycc422") -> DDState:
    st = DDState(active=False)
    for i in ("80061085", "82061085"):
        st.displays.append(DisplayInfo(id=i, name="Tessera SX40", status="Desktop @(0,0)", refresh_rate=60.0,
                                       resolution=(3840, 2160), bits_per_channel=bits, pixel_encoding=enc))
    st.display_count = 2
    return st


# --- validator ---------------------------------------------------------------


def test_color_depth_passes_when_live_matches_profile():
    c = validator._check_color_depth(_dd(10), _profile(10))
    assert c.passed is True
    assert c.detail == "10-bit Ycc422 on 2 display(s)"


def test_color_depth_fails_when_panels_stayed_at_10_under_12b_profile():
    # site 2026-11-04: driver refused 12-bit, panels stayed 10-bit.
    c = validator._check_color_depth(_dd(10), _profile(12))
    assert c.passed is False
    assert "display 80061085: 10-bit Ycc422 (profile expects 12-bit Ycc422)" in c.detail
    assert "82061085" in c.detail


def test_color_depth_fails_on_encoding_drift():
    c = validator._check_color_depth(_dd(10, "Rgb444"), _profile(10))
    assert c.passed is False and "Rgb444" in c.detail and "Ycc422" in c.detail


def test_color_depth_accepts_encoding_spelling_variants():
    c = validator._check_color_depth(_dd(10, "YCbCr422"), _profile(10))
    assert c.passed is True


def test_color_depth_skipped_without_profile_or_colour():
    assert "skipped" in validator._check_color_depth(_dd(), None).detail
    assert "skipped" in validator._check_color_depth(_dd(), Profile(name="x")).detail
    # Windows reports nothing (rig without the WinRT colour block) -> skipped, not red
    c = validator._check_color_depth(_dd(0, ""), _profile(10))
    assert c.passed is True and "skipped" in c.detail


def test_color_depth_inherits_sibling_colour_for_legacy_profile():
    p = _profile(10)
    p.display_ids["82061085"].update({"bits_per_channel": 0, "pixel_encoding": "", "color_space": ""})
    assert validator._check_color_depth(_dd(10), p).passed is True
    assert validator._check_color_depth(_dd(8, "Rgb444"), p).passed is False


def test_validate_all_carries_color_depth_row():
    from nvmon.types import MosaicState, QSyncState
    res = validator.validate_all(dd=_dd(10), mosaic=MosaicState(), qsync=QSyncState(), gpus=[], profile=_profile(12))
    assert "color_depth" in res.checks and res.checks["color_depth"].passed is False
    assert res.all_passed is False


# --- DP link -------------------------------------------------------------------


def _link(cur="HBR3 8.10 x2", mx="HBR3 8.10 x4", cur_rate=30, max_rate=30, payload=12.96, is_dp=True):
    return {"current_link": cur, "max_link": mx, "current_payload_gbps": payload,
            "raw": {"curLinkRate": cur_rate, "maxLinkRate": max_rate}, "flags": {"is_dp": is_dp}}


def test_dp_link_reports_reduced_link_without_failing(monkeypatch):
    # The driver may train the minimum lanes for the mode; informational only.
    from nvmon.agent import nvapi
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: _link())
    c = validator._check_dp_link(_dd())
    assert c.passed is True
    assert c.detail == "HBR3 8.10 x2 (12.96 Gbit/s), sink offers HBR3 8.10 x4 on 2 display(s)"
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: _link(cur="HBR2 5.40 x4", cur_rate=20, payload=17.28))
    assert validator._check_dp_link(_dd()).passed is True


def test_dp_link_green_at_full_link_and_skipped_without_data(monkeypatch):
    from nvmon.agent import nvapi
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: _link(cur="HBR3 8.10 x4", payload=25.92))
    c = validator._check_dp_link(_dd())
    assert c.passed is True and c.detail == "HBR3 8.10 x4 (25.92 Gbit/s) on 2 display(s)"
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: None)
    c = validator._check_dp_link(_dd())
    assert c.passed is True and "skipped" in c.detail
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: _link(is_dp=False))
    assert "skipped" in validator._check_dp_link(_dd()).detail


def test_link_bandwidth_math_4k60_on_two_lanes():
    from nvmon.agent import nvapi
    m = nvapi.link_bandwidth_math(594_000_000, 12.96)
    by = {r["format"]: r for r in m["formats"]}
    assert by["10-bit YCbCr422"]["needed_gbps"] == 11.88 and by["10-bit YCbCr422"]["fits"]
    assert by["12-bit YCbCr422"]["needed_gbps"] == 14.26 and not by["12-bit YCbCr422"]["fits"]
    assert by["8-bit RGB444"]["needed_gbps"] == 14.26 and not by["8-bit RGB444"]["fits"]
    assert by["12-bit YCbCr420"]["needed_gbps"] == 10.69 and by["12-bit YCbCr420"]["fits"]
    m4 = nvapi.link_bandwidth_math(594_000_000, 25.92)
    assert "12-bit YCbCr444" in m4["fits"]
    m30 = nvapi.link_bandwidth_math(296_340_000, 12.96)
    assert "12-bit YCbCr422" in m30["fits"] and m30["pixel_clock_mhz"] == 296.34


# --- fleet view --------------------------------------------------------------


def test_color_text_summarises_wire_format():
    status = {"direct_display": {"displays": [
        {"bits_per_channel": 10, "pixel_encoding": "Ycc422"},
        {"bits_per_channel": 10, "pixel_encoding": "Ycc422"},
    ]}}
    assert _color_text(status) == "10-bit Ycc422"
    status["direct_display"]["displays"][1] = {"bits_per_channel": 8, "pixel_encoding": "Rgb444"}
    assert _color_text(status) == "10-bit Ycc422 · 8-bit Rgb444"
    assert _color_text({"direct_display": {"displays": [{"bits_per_channel": 0, "pixel_encoding": ""}]}}) == ""
    assert _color_text(None) == ""


# --- EDID capabilities -------------------------------------------------------


def _edid(bit_depth_code: int, enc_bits: int, *, cta: bytes | None = None) -> bytes:
    base = bytearray(128)
    base[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    base[8:10] = bytes([0x0A, 0x74])          # "BRT"
    base[18], base[19] = 1, 4
    base[0x14] = 0x80 | (bit_depth_code << 4) | 0x05   # digital, depth, DisplayPort
    base[0x18] = enc_bits << 3
    base[126] = 1 if cta else 0
    blob = bytes(base)
    if cta:
        blob += cta
    return blob


def test_edid_capabilities_bit_depth_and_encodings():
    caps = edid.parse_edid_capabilities(_edid(3, 0b10))
    assert caps["digital"] and caps["interface"] == "DisplayPort"
    assert caps["bit_depth"] == 10
    assert caps["encodings"] == ["RGB444", "YCbCr422"]
    caps12 = edid.parse_edid_capabilities(_edid(4, 0b11))
    assert caps12["bit_depth"] == 12 and caps12["encodings"] == ["RGB444", "YCbCr444", "YCbCr422"]


def test_edid_capabilities_cta_block_flags():
    cta = bytearray(128)
    cta[0], cta[1], cta[2] = 0x02, 0x03, 4 + 8   # tag, revision, DTD offset after one 7-byte VSDB (+hdr)
    cta[3] = 0x30                                # YCbCr444 + YCbCr422
    # HDMI VSDB: tag 3, length 7; OUI 00-0C-03, phys addr 1.0.0.0, flags DC_30|DC_36|DC_Y444
    cta[4] = (3 << 5) | 7
    cta[5:8] = b"\x03\x0c\x00"
    cta[8:10] = b"\x10\x00"
    cta[10] = 0x10 | 0x20 | 0x08
    caps = edid.parse_edid_capabilities(_edid(3, 0b10, cta=bytes(cta)))
    assert caps["cta"]["present"] and caps["cta"]["ycbcr444"] and caps["cta"]["ycbcr422"]
    assert caps["cta"]["hdmi_deep_color_bits"] == [30, 36, "Y444"]
    assert edid.parse_edid(_edid(3, 0b10, cta=bytes(cta)))["capabilities"]["bit_depth"] == 10
