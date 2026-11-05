# nvmon HTTP API Reference

Three services ship as part of nvmon, each with its own HTTP surface:

| Service    | Port | Auth                          | Purpose                                     |
|------------|------|-------------------------------|---------------------------------------------|
| agent      | 9998 | none (trusted LAN)            | Per-machine NVIDIA control surface          |
| controller | 9997 | one shared password (optional)| Aggregates agents, fans out commands        |
| launcher   | 9996 | none                          | Generic app launcher (future Wave 2)        |

All three speak JSON over HTTP. Hostnames below are placeholders - replace with the target machine.

---

## Quick Start

```bash
# Install once, per machine (as Administrator)
install.bat --role node    # render nodes: agent + launcher (Startup shortcuts)
install.bat --role host    # workstation:  controller + Desktop dashboard shortcut

# Or start manually (any time, after install)
python\python.exe -m nvmon agent
python\python.exe -m nvmon controller --open-browser
python\python.exe -m nvmon launcher

# Probe liveness
curl http://localhost:9998/health          # agent
curl http://localhost:9997/health          # controller
curl http://localhost:9996/health          # launcher

# Read full node state
curl http://localhost:9998/status

# Apply a profile to a single node
curl -X POST http://localhost:9998/apply -d "{\"profile\":\"mosaic-2397\"}"

# Apply the same profile to every node, via the controller
curl -X POST http://localhost:9997/all/apply -H "X-NVMON-Password: mypw" \
     -d "{\"profile\":\"mosaic-2397\"}"
```

---

# Agent API (port 9998)

Base URL: `http://<host>:9998`

The agent runs on every node that has an NVIDIA GPU. Read endpoints are
unauthenticated. POST endpoints require the process to be running as
Administrator - if not, the agent returns `403`.

Any subsystem whose underlying CLI tool is missing (e.g. `configureMosaic.exe`
not installed) reports `available: false` in `/health.capabilities`, and its
dedicated endpoint returns `503 Service Unavailable`.

---

## GET Endpoints

### `GET /health`

Identity and capability advertisement.

**Response `200 OK`**

```json
{
  "ok": true,
  "version": "0.1.0",
  "hostname": "RENDER-NODE-03",
  "ip": "192.0.2.17",
  "capabilities": {
    "gpu":      { "available": true,  "exe": "nvidia-smi" },
    "displays": { "available": true,  "exe": "configureDdisplay.exe" },
    "mosaic":   { "available": true,  "exe": "configureMosaic.exe" },
    "qsync":    { "available": false, "exe": null,
                  "reason": "configureQsync2.exe not found" },
    "edid":     { "available": true },
    "color":    { "available": true, "exe": "nvapi64.dll",
                  "api": "NvAPI_Disp_ColorControl" }
  }
}
```

`color` is the NVAPI colour-depth / pixel-encoding control the apply uses
for `display_ids[<id>].bits_per_channel` / `pixel_encoding` (v0.9.1). When
it is unavailable the apply still runs, logs a warning, and the panels keep
whatever colour format the driver picked for the mode.

| Field                       | Type    | Meaning                                          |
|-----------------------------|---------|--------------------------------------------------|
| `ok`                        | bool    | Daemon is healthy                                |
| `version`                   | string  | nvmon package version                            |
| `hostname`                  | string  | Machine hostname                                 |
| `ip`                        | string  | Primary outbound IPv4                            |
| `capabilities.<name>`       | object  | One entry per subsystem                          |
| `capabilities.<name>.available` | bool | Whether that subsystem is usable                |
| `capabilities.<name>.exe`   | string  | Path (or name) of the backing CLI tool           |
| `capabilities.<name>.reason`| string  | Only present when `available` is `false`         |

---

### `GET /status`

Full subsystem state. Mirrors `status.json` on disk (written by the monitor
loop every `poll_interval_seconds`).

**Response `200 OK`**

```json
{
  "timestamp": "2026-04-17T14:55:02.123456+00:00",
  "hostname": "RENDER-NODE-03",
  "active_profile": "mosaic-2397",
  "uptime_seconds": 3600,
  "poll_count": 720,
  "direct_display": { ... },
  "mosaic": { ... },
  "qsync": { ... },
  "gpu": [ ... ],
  "output_health": { ... },
  "validation": { ... },
  "alerts": [ ... ],
  "errors": [ ... ]
}
```

See [status.json schema](#statusjson-schema) below for every sub-field.

Two HTTP-only fields are added on top of the on-disk snapshot (v0.8.0):

| Field | Meaning |
|---|---|
| `status_age_s` | Seconds since the snapshot was taken. While an apply / restore / boot-apply runs, the monitor keeps its last snapshot (it must not overlap the job's NVIDIA calls), so this grows — show "stale Ns" rather than a frozen reading. |
| `job_active` | `true` while an apply / restore / boot-apply owns the NVIDIA tools. |

`direct_display.signals[]` (v0.8.0) is Windows' CCD view of every active
path — `{name, source: [w,h], position: [x,y], target: [w,h], hz}`.
`source` is the desktop size, `target` the signal on the wire. They differ
when Windows is scaling the desktop (the "Mosaic says 3840x2160@23.976
but the wall receives 10240x5760@60" failure); `validation.checks.output_signal`
fails in that case.

---

### `GET /mosaic/timings`

Dry-run a Mosaic grid with `configureMosaic test` — **nothing is applied**.
Returns the refresh rate the driver would actually pick for the requested
mode (asked `30` → `29.97` on panels without a true-30 timing) and whether
the exact mode is accepted. Use it as a preflight before blanking a wall.

```
GET /mosaic/timings                       # the active profile's grid + mode
GET /mosaic/timings?profile=24_Server     # that profile's grid + mode
GET /mosaic/timings?rows=2&cols=2&res=3840,2160,30&out=out=1,0;out=1,1;out=1,2;out=1,3
```

**Response `200 OK`**

```json
{"command": "test rows=2 cols=2 out=1,0 out=1,1 out=1,2 out=1,3 res=3840,2160,30",
 "success": true, "valid": true, "rate_hz": 29.97, "output": "<...configureMosaic xml...>",
 "profile": "24_Server"}
```

`404` when the named profile doesn't exist. The call can take up to a
minute on a busy node (it is serialized with every other NVIDIA call).

---

### `GET /displays/signals`

`{"signals": [...]}` — the same CCD list as `status.direct_display.signals`,
read live. First stop when a wall looks "wrong but Mosaic says fine".

---

### `GET /displays/color`

NVAPI colour settings per display (v0.9.1) — what NVIDIA Control Panel's
"Use NVIDIA color settings" shows — plus the driver's own default for the
display. Read-only; this is what the apply's colour step compares against.

```json
{"available": true,
 "displays": [
   {"display": "80061085",
    "current":        {"color_format": 1, "colorimetry": 2, "dynamic_range": 1, "bpc": 3,
                       "color_selection_policy": 0, "desktop_color_depth": 1,
                       "bits_per_channel": 10, "pixel_encoding": "YCbCr422",
                       "colorimetry_name": "BT709", "range": "limited", "policy": "user",
                       "summary": "10-bit YCbCr422 BT709 limited (user)"},
    "driver_default": {"...": "same shape"}}]}
```

`available: false` (with `error`) when NVAPI or `NvAPI_Disp_ColorControl`
is missing on the host. Compare `current.bits_per_channel` /
`pixel_encoding` with `GET /displays` (the Windows wire format) — they
should agree.

**`link` (v0.9.3, DisplayPort sinks):** from `NvAPI_GetDisplayPortInfo` —
the trained link and what the sink allows. This is the authoritative answer
to "can this panel take 12-bit?":

```json
"link": {"dpcd_version": "1.2", "max_link": "HBR2 5.40 x4", "current_link": "HBR2 5.40 x4",
         "current_payload_gbps": 17.28, "current": "10-bit YCbCr422 BT709 limited",
         "sink_supports_bpc": [8, 10], "sink_supports_encodings": ["RGB444", "YCbCr444", "YCbCr422"],
         "current_mode_bpc": [8, 10], "current_mode_encodings": ["RGB444", "YCbCr444", "YCbCr422"],
         "flags": {"...": "all decoded bits"}, "raw": {"flags_hex": "0x..."}}
```

`sink_supports_bpc` without 12 → the panel never offered it (fix on the
processor side, not the GPU). 12 present in `sink_supports_bpc` but not in
`current_mode_bpc` → the link cannot carry it at this mode (lower the
rate, or fewer bits per pixel: 4:2:2 instead of 4:4:4).

`link.bandwidth` (v0.9.5) spells the arithmetic out for the live pixel
clock: `needed Gbit/s = pixel clock × bits per pixel` (RGB/4:4:4 = 3 × bpc,
4:2:2 = 2 × bpc, 4:2:0 = 1.5 × bpc) against `link_payload_gbps` = lanes ×
Gbit/s per lane × 0.8. Each of 8/10/12-bit × RGB444/YCbCr444/422/420 gets
`needed_gbps` and `fits`. DSC is not modelled.

The validator's `dp_link` row (v0.9.5) is red whenever a panel's trained
lanes or rate are below what its sink offers.

**Probe a depth (v0.9.2):** `GET /displays/color?probe=12` (or
`probe=12,Ycc422,BT709`) adds, per display,
`"probe": {"request": "12-bit YCbCr422 BT709 limited (user)", "supported": true, "already": false}`
— the driver's IS_SUPPORTED_COLOR answer. Nothing is changed. **Advisory
only:** on 596.86 / 597.30 the driver answers `true` for every request,
including 12-bit RGB at 4K60 which the DisplayPort link cannot carry, while
the SET of 12-bit YCbCr422 is refused with NVAPI_NOT_SUPPORTED (site
2026-11-04). The apply's read-back after SET is the truth; the panel's own
limits are in `GET /edid/raw`.

---

### `GET /edid/raw`

Every panel EDID Windows has on file (registry `EDID` value per display
instance), decoded (v0.9.3). Windows keeps entries for every panel ever
plugged in — filter with `?name=BRT` (matches the registry id or the
monitor name). `?blob=1` adds the bytes as base64.

```json
{"count": 2, "edids": [
  {"instance": "BRT0040\\5&14374ae&0&UID69894", "size_bytes": 256, "has_override": false,
   "manufacturer": "BRT", "monitor_name": "Tessera SX40", "preferred_mode": "3840x2160 @ 60.00 Hz",
   "capabilities": {"digital": true, "interface": "DisplayPort", "bit_depth": 10,
                    "encodings": ["RGB444", "YCbCr444", "YCbCr422"],
                    "cta": {"present": true, "ycbcr444": true, "ycbcr422": true, "ycbcr420": false,
                            "hdmi_deep_color_bits": []}}}]}
```

`capabilities.bit_depth` is the EDID base-block colour depth (byte 0x14);
it is what the driver checks before accepting a bits-per-channel request.

---

### `GET /gpu`

GPU-only slice. Equivalent to `status.gpu` without the rest.

**Response `200 OK`**

```json
[
  {
    "index": 0,
    "name": "NVIDIA RTX A6000",
    "pci_bus_id": "00000000:41:00.0",
    "driver_version": "573.48",
    "temperature_c": 69,
    "power_draw_w": 92.0,
    "power_limit_w": 300.0,
    "gpu_utilization_pct": 1,
    "memory_used_mb": 760,
    "memory_total_mb": 49140,
    "gpu_clock_mhz": 1245,
    "mem_clock_mhz": 6800,
    "video_clock_mhz": 1320,
    "pstate": "P0",
    "display_active": true,
    "fan_speed_pct": 42,
    "pcie_gen": 4,
    "pcie_width": 16
  }
]
```

`503` if `nvidia-smi` is not found.

---

### `GET /displays`

DirectDisplay (DD) subsystem state.

**Response `200 OK`**

```json
{
  "active": true,
  "display_count": 5,
  "displays": [
    {
      "id": "82061085",
      "name": "HELIOS-DP",
      "status": "Removed, Managed, Not Acquired",
      "refresh_rate": 23.976,
      "refresh_fraction": "23976/1000",
      "resolution": [3840, 2160],
      "connector": "DisplayPort",
      "bits_per_channel": 10,
      "color_space": "RGB",
      "pixel_encoding": "RGB444",
      "gpu_index": 1,
      "display_index": 0,
      "os_target_id": "61100"
    }
  ]
}
```

`503` if `configureDdisplay.exe` is not found.

---

### `GET /mosaic`

Mosaic subsystem state.

**Response `200 OK`**

```json
{
  "enabled": true,
  "rows": 2,
  "cols": 2,
  "per_display_resolution": [3840, 2160],
  "total_resolution": [7680, 4320],
  "refresh_rate": 23.976,
  "output_count": 4,
  "synchronized": true,
  "outputs": [
    { "gpu": 1, "display": 0, "display_id": "0x82061085" },
    { "gpu": 1, "display": 1, "display_id": "0x82061087" },
    { "gpu": 1, "display": 2, "display_id": "0x82061081" },
    { "gpu": 1, "display": 3, "display_id": "0x82061083" }
  ]
}
```

`503` if `configureMosaic.exe` is not found.

---

### `GET /qsync`

QSync (NVIDIA sync board) state.

**Response `200 OK`**

```json
{
  "available": true,
  "synced": true,
  "house_sync_detected": true,
  "house_sync_incoming_hz": 23.976,
  "source": "HOUSESYNC",
  "trigger": "FALLING",
  "per_gpu_sync": [
    {
      "gpu": 0,
      "synced": false,
      "signal_available": true,
      "effective_synced": true,
      "sync_note": "via HOUSESYNC"
    },
    {
      "gpu": 1,
      "synced": false,
      "signal_available": false,
      "effective_synced": true,
      "sync_note": "via GPU 0"
    }
  ]
}
```

> **Use `effective_synced`**, not `synced`. In HOUSESYNC server mode the
> driver reports raw `synced: false` for non-signal GPUs even when they're
> locked via the master. `effective_synced` resolves that correctly.

`503` if `configureQsync2.exe` is not found.

---

### `GET /profiles`

List available profile names (filename stems in `profile_dir`).

**Response `200 OK`**

```json
{
  "profiles": [
    "desktop",
    "mosaic-2397",
    "mosaic-24",
    "mosaic-60"
  ]
}
```

---

### `GET /validate`

Run all validation checks against the active profile (or defaults).

**Response `200 OK`**

```json
{
  "all_passed": true,
  "checks": {
    "dd_active":      { "passed": true,  "detail": "DD active with 4 displays" },
    "mosaic_enabled": { "passed": true,  "detail": "Mosaic 2x2 @ 23.976Hz" },
    "qsync_locked":   { "passed": true,  "detail": "Locked, house sync @ 23.977Hz" },
    "refresh_rate":   { "passed": true,  "detail": "23.976Hz (expected 23.976 +/- 0.002)" },
    "gpu_health":     { "passed": true,  "detail": "All GPUs healthy (42C, 45C)" }
  }
}
```

---

### `GET /`

Auto-refreshing HTML debug page for this node. Returns `text/html`, not JSON.

---

## POST Endpoints

All POST endpoints require Administrator privileges. Non-admin requests get:

```http
HTTP/1.1 403 Forbidden
Content-Type: application/json

{"error": "admin required", "message": "This endpoint requires Administrator privileges."}
```

### `POST /apply`

Apply a saved profile. **Asynchronous** - returns `202` immediately and runs
in the background. Poll `/apply/status` for progress (`step`, `in_progress`,
truthful `success`/`message` at the end).

**Latest request wins (v0.8.0).** If an apply / restore / boot-apply is
already running it is cancelled at its next NVIDIA command or settle wait
and the new request starts once it has unwound (up to 90 s). The 202 body
carries `"preempting": true` in that case; the cancelled job finishes with
`success: false, cancelled: true` in `/apply/status`. Before v0.8.0 the
second request was rejected — an Apply-All right after a fleet reboot was
silently dropped on every node still boot-applying.

**The pipeline now refuses to lie (v0.8.0).** `/apply/status.success` is
`false`, with the reason in `message`, when:

- Mosaic came up at a different rate than the profile asks for (the driver
  picked the nearest timing it has — check `/mosaic/timings`); the pipeline
  rolls back instead of waiting minutes for an impossible QSync lock.
- The incoming house-sync clock is at a different rate than
  `validation.expected_house_sync_freq` (QSync is not even attempted).
- QSync did not lock to the requested **source** within 20 s plus one clean
  disable + enable retry. A lock to the wrong source (VSYNC when HOUSESYNC
  was asked) does not count.
- The operator display named by `[agent.displays].excluded_name_prefixes`
  could not be made primary before DirectDisplay.

### `POST /apply/cancel`

Cancel the running apply / restore / boot-apply at its next command or wait.
`200 {"cancelling": true, "job": {...}}` when something was running,
`409 {"cancelling": false, ...}` otherwise. Admin required.

**Request body**

```json
{ "profile": "mosaic-2397" }
```

**Response `202 Accepted`**

```json
{ "status": "accepted", "profile": "mosaic-2397" }
```

**Errors**

- `400` - `profile` missing or unknown
- `403` - not running as Administrator

---

### `POST /restore`

Restore displays to the Windows desktop (disable Mosaic + DD + QSync).
**Asynchronous** - `202` immediately.

**Request body:** empty or `{}`

**Response `202 Accepted`**

```json
{ "status": "accepted", "action": "restore" }
```

---

### `POST /dd/enable` / `POST /dd/disable`

Enter / leave DirectDisplay mode. **Synchronous** - blocks until the underlying
CLI command returns.

**Request body:** empty or `{}`

**Response `200 OK`**

```json
{ "success": true, "stdout": "...", "stderr": "" }
```

**Errors**

- `500` - the CLI call failed (see `stderr`)
- `503` - `configureDdisplay.exe` not found

---

### `POST /qsync/enable`

Enable QSync with optional overrides.

**Request body** (all fields optional)

```json
{
  "source": "HOUSESYNC",
  "trigger": "FALLING",
  "grid": 1
}
```

**Response `200 OK`**

```json
{ "success": true, "stdout": "...", "stderr": "" }
```

---

### `POST /qsync/disable`

Disable QSync.

**Request body:** empty or `{}`

**Response `200 OK`**

```json
{ "success": true, "stdout": "...", "stderr": "" }
```

---

### `POST /mosaic/disable`

Tear down the current Mosaic grid.

**Request body:** empty or `{}`

**Response `200 OK`**

```json
{ "success": true, "stdout": "...", "stderr": "" }
```

> There is no `POST /mosaic/enable` - use `POST /apply` with a Mosaic profile.

---

# Controller API (port 9997)

Base URL: `http://<controller-host>:9997`

The controller polls every configured agent, aggregates their `/status` and
`/health`, and fans out commands.

## Authentication

The controller is the only authenticated surface. It reads a single shared
password from `controller.password_file`:

- **File missing or empty** -> **no-auth mode**. Controller prints one warning
  at startup and accepts any request. Intentional to make getting started painless.
- **File contains a password** -> every request must include
  `X-NVMON-Password: <password>` (API) or an auth cookie from the login form (browser).

Missing / wrong credential -> `401 Unauthorized`.

To change the password: edit the file, restart the controller.

---

## GET Endpoints

### `GET /health`

Controller identity and node count.

**Response `200 OK`**

```json
{
  "ok": true,
  "version": "0.1.0",
  "hostname": "WORKSTATION-02",
  "node_count": 4,
  "reachable_count": 3
}
```

---

### `GET /nodes`

Every node declared in config, with its latest `/health` snapshot.

**Response `200 OK`**

```json
{
  "nodes": [
    {
      "hostname": "RENDER-NODE-01",
      "url": "http://192.0.2.12:9998",
      "reachable": true,
      "last_seen": "2026-04-17T14:55:02+00:00",
      "capabilities": { "gpu": true, "displays": true, "mosaic": true,
                        "qsync": true, "edid": true }
    },
    {
      "hostname": "RENDER-NODE-02",
      "url": "http://192.0.2.14:9998",
      "reachable": false,
      "last_seen": "2026-04-17T14:50:00+00:00",
      "error": "connection refused"
    }
  ]
}
```

---

### `GET /status`

Aggregated status across every reachable node.

**Response `200 OK`**

```json
{
  "timestamp": "2026-04-17T14:55:02+00:00",
  "node_count": 4,
  "reachable_count": 3,
  "nodes": {
    "RENDER-NODE-01": { "timestamp": "...", "direct_display": {...}, ... },
    "RENDER-NODE-02": { "reachable": false, "error": "timeout" },
    "RENDER-NODE-03": { "timestamp": "...", "direct_display": {...}, ... },
    "RENDER-NODE-04": { "timestamp": "...", "direct_display": {...}, ... }
  }
}
```

---

### `GET /node/<hostname>/status`

Pass-through to one specific node's `/status`. Preserves that node's response
unchanged.

Other per-node passthroughs (v0.8.0): `GET /node/<hostname>/mosaic/timings[?profile=X]`
(Mosaic dry run, 70 s timeout), `GET /node/<hostname>/displays/signals`
(CCD view) and `POST /node/<hostname>/apply/cancel`.

**Re-apply All with per-node profiles (v0.8.1).** `POST /fanout/job`
with `action: "apply"` and **no** `profile` applies, per node,
`[controller.node_profiles]` (see CONFIG.md) when set, else the node's
currently active profile, else skips that node with a reason.

**Apply preflight (v0.8.0).** `POST /fanout/job` with `action: "apply"`
(the dashboard's Apply Profile / Re-apply All) first dry-runs every
target's Mosaic timing via its `/mosaic/timings` (nothing is applied).
A node whose wall would come up at a different rate than its profile asks
for (beyond the profile's `refresh_rate_tolerance`, floor 0.02 Hz) is
returned as `status: "skipped"` with a `preflight: ...` reason and is not
blanked. The response carries `preflight: [{hostname, profile, checked,
wanted_hz, rate_hz, valid, skip?}]`. Nodes whose profile has Mosaic off,
whose profile JSON isn't in the controller's `profiles/`, or whose agent
predates the endpoint are passed through. Send `"skip_preflight": true`
in the body to bypass.

**Errors**

- `404` - `hostname` not in config
- `502 Bad Gateway` - node responded with an error
- `504 Gateway Timeout` - node did not respond within `fanout_timeout_seconds`

---

### `GET /`

Unified HTML dashboard showing all nodes in one page. Returns `text/html`.

If auth is enabled and no cookie is set, a one-input login form is served.
Submitting the form sets a session cookie and redirects to `/`.

---

## POST Endpoints

Fan-out endpoints return a standard result shape:

```json
{
  "requested": 4,
  "succeeded": 3,
  "failed": 1,
  "timed_out": 0,
  "results": [
    { "hostname": "RN-01", "status": "accepted", "http_code": 202 },
    { "hostname": "RN-02", "status": "accepted", "http_code": 202 },
    { "hostname": "RN-03", "status": "accepted", "http_code": 202 },
    { "hostname": "RN-04", "status": "failed",   "http_code": 403,
      "error": "admin required on target" }
  ]
}
```

Per-result `status` values:

| Value         | Meaning                                                  |
|---------------|----------------------------------------------------------|
| `accepted`    | Target returned 202 (async work dispatched)              |
| `ok`          | Target returned 200 (sync op completed)                  |
| `failed`      | Target returned a non-2xx error                          |
| `timed_out`   | No response within `fanout_timeout_seconds`              |
| `unreachable` | Could not connect                                        |
| `skipped`     | Capability missing on that target                        |

---

### `POST /all/apply`

Fan-out `POST /apply` to every reachable node that has the needed capabilities.

**Request body**

```json
{ "profile": "mosaic-2397" }
```

**Response `200 OK`** (see fan-out shape above).

---

### `POST /all/restore`

Fan-out `POST /restore`.

**Request body:** empty or `{}`

---

### `POST /all/qsync/enable`

Fan-out `POST /qsync/enable` with optional overrides.

**Request body**

```json
{ "source": "HOUSESYNC", "trigger": "FALLING", "grid": 1 }
```

---

### `POST /all/qsync/disable`

Fan-out `POST /qsync/disable`.

---

### `POST /group/<name>/apply`

Same as `/all/apply` but restricted to the named group in config.

**Errors**

- `404` - no group with that name

---

### `POST /node/<hostname>/apply`

Pass-through to a single node's `POST /apply`.

**Errors**

- `404` - `hostname` not in config
- Whatever the target returns (status, body, headers) is proxied back

---

### Chain sync (v0.9.9)

The controller starts each frame-lock daisy chain in order after reboots:
server enable → wait for its lock → client enables → wait. A group is a
chain only when exactly one member's active profile has QSync enabled as
`server` and at least one as `client`; every other group (standalone
servers, sync off, two servers) is left to the nodes' own boot sync.

- `GET /sync_chain?host=<name>` — **open** (no password, read-only), asked
  by agents at boot: `{"in_chain": true, "group": "Ceiling", "role":
  "client", "server": "RENDER-05", "clients": [...]}` or
  `{"in_chain": false}`.
- `GET /chains` — chains, learned roles, run counters and the last result
  per chain (`ok`, `server_locked`, per-client lock, `detail`, `seconds`).
- `POST /chains/run {"group": "Ceiling"}` — sequence one chain now (omit
  `group` for all). 202.

Roles come from each node's active profile and persist in
`secrets/sync_chains.json`. An agent in a chain applies timing + colour at
boot, leaves QSync off and shows `chain_sync.pending` in `/status`; if no
chain sync locks it within 8 min it syncs itself (4 × 45 s retries).

---

### `POST /system/restart_monitor`

Bounce **this controller process** (v0.9.2; the REM-gated "Restart monitor"
item in the dashboard's power menu). No OS reboot, render nodes untouched.
Body (optional): `{"delay_seconds": 2, "open_browser": false}`.

Answers `202` immediately, then a detached helper on the monitor box waits
`delay_seconds`, runs `kill_monitor.bat` and `run_monitor.bat` (which comes
back minimized and re-checks its Startup shortcut). Trace:
`logs\restart_monitor-<stamp>.log`. `500` with `error` when
`bats\_restart_monitor_helper.bat`, `run_monitor.bat` or `kill_monitor.bat`
is missing (nothing is killed in that case).

```json
{"ok": true, "action": "restart_monitor", "delay_seconds": 2,
 "message": "controller restarting in 2s; run_monitor.bat relaunches it minimized (does not open a browser)"}
```

The dashboard tab polls `GET /health` and reloads itself once the controller
answers again.

---

### `POST /logout` (browser only)

Clears the auth cookie. No-op in no-auth mode.

---

# Launcher API (port 9996)

Base URL: `http://<host>:9996`

Generic launcher for configured applications. Wave 1 ships with an empty app
list - populate `[launcher.apps]` in `nvmon.toml` to enable.

No authentication. Intended for trusted LAN only.

### `GET /health`

**Response `200 OK`**

```json
{ "ok": true, "version": "0.1.0", "hostname": "RENDER-NODE-03", "app_count": 0 }
```

### `GET /apps`

List configured apps.

```json
{ "apps": [] }
```

### `POST /launch`

Launch a configured app by name.

**Request body**

```json
{ "name": "touchdesigner" }
```

**Response `200 OK`**

```json
{ "success": true, "pid": 12345, "name": "touchdesigner" }
```

### `POST /kill`

Kill a previously-launched app by name.

**Request body**

```json
{ "name": "touchdesigner" }
```

---

# status.json Schema

The agent writes this to disk atomically every poll cycle. `GET /status`
returns the same structure.

| Field             | Type    | Description                                    |
|-------------------|---------|------------------------------------------------|
| `timestamp`       | string  | ISO 8601 UTC of last poll                      |
| `hostname`        | string  | Machine hostname                               |
| `active_profile`  | string  | Currently applied profile, or `""`             |
| `uptime_seconds`  | int     | Seconds since monitor started                  |
| `poll_count`      | int     | Completed poll cycles                          |

## `direct_display`

| Field           | Type      | Description                                       |
|-----------------|-----------|---------------------------------------------------|
| `active`        | bool      | Any managed display in DD mode                    |
| `display_count` | int       | Total displays (including excluded)               |
| `displays[]`    | array     | Per-display detail (see below)                    |

### `displays[]`

| Field              | Type       | Description                                       |
|--------------------|------------|---------------------------------------------------|
| `id`               | string     | NVAPI display ID (e.g. `"82061085"`)              |
| `name`             | string     | Physical display name                             |
| `status`           | string     | `"Removed, Managed, ..."` or `"Desktop @(X,Y)"`   |
| `refresh_rate`     | float      | Hz (`0.0` when the display is "Removed")          |
| `refresh_fraction` | string     | Rational form (e.g. `"23976/1000"`)               |
| `resolution`       | [int, int] | Width x Height                                    |
| `connector`        | string     | `"DisplayPort"`, `"HDMI"`, etc.                   |
| `bits_per_channel` | int        | Color depth per channel                           |
| `color_space`      | string     | `"RGB"`, etc.                                     |
| `pixel_encoding`   | string     | `"RGB444"`, etc.                                  |
| `gpu_index`        | int        | Adapter index (0-based, matches `nvidia-smi`)     |
| `display_index`    | int        | NVIDIA display index within the adapter           |
| `os_target_id`     | string     | Windows OS target ID                              |

## `mosaic`

| Field                    | Type       | Description                      |
|--------------------------|------------|----------------------------------|
| `enabled`                | bool       | Mosaic is active                 |
| `rows`                   | int        | Grid rows                        |
| `cols`                   | int        | Grid columns                     |
| `per_display_resolution` | [int, int] | Per-panel resolution             |
| `total_resolution`       | [int, int] | Combined surface resolution      |
| `refresh_rate`           | float      | Operating Hz                     |
| `output_count`           | int        | Number of outputs                |
| `synchronized`           | bool       | Outputs are sync-locked          |
| `outputs[]`              | array      | Per-output GPU/display mapping   |

## `qsync`

| Field                    | Type    | Description                                    |
|--------------------------|---------|------------------------------------------------|
| `available`              | bool    | QSync hardware detected                        |
| `synced`                 | bool    | System as a whole is sync-locked               |
| `house_sync_detected`    | bool    | House sync signal present                      |
| `house_sync_incoming_hz` | float   | Incoming house sync rate                       |
| `source`                 | string  | `"HOUSESYNC"`, `"INTERNAL"`, `""`              |
| `trigger`                | string  | `"FALLING"` or `"RISING"`                      |
| `per_gpu_sync[]`         | array   | Per-GPU sync detail                            |

### `per_gpu_sync[]`

| Field              | Type    | Description                                          |
|--------------------|---------|------------------------------------------------------|
| `gpu`              | int     | QSync GPU index                                      |
| `synced`           | bool    | Raw driver "Is synced" field                         |
| `signal_available` | bool    | Sync signal available to this GPU                    |
| `effective_synced` | bool    | Resolved sync state (**use this**)                   |
| `sync_note`        | string  | e.g. `"via HOUSESYNC"`, `"via GPU 0"`                |

## `gpu[]`

See the `GET /gpu` example above - every field is listed there.

## `output_health`

| Field                  | Type    | Description                                      |
|------------------------|---------|--------------------------------------------------|
| `total_displays`       | int     | Managed displays counted                         |
| `dd_active_count`      | int     | How many are in DD mode                          |
| `dd_expected_count`    | int     | Expected count (default 4 for 2x2)               |
| `all_same_refresh`     | bool    | All managed displays at one refresh              |
| `all_same_resolution`  | bool    | All managed displays at one resolution           |
| `refresh_rate_hz`      | float   | Operating Hz (falls back to Mosaic/QSync)        |

## `validation` (optional)

Present when the monitor runs with a `--profile` argument.

| Field         | Type    | Description                         |
|---------------|---------|-------------------------------------|
| `all_passed`  | bool    | All checks pass                     |
| `checks`      | object  | Map `check_name -> {passed, detail}`|

## `alerts[]`

| Field       | Type    | Description                      |
|-------------|---------|----------------------------------|
| `severity`  | string  | `"CRITICAL"` / `"WARNING"` / `"INFO"` |
| `type`      | string  | `"DD Dropout"`, `"QSync Lost"`, ... |
| `message`   | string  | Human-readable description       |
| `timestamp` | string  | ISO 8601 UTC                     |

## `errors[]`

Array of strings describing errors encountered during the last poll cycle.

---

# Error Response Shape

All error responses follow the same JSON shape:

```json
{
  "error": "short-code",
  "message": "Human-readable explanation",
  "detail": "Optional extra context"
}
```

Common errors:

| HTTP | `error`               | When                                                     |
|------|-----------------------|----------------------------------------------------------|
| 400  | `bad_request`         | Missing / invalid body field                             |
| 401  | `auth_required`       | Controller, wrong or missing password                    |
| 403  | `admin_required`      | Agent POST without Administrator privileges              |
| 404  | `not_found`           | Unknown profile / group / hostname                       |
| 500  | `internal_error`      | Underlying CLI call failed unexpectedly                  |
| 502  | `upstream_error`      | Controller proxied a node that returned an error         |
| 503  | `capability_missing`  | Subsystem exe not installed on this node                 |
| 504  | `upstream_timeout`    | Controller fan-out timed out waiting for a node          |


## Profile templates (controller, v0.8.2)

A *template* is the shared prefix of a matched profile set (e.g. `59.94 - 10b`
for `59.94 - 10b - SyncHost` + `59.94 - 10b - SyncClient`). Which profile each
node gets for a template comes from the `{template}` patterns in
`[controller.node_profiles]` (see CONFIG.md).

### `GET /templates`

```json
{
  "templates": ["30", "59.94 - 10b"],
  "active": "59.94 - 10b",
  "default": "59.94 - 10b",
  "current": "59.94 - 10b",
  "patterns": ["{template} - SyncClient", "{template} - SyncHost"],
  "roles": {"{template} - SyncHost": ["RENDER-01", "RENDER-05"], "{template} - SyncClient": ["RENDER-02", "..."]},
  "missing": ["24"]
}
```

`templates` lists only COMPLETE sets (every pattern has a profile). `missing`
lists values seen for some pattern but not all. `current` is what Re-apply
All will resolve `{template}` to right now.

### `POST /all/template` and `POST /group/<name>/template`

Body: `{"template": "30"}` (optional `"skip_preflight": true`). Requires the
controller password like every other POST. Validates the template against
`GET /templates`, persists it as the active template, then fans out
`POST /apply` per node with that node's role profile. Response is the
`/fanout/job` shape: `{"summary": {...}, "progress": [...], "template": "30"}`.
`400` with the `/templates` payload embedded when the template is unknown or
incomplete.

Example from any machine on the stage net:

```powershell
Invoke-RestMethod -Uri http://<controller-host>:9997/all/template -Method Post `
  -Headers @{ 'X-NVMON-Password' = '<password>'; 'Content-Type' = 'application/json' } `
  -Body '{"template": "30"}'
```

`POST /fanout/job` also accepts `{"action": "template", "template": "30", "scope": [...]}`
(this is what the dashboard button sends).
