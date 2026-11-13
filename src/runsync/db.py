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
        status TEXT NOT NULL DEFAULT 'pending', -- pending | downloaded | unavailable | trashed | merged
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
    # --- v3: resolved metadata (Phase 2a) --------------------------------
    """
    CREATE TABLE metadata (
        video_id TEXT PRIMARY KEY REFERENCES tracks(video_id) ON DELETE CASCADE,
        artist TEXT,                 -- display artist(s), "A, B"
        artists_json TEXT,           -- JSON list, primary first
        album_artist TEXT,
        title TEXT,
        album TEXT,
        year INTEGER,
        language TEXT,               -- primary language
        languages_json TEXT,
        versions_json TEXT,          -- live / remix / remaster ... (Phase 3 uses these)
        original_title TEXT,         -- as uploaded
        confidence REAL,
        source_tier TEXT,            -- override | youtube | musicbrainz | itunes | parser
        locked INTEGER NOT NULL DEFAULT 0,   -- 1 = came from an override, never overwritten
        needs_review INTEGER NOT NULL DEFAULT 0,
        review_reason TEXT,
        mb_recording_id TEXT,
        mb_release_id TEXT,
        cover_path TEXT,
        cover_source TEXT,           -- coverartarchive | itunes | youtube
        replaygain_gain_db REAL,
        replaygain_peak REAL,
        loudness_lufs REAL,
        resolved_at TEXT
    );
    CREATE TABLE metadata_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL,
        at TEXT NOT NULL,
        tier TEXT NOT NULL,
        artist TEXT, title TEXT, album TEXT, year INTEGER,
        confidence REAL,
        detail TEXT                  -- JSON
    );
    CREATE INDEX idx_candidates_video ON metadata_candidates(video_id, at);
    CREATE TABLE artists (
        name TEXT PRIMARY KEY,       -- canonical display name
        key TEXT NOT NULL,           -- normalised comparison key
        languages_json TEXT,
        confidence REAL,
        source TEXT,                 -- override | itunes_genre | musicbrainz_country | unknown
        country TEXT,
        genre TEXT,
        updated_at TEXT
    );
    CREATE UNIQUE INDEX idx_artists_key ON artists(key);
    CREATE TABLE http_cache (
        key TEXT PRIMARY KEY,        -- full request URL
        body TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE overrides (
        kind TEXT NOT NULL,          -- track | artist
        key TEXT NOT NULL,           -- video id, or artist name
        fields_json TEXT NOT NULL,   -- track: artist/title/album/year/language; artist: languages
        source TEXT NOT NULL,        -- human | search
        evidence TEXT,
        confidence REAL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, key)
    );
    """,
    # --- v4: dedupe --------------------------------------------------------
    """
    CREATE TABLE aliases (            -- a removed duplicate and the track that replaced it
        video_id TEXT PRIMARY KEY,
        winner_id TEXT NOT NULL,
        similarity REAL,
        reason TEXT,
        merged_at TEXT NOT NULL
    );
    CREATE TABLE dedupe_decisions (   -- the owner's answer to "same song?" (a < b)
        a TEXT NOT NULL,
        b TEXT NOT NULL,
        same INTEGER NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (a, b)
    );
    CREATE TABLE fingerprints (       -- chromaprint of a master, keyed by its sha256 so an edited file is redone
        video_id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL,
        ints TEXT NOT NULL
    );
    """,
    # --- v5: cards (Phase 4) -------------------------------------------------
    """
    CREATE TABLE devices (            -- a card runsync may write to; identified by its volume UUID, never by name
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        volume_uuid TEXT NOT NULL UNIQUE,
        subpath TEXT NOT NULL DEFAULT 'Music',
        registered_at TEXT NOT NULL
    );
    CREATE TABLE device_tracks (      -- the manifest: exactly what runsync has put on this card (audio and playlists)
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        video_id TEXT NOT NULL,       -- '' for a playlist file
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        src_mtime_ns INTEGER NOT NULL DEFAULT 0,
        synced_at TEXT NOT NULL,
        PRIMARY KEY (device_id, filename)
    );
    CREATE TABLE card_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,         -- running | done | interrupted | undone
        plan_id TEXT
    );
    CREATE TABLE card_journal (       -- what a run did, so the last run can be undone
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES card_runs(id) ON DELETE CASCADE,
        op TEXT NOT NULL,             -- add | update | remove
        video_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        src TEXT,
        size INTEGER
    );
    """,
    # --- v6: the YouTube API (quota ledger and a journal of playlist edits) ---
    """
    CREATE TABLE api_usage (          -- quota units spent per day (YouTube's quota resets at midnight Pacific)
        day TEXT PRIMARY KEY,
        units INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE youtube_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,         -- running | done | interrupted | undone
        plan_id TEXT
    );
    CREATE TABLE youtube_journal (    -- every playlist edit, so the last run can be undone
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES youtube_runs(id) ON DELETE CASCADE,
        op TEXT NOT NULL,             -- delete | add
        playlist_id TEXT NOT NULL,
        video_id TEXT NOT NULL,
        playlist_item_id TEXT,
        position INTEGER
    );
    """,
    """
    CREATE TABLE replacement_picks (   -- the owner's answer for a song YouTube no longer serves (one row per lost video id)
        lost_id TEXT PRIMARY KEY,
        pick_id TEXT,                  -- the upload chosen as its replacement (NULL when skipped)
        title TEXT,                    -- the chosen upload's title
        status TEXT NOT NULL,          -- picked (replacement chosen, waiting to be pushed) | skipped (owner said none)
        decided_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE tracks ADD COLUMN player_built_at TEXT;   -- when the player copy was last built (was an event)
    UPDATE tracks SET player_built_at = (SELECT MAX(at) FROM track_events e
                                         WHERE e.video_id = tracks.video_id AND e.event = 'player_built');
    DROP TABLE track_events;             -- history: nothing else read it
    DROP TABLE runs;                     -- history: nothing read it
    """,
    """
    DROP TABLE metadata_candidates;      -- every candidate ever considered: written, never read
    """,
    """
    CREATE TABLE unwanted (              -- songs the owner wants gone: playlist entries removed, liked ones unliked by hand, then forgotten
        video_id TEXT PRIMARY KEY,
        decided_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE unwanted RENAME TO excluded;   -- songs that must never be in the library (removed while still liked)
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- preferences, e.g. library_playlist
    CREATE TABLE liked_seen (video_id TEXT PRIMARY KEY);                 -- Liked Music as of the last mirror
    """,
    """
    ALTER TABLE tracks ADD COLUMN quality_searched_at TEXT;   -- when we last looked for a better upload of this song
    CREATE TABLE upgrades (              -- better uploads found, waiting to be added to the library list
        new_id TEXT PRIMARY KEY,
        old_id TEXT NOT NULL,
        title TEXT,
        new_channel TEXT,
        new_kbps REAL,
        old_channel TEXT,
        old_kbps REAL
    );
    """,
    """
    ALTER TABLE tracks ADD COLUMN quality_result TEXT;   -- why the last quality search ended: found | no_results | no_match | matches_low
    UPDATE tracks SET quality_result = 'found' WHERE video_id IN (SELECT old_id FROM upgrades);
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    conn = sqlite3.connect(path, timeout=30)  # wait for a concurrent writer instead of failing
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if existed and 0 < version < len(MIGRATIONS):
        # a migration can drop tables: keep one copy of the database as it was (overwritten by the next migration)
        backup = path.with_name(path.name + ".pre-migration")
        with sqlite3.connect(backup) as dest:
            conn.backup(dest)
        dest.close()
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


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {i}")
    conn.commit()
