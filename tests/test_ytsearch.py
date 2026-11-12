"""Replacement search and adding songs to playlists: fake search, fake API, no network."""
import json

import pytest
from test_ytclean import FakeApi, entries

from runsync import youtube as yt
from runsync import ytclean, ytsearch


def fake_search(results):
    return lambda q, n: results


def r(vid, title, channel="Chan", dur=200):
    return {"id": vid.ljust(11, "x"), "title": title, "channel": channel, "duration": dur}


def test_the_artists_own_channel_and_official_uploads_rank_first_and_live_or_cover_last():
    found = ytsearch.candidates("Sweet Emotion Aerosmith", search=fake_search([
        r("a", "Sweet Emotion (cover)", "Random Guy"), r("b", "Sweet Emotion live 1999", "Fan"),
        r("c", "Sweet Emotion", "Aerosmith"), r("d", "Sweet Emotion", "Someone Else")]))
    assert [c.video_id[0] for c in found] == ["c", "d", "a", "b"] and found[0].official and not found[1].official


def test_a_live_query_does_not_penalise_live():
    found = ytsearch.candidates("Alone live", search=fake_search([r("a", "Alone (Live)"), r("b", "Alone")]))
    assert found[0].video_id.startswith("a")


def test_search_skips_entries_that_are_not_videos():
    found = ytsearch.candidates("x", search=fake_search([{"id": "UCchannelid", "title": "A channel", "ie_key": "YoutubeTab"}, {"title": "no id"},
                                                         r("v", "Real")]))
    assert [c.video_id[0] for c in found] == ["v"]


def add_track(conn, vid, title, status):
    conn.execute("INSERT INTO tracks (video_id, title, status, last_error) VALUES (?, ?, ?, ?)", (vid, title, status,
                 "ERROR: Video unavailable. It was blocked due to the claimed content by UMG." if status == "unavailable" else None))


def test_lost_songs_skip_what_the_library_has_and_count_the_nameless(conn):
    add_track(conn, "u1", "Hotel California (2013 Remaster)", "unavailable")
    add_track(conn, "d1", "Hotel California (2013 Remaster)", "downloaded")
    add_track(conn, "u2", "Smoke on the water", "unavailable")
    add_track(conn, "d2", "Deep Purple - Smoke On The Water  (HD)", "downloaded")
    add_track(conn, "u3", "Paisa", "unavailable")
    add_track(conn, "u4", "Paisa", "unavailable")             # the same song twice: one entry
    add_track(conn, "u5", "", "unavailable")                  # no title recorded: cannot be searched
    add_track(conn, "d3", "Paisa Paisa Paisa", "downloaded")  # a single-word title never matches a longer one
    lost, nameless = ytsearch.lost_songs(conn)
    by = {s.title: s for s in lost}
    assert nameless == 1 and len(lost) == 3
    assert by["Hotel California (2013 Remaster)"].in_library["video_id"] == "d1"
    assert by["Smoke on the water"].in_library["video_id"] == "d2"      # shown to the owner, never silent
    assert by["Paisa"].in_library is None and by["Paisa"].reason == "blocked by the rights holder"


def test_normalize_drops_noise_and_keeps_words():
    assert ytsearch.normalize("Kiss - I was made for lovin' you -official video clip (HD)") == "kiss i was made for lovin you"
    assert ytsearch.normalize("Above & Beyond pres. Oceanlab - I Am What I Am") == "above and beyond pres oceanlab i am what i am"


def test_the_search_query_drops_upload_noise_but_keeps_live_remix_and_remaster():
    assert ytsearch.query_for("Kiss - I was made for lovin' you -official video clip (HD)") == "kiss i was made for lovin you"
    assert ytsearch.query_for("Alone (Live)") == "alone live"
    assert ytsearch.query_for("Alive (2004 Remix)") == "alive remix"


# ---- the add plan -----------------------------------------------------------------------------------------------------

def mine(monkeypatch, *titles):
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": f"PL{i}", "title": t, "count": 1} for i, t in enumerate(titles)])


def test_add_plan_skips_songs_already_in_the_playlist_and_repeated_ids(conn, monkeypatch):
    mine(monkeypatch, "rerunsync", "Running")
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: entries("old"))
    plan, skipped = ytclean.build_add_plan(object(), conn, ["new1", "old", "new1", "new2"], "rerunsync", titles={"new1": "One"})
    assert [(o.kind, o.video_id, o.title) for o in plan.ops] == [("add", "new1", "One"), ("add", "new2", "")]
    assert skipped == [{"video_id": "old", "why": "already in the playlist"}] and plan.playlists == {"PL0": "rerunsync"}


def test_add_plan_for_a_missing_playlist_needs_create_and_then_makes_it_first(conn, monkeypatch):
    mine(monkeypatch, "Running")
    with pytest.raises(ytclean.NoSuchPlaylist, match="No playlist called"):
        ytclean.build_add_plan(object(), conn, ["v1"], "rerunsync")
    plan, _ = ytclean.build_add_plan(object(), conn, ["v1", "v2"], "rerunsync", create=True)
    assert [o.kind for o in plan.ops] == ["create", "add", "add"] and plan.ops[1].playlist_id == "new:rerunsync"


def test_add_plan_refuses_the_special_playlists_and_ambiguous_names(conn, monkeypatch):
    mine(monkeypatch, "Gym Mix", "Gym Run")
    with pytest.raises(ytclean.NoSuchPlaylist, match="matches 2"):
        ytclean.build_add_plan(object(), conn, ["v"], "Gym")
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "LL", "title": "Liked videos", "count": 1}])
    with pytest.raises(ytclean.NoSuchPlaylist, match="cannot be changed"):
        ytclean.build_add_plan(object(), conn, ["v"], "Liked videos")


def test_applying_a_create_and_add_plan_makes_the_playlist_selects_it_and_adds(conn, monkeypatch):
    mine(monkeypatch, "Running")
    plan, _ = ytclean.build_add_plan(FakeApi(), conn, ["v1", "v2"], "rerunsync", create=True)
    api = FakeApi()
    with pytest.raises(ytclean.PlanRefused):
        ytclean.apply_plan(api, conn, plan, "nope")
    assert api.calls == []
    r = ytclean.apply_plan(api, conn, plan, ytclean.plan_id(plan))
    assert (r.created, r.added, r.left) == (1, 2, 0)
    assert api.calls[0][1] == "playlists" and api.calls[0][3]["status"]["privacyStatus"] == "private"
    assert [c[3]["snippet"]["playlistId"] for c in api.calls[1:]] == ["new1", "new1"]          # the id Google gave back
    row = conn.execute("SELECT selected, kind FROM playlists WHERE id = 'new1'").fetchone()
    assert (row["selected"], row["kind"]) == (1, "account")                                      # the next refresh downloads it


def test_undo_of_an_add_removes_the_songs_and_leaves_the_empty_playlist(conn, monkeypatch):
    mine(monkeypatch, "Running")
    plan, _ = ytclean.build_add_plan(FakeApi(), conn, ["v1"], "rerunsync", create=True)
    ytclean.apply_plan(FakeApi(), conn, plan, ytclean.plan_id(plan))
    api = FakeApi()
    out = ytclean.undo_last_run(api, conn)
    assert (out.removed, out.restored, out.left) == (1, 0, 0) and [c[0] for c in api.calls] == ["DELETE"]


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
    monkeypatch.setattr(yt, "_http", lambda method, url, headers, body: calls.append((method, url)) or (200, {"id": "NEWID"}))
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PLx", "title": "Running", "count": 1}])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: entries("old"))
    monkeypatch.setattr(ytsearch, "ytdlp_search", lambda q, n=6: [r("a", "Found Song", "Artist")])
    monkeypatch.setattr(ytsearch, "video_titles", lambda ids, lookup=None: {v: f"Title of {v}" for v in ids})
    return cli, calls


def _run(cli, *args):
    from typer.testing import CliRunner
    return CliRunner().invoke(cli.app, ["youtube", *args])


def test_search_command_lists_candidates_without_any_api_call(cli_env, monkeypatch):
    cli, calls = cli_env
    monkeypatch.setattr(ytsearch, "candidates", lambda q, n=5, search=None: [ytsearch.Candidate("abcdefghijk", "Found Song", "Artist", 200, True)])
    out = json.loads(_run(cli, "--search", "found song", "--json").stdout)
    assert out["search"]["candidates"][0]["video_id"] == "abcdefghijk" and calls == []


def test_add_through_the_command_previews_then_needs_the_exact_plan_id(cli_env):
    cli, calls = cli_env
    prev = json.loads(_run(cli, "--add", "n1,old", "--to", "Running", "--json").stdout)["add"]
    assert [v["video_id"] for v in prev["videos"]] == ["n1"] and prev["skipped"][0]["video_id"] == "old" and calls == []
    refused = _run(cli, "--add", "n1", "--to", "Running", "--apply", "--confirm", "wrong", "--json")
    assert refused.exit_code == 2 and calls == []
    ok = _run(cli, "--add", "n1,old", "--to", "Running", "--apply", "--confirm", prev["plan_id"], "--json")
    assert ok.exit_code == 0 and json.loads(ok.stdout)["applied"]["added"] == 1 and [c[0] for c in calls] == ["POST"]


def test_add_without_a_playlist_or_with_an_unknown_one_is_refused(cli_env):
    cli, calls = cli_env
    assert _run(cli, "--add", "n1", "--json").exit_code == 2
    out = _run(cli, "--add", "n1", "--to", "Nope", "--json")
    assert out.exit_code == 2 and "No playlist called" in json.loads(out.stdout)["error"] and calls == []
