# nvmon Configuration Reference

nvmon reads `nvmon.toml` from its install root (default `F:\nvmon\`).

- If the file is missing, safe defaults are used and a warning is logged.
- If the file is malformed, it is ignored and defaults are used (no crash).
- Configuration is loaded once per process. To apply changes, restart the
  service whose config you changed.

A fully-commented template lives in `nvmon.toml.example`. Copy it to
`nvmon.toml` and edit.

```bash
copy nvmon.toml.example nvmon.toml
```

---

## Top-level structure

```toml
[agent]         # per-machine agent (port 9998)
[agent.gpu]
[agent.displays]
[agent.bin]

[controller]    # aggregator + dashboard (port 9997)
[[controller.nodes]]
[[controller.groups]]

[launcher]      # generic app launcher (port 9996)
[[launcher.apps]]
```

Any section is optional - missing sections fall back entirely to defaults.

---

# `[agent]`

Settings for the per-machine agent daemon.

| Key                     | Type   | Default            | Purpose                                                |
|-------------------------|--------|--------------------|--------------------------------------------------------|
| `port`                  | int    | `9998`             | HTTP listen port.                                      |
| `poll_interval_seconds` | int    | `5`                | Seconds between subsystem polls.                       |
| `profile_dir`           | string | `"./profiles"`     | Where profile JSON files are stored.                   |
| `status_file`           | string | `"./status.json"`  | Path to atomic status snapshot on disk.                |
| `log_dir`               | string | `"./logs"`         | Log file destination.                                  |
| `controller_url`        | string | `""`               | Controller to push-register with; blank = read `monitor.url`. |
| `register_interval_seconds` | int | `60`              | Heartbeat interval for push-registration.              |
| `sync_card_firmware`    | string | `""`               | RTX PRO Sync firmware (e.g. `"3.06"`) when the agent can't read it live; drives the Blackwell sync advisory (v0.8.2). |
| `driver_advisories`     | bool   | `true`             | Emit known driver/firmware advisories into `status.advisories` (never `errors`). `false` = silent (v0.8.2). |
| `boot_apply_max_uptime_minutes` | int | `15`          | Boot-apply only runs when Windows has been up less than this. A listener restart hours into the day is not a boot and leaves the wall alone. `0` = always (v0.8.2). |

Example:

```toml
[agent]
port = 9998
poll_interval_seconds = 5
profile_dir = "./profiles"
status_file = "./status.json"
log_dir = "./logs"
```

---

## `[agent.gpu]`

GPU alerting thresholds.

| Key                  | Type | Default | Purpose                                                     |
|----------------------|------|---------|-------------------------------------------------------------|
| `temp_warning_c`     | int  | `85`    | Emit `WARNING` alert when any GPU reaches this temperature. |
| `power_warning_pct`  | int  | `95`    | Emit `WARNING` alert when power draw reaches this % of cap. |

Example:

```toml
[agent.gpu]
temp_warning_c = 85
power_warning_pct = 95
```

---

## `[agent.displays]`

Rules for excluding displays from DD / Mosaic operations. Leave lists empty
(the default) to operate on every display.

| Key                      | Type         | Default | Purpose                                                                                 |
|--------------------------|--------------|---------|-----------------------------------------------------------------------------------------|
| `excluded_name_prefixes` | list[string] | `[]`    | Any display whose name starts with one of these prefixes is ignored.                    |
| `excluded_resolutions`   | list[[w, h]] | `[]`    | Displays at these resolutions are ignored **only when the display name is unknown**.    |

Example (exclude KVM-extender monitors named `KVM-*` and generic 1080p):

```toml
[agent.displays]
excluded_name_prefixes = ["KVM-"]
excluded_resolutions = [[1920, 1080]]
```

---

## `[agent.bin]`

Override NVIDIA CLI tool paths. Leave a value empty and nvmon searches:

1. `F:\nvmon\bin\<name>.exe` (the bundled copy)
2. `F:\nvmon\<name>.exe` (legacy layout)
3. `PATH`

| Key                    | Type   | Default | Purpose                                      |
|------------------------|--------|---------|----------------------------------------------|
| `nvidia_smi`           | string | `""`    | Full path to `nvidia-smi.exe` (if elsewhere) |
| `configure_mosaic`     | string | `""`    | Full path to `configureMosaic.exe`           |
| `configure_ddisplay`   | string | `""`    | Full path to `configureDdisplay.exe`         |
| `configure_qsync2`     | string | `""`    | Full path to `configureQsync2.exe`           |

Example:

```toml
[agent.bin]
nvidia_smi = "C:/Windows/System32/nvidia-smi.exe"
configure_mosaic = ""
configure_ddisplay = ""
configure_qsync2 = ""
```

---

# `[controller]`

Settings for the aggregator / dashboard.

| Key                      | Type   | Default                                      | Purpose                                                              |
|--------------------------|--------|----------------------------------------------|----------------------------------------------------------------------|
| `port`                   | int    | `9997`                                       | HTTP listen port.                                                    |
| `poll_interval_seconds`  | int    | `5`                                          | Seconds between agent polls.                                         |
| `fanout_timeout_seconds` | int    | `15`                                         | Per-node timeout for `/all/*` fan-out operations.                    |
| `poll_timeout_seconds`   | int    | `5`                                          | Per-node timeout for the background `/health` + `/status` poll (v0.8.0). |
| `poll_workers`           | int    | `0` (auto)                                   | Parallel poll workers; auto = one per node, capped at 64 (v0.8.0).   |
| `node_ips`               | list   | `[]`                                         | IP list of agents; hostnames learned from `/health` (v0.8.0, see below). |
| `password_file`          | string | `"F:/nvmon/secrets/controller.password"`     | Path to the shared-password file. Missing or empty -> no-auth mode.  |

Example:

```toml
[controller]
port = 9997
poll_interval_seconds = 5
fanout_timeout_seconds = 15
password_file = "F:/nvmon/secrets/controller.password"
```

---

## `[controller].node_ips` — the IP-list form (v0.8.0)

For a big fleet you usually have a list of IPs, not hostnames. Put them
here and skip the per-node blocks:

```toml
[controller]
node_ips = ["10.0.0.11", "10.0.0.12", "10.0.0.13:9998"]
```

Each entry is `ip` or `ip:port` (default agent port 9998). The controller
learns the node's real hostname from its `/health` on the first successful
poll and renames the entry in place — the dashboard card, groups,
`apply_excluded_hosts` and `/node/<hostname>/...` routes all use that name
from then on; until then the card is labelled with the IP. An IP that also
appears in a `[[controller.nodes]]` block is ignored (the explicit block
wins), and an mDNS / push-registered entry for the same host is replaced by
the IP-list entry (operator-declared beats discovered).

## `[controller.node_profiles]` — per-node profiles (v0.8.1)

A mixed fleet (QSync master + daisy-chained clients, or one odd-resolution
box) needs different profiles on different nodes. Declare them once:

```toml
[controller.node_profiles]
"RENDER-01" = "24_Server"
"RENDER-02" = "24_Client"
"RENDER-03" = "24_Client"
```

or inline as `profile = "24_Client"` inside a `[[controller.nodes]]` block.
Keys are hostnames, matched case-insensitively on the leading DNS label, so
they work for `node_ips` entries too once the hostname has been learned.

What it changes: **Re-apply All** (the dashboard button that sends no
profile name) applies each node's configured profile. Precedence per node:
this map, then the node's currently active profile, then skipped with a
reason. So the very first apply on a fresh mixed fleet is one click.
**Apply Profile X** (an explicit pick) is unchanged and pushes X to every
node in scope. The Apply-All Mosaic preflight runs per node against that
node's profile.

### Templates — switching the whole fleet to another profile set (v0.8.2)

Put the literal token `{template}` where the shared rate/variant prefix goes:

```toml
[controller]
template_default = "59.94 - 10b"
[controller.node_profiles]
"RENDER-01" = "{template} - SyncHost"
"RENDER-02" = "{template} - SyncClient"
```

With profiles `59.94 - 10b - SyncHost`, `59.94 - 10b - SyncClient`,
`30 - SyncHost`, `30 - SyncClient` on disk, the controller offers two
templates: `59.94 - 10b` and `30`. A template is offered only when **every**
pattern has a profile for it, so a half-captured set can never be applied.

- Dashboard **Template** button (shortcut `T`): pick a template, every node
  in scope gets its own role's profile for it in one fan-out.
- `GET /templates` lists them; `POST /all/template {"template": "30"}` and
  `POST /group/<name>/template` apply one (the external-system entry point).
- The pick is persisted in `secrets/active_template.json`. **Re-apply All**
  resolves `{template}` against that pick, else `template_default`. So after
  switching to `30`, a plain Re-apply All stays on `30`.

Adding a rate is capture-only: save `<X> - SyncHost` on a host node and
`<X> - SyncClient` on a client node. No TOML edit, no controller restart.

| Key                | Type   | Default | Purpose                                                                 |
|--------------------|--------|---------|-------------------------------------------------------------------------|
| `template_default` | string | `""`    | `{template}` value Re-apply All uses until a template has been picked. |

Two poll knobs matter at fleet scale (v0.8.0):

| Key | Default | Purpose |
|---|---|---|
| `poll_timeout_seconds` | `5` | Per-node `/health` + `/status` timeout for the background poll. Separate from `fanout_timeout_seconds` so a dead node cannot stall the 5 s poll cycle for 15 s. |
| `poll_workers` | `0` (auto) | Nodes polled in parallel. Auto = one worker per node, capped at 64. |

---

## `[[controller.nodes]]`

Repeat for every agent node the controller should poll (or use the
`node_ips` list above). Static entries always win over mDNS discovery.

| Key        | Type   | Purpose                                           |
|------------|--------|---------------------------------------------------|
| `hostname` | string | Display label (shown on dashboard, used in URLs). |
| `url`      | string | Base URL of the agent, e.g. `http://10.0.0.11:9998`. |

Example:

```toml
[[controller.nodes]]
hostname = "RENDER-NODE-01"
url = "http://10.0.0.11:9998"

[[controller.nodes]]
hostname = "RENDER-NODE-02"
url = "http://10.0.0.12:9998"
```

---

## `[[controller.groups]]`

Optional. Define named groups for targeted fan-out via
`POST /group/<name>/apply`.

| Key     | Type         | Purpose                                                           |
|---------|--------------|-------------------------------------------------------------------|
| `name`  | string       | Group identifier used in `/group/<name>/...` URLs.                |
| `nodes` | list[string] | Hostnames from `[[controller.nodes]]` that belong to this group.  |

Example:

```toml
[[controller.groups]]
name = "outputs"
nodes = ["RENDER-NODE-01", "RENDER-NODE-02"]

[[controller.groups]]
name = "backups"
nodes = ["RENDER-NODE-03"]
```

---

# `[launcher]`

Generic app-launcher surface. Empty by default in Wave 1.

| Key    | Type | Default | Purpose           |
|--------|------|---------|-------------------|
| `port` | int  | `9996`  | HTTP listen port. |

Example:

```toml
[launcher]
port = 9996
```

## `[[launcher.apps]]`

Repeat per launchable app. The exact supported keys depend on the launcher
implementation - at minimum:

| Key       | Type   | Purpose                                                 |
|-----------|--------|---------------------------------------------------------|
| `name`    | string | Identifier used in `/launch` / `/kill` requests.        |
| `command` | string | Full path to the executable.                            |
| `args`    | list[string] | Optional. Command-line arguments.                  |
| `cwd`     | string | Optional. Working directory.                            |

Example:

```toml
[[launcher.apps]]
name = "notepad"
command = "C:/Windows/System32/notepad.exe"
args = []
```

---

# `[network]`

Multi-NIC overrides. Both optional; blank = disabled.

| Key           | Type   | Default | Purpose                                                                                     |
|---------------|--------|---------|---------------------------------------------------------------------------------------------|
| `subnet`      | string | `""`    | CIDR of the nvmon LAN, e.g. `"10.20.30.0/24"`. Pins announce / registration to it (v0.8.2). |
| `announce_ip` | string | `""`    | Per-box verbatim IP override. Beats `subnet` and auto-detect.                               |

**`subnet`** is the one to use in a toml that is mirrored across the fleet.
With it set, every agent announces and push-registers the local address
inside that subnet instead of the default-route NIC, the controller answers
`403` to a `/register` whose agent URL or source address is outside it, and
`/health` carries `subnet_ok` (the dashboard shows an **OFF CONTROL-NET** banner
when it is `false`). A box with no address in the subnet logs one warning and
falls back to auto-detect so the listener still comes up. Static
`[[controller.nodes]]` entries are never filtered — the operator typed them.

```toml
[network]
subnet = "10.20.30.0/24"
announce_ip = ""
```

---

# Complete Example

```toml
[agent]
port = 9998
poll_interval_seconds = 5
profile_dir = "./profiles"
status_file = "./status.json"
log_dir = "./logs"

[agent.gpu]
temp_warning_c = 85
power_warning_pct = 95

[agent.displays]
excluded_name_prefixes = ["KVM-"]
excluded_resolutions = []

[agent.bin]
nvidia_smi = ""
configure_mosaic = ""
configure_ddisplay = ""
configure_qsync2 = ""

[controller]
port = 9997
poll_interval_seconds = 5
fanout_timeout_seconds = 15
password_file = "F:/nvmon/secrets/controller.password"

[[controller.nodes]]
hostname = "RENDER-NODE-01"
url = "http://10.0.0.11:9998"

[[controller.nodes]]
hostname = "RENDER-NODE-02"
url = "http://10.0.0.12:9998"

[[controller.nodes]]
hostname = "RENDER-NODE-03"
url = "http://10.0.0.13:9998"

[[controller.groups]]
name = "outputs"
nodes = ["RENDER-NODE-01", "RENDER-NODE-02"]

[[controller.groups]]
name = "backups"
nodes = ["RENDER-NODE-03"]

[launcher]
port = 9996

[[launcher.apps]]
name = "notepad"
command = "C:/Windows/System32/notepad.exe"
```

---

# Validation Behavior

- **Missing file** -> `{}` cached, every accessor returns its defaults.
- **Malformed TOML** -> `{}` cached, warning logged, every accessor returns its defaults.
- **Wrong type inside a section** (e.g. `port = "bad"`) -> nvmon tries to coerce;
  if it fails, falls back to the default for that single key.
- **Unknown keys** -> silently ignored. Safe to add future keys without breaking older builds.
