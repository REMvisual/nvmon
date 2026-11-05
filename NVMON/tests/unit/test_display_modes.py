"""Per-display mode set (`mosaic.set_modes_from_profile`) — one
`configureMosaic set` for ALL displays.

Regression (RENDER-01, 2026-11-01): the apply pipeline issued one
`set rows=1 cols=1 res=... out=0,0` per display. configureMosaic's `set` is
exhaustive — "any display not given in one of the grids will be
deactivated" — so the GPU-1 panel dropped off the desktop on every apply
and the operator had to re-extend it in Windows Display settings.
"""

from __future__ import annotations

import copy
from unittest.mock import patch

from nvmon import executors
from nvmon.agent import displays, mosaic
from nvmon.types import (
    CommandResult, DDState, DisplayInfo, Profile, ProfileValidation,
)


def _live(*rows: tuple[str, int, int, str, float]) -> DDState:
    """rows: (nvapi_id, gpu_index, display_index, status, refresh)."""
    st = DDState(active=False)
    for nvapi_id, gpu, disp, status, rr in rows:
        st.displays.append(DisplayInfo(
            id=nvapi_id, name="Tessera SX40", status=status, refresh_rate=rr,
            resolution=(3840, 2160), gpu_index=gpu, display_index=disp,
        ))
    st.display_count = len(st.displays)
    return st


def _two_gpu_profile(second_rate: float = 0.0, expected: float = 23.976) -> Profile:
    return Profile(
        name="23.97 - 10b - SyncHost",
        validation=ProfileValidation(expected_refresh_rate=expected),
        display_ids={
            "80061085": {"gpu_index": 0, "display_index": 0,
                         "resolution": [3840, 2160], "refresh_rate": expected},
            # Captured with the hex-target-ID parser gap: refresh unknown.
            "82061085": {"gpu_index": 1, "display_index": 1,
                         "resolution": [3840, 2160], "refresh_rate": second_rate},
        },
    )


def _landed(profile: Profile, live: DDState | None) -> DDState | None:
    """The post-set state a well-behaved driver produces: every profile
    target at its asked rate, everything else untouched."""
    if live is None:
        return None
    after = copy.deepcopy(live)
    for d in after.displays:
        info = (profile.display_ids or {}).get(d.id) or {}
        rate = float(info.get("refresh_rate") or 0) or float(profile.validation.expected_refresh_rate or 0)
        if info and rate > 0 and "desktop" in (d.status or "").lower():
            d.refresh_rate = rate
    return after


def _run(profile: Profile, live: DDState | None, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(exe, args, **kw):
        calls.append(list(args))
        return CommandResult(success=True, return_code=0, stdout="ok")

    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: r"E:\NVMON\bin\configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    after = _landed(profile, live)
    states = iter([live, after])
    monkeypatch.setattr(displays, "status", lambda: next(states, after))
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    with patch.object(executors, "run", side_effect=fake_run):
        out = mosaic.set_modes_from_profile(profile)
    return out, calls


def test_one_set_call_lists_every_display(monkeypatch):
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 59.94),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 59.94))
    (n_attempted, n_skipped, results), calls = _run(_two_gpu_profile(), live, monkeypatch)

    assert len(calls) == 1, "must be ONE configureMosaic invocation"
    # Exactly the command validated with `configureMosaic test` on site.
    assert calls[0] == [
        "set", "rows=1", "cols=1", "res=3840,2160,23.976", "display=0x80061085",
        "nextgrid", "rows=1", "cols=1", "res=3840,2160,23.976", "display=0x82061085",
    ]
    assert n_attempted == 2 and n_skipped == 0
    assert len(results) == 1 and results[0].success


def test_primary_display_is_first_grid(monkeypatch):
    """The first grid becomes the GDI primary — keep the current primary."""
    live = _live(("80061085", 0, 0, "Desktop @(3840,0)", 59.94),
                 ("82061085", 1, 1, "Desktop @(0,0)", 59.94))
    _, calls = _run(_two_gpu_profile(), live, monkeypatch)
    args = calls[0]
    assert args.index("display=0x82061085") < args.index("nextgrid") < args.index("display=0x80061085")


def test_nothing_runs_when_all_already_at_target(monkeypatch):
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 23.976))
    (n_attempted, n_skipped, results), calls = _run(_two_gpu_profile(), live, monkeypatch)
    assert calls == []
    assert (n_attempted, n_skipped, results) == (0, 2, [])


def test_23976_is_not_24(monkeypatch):
    """site 2026-11-02: under the `24 - 10b` profile every client stayed at
    23.976 because 0.05 Hz tolerance called it "already at 24". A 23.976
    panel under a 24.000 target MUST be pushed — even when the profile JSON
    still carries the old 0.05 tolerance."""
    prof = _two_gpu_profile(second_rate=24.0, expected=24.0)
    prof.validation.refresh_rate_tolerance = 0.05   # legacy stored value
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 23.976))
    (n_attempted, n_skipped, _), calls = _run(prof, live, monkeypatch)
    assert n_attempted == 2 and n_skipped == 0
    assert calls and calls[0].count("res=3840,2160,24.000") == 2


def test_third_decimal_disagreement_still_matches(monkeypatch):
    """configureQsync2 says 23.975 where configureDdisplay says 23.976 —
    that is the same timing and must not trigger a mode set."""
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.975),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 23.976))
    (n_attempted, n_skipped, _), calls = _run(_two_gpu_profile(), live, monkeypatch)
    assert calls == [] and (n_attempted, n_skipped) == (0, 2)


def test_already_correct_display_still_listed_when_other_changes(monkeypatch):
    """One panel already at 23.976, the other at 59.94: the correct one must
    still be in the set (at its current mode) or it would be switched off."""
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 59.94))
    (n_attempted, n_skipped, _), calls = _run(_two_gpu_profile(), live, monkeypatch)
    args = calls[0]
    assert "display=0x80061085" in args and "display=0x82061085" in args
    assert n_attempted == 1 and n_skipped == 1


def test_uncaptured_desktop_display_is_kept(monkeypatch):
    """An operator/KVM monitor the profile never captured stays lit at its
    current mode; a display that is NOT on the desktop is left alone."""
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 59.94),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 59.94),
                 ("deadbeef", 0, 2, "Desktop @(7680,0)", 60.0),
                 ("0badf00d", 1, 3, "Removed, Managed", 0.0))
    _, calls = _run(_two_gpu_profile(), live, monkeypatch)
    args = calls[0]
    assert args.count("nextgrid") == 2
    assert "display=0xdeadbeef" in args
    assert "res=3840,2160,60.000" in args
    assert "display=0x0badf00d" not in args


def test_missing_exe_reports_failure_without_running(monkeypatch):
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: None)
    n_attempted, n_skipped, results = mosaic.set_modes_from_profile(_two_gpu_profile())
    assert (n_attempted, n_skipped) == (0, 0)
    assert len(results) == 1 and not results[0].success


def test_wrong_landing_rate_is_a_failed_set(monkeypatch):
    """site 2026-11-02: `set … res=3840,2160,24.000` returned success and the
    Tessera SX40 panels came up at 23.976 (the only "24" timing they offer).
    The apply must fail with the asked/got rates instead of waiting for an
    impossible QSync lock."""
    before = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                   ("82061085", 1, 1, "Desktop @(3840,0)", 23.976))
    after = before  # driver picked the nearest timing: nothing changed
    seq = iter([before, after])
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: next(seq))
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    with patch.object(executors, "run",
                      return_value=CommandResult(success=True, return_code=0, stdout="ok")):
        n_attempted, _, results = mosaic.set_modes_from_profile(
            _two_gpu_profile(second_rate=24.0, expected=24.0))
    assert n_attempted == 2
    assert len(results) == 1 and results[0].success is False
    assert "asked 24.000Hz" in results[0].stderr and "picked 23.976Hz" in results[0].stderr
    assert "/mosaic/timings" in results[0].stderr


def _profile_with_target_ids(rate: float = 24.0, fraction: str = "24/1") -> Profile:
    p = _two_gpu_profile(second_rate=rate, expected=rate)
    p.display_ids["80061085"].update({"os_target_id": "11106", "refresh_fraction": fraction})
    p.display_ids["82061085"].update({"os_target_id": "f1106", "refresh_fraction": fraction})
    return p


def test_refresh_only_change_goes_through_windows_first(monkeypatch):
    """24.000 vs 23.976: configureMosaic can't pick it, Windows can. When
    every changing display keeps its resolution and has a target id, the
    set goes through ccd.set_refresh and configureMosaic is NOT run."""
    before = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                   ("82061085", 1, 1, "Desktop @(3840,0)", 23.976))
    after = _live(("80061085", 0, 0, "Desktop @(0,0)", 24.0),
                  ("82061085", 1, 1, "Desktop @(3840,0)", 24.0))
    seq = iter([before, after])
    asked: dict = {}
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: next(seq))
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    monkeypatch.setattr(mosaic.ccd, "is_available", lambda: True)

    def fake_set_refresh(targets):
        asked.update(targets)
        return {t: 24.0 for t in targets}

    monkeypatch.setattr(mosaic.ccd, "set_refresh", fake_set_refresh)
    with patch.object(executors, "run") as run:
        n_attempted, n_skipped, results = mosaic.set_modes_from_profile(_profile_with_target_ids())
    run.assert_not_called()
    assert asked == {0x11106: [(24, 1), (24000, 1000)], 0xF1106: [(24, 1), (24000, 1000)]}
    assert (n_attempted, n_skipped) == (2, 0)
    assert results[0].success and "SetDisplayConfig" in results[0].stdout


def test_windows_refusal_falls_back_to_configure_mosaic(monkeypatch):
    before = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                   ("82061085", 1, 1, "Desktop @(3840,0)", 23.976))
    after = _live(("80061085", 0, 0, "Desktop @(0,0)", 24.0),
                  ("82061085", 1, 1, "Desktop @(3840,0)", 24.0))
    seq = iter([before, after])
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: next(seq))
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    monkeypatch.setattr(mosaic.ccd, "is_available", lambda: True)
    monkeypatch.setattr(mosaic.ccd, "set_refresh", lambda targets: None)
    with patch.object(executors, "run",
                      return_value=CommandResult(success=True, return_code=0, stdout="ok")) as run:
        _, _, results = mosaic.set_modes_from_profile(_profile_with_target_ids())
    assert run.call_count == 1
    assert "res=3840,2160,24.000" in run.call_args.args[1]
    assert results[0].success


def test_resolution_change_never_uses_windows_path(monkeypatch):
    """A resolution change is configureMosaic's job; Windows is refresh-only."""
    before = _live(("80061085", 0, 0, "Desktop @(0,0)", 24.0),
                   ("82061085", 1, 1, "Desktop @(3840,0)", 24.0))
    for d in before.displays:
        d.resolution = (1920, 1080)
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: before)
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    monkeypatch.setattr(mosaic.ccd, "is_available", lambda: True)
    monkeypatch.setattr(mosaic.ccd, "set_refresh",
                        lambda targets: (_ for _ in ()).throw(AssertionError("must not be called")))
    with patch.object(executors, "run",
                      return_value=CommandResult(success=True, return_code=0, stdout="ok")) as run:
        mosaic.set_modes_from_profile(_profile_with_target_ids())
    assert run.call_count == 1


def test_rational_candidates():
    """The driver labels 1.001 rates as mHz/1000 on these panels; asking
    for 60000/1001 failed strict validation and Windows then substituted
    30 Hz (srv1/srv5, 2026-11-02 16:54). Captured fraction first, then
    mHz/1000, then N*1000/1001; integers N/1 then N000/1000."""
    from nvmon.agent import ccd
    assert ccd.rational_candidates(24.0, "24/1") == [(24, 1), (24000, 1000)]
    assert ccd.rational_candidates(23.976, "23976/1000") == [(23976, 1000), (24000, 1001)]
    assert ccd.rational_candidates(23.976, "24000/1001") == [(24000, 1001), (23976, 1000)]
    assert ccd.rational_candidates(23.976, "24/1") == [(23976, 1000), (24000, 1001)]  # stale fraction ignored
    assert ccd.rational_candidates(59.94) == [(59940, 1000), (60000, 1001)]
    assert ccd.rational_candidates(29.97) == [(29970, 1000), (30000, 1001)]
    assert ccd.rational_candidates(60.0) == [(60, 1), (60000, 1000)]
    assert ccd.rational_candidates(0.0) == []
    assert ccd.rational_candidates(0.0, "59940/1000") == [(59940, 1000), (60000, 1001)]
    assert ccd.rational_for(59.94) == (59940, 1000)
    assert ccd.rational_for(0.0) == (0, 0)


def test_windows_wrong_landing_retries_with_configure_mosaic(monkeypatch):
    """srv1 2026-11-02 16:54: Windows accepted the request (ALLOW_CHANGES) and
    left panel 82061085 at 30 Hz. The set must retry through configureMosaic
    before failing — and succeed when that lands right."""
    before = _live(("80061085", 0, 0, "Desktop @(0,0)", 30.0),
                   ("82061085", 1, 1, "Desktop @(3840,0)", 30.0))
    half = _live(("80061085", 0, 0, "Desktop @(0,0)", 59.94),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 30.0))
    good = _live(("80061085", 0, 0, "Desktop @(0,0)", 59.94),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 59.94))
    seq = iter([before, half, good])
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: next(seq))
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    monkeypatch.setattr(mosaic.ccd, "is_available", lambda: True)
    monkeypatch.setattr(mosaic.ccd, "set_refresh", lambda targets: {t: 59.94 for t in targets})
    with patch.object(executors, "run",
                      return_value=CommandResult(success=True, return_code=0, stdout="ok")) as run:
        n_attempted, _, results = mosaic.set_modes_from_profile(
            _profile_with_target_ids(rate=59.94, fraction="59940/1000"))
    assert run.call_count == 1 and "res=3840,2160,59.940" in run.call_args.args[1]
    assert results[0].success


def _profile_with_timing(rate: float = 24.0) -> Profile:
    from nvmon.agent import timing as timing_mod
    p = _profile_with_target_ids(rate=rate, fraction=f"{int(rate)}/1")
    tm = timing_mod.design_timing(3840, 2160, rate).to_dict()
    for info in p.display_ids.values():
        info["timing"] = dict(tm)
    return p


def test_landing_fails_when_driver_record_runs_the_5994_flavour(monkeypatch):
    """site 2026-11-04: CCD and live_timings said 594.000 MHz, the driver's
    mode record (NvAPI_DISP_GetTiming) ran 593.410 — 59.94 Hz, chain never
    locked. The landing check must read the driver record."""
    from nvmon.agent import timing as timing_mod
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 60.0), ("82061085", 1, 1, "Desktop @(3840,0)", 60.0))
    for d in live.displays:
        d.os_target_id = "11106" if d.id == "80061085" else "f1106"
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: live)
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    monkeypatch.setattr(timing_mod, "is_available", lambda: True)
    want = (592_680_000, 4400, 2245)
    live_now = {0x80061085: (594_000_000, 4400, 2250), 0x82061085: (594_000_000, 4400, 2250)}
    monkeypatch.setattr(timing_mod, "live_timings", lambda: dict(live_now))

    def fake_apply(ids, tm, save=True, **kw):
        for d in ids:
            live_now[int(d)] = (tm.pclk_hz, tm.h_total, tm.v_total)     # CCD/live claims the request
        return {"applied": [{"display": f"0x{ids[0]:x}"}], "mismatch": [], "saved": save}

    monkeypatch.setattr(timing_mod, "apply_custom_timing", fake_apply)
    # ...but the driver actually runs the CTA 59.94 flavour.
    monkeypatch.setattr(timing_mod, "get_timing",
                        lambda did: timing_mod.Timing(3840, 176, 88, 4400, 2160, 8, 10, 2250, 593_410_000))
    monkeypatch.setattr(mosaic.ccd, "set_refresh", lambda targets: None)
    p = _profile_with_timing()
    p.validation.expected_refresh_rate = 60.0
    tm = timing_mod.design_timing(3840, 2160, 60, v_total=2245)
    for info in p.display_ids.values():
        info.update({"refresh_rate": 60.0, "refresh_fraction": "60/1", "timing": tm.to_dict()})
    assert (tm.pclk_hz, tm.h_total, tm.v_total) == want
    with patch.object(executors, "run", return_value=CommandResult(success=True, return_code=0)):
        _, _, results = mosaic.set_modes_from_profile(p)
    assert not results[0].success
    assert "593.410 MHz" in results[0].stderr and "59.94" in results[0].stderr


def test_same_rate_different_pixel_clock_is_a_change_and_goes_via_nvapi(monkeypatch):
    """site 2026-11-02: both panels already read 24.000 Hz on the CTA 297.000 MHz
    timing (which never frame-locks). A profile that carries the exact
    5500x2245 @ 296.34 MHz timing must still push it — through NVAPI custom
    displays — and must not touch configureMosaic or the Windows refresh path."""
    from nvmon.agent import timing as timing_mod
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 24.0),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 24.0))
    for d in live.displays:
        d.os_target_id = "11106" if d.id == "80061085" else "f1106"
    applied: list = []
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: live)
    monkeypatch.setattr(displays, "ensure_all_active", lambda: [])
    monkeypatch.setattr(timing_mod, "is_available", lambda: True)
    live_now = {0x80061085: (297_000_000, 5500, 2250), 0x82061085: (297_000_000, 5500, 2250)}
    monkeypatch.setattr(timing_mod, "live_timings", lambda: dict(live_now))

    def fake_apply(ids, tm, save=True, **kw):
        applied.append((tuple(ids), tm.pclk_hz, tm.h_total, tm.v_total, save))
        for d in ids:
            live_now[int(d)] = (tm.pclk_hz, tm.h_total, tm.v_total)   # the driver programmed it
        return {"applied": [{"display": f"0x{ids[0]:x}"}], "mismatch": [], "saved": save}

    monkeypatch.setattr(timing_mod, "apply_custom_timing", fake_apply)
    # The driver's mode record follows what was programmed (never read the
    # real GPU of the box running the tests).
    monkeypatch.setattr(timing_mod, "get_timing", lambda did: timing_mod.Timing(
        3840, 0, 0, live_now[did][1], 2160, 0, 0, live_now[did][2], live_now[did][0]))
    monkeypatch.setattr(mosaic.ccd, "set_refresh",
                        lambda targets: (_ for _ in ()).throw(AssertionError("CCD path must not run")))
    with patch.object(executors, "run") as run:
        n_attempted, n_skipped, results = mosaic.set_modes_from_profile(_profile_with_timing())
    run.assert_not_called()
    assert (n_attempted, n_skipped) == (2, 0)
    assert sorted(applied) == [((0x80061085,), 296_340_000, 5500, 2245, True),
                               ((0x82061085,), 296_340_000, 5500, 2245, True)]
    assert results[0].success and "NVAPI custom timing" in results[0].stdout


def test_exact_timing_already_live_is_skipped(monkeypatch):
    from nvmon.agent import timing as timing_mod
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 24.0),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 24.0))
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(displays, "status", lambda: live)
    monkeypatch.setattr(timing_mod, "live_timings",
                        lambda: {0x80061085: (296_340_000, 5500, 2245), 0x82061085: (296_340_000, 5500, 2245)})
    with patch.object(executors, "run") as run:
        n_attempted, n_skipped, results = mosaic.set_modes_from_profile(_profile_with_timing())
    run.assert_not_called()
    assert (n_attempted, n_skipped, results) == (0, 2, [])


def test_nvapi_refusal_falls_back_but_exact_check_still_fails_wrong_clock(monkeypatch):
    """NVAPI refused. The set may fall back to the rate paths (that is how a
    driver-native timing such as CTA 4K60 still gets applied), but a profile
    that demands 296.34 MHz must NOT be reported as applied when the fallback
    lands on the 297.000 MHz entry — that is the lock failure again."""
    from nvmon.agent import timing as timing_mod
    live = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                 ("82061085", 1, 1, "Desktop @(3840,0)", 23.976))
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: live)
    monkeypatch.setattr(timing_mod, "is_available", lambda: True)
    monkeypatch.setattr(timing_mod, "live_timings", lambda: {})

    def refuse(ids, tm, save=True, **kw):
        raise timing_mod.TimingUnavailable("NvAPI_DISP_TryCustomDisplay: NVAPI_INVALID_ARGUMENT")

    monkeypatch.setattr(timing_mod, "apply_custom_timing", refuse)
    with patch.object(executors, "run",
                      return_value=CommandResult(success=True, return_code=0, stdout="ok")):
        _, _, results = mosaic.set_modes_from_profile(_profile_with_timing())
    assert results and results[0].success is False
    assert "driver picked" in results[0].stderr or "driver programmed" in results[0].stderr


def test_post_set_reattaches_a_dropped_display(monkeypatch):
    """Belt and braces: if a listed display still left the desktop after the
    set, ensure_all_active() is called."""
    before = _live(("80061085", 0, 0, "Desktop @(0,0)", 59.94),
                   ("82061085", 1, 1, "Desktop @(3840,0)", 59.94))
    after = _live(("80061085", 0, 0, "Desktop @(0,0)", 23.976),
                  ("82061085", 1, 1, "Removed, Managed", 0.0))
    seq = iter([before, after])
    reattached = []
    monkeypatch.setattr(mosaic.config, "get_mosaic_exe", lambda: "configureMosaic.exe")
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(displays, "status", lambda: next(seq))
    monkeypatch.setattr(displays, "ensure_all_active", lambda: reattached.append(True) or [])
    with patch.object(executors, "run",
                      return_value=CommandResult(success=True, return_code=0)):
        mosaic.set_modes_from_profile(_two_gpu_profile())
    assert reattached == [True]
