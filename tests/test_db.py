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
    assert conn.execute("SELECT COUNT(*) FROM track_events").fetchone()[0] == 0


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


def test_log_event(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    db.log_event(conn, "a", "download_ok", {"size": 5})
    row = conn.execute("SELECT event, detail FROM track_events").fetchone()
    assert row["event"] == "download_ok" and '"size": 5' in row["detail"]


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
