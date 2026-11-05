"""Pre-launch hooks — declarative, sequential, portable.

Every write endpoint the nvmon agent exposes has a matching hook;
every pollable read endpoint has an `await` hook. Thirteen hook types
total per spec §4a:

    nvmon_apply, nvmon_restore, nvmon_dd_enable, nvmon_dd_disable,
    nvmon_qsync_enable, nvmon_qsync_disable, nvmon_mosaic_disable,
    nvmon_await_status, nvmon_validate, http_post, http_get_wait,
    shell_command, delay

All `nvmon_*` hooks implicitly target http://127.0.0.1:<agent_port>
where agent_port comes from the shared [agent] section of nvmon.toml.

Execution contract (spec §4a):
    - Each hook has its own `timeout_seconds` — there's no wall-clock
      ceiling on the sequence.
    - `required=true` (default) means an abort-on-failure hook.
      `required=false` continues the sequence on failure.
    - `on_fail` overrides: "abort", "continue", "retry" (with
      `max_retries`, default 2).
    - A cancellation Event lets the supervisor abandon a running
      hook when the operator kills the app mid-pre_launch.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# The full set of supported hook types (spec §4a, exactly 13)
# ---------------------------------------------------------------------------

HOOK_TYPES: tuple[str, ...] = (
    "nvmon_apply",
    "nvmon_restore",
    "nvmon_dd_enable",
    "nvmon_dd_disable",
    "nvmon_qsync_enable",
    "nvmon_qsync_disable",
    "nvmon_mosaic_disable",
    "nvmon_await_status",
    "nvmon_validate",
    "http_post",
    "http_get_wait",
    "shell_command",
    "delay",
)


NVMON_STATUS_CONDITION_KEYS: tuple[str, ...] = (
    "dd_active",
    "dd_count",
    "mosaic_enabled",
    "mosaic_refresh_rate",
    "qsync_synced",
    "qsync_house_sync_detected",
    "gpu_all_same_refresh",
    "gpu_max_temp_c",
    "active_profile",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class HookConfig:
    """One entry in an app's `pre_launch` list.

    Only `type` is required to construct; hook-specific fields live in
    `params` keyed by their declared name in the spec table. Validation
    happens in `hook_from_dict`.
    """
    type: str
    timeout_seconds: float = 30.0
    required: bool = True
    name: str = ""
    on_fail: str = ""          # "abort" | "continue" | "retry" | ""=auto
    max_retries: int = 2
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_on_fail(self) -> str:
        if self.on_fail in ("abort", "continue", "retry"):
            return self.on_fail
        return "abort" if self.required else "continue"

    @property
    def label(self) -> str:
        return self.name or self.type


@dataclass
class HookContext:
    """Ambient state passed to every hook invocation."""
    agent_base_url: str              # "http://127.0.0.1:9998"
    cancel_event: threading.Event    # set when app is being killed mid-prelaunch
    logger: Any = None               # anything with .info/.warning/.error
    app_name: str = ""
    run_id: str = ""
    hook_index: int = 0
    hook_total: int = 0


@dataclass
class HookResult:
    ok: bool
    detail: str = ""
    elapsed_seconds: float = 0.0
    cancelled: bool = False
    timed_out: bool = False


# ---------------------------------------------------------------------------
# TOML -> HookConfig
# ---------------------------------------------------------------------------

_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "nvmon_apply": ("profile",),
    "nvmon_restore": (),
    "nvmon_dd_enable": (),
    "nvmon_dd_disable": (),
    "nvmon_qsync_enable": ("source", "trigger", "grid"),
    "nvmon_qsync_disable": (),
    "nvmon_mosaic_disable": (),
    "nvmon_await_status": ("conditions",),
    "nvmon_validate": (),
    "http_post": ("url",),
    "http_get_wait": ("url", "json_path", "expect"),
    "shell_command": ("exe", "args"),
    "delay": ("seconds",),
}


def hook_from_dict(raw: dict[str, Any]) -> HookConfig:
    """Convert a TOML table into a validated HookConfig."""
    t = raw.get("type")
    if not isinstance(t, str) or t not in HOOK_TYPES:
        raise ValueError(
            f"unknown hook type {t!r}; expected one of {HOOK_TYPES}"
        )

    # timeout_seconds required for all hook types EXCEPT `delay`
    # (delay uses `seconds` instead).
    timeout = raw.get("timeout_seconds")
    if t == "delay":
        seconds = raw.get("seconds")
        if not isinstance(seconds, (int, float)) or float(seconds) < 0:
            raise ValueError("delay hook requires non-negative 'seconds'")
        timeout_val = float(seconds) + 5.0
    else:
        if not isinstance(timeout, (int, float)) or float(timeout) <= 0:
            raise ValueError(
                f"hook {t!r} requires positive 'timeout_seconds'"
            )
        timeout_val = float(timeout)

    # Required per-type param presence check
    for p in _REQUIRED_PARAMS[t]:
        if p not in raw:
            raise ValueError(f"hook {t!r} missing required param {p!r}")

    required = raw.get("required", True)
    if not isinstance(required, bool):
        required = True

    on_fail = raw.get("on_fail", "")
    if on_fail not in ("", "abort", "continue", "retry"):
        raise ValueError(f"hook on_fail must be abort|continue|retry, got {on_fail!r}")

    # Strip meta fields; everything else becomes params.
    meta_fields = {
        "type", "timeout_seconds", "required", "name", "on_fail",
        "max_retries",
    }
    params = {k: v for k, v in raw.items() if k not in meta_fields}

    # conditions on nvmon_await_status must only contain supported keys
    if t == "nvmon_await_status":
        cond = params.get("conditions")
        if not isinstance(cond, dict) or not cond:
            raise ValueError("nvmon_await_status requires non-empty 'conditions'")
        for k in cond:
            if k not in NVMON_STATUS_CONDITION_KEYS:
                raise ValueError(
                    f"nvmon_await_status condition {k!r} not supported; "
                    f"expected one of {NVMON_STATUS_CONDITION_KEYS}"
                )

    return HookConfig(
        type=t,
        timeout_seconds=timeout_val,
        required=required,
        name=str(raw.get("name", "")),
        on_fail=on_fail,
        max_retries=int(raw.get("max_retries", 2) or 2),
        params=params,
    )


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (urllib — no external deps)
# ---------------------------------------------------------------------------

def _cancellable_sleep(event: threading.Event, seconds: float) -> bool:
    """Sleep for up to `seconds`, returning True if cancelled."""
    if seconds <= 0:
        return event.is_set()
    return event.wait(seconds)


def _http_request(
    method: str,
    url: str,
    *,
    timeout: float,
    body: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    """Issue one HTTP request. Returns (status, body_bytes).

    Raises urllib.error.URLError on network failures and
    TimeoutError on explicit timeout.
    """
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        # HTTP-level error — still gives us a body
        return e.code, (e.read() if hasattr(e, "read") else b"")


def _dotted_path(obj: Any, path: str) -> Any:
    """Fetch value at `a.b.c` from nested dict/list. None if absent."""
    cur: Any = obj
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
            continue
        # list index support
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        return None
    return cur


# ---------------------------------------------------------------------------
# Condition evaluation for nvmon_await_status (spec §4a)
# ---------------------------------------------------------------------------

def _eval_condition(status: dict[str, Any], key: str, expected: Any) -> bool:
    """Return True iff status satisfies `key == expected`.

    Keys map to /status paths per spec §4a.
    """
    if key == "dd_active":
        return bool(_dotted_path(status, "direct_display.active")) == bool(expected)
    if key == "dd_count":
        actual = _dotted_path(status, "direct_display.display_count")
        try:
            return int(actual) == int(expected)
        except (TypeError, ValueError):
            return False
    if key == "mosaic_enabled":
        return bool(_dotted_path(status, "mosaic.enabled")) == bool(expected)
    if key == "mosaic_refresh_rate":
        actual = _dotted_path(status, "mosaic.refresh_rate")
        try:
            return abs(float(actual) - float(expected)) <= 0.01
        except (TypeError, ValueError):
            return False
    if key == "qsync_synced":
        return bool(_dotted_path(status, "qsync.synced")) == bool(expected)
    if key == "qsync_house_sync_detected":
        return (
            bool(_dotted_path(status, "qsync.house_sync_detected"))
            == bool(expected)
        )
    if key == "gpu_all_same_refresh":
        return (
            bool(_dotted_path(status, "output_health.all_same_refresh"))
            == bool(expected)
        )
    if key == "gpu_max_temp_c":
        gpus = _dotted_path(status, "gpu") or _dotted_path(status, "gpus") or []
        if not isinstance(gpus, list) or not gpus:
            return False
        try:
            max_temp = max(int(g.get("temperature_c", 0)) for g in gpus)
            return max_temp <= int(expected)
        except (TypeError, ValueError):
            return False
    if key == "active_profile":
        return str(_dotted_path(status, "active_profile") or "") == str(expected)
    return False


# ---------------------------------------------------------------------------
# Per-hook implementations
# ---------------------------------------------------------------------------

def _run_delay(cfg: HookConfig, ctx: HookContext) -> HookResult:
    seconds = float(cfg.params.get("seconds", 0))
    t0 = time.monotonic()
    cancelled = _cancellable_sleep(ctx.cancel_event, seconds)
    elapsed = time.monotonic() - t0
    if cancelled:
        return HookResult(ok=False, cancelled=True, elapsed_seconds=elapsed,
                          detail="cancelled")
    return HookResult(ok=True, elapsed_seconds=elapsed,
                      detail=f"slept {seconds}s")


def _run_shell_command(cfg: HookConfig, ctx: HookContext) -> HookResult:
    exe = str(cfg.params.get("exe", ""))
    args = cfg.params.get("args") or []
    if not isinstance(args, list):
        return HookResult(ok=False, detail="args must be a list")
    args_str = [str(a) for a in args]
    expected_codes_raw = cfg.params.get("expected_exit_codes") or [0]
    try:
        expected_codes = [int(x) for x in expected_codes_raw]
    except (TypeError, ValueError):
        expected_codes = [0]

    p = Path(exe)
    if not p.is_absolute():
        return HookResult(ok=False, detail=f"exe must be absolute: {exe!r}")

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [exe] + args_str,
            shell=False,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return HookResult(
            ok=False, timed_out=True,
            elapsed_seconds=time.monotonic() - t0,
            detail=f"timed out after {cfg.timeout_seconds}s",
        )
    except FileNotFoundError:
        return HookResult(ok=False, detail=f"exe not found: {exe!r}",
                          elapsed_seconds=time.monotonic() - t0)
    elapsed = time.monotonic() - t0
    if proc.returncode in expected_codes:
        return HookResult(ok=True, elapsed_seconds=elapsed,
                          detail=f"exit={proc.returncode}")
    return HookResult(
        ok=False,
        elapsed_seconds=elapsed,
        detail=(
            f"exit={proc.returncode} not in {expected_codes}; "
            f"stderr={proc.stderr.strip()[:300]}"
        ),
    )


def _run_http_post(cfg: HookConfig, ctx: HookContext) -> HookResult:
    url = str(cfg.params.get("url"))
    body = cfg.params.get("body")
    if body is not None and not isinstance(body, dict):
        return HookResult(ok=False, detail="body must be a JSON object")

    expected = cfg.params.get("expected_status", [200, 202])
    if isinstance(expected, int):
        expected = [expected]
    try:
        expected_list = [int(x) for x in expected]
    except (TypeError, ValueError):
        expected_list = [200, 202]

    t0 = time.monotonic()
    try:
        status, _ = _http_request(
            "POST", url, timeout=cfg.timeout_seconds, body=body
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return HookResult(
            ok=False, elapsed_seconds=time.monotonic() - t0,
            detail=f"http_post error: {e}",
        )
    elapsed = time.monotonic() - t0
    if status in expected_list:
        return HookResult(ok=True, elapsed_seconds=elapsed,
                          detail=f"POST {url} -> {status}")
    return HookResult(
        ok=False, elapsed_seconds=elapsed,
        detail=f"POST {url} -> {status}, expected {expected_list}",
    )


def _run_http_get_wait(cfg: HookConfig, ctx: HookContext) -> HookResult:
    url = str(cfg.params.get("url"))
    json_path = str(cfg.params.get("json_path"))
    expect = cfg.params.get("expect")
    poll_seconds = float(cfg.params.get("poll_seconds", 2.0))
    deadline = time.monotonic() + cfg.timeout_seconds
    last_detail = ""

    while True:
        if ctx.cancel_event.is_set():
            return HookResult(ok=False, cancelled=True, detail="cancelled")
        try:
            status, body = _http_request(
                "GET", url, timeout=min(poll_seconds + 5.0, cfg.timeout_seconds)
            )
            if status == 200:
                try:
                    parsed = json.loads(body.decode("utf-8") or "null")
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    last_detail = f"bad json: {e}"
                else:
                    actual = _dotted_path(parsed, json_path)
                    if actual == expect:
                        return HookResult(
                            ok=True, detail=f"{json_path}={actual}"
                        )
                    last_detail = f"{json_path}={actual!r}, want {expect!r}"
            else:
                last_detail = f"GET {url} -> {status}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_detail = f"http_get error: {e}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return HookResult(
                ok=False, timed_out=True,
                detail=f"timed out; last: {last_detail}",
            )
        if _cancellable_sleep(ctx.cancel_event, min(poll_seconds, remaining)):
            return HookResult(ok=False, cancelled=True, detail="cancelled")


def _poll_agent_until(
    ctx: HookContext,
    endpoint: str,
    check: Callable[[dict[str, Any]], tuple[bool, str]],
    poll_seconds: float,
    timeout_seconds: float,
) -> HookResult:
    """Poll GET <agent>/<endpoint> until check(body) returns (True, _).

    `check` returns (is_done, detail_for_logs).
    """
    url = f"{ctx.agent_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    deadline = time.monotonic() + timeout_seconds
    last_detail = ""
    while True:
        if ctx.cancel_event.is_set():
            return HookResult(ok=False, cancelled=True, detail="cancelled")
        try:
            status, body = _http_request(
                "GET", url, timeout=min(poll_seconds + 5.0, timeout_seconds)
            )
            if status == 200:
                try:
                    parsed = json.loads(body.decode("utf-8") or "null")
                    if not isinstance(parsed, dict):
                        parsed = {}
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    last_detail = f"bad json: {e}"
                    parsed = None  # type: ignore[assignment]
                if parsed is not None:
                    done, detail = check(parsed)
                    if done:
                        return HookResult(ok=True, detail=detail)
                    last_detail = detail
            else:
                last_detail = f"GET {url} -> {status}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_detail = f"http error: {e}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return HookResult(ok=False, timed_out=True,
                              detail=f"timed out; last: {last_detail}")
        if _cancellable_sleep(ctx.cancel_event, min(poll_seconds, remaining)):
            return HookResult(ok=False, cancelled=True, detail="cancelled")


def _post_to_agent(
    ctx: HookContext,
    endpoint: str,
    body: dict[str, Any] | None,
    timeout: float,
    expected_status: list[int],
) -> HookResult:
    url = f"{ctx.agent_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    t0 = time.monotonic()
    try:
        status, _ = _http_request("POST", url, timeout=timeout, body=body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return HookResult(
            ok=False, elapsed_seconds=time.monotonic() - t0,
            detail=f"POST {url} error: {e}",
        )
    elapsed = time.monotonic() - t0
    if status in expected_status:
        return HookResult(ok=True, elapsed_seconds=elapsed,
                          detail=f"POST {endpoint} -> {status}")
    return HookResult(
        ok=False, elapsed_seconds=elapsed,
        detail=f"POST {endpoint} -> {status}, expected {expected_status}",
    )


def _run_nvmon_apply(cfg: HookConfig, ctx: HookContext) -> HookResult:
    """POST /apply then poll /status.active_profile == profile AND /validate."""
    profile = str(cfg.params.get("profile", ""))
    deadline = time.monotonic() + cfg.timeout_seconds

    # POST /apply (async, 202).
    r = _post_to_agent(
        ctx, "apply", {"profile": profile},
        timeout=min(15.0, cfg.timeout_seconds),
        expected_status=[200, 202],
    )
    if not r.ok:
        return r

    poll = float(cfg.params.get("poll_seconds", 2.0))

    # Wait for active_profile to match.
    def _profile_check(parsed: dict[str, Any]) -> tuple[bool, str]:
        actual = str(parsed.get("active_profile") or "")
        return (actual == profile, f"active_profile={actual!r}")

    r = _poll_agent_until(
        ctx, "status", _profile_check,
        poll_seconds=poll,
        timeout_seconds=max(1.0, deadline - time.monotonic()),
    )
    if not r.ok:
        return r

    # Then, if there's any remaining time, one validation poll — non-fatal
    # if /validate returns all_passed=false, per spec (best-effort check
    # AND /validate.all_passed == true if the profile defines validation).
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return HookResult(ok=True, detail="apply+profile_active OK (no validate window)")

    def _validate_check(parsed: dict[str, Any]) -> tuple[bool, str]:
        passed = bool(parsed.get("all_passed"))
        return (passed, f"all_passed={passed}")

    vr = _poll_agent_until(
        ctx, "validate", _validate_check,
        poll_seconds=poll,
        timeout_seconds=remaining,
    )
    if vr.ok:
        return HookResult(ok=True, detail="apply+validate OK")
    # Validate failure routes to the hook failing — but only if validation
    # was actually reachable. Timeouts here mean "validate never passed within
    # the hook timeout", which IS a failure per spec.
    return HookResult(ok=False, timed_out=vr.timed_out,
                      detail=f"apply OK but validate: {vr.detail}")


def _run_nvmon_restore(cfg: HookConfig, ctx: HookContext) -> HookResult:
    r = _post_to_agent(
        ctx, "restore", None,
        timeout=min(15.0, cfg.timeout_seconds),
        expected_status=[200, 202],
    )
    if not r.ok:
        return r
    deadline_rem = cfg.timeout_seconds - r.elapsed_seconds

    def _check(parsed: dict[str, Any]) -> tuple[bool, str]:
        dd_active = bool(_dotted_path(parsed, "direct_display.active"))
        mosaic_en = bool(_dotted_path(parsed, "mosaic.enabled"))
        done = (not dd_active) and (not mosaic_en)
        return (done, f"dd_active={dd_active} mosaic_enabled={mosaic_en}")

    return _poll_agent_until(
        ctx, "status", _check,
        poll_seconds=float(cfg.params.get("poll_seconds", 2.0)),
        timeout_seconds=max(1.0, deadline_rem),
    )


def _run_nvmon_dd_enable(cfg: HookConfig, ctx: HookContext) -> HookResult:
    return _post_to_agent(
        ctx, "dd/enable", None,
        timeout=cfg.timeout_seconds, expected_status=[200],
    )


def _run_nvmon_dd_disable(cfg: HookConfig, ctx: HookContext) -> HookResult:
    return _post_to_agent(
        ctx, "dd/disable", None,
        timeout=cfg.timeout_seconds, expected_status=[200],
    )


def _run_nvmon_qsync_enable(cfg: HookConfig, ctx: HookContext) -> HookResult:
    body = {
        "source": str(cfg.params.get("source")),
        "trigger": str(cfg.params.get("trigger")),
        "grid": int(cfg.params.get("grid", 1)),
    }
    r = _post_to_agent(
        ctx, "qsync/enable", body,
        timeout=min(30.0, cfg.timeout_seconds), expected_status=[200],
    )
    if not r.ok:
        return r
    # Then poll /qsync.synced == true.
    deadline_rem = cfg.timeout_seconds - r.elapsed_seconds

    def _check(parsed: dict[str, Any]) -> tuple[bool, str]:
        synced = bool(parsed.get("synced"))
        return (synced, f"qsync.synced={synced}")

    return _poll_agent_until(
        ctx, "qsync", _check,
        poll_seconds=float(cfg.params.get("poll_seconds", 2.0)),
        timeout_seconds=max(1.0, deadline_rem),
    )


def _run_nvmon_qsync_disable(cfg: HookConfig, ctx: HookContext) -> HookResult:
    return _post_to_agent(
        ctx, "qsync/disable", None,
        timeout=cfg.timeout_seconds, expected_status=[200],
    )


def _run_nvmon_mosaic_disable(cfg: HookConfig, ctx: HookContext) -> HookResult:
    return _post_to_agent(
        ctx, "mosaic/disable", None,
        timeout=cfg.timeout_seconds, expected_status=[200],
    )


def _run_nvmon_await_status(cfg: HookConfig, ctx: HookContext) -> HookResult:
    conds = cfg.params.get("conditions") or {}

    def _check(parsed: dict[str, Any]) -> tuple[bool, str]:
        unmet = []
        for key, expected in conds.items():
            if not _eval_condition(parsed, key, expected):
                unmet.append(f"{key}!={expected!r}")
        if not unmet:
            return (True, "all conditions met")
        return (False, "; ".join(unmet))

    return _poll_agent_until(
        ctx, "status", _check,
        poll_seconds=float(cfg.params.get("poll_seconds", 2.0)),
        timeout_seconds=cfg.timeout_seconds,
    )


def _run_nvmon_validate(cfg: HookConfig, ctx: HookContext) -> HookResult:
    def _check(parsed: dict[str, Any]) -> tuple[bool, str]:
        passed = bool(parsed.get("all_passed"))
        return (passed, f"all_passed={passed}")

    return _poll_agent_until(
        ctx, "validate", _check,
        poll_seconds=float(cfg.params.get("poll_seconds", 2.0)),
        timeout_seconds=cfg.timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Callable[[HookConfig, HookContext], HookResult]] = {
    "delay": _run_delay,
    "shell_command": _run_shell_command,
    "http_post": _run_http_post,
    "http_get_wait": _run_http_get_wait,
    "nvmon_apply": _run_nvmon_apply,
    "nvmon_restore": _run_nvmon_restore,
    "nvmon_dd_enable": _run_nvmon_dd_enable,
    "nvmon_dd_disable": _run_nvmon_dd_disable,
    "nvmon_qsync_enable": _run_nvmon_qsync_enable,
    "nvmon_qsync_disable": _run_nvmon_qsync_disable,
    "nvmon_mosaic_disable": _run_nvmon_mosaic_disable,
    "nvmon_await_status": _run_nvmon_await_status,
    "nvmon_validate": _run_nvmon_validate,
}


def run_hook(cfg: HookConfig, context: HookContext) -> HookResult:
    """Execute one hook, honoring its retry policy."""
    handler = _HANDLERS.get(cfg.type)
    if handler is None:
        return HookResult(ok=False, detail=f"unknown hook type {cfg.type!r}")

    on_fail = cfg.effective_on_fail
    attempts = 1 + (cfg.max_retries if on_fail == "retry" else 0)
    last: HookResult | None = None
    for attempt in range(attempts):
        if context.cancel_event.is_set():
            return HookResult(ok=False, cancelled=True, detail="cancelled before start")
        try:
            result = handler(cfg, context)
        except Exception as e:  # defensive — handler must never crash the launcher
            result = HookResult(ok=False, detail=f"handler error: {e}")
        last = result
        if result.ok or result.cancelled:
            return result
        if attempt + 1 < attempts:
            if context.logger is not None:
                context.logger.warning(
                    f"[{context.app_name}] hook {cfg.label} failed "
                    f"(attempt {attempt + 1}/{attempts}): {result.detail}; "
                    f"retrying"
                )
            if _cancellable_sleep(context.cancel_event, 1.0):
                return HookResult(ok=False, cancelled=True,
                                  detail="cancelled between retries")
    assert last is not None
    return last


__all__ = [
    "HOOK_TYPES",
    "NVMON_STATUS_CONDITION_KEYS",
    "HookConfig",
    "HookContext",
    "HookResult",
    "hook_from_dict",
    "run_hook",
]
