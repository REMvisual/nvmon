"""Minimal controller landing page — placeholder for D's unified dashboard.

Shows one table of known nodes with a status badge, plus a short message
directing operators to the JSON APIs. Deliberately simple; sub-project D
replaces this with the full dashboard blueprint.
"""

from __future__ import annotations

import html
from typing import Any


def render(snapshot: dict[str, Any], *, auth_enabled: bool) -> str:
    """Render the landing page HTML from an aggregator snapshot."""
    nodes = snapshot.get("nodes") or []
    last_update = snapshot.get("last_update") or 0.0

    rows: list[str] = []
    for n in nodes:
        hostname = html.escape(str(n.get("hostname", "?")))
        url = html.escape(str(n.get("url", "")))
        reachable = bool(n.get("reachable"))
        caps = n.get("capabilities") or {}
        cap_bits = []
        for name in ("gpu", "displays", "mosaic", "qsync", "edid"):
            entry = caps.get(name) if isinstance(caps, dict) else None
            avail = isinstance(entry, dict) and entry.get("available") is True
            cap_bits.append(
                f'<span class="cap {"on" if avail else "off"}">{html.escape(name)}</span>'
            )
        health = n.get("health") or {}
        version = html.escape(str(health.get("version", "?"))) if isinstance(health, dict) else "?"
        badge_class = "ok" if reachable else "off"
        badge_text = "ONLINE" if reachable else "OFFLINE"
        rows.append(
            f"<tr>"
            f'<td><strong>{hostname}</strong></td>'
            f'<td><code>{url}</code></td>'
            f'<td><span class="badge {badge_class}">{badge_text}</span></td>'
            f'<td>{version}</td>'
            f'<td class="caps">{"".join(cap_bits)}</td>'
            f"</tr>"
        )
    rows_html = "\n".join(rows) if rows else (
        '<tr><td colspan="5" class="empty">No nodes configured — add entries to nvmon.toml</td></tr>'
    )
    auth_line = (
        "Authentication: ENABLED (cookie/header)"
        if auth_enabled
        else "Authentication: DISABLED (no password file)"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>nvmon controller</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #111; color: #ddd; margin: 0; padding: 24px; }}
  h1 {{ margin: 0 0 4px 0; font-size: 20px; }}
  .sub {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
  .note {{ background: #222; border-left: 3px solid #5af; padding: 10px 14px; margin: 16px 0; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #333; vertical-align: middle; }}
  th {{ color: #888; font-weight: 500; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em; }}
  code {{ font-family: Consolas, monospace; color: #9cf; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; }}
  .badge.ok {{ background: #1a3d1a; color: #7fd97f; }}
  .badge.off {{ background: #3d1a1a; color: #d97f7f; }}
  .cap {{ display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 10px; margin-right: 4px; }}
  .cap.on {{ background: #1a3d1a; color: #7fd97f; }}
  .cap.off {{ background: #2a2a2a; color: #666; text-decoration: line-through; }}
  .empty {{ color: #666; text-align: center; padding: 24px; }}
  .footer {{ margin-top: 24px; color: #666; font-size: 11px; }}
</style>
</head>
<body>
<h1>nvmon controller</h1>
<div class="sub">{len(nodes)} node(s) configured &middot; last update: {last_update:.0f} &middot; {html.escape(auth_line)}</div>
<div class="note">
  Unified dashboard coming &mdash; see <code>/api/status</code> for JSON,
  <code>/nodes</code> for the raw node list, and <code>/node/&lt;hostname&gt;/status</code> for a single node.
</div>
<table>
  <thead>
    <tr><th>Hostname</th><th>URL</th><th>Status</th><th>Version</th><th>Capabilities</th></tr>
  </thead>
  <tbody>
{rows_html}
  </tbody>
</table>
<div class="footer">nvmon controller &middot; placeholder landing &middot; GET /health for diagnostics</div>
</body>
</html>
"""


def render_login(*, error: str | None = None, next_path: str = "/") -> str:
    """Tiny login form served when the controller has auth enabled."""
    err_html = (
        f'<div class="err">{html.escape(error)}</div>' if error else ""
    )
    next_attr = html.escape(next_path or "/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>nvmon &mdash; sign in</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #111; color: #ddd; margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center; }}
  form {{ background: #1a1a1a; padding: 28px 32px; border-radius: 6px; min-width: 280px; }}
  h1 {{ margin: 0 0 16px 0; font-size: 16px; letter-spacing: 0.05em; text-transform: uppercase; color: #888; }}
  input[type=password] {{ width: 100%; padding: 10px; border: 1px solid #333; background: #111; color: #ddd; border-radius: 3px; font-size: 14px; box-sizing: border-box; }}
  button {{ margin-top: 12px; width: 100%; padding: 10px; background: #2a5; color: #fff; border: none; border-radius: 3px; cursor: pointer; font-size: 13px; font-weight: 600; }}
  button:hover {{ background: #3c6; }}
  .err {{ background: #3d1a1a; color: #d97f7f; padding: 8px 12px; border-radius: 3px; margin-bottom: 12px; font-size: 12px; }}
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>nvmon controller</h1>
  {err_html}
  <input type="password" name="password" autofocus required placeholder="Password">
  <input type="hidden" name="next" value="{next_attr}">
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""
