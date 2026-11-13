import shutil
import subprocess

import pytest

from runsync import db
from runsync.download import extract_source_meta, file_facts


def test_migrations_reach_latest_and_columns_exist(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
    assert {"sha256", "file_size_bytes", "original_title", "clean_filename", "candidates_json"} <= cols
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not ({"track_events", "runs"} & tables)           # no history tables: the database is a snapshot


def test_v1_database_upgrades_keeping_rows(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.MIGRATIONS[0])
    raw.execute("INSERT INTO tracks (video_id, title, status) VALUES ('abc', 'T', 'downloaded')")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    row = conn.execute("SELECT * FROM tracks WHERE video_id = 'abc'").fetchone()
    assert row["title"] == "T" and row["first_seen_at"] and row["sha256"] is None


def test_update_track_rejects_unknown_column(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    conn.execute("INSERT INTO tracks (video_id) VALUES ('a')")
    db.update_track(conn, "a", channel="X")
    assert conn.execute("SELECT channel FROM tracks").fetchone()[0] == "X"
    with pytest.raises(KeyError):
        db.update_track(conn, "a", bogus=1)


def test_extract_source_meta():
    m = extract_source_meta({"title": "T", "channel": "C", "duration": 200, "description": "d" * 9000,
                             "tags": ["a"], "artists": ["X", "Y"], "album": "Al", "release_year": 2020})
    assert m["original_title"] == "T" and len(m["description"]) == 4000
    assert m["yt_artist"] == "X, Y" and m["yt_tags"] == '["a"]' and m["yt_release_year"] == 2020


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_file_facts_on_real_file(tmp_path):
    f = tmp_path / "t.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", str(f)], check=True)
    facts = file_facts(f, youtube_duration=2.0)
    assert facts["file_ext"] == "m4a" and facts["file_size_bytes"] == f.stat().st_size
    assert len(facts["sha256"]) == 64 and facts["probe_codec"] == "aac"
    assert abs(facts["duration_drift_s"]) < 0.5


def test_the_player_built_event_becomes_a_column_and_the_history_tables_are_dropped(tmp_path):
    import sqlite3
    idx = next(i for i, s in enumerate(db.MIGRATIONS) if "player_built_at" in s)
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    for script in db.MIGRATIONS[:idx]:
        conn.executescript(script)
    conn.execute(f"PRAGMA user_version = {idx}")
    conn.execute("INSERT INTO tracks (video_id, status) VALUES ('a', 'downloaded'), ('b', 'downloaded')")
    conn.execute("INSERT INTO track_events (video_id, at, event) VALUES ('a', '2026-11-01T10:00:00', 'player_built'), "
                 "('a', '2026-11-02T10:00:00', 'player_built'), ('a', '2026-11-03T10:00:00', 'download_ok')")
    conn.commit()
    conn.close()
    new = db.connect(tmp_path / "old.db")
    built = {r["video_id"]: r["player_built_at"] for r in new.execute("SELECT video_id, player_built_at FROM tracks")}
    assert built == {"a": "2026-11-02T10:00:00", "b": None}                # the latest build time survives
    tables = {r["name"] for r in new.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not ({"track_events", "runs"} & tables)


def test_a_database_that_already_ran_the_previous_migrations_still_drops_the_candidates_table(tmp_path):
    """Migrations are append-only: a database already past a version never re-runs it, so each change is its own step."""
    import sqlite3
    idx = next(i for i, s in enumerate(db.MIGRATIONS) if "DROP TABLE metadata_candidates" in s)
    conn = sqlite3.connect(tmp_path / "old.db")
    for script in db.MIGRATIONS[:idx]:
        conn.executescript(script)
    conn.execute(f"PRAGMA user_version = {idx}")
    conn.commit()
    conn.close()
    tables = {r["name"] for r in db.connect(tmp_path / "old.db").execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "metadata_candidates" not in tables and idx < len(db.MIGRATIONS)


def test_a_pending_migration_first_keeps_a_copy_of_the_database_as_it_was(tmp_path):
    import sqlite3
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    for script in db.MIGRATIONS[:-1]:
        conn.executescript(script)
    conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) - 1}")
    conn.execute("INSERT INTO playlists (id, title, url) VALUES ('P', 'Keep me', 'u')")
    conn.commit()
    conn.close()
    db.connect(path).close()
    backup = sqlite3.connect(tmp_path / "state.db.pre-migration")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) - 1      # as it was before
    assert backup.execute("SELECT title FROM playlists").fetchone()[0] == "Keep me"
    backup.close()


def test_no_backup_for_a_new_or_current_database(tmp_path):
    db.connect(tmp_path / "a.db").close()                 # brand new
    db.connect(tmp_path / "a.db").close()                 # already current
    assert not (tmp_path / "a.db.pre-migration").exists()
