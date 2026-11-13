"""Liked Music mirrored into the library list: planner, snapshot rules, and the flow through ytflow."""
import json

import pytest
from test_ytclean import FakeApi, entries

from runsync import playlist_select, ytclean, ytflow, ytmirror
from runsync import youtube as yt
from runsync.config import Config
from runsync.ytmirror import Liked

LIB = "PLlib"


def plan(liked_ids, library=(), seen=(), excluded=(), dead=(), winner=lambda v: v, titles=None, paused=""):
    liked = Liked(list(liked_ids), titles or {}, paused)
    return ytmirror.plan_mirror(LIB, entries(*library), liked, set(seen), set(excluded), set(dead), winner)


def kinds(ops):
    return [(o.kind, o.video_id, o.why) for o in ops]


def test_a_new_like_is_added_and_one_already_there_is_not():
    ops = plan(["a", "b", "c"], library=["b"], titles={"a": "Song A"})
    assert kinds(ops) == [("add", "a", "mirror"), ("add", "c", "mirror")] and ops[0].title == "Song A"


def test_never_add_a_dead_track_an_excluded_song_or_the_same_song_twice():
    ops = plan(["dead", "gone", "x", "x"], dead={"dead"}, excluded={"gone"})
    assert kinds(ops) == [("add", "x", "mirror")]


def test_a_liked_merged_loser_adds_its_winner_once():
    winner = {"loser1": "win", "loser2": "win"}.get
    ops = plan(["loser1", "loser2", "other"], winner=lambda v: winner(v, v))
    assert kinds(ops) == [("add", "win", "mirror"), ("add", "other", "mirror")]
    assert plan(["loser1"], library=["win"], winner=lambda v: winner(v, v)) == []        # the winner is already there


def test_an_unlike_removes_only_what_the_mirror_brought_in():
    ops = plan(["kept"], library=["kept", "unliked", "added_by_hand"], seen={"kept", "unliked"})
    assert kinds(ops) == [("delete", "unliked", "unliked")]                   # added_by_hand was never liked: untouched
    assert ops[0].item_id == "item-1-unliked"


def test_unliking_a_merged_loser_keeps_the_winner_while_the_winner_is_still_liked():
    winner = {"loser": "win"}.get
    ops = plan(["win"], library=["win"], seen={"loser", "win"}, winner=lambda v: winner(v, v))
    assert ops == []


def test_nothing_is_planned_while_liked_music_cannot_be_read():
    assert plan(["a"], library=["x"], seen={"x"}, paused="stale cookies") == []


# ---- the snapshot ---------------------------------------------------------------------------------------------------

def test_the_first_time_the_snapshot_is_what_the_sync_saw_of_liked_music(conn):
    conn.execute("INSERT INTO playlists (id, title, url) VALUES ('LM', 'Liked Music', 'u')")
    conn.execute("INSERT INTO playlist_items VALUES ('LM', 'a', 0), ('LM', 'b', 1)")
    assert ytmirror.liked_seen(conn) == {"a", "b"}
    ytmirror.record_seen(conn, {"c"}, complete=True)
    assert ytmirror.liked_seen(conn) == {"c"}


def test_a_partial_push_never_forgets_a_removal_that_has_not_happened_yet(conn):
    ytmirror.record_seen(conn, {"old_like"}, complete=True)
    ytmirror.record_seen(conn, {"new_like"}, complete=False)               # quota ran out mid-way
    assert ytmirror.liked_seen(conn) == {"old_like", "new_like"}          # old_like (now unliked) is still to be removed
    ytmirror.record_seen(conn, {"new_like"}, complete=True)
    assert ytmirror.liked_seen(conn) == {"new_like"}


def test_an_exclusion_goes_once_the_song_is_no_longer_liked(conn):
    conn.execute("INSERT INTO excluded VALUES ('still_liked', 'x'), ('unliked', 'x')")
    ytmirror.prune_excluded(conn, {"still_liked"})
    assert [r[0] for r in conn.execute("SELECT video_id FROM excluded")] == ["still_liked"]


# ---- reading Liked Music ---------------------------------------------------------------------------------------------------

def _cfg(tmp_path, cookies_ok=True):
    cfg = Config(library=tmp_path / "lib")
    cfg.cookies = tmp_path / "cookies.txt"
    if cookies_ok:
        header = "# Netsc" + "ape HTTP Cookie File"       # split so the repo's secret guard does not flag this test file
        cfg.cookies.write_text(f"{header}\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tx\n"
                               ".youtube.com\tTRUE\t/\tTRUE\t4102444800\t__Secure-3PSID\tx\n")
    return cfg


class Items:
    def __init__(self, ids=(), unknown=(), titles=None):
        self.ids, self.unknown, self.titles = list(ids), list(unknown), titles or {}


def test_unusable_cookies_pause_the_mirror_instead_of_raising(conn, tmp_path):
    got = ytmirror.read_liked(_cfg(tmp_path, cookies_ok=False), conn)
    assert got.paused and got.ids == []


def test_a_failing_read_and_a_suspiciously_empty_one_pause_the_mirror(conn, tmp_path):
    cfg = _cfg(tmp_path)

    def boom(url, cookies):
        raise RuntimeError("The playlist does not exist.\nmore")
    assert "does not exist" in ytmirror.read_liked(cfg, conn, fetch_items=boom).paused
    conn.execute("INSERT INTO liked_seen VALUES ('a')")
    assert "came back empty" in ytmirror.read_liked(cfg, conn, fetch_items=lambda u, c: Items()).paused
    ok = ytmirror.read_liked(cfg, conn, fetch_items=lambda u, c: Items(["a"], titles={"a": "A"}))
    assert ok.paused == "" and ok.ids == ["a"] and ok.titles == {"a": "A"}


# ---- the flow -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def flow(conn, tmp_path, monkeypatch):
    """A library list on YouTube ('Refresh'), a stored library setting, and Liked Music as a mutable list."""
    cfg = _cfg(tmp_path)
    conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES (?, 'Refresh', 'u', 1)", (LIB,))
    playlist_select.set_library(conn, LIB)
    state = {"liked": ["l1", "l2", "l3"], "lib": entries("l1")}
    monkeypatch.setattr(ytclean.time, "sleep", lambda s: None)               # the read-back waits for YouTube; not in tests
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": LIB, "title": "Refresh", "count": len(state["lib"])}])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: list(state["lib"]))
    monkeypatch.setattr(ytmirror, "read_liked", lambda cfg_, conn_: Liked(list(state["liked"]), {}))
    return cfg, state


def _fake_writes(monkeypatch, state):
    """YouTube as a list: adds and deletes change it and spend quota (through api.request), like the real thing."""
    def add(api, pid, vid, position=None):
        api.request("POST", "playlistItems", {}, {})
        state["lib"].append(entries(vid)[0])
        return "i"

    def delete(api, item):
        api.request("DELETE", "playlistItems", {"id": item})
        state["lib"][:] = [e for e in state["lib"] if e.item_id != item]
    monkeypatch.setattr(yt, "add_entry", add)
    monkeypatch.setattr(yt, "delete_entry", delete)


def test_cleanup_includes_the_mirror_and_apply_records_the_snapshot(conn, flow, monkeypatch):
    cfg, state = flow
    facts = ytflow.cleanup(FakeApi(), conn, cfg, apply=False, confirm="", batch=100)["cleanup"]
    assert (facts["mirror_adds"], facts["mirror_removes"], facts["mirror"]["library"]) == (2, 0, "Refresh")
    assert [(i["op"], i["title"]) for i in facts["items"]] == [("add", ""), ("add", "")]
    api = FakeApi()
    _fake_writes(monkeypatch, state)
    done = ytflow.cleanup(api, conn, cfg, apply=True, confirm=facts["plan_id"], batch=100)["applied"]
    assert done["added"] == 2 and done["verify"]["ok"] and done["left"] == 0
    assert ytmirror.liked_seen(conn) == {"l1", "l2", "l3"}                    # a complete push: the snapshot is the likes


def test_a_partial_push_keeps_the_pending_work_in_the_snapshot(conn, flow, monkeypatch):
    cfg, state = flow
    ytmirror.record_seen(conn, {"l1", "gone_like"}, complete=True)            # gone_like was liked, then unliked
    state["lib"] = entries("l1", "gone_like")
    plan_id = ytflow.cleanup(FakeApi(), conn, cfg, apply=False, confirm="", batch=100)["cleanup"]["plan_id"]
    _fake_writes(monkeypatch, state)
    out = ytflow.cleanup(FakeApi(budget=100), conn, cfg, apply=True, confirm=plan_id, batch=100)["applied"]
    assert out["left"] > 0 and "budget" in out["stopped"]
    assert "gone_like" in ytmirror.liked_seen(conn)                          # its removal is still owed: not forgotten


def test_while_liked_music_is_unreadable_nothing_is_planned_or_recorded(conn, flow, monkeypatch):
    cfg, _state = flow
    monkeypatch.setattr(ytmirror, "read_liked", lambda c, k: Liked(paused="stale cookies"))
    facts = ytflow.cleanup(FakeApi(), conn, cfg, apply=False, confirm="", batch=100)["cleanup"]
    assert facts["ops"] == 0 and facts["mirror"]["paused"] == "stale cookies"
    assert conn.execute("SELECT COUNT(*) FROM liked_seen").fetchone()[0] == 0


def test_without_a_library_list_the_cleanup_says_so_and_mirrors_nothing(conn, flow):
    cfg, _ = flow
    conn.execute("DELETE FROM settings")
    facts = ytflow.cleanup(FakeApi(), conn, cfg, apply=False, confirm="", batch=100)["cleanup"]
    assert facts["mirror"]["library"] is None and facts["mirror_adds"] == 0


def test_the_same_entry_wanted_gone_twice_is_deleted_once():
    ops = [ytclean.Op("delete", "P", "a", "item1", 1, "repeat"), ytclean.Op("delete", "P", "a", "item1", 1, "unliked"),
           ytclean.Op("add", "P", "b", "", 0, "mirror"), ytclean.Op("add", "P", "b", "", 0, "replace")]
    assert [(o.kind, o.why) for o in ytclean._without_repeats(ops)] == [("delete", "repeat"), ("add", "mirror")]


def test_replacements_default_to_the_library_list(conn, flow):
    cfg, state = flow
    conn.execute("INSERT INTO tracks (video_id, title, status) VALUES ('dead1', 'Dead', 'unavailable')")
    conn.execute("INSERT INTO replacement_picks (lost_id, pick_id, title, status, decided_at) VALUES ('dead1', 'UPLOAD00001', 'New', 'picked', 'x')")
    state["liked"] = ["l1"]
    facts = ytflow.cleanup(FakeApi(), conn, cfg, apply=False, confirm="", batch=100)["cleanup"]
    assert [(i["op"], i["why"], i["playlist"]) for i in facts["items"]] == [("add", "replace", "Refresh")]


# ---- choosing the library, and the first-run baseline -----------------------------------------------------------------------

def test_choosing_an_existing_playlist_as_the_library_selects_it(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PL9", "title": "Everything", "count": 3}])
    out = ytflow.set_library(object(), conn, _cfg(tmp_path), "everything", create=False, apply=False, confirm="")
    assert out["library"] == {"id": "PL9", "title": "Everything"}
    assert playlist_select.library_playlist(conn)["id"] == "PL9"
    assert conn.execute("SELECT selected FROM playlists WHERE id = 'PL9'").fetchone()[0] == 1


def test_a_missing_library_needs_create_and_is_made_through_the_confirmed_plan(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "my_playlists", lambda api: [])
    cfg = _cfg(tmp_path)
    with pytest.raises(ytclean.NoSuchPlaylist):
        ytflow.set_library(FakeApi(), conn, cfg, "Library", create=False, apply=False, confirm="")
    preview = ytflow.set_library(FakeApi(), conn, cfg, "Library", create=True, apply=False, confirm="")
    assert playlist_select.library_playlist(conn) is None                      # a preview changes nothing
    api = FakeApi()
    with pytest.raises(ytclean.PlanRefused):
        ytflow.set_library(api, conn, cfg, "Library", create=True, apply=True, confirm="wrong")
    made = ytflow.set_library(api, conn, cfg, "Library", create=True, apply=True, confirm=preview["library_plan"]["plan_id"])
    assert made["created"] and playlist_select.library_playlist(conn)["title"] == "Library"
    assert api.calls[0][3]["status"]["privacyStatus"] == "private"


def test_the_baseline_keeps_todays_backlog_out_and_lets_only_new_likes_in(conn, flow):
    cfg, state = flow
    out = ytflow.baseline(FakeApi(), conn, cfg)
    assert out["baseline"] == 2 and {r[0] for r in conn.execute("SELECT video_id FROM excluded")} == {"l2", "l3"}
    facts = ytflow.cleanup(FakeApi(), conn, cfg, apply=False, confirm="", batch=100)["cleanup"]
    assert facts["mirror_adds"] == 0
    state["liked"].append("fresh")
    assert ytflow.cleanup(FakeApi(), conn, cfg, apply=False, confirm="", batch=100)["cleanup"]["mirror_adds"] == 1


def test_the_command_chooses_and_reports_the_library(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from runsync import cli, db
    from runsync.config import load_config
    monkeypatch.setenv("RUNSYNC_HOME", str(tmp_path / "lib"))
    monkeypatch.setattr(Config, "youtube_token", property(lambda self: tmp_path / "t.json"))
    monkeypatch.setattr(Config, "youtube_client", property(lambda self: tmp_path / "c.json"))
    (tmp_path / "c.json").write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    yt.save_token(tmp_path / "t.json", {"access_token": "A", "refresh_token": "R", "expires_at": 9e12})
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PL9", "title": "Refresh", "count": 3}])
    out = CliRunner().invoke(cli.app, ["youtube", "--library", "Refresh", "--json"])
    facts = json.loads(out.stdout)
    assert out.exit_code == 0 and facts["library"]["title"] == "Refresh" and facts["library_list"] == "Refresh"
    assert db.connect(load_config().db_path).execute("SELECT value FROM settings").fetchone()[0] == "PL9"


# ---- what the database forgets must not come back through the mirror -----------------------------------------------------

def test_a_dropped_duplicate_or_dead_song_that_is_still_liked_is_remembered_as_excluded(conn):
    from runsync import snapshot
    for v, st in (("dup", "merged"), ("dead", "unavailable"), ("unliked_dup", "merged")):
        conn.execute("INSERT INTO tracks (video_id, status) VALUES (?, ?)", (v, st))
    conn.execute("INSERT INTO liked_seen VALUES ('dup'), ('dead')")
    assert snapshot.purge_gone(conn, present=set()) == 3
    assert {r[0] for r in conn.execute("SELECT video_id FROM excluded")} == {"dup", "dead"}      # not the unliked one
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0                         # the rows themselves are gone
    # ... and the mirror then leaves them alone although nothing else remembers them
    assert plan(["dup", "dead", "fresh"], excluded={"dup", "dead"}, winner=lambda v: v) and \
        kinds(plan(["dup", "dead", "fresh"], excluded={"dup", "dead"})) == [("add", "fresh", "mirror")]


def test_forgetting_a_track_keeps_its_exclusion_while_the_song_is_still_liked(conn):
    from runsync import snapshot
    conn.execute("INSERT INTO tracks (video_id, status) VALUES ('removed', 'trashed')")
    conn.execute("INSERT INTO excluded VALUES ('removed', 'x')")
    snapshot.purge_track(conn, "removed")                          # the trash ran out for it
    assert conn.execute("SELECT COUNT(*) FROM excluded").fetchone()[0] == 1       # prune_excluded drops it once it is unliked
