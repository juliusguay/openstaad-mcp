r"""Scratch-file creation and safe save for STAAD.Pro: ``new_scratch_staad`` / ``save_staad``.

Create: a NEW ``.std`` is written offline (a STAAD model is a plain-text command file) from a small
built-in template, or copied from a seed ``.std`` the caller names, into
``%TEMP%\bentley-scratch\staad\<timestamp>_<name>.std`` -- never into a project folder. Opening goes
through ``launch_staad(file)``: idempotent, RAM-guarded, and it NEVER opens a file in (or replaces the model
of) an already-running STAAD.Pro, so ``opened`` is False in that case. Live behaviour of the offline-written
templates (STAAD must accept the file) is unverified until the live smoke test.

Save: through OpenSTAAD COM (``SetSilentMode``, ``SaveModel`` / ``SaveAs``). A timestamped backup copy of the
on-disk file is made first (>= 2 GB free); an in-place save of a non-scratch model or a save_as onto an
existing file needs ``overwrite=True``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from openstaad_mcp import _scratch as sx

APP = "staad"
APP_NAME = "STAAD.Pro"

_HEAD = "STAAD {kind}\r\nSTART JOB INFORMATION\r\nENGINEER DATE {date}\r\nEND JOB INFORMATION\r\nINPUT WIDTH 79\r\n"
_TEMPLATES = {
    "space_metric": ("SPACE", "UNIT METER KN"),
    "space_imperial": ("SPACE", "UNIT FEET KIP"),
    "plane_metric": ("PLANE", "UNIT METER KN"),
    "plane_imperial": ("PLANE", "UNIT FEET KIP"),
}
DEFAULT_TEMPLATE = "space_metric"


def template_names() -> list[str]:
    return sorted(_TEMPLATES)


def _text_for(template: str) -> str:
    import time
    kind, units = _TEMPLATES[template]
    return _HEAD.format(kind=kind, date=time.strftime("%d-%b-%y")) + units + "\r\nFINISH\r\n"


def new_scratch_staad(
    name: Optional[str] = None,
    template: Optional[str] = None,
    open: bool = True,  # noqa: A002 - tool argument name
    launch: Optional[Callable[[str], dict[str, Any]]] = None,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """Create a scratch ``.std`` and (optionally) open it via the launcher. ``template`` is one of
    ``template_names()`` or a path to a seed ``.std`` (copied). ``launch(path)`` is launch_staad bound to the
    instance registry (injected by server.py / tests)."""
    base = {"app": APP_NAME, "path": None, "opened": False}
    template = template or DEFAULT_TEMPLATE
    seed = None
    if template not in _TEMPLATES:
        seed = os.path.abspath(template)
        if not (seed.lower().endswith(".std") and os.path.isfile(seed)):
            return {**base, "status": "error", "detail": f"template must be one of {template_names()} or an existing .std path"}
    dest = sx.new_scratch_path(APP, name, ".std", root)
    if seed:
        shutil.copyfile(seed, dest)
    else:
        dest.write_bytes(_text_for(template).encode("ascii"))
    out: dict[str, Any] = {**base, "status": "created", "path": str(dest), "template": template}
    if open and launch is not None:
        try:
            res = launch(str(dest))
        except Exception as exc:  # launcher failures must not hide the created file
            out["detail"] = f"created but could not open: {exc}"
            return out
        out["launch"] = {k: res.get(k) for k in ("status", "launched", "pid", "file_opened", "message") if k in res}
        out["opened"] = res.get("status") in ("launched_and_ready",) or bool(res.get("file_opened"))
        if res.get("message"):
            out["detail"] = res["message"]
    return out


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def save_staad(
    current_file: str,
    run: Callable[[Callable[[Any], Any], str], Any],
    save_as: Optional[str] = None,
    overwrite: bool = False,
    backup_to: Optional[Path] = None,
) -> dict[str, Any]:
    """Save the model open in the instance whose file is ``current_file``.

    ``run(fn, current_file)`` executes ``fn(staad)`` on the COM object (connection.connect_and_run)."""
    cur = os.path.abspath(current_file)
    in_place = save_as is None or _same(save_as, cur)
    target = cur if in_place else os.path.abspath(save_as)
    fail = lambda status, detail: {"status": status, "path": target, "backed_up": None, "detail": detail}  # noqa: E731

    if in_place:
        if not sx.is_scratch(cur) and not overwrite:
            return fail("refused", "in-place save of a non-scratch model needs overwrite=True (a timestamped backup is made first)")
    else:
        from openstaad_mcp.sandbox.com_proxy import validate_file_path  # same path rules as execute_code's SaveAs
        try:
            validate_file_path(target, allowed_extensions=frozenset({".std"}), method_name="SaveAs")
        except ValueError as exc:
            return fail("error", str(exc))
        if os.path.exists(target) and not overwrite:
            return fail("refused", "save_as target exists; pass overwrite=True (a timestamped backup is made first)")

    # Backup the file that is about to be replaced on disk (the in-place model, or an existing save_as target).
    backed, err = sx.backup_file(target, backup_to)
    if err:
        return fail("error", err)
    if not in_place:
        os.makedirs(os.path.dirname(target), exist_ok=True)

    def _do(staad: Any) -> dict[str, Any]:
        staad.SetSilentMode(True)  # the save would otherwise be able to raise a blocking dialog
        try:
            if in_place:
                staad.SaveModel(True)
                method = "SaveModel"
            else:
                try:
                    staad.SaveAs(target)
                    method = "SaveAs"
                except AttributeError:
                    # STAAD.Pro 2026 (26.0.0.340): the OpenSTAAD root object has no SaveAs (live-verified 2026-10-08).
                    # Save in place, then copy the saved file; the open document stays the original.
                    staad.SaveModel(True)
                    shutil.copyfile(cur, target)
                    method = "save_then_copy"
        finally:
            staad.SetSilentMode(False)
        try:
            return {"active_file": staad.GetSTAADFile(), "method": method}
        except Exception:
            return {"active_file": None, "method": method}

    try:
        info = run(_do, cur)
    except TimeoutError:
        return fail("error", "OpenSTAAD call timed out (a dialog may be open in STAAD.Pro)")
    except Exception as exc:
        return fail("error", str(exc))
    return {"status": "saved", "path": target, "backed_up": backed, "in_place": in_place, **info}
