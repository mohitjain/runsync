"""The card engine: wrong card refused, only our own files ever deleted, interrupted runs recover, undo works.
A temporary folder stands in for the card (nothing is mounted); volume info is plain data."""
import os
from pathlib import Path

import pytest

from runsync import card, db
from runsync.card import (
    VolumeInfo,
    apply_card_plan,
    build_card_plan,
    card_plan_id,
    card_state,
    copy_verified,
    find_card,
    parse_volume_plist,
    register_device,
    undo_last_run,
)


class World:
    def __init__(self, tmp_path, register=True):
        self.lib = tmp_path / "lib"
        self.lib.mkdir()
        self.mount = tmp_path / "card"
        self.mount.mkdir()
        self.conn = db.connect(self.lib / "state.db")
        self.player = self.lib / "player" / "m4a"
        self.player.mkdir(parents=True)
        self.vol = VolumeInfo(self.mount, "RUNCARD", "UUID-CARD", "exFAT", 10**9, dev=-1)   # not the library's disk
        self.dev = register_device(self.conn, self.vol, "shanling", self.lib) if register else None
        self.n = 0

    def playlist(self, pid, title, selected=1):
        self.conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES (?,?,?,?)", (pid, title, "u", selected))
        self.conn.commit()

    def song(self, vid, title, playlists, size=1000, built=True):
        """A downloaded track with a player copy, in the given playlists (id -> position)."""
        path = self.player / f"{title}.m4a"
        if built:
            path.write_bytes(os.urandom(size))
        self.conn.execute("INSERT INTO tracks (video_id, status, player_path, probe_duration_s) VALUES (?,?,?,?)",
                          (vid, "downloaded", str(path) if built else None, 200.0))
        self.conn.execute("INSERT INTO metadata (video_id, artist, title, resolved_at) VALUES (?,?,?,?)",
                          (vid, "Artist", title, "x"))
        for pid, pos in playlists.items():
            self.conn.execute("INSERT INTO playlist_items (playlist_id, video_id, position) VALUES (?,?,?)", (pid, vid, pos))
        self.conn.commit()
        return path

    def plan(self, free=10**9, **kw):
        return build_card_plan(self.conn, self.dev, self.mount, "Music", free, **kw)

    def apply(self, plan=None, **kw):
        return apply_card_plan(self.conn, self.dev, self.mount, "Music", plan or self.plan(), **kw)

    def on_card(self):
        d = self.mount / "Music"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_volume_plist_parsing():
    data = {"VolumeUUID": "ABC-1", "VolumeName": "SD", "FilesystemName": "ExFAT"}
    v = parse_volume_plist(data, Path("/Volumes/SD"), 123, 7)
    assert (v.uuid, v.name, v.fs, v.free_bytes, v.dev) == ("ABC-1", "SD", "ExFAT", 123, 7)
    assert parse_volume_plist({"VolumeName": "x"}, Path("/x"), 1, 1) is None      # no UUID: cannot be identified
    fixed = parse_volume_plist({"VolumeUUID": "U", "Internal": True, "RemovableMedia": False}, Path("/Volumes/Recovery"), 1, 1)
    assert fixed.internal and not fixed.removable                                  # a fixed disk is never offered as a card
    sd = parse_volume_plist({"VolumeUUID": "U", "Internal": True, "RemovableMedia": True}, Path("/Volumes/SD"), 1, 1)
    assert sd.removable                                                            # a card in the Mac's own SD slot


def test_the_macs_own_disk_and_a_second_registration_are_refused(w, tmp_path):
    own = VolumeInfo(tmp_path, "Macintosh HD", "UUID-OWN", "APFS", 10**9, dev=os.stat(w.lib).st_dev)
    with pytest.raises(ValueError, match="same disk as your library"):
        register_device(w.conn, own, "oops", w.lib)
    with pytest.raises(ValueError, match="already registered"):
        register_device(w.conn, w.vol, "again", w.lib)


def test_a_card_is_found_by_uuid_never_by_name(w):
    imposter = VolumeInfo(Path("/Volumes/RUNCARD"), "RUNCARD", "UUID-OTHER", "exFAT", 10**9, dev=-2)   # same name
    assert find_card(w.conn, [imposter]) is None
    assert find_card(w.conn, [imposter, w.vol])[1] is w.vol


def test_plan_covers_selected_playlists_only_and_maps_merged_duplicates_to_the_kept_track(w):
    w.playlist("P1", "Running")
    w.playlist("P2", "Not synced", selected=0)
    w.song("a", "Alpha", {"P1": 0})
    w.song("b", "Beta", {"P2": 0})                       # only in a playlist that is not synced
    w.song("keep", "Gamma", {"P1": 1})
    w.conn.execute("INSERT INTO playlist_items VALUES ('P1', 'loser', 2)")
    w.conn.execute("INSERT INTO aliases (video_id, winner_id, merged_at) VALUES ('loser', 'keep', 'x')")
    w.song("nocopy", "Delta", {"P1": 3}, built=False)    # no player copy yet
    p = w.plan()
    assert [i.filename for i in p.adds] == ["Alpha.m4a", "Gamma.m4a"]
    assert p.playlists == {"Running.m3u8": ["Alpha.m4a", "Gamma.m4a"]}


def test_apply_writes_files_manifest_and_relative_playlists_and_a_second_plan_is_empty(w):
    w.playlist("P1", "Running")
    w.playlist("P2", "Gym")
    w.song("a", "Alpha", {"P1": 0, "P2": 1})
    w.song("b", "Beta", {"P1": 1})
    res = w.apply()
    assert (res.added, res.removed, res.playlists) == (2, 0, 2)
    assert w.on_card() == ["Alpha.m4a", "Beta.m4a", "Gym.m3u8", "Running.m3u8"]
    assert (w.mount / "Music" / "Running.m3u8").read_text(encoding="utf-8") == (
        "#EXTM3U\n#EXTINF:200,Artist - Alpha\nAlpha.m4a\n#EXTINF:200,Artist - Beta\nBeta.m4a\n")
    assert (w.mount / "Music" / "Alpha.m4a").read_bytes() == (w.player / "Alpha.m4a").read_bytes()
    assert not [n for n in w.on_card() if n.endswith(card.TMP_SUFFIX)]
    assert w.plan().changes == 0 and card_plan_id(w.plan()) == card_plan_id(w.plan())


def test_a_song_still_in_another_selected_playlist_stays_on_the_card(w):
    w.playlist("P1", "Running")
    w.playlist("P2", "Gym")
    w.song("a", "Alpha", {"P1": 0, "P2": 0})
    w.song("b", "Beta", {"P1": 1})
    w.apply()
    w.conn.execute("DELETE FROM playlist_items WHERE playlist_id = 'P1'")     # removed from Running only
    p = w.plan()
    assert [r.filename for r in p.removes] == ["Beta.m4a"]                    # Alpha stays: Gym still has it
    w.apply(p, force=True)                                                    # (1 of 2 is over the guard)
    assert "Alpha.m4a" in w.on_card() and "Beta.m4a" not in w.on_card()


def test_only_files_in_the_manifest_are_ever_deleted(w):
    w.playlist("P1", "Running")
    for i in range(10):
        w.song(f"v{i}", f"Song{i}", {"P1": i})
    w.apply()
    (w.mount / "Music" / "My own song.mp3").write_bytes(b"the owner's file")
    (w.mount / "Music" / ".DS_Store").write_bytes(b"x")
    w.conn.execute("DELETE FROM playlist_items WHERE video_id IN ('v0')")
    w.apply()
    assert (w.mount / "Music" / "My own song.mp3").read_bytes() == b"the owner's file"
    assert (w.mount / "Music" / ".DS_Store").exists() and "Song0.m4a" not in w.on_card()


def test_a_foreign_file_with_our_name_blocks_the_plan_and_an_identical_one_is_adopted(w):
    w.playlist("P1", "Running")
    src = w.song("a", "Alpha", {"P1": 0})
    w.song("b", "Beta", {"P1": 1})
    folder = w.mount / "Music"
    folder.mkdir()
    (folder / "Alpha.m4a").write_bytes(b"someone else's Alpha")
    (folder / "Beta.m4a").write_bytes((w.player / "Beta.m4a").read_bytes())    # the very same bytes
    p = w.plan()
    assert p.blocked == "conflict" and p.conflicts == ["Alpha.m4a"] and [i.filename for i in p.adopted] == ["Beta.m4a"]
    with pytest.raises(OSError, match="not ours"):
        w.apply(p)
    assert (folder / "Alpha.m4a").read_bytes() == b"someone else's Alpha"     # never overwritten
    (folder / "Alpha.m4a").write_bytes(src.read_bytes())
    p = w.plan()
    assert p.blocked is None and len(p.adopted) == 2
    w.apply(p)
    assert w.plan().changes == 0


def test_removing_more_than_a_fifth_stops_unless_forced(w):
    w.playlist("P1", "Running")
    for i in range(10):
        w.song(f"v{i}", f"Song{i}", {"P1": i})
    w.apply()
    w.conn.execute("DELETE FROM playlist_items WHERE video_id IN ('v0', 'v1', 'v2')")
    p = w.plan()
    assert p.blocked == "guard" and len(p.removes) == 3
    with pytest.raises(OSError, match="guard"):
        w.apply(p)
    assert len([n for n in w.on_card() if n.endswith(".m4a")]) == 10
    w.apply(p, force=True)
    assert len([n for n in w.on_card() if n.endswith(".m4a")]) == 7


def test_not_enough_space_stops_before_the_first_copy(w):
    w.playlist("P1", "Running")
    w.song("a", "Alpha", {"P1": 0}, size=5000)
    p = w.plan(free=card.SPACE_MARGIN + 100)
    assert p.blocked == "space"
    with pytest.raises(OSError, match="free space"):
        w.apply(p)
    assert w.on_card() == []


def test_a_pulled_card_mid_sync_is_finished_cleanly_by_the_next_run(w):
    w.playlist("P1", "Running")
    for i in range(4):
        w.song(f"v{i}", f"Song{i}", {"P1": i})
    calls = []

    def flaky(src, folder, name):
        calls.append(name)
        if len(calls) == 3:
            (folder / f"{name}{card.TMP_SUFFIX}").write_bytes(b"half a file")      # the card was pulled mid-copy
            raise OSError("card removed")
        return copy_verified(src, folder, name)
    with pytest.raises(OSError, match="card removed"):
        w.apply(copy=flaky)
    assert any(n.endswith(card.TMP_SUFFIX) for n in w.on_card())
    assert w.conn.execute("SELECT status FROM card_runs").fetchone()[0] == "running"
    p = w.plan()
    assert len(p.adds) == 2                                                           # the two not yet copied
    w.apply(p)
    assert [n for n in w.on_card() if n.endswith(".m4a")] == [f"Song{i}.m4a" for i in range(4)]
    assert not any(n.endswith(card.TMP_SUFFIX) for n in w.on_card())
    assert w.conn.execute("SELECT status FROM card_runs ORDER BY id").fetchall()[0][0] == "interrupted"
    assert w.plan().changes == 0


def test_a_failed_write_never_leaves_a_file_under_the_final_name(w, monkeypatch):
    src = w.player / "x.m4a"
    src.write_bytes(os.urandom(100))
    folder = w.mount / "Music"
    folder.mkdir()
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        copy_verified(src, folder, "x.m4a")
    assert list(folder.iterdir()) == []


def test_a_rebuilt_player_copy_is_updated_on_the_card(w):
    w.playlist("P1", "Running")
    path = w.song("a", "Alpha", {"P1": 0}, size=500)
    w.apply()
    path.write_bytes(os.urandom(700))                                                 # re-tagged: different bytes
    p = w.plan()
    assert [i.filename for i in p.updates] == ["Alpha.m4a"] and p.adds == [] and p.removes == []
    w.apply(p)
    assert (w.mount / "Music" / "Alpha.m4a").read_bytes() == path.read_bytes()


def test_a_file_the_owner_deleted_from_the_card_is_put_back(w):
    w.playlist("P1", "Running")
    w.song("a", "Alpha", {"P1": 0})
    w.apply()
    (w.mount / "Music" / "Alpha.m4a").unlink()
    assert [i.filename for i in w.plan().adds] == ["Alpha.m4a"]


def test_a_playlist_that_is_no_longer_synced_loses_its_file_on_the_card(w):
    w.playlist("P1", "Running")
    w.playlist("P2", "Gym")
    w.song("a", "Alpha", {"P1": 0, "P2": 0})
    w.apply()
    assert "Gym.m3u8" in w.on_card()
    w.conn.execute("UPDATE playlists SET selected = 0 WHERE id = 'P2'")
    w.apply()
    assert "Gym.m3u8" not in w.on_card() and "Running.m3u8" in w.on_card()


def test_undo_reverses_the_last_run_only(w):
    w.playlist("P1", "Running")
    w.song("a", "Alpha", {"P1": 0})
    w.song("b", "Beta", {"P1": 1})
    w.apply()                                                                         # run 1: adds Alpha, Beta
    w.conn.execute("DELETE FROM playlist_items WHERE video_id = 'a'")
    w.song("c", "Gamma", {"P1": 2})
    w.apply(force=True)                                                               # run 2: removes Alpha, adds Gamma
    assert [n for n in w.on_card() if n.endswith(".m4a")] == ["Beta.m4a", "Gamma.m4a"]
    w.conn.execute("INSERT OR IGNORE INTO playlist_items VALUES ('P1', 'a', 0)")      # (the plan after undo wants Alpha)
    res = undo_last_run(w.conn, w.dev, w.mount, "Music", w.plan())
    assert [n for n in w.on_card() if n.endswith(".m4a")] == ["Alpha.m4a", "Beta.m4a"]
    assert (res.added, res.removed) == (1, 1)
    assert w.conn.execute("SELECT status FROM card_runs ORDER BY id").fetchall()[1][0] == "undone"
    with pytest.raises(ValueError, match="nothing to undo"):
        undo_last_run(w.conn, 999, w.mount, "Music")


def test_state_facts_for_the_command_and_the_skill(w, tmp_path):
    fresh = db.connect(tmp_path / "other.db")
    assert card_state(fresh, w.lib, [w.vol])["state"] == "unregistered"
    assert card_state(fresh, w.lib, [w.vol])["candidates"][0]["uuid"] == "UUID-CARD"
    assert card_state(w.conn, w.lib, [])["state"] == "not_inserted"
    w.playlist("P1", "Running")
    w.song("a", "Alpha", {"P1": 0})
    s = card_state(w.conn, w.lib, [w.vol])
    assert (s["state"], s["adds"], s["removes"], s["device"]) == ("ready", 1, 0, "shanling") and len(s["plan_id"]) == 12


def test_a_leftover_temp_file_of_a_track_that_is_no_longer_wanted_is_cleaned_up_too(w):
    w.playlist("P1", "Running")
    w.song("a", "Alpha", {"P1": 0})
    folder = w.mount / "Music"
    folder.mkdir()
    (folder / f"Ghost.m4a{card.TMP_SUFFIX}").write_bytes(b"left by a pulled card")
    w.apply()
    assert not any(n.endswith(card.TMP_SUFFIX) for n in w.on_card())


def test_internal_fixed_volumes_are_not_offered_as_cards(tmp_path, monkeypatch):
    vols = tmp_path / "Volumes"
    for n in ("Recovery", "SDCARD"):
        (vols / n).mkdir(parents=True)
    info = {"Recovery": VolumeInfo(vols / "Recovery", "Recovery", "U1", "APFS", 1, dev=-5, internal=True, removable=False),
            "SDCARD": VolumeInfo(vols / "SDCARD", "SDCARD", "U2", "exFAT", 1, dev=-6, internal=True, removable=True)}
    monkeypatch.setattr(card, "volume_info", lambda p: info[p.name])
    assert [v.name for v in card.candidate_volumes(tmp_path, vols)] == ["SDCARD"]
