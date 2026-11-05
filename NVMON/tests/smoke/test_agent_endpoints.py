"""Smoke tests for the agent HTTP API.

These tests require a running agent. They skip unless the environment variable
NVMON_SMOKE_URL is set:

    # PowerShell
    $env:NVMON_SMOKE_URL = "http://localhost:9998"
    pytest tests/smoke/test_agent_endpoints.py -v

    # cmd
    set NVMON_SMOKE_URL=http://localhost:9998
    pytest tests/smoke/test_agent_endpoints.py -v

They never fail CI if the agent isn't running - they skip cleanly.
"""

from __future__ import annotations

import os

import pytest


pytest.importorskip("requests", reason="requests library required for smoke tests")
import requests  # noqa: E402


AGENT_URL_ENV = "NVMON_SMOKE_URL"


def _agent_url() -> str:
    url = os.environ.get(AGENT_URL_ENV)
    if not url:
        pytest.skip(
            f"{AGENT_URL_ENV} not set - skipping agent smoke test. "
            f'Set e.g. {AGENT_URL_ENV}="http://localhost:9998" to run.'
        )
    return url.rstrip("/")


def _get(path: str, *, timeout: float = 5.0) -> requests.Response:
    return requests.get(f"{_agent_url()}{path}", timeout=timeout)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_returns_200():
    r = _get("/health")
    assert r.status_code == 200


def test_health_shape():
    r = _get("/health")
    body = r.json()
    assert isinstance(body, dict)
    # Spec requires ok, version, hostname, ip, capabilities.
    assert "ok" in body
    assert "version" in body
    assert "hostname" in body
    assert "capabilities" in body
    caps = body["capabilities"]
    assert isinstance(caps, dict)
    for key in ("gpu", "displays", "mosaic", "qsync", "edid"):
        assert key in caps, f"capability {key} missing from /health"
        assert "available" in caps[key]
        assert isinstance(caps[key]["available"], bool)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def test_status_returns_200():
    r = _get("/status")
    assert r.status_code == 200


def test_status_shape():
    r = _get("/status")
    body = r.json()
    assert isinstance(body, dict)
    # Spec: status.json contains these top-level fields.
    assert "timestamp" in body
    assert "hostname" in body
    # direct_display / mosaic / qsync / gpu may be present even if empty
    for key in ("direct_display", "mosaic", "qsync", "gpu"):
        assert key in body, f"top-level key {key} missing from /status"


# ---------------------------------------------------------------------------
# /gpu
# ---------------------------------------------------------------------------

def test_gpu_endpoint():
    r = _get("/gpu")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        # Can be a list of GPUInfo or a dict wrapping one
        assert isinstance(body, (list, dict))


# ---------------------------------------------------------------------------
# /displays
# ---------------------------------------------------------------------------

def test_displays_endpoint():
    r = _get("/displays")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert isinstance(body, dict)
        assert "display_count" in body or "displays" in body


# ---------------------------------------------------------------------------
# /mosaic
# ---------------------------------------------------------------------------

def test_mosaic_endpoint():
    r = _get("/mosaic")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert isinstance(body, dict)
        assert "enabled" in body


# ---------------------------------------------------------------------------
# /qsync
# ---------------------------------------------------------------------------

def test_qsync_endpoint():
    r = _get("/qsync")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert isinstance(body, dict)
        assert "available" in body


# ---------------------------------------------------------------------------
# /profiles
# ---------------------------------------------------------------------------

def test_profiles_endpoint():
    r = _get("/profiles")
    assert r.status_code == 200
    body = r.json()
    # Could be {"profiles": [...]} or a bare list
    assert isinstance(body, (list, dict))
    if isinstance(body, dict):
        assert "profiles" in body
        assert isinstance(body["profiles"], list)
    else:
        assert all(isinstance(x, (str, dict)) for x in body)


# ---------------------------------------------------------------------------
# /validate
# ---------------------------------------------------------------------------

def test_validate_endpoint():
    r = _get("/validate")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert "all_passed" in body
    assert isinstance(body["all_passed"], bool)
    assert "checks" in body


# ---------------------------------------------------------------------------
# / (HTML dashboard)
# ---------------------------------------------------------------------------

def test_root_returns_html():
    r = _get("/")
    assert r.status_code == 200
    ctype = r.headers.get("Content-Type", "").lower()
    # Accept any HTML variant
    assert "html" in ctype or r.text.lstrip().lower().startswith("<!doctype")
