"""Boot-apply sync retry (site 2026-11-04 cold boot: whole rig powered on at
once, colour/timing landed, the single sync attempt lost the race with the
daisy chain coming up)."""

from __future__ import annotations

from nvmon import executors
from nvmon.agent import monitor, qsync
from nvmon.types import GPUSyncInfo, Profile, ProfileQSync, QSyncState


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(m)


class _Profiles:
    def __init__(self, role="client"):
        self.p = Profile(name="60 - 12b - SyncClient", qsync=ProfileQSync(enabled=True, role=role))

    def load_active(self):
        return (self.p, None)


def test_retries_until_client_locks(monkeypatch):
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    states = iter([QSyncState(), QSyncState(),                                    # attempt 1: before, after enable
                   QSyncState(), QSyncState(per_gpu_sync=[GPUSyncInfo(gpu=0, synced=True)])])
    enables = []
    monkeypatch.setattr(qsync, "status", lambda: next(states))
    monkeypatch.setattr(qsync, "enable_from_profile", lambda p: enables.append(p.name))
    ok, msg = monitor._boot_sync_retries(_Profiles(), _Log(), "applied at 60Hz but QSync not locked")
    assert ok and "boot retry 2" in msg
    assert len(enables) == 2


def test_gives_up_after_retries_with_server_hint(monkeypatch):
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(qsync, "status", lambda: QSyncState())
    monkeypatch.setattr(qsync, "enable_from_profile", lambda p: None)
    log = _Log()
    ok, msg = monitor._boot_sync_retries(_Profiles("server"), log, "QSync not locked")
    assert not ok
    assert f"after {monitor.BOOT_SYNC_RETRIES} boot retries" in msg and "restart Windows" in msg
    assert len(log.lines) == monitor.BOOT_SYNC_RETRIES


def test_chain_membership_only_for_server_or_client_profiles(monkeypatch):
    from nvmon import config
    asked: list[str] = []

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen", lambda url, timeout=5: asked.append(url) or _Resp(
        b'{"in_chain": true, "group": "Ceiling", "role": "client", "server": "N5"}'))
    monkeypatch.setattr(config, "get_agent_config", lambda: {"controller_url": "http://ctl:9997"})
    monkeypatch.setattr(config, "get_hostname", lambda: "N6")

    p = _Profiles("client")
    assert monitor._chain_membership(p, _Log())["group"] == "Ceiling"
    assert asked and asked[0].endswith("/sync_chain?host=N6")

    off = _Profiles("client")
    off.p.qsync.enabled = False
    asked.clear()
    assert monitor._chain_membership(off, _Log()) is None and asked == []

    monkeypatch.setattr(config, "get_agent_config", lambda: {"controller_url": ""})
    assert monitor._chain_membership(_Profiles("server"), _Log()) is None


def test_already_locked_by_the_time_of_retry(monkeypatch):
    monkeypatch.setattr(executors, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(qsync, "status", lambda: QSyncState(synced=True))
    monkeypatch.setattr(qsync, "enable_from_profile", lambda p: (_ for _ in ()).throw(AssertionError("no enable needed")))
    ok, msg = monitor._boot_sync_retries(_Profiles("server"), _Log(), "QSync not locked")
    assert ok and "boot retry 1" in msg
