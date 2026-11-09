"""SQLite state with simple versioned migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE playlists (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'account',   -- account | url
        selected INTEGER NOT NULL DEFAULT 0,
        last_fetch_ok INTEGER,                  -- NULL never, 1 ok, 0 failed
        last_fetched_at TEXT
    );
    CREATE TABLE tracks (
        video_id TEXT PRIMARY KEY,
        title TEXT,
        status TEXT NOT NULL DEFAULT 'pending', -- pending | downloaded | unavailable | trashed
        itag TEXT, codec TEXT, bitrate REAL,
        master_path TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_attempt_at TEXT
    );
    CREATE TABLE playlist_items (
        playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        video_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (playlist_id, video_id)
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        dry_run INTEGER NOT NULL DEFAULT 0,
        summary TEXT
    );
    """,
    # --- v2: full per-track record --------------------------------------
    """
    -- source facts (YouTube)
    ALTER TABLE tracks ADD COLUMN original_title TEXT;
    ALTER TABLE tracks ADD COLUMN channel TEXT;
    ALTER TABLE tracks ADD COLUMN channel_id TEXT;
    ALTER TABLE tracks ADD COLUMN uploader TEXT;
    ALTER TABLE tracks ADD COLUMN upload_date TEXT;
    ALTER TABLE tracks ADD COLUMN duration_s REAL;
    ALTER TABLE tracks ADD COLUMN description TEXT;
    ALTER TABLE tracks ADD COLUMN yt_tags TEXT;            -- JSON list
    ALTER TABLE tracks ADD COLUMN yt_categories TEXT;      -- JSON list
    ALTER TABLE tracks ADD COLUMN webpage_url TEXT;
    ALTER TABLE tracks ADD COLUMN availability TEXT;
    ALTER TABLE tracks ADD COLUMN yt_track TEXT;           -- yt-dlp music fields (Topic uploads)
    ALTER TABLE tracks ADD COLUMN yt_artist TEXT;
    ALTER TABLE tracks ADD COLUMN yt_album TEXT;
    ALTER TABLE tracks ADD COLUMN yt_release_year INTEGER;
    -- chosen stream
    ALTER TABLE tracks ADD COLUMN sample_rate INTEGER;
    ALTER TABLE tracks ADD COLUMN channels INTEGER;
    ALTER TABLE tracks ADD COLUMN container TEXT;
    ALTER TABLE tracks ADD COLUMN format_note TEXT;
    ALTER TABLE tracks ADD COLUMN stream_size_approx INTEGER;
    ALTER TABLE tracks ADD COLUMN candidates_json TEXT;    -- every audio format offered
    ALTER TABLE tracks ADD COLUMN used_cookies INTEGER;
    ALTER TABLE tracks ADD COLUMN ytdlp_version TEXT;
    -- file on disk (master)
    ALTER TABLE tracks ADD COLUMN master_filename TEXT;
    ALTER TABLE tracks ADD COLUMN file_ext TEXT;
    ALTER TABLE tracks ADD COLUMN file_size_bytes INTEGER;
    ALTER TABLE tracks ADD COLUMN sha256 TEXT;
    ALTER TABLE tracks ADD COLUMN probe_codec TEXT;
    ALTER TABLE tracks ADD COLUMN probe_bitrate_kbps REAL;
    ALTER TABLE tracks ADD COLUMN probe_sample_rate INTEGER;
    ALTER TABLE tracks ADD COLUMN probe_channels INTEGER;
    ALTER TABLE tracks ADD COLUMN probe_duration_s REAL;
    ALTER TABLE tracks ADD COLUMN duration_drift_s REAL;   -- probe minus YouTube duration
    ALTER TABLE tracks ADD COLUMN download_seconds REAL;
    -- naming + player copy (filled in later phases)
    ALTER TABLE tracks ADD COLUMN clean_filename TEXT;
    ALTER TABLE tracks ADD COLUMN player_path TEXT;
    ALTER TABLE tracks ADD COLUMN player_format TEXT;
    ALTER TABLE tracks ADD COLUMN player_size_bytes INTEGER;
    -- lifecycle
    ALTER TABLE tracks ADD COLUMN first_seen_at TEXT;
    ALTER TABLE tracks ADD COLUMN last_seen_at TEXT;
    ALTER TABLE tracks ADD COLUMN downloaded_at TEXT;
    ALTER TABLE tracks ADD COLUMN trashed_at TEXT;
    ALTER TABLE tracks ADD COLUMN trash_path TEXT;
    UPDATE tracks SET first_seen_at = COALESCE(last_attempt_at, strftime('%Y-%m-%dT%H:%M:%S+00:00','now')),
                      last_seen_at = strftime('%Y-%m-%dT%H:%M:%S+00:00','now');

    CREATE TABLE track_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL,
        at TEXT NOT NULL,
        event TEXT NOT NULL,        -- discovered | download_ok | download_failed | retry | unavailable | trashed | ...
        detail TEXT                 -- JSON
    );
    CREATE INDEX idx_track_events_video ON track_events(video_id, at);
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)  # wait for a concurrent writer instead of failing
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


TRACK_COLUMNS: set[str] = set()


def update_track(conn: sqlite3.Connection, video_id: str, **cols) -> None:
    """UPDATE tracks SET <cols> WHERE video_id. Column names are checked against the schema."""
    if not TRACK_COLUMNS:
        TRACK_COLUMNS.update(r["name"] for r in conn.execute("PRAGMA table_info(tracks)"))
    bad = set(cols) - TRACK_COLUMNS
    if bad:
        raise KeyError(f"unknown tracks columns: {sorted(bad)}")
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE tracks SET {sets} WHERE video_id = ?", [*cols.values(), video_id])


def log_event(conn: sqlite3.Connection, video_id: str, event: str, detail: dict | None = None) -> None:
    import json
    from datetime import UTC, datetime
    conn.execute("INSERT INTO track_events (video_id, at, event, detail) VALUES (?, ?, ?, ?)",
                 (video_id, datetime.now(UTC).isoformat(timespec="seconds"), event,
                  json.dumps(detail, default=str) if detail else None))


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {i}")
    conn.commit()
