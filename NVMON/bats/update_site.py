"""Update an existing nvmon site (monitor box and/or listeners) from a newer
NVMON copy - without touching the site's own data.

Run through ``update_site.bat`` in the NEW copy (e.g. a fresh clone):

    update_site.bat                      asks for the existing NVMON folder
    update_site.bat E:\\NVMON            update that folder
    update_site.bat E:\\NVMON --dry-run  only show what would happen

Modes (asked interactively, or ``--mode``):
  monitor            This box is the monitor. Update its code, add the
                     desktop app (nvmon app + floating sync widget) and its
                     shortcuts. Listeners are left exactly as they are.
  monitor+listeners  As above, then push the same code to every listener's
                     NVMON share and restart each listener.
  listener           This box is a render node: update it in place.

Profiles are checked (every one must load with the new code), never
rewritten.

What is never touched (the site's own state):
  nvmon.toml, secrets\\, profiles\\*.json (your profiles), Edids\\, logs\\,
  status.json, active_profile.json, monitor.url, python\\, .no_autostart
  and any file that only exists on the site (site notes, site tools).
  Nothing is deleted; every overwritten file is backed up first to
  backup\\update-<stamp>\\ (with a ROLLBACK.txt that says how to undo).

Every step says up front how invasive it is:
  [safe]          read-only, or adds files only
  [brief]         a service blips for a few seconds; nodes/displays untouched
  [CHANGES FILES] rewrites existing files (backed up first)
  [DESTRUCTIVE]   could disturb a running show - always asked, default No

Stdlib only; runs on the bundled embeddable Python 3.11.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# What is code (updated) and what is site data (never touched)
# ---------------------------------------------------------------------------

CODE_DIRS = ("nvmon", "bats", "bin", "docs")
ROOT_GLOBS = ("*.bat", "pyproject.toml", "requirements.txt", "README.md", "nvmon.toml.example")
PROFILE_EXAMPLES = "profiles/*.example.json"
NEVER = {
    "nvmon.toml", "secrets", "profiles", "edids", "logs", "status.json", "active_profile.json",
    "monitor.url", "python", ".no_autostart", "backup", "run",
}
SKIP_PARTS = {"__pycache__", ".pytest_cache"}

CREATE_NO_WINDOW = 0x08000000
LISTENER_RESTART_SAFE_AFTER_MIN = 15   # agent replays its profile only this soon after an OS boot


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def tag(kind: str) -> str:
    return {"safe": "[safe]         ", "brief": "[brief]        ",
            "files": "[CHANGES FILES]", "destr": "[DESTRUCTIVE]  "}[kind]


def ask(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return default
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            a = input(f"    {question} [{hint}] ").strip().lower()
        except EOFError:
            return default
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False


def version_of(root: Path) -> str:
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "nvmon" / "__init__.py").read_text("utf-8"))
        return m.group(1) if m else "?"
    except OSError:
        return "?"


def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: float = 5.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


# ---------------------------------------------------------------------------
# Plan + copy (non-destructive: never deletes, backs up what it overwrites)
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    source: Path
    target: Path
    new: list[str] = field(default_factory=list)       # relative paths
    changed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.new and not self.changed


def _is_site_data(rel: str) -> bool:
    first = rel.split("/", 1)[0].lower()
    if first in NEVER and not (first == "profiles" and rel.endswith(".example.json")):
        return True
    return any(p in SKIP_PARTS for p in rel.split("/"))


def code_files(source: Path) -> list[str]:
    rels: list[str] = []
    for d in CODE_DIRS:
        base = source / d
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    rels.append(p.relative_to(source).as_posix())
    for g in ROOT_GLOBS + (PROFILE_EXAMPLES,):
        rels += [p.relative_to(source).as_posix() for p in source.glob(g) if p.is_file()]
    return sorted(r for r in set(rels) if not _is_site_data(r))


def make_plan(source: Path, target: Path) -> Plan:
    plan = Plan(source, target)
    for rel in code_files(source):
        dst = target / rel
        if not dst.exists():
            plan.new.append(rel)
        elif not filecmp.cmp(source / rel, dst, shallow=False):
            plan.changed.append(rel)
    return plan


def apply_plan(plan: Plan, stamp: str) -> Path | None:
    """Back up every file about to be overwritten, then copy. Returns the
    backup folder (None when nothing was overwritten)."""
    backup = plan.target / "backup" / f"update-{stamp}"
    if plan.changed:
        for rel in plan.changed:
            b = backup / rel
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plan.target / rel, b)
    failed = []
    for rel in plan.new + plan.changed:
        dst = plan.target / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plan.source / rel, dst)
        except OSError as exc:
            failed.append(f"{rel}: {exc}")
    for p in (plan.target / "nvmon").rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    if plan.changed or plan.new:
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "ROLLBACK.txt").write_text(
            "nvmon update rollback\r\n"
            f"from v{version_of(backup) if (backup / 'nvmon').exists() else '?'} (files below)\r\n\r\n"
            "1. Stop nvmon (kill_monitor.bat / kill_listener.bat).\r\n"
            f"2. Copy everything in this folder back over {plan.target}\r\n"
            "   (robocopy \"<this folder>\" \"<NVMON>\" /E /XF ROLLBACK.txt)\r\n"
            "3. Delete the files this update ADDED (listed below), then start nvmon again.\r\n\r\n"
            "Added by the update:\r\n" + "".join(f"  {r}\r\n" for r in plan.new),
            encoding="utf-8")
    if failed:
        say("    some files could not be written (in use or no permission):")
        for f in failed[:15]:
            say(f"      {f}")
        if len(failed) > 15:
            say(f"      ... and {len(failed) - 15} more")
    return backup if (plan.changed or plan.new) else None


def describe_plan(plan: Plan, indent: str = "    ") -> None:
    say(f"{indent}{len(plan.new)} new file(s), {len(plan.changed)} changed file(s); "
        f"site data untouched")
    groups: dict[str, int] = {}
    for rel in plan.new + plan.changed:
        top = rel.split("/")[0] if "/" in rel else "(root files)"
        groups[top] = groups.get(top, 0) + 1
    if groups:
        say(indent + ", ".join(f"{k}: {v}" for k, v in sorted(groups.items())))


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_profiles(source: Path, target: Path) -> None:
    """Check every site profile loads with the NEW code's own loader.

    Read-only: profiles are never rewritten. The profile format the agent
    uses (agent/profiles.py) is the same across these versions; this just
    proves it before the listeners run the new code. (Note: the separate
    agent/profile_migrate.py "v1 -> v2" module is NOT used at runtime and
    must not be run on these files.)"""
    prof_dir = target / "profiles"
    files = sorted(p for p in prof_dir.glob("*.json") if not p.name.endswith(".example.json")) \
        if prof_dir.is_dir() else []
    say(f"    {tag('safe')} read-only check of {len(files)} profile(s) against the new loader")
    if not files:
        return
    sys.path.insert(0, str(source))
    try:
        from nvmon.agent import profiles as prof  # type: ignore
    except Exception as exc:  # noqa: BLE001
        say(f"    (skipped: could not load the new profile code here: {exc})")
        return
    bad = []
    for p in files:
        try:
            prof._dict_to_profile(json.loads(p.read_text("utf-8-sig")))
        except Exception as exc:  # noqa: BLE001
            bad.append((p.name, exc))
    if not bad:
        say(f"    all {len(files)} load fine with v{version_of(source)}; nothing to change")
        return
    say(f"    {len(bad)} profile(s) do NOT load with the new code (left untouched - fix or")
    say("    re-capture them from the dashboard before applying them):")
    for name, exc in bad:
        say(f"      {name}: {exc}")


def step_config_report(source: Path, target: Path) -> None:
    """Report settings the new example has that this site's nvmon.toml lacks."""
    ex, live = source / "nvmon.toml.example", target / "nvmon.toml"
    if not ex.is_file() or not live.is_file():
        say("    (no nvmon.toml or example to compare)")
        return

    def keys(d: dict, prefix: str = "") -> set[str]:
        out = set()
        for k, v in d.items():
            name = f"{prefix}{k}"
            if isinstance(v, dict):
                out |= keys(v, name + ".")
            elif not (isinstance(v, list) and v and isinstance(v[0], dict)):
                out.add(name)
        return out

    try:
        new = keys(tomllib.loads(ex.read_text("utf-8")))
        have = keys(tomllib.loads(live.read_text("utf-8")))
    except Exception as exc:  # noqa: BLE001
        say(f"    (could not parse: {exc})")
        return
    missing = sorted(new - have)
    if not missing:
        say("    nvmon.toml already has every setting the example shows")
        return
    say(f"    {len(missing)} optional setting(s) are new since this site's nvmon.toml was written;")
    say("    they run on their defaults until you add them (see nvmon.toml.example):")
    for k in missing[:25]:
        say(f"      {k}")
    if len(missing) > 25:
        say(f"      ... and {len(missing) - 25} more")


def step_requirements(target: Path, backup: Path | None, dry: bool, yes: bool) -> None:
    req = target / "requirements.txt"
    old = backup / "requirements.txt" if backup else None
    if not old or not old.is_file():
        say("    Python packages: unchanged")
        return
    py = target / "python" / "python.exe"
    say(f"    {tag('files')} requirements.txt changed: installs/updates Python packages "
        "in python\\ (needs internet)")
    if dry or not py.is_file() or not ask("Run pip install now?", True, yes):
        return
    subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(req)], cwd=str(target))


def stop_app(target: Path) -> None:
    """Quit `nvmon app` on this box (its window exe would be locked)."""
    try:
        import ctypes
        u = ctypes.WinDLL("user32")
        u.FindWindowW.restype = ctypes.c_void_p
        h = u.FindWindowW("nvmonAppBubble", None)
        if h:
            u.PostMessageW(ctypes.c_void_p(h), 0x10, 0, 0)   # WM_CLOSE = Quit
            time.sleep(4)
            say("    closed the running nvmon app (it is reopened below)")
    except Exception:  # noqa: BLE001
        pass


def step_app(target: Path, dry: bool, yes: bool) -> None:
    py = target / "python" / "pythonw.exe"
    if not py.is_file():
        say("    (python\\pythonw.exe missing here: run bats\\install.bat --monitor first)")
        return
    say(f"    {tag('safe')} adds 'nvmon' shortcuts (desktop + this folder) for the desktop app")
    say("                    (the app runs next to the monitor; it does not replace it)")
    if dry or not ask("Create the shortcuts and start the app?", True, yes):
        return
    pyc = target / "python" / "python.exe"
    subprocess.run([str(pyc), "-m", "nvmon", "app", "--shortcuts"], cwd=str(target))
    subprocess.Popen([str(py), "-m", "nvmon", "app"], cwd=str(target),
                     creationflags=CREATE_NO_WINDOW)


def step_restart_monitor(target: Path, dry: bool, yes: bool) -> None:
    if not port_open(9997):
        say("    monitor is not running here; it uses the new code next time it starts")
        return
    helper = target / "bats" / "_restart_monitor_helper.bat"
    say(f"    {tag('brief')} restarting the monitor: dashboards drop for ~5 s; "
        "nodes and displays are not touched")
    if dry or not ask("Restart the monitor now?", True, yes):
        say("    (it keeps running the old code until it is restarted)")
        return
    if helper.is_file():
        subprocess.Popen(["cmd.exe", "/c", str(helper), "1", "nobrowser"], cwd=str(target),
                         creationflags=0x00000008 | 0x00000200)   # detached
    else:
        say("    restart helper missing: run kill_monitor.bat then run_monitor.bat")


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------

def site_nodes(target: Path) -> list[tuple[str, str]]:
    """(hostname, url) from nvmon.toml + push-registered nodes."""
    out: dict[str, str] = {}
    try:
        cfg = tomllib.loads((target / "nvmon.toml").read_text("utf-8"))
        for n in (cfg.get("controller", {}) or {}).get("nodes", []) or []:
            if n.get("hostname") and n.get("url"):
                out[n["hostname"]] = n["url"].rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    try:
        reg = json.loads((target / "secrets" / "registered_nodes.json").read_text("utf-8"))
        items = reg.get("nodes", reg) if isinstance(reg, dict) else reg
        for n in items if isinstance(items, list) else []:
            if isinstance(n, dict) and n.get("hostname") and n.get("url"):
                out.setdefault(n["hostname"], n["url"].rstrip("/"))
    except Exception:  # noqa: BLE001
        pass
    return sorted(out.items())


def step_listeners(source: Path, target: Path, share_tpl: str, dry: bool, yes: bool, stamp: str) -> None:
    nodes = site_nodes(target)
    if not nodes:
        say("    no listeners listed in nvmon.toml / registered nodes")
        return
    say(f"    {len(nodes)} listener(s); code is copied to each node's share: {share_tpl}")
    say(f"    {tag('brief')} each listener restarts (~10 s offline in the dashboard). Displays,")
    say(f"                    sync and profiles are NOT re-applied (that only happens right after an")
    say(f"                    OS boot; listeners up < {LISTENER_RESTART_SAFE_AFTER_MIN} min are copied but not restarted).")
    todo = []
    for host, url in nodes:
        ip = re.sub(r"^https?://([^:/]+).*$", r"\1", url)
        share = Path(share_tpl.format(host=host, ip=ip))
        code, health = http_json(f"{url}/health", timeout=3)
        ver = (health or {}).get("version", "?") if code == 200 else "offline"
        up = None
        if code == 200:
            # Agent-process uptime: a conservative stand-in for "the node just
            # booted" (it is never longer than the OS uptime).
            _, st = http_json(f"{url}/status", timeout=8)
            up = (st or {}).get("uptime_seconds")
        if not (share / "nvmon").is_dir():
            say(f"      {host:22} v{ver:10} share not reachable ({share}) -> run update_site.bat "
                "on that node (mode: listener)")
            continue
        plan = make_plan(source, share)
        say(f"      {host:22} v{ver:10} {len(plan.new)} new / {len(plan.changed)} changed file(s)")
        todo.append((host, url, share, plan, code == 200, up))
    if dry or not todo or not ask(f"Update {len(todo)} listener(s) now?", False, yes):
        return
    for host, url, share, plan, online, up in todo:
        if not plan.empty:
            apply_plan(plan, stamp)
        if not online:
            say(f"      {host}: copied; listener offline, it runs the new code when it starts")
            continue
        if isinstance(up, (int, float)) and up < LISTENER_RESTART_SAFE_AFTER_MIN * 60:
            say(f"      {host}: copied; NOT restarted (its listener started {int(up // 60)} min ago, "
                "the node may have just booted and a restart could re-apply its profile). "
                "Restart it later from the dashboard.")
            continue
        code, _ = http_json(f"{url}/system/restart_listener", "POST", {"delay_seconds": 2})
        say(f"      {host}: copied; " + ("listener restarting" if code in (200, 202) else
                                         f"restart request failed (HTTP {code}) - restart it from "
                                         "the dashboard or reboot the node"))


def step_local_listener_restart(dry: bool, yes: bool) -> None:
    if not port_open(9998):
        say("    listener not running here; it uses the new code next time it starts")
        return
    say(f"    {tag('brief')} restart this listener (~10 s offline; displays/sync not re-applied "
        f"unless this box booted < {LISTENER_RESTART_SAFE_AFTER_MIN} min ago)")
    if dry or not ask("Restart the listener now?", True, yes):
        return
    code, _ = http_json("http://127.0.0.1:9998/system/restart_listener", "POST", {"delay_seconds": 2})
    say("    listener restarting" if code in (200, 202) else
        f"    restart request failed (HTTP {code}): run kill_listener.bat then run_listener.bat")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Update an existing nvmon site from this copy.")
    ap.add_argument("--source", required=True, help="the NEW NVMON folder (this copy)")
    ap.add_argument("--target", default="", help="the EXISTING NVMON folder to update")
    ap.add_argument("--mode", choices=("monitor", "monitor+listeners", "listener"))
    ap.add_argument("--share", default=r"\\{host}\NVMON",
                    help=r"listener NVMON share, {host}/{ip} filled in (default \\{host}\NVMON)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--yes", action="store_true", help="accept the default answer everywhere")
    a = ap.parse_args(argv)

    source = Path(a.source).resolve()
    target_s = a.target.strip().strip('"')
    if not target_s:
        target_s = input("Existing NVMON folder to update (e.g. E:\\NVMON): ").strip().strip('"')
    target = Path(target_s).resolve()

    say("")
    say("=" * 72)
    say(f"  nvmon site update     v{version_of(target)}  ->  v{version_of(source)}")
    say(f"  from  {source}")
    say(f"  into  {target}")
    say("=" * 72)
    if not (source / "nvmon" / "__init__.py").is_file():
        say("ERROR: the source is not an NVMON folder (no nvmon\\__init__.py).")
        return 2
    if not (target / "nvmon").is_dir():
        say("ERROR: the target is not an existing NVMON install (no nvmon\\ folder).")
        return 2
    if source == target:
        say("ERROR: source and target are the same folder. Run update_site.bat from the NEW")
        say("       copy (e.g. a fresh clone) and point it at the existing install.")
        return 2

    mode = a.mode
    if not mode:
        is_monitor = (target / "run_monitor.bat").exists() and (
            (target / "secrets" / "controller.password").exists() or port_open(9997))
        say("")
        say("What should be updated?")
        say("  1) monitor only          this box's monitor + the new desktop app;")
        say("                           listeners keep running their current version")
        say("  2) monitor + listeners   also push the update to every listener")
        say("  3) this box is a listener")
        default = "1" if is_monitor else "3"
        pick = (input(f"Choice [{default}]: ").strip() or default) if not a.yes else default
        mode = {"1": "monitor", "2": "monitor+listeners", "3": "listener"}.get(pick, "monitor")
    dry = a.dry_run
    if not dry and not a.yes:
        say("")
        dry = ask("Do a dry run first (show everything, change nothing)?", True, False)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    say("")
    say(f"Mode: {mode}{'   (DRY RUN - nothing is changed)' if dry else ''}")

    # 1. code
    say("")
    say(f"1. Code   {tag('files')} (backed up first; your config/profiles/secrets/logs are never touched)")
    plan = make_plan(source, target)
    describe_plan(plan)
    backup = None
    if not plan.empty and not dry:
        if ask("Update the code now?", True, a.yes):
            if mode != "listener":
                stop_app(target)
            backup = apply_plan(plan, stamp)
            if backup:
                say(f"    done. Backup + ROLLBACK.txt: {backup}")
        else:
            say("    skipped; stopping here (later steps need the new code).")
            return 0

    # 2. python packages
    say("")
    say("2. Python packages")
    step_requirements(target, backup, dry, a.yes)

    # 3. profiles
    say("")
    say("3. Profiles")
    step_profiles(source, target)

    # 4. config report
    say("")
    say(f"4. Settings   {tag('safe')} (report only - nvmon.toml is not edited)")
    step_config_report(source, target)

    if mode == "listener":
        say("")
        say("5. Listener restart")
        step_local_listener_restart(dry, a.yes)
    else:
        say("")
        say("5. Monitor restart")
        step_restart_monitor(target, dry, a.yes)
        say("")
        say("6. Desktop app (nvmon app + floating sync widget)")
        step_app(target, dry, a.yes)
        say("")
        say("7. Listeners")
        if mode == "monitor+listeners":
            step_listeners(source, target, a.share, dry, a.yes, stamp)
        else:
            say("    left as they are (option 1). The new monitor works with them; listener-side")
            say("    features added since their version arrive when they are updated.")

    say("")
    say("Dry run finished - nothing was changed. Run again and answer No to the dry run "
        "to apply." if dry else "Update finished.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        say("\ncancelled.")
        sys.exit(130)
