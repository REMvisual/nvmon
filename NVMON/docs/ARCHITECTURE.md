# nvmon Architecture

nvmon is a unified monitoring + fan-out control layer for a fleet of
Windows machines with NVIDIA GPUs. It ships as a single Python package
with three process roles - agent, controller, launcher - all running off
the same codebase, differing only by entry-point.

The HTTP surface is documented in [API.md](API.md).

---

## System diagram

```
                       +----------------------------+
                       |   Operator workstation     |
                       |   browser :9997            |
                       +-------------+--------------+
                                     |
                                     | HTTP (+ optional password)
                                     v
                       +----------------------------+
                       |     nvmon controller       |
                       |     F:\nvmon\ :9997        |
                       |  - polls every agent       |
                       |  - serves unified HTML     |
                       |  - fans out commands       |
                       +------+-----+-----+----+----+
                              |     |     |    |
                  HTTP polls  |     |     |    |  fan-out POST
                              v     v     v    v
      +-----------+  +-----------+  +-----------+  +-----------+
      |  agent    |  |  agent    |  |  agent    |  |  agent    |
      |  :9998    |  |  :9998    |  |  :9998    |  |  :9998    |
      |  Node-01  |  |  Node-02  |  |  Node-03  |  |  Node-04  |
      +-----+-----+  +-----+-----+  +-----+-----+  +-----+-----+
            |              |              |              |
            | subprocess   |              |              |
            v              v              v              v
     nvidia-smi     configureMosaic.exe  configureDdisplay.exe  configureQsync2.exe
     + EDID registry writes

     (Optional, on any node)
     +------------------+
     |  nvmon launcher  |  :9996 - generic app-launcher surface
     +------------------+
```

---

## Process roles

### Agent - `python -m nvmon agent`

Runs on every render / output node. One process per machine. Owns the
local NVIDIA hardware.

- HTTP server on port `9998`.
- Background monitor loop polls GPU, DirectDisplay, Mosaic, and QSync every
  `poll_interval_seconds`. Writes `status.json` atomically each tick.
- Read endpoints (`GET /health`, `/status`, `/gpu`, `/displays`, `/mosaic`,
  `/qsync`, `/profiles`, `/validate`) serve the cached snapshot.
- Write endpoints (`POST /apply`, `/restore`, `/dd/*`, `/qsync/*`,
  `/mosaic/disable`) shell out to the NVIDIA CLI tools. These require the
  agent itself to be running elevated.
- No authentication. Intended for a trusted LAN.
- Graceful degradation: if a subsystem exe (e.g. `configureMosaic.exe`) isn't
  present, the daemon boots anyway, `/health.capabilities.mosaic.available`
  is `false`, and `/mosaic` returns `503`. Other subsystems keep working.

### Controller - `python -m nvmon controller`

Runs on your workstation (or any box with network reach to all agents).
One process serves the whole fleet.

- HTTP server on port `9997`.
- Reads the node list from `[[controller.nodes]]` in `nvmon.toml` (no
  auto-discovery in Wave 1).
- Poll loop hits every agent's `/health` and `/status` in parallel, merges
  into a single view.
- Serves the **unified HTML dashboard** at `/`.
- Fan-out endpoints (`POST /all/*`, `/group/<name>/*`, `/node/<hostname>/*`)
  dispatch the same request to every matching node in parallel, with a
  per-node timeout, and return a per-node result summary.
- Single shared password. If the password file is missing or empty,
  runs in no-auth mode and prints one warning at startup.

### Launcher - `python -m nvmon launcher`

Optional. One process per machine where you want remote app launching.

- HTTP server on port `9996`.
- Reads `[[launcher.apps]]` from `nvmon.toml`.
- Endpoints: `GET /health`, `GET /apps`, `POST /launch`, `POST /kill`.
- Wave 1 ships an empty `apps` list; Wave 2 populates it.

---

## Data flow

### Read path (dashboard + API)

```
[every poll_interval]           [every HTTP request]
    agent monitor                 client -> controller
        |                                 |
        v                                 v
    subprocess call                 HTTP fan-out to every agent
    (nvidia-smi,                         |
     configure*.exe)                     v
        |                           in-memory cache
        v                                 |
    parse output                          v
    (nvmon/parsers.py)            JSON aggregate or HTML render
        |                                 |
        v                                 v
    build dataclass                 HTTP response
    (nvmon/types.py)
        |
        v
    write status.json (atomic)
        |
        v
    served by /status on GET
```

### Write path (control)

```
client --> controller :9997 /all/apply {profile}
               |
               v
     password check (if enabled)
               |
               v
     look up matching nodes (capability filter)
               |
               v
     parallel POST /apply to each node, per-node timeout
               |
               v
     collect {hostname, status, http_code} per node
               |
               v
     respond with fan-out summary

(On each agent)
     POST /apply -> admin check -> queue async worker ->
        subprocess configureMosaic.exe + configureDdisplay.exe +
        configureQsync2.exe per profile ->
        monitor loop picks up new state next poll
```

---

## Polling model

Both the agent (local subsystems) and controller (remote agents) use the
same default interval: `poll_interval_seconds = 5`. Each poll is a full
snapshot - no incremental diffs, no event stream. This keeps the model
simple at the cost of ~5s max staleness.

### Agent-side

- One background thread owns the monitor loop.
- Loop body: call each subsystem's read path, assemble a `Status` dataclass,
  write `status.json` atomically (write to tempfile + `os.replace`).
- Read endpoints never block on the subsystem - they serve the last snapshot.
- If a subsystem query raises, it's logged and marked as an `error` in the
  next `status.json`; other subsystems in the same cycle still update.

### Controller-side

- One background thread per node? No - one shared thread pool, `len(nodes)`
  parallel HTTP gets per tick.
- Aggregator keeps a per-node cache: last `/health`, last `/status`, last
  contact time, last error.
- `GET /status` on the controller returns the cache - it does NOT trigger
  a fresh poll. Use `poll_interval_seconds` to tune freshness.

---

## Fan-out model

The controller's `/all/*` and `/group/*` endpoints dispatch a POST to every
matching node in parallel and collect the results.

```
POST /all/apply {profile: "mosaic-2397"}
    |
    v
 plan = [for each node in config / group:
           skip if not reachable (last_seen stale)
           skip if missing capability (e.g. no mosaic exe)
           else dispatch]
    |
    v
 parallel HTTP POSTs, each bounded by fanout_timeout_seconds
    |
    v
 collect:
   {hostname, status: accepted|ok|failed|timed_out|unreachable|skipped,
    http_code, error?}
    |
    v
 respond: {requested, succeeded, failed, timed_out, results}
```

Key properties:

- **Best-effort**: one node failing doesn't cancel the others.
- **No retries**: operator reads the summary, decides.
- **Capability-aware**: a node without `configureMosaic.exe` gets `skipped`,
  not `failed`, when you fan out a Mosaic command.
- **Idempotent where possible**: `/apply`, `/restore`, `/dd/enable` can all
  be safely retried against the same node.

---

## Authentication model

Minimal by design. The threat being addressed is "someone walks past an
unlocked browser and hits Restore All", not a remote adversary.

| Surface    | Auth                         |
|------------|------------------------------|
| Agent      | none (trusted LAN)           |
| Launcher   | none (trusted LAN)           |
| Controller | one shared password, or none |

Controller details:

- Password lives in one plaintext file (default
  `F:\nvmon\secrets\controller.password`). One line. No hashing.
- **Missing or empty file** -> no-auth mode. One warning logged at startup.
  Intentional - keeps getting-started painless.
- Browser: first request without the auth cookie serves a one-input login
  form. Submit -> cookie set -> cookie has no expiry (clears when the
  browser does). `POST /logout` clears it.
- API: include `X-NVMON-Password: <password>` header. CLI tools read the
  same password file so the operator never types it.
- To rotate: edit the file, restart the controller. No UI.

Explicitly out of scope for Wave 1: TLS, per-user accounts, session
timeouts, bearer tokens, audit logs, password-change UI.

---

## Error handling

| Failure                            | Behavior                                              |
|------------------------------------|-------------------------------------------------------|
| Subsystem query fails              | Logged; subsystem marked `available: false`; daemon continues. |
| Subsystem exe missing at startup   | Warn in log; capability advertises `available: false`; its endpoint returns 503. |
| POST without Administrator         | 403 with `{"error": "admin_required", ...}`.          |
| HTTP bind failure                  | Fatal exit with clear message (port in use / need admin). |
| Config file missing                | Warn; boot with defaults.                             |
| Malformed TOML                     | Warn; boot with defaults.                             |
| Controller: node unreachable       | Aggregator marks `reachable: false`; dashboard shows OFFLINE; fan-out reports `status: unreachable`. |
| Controller: node timeout           | Fan-out reports `status: timed_out` (distinct from `failed`). |
| Controller: node missing capability| Fan-out reports `status: skipped, reason: capability X unavailable`. |

---

## Repository layout

```
F:\nvmon\
  install.bat            # One installer — enclosed Python, deps, firewall, shortcuts
  kill.bat               # Kill by port (agent / controller / launcher / all)
  python\                # Enclosed embedded Python (created by install.bat)
  nvmon.toml.example     # Config template
  requirements.txt
  pyproject.toml

  nvmon\                 # Python package (owned by other Wave 2 agents)
    __main__.py
    cli.py
    config.py
    types.py
    parsers.py
    executors.py
    log.py
    agent\...
    controller\...
    launcher\...

  bin\                   # NVIDIA CLI tools (bundled; NVIDIA licence)
    configureMosaic.exe
    configureDdisplay.exe
    configureQsync2.exe

  profiles\              # User-managed profile JSON files
  secrets\               # Password file (owner-only ACL)
  logs\                  # Runtime logs

  tests\
    conftest.py
    unit\
      test_config.py
      test_parsers.py
      test_types.py
    smoke\
      test_agent_endpoints.py
      test_controller_endpoints.py
    fixtures\
      nvdd_output\       # Real captured output from NVIDIA CLIs

  docs\
    API.md
    CONFIG.md
    ARCHITECTURE.md      # you are here
    DEPLOYMENT.md
    specs\               # Design documents
```

---

## Non-goals for Wave 1

Tracked separately for Wave 2-6:

- Network auto-discovery (controller reads a static node list today)
- Profile schema v2 / modular profiles
- Rich dashboard UX (minimal but usable HTML today)
- Listener rewrite
- Direct NVAPI bindings (everything shells out)
- History / time-series / alerting database
- TLS / per-user auth / audit logging
