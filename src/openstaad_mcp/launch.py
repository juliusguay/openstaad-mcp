"""Startup tools for STAAD.Pro: idempotent ``launch_staad`` and safe ``close_staad``.

STAAD.Pro is launched via ``Bentley.Staad.exe [file.std]``. Readiness:

* with a file: the OpenSTAAD ROT scan (``InstanceRegistry.get_active_instances``) shows an instance
  whose open file matches -- the same discovery path every other tool here uses;
* without a file: the new process has a visible top-level window (a bare STAAD.Pro does not register
  in the ROT until a model is open).

Rules (shared with the sibling launchers): idempotent, never touches an already-running session
beyond reporting (an existing instance is NEVER given a different file, replaced or closed),
RAM guard (>= 2.5 GB free), returns a dict. Live behaviour is unverified until a live smoke test
(see README "Startup tools").
"""
from __future__ import annotations

import glob
import logging
import os
from typing import Any, Callable, Optional

from openstaad_mcp import _applaunch as al

logger = logging.getLogger(__name__)

IMAGE_NAME = "Bentley.Staad.exe"
_GLOB = r"C:\Program Files\Bentley\Engineering\STAAD.Pro*\STAAD\Bentley.Staad.exe"
APP_KEY = "staad"
DEFAULT_TIMEOUT_S = 180.0


def resolve_staad_exe() -> Optional[str]:
    """STAAD_EXE override, else the newest STAAD.Pro* install under Program Files, else None."""
    env = os.environ.get("STAAD_EXE")
    if env:
        return env
    hits = sorted(glob.glob(_GLOB))
    return hits[-1] if hits else None


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)) if p else ""


def launch_staad(
    file_path: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    get_instances: Optional[Callable[[], list[Any]]] = None,
) -> dict[str, Any]:
    """Ensure STAAD.Pro is running; optionally open a ``.std`` model in a NEW instance.

    ``get_instances`` returns the ROT-discovered instances (objects with .pid/.file_path/.alias);
    injected by server.py (and by tests).
    """
    get_instances = get_instances or (lambda: [])
    if file_path:
        if not file_path.lower().endswith(".std"):
            return {"status": "error", "launched": False, "message": f"expected a .std file, got {file_path!r}"}
        if not os.path.isfile(file_path):
            return {"status": "error", "launched": False, "message": f"file not found: {file_path}"}

    pids = al.image_pids(IMAGE_NAME)
    try:
        instances = get_instances()
    except Exception as exc:  # ROT scan failure must not trigger a second launch
        logger.warning("ROT scan failed: %s", exc)
        instances = []
    if pids or instances:
        info = [{"alias": getattr(i, "alias", None), "pid": i.pid, "file": i.file_path} for i in instances]
        res: dict[str, Any] = {
            "status": "already_running", "launched": False,
            "pid": (info[0]["pid"] if info else pids[0]), "pids": sorted(set(pids) | {i["pid"] for i in info}),
            "instances": info, "file": info[0]["file"] if info else None,
            "port": None, "url": None,
        }
        if file_path:
            already = any(_norm(i["file"]) == _norm(file_path) for i in info)
            res["file_opened"] = already
            if not already:
                res["message"] = ("STAAD.Pro is already running; NOT opening the requested file in or "
                                  "replacing the user's session. Open it by hand, or close STAAD.Pro and retry.")
        return res

    exe = resolve_staad_exe()
    if not exe or not os.path.isfile(exe):
        return {"status": "error", "launched": False,
                "message": "Bentley.Staad.exe not found under Program Files\\Bentley\\Engineering\\STAAD.Pro*; "
                           "set STAAD_EXE."}
    refusal = al.ram_guard()
    if refusal:
        return refusal

    before = al.image_pids(IMAGE_NAME)
    popen_pid = al.spawn(exe, [file_path] if file_path else [])
    pid = al.new_pid(IMAGE_NAME, before, popen_pid)
    al.LAUNCHED[APP_KEY] = pid

    def ready() -> Any:
        if file_path:
            for i in get_instances():
                if _norm(i.file_path) == _norm(file_path):
                    return i
            return None
        live = al.image_pids(IMAGE_NAME)
        for p in live or [pid]:
            if al.visible_window_titles(p):
                return p
        return None

    try:
        ok, waited = al.wait_for(ready, timeout_s, 3.0)
    except Exception as exc:
        ok, waited = None, 0.0
        logger.warning("readiness poll failed: %s", exc)
    if ok:
        pid = ok.pid if file_path else int(ok)
        al.LAUNCHED[APP_KEY] = pid
    return {
        "status": "launched_and_ready" if ok else "timeout", "launched": True, "pid": pid,
        "file": file_path, "port": None, "url": None, "waited_s": round(waited, 1),
        **({} if ok else {"message": f"STAAD.Pro started (pid {pid}) but was not ready within {timeout_s:.0f}s; "
                                      "it may still be loading or showing a dialog."}),
    }


def close_staad(force: bool = False) -> dict[str, Any]:
    """Close ONLY the STAAD.Pro instance this server session launched (WM_CLOSE; a save-changes
    dialog is reported, never answered). Refuses user-started instances."""
    return al.close_launched(APP_KEY, force=force)
