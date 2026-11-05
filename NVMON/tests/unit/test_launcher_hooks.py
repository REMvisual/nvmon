"""Unit tests for `nvmon.launcher.hooks`.

Covers:
    - `delay` hook: waits for the configured duration, cancels cleanly.
    - `shell_command` hook: runs `cmd /c echo hi` on Windows, asserts ok.
    - `http_get_wait` hook: mocks HTTP so the test is fully offline.

The tests must run via `pytest` without any network access.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest import mock

import pytest

from nvmon.launcher import hooks as hooks_mod


# ---------------------------------------------------------------------------
# delay
# ---------------------------------------------------------------------------

def _base_context() -> hooks_mod.HookContext:
    return hooks_mod.HookContext(
        agent_base_url="http://127.0.0.1:9998",
        cancel_event=threading.Event(),
    )


def test_delay_hook_waits_and_reports_ok():
    cfg = hooks_mod.hook_from_dict({
        "type": "delay",
        "seconds": 0.05,
    })
    ctx = _base_context()
    t0 = time.monotonic()
    res = hooks_mod.run_hook(cfg, ctx)
    elapsed = time.monotonic() - t0
    assert res.ok is True
    assert res.cancelled is False
    # Allow a generous lower bound to avoid flaky timing on CI
    assert elapsed >= 0.04


def test_delay_hook_is_cancellable():
    cfg = hooks_mod.hook_from_dict({
        "type": "delay",
        "seconds": 10.0,
    })
    ctx = _base_context()
    # Fire the cancel event shortly after starting the hook.
    t = threading.Timer(0.05, ctx.cancel_event.set)
    t.start()
    t0 = time.monotonic()
    res = hooks_mod.run_hook(cfg, ctx)
    elapsed = time.monotonic() - t0
    t.cancel()
    assert res.ok is False
    assert res.cancelled is True
    assert elapsed < 1.0   # nowhere near the 10s timeout


# ---------------------------------------------------------------------------
# shell_command — uses `cmd /c echo hi` on Windows
# ---------------------------------------------------------------------------

def test_shell_command_runs_cmd_echo():
    cfg = hooks_mod.hook_from_dict({
        "type": "shell_command",
        "exe": r"C:\Windows\System32\cmd.exe",
        "args": ["/c", "echo", "hi"],
        "timeout_seconds": 5.0,
    })
    ctx = _base_context()
    res = hooks_mod.run_hook(cfg, ctx)
    assert res.ok is True, res.detail
    assert "exit=0" in res.detail


def test_shell_command_bad_exit_code_fails():
    # cmd /c exit 7 -> exit code 7, NOT in expected_exit_codes default [0]
    cfg = hooks_mod.hook_from_dict({
        "type": "shell_command",
        "exe": r"C:\Windows\System32\cmd.exe",
        "args": ["/c", "exit", "7"],
        "timeout_seconds": 5.0,
        "required": False,      # keep the sequence non-aborting for tests
    })
    ctx = _base_context()
    res = hooks_mod.run_hook(cfg, ctx)
    assert res.ok is False
    assert "exit=7" in res.detail


def test_shell_command_rejects_relative_path():
    cfg = hooks_mod.hook_from_dict({
        "type": "shell_command",
        "exe": "cmd.exe",    # not absolute
        "args": ["/c", "echo", "hi"],
        "timeout_seconds": 5.0,
    })
    ctx = _base_context()
    res = hooks_mod.run_hook(cfg, ctx)
    assert res.ok is False
    assert "absolute" in res.detail


# ---------------------------------------------------------------------------
# http_get_wait — mocked HTTP
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def test_http_get_wait_matches_expected_value():
    """Poll a fake endpoint that returns {"active_profile":"p1"} on the 2nd call."""
    cfg = hooks_mod.hook_from_dict({
        "type": "http_get_wait",
        "url": "http://example.invalid/status",
        "json_path": "active_profile",
        "expect": "p1",
        "poll_seconds": 0.05,
        "timeout_seconds": 2.0,
    })

    responses = [
        _FakeHTTPResponse(200, json.dumps({"active_profile": "none"}).encode()),
        _FakeHTTPResponse(200, json.dumps({"active_profile": "p1"}).encode()),
    ]

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return responses.pop(0)

    ctx = _base_context()
    with mock.patch(
        "nvmon.launcher.hooks.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        res = hooks_mod.run_hook(cfg, ctx)
    assert res.ok is True, res.detail
    assert "active_profile" in res.detail


def test_http_get_wait_times_out_when_value_never_arrives():
    cfg = hooks_mod.hook_from_dict({
        "type": "http_get_wait",
        "url": "http://example.invalid/status",
        "json_path": "active_profile",
        "expect": "p1",
        "poll_seconds": 0.02,
        "timeout_seconds": 0.2,
    })

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return _FakeHTTPResponse(
            200, json.dumps({"active_profile": "other"}).encode(),
        )

    ctx = _base_context()
    with mock.patch(
        "nvmon.launcher.hooks.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        res = hooks_mod.run_hook(cfg, ctx)
    assert res.ok is False
    assert res.timed_out is True


# ---------------------------------------------------------------------------
# HookConfig validation
# ---------------------------------------------------------------------------

def test_hook_from_dict_rejects_unknown_type():
    with pytest.raises(ValueError):
        hooks_mod.hook_from_dict({"type": "not_a_real_hook"})


def test_hook_from_dict_await_status_rejects_unknown_key():
    with pytest.raises(ValueError):
        hooks_mod.hook_from_dict({
            "type": "nvmon_await_status",
            "timeout_seconds": 5.0,
            "conditions": {"not_a_real_condition": True},
        })


def test_hook_types_has_all_thirteen():
    # Guard against accidental hook-type drift.
    assert len(hooks_mod.HOOK_TYPES) == 13
