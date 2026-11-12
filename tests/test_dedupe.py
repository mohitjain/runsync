"""Dedupe: fingerprint matching, never across versions, permanent removal with an alias, undo."""
import json
import random

import pytest
from conftest import add_track, make_m4a, needs_ffmpeg

from runsync import db
from runsync.config import Config
from runsync.dedupe import (
    apply_plan,
    build_plan,
    plan_to_dict,
    record_decision,
    undo,
    winner_of,
)
from runsync.diff import MassDeleteError

RNG = random.Random(7)
BASE = [RNG.getrandbits(32) for _ in range(800)]


def noisy(fp, rate):
    out = []
    for v in fp:
        mask = sum(1 << b for b in range(32) if RNG.random() < rate)
        out.append(v ^ mask)
    return out


def meta(conn, vid, artist="Avicii", title="Wake Me Up", versions=(), review=0):
    conn.execute("INSERT INTO metadata (video_id, artist, title, versions_json, needs_review, resolved_at) "
                 "VALUES (?,?,?,?,?,?)", (vid, artist, title, json.dumps(list(versions)), review, "x"))
    conn.commit()


def track(conn, tmp_path, vid, fp, channel="Chan", codec="aac", kbps=256.0, **kw):
    f = tmp_path / f"{vid}.m4a"
    f.write_bytes(b"x")
    add_track(conn, vid, f, channel=channel, probe_codec=codec, probe_bitrate_kbps=kbps, probe_duration_s=200.0,
              sha256=vid, **kw)
    FPS[vid] = fp


FPS: dict = {}


def fp_fn(vid, path, sha):
    return FPS.get(vid)


@pytest.fixture(autouse=True)
def _clear():
    FPS.clear()


def plan(conn):
    return build_plan(conn, fp_fn)


def test_same_recording_with_an_intro_is_merged_and_the_better_source_wins(conn, tmp_path):
    track(conn, tmp_path, "audio", BASE, channel="Avicii - Topic")
    track(conn, tmp_path, "video", [RNG.getrandbits(32) for _ in range(120)] + noisy(BASE, 0.05), channel="Label")
    for v in ("audio", "video"):
        meta(conn, v)
    p = plan(conn)
    assert len(p.groups) == 1 and p.unsure == []
    assert p.groups[0].winner.video_id == "audio" and [m.video_id for m, _ in p.groups[0].losers] == ["video"]


def test_anything_below_a_clear_match_is_left_to_the_owner_and_other_songs_stay_apart(conn, tmp_path):
    track(conn, tmp_path, "a", BASE)
    track(conn, tmp_path, "b", noisy(BASE, 0.22))                      # a different edit
    track(conn, tmp_path, "c", [RNG.getrandbits(32) for _ in range(800)])   # not the same recording at all
    for v in "abc":
        meta(conn, v)
    p = plan(conn)
    assert p.groups == []
    assert {frozenset((u.a.video_id, u.b.video_id)) for u in p.unsure} == {frozenset("ab"), frozenset("ac"), frozenset("bc")}


def test_a_remix_or_cover_is_never_a_candidate(conn, tmp_path):
    track(conn, tmp_path, "studio", BASE)
    track(conn, tmp_path, "remix", BASE)
    meta(conn, "studio")
    meta(conn, "remix", versions=["remix"])
    p = plan(conn)
    assert p.groups == [] and p.unsure == []


def test_live_and_remastered_uploads_merge_into_the_studio_version_without_a_fingerprint_match(conn, tmp_path):
    track(conn, tmp_path, "studio", BASE, channel="Chan")
    track(conn, tmp_path, "live", [RNG.getrandbits(32) for _ in range(800)])      # audio looks nothing alike
    track(conn, tmp_path, "remaster", BASE)
    meta(conn, "studio")
    meta(conn, "live", versions=["live"])
    meta(conn, "remaster", versions=["remaster"])
    p = plan(conn)
    assert len(p.groups) == 1 and p.unsure == []
    assert p.groups[0].winner.video_id in {"studio", "remaster"}                  # never the live upload
    assert "live" in {m.video_id for m, _ in p.groups[0].losers}


def test_the_same_song_matches_even_when_the_artist_order_differs(conn, tmp_path):
    track(conn, tmp_path, "a", BASE)
    track(conn, tmp_path, "b", noisy(BASE, 0.03))
    meta(conn, "a", artist="Diljit Dosanjh, Badshah", title="Proper Patola")
    meta(conn, "b", artist="Badshah, Diljit Dosanjh, Aastha Gill", title="Proper Patola")
    assert len(plan(conn).groups) == 1
    track(conn, tmp_path, "c", BASE)
    meta(conn, "c", artist="Someone Else", title="Proper Patola")                  # no shared artist: another song
    assert {m.video_id for g in plan(conn).groups for m in [g.winner, *[x for x, _ in g.losers]]} == {"a", "b"}


def test_tracks_that_need_review_are_ignored(conn, tmp_path):
    track(conn, tmp_path, "a", BASE)
    track(conn, tmp_path, "b", BASE)
    meta(conn, "a")
    meta(conn, "b", review=1)
    assert plan(conn).groups == []


def test_your_answer_overrides_the_fingerprint_and_is_remembered(conn, tmp_path):
    track(conn, tmp_path, "a", BASE)
    track(conn, tmp_path, "b", noisy(BASE, 0.22))
    meta(conn, "a")
    meta(conn, "b")
    record_decision(conn, "b", "a", same=True)
    assert len(plan(conn).groups) == 1
    record_decision(conn, "a", "b", same=False)
    p = plan(conn)
    assert p.groups == [] and p.unsure == []
    with pytest.raises(ValueError):
        record_decision(conn, "a", "nope", same=True)


def test_ranking_playable_codec_first_then_source_then_bitrate(conn, tmp_path):
    track(conn, tmp_path, "topic", BASE, channel="Someone - Topic", codec="opus", kbps=275.0)
    track(conn, tmp_path, "label", BASE, channel="T-Series", codec="aac")
    track(conn, tmp_path, "lyric", BASE, original_title="Wake Me Up (Lyric Video)")
    for v in ("topic", "label", "lyric"):
        meta(conn, v)
    assert plan(conn).groups[0].winner.video_id == "label"          # the player cannot use Opus yet: AAC keeps the song
    conn.execute("UPDATE tracks SET channel = 'Someone - Topic' WHERE video_id = 'label'")
    assert plan(conn).groups[0].winner.video_id == "label"          # same codec as lyric: source quality decides
    conn.execute("UPDATE tracks SET probe_codec = 'opus' WHERE video_id IN ('label', 'lyric')")
    assert plan(conn).groups[0].winner.video_id in {"topic", "label"}   # all Opus: source quality, then bitrate


def test_building_a_plan_changes_nothing(conn, tmp_path):
    track(conn, tmp_path, "a", BASE)
    track(conn, tmp_path, "b", BASE)
    meta(conn, "a")
    meta(conn, "b")
    before = [tuple(r) for r in conn.execute("SELECT video_id, status, master_path FROM tracks ORDER BY 1")]
    plan(conn)
    assert before == [tuple(r) for r in conn.execute("SELECT video_id, status, master_path FROM tracks ORDER BY 1")]
    assert conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 0


@pytest.fixture
def cfg(tmp_path):
    c = Config(library=tmp_path / "lib")
    c.ensure_dirs()
    return c


def two(conn, cfg):
    for vid, ch in (("keep", "Avicii - Topic"), ("lose", "Label")):
        f = cfg.master / f"{vid}.m4a"
        f.write_bytes(b"x")
        add_track(conn, vid, f, channel=ch, probe_codec="aac", probe_bitrate_kbps=256.0, probe_duration_s=200.0,
                  sha256=vid)
        FPS[vid] = BASE
        meta(conn, vid)
    for i in range(8):                                             # enough other tracks to stay under the guard
        add_track(conn, f"other{i}", None)


def test_apply_deletes_the_loser_for_good_keeps_an_alias_and_spares_the_winner(cfg):
    conn = db.connect(cfg.db_path)
    two(conn, cfg)
    p = plan(conn)
    assert apply_plan(cfg, conn, p) == 1
    assert (cfg.master / "keep.m4a").exists() and not (cfg.master / "lose.m4a").exists()
    row = conn.execute("SELECT status, master_path FROM tracks WHERE video_id = 'lose'").fetchone()
    assert (row["status"], row["master_path"]) == ("merged", None)
    assert winner_of(conn, "lose") == "keep" and winner_of(conn, "keep") == "keep"
    assert plan(conn).groups == []                                 # nothing left to merge


def test_a_loser_is_not_deleted_when_its_winner_file_is_gone(cfg):
    conn = db.connect(cfg.db_path)
    two(conn, cfg)
    p = plan(conn)
    conn.execute("UPDATE tracks SET status = 'trashed' WHERE video_id = 'keep'")
    assert apply_plan(cfg, conn, p) == 0 and (cfg.master / "lose.m4a").exists()


def test_the_mass_delete_guard_stops_a_large_removal(cfg):
    conn = db.connect(cfg.db_path)
    two(conn, cfg)
    conn.execute("DELETE FROM tracks WHERE video_id LIKE 'other%'")
    with pytest.raises(MassDeleteError):
        apply_plan(cfg, conn, plan(conn))
    assert (cfg.master / "lose.m4a").exists()
    assert apply_plan(cfg, conn, plan(conn), force=True) == 1


@needs_ffmpeg
def test_only_a_player_copy_made_for_that_track_is_deleted(cfg):
    from runsync.player import build_player_copy
    conn = db.connect(cfg.db_path)
    two(conn, cfg)
    master = make_m4a(cfg.master / "lose.m4a")
    out = build_player_copy(master, cfg.player / "m4a", "lose", {"artist": "Avicii", "title": "Wake Me Up"}, None)
    conn.execute("UPDATE tracks SET player_path = ? WHERE video_id = 'lose'", (str(out["path"]),))
    apply_plan(cfg, conn, plan(conn))
    assert not out["path"].exists()


def test_undo_queues_the_download_again_and_never_merges_that_pair_again(cfg):
    conn = db.connect(cfg.db_path)
    two(conn, cfg)
    apply_plan(cfg, conn, plan(conn))
    assert undo(conn, "lose") == "keep"
    assert conn.execute("SELECT status FROM tracks WHERE video_id = 'lose'").fetchone()[0] == "pending"
    assert winner_of(conn, "lose") == "lose"
    conn.execute("UPDATE tracks SET status = 'downloaded', master_path = ? WHERE video_id = 'lose'",
                 (str(cfg.master / "lose.m4a"),))
    (cfg.master / "lose.m4a").write_bytes(b"x")
    assert plan(conn).groups == []
    with pytest.raises(ValueError):
        undo(conn, "keep")


def test_plan_json_has_what_the_skill_needs(conn, tmp_path):
    track(conn, tmp_path, "a", BASE, channel="Avicii - Topic", original_title="Wake Me Up")
    track(conn, tmp_path, "b", BASE, original_title="Avicii - Wake Me Up (Official Video)")
    meta(conn, "a")
    meta(conn, "b")
    d = plan_to_dict(plan(conn))
    assert (d["remove"][0]["keep"]["youtube_title"], d["remove"][0]["remove"]["youtube_title"]) == (
        "Wake Me Up", "Avicii - Wake Me Up (Official Video)")   # two same-named tracks stay tellable apart
    assert d["remove"][0]["keep"]["id"] == "a" and d["remove"][0]["remove"]["id"] == "b" and d["unsure"] == []


@needs_ffmpeg
def test_a_player_file_that_belongs_to_another_track_is_never_deleted(cfg):
    from runsync.player import build_player_copy
    conn = db.connect(cfg.db_path)
    two(conn, cfg)
    master = make_m4a(cfg.master / "keep.m4a")
    winners_copy = build_player_copy(master, cfg.player / "m4a", "keep", {"artist": "Avicii", "title": "Wake Me Up"}, None)
    conn.execute("UPDATE tracks SET player_path = ? WHERE video_id = 'lose'", (str(winners_copy["path"]),))
    apply_plan(cfg, conn, plan(conn))
    assert winners_copy["path"].exists()


def test_apply_needs_the_id_of_the_exact_plan_and_nothing_is_deleted_otherwise(cfg, monkeypatch):
    from typer.testing import CliRunner

    from runsync import cli
    from runsync.dedupe import plan_id
    conn = db.connect(cfg.db_path)
    two(conn, cfg)
    monkeypatch.setenv("RUNSYNC_HOME", str(cfg.library))
    monkeypatch.setattr(cli, "build_plan", lambda c: plan(c))
    run = CliRunner().invoke
    pid = plan_id(plan(conn))
    for args in (["--apply"], ["--apply", "--confirm", "wrongplan123"], ["--apply", "--confirm", ""]):
        out = run(cli.app, ["dedupe", *args])
        assert out.exit_code == 2 and "Not confirmed" in out.output
        assert (cfg.master / "lose.m4a").exists()
    assert run(cli.app, ["dedupe", "--apply", "--confirm", pid]).exit_code == 0
    assert not (cfg.master / "lose.m4a").exists() and (cfg.master / "keep.m4a").exists()


def test_plan_id_changes_when_the_plan_changes(conn, tmp_path):
    from runsync.dedupe import plan_id
    track(conn, tmp_path, "a", BASE, channel="Avicii - Topic")
    track(conn, tmp_path, "b", BASE)
    meta(conn, "a")
    meta(conn, "b")
    first = plan_id(plan(conn))
    assert first == plan_id(plan(conn)) and len(first) == 12
    track(conn, tmp_path, "c", BASE)
    meta(conn, "c")
    assert plan_id(plan(conn)) != first
