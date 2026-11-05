"""nvmon.agent.timing — exact display timings (pure parts; NVAPI calls are not exercised).

Background (site 2026-11-02): RTX PRO Sync would not frame-lock the CTA 4K
timings whose pixel clock is exactly 297.000 MHz (24/25/30 Hz); a custom
timing with the same structure and an off-grid clock locked at once.
"""

from __future__ import annotations

import ctypes

import pytest

from nvmon.agent import timing


def test_struct_sizes_match_nvapi_h():
    assert ctypes.sizeof(timing.NV_TIMING) == 96
    assert ctypes.sizeof(timing.NV_TIMING_INPUT) == 32     # probed on 597.30: 24 is rejected
    assert ctypes.sizeof(timing.NV_CUSTOM_DISPLAY) == 144
    assert timing.NV_TIMING_INPUT_VER == 0x10020
    assert timing.NV_CUSTOM_DISPLAY_VER == 0x10090


@pytest.mark.parametrize("rate,h_total,expected_clock", [
    (24, 5500, 296_340_000),
    (25, 5280, 296_340_000),
    (30, 4400, 296_340_000),
])
def test_design_4k_integer_rates_avoid_297mhz_and_are_exact(rate, h_total, expected_clock):
    tm = timing.design_timing(3840, 2160, rate)
    assert (tm.h_total, tm.v_total) == (h_total, 2245)
    assert tm.pclk_hz == expected_clock
    assert tm.pclk_hz not in timing.POISON_PIXEL_CLOCKS_HZ
    assert tm.pclk_hz % 10_000 == 0                      # programmed exactly, no rounding
    assert tm.exact_hz == pytest.approx(rate, abs=1e-9)  # exactly the integer rate
    assert tm.h_back >= 0 and tm.v_back >= 0
    assert tm.validate() == []


def test_design_60_keeps_cta_structure_but_not_needed_clock():
    tm = timing.design_timing(3840, 2160, 60)
    assert tm.h_total == 4400 and tm.exact_hz == pytest.approx(60.0)
    assert tm.pclk_hz % 10_000 == 0


def test_design_rejects_fractional_rates():
    with pytest.raises(ValueError):
        timing.design_timing(3840, 2160, 23.976)


def test_validate_flags_poison_clock_and_off_grid_clock():
    cta24 = timing.Timing(3840, 1276, 88, 5500, 2160, 8, 10, 2250, 297_000_000)
    assert any("will not frame-lock" in p for p in cta24.validate())
    assert (cta24.h_pol, cta24.v_pol) == (0, 0)   # nvapi.h: 0 = positive
    odd = timing.Timing(3840, 1276, 88, 5500, 2160, 8, 10, 2248, 296_736_000)
    assert any("10 kHz grid" in p for p in odd.validate())
    assert odd.pclk_10khz == 29674


def test_round_trip_dict_and_nv_struct():
    tm = timing.design_timing(3840, 2160, 24)
    d = tm.to_dict()
    back = timing.Timing.from_dict(d)
    assert back == timing.Timing.from_dict(back.to_dict())
    nv = timing._to_nv(tm)
    assert (nv.HTotal, nv.VTotal, nv.pclk) == (5500, 2245, 29634)
    assert nv.etc.rrx1k == 24000 and nv.etc.rr == 24
    again = timing._from_nv(nv, "test")
    assert (again.h_total, again.v_total, again.pclk_hz) == (5500, 2245, 296_340_000)
    assert again.name.startswith("nvmon 3840x2160@24")


def test_apply_removes_conflicting_custom_then_saves(monkeypatch):
    """srv5 2026-11-02: a hand-made 5500x2248 custom 24 made SaveCustomDisplay
    fail (NVAPI_ERROR) for nvmon's 5500x2245. Same size+rate, different
    totals → delete first; identical entry → keep and tolerate a Save error."""
    from nvmon import executors
    calls: list = []
    hand = timing.Timing(3840, 1276, 88, 5500, 2160, 8, 10, 2248, 296_740_000, name="hand")
    want = timing.design_timing(3840, 2160, 24)
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(timing, "enum_custom", lambda did: [hand] if did == 0x82061085 else [want])
    monkeypatch.setattr(timing, "delete_custom", lambda ids, t: calls.append(("delete", tuple(ids), t.v_total)))
    monkeypatch.setattr(timing, "try_custom", lambda ids, t: calls.append(("try", tuple(ids), t.v_total)))

    def save(ids):
        calls.append(("save", tuple(ids)))
        if 0x80061085 in ids:
            raise timing.TimingUnavailable("NvAPI_DISP_SaveCustomDisplay: NVAPI_ERROR")
        return "monitor"

    monkeypatch.setattr(timing, "save_custom", save)
    live = lambda: {0x82061085: (want.pclk_hz, want.h_total, want.v_total),  # noqa: E731
                    0x80061085: (want.pclk_hz, want.h_total, want.v_total)}
    # conflicting hand-made entry: deleted, then try + save
    r = timing.apply_custom_timing([0x82061085], want, verify=live)
    assert r["saved"] and not r["mismatch"]
    assert calls[:3] == [("delete", (0x82061085,), 2248), ("try", (0x82061085,), 2245), ("save", (0x82061085,))]
    # identical entry already stored: Save's error is tolerated
    calls.clear()
    r = timing.apply_custom_timing([0x80061085], want, verify=live)
    assert r["saved"] and "identical" in r.get("note", "")
    assert ("delete", (0x80061085,), 2245) not in calls


def test_apply_keeps_verified_trial_when_save_fails(monkeypatch):
    """srv5 2026-11-02: Save returned NVAPI_ERROR for every scope although the
    trial was live and verified. The apply must continue (timing IS running),
    report saved=False with a note, and never revert a good trial."""
    from nvmon import executors
    calls: list = []
    want = timing.design_timing(3840, 2160, 24)
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(timing, "enum_custom", lambda did: [])
    monkeypatch.setattr(timing, "try_custom", lambda ids, t: calls.append("try"))
    monkeypatch.setattr(timing, "revert_trial", lambda ids: calls.append("revert"))

    def save(ids):
        raise timing.TimingUnavailable("NvAPI_DISP_SaveCustomDisplay: NVAPI_ERROR")

    monkeypatch.setattr(timing, "save_custom", save)
    r = timing.apply_custom_timing([0x80061085], want,
                                   verify=lambda: {0x80061085: (want.pclk_hz, want.h_total, want.v_total)})
    assert r["applied"] and not r["mismatch"]
    assert r["saved"] is False and "boot-apply" in r["note"]
    assert calls == ["try"]


def test_apply_reverts_when_driver_lands_elsewhere(monkeypatch):
    from nvmon import executors
    calls: list = []
    want = timing.design_timing(3840, 2160, 30)
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(timing, "enum_custom", lambda did: [])
    monkeypatch.setattr(timing, "try_custom", lambda ids, t: None)
    monkeypatch.setattr(timing, "save_custom", lambda ids: calls.append("save"))
    monkeypatch.setattr(timing, "revert_trial", lambda ids: calls.append("revert"))
    r = timing.apply_custom_timing([0x80061085], want, verify=lambda: {0x80061085: (297_000_000, 4400, 2250)})
    assert r["mismatch"] and not r["saved"] and calls == ["revert"]


def test_parse_display_id():
    assert timing.parse_display_id("80061085") == 0x80061085
    assert timing.parse_display_id("0x82061085") == 0x82061085
    assert timing.parse_display_id(0x11106) == 0x11106
