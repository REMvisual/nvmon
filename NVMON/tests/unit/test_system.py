"""Unit tests for `nvmon.agent.system` — the restart/shutdown/abort wrapper.

These tests do NOT actually call `shutdown.exe`; they patch `subprocess.run`
inside the system module and assert:
    1. The argv handed to `shutdown.exe` is exactly what we expect for each
       action (right flag, right delay, /f, /d p:0:0, comment).
    2. PowerResult.success and rc are mapped from the subprocess result.
    3. Non-int delay_seconds inputs are coerced rather than crashing.
    4. abort() calls shutdown.exe with `/a`.

This is the only safe way to exercise this code on a dev box — actually
firing /r or /s would reboot the developer's machine.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from nvmon.agent import system as system_mod


def _fake_completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["shutdown.exe"], returncode=rc, stdout=stdout, stderr=stderr,
    )


def test_restart_builds_correct_argv() -> None:
    with mock.patch.object(system_mod.subprocess, "run", return_value=_fake_completed()) as mock_run:
        result = system_mod.restart(delay_seconds=8)

    assert result.success is True
    assert result.action == "restart"
    assert result.delay_seconds == 8
    assert result.return_code == 0
    assert result.error == ""

    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    # Must use /r (restart), not /s (shutdown).
    assert argv[0] == "shutdown.exe"
    assert "/r" in argv
    assert "/s" not in argv
    # Delay applied as /t <int>.
    assert "/t" in argv
    assert argv[argv.index("/t") + 1] == "8"
    # Force-close apps and "planned, other" reason class.
    assert "/f" in argv
    assert "/d" in argv
    assert argv[argv.index("/d") + 1] == "p:0:0"
    # A human-readable comment is provided.
    assert "/c" in argv
    comment = argv[argv.index("/c") + 1]
    assert "nvmon" in comment.lower()
    assert "restart" in comment.lower()


def test_shutdown_builds_correct_argv() -> None:
    with mock.patch.object(system_mod.subprocess, "run", return_value=_fake_completed()) as mock_run:
        result = system_mod.shutdown(delay_seconds=15)

    assert result.success is True
    assert result.action == "shutdown"
    assert result.delay_seconds == 15

    argv = mock_run.call_args.args[0]
    assert "/s" in argv
    assert "/r" not in argv
    assert argv[argv.index("/t") + 1] == "15"
    assert "/f" in argv


def test_abort_builds_correct_argv() -> None:
    with mock.patch.object(system_mod.subprocess, "run", return_value=_fake_completed()) as mock_run:
        result = system_mod.abort()

    assert result.success is True
    assert result.action == "abort"
    assert result.delay_seconds == 0

    argv = mock_run.call_args.args[0]
    assert argv == ["shutdown.exe", "/a"]


def test_default_delay_is_used_when_absent() -> None:
    with mock.patch.object(system_mod.subprocess, "run", return_value=_fake_completed()) as mock_run:
        system_mod.restart()
    argv = mock_run.call_args.args[0]
    assert argv[argv.index("/t") + 1] == str(system_mod.DEFAULT_DELAY_SECONDS)


def test_negative_delay_clamped_to_zero() -> None:
    # max(0, int(...)) — operator passing -5 shouldn't get a negative arg.
    with mock.patch.object(system_mod.subprocess, "run", return_value=_fake_completed()) as mock_run:
        result = system_mod.restart(delay_seconds=-5)
    assert result.delay_seconds == 0
    argv = mock_run.call_args.args[0]
    assert argv[argv.index("/t") + 1] == "0"


def test_nonzero_rc_marks_failure_and_surfaces_stderr() -> None:
    with mock.patch.object(
        system_mod.subprocess,
        "run",
        return_value=_fake_completed(rc=5, stderr="Access is denied.(5)\r\n"),
    ):
        result = system_mod.restart()

    assert result.success is False
    assert result.return_code == 5
    assert "Access is denied" in result.error
    assert result.action == "restart"


def test_shutdown_exe_missing_is_a_clean_failure() -> None:
    with mock.patch.object(
        system_mod.subprocess,
        "run",
        side_effect=FileNotFoundError("[WinError 2] The system cannot find the file specified"),
    ):
        result = system_mod.shutdown()

    assert result.success is False
    assert result.return_code == -1
    assert "shutdown.exe not found" in result.error.lower() or "shutdown" in result.error.lower()


def test_shutdown_exe_timeout_is_a_clean_failure() -> None:
    with mock.patch.object(
        system_mod.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="shutdown.exe", timeout=10),
    ):
        result = system_mod.shutdown()

    assert result.success is False
    assert result.return_code == -1
    assert "10s" in result.error or "timeout" in result.error.lower() or "did not return" in result.error.lower()


@pytest.mark.parametrize("bad", ["abc", None, [1, 2], object()])
def test_invalid_delay_raises_typeerror(bad: object) -> None:
    # The agent's _coerce_delay() handler catches TypeError/ValueError, but
    # the underlying system.restart()/shutdown() should raise on garbage —
    # we want callers (and the agent layer) to coerce explicitly so we
    # never accidentally pass a string to /t and get a confusing rc=1.
    with mock.patch.object(system_mod.subprocess, "run", return_value=_fake_completed()):
        with pytest.raises((TypeError, ValueError)):
            system_mod.restart(delay_seconds=bad)  # type: ignore[arg-type]
