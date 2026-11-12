"""YouTube playlist cleanup: the plan (pure), the gated apply, the journal and undo. A fake API, no network."""
import json

import pytest

from runsync import youtube as yt
from runsync import ytclean
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
    assert ytclean.resolve_winner(conn, "a") == "c" and ytclean.resolve_winner(conn, "z") == "z"
    assert ytclean.resolve_winner(conn, "x") in {"x", "y"}              # a loop ends instead of hanging


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
    assert ok.exit_code == 0 and json.loads(ok.stdout)["applied"]["deleted"] == 1 and calls == ["DELETE"]


def test_cleanup_when_not_signed_in_says_so(tmp_path, monkeypatch):
    from runsync import cli
    from runsync.config import Config
    monkeypatch.setenv("RUNSYNC_HOME", str(tmp_path / "lib"))
    monkeypatch.setattr(Config, "youtube_token", property(lambda self: tmp_path / "none.json"))
    out = _run(cli, "--cleanup", "--json")
    assert out.exit_code == 2 and "sign in" in json.loads(out.stdout)["error"].lower()
