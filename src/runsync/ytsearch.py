"""Find replacements for songs YouTube no longer serves. Searching costs no API quota (yt-dlp `ytsearch`).

`candidates` ranks what the search finds: uploads that look official first, then the order YouTube gave; live
recordings, covers, karaoke, slowed/reverb and the like are pushed down unless the query asks for them.
`lost_songs` lists the unavailable tracks we still know a title for, with any copy already in the library, so the owner
never gets offered a song they have. Nothing here writes anywhere.
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

import yt_dlp

from .logs import YtdlpLogger
from .util import now_iso

PENALTY_WORDS = ("live", "cover", "karaoke", "reaction", "slowed", "reverb", "8d", "nightcore", "instrumental", "lyrics",
                 "tutorial", "lesson", "remix", "mashup", "speed up", "sped up")
OFFICIAL_HINTS = ("official", "vevo", "- topic", " records", "t-series", "sony music", "zee music", "universal music",
                  "warner music", "saregama", "tips official")
NOISE = re.compile(r"\(.*?\)|\[.*?\]|\bofficial\b|\bvideo\b|\bclip\b|\bhd\b|\blyric(?:al|s)?\b|\baudio\b|\bfull\b|\bsong\b|\*", re.IGNORECASE)


def normalize(title: str) -> str:
    t = NOISE.sub(" ", (title or "").lower()).replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9\u0900-\u097f\u0a00-\u0a7f ]+", " ", t).split())


def tokens(title: str) -> set[str]:
    return set(normalize(title).split())


KEEP_PAREN = re.compile(r"\(([^)]*(?:remix|mix|live|remaster|version|acoustic|edit)[^)]*)\)", re.IGNORECASE)


def query_for(title: str) -> str:
    """The title without upload noise ("official video clip (HD)"), keeping what changes the recording: a bracket that says
    remix, live, remaster, version, acoustic or edit stays (with its year)."""
    kept = [normalize(m) for m in KEEP_PAREN.findall(title or "")]
    q = normalize(title)
    extra = [w for w in ("live", "remix", "remaster") if w in re.findall(r"[a-z]+", (title or "").lower())]
    words = q.split()
    for k in kept:
        words += [w for w in k.split() if w not in words]
    words += [w for w in extra if w not in words]
    return " ".join(words)


@dataclass
class Candidate:
    video_id: str
    title: str
    channel: str
    seconds: int
    official: bool
    score: float = 0.0


def ytdlp_search(query: str, n: int = 6) -> list[dict]:
    opts = {"quiet": True, "extract_flat": True, "skip_download": True, "logger": YtdlpLogger()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return list((info or {}).get("entries") or [])


def candidates(query: str, n: int = 5, search: Callable[[str, int], list[dict]] = ytdlp_search) -> list[Candidate]:
    asked = query.lower()
    out: list[Candidate] = []
    for rank, e in enumerate(search(query, n + 3)):
        vid = (e or {}).get("id")
        if not vid or len(vid) != 11 or e.get("ie_key") not in (None, "Youtube"):   # videos only, not channels or playlists
            continue
        title, channel = e.get("title") or "", e.get("channel") or e.get("uploader") or ""
        low = f"{title} {channel}".lower()
        chan = channel.lower().strip()
        official = any(h in low for h in OFFICIAL_HINTS) or (len(chan) >= 3 and chan in asked)   # the artist's own channel
        score = -rank + (3 if official else 0) - sum(4 for w in PENALTY_WORDS if re.search(rf"\b{w}\b", low) and w not in asked)
        out.append(Candidate(vid, title, channel, int(e.get("duration") or 0), official, score))
    out.sort(key=lambda c: -c.score)
    return out[:n]


@dataclass
class Lost:
    video_id: str
    title: str
    reason: str
    in_library: dict | None = None
    candidates: list[Candidate] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)   # every lost video id of this song (uploads of one title)


def in_library(conn: sqlite3.Connection, title: str) -> dict | None:
    """A downloaded track that is this song: same words after removing the noise, or one title holds all of the other's
    words (at least two). Shown to the owner, so a wrong guess is visible, never silent."""
    want = tokens(title)
    if not want:
        return None
    for r in conn.execute("SELECT video_id, title FROM tracks WHERE status = 'downloaded' AND title IS NOT NULL"):
        have = tokens(r["title"])
        if normalize(r["title"]) == normalize(title) or (min(len(have), len(want)) >= 2 and (have <= want or want <= have)):
            return {"video_id": r["video_id"], "title": r["title"]}
    return None


def _lost_groups(conn: sqlite3.Connection) -> tuple[list[Lost], int]:
    """Every song YouTube no longer serves that we still know a title for (titles a spelling apart are one song with
    several lost ids), and how many unavailable tracks have no title at all."""
    rows = conn.execute("SELECT video_id, title, last_error FROM tracks WHERE status = 'unavailable' ORDER BY rowid").fetchall()
    groups: list[Lost] = []
    keys: list[str] = []
    nameless = 0
    for r in rows:
        if not (r["title"] or "").strip():
            nameless += 1
            continue
        key = normalize(r["title"])
        hit = next((i for i, k in enumerate(keys) if k == key or difflib.SequenceMatcher(None, k, key).ratio() >= 0.9), None)
        if hit is not None:
            groups[hit].ids.append(r["video_id"])
            continue
        err = r["last_error"] or ""
        reason = ("blocked by the rights holder" if "blocked" in err or "copyright" in err else
                  "private" if "private" in err else "removed" if "terminated" in err else "unavailable")
        groups.append(Lost(r["video_id"], r["title"], reason, in_library(conn, r["title"]), ids=[r["video_id"]]))
        keys.append(key)
    return groups, nameless


def lost_songs(conn: sqlite3.Connection) -> tuple[list[Lost], int]:
    """(lost songs the owner has not decided about yet, unavailable tracks with no title)."""
    decided = {r["lost_id"] for r in conn.execute("SELECT lost_id FROM replacement_picks")}
    groups, nameless = _lost_groups(conn)
    return [g for g in groups if not any(i in decided for i in g.ids)], nameless


def lost_ids_of(conn: sqlite3.Connection, lost_id: str) -> list[str]:
    """The lost video ids that are the same song as this one (so one answer covers them all)."""
    for g in _lost_groups(conn)[0]:
        if lost_id in g.ids:
            return g.ids
    return [lost_id]


def record_pick(conn: sqlite3.Connection, lost_id: str, pick_id: str, title: str = "") -> list[str]:
    ids = lost_ids_of(conn, lost_id)
    for i in ids:
        conn.execute("INSERT OR REPLACE INTO replacement_picks (lost_id, pick_id, title, status, decided_at) "
                     "VALUES (?, ?, ?, 'picked', ?)", (i, pick_id, title, now_iso()))
    conn.commit()
    return ids


def record_skip(conn: sqlite3.Connection, lost_id: str) -> list[str]:
    ids = lost_ids_of(conn, lost_id)
    for i in ids:
        conn.execute("INSERT OR REPLACE INTO replacement_picks (lost_id, pick_id, title, status, decided_at) "
                     "VALUES (?, NULL, NULL, 'skipped', ?)", (i, now_iso()))
    conn.commit()
    return ids


def pick_counts(conn: sqlite3.Connection) -> dict:
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(DISTINCT COALESCE(pick_id, lost_id)) AS n FROM replacement_picks GROUP BY status")}


def video_titles(ids: list[str], lookup: Callable[[str], str] | None = None) -> dict[str, str]:
    """Best-effort titles for a few video ids (no quota). A failure just leaves the title empty."""
    def default(vid: str) -> str:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "logger": YtdlpLogger()}) as ydl:
            return (ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False) or {}).get("title") or ""
    out = {}
    for v in ids:
        try:
            out[v] = (lookup or default)(v)
        except Exception:  # noqa: BLE001
            out[v] = ""
    return out


def as_dict(c: Candidate) -> dict:
    return {"video_id": c.video_id, "title": c.title, "channel": c.channel, "seconds": c.seconds, "official": c.official}
