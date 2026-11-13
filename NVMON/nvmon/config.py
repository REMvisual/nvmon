"""Configuration: paths, TOML loading, defaults, exe discovery, admin check.

Read `nvmon.toml` from BASE_DIR if present, otherwise fall back to built-in
defaults. All site-specific constants (server IPs, display-name prefixes,
hardcoded grid sizes) have been removed from the code — they now live in
`nvmon.toml`.

Public accessors:
    get_agent_config()      -> dict
    get_controller_config() -> dict
    get_launcher_config()   -> dict
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import tomllib
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Base paths — everything relative to the nvmon package's parent
# ---------------------------------------------------------------------------

# BASE_DIR is F:\nvmon\ (the parent of the nvmon package directory).
BASE_DIR = Path(__file__).resolve().parent.parent

PROFILES_DIR = BASE_DIR / "profiles"
LOGS_DIR = BASE_DIR / "logs"
SECRETS_DIR = BASE_DIR / "secrets"
BIN_DIR = BASE_DIR / "bin"
STATUS_FILE = BASE_DIR / "status.json"
CONFIG_FILE = BASE_DIR / "nvmon.toml"
# Zero-touch fallback for the agent's controller_url. When [agent].controller_url
# is not set in nvmon.toml, the agent reads this single-line file on boot and
# uses its contents as the push-registration target. The workspace ships this
# file pre-filled with the monitor's URL so FreeFileSync mirrors it to every
# render node — no per-node operator typing.
MONITOR_URL_FILE = BASE_DIR / "monitor.url"


# ---------------------------------------------------------------------------
# Built-in defaults (used when nvmon.toml is missing or incomplete)
# ---------------------------------------------------------------------------

DEFAULT_AGENT_PORT = 9998
DEFAULT_CONTROLLER_PORT = 9997
DEFAULT_LAUNCHER_PORT = 9996

DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_FANOUT_TIMEOUT_SECONDS = 15
DEFAULT_POLL_TIMEOUT_SECONDS = 5
MAX_POLL_WORKERS = 64


def _node_ip_to_url(raw: str) -> str:
    """'10.0.0.5' / '10.0.0.5:9998' / 'http://10.0.0.5:9998' -> agent URL."""
    s = raw.strip().strip('"').strip("'").rstrip("/")
    if not s:
        return ""
    if "://" in s:
        return s
    if ":" in s and not s.startswith("["):
        host, _, port = s.rpartition(":")
        if port.isdigit():
            return f"http://{host}:{int(port)}"
    return f"http://{s}:{DEFAULT_AGENT_PORT}"


def _node_ip_label(url: str) -> str:
    """Placeholder hostname for an IP-list node until /health tells us."""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


def node_profile_for(hostname: str, node_profiles: dict[str, str]) -> str:
    """The configured profile for ``hostname`` from [controller].node_profiles.

    Match is case-insensitive on the leading DNS label (same rule as
    apply_excluded_hosts), so "node-02", "NODE-02" and "Node-02.lan" all
    hit the same entry. Returns '' when nothing is configured.
    """
    if not hostname or not node_profiles:
        return ""
    want = hostname.strip().lower().split(".")[0]
    for h, p in node_profiles.items():
        if h.strip().lower().split(".")[0] == want:
            return p
    return ""


# ---------------------------------------------------------------------------
# Profile templates (v0.8.2)
#
# A "template" is the shared prefix of a matched set of profiles — e.g. the
# rate/variant "59.94 - 10b" in "59.94 - 10b - SyncHost" + "59.94 - 10b -
# SyncClient". [controller.node_profiles] values may carry the literal token
# "{template}" where that prefix goes:
#
#     [controller.node_profiles]
#     "NODE-01" = "{template} - SyncHost"
#     "NODE-02" = "{template} - SyncClient"
#
# The controller discovers which templates exist by matching every distinct
# pattern against the profile names on disk; a template is offered only when
# EVERY pattern has a profile for it (no half-applied fleets). The dashboard
# "Template" button and POST /all/template {"template": "..."} pick one;
# Re-apply All keeps using the last-picked (or template_default) template.
# ---------------------------------------------------------------------------

TEMPLATE_TOKEN = "{template}"


def template_patterns(node_profiles: dict[str, str]) -> list[str]:
    """Distinct node_profiles values that contain the {template} token."""
    return sorted({
        str(v) for v in (node_profiles or {}).values()
        if isinstance(v, str) and TEMPLATE_TOKEN in v
    })


def resolve_node_profiles(node_profiles: dict[str, str], template: str) -> dict[str, str]:
    """Substitute ``template`` into every templated entry.

    Entries without the token pass through unchanged. Templated entries are
    DROPPED when ``template`` is blank — the caller then falls back to the
    node's active profile exactly as if the entry didn't exist.
    """
    t = (template or "").strip()
    out: dict[str, str] = {}
    for h, p in (node_profiles or {}).items():
        if not isinstance(p, str):
            continue
        if TEMPLATE_TOKEN in p:
            if not t:
                continue
            out[h] = p.replace(TEMPLATE_TOKEN, t)
        else:
            out[h] = p
    return out


def _template_sort_key(s: str) -> tuple:
    """Natural sort so "24", "29.97", "30", "59.94", "60" order numerically."""
    parts: list = []
    for chunk in re.split(r"(\d+(?:\.\d+)?)", s or ""):
        if not chunk:
            continue
        try:
            parts.append((1, float(chunk)))
        except ValueError:
            parts.append((0, chunk.lower()))
    return tuple(parts)


def discover_templates(node_profiles: dict[str, str], profile_names) -> list[str]:
    """Templates for which every templated pattern has a matching profile.

    Match is case-insensitive on the whole name; the token matches any
    non-empty text. Returns a naturally-sorted list ("24" < "59.94").
    """
    patterns = template_patterns(node_profiles)
    if not patterns:
        return []
    names = {str(n).strip() for n in (profile_names or []) if str(n).strip()}
    per_pattern: list[set[str]] = []
    for pat in patterns:
        rx = re.compile(
            "^" + re.escape(pat).replace(re.escape(TEMPLATE_TOKEN), "(.+?)") + "$",
            re.IGNORECASE,
        )
        found: set[str] = set()
        for n in names:
            m = rx.match(n)
            if m and m.group(1).strip():
                found.add(m.group(1).strip())
        per_pattern.append(found)
    common = set.intersection(*per_pattern) if per_pattern else set()
    return sorted(common, key=_template_sort_key)

# GPU alert thresholds
DEFAULT_GPU_TEMP_WARNING_C = 85
DEFAULT_GPU_POWER_WARNING_PCT = 95

# Subsystem-operation timing (seconds). Kept at module level so they are easy
# to tune centrally in one place.
QSYNC_WAIT_TIME = 20
# Lock polling cap. Every real lock observed on A6000/RTX PRO Sync hardware
# locked in ~3s; a longer cap only ever waited on locks that could not
# happen (house clock at another rate, sync partner rebooting). nvdd went
# 45 -> 90 -> 20 on hardware evidence (the legacy tool 487ff42).
QSYNC_SYNC_TIMEOUT = 20
# The ONE rate-match tolerance: display vs house sync, live vs profile,
# client vs its timing server. 23.976 and 24.000 are different timings for
# sync purposes (a 24.000 house clock never locks a 23.976 panel) and sit
# only 0.024 Hz apart, so the tolerance must be well under that. The
# NVIDIA tools disagree with each other at the third decimal (configureQsync2
# says 23.975 where configureDdisplay says 23.976), so not tighter than 0.01.
# History: the old 0.05 Hz "23.976 satisfies 24 convention" made the apply
# skip the mode set ("already at 24") and left every site client at 23.976
# under a 24 Hz profile while the validator stayed green (2026-11-02).
RATE_MATCH_TOLERANCE_HZ = 0.01


def rate_tolerance(profile_tolerance: float | None = None) -> float:
    """Effective rate-match tolerance for a profile.

    A profile's ``validation.refresh_rate_tolerance`` may tighten the match,
    never loosen it: every nvmon-captured profile before 2026-11-02 carries
    0.05, which would re-open the 23.976-counts-as-24 hole. Legacy values
    below 0.002 are floored (tighter than the tools' decimal consistency).
    """
    try:
        t = float(profile_tolerance or 0.0)
    except (TypeError, ValueError):
        t = 0.0
    if t <= 0:
        return RATE_MATCH_TOLERANCE_HZ
    return max(0.002, min(t, RATE_MATCH_TOLERANCE_HZ))
# How long the agent waits at boot for the NVIDIA driver to answer
# (nvidia-smi + configureQsync2 queryAll) before running boot-apply. On a
# cold boot the Scheduled Task fires before the display driver has finished
# enumerating; applying a profile then fails or half-applies.
DRIVER_READY_TIMEOUT = 60
DRIVER_READY_POLL = 5
QSYNC_POLL_INTERVAL = 3
DD_TO_QSYNC_SETTLE = 10
MOSAIC_STABILIZE_TIME = 5
DD_RETURN_WAIT = 2
MOSAIC_DISABLE_WAIT = 3
QSYNC_DISABLE_WAIT = 2
DISPLAY_SETTLE_TIME = 3


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------

_loaded_config: dict[str, Any] | None = None


def _load_config_file() -> dict[str, Any]:
    """Load nvmon.toml from BASE_DIR. Returns {} if missing or unreadable."""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        # Intentionally silent here — callers can report via logger.
        # config.py is imported before log.py is set up.
        return {}


def load_config(*, force_reload: bool = False) -> dict[str, Any]:
    """Load and cache the full nvmon.toml content.

    Subsequent calls return the cached dict unless force_reload=True.
    """
    global _loaded_config
    if _loaded_config is None or force_reload:
        _loaded_config = _load_config_file()
    return _loaded_config


def _section(name: str) -> dict[str, Any]:
    """Return a top-level TOML section as a dict, or {} if missing."""
    cfg = load_config()
    val = cfg.get(name, {})
    return val if isinstance(val, dict) else {}


def _read_monitor_url_file() -> str:
    """Read BASE_DIR/monitor.url and return its first non-blank, non-comment line.

    Zero-touch fallback for the agent's controller_url. The file is just a
    URL — no parsing nuance, no quoting. Lines starting with `#` are skipped
    so the shipped example file can carry documentation. Missing file or
    empty contents return "" (registration stays disabled, legacy behavior).
    """
    try:
        if not MONITOR_URL_FILE.is_file():
            return ""
        text = MONITOR_URL_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Operators sometimes wrap the URL in quotes (http://"10.0.0.5":9997).
        # A URL never legitimately contains a quote, and embedded quotes make
        # getaddrinfo() fail ([Errno 11001]) because the host becomes the
        # literal string "10.0.0.5" with quotes. Strip them anywhere in the line.
        return stripped.replace('"', "").replace("'", "").strip()
    return ""


# ---------------------------------------------------------------------------
# Public config accessors — each returns a dict with all spec-defined keys
# ---------------------------------------------------------------------------

def _as_int(value: Any, default: int) -> int:
    """Tolerant int: TOML ints pass through; numeric strings too; else default."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    """Tolerant bool: TOML bools pass through; "false"/"0"/"no" strings too."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def get_agent_config() -> dict[str, Any]:
    """Return the merged [agent] config with defaults filled in."""
    section = _section("agent")
    gpu_section = section.get("gpu", {}) if isinstance(section.get("gpu"), dict) else {}
    displays_section = section.get("displays", {}) if isinstance(section.get("displays"), dict) else {}
    bin_section = section.get("bin", {}) if isinstance(section.get("bin"), dict) else {}

    # Resolve excluded_resolutions: TOML gives us a list of [w, h] pairs;
    # normalize to list of (int, int) tuples.
    raw_res = displays_section.get("excluded_resolutions", []) or []
    excluded_resolutions: list[tuple[int, int]] = []
    for entry in raw_res:
        try:
            w, h = entry
            excluded_resolutions.append((int(w), int(h)))
        except (TypeError, ValueError):
            continue

    # controller_url — when set, the agent push-registers with this
    # controller on boot via POST /register, then heartbeats every
    # `register_interval_seconds`. Reliable TCP path that doesn't depend
    # on multicast surviving the LAN. Example:
    #     controller_url = "http://192.0.2.10:9997"
    #
    # Zero-touch fallback: if controller_url is not set in nvmon.toml,
    # we read BASE_DIR/monitor.url. That file ships in the deployed
    # NVVMON tree so FreeFileSync mirrors it to every render node —
    # the operator edits it once in the workspace and every node picks
    # up the monitor URL on its next boot. Empty value or missing file
    # = registration disabled (legacy mDNS / static [[controller.nodes]]
    # only).
    controller_url = str(section.get("controller_url", "") or "").strip()
    controller_url = controller_url.replace('"', "").replace("'", "").strip()
    if not controller_url:
        controller_url = _read_monitor_url_file()
    try:
        register_interval_seconds = int(section.get("register_interval_seconds", 60))
    except (TypeError, ValueError):
        register_interval_seconds = 60

    return {
        "port": int(section.get("port", DEFAULT_AGENT_PORT)),
        "poll_interval_seconds": int(
            section.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        ),
        "profile_dir": str(section.get("profile_dir", str(PROFILES_DIR))),
        "status_file": str(section.get("status_file", str(STATUS_FILE))),
        "log_dir": str(section.get("log_dir", str(LOGS_DIR))),
        "controller_url": controller_url,
        "register_interval_seconds": register_interval_seconds,
        # Operator-declared RTX PRO Sync firmware (e.g. "3.06"). Used only
        # when the agent cannot read the live value (NVCP topology JSON /
        # NVWMI don't expose it on every driver). Lets driver_info confirm
        # or suppress the Blackwell timing-server advisory. Blank = unknown.
        "sync_card_firmware": str(section.get("sync_card_firmware", "") or "").strip().lstrip("vV"),
        # driver_advisories — emit known driver/firmware advisories into
        # status.advisories (and the agent log, once). False = silent; use
        # once the operator has read and acknowledged them for this fleet.
        "driver_advisories": _as_bool(section.get("driver_advisories", True), True),
        # boot_apply_max_uptime_minutes — the boot-apply (replay of
        # active_profile.json) only runs when Windows has been up for less
        # than this. A listener restart hours after boot (code pickup via
        # "Restart listener", a supervisor respawn) is NOT a boot: the
        # driver state is still whatever the last apply left, so replaying
        # the profile only churns the wall. 0 = always run the boot-apply.
        "boot_apply_max_uptime_minutes": _as_int(
            section.get("boot_apply_max_uptime_minutes", 15), 15,
        ),
        "gpu": {
            "temp_warning_c": int(
                gpu_section.get("temp_warning_c", DEFAULT_GPU_TEMP_WARNING_C)
            ),
            "power_warning_pct": int(
                gpu_section.get("power_warning_pct", DEFAULT_GPU_POWER_WARNING_PCT)
            ),
        },
        "displays": {
            "excluded_name_prefixes": list(
                displays_section.get("excluded_name_prefixes", []) or []
            ),
            "excluded_resolutions": excluded_resolutions,
        },
        "bin": {
            "nvidia_smi": str(bin_section.get("nvidia_smi", "") or ""),
            "configure_mosaic": str(bin_section.get("configure_mosaic", "") or ""),
            "configure_ddisplay": str(bin_section.get("configure_ddisplay", "") or ""),
            "configure_qsync2": str(bin_section.get("configure_qsync2", "") or ""),
        },
    }


def get_controller_config() -> dict[str, Any]:
    """Return the merged [controller] config with defaults filled in.

    `nodes` and `groups` are lists from [[controller.nodes]] / [[controller.groups]].
    """
    section = _section("controller")

    raw_nodes = section.get("nodes", []) or []
    nodes: list[dict[str, Any]] = []
    for n in raw_nodes:
        if not isinstance(n, dict):
            continue
        nodes.append({
            "hostname": str(n.get("hostname", "") or ""),
            "url": str(n.get("url", "") or ""),
        })

    # node_profiles — which profile each node gets on "Re-apply All" when the
    # fleet runs mixed profiles (QSync master vs daisy-chain clients, a 1080p
    # box in a 4K fleet). Two spellings, merged:
    #     [controller.node_profiles]
    #     "RENDER-01" = "24_Server"
    #     "RENDER-02" = "24_Client"
    # or `profile = "24_Client"` inside a [[controller.nodes]] block.
    # Keys are hostnames (case-insensitive, leading DNS label). Precedence
    # at apply time: this map > the node's currently active profile.
    node_profiles: dict[str, str] = {}
    raw_map = section.get("node_profiles", {}) or {}
    if isinstance(raw_map, dict):
        for h, p in raw_map.items():
            if isinstance(h, str) and isinstance(p, str) and h.strip() and p.strip():
                node_profiles[h.strip()] = p.strip()
    for n in raw_nodes:
        if isinstance(n, dict) and isinstance(n.get("profile"), str) and n["profile"].strip():
            host = str(n.get("hostname", "") or "").strip()
            if host:
                node_profiles[host] = n["profile"].strip()

    # node_ips — the "I just have a list of IPs" form of [[controller.nodes]].
    # Each entry is "ip" or "ip:port" (default agent port). The hostname is
    # learned from the agent's /health on the first successful poll and
    # the registry entry is renamed to it; until then the card shows the
    # IP. Entries whose IP already appears in [[controller.nodes]] are
    # ignored (the explicit block wins).
    raw_ips = section.get("node_ips", []) or []
    known_urls = {n["url"].rstrip("/").lower() for n in nodes}
    if isinstance(raw_ips, list):
        for raw in raw_ips:
            if not isinstance(raw, str) or not raw.strip():
                continue
            url = _node_ip_to_url(raw.strip())
            if not url or url.lower() in known_urls:
                continue
            known_urls.add(url.lower())
            nodes.append({"hostname": _node_ip_label(url), "url": url, "pending_hostname": True})

    raw_groups = section.get("groups", []) or []
    groups: list[dict[str, Any]] = []
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        groups.append({
            "name": str(g.get("name", "") or ""),
            "nodes": [str(x) for x in (g.get("nodes", []) or [])],
        })

    # Hostnames the operator wants the controller to NEVER include in a
    # /fanout/job action="apply". sync, restore, restart, restart_listener,
    # etc. all still hit them — only profile apply is filtered out. Use
    # case: NODE-11 has 1080p where the rest of the fleet is 4K, so the
    # standard "default" profile would break NODE-11 if applied. The
    # operator can later make a special profile and remove it from this
    # list. Hostname compare is case-insensitive.
    raw_block = section.get("apply_excluded_hosts", []) or []
    apply_excluded_hosts: list[str] = []
    if isinstance(raw_block, list):
        for h in raw_block:
            if isinstance(h, str) and h.strip():
                apply_excluded_hosts.append(h.strip())

    # registered_node_ttl_hours — auto-evict source="registered" entries that
    # haven't heartbeated for this long. 0 or negative disables the sweep.
    try:
        registered_node_ttl_hours = int(section.get("registered_node_ttl_hours", 24))
    except (TypeError, ValueError):
        registered_node_ttl_hours = 24

    # poll_timeout_seconds — per-node /health + /status timeout for the
    # background poll. Separate from fanout_timeout_seconds: a dead node
    # must not hold a 5-second poll cycle for 15 s. poll_workers — how many
    # nodes are polled in parallel; 0 = auto (one per node, capped at 64).
    try:
        poll_timeout_seconds = int(section.get("poll_timeout_seconds", DEFAULT_POLL_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        poll_timeout_seconds = DEFAULT_POLL_TIMEOUT_SECONDS
    try:
        poll_workers = int(section.get("poll_workers", 0))
    except (TypeError, ValueError):
        poll_workers = 0

    return {
        "port": int(section.get("port", DEFAULT_CONTROLLER_PORT)),
        "poll_interval_seconds": int(
            section.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        ),
        "fanout_timeout_seconds": int(
            section.get("fanout_timeout_seconds", DEFAULT_FANOUT_TIMEOUT_SECONDS)
        ),
        "poll_timeout_seconds": max(1, poll_timeout_seconds),
        "poll_workers": max(0, poll_workers),
        "node_profiles": node_profiles,
        # template_default — the {template} value Re-apply All uses until an
        # operator / external system picks one via the Template button or
        # POST /all/template. The last pick is persisted on the controller.
        "template_default": str(section.get("template_default", "") or "").strip(),
        "password_file": str(
            section.get("password_file", str(SECRETS_DIR / "controller.password"))
        ),
        "nodes": nodes,
        "groups": groups,
        "apply_excluded_hosts": apply_excluded_hosts,
        "registered_node_ttl_hours": registered_node_ttl_hours,
    }


def get_launcher_config() -> dict[str, Any]:
    """Return the merged [launcher] config with defaults filled in."""
    section = _section("launcher")

    raw_apps = section.get("apps", []) or []
    apps: list[dict[str, Any]] = []
    for a in raw_apps:
        if isinstance(a, dict):
            apps.append(dict(a))

    return {
        "port": int(section.get("port", DEFAULT_LAUNCHER_PORT)),
        "apps": apps,
    }


# ---------------------------------------------------------------------------
# NVIDIA CLI executable discovery
# ---------------------------------------------------------------------------

def _find_exe(name: str, override: str = "") -> str:
    """Find an NVIDIA CLI exe.

    Resolution order:
      1. Explicit override path from config (if file exists).
      2. BIN_DIR / name (bundled copy in bin\).
      3. BASE_DIR / name (legacy layout).
      4. PATH lookup.
    """
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)

    local = BIN_DIR / name
    if local.is_file():
        return str(local)

    legacy = BASE_DIR / name
    if legacy.is_file():
        return str(legacy)

    import shutil
    found = shutil.which(name)
    if found:
        return found

    return ""


# These tools are NVIDIA software (NVIDIA licence terms) and ship in bin\.
# If one has gone missing, say where it goes and what it is.
NVIDIA_TOOL_SOURCES = {
    "configureMosaic.exe": "NVIDIA Mosaic Utility (configureMosaic)",
    "configureDdisplay.exe": "NVIDIA Direct Display configuration utility (configureDdisplay)",
    "configureQsync2.exe": "NVIDIA Quadro Sync configuration utility (configureQsync2)",
}


def missing_exe_reason(name: str) -> str:
    """Operator-facing message for an NVIDIA CLI tool that wasn't found.

    Starts with ``"<name> not found"`` so existing log greps still match,
    then says where to put the file and where to get it.
    """
    source = NVIDIA_TOOL_SOURCES.get(name, "NVIDIA's enterprise driver tools")
    return (
        f"{name} not found - it ships in {BIN_DIR}; restore it from the nvmon "
        f"repo (or set the [agent.bin] override in nvmon.toml). It is the "
        f"{source} from NVIDIA. See README 'Bundled NVIDIA tools'."
    )


def get_mosaic_exe() -> str:
    override = get_agent_config()["bin"]["configure_mosaic"]
    return _find_exe("configureMosaic.exe", override=override)


def get_dd_exe() -> str:
    override = get_agent_config()["bin"]["configure_ddisplay"]
    return _find_exe("configureDdisplay.exe", override=override)


def get_qsync_exe() -> str:
    override = get_agent_config()["bin"]["configure_qsync2"]
    return _find_exe("configureQsync2.exe", override=override)


def get_nvidia_smi() -> str:
    """Find nvidia-smi — typically on PATH or in C:\\Windows\\System32."""
    override = get_agent_config()["bin"]["nvidia_smi"]
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)

    import shutil
    found = shutil.which("nvidia-smi")
    if found:
        return found

    fallback = r"C:\Windows\System32\nvidia-smi.exe"
    if os.path.isfile(fallback):
        return fallback
    return ""


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------

def get_hostname() -> str:
    """Return the local machine hostname."""
    return socket.gethostname()


def get_network_config() -> dict[str, Any]:
    """Return the ``[network]`` section with defaults applied.

    Keys:
        announce_ip   per-box IP override (verbatim). Beats everything.
        subnet        CIDR of the nvmon LAN (e.g. "10.20.30.0/24"). When set,
                      nvmon announces / registers with the local address that
                      sits inside this subnet, and the controller rejects
                      /register calls that come from, or point at, any other
                      network. One value, safe in a fleet-wide shared toml.
    """
    network = _section("network")
    return {
        "announce_ip": str(network.get("announce_ip", "") or "").strip(),
        "subnet": str(network.get("subnet", "") or "").strip(),
    }


def parse_subnet(text: str) -> ipaddress.IPv4Network | None:
    """Parse a CIDR string; None when blank or malformed (never raises)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return ipaddress.IPv4Network(text, strict=False)
    except ValueError:
        return None


def ip_in_subnet(ip: str, subnet: ipaddress.IPv4Network | str | None) -> bool:
    """True when ``ip`` is a valid IPv4 inside ``subnet``. A blank / unparsable
    subnet means "no restriction" and returns True."""
    net = parse_subnet(subnet) if isinstance(subnet, str) or subnet is None else subnet
    if net is None:
        return True
    try:
        return ipaddress.IPv4Address((ip or "").strip()) in net
    except ValueError:
        return False


def _local_ipv4_addresses() -> list[str]:
    """Every IPv4 address this host currently holds (best-effort, deduped)."""
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except Exception:
        pass
    return found


def local_ip_in_subnet(subnet: ipaddress.IPv4Network | str | None) -> str:
    """The local IPv4 address that lives inside ``subnet``, or "" if none.

    Two probes, in order:
      1. UDP connect() towards a host inside the subnet — the kernel picks
         the interface that routes there, and getsockname() tells us its
         address. Reflects live routing, immune to stale DNS entries.
      2. Scan every address the host holds and take the first one inside
         the subnet (covers a NIC with no route entry yet).
    """
    net = parse_subnet(subnet) if isinstance(subnet, str) or subnet is None else subnet
    if net is None:
        return ""
    target = str(net.network_address + 1) if net.num_addresses > 2 else str(net.network_address)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 9))
            ip = s.getsockname()[0]
            if ip_in_subnet(ip, net):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    for ip in _local_ipv4_addresses():
        if ip_in_subnet(ip, net):
            return ip
    return ""


_subnet_miss_warned = False


def get_local_ip() -> str:
    """Best-effort discovery of this machine's primary outbound IP.

    Override path: when ``[network].announce_ip`` is set in nvmon.toml,
    that value is returned verbatim. This is the supported escape hatch
    on multi-NIC boxes where the auto-detect picks the wrong interface
    (e.g. monitor with both office and render-subnet NICs — auto-detect
    returns the office IP, but render nodes can only reach the render IP).
    Operators set this once per box.

    Subnet pin: when ``[network].subnet`` is set (one CIDR, fleet-wide), the
    local address inside that subnet wins over the auto-detect. This is the
    multi-NIC fix that works from a single shared toml: a render node with
    a Content-Net default route still announces its Control-Net address. If the
    box has no address in the subnet we warn once and fall through.

    Auto-detect fallback: use the UDP-connect-to-public-target trick first
    — it reflects the *live* outbound interface the OS would actually use,
    so it's immune to stale DNS / hosts-cache records left over from an old
    DHCP lease (which is what makes ``gethostbyname_ex`` report e.g. .232
    when the NIC has already moved to .233). Fall back to the hostname DNS
    reply only if the socket trick fails. No subnet is assumed.
    """
    global _subnet_miss_warned
    network = get_network_config()
    if network["announce_ip"]:
        return network["announce_ip"]

    if network["subnet"]:
        net = parse_subnet(network["subnet"])
        if net is None:
            if not _subnet_miss_warned:
                _subnet_miss_warned = True
                logging.getLogger("nvmon.config").warning(
                    "[network].subnet=%r is not a valid CIDR — ignoring it",
                    network["subnet"],
                )
        else:
            pinned = local_ip_in_subnet(net)
            if pinned:
                return pinned
            if not _subnet_miss_warned:
                _subnet_miss_warned = True
                logging.getLogger("nvmon.config").warning(
                    "[network].subnet=%s but this host has no address in it "
                    "(have: %s) — falling back to auto-detect. Check the "
                    "control-net NIC / cable on this box.",
                    net, ", ".join(_local_ipv4_addresses()) or "none",
                )

    # Primary: the UDP "connect" trick. 8.8.8.8:80 is a routable target; no
    # packet is actually sent because UDP connect() just records the peer,
    # but getsockname() then reports the source IP of the interface the
    # kernel routes through — the real, current address of this box. This
    # beats gethostbyname_ex, whose answer comes from DNS/hosts and can be a
    # stale lease from before the box moved subnets.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        finally:
            s.close()
    except Exception:
        pass

    # Fallback: the DNS reply for our own hostname. Only reached if the
    # socket trick failed (e.g. no default route on an air-gapped subnet).
    try:
        for iface_ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if iface_ip and not iface_ip.startswith("127."):
                return iface_ip
    except Exception:
        pass

    return "127.0.0.1"


# ---------------------------------------------------------------------------
# Admin check
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    """Check if the current process is running with Administrator privileges."""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Display filtering
# ---------------------------------------------------------------------------

def is_excluded_display(name: str, resolution: tuple[int, int] = (0, 0)) -> bool:
    """Check if a display should be excluded from operations.

    Exclusion rules come from [agent.displays] in nvmon.toml. Defaults to
    excluding nothing.
    """
    displays_cfg = get_agent_config()["displays"]
    prefixes: list[str] = displays_cfg["excluded_name_prefixes"]
    excluded_res: list[tuple[int, int]] = displays_cfg["excluded_resolutions"]

    for prefix in prefixes:
        if prefix and name.startswith(prefix):
            return True

    if resolution != (0, 0) and resolution in excluded_res:
        # Only exclude by resolution when the name is generic/unknown.
        if not name or name in ("<not defined>", "Unknown"):
            return True

    return False


# ---------------------------------------------------------------------------
# Config writer — used by install_listener.bat to bake the controller URL
# into a node's nvmon.toml without requiring the operator to hand-edit.
# ---------------------------------------------------------------------------

def set_agent_setting(key: str, value: str) -> Path:
    """Persist a single `[agent].<key> = "<value>"` setting to nvmon.toml.

    Reads the existing file (if any), updates or appends the [agent]
    section's `key = "value"` line, and writes the file back atomically.
    Idempotent — re-running with the same value is a no-op (rewrite is
    fine, the file is small).

    The writer is hand-rolled (no `tomli-w` dep) because we keep the
    embedded Python lean per `NVVMON\\requirements.txt`. We preserve
    every other section verbatim by reading the source line-by-line
    and only rewriting the [agent] block. Lines outside [agent] are
    untouched.

    Args:
        key: Setting name within the [agent] section, e.g. "controller_url".
        value: String value. Always emitted as a quoted TOML string.
               Backslashes and double quotes are escaped.

    Returns:
        Path to the written nvmon.toml.
    """
    if not key or not key.replace("_", "").isalnum():
        raise ValueError(f"refusing to write key with non-identifier chars: {key!r}")

    target = CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)

    # TOML string escape: backslash and double quote.
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'{key} = "{escaped}"'

    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8").splitlines(keepends=False)
        except Exception:
            existing = []
    else:
        existing = []

    out: list[str] = []
    in_agent_section = False
    key_replaced = False
    agent_section_seen = False
    # Pattern matching: a section header is a line whose stripped form
    # is `[name]` or `[name.subname]`. We only care about the bare `[agent]`
    # header here — `[agent.gpu]` etc. shouldn't trigger replacement.
    for line in existing:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            header_name = stripped[1:-1].strip()
            if header_name == "agent":
                in_agent_section = True
                agent_section_seen = True
                out.append(line)
                continue
            else:
                # Leaving [agent]; if we never replaced the key, append it
                # right before the next section starts.
                if in_agent_section and not key_replaced:
                    out.append(new_line)
                    key_replaced = True
                in_agent_section = False
                out.append(line)
                continue

        if in_agent_section and "=" in stripped and not stripped.startswith("#"):
            # Existing key/value inside [agent]. Replace if it's our key.
            current_key = stripped.split("=", 1)[0].strip()
            if current_key == key:
                out.append(new_line)
                key_replaced = True
                continue

        out.append(line)

    # Tail handling: still inside [agent] at EOF, OR no [agent] section
    # seen at all.
    if in_agent_section and not key_replaced:
        out.append(new_line)
        key_replaced = True
    elif not agent_section_seen:
        if out and out[-1].strip():
            out.append("")
        out.append("[agent]")
        out.append(new_line)
        key_replaced = True

    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target
