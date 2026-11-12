"""Keep state.db a snapshot of YouTube, not a history.

YouTube is the truth. The database holds what the last sync saw plus the changes still waiting to be pushed. Once a push
is verified, what is dead is deleted from the database too: tracks that are merged or unavailable and gone from every
playlist, their aliases, decisions and picks, and the undo note once a later sync has confirmed YouTube. Nothing here
prints.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import youtube as yt


def purge_track(conn: sqlite3.Connection, video_id: str) -> None:
    """Forget a video entirely (the row and everything keyed by it)."""
    for sql in ("DELETE FROM metadata WHERE video_id = ?",
                "DELETE FROM overrides WHERE kind = 'track' AND key = ?", "DELETE FROM fingerprints WHERE video_id = ?",
                "DELETE FROM aliases WHERE video_id = ?", "DELETE FROM replacement_picks WHERE lost_id = ?",
                "DELETE FROM unwanted WHERE video_id = ?",
                "DELETE FROM playlist_items WHERE video_id = ?", "DELETE FROM tracks WHERE video_id = ?"):
        conn.execute(sql, (video_id,))
    conn.execute("DELETE FROM dedupe_decisions WHERE a = ? OR b = ?", (video_id, video_id))


def purge_gone(conn: sqlite3.Connection, present: set[str] | None = None, candidates: set[str] | None = None) -> int:
    """Delete merged and unavailable tracks that are in no playlist any more. `present` = every video id the selected
    playlists list right now (None: use the stored snapshot); `candidates` narrows what may be purged."""
    if present is None:
        present = {r["video_id"] for r in conn.execute("SELECT DISTINCT video_id FROM playlist_items")}
    rows = conn.execute("SELECT video_id FROM tracks WHERE status IN ('merged', 'unavailable') AND master_path IS NULL").fetchall()
    gone = [r["video_id"] for r in rows if r["video_id"] not in present and (candidates is None or r["video_id"] in candidates)]
    for v in gone:
        purge_track(conn, v)
    conn.commit()
    return len(gone)


def purge_trashed(conn: sqlite3.Connection) -> int:
    """A trashed track's row goes with its file once the trash has been emptied of it."""
    rows = conn.execute("SELECT video_id, trash_path FROM tracks WHERE status = 'trashed'").fetchall()
    gone = [r["video_id"] for r in rows if not r["trash_path"] or not Path(r["trash_path"]).exists()]
    for v in gone:
        purge_track(conn, v)
    conn.commit()
    return len(gone)


def refresh_playlists(conn: sqlite3.Connection, live: dict[str, list[yt.Entry]]) -> None:
    """Make the stored playlists match what YouTube just showed (the snapshot)."""
    for pid, entries in live.items():
        if conn.execute("SELECT 1 FROM playlists WHERE id = ?", (pid,)).fetchone() is None:
            continue
        conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))
        seen: set[str] = set()
        rows = []
        for e in entries:
            if e.available and e.video_id not in seen:
                seen.add(e.video_id)
                rows.append((pid, e.video_id, len(rows)))
        conn.executemany("INSERT OR IGNORE INTO playlist_items VALUES (?, ?, ?)", rows)
    conn.commit()


def settle_push(conn: sqlite3.Connection, applied, ok: bool) -> dict:
    """After a verified push: the snapshot follows YouTube and what the push made dead is deleted. Does nothing unless
    the read-back confirmed every change."""
    if not ok or not applied.ops_done:
        return {"purged": 0}
    refresh_playlists(conn, applied.live)
    removed = {o.video_id for o in applied.ops_done if o.kind == "delete" and o.why in ("dead", "replace", "loser", "swap")}
    return {"purged": purge_gone(conn, candidates=removed)}


def forget_pushed(conn: sqlite3.Connection, today: str) -> None:
    """A completed sync has confirmed YouTube: the undo note for the last push and old quota days are not needed."""
    conn.execute("DELETE FROM youtube_journal")
    conn.execute("DELETE FROM youtube_runs")
    conn.execute("DELETE FROM api_usage WHERE day != ?", (today,))
    conn.commit()
