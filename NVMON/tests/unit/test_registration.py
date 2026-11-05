"""Unit tests for nvmon.agent.registration.RegistrationClient.

Mocks `urllib.request.urlopen` at the module's import site so no socket
is ever opened. Verifies payload shape, retry/backoff lifecycle, and the
`controller_url=""` disabled path.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest import mock

import pytest

from nvmon.agent import registration


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _make_client(controller_url: str = "http://mon:9997", interval: int = 60) -> registration.RegistrationClient:
    return registration.RegistrationClient(
        controller_url=controller_url,
        hostname="NODE-04",
        agent_url="http://10.0.0.4:9998",
        machine_id="abc123",
        version="0.6.19",
        capabilities={"gpu": {"available": True}},
        interval_seconds=interval,
    )


def test_payload_shape() -> None:
    client = _make_client()
    payload = client._payload()
    assert payload["hostname"] == "NODE-04"
    assert payload["url"] == "http://10.0.0.4:9998"
    assert payload["machine_id"] == "abc123"
    assert payload["version"] == "0.6.19"
    assert payload["capabilities"] == {"gpu": {"available": True}}


def test_post_once_success() -> None:
    client = _make_client()
    with mock.patch.object(registration.urllib.request, "urlopen") as urlopen:
        urlopen.return_value = _FakeResponse(200)
        ok, err = client._post_once()
    assert ok is True
    assert err == ""
    # Check the request body was JSON-encoded payload.
    sent_req = urlopen.call_args[0][0]
    assert sent_req.full_url == "http://mon:9997/register"
    assert sent_req.method == "POST"
    body = json.loads(sent_req.data.decode("utf-8"))
    assert body["hostname"] == "NODE-04"


def test_post_once_http_error() -> None:
    client = _make_client()
    fake_err = registration.urllib.error.HTTPError(
        url="http://mon:9997/register",
        code=500,
        msg="server fell over",
        hdrs=None,
        fp=None,
    )
    with mock.patch.object(registration.urllib.request, "urlopen", side_effect=fake_err):
        ok, err = client._post_once()
    assert ok is False
    assert "500" in err


def test_post_once_connection_refused() -> None:
    client = _make_client()
    with mock.patch.object(
        registration.urllib.request,
        "urlopen",
        side_effect=registration.urllib.error.URLError("connection refused"),
    ):
        ok, err = client._post_once()
    assert ok is False
    assert "refused" in err.lower()


def test_disabled_when_controller_url_empty() -> None:
    """Empty controller_url -> start() does not spawn a thread."""
    client = _make_client(controller_url="")
    with mock.patch.object(registration.urllib.request, "urlopen") as urlopen:
        client.start()
        # Give it a moment to NOT do anything.
        time.sleep(0.05)
    assert client._thread is None
    urlopen.assert_not_called()


def test_start_stop_lifecycle() -> None:
    """start() spawns daemon thread, single success -> long sleep, stop() cleans up."""
    client = _make_client(interval=3600)
    with mock.patch.object(registration.urllib.request, "urlopen") as urlopen:
        urlopen.return_value = _FakeResponse(200)
        client.start()
        # Wait until the loop has registered at least once.
        for _ in range(100):
            if client._registered_once:
                break
            time.sleep(0.01)
        assert client._registered_once is True
        client.stop()
    assert client._thread is None or not client._thread.is_alive()


def test_backoff_doubles_until_first_success() -> None:
    """First few POSTs fail; client retries with doubling backoff, then succeeds."""
    client = _make_client(interval=3600)
    call_count = {"n": 0}

    def fake_urlopen(*_a: Any, **_kw: Any) -> _FakeResponse:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise registration.urllib.error.URLError("nope")
        return _FakeResponse(200)

    # Patch wait so we don't actually sleep through the backoff windows.
    with mock.patch.object(registration.urllib.request, "urlopen", side_effect=fake_urlopen), \
         mock.patch.object(threading.Event, "wait", return_value=False):
        client.start()
        for _ in range(200):
            if client._registered_once:
                break
            time.sleep(0.01)
        client.stop()

    assert call_count["n"] >= 3
    assert client._registered_once is True
