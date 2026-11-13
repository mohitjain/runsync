"""YouTube playlist cleanup: the plan (pure), the gated apply, the journal and undo. A fake API, no network."""
import json

import pytest

from runsync import youtube as yt
from runsync import ytclean
from runsync.dedupe import winner_of
from runsync.ytclean import Op, Plan, PlanRefused


def entries(*vids):
    return [yt.Entry(v, f"item-{n}-{v}", n, f"Title {v}", "Chan", True) for n, v in enumerate(vids)]


def plan_for(vids, winners=None, pid="PL1"):
    winners = winners or {}
    return ytclean.plan_playlist(pid, entries(*vids), lambda v: winners.get(v, v), lambda v: f"Title {v}")


def keys(ops):
    return [(o.kind, o.video_id, o.why) for o in ops]


def test_repeats_keep_the_first_entry_and_delete_the_rest():
    ops = plan_for(["a", "b", "a", "c", "a"])
    assert keys(ops) == [("delete", "a", "repeat"), ("delete", "a", "repeat")]
    assert [o.item_id for o in ops] == ["item-2-a", "item-4-a"]          # the first entry (item-0-a) stays


def test_a_loser_is_deleted_when_the_kept_upload_is_already_in_the_playlist():
    ops = plan_for(["win", "x", "lose"], {"lose": "win"})
    assert keys(ops) == [("delete", "lose", "loser")] and ops[0].keep_id == "win"


def test_a_loser_whose_winner_is_missing_is_swapped_add_first_then_delete():
    ops = plan_for(["x", "lose"], {"lose": "win"})
    assert keys(ops) == [("add", "win", "swap"), ("delete", "lose", "swap")]
    assert ops[0].position == 1 and ops[0].title == "Title win"


def test_two_losers_of_one_missing_winner_add_it_only_once():
    ops = plan_for(["l1", "l2"], {"l1": "win", "l2": "win"})
    assert keys(ops) == [("add", "win", "swap"), ("delete", "l1", "swap"), ("delete", "l2", "loser")]


def test_a_clean_playlist_has_nothing_to_do():
    assert plan_for(["a", "b", "c"]) == []


def test_resolve_winner_follows_chains_and_survives_loops(conn):
    for v, w in [("a", "b"), ("b", "c"), ("x", "y"), ("y", "x")]:
        conn.execute("INSERT INTO aliases (video_id, winner_id, merged_at) VALUES (?, ?, 't')", (v, w))
    assert winner_of(conn, "a") == "c" and winner_of(conn, "z") == "z"
    assert winner_of(conn, "x") in {"x", "y"}              # a loop ends instead of hanging


def test_plan_id_names_the_exact_operations():
    a, b = Plan(plan_for(["a", "a"])), Plan(plan_for(["a", "a", "a"]))
    assert ytclean.plan_id(a) == ytclean.plan_id(Plan(plan_for(["a", "a"]))) != ytclean.plan_id(b)


class FakeApi:
    """Records every write; `fail_after` raises a quota error on that call number."""
    def __init__(self, budget=8000, fail_after=None, bad_position=False):
        self.budget, self.calls, self.fail_after, self.bad_position, self._units = budget, [], fail_after, bad_position, 0

    def can_spend(self, units):
        return self._units + units <= self.budget

    def request(self, method, resource, params=None, body=None):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise yt.QuotaError("quota exhausted")
        if self.bad_position and method == "POST" and (body or {}).get("snippet", {}).get("position") is not None:
            raise yt.YouTubeError("invalidPosition")
        self.calls.append((method, resource, params, body))
        self._units += 50
        return {"id": f"new{len(self.calls)}"}


def swap_plan():
    return Plan(plan_for(["x", "lose", "x2", "x2"], {"lose": "win"}), {"PL1": "Running"})


def test_apply_without_the_matching_confirm_writes_nothing(conn):
    api, plan = FakeApi(), swap_plan()
    for wrong in ("", "nope", "0" * 12):
        with pytest.raises(PlanRefused, match="Not confirmed"):
            ytclean.apply_plan(api, conn, plan, wrong)
    assert api.calls == [] and conn.execute("SELECT COUNT(*) FROM youtube_runs").fetchone()[0] == 0


def test_apply_refuses_to_touch_liked_music_liked_videos_or_watch_later(conn):
    for pid in ("LM", "LL", "WL"):
        plan = Plan([Op("delete", pid, "v", "item", 0, "repeat")])
        api = FakeApi()
        with pytest.raises(PlanRefused, match="may not touch"):
            ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan))
        assert api.calls == []


def test_apply_adds_before_it_deletes_and_journals_every_step(conn):
    api, plan = FakeApi(), swap_plan()
    r = ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan))
    assert [(c[0], c[1]) for c in api.calls] == [("POST", "playlistItems"), ("DELETE", "playlistItems"), ("DELETE", "playlistItems")]
    assert api.calls[0][3]["snippet"]["resourceId"]["videoId"] == "win" and api.calls[1][2] == {"id": "item-1-lose"}
    assert (r.added, r.deleted, r.left, r.stopped) == (1, 2, 0, "")
    rows = conn.execute("SELECT op, video_id, playlist_item_id FROM youtube_journal ORDER BY id").fetchall()
    assert [(x["op"], x["video_id"]) for x in rows] == [("add", "win"), ("delete", "lose"), ("delete", "x2")]
    assert rows[0]["playlist_item_id"] == "new1"                          # the new entry's id, so undo can remove it
    assert conn.execute("SELECT status FROM youtube_runs").fetchone()[0] == "done"


def test_a_batch_runs_only_its_first_operations_and_the_rest_waits(conn):
    api, plan = FakeApi(), swap_plan()
    r = ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan), batch=2)
    assert len(api.calls) == 2 and r.left == 1 and r.added == 1 and r.deleted == 1   # the add and its swap delete, in that order


def test_the_daily_budget_stops_the_run_cleanly(conn):
    api, plan = FakeApi(budget=100), swap_plan()
    r = ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan))
    assert len(api.calls) == 2 and r.left == 1 and "budget" in r.stopped
    assert conn.execute("SELECT status FROM youtube_runs").fetchone()[0] == "stopped"


def test_a_quota_error_mid_run_stops_cleanly_and_keeps_the_journal(conn):
    api, plan = FakeApi(fail_after=1), swap_plan()
    r = ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan))
    assert r.added == 1 and r.deleted == 0 and "quota" in r.stopped
    assert [x["op"] for x in conn.execute("SELECT op FROM youtube_journal")] == ["add"]


def test_a_swap_never_deletes_the_loser_if_the_add_failed(conn):
    api, plan = FakeApi(fail_after=0), swap_plan()
    r = ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan))
    assert api.calls == [] and r.deleted == 0                              # nothing lost from the playlist


def test_undo_puts_deleted_entries_back_and_removes_added_ones_newest_first(conn):
    plan = swap_plan()
    ytclean.apply_plan(FakeApi(), conn, plan, ytclean.plan_id(plan))
    api = FakeApi()
    r = ytclean.undo_last_run(api, conn)
    assert (r.restored, r.removed, r.left) == (2, 1, 0)
    kinds = [(c[0], (c[3] or {}).get("snippet", {}).get("resourceId", {}).get("videoId") or c[2]["id"]) for c in api.calls]
    assert kinds == [("POST", "x2"), ("POST", "lose"), ("DELETE", "new1")]       # newest first: the swap undone in order
    assert conn.execute("SELECT status FROM youtube_runs").fetchone()[0] == "undone"
    assert conn.execute("SELECT COUNT(*) FROM youtube_journal").fetchone()[0] == 0
    assert ytclean.undo_last_run(FakeApi(), conn).restored == 0                  # nothing left to undo


def test_undo_is_resumable_and_retries_a_bad_position_at_the_end(conn):
    plan = swap_plan()
    ytclean.apply_plan(FakeApi(), conn, plan, ytclean.plan_id(plan))
    r = ytclean.undo_last_run(FakeApi(budget=50), conn)
    assert r.restored == 1 and r.left == 2 and "budget" in r.stopped
    api = FakeApi(bad_position=True)
    r = ytclean.undo_last_run(api, conn)
    assert (r.restored, r.removed, r.left) == (1, 1, 0)
    assert any(c[0] == "POST" and "position" not in c[3]["snippet"] for c in api.calls)


def test_build_plan_reads_only_owned_selected_playlists_and_never_the_special_ones(conn, monkeypatch):
    for pid, sel in [("PL1", 1), ("PL2", 0), ("LM", 1), ("LL", 1), ("WL", 1)]:
        conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES (?, ?, ?, ?)", (pid, pid, pid, sel))
    conn.execute("INSERT INTO aliases (video_id, winner_id, merged_at) VALUES ('lose', 'win', 't')")
    asked = []
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": p, "title": p, "count": 1} for p in ("PL1", "PL2", "LM", "LL", "WL", "PL3")])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: asked.append(pid) or entries("a", "lose", "a"))
    plan = ytclean.build_plan(object(), conn)
    assert asked == ["PL1"]                                    # not PL2 (unselected), PL3 (not in the db), LM/LL/WL (never)
    assert keys(plan.ops) == [("add", "win", "swap"), ("delete", "lose", "swap"), ("delete", "a", "repeat")]
    assert ytclean.summary(plan)["ops"] == 3 and ytclean.days_needed(plan, 8000) == 1 and ytclean.days_needed(Plan(), 8000) == 0


def test_build_plan_puts_every_add_before_any_delete(conn, monkeypatch):
    for pid in ("PL1", "PL2"):
        conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES (?, ?, ?, 1)", (pid, pid, pid))
    conn.execute("INSERT INTO aliases (video_id, winner_id, merged_at) VALUES ('lose', 'win', 't')")
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": p, "title": p, "count": 1} for p in ("PL1", "PL2")])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: entries("a", "a", "lose"))
    kinds = [o.kind for o in ytclean.build_plan(object(), conn).ops]
    assert kinds == sorted(kinds, key=lambda k: k != "add") and kinds.count("add") == 2


# ---- the command ------------------------------------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from runsync import cli
    from runsync.config import Config
    monkeypatch.setenv("RUNSYNC_HOME", str(tmp_path / "lib"))
    monkeypatch.setattr(Config, "youtube_token", property(lambda self: tmp_path / "t.json"))
    monkeypatch.setattr(Config, "youtube_client", property(lambda self: tmp_path / "c.json"))
    (tmp_path / "c.json").write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    yt.save_token(tmp_path / "t.json", {"access_token": "A", "refresh_token": "R", "expires_at": 9e12})
    monkeypatch.setattr(ytclean.time, "sleep", lambda s: None)      # the read-back waits for YouTube; not in tests
    calls = []
    monkeypatch.setattr(yt, "_http", lambda method, url, headers, body: calls.append(method) or (200, {}))
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PL1", "title": "Running", "count": 3}])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: entries("a", "a", "b"))
    return cli, calls


def _run(cli, *args):
    from typer.testing import CliRunner
    return CliRunner().invoke(cli.app, ["youtube", *args])


def _select_playlist():
    from runsync import db
    from runsync.config import load_config
    cfg = load_config()
    cfg.ensure_dirs()
    c = db.connect(cfg.db_path)
    c.execute("INSERT INTO playlists (id, title, url, selected) VALUES ('PL1', 'Running', 'u', 1)")
    c.commit()


def test_the_cleanup_preview_changes_nothing_and_names_the_plan(cli_env):
    cli, calls = cli_env
    _select_playlist()
    out = _run(cli, "--cleanup", "--json")
    facts = json.loads(out.stdout)
    assert out.exit_code == 0 and facts["cleanup"]["deletes"] == 1 and facts["cleanup"]["plan_id"]
    assert facts["cleanup"]["items"][0]["why"] == "repeat" and calls == []      # reads only (my_playlists/entries are faked)


def test_apply_through_the_command_needs_the_exact_plan_id(cli_env):
    cli, calls = cli_env
    _select_playlist()
    plan_id = json.loads(_run(cli, "--cleanup", "--json").stdout)["cleanup"]["plan_id"]
    refused = _run(cli, "--apply", "--confirm", "wrong", "--json")
    assert refused.exit_code == 2 and calls == [] and "Not confirmed" in json.loads(refused.stdout)["error"]
    assert _run(cli, "--apply", "--json").exit_code == 2 and calls == []
    ok = _run(cli, "--apply", "--confirm", plan_id, "--json")
    applied = json.loads(ok.stdout)["applied"]
    assert ok.exit_code == 0 and applied["deleted"] == 1 and calls == ["DELETE"]
    assert applied["items"][0]["title"] == "Title a" and applied["items"][0]["why"] == "repeat"      # which song went
    assert applied["verify"]["checked"] is True


def test_cleanup_when_not_signed_in_says_so(tmp_path, monkeypatch):
    from runsync import cli
    from runsync.config import Config
    monkeypatch.setenv("RUNSYNC_HOME", str(tmp_path / "lib"))
    monkeypatch.setattr(Config, "youtube_token", property(lambda self: tmp_path / "none.json"))
    out = _run(cli, "--cleanup", "--json")
    assert out.exit_code == 2 and "sign in" in json.loads(out.stdout)["error"].lower()


# ---- the read-back after the writes ------------------------------------------------------------------------------------

def _applied(ops, before):
    return ytclean.Applied(run_id=1, done=len(ops), ops_done=ops, before=before, playlists={"PL1": "Running"})


def _live(monkeypatch, *states):
    """Each read of the playlist returns the next state (the last one repeats): lets a test model YouTube's lag."""
    reads = []

    def read(api, pid):
        reads.append(pid)
        return states[min(len(reads) - 1, len(states) - 1)]
    monkeypatch.setattr(yt, "playlist_entries", read)
    return reads


OPS = [Op("add", "PL1", "win", "", 1, "swap", "Win", "win", "Win"),
       Op("delete", "PL1", "lose", "item-1-lose", 1, "swap", "Lose", "win", "Win"),
       Op("delete", "PL1", "a", "item-3-a", 3, "repeat", "A", "a", "A")]


def test_verify_confirms_every_change_and_the_counts(monkeypatch):
    live = [yt.Entry("a", "item-0-a", 0, "A", "c", True), yt.Entry("win", "new", 1, "Win", "c", True)]
    _live(monkeypatch, live)
    sleeps = []
    v = ytclean.verify(object(), _applied(OPS, {"PL1": 3}), sleep=sleeps.append)
    assert v["checked"] and v["ok"] and v["confirmed"] == 3 and v["problems"] == [] and sleeps == []
    assert v["playlists"] == [{"id": "PL1", "title": "Running", "before": 3, "after": 2, "expected": 2}]


def test_verify_waits_out_youtubes_lag_then_passes(monkeypatch):
    stale = [yt.Entry("a", "item-0-a", 0, "A", "c", True), yt.Entry("lose", "item-1-lose", 1, "Lose", "c", True),
             yt.Entry("a", "item-3-a", 3, "A", "c", True)]
    fresh = [yt.Entry("a", "item-0-a", 0, "A", "c", True), yt.Entry("win", "new", 1, "Win", "c", True)]
    reads = _live(monkeypatch, stale, fresh)
    sleeps = []
    v = ytclean.verify(object(), _applied(OPS, {"PL1": 3}), sleep=sleeps.append)
    assert v["ok"] and len(reads) == 2 and sleeps == [3]


def test_verify_reports_an_entry_that_is_still_there_after_the_retries(monkeypatch):
    still = [yt.Entry("a", "item-0-a", 0, "A", "c", True), yt.Entry("win", "new", 1, "Win", "c", True),
             yt.Entry("a", "item-3-a", 3, "A", "c", True)]
    _live(monkeypatch, still)
    sleeps = []
    v = ytclean.verify(object(), _applied(OPS, {"PL1": 3}), sleep=sleeps.append)
    assert not v["ok"] and v["confirmed"] == 2 and sleeps == [3, 8, 15]
    assert v["problems"][0]["problem"] == "the entry is still in the playlist" and v["problems"][0]["title"] == "A"


def test_verify_reports_a_kept_upload_that_is_missing_and_a_song_that_vanished(monkeypatch):
    _live(monkeypatch, [yt.Entry("x", "item-9-x", 9, "X", "c", True)])
    v = ytclean.verify(object(), _applied(OPS[1:], {"PL1": 3}), sleep=lambda s: None)
    kinds = {p["problem"] for p in v["problems"]}
    assert not v["ok"] and kinds == {"the kept upload is missing from the playlist", "no copy of the song is left in the playlist"}


def test_verify_when_google_will_not_answer_says_it_could_not_check(monkeypatch):
    def boom(api, pid):
        raise yt.QuotaError("quota used up")
    monkeypatch.setattr(yt, "playlist_entries", boom)
    v = ytclean.verify(object(), _applied(OPS, {"PL1": 3}), sleep=lambda s: None)
    assert v["checked"] is False and "quota" in v["reason"]


def test_verify_with_nothing_done_is_trivially_fine():
    assert ytclean.verify(object(), ytclean.Applied(run_id=1))["ok"]


def test_apply_returns_what_it_really_did_for_the_read_back(conn):
    api, plan = FakeApi(), swap_plan()
    plan.counts["PL1"] = 4
    r = ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan), batch=2)
    assert [(o.kind, o.video_id) for o in r.ops_done] == [("add", "win"), ("delete", "lose")]    # only what ran
    assert r.before == {"PL1": 4} and r.playlists == {"PL1": "Running"}


# ---- dead songs and in-place replacements -------------------------------------------------------------------------------

def dead_entries(*specs):
    """specs: (video_id, available)"""
    return [yt.Entry(v, f"item-{n}-{v}", n, f"Title {v}" if ok else "Deleted video", "Chan", ok) for n, (v, ok) in enumerate(specs)]


def test_a_dead_song_with_no_replacement_is_deleted_and_an_unplayable_entry_too():
    ops = ytclean.plan_playlist("PL1", dead_entries(("a", True), ("gone", True), ("hidden", False)), lambda v: v,
                                lambda v: f"Title {v}", picks={}, dead={"gone"})
    assert [(o.kind, o.video_id, o.why) for o in ops] == [("delete", "gone", "dead"), ("delete", "hidden", "dead")]
    assert ops[0].title == "Title gone"                              # the owner sees the song's name, not "Deleted video"


def test_a_picked_replacement_goes_in_place_add_first_then_the_dead_entry_goes():
    ops = ytclean.plan_playlist("PL1", dead_entries(("a", True), ("gone", True)), lambda v: v, lambda v: f"Title {v}",
                                picks={"gone": "new"}, dead=set())
    assert [(o.kind, o.video_id, o.why) for o in ops] == [("add", "new", "replace"), ("delete", "gone", "replace")]
    assert ops[0].position == 1 and ops[1].keep_id == "new"


def test_two_dead_entries_with_the_same_pick_add_it_once_and_a_pick_already_there_is_not_added():
    twice = ytclean.plan_playlist("PL1", dead_entries(("g1", True), ("g2", True)), lambda v: v, lambda v: v,
                                  picks={"g1": "new", "g2": "new"}, dead=set())
    assert [(o.kind, o.video_id) for o in twice] == [("add", "new"), ("delete", "g1"), ("delete", "g2")]
    there = ytclean.plan_playlist("PL1", dead_entries(("new", True), ("g1", True)), lambda v: v, lambda v: v,
                                  picks={"g1": "new"}, dead=set())
    assert [(o.kind, o.video_id) for o in there] == [("delete", "g1")]


def test_build_plan_reads_dead_songs_and_picks_from_the_database(conn, monkeypatch):
    conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES ('PL1', 'Running', 'u', 1)")
    for v in ("d1", "d2", "d3"):
        conn.execute("INSERT INTO tracks (video_id, title, status) VALUES (?, ?, 'unavailable')", (v, f"Lost {v}"))
    conn.execute("INSERT INTO replacement_picks (lost_id, pick_id, title, status, decided_at) VALUES "
                 "('d1', 'UP1', 'Found one', 'picked', 'now'), ('d2', NULL, NULL, 'skipped', 'now')")
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PL1", "title": "Running", "count": 3}])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: dead_entries(("d1", True), ("d2", True), ("ok", True)))
    plan = ytclean.build_plan(object(), conn)
    assert keys(plan.ops) == [("add", "UP1", "replace"), ("delete", "d1", "replace"), ("delete", "d2", "dead")]
    assert plan.ops[0].title == "Found one"
    s = ytclean.summary(plan)
    assert s["playlists"][0]["dead"] == 1 and s["playlists"][0]["replaced"] == 1


def test_verify_checks_that_a_replacement_is_present_and_the_dead_entry_is_gone(monkeypatch):
    ops = [Op("add", "PL1", "new", "", 1, "replace", "New", "new", "New"),
           Op("delete", "PL1", "gone", "item-1-gone", 1, "replace", "Gone", "new", "New")]
    _live(monkeypatch, [yt.Entry("a", "item-0-a", 0, "A", "c", True)])
    v = ytclean.verify(object(), _applied(ops, {"PL1": 2}), sleep=lambda s: None)
    assert not v["ok"] and {p["problem"] for p in v["problems"]} == {"the song is not in the playlist",
                                                                    "the kept upload is missing from the playlist"}


# ---- after a verified push the database follows YouTube -----------------------------------------------------------------

def test_a_verified_push_updates_the_snapshot_and_deletes_what_it_made_dead(conn):
    from runsync import snapshot
    conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES ('PL1', 'Running', 'u', 1)")
    for v, st in (("gone", "unavailable"), ("lose", "merged"), ("keep", "downloaded"), ("elsewhere", "unavailable")):
        conn.execute("INSERT INTO tracks (video_id, title, status) VALUES (?, ?, ?)", (v, v, st))
    conn.execute("INSERT INTO playlist_items VALUES ('PL1', 'gone', 0), ('PL1', 'lose', 1), ('PL1', 'keep', 2), ('PL1', 'elsewhere', 3)")
    conn.execute("INSERT INTO aliases (video_id, winner_id, merged_at) VALUES ('lose', 'keep', 'x')")
    conn.execute("INSERT INTO replacement_picks (lost_id, pick_id, title, status, decided_at) VALUES ('gone', 'UP1', 't', 'picked', 'x')")
    conn.commit()
    ops = [Op("delete", "PL1", "gone", "i1", 0, "replace", "Gone", "UP1", "Up"), Op("delete", "PL1", "lose", "i2", 1, "loser", "Lose", "keep")]
    applied = _applied(ops, {"PL1": 4})
    applied.live = {"PL1": [yt.Entry("keep", "i3", 0, "Keep", "c", True), yt.Entry("UP1", "i4", 1, "Up", "c", True),
                            yt.Entry("elsewhere", "i5", 2, "E", "c", True)]}
    assert snapshot.settle_push(conn, applied, ok=False) == {"purged": 0}               # not verified: nothing is forgotten
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 4
    assert snapshot.settle_push(conn, applied, ok=True) == {"purged": 2}
    left = {r[0] for r in conn.execute("SELECT video_id FROM tracks")}
    assert left == {"keep", "elsewhere"}                         # elsewhere is still listed by YouTube: it stays
    assert [r[0] for r in conn.execute("SELECT video_id FROM playlist_items ORDER BY position")] == ["keep", "UP1", "elsewhere"]
    for table in ("aliases", "replacement_picks"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_the_cleanup_command_through_to_the_purge(cli_env, monkeypatch):
    from runsync import db
    from runsync.config import load_config
    cli, _calls = cli_env
    cfg = load_config()
    cfg.ensure_dirs()
    c = db.connect(cfg.db_path)
    c.execute("INSERT INTO playlists (id, title, url, selected) VALUES ('PL1', 'Running', 'u', 1)")
    c.execute("INSERT INTO tracks (video_id, title, status) VALUES ('a', 'Song A', 'unavailable')")
    c.execute("INSERT INTO playlist_items VALUES ('PL1', 'a', 0), ('PL1', 'b', 1)")
    c.commit()
    playlist = dead_entries(("a", True), ("b", True))                      # YouTube, as a list that deletes change
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: list(playlist))
    monkeypatch.setattr(yt, "delete_entry", lambda api, item: playlist.__delitem__(
        next(i for i, e in enumerate(playlist) if e.item_id == item)))
    prev = json.loads(_run(cli, "--cleanup", "--json").stdout)["cleanup"]
    assert prev["deletes"] == 1 and prev["items"][0]["why"] == "dead" and prev["items"][0]["title"] == "Song A"
    done = json.loads(_run(cli, "--apply", "--confirm", prev["plan_id"], "--json").stdout)["applied"]
    assert done["deleted"] == 1 and done["verify"]["ok"] and done["forgotten"] == 1
    assert c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0              # the dead song is gone from the database too
    assert [r[0] for r in c.execute("SELECT video_id FROM playlist_items")] == ["b"]   # and the snapshot follows YouTube


def test_an_unexpected_crash_mid_run_is_recorded_and_can_still_be_undone(conn):
    class Crashing(FakeApi):
        def request(self, method, resource, params=None, body=None):
            if len(self.calls) == 1:
                raise RuntimeError("disk full")
            return super().request(method, resource, params, body)
    plan = swap_plan()
    with pytest.raises(RuntimeError, match="disk full"):
        ytclean.apply_plan(Crashing(), conn, plan, ytclean.plan_id(plan))
    assert conn.execute("SELECT status FROM youtube_runs").fetchone()[0] == "interrupted"     # never left as "running"
    undo = FakeApi()
    assert ytclean.undo_last_run(undo, conn).removed == 1                                       # the one add that did happen


# ---- songs the owner wants gone: entries removed, then unliked -------------------------------------------------------------

def test_an_unwanted_song_is_deleted_from_every_playlist_entry():
    ops = ytclean.plan_playlist("PL1", dead_entries(("a", True), ("bad", True), ("c", True), ("bad", True)), lambda v: v,
                                lambda v: f"Title {v}", unwanted={"bad"})
    assert [(o.kind, o.video_id, o.why) for o in ops] == [("delete", "bad", "unwanted"), ("delete", "bad", "repeat")]


def test_nothing_can_change_a_like():
    assert not hasattr(yt, "rate") and not hasattr(yt, "ratings")       # there is no function that could touch a like


def test_the_remove_command_saves_the_wish_locally_and_only_for_real_ids(cli_env):
    from runsync import db
    from runsync.config import load_config
    cli, calls = cli_env
    out = json.loads(_run(cli, "--remove", "abcdefghijk,ZZZZZZZZZZZ", "--json").stdout)
    assert out["wanted_gone"] == ["abcdefghijk", "ZZZZZZZZZZZ"] and calls == []
    assert db.connect(load_config().db_path).execute("SELECT COUNT(*) FROM excluded").fetchone()[0] == 2
    bad = _run(cli, "--remove", "nope", "--json")
    assert bad.exit_code == 2 and "not a YouTube video id" in json.loads(bad.stdout)["error"]


# ---- replacements whose dead song lives only in Liked Music ---------------------------------------------------------------

def _picks_world(conn, monkeypatch, in_refresh=()):
    for pid, title in (("PL1", "Running"), ("PL2", "Refresh")):
        conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES (?, ?, 'u', 1)", (pid, title))
    for v in ("lost1", "lost2"):
        conn.execute("INSERT INTO tracks (video_id, title, status) VALUES (?, ?, 'unavailable')", (v, f"Lost {v}"))
    conn.execute("INSERT INTO replacement_picks (lost_id, pick_id, title, status, decided_at) VALUES "
                 "('lost1', 'UPLOAD00001', 'Found one', 'picked', 'x'), ('lost2', 'UPLOAD00002', 'Found two', 'picked', 'x')")
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PL1", "title": "Running", "count": 1},
                                                          {"id": "PL2", "title": "Refresh", "count": 1}])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: dead_entries(*[(v, True) for v in (("ok",) if pid == "PL1" else ("ok2", *in_refresh))]))


def test_replacements_for_songs_that_live_only_in_liked_music_go_where_the_owner_chose(conn, monkeypatch):
    _picks_world(conn, monkeypatch)
    without = ytclean.build_plan(object(), conn)
    assert without.ops == []                                                  # nowhere to put them yet
    plan = ytclean.build_plan(object(), conn, place_in="PL2")
    assert [(o.kind, o.playlist_id, o.video_id, o.why, o.title) for o in plan.ops] == [
        ("add", "PL2", "UPLOAD00001", "replace", "Found one"), ("add", "PL2", "UPLOAD00002", "replace", "Found two")]


def test_a_replacement_already_in_the_playlist_is_not_added_again(conn, monkeypatch):
    _picks_world(conn, monkeypatch, in_refresh=("UPLOAD00001",))
    plan = ytclean.build_plan(object(), conn, place_in="PL2")
    assert [(o.video_id) for o in plan.ops] == ["UPLOAD00002"]


def test_a_place_that_is_not_one_of_your_synced_playlists_is_refused(conn, monkeypatch):
    _picks_world(conn, monkeypatch)
    with pytest.raises(ytclean.NoSuchPlaylist):
        ytclean.build_plan(object(), conn, place_in="PLnope")
