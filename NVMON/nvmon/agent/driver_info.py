"""Driver-branch awareness — detect which NVIDIA driver branch is loaded
and surface known-issue warnings tied to specific branch + hardware combos.

What this module is for
-----------------------
nvmon's subsystems all use stable CLI surfaces (``configureQsync2.exe``,
``configureMosaic.exe``, ``configureDdisplay.exe``, ``nvidia-smi`` CSV) so
the *parsing* of driver output is largely branch-agnostic. What this module
adds is a thin **diagnostic** layer on top: classify the loaded driver
version into a branch tuple (R580 U9, R595 U4, etc.), match the GPU model
against known Blackwell SKUs, and emit warnings for documented
hardware+branch interactions that operators need to know about before
fan-out.

The output is consumed by ``monitor.py``, which folds any warnings into
``status.json.errors[]`` so the dashboard surfaces them on every node card.
This is intentionally read-only / advisory — nothing here mutates state or
changes capability gating. If a subsystem's exe is missing, that's still
the existing ``capability().available = False`` path; this module only
adds *additional* operator-facing warnings on top.

Branch identification
---------------------
NVIDIA ships two co-versioned WHQL bundles per release: a Studio / GeForce
bundle and an RTX Enterprise (Quadro) Production Branch bundle, both with
the same ``driver_version`` string. From a single ``driver_version`` we
can't tell which SKU was installed — that's a registry lookup we don't
currently do. So the branch classification here is best-effort by version
number only; the surfaced warnings are gated on hardware (GPU model) and
versions, not branch SKU.

Known-issue table
-----------------
``KNOWN_ISSUES`` is a small static table mapping (driver-version, gpu-name
prefix) tuples to human-readable warning strings. Add entries here as new
issues land in NVIDIA's release notes and forums. Each entry must include
a source URL in the message so an operator can read the upstream report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Branch classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriverBranch:
    """Classified driver version.

    ``raw`` is the verbatim string from nvidia-smi (e.g. "596.36").
    ``major`` / ``minor`` are the parsed integers; both are 0 when the
    string didn't parse. ``family`` is one of "R596", "R580", "R595", or
    "" when unknown — these are the NVIDIA branch families documented at
    nvidia.com/en-us/drivers/rtx-enterprise-and-quadro-driver-branch-history/.
    """
    raw: str = ""
    major: int = 0
    minor: int = 0
    family: str = ""

    @property
    def parsed(self) -> bool:
        return self.major > 0


def classify(driver_version: str) -> DriverBranch:
    """Parse a driver version string into a branch tuple.

    Accepts the ``"<major>.<minor>"`` form nvidia-smi emits. Anything else
    (empty, malformed, future schema change) falls back to a zero-valued
    DriverBranch — callers should treat that as "unknown" and skip
    branch-keyed checks.
    """
    if not driver_version:
        return DriverBranch()
    m = re.match(r"^\s*(\d+)\.(\d+)", driver_version.strip())
    if not m:
        return DriverBranch(raw=driver_version)
    major, minor = int(m.group(1)), int(m.group(2))
    family = _family_for_major(major)
    return DriverBranch(raw=driver_version, major=major, minor=minor, family=family)


def _family_for_major(major: int) -> str:
    # NVIDIA's branch families on Windows track the leading three digits of
    # the version: 580.x → R580, 595.x → R595, 596.x → R596 (consumer) /
    # R595 U4 (enterprise share the 596.36 number). We don't try to
    # disambiguate enterprise vs studio here; the major prefix is what we
    # key on for known-issue lookups.
    if major <= 0:
        return ""
    return f"R{major}"


# ---------------------------------------------------------------------------
# GPU model classification
# ---------------------------------------------------------------------------

# Blackwell pro SKUs documented as of 2026-05. Extend as NVIDIA ships more.
# Match is case-insensitive substring. Keep entries narrow so we don't
# false-positive on future Ada/Hopper/Rubin parts.
_BLACKWELL_PRO_NAMES: tuple[str, ...] = (
    "rtx pro 6000 blackwell",
    "rtx pro 5000 blackwell",
    "rtx pro 4500 blackwell",
    "rtx pro 4000 blackwell",
    "rtx pro 2000 blackwell",
)


def is_blackwell_pro(gpu_name: str) -> bool:
    """True when the GPU model string matches a known Blackwell RTX PRO SKU."""
    if not gpu_name:
        return False
    n = gpu_name.lower()
    return any(p in n for p in _BLACKWELL_PRO_NAMES)


# ---------------------------------------------------------------------------
# Known-issue rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownIssue:
    """A rule that emits a warning when the (driver, hardware) combo matches."""
    id: str
    message: str
    # When set, the issue applies only when the loaded driver version equals
    # one of these (e.g. {"596.36"}). Empty set = applies to any driver.
    driver_versions: frozenset[str] = frozenset()
    # Predicate over the GPU list — if any GPU matches, the warning fires.
    # Callers pass a list of (name, driver_version) tuples.
    requires_blackwell_pro: bool = False


# RTX PRO Sync firmware versions documented as having the Blackwell timing-
# server bug. v3.04 was the first Blackwell-mandatory firmware; v3.06 inherited
# the bug. Update if NVIDIA publishes a fixed firmware (then add the fixed
# version to a separate "known good" list).
BUGGY_SYNC_FIRMWARES: frozenset[str] = frozenset({"3.04", "3.06"})


# Latest R595 Production Branch (RTX Enterprise) driver, verified against
# nvidia.com on 2026-10-29: R595 U7 = 596.86 (2026-07-30). Fixes "[Mosaic]
# some configurations output 60.00 Hz instead of 59.94 Hz" and is the fixed
# version for NVIDIA's September-2026 display-driver security bulletin
# (every R595 build before 596.86 is affected). The R610 New Feature Branch
# (610.88, 2026-07-30) also exists; nvmon doesn't key on it.
R595_RECOMMENDED_DRIVER = "596.86"
# Latest RTX PRO Sync firmware on nvidia.com as of 2026-10-29. No 3.07+ exists.
LATEST_SYNC_FIRMWARE = "3.06"


def _version_tuple(v: str) -> tuple[int, int]:
    b = classify(v)
    return (b.major, b.minor)


# Static table. Each entry needs a source URL in the message so an operator
# can read the upstream report and decide whether to defer the upgrade.
KNOWN_ISSUES: tuple[KnownIssue, ...] = (
    KnownIssue(
        id="blackwell-sync-fw-3.04-3.06-timing-server",
        message=(
            "Sync compatibility advisory: RTX PRO Blackwell + RTX PRO Sync "
            "FW v3.04/v3.06 has a reported bug where 'Timing Server: "
            "External' / 'On this system' fails to apply (reproduced by "
            "users on R595 U4/U5/U6 = 596.36/596.59/596.72, still open "
            "2026-08; NVIDIA says it works in their lab and that sync "
            "tolerances were TIGHTENED on Blackwell, and recommends R595 U7 "
            "596.86 + testing with configureQsync2 queryAll first without "
            "house sync). nvmon drives configureQsync2.exe (CLI), not the "
            "NVCP UI. Practical rules: house-sync rate must match the "
            "display rate exactly (nvmon's rate check enforces 0.02 Hz); "
            "the canary is qsync verify reporting 'source' drift (expected "
            "HOUSESYNC, actual VSYNC/INTERNAL) after enable_from_profile. "
            "Blackwell mandates FW >= v3.04, so rolling back to v2.02 is "
            "not an option. Ref: https://forums.developer.nvidia.com/t/362499"
        ),
        requires_blackwell_pro=True,
    ),
)


def _r595_driver_advisories(drivers: set[str]) -> list[str]:
    """Advisories keyed on driver version only (any GPU)."""
    out: list[str] = []
    for d in sorted(drivers):
        major, minor = _version_tuple(d)
        if major == 596 and (major, minor) < _version_tuple(R595_RECOMMENDED_DRIVER):
            out.append(
                f"Driver advisory: {d} is an R595 build older than U7 "
                f"({R595_RECOMMENDED_DRIVER}, 2026-07-30). U7 fixes Mosaic "
                f"outputting 60.00 Hz instead of 59.94 Hz on some configs and "
                f"closes NVIDIA's Sept-2026 display-driver security bulletin "
                f"(all R595 < 596.86 affected). Upgrade the whole fleet to the "
                f"same build. Ref: https://www.nvidia.com/Download/driverResults.aspx/209270"
            )
    return out


def check_known_issues(
    gpus: Iterable, *, sync_card_firmware: str = "",
) -> list[str]:
    """Return advisory messages for every KNOWN_ISSUES rule that fires.

    ``gpus`` is anything iterable yielding objects with ``name`` and
    ``driver_version`` attributes (typically the agent's ``list[GPUInfo]``).
    Empty list or no matches → ``[]``.

    ``sync_card_firmware`` is the live RTX PRO Sync firmware version (e.g.
    "3.06") read from ``framelock.status().sync_card_firmware``. When
    provided, the Blackwell timing-server advisory is suppressed if the
    firmware is NOT in ``BUGGY_SYNC_FIRMWARES`` (the bug doesn't apply),
    and is sharpened to a confirmed-affected message if it IS. When empty
    (NVWMI didn't expose the field) the advisory falls back to the
    generic "may be affected" form.
    """
    gpu_list = list(gpus)
    if not gpu_list:
        return []

    out: list[str] = []
    has_blackwell_pro = any(is_blackwell_pro(getattr(g, "name", "")) for g in gpu_list)
    drivers = {(getattr(g, "driver_version", "") or "").strip() for g in gpu_list}
    fw = (sync_card_firmware or "").strip()

    for issue in KNOWN_ISSUES:
        if issue.requires_blackwell_pro and not has_blackwell_pro:
            continue
        if issue.driver_versions and not (drivers & issue.driver_versions):
            continue

        message = issue.message
        # Sharpen / suppress the Blackwell timing-server advisory based on
        # observed firmware. Other rules pass through unchanged.
        if issue.id == "blackwell-sync-fw-3.04-3.06-timing-server" and fw:
            if fw not in BUGGY_SYNC_FIRMWARES:
                # FW is known and NOT in the buggy set — the rule does not
                # apply to this node; skip the advisory entirely.
                continue
            # Confirmed-affected: prepend the live firmware version so the
            # operator sees "this node IS on v3.06" rather than "may be."
            message = (
                f"[CONFIRMED: this node is on RTX PRO Sync FW v{fw}] "
                + message
            )

        out.append(message)
    out.extend(_r595_driver_advisories(drivers))
    return out
