# nvmon Deployment Guide

Step-by-step for bringing up nvmon on a fresh Windows machine. Two roles exist:

- **node** — a render node. Installs agent + launcher, auto-starts at login.
- **host** — your workstation / viewer. Installs controller + Desktop dashboard shortcut.

Both install from the same source tree with a single `install.bat`. Everything is enclosed in `F:\nvmon\` — no system Python, no Program Files install, no PATH pollution.

Everything below assumes a Windows 10/11 machine with an Administrator account.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [Install on a render node](#install-on-a-render-node)
3. [Install on your viewer/host](#install-on-your-viewerhost)
4. [Password setup](#password-setup)
5. [Firewall rules](#firewall-rules)
6. [Verification checklist](#verification-checklist)
7. [Common tasks](#common-tasks)
8. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Windows 10 / 11** (or Windows Server 2019+).
- **Administrator** account for installation (firewall + shortcut creation).
- **Internet access during install** (one-time, to download embedded Python).

**No system Python required.** `install.bat` downloads an embedded Python 3.11 into `F:\nvmon\python\` if none is present. It does not modify your system.

Additional on render nodes only:
- **NVIDIA drivers** installed (`nvidia-smi` resolvable on `PATH`).
- NVIDIA CLI tools (`configureMosaic.exe`, `configureDdisplay.exe`, `configureQsync2.exe`). See [Required NVIDIA tools](#required-nvidia-tools) below.

### Required NVIDIA tools

These are **not in the repo**: they are NVIDIA software under NVIDIA's own licence terms, so nvmon does not redistribute them. Download the Mosaic Utility, the Direct Display configuration utility and the Quadro Sync configuration utility from NVIDIA's enterprise / RTX professional (Quadro) driver tools pages, and drop the exes in `F:\nvmon\bin\`. If a folder already holds them (e.g. an older tool's install), set `NVMON_NVIDIA_TOOLS=<folder>` before running `install.bat` and it copies them into `bin\`.

`install.bat` prints a warning before anything else runs if any of them is missing from `bin\`. A missing tool does not stop the agent. That subsystem reports `available: false` in `/health`, with a reason naming the file and the `bin\` path, and the rest keep working.

| File | NVIDIA utility | Tested version | SHA-256 of the copy we run |
|---|---|---|---|
| `configureMosaic.exe` | Mosaic Utility | 2.4.9 | `9701e401f5a6535b599ae7538e3754394930661416dde844cf22737ed073220c` |
| `configureMosaic-2.5.3.exe` | Mosaic Utility (optional, staged upgrade) | **2.5.3** | `ec2609399c9aa68c4ee14017a94552b8f8fa40e8a3f20240d535c4d1a2d93f68` |
| `configureDdisplay.exe` | Direct Display configuration | 1.8.3405.878 | `b73920f75509fd7acbeb7367b5021e128033ef3fafbd6c69c41a5d88cf91805c` |
| `configureQsync2.exe` | Quadro Sync configuration | 2.1.3339.3914 | `b220ec180d30ca9a238509e95965f7c60a2dee45c2349c474000d703d3168ce4` |

nvmon only looks up `configureMosaic.exe` (see the Mosaic Utility note under the Blackwell notes for switching to 2.5.3). Verify a download with `certutil -hashfile <file> SHA256`.

### Download nvmon

Copy the source tree to `F:\nvmon\` (git clone, USB stick, SMB share — any means).

---

## Install on a render node

Repeat on every render node.

### 1. Copy the source tree

```cmd
xcopy /E /I \\fileserver\share\nvmon F:\nvmon
```

### 2. Run the installer as Administrator

```cmd
cd F:\nvmon
install.bat --role node
```

This:

- Downloads embedded Python 3.11 to `python\` if missing (~10 MB).
- `pip install -r requirements.txt` into the enclosed Python.
- Auto-copies NVIDIA CLI tools from the legacy tool install if found; otherwise warns and continues.
- Creates firewall rules for TCP 9998 (agent), 9996 (launcher), UDP 5353 (mDNS).
- Installs two shortcuts into the Startup folder:
  - **nvmon Agent** → `pythonw -m nvmon agent` (hidden background window)
  - **nvmon Launcher** → `pythonw -m nvmon launcher` (hidden background window)

### 4. Create `nvmon.toml`

```cmd
copy F:\nvmon\nvmon.toml.example F:\nvmon\nvmon.toml
notepad F:\nvmon\nvmon.toml
```

For an agent-only install you only need to worry about the `[agent]`
sections - leave `[controller]` / `[launcher]` as-is.

### 4. Start now (optional)

The shortcuts will auto-start on next login. To start immediately without waiting:

```cmd
F:\nvmon\python\pythonw.exe -m nvmon agent
F:\nvmon\python\pythonw.exe -m nvmon launcher
```

Both run hidden (no console window). Stop them with `F:\nvmon\kill.bat all`.

Or just double-click the "nvmon Agent" and "nvmon Launcher" shortcuts in your Startup folder.

### 5. Verify

```cmd
curl http://localhost:9998/health
```

You should see JSON with `"ok": true`, the hostname, and a `capabilities` object describing what this node can do.

---

## Install on your viewer/host

Pick one machine with network reach to all your render nodes. This doesn't have to be a render node itself — any workstation works.

### 1. Copy the source tree

Same as step 1 of the node install.

### 2. Run the installer

```cmd
cd F:\nvmon
install.bat --role host
```

- Prompts for a controller password (leave blank for no-auth mode).
- Firewall rule for TCP 9997 and UDP 5353 created.
- Creates **"nvmon Dashboard"** shortcut on your Desktop.

### 3. Configure the node list (optional — mDNS auto-discovery will find nodes on the LAN)

Edit `F:\nvmon\nvmon.toml` and fill in the `[[controller.nodes]]` entries:

```toml
[[controller.nodes]]
hostname = "RENDER-NODE-01"
url = "http://10.0.0.11:9998"

[[controller.nodes]]
hostname = "RENDER-NODE-02"
url = "http://10.0.0.12:9998"
```

Optionally define groups for targeted fan-out:

```toml
[[controller.groups]]
name = "outputs"
nodes = ["RENDER-NODE-01", "RENDER-NODE-02"]
```

See `docs/CONFIG.md` for the full reference.

#### Big studio: many servers from an IP list

When you have dozens of render servers, declare them as a plain IP list
instead of one block each — the controller fills in the hostnames itself
from each agent's `/health`:

```toml
[controller]
node_ips = [
  "192.0.2.11", "192.0.2.12", "192.0.2.13", "192.0.2.14",
  "192.0.2.21", "192.0.2.22",
]
poll_timeout_seconds = 5   # dead node can't stall the poll cycle
poll_workers = 0           # auto: one worker per node (cap 64)
```

Per-server install is the same on every box: copy `NVMON\`, run
`install.bat --listener`, make sure `monitor.url` points at the controller.
Each server runs its own **listener** (agent + launcher); the **monitor**
(controller + dashboard) runs on one box. Nothing per-node needs hand
editing — hostnames, GPU count (single or multi-GPU), display topology and
sync role are all discovered.

#### Blackwell (RTX PRO 6000) and multi-GPU notes (verified 2026-10-29)

- **Driver**: run the whole fleet on one build. R595 U7 **596.86**
  (2026-07-30) is the current RTX Enterprise Production Branch; it fixes
  Mosaic outputting 60.00 Hz instead of 59.94 Hz on some configurations and
  is the fixed version for NVIDIA's September-2026 display-driver security
  bulletin. Older R595 builds raise a driver advisory in `status.errors[]`.
- **RTX PRO Sync firmware**: 3.06 is the latest on nvidia.com (no 3.07+).
  Blackwell requires ≥ 3.04. Users still report the "Timing Server fails to
  apply" bug on 3.04/3.06 with 596.36–596.72
  (forums.developer.nvidia.com/t/362499); NVIDIA states sync tolerances were
  tightened on Blackwell and recommends 596.86. In practice: the house-sync
  rate must match the display rate exactly — nvmon enforces 0.02 Hz, fails
  the apply fast when the generator is at another rate, and only counts a
  lock to the **requested** source.
- **Mosaic Utility**: `configureMosaic.exe` 2.5.3 (2026-07-13) deprecates the
  `display=` parameter and adds an admin check. nvmon only ever uses
  `out=GPU,OUTPUT`, so both 2.4.9 and 2.5.3 work. Our fleet stages the
  signed 2.5.3 binary as `bin\configureMosaic-2.5.3.exe` (NVIDIA
  Authenticode signature verified, SHA-256
  `ec2609399c9aa68c4ee14017a94552b8f8fa40e8a3f20240d535c4d1a2d93f68`; not
  in the repo, see [Required NVIDIA tools](#required-nvidia-tools)).
  To switch the fleet: rename the 2.4.9 `configureMosaic.exe` to
  `configureMosaic-2.4.9.exe`, copy `configureMosaic-2.5.3.exe` to
  `configureMosaic.exe`, run `configureMosaic.exe query current` on one node
  as Administrator, then mirror `bin\`. It was not made the active tool
  automatically because the swap changes what every apply executes.
- **Multi-GPU vs single-GPU**: profiles carry `out=GPU,OUTPUT` per panel and
  QSync displays as `(gpu, display)` pairs discovered from
  `configureQsync2 queryAll`; one GPU with four heads and two GPUs with two
  heads each both capture and apply without edits. A Mosaic spanning GPUs
  needs the RTX PRO Sync board (that's NVIDIA's rule, not nvmon's).
- **Preflight before blanking a wall**: `GET /node/<host>/mosaic/timings?profile=X`
  tells you in seconds whether the wall would come up at the profile's rate.
  Asking for `24`/`30` on panels that only offer 23.976/29.97 natively needs
  a custom timing in NVIDIA Control Panel first.

### 4. Launch the dashboard

Double-click **"nvmon Dashboard"** on your Desktop. This:

1. Starts the controller in a hidden background window (no console).
2. Opens your default browser to `http://localhost:9997/`.

If auth is enabled, the first request serves a one-input login form. Enter the password once; the cookie persists until you close the browser.

To stop: `F:\nvmon\kill.bat controller`.

---

## Password setup

### When `install.bat` runs

You're prompted once:

```
Password (blank for no-auth):
```

- Enter a password -> written to `F:\nvmon\secrets\controller.password`.
- Leave blank -> empty file created; controller boots in no-auth mode
  and prints one warning line at startup.

### Rotating the password

1. Edit `F:\nvmon\secrets\controller.password` (single line, just the password).
2. Restart the controller:
   ```cmd
   F:\nvmon\kill.bat controller
   ```
   Then double-click the "nvmon Dashboard" shortcut again.

Clients with old cookies will get a fresh login form on their next request.

### Disabling auth

Empty the password file, restart the controller. It'll print a no-auth warning.

### ACL hardening

`install.bat` runs `icacls` to lock `secrets\` to owner + `SYSTEM` + `Administrators`.
To re-apply (e.g. after restoring from backup):

```cmd
icacls F:\nvmon\secrets /inheritance:r
icacls F:\nvmon\secrets /grant:r "%USERNAME%:(OI)(CI)F"
icacls F:\nvmon\secrets /grant:r "SYSTEM:(OI)(CI)F"
icacls F:\nvmon\secrets /grant:r "Administrators:(OI)(CI)F"
```

---

## Firewall rules

`install.bat` creates rules automatically, but if you ever need them by hand:

```cmd
netsh advfirewall firewall add rule name="nvmon agent"      dir=in action=allow protocol=TCP localport=9998 profile=private,domain
netsh advfirewall firewall add rule name="nvmon controller" dir=in action=allow protocol=TCP localport=9997 profile=private,domain
netsh advfirewall firewall add rule name="nvmon launcher"   dir=in action=allow protocol=TCP localport=9996 profile=private,domain
netsh advfirewall firewall add rule name="nvmon mDNS"       dir=in action=allow protocol=UDP localport=5353 profile=private,domain
```

The rules are scoped to the `private` and `domain` profiles by default -
**not** `public`. If your render LAN is incorrectly classified as Public,
either reclassify it or add `public` to the rule.

---

## Verification checklist

Run through this once after install, then again whenever something seems off.

### On each render node

```cmd
REM Agent is running
curl http://localhost:9998/health
REM Expected: JSON with "ok": true, capabilities listed

REM Full status dump
curl http://localhost:9998/status

REM Each subsystem
curl http://localhost:9998/gpu
curl http://localhost:9998/displays
curl http://localhost:9998/mosaic
curl http://localhost:9998/qsync

REM Profiles loaded
curl http://localhost:9998/profiles

REM From another machine, confirm firewall
curl http://<node-ip>:9998/health
```

### On the controller

```cmd
REM Controller is running
curl http://localhost:9997/health

REM Node list loaded - every hostname from nvmon.toml should appear
curl http://localhost:9997/nodes -H "X-NVMON-Password: <your-password>"

REM Aggregated status
curl http://localhost:9997/status -H "X-NVMON-Password: <your-password>"

REM Dashboard in a browser
start http://localhost:9997/
```

### Sanity probes

- [ ] Every `[[controller.nodes]]` entry shows `reachable: true` in `/nodes`.
- [ ] Every render node's `/health.capabilities` shows what you expect
      (`true` where the exe exists, `false` where it doesn't).
- [ ] `POST /apply` works against one node:
      ```cmd
      curl -X POST http://<node>:9998/apply -d "{\"profile\":\"desktop\"}"
      ```
- [ ] Fan-out works against all nodes:
      ```cmd
      curl -X POST http://localhost:9997/all/restore -H "X-NVMON-Password: <pw>"
      ```
      Every node should return `status: accepted` (or `skipped` with a
      reason that makes sense).

---

## Common tasks

### Stop a service

```cmd
F:\nvmon\kill.bat agent        # or controller / launcher / all
```

### Restart a service

```cmd
F:\nvmon\kill.bat agent
```
Then double-click the "nvmon Agent" shortcut (or wait for next login).

### Reload config

Restart the service whose config you changed — there is no hot-reload.

### Upgrade nvmon

```cmd
F:\nvmon\kill.bat all
git pull
F:\nvmon\python\python.exe -m pip install -r requirements.txt
```
Restart the shortcuts (or log out / in).

### Un-install startup shortcuts

Delete them from the Startup folder:

```cmd
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\nvmon Agent.lnk"
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\nvmon Launcher.lnk"
del "%USERPROFILE%\Desktop\nvmon Dashboard.lnk"
```

### Remove firewall rules

```cmd
netsh advfirewall firewall delete rule name="nvmon agent"
netsh advfirewall firewall delete rule name="nvmon controller"
netsh advfirewall firewall delete rule name="nvmon launcher"
netsh advfirewall firewall delete rule name="nvmon mDNS"
```

---

## Troubleshooting

### `install.bat` exits immediately with "Run As Administrator"

Right-click `install.bat` and choose `Run as administrator`. The script intentionally refuses to run without admin — it needs `netsh`, `icacls`, and shortcut creation in system locations.

### Shortcut does nothing when double-clicked

Most common causes:

- **Port already in use** — a previous copy is still running. Run `F:\nvmon\kill.bat all` first.
- **Enclosed Python is broken** — rerun `install.bat --role <role>` to reinstall `python\`.
- **NVIDIA subsystems missing** — check `F:\nvmon\bin\` contains `configureMosaic.exe` / `configureDdisplay.exe` / `configureQsync2.exe`. Missing exes just disable those subsystems; the agent still boots.

To debug, start the daemon directly (console visible):

```cmd
F:\nvmon\python\python.exe -m nvmon agent
```

You'll see the boot log and any errors.

### `/health` returns, but capabilities show `available: false` everywhere

The agent can't find the NVIDIA CLI exes. Check:

1. `F:\nvmon\bin\` exists and contains `configureMosaic.exe` etc.
2. Or, the `[agent.bin]` paths in `nvmon.toml` point at the right files.
3. Or, the exes are on `PATH`.

`nvidia-smi` being missing usually means NVIDIA drivers aren't installed,
or this isn't an NVIDIA-GPU machine.

### POST endpoints return `403 admin_required`

The agent process isn't running elevated. The Startup-folder shortcut launches as the logged-in user, so if that user isn't an administrator, POSTs will be gated. Either log in as an admin user, or right-click the shortcut → Properties → Advanced → "Run as administrator".

### Controller shows every node as `reachable: false`

Most common causes:

- Firewall on the render node is blocking TCP 9998. Run
  `netsh advfirewall firewall show rule name="nvmon agent"` on the node.
  If the rule is present but inbound still blocked, check the network
  profile classification (`private` vs `public`).
- Render node isn't actually running the agent. Sign in to the node,
  run `curl http://localhost:9998/health`.
- URL in `[[controller.nodes]]` is wrong. Copy/paste failed, port number
  swapped, http vs https, etc.

### Controller returns `401 Unauthorized`

You're in password mode but missing / sending the wrong password.

- Browser: clear cookies, reload, enter the password in the login form.
- API client: include `X-NVMON-Password: <password>` header.
- If you forgot the password: edit
  `F:\nvmon\secrets\controller.password` on the controller box, restart
  the controller.

### `/all/apply` returns many `status: failed, error: admin required on target`

The target nodes are running their agents non-elevated. Run `install.bat agent`
on those nodes (the Task Scheduler entry uses `/RL HIGHEST`), then reboot
or log out / back in.

### `/status` returns but shows stale data

- Check `poll_interval_seconds` in `nvmon.toml`.
- Look at `logs\nvmon.log` for subsystem-query errors.
- One failing subsystem shouldn't stall the others - if it does, that's
  a bug: capture `logs\nvmon.log` and file an issue.

### Logs to look at

| Log                                          | Who writes it     |
|----------------------------------------------|-------------------|
| `F:\nvmon\logs\nvmon.log`                    | Agent / controller / launcher runtime |
| `F:\nvmon\logs\install.log`                  | `install.bat`     |
| Windows Event Viewer -> Task Scheduler       | Scheduled task start/stop history |

---

## Reference

- [API.md](API.md) - HTTP endpoint details for every service
- [CONFIG.md](CONFIG.md) - Every `nvmon.toml` key documented
- [ARCHITECTURE.md](ARCHITECTURE.md) - Design, data flow, polling model
