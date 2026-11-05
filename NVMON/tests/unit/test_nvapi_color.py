"""nvmon.agent.nvapi — colour depth / pixel encoding (pure parts; no NVAPI calls).

Background (site 2026-11-03): after a "60 - 10b" apply six of seven Tessera
SX40 nodes sat at 8-bit RGB444 although every profile says 10-bit Ycc422.
The apply pipeline never called the colour module, and the module itself
could not have worked (wrong function ids, wrong struct size, bare-name DLL
load). These tests pin the struct layout the driver accepts and the planning
logic that turns a profile into per-display requests.
"""

from __future__ import annotations

import ctypes

from nvmon.agent import nvapi, profiles
from nvmon.types import Profile


# --- struct layout -----------------------------------------------------------


def test_color_data_v5_matches_nvapi_h():
    # nvapi.h: NvU32 version, NvU16 size, NvU8 cmd, then a struct of three
    # NvU8 + three 4-byte enums -> 24 bytes, data at offset 8, version 0x50018.
    assert ctypes.sizeof(nvapi.NV_COLOR_DATA_V5) == 24
    assert nvapi.NV_COLOR_DATA_V5.data.offset == 8
    assert nvapi._NV_COLOR_DATA_V5_DATA.bpc.offset == 4
    assert nvapi.NV_COLOR_DATA_V5_VER == 0x50018


def test_struct_round_trip_keeps_zero_values():
    want = nvapi.ColorData(color_format=0, colorimetry=0, dynamic_range=0, bpc=2,
                           color_selection_policy=1, desktop_color_depth=1)
    st = nvapi._to_struct(nvapi.NV_COLOR_CMD_SET, want)
    assert (st.version, st.size, st.cmd) == (0x50018, 24, 2)
    assert nvapi._from_struct(st) == want


def test_unset_fields_become_default_or_auto_in_struct():
    st = nvapi._to_struct(nvapi.NV_COLOR_CMD_SET, nvapi.ColorData(bpc=nvapi.NV_BPC_10))
    assert st.data.colorFormat == nvapi.NV_COLOR_FORMAT_DEFAULT
    assert st.data.colorimetry == nvapi.NV_COLORIMETRY_DEFAULT
    assert st.data.dynamicRange == nvapi.NV_DYNAMIC_RANGE_AUTO
    assert st.data.bpc == nvapi.NV_BPC_10
    assert st.data.colorSelectionPolicy == nvapi.NV_COLOR_SELECTION_POLICY_DEFAULT


def test_displayport_info_struct_and_flag_decode():
    assert ctypes.sizeof(nvapi.NV_DISPLAY_PORT_INFO_V2) == 44
    assert nvapi.NV_DISPLAY_PORT_INFO_V2_VER == 0x2002C
    # Probed on 596.86 against an 8-bit DP monitor (HBR2 x4, RGB 8 bpc):
    f = nvapi.decode_dp_flags(0x0001BE1B)
    assert f["is_dp"] and f["bpc6"] and f["bpc8"] and not f["bpc10"] and not f["bpc12"]
    assert f["ycbcr422"] and f["ycbcr444"] and not f["ycbcr420"]
    assert f["rgb444_on_current_mode"] and f["ycbcr444_on_current_mode"] and f["ycbcr422_on_current_mode"]
    assert f["bpc8_on_current_mode"] and not f["bpc10_on_current_mode"] and not f["bpc12_on_current_mode"]


def test_describe_displays_includes_link_when_available(monkeypatch):
    live = nvapi.ColorData(color_format=1, colorimetry=2, dynamic_range=1, bpc=3, color_selection_policy=0)
    monkeypatch.setattr(nvapi, "get_color_data", lambda did: live)
    monkeypatch.setattr(nvapi, "get_default_color_data", lambda did: None)
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: {"current_link": "HBR2 5.40 x4", "sink_supports_bpc": [8, 10]})
    out = nvapi.describe_displays([(0x80061085, "80061085")])
    assert out[0]["link"]["current_link"] == "HBR2 5.40 x4"
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: None)
    assert "link" not in nvapi.describe_displays([(0x80061085, "80061085")])[0]


# --- ColorData ---------------------------------------------------------------


def test_from_dict_treats_zero_as_a_value_not_as_missing():
    # RGB (0), full range (0), policy USER (0) are real values.
    cd = nvapi.ColorData.from_dict({"color_format": 0, "bpc": 3, "dynamic_range": 0})
    assert cd.color_format == 0 and cd.bpc == 3 and cd.dynamic_range == 0
    assert cd.colorimetry == -1
    assert nvapi.ColorData.from_dict(None).is_empty()
    assert nvapi.ColorData.from_dict({"bpc": "", "color_format": None}).is_empty()


def test_describe_and_bits():
    cd = nvapi.ColorData(color_format=1, colorimetry=2, dynamic_range=1, bpc=3, color_selection_policy=0)
    assert cd.bits() == 10
    assert cd.describe() == "10-bit YCbCr422 BT709 limited (user)"


def test_satisfied_by_compares_only_specified_fields():
    want = nvapi.ColorData(color_format=1, bpc=3)
    live_ok = nvapi.ColorData(color_format=1, colorimetry=2, dynamic_range=1, bpc=3, color_selection_policy=0)
    live_rgb8 = nvapi.ColorData(color_format=0, colorimetry=0, dynamic_range=0, bpc=2, color_selection_policy=1)
    assert want.satisfied_by(live_ok)
    assert not want.satisfied_by(live_rgb8)
    assert nvapi.ColorData(color_format=1, bpc=3, colorimetry=2).satisfied_by(live_ok)
    assert not nvapi.ColorData(color_format=1, bpc=3, colorimetry=9).satisfied_by(live_ok)


# --- strings -> enums (what capture stores from the Windows wire format) -----


def test_windows_wire_format_strings_map_to_nvapi_enums():
    cd = nvapi.color_data_from_strings(pixel_encoding="Ycc422", bits_per_channel=10, color_space="BT709")
    assert cd.color_format == nvapi.NV_COLOR_FORMAT_YUV422
    assert cd.bpc == nvapi.NV_BPC_10
    assert cd.colorimetry == nvapi.NV_COLORIMETRY_YCC709

    rgb = nvapi.color_data_from_strings(pixel_encoding="Rgb444", bits_per_channel=8, color_space="BT709")
    assert rgb.color_format == nvapi.NV_COLOR_FORMAT_RGB
    assert rgb.bpc == nvapi.NV_BPC_8
    assert rgb.colorimetry == nvapi.NV_COLORIMETRY_RGB      # BT709 primaries, RGB encoding

    assert nvapi.color_data_from_strings(pixel_encoding="", bits_per_channel=0, color_space="").is_empty()
    odd = nvapi.color_data_from_strings(pixel_encoding="Ycc444", bits_per_channel=7,
                                        color_space="ProfileDefinedWideColorGamut")
    assert odd.color_format == nvapi.NV_COLOR_FORMAT_YUV444
    assert odd.bpc == -1 and odd.colorimetry == -1


# --- merge logic for SET -----------------------------------------------------


def test_merge_fills_unset_from_live_and_forces_user_policy():
    live = nvapi.ColorData(color_format=0, colorimetry=0, dynamic_range=0, bpc=2, color_selection_policy=1,
                           desktop_color_depth=1)
    want = nvapi.ColorData(color_format=1, bpc=3, colorimetry=2)
    req = nvapi._merge_request(want, live)
    assert (req.color_format, req.bpc, req.colorimetry) == (1, 3, 2)
    assert req.color_selection_policy == nvapi.NV_COLOR_SELECTION_POLICY_USER
    # RGB -> YCbCr: range left to the driver unless the profile says.
    assert req.dynamic_range == nvapi.NV_DYNAMIC_RANGE_AUTO
    assert req.desktop_color_depth == nvapi.NV_DESKTOP_COLOR_DEPTH_DEFAULT


def test_merge_keeps_live_range_when_format_unchanged():
    live = nvapi.ColorData(color_format=1, colorimetry=2, dynamic_range=1, bpc=2, color_selection_policy=0)
    req = nvapi._merge_request(nvapi.ColorData(color_format=1, bpc=3), live)
    assert req.dynamic_range == 1 and req.colorimetry == 2


# --- SET with IS_SUPPORTED preflight -----------------------------------------


class _FakeDriver:
    """Stands in for NvAPI_Disp_ColorControl: one display, scripted answers."""

    def __init__(self, live: nvapi.ColorData, *, supported=None, refuse_first=False):
        self.live = live
        self.supported = supported          # callable(ColorData) -> bool, or None = everything
        self.refuse_first = refuse_first
        self.sets: list[nvapi.ColorData] = []
        self.probes: list[nvapi.ColorData] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(nvapi, "get_color_data", lambda did: self.live)
        monkeypatch.setattr(nvapi, "is_supported", self._is_supported)
        monkeypatch.setattr(nvapi, "_call", self._call)

    def _is_supported(self, did, cd):
        self.probes.append(cd)
        return True if self.supported is None else self.supported(cd)

    def _call(self, did, cmd, data=None):
        assert cmd == nvapi.NV_COLOR_CMD_SET
        self.sets.append(data)
        if self.refuse_first and len(self.sets) == 1:
            return -1, data
        self.live = nvapi.ColorData(**data.to_dict())
        return 0, data


_LIVE_8RGB = nvapi.ColorData(color_format=0, colorimetry=0, dynamic_range=0, bpc=2,
                             color_selection_policy=1, desktop_color_depth=1)


def test_set_color_data_sets_once_and_verifies(monkeypatch):
    drv = _FakeDriver(_LIVE_8RGB)
    drv.install(monkeypatch)
    want = nvapi.color_data_from_strings(pixel_encoding="Ycc422", bits_per_channel=10, color_space="BT709")
    assert nvapi.set_color_data(0x80061085, want) is True
    assert len(drv.sets) == 1
    assert (drv.sets[0].color_format, drv.sets[0].bpc, drv.sets[0].colorimetry) == (1, 3, 2)
    assert drv.sets[0].color_selection_policy == nvapi.NV_COLOR_SELECTION_POLICY_USER
    assert drv.sets[0].dynamic_range == nvapi.NV_DYNAMIC_RANGE_AUTO


def test_set_color_data_unsupported_depth_is_reported_without_a_set(monkeypatch):
    # A panel/link that cannot take 12-bit: every candidate unsupported -> no SET.
    drv = _FakeDriver(_LIVE_8RGB, supported=lambda cd: cd.bpc != nvapi.NV_BPC_12)
    drv.install(monkeypatch)
    want = nvapi.color_data_from_strings(pixel_encoding="Ycc422", bits_per_channel=12, color_space="BT709")
    assert nvapi.set_color_data(0x80061085, want) is False
    assert drv.sets == []
    assert len(drv.probes) >= 1


def test_set_color_data_falls_back_to_auto_colorimetry_when_exact_refused(monkeypatch):
    drv = _FakeDriver(_LIVE_8RGB, refuse_first=True)
    drv.install(monkeypatch)
    want = nvapi.ColorData(color_format=1, bpc=3)           # colorimetry / range not pinned
    assert nvapi.set_color_data(0x80061085, want) is True
    assert len(drv.sets) == 2
    assert drv.sets[1].colorimetry == nvapi.NV_COLORIMETRY_AUTO
    assert drv.sets[1].dynamic_range == nvapi.NV_DYNAMIC_RANGE_AUTO


def test_set_color_data_reports_wrong_landing(monkeypatch):
    drv = _FakeDriver(_LIVE_8RGB)
    drv.install(monkeypatch)
    # Driver "accepts" but the read-back still says 8-bit.
    monkeypatch.setattr(nvapi, "get_color_data", lambda did: _LIVE_8RGB)
    assert nvapi.set_color_data(0x80061085, nvapi.ColorData(color_format=1, bpc=3)) is False


def test_parse_probe_and_describe_displays_probe(monkeypatch):
    p = nvapi.parse_probe("12,Ycc422,BT709")
    assert (p.bpc, p.color_format, p.colorimetry) == (nvapi.NV_BPC_12, 1, 2)
    assert nvapi.parse_probe("12").bpc == nvapi.NV_BPC_12 and nvapi.parse_probe("12").color_format == -1
    assert nvapi.parse_probe("").is_empty()

    live = nvapi.ColorData(color_format=1, colorimetry=2, dynamic_range=1, bpc=3, color_selection_policy=0)
    monkeypatch.setattr(nvapi, "get_color_data", lambda did: live)
    monkeypatch.setattr(nvapi, "get_default_color_data", lambda did: _LIVE_8RGB)
    monkeypatch.setattr(nvapi, "is_supported", lambda did, cd: cd.bpc != nvapi.NV_BPC_12)
    out = nvapi.describe_displays([(0x80061085, "80061085")], probe=nvapi.parse_probe("12"))
    assert out[0]["current"]["summary"] == "10-bit YCbCr422 BT709 limited (user)"
    assert out[0]["probe"] == {"request": "12-bit YCbCr422 BT709 limited (user)", "supported": False, "already": False}
    out10 = nvapi.describe_displays([(0x80061085, "80061085")], probe=nvapi.parse_probe("10"))
    assert out10[0]["probe"]["already"] is True and out10[0]["probe"]["supported"] is True


# --- profile planning --------------------------------------------------------


def _s01_profile() -> Profile:
    """A profile as captured before the hex-target-ID parser fix: the GPU-1
    panel has no colour fields of its own."""
    return Profile(
        name="24 - 10b - SyncClient",
        display_ids={
            "80061085": {"name": "Tessera SX40", "gpu_index": 0, "color_space": "BT709",
                         "bits_per_channel": 10, "pixel_encoding": "Ycc422"},
            "82061085": {"name": "Tessera SX40", "gpu_index": 1, "color_space": "",
                         "bits_per_channel": 0, "pixel_encoding": ""},
        },
    )


def test_plan_inherits_colour_for_sibling_panel_without_fields():
    plan = nvapi.plan_color_from_profile(_s01_profile())
    assert set(plan) == {"80061085", "82061085"}
    first, note = plan["80061085"]
    second, note2 = plan["82061085"]
    assert note == ""
    assert "inherited from display 80061085" in note2
    assert first == second
    assert first.color_format == nvapi.NV_COLOR_FORMAT_YUV422 and first.bpc == nvapi.NV_BPC_10


def test_plan_does_not_inherit_across_different_panels():
    p = _s01_profile()
    p.display_ids["82061085"]["name"] = "Operator KVM"
    plan = nvapi.plan_color_from_profile(p)
    assert set(plan) == {"80061085"}


def test_plan_prefers_nvapi_color_block_when_present():
    p = _s01_profile()
    p.display_ids["80061085"]["nvapi_color"] = {"color_format": 2, "bpc": 4, "colorimetry": 2,
                                                 "dynamic_range": 1, "color_selection_policy": 0}
    plan = nvapi.plan_color_from_profile(p)
    assert plan["80061085"][0].color_format == 2 and plan["80061085"][0].bpc == 4


def test_plan_empty_for_profile_without_colour():
    assert nvapi.plan_color_from_profile(Profile(name="x")) == {}
    p = Profile(name="y", display_ids={"80061085": {"name": "Tessera SX40", "bits_per_channel": 0}})
    assert nvapi.plan_color_from_profile(p) == {}


# --- apply -------------------------------------------------------------------


def test_apply_sets_only_displays_that_differ_and_reports_per_display(monkeypatch):
    live = {
        0x80061085: nvapi.ColorData(color_format=1, colorimetry=2, dynamic_range=1, bpc=3, color_selection_policy=0),
        0x82061085: nvapi.ColorData(color_format=0, colorimetry=0, dynamic_range=0, bpc=2, color_selection_policy=1),
    }
    sets: list[tuple[int, nvapi.ColorData]] = []

    def fake_set(did, data):
        sets.append((did, data))
        live[did] = nvapi.ColorData(**{**live[did].to_dict(), "color_format": data.color_format, "bpc": data.bpc})
        return True

    monkeypatch.setattr(nvapi, "is_available", lambda: True)
    monkeypatch.setattr(nvapi, "get_color_data", lambda did: live.get(did))
    monkeypatch.setattr(nvapi, "set_color_data", fake_set)

    results = nvapi.apply_color_data_from_profile(_s01_profile())
    assert results == {"80061085": True, "82061085": True}
    assert [d for d, _ in sets] == [0x82061085]       # the first panel was already right
    assert sets[0][1].color_format == 1 and sets[0][1].bpc == 3


def test_apply_records_failure_and_continues(monkeypatch):
    monkeypatch.setattr(nvapi, "is_available", lambda: True)
    monkeypatch.setattr(nvapi, "get_color_data", lambda did: None)
    monkeypatch.setattr(nvapi, "set_color_data", lambda did, data: did != 0x80061085)
    results = nvapi.apply_color_data_from_profile(_s01_profile())
    assert results == {"80061085": False, "82061085": True}


def test_refused_depth_falls_back_to_deepest_link_depth_same_encoding(monkeypatch):
    sets: list[tuple[int, nvapi.ColorData]] = []

    def fake_set(did, data):
        sets.append((did, data))
        return data.bpc != nvapi.NV_BPC_12          # 12-bit refused, 10-bit accepted

    monkeypatch.setattr(nvapi, "is_available", lambda: True)
    monkeypatch.setattr(nvapi, "get_color_data", lambda did: _LIVE_8RGB)
    monkeypatch.setattr(nvapi, "set_color_data", fake_set)
    monkeypatch.setattr(nvapi, "get_displayport_info", lambda did: {"current_mode_bpc": [8, 10]})
    p = _s01_profile()
    for info in p.display_ids.values():
        info.update({"bits_per_channel": 12, "pixel_encoding": "Ycc422", "color_space": "BT709"})
    results = nvapi.apply_color_data_from_profile(p)
    assert results == {"80061085": False, "82061085": False}     # still reported: profile not met
    fallbacks = [d for _, d in sets if d.bpc == nvapi.NV_BPC_10]
    assert len(fallbacks) == 2
    assert all(d.color_format == nvapi.NV_COLOR_FORMAT_YUV422 and d.colorimetry == nvapi.NV_COLORIMETRY_YCC709
               for d in fallbacks)


def test_apply_without_nvapi_is_a_logged_noop(monkeypatch):
    monkeypatch.setattr(nvapi, "is_available", lambda: False)
    monkeypatch.setattr(nvapi, "capability", lambda: {"available": False, "reason": "no dll"})
    assert nvapi.apply_color_data_from_profile(_s01_profile()) == {}


def test_apply_dry_run_touches_nothing(monkeypatch):
    monkeypatch.setattr(nvapi, "is_available", lambda: True)
    monkeypatch.setattr(nvapi, "get_color_data", lambda did: (_ for _ in ()).throw(AssertionError("GET in dry-run")))
    monkeypatch.setattr(nvapi, "set_color_data", lambda did, data: (_ for _ in ()).throw(AssertionError("SET in dry-run")))
    assert nvapi.apply_color_data_from_profile(_s01_profile(), dry_run=True) == {"80061085": True, "82061085": True}


# --- pipeline wiring ---------------------------------------------------------


def _pipeline_stubs(monkeypatch, tmp_path, order: list[str]) -> None:
    """Quiet every subsystem the apply pipeline touches. Nothing may reach a
    real NVIDIA exe: `executors.run` is replaced with a tripwire."""
    from nvmon import config, executors
    from nvmon.agent import displays, mosaic, qsync, edid
    from nvmon.types import CommandResult, DDState, QSyncState

    def tripwire(*a, **k):  # pragma: no cover — only fires on a test bug
        raise AssertionError(f"pipeline test shelled out: {a} {k}")

    monkeypatch.setattr(executors, "run", tripwire)
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(config, "is_admin", lambda: True)
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    monkeypatch.setattr(displays, "status", lambda *a, **k: DDState(active=False))
    monkeypatch.setattr(displays, "disable", lambda **k: CommandResult(success=True, return_code=0))
    monkeypatch.setattr(displays, "apply_arrangement", lambda *a, **k: True)
    monkeypatch.setattr(mosaic, "query_current", lambda **k: None)
    monkeypatch.setattr(mosaic, "status", lambda: None)
    monkeypatch.setattr(mosaic, "disable", lambda **k: CommandResult(success=True, return_code=0))
    monkeypatch.setattr(mosaic, "configure_from_profile", tripwire)
    monkeypatch.setattr(mosaic, "configure", tripwire)
    monkeypatch.setattr(qsync, "status", lambda: QSyncState())
    monkeypatch.setattr(qsync, "disable", lambda **k: CommandResult(success=True, return_code=0))
    monkeypatch.setattr(qsync, "enable_from_profile", tripwire)
    monkeypatch.setattr(edid, "read_active_edid_overrides", lambda: {})

    def fake_modes(profile, dry_run=False):
        order.append("modes")
        return 2, 0, [CommandResult(success=True, return_code=0)]

    monkeypatch.setattr(mosaic, "set_modes_from_profile", fake_modes)


def _no_mosaic_no_sync(p: Profile) -> Profile:
    """site shape: Mosaic off, DD untouched, QSync off for the test."""
    p.mosaic.enabled = False
    p.qsync.enabled = False
    p.direct_display.enabled = False
    p.direct_display.skip_apply = True
    return p


def test_apply_pipeline_runs_colour_step_after_mode_set(monkeypatch, tmp_path):
    """The whole reason for v0.9.1: _apply_inner_with_profile must call the
    colour apply once the per-display modes are set, and carry a failure
    into the apply message without failing the apply."""
    order: list[str] = []
    _pipeline_stubs(monkeypatch, tmp_path, order)

    def fake_colour(profile, dry_run=False):
        order.append("colour")
        return {"80061085": True, "82061085": False}

    monkeypatch.setattr(nvapi, "apply_color_data_from_profile", fake_colour)

    ok, msg = profiles._apply_inner_with_profile(_no_mosaic_no_sync(_s01_profile()), dry_run=False, skip=set())
    assert ok
    assert order == ["modes", "colour"]
    assert "colour depth/encoding not applied on display(s) 82061085" in msg


def _rate_profile(rate: float = 60.0) -> Profile:
    p = _no_mosaic_no_sync(_s01_profile())
    p.validation.expected_refresh_rate = rate
    for info in p.display_ids.values():
        info.update({"resolution": [3840, 2160], "refresh_rate": rate, "bits_per_channel": 12,
                     "pixel_encoding": "Ycc422", "color_space": "BT709"})
    return p


import pytest as _pytest


@_pytest.mark.skip(reason="retrain cycle disabled 2026-11-04: did not change the trained lane count on the rig")
def test_colour_refused_on_small_link_retrains_via_30hz(monkeypatch, tmp_path):
    """site 2026-11-04: 12-bit refused at 4K60 on a 2-lane link; a fresh mode
    set with 12-bit active trains 4 lanes. Order must be: target modes,
    colour (refused), 30 Hz modes, colour, target modes, colour (ok)."""
    order: list[str] = []
    _pipeline_stubs(monkeypatch, tmp_path, order)
    from nvmon.agent import mosaic
    from nvmon.types import CommandResult

    rates: list[float] = []

    def fake_modes(profile, dry_run=False):
        r = float(profile.validation.expected_refresh_rate)
        rates.append(r)
        order.append(f"modes@{r:g}")
        return 2, 0, [CommandResult(success=True, return_code=0)]

    calls = {"n": 0}

    def fake_colour(profile, dry_run=False):
        calls["n"] += 1
        order.append("colour")
        ok = calls["n"] >= 3          # refused until after the retrain
        return {"80061085": ok, "82061085": ok}

    monkeypatch.setattr(mosaic, "set_modes_from_profile", fake_modes)
    monkeypatch.setattr(nvapi, "apply_color_data_from_profile", fake_colour)
    monkeypatch.setattr(nvapi, "get_displayport_info",
                        lambda did: {"sink_supports_bpc": [8, 10, 12], "current_mode_bpc": [8, 10]})

    ok, msg = profiles._apply_inner_with_profile(_rate_profile(60.0), dry_run=False, skip=set())
    assert ok and msg.endswith("applied"), msg
    assert order == ["modes@60", "colour", "modes@30", "colour", "modes@60", "colour"]


def test_colour_refused_by_sink_does_not_retrain(monkeypatch, tmp_path):
    order: list[str] = []
    _pipeline_stubs(monkeypatch, tmp_path, order)
    monkeypatch.setattr(nvapi, "apply_color_data_from_profile",
                        lambda profile, dry_run=False: order.append("colour") or {"80061085": False, "82061085": False})
    monkeypatch.setattr(nvapi, "get_displayport_info",
                        lambda did: {"sink_supports_bpc": [8, 10], "current_mode_bpc": [8, 10]})
    ok, msg = profiles._apply_inner_with_profile(_rate_profile(60.0), dry_run=False, skip=set())
    assert ok and "colour depth/encoding not applied" in msg
    assert order == ["modes", "colour"]


def test_apply_pipeline_colour_can_be_skipped(monkeypatch, tmp_path):
    order: list[str] = []
    _pipeline_stubs(monkeypatch, tmp_path, order)
    called: list[int] = []
    monkeypatch.setattr(nvapi, "apply_color_data_from_profile", lambda *a, **k: called.append(1) or {})

    ok, msg = profiles._apply_inner_with_profile(_no_mosaic_no_sync(_s01_profile()), dry_run=False, skip={"color"})
    assert ok and msg.endswith("applied")
    assert order == ["modes"]
    assert called == []
