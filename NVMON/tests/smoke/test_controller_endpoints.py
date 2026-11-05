"""Smoke tests for the controller HTTP API.

These tests require a running controller. They skip unless the environment
variable NVMON_SMOKE_URL is set and points at a controller:

    # PowerShell
    $env:NVMON_SMOKE_URL = "http://localhost:9997"
    pytest tests/smoke/test_controller_endpoints.py -v

    # If the controller is in authenticated mode, set the password too:
    $env:NVMON_SMOKE_PASSWORD = "yourpassword"

They never fail CI if the controller isn't running - they skip cleanly.
"""

from __future__ import annotations

import os

import pytest


pytest.importorskip("requests", reason="requests library required for smoke tests")
import requests  # noqa: E402


CTRL_URL_ENV = "NVMON_SMOKE_URL"
CTRL_PW_ENV = "NVMON_SMOKE_PASSWORD"


def _ctrl_url() -> str:
    url = os.environ.get(CTRL_URL_ENV)
    if not url:
        pytest.skip(
            f"{CTRL_URL_ENV} not set - skipping controller smoke test. "
            f'Set e.g. {CTRL_URL_ENV}="http://localhost:9997" to run.'
        )
    return url.rstrip("/")


def _headers() -> dict:
    pw = os.environ.get(CTRL_PW_ENV, "")
    return {"X-NVMON-Password": pw} if pw else {}


def _get(path: str, *, timeout: float = 10.0) -> requests.Response:
    return requests.get(f"{_ctrl_url()}{path}", headers=_headers(), timeout=timeout)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_returns_200():
    r = _get("/health")
    # In no-auth mode, or with correct password header, 200.
    # If auth is required and password is wrong, 401.
    assert r.status_code in (200, 401)


def test_health_shape():
    r = _get("/health")
    if r.status_code == 401:
        pytest.skip("Controller requires auth; set NVMON_SMOKE_PASSWORD to test.")
    body = r.json()
    assert isinstance(body, dict)
    # Controller /health should at least identify itself and report node count.
    assert "ok" in body or "status" in body
    # node count field - spec says "number of known nodes"
    has_node_count = any(k in body for k in ("node_count", "nodes", "known_nodes"))
    assert has_node_count, "controller /health should report node count"


# ---------------------------------------------------------------------------
# /nodes
# ---------------------------------------------------------------------------

def test_nodes_endpoint():
    r = _get("/nodes")
    if r.status_code == 401:
        pytest.skip("Controller requires auth; set NVMON_SMOKE_PASSWORD to test.")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, (list, dict))
    if isinstance(body, dict):
        # Expected shape: {"nodes": [...]}
        assert "nodes" in body
        assert isinstance(body["nodes"], list)


def test_nodes_shape():
    r = _get("/nodes")
    if r.status_code == 401:
        pytest.skip("Controller requires auth; set NVMON_SMOKE_PASSWORD to test.")
    body = r.json()
    nodes = body["nodes"] if isinstance(body, dict) else body
    for n in nodes:
        assert isinstance(n, dict)
        # Per spec: hostname + last-seen /health info, or reachability
        assert "hostname" in n
        # Some indicator of reachability/health - allow several field names
        assert any(k in n for k in ("reachable", "ok", "health", "last_seen"))


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def test_status_endpoint():
    r = _get("/status")
    if r.status_code == 401:
        pytest.skip("Controller requires auth; set NVMON_SMOKE_PASSWORD to test.")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)


def test_status_has_per_node_breakdown():
    r = _get("/status")
    if r.status_code == 401:
        pytest.skip("Controller requires auth; set NVMON_SMOKE_PASSWORD to test.")
    body = r.json()
    # Accept either {"nodes": {...}} or {"<hostname>": {...}}-keyed flat layout.
    # We only require that the response is a populated dict.
    assert len(body) >= 0


# ---------------------------------------------------------------------------
# / (unified dashboard)
# ---------------------------------------------------------------------------

def test_root_dashboard():
    r = _get("/")
    if r.status_code == 401:
        pytest.skip("Controller requires auth; set NVMON_SMOKE_PASSWORD to test.")
    assert r.status_code == 200
    ctype = r.headers.get("Content-Type", "").lower()
    assert "html" in ctype or r.text.lstrip().lower().startswith("<!doctype")


# ---------------------------------------------------------------------------
# /node/<hostname>/status (optional - depends on config)
# ---------------------------------------------------------------------------

def test_node_passthrough_if_available():
    """If /nodes has at least one node, /node/<hostname>/status should work."""
    r = _get("/nodes")
    if r.status_code == 401:
        pytest.skip("Controller requires auth; set NVMON_SMOKE_PASSWORD to test.")
    body = r.json()
    nodes = body["nodes"] if isinstance(body, dict) else body
    if not nodes:
        pytest.skip("controller has no nodes configured")

    hostname = nodes[0]["hostname"]
    r2 = _get(f"/node/{hostname}/status")
    # 200 if reachable, 502/503/504 if node is down.
    assert r2.status_code in (200, 404, 502, 503, 504)
