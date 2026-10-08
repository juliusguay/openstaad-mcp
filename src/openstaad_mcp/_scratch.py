r"""Shared scratch-file + backup safety helpers for the new_scratch_<app> / save_<app> tools.

Same rules as the OpenRoads/OpenBuildings scratch tools (copy kept identical across the sibling
MCP repos; this module imports nothing app specific):

* scratch files live ONLY under ``%TEMP%\bentley-scratch\<app>\<timestamp>_<name>.<ext>`` --
  never inside a project folder;
* before anything is overwritten a timestamped backup copy is made to
  ``C:\Users\JJGIV\Backups`` (env ``BENTLEY_BACKUP_DIR``), and only if >= 2 GB stay free;
* nothing here launches or closes an application.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

MIN_FREE_GB = 2.0
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def scratch_base() -> Path:
    return Path(tempfile.gettempdir()) / "bentley-scratch"


def scratch_root(app: str) -> Path:
    return scratch_base() / app


def backup_dir() -> Path:
    return Path(os.environ.get("BENTLEY_BACKUP_DIR", r"C:\Users\JJGIV\Backups"))


def safe_name(name: Optional[str], default: str = "scratch") -> str:
    s = _SAFE.sub("_", (name or "").strip()).strip("_")
    return s[:60] or default


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def new_scratch_path(app: str, name: Optional[str], ext: str, root: Optional[Path] = None) -> Path:
    """<scratch root>/<timestamp>_<name><ext>; creates the folder, never overwrites (adds -N)."""
    folder = Path(root) if root else scratch_root(app)
    folder.mkdir(parents=True, exist_ok=True)
    ext = ext if ext.startswith(".") else "." + ext
    base = f"{stamp()}_{safe_name(name)}"
    cand, n = folder / f"{base}{ext}", 1
    while cand.exists():
        n += 1
        cand = folder / f"{base}-{n}{ext}"
    return cand


def is_scratch(path) -> bool:
    try:
        Path(path).resolve().relative_to(scratch_base().resolve())
        return True
    except (ValueError, OSError):
        return False


def free_gb(path) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1024 ** 3


def backup_file(src, dest_dir: Optional[Path] = None) -> tuple[Optional[str], Optional[str]]:
    """Timestamped copy of ``src`` into the backup dir. Returns (backup_path, error);
    (None, None) when ``src`` does not exist (nothing to back up)."""
    src = Path(src)
    if not src.is_file():
        return None, None
    d = Path(dest_dir) if dest_dir else backup_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"cannot create backup dir {d}: {exc}"
    free = free_gb(d)
    if free < MIN_FREE_GB:
        return None, f"only {free:.1f} GB free on the backup drive (need >= {MIN_FREE_GB:.0f} GB); nothing was changed"
    dest = d / f"{stamp()}_{src.name}"
    n = 1
    while dest.exists():
        n += 1
        dest = d / f"{stamp()}-{n}_{src.name}"
    shutil.copy2(src, dest)
    return str(dest), None
