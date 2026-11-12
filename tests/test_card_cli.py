"""`runsync card` through the real CLI, with a temporary folder as the card (nothing is mounted)."""
import json

import pytest
from test_card import World
from typer.testing import CliRunner

from runsync import cli
from runsync.card import VolumeInfo


@pytest.fixture
def env(tmp_path, monkeypatch):
    w = World(tmp_path, register=False)
    monkeypatch.setenv("RUNSYNC_HOME", str(w.lib))
    state = {"volumes": [w.vol]}
    monkeypatch.setattr(cli, "candidate_volumes", lambda library: state["volumes"])
    monkeypatch.setattr(cli, "volume_info", lambda p: w.vol)
    w.playlist("P1", "Running")
    w.song("a", "Alpha", {"P1": 0})
    w.song("b", "Beta", {"P1": 1})
    return w, state


def run(*args):
    out = CliRunner().invoke(cli.app, ["card", *args])
    return out


def facts(*args):
    out = run("--json", *args)
    return out.exit_code, json.loads([ln for ln in out.output.splitlines() if ln.startswith("{")][-1])


def test_the_whole_flow_register_preview_confirm_apply_undo(env):
    w, _ = env
    _code, s = facts()
    assert s["state"] == "unregistered" and s["candidates"][0]["name"] == "RUNCARD"
    assert run("--register", str(w.mount), "--name", "shanling").exit_code == 0
    _code, s = facts()
    assert (s["state"], s["adds"], s["removes"]) == ("ready", 2, 0)
    pid = s["plan_id"]
    for args in (["--apply"], ["--apply", "--confirm", "wrongplan123"]):          # the gate lives in the command
        out = run(*args)
        assert out.exit_code == 2 and "Not confirmed" in out.output
        assert not (w.mount / "Music").exists()
    code, s = facts("--apply", "--confirm", pid)
    assert code == 0 and s["applied"]["added"] == 2 and s["state"] == "ready" and s["adds"] == 0
    assert sorted(p.name for p in (w.mount / "Music").iterdir()) == ["Alpha.m4a", "Beta.m4a", "Running.m3u8"]
    code, s = facts("--undo")
    assert code == 0 and s["undone"]["removed"] == 2
    assert sorted(p.name for p in (w.mount / "Music").iterdir()) == ["Running.m3u8"]    # playlist rewritten empty


def test_a_different_card_is_never_written_and_the_command_says_so(env, tmp_path):
    w, state = env
    assert run("--register", str(w.mount)).exit_code == 0
    other = tmp_path / "other"
    other.mkdir()
    state["volumes"] = [VolumeInfo(other, "RUNCARD", "UUID-OTHER", "exFAT", 10**9, dev=-3)]   # same name, another UUID
    _code, s = facts()
    assert s["state"] == "not_inserted"
    out = run("--apply", "--confirm", "anything")
    assert out.exit_code == 2 and "No registered card is inserted" in out.output
    assert list(other.iterdir()) == []


def test_the_macs_own_disk_cannot_be_registered(env, monkeypatch):
    w, _ = env
    w.vol = VolumeInfo(w.mount, "Macintosh HD", "UUID-OWN", "APFS", 10**9, dev=w.lib.stat().st_dev)
    import pytest as _p

    from runsync import cli as c
    mp = _p.MonkeyPatch()
    mp.setattr(c, "volume_info", lambda p: w.vol)
    try:
        out = run("--register", str(w.mount))
    finally:
        mp.undo()
    assert out.exit_code == 2 and "same disk as your library" in out.output
