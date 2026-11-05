"""ccd.set_refresh — exact vertical refresh through SetDisplayConfig.

Windows-only (the ctypes structures exist only there). The Win32 calls are
intercepted: ``ccd.query`` returns hand-built path/mode arrays and
``ccd._set_display_config`` records the flags it was called with.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from nvmon.agent import ccd

pytestmark = pytest.mark.skipif(not sys.platform.startswith("win"), reason="CCD is Windows-only")


def _config(target_ids: list[int], hz: tuple[int, int]):
    """One active path per target id, each with a source and a target mode."""
    n = len(target_ids)
    paths = (ccd.PATH_INFO * n)()
    modes = (ccd.MODE_INFO * (2 * n))()
    for i, tid in enumerate(target_ids):
        p = paths[i]
        p.sourceInfo.id = i
        p.sourceInfo.modeInfoIdx = 2 * i
        p.targetInfo.id = tid
        p.targetInfo.modeInfoIdx = 2 * i + 1
        p.targetInfo.refreshRate.Numerator, p.targetInfo.refreshRate.Denominator = hz
        modes[2 * i].infoType = ccd.MODE_SOURCE
        modes[2 * i].u.sourceMode.width = 3840
        modes[2 * i].u.sourceMode.height = 2160
        modes[2 * i + 1].infoType = ccd.MODE_TARGET
        tm = modes[2 * i + 1].u.targetMode
        tm.vSyncFreq.Numerator, tm.vSyncFreq.Denominator = hz
        tm.activeSize.cx, tm.activeSize.cy = 3840, 2160
    return paths, n, modes, 2 * n


def test_set_refresh_asks_windows_for_the_exact_rational(monkeypatch):
    before = _config([0x11106, 0xF1106], (23976, 1000))
    after = _config([0x11106, 0xF1106], (24, 1))
    seq = iter([before, after])
    calls: list[int] = []
    monkeypatch.setattr(ccd, "query", lambda: next(seq))

    def fake_set(n_paths, paths, n_modes, modes, flags):
        calls.append(flags)
        # Every requested path must carry the rational and no target mode index.
        for i in range(n_paths):
            t = paths[i].targetInfo
            assert t.modeInfoIdx == ccd._INVALID_IDX
            assert (t.refreshRate.Numerator, t.refreshRate.Denominator) == (24, 1)
            assert t.scanLineOrdering == ccd.SCANLINE_PROGRESSIVE
        return ccd.ERROR_SUCCESS

    monkeypatch.setattr(ccd, "_set_display_config", fake_set)
    got = ccd.set_refresh({0x11106: (24, 1), 0xF1106: (24, 1)})
    assert got == {0x11106: 24.0, 0xF1106: 24.0}
    # strict validate first (no ALLOW_CHANGES), then apply + save
    assert calls[0] & ccd.SDC_VALIDATE and not calls[0] & ccd.SDC_APPLY
    assert not calls[0] & ccd.SDC_ALLOW_CHANGES
    assert calls[1] & ccd.SDC_APPLY and calls[1] & ccd.SDC_SAVE_TO_DATABASE
    assert all(f & ccd.SDC_USE_SUPPLIED_DISPLAY_CONFIG for f in calls)


def test_set_refresh_retries_validation_with_allow_changes(monkeypatch):
    before = _config([0x11106], (23976, 1000))
    after = _config([0x11106], (24, 1))
    seq = iter([before, after])
    monkeypatch.setattr(ccd, "query", lambda: next(seq))
    calls: list[int] = []

    def strict_fails(n_paths, paths, n_modes, modes, flags):
        calls.append(flags)
        if flags & ccd.SDC_VALIDATE and not flags & ccd.SDC_ALLOW_CHANGES:
            return 1610  # ERROR_BAD_CONFIGURATION
        return ccd.ERROR_SUCCESS

    monkeypatch.setattr(ccd, "_set_display_config", strict_fails)
    assert ccd.set_refresh({0x11106: (24, 1)}) == {0x11106: 24.0}
    assert len(calls) == 3
    assert calls[1] & ccd.SDC_VALIDATE and calls[1] & ccd.SDC_ALLOW_CHANGES
    assert calls[2] & ccd.SDC_APPLY and calls[2] & ccd.SDC_ALLOW_CHANGES


def test_set_refresh_leaves_other_paths_alone(monkeypatch):
    before = _config([0x11106, 0xF1106], (23976, 1000))
    monkeypatch.setattr(ccd, "query", lambda: before)
    monkeypatch.setattr(ccd, "_set_display_config", lambda *a: ccd.ERROR_SUCCESS)
    ccd.set_refresh({0x11106: (24, 1)})
    paths = before[0]
    assert paths[0].targetInfo.modeInfoIdx == ccd._INVALID_IDX
    assert paths[1].targetInfo.modeInfoIdx == 3          # untouched
    assert paths[1].targetInfo.refreshRate.Numerator == 23976


def test_set_refresh_returns_none_when_validation_fails_or_target_missing(monkeypatch):
    before = _config([0x11106], (23976, 1000))
    monkeypatch.setattr(ccd, "query", lambda: before)
    applied: list[int] = []

    def refuse(n_paths, paths, n_modes, modes, flags):
        applied.append(flags)
        return 87  # ERROR_INVALID_PARAMETER

    monkeypatch.setattr(ccd, "_set_display_config", refuse)
    assert ccd.set_refresh({0x11106: (24, 1)}) is None
    assert len(applied) == 2 and all(f & ccd.SDC_VALIDATE for f in applied)   # never reached APPLY
    # a target id that is not an active path -> None before any Win32 call
    applied.clear()
    assert ccd.set_refresh({0xDEAD: (24, 1)}) is None
    assert applied == []
