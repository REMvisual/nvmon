"""Unit tests for nvmon/agent/driver_info.py — version classification,
Blackwell GPU detection, and the KNOWN_ISSUES advisory table."""

from __future__ import annotations

from dataclasses import dataclass

from nvmon.agent import driver_info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Gpu:
    """Minimal GPUInfo-shaped stub — driver_info only reads name + driver_version."""
    name: str = ""
    driver_version: str = ""


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


def test_classify_r595_u4_596_36():
    b = driver_info.classify("596.36")
    assert b.parsed
    assert b.major == 596
    assert b.minor == 36
    assert b.family == "R596"


def test_classify_r580_u9_582_53():
    b = driver_info.classify("582.53")
    assert b.parsed
    assert b.major == 582
    assert b.family == "R582"


def test_classify_handles_whitespace_and_trailing_garbage():
    b = driver_info.classify("  596.36-someBuild  ")
    assert b.major == 596 and b.minor == 36


def test_classify_empty_or_malformed_returns_unparsed():
    assert not driver_info.classify("").parsed
    assert not driver_info.classify("not-a-version").parsed
    assert not driver_info.classify("596").parsed  # missing minor


# ---------------------------------------------------------------------------
# is_blackwell_pro()
# ---------------------------------------------------------------------------


def test_blackwell_pro_matches_documented_skus():
    assert driver_info.is_blackwell_pro("NVIDIA RTX PRO 6000 Blackwell Workstation Edition")
    assert driver_info.is_blackwell_pro("RTX PRO 6000 Blackwell Max-Q")
    assert driver_info.is_blackwell_pro("rtx pro 4000 blackwell sff edition")
    assert driver_info.is_blackwell_pro("RTX PRO 2000 Blackwell")


def test_blackwell_pro_rejects_other_generations():
    # Ada / Ampere generation pro GPUs and consumer Blackwells must not match
    # the pro Blackwell rule (they have different driver paths and aren't
    # subject to the sync-firmware advisory).
    assert not driver_info.is_blackwell_pro("NVIDIA RTX A6000")
    assert not driver_info.is_blackwell_pro("NVIDIA RTX 6000 Ada Generation")
    assert not driver_info.is_blackwell_pro("GeForce RTX 5090")
    assert not driver_info.is_blackwell_pro("")


# ---------------------------------------------------------------------------
# check_known_issues()
# ---------------------------------------------------------------------------


def test_blackwell_596_36_fires_sync_firmware_advisory():
    gpus = [_Gpu(name="NVIDIA RTX PRO 6000 Blackwell Workstation Edition", driver_version="596.36")]
    msgs = driver_info.check_known_issues(gpus)
    assert any("Timing Server" in m for m in msgs)
    assert any("v3.04" in m and "v3.06" in m for m in msgs)
    # Forum link must be present so operators can read the upstream report.
    assert any("forums.developer.nvidia.com" in m for m in msgs)
    # Operators must be told what the symptom looks like in nvmon's own
    # output (source drift in /qsync/verify), since they're locked on the
    # buggy firmware and need to know how to detect it themselves.
    assert any("source" in m and ("drift" in m or "verify" in m) for m in msgs)


def test_blackwell_advisory_fires_on_any_r595_driver():
    # Users reproduced the timing-server failure on R595 U4/U5/U6
    # (596.36/596.59/596.72, forum thread 362499, Aug 2026) and the bug is
    # firmware-side (v3.04+ is mandatory on Blackwell), so the advisory is
    # keyed on hardware only, not on one driver build.
    for drv in ("582.53", "596.59", "596.72", "596.86"):
        gpus = [_Gpu(name="RTX PRO 6000 Blackwell", driver_version=drv)]
        msgs = driver_info.check_known_issues(gpus)
        assert any("Timing Server" in m for m in msgs), drv


def test_r595_pre_u7_driver_fires_upgrade_advisory_on_any_gpu():
    # 596.72 (U6) is older than the recommended U7 596.86 -> advisory, even on Ada.
    gpus = [_Gpu(name="NVIDIA RTX 6000 Ada Generation", driver_version="596.72")]
    msgs = driver_info.check_known_issues(gpus)
    assert len(msgs) == 1
    assert "596.86" in msgs[0] and "59.94" in msgs[0]


def test_r595_u7_and_other_branches_do_not_fire_upgrade_advisory():
    for drv in ("596.86", "582.53", "610.88"):
        gpus = [_Gpu(name="NVIDIA RTX A6000", driver_version=drv)]
        assert driver_info.check_known_issues(gpus) == [], drv


def test_596_36_on_non_blackwell_does_not_fire():
    # Same driver on Ada — advisory must NOT fire because the bug is gated
    # on Blackwell hardware (Ada was on the working v2.02 firmware).
    gpus = [_Gpu(name="NVIDIA RTX 6000 Ada Generation", driver_version="596.36")]
    msgs = driver_info.check_known_issues(gpus)
    assert not any("Timing Server" in m for m in msgs)


def test_empty_gpu_list_returns_empty():
    assert driver_info.check_known_issues([]) == []


def test_mixed_fleet_one_blackwell_node_fires_advisory():
    # Realistic case: a node has multiple GPUs and only one is the Blackwell
    # PRO with the sync card. The advisory should fire because the affected
    # hardware is present at all.
    gpus = [
        _Gpu(name="NVIDIA RTX A4000", driver_version="596.36"),
        _Gpu(name="NVIDIA RTX PRO 6000 Blackwell Workstation Edition", driver_version="596.36"),
    ]
    msgs = driver_info.check_known_issues(gpus)
    assert sum("Timing Server" in m for m in msgs) == 1


def test_blackwell_596_36_with_buggy_firmware_sharpens_advisory():
    # Live FW read confirms the node is on the affected firmware. Message
    # must lead with the CONFIRMED tag and embed the actual version so the
    # operator can paste it into a ticket without having to look it up.
    gpus = [_Gpu(name="RTX PRO 6000 Blackwell", driver_version="596.36")]
    msgs = [m for m in driver_info.check_known_issues(gpus, sync_card_firmware="3.06")
            if "Timing Server" in m]
    assert len(msgs) == 1
    assert msgs[0].startswith("[CONFIRMED:")
    assert "v3.06" in msgs[0]


def test_blackwell_596_36_with_clean_firmware_suppresses_advisory():
    # If NVIDIA ever ships a fixed firmware (say v3.10), the advisory must
    # stop firing on nodes that have it. The bug isn't a property of the
    # driver alone — it's the (driver, firmware, hardware) triple.
    gpus = [_Gpu(name="RTX PRO 6000 Blackwell", driver_version="596.36")]
    msgs = driver_info.check_known_issues(gpus, sync_card_firmware="3.10")
    assert not any("Timing Server" in m for m in msgs)


def test_blackwell_596_36_with_unknown_firmware_falls_back_to_generic():
    # NVWMI didn't expose the firmware (either driver schema drift or the
    # node is missing PowerShell). The advisory should still fire in its
    # "may be affected" form — silently dropping the warning when we
    # can't read the FW would hide the bug from operators on a misconfigured
    # node.
    gpus = [_Gpu(name="RTX PRO 6000 Blackwell", driver_version="596.36")]
    msgs = [m for m in driver_info.check_known_issues(gpus, sync_card_firmware="")
            if "Timing Server" in m]
    assert len(msgs) == 1
    assert not msgs[0].startswith("[CONFIRMED:")
    assert "v3.04" in msgs[0] and "v3.06" in msgs[0]
