"""Controller authentication — one shared password, one file.

Per spec (2026-04-17-A), authentication is deliberately minimal:

  - Password file at `controller.password_file` (one line plaintext).
  - If the file is missing or empty, auth is disabled and we log one
    warning line at startup. `is_auth_enabled()` then returns False.
  - If enabled, requests must present either:
        * the `nvmon_session` cookie (HttpOnly, no expiry), OR
        * an `X-NVMON-Password: <plaintext>` header.
  - Cookie value is an HMAC of the password with a random in-memory nonce.
    The nonce resets on controller restart, which invalidates old sessions.
    This is a front-door lock, not crypto-grade auth.

Public surface:
    init(password_file)              — load once at startup
    is_auth_enabled()                — bool
    password_ok(plaintext)           — constant-time compare
    mint_session_cookie()            — value to set as `nvmon_session`
    session_cookie_ok(value)         — verify a presented cookie
    require_auth(handler_method)     — decorator: 401 on failure
    parse_cookies(header_value)      — small cookie-header parser
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Callable

from .. import log


# ---------------------------------------------------------------------------
# Module-level state (set by init())
# ---------------------------------------------------------------------------

_password: str | None = None          # None => auth disabled
_nonce: bytes = b""                   # random per-process; invalidates cookies on restart
_initialized: bool = False

SESSION_COOKIE_NAME = "nvmon_session"
HEADER_NAME = "X-NVMON-Password"


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init(password_file: str | os.PathLike[str]) -> None:
    """Load the password from disk. Call once at controller startup.

    If the file is missing or empty, auth runs in no-auth mode and a single
    warning is logged. Otherwise the password is held in memory and a fresh
    32-byte nonce is generated for the session HMAC.
    """
    global _password, _nonce, _initialized
    logger = log.get()
    _initialized = True

    path = Path(password_file)
    try:
        if not path.is_file():
            logger.warning(
                "[auth] password file not found at %s — controller running WITHOUT authentication",
                path,
            )
            _password = None
            _nonce = b""
            return

        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            logger.warning(
                "[auth] password file %s is empty — controller running WITHOUT authentication",
                path,
            )
            _password = None
            _nonce = b""
            return

        # Take only the first line to tolerate accidental trailing whitespace/newlines.
        _password = raw.splitlines()[0].strip()
        if not _password:
            logger.warning(
                "[auth] password file %s has no usable content — controller running WITHOUT authentication",
                path,
            )
            _password = None
            _nonce = b""
            return

        _nonce = secrets.token_bytes(32)
        logger.info("[auth] password loaded from %s — authentication enabled", path)
    except OSError as exc:
        logger.warning(
            "[auth] failed to read password file %s: %s — controller running WITHOUT authentication",
            path,
            exc,
        )
        _password = None
        _nonce = b""


def init_secret(secret: str) -> None:
    """Enable auth with an in-memory password that never touches disk.

    Used by ``nvmon app``: a random secret per launch, handed to its own
    browser window once via ``/app/enter``. Anything else on the machine
    (another browser tab, a web page POSTing to 127.0.0.1) has no cookie,
    and the SameSite=Lax cookie is not sent on cross-site POSTs.
    """
    global _password, _nonce, _initialized
    _initialized = True
    _password = secret
    _nonce = secrets.token_bytes(32)


def is_auth_enabled() -> bool:
    """Return True if a password is loaded and auth is enforced."""
    return _password is not None


# ---------------------------------------------------------------------------
# Password + cookie primitives
# ---------------------------------------------------------------------------

def password_ok(plaintext: str | None) -> bool:
    """Constant-time compare against the loaded password."""
    if _password is None or plaintext is None:
        return False
    try:
        return hmac.compare_digest(
            _password.encode("utf-8"),
            plaintext.encode("utf-8"),
        )
    except (AttributeError, UnicodeEncodeError):
        return False


def mint_session_cookie() -> str:
    """Return the session cookie value for the currently loaded password.

    Value = hex HMAC-SHA256(nonce, password). Safe to log length of, but
    never log the value itself.
    """
    if _password is None:
        return ""
    mac = hmac.new(_nonce, _password.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def session_cookie_ok(value: str | None) -> bool:
    """Verify a presented cookie value."""
    if _password is None or not value:
        return False
    expected = mint_session_cookie()
    if not expected:
        return False
    return hmac.compare_digest(expected, value)


# ---------------------------------------------------------------------------
# Cookie-header parsing
# ---------------------------------------------------------------------------

def parse_cookies(header_value: str | None) -> dict[str, str]:
    """Parse a `Cookie:` request header into a {name: value} dict.

    Lenient: ignores malformed pairs, collapses duplicates (last wins).
    """
    if not header_value:
        return {}
    out: dict[str, str] = {}
    for part in header_value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            out[name] = value
    return out


# ---------------------------------------------------------------------------
# Request-level check
# ---------------------------------------------------------------------------

def request_is_authorized(handler) -> bool:
    """Return True if a BaseHTTPRequestHandler request carries valid auth.

    Accepts:
        * X-NVMON-Password header matching the loaded password.
        * nvmon_session cookie matching the minted session value.
    """
    if not is_auth_enabled():
        return True

    # Header-based auth (API callers).
    header = handler.headers.get(HEADER_NAME)
    if header is not None and password_ok(header.strip()):
        return True

    # Cookie-based auth (browser).
    cookie_header = handler.headers.get("Cookie")
    cookies = parse_cookies(cookie_header)
    if session_cookie_ok(cookies.get(SESSION_COOKIE_NAME)):
        return True

    return False


def require_auth(method: Callable) -> Callable:
    """Decorator for BaseHTTPRequestHandler methods — 401 if unauthorized.

    Usage:
        @require_auth
        def _handle_all_apply(self, body): ...
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not request_is_authorized(self):
            # Keep response minimal — no password reflection, no auth-method disclosure.
            self._json_response(401, {
                "error": "authentication required",
                "hint": "set X-NVMON-Password header or log in via /login",
            })
            return None
        return method(self, *args, **kwargs)

    return wrapper
