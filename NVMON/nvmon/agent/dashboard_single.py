"""Single-host HTML dashboard — debug-only.

Renders the current `status.json` shape into a self-contained HTML page with
a 5-second meta refresh. Intended for quick "is this node alive and synced?"
checks while developing; the real operator UX lives on the controller.

The page is pure server-rendered HTML with one small inline script for
QSync enable/disable buttons. No external CSS, no frameworks.
"""

from __future__ import annotations

from .. import __version__, config


def build_html(data: dict, *, capabilities: dict | None = None) -> str:
    """Build the dashboard HTML from a status dict.

    Args:
        data: The status dict (same shape as GET /status).
        capabilities: Optional dict from `/health.capabilities` — if supplied,
            renders a capabilities row at the top of the page.
    """
    hostname = data.get("hostname", "?")
    ip = data.get("ip", "")
    timestamp = data.get("timestamp", "?")
    profile = data.get("active_profile", "") or "none"
    uptime = int(data.get("uptime_seconds", 0) or 0)
    poll = int(data.get("poll_count", 0) or 0)

    hours, rem = divmod(uptime, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    # --- Capabilities row ---
    cap_html = ""
    if capabilities:
        chips = []
        for name, cap in capabilities.items():
            ok = bool(cap.get("available"))
            cls = "cap-ok" if ok else "cap-off"
            tip = cap.get("reason") or cap.get("exe") or ""
            chips.append(
                f'<span class="cap {cls}" title="{_esc(tip)}">{name.upper()}</span>'
            )
        cap_html = '<div class="caps">' + "".join(chips) + "</div>"

    # --- Direct Display ---
    dd = data.get("direct_display", {}) or {}
    dd_active = bool(dd.get("active", False))
    displays_list = dd.get("displays", []) or []

    managed = [
        d for d in displays_list
        if not config.is_excluded_display(d.get("name", ""), tuple(d.get("resolution", [0, 0])))
    ]
    excluded = [
        d for d in displays_list
        if config.is_excluded_display(d.get("name", ""), tuple(d.get("resolution", [0, 0])))
    ]

    dd_rows = ""
    for d in managed:
        res = d.get("resolution", [0, 0])
        rate = d.get("refresh_rate", 0) or 0
        frac = d.get("refresh_fraction", "") or ""
        status = d.get("status", "?") or "?"
        status_cls = "ok" if "removed" in status.lower() else "warn"
        gpu_idx = d.get("gpu_index", -1)
        disp_idx = d.get("display_index", -1)
        dd_rows += (
            "<tr>"
            f"<td>{_esc(d.get('name', '?'))}</td>"
            f"<td>GPU {gpu_idx} / #{disp_idx}</td>"
            f"<td>{_esc(d.get('id', '?'))}</td>"
            f"<td>{_esc(d.get('os_target_id', '?'))}</td>"
            f"<td>{res[0]}x{res[1]}</td>"
            f"<td>{rate:.3f}Hz {'(' + _esc(frac) + ')' if frac else ''}</td>"
            f'<td class="{status_cls}">{_esc(status)}</td>'
            "</tr>"
        )

    # --- Mosaic ---
    mos = data.get("mosaic", {}) or {}
    mos_on = bool(mos.get("enabled", False))
    mos_per = mos.get("per_display_resolution", [0, 0])
    mos_total = mos.get("total_resolution", [0, 0])

    # --- QSync ---
    qs = data.get("qsync", {}) or {}
    qs_synced = bool(qs.get("synced", False))
    qs_house = bool(qs.get("house_sync_detected", False))
    qs_gpus = qs.get("per_gpu_sync", []) or []

    gpu_sync_rows = ""
    for g in qs_gpus:
        synced = bool(g.get("synced", False))
        sig = bool(g.get("signal_available", False))
        note = g.get("sync_note", "") or ""
        label = f"YES ({_esc(note)})" if synced and note else _yn(synced)
        gpu_sync_rows += (
            "<tr>"
            f"<td>GPU {g.get('gpu', '?')}</td>"
            f'<td class="{"ok" if synced else "crit"}">{label}</td>'
            f'<td class="{"ok" if sig else "warn"}">{_yn(sig)}</td>'
            "</tr>"
        )

    # --- GPU table ---
    gpus = data.get("gpu", []) or []
    gpu_rows = ""
    for g in gpus:
        temp = g.get("temperature_c", 0) or 0
        temp_cls = "crit" if temp > 85 else ("warn" if temp > 75 else "ok")
        pwr = g.get("power_draw_w", 0) or 0
        pwr_lim = g.get("power_limit_w", 1) or 1
        pwr_pct = (pwr / pwr_lim * 100) if pwr_lim > 0 else 0
        mem_u = g.get("memory_used_mb", 0) or 0
        mem_t = g.get("memory_total_mb", 1) or 1
        mem_pct = (mem_u / mem_t * 100) if mem_t > 0 else 0
        gpu_rows += (
            "<tr>"
            f"<td>GPU {g.get('index', 0)}</td>"
            f"<td>{_esc(g.get('name', '?'))}</td>"
            f'<td class="{temp_cls}">{temp}C</td>'
            f"<td>{pwr:.0f}W / {pwr_lim:.0f}W ({pwr_pct:.0f}%)</td>"
            f"<td>{g.get('gpu_utilization_pct', 0)}%</td>"
            f"<td>{mem_u}MB / {mem_t}MB ({mem_pct:.0f}%)</td>"
            f"<td>{_esc(g.get('pstate', '?'))}</td>"
            "</tr>"
        )

    # --- Output health ---
    oh = data.get("output_health", {}) or {}

    # --- Validation ---
    val = data.get("validation", {}) or {}
    val_rows = ""
    for name, check in (val.get("checks", {}) or {}).items():
        passed = bool(check.get("passed", False))
        detail = check.get("detail", "")
        val_rows += (
            "<tr>"
            f"<td>{_esc(name)}</td>"
            f'<td class="{"ok" if passed else "crit"}">{_pass(passed)}</td>'
            f"<td>{_esc(detail)}</td>"
            "</tr>"
        )

    # --- Alerts ---
    alerts = data.get("alerts", []) or []
    if alerts:
        alerts_html = "".join(
            f'<div class="alert {"crit" if a.get("severity") == "CRITICAL" else "warn"}">'
            f'[{_esc(a.get("severity", "WARNING"))}] '
            f'{_esc(a.get("type", ""))}: {_esc(a.get("message", ""))}'
            "</div>"
            for a in alerts
        )
    else:
        alerts_html = '<div class="alert ok">No active alerts</div>'

    excluded_note = ""
    if excluded:
        names = ", ".join(_esc(d.get("name", "?")) for d in excluded)
        excluded_note = f'<div class="note">Excluded from monitoring: {names}</div>'

    host_title = f"{hostname}" + (f" &middot; {ip}" if ip else "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>nvmon agent - {hostname}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Consolas, monospace; background: #0a0a0a; color: #e0e0e0; padding: 20px; }}
  h1 {{ color: #76b900; margin-bottom: 5px; font-size: 22px; }}
  h2 {{ color: #999; font-size: 15px; margin: 20px 0 8px; border-bottom: 1px solid #333; padding-bottom: 4px; }}
  .header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }}
  .header-right {{ color: #666; font-size: 12px; }}
  .meta {{ color: #888; font-size: 13px; margin-bottom: 10px; }}
  .meta span {{ margin-right: 20px; }}
  .caps {{ display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }}
  .cap {{ font-size: 11px; padding: 2px 8px; border-radius: 3px; font-weight: 600; letter-spacing: 0.03em; }}
  .cap-ok {{ background: #0f2a0f; color: #76b900; border: 1px solid #76b900; }}
  .cap-off {{ background: #2a0f0f; color: #ff6666; border: 1px solid #ff4444; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 15px; }}
  .card {{ background: #151515; border: 1px solid #2a2a2a; border-radius: 6px; padding: 14px; }}
  .card-title {{ font-size: 14px; font-weight: 600; color: #76b900; margin-bottom: 10px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 3px; font-size: 12px; font-weight: 600; margin-left: 6px; }}
  .badge.on {{ background: #1a3a00; color: #76b900; border: 1px solid #76b900; }}
  .badge.off {{ background: #3a1a00; color: #ff6633; border: 1px solid #ff6633; }}
  .kv {{ display: flex; justify-content: space-between; padding: 3px 0; font-size: 13px; }}
  .kv .label {{ color: #888; }}
  .kv .val {{ color: #ddd; font-weight: 500; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 6px 8px; color: #888; border-bottom: 1px solid #333; font-weight: 500; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #1a1a1a; }}
  .ok {{ color: #76b900; }}
  .warn {{ color: #ffaa00; }}
  .crit {{ color: #ff4444; }}
  .alert {{ padding: 8px 12px; border-radius: 4px; margin-bottom: 6px; font-size: 13px; }}
  .alert.ok {{ background: #0a1a00; border-left: 3px solid #76b900; }}
  .alert.warn {{ background: #1a1500; border-left: 3px solid #ffaa00; }}
  .alert.crit {{ background: #1a0a0a; border-left: 3px solid #ff4444; }}
  .note {{ color: #666; font-size: 12px; font-style: italic; margin-top: 6px; }}
  .wide {{ grid-column: 1 / -1; }}
  .footer {{ color: #444; font-size: 11px; margin-top: 20px; text-align: center; }}
  .footer a {{ color: #555; margin: 0 6px; }}
  .btn {{ padding: 4px 14px; border: 1px solid #444; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; background: #222; color: #ddd; }}
  .btn:hover {{ background: #333; }}
  .btn-green {{ border-color: #76b900; color: #76b900; }}
  .btn-green:hover {{ background: #1a3a00; }}
  .btn-red {{ border-color: #ff4444; color: #ff4444; }}
  .btn-red:hover {{ background: #1a0a0a; }}
</style>
</head>
<body>

<div class="header">
  <h1>nvmon agent</h1>
  <div class="header-right">{host_title} &middot; auto-refresh 5s &middot; v{__version__}</div>
</div>

<div class="meta">
  <span>Profile: <b>{_esc(profile)}</b></span>
  <span>Uptime: {uptime_str}</span>
  <span>Polls: {poll}</span>
  <span>Updated: {_esc(timestamp[:19].replace('T', ' '))}</span>
</div>

{cap_html}

{alerts_html}

<div class="grid">

  <div class="card">
    <div class="card-title">DirectDisplay <span class="badge {'on' if dd_active else 'off'}">{'ACTIVE' if dd_active else 'OFF'}</span></div>
    <div class="kv"><span class="label">Managed Displays</span><span class="val">{len(managed)}</span></div>
    <div class="kv"><span class="label">All Same Refresh</span><span class="val {'ok' if oh.get('all_same_refresh') else 'warn'}">{_yn(bool(oh.get('all_same_refresh', False)))}</span></div>
    <div class="kv"><span class="label">Refresh Rate</span><span class="val">{(oh.get('refresh_rate_hz', 0) or 0):.3f}Hz</span></div>
    {excluded_note}
  </div>

  <div class="card">
    <div class="card-title">Mosaic <span class="badge {'on' if mos_on else 'off'}">{'ON' if mos_on else 'OFF'}</span></div>
    <div class="kv"><span class="label">Grid</span><span class="val">{mos.get('rows', 0)}x{mos.get('cols', 0)}</span></div>
    <div class="kv"><span class="label">Per Display</span><span class="val">{mos_per[0]}x{mos_per[1]}</span></div>
    <div class="kv"><span class="label">Total</span><span class="val">{mos_total[0]}x{mos_total[1]}</span></div>
    <div class="kv"><span class="label">Refresh Rate</span><span class="val">{mos.get('refresh_rate', 0) or 0}Hz</span></div>
  </div>

  <div class="card">
    <div class="card-title">QSync <span class="badge {'on' if qs_synced else 'off'}">{'LOCKED' if qs_synced else 'UNLOCKED'}</span></div>
    <div class="kv"><span class="label">Available</span><span class="val {'ok' if qs.get('available') else 'crit'}">{_yn(bool(qs.get('available', False)))}</span></div>
    <div class="kv"><span class="label">House Sync</span><span class="val {'ok' if qs_house else 'crit'}">{_yn(qs_house)} {'@ ' + format(qs.get('house_sync_incoming_hz', 0) or 0, '.3f') + 'Hz' if qs.get('house_sync_incoming_hz') else ''}</span></div>
    <div class="kv"><span class="label">Source</span><span class="val">{_esc(qs.get('source', '?'))}</span></div>
    <div class="kv"><span class="label">Trigger</span><span class="val">{_esc(qs.get('trigger', '?'))}</span></div>
    {'<table style="margin-top:8px"><tr><th>GPU</th><th>Synced</th><th>Signal</th></tr>' + gpu_sync_rows + '</table>' if gpu_sync_rows else ''}
    <div style="margin-top:10px; display:flex; gap:8px;">
      <button class="btn btn-green" onclick="qsyncAction('enable')">Enable QSync</button>
      <button class="btn btn-red" onclick="qsyncAction('disable')">Disable QSync</button>
      <span id="qsync-feedback" style="font-size:12px; line-height:28px; color:#888;"></span>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Output Health</div>
    <div class="kv"><span class="label">Total Displays</span><span class="val">{oh.get('total_displays', 0)}</span></div>
    <div class="kv"><span class="label">DD Active</span><span class="val">{oh.get('dd_active_count', 0)} / {oh.get('dd_expected_count', 4)}</span></div>
    <div class="kv"><span class="label">Same Refresh</span><span class="val {'ok' if oh.get('all_same_refresh') else 'warn'}">{_yn(bool(oh.get('all_same_refresh', False)))}</span></div>
    <div class="kv"><span class="label">Same Resolution</span><span class="val {'ok' if oh.get('all_same_resolution') else 'warn'}">{_yn(bool(oh.get('all_same_resolution', False)))}</span></div>
  </div>

</div>

<h2>Displays</h2>
<div class="card wide" style="margin-bottom:15px">
<table>
  <tr><th>Physical</th><th>NVIDIA</th><th>NVAPI ID</th><th>Win Target</th><th>Resolution</th><th>Refresh Rate</th><th>Status</th></tr>
  {dd_rows or '<tr><td colspan="7" class="note">No DD displays detected</td></tr>'}
</table>
</div>

<h2>GPUs</h2>
<div class="card wide" style="margin-bottom:15px">
<table>
  <tr><th>Index</th><th>Name</th><th>Temp</th><th>Power</th><th>GPU Load</th><th>Memory</th><th>PState</th></tr>
  {gpu_rows or '<tr><td colspan="7" class="note">No GPU data</td></tr>'}
</table>
</div>

{('<h2>Validation</h2><div class="card wide" style="margin-bottom:15px"><table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>' + val_rows + '</table></div>') if val_rows else ''}

<div class="footer">
  nvmon v{__version__}
  <a href="/status">status</a>
  <a href="/health">health</a>
  <a href="/validate">validate</a>
  <a href="/profiles">profiles</a>
</div>

<script>
function qsyncAction(action) {{
  var fb = document.getElementById('qsync-feedback');
  fb.textContent = (action === 'enable' ? 'Enabling...' : 'Disabling...');
  var url = '/qsync/' + action;
  var opts = {{method: 'POST', headers: {{'Content-Type': 'application/json'}}}};
  if (action === 'enable') {{
    opts.body = JSON.stringify({{source: 'HOUSESYNC', trigger: 'FALLING', grid: 1}});
  }}
  fetch(url, opts)
    .then(function(r) {{ return r.json().then(function(d) {{ return {{ ok: r.ok, data: d }}; }}); }})
    .then(function(res) {{
      if (res.ok && res.data.success) {{ fb.textContent = 'OK'; }}
      else {{ fb.textContent = 'Failed: ' + (res.data.error || res.data.stderr || 'unknown'); }}
      setTimeout(function() {{ location.reload(); }}, 2000);
    }})
    .catch(function(e) {{ fb.textContent = 'Error: ' + e; }});
}}
</script>

</body>
</html>"""


def _yn(val: bool) -> str:
    return "YES" if val else "NO"


def _pass(val: bool) -> str:
    return "PASS" if val else "FAIL"


def _esc(s) -> str:
    """Minimal HTML escape. Safe for attribute and text contexts we use."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
