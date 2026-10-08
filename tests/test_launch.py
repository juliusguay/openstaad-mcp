"""Offline tests for launch_staad / close_staad. All OS access is faked; nothing is launched."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from openstaad_mcp import _applaunch as al
from openstaad_mcp import launch as lc


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    st = SimpleNamespace(pids=[], spawned=[], ram=8.0, windows={}, now=0.0)
    exe = tmp_path / "Bentley.Staad.exe"
    exe.write_text("x")
    monkeypatch.setenv("STAAD_EXE", str(exe))
    monkeypatch.setattr(al, "free_ram_gb", lambda: st.ram)
    monkeypatch.setattr(al, "image_pids", lambda name: list(st.pids))

    def spawn(e, args=None):
        st.spawned.append((e, args or []))
        st.pids.append(4242)
        return 4242

    monkeypatch.setattr(al, "spawn", spawn)
    monkeypatch.setattr(al, "visible_window_titles", lambda pid: st.windows.get(pid, []))
    monkeypatch.setattr(al, "_sleep", lambda s: setattr(st, "now", st.now + s))
    monkeypatch.setattr(al, "_now", lambda: st.now)
    al.LAUNCHED.clear()
    return st


def test_already_running_never_spawns_and_does_not_replace_file(fake, tmp_path):
    model = tmp_path / "new.std"
    model.write_text("STAAD SPACE")
    inst = SimpleNamespace(alias="staadPro1", pid=99, file_path=r"C:\users\x\user.std")
    fake.pids = [99]
    r = lc.launch_staad(str(model), 5, get_instances=lambda: [inst])
    assert r["status"] == "already_running" and r["launched"] is False and r["pid"] == 99
    assert r["file_opened"] is False and "NOT opening" in r["message"]
    assert fake.spawned == []


def test_running_bare_process_without_rot_entry_still_idempotent(fake):
    fake.pids = [77]
    r = lc.launch_staad(None, 5, get_instances=lambda: [])
    assert r["status"] == "already_running" and r["pid"] == 77 and fake.spawned == []


def test_ram_guard_refuses(fake):
    fake.ram = 1.0
    r = lc.launch_staad(None, 5)
    assert r["status"] == "insufficient_ram" and r["launched"] is False and fake.spawned == []


def test_rejects_bad_file(fake, tmp_path):
    assert lc.launch_staad(str(tmp_path / "a.txt"), 5)["status"] == "error"
    assert lc.launch_staad(str(tmp_path / "missing.std"), 5)["status"] == "error"
    assert fake.spawned == []


def test_bare_launch_ready_when_window_appears(fake, monkeypatch):
    polls = {"n": 0}

    def titles(pid):
        polls["n"] += 1
        return ["STAAD.Pro"] if polls["n"] >= 3 else []

    monkeypatch.setattr(al, "visible_window_titles", titles)
    r = lc.launch_staad(None, 60)
    assert r["status"] == "launched_and_ready" and r["launched"] is True and r["pid"] == 4242
    assert fake.spawned == [(fake.spawned[0][0], [])]
    assert al.LAUNCHED["staad"] == 4242


def test_launch_with_file_waits_for_rot_match(fake, tmp_path):
    model = tmp_path / "scratch.std"
    model.write_text("STAAD SPACE")
    seen = {"n": 0}

    def inst():
        seen["n"] += 1
        if seen["n"] <= 2:  # 1st = pre-launch idempotency check, 2nd = first readiness poll
            return []
        return [SimpleNamespace(alias="staadPro1", pid=4242, file_path=str(model))]

    r = lc.launch_staad(str(model), 60, get_instances=inst)
    assert r["status"] == "launched_and_ready" and r["file"] == str(model)
    assert fake.spawned[0][1] == [str(model)]


def test_timeout_reported(fake):
    r = lc.launch_staad(None, 6)
    assert r["status"] == "timeout" and r["launched"] is True and r["pid"] == 4242


def test_close_refuses_unknown_instance(fake):
    r = lc.close_staad()
    assert r["status"] == "refused" and r["closed"] is False


def test_close_graceful_and_pending(fake, monkeypatch):
    al.LAUNCHED["staad"] = 4242
    alive = {"v": True}
    monkeypatch.setattr(al, "pid_alive", lambda pid: alive["v"])
    monkeypatch.setattr(al, "request_close", lambda pid: (alive.__setitem__("v", False), 1)[1])
    assert lc.close_staad()["status"] == "closed"
    al.LAUNCHED["staad"] = 4242
    alive["v"] = True
    monkeypatch.setattr(al, "request_close", lambda pid: 1)
    r = lc.close_staad()
    assert r["status"] == "close_pending" and r["closed"] is False
