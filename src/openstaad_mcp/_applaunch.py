"""Shared, stdlib-only helpers for the `launch_<app>` / `close_<app>` MCP tools.

This exact file is vendored into each desktop-app MCP repo (openstaad-mcp, easypower-mcp-tool,
SPIDA-Suite-MCP, PLS-Suite-MCP, openflows-storm-mcp) so each stays self-contained. Keep the
copies identical; fix bugs in all of them.

Contract every launcher built on this follows:
  * idempotent: if the app (or its port) is already up, report it and NEVER start a second one;
  * never touches an already-running user session beyond reporting (no file swap, no close);
  * RAM guard: refuse to start a heavy app with < MIN_FREE_RAM_GB free physical memory;
  * poll readiness (window visible / port open) up to a timeout, return a dict (pid, port/url, file).

Everything that touches the OS goes through the module-level functions below so tests can
monkeypatch them with fakes (no real launches in tests).
"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any, Callable, Optional

MIN_FREE_RAM_GB = 2.5

_sleep = time.sleep
_now = time.monotonic

# pids this process started (name -> pid); close_* tools only ever touch these.
LAUNCHED: dict[str, int] = {}


# ----------------------------------------------------------------------------- memory
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def free_ram_gb() -> float:
    """Available physical memory in GiB (Windows). Returns +inf off-Windows (guard disabled)."""
    if sys.platform != "win32":
        return float("inf")
    st = _MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return st.ullAvailPhys / (1024 ** 3)


def ram_guard(min_gb: float = MIN_FREE_RAM_GB) -> Optional[dict[str, Any]]:
    """None when there is enough free RAM, else a ready-to-return refusal dict."""
    free = free_ram_gb()
    if free >= min_gb:
        return None
    return {
        "status": "insufficient_ram",
        "launched": False,
        "free_ram_gb": round(free, 2),
        "required_ram_gb": min_gb,
        "message": (
            f"Only {free:.2f} GB RAM free (< {min_gb} GB); not launching. Close something heavy "
            "and call again."
        ),
    }


# ----------------------------------------------------------------------------- processes
def image_pids(image_name: str) -> list[int]:
    """PIDs of running processes whose image name equals `image_name` (case-insensitive)."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.strip().split('","')]
        if len(parts) >= 2 and parts[0].strip('"').lower() == image_name.lower():
            try:
                pids.append(int(parts[1].strip('"')))
            except ValueError:
                pass
    return pids


def port_listener_pid(port: int) -> Optional[int]:
    """PID listening on a local TCP port (via netstat), or None."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[3].upper() == "LISTENING" and cols[1].rsplit(":", 1)[-1] == str(port):
            try:
                return int(cols[4])
            except ValueError:
                return None
    return None


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    return pid in image_pids_for_pid(pid)


def image_pids_for_pid(pid: int) -> list[int]:
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [pid] if f'"{pid}"' in out else []


def _enum_windows() -> list[tuple[int, int, str]]:
    """(hwnd, pid, title) for every visible top-level window with a title."""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, str]] = []
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((int(hwnd), pid.value, buf.value))
        return True

    user32.EnumWindows(proc_t(cb), 0)
    return found


def visible_window_titles(pid: int) -> list[str]:
    return [t for _h, p, t in _enum_windows() if p == pid]


def spawn(exe: str, args: Optional[list[str]] = None) -> int:
    """Start `exe` detached (no console, no inherited handles); returns the new pid."""
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [exe, *(args or [])], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags, cwd=os.path.dirname(exe) or None,
    )
    return proc.pid


def wait_for(pred: Callable[[], Any], timeout_s: float, poll_s: float = 2.0) -> tuple[Any, float]:
    """Poll `pred` until truthy or timeout. Returns (last_value, waited_seconds)."""
    start = _now()
    while True:
        val = pred()
        waited = _now() - start
        if val:
            return val, waited
        if waited >= timeout_s:
            return val, waited
        _sleep(poll_s)


def new_pid(image_name: str, before: list[int], fallback: int) -> int:
    """First pid of `image_name` not present in `before` (a launcher stub may hand off to another
    process), else `fallback` (the pid Popen returned)."""
    fresh = [p for p in image_pids(image_name) if p not in before]
    return fresh[0] if fresh else fallback


# ----------------------------------------------------------------------------- closing
def request_close(pid: int) -> int:
    """Politely ask every visible window of `pid` to close (WM_CLOSE). The app may answer with a
    'save changes?' dialog -- that is deliberately left for a human. Returns windows signalled."""
    WM_CLOSE = 0x0010
    n = 0
    for hwnd, p, _t in _enum_windows():
        if p == pid:
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            n += 1
    return n


def close_launched(name: str, force: bool = False, wait_s: float = 15.0) -> dict[str, Any]:
    """Close an app instance THIS process launched (LAUNCHED[name]); refuses anything else.

    Graceful (WM_CLOSE) by default. A pending 'save changes?' dialog is reported, never answered.
    force=True additionally taskkills the pid if it is still alive after wait_s (discards unsaved work).
    """
    pid = LAUNCHED.get(name)
    if pid is None:
        return {"status": "refused", "closed": False,
                "message": f"No {name} instance was launched by this server session; refusing to close "
                           "an instance this tool did not start."}
    if not pid_alive(pid):
        LAUNCHED.pop(name, None)
        return {"status": "already_closed", "closed": True, "pid": pid}
    signalled = request_close(pid)
    _gone, waited = wait_for(lambda: not pid_alive(pid), wait_s, 1.0)
    if not pid_alive(pid):
        LAUNCHED.pop(name, None)
        return {"status": "closed", "closed": True, "pid": pid, "windows_signalled": signalled,
                "waited_s": round(waited, 1)}
    if force:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20)
        LAUNCHED.pop(name, None)
        return {"status": "force_killed", "closed": True, "pid": pid}
    return {"status": "close_pending", "closed": False, "pid": pid, "windows_signalled": signalled,
            "message": "Close requested but the app is still up (likely a save-changes dialog). "
                       "Dismiss it by hand, or call again with force=True to discard unsaved work."}
