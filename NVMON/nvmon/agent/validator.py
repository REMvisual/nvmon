"""Cross-subsystem state validation.

Given the current DD, Mosaic, QSync and GPU state — plus an optional profile
— produce a `ValidationResult` that the `/validate` endpoint returns verbatim.
Each check is independent and never raises; every path returns a
`ValidationCheck(passed=..., detail=...)`.
"""

from __future__ import annotations

from .. import config, log
from ..types import (
    DDState, GPUInfo, MosaicState, QSyncState,
    Profile, ValidationCheck, ValidationResult,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_all(
    *,
    dd: DDState,
    mosaic: MosaicState,
    qsync: QSyncState,
    gpus: list[GPUInfo],
    profile: Profile | None = None,
) -> ValidationResult:
    """Run every available check.

    Skips peer/backup checks (no peers in nvmon) and any site-specific checks.
    """
    checks: dict[str, ValidationCheck] = {}

    # "synced" only counts when the displays run at the house-sync rate —
    # a 23.976 Hz wall on a 60 Hz house signal cannot be locked, whatever
    # configureQsync2 says. Use the Mosaic rate when Mosaic is on, else the
    # DD/desktop rate the refresh_rate check would use.
    display_rate = _display_rate(dd, mosaic)
    if display_rate > 0:
        from .qsync import apply_rate_check
        apply_rate_check(qsync, display_rate)

    checks["dd_active"] = _check_dd_active(dd, profile)
    checks["refresh_rate"] = _check_refresh_rate(dd, profile, mosaic=mosaic, qsync=qsync)
    checks["mosaic_config"] = _check_mosaic(mosaic, profile)
    checks["output_signal"] = _check_output_signal(dd, profile)
    checks["color_depth"] = _check_color_depth(dd, profile)
    checks["dp_link"] = _check_dp_link(dd)
    checks["qsync_locked"] = _check_qsync(qsync, profile)
    checks["qsync_role"] = _check_qsync_role(qsync, profile)
    checks["house_sync"] = _check_house_sync(qsync, profile)
    checks["gpu_health"] = _check_gpu_health(gpus)

    all_passed = all(c.passed for c in checks.values())
    return ValidationResult(all_passed=all_passed, checks=checks)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _managed_displays(dd: DDState):
    return [
        d for d in dd.displays
        if not config.is_excluded_display(d.name, d.resolution)
    ]


def _display_rate(dd: DDState, mosaic: MosaicState | None) -> float:
    """The rate the managed displays are actually running at (0 = unknown)."""
    if mosaic is not None and mosaic.enabled and mosaic.refresh_rate > 0:
        return float(mosaic.refresh_rate)
    rates = [d.refresh_rate for d in _managed_displays(dd) if d.refresh_rate > 0]
    if rates:
        return sum(rates) / len(rates)
    return 0.0


def _check_output_signal(dd: DDState, profile: Profile | None) -> ValidationCheck:
    """Each wall path must send what the desktop is (no scaling), at the
    profile rate.

    Reads Windows' CCD view (``DDState.signals`` from ``ccd.describe()``):
    source = desktop size, target = signal on the wire. configureMosaic can
    report 3840x2160@23.976 while Windows is actually sending 10240x5760@60
    (the panel's EDID default) and scaling the desktop to it — the LED
    processor then sees the wrong mode and QSync never locks. Only a fresh
    Mosaic set fixes it; this check is what makes boot-apply notice.
    """
    walls = [
        s for s in (dd.signals or [])
        if not config.is_excluded_display(str(s.get("name", "")), tuple(s.get("source") or (0, 0)))
    ]
    if not walls:
        return ValidationCheck(passed=True, detail="Signal not reported (skipped)")

    # Rate is only this check's business when the profile actually drives
    # the wall (Mosaic or DirectDisplay). A desktop-only / QSync-only
    # profile keeps the scaling check but leaves the rate verdict to
    # refresh_rate — otherwise a 60 Hz operator desktop under a 24 Hz
    # expectation fails twice for the same reason.
    drives_wall = bool(profile and (profile.mosaic.enabled or profile.direct_display.enabled))
    expected = float(profile.validation.expected_refresh_rate) if (profile and drives_wall) else 0.0
    tol = config.rate_tolerance(profile.validation.refresh_rate_tolerance if profile else None)
    bad: list[str] = []
    for s in walls:
        src, tgt, hz = s.get("source"), s.get("target"), float(s.get("hz") or 0)
        src = tuple(src) if src else None
        tgt = tuple(tgt) if tgt else None
        if tgt and src and tgt != src:
            bad.append(f"desktop {src[0]}x{src[1]} sent as {tgt[0]}x{tgt[1]}@{hz:g}Hz (scaled)")
        elif expected > 0 and hz > 0 and abs(hz - expected) > tol:
            where = f"{tgt[0]}x{tgt[1]} " if tgt else ""
            bad.append(f"{where}sent at {hz:g}Hz (expected {expected:g}Hz)")
    if bad:
        return ValidationCheck(passed=False, detail="; ".join(sorted(set(bad))))
    s = walls[0]
    tgt = tuple(s.get("target") or s.get("source") or (0, 0))
    return ValidationCheck(
        passed=True,
        detail=f"{tgt[0]}x{tgt[1]} @ {float(s.get('hz') or 0):g}Hz, not scaled",
    )


def _normalise_encoding(text: str) -> str:
    """'Ycc422' / 'YCbCr422' / 'yuv422' -> 'ycc422'; 'Rgb' -> 'rgb444'."""
    t = (text or "").strip().lower().replace("-", "").replace(" ", "").replace("_", "")
    t = t.replace("ycbcr", "ycc").replace("yuv", "ycc")
    if t == "rgb":
        t = "rgb444"
    return t


_FORMAT_TO_WIRE = {0: "rgb444", 1: "ycc422", 2: "ycc444", 3: "ycc420"}
_WIRE_LABEL = {"rgb444": "Rgb444", "ycc422": "Ycc422", "ycc444": "Ycc444", "ycc420": "Ycc420"}


def _check_color_depth(dd: DDState, profile: Profile | None) -> ValidationCheck:
    """Each managed panel must carry the bits per channel and pixel encoding
    the profile's ``display_ids`` ask for (v0.9.3).

    Compares Windows' wire format (configureDdisplay / WinRT: ``10`` +
    ``Ycc422``) with the profile's colour per display, resolved the same way
    the apply resolves it (``nvapi.plan_color_from_profile`` — explicit
    fields, else ``nvapi_color``, else inherited from the sibling panel).
    site 2026-11-04: after a "60 - 12b" apply the driver refused 12-bit and
    every panel stayed at 10-bit; the apply message said so once, this row
    says so for as long as it is true, and boot-apply sees the drift.
    """
    if profile is None:
        return ValidationCheck(passed=True, detail="No profile loaded — colour depth not checked (skipped)")
    from . import nvapi as nvapi_mod  # local: keep validator import-light

    try:
        plan = nvapi_mod.plan_color_from_profile(profile)
    except Exception as exc:  # noqa: BLE001
        return ValidationCheck(passed=True, detail=f"Colour depth plan unreadable ({exc}) — skipped")
    if not plan:
        return ValidationCheck(passed=True, detail="Colour depth not in profile — skipped")

    displays = _managed_displays(dd)
    by_id = {str(d.id).lower(): d for d in displays if d.id}
    bad: list[str] = []
    good: list[str] = []
    unreported: list[str] = []
    for hex_id, (wanted, _note) in plan.items():
        live = by_id.get(str(hex_id).lower())
        if live is None:
            continue  # panel not present right now — output_signal / dd rows cover that
        want_bits = wanted.bits()
        want_enc = _FORMAT_TO_WIRE.get(wanted.color_format, "")
        live_bits = int(live.bits_per_channel or 0)
        live_enc = _normalise_encoding(live.pixel_encoding)
        if not live_bits and not live_enc:
            unreported.append(hex_id)
            continue
        live_label = (f"{live_bits}-bit" if live_bits else "?-bit") + (f" {live.pixel_encoding}" if live.pixel_encoding else "")
        want_label = (f"{want_bits}-bit" if want_bits else "") + (f" {_WIRE_LABEL.get(want_enc, want_enc)}" if want_enc else "")
        mismatch = (want_bits and live_bits and want_bits != live_bits) or (want_enc and live_enc and want_enc != live_enc)
        if mismatch:
            bad.append(f"display {hex_id}: {live_label} (profile expects {want_label.strip()})")
        else:
            good.append(live_label)
    if bad:
        return ValidationCheck(passed=False, detail="; ".join(bad))
    if not good:
        if unreported:
            return ValidationCheck(passed=True, detail="Colour depth not reported by Windows for these panels (skipped)")
        return ValidationCheck(passed=True, detail="Colour depth: profile panels not present (skipped)")
    uniq = sorted(set(good))
    label = uniq[0] if len(uniq) == 1 else " · ".join(uniq)
    tail = f" ({len(unreported)} not reported)" if unreported else ""
    return ValidationCheck(passed=True, detail=f"{label} on {len(good)} display(s){tail}")


def _check_dp_link(dd: DDState) -> ValidationCheck:
    """Each managed DisplayPort panel should be trained at the link its sink
    offers (v0.9.5). site 2026-11-04: every panel reported HBR3 x2 of x4
    lanes — half the bandwidth — which is why 12-bit was refused at 4K60
    while the sink itself supports 12 bpc. Red whenever lanes or rate are
    below the sink's maximum, so a degraded cable or extender shows up
    before someone asks for more bits. Skipped for non-DP displays or when
    NVAPI is unavailable (never red by absence of data).
    """
    from . import nvapi as nvapi_mod  # local: keep validator import-light

    displays = [d for d in _managed_displays(dd) if d.id]
    if not displays:
        return ValidationCheck(passed=True, detail="DP link: no managed displays (skipped)")
    bad: list[str] = []
    good: list[str] = []
    seen = 0
    for d in displays:
        did = nvapi_mod.display_id_from_hex(str(d.id))
        if did is None:
            continue
        try:
            info = nvapi_mod.get_displayport_info(did)
        except Exception:  # noqa: BLE001
            info = None
        if not info or not (info.get("flags") or {}).get("is_dp"):
            continue
        seen += 1
        raw = info.get("raw") or {}
        cur_rate, max_rate = int(raw.get("curLinkRate") or 0), int(raw.get("maxLinkRate") or 0)
        cur_lanes = _lanes_of(info.get("current_link", ""))
        max_lanes = _lanes_of(info.get("max_link", ""))
        label = f"{info.get('current_link', '?')} ({info.get('current_payload_gbps', 0)} Gbit/s)"
        # Informational only (v0.9.5 correction): the driver may train the
        # fewest lanes the current mode needs, so "x2 of x4" is not by itself
        # a fault. site ran 8-bit RGB at 4K60 (14.26 Gbit/s, needs x4) the same
        # morning these links read x2 under 10-bit 4:2:2. Shown, never red.
        if (max_lanes and cur_lanes and cur_lanes < max_lanes) or (max_rate and cur_rate and cur_rate < max_rate):
            good.append(f"{label}, sink offers {info.get('max_link', '?')}")
        else:
            good.append(label)
    if not seen:
        return ValidationCheck(passed=True, detail="DP link: not reported (skipped)")
    if bad:
        return ValidationCheck(passed=False, detail="; ".join(bad))
    uniq = sorted(set(good))
    return ValidationCheck(passed=True, detail=f"{' · '.join(uniq)} on {len(good)} display(s)")


def _lanes_of(link_text: str) -> int:
    """'HBR3 8.10 x4' -> 4; 0 when unparsable."""
    try:
        return int(str(link_text).rsplit("x", 1)[1])
    except (IndexError, ValueError):
        return 0


def _check_dd_active(dd: DDState, profile: Profile | None) -> ValidationCheck:
    if profile is None:
        # No profile loaded = no expectation. The legacy default assumed
        # every rig runs DirectDisplay and painted a red X on a healthy
        # desktop-only node until the operator applied something. The
        # "skipped" wording lets the dashboard drop the row.
        return ValidationCheck(passed=True, detail="No profile loaded — DD not checked (skipped)")
    if not profile.direct_display.enabled:
        return ValidationCheck(passed=True, detail="DD not required by profile")

    pd = profile.direct_display
    expect = f" (profile expects DD={pd.mode}, adapter {pd.index})"

    displays = _managed_displays(dd)
    if not displays:
        return ValidationCheck(passed=False, detail=f"No managed displays detected{expect}")

    removed = [d for d in displays if "removed" in d.status.lower()]
    total = len(displays)

    if len(removed) == total:
        return ValidationCheck(passed=True, detail=f"{total}/{total} displays in DD mode")
    return ValidationCheck(
        passed=False,
        detail=(
            f"{len(removed)}/{total} displays in DD mode "
            f"({total - len(removed)} still on desktop){expect}"
        ),
    )


def _check_refresh_rate(
    dd: DDState, profile: Profile | None,
    *, mosaic: MosaicState | None = None, qsync: QSyncState | None = None,
) -> ValidationCheck:
    displays = _managed_displays(dd)
    rates = [d.refresh_rate for d in displays if d.refresh_rate > 0]

    if not rates:
        fallback_rate = 0.0
        if mosaic and mosaic.refresh_rate > 0:
            fallback_rate = mosaic.refresh_rate
        elif qsync and qsync.house_sync_incoming_hz > 0:
            fallback_rate = qsync.house_sync_incoming_hz

        if fallback_rate > 0:
            rates = [fallback_rate]
        else:
            expect = ""
            if profile and profile.validation.expected_refresh_rate > 0:
                expect = f" (profile expects {profile.validation.expected_refresh_rate}Hz)"
            detail = "No managed displays to check" if not displays else "No refresh rate data"
            return ValidationCheck(passed=False, detail=f"{detail}{expect}")

    avg_rate = sum(rates) / len(rates)
    all_same = all(abs(r - rates[0]) < 0.01 for r in rates)

    if profile and profile.validation.expected_refresh_rate > 0:
        expected = profile.validation.expected_refresh_rate
        # 23.976 under a 24.000 profile is a FAIL — the two never lock to
        # the same house clock (site 2026-11-02). config.rate_tolerance()
        # caps the profile's stored value at 0.01 Hz and floors legacy
        # 0.002 values that were tighter than the tools' decimals.
        tolerance = config.rate_tolerance(profile.validation.refresh_rate_tolerance)
        if abs(avg_rate - expected) <= tolerance:
            # Passing case: just show the live rate. The "expected X" used
            # to be appended here too, but operators read that as "the
            # profile is forcing X" even when X was just a stale capture-
            # time value. Clean detail string = green tick + just the rate.
            return ValidationCheck(
                passed=True,
                detail=f"{avg_rate:.3f}Hz",
            )
        return ValidationCheck(
            passed=False,
            detail=f"{avg_rate:.3f}Hz (expected {expected}Hz, tolerance {tolerance})",
        )

    # Profile carries no expected rate (legacy or freshly-empty profile)
    # — pass with the live rate so the dashboard stops showing a red X
    # for "expected 23.976Hz" when nobody asked for that rate.
    if all_same:
        return ValidationCheck(passed=True, detail=f"{rates[0]:.3f}Hz (no profile expectation)")
    return ValidationCheck(passed=False, detail=f"Mixed rates: {rates}")


def _check_mosaic(mosaic: MosaicState, profile: Profile | None) -> ValidationCheck:
    if profile is None:
        # Same reasoning as _check_dd_active: without a profile there is
        # no grid to compare against, so "Mosaic is not enabled" is not a
        # failure — it is the normal state of a non-Mosaic rig.
        return ValidationCheck(passed=True, detail="No profile loaded — Mosaic not checked (skipped)")
    if not profile.mosaic.enabled:
        # Profile doesn't use Mosaic — don't drift-check it. Validator stays
        # green even if some legacy state has Mosaic ON; the apply pipeline
        # will disable Mosaic the next time the profile is applied. We don't
        # want validate to flag transient state on a profile that has no
        # Mosaic expectations.
        return ValidationCheck(
            passed=True,
            detail="Mosaic not in profile — skipped",
        )

    if not mosaic.enabled:
        expect = ""
        if profile:
            pm = profile.mosaic
            expect = (
                f" (profile expects {pm.rows}x{pm.cols} "
                f"@ {pm.width}x{pm.height} {pm.refresh_rate}Hz)"
            )
        return ValidationCheck(passed=False, detail=f"Mosaic is not enabled{expect}")

    detail_parts = [f"{mosaic.rows}x{mosaic.cols}"]
    if profile:
        pm = profile.mosaic
        if mosaic.rows != pm.rows or mosaic.cols != pm.cols:
            return ValidationCheck(
                passed=False,
                detail=(
                    f"Grid mismatch: got {mosaic.rows}x{mosaic.cols}, "
                    f"profile expects {pm.rows}x{pm.cols}"
                ),
            )
        w, h = mosaic.per_display_resolution
        if w != pm.width or h != pm.height:
            return ValidationCheck(
                passed=False,
                detail=(
                    f"Resolution mismatch: got {w}x{h}, "
                    f"profile expects {pm.width}x{pm.height}"
                ),
            )
        detail_parts.append(f"{w}x{h}")
        # Rate too — otherwise boot-apply leaves a wall at the wrong rate
        # (e.g. 29.97 under a 60 Hz profile) untouched because grid and
        # resolution still match (the legacy tool 5d7ba95).
        try:
            wanted = float(pm.refresh_rate)
        except (TypeError, ValueError):
            wanted = 0.0
        tolerance = config.rate_tolerance(profile.validation.refresh_rate_tolerance)
        if wanted > 0 and mosaic.refresh_rate > 0 and abs(mosaic.refresh_rate - wanted) > tolerance:
            return ValidationCheck(
                passed=False,
                detail=(
                    f"Rate mismatch: Mosaic at {mosaic.refresh_rate}Hz, "
                    f"profile expects {wanted:g}Hz"
                ),
            )

    detail_parts.append(f"{mosaic.refresh_rate}Hz")
    return ValidationCheck(passed=True, detail=" @ ".join(detail_parts))


def _check_qsync(qsync: QSyncState, profile: Profile | None) -> ValidationCheck:
    if profile and not profile.qsync.enabled:
        return ValidationCheck(passed=True, detail="QSync not required by profile")

    if not qsync.available:
        pq = profile.qsync if profile else None
        expect = ""
        if pq:
            expect = f" (profile expects QSync={pq.source or 'sync'}, trigger={pq.trigger})"
        return ValidationCheck(passed=False, detail=f"QSync not available{expect}")

    # Client role: lock criterion is "any per-display sync_state=CLIENT".
    # Source / trigger expectations don't apply because the upstream timing
    # server owns those; the client just locks to whatever pulse arrives
    # over RJ45. configureQsync2's overall "Is synced: NO" line is also
    # not authoritative for clients (the parser handles that override
    # too). This is what the operator asked for: "validator should match
    # the profile" — for client profiles, the only thing to verify is
    # that the per-display CLIENT state is present.
    expected_role = (
        (getattr(profile.qsync, "role", "server") or "server").lower()
        if profile and profile.qsync.enabled else "server"
    )
    if expected_role == "client":
        # "Sync State : CLIENT" is the configured role, not a lock — site
        # 2026-11-02 had every client in CLIENT state with "Is synced : NO"
        # while both servers were UNSYNCED. Lock = a GPU says synced (or
        # the upstream pulse is reported available).
        client_locked = any(
            g.synced or g.signal_available
            for g in (qsync.per_gpu_sync or [])
        )
        if client_locked:
            gpu_detail = ", ".join(
                f"GPU{g.gpu}={'locked' if (g.effective_synced or g.synced) else 'UNLOCKED'}"
                for g in qsync.per_gpu_sync
            )
            return ValidationCheck(
                passed=True,
                detail=f"Locked (client) {gpu_detail}".rstrip(),
            )
        return ValidationCheck(
            passed=False,
            detail="QSync client not locked — no upstream framelock pulse detected",
        )

    if qsync.synced:
        # "synced" alone isn't enough for fleet operation. A card locked to
        # its own internal clock (source=VSYNC, mapped from "INTERNAL" by
        # the parser) reports synced=True but is not actually following the
        # daisy-chain master — the operator hit this exact failure: "node
        # said it was synced but only to itself, not the upstream machine".
        # Fail the check loudly when the live source is internal but the
        # profile expects HOUSESYNC.
        live_src = (qsync.source or "").upper()
        is_internal = live_src in ("VSYNC", "INTERNAL", "")
        pq = profile.qsync if profile else None
        wants_housesync = bool(pq and (pq.source or "").upper().startswith("HOUSE"))
        if is_internal and wants_housesync:
            return ValidationCheck(
                passed=False,
                detail=(
                    f"QSync locked to INTERNAL clock — not following the daisy "
                    f"chain. Live source={live_src or 'unset'}, profile expects "
                    f"{pq.source}. Check the BNC/RJ45 cabling or the upstream master."
                ),
            )
        # Locked — but to the RIGHT source and trigger? Any other source
        # mismatch (e.g. profile INTERNAL, live HOUSESYNC) is still drift
        # the enforcer must flip, and a wrong trigger edge (RISING vs
        # FALLING) is a real lock to the wrong clock edge (the legacy tool 602e158).
        want_src = (pq.source or "").upper() if pq else ""
        if want_src and live_src and want_src != live_src and not (
            want_src.startswith("HOUSE") and live_src.startswith("HOUSE")
        ):
            return ValidationCheck(
                passed=False,
                detail=(
                    f"QSync locked but to wrong source: got {live_src}, "
                    f"profile expects {want_src} (trigger={pq.trigger})"
                ),
            )
        want_trig = (pq.trigger or "").upper() if pq else ""
        live_trig = (qsync.trigger or "").upper()
        if want_trig and live_trig and want_trig != live_trig and live_src.startswith("HOUSE"):
            return ValidationCheck(
                passed=False,
                detail=(
                    f"QSync locked to {live_src} but wrong trigger: got {live_trig}, "
                    f"profile expects {want_trig}"
                ),
            )
        gpu_detail = ", ".join(
            f"GPU{g.gpu}={'locked' if g.effective_synced else 'UNLOCKED'}"
            + (f" {g.sync_note}" if g.sync_note else "")
            for g in qsync.per_gpu_sync
        )
        src_tag = f" via {live_src}" if live_src else ""
        return ValidationCheck(passed=True, detail=f"Locked{src_tag} ({gpu_detail})")

    pq = profile.qsync if profile else None
    expect = ""
    if pq:
        expect = f" (profile expects QSync={pq.source}, trigger={pq.trigger})"
    return ValidationCheck(passed=False, detail=f"QSync not locked{expect}")


def _check_qsync_role(qsync: QSyncState, profile: Profile | None) -> ValidationCheck:
    """Verify the live QSync role (server vs client) matches the profile.

    A profile that says role=server but reports a CLIENT-state display is
    exactly the production bug we're guarding against — server node came up
    as client on restart and refused to change sync policy. Surface it here
    so /validate's all_passed flag goes red, the dashboard shows the drift,
    and the boot-apply gate's "validator already passes — skip" shortcut
    (Monitor._maybe_apply_on_boot) DOESN'T fire on a misconfigured node.
    """
    if profile is None or not profile.qsync.enabled:
        return ValidationCheck(passed=True, detail="QSync not required by profile")

    expected = (getattr(profile.qsync, "role", "server") or "server").lower()

    if not qsync.available:
        return ValidationCheck(
            passed=False,
            detail=f"QSync unavailable, cannot verify role={expected}",
        )

    # Infer live role from per-display sync_state. SERVER on any GPU wins:
    # the second GPU of a two-GPU server reports CLIENT internally, so
    # "any CLIENT → client" would flag every multi-GPU server as drifted.
    from . import qsync as qsync_mod  # local: avoid import cycle at module load

    evidence = qsync_mod.live_role_evidence(qsync)
    if evidence is None:
        return ValidationCheck(
            passed=False,
            detail=(
                f"QSync not engaged: no GPU reports SERVER or CLIENT "
                f"(profile expects {expected})"
            ),
        )
    live_role = evidence

    if expected == live_role:
        return ValidationCheck(
            passed=True,
            detail=f"role={live_role}",
        )
    return ValidationCheck(
        passed=False,
        detail=(
            f"role drift: live={live_role}, profile expects {expected}. "
            f"Re-apply the profile to force the role transition."
        ),
    )


def _check_house_sync(qsync: QSyncState, profile: Profile | None) -> ValidationCheck:
    if profile and not profile.qsync.enabled:
        return ValidationCheck(passed=True, detail="QSync not required by profile")

    # Client nodes don't directly receive house sync — they lock to the
    # upstream framelock pulse over RJ45 instead, and their card reports
    # source VSYNC/INTERNAL because there is no BNC. Skip the check so a
    # working daisy-chain client doesn't show a red house_sync row. Role
    # comes from the profile when there is one; with no profile loaded
    # (fresh node, before the first apply) fall back to the LIVE role so
    # a locked client isn't red just because nothing has been applied yet.
    if profile is not None:
        expected_role = (getattr(profile.qsync, "role", "server") or "server").lower()
    else:
        from . import qsync as qsync_mod  # local: avoid import cycle at module load
        expected_role = qsync_mod.live_role(qsync) if qsync.available else "server"
    if expected_role == "client":
        return ValidationCheck(
            passed=True,
            detail="House sync not required for client (locks to upstream pulse)",
        )

    if not qsync.house_sync_detected:
        expect = ""
        if profile and profile.validation.expected_house_sync_freq > 0:
            expect = f" (profile expects {profile.validation.expected_house_sync_freq}Hz)"
        return ValidationCheck(passed=False, detail=f"House sync not detected{expect}")

    detail = f"House sync detected at {qsync.house_sync_incoming_hz:.3f}Hz"

    if profile and profile.validation.expected_house_sync_freq > 0:
        expected = profile.validation.expected_house_sync_freq
        tolerance = config.rate_tolerance(profile.validation.refresh_rate_tolerance)
        if abs(qsync.house_sync_incoming_hz - expected) <= tolerance:
            return ValidationCheck(passed=True, detail=detail)
        return ValidationCheck(
            passed=False,
            detail=f"{detail} (expected {expected}Hz)",
        )

    return ValidationCheck(passed=True, detail=detail)


def _check_gpu_health(gpus: list[GPUInfo]) -> ValidationCheck:
    if not gpus:
        return ValidationCheck(passed=False, detail="No GPU data")

    agent_cfg = config.get_agent_config()
    temp_warning = agent_cfg["gpu"]["temp_warning_c"]
    power_warning_pct = agent_cfg["gpu"]["power_warning_pct"]

    issues: list[str] = []
    for g in gpus:
        if g.temperature_c > temp_warning:
            issues.append(
                f"GPU{g.index} temp {g.temperature_c}C > {temp_warning}C"
            )
        if g.power_limit_w > 0:
            pct = (g.power_draw_w / g.power_limit_w) * 100
            if pct > power_warning_pct:
                issues.append(f"GPU{g.index} power {pct:.0f}% of limit")

    if issues:
        return ValidationCheck(passed=False, detail="; ".join(issues))

    temps = [g.temperature_c for g in gpus]
    return ValidationCheck(
        passed=True,
        detail=f"{len(gpus)} GPUs, temps {min(temps)}-{max(temps)}C",
    )


# Quiet the linter for the module-level unused log import — we keep it
# available for future per-check logging without changing signatures.
_ = log
