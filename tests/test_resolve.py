"""End-to-end resolve pipeline on a real DB and real files, with a fake network."""
import hashlib
import json

import pytest
from conftest import add_track, make_m4a, needs_ffmpeg
from mutagen.mp4 import MP4

from runsync.config import Config
from runsync.resolve import run as run_mod
from runsync.resolve.overrides import save_override
from runsync.resolve.run import Reporter, run_resolve

YT = {"yt_track": "In Pieces", "yt_artist": "Linkin Park", "yt_album": "Minutes to Midnight",
      "yt_release_year": 2007, "original_title": "In Pieces", "channel": "Linkin Park"}


class FakeHttp:
    """Answers iTunes genre lookups; counts calls. mb/itunes_songs can be set per test."""
    hits = misses = failures = 0

    def __init__(self, genre="Rock", songs=None):
        self.genre, self.songs, self.calls = genre, songs or [], 0

    def get_json(self, base, params):
        self.calls += 1
        if "itunes" in base:
            if params.get("attribute") == "artistTerm":
                return {"results": [{"artistName": params["term"], "primaryGenreName": self.genre}]}
            return {"results": self.songs}
        return {"recordings": [], "artists": []}


class Rec(Reporter):
    def __init__(self):
        self.resolved_, self.skipped_, self.warned = [], [], []

    def resolved(self, video_id, title, cand, review, languages=()):
        self.resolved_.append((video_id, cand.tier, review))

    def skipped(self, video_id, reason):
        self.skipped_.append((video_id, reason))

    def warn(self, msg):
        self.warned.append(msg)


@pytest.fixture
def cfg(tmp_path):
    c = Config(library=tmp_path / "lib")
    c.ensure_dirs()
    return c


@pytest.fixture
def lib(cfg):
    from runsync import db
    return db.connect(cfg.db_path)


def no_cover(*a, **k):
    return None


def fake_gain(path):
    return {"loudness_lufs": -10.0, "replaygain_gain_db": -8.0, "replaygain_peak": 0.9}


def meta(conn, vid):
    return conn.execute("SELECT * FROM metadata WHERE video_id = ?", (vid,)).fetchone()


@needs_ffmpeg
def test_resolves_tags_and_builds_player_copy_without_touching_master(cfg, lib):
    master = make_m4a(cfg.master / "a.m4a")
    sha = hashlib.sha256(master.read_bytes()).hexdigest()
    add_track(lib, "a", master, **YT)
    res = run_resolve(cfg, lib, http=FakeHttp(), cover_fn=no_cover, analyse_fn=fake_gain)
    m = meta(lib, "a")
    assert (m["artist"], m["title"], m["album"], m["year"], m["language"]) == (
        "Linkin Park", "In Pieces", "Minutes to Midnight", 2007, "english")
    assert m["source_tier"] == "youtube" and not m["needs_review"] and m["replaygain_gain_db"] == -8.0
    assert (res.resolved, res.built, res.review) == (1, 1, 0)
    copy = cfg.player / "m4a" / "In Pieces.m4a"
    assert MP4(copy).tags["\xa9alb"] == ["Minutes to Midnight"]
    assert hashlib.sha256(master.read_bytes()).hexdigest() == sha
    track = lib.execute("SELECT clean_filename, player_format FROM tracks WHERE video_id='a'").fetchone()
    assert (track["clean_filename"], track["player_format"]) == (copy.name, "m4a")
    events = [r["event"] for r in lib.execute("SELECT event FROM track_events WHERE video_id='a'")]
    assert "resolved" in events and "player_built" in events


@needs_ffmpeg
def test_rerun_is_a_no_op(cfg, lib):
    add_track(lib, "a", make_m4a(cfg.master / "a.m4a"), **YT)
    run_resolve(cfg, lib, http=FakeHttp(), cover_fn=no_cover, analyse_fn=fake_gain)
    http = FakeHttp()
    res = run_resolve(cfg, lib, http=http, cover_fn=no_cover, analyse_fn=fake_gain)
    assert (res.considered, res.built, http.calls) == (0, 0, 0)


def test_offline_resolves_but_does_not_guess_the_language(cfg, lib):
    add_track(lib, "a", None, **YT)
    res = run_resolve(cfg, lib, http=None, build=False)
    m = meta(lib, "a")
    assert m["artist"] == "Linkin Park" and m["language"] is None
    assert m["needs_review"] == 0 and res.review == 0           # a missing language never blocks a track
    assert res.unlabelled == {"Linkin Park": 1}                  # ...it is listed for you to label once


def test_dry_run_writes_nothing(cfg, lib):
    add_track(lib, "a", None, **YT)
    rep = Rec()
    res = run_resolve(cfg, lib, dry_run=True, http=FakeHttp(), rep=rep)
    assert res.resolved == 1 and rep.resolved_
    assert lib.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 0
    assert lib.execute("SELECT COUNT(*) FROM metadata_candidates").fetchone()[0] == 0
    assert not cfg.review_path.exists() and not cfg.player.exists()


def test_low_confidence_goes_to_review_csv_with_reason(cfg, lib):
    add_track(lib, "p", None, original_title="Song | Singer | Actor | Latest Punjabi Songs", channel="T-Series")
    run_resolve(cfg, lib, http=None, build=False)
    m = meta(lib, "p")
    assert m["needs_review"] == 1 and m["title"] == "Song"
    text = cfg.review_path.read_text()
    assert "p," in text and "no artist" in text


def test_override_wins_locks_and_survives_reruns(cfg, lib):
    add_track(lib, "a", None, **YT)
    save_override(lib, "track", "a", {"artist": "Chester", "title": "Pieces", "language": "hindi"})
    run_resolve(cfg, lib, http=FakeHttp(), build=False)
    m = meta(lib, "a")
    assert (m["artist"], m["title"], m["source_tier"], m["locked"], m["language"]) == (
        "Chester", "Pieces", "override", 1, "hindi")
    # the override row is deleted and a better-looking candidate appears: the lock still holds
    lib.execute("DELETE FROM overrides")
    lib.execute("UPDATE metadata SET needs_review = 1 WHERE video_id='a'")
    run_resolve(cfg, lib, http=FakeHttp(), build=False)
    assert meta(lib, "a")["artist"] == "Chester"


def test_existing_higher_trust_result_is_not_replaced_by_a_lower_tier(cfg, lib):
    add_track(lib, "a", None, original_title="Zzz - Qqq (Official Video)", channel="Zzz")
    lib.execute("INSERT INTO metadata (video_id, artist, title, source_tier, confidence, needs_review, locked) "
                "VALUES ('a', 'Real Artist', 'Real Title', 'youtube', 0.95, 1, 0)")
    lib.commit()
    res = run_resolve(cfg, lib, http=FakeHttp(), build=False)
    m = meta(lib, "a")
    assert (m["artist"], m["source_tier"], res.kept) == ("Real Artist", "youtube", 1)


def test_network_failure_keeps_offline_result_and_does_not_crash(cfg, lib):
    class Down(FakeHttp):
        failures = 3

        def get_json(self, base, params):
            return None
    add_track(lib, "a", None, original_title="Metallica - One (Official Video)", channel="Label Records")
    res = run_resolve(cfg, lib, http=Down(), build=False)
    m = meta(lib, "a")
    assert (m["artist"], m["title"]) == ("Metallica", "One") and m["language"] is None and res.resolved == 1


def test_one_bad_track_does_not_stop_the_run(cfg, lib, monkeypatch):
    add_track(lib, "bad", None, **YT)
    add_track(lib, "good", None, **YT)
    real = run_mod.resolve_candidates

    def flaky(row, http):
        if row["video_id"] == "bad":
            raise RuntimeError("boom")
        return real(row, http)
    monkeypatch.setattr(run_mod, "resolve_candidates", flaky)
    rep = Rec()
    res = run_resolve(cfg, lib, http=None, build=False, rep=rep)
    assert res.resolved == 1 and meta(lib, "good") and meta(lib, "bad") is None
    assert any("bad" in w and "boom" in w for w in rep.warned)


@needs_ffmpeg
def test_opus_master_and_unholdable_title_get_no_player_copy(cfg, lib):
    (cfg.master / "o.webm").write_bytes(b"opus")
    add_track(lib, "o", cfg.master / "o.webm", **YT)
    add_track(lib, "d", make_m4a(cfg.master / "d.m4a"), original_title="Привет", yt_track="Привет",
              yt_artist="Arijit Singh", channel="Arijit Singh")
    rep = Rec()
    res = run_resolve(cfg, lib, http=FakeHttp(), rep=rep, cover_fn=no_cover, analyse_fn=fake_gain)
    assert res.built == 0 and res.build_skipped == 1 and res.held == 1
    assert "cannot show" in meta(lib, "d")["review_reason"]
    assert any("Phase 4" in r for _, r in rep.skipped_)
    assert not (cfg.player / "m4a").exists() or not list((cfg.player / "m4a").glob("*.m4a"))


def test_review_csv_fixes_become_overrides_and_are_applied_next_run(cfg, lib):
    import csv
    add_track(lib, "p", None, original_title="Song | Singer | Actor | Latest Punjabi Songs", channel="T-Series")
    run_resolve(cfg, lib, http=None, build=False)
    with cfg.review_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0]["fix_artist"], rows[0]["fix_language"] = "Real Singer", "Punjabi"
    with cfg.review_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    res = run_resolve(cfg, lib, http=None, build=False)
    m = meta(lib, "p")
    assert res.imported_fixes == 1
    assert (m["artist"], m["language"], m["source_tier"], m["needs_review"]) == ("Real Singer", "punjabi",
                                                                               "override", 0)
    assert json.loads(m["artists_json"]) == ["Real Singer"]


def test_candidates_from_every_tier_are_kept_for_audit(cfg, lib):
    add_track(lib, "a", None, original_title="Metallica - One (Official Video)", channel="Label Records")
    songs = [{"trackName": "One", "artistName": "Metallica", "collectionName": "...And Justice for All",
              "releaseDate": "1988-08-25T07:00:00Z", "trackTimeMillis": 447000, "primaryGenreName": "Metal"}]
    run_resolve(cfg, lib, http=FakeHttp(songs=songs), build=False)
    tiers = {r["tier"] for r in lib.execute("SELECT tier FROM metadata_candidates WHERE video_id='a'")}
    assert "parser" in tiers


def test_locked_row_is_never_rewritten_even_if_its_tier_looks_weak(cfg, lib):
    """The lock must hold on its own, not only because 'override' outranks other tiers."""
    add_track(lib, "a", None, **YT)
    lib.execute("INSERT INTO metadata (video_id, artist, title, source_tier, confidence, needs_review, locked) "
                "VALUES ('a', 'Hand Fixed', 'Hand Title', 'parser', 0.1, 1, 1)")
    lib.commit()
    res = run_resolve(cfg, lib, http=FakeHttp(), build=False)
    assert (meta(lib, "a")["artist"], res.kept) == ("Hand Fixed", 1)


def test_dry_run_stores_no_artist_labels_either(cfg, lib):
    add_track(lib, "a", None, **YT)
    run_resolve(cfg, lib, dry_run=True, http=FakeHttp())
    assert lib.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 0


def test_the_uploads_own_evidence_beats_the_artists_label(cfg, lib):
    """A Punjabi artist singing in a Bollywood film: the upload says Hindi, so the track is Hindi."""
    add_track(lib, "a", None, original_title="Duniyaa | Luka Chuppi", channel="T-Series",
              yt_tags='["bollywood songs", "latest hindi songs"]', yt_track="Duniyaa", yt_artist="Akhil")
    run_resolve(cfg, lib, http=FakeHttp(genre="Punjabi Pop"), build=False)
    assert meta(lib, "a")["language"] == "hindi"


def test_without_any_evidence_the_artists_label_is_used_and_never_a_guess(cfg, lib):
    add_track(lib, "a", None, **YT)
    run_resolve(cfg, lib, http=FakeHttp(genre="Bollywood"), build=False)
    assert meta(lib, "a")["language"] == "hindi"
    add_track(lib, "b", None, **{**YT, "yt_artist": "Badshah", "yt_track": "Bamb"})
    res = run_resolve(cfg, lib, http=FakeHttp(genre="Indian Pop"), build=False)
    assert meta(lib, "b")["language"] is None and "Badshah" in res.unlabelled


def test_lead_artist_decides_language_collaborators_do_not_vote(cfg, lib):
    add_track(lib, "a", None, original_title="SUIT", yt_track="SUIT", yt_artist="Guru Randhawa, Arjun, Intense")

    class Http(FakeHttp):
        def get_json(self, base, params):
            self.calls += 1
            if "itunes" in base and params.get("attribute") == "artistTerm":
                g = "Pop" if params["term"] == "Guru Randhawa" else "Rock"
                return {"results": [{"artistName": params["term"], "primaryGenreName": g}]}
            return {"results": [], "recordings": [], "artists": []}
    run_resolve(cfg, lib, http=Http(), build=False)
    assert meta(lib, "a")["language"] is None          # lead is unlabelled; the "Rock" collaborators must not decide


@needs_ffmpeg
def test_redo_renames_and_removes_the_stale_copy_but_never_the_master(cfg, lib):
    master = make_m4a(cfg.master / "a.m4a")
    add_track(lib, "a", master, **YT)
    run_resolve(cfg, lib, http=FakeHttp(), cover_fn=no_cover, analyse_fn=fake_gain)
    before = hashlib.sha256(master.read_bytes()).hexdigest()
    old = cfg.player / "m4a" / "In Pieces.m4a"
    assert old.exists()
    save_override(lib, "track", "a", {"title": "In Pieces (Fixed)"})
    run_resolve(cfg, lib, http=FakeHttp(), cover_fn=no_cover, analyse_fn=fake_gain, redo=True)
    assert not old.exists() and (cfg.player / "m4a" / "In Pieces (Fixed).m4a").exists()
    assert hashlib.sha256(master.read_bytes()).hexdigest() == before


def test_search_answer_is_applied_on_the_next_run_and_labels_other_tracks(cfg, lib):
    add_track(lib, "a", None, original_title="Mystery Song", yt_track="Mystery Song", yt_artist="Guru Randhawa")
    add_track(lib, "b", None, original_title="Other", yt_track="Other", yt_artist="Guru Randhawa, Someone")
    res = run_resolve(cfg, lib, http=FakeHttp(genre="Pop"), build=False)
    assert meta(lib, "a")["language"] is None and res.enrich_open == 1       # one artist row, covering both tracks
    import csv
    rows = list(csv.DictReader(cfg.enrich_path.open(encoding="utf-8")))
    rows[0].update(suggested_language="punjabi", confidence="0.9", evidence_url="https://example.com/g")
    with cfg.enrich_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    res = run_resolve(cfg, lib, http=FakeHttp(genre="Pop"), build=False)
    assert res.enrich_applied == 1 and res.considered == 2
    assert meta(lib, "a")["language"] == "punjabi" and meta(lib, "b")["language"] == "punjabi"
    assert res.enrich_open == 0 and not cfg.enrich_path.exists()


def test_result_dict_and_cli_json_for_the_skills(cfg, lib, monkeypatch):
    import json as jsonlib

    from typer.testing import CliRunner

    from runsync.cli import app
    add_track(lib, "a", None, **YT)
    run_resolve(cfg, lib, http=FakeHttp(genre="Rock"), build=False)
    d = run_mod.ResolveResult(resolved=3, review=1, by_tier={"youtube": 2}).to_dict()
    assert (d["resolved"], d["review"], d["by_tier"]) == (3, 1, {"youtube": 2}) and "enrich_open" in d
    monkeypatch.setenv("RUNSYNC_HOME", str(cfg.library))
    out = CliRunner().invoke(app, ["resolve", "--offline", "--no-build", "--json"])
    last = [ln for ln in out.output.splitlines() if ln.startswith("{")][-1]
    assert jsonlib.loads(last)["dry_run"] is False
