"""Shared utilities used by both agent and controller.

Keep this module dependency-free (no imports from nvmon.config, no logging
import) so it can be imported from anywhere without circular trouble.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path | str, data: Any) -> None:
    """Write JSON atomically: write to .tmp on same drive, then os.replace.

    Reused for status.json (agent), registered_nodes.json (controller),
    and any other shared state where a torn read by another process
    would corrupt the consumer. The .tmp file is on the same drive as
    the destination so os.replace is a true atomic rename, not a copy.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = str(path) + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def disable_console_quick_edit() -> bool:
    """Turn off console QuickEdit (click-to-select) on this process' console.

    A click in a cmd window starts a selection and Windows then blocks every
    write to that console until Esc/Enter. Every INFO log line goes to the
    console, so the daemon freezes (status goes stale, POSTs hang while
    plain GETs keep answering). Children that inherit the console inherit
    the mode. Returns True when the mode was changed; False when there is
    no console (service / redirected) or not on Windows. Never raises.
    """
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except Exception:
        return False
    try:
        STD_INPUT_HANDLE = -10
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False
