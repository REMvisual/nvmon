"""nvmon app — the monitor as a desktop widget, on any Windows box.

    python -m nvmon app [--discover] [--port N] [--no-window]
    (or double-click nvmon_app.bat)

One process, no install, no admin, nothing beyond the bundled Python:

* Runs its own controller in-process, bound to 127.0.0.1 only, so there is
  no firewall prompt. Its window is signed in automatically with a random
  per-launch secret (/app/enter), so no login form and nothing else on the
  machine can drive it. Nodes come from nvmon.toml; it talks to the
  listeners directly over HTTP, exactly like the monitor box's controller.
* Shows the dashboard in bin/nvmon-window/nvmon-window.exe: a 14 KB
  borderless, rounded window hosting WebView2 (the Edge engine built into
  Windows 10/11; the three WebView2 DLLs ship next to it). The dashboard's
  own header is the title bar: drag it to move (native, Snap works),
  double-click to maximise; opacity / pin-on-top / minimise / maximise /
  close buttons sit at its right end; drag any edge to resize. Opacity
  cycles 100 → 95 → 90 → 80 → 70 %. (Edge's --app windows can't lose
  their title strip from outside — tried — so they are only the fallback
  when nvmon-window.exe is missing.)
* While the dashboard is minimised or closed, a small translucent bubble
  floats on top showing ``online/total`` (green = all up, amber = some
  down, red = none up). Drag it anywhere; click to open the
  dashboard; right-click for Open in browser / Start with Windows / Quit.
* Starts with Windows via a per-user Startup shortcut (no UAC), on by
  default, toggled from the bubble's menu. At logon it comes up as just the
  bubble (``--no-window``). Launching it again just reopens the dashboard.

What it deliberately does NOT do (the monitor box's controller still owns
these): chain sync at boot, accept /register from listeners, or restart
"the monitor". It only watches and sends commands.

State (browser profile, positions, opacity, log): %LOCALAPPDATA%\\nvmon-app.
Everything on screen is plain Win32/GDI through ctypes. No GUI toolkit.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes as wt
from pathlib import Path
from typing import Sequence

APP_PORT = 9995
BUBBLE_CLASS = "nvmonAppBubble"
MUTEX_NAME = "Local\\nvmon-app"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "nvmon-app"
STATE_FILE = STATE_DIR / "state.json"
STARTUP_LNK = (Path(os.environ.get("APPDATA") or Path.home())
               / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "nvmon app.lnk")
CREATE_NO_WINDOW = 0x08000000
OPACITY_STEPS = (1.0, 0.95, 0.9, 0.8, 0.7)
DEFAULT_OPACITY = 0.95
BUBBLE_DIP = 92       # floating widget diameter at 100 % scaling
WM_APP_HOST_SHOW = 0x8005   # nvmon-window.exe: come back from collapsed/minimised
VK_LBUTTON = 0x01

# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD),
                ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD)]


def _sig(dll, name, res, *args):
    fn = getattr(dll, name)
    fn.restype = res
    fn.argtypes = list(args)
    return fn


H, U, I, P = wt.HANDLE, wt.UINT, ctypes.c_int, ctypes.c_void_p
PRECT, PPOINT = ctypes.POINTER(wt.RECT), ctypes.POINTER(wt.POINT)
DefWindowProcW = _sig(user32, "DefWindowProcW", LRESULT, wt.HWND, U, wt.WPARAM, wt.LPARAM)
RegisterClassW = _sig(user32, "RegisterClassW", wt.ATOM, ctypes.POINTER(WNDCLASSW))
CreateWindowExW = _sig(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                       wt.DWORD, I, I, I, I, wt.HWND, H, wt.HINSTANCE, P)
DestroyWindow = _sig(user32, "DestroyWindow", wt.BOOL, wt.HWND)
GetMessageW = _sig(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, U, U)
TranslateMessage = _sig(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(wt.MSG))
DispatchMessageW = _sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wt.MSG))
PostQuitMessage = _sig(user32, "PostQuitMessage", None, I)
PostMessageW = _sig(user32, "PostMessageW", wt.BOOL, wt.HWND, U, wt.WPARAM, wt.LPARAM)
FindWindowW = _sig(user32, "FindWindowW", wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
ShowWindow = _sig(user32, "ShowWindow", wt.BOOL, wt.HWND, I)
SetForegroundWindow = _sig(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
GetForegroundWindow = _sig(user32, "GetForegroundWindow", wt.HWND)
AllowSetForegroundWindow = _sig(user32, "AllowSetForegroundWindow", wt.BOOL, wt.DWORD)
IsWindow = _sig(user32, "IsWindow", wt.BOOL, wt.HWND)
IsWindowVisible = _sig(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
IsIconic = _sig(user32, "IsIconic", wt.BOOL, wt.HWND)
SetWindowPos = _sig(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, I, I, I, I, U)
GetWindowRect = _sig(user32, "GetWindowRect", wt.BOOL, wt.HWND, PRECT)
GetClientRect = _sig(user32, "GetClientRect", wt.BOOL, wt.HWND, PRECT)
GetWindowLongPtrW = _sig(user32, "GetWindowLongPtrW", LONG_PTR, wt.HWND, I)
SetWindowLongPtrW = _sig(user32, "SetWindowLongPtrW", LONG_PTR, wt.HWND, I, LONG_PTR)
SetLayeredWindowAttributes = _sig(user32, "SetLayeredWindowAttributes", wt.BOOL, wt.HWND,
                                  wt.DWORD, ctypes.c_ubyte, wt.DWORD)
UpdateLayeredWindow = _sig(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HDC, PPOINT,
                           ctypes.POINTER(wt.SIZE), wt.HDC, PPOINT, wt.DWORD,
                           ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
GetCursorPos = _sig(user32, "GetCursorPos", wt.BOOL, PPOINT)
ScreenToClient = _sig(user32, "ScreenToClient", wt.BOOL, wt.HWND, PPOINT)
SetCapture = _sig(user32, "SetCapture", wt.HWND, wt.HWND)
ReleaseCapture = _sig(user32, "ReleaseCapture", wt.BOOL)
TrackMouseEvent = _sig(user32, "TrackMouseEvent", wt.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
SetTimer = _sig(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, U, P)
InvalidateRect = _sig(user32, "InvalidateRect", wt.BOOL, wt.HWND, P, wt.BOOL)
BeginPaint = _sig(user32, "BeginPaint", wt.HDC, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
EndPaint = _sig(user32, "EndPaint", wt.BOOL, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
DrawTextW = _sig(user32, "DrawTextW", I, wt.HDC, wt.LPCWSTR, I, PRECT, U)
FillRect = _sig(user32, "FillRect", I, wt.HDC, PRECT, H)
SetWindowRgn = _sig(user32, "SetWindowRgn", I, wt.HWND, H, wt.BOOL)
LoadCursorW = _sig(user32, "LoadCursorW", H, wt.HINSTANCE, P)
CreatePopupMenu = _sig(user32, "CreatePopupMenu", H)
AppendMenuW = _sig(user32, "AppendMenuW", wt.BOOL, H, U, ctypes.c_size_t, wt.LPCWSTR)
TrackPopupMenu = _sig(user32, "TrackPopupMenu", I, H, U, I, I, I, wt.HWND, P)
DestroyMenu = _sig(user32, "DestroyMenu", wt.BOOL, H)
EnumWindows = _sig(user32, "EnumWindows", wt.BOOL, WNDENUMPROC, wt.LPARAM)
GetWindowThreadProcessId = _sig(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND,
                                ctypes.POINTER(wt.DWORD))
GetClassNameW = _sig(user32, "GetClassNameW", I, wt.HWND, wt.LPWSTR, I)
GetWindowTextW = _sig(user32, "GetWindowTextW", I, wt.HWND, wt.LPWSTR, I)
GetSystemMetrics = _sig(user32, "GetSystemMetrics", I, I)
SystemParametersInfoW = _sig(user32, "SystemParametersInfoW", wt.BOOL, U, U, P, U)
MessageBoxW = _sig(user32, "MessageBoxW", I, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, U)
MonitorFromWindow = _sig(user32, "MonitorFromWindow", H, wt.HWND, wt.DWORD)
GetMonitorInfoW = _sig(user32, "GetMonitorInfoW", wt.BOOL, H, ctypes.POINTER(MONITORINFO))
GetDC = _sig(user32, "GetDC", wt.HDC, wt.HWND)
ReleaseDC = _sig(user32, "ReleaseDC", I, wt.HWND, wt.HDC)
CreateEllipticRgn = _sig(gdi32, "CreateEllipticRgn", H, I, I, I, I)
CreateRoundRectRgn = _sig(gdi32, "CreateRoundRectRgn", H, I, I, I, I, I, I)
CreateRectRgn = _sig(gdi32, "CreateRectRgn", H, I, I, I, I)
CombineRgn = _sig(gdi32, "CombineRgn", I, H, H, H, I)
CreateSolidBrush = _sig(gdi32, "CreateSolidBrush", H, wt.DWORD)
SelectObject = _sig(gdi32, "SelectObject", H, wt.HDC, H)
DeleteObject = _sig(gdi32, "DeleteObject", wt.BOOL, H)
Ellipse = _sig(gdi32, "Ellipse", wt.BOOL, wt.HDC, I, I, I, I)
SetBkMode = _sig(gdi32, "SetBkMode", I, wt.HDC, I)
SetTextColor = _sig(gdi32, "SetTextColor", wt.DWORD, wt.HDC, wt.DWORD)
CreateFontW = _sig(gdi32, "CreateFontW", H, I, I, I, I, I, wt.DWORD, wt.DWORD, wt.DWORD,
                   wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR)
CreateCompatibleDC = _sig(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
CreateCompatibleBitmap = _sig(gdi32, "CreateCompatibleBitmap", H, wt.HDC, I, I)
CreateDIBSection = _sig(gdi32, "CreateDIBSection", H, wt.HDC, ctypes.POINTER(BITMAPINFOHEADER),
                        U, ctypes.POINTER(P), H, wt.DWORD)
DeleteDC = _sig(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
GetStockObject = _sig(gdi32, "GetStockObject", H, I)
IsZoomed = _sig(user32, "IsZoomed", wt.BOOL, wt.HWND)
GetAsyncKeyState =_sig(user32, "GetAsyncKeyState", ctypes.c_short, I)
KillTimer = _sig(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)
GetWindowRgnBox = _sig(user32, "GetWindowRgnBox", I, wt.HWND, PRECT)
BitBlt = _sig(gdi32, "BitBlt", wt.BOOL, wt.HDC, I, I, I, I, wt.HDC, I, I, wt.DWORD)
GetModuleHandleW = _sig(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
CreateMutexW = _sig(kernel32, "CreateMutexW", H, P, wt.BOOL, wt.LPCWSTR)
try:
    GetDpiForWindow = _sig(user32, "GetDpiForWindow", U, wt.HWND)
except AttributeError:  # pre-Win10 1607
    GetDpiForWindow = None

WM_DESTROY, WM_PAINT, WM_CLOSE, WM_ERASEBKGND = 0x0002, 0x000F, 0x0010, 0x0014
WM_TIMER, WM_MOUSEMOVE, WM_MOUSELEAVE = 0x0113, 0x0200, 0x02A3
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0201, 0x0202, 0x0203, 0x0205
WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0021, 3
WM_APP_SHOW, WM_APP_DRAG = 0x8001, 0x8002
DRAG_TIMER = 3
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x8, 0x80, 0x80000, 0x8000000
GWL_EXSTYLE = -20
CS_DBLCLKS = 0x0008
SW_HIDE, SW_MAXIMIZE, SW_MINIMIZE, SW_SHOW, SW_SHOWNA, SW_RESTORE = 0, 3, 6, 5, 8, 9
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
HWND_TOP, HWND_TOPMOST, HWND_NOTOPMOST = wt.HWND(0), wt.HWND(-1), wt.HWND(-2)
MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
DT_LEFT, DT_CENTER, DT_VCENTER, DT_SINGLELINE, DT_NOPREFIX = 0x0, 0x1, 0x4, 0x20, 0x800
LWA_ALPHA, ULW_ALPHA, SRCCOPY = 0x2, 0x2, 0x00CC0020
TME_LEAVE = 0x2
RGN_AND = 1


def _rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)


def _channels(c: int) -> tuple[int, int, int]:
    return c & 255, (c >> 8) & 255, (c >> 16) & 255


GREY, GREEN, AMBER, RED = _rgb(110, 118, 129), _rgb(63, 185, 80), _rgb(210, 153, 34), _rgb(248, 81, 73)
BG, BAR_BG, BAR_LINE = _rgb(10, 10, 10), _rgb(17, 17, 17), _rgb(38, 38, 38)
TEXT, TEXT_DIM, HOVER, CLOSE_HOVER = _rgb(230, 237, 243), _rgb(139, 148, 158), _rgb(45, 45, 45), _rgb(196, 43, 28)
WHITE = _rgb(255, 255, 255)


def _font(px: int, weight: int = 400, face: str = "Segoe UI") -> int:
    # quality 5 = CLEARTYPE (opaque bar); the bubble asks for 4 = ANTIALIASED
    return CreateFontW(-px, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or corrupt: start fresh
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.is_file() else exe)


def _cursor() -> tuple[int, int]:
    pt = wt.POINT()
    GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _rect(hwnd) -> wt.RECT:  # noqa: ANN001
    r = wt.RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _dpi(hwnd) -> int:  # noqa: ANN001
    try:
        return int(GetDpiForWindow(hwnd)) or 96 if GetDpiForWindow else 96
    except Exception:  # noqa: BLE001
        return 96


def _find_browser() -> str | None:
    """Edge first (ships with Windows 10/11), then Chrome."""
    import winreg
    for exe in ("msedge.exe", "chrome.exe"):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                    path = winreg.QueryValue(k, None)
                if path and Path(path).is_file():
                    return path
            except OSError:
                pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    return None


def make_shortcut(lnk: Path, *, no_window: bool = False) -> bool:
    """Write a .lnk that starts `nvmon app` from this NVMON copy (pythonw,
    no console) with the nvmon icon. Used for the Startup entry and by
    `python -m nvmon app --shortcuts` (desktop + NVMON folder)."""
    from . import config as cfgmod
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:NV_LNK);"
        "$s.TargetPath=$env:NV_EXE;$s.Arguments=$env:NV_ARGS;"
        "$s.WorkingDirectory=$env:NV_DIR;$s.Description='nvmon — fleet dashboard + sync widget';"
        "if (Test-Path $env:NV_ICO) { $s.IconLocation=$env:NV_ICO + ',0' };"
        "$s.Save()"
    )
    env = dict(os.environ, NV_LNK=str(lnk), NV_EXE=_pythonw(), NV_DIR=str(cfgmod.BASE_DIR),
               NV_ARGS="-m nvmon app" + (" --no-window" if no_window else ""),
               NV_ICO=str(cfgmod.BASE_DIR / "bin" / "nvmon-window" / "nvmon.ico"))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       env=env, creationflags=CREATE_NO_WINDOW, timeout=30,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass
    return lnk.exists()


def _set_startup(enabled: bool) -> bool:
    """Create/remove the per-user Startup shortcut. Returns the new state."""
    if not enabled:
        try:
            STARTUP_LNK.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return True
        return False
    return make_shortcut(STARTUP_LNK, no_window=True)


def _fmt_rate(hz: float) -> str:
    if abs(hz - round(hz)) < 0.01:
        return str(int(round(hz)))
    return f"{hz:.2f}".rstrip("0").rstrip(".")


def _status(aggregator) -> tuple[tuple[str, str, str], int]:  # noqa: ANN001
    """What the floating widget shows: ((count, fps, depth), ring colour).

    Ring = fleet sync, read exactly like the dashboard's QSYNC column:
    green when every node is online and QSync OK, red when a node is
    offline or any QSync is CRIT, amber for anything in between (warn, or
    sync off). fps / depth are the fleet's common refresh rate and colour
    depth, or "mix" when nodes disagree."""
    from .controller.dashboard_unified import view_model as vm
    snap = aggregator.snapshot()
    nodes = snap.get("nodes") or []
    up, levels, rates, depths = 0, [], set(), set()
    for n in nodes:
        st = n.get("status")
        if not n.get("reachable") or not isinstance(st, dict):
            levels.append("crit")
            continue
        up += 1
        levels.append(vm._qsync_view(st).get("level", "off"))
        hz = vm._refresh_hz(st)
        if hz:
            rates.add(round(float(hz), 2))
        ct = vm._color_text(st)
        if ct:
            depths.add(ct.split()[0])
    count = f"{up}/{len(nodes)}"
    fps = "—" if not rates else ("mix" if len(rates) > 1 else _fmt_rate(next(iter(rates))))
    depth = "" if not depths else ("mixed" if len(depths) > 1 else next(iter(depths)))
    if not nodes or not snap.get("last_update"):
        color = GREY
    elif all(lv == "ok" for lv in levels):
        color = GREEN
    elif "crit" in levels:
        color = RED
    else:
        color = AMBER
    return (count, fps, depth), color


def _register_class(name: str, proc, style: int = 0, cursor: int = 32512) -> None:  # noqa: ANN001
    wc = WNDCLASSW(style=style, lpfnWndProc=proc, hInstance=GetModuleHandleW(None),
                   hCursor=LoadCursorW(None, ctypes.c_void_p(cursor)), lpszClassName=name)
    RegisterClassW(ctypes.byref(wc))


def _safe(fn):  # noqa: ANN001
    """Never let a Python exception escape a WNDPROC (it would kill the app)."""
    def wrapper(hwnd, msg, wp, lp):  # noqa: ANN001
        try:
            r = fn(hwnd, msg, wp, lp)
            if r is not None:
                return r
        except Exception:  # noqa: BLE001
            from . import log as logmod
            logmod.get().exception("[app] window message %#x failed", msg)
        return DefWindowProcW(hwnd, msg, wp, lp)
    return WNDPROC(wrapper)


# ---------------------------------------------------------------------------
# In-process controller (loopback only)
# ---------------------------------------------------------------------------

def start_controller(port: int, discover: bool, secret: str, app_hook):  # noqa: ANN001, ANN201
    """Boot registry + aggregator + fan-out + HTTP on 127.0.0.1.

    Returns (server, registry, aggregator, disc_registry)."""
    from . import config as cfgmod
    from . import log as logmod
    from .controller import auth, selfrestart
    from .controller import server as servermod
    from .controller.aggregator import Aggregator
    from .controller.dashboard_unified import render
    from .controller.fanout import FanoutExecutor
    from .controller.main import start_discovery
    from .controller.registry import NodeRegistry
    from .controller.server import ControllerServer

    logger = logmod.get()
    ctrl = cfgmod.get_controller_config()

    # Loopback-only server, and still locked: a random secret per launch
    # that only our own window receives (via /app/enter). A web page the
    # user opens elsewhere can't drive the fleet through 127.0.0.1.
    auth.init_secret(secret)
    servermod.APP_HOOK = app_hook
    render.get_env().globals["app_mode"] = True   # header = title bar + window buttons
    # Assets are cache-busted by version only; the app's WebView2 profile
    # outlives code edits between version bumps, so add a per-launch tag.
    render.get_env().globals["cache_bust"] = f"-{int(time.time())}"
    selfrestart.DISABLED_REASON = (
        "this is the nvmon desktop app, not the monitor box controller; "
        "restart it on the monitor box (run_monitor.bat) instead"
    )

    registry = NodeRegistry(nodes_cfg=ctrl["nodes"], groups_cfg=ctrl["groups"])
    # Read the monitor's push-registered nodes when this copy has them, but
    # never write back into secrets\ (that file belongs to the real monitor).
    try:
        registry.load_registered_from_file(cfgmod.SECRETS_DIR / "registered_nodes.json")
    except Exception:  # noqa: BLE001
        pass

    workers = int(ctrl.get("poll_workers", 0) or 0)
    if workers <= 0:
        workers = min(cfgmod.MAX_POLL_WORKERS, max(4, registry.count()))
    aggregator = Aggregator(
        registry,
        poll_interval_seconds=int(ctrl["poll_interval_seconds"]),
        fanout_timeout_seconds=int(ctrl.get("poll_timeout_seconds", 5)),
        max_workers=workers,
    )
    fanout = FanoutExecutor(registry, timeout_seconds=int(ctrl["fanout_timeout_seconds"]))

    disc = start_discovery(registry, aggregator, logger) if discover else None
    aggregator.start()

    server = None
    for p in ([port] if not _port_busy(port) else []) + [0]:
        try:
            server = ControllerServer(
                bind_host="127.0.0.1", port=p, registry=registry, aggregator=aggregator,
                fanout=fanout, disc_registry=disc,
                registered_file=STATE_DIR / "registered_nodes.json", chainsync=None,
            )
            break
        except OSError as exc:
            logger.warning("[app] bind 127.0.0.1:%d failed: %s", p, exc)
    if server is None:
        raise RuntimeError("could not bind a local port for the dashboard")
    server.serve_forever_in_thread()
    logger.info("[app] nvmon app: %d node(s), dashboard http://127.0.0.1:%d/  discovery=%s",
                registry.count(), server.port, "on" if disc else "off")
    return server, registry, aggregator, disc


# ---------------------------------------------------------------------------
# Dashboard window
# ---------------------------------------------------------------------------

def _host_exe() -> Path | None:
    """bin/nvmon-window/nvmon-window.exe: frameless WebView2 window (see its
    src/). None if it or its WebView2 DLLs are missing from this copy."""
    from . import config as cfgmod
    d = cfgmod.BASE_DIR / "bin" / "nvmon-window"
    exe = d / "nvmon-window.exe"
    need = ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll",
            "WebView2Loader.dll")
    return exe if exe.is_file() and all((d / n).is_file() for n in need) else None


class DashboardWindow:
    """Owns the window process the dashboard is shown in.

    Normally ``nvmon-window.exe``: a borderless, rounded WebView2 window that
    handles drag / resize / pin / opacity itself (the page header is its
    title bar). Fallback when that is missing: a plain Edge/Chrome --app
    window; the header buttons then reach :meth:`command` through POST
    /app/<cmd>, and dragging the header moves it via :meth:`start_drag`."""

    def __init__(self, base_url: str, secret: str, state: dict) -> None:
        self.base_url, self.secret, self.state = base_url, secret, state
        self.host = _host_exe()
        self.browser = None if self.host else _find_browser()
        self.proc: subprocess.Popen | None = None
        self._hwnd = None
        self._adopted = None          # Edge fallback: hwnd we applied alpha/pin to
        self._lock = threading.Lock()
        self.ui_hwnd = None           # the bubble: runs drags on the UI thread
        self._drag = None             # (cursor x, y, window x, y) while dragging

    @property
    def url(self) -> str:
        return self.base_url

    def _enter_url(self) -> str:
        return f"{self.base_url}app/enter?t={self.secret}"

    # -- process / window discovery --
    def launch(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return  # already starting; the window shows up in a moment
        if self.host:
            args = [str(self.host), "--url", self._enter_url(),
                    "--data", str(STATE_DIR / "window"), "--title", "nvmon"]
        elif self.browser:
            args = [self.browser, f"--app={self._enter_url()}",
                    f"--user-data-dir={STATE_DIR / 'browser'}",
                    "--no-first-run", "--no-default-browser-check",
                    "--hide-crash-restore-bubble", "--window-size=1280,760"]
        else:
            webbrowser.open(self._enter_url())
            return
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def hwnd(self):  # noqa: ANN201
        """The dashboard's top-level window, or None if it is not open.

        A collapsed nvmon-window is hidden but still counts (it is shown
        again with WM_APP_HOST_SHOW); Edge windows must be visible."""
        alive = self.proc is not None and self.proc.poll() is None
        if self._hwnd and IsWindow(self._hwnd) and (
                IsWindowVisible(self._hwnd) or (self.host and alive)):
            return self._hwnd
        self._hwnd = None
        pid = self.proc.pid if alive else None
        found = []
        buf = ctypes.create_unicode_buffer(256)

        def _cb(h, _lp):  # noqa: ANN001
            GetClassNameW(h, buf, 256)
            cls = buf.value
            host_form = cls.startswith("WindowsForms10.Window")
            if cls != "Chrome_WidgetWin_1" and not host_form:
                return True
            if not IsWindowVisible(h) and not host_form:
                return True
            GetWindowTextW(h, buf, 256)
            title = buf.value
            owner = wt.DWORD()
            GetWindowThreadProcessId(h, ctypes.byref(owner))
            if pid is not None and owner.value == pid:
                # WinForms also owns an untitled hidden "parking" window.
                if host_form and not title:
                    return True
                found.insert(0, h)
            elif cls == "Chrome_WidgetWin_1" and title.startswith("nvmon") and " - " not in title:
                # Edge fallback whose launcher process handed off: app
                # windows carry the bare page title; browser tabs end in
                # " - Microsoft Edge" / " - Google Chrome".
                found.append(h)
            return True

        EnumWindows(WNDENUMPROC(_cb), 0)
        self._hwnd = found[0] if found else None
        return self._hwnd

    def is_shown(self) -> bool:
        h = self.hwnd()
        return bool(h) and bool(IsWindowVisible(h)) and not IsIconic(h)

    def show(self) -> None:
        h = self.hwnd()
        if not h:
            self.launch()
            return
        if self.host:
            # Let the host take the foreground, then ask it to come back
            # itself (so WinForms' own Visible state stays right).
            if self.proc is not None:
                AllowSetForegroundWindow(self.proc.pid)
            PostMessageW(h, WM_APP_HOST_SHOW, 0, 0)
            return
        ShowWindow(h, SW_RESTORE if IsIconic(h) else SW_SHOW)
        SetForegroundWindow(h)

    def close(self) -> None:
        h = self.hwnd()
        if h:
            PostMessageW(h, WM_CLOSE, 0, 0)  # lets the window save size/position
        if self.proc is not None:
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # -- Edge fallback only: opacity / pin / drag from outside --
    @property
    def opacity(self) -> float:
        try:
            return min(1.0, max(0.3, float(self.state.get("opacity", DEFAULT_OPACITY))))
        except (TypeError, ValueError):
            return DEFAULT_OPACITY

    @property
    def pinned(self) -> bool:
        return bool(self.state.get("pinned", False))

    def _apply_alpha(self, h) -> None:  # noqa: ANN001
        ex = GetWindowLongPtrW(h, GWL_EXSTYLE)
        if not ex & WS_EX_LAYERED:
            SetWindowLongPtrW(h, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        SetLayeredWindowAttributes(h, 0, round(255 * self.opacity), LWA_ALPHA)

    def _apply_pin(self, h) -> None:  # noqa: ANN001
        SetWindowPos(h, HWND_TOPMOST if self.pinned else HWND_NOTOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def track(self) -> None:
        """Edge fallback: apply the saved opacity/pin to a fresh window."""
        h = self._hwnd
        if self.host or not (h and IsWindow(h)) or h == self._adopted:
            return
        with self._lock:
            self._adopted = h
            self._apply_alpha(h)
            self._apply_pin(h)

    def start_drag(self) -> None:
        h = self._hwnd
        if not h or self.host:
            return
        if IsZoomed(h):
            ShowWindow(h, SW_RESTORE)
        cx, cy = _cursor()
        r = _rect(h)
        self._drag = (cx, cy, r.left, r.top)

    def drag_tick(self) -> bool:
        """Follow the cursor; returns False once the button is released."""
        if not self._drag or not self._hwnd or not (GetAsyncKeyState(VK_LBUTTON) & 0x8000):
            self._drag = None
            return False
        cx, cy = _cursor()
        SetWindowPos(self._hwnd, None, self._drag[2] + cx - self._drag[0],
                     self._drag[3] + cy - self._drag[1], 0, 0,
                     SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return True

    def command(self, cmd: str) -> dict:
        """POST /app/<cmd> from the page header (HTTP thread, Edge fallback)."""
        h = self.hwnd()
        if h and not self.host:
            if cmd == "drag" and self.ui_hwnd:
                PostMessageW(self.ui_hwnd, WM_APP_DRAG, 0, 0)
            elif cmd == "max":
                ShowWindow(h, SW_RESTORE if IsZoomed(h) else SW_MAXIMIZE)
            elif cmd in ("min", "collapse"):   # Edge can't hide cleanly: minimise
                ShowWindow(h, SW_MINIMIZE)
            elif cmd == "close":
                PostMessageW(h, WM_CLOSE, 0, 0)
            elif cmd == "pin":
                self.state["pinned"] = not self.pinned
                _save_state(self.state)
                self._apply_pin(h)
            elif cmd == "opacity":
                cur = self.opacity
                self.state["opacity"] = next((o for o in OPACITY_STEPS if o < cur - 0.001),
                                             OPACITY_STEPS[0])
                _save_state(self.state)
                self._apply_alpha(h)
        return {"pinned": self.pinned, "opacity": self.opacity,
                "maximised": bool(h and IsZoomed(h))}


# ---------------------------------------------------------------------------
# Floating bubble (per-pixel alpha, anti-aliased)
# ---------------------------------------------------------------------------

class Bubble:
    MENU_OPEN, MENU_BROWSER, MENU_STARTUP, MENU_QUIT = 1, 2, 3, 4

    def __init__(self, dash: DashboardWindow, registry, aggregator, state: dict) -> None:  # noqa: ANN001
        self.dash, self.registry, self.aggregator, self.state = dash, registry, aggregator, state
        self.hwnd = None
        self.size = BUBBLE_DIP
        self.drag = None
        self.moved = False
        self.label, self.color = ("", "", ""), -1
        self.visible = False
        self._proc_ref = _safe(self._proc)

    def create(self) -> None:
        dpi = 96
        try:
            dpi = int(user32.GetDpiForSystem())
        except Exception:  # noqa: BLE001
            pass
        self.size = round(BUBBLE_DIP * dpi / 96)
        _register_class(BUBBLE_CLASS, self._proc_ref, cursor=32649)  # IDC_HAND
        x, y = self._initial_pos()
        self.hwnd = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED, BUBBLE_CLASS,
                                    "nvmon", WS_POPUP, x, y, self.size, self.size, None, None,
                                    GetModuleHandleW(None), None)
        self.dash.ui_hwnd = self.hwnd
        self._refresh()
        SetTimer(self.hwnd, 1, 400, None)   # status + visibility
        SetTimer(self.hwnd, 2, 30, None)    # keep the dashboard window skinned

    def _initial_pos(self) -> tuple[int, int]:
        vx, vy = GetSystemMetrics(76), GetSystemMetrics(77)          # virtual screen
        vw, vh = GetSystemMetrics(78), GetSystemMetrics(79)
        pos = self.state.get("bubble")
        if isinstance(pos, list) and len(pos) == 2:
            x, y = int(pos[0]), int(pos[1])
            if vx <= x <= vx + vw - self.size and vy <= y <= vy + vh - self.size:
                return x, y
        work = wt.RECT()
        SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)       # SPI_GETWORKAREA
        return work.right - self.size - 24, work.bottom - self.size - 24

    def _badge(self) -> tuple[float, float, float]:
        """Centre x, y and radius of the little "reopen" button (top-right)."""
        s = self.size
        return s * 0.83, s * 0.17, s * 0.16

    def _render(self) -> None:
        """Draw the widget into a 32-bit premultiplied DIB and push it.

        Layout: ring = fleet sync colour; inside, ``7/7`` (dim), the
        refresh rate big, the colour depth under it; a small reopen badge
        overlaps the ring top-right."""
        s = self.size
        count, fps, depth = self.label
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=s, biHeight=-s,
                               biPlanes=1, biBitCount=32, biCompression=0)
        bits = ctypes.c_void_p()
        screen = GetDC(None)
        hdc = CreateCompatibleDC(screen)
        dib = CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = SelectObject(hdc, dib)
        px = (ctypes.c_uint32 * (s * s)).from_address(bits.value)
        # 1) text coverage mask (GDI cannot write alpha): grey level = how
        #    strongly the text colour covers the pixel, so dim lines are grey.
        SetBkMode(hdc, 1)
        bx, by, br = self._badge()

        def text(t: str, px_h: float, weight: int, grey: int, top: float, bottom: float,
                 face: str = "Segoe UI", rect: tuple | None = None) -> None:
            f = CreateFontW(-max(6, round(px_h)), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 4, 0, face)
            o = SelectObject(hdc, f)
            SetTextColor(hdc, _rgb(grey, grey, grey))
            rc = wt.RECT(*(rect or (0, round(top), s, round(bottom))))
            DrawTextW(hdc, t, -1, ctypes.byref(rc), DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
            SelectObject(hdc, o)
            DeleteObject(f)

        big = s * (0.30 if len(fps) <= 3 else 0.22)
        text(count, s * 0.12, 600, 150, s * 0.17, s * 0.33)
        text(fps, big, 700, 255, s * 0.30, s * 0.64)
        text(depth, s * 0.13, 600, 215, s * 0.62, s * 0.78)
        text("", br * 0.95, 400, 230, 0, 0, "Segoe MDL2 Assets",
             (round(bx - br), round(by - br), round(bx + br), round(by + br)))
        mask = [p & 0xFF for p in px]
        # 2) disc + status ring + badge, anti-aliased by distance
        fr, fg, fb = _channels(BG)
        rr, rg, rb = _channels(self.color)
        tr, tg, tb = _channels(TEXT)
        kr, kg, kb = 44, 48, 56          # badge fill
        c = (s - 1) / 2
        radius = s / 2 - 1
        ring = max(2.5, s / 22)
        for y in range(s):
            for x in range(s):
                i = y * s + x
                d = math.hypot(x - c, y - c)
                edge = min(1.0, max(0.0, radius - d + 0.5))
                db = math.hypot(x - bx, y - by)
                bedge = min(1.0, max(0.0, br - db + 0.5))
                if edge <= 0 and bedge <= 0:
                    px[i] = 0
                    continue
                t = min(1.0, max(0.0, d - (radius - ring) + 0.5))   # 0 = disc, 1 = ring
                r_ = fr + (rr - fr) * t
                g_ = fg + (rg - fg) * t
                b_ = fb + (rb - fb) * t
                a = (0.82 + 0.18 * t) * edge
                if bedge > 0:   # badge sits on top, with a thin dark outline
                    outline = min(1.0, max(0.0, db - (br - 1.6) + 0.5))
                    cr_, cg_, cb_ = (kr + (10 - kr) * outline, kg + (10 - kg) * outline,
                                     kb + (10 - kb) * outline)
                    r_, g_, b_ = r_ + (cr_ - r_) * bedge, g_ + (cg_ - g_) * bedge, b_ + (cb_ - b_) * bedge
                    a = a + (1 - a) * bedge
                k = mask[i] / 255.0
                if k:
                    r_, g_, b_ = r_ + (tr - r_) * k, g_ + (tg - g_) * k, b_ + (tb - b_) * k
                    a = max(a, k * max(edge, bedge))
                px[i] = (int(a * 255) << 24 | int(r_ * a) << 16 | int(g_ * a) << 8 | int(b_ * a))
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        UpdateLayeredWindow(self.hwnd, screen, None, ctypes.byref(wt.SIZE(s, s)), hdc,
                            ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
        SelectObject(hdc, old)
        DeleteObject(dib)
        DeleteDC(hdc)
        ReleaseDC(None, screen)

    def _refresh(self) -> None:
        label, color = _status(self.aggregator)
        if (label, color) != (self.label, self.color):
            self.label, self.color = label, color
            self._render()
        self.dash.hwnd()  # re-discover the window if it came back / went away
        want = not self.dash.is_shown()
        if want != self.visible:
            self.visible = want
            ShowWindow(self.hwnd, SW_SHOWNA if want else SW_HIDE)
            if want:
                SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _menu(self) -> None:
        startup = STARTUP_LNK.exists()
        m = CreatePopupMenu()
        count, fps, depth = self.label
        sync = {GREEN: "in sync", AMBER: "check sync", RED: "OUT OF SYNC"}.get(self.color, "waiting")
        AppendMenuW(m, MF_STRING | MF_GRAYED, 0,
                    f"nvmon — {count} online · {sync} · {fps} fps {depth}".strip())
        AppendMenuW(m, MF_SEPARATOR, 0, None)
        AppendMenuW(m, MF_STRING, self.MENU_OPEN, "Open dashboard")
        AppendMenuW(m, MF_STRING, self.MENU_BROWSER, "Open in browser")
        AppendMenuW(m, MF_SEPARATOR, 0, None)
        AppendMenuW(m, MF_STRING | (MF_CHECKED if startup else 0), self.MENU_STARTUP, "Start with Windows")
        AppendMenuW(m, MF_SEPARATOR, 0, None)
        AppendMenuW(m, MF_STRING, self.MENU_QUIT, "Quit")
        x, y = _cursor()
        SetForegroundWindow(self.hwnd)  # required so the menu closes on outside click
        cmd = TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD, x, y, 0, self.hwnd, None)
        DestroyMenu(m)
        if cmd == self.MENU_OPEN:
            self.dash.show()
        elif cmd == self.MENU_BROWSER:
            webbrowser.open(self.dash.url)
        elif cmd == self.MENU_STARTUP:
            self.state["startup"] = _set_startup(not startup)
            _save_state(self.state)
        elif cmd == self.MENU_QUIT:
            DestroyWindow(self.hwnd)

    def _proc(self, hwnd, msg, wp, lp):  # noqa: ANN001
        if msg == WM_TIMER:
            if wp == DRAG_TIMER:
                if not self.dash.drag_tick():
                    KillTimer(hwnd, DRAG_TIMER)
            elif wp == 2:
                self.dash.track()
            else:
                self._refresh()
            return 0
        if msg == WM_APP_DRAG:  # header pressed in the dashboard page
            self.dash.start_drag()
            SetTimer(hwnd, DRAG_TIMER, 8, None)
            return 0
        if msg == WM_LBUTTONDOWN:
            cx, cy = _cursor()
            r = _rect(hwnd)
            self.drag, self.moved = (cx, cy, r.left, r.top), False
            SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self.drag:
            cx, cy = _cursor()
            dx, dy = cx - self.drag[0], cy - self.drag[1]
            if self.moved or abs(dx) > 3 or abs(dy) > 3:
                self.moved = True
                SetWindowPos(hwnd, None, self.drag[2] + dx, self.drag[3] + dy, 0, 0,
                             SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            return 0
        if msg == WM_LBUTTONUP and self.drag:
            self.drag = None
            ReleaseCapture()
            if self.moved:
                r = _rect(hwnd)
                self.state["bubble"] = [r.left, r.top]
                _save_state(self.state)
            else:
                self.dash.show()
            return 0
        if msg == WM_RBUTTONUP:
            self._menu()
            return 0
        if msg == WM_APP_SHOW:  # a second launch asked us to open up
            self.dash.show()
            return 0
        if msg == WM_DESTROY:
            PostQuitMessage(0)
            return 0
        return None

    def run(self) -> None:
        msg = wt.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _hand_off_to_running_instance() -> bool:
    """If the app is already running, ask it to open the dashboard."""
    CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return False
    for _ in range(20):  # the first instance may still be booting
        h = FindWindowW(BUBBLE_CLASS, None)
        if h:
            pid = wt.DWORD()
            GetWindowThreadProcessId(h, ctypes.byref(pid))
            AllowSetForegroundWindow(pid.value)
            PostMessageW(h, WM_APP_SHOW, 0, 0)
            break
        time.sleep(0.25)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nvmon app", description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=APP_PORT,
                        help=f"local dashboard port (default {APP_PORT}; any free port if taken)")
    parser.add_argument("--discover", action="store_true",
                        help="also find listeners by mDNS (may trigger a one-time firewall prompt)")
    parser.add_argument("--no-window", action="store_true",
                        help="start as the bubble only (this is how the Startup shortcut runs too)")
    parser.add_argument("--config", default=None, help="explicit nvmon.toml")
    parser.add_argument("--shortcuts", action="store_true",
                        help="put an 'nvmon' shortcut on the desktop and in the NVMON folder, then exit")
    args = parser.parse_args(list(argv or []))

    if args.shortcuts:
        from . import config as cfgmod
        try:
            desktop = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "[Environment]::GetFolderPath('Desktop')"],
                capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            desktop = ""
        targets = [Path(desktop or Path.home() / "Desktop") / "nvmon.lnk", cfgmod.BASE_DIR / "nvmon.lnk"]
        ok = True
        for lnk in targets:
            made = make_shortcut(lnk)
            ok &= made
            print(f"{'created' if made else 'FAILED '} {lnk}")
        if STARTUP_LNK.exists():  # refresh it too (icon, current paths)
            print(f"{'updated' if make_shortcut(STARTUP_LNK, no_window=True) else 'FAILED '} {STARTUP_LNK}")
        return 0 if ok else 1

    # pythonw has no console: give print()/logging somewhere harmless to go.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115

    if _hand_off_to_running_instance():
        return 0

    try:  # physical pixels everywhere, so the bar lines up with Edge exactly
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:  # noqa: BLE001
        pass

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    from . import config as cfgmod
    from . import log as logmod
    cfgmod.LOGS_DIR = STATE_DIR / "logs"  # keep the monitor box's logs\ clean
    logger = logmod.setup()
    if args.config:
        cfgmod.CONFIG_FILE = Path(args.config).resolve()  # type: ignore[assignment]
        cfgmod.load_config(force_reload=True)

    state = _load_state()
    secret = secrets.token_urlsafe(24)
    dash = DashboardWindow("", secret, state)

    try:
        server, registry, aggregator, disc = start_controller(
            args.port, args.discover, secret, lambda cmd, _body: dash.command(cmd))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[app] startup failed")
        MessageBoxW(None, f"nvmon app could not start:\n\n{exc}\n\nLog: {cfgmod.LOGS_DIR}",
                    "nvmon", 0x10)
        return 1
    dash.base_url = f"http://127.0.0.1:{server.port}/"

    if "startup" not in state:  # first run: start with Windows by default
        state["startup"] = _set_startup(True)
        _save_state(state)

    bubble = Bubble(dash, registry, aggregator, state)
    bubble.create()
    if not args.no_window:
        dash.launch()
    bubble.run()  # until Quit

    logger.info("[app] quitting")
    dash.close()
    server.shutdown()
    aggregator.stop()
    if disc is not None:
        try:
            disc.stop()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
