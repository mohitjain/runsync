"""Choose playlists without a prompt: numbered list, then "sync 1,3" / "stop 2" by number or name."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import NamedTuple

from . import fetch
from . import youtube as yt

log = logging.getLogger("runsync.playlists")


class Refresh(NamedTuple):
    source: str            # "api" or "cookies"
    gone: list[dict]       # playlists you sync that no longer exist in your account: [{id, title}]


def refresh_account_playlists(conn: sqlite3.Connection, cookies: Path | None, api: yt.Api | None = None) -> Refresh:
    """Upsert your account playlists from YouTube (the API when signed in, else cookies), keeping the saved selection.
    Unselected ones that vanished are dropped; selected ones that vanished are reported (every sync would fail on them)."""
    found, source = None, "cookies"
    if api is not None:
        try:
            found, source = fetch.api_account_playlists(api), "api"
        except yt.YouTubeError as exc:
            log.warning("playlist list via API failed (%s); using cookies", exc)
    if found is None:
        if cookies is None:
            raise RuntimeError("no YouTube login and no usable cookies")
        found = fetch.fetch_account_playlists(cookies)
    for p in found:
        conn.execute("INSERT INTO playlists (id, title, url, kind) VALUES (?, ?, ?, 'account') "
                     "ON CONFLICT(id) DO UPDATE SET title = excluded.title", (p.id, p.title, p.url))
    ids = [p.id for p in found]
    if ids:  # never prune on an empty result
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM playlists WHERE kind = 'account' AND selected = 0 AND id NOT IN ({marks})", ids)
    gone = [{"id": r["id"], "title": r["title"]} for r in conn.execute(
        f"SELECT id, title FROM playlists WHERE kind = 'account' AND selected = 1 AND id NOT IN ({','.join('?' * len(ids))})", ids)
        ] if ids else []
    conn.commit()
    return Refresh(source, gone)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def library_playlist(conn: sqlite3.Connection) -> dict | None:
    """The library list: the playlist Liked Music is mirrored into and runsync syncs from. {id, title} or None."""
    pid = get_setting(conn, "library_playlist")
    row = conn.execute("SELECT id, title FROM playlists WHERE id = ?", (pid,)).fetchone() if pid else None
    return {"id": row["id"], "title": row["title"]} if row else None


def set_library(conn: sqlite3.Connection, playlist_id: str) -> None:
    """Make a (synced) playlist the library list."""
    conn.execute("UPDATE playlists SET selected = 1 WHERE id = ?", (playlist_id,))
    set_setting(conn, "library_playlist", playlist_id)


def list_playlists(conn: sqlite3.Connection) -> list[dict]:
    """Numbered (1-based, stable order: title then id), with sync state and track count."""
    rows = conn.execute(
        "SELECT p.id, p.title, p.selected, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id = p.id) AS tracks "
        "FROM playlists p ORDER BY lower(p.title), p.id").fetchall()
    return [{"n": i, "id": r["id"], "title": r["title"], "synced": bool(r["selected"]), "tracks": r["tracks"]}
            for i, r in enumerate(rows, start=1)]


def resolve_tokens(rows: list[dict], text: str) -> list[str]:
    """"1, 3, Gym Mix" -> playlist ids. A number, an exact name, or a name part that matches exactly one playlist.
    Anything unknown or ambiguous raises ValueError, so nothing is half-applied."""
    ids: list[str] = []
    for tok in (t.strip() for t in (text or "").split(",")):
        if not tok:
            continue
        if tok.isdigit():
            hit = [r for r in rows if r["n"] == int(tok)]
            if not hit:
                raise ValueError(f"no playlist number {tok} (valid: 1-{len(rows)})")
        else:
            exact = [r for r in rows if r["title"].casefold() == tok.casefold()]
            hit = exact or [r for r in rows if tok.casefold() in r["title"].casefold()]
            if not hit:
                raise ValueError(f"no playlist named {tok!r}")
            if len(hit) > 1:
                raise ValueError(f"{tok!r} matches more than one playlist: " + ", ".join(r["title"] for r in hit))
        if hit[0]["id"] not in ids:
            ids.append(hit[0]["id"])
    return ids


def leaving_tracks(conn: sqlite3.Connection, stop_ids: list[str]) -> int:
    """Downloaded tracks that would be in no selected playlist once these are stopped (trashed by the next sync)."""
    if not stop_ids:
        return 0
    stop = ",".join("?" * len(stop_ids))
    return conn.execute(
        f"SELECT COUNT(DISTINCT t.video_id) FROM tracks t JOIN playlist_items i USING (video_id) "
        f"WHERE t.status = 'downloaded' AND i.playlist_id IN ({stop}) AND NOT EXISTS ("
        f"SELECT 1 FROM playlist_items j JOIN playlists p ON p.id = j.playlist_id "
        f"WHERE j.video_id = t.video_id AND p.selected = 1 AND j.playlist_id NOT IN ({stop}))",
        [*stop_ids, *stop_ids]).fetchone()[0]


def apply_selection(conn: sqlite3.Connection, sync_text: str, stop_text: str) -> dict:
    """Start syncing / stop syncing. Raises ValueError (changing nothing) on a bad token or a playlist in both lists."""
    rows = list_playlists(conn)
    start, stop = resolve_tokens(rows, sync_text), resolve_tokens(rows, stop_text)
    both = set(start) & set(stop)
    if both:
        raise ValueError("in both lists: " + ", ".join(r["title"] for r in rows if r["id"] in both))
    was_synced = [i for i in stop if next(r for r in rows if r["id"] == i)["synced"]]
    conn.executemany("UPDATE playlists SET selected = 1 WHERE id = ?", [(i,) for i in start])
    conn.executemany("UPDATE playlists SET selected = 0 WHERE id = ?", [(i,) for i in stop])
    leaving = leaving_tracks(conn, was_synced)   # counted after the change, so playlists started now keep their tracks
    conn.commit()
    title = {r["id"]: r["title"] for r in rows}
    return {"started": [title[i] for i in start], "stopped": [title[i] for i in stop], "leaving": leaving}
