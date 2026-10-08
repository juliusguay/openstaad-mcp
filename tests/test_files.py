"""Offline tests for new_scratch_staad / save_staad. COM, launcher and disk space are faked."""
from __future__ import annotations

import pytest

from openstaad_mcp import _files as fx
from openstaad_mcp import _scratch as sx


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(sx.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    monkeypatch.setenv("BENTLEY_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setattr(sx, "free_gb", lambda p: 50.0)
    return tmp_path


class FakeStaad:
    def __init__(self):
        self.calls = []
        self.file = None

    def SetSilentMode(self, v): self.calls.append(("silent", v))
    def SaveModel(self, v): self.calls.append(("SaveModel", v))
    def SaveAs(self, p): self.calls.append(("SaveAs", p)); self.file = p
    def GetSTAADFile(self): return self.file


def runner(staad):
    def run(fn, cur):
        staad.file = staad.file or cur
        return fn(staad)
    return run


def test_create_is_in_scratch_folder_and_looks_like_std(env):
    r = fx.new_scratch_staad("My Frame!", "space_imperial", open=False)
    assert r["status"] == "created" and r["opened"] is False and r["app"] == "STAAD.Pro"
    assert sx.is_scratch(r["path"]) and r["path"].endswith("_My_Frame.std")
    assert "bentley-scratch" in r["path"] and "staad" in r["path"]
    text = open(r["path"], "rb").read().decode()
    assert text.startswith("STAAD SPACE") and "UNIT FEET KIP" in text and text.rstrip().endswith("FINISH")


def test_two_creates_never_collide(env):
    a = fx.new_scratch_staad("x", open=False)["path"]
    b = fx.new_scratch_staad("x", open=False)["path"]
    assert a != b


def test_unknown_template_errors_and_writes_nothing(env):
    r = fx.new_scratch_staad("x", "nope", open=False)
    assert r["status"] == "error" and r["path"] is None
    assert not (env / "tmp" / "bentley-scratch").exists()


def test_seed_copy(env):
    seed = env / "seed.std"
    seed.write_text("STAAD PLANE\nFINISH\n")
    r = fx.new_scratch_staad("c", str(seed), open=False)
    assert open(r["path"]).read() == "STAAD PLANE\nFINISH\n" and str(seed) != r["path"]


def test_open_delegates_to_launcher_and_respects_running_session(env):
    seen = []
    r = fx.new_scratch_staad("o", launch=lambda p: seen.append(p) or {
        "status": "already_running", "launched": False, "file_opened": False, "message": "NOT opening"})
    assert seen == [r["path"]] and r["opened"] is False and "NOT opening" in r["detail"]
    r2 = fx.new_scratch_staad("o", launch=lambda p: {"status": "launched_and_ready", "launched": True, "pid": 5})
    assert r2["opened"] is True


def test_launcher_exception_keeps_file(env):
    def boom(p):
        raise RuntimeError("x")
    r = fx.new_scratch_staad("o", launch=boom)
    assert r["status"] == "created" and r["opened"] is False and "could not open" in r["detail"]


def test_open_false_does_not_launch(env):
    r = fx.new_scratch_staad("o", open=False, launch=lambda p: pytest.fail("no launch"))
    assert r["opened"] is False


def test_save_in_place_scratch_ok_with_backup(env):
    p = fx.new_scratch_staad("s", open=False)["path"]
    st = FakeStaad()
    r = fx.save_staad(p, runner(st))
    assert r["status"] == "saved" and r["in_place"] and r["backed_up"]
    assert ("SaveModel", True) in st.calls and st.calls[0] == ("silent", True) and st.calls[-1] == ("silent", False)
    assert open(r["backed_up"]).read() == open(p).read()


def test_save_in_place_user_file_refused_without_overwrite(env):
    user = env / "proj" / "bridge.std"
    user.parent.mkdir()
    user.write_text("orig")
    st = FakeStaad()
    r = fx.save_staad(str(user), runner(st))
    assert r["status"] == "refused" and st.calls == [] and not (env / "bk").exists()
    r = fx.save_staad(str(user), runner(st), overwrite=True)
    assert r["status"] == "saved" and open(r["backed_up"]).read() == "orig"


def test_save_as_new_file_and_existing_target_rules(env):
    user = env / "proj" / "a.std"
    user.parent.mkdir()
    user.write_text("a")
    tgt = env / "out" / "b.std"
    st = FakeStaad()
    r = fx.save_staad(str(user), runner(st), save_as=str(tgt))
    assert r["status"] == "saved" and r["in_place"] is False and r["backed_up"] is None
    assert ("SaveAs", str(tgt)) in st.calls and r["active_file"] == str(tgt)
    tgt.write_text("old")
    st2 = FakeStaad()
    assert fx.save_staad(str(user), runner(st2), save_as=str(tgt))["status"] == "refused" and st2.calls == []
    r = fx.save_staad(str(user), runner(st2), save_as=str(tgt), overwrite=True)
    assert r["status"] == "saved" and open(r["backed_up"]).read() == "old"


def test_save_as_bad_extension_or_protected_dir(env):
    st = FakeStaad()
    assert fx.save_staad(str(env / "a.std"), runner(st), save_as=str(env / "b.txt"))["status"] == "error"
    assert fx.save_staad(str(env / "a.std"), runner(st), save_as=r"C:\Windows\x.std")["status"] == "error"
    assert st.calls == []


def test_low_disk_blocks_overwrite_before_com(env, monkeypatch):
    monkeypatch.setattr(sx, "free_gb", lambda p: 0.5)
    p = fx.new_scratch_staad("s", open=False)["path"]
    st = FakeStaad()
    r = fx.save_staad(p, runner(st))
    assert r["status"] == "error" and "GB free" in r["detail"] and st.calls == []


def test_com_failure_and_timeout_reported(env):
    p = fx.new_scratch_staad("s", open=False)["path"]

    def t(fn, cur):
        raise TimeoutError()

    def c(fn, cur):
        raise RuntimeError("com")
    assert "timed out" in fx.save_staad(p, t)["detail"]
    assert fx.save_staad(p, c)["detail"] == "com"


def test_silent_mode_restored_when_save_raises(env):
    p = fx.new_scratch_staad("s", open=False)["path"]
    st = FakeStaad()

    def bad(v):
        raise RuntimeError("dlg")
    st.SaveModel = bad
    r = fx.save_staad(p, runner(st))
    assert r["status"] == "error" and st.calls[-1] == ("silent", False)


def test_save_as_falls_back_to_save_then_copy_when_saveas_missing(env):
    user = env / "proj" / "a.std"
    user.parent.mkdir()
    user.write_text("model")
    tgt = env / "out" / "c.std"

    class NoSaveAs(FakeStaad):
        def __getattribute__(self, n):
            if n == "SaveAs":
                raise AttributeError("'OSRoot' object has no attribute 'SaveAs'")
            return object.__getattribute__(self, n)

    st = NoSaveAs()
    r = fx.save_staad(str(user), runner(st), save_as=str(tgt))
    assert r["status"] == "saved" and r["method"] == "save_then_copy" and tgt.read_text() == "model"
    assert ("SaveModel", True) in st.calls and r["active_file"] == str(user)
