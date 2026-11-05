"""EDID file listing, Windows registry IO, and apply pipeline support.

Capture and apply EDID overrides as a first-class subsystem alongside Mosaic /
DD / QSync. Reads live `EDID_OVERRIDE` blobs from the registry on capture,
writes them back on apply, and forces a desktop redetect so the driver
re-reads the override.

Registry path used by NVIDIA custom resolutions:
    HKLM\\SYSTEM\\CurrentControlSet\\Enum\\DISPLAY\\<monitor>\\<instance>
        \\Device Parameters\\EDID_OVERRIDE
"""

from __future__ import annotations

import binascii
from pathlib import Path

from .. import config, log

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:
    winreg = None  # Not on Windows — registry ops become no-ops.


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------

def capability() -> dict:
    """Report whether the EDID subsystem is usable.

    EDID queries only require the Windows registry. On non-Windows systems
    we report unavailable.
    """
    if winreg is None:
        return {
            "available": False,
            "reason": "winreg not available (non-Windows system)",
        }
    return {"available": True}


def is_available() -> bool:
    return winreg is not None


# ---------------------------------------------------------------------------
# Status (for /health + /status use)
# ---------------------------------------------------------------------------

def status() -> dict:
    """Return a read-only snapshot of EDID overrides discoverable in the
    registry plus EDID files present in BASE_DIR.

    This is the `/edid` slice. Returns a dict (not a typed dataclass) because
    the shape is inherently heterogeneous. When at least one display has an
    active override, ``active_details`` carries a parsed view of the most
    common override blob (monitor name, manufacturer, preferred mode, year)
    plus ``source_file`` if the bytes match one of the on-disk Edids/* files.
    """
    if winreg is None:
        return {
            "available": False,
            "registry_overrides": {},
            "edid_files": [],
            "active_details": None,
        }

    overrides = check_registry_edid()
    files = list_edid_files()
    active_details: dict | None = None
    try:
        live = read_active_edid_overrides()
    except Exception:
        live = {}
    if live:
        # Most common blob wins — render rigs typically apply the same
        # override across every monitor; the rare per-display variant is
        # logged but the card just shows the dominant one.
        blobs: dict[bytes, list[str]] = {}
        for k, v in live.items():
            blobs.setdefault(v, []).append(k)
        winner_blob, winner_keys = max(
            blobs.items(), key=lambda kv: len(kv[1]),
        )
        details = parse_edid(winner_blob)
        details["applied_to"] = sorted(winner_keys)
        details["distinct_blobs"] = len(blobs)
        details["source_file"] = find_edid_file_matching(winner_blob)
        active_details = details

    return {
        "available": True,
        "registry_overrides": overrides,
        "edid_files": files,
        "active_details": active_details,
    }


# ---------------------------------------------------------------------------
# File listing
# ---------------------------------------------------------------------------

EDID_DIR_NAME = "Edids"


# ---------------------------------------------------------------------------
# EDID parser — extracts monitor name / manufacturer / preferred mode
# ---------------------------------------------------------------------------

def _decode_manufacturer_id(b0: int, b1: int) -> str:
    """Decode the 3-letter PNP manufacturer ID from EDID bytes 8-9.

    Big-endian 16-bit value: bit 14 is the reserved 0; the next three
    5-bit chunks are letter ordinals where A=1..Z=26.
    """
    word = (b0 << 8) | b1
    a = (word >> 10) & 0x1F
    b = (word >> 5) & 0x1F
    c = word & 0x1F
    out = []
    for n in (a, b, c):
        if 1 <= n <= 26:
            out.append(chr(ord("A") + n - 1))
        else:
            out.append("?")
    return "".join(out)


def _read_monitor_descriptor_text(block: bytes) -> str:
    """Strip an 18-byte monitor descriptor down to its ASCII payload.

    Descriptor layout: 00 00 00 <tag> 00 <13 bytes payload>. NVIDIA pads
    short names with 0x0a (LF) followed by spaces — strip both.
    """
    payload = block[5:18]
    text = payload.split(b"\x0a", 1)[0]
    return text.decode("ascii", errors="replace").rstrip(" \x00")


def _parse_detailed_timing(block: bytes) -> dict | None:
    """Parse an 18-byte detailed timing descriptor.

    Returns {"width", "height", "pixel_clock_khz", "refresh_hz"} or None if
    the block is actually a monitor descriptor (pixel clock == 0).
    """
    pixel_clock_10khz = block[0] | (block[1] << 8)
    if pixel_clock_10khz == 0:
        return None  # monitor descriptor, not a timing
    pixel_clock_khz = pixel_clock_10khz * 10

    h_active = block[2] | ((block[4] & 0xF0) << 4)
    h_blank = block[3] | ((block[4] & 0x0F) << 8)
    v_active = block[5] | ((block[7] & 0xF0) << 4)
    v_blank = block[6] | ((block[7] & 0x0F) << 8)

    h_total = h_active + h_blank
    v_total = v_active + v_blank
    refresh = 0.0
    if h_total and v_total:
        refresh = (pixel_clock_khz * 1000.0) / (h_total * v_total)
    return {
        "width": h_active,
        "height": v_active,
        "pixel_clock_khz": pixel_clock_khz,
        "refresh_hz": round(refresh, 3),
    }


def parse_edid(blob: bytes) -> dict:
    """Parse a binary EDID blob into a human-readable dict.

    Returns a dict with whatever fields could be decoded; never raises —
    truncated / non-EDID input yields ``{"valid": False, "size_bytes": ...}``.
    """
    out: dict = {
        "valid": False,
        "size_bytes": len(blob),
    }
    if len(blob) < 128 or blob[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return out

    out["valid"] = True
    out["manufacturer"] = _decode_manufacturer_id(blob[8], blob[9])
    out["product_code"] = blob[10] | (blob[11] << 8)
    out["serial_number"] = (
        blob[12] | (blob[13] << 8) | (blob[14] << 16) | (blob[15] << 24)
    )
    out["week_of_manufacture"] = blob[16]
    out["year_of_manufacture"] = 1990 + blob[17]
    out["edid_version"] = f"{blob[18]}.{blob[19]}"
    out["extension_count"] = blob[126]

    # Walk the four 18-byte descriptors at bytes 54..125. The first one is
    # conventionally the preferred timing; the rest may be monitor
    # descriptors carrying the friendly name (FC), serial (FF), or range
    # limits (FD).
    monitor_name = ""
    monitor_serial = ""
    preferred: dict | None = None
    for i in range(4):
        block = blob[54 + i * 18: 54 + (i + 1) * 18]
        if len(block) < 18:
            break
        if block[0] == 0 and block[1] == 0 and block[2] == 0:
            tag = block[3]
            if tag == 0xFC and not monitor_name:
                monitor_name = _read_monitor_descriptor_text(block)
            elif tag == 0xFF and not monitor_serial:
                monitor_serial = _read_monitor_descriptor_text(block)
        else:
            timing = _parse_detailed_timing(block)
            if timing and preferred is None:
                preferred = timing

    out["monitor_name"] = monitor_name
    out["monitor_serial"] = monitor_serial
    if preferred:
        out["preferred_mode"] = (
            f"{preferred['width']}x{preferred['height']} @ "
            f"{preferred['refresh_hz']:.2f} Hz"
        )
        out["preferred_width"] = preferred["width"]
        out["preferred_height"] = preferred["height"]
        out["preferred_refresh_hz"] = preferred["refresh_hz"]
    out["capabilities"] = parse_edid_capabilities(blob)
    return out


_EDID_BIT_DEPTH = {1: 6, 2: 8, 3: 10, 4: 12, 5: 14, 6: 16}
_EDID_INTERFACE = {1: "DVI", 2: "HDMI-a", 3: "HDMI-b", 4: "MDDI", 5: "DisplayPort"}


def parse_edid_capabilities(blob: bytes) -> dict:
    """Colour capabilities the panel advertises (v0.9.3) — the answer to
    "why does the driver refuse 12-bit?".

    Base block: byte 0x14 (video input) bit 7 digital, bits 6-4 colour bit
    depth, bits 3-0 interface; byte 0x18 (feature support) bits 4-3 the
    supported encodings. CTA-861 extension (tag 0x02): byte 3 bits 5/4
    YCbCr 4:4:4 / 4:2:2; HDMI vendor block byte 6 deep-colour flags
    (DC_30/36/48, DC_Y444); extended tags 14/15 announce YCbCr 4:2:0.
    """
    caps: dict = {
        "digital": False,
        "interface": "",
        "bit_depth": 0,
        "encodings": [],
        "cta": {"present": False, "ycbcr444": False, "ycbcr422": False, "ycbcr420": False,
                "hdmi_deep_color_bits": []},
    }
    if len(blob) < 128:
        return caps
    vid = blob[0x14]
    caps["digital"] = bool(vid & 0x80)
    if caps["digital"]:
        caps["bit_depth"] = _EDID_BIT_DEPTH.get((vid >> 4) & 0x07, 0)
        caps["interface"] = _EDID_INTERFACE.get(vid & 0x0F, "undefined")
        enc_bits = (blob[0x18] >> 3) & 0x03
        encodings = ["RGB444"]
        if enc_bits in (1, 3):
            encodings.append("YCbCr444")
        if enc_bits in (2, 3):
            encodings.append("YCbCr422")
        caps["encodings"] = encodings
    ext_count = blob[126]
    for k in range(1, ext_count + 1):
        ext = blob[128 * k: 128 * (k + 1)]
        if len(ext) < 128 or ext[0] != 0x02:
            continue
        cta = caps["cta"]
        cta["present"] = True
        if ext[1] >= 3:
            cta["ycbcr444"] = cta["ycbcr444"] or bool(ext[3] & 0x20)
            cta["ycbcr422"] = cta["ycbcr422"] or bool(ext[3] & 0x10)
        dtd_start = ext[2]
        pos = 4
        end = dtd_start if 4 <= dtd_start <= 127 else 127
        while pos < end:
            tag = (ext[pos] >> 5) & 0x07
            length = ext[pos] & 0x1F
            payload = ext[pos + 1: pos + 1 + length]
            if tag == 3 and len(payload) >= 6 and payload[:3] == b"\x03\x0c\x00":   # HDMI VSDB
                flags = payload[5]
                bits = []
                if flags & 0x10:
                    bits.append(30)
                if flags & 0x20:
                    bits.append(36)
                if flags & 0x40:
                    bits.append(48)
                if flags & 0x08:
                    bits.append("Y444")
                cta["hdmi_deep_color_bits"] = bits
            elif tag == 7 and payload and payload[0] in (14, 15):                     # YCbCr 4:2:0 blocks
                cta["ycbcr420"] = True
            pos += 1 + length
    return caps


def read_active_edids() -> dict[str, bytes]:
    """The driver-read ``EDID`` value for every registry display instance
    (``<monitor>\\<instance>`` -> bytes). Unlike ``EDID_OVERRIDE`` this is
    what the panel actually sent; Windows keeps entries for every panel ever
    plugged in, so callers filter by manufacturer / name."""
    result: dict[str, bytes] = {}
    if winreg is None:
        return result
    for key, params_path in _iter_display_params_paths():
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, params_path) as params_key:
                try:
                    value, _kind = winreg.QueryValueEx(params_key, "EDID")
                except FileNotFoundError:
                    continue
                if isinstance(value, (bytes, bytearray)) and value:
                    result[key] = bytes(value)
        except OSError:
            continue
    return result


def describe_active_edids(*, include_blob: bool = False) -> list[dict]:
    """``GET /edid/raw`` body: parsed EDID + colour capabilities per
    registry instance, overrides marked. Sorted so panels seen most often
    (same monitor id) group together."""
    import base64
    overrides = check_registry_edid()
    out: list[dict] = []
    for key, blob in sorted(read_active_edids().items()):
        parsed = parse_edid(blob)
        entry = {
            "instance": key,
            "size_bytes": len(blob),
            "has_override": bool(overrides.get(key)),
            "manufacturer": parsed.get("manufacturer", ""),
            "monitor_name": parsed.get("monitor_name", ""),
            "preferred_mode": parsed.get("preferred_mode", ""),
            "capabilities": parsed.get("capabilities", {}),
        }
        if include_blob:
            entry["edid_base64"] = base64.b64encode(blob).decode("ascii")
        out.append(entry)
    return out


def edid_dir() -> Path:
    """Canonical location for EDID files: ``BASE_DIR/Edids/``.

    Operators drop hex-text or binary EDID dumps here; capture writes
    captured overrides here too (named after the profile).
    """
    return config.BASE_DIR / EDID_DIR_NAME


def list_edid_files(search_dir: str | Path | None = None) -> list[str]:
    """List EDID files in ``BASE_DIR/Edids/`` (and BASE_DIR for legacy).

    Returns paths relative to BASE_DIR (e.g. ``Edids/Node1-24-Server.txt``)
    so the result can be stored verbatim in ``profile.edid.file``.

    Accepts ``.txt`` / ``.bin`` / ``.dat`` and falls back to a loose name
    heuristic ("edid", "proper", "custom", "2397", …) for files in BASE_DIR.
    Files inside ``Edids/`` are listed unconditionally — that folder is
    purpose-built for EDIDs, no name filter needed.
    """
    found: list[str] = []
    base = config.BASE_DIR

    if search_dir is None:
        ed_dir = edid_dir()
        if ed_dir.is_dir():
            for ext in ("*.txt", "*.bin", "*.dat"):
                for f in ed_dir.glob(ext):
                    try:
                        rel = f.relative_to(base).as_posix()
                    except ValueError:
                        rel = f.name
                    found.append(rel)
        # Legacy: hex dumps dropped at the BASE_DIR root, kept for
        # backwards-compat with operators who haven't moved files yet.
        for ext in ("*.txt", "*.bin", "*.dat"):
            for f in base.glob(ext):
                name_lower = f.name.lower()
                if any(
                    kw in name_lower
                    for kw in ("edid", "proper", "custom", "23.976", "2397")
                ):
                    found.append(f.name)
        return sorted(set(found))

    # Caller supplied an explicit dir — original heuristic behaviour for
    # backwards compatibility with any external caller.
    sd = Path(search_dir)
    if not sd.is_dir():
        return found
    for ext in ("*.txt", "*.bin", "*.dat"):
        for f in sd.glob(ext):
            name_lower = f.name.lower()
            if any(
                kw in name_lower
                for kw in ("edid", "proper", "custom", "23.976", "2397")
            ):
                found.append(f.name)
    return sorted(set(found))


def find_edid_file_matching(blob: bytes) -> str | None:
    """Return BASE_DIR-relative path of an EDID file whose bytes equal `blob`.

    Used at capture time so that if the operator already dropped the right
    EDID into ``Edids/`` (matching the live override), we point the profile
    at it rather than writing a duplicate file with a profile-derived name.
    Returns None when nothing matches.
    """
    for rel in list_edid_files():
        full = config.BASE_DIR / rel
        try:
            existing = read_edid_file(full)
        except (OSError, ValueError):
            continue
        if existing == blob:
            return rel
    return None


# ---------------------------------------------------------------------------
# Registry read
# ---------------------------------------------------------------------------

def _iter_display_params_paths():
    """Yield ("monitor\\instance", full_registry_path) for every display.

    Walks `HKLM\\SYSTEM\\CurrentControlSet\\Enum\\DISPLAY\\*\\*\\Device
    Parameters` once. Used by every read/write helper in this module so the
    enumeration logic lives in one place.
    """
    if winreg is None:
        return

    display_key_path = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, display_key_path,
        ) as display_key:
            i = 0
            while True:
                try:
                    monitor_name = winreg.EnumKey(display_key, i)
                except OSError:
                    break
                i += 1

                monitor_path = f"{display_key_path}\\{monitor_name}"
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, monitor_path,
                    ) as monitor_key:
                        j = 0
                        while True:
                            try:
                                instance = winreg.EnumKey(monitor_key, j)
                            except OSError:
                                break
                            j += 1
                            yield (
                                f"{monitor_name}\\{instance}",
                                f"{monitor_path}\\{instance}\\Device Parameters",
                            )
                except OSError:
                    continue
    except OSError:
        return


def check_registry_edid() -> dict[str, bool]:
    """Walk the Windows registry for EDID overrides.

    Returns a dict mapping "<monitor>\\<instance>" to True when an
    EDID_OVERRIDE value is present on that device, False when the registry
    key exists but the value does not.
    """
    result: dict[str, bool] = {}
    if winreg is None:
        return result

    for key, params_path in _iter_display_params_paths():
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, params_path,
            ) as params_key:
                try:
                    winreg.QueryValueEx(params_key, "EDID_OVERRIDE")
                    result[key] = True
                except FileNotFoundError:
                    result[key] = False
        except OSError:
            continue

    return result


def read_active_edid_overrides() -> dict[str, bytes]:
    """Return the raw EDID_OVERRIDE blob for every display that has one.

    Map "<monitor>\\<instance>" -> bytes. Skips displays without an override.
    """
    result: dict[str, bytes] = {}
    if winreg is None:
        return result

    for key, params_path in _iter_display_params_paths():
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, params_path,
            ) as params_key:
                try:
                    value, _kind = winreg.QueryValueEx(
                        params_key, "EDID_OVERRIDE",
                    )
                except FileNotFoundError:
                    continue
                if isinstance(value, (bytes, bytearray)) and value:
                    result[key] = bytes(value)
        except OSError:
            continue

    return result


# ---------------------------------------------------------------------------
# Registry write
# ---------------------------------------------------------------------------

def read_edid_file(path: str | Path) -> bytes:
    """Load an EDID blob from disk.

    Accepts:
      * Hex text: space- or newline-separated pairs (`.txt` files like
        `NoMosaic_2397.txt`). Leading/trailing whitespace ignored.
      * Raw binary: any non-hex content (`.bin`, `.dat`, NCP exports).

    Detection is content-based, not extension-based — operators sometimes
    rename hex dumps to `.bin` and vice versa.
    """
    p = Path(path)
    raw = p.read_bytes()
    if not raw:
        raise ValueError(f"EDID file is empty: {p}")

    # Heuristic: hex-text dumps contain only hex digits + whitespace.
    # A 128-byte EDID encoded as space-separated hex is ~384 chars; raw
    # binary will trip on the first non-printable byte.
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return raw

    cleaned = "".join(text.split())
    if cleaned and all(c in "0123456789abcdefABCDEF" for c in cleaned):
        try:
            return binascii.unhexlify(cleaned)
        except binascii.Error:
            pass

    return raw


def apply_edid_to_all_displays(
    edid_bytes: bytes, *, dry_run: bool = False,
) -> tuple[int, int, list[str]]:
    """Write `edid_bytes` as `EDID_OVERRIDE` to every DISPLAY\\*\\* entry.

    Skips displays whose existing override already matches `edid_bytes` —
    so re-applying the same profile is a no-op (no display blink).

    Returns (changed, total, changed_keys). `changed > 0` means the caller
    should force a desktop redetect.
    """
    if winreg is None:
        return (0, 0, [])

    if not edid_bytes:
        raise ValueError("edid_bytes must not be empty")

    logger = log.get()
    changed: list[str] = []
    total = 0

    for key, params_path in _iter_display_params_paths():
        total += 1
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, params_path,
            ) as params_key:
                try:
                    existing, _kind = winreg.QueryValueEx(
                        params_key, "EDID_OVERRIDE",
                    )
                except FileNotFoundError:
                    existing = None
        except OSError as exc:
            logger.debug(f"EDID: skipped {key} (open failed: {exc})")
            continue

        if isinstance(existing, (bytes, bytearray)) and bytes(existing) == edid_bytes:
            continue

        if dry_run:
            changed.append(key)
            continue

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, params_path, 0, winreg.KEY_SET_VALUE,
            ) as params_key:
                winreg.SetValueEx(
                    params_key, "EDID_OVERRIDE", 0,
                    winreg.REG_BINARY, edid_bytes,
                )
            changed.append(key)
        except PermissionError:
            logger.error(
                f"EDID write denied for {key} — administrator required"
            )
        except OSError as exc:
            logger.error(f"EDID write failed for {key}: {exc}")

    return (len(changed), total, changed)


def write_registry_edid(
    monitor: str, instance: str, edid_bytes: bytes,
) -> bool:
    """Write an EDID_OVERRIDE value under a specific display instance.

    Requires Administrator. Returns True on success.

    Note: callers (e.g. the HTTP server) should gate this behind an admin
    check before invoking.
    """
    if winreg is None:
        log.get().warning("write_registry_edid: winreg unavailable")
        return False

    logger = log.get()
    params_path = (
        rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{monitor}\{instance}"
        r"\Device Parameters"
    )

    try:
        # KEY_WRITE requires admin for HKLM\SYSTEM\CurrentControlSet
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, params_path, 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key, "EDID_OVERRIDE", 0, winreg.REG_BINARY, edid_bytes,
            )
        logger.info(f"EDID: wrote override for {monitor}\\{instance}")
        return True
    except PermissionError:
        logger.error(
            f"EDID write denied for {monitor}\\{instance} — "
            "administrator privileges required"
        )
        return False
    except Exception as exc:
        logger.error(f"EDID write failed for {monitor}\\{instance}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Profile validation helper
# ---------------------------------------------------------------------------

def validate_edid_for_profile(profile) -> dict:
    """Validate that EDID overrides exist for a profile's displays.

    Args:
        profile: Profile dataclass.

    Returns:
        {"found": bool, "detail": str, "displays": list[str] | absent}
    """
    logger = log.get()

    edid_file = profile.edid.file if profile.edid else ""
    if not edid_file:
        return {"found": True, "detail": "No EDID file specified in profile"}

    edid_path = config.BASE_DIR / edid_file
    if not edid_path.is_file():
        logger.warning(f"EDID file not found: {edid_path}")
        return {"found": False, "detail": f"EDID file not found: {edid_file}"}

    reg = check_registry_edid()
    overrides = [k for k, v in reg.items() if v]

    if overrides:
        return {
            "found": True,
            "detail": f"EDID overrides found for {len(overrides)} display(s)",
            "displays": overrides,
        }
    return {
        "found": False,
        "detail": (
            f"EDID file exists ({edid_file}) but no registry overrides found. "
            "Custom resolutions may not be active."
        ),
    }
