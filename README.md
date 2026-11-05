<p align="center">
  <img alt="nvmon" src="NVMON/nvmon/controller/dashboard_unified/static/img/nvmon-banner-dark.svg">
</p>

# nvmon

Monitoring and control daemon for NVIDIA Windows render nodes — agent /
controller / launcher with a unified HTTP dashboard.

`nvmon` provides a single monitoring and fan-out control surface across a
fleet of NVIDIA-equipped servers. It covers DirectDisplay, Mosaic, QSync,
GPU telemetry, EDID, profile load/apply, and validation — exposed over
HTTP and usable from a browser or from scripts.

> **The deployable product is in [`NVMON/`](NVMON/).** That folder is what
> you copy to each render node and to the central monitor box. The repo
> root only contains this README and `.gitignore`.

## Dashboard

The controller exposes the unified fleet dashboard at `http://<monitor>:9997`.
Every node appears as a card with live status, GPU telemetry, sync state,
and per-node actions. Fan-out actions across the whole fleet (Re-apply All,
Sync All, Restore) sit in the header.

![nvmon dashboard](NVMON/docs/screenshots/dashboard-home.png)

Access is gated by a single shared password in
`NVMON/secrets/controller.password`. Empty file → no-auth mode (with a
startup warning). Browsers see a login form; API clients send the password
in the `X-NVMON-Password` header.

![nvmon login](NVMON/docs/screenshots/dashboard-login.png)

## Quick start

```cmd
cd NVMON

bats\install.bat --listener        :: render node, once, as administrator
bats\install.bat --monitor         :: monitor box, once, as administrator

run_listener.bat                   :: every render node — agent + launcher
run_monitor.bat                    :: monitor box — controller + dashboard
```

`kill_listener.bat` / `kill_monitor.bat` are the safety-net stops.

### Required NVIDIA tools

Render nodes need three NVIDIA command-line utilities. **They are not in this
repo**: they are NVIDIA software under NVIDIA's own licence terms. Download them from
NVIDIA's enterprise / RTX professional (Quadro) driver tools pages and drop
them in `NVMON\bin\`. Alternatively, set `NVMON_NVIDIA_TOOLS` to a folder that
holds them before you run `install.bat`. `install.bat` warns up front about any
that are missing. The agent still boots without them, and each missing tool's
subsystem reports `available: false` with a message naming the file.

| File | NVIDIA utility | Tested version | SHA-256 of the copy we run |
|---|---|---|---|
| `configureMosaic.exe` | Mosaic Utility | 2.4.9 (per `docs/DEPLOYMENT.md`) | `9701e401f5a6535b599ae7538e3754394930661416dde844cf22737ed073220c` |
| `configureMosaic-2.5.3.exe` | Mosaic Utility (optional, staged upgrade) | **2.5.3** | `ec2609399c9aa68c4ee14017a94552b8f8fa40e8a3f20240d535c4d1a2d93f68` |
| `configureDdisplay.exe` | Direct Display configuration | 1.8.3405.878 | `b73920f75509fd7acbeb7367b5021e128033ef3fafbd6c69c41a5d88cf91805c` |
| `configureQsync2.exe` | Quadro Sync configuration | 2.1.3339.3914 | `b220ec180d30ca9a238509e95965f7c60a2dee45c2349c474000d703d3168ce4` |

nvmon only looks up `configureMosaic.exe`. To run Mosaic 2.5.3, save it under
that name (see `NVMON/docs/DEPLOYMENT.md`). Check a download with
`certutil -hashfile <file> SHA256` (or PowerShell `Get-FileHash`).

The prebuilt dashboard window **is** in the repo. Verify it the same way:

| File | SHA-256 |
|---|---|
| `NVMON\bin\nvmon-window\nvmon-window.exe` | `0a9ece65404cdb15f6f23b972e9af57f5d42e00e4a923d9187fc21641b4e77db` |

Its source and build script are in `NVMON\bin\nvmon-window\src\`. The WebView2
DLLs next to it ship under Microsoft's redistribution licence
(`WebView2-LICENSE.txt`).

### Desktop app (any Windows machine on the control network)

```cmd
nvmon_app.bat                      :: or: python\python.exe -m nvmon app
python\python.exe -m nvmon app --shortcuts   :: 'nvmon' on the desktop + in NVMON\
```

A borderless, resizable dashboard window (`bin\nvmon-window\`, WebView2 —
built into Windows 10/11, nothing to install) with its own local controller.
The header is the title bar; its ◉ button collapses everything into a small
floating widget: sync ring (green = all nodes online and in sync), refresh
rate, colour depth, and a reopen button. Starts with Windows (toggle in the
widget's right-click menu).

### Updating an existing site

From a newer copy (e.g. a fresh clone), point it at the existing install:

```cmd
update_site.bat E:\NVMON --dry-run   :: show the plan first
update_site.bat E:\NVMON             :: 1) monitor only  2) monitor + listeners  3) listener
```

Code only: `nvmon.toml`, secrets, profiles, EDIDs and logs are never touched;
every replaced file is backed up with a `ROLLBACK.txt`; each step says
whether it is safe, a brief restart, or changes files before asking.

See [`NVMON/README.md`](NVMON/README.md) for the full product overview,
manual IP configuration (`nvmon.toml`), preset sync, redeploy, and the
complete catalog of batch scripts.

## Architecture

| Role         | Port | Auth                | Per machine        |
|--------------|------|---------------------|--------------------|
| `agent`      | 9998 | none (trusted LAN)  | one per node       |
| `controller` | 9997 | one shared password | one for the fleet  |
| `launcher`   | 9996 | none                | optional, per node |
| `listener`   | —    | n/a (supervisor)    | spawns agent + launcher together |

- The **agent** owns the local hardware — shells out to `nvidia-smi`,
  `configureMosaic.exe`, `configureDdisplay.exe`, `configureQsync2.exe`.
- The **controller** polls every agent, aggregates `/status` + `/health`,
  and fans out commands across the fleet.
- The **launcher** is a generic process supervisor for production apps that
  need to be tied to nvmon's lifecycle.
- The **listener** is a convenience supervisor that spawns `agent` and
  `launcher` together in one console.

Architecture and design specs live in [`NVMON/docs/`](NVMON/docs/).

## Testing

```cmd
cd NVMON
python\python.exe -m pytest                     :: full suite
python\python.exe -m pytest tests/unit          :: unit only
python\python.exe -m pytest tests/smoke         :: smoke (requires a running controller)
```

## Threat model

- The agent and launcher are **unauthenticated** by design — the assumption
  is a private render subnet, not the public internet.
- The controller is the only authenticated surface. One shared password in
  `NVMON/secrets/controller.password`, sent via the `X-NVMON-Password`
  header or a session cookie from the login page.
- Out of scope for the current wave: TLS, per-user accounts, session
  timeouts, audit logs.

## License

nvmon is released under the [MIT License](LICENSE).

Third-party components keep their own licences:

- **NVIDIA CLI tools** (`configureMosaic.exe`, `configureDdisplay.exe`,
  `configureQsync2.exe`) are NVIDIA software and are **not included**. Get
  them from NVIDIA (see [Required NVIDIA tools](#required-nvidia-tools)).
- **WebView2** runtime DLLs in `NVMON/bin/nvmon-window/` are Microsoft's,
  redistributed under `WebView2-LICENSE.txt`.
- **htmx, Alpine.js and Chart.js** in
  `NVMON/nvmon/controller/dashboard_unified/static/vendor/` are under their
  upstream projects' licences.
- The enclosed Python runtime and pip packages, which `install.bat` downloads
  at install time, are covered by their own licences.
