"""Choose playlists without a prompt: numbered list, then "sync 1,3" / "stop 2" by number or name."""
from __future__ import annotations

import sqlite3


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
