"""Fix hints — soft, known-case suggestions for states the GUI cannot fix.

Each hint says what to do by hand (restart this node / reseat this cable /
re-apply the profile), on which node and output, and the evidence that
triggered it. They are advisory: nothing here acts. The rules come from
a production bring-up (2026-11-03, colour depth):

  server_wedged     server role, house sync present at the profile rate,
                    not locked for a while          -> restart Windows
  timing_stuck      the driver's own mode record differs from the profile
                    timing (e.g. 593.41 MHz / 59.94 instead of 60.000)
                                                    -> restart this node
  link_too_small    profile wants N-bit, panel supports it, current link
                    does not offer it               -> reseat the DP at the GPU
  display_missing   a profile display is not detected -> reseat DP / adapter
  colour_mismatch   Windows sends RGB, NVIDIA set YCbCr (purple/green)
                                                    -> re-apply, restart if back

`evaluate()` is pure (tests); `collect()` reads the live state.
"""

from __future__ import annotations

import time
from typing import Any

# A state has to persist this long before a hint is shown (soft, not flappy).
PERSIST_S = 90.0


def _hint(hid: str, action: str, title: str, detail: str, *, target: str = "", evidence: str = "") -> dict:
    return {"id": hid, "action": action, "title": title, "detail": detail,
            "target": target, "evidence": evidence}


def evaluate(s: dict[str, Any]) -> list[dict]:
    """Pure rules over a snapshot dict:

    {
      "role": "server|client|", "qsync_enabled": bool,
      "profile_rate": 60.0, "house_hz": 60.001, "house_detected": bool, "synced": bool,
      "displays": {hex_id: {"pos": "M1", "present": bool,
                            "want_bits": 12, "live_bits": 10,
                            "sink_bpc": [..], "mode_bpc": [..], "link": "HBR3 8.10 x2",
                            "want_pclk": 592680000, "want_totals": (4400, 2245),
                            "rec_pclk": 593410000, "rec_totals": (4400, 2250),
                            "win_encoding": "Rgb444", "nv_format": "YCbCr422"}},
      "unsynced_for_s": 0.0, "apply_message": "..."
    }
    """
    out: list[dict] = []
    role = (s.get("role") or "").lower()
    rate = float(s.get("profile_rate") or 0.0)
    house = float(s.get("house_hz") or 0.0)

    if (s.get("qsync_enabled") and role == "server" and s.get("house_detected") and not s.get("synced")
            and (not rate or abs(house - rate) <= 0.01)
            and float(s.get("unsynced_for_s") or 0) >= PERSIST_S):
        out.append(_hint(
            "server_wedged", "restart", "Sync server will not engage — restart this node",
            "House sync is present at the right rate but no GPU became SERVER. Re-syncing does not clear "
            "this state; a Windows restart of this node does. Its clients follow within seconds once it "
            "is up.",
            evidence=f"house sync {house:.3f} Hz, profile {rate:g} Hz, not locked for "
                     f"{float(s.get('unsynced_for_s') or 0) / 60:.0f} min"))

    for hid, d in sorted((s.get("displays") or {}).items()):
        pos = d.get("pos") or hid
        if d.get("present") is False:
            out.append(_hint(
                "display_missing", "reseat", f"{pos} not detected — reseat its cable",
                "The profile drives this display but Windows does not see it. Reseat the DisplayPort at the "
                "GPU, check the adapter is seated (and lit), then the HDMI cable to the matrix. Re-apply the "
                "profile once it is back.", target=f"{pos} ({hid})", evidence="display absent from Windows"))
            continue
        wb, lb = int(d.get("want_bits") or 0), int(d.get("live_bits") or 0)
        if wb and lb and lb < wb and wb in (d.get("sink_bpc") or []) and wb not in (d.get("mode_bpc") or []):
            out.append(_hint(
                "link_too_small", "reseat", f"{pos} cannot reach {wb}-bit — reseat its DisplayPort at the GPU",
                f"The panel supports {wb}-bit but this link trained too small for it at this rate. A physical "
                f"reseat of the DisplayPort at the GPU end lets it train all 4 lanes. Then re-apply the "
                f"profile.",
                target=f"{pos} ({hid})", evidence=f"link {d.get('link') or '?'}, offers {d.get('mode_bpc')}, "
                                                    f"running {lb}-bit"))
        wp, rp = int(d.get("want_pclk") or 0), int(d.get("rec_pclk") or 0)
        wt, rt = tuple(d.get("want_totals") or ()), tuple(d.get("rec_totals") or ())
        if wp and rp and (abs(rp - wp) > wp * 0.0002 or (wt and rt and wt != rt)):
            hz = rp / (rt[0] * rt[1]) if len(rt) == 2 and all(rt) else 0.0
            out.append(_hint(
                "timing_stuck", "restart", f"{pos} runs the wrong timing — restart this node",
                "The driver is running its own timing instead of the profile's (typically the 59.94 flavour "
                "of a dual 60/59.94 entry), so the chain cannot lock. A restart clears the stuck custom-"
                "timing state and boot-apply sets the profile timing. If it persists, remove the custom "
                "3840x2160 60 Hz entry in NVIDIA Control Panel and re-apply.",
                target=f"{pos} ({hid})",
                evidence=f"driver {rt[0] if rt else '?'}x{rt[1] if len(rt) > 1 else '?'} @ {rp / 1e6:.3f} MHz"
                         f"{f' ({hz:.3f} Hz)' if hz else ''}, profile {wp / 1e6:.3f} MHz"))
        we, ne = str(d.get("win_encoding") or "").lower(), str(d.get("nv_format") or "").lower()
        if we.startswith("rgb") and ne.startswith("ycbcr"):
            out.append(_hint(
                "colour_mismatch", "reapply", f"{pos} colour state is contradictory (purple/green)",
                "Windows sends RGB while NVIDIA is set to YCbCr, so the processor decodes the wrong format. "
                "Re-apply the profile; if it comes back, restart this node.",
                target=f"{pos} ({hid})", evidence=f"wire {d.get('win_encoding')}, NVIDIA {d.get('nv_format')}"))
    return out


class Tracker:
    """Remembers since when the node has been unsynced (for the persistence rule)."""

    def __init__(self) -> None:
        self._unsynced_since: float | None = None

    def unsynced_for(self, synced: bool, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if synced:
            self._unsynced_since = None
            return 0.0
        if self._unsynced_since is None:
            self._unsynced_since = now
        return now - self._unsynced_since


def collect(dd, qsync, tracker: Tracker) -> list[dict]:
    """Gather the live readings for `evaluate`. Best effort: every source is
    optional; a missing reading simply means that rule does not fire."""
    from . import nvapi as nvapi_mod
    from . import profiles as profiles_mod

    loaded = profiles_mod.load_active()
    if not loaded:
        return []
    profile = loaded[0]
    role = (getattr(profile.qsync, "role", "") or "").lower() if profile.qsync.enabled else ""
    synced = bool(getattr(qsync, "synced", False)) or (
        role == "client" and any(getattr(g, "synced", False) for g in (getattr(qsync, "per_gpu_sync", None) or [])))
    snap: dict[str, Any] = {
        "role": role, "qsync_enabled": bool(profile.qsync.enabled),
        "profile_rate": float(profile.validation.expected_refresh_rate or 0.0),
        "house_hz": float(getattr(qsync, "house_sync_incoming_hz", 0.0) or 0.0),
        "house_detected": bool(getattr(qsync, "house_sync_detected", False)),
        "synced": synced,
        "unsynced_for_s": tracker.unsynced_for(synced) if profile.qsync.enabled else 0.0,
        "displays": {},
    }
    live = {str(d.id).lower(): d for d in (getattr(dd, "displays", None) or []) if d.id}
    plan = nvapi_mod.plan_color_from_profile(profile)
    try:
        from . import timing as timing_mod
        timing_ok = timing_mod.is_available()
    except Exception:  # noqa: BLE001
        timing_mod, timing_ok = None, False
    for hex_id, info in (profile.display_ids or {}).items():
        if not isinstance(info, dict):
            continue
        h = str(hex_id).lower()
        d = live.get(h)
        entry: dict[str, Any] = {"present": d is not None}
        if d is not None:
            entry["pos"] = "M1" if "(0,0)" in str(getattr(d, "status", "")) else "M2"
            entry["win_encoding"] = getattr(d, "pixel_encoding", "")
            did = nvapi_mod.display_id_from_hex(h)
            want = plan.get(str(hex_id), (None, ""))[0]
            if want is not None:
                entry["want_bits"] = want.bits()
            cur = nvapi_mod.get_color_data(did) if did is not None else None
            if cur is not None:
                entry["live_bits"] = cur.bits()
                entry["nv_format"] = nvapi_mod.FORMAT_NAMES.get(cur.color_format, "")
            link = nvapi_mod.get_displayport_info(did) if did is not None else None
            if link:
                entry.update(sink_bpc=link.get("sink_supports_bpc"), mode_bpc=link.get("current_mode_bpc"),
                             link=link.get("current_link"))
            tb = info.get("timing") if isinstance(info.get("timing"), dict) else None
            if tb and timing_ok and did is not None:
                try:
                    rec = timing_mod.get_timing(did)
                    entry.update(want_pclk=int(tb.get("pclk_hz") or 0),
                                 want_totals=(int(tb.get("h_total") or 0), int(tb.get("v_total") or 0)),
                                 rec_pclk=int(rec.pclk_hz), rec_totals=(int(rec.h_total), int(rec.v_total)))
                except Exception:  # noqa: BLE001
                    pass
        else:
            entry["pos"] = f"display {hex_id}"
        snap["displays"][h] = entry
    return evaluate(snap)
