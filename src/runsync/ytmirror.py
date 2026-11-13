"""Mirror Liked Music into the library list.

Liked Music cannot be edited through the API, a playlist can. So Liked Music is a read-only input that feeds the
library list: a new like is added to it, an unlike is removed from it. The library list is what runsync syncs.
Dead songs, merged duplicates and songs the owner excluded never enter it; nothing here ever changes a like.

`plan_mirror` is pure: it only returns the operations that would make the list match. They join the normal
cleanup plan (same preview, confirmation, read-back and undo).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from . import fetch
from . import youtube as yt
from .config import Config
from .cookies import check_cookies
from .ytclean import Op

LIKED_URL = "https://music.youtube.com/playlist?list=LM"


@dataclass
class Liked:
    """Liked Music as read just now. `paused` says why it could not be read (then nothing is planned)."""
    ids: list[str] = field(default_factory=list)          # available liked videos, in YouTube's order
    titles: dict[str, str] = field(default_factory=dict)
    paused: str = ""


def liked_seen(conn: sqlite3.Connection) -> set[str]:
    """Liked Music as of the last mirror; the first time, what the last sync saw of it."""
    rows = {r[0] for r in conn.execute("SELECT video_id FROM liked_seen")}
    return rows or {r[0] for r in conn.execute("SELECT video_id FROM playlist_items WHERE playlist_id = 'LM'")}


def record_seen(conn: sqlite3.Connection, liked_now: set[str], complete: bool) -> None:
    """After a push. Complete: the snapshot becomes the likes just read. Partial (quota ran out): the union, so a removal
    that has not happened yet is never forgotten."""
    keep = liked_now if complete else liked_now | liked_seen(conn)
    conn.execute("DELETE FROM liked_seen")
    conn.executemany("INSERT INTO liked_seen (video_id) VALUES (?)", [(v,) for v in sorted(keep)])
    conn.commit()


def prune_excluded(conn: sqlite3.Connection, liked_now: set[str]) -> None:
    """An exclusion is only needed while the song is still liked; once it is not, the row goes."""
    gone = [r[0] for r in conn.execute("SELECT video_id FROM excluded") if r[0] not in liked_now]
    for v in gone:
        conn.execute("DELETE FROM excluded WHERE video_id = ?", (v,))
    conn.commit()


def read_liked(cfg: Config, conn: sqlite3.Connection,
               fetch_items: Callable = fetch.fetch_playlist_items) -> Liked:
    """Read Liked Music through the cookies, as the sync does. Never raises: a problem pauses the mirror."""
    if not check_cookies(cfg.cookies).usable:
        return Liked(paused="Liked Music can only be read with fresh cookies, and they are missing or expired")
    try:
        items = fetch_items(LIKED_URL, cfg.cookies)
    except Exception as exc:  # noqa: BLE001
        return Liked(paused=f"Liked Music could not be read: {(str(exc).splitlines() or [''])[0][:120]}")
    if not items.ids and not items.unknown and liked_seen(conn):
        return Liked(paused="Liked Music came back empty but had songs before (stale cookies?)")
    return Liked(items.ids, items.titles)


def plan_mirror(library_pid: str, entries: list[yt.Entry], liked: Liked, seen: set[str], excluded: set[str],
                dead: set[str], winner: Callable[[str], str]) -> list[Op]:
    """What makes the library list match the likes. Writes nothing."""
    if liked.paused:
        return []
    present = {e.video_id for e in entries}
    liked_effective = {winner(v) for v in liked.ids}
    seen_effective = {winner(v) for v in seen}
    ops: list[Op] = []
    added: set[str] = set()
    for v in liked.ids:                                    # a new like is added (a merged loser adds its winner)
        w = winner(v)
        if {v, w} & dead or {v, w} & excluded or w in present or w in added:
            continue
        added.add(w)
        title = liked.titles.get(v, "")
        ops.append(Op("add", library_pid, w, "", 0, "mirror", title, w, title))
    for e in entries:                                      # an unlike removes what the mirror brought in, nothing else
        if e.video_id in seen_effective and e.video_id not in liked_effective:
            ops.append(Op("delete", library_pid, e.video_id, e.item_id, e.position, "unliked", e.title))
    return ops
