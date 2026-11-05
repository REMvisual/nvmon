"""Jinja2 environment + render helpers for the unified dashboard.

Module-level cache for the Environment so we pay startup cost once.

Templates live next to this file under ``templates/``; the layout file
already wires up ``static_url`` for asset paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import TEMPLATES_DIR


_env: Environment | None = None


def get_env() -> Environment:
    """Return the cached Jinja2 environment (built lazily)."""
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "htm"]),
            trim_blocks=False,
            lstrip_blocks=False,
        )
        # Convenience filter for templates that want to embed JSON in script
        # tags. ``| tojson`` already exists in Jinja but is escaped — this is
        # an unescaped variant for ``<script type="application/json">``.
        _env.filters.setdefault("rawjson", lambda v: json.dumps(v))
    return _env


def render(template_name: str, /, **context: Any) -> str:
    """Render a template by name with the given context."""
    tmpl = get_env().get_template(template_name)
    # static_url is referenced by every page that extends layout.html.
    context.setdefault("static_url", "/static")
    return tmpl.render(**context)


def render_dashboard(ctx: dict[str, Any]) -> str:
    return render("dashboard.html", **ctx)


def render_node_detail(node: dict[str, Any], static_url: str = "/static") -> str:
    return render("node_detail.html", node=node, static_url=static_url)


def render_login(*, error: str = "", next_path: str = "/") -> str:
    return render(
        "login.html",
        error=error,
        next=next_path,
        controller={
            "version": "0.1.0",
            "nodes_online": 0,
            "nodes_offline": 0,
            "alerts_count": 0,
        },
    )


def render_fanout_modal(
    *,
    action: str,
    scope: list[str],
    skipped: list[dict[str, Any]],
    profiles: list[str],
) -> str:
    return render(
        "fanout_modal.html",
        action=action,
        scope=scope,
        skipped=skipped,
        profiles=profiles,
    )


__all__ = [
    "get_env",
    "render",
    "render_dashboard",
    "render_node_detail",
    "render_login",
    "render_fanout_modal",
]
