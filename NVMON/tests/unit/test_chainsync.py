"""Chain sync (v0.9.9): the controller starts frame-lock chains server-first
after reboots — and ONLY for groups that are a chain (one server + clients)."""

from __future__ import annotations

from nvmon.controller import chainsync
from nvmon.controller.chainsync import ChainSync, find_chains, is_locked


# --- chain detection (the operator's scope rule) -------------------------------


def test_only_one_server_plus_clients_is_a_chain():
    groups = {
        "Wall": ["N1", "N2", "N3", "N4"],
        "Ceiling": ["N5", "N6", "N7"],
        "Standalone": ["N8"],            # a server alone: not a chain
        "NoSync": ["N9", "N10"],         # sync off
        "TwoServers": ["N11", "N12", "N13"],
        "ClientsOnly": ["N14", "N15"],
    }
    roles = {"N1": "server", "N2": "client", "N3": "client", "N4": "client",
             "N5": "server", "N6": "client", "N7": "client",
             "N8": "server", "N9": "none", "N10": "none",
             "N11": "server", "N12": "server", "N13": "client",
             "N14": "client", "N15": "client"}
    ch = find_chains(groups, roles)
    assert set(ch) == {"Wall", "Ceiling"}
    assert ch["Wall"] == {"group": "Wall", "server": "N1", "clients": ["N2", "N3", "N4"]}
    assert ch["Ceiling"]["server"] == "N5"


def test_is_locked_matches_agent_criterion():
    assert is_locked({"synced": True}, "server")
    assert not is_locked({"synced": False, "per_gpu_sync": [{"synced": True}]}, "server")
    assert is_locked({"synced": False, "per_gpu_sync": [{"synced": True}]}, "client")
    assert not is_locked(None, "client")


# --- fakes -----------------------------------------------------------------------


class _Node:
    def __init__(self, h, g):
        self.hostname, self.group = h, g


class _Registry:
    def __init__(self, groups):
        self.groups = groups

    def group_names(self):
        return list(self.groups)

    def get_by_group(self, g):
        return [_Node(h, g) for h in self.groups[g]]


class _Agg:
    def __init__(self, status):
        self.status = status

    def snapshot(self):
        return {"nodes": [{"hostname": h, "url": f"http://{h}", "status": s} for h, s in self.status.items()]}

    def node_snapshot(self, h):
        return {"hostname": h, "url": f"http://{h}", "status": self.status.get(h)}


def _cs(monkeypatch, status, roles, http):
    reg = _Registry({"Ceiling": ["N5", "N6", "N7"]})
    c = ChainSync(reg, _Agg(status), None, interval_s=2, min_gap_s=0)
    c._roles = {h: {"profile": f"p-{h}", "role": r} for h, r in roles.items()}
    monkeypatch.setattr(chainsync, "_http_json", http)
    monkeypatch.setattr(chainsync, "POLL_EVERY_S", 0)
    monkeypatch.setattr(chainsync.time, "sleep", lambda s: None)
    return c


_ROLES = {"N5": "server", "N6": "client", "N7": "client"}


def test_chain_for_is_case_insensitive_and_reports_role(monkeypatch):
    c = _cs(monkeypatch, {}, _ROLES, lambda *a, **k: (200, {}))
    info = c.chain_for("n6")
    assert info["role"] == "client" and info["server"] == "N5" and info["group"] == "Ceiling"
    assert c.chain_for("N99") is None


def test_run_chain_enables_server_first_and_clients_only_after_server_locks(monkeypatch):
    calls: list[str] = []
    locked = {"N5": False, "N6": False, "N7": False}

    def http(url, *, method="GET", body=None, timeout=15.0):
        host = url.split("//")[1].split("/")[0]
        if url.endswith("/qsync/enable_from_profile"):
            calls.append(f"enable {host}")
            if host != "N5":
                assert locked["N5"], "client enabled before the server locked"
            locked[host] = True
            return 202, {}
        if url.endswith("/qsync"):
            return 200, {"synced": locked[host], "per_gpu_sync": [{"synced": locked[host]}]}
        return 200, {}

    c = _cs(monkeypatch, {}, _ROLES, http)
    res = c.run_chain("Ceiling", {"server": "N5", "clients": ["N6", "N7"]},
                      {h: {"url": f"http://{h}"} for h in ("N5", "N6", "N7")})
    assert res["ok"] and res["server_locked"]
    assert calls[0] == "enable N5" and sorted(calls[1:]) == ["enable N6", "enable N7"]


def test_wedged_server_stops_and_names_the_node(monkeypatch):
    calls: list[str] = []

    def http(url, *, method="GET", body=None, timeout=15.0):
        if url.endswith("/qsync/enable_from_profile"):
            calls.append(url)
            return 202, {}
        if url.endswith("/qsync"):
            return 200, {"synced": False}
        return 200, {}

    c = _cs(monkeypatch, {}, _ROLES, http)
    monkeypatch.setattr(chainsync, "SERVER_LOCK_TIMEOUT_S", 0.01)
    res = c.run_chain("Ceiling", {"server": "N5", "clients": ["N6", "N7"]},
                      {h: {"url": f"http://{h}"} for h in ("N5", "N6", "N7")})
    assert not res["ok"] and not res["server_locked"]
    assert "restart Windows on N5" in res["detail"]
    assert len(calls) == 1                       # clients never touched


def test_tick_waits_for_every_member_and_needs_a_pending_member(monkeypatch):
    started: list[str] = []
    up = {"active_profile": "p", "chain_sync": {"pending": False}}
    pend = {"active_profile": "p", "chain_sync": {"pending": True}}

    def http(url, **k):
        return (200, {"in_progress": False}) if url.endswith("/apply/status") else (200, {})

    # nobody pending -> nothing
    c = _cs(monkeypatch, {"N5": up, "N6": up, "N7": up}, _ROLES, http)
    monkeypatch.setattr(c, "refresh_roles", lambda: None)
    monkeypatch.setattr(c, "_run_chain", lambda g, ch, s: started.append(g))
    monkeypatch.setattr(chainsync.threading, "Thread",
                        lambda target, args, **k: type("T", (), {"start": lambda self: target(*args)})())
    c.tick()
    assert started == []
    # pending but N7 still offline -> wait
    c._aggregator.status = {"N5": up, "N6": pend, "N7": None}
    c.tick()
    assert started == []
    # all up, one pending -> run once
    c._aggregator.status = {"N5": up, "N6": pend, "N7": pend}
    c.tick()
    assert started == ["Ceiling"]


def test_non_chain_group_is_never_driven(monkeypatch):
    started: list[str] = []
    reg = _Registry({"Solo": ["N8"]})
    c = ChainSync(reg, _Agg({"N8": {"active_profile": "p", "chain_sync": {"pending": True}}}), None, min_gap_s=0)
    c._roles = {"N8": {"profile": "p", "role": "server"}}
    monkeypatch.setattr(c, "refresh_roles", lambda: None)
    monkeypatch.setattr(c, "_run_chain", lambda g, ch, s: started.append(g))
    c.tick()
    assert started == [] and c.chain_for("N8") is None
