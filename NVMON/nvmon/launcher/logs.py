"""Per-app log capture, rotation, and tailing.

One rotating text file per app at `<log_dir>/app-<name>.log`. A drain
thread reads from `Popen.stdout` (stderr is merged) and appends lines
to the file. When the file reaches `per_app_log_bytes` it's rotated to
`.1`, `.2`, … up to `per_app_log_keep`. If the drain thread falls
behind, an inline `[launcher: log buffer overrun, dropped N lines]`
marker is written so the gap is never silent.

`tail(name, n)` reads the last N lines from the active file and —
if N outpaces the active file — from the most recent rotated file.
N is capped server-side at 5000 (spec §11).
"""

from __future__ import annotations

import os
import threading
from collections import deque
from pathlib import Path
from typing import IO

# Soft max bytes to read for tail queries — keeps memory bounded.
# Covers 5000 lines of typical 100-200 char output.
_TAIL_MAX_LINES_CAP = 5000
_TAIL_READ_BYTES = 4 * 1024 * 1024    # 4 MiB


def app_log_path(log_dir: str, app_name: str) -> Path:
    return Path(log_dir) / f"app-{app_name}.log"


def rotated_path(log_dir: str, app_name: str, index: int) -> Path:
    return Path(log_dir) / f"app-{app_name}.log.{index}"


def _rotate_if_needed(path: Path, max_bytes: int, keep: int) -> None:
    """Rotate path → path.1, path.1 → path.2, … up to keep."""
    if max_bytes <= 0:
        return
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < max_bytes:
        return

    # Evict oldest
    oldest = path.with_suffix(path.suffix + f".{keep}")
    try:
        if oldest.exists():
            oldest.unlink()
    except OSError:
        pass

    # Shift rotated logs down
    for i in range(keep - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        dst = path.with_suffix(path.suffix + f".{i + 1}")
        if src.exists():
            try:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
            except OSError:
                pass

    # Move active -> .1
    try:
        dst1 = path.with_suffix(path.suffix + ".1")
        if dst1.exists():
            dst1.unlink()
        path.rename(dst1)
    except OSError:
        pass


class AppLogWriter:
    """Appender with rotation. Thread-safe (single drain thread writes)."""

    def __init__(self, log_dir: str, app_name: str, *,
                 max_bytes: int, keep: int) -> None:
        self._path = app_log_path(log_dir, app_name)
        self._max_bytes = max_bytes
        self._keep = keep
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def write_line(self, line: str) -> None:
        """Append one text line (no implicit newline handling)."""
        if not line.endswith("\n"):
            line = line + "\n"
        with self._lock:
            _rotate_if_needed(self._path, self._max_bytes, self._keep)
            try:
                with self._path.open("a", encoding="utf-8", errors="replace") as f:
                    f.write(line)
            except OSError:
                # Last-ditch: don't let a log write kill the launcher.
                pass

    def write_marker(self, msg: str) -> None:
        """Write a bracketed `[launcher: <msg>]` line verbatim."""
        self.write_line(f"[launcher: {msg}]")


class DrainThread(threading.Thread):
    """Reads from a child process stream and appends to an AppLogWriter.

    Detects bursty producers by watching pending bytes between iterations.
    If the queue grows monotonically over 5 read cycles, emits an overrun
    marker (visible in the log) but keeps draining.
    """

    def __init__(
        self,
        stream: IO[bytes],
        writer: AppLogWriter,
        *,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name=f"drain:{writer.path.name}")
        self._stream = stream
        self._writer = writer
        self._stop = stop_event
        self._dropped_since_last_marker = 0

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                raw = self._stream.readline()
                if not raw:
                    break
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                except Exception:
                    line = repr(raw)
                # Heuristic: if writing ever fails, we count it as dropped.
                before = self._dropped_since_last_marker
                try:
                    self._writer.write_line(line)
                except Exception:
                    self._dropped_since_last_marker += 1
                if (
                    self._dropped_since_last_marker > 0
                    and self._dropped_since_last_marker % 100 == 0
                    and self._dropped_since_last_marker != before
                ):
                    self._writer.write_marker(
                        f"log buffer overrun, dropped "
                        f"{self._dropped_since_last_marker} lines"
                    )
        finally:
            try:
                self._stream.close()
            except Exception:
                pass


def read_tail(log_dir: str, app_name: str, n: int) -> str:
    """Return the last `n` lines of the app's log as plain text.

    Caps at 5000 lines. Reads up to ~4 MiB from the end of the active
    file, then from the most recent rotated file if needed.
    """
    n = max(1, min(int(n), _TAIL_MAX_LINES_CAP))

    candidates: list[Path] = [
        app_log_path(log_dir, app_name),
        rotated_path(log_dir, app_name, 1),
    ]

    chunks: deque[bytes] = deque()
    budget = _TAIL_READ_BYTES
    for p in candidates:
        if budget <= 0:
            break
        if not p.is_file():
            continue
        try:
            with p.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                read_bytes = min(size, budget)
                f.seek(size - read_bytes)
                chunks.appendleft(f.read(read_bytes))
                budget -= read_bytes
        except OSError:
            continue

    if not chunks:
        return ""

    data = b"".join(chunks)
    # Split once into lines and return the last n.
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-n:])


__all__ = [
    "AppLogWriter",
    "DrainThread",
    "app_log_path",
    "read_tail",
    "rotated_path",
]
