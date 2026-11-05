"""Fix hints (v0.9.10): the soft known-case rules from the site bring-up."""

from __future__ import annotations

from nvmon.agent import remedies


def _ids(hints):
    return [h["id"] for h in hints]


def _base(**over):
    s = {"role": "client", "qsync_enabled": True, "profile_rate": 60.0, "house_hz": 0.0,
         "house_detected": False, "synced": True, "unsynced_for_s": 0.0, "displays": {}}
    s.update(over)
    return s


def _disp(**over):
    d = {"pos": "M1", "present": True, "want_bits": 12, "live_bits": 12, "sink_bpc": [8, 10, 12],
         "mode_bpc": [8, 10, 12], "link": "HBR2 5.40 x4", "want_pclk": 592_680_000,
         "want_totals": (4400, 2245), "rec_pclk": 592_680_000, "rec_totals": (4400, 2245),
         "win_encoding": "Ycc422", "nv_format": "YCbCr422"}
    d.update(over)
    return d


def test_healthy_node_has_no_hints():
    assert remedies.evaluate(_base(displays={"80061085": _disp()})) == []


def test_wedged_server_needs_persistence_and_matching_house_rate():
    s = _base(role="server", house_detected=True, house_hz=60.001, synced=False, unsynced_for_s=200)
    h = remedies.evaluate(s)
    assert _ids(h) == ["server_wedged"] and h[0]["action"] == "restart"
    assert remedies.evaluate({**s, "unsynced_for_s": 30}) == []          # not yet: soft
    assert remedies.evaluate({**s, "house_hz": 30.0}) == []               # wrong house rate: GUI problem
    assert remedies.evaluate({**s, "role": "client"}) == []


def test_link_too_small_suggests_reseat_of_that_output():
    h = remedies.evaluate(_base(displays={"82061085": _disp(pos="M2", live_bits=10, mode_bpc=[8, 10],
                                                             link="HBR3 8.10 x2")}))
    assert _ids(h) == ["link_too_small"] and h[0]["action"] == "reseat"
    assert "M2" in h[0]["target"] and "HBR3 8.10 x2" in h[0]["evidence"]
    # panel itself can't do it -> not a reseat case
    assert remedies.evaluate(_base(displays={"x": _disp(live_bits=10, mode_bpc=[8, 10], sink_bpc=[8, 10])})) == []


def test_timing_stuck_on_5994_flavour_suggests_restart():
    h = remedies.evaluate(_base(displays={"80061085": _disp(rec_pclk=593_410_000, rec_totals=(4400, 2250))}))
    assert _ids(h) == ["timing_stuck"] and h[0]["action"] == "restart"
    assert "593.410 MHz" in h[0]["evidence"] and "59.94" in h[0]["evidence"]


def test_missing_display_and_colour_mismatch():
    h = remedies.evaluate(_base(displays={"a": _disp(present=False), "b": _disp(win_encoding="Rgb444")}))
    assert sorted(_ids(h)) == ["colour_mismatch", "display_missing"]


def test_tracker_measures_unsynced_time():
    t = remedies.Tracker()
    assert t.unsynced_for(False, now=100.0) == 0.0
    assert t.unsynced_for(False, now=250.0) == 150.0
    assert t.unsynced_for(True, now=260.0) == 0.0
    assert t.unsynced_for(False, now=300.0) == 0.0
