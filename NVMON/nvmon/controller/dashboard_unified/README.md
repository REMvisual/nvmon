# nvmon — Unified Dashboard (Sub-project D)

Real-time operator dashboard for the nvmon controller. Single page, drill-over
node detail, fan-out apply/sync/restore, alert banner, groups.

**Status:** UI implementation complete; awaits A3 controller wiring.

## What's here

```
dashboard_unified/
├── __init__.py              # mount_routes(server, aggregator, fanout) — stub for A3
├── routes.py                # canonical list of routes A3 must register
├── sse.py                   # SSE event generator contract + dataclasses
├── ws_fanout.py             # WebSocket fan-out progress protocol + dataclasses
├── render_test.py           # (removed after dev-preview.html was generated)
├── dev-preview.html         # static preview of the dashboard against mock data
├── templates/
│   ├── layout.html          # Jinja2 base (header, counters, footer, portals)
│   ├── dashboard.html       # main unified view
│   ├── node_detail.html     # slide-over HTMX partial
│   ├── fanout_modal.html    # confirm/progress/summary modal
│   └── login.html           # one-field password form
├── static/
│   ├── css/nvmon.css        # from-scratch dark theme, NVIDIA-green accents
│   ├── js/nvmon.js          # SSE client, Alpine store, sparklines, shortcuts
│   ├── js/charts.js         # Chart.js GPU telemetry history
│   └── vendor/              # htmx.min.js, alpine.min.js, chart.umd.js (real)
└── fixtures/
    └── mock_status.json     # realistic aggregate state for dev/tests
```

## Tech stack

- **Vanilla JS + HTMX 2.x + Alpine.js 3.x + Chart.js 4.x** — all vendored under
  `static/vendor/`. Zero build step. No CDN fetches.
- **Jinja2** templates rendered server-side by A3.
- **SSE** (`/stream`) for live updates — one long-lived connection per tab.
- **WebSocket** (`/fanout/job/<id>/ws`) only for short-lived fan-out progress.

## How A3 activates the dashboard

```python
from nvmon.controller.dashboard_unified import (
    mount_routes, TEMPLATES_DIR, STATIC_DIR
)
from nvmon.controller.aggregator import Aggregator
from nvmon.controller.fanout     import FanoutExecutor

aggregator = Aggregator(...)
fanout     = FanoutExecutor(...)

# In controller/server.py:
mount_routes(server, aggregator=aggregator, fanout=fanout)
```

### What A3 must do beyond calling `mount_routes`

1. **Serve `static/`** at `/static` (so `/static/css/nvmon.css`, `/static/vendor/htmx.min.js`,
   etc. resolve).
2. **Create a Jinja2 `Environment`** rooted at `TEMPLATES_DIR` and inject the
   template context on each render:
   - `static_url`      — default `/static`
   - `controller`      — dict: `{version, uptime_seconds, stream_status,
                                 last_aggregate, nodes_online, nodes_offline,
                                 alerts_count}`
   - `groups`          — list[{name, count, nodes}]
   - `nodes`           — list[aggregated node dict — see `fixtures/mock_status.json`]
   - `alerts`          — list[alert dict]
   - `profiles_union`  — list[str] (profiles present on every online node)
3. **Wire the routes** per `routes.py` (11 routes; see `routes.summary()`).
4. **Auth**: every dashboard route except `/login` and `/static/*` requires the
   password cookie. Use the existing `controller/auth.py`. If the password
   file is missing/empty (spec §auth), bypass the check and print a warning.
5. **Implement the aggregator hooks** expected by `sse.stream()`:
   - `snapshot() -> dict` — matches `fixtures/mock_status.json`
   - `subscribe() -> Iterator` — yields `sse.NodeUpdate | NodeReachableChange |
     AlertEvent | Heartbeat`
   - `history(hostname, metric, window_seconds) -> list[{t, v}]`
6. **Implement the fan-out hooks** expected by `ws_fanout.fanout_ws_handler()`:
   - `create_job(action, scope, profile=None) -> job_id`
   - `job_stream(job_id) -> Iterator[ProgressFrame | SummaryFrame]`
   - `cancel(job_id) -> None`

## SSE event catalogue

| Event name                | Payload (see `sse.py`)                                        |
|---------------------------|---------------------------------------------------------------|
| `node_update`             | Full node aggregate after each poll cycle                    |
| `node_reachable_change`   | Online↔offline transition only                                |
| `alert`                   | New alert firing (new id)                                     |
| `heartbeat`               | Every ~10s; keepalive + header counter refresh                |

Use `sse.format_sse(event_name, payload)` to produce wire bytes.

## Fan-out WebSocket protocol

Server → client:
- `{type: "progress", hostname, state, state_label?, duration_ms?, http_code?, reason?, error?}`
- `{type: "summary", succeeded, failed, skipped, failures: [{hostname, error, http_code?}]}`

Client → server:
- `{action: "cancel"}` — stop dispatching remaining pending nodes
- `{action: "ping"}`   — optional; server replies `{type: "pong", t}`

After `summary`, server closes with code 1000.

## Responsive breakpoints

| Width        | Grid       | Slide-over      | Header actions              |
|--------------|------------|-----------------|-----------------------------|
| < 640 px     | 1-col      | full-screen     | Alert banner always visible |
| 640–1023 px  | 2-col      | 85% drawer      | Full bar                    |
| 1024–1439 px | 3-col      | 70% drawer      | Full bar                    |
| ≥ 1440 px    | 4-col      | 60% drawer      | Full bar                    |

## Accessibility features

- Skip link → `#main`
- Focus-visible 2px NVIDIA-green outline on every interactive element
- Alert banner `role="alert"` + `aria-live="assertive"`
- Header counters `aria-live="polite"`
- Fan-out modal `role="dialog"` + `x-trap.inert.noscroll` for focus trap
- Node cards are `<article>` with `aria-labelledby`
- `prefers-reduced-motion: reduce` disables slide-in, spin, and pulse animations
- AAA (≥ 7:1) contrast for body text, AA (≥ 4.5:1) for muted labels
- Status dots paired with text labels — never color-only

## Keyboard shortcuts

- `A` — open Re-apply All picker
- `S` — trigger Sync All
- `R` — force reconnect stream
- `G` — focus group tabs
- `/` — focus filter input
- `Esc` — close slide-over / modal
- `↑ ↓ ← →` — navigate node cards within the grid
- `Enter` — open focused node's drill-down

## Dev preview

Run from this directory:

```bash
python render_test.py     # (script removed after first run — see dev-preview.html)
```

`dev-preview.html` is a static render of `dashboard.html` against
`fixtures/mock_status.json`. Open it in a browser to inspect layout and
styling without running the controller. (Note: SSE, HTMX partials, and
fan-out will not function in the preview — it's a visual-only artifact.)

## Vendor files

All three libraries are real minified builds downloaded from unpkg.com:

- `static/vendor/htmx.min.js`   (HTMX 2.0.4, 51 KB)
- `static/vendor/alpine.min.js` (Alpine.js 3.14.1, 45 KB)
- `static/vendor/chart.umd.js`  (Chart.js 4.4.4, 201 KB UMD build)

Total ~297 KB uncompressed; ~90 KB gzipped on-the-wire. Keep these updated
on a quarterly cadence.
