"""nvmon.controller.selfrestart — POST /system/restart_monitor backend.

The controller must never actually be bounced from a unit test: subprocess.Popen
is replaced and only the spawn arguments are checked.
"""

from __future__ import annotations

from pathlib import Path

from nvmon import config
from nvmon.controller import selfrestart


def _tree(tmp_path: Path, *, helper: bool = True, run: bool = True, kill: bool = True) -> Path:
    (tmp_path / "bats").mkdir()
    if helper:
        (tmp_path / "bats" / selfrestart.HELPER_NAME).write_text("@echo off\r\n", encoding="ascii")
    if run:
        (tmp_path / "run_monitor.bat").write_text("@echo off\r\n", encoding="ascii")
    if kill:
        (tmp_path / "kill_monitor.bat").write_text("@echo off\r\n", encoding="ascii")
    return tmp_path


def _capture_popen(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        calls.append((list(args), kwargs))
        return object()

    monkeypatch.setattr(selfrestart.subprocess, "Popen", fake_popen)
    return calls


def test_spawns_detached_helper_with_delay_and_browser_flag(monkeypatch, tmp_path):
    base = _tree(tmp_path)
    monkeypatch.setattr(config, "BASE_DIR", base)
    calls = _capture_popen(monkeypatch)

    r = selfrestart.restart_monitor(3)
    assert r["ok"] is True and r["action"] == "restart_monitor" and r["delay_seconds"] == 3
    assert len(calls) == 1
    args, kw = calls[0]
    assert args == ["cmd.exe", "/c", str(base / "bats" / selfrestart.HELPER_NAME), "3", "nobrowser"]
    assert kw["cwd"] == str(base)
    assert kw["close_fds"] is True
    if "creationflags" in kw:   # Windows only
        assert kw["creationflags"] & selfrestart._DETACHED_PROCESS

    r2 = selfrestart.restart_monitor(0, open_browser=True)
    assert r2["ok"] and calls[1][0][-2:] == ["0", "browser"]


def test_bad_or_negative_delay_is_clamped(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", _tree(tmp_path))
    calls = _capture_popen(monkeypatch)
    assert selfrestart.restart_monitor(-5)["delay_seconds"] == 0
    assert selfrestart.restart_monitor("nope")["delay_seconds"] == selfrestart.DEFAULT_DELAY_SECONDS  # type: ignore[arg-type]
    assert [c[0][3] for c in calls] == ["0", str(selfrestart.DEFAULT_DELAY_SECONDS)]


def test_missing_files_refuse_without_spawning(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", _tree(tmp_path, helper=False, kill=False))
    calls = _capture_popen(monkeypatch)
    r = selfrestart.restart_monitor()
    assert r["ok"] is False
    assert selfrestart.HELPER_NAME in r["error"] and "kill_monitor.bat" in r["error"]
    assert calls == []


def test_spawn_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", _tree(tmp_path))

    def boom(*a, **k):
        raise OSError("no cmd.exe")

    monkeypatch.setattr(selfrestart.subprocess, "Popen", boom)
    r = selfrestart.restart_monitor()
    assert r["ok"] is False and "no cmd.exe" in r["error"]
