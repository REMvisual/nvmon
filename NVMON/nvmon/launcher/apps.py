"""App configuration: AppConfig dataclass + TOML loader.

Reads `[launcher]` settings and the `[[launcher.apps]]` array from
`nvmon.toml`. Validates names (regex), rejects duplicates, and fills in
defaults for every optional field so downstream code never has to guess.

Required TOML fields: `name`, `exe_path`.
Everything else has a safe default — see spec §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config as _config
from .hooks import HookConfig, hook_from_dict


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

NAME_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class AppConfigError(ValueError):
    """Raised when an app config is malformed or violates a spec rule."""


# ---------------------------------------------------------------------------
# Launcher-wide defaults (from [launcher] section)
# ---------------------------------------------------------------------------

DEFAULT_PER_APP_LOG_BYTES = 10 * 1024 * 1024   # 10 MiB
DEFAULT_PER_APP_LOG_KEEP = 5
DEFAULT_KILL_GRACE_SECONDS = 10
DEFAULT_ADMIN_REQUIRED_FOR_CONTROL = True
DEFAULT_RESTART_BACKOFF_SECONDS: list[int] = [2, 5, 15, 60]
DEFAULT_CRASH_COOLDOWN_SECONDS = 120
DEFAULT_EXPECTED_EXIT_CODES: list[int] = [0]


# ---------------------------------------------------------------------------
# AppConfig dataclass
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    """One launchable application, declared in `[[launcher.apps]]`."""

    name: str
    exe_path: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    working_dir: str = ""
    elevated: bool = False
    kill_by_name: str = ""
    auto_restart: bool = False
    restart_backoff_seconds: list[int] = field(
        default_factory=lambda: list(DEFAULT_RESTART_BACKOFF_SECONDS)
    )
    crash_cooldown_seconds: int = DEFAULT_CRASH_COOLDOWN_SECONDS
    expected_exit_codes: list[int] = field(
        default_factory=lambda: list(DEFAULT_EXPECTED_EXIT_CODES)
    )
    kill_grace_seconds: int = DEFAULT_KILL_GRACE_SECONDS
    pre_launch: list[HookConfig] = field(default_factory=list)
    pre_launch_on_auto_restart: bool = True


@dataclass
class LauncherConfig:
    """Launcher-wide settings plus the app list."""

    port: int = _config.DEFAULT_LAUNCHER_PORT
    bind_host: str = "127.0.0.1"
    admin_required_for_control: bool = DEFAULT_ADMIN_REQUIRED_FOR_CONTROL
    reload_config_on_sighup: bool = True
    log_dir: str = ""
    per_app_log_bytes: int = DEFAULT_PER_APP_LOG_BYTES
    per_app_log_keep: int = DEFAULT_PER_APP_LOG_KEEP
    default_kill_grace_seconds: int = DEFAULT_KILL_GRACE_SECONDS
    agent_port: int = _config.DEFAULT_AGENT_PORT
    apps: list[AppConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TOML -> dataclass
# ---------------------------------------------------------------------------

def _as_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val)


def _as_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _as_bool(val: Any, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    return default


def _as_str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(x) for x in val]


def _as_int_list(val: Any, default: list[int]) -> list[int]:
    if not isinstance(val, list):
        return list(default)
    out: list[int] = []
    for x in val:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out if out else list(default)


def _as_env(val: Any) -> dict[str, str]:
    if not isinstance(val, dict):
        return {}
    return {str(k): str(v) for k, v in val.items()}


def app_from_dict(raw: dict[str, Any]) -> AppConfig:
    """Build an AppConfig from a single `[[launcher.apps]]` entry.

    Raises AppConfigError for missing required fields or invalid values.
    """
    name = _as_str(raw.get("name"))
    exe_path = _as_str(raw.get("exe_path"))

    if not name:
        raise AppConfigError("app config missing required field: name")
    if not NAME_REGEX.match(name):
        raise AppConfigError(
            f"invalid app name {name!r}: must match {NAME_REGEX.pattern}"
        )
    if not exe_path:
        raise AppConfigError(f"app {name!r} missing required field: exe_path")

    exe_p = Path(exe_path)
    if not exe_p.is_absolute():
        raise AppConfigError(
            f"app {name!r} exe_path must be absolute: {exe_path!r}"
        )

    pre_launch_raw = raw.get("pre_launch") or []
    if not isinstance(pre_launch_raw, list):
        raise AppConfigError(f"app {name!r} pre_launch must be a list")
    pre_launch: list[HookConfig] = []
    for i, h in enumerate(pre_launch_raw):
        if not isinstance(h, dict):
            raise AppConfigError(
                f"app {name!r} pre_launch[{i}] must be a table"
            )
        try:
            pre_launch.append(hook_from_dict(h))
        except ValueError as e:
            raise AppConfigError(
                f"app {name!r} pre_launch[{i}]: {e}"
            ) from e

    return AppConfig(
        name=name,
        exe_path=exe_path,
        args=_as_str_list(raw.get("args")),
        env=_as_env(raw.get("env")),
        working_dir=_as_str(raw.get("working_dir")),
        elevated=_as_bool(raw.get("elevated"), False),
        kill_by_name=_as_str(raw.get("kill_by_name")),
        auto_restart=_as_bool(raw.get("auto_restart"), False),
        restart_backoff_seconds=_as_int_list(
            raw.get("restart_backoff_seconds"),
            DEFAULT_RESTART_BACKOFF_SECONDS,
        ),
        crash_cooldown_seconds=_as_int(
            raw.get("crash_cooldown_seconds"), DEFAULT_CRASH_COOLDOWN_SECONDS
        ),
        expected_exit_codes=_as_int_list(
            raw.get("expected_exit_codes"), DEFAULT_EXPECTED_EXIT_CODES
        ),
        kill_grace_seconds=_as_int(
            raw.get("kill_grace_seconds"), DEFAULT_KILL_GRACE_SECONDS
        ),
        pre_launch=pre_launch,
        pre_launch_on_auto_restart=_as_bool(
            raw.get("pre_launch_on_auto_restart"), True
        ),
    )


def load_launcher_config(*, force_reload: bool = False) -> LauncherConfig:
    """Build a LauncherConfig from the current nvmon.toml.

    If `[launcher.apps]` contains duplicates or invalid entries, raises
    AppConfigError. This is fatal at boot (spec §4) — operator intervention
    required.
    """
    if force_reload:
        _config.load_config(force_reload=True)

    launcher_raw = _config.get_launcher_config()
    agent_raw = _config.get_agent_config()

    # Pull extra launcher-wide fields that get_launcher_config() doesn't expose.
    root = _config.load_config()
    section = root.get("launcher", {}) if isinstance(root.get("launcher"), dict) else {}

    log_dir_raw = _as_str(
        section.get("log_dir"),
        str(Path(_config.LOGS_DIR) / "launcher"),
    )

    cfg = LauncherConfig(
        port=_as_int(launcher_raw.get("port"), _config.DEFAULT_LAUNCHER_PORT),
        bind_host=_as_str(section.get("bind_host"), "127.0.0.1"),
        admin_required_for_control=_as_bool(
            section.get("admin_required_for_control"),
            DEFAULT_ADMIN_REQUIRED_FOR_CONTROL,
        ),
        reload_config_on_sighup=_as_bool(
            section.get("reload_config_on_sighup"), True
        ),
        log_dir=log_dir_raw,
        per_app_log_bytes=_as_int(
            section.get("per_app_log_bytes"), DEFAULT_PER_APP_LOG_BYTES
        ),
        per_app_log_keep=_as_int(
            section.get("per_app_log_keep"), DEFAULT_PER_APP_LOG_KEEP
        ),
        default_kill_grace_seconds=_as_int(
            section.get("default_kill_grace_seconds"),
            DEFAULT_KILL_GRACE_SECONDS,
        ),
        agent_port=_as_int(
            agent_raw.get("port"), _config.DEFAULT_AGENT_PORT
        ),
    )

    seen: set[str] = set()
    for raw in launcher_raw.get("apps", []):
        if not isinstance(raw, dict):
            raise AppConfigError("launcher.apps entries must be tables")
        app = app_from_dict(raw)
        if app.name in seen:
            raise AppConfigError(
                f"duplicate app name in launcher.apps: {app.name!r}"
            )
        seen.add(app.name)

        # Inherit the launcher-wide grace if the app didn't override it.
        # raw check: the default on AppConfig matches DEFAULT_KILL_GRACE_SECONDS,
        # but we want launcher.default_kill_grace_seconds to take effect when
        # the app is silent.
        if "kill_grace_seconds" not in raw:
            app.kill_grace_seconds = cfg.default_kill_grace_seconds
        cfg.apps.append(app)

    return cfg


def load_apps_from_toml(*, force_reload: bool = False) -> list[AppConfig]:
    """Convenience — just the app list."""
    return load_launcher_config(force_reload=force_reload).apps


__all__ = [
    "AppConfig",
    "AppConfigError",
    "LauncherConfig",
    "NAME_REGEX",
    "app_from_dict",
    "load_apps_from_toml",
    "load_launcher_config",
]
