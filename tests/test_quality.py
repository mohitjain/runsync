"""Quality upgrades: which songs are searched, which uploads are considered, the gentle search and its canary."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from runsync import quality
from runsync.config import Config
from runsync.dedupe import Member
from runsync.ytsearch import Candidate

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def add_song(conn, vid, kbps=128, artist="Artist", title="Natural", secs=200, channel="Chan", searched=None, review=0,
             status="downloaded", original="Artist - Natural (Official Video)", result="auto"):
    conn.execute("INSERT INTO tracks (video_id, status, master_path, probe_bitrate_kbps, probe_duration_s, channel, original_title, "
                 "quality_searched_at, quality_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (vid, status, "/m/x.m4a", kbps, secs, channel, original, searched,
                  ("no_match" if searched else None) if result == "auto" else result))
    conn.execute("INSERT INTO metadata (video_id, artist, title, needs_review) VALUES (?, ?, ?, ?)", (vid, artist, title, review))
    conn.commit()


def cand(vid, title="Artist - Natural", channel="Some Channel", secs=200, official=False):
    return Candidate(vid, title, channel, secs, official)


def days_ago(n):
    return (NOW - timedelta(days=n)).isoformat(timespec="seconds")


# ---- which songs -----------------------------------------------------------------------------------------------------------

def test_a_top_quality_song_is_never_due_however_old(conn):
    add_song(conn, "top", kbps=258)
    add_song(conn, "ripe", kbps=128)
    assert [s.video_id for s in quality.due_songs(conn, 30, NOW)] == ["ripe"]


def test_a_searched_song_is_due_again_only_after_the_recheck_period(conn):
    add_song(conn, "fresh", searched=days_ago(10))
    add_song(conn, "edge", searched=days_ago(30))
    add_song(conn, "old", searched=days_ago(45))
    assert sorted(s.video_id for s in quality.due_songs(conn, 30, NOW)) == ["edge", "old"]


def test_held_and_missing_songs_are_skipped(conn):
    add_song(conn, "held", review=1)
    add_song(conn, "gone", status="trashed")
    conn.execute("INSERT INTO tracks (video_id, status, master_path, probe_bitrate_kbps) VALUES ('nometa', 'downloaded', '/m', 128)")
    assert quality.due_songs(conn, 30, NOW) == []


def test_the_facts_say_how_many_are_top_due_waiting_and_when_the_next_search_is(conn):
    add_song(conn, "t1", kbps=258)
    add_song(conn, "t2", kbps=257)
    add_song(conn, "due", searched=None)
    add_song(conn, "wait", searched=days_ago(5))
    add_song(conn, "held", review=1)
    f = quality.facts(conn, 30, NOW)
    assert (f["songs"], f["top"], f["below_top"], f["due"], f["waiting"], f["held"]) == (5, 2, 2, 1, 1, 1)
    conn.execute("DELETE FROM tracks WHERE video_id = 'due'")
    assert quality.facts(conn, 30, NOW)["next_due"] == (NOW - timedelta(days=5) + timedelta(days=30)).date().isoformat()


# ---- which uploads ---------------------------------------------------------------------------------------------------------

def song():
    return quality.Song("old", "Artist", "Natural", "Chan", 128, 200, "Artist - Natural (Official Video)")


@pytest.mark.parametrize("c, ok", [
    (cand("a", secs=200), True), (cand("a", secs=205), True), (cand("a", secs=206), False), (cand("a", secs=194), False),
    (cand("a", secs=0), False), (cand("old"), False),                                  # no length / the song's own id
    (cand("a", title="Something Else"), False), (cand("a", title="Artist - Natural (Lyrics)"), True),
    (cand("a", title="Artist - Natural (Live)"), False), (cand("a", title="Artist - Natural (Remix)"), False),
    (cand("a", title="Artist - Natural cover by someone"), False), (cand("a", title="Artist - Natural (Audio)"), True),
    (cand("a", title="Artist - Natural (Romantic Version)"), False), (cand("a", title="Artist - Natural (Acoustic)"), False),
    (cand("a", title="Artist - Natural (Remastered)"), True), (cand("a", title="Artist - Natural (Official Audio)"), True),
])
def test_only_uploads_that_could_be_the_same_recording_pass(c, ok):
    assert quality.acceptable(song(), c, excluded=set(), known=set()) is ok


def test_a_live_marker_is_fine_when_the_song_itself_is_live():
    live = quality.Song("old", "Artist", "Natural (Live)", "Chan", 128, 200, "Artist - Natural (Live at Wembley)")
    assert quality.acceptable(live, cand("a", title="Artist - Natural (Live at Wembley)"), set(), set())


def test_excluded_and_already_known_uploads_are_never_proposed():
    assert not quality.acceptable(song(), cand("bad"), excluded={"bad"}, known=set())
    assert not quality.acceptable(song(), cand("have"), excluded=set(), known={"have"})


def test_the_shortlist_prefers_official_looking_channels_and_stops_at_three():
    cs = [cand("c1", secs=204), cand("c2", official=True, secs=203), cand("c3", secs=200), cand("c4", secs=201),
          cand("c5", official=True, secs=205)]
    assert [c.video_id for c in quality.shortlist(song(), cs, set(), set())] == ["c2", "c5", "c3"]


# ---- the gentle search ------------------------------------------------------------------------------------------------------

class Probe:
    """Format lookups, scripted per video id; records every lookup."""
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, video_id, cookies):
        self.calls.append(video_id)
        value = self.table.get(video_id, 130)
        return value.pop(0) if isinstance(value, list) else value


@pytest.fixture
def env(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "check_cookies", lambda p: SimpleNamespace(usable=True))
    cfg = Config(library=tmp_path)
    cfg.cookies = tmp_path / "cookies.txt"
    add_song(conn, "canary", kbps=258, title="Canary")
    sleeps = []
    return cfg, sleeps


def run(conn, cfg, sleeps, searches, probe, **kw):
    return quality.search(conn, cfg, search_fn=lambda q, n: searches.get(q.split()[-1], []), probe_fn=probe,
                          sleep=sleeps.append, now=NOW, **kw)


def test_a_better_upload_is_queued_and_the_song_is_stamped_whether_or_not_one_is_found(conn, env):
    cfg, sleeps = env
    add_song(conn, "s1", title="One")
    add_song(conn, "s2", title="Two")
    out = run(conn, cfg, sleeps, {"One": [cand("n1", title="Artist - One")], "Two": [cand("n2", title="Artist - Two")]},
              Probe({"canary": 258, "n1": 257.5, "n2": 129.5}))
    assert (out.searched, len(out.found), out.not_found, out.aborted) == (2, 1, 1, "")
    row = conn.execute("SELECT * FROM upgrades").fetchone()
    assert (row["new_id"], row["old_id"], row["new_kbps"], row["old_kbps"]) == ("n1", "s1", 257.5, 128)
    stamped = {r[0] for r in conn.execute("SELECT video_id FROM tracks WHERE quality_searched_at IS NOT NULL")}
    assert stamped == {"s1", "s2"}                                    # "not found" is remembered too: retried after a month
    assert quality.due_songs(conn, 30, NOW) == []


def test_the_search_is_capped_and_paced(conn, env):
    cfg, sleeps = env
    cfg.quality_songs_per_run = 2
    for i in range(5):
        add_song(conn, f"s{i}", title=f"T{i}")
    searches = {f"T{i}": [cand(f"c{i}{j}", title=f"Artist - T{i}", secs=200 + j) for j in range(5)] for i in range(5)}
    probe = Probe({"canary": 258})
    out = run(conn, cfg, sleeps, searches, probe)
    assert out.searched == 2                                          # only the per-run number of songs
    song_lookups = [c for c in probe.calls if c != "canary"]
    assert len(song_lookups) <= 2 * quality.CANDIDATES_PER_SONG       # at most 3 candidates per song
    assert all(s == quality.PAUSE_SECONDS for s in sleeps) and len(sleeps) >= len(probe.calls) - 1


def test_top_quality_songs_are_never_searched(conn, env):
    cfg, sleeps = env
    probe = Probe({"canary": 258})
    out = run(conn, cfg, sleeps, {}, probe)
    assert out.searched == 0 and probe.calls == []                    # only a canary exists, so no lookups at all


def test_a_candidate_must_reach_the_top_tier_and_beat_the_current_file(conn, env):
    cfg, sleeps = env
    add_song(conn, "s1", kbps=140, title="One")
    out = run(conn, cfg, sleeps, {"One": [cand("n1", title="Artist - One")]}, Probe({"canary": 258, "n1": 200}))
    assert out.found == [] and out.not_found == 1 and conn.execute("SELECT COUNT(*) FROM upgrades").fetchone()[0] == 0


def test_a_throttled_session_at_the_start_stops_before_anything_is_written(conn, env):
    cfg, sleeps = env
    add_song(conn, "s1", title="One")
    out = run(conn, cfg, sleeps, {"One": [cand("n1", title="Artist - One")]}, Probe({"canary": 130, "n1": 258}))
    assert "throttled" in out.aborted and out.searched == 0
    assert conn.execute("SELECT COUNT(*) FROM upgrades").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tracks WHERE quality_searched_at IS NOT NULL").fetchone()[0] == 0


def test_a_session_that_throttles_mid_run_keeps_real_finds_but_does_not_mark_empty_results_as_searched(conn, env):
    cfg, sleeps = env
    add_song(conn, "s1", title="One")
    add_song(conn, "s2", title="Two")
    out = run(conn, cfg, sleeps, {"One": [cand("n1", title="Artist - One")], "Two": [cand("n2", title="Artist - Two")]},
              Probe({"canary": [258, 130], "n1": 258, "n2": 130}))                 # the canary fails the second time
    assert "throttled" in out.aborted and [f.new_id for f in out.found] == ["n1"]   # a seen 258 is real
    stamped = {r[0] for r in conn.execute("SELECT video_id FROM tracks WHERE quality_searched_at IS NOT NULL")}
    assert stamped == {"s1"}                                           # "nothing better" for s2 may just be the throttle


def test_without_usable_cookies_nothing_happens(conn, env, monkeypatch):
    cfg, sleeps = env
    add_song(conn, "s1")
    monkeypatch.setattr(quality, "check_cookies", lambda p: SimpleNamespace(usable=False))
    out = run(conn, cfg, sleeps, {}, Probe({}))
    assert "cookies" in out.aborted and out.searched == 0


# ---- replacing the old one: dedupe prefers the top bitrate --------------------------------------------------------------------

def member(vid, kbps, tier, codec="aac", live=False):
    return Member(vid, "A", "T", "C", kbps, codec, 200, tier, markers=frozenset({"live"}) if live else frozenset())


def test_a_258k_lyric_video_beats_a_128k_official_video_in_dedupe():
    lyric, official = member("lyric", 258, tier=0), member("official", 128, tier=3)
    assert max([official, lyric], key=Member.rank) is lyric
    assert max([member("a", 258, 1), member("b", 258, 3)], key=Member.rank).video_id == "b"      # equal bitrate: channel decides
    assert max([member("opus", 275, 4, codec="opus"), member("aac", 128, 1)], key=Member.rank).video_id == "aac"   # AAC still first
    assert max([member("live", 258, 4, live=True), member("studio", 128, 1)], key=Member.rank).video_id == "studio"


# ---- from the outbox into the plan ----------------------------------------------------------------------------------------------

def _queued(conn, new_id="n1", old_id="old1"):
    conn.execute("INSERT INTO upgrades (new_id, old_id, title, new_channel, new_kbps, old_channel, old_kbps) "
                 "VALUES (?, ?, 'Natural (Lyrics)', 'ImagineDragons', 257.5, 'Sony Music India', 128)", (new_id, old_id))


def test_a_queued_upgrade_becomes_an_add_with_a_note_and_nothing_is_removed(conn):
    from runsync import ytclean
    _queued(conn)
    ops = ytclean._upgrade_ops(conn, "LIB", present={"other"})
    assert [(o.kind, o.playlist_id, o.video_id, o.why) for o in ops] == [("add", "LIB", "n1", "upgrade")]
    assert ops[0].note == "Sony Music India 128k → ImagineDragons 258k" and ops[0].title == "Natural (Lyrics)"
    assert all(o.kind == "add" for o in ops)


def test_an_upgrade_already_in_the_library_or_excluded_is_not_added_again(conn):
    from runsync import ytclean
    _queued(conn, "there")
    _queued(conn, "banned", "old2")
    conn.execute("INSERT INTO excluded VALUES ('banned', 'x')")
    assert ytclean._upgrade_ops(conn, "LIB", present={"there"}) == []


def test_a_verified_push_clears_the_outbox_and_forgetting_a_track_clears_its_row(conn):
    from runsync import snapshot, ytclean
    _queued(conn, "n1", "old1")
    _queued(conn, "n2", "old2")
    applied = SimpleNamespace(ops_done=[ytclean.Op("add", "LIB", "n1", "", 0, "upgrade")], live={})
    snapshot.settle_push(conn, applied, ok=True)
    assert [r[0] for r in conn.execute("SELECT new_id FROM upgrades")] == ["n2"]
    snapshot.purge_track(conn, "old2")
    assert conn.execute("SELECT COUNT(*) FROM upgrades").fetchone()[0] == 0


# ---- why a search ended -----------------------------------------------------------------------------------------------------------

def test_every_search_records_why_it_ended(conn, env):
    cfg, sleeps = env
    for i, t in enumerate(("Found", "Miss", "Empty", "Low")):
        add_song(conn, f"s{i}", title=t)
    searches = {"Found": [cand("n0", title="Artist - Found")], "Miss": [cand("x1", title="Totally different", secs=100)],
                "Empty": [], "Low": [cand("n3", title="Artist - Low")]}
    cfg.quality_songs_per_run = 4
    out = run(conn, cfg, sleeps, searches, Probe({"canary": 258, "n0": 258, "n3": 129}))
    codes = {r[0]: r[1] for r in conn.execute("SELECT video_id, quality_result FROM tracks WHERE video_id LIKE 's%'")}
    assert codes == {"s0": "found", "s1": "no_match", "s2": "no_results", "s3": "matches_low"}
    assert out.reasons == {"found": 1, "no_match": 1, "no_results": 1, "matches_low": 1}


def test_songs_searched_before_reasons_existed_are_due_once_and_found_ones_are_not(conn):
    add_song(conn, "legacy", searched=days_ago(3), result=None)            # stamped, but no reason recorded
    add_song(conn, "found", searched=days_ago(3), result="found")
    add_song(conn, "known", searched=days_ago(3), result="no_match")
    assert [s.video_id for s in quality.due_songs(conn, 30, NOW)] == ["legacy"]


def test_the_facts_break_down_why_songs_had_no_better_upload(conn):
    add_song(conn, "a", searched=days_ago(2), result="found")
    add_song(conn, "b", searched=days_ago(2), result="no_match")
    add_song(conn, "c", searched=days_ago(2), result="matches_low")
    add_song(conn, "d", searched=days_ago(2), result="matches_low")
    add_song(conn, "e", searched=days_ago(2), result=None)
    assert quality.facts(conn, 30, NOW)["results"] == {"found": 1, "no_match": 1, "matches_low": 2, "unknown": 1}
