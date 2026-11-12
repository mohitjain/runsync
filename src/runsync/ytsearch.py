"""Find replacements for songs YouTube no longer serves. Searching costs no API quota (yt-dlp `ytsearch`).

`candidates` ranks what the search finds: uploads that look official first, then the order YouTube gave; live
recordings, covers, karaoke, slowed/reverb and the like are pushed down unless the query asks for them.
`lost_songs` lists the unavailable tracks we still know a title for, with any copy already in the library, so the owner
never gets offered a song they have. Nothing here writes anywhere.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

import yt_dlp

from .logs import YtdlpLogger

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


def query_for(title: str) -> str:
    """The title without upload noise (\"official video clip (HD)\"), keeping the words that change the recording."""
    q = normalize(title)
    return " ".join([q] + [w for w in ("live", "remix", "remaster") if w in re.findall(r"[a-z]+", (title or "").lower()) and w not in q.split()])


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


def lost_songs(conn: sqlite3.Connection) -> tuple[list[Lost], int]:
    """(songs we still know the title of, how many unavailable tracks have no title at all). One entry per song."""
    rows = conn.execute("SELECT video_id, title, last_error FROM tracks WHERE status = 'unavailable' ORDER BY rowid").fetchall()
    seen: set[str] = set()
    out: list[Lost] = []
    nameless = 0
    for r in rows:
        if not (r["title"] or "").strip():
            nameless += 1
            continue
        key = normalize(r["title"])
        if key in seen:
            continue
        seen.add(key)
        err = r["last_error"] or ""
        reason = ("blocked by the rights holder" if "blocked" in err or "copyright" in err else
                  "private" if "private" in err else "removed" if "terminated" in err else "unavailable")
        out.append(Lost(r["video_id"], r["title"], reason, in_library(conn, r["title"])))
    return out, nameless


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
