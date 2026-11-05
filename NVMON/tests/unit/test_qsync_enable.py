"""Tests for nvmon.agent.qsync.enable() — argument construction.

The bug these pin: the previous default was `grid=1`, which referenced a
Mosaic grid that doesn't exist on non-Mosaic rigs. configureQsync2.exe
silently exited 0 without engaging genlock — the dashboard reported
"command accepted" while NVIDIA Control Panel showed nothing changed.

The new default discovers all connected displays via queryAll and passes
`display=G,D` for each — the first becomes the server, rest become clients.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nvmon import executors
from nvmon.agent import qsync
from nvmon.types import CommandResult


def _captured_args() -> tuple[list[list[str]], object]:
    """Patch executors.run + config.get_qsync_exe so enable() runs without
    touching the real executable. Returns (captured_arg_lists, patcher_ctx).
    """
    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        # The first call from enable() may be qsync-discover-displays,
        # the second is qsync-enable. Return queryAll output for the
        # discover call; an empty success for the enable call.
        if kw.get("label") == "qsync-discover-displays":
            return CommandResult(
                success=True, return_code=0,
                stdout=_FAKE_QUERYALL, stderr="",
            )
        return CommandResult(success=True, return_code=0, stdout="", stderr="")

    return captured, fake_run


_FAKE_QUERYALL = """\
QSync topology:
\tQSync Target #0(GPU) NVIDIA RTX A6000:
\t\tGPU index             : 0
\t\tDisplay 0:
\t\t\tDisplay index  : 0
\t\tDisplay 1:
\t\t\tDisplay index  : 1
\tQSync Target #1(GPU) NVIDIA RTX A6000:
\t\tGPU index             : 1
\t\tDisplay 0:
\t\t\tDisplay index  : 0
"""


def test_enable_default_path_uses_display_args_not_grid():
    """Default invocation (Sync All button posts an empty body) must build
    `display=G,D` args from queryAll discovery, NOT pass `grid=1`."""
    captured, fake_run = _captured_args()
    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        result = qsync.enable(wait=False)

    assert result.success is True
    # Two calls: discover then enable.
    assert len(captured) == 2
    enable_args = captured[1]
    assert enable_args[0:2] == ["enable", "server"]
    # Every connected display is in the args.
    assert "display=0,0" in enable_args
    assert "display=0,1" in enable_args
    assert "display=1,0" in enable_args
    # Source/trigger present.
    assert "source=HOUSESYNC" in enable_args
    assert "trigger=FALLING" in enable_args
    # No legacy grid arg.
    assert not any(a.startswith("grid=") for a in enable_args)


def test_enable_explicit_displays_overrides_discovery():
    """If the caller passes `displays=[(0,0)]`, discovery is skipped and
    only that display is in the args. Used by the future manual-select UI."""
    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        return CommandResult(success=True, return_code=0, stdout="", stderr="")

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        qsync.enable(displays=[(0, 0)], wait=False)

    # Only one call to executors.run — discovery did not fire.
    assert len(captured) == 1
    enable_args = captured[0]
    assert "display=0,0" in enable_args
    assert "display=0,1" not in enable_args
    assert not any(a.startswith("grid=") for a in enable_args)


def test_enable_explicit_grid_uses_grid_arg():
    """An operator who explicitly opts into Mosaic-grid sync (grid >= 0)
    still gets `grid=N` — discovery is skipped. Reserved for Mosaic setups."""
    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        return CommandResult(success=True, return_code=0, stdout="", stderr="")

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        qsync.enable(grid=0, wait=False)

    assert len(captured) == 1
    enable_args = captured[0]
    assert "grid=0" in enable_args
    assert not any(a.startswith("display=") for a in enable_args)


def test_enable_discovery_failure_falls_back_to_cli_default():
    """When queryAll yields no parseable displays, the args contain neither
    `display=` nor `grid=` — configureQsync2 then defaults to display=0,0.
    """
    captured: list[list[str]] = []

    def fake_run(exe, args, **kw):
        captured.append(list(args))
        if kw.get("label") == "qsync-discover-displays":
            # Garbage output that yields zero (gpu, display) pairs.
            return CommandResult(success=True, return_code=0, stdout="garbage", stderr="")
        return CommandResult(success=True, return_code=0, stdout="", stderr="")

    with patch.object(qsync.config, "get_qsync_exe", return_value="qsync.exe"), \
         patch.object(executors, "run", side_effect=fake_run):
        qsync.enable(wait=False)

    enable_args = captured[1]
    assert not any(a.startswith("display=") for a in enable_args)
    assert not any(a.startswith("grid=") for a in enable_args)
    # Source/trigger still present so the CLI knows it's a HOUSESYNC server.
    assert "source=HOUSESYNC" in enable_args
