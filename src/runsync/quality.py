"""Quality upgrades: find a better upload for songs stuck below the top bitrate tier.

About 1 song in 7 was uploaded without YouTube's 256 kbps tier (even official channels do that). For each such song we
look for another upload of the same recording that has it. A song that is already top quality is never searched; one
that was searched without luck is searched again only after `quality_recheck_days`.

Finding the 256 kbps tier needs the Premium session (cookies), and lookups with it are exactly what YouTube throttles,
so the search is gentle: a few songs per run, a few candidates each, pauses between lookups, and a canary (a song known to
have the top tier) before and after. A throttled session shows 129 kbps for everything, which would look like "nothing
better exists", so if the canary fails nothing is marked as searched. The search only *finds*: results wait in the
`upgrades` outbox until the owner confirms the plan that adds them to the library list; the old upload is removed
later by dedupe (audio fingerprint) and the YouTube cleanup, each with its own confirmation.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import download, ytsearch
from .config import Config
from .cookies import check_cookies
from .dedupe import TOP_KBPS
from .util import now_iso
from .ytsearch import Candidate

LENGTH_TOLERANCE = 5            # seconds: a different cut (a video with an intro) is a different file to dedupe
CANDIDATES_PER_SONG = 3         # probed per song, at most
PAUSE_SECONDS = 5.0             # between lookups with the cookies
# Words that make an upload a different recording. "lyrics" and "remastered" are allowed: the same audio, cleaned up or
# with words on screen, is a fine source (the owner treats a remaster as the same song).
REJECT_WORDS = ("live", "cover", "karaoke", "reaction", "slowed", "reverb", "8d", "nightcore", "instrumental", "tutorial",
                "lesson", "remix", "mashup", "speed up", "sped up", "version", "acoustic", "unplugged", "reprise", "mix", "edit",
                "extended", "demo", "rehearsal", "romantic", "unwind", "lofi", "lo-fi")


@dataclass
class Song:
    video_id: str
    artist: str
    title: str
    channel: str
    kbps: float
    seconds: float
    original_title: str = ""


@dataclass
class Found:
    old_id: str
    song: str
    new_id: str
    title: str
    new_channel: str
    new_kbps: float
    old_channel: str
    old_kbps: float


@dataclass
class SearchResult:
    searched: int = 0
    found: list[Found] = field(default_factory=list)
    not_found: int = 0
    reasons: dict[str, int] = field(default_factory=dict)      # result code -> songs, for this run
    aborted: str = ""             # why the run stopped early ("" = it finished)
    canary_before: float | None = None
    canary_after: float | None = None


# ---- which songs ----------------------------------------------------------------------------------------------------------

def _songs(conn: sqlite3.Connection, where: str, args: tuple = ()) -> list[Song]:
    rows = conn.execute(
        "SELECT t.video_id, t.channel, t.original_title, COALESCE(t.probe_bitrate_kbps, t.bitrate, 0) AS kbps, "
        "COALESCE(t.probe_duration_s, t.duration_s, 0) AS seconds, m.artist, m.title FROM tracks t "
        "JOIN metadata m USING (video_id) WHERE t.status = 'downloaded' AND t.master_path IS NOT NULL "
        "AND m.needs_review = 0 AND m.artist IS NOT NULL AND m.title IS NOT NULL " + where + " ORDER BY t.video_id", args).fetchall()
    return [Song(r["video_id"], r["artist"], r["title"], r["channel"] or "", r["kbps"], r["seconds"], r["original_title"] or "")
            for r in rows]


def due_songs(conn: sqlite3.Connection, recheck_days: int, now: datetime | None = None) -> list[Song]:
    """Songs below the top tier that were never searched (or searched before the reason was recorded), or last searched
    `recheck_days` ago or more."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=recheck_days)).isoformat(timespec="seconds")
    return _songs(conn, "AND COALESCE(t.probe_bitrate_kbps, t.bitrate, 0) < ? AND (t.quality_result IS NULL "
                        "OR t.quality_searched_at <= ?)", (TOP_KBPS, cutoff))


def facts(conn: sqlite3.Connection, recheck_days: int, now: datetime | None = None) -> dict:
    """Local facts for the skill: how many songs are top quality, how many are due a search, when the next one is."""
    now = now or datetime.now(UTC)
    total = conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'downloaded' AND master_path IS NOT NULL").fetchone()[0]
    eligible = _songs(conn, "")
    low = [s for s in eligible if s.kbps < TOP_KBPS]
    due = due_songs(conn, recheck_days, now)
    waiting = conn.execute(
        "SELECT MIN(quality_searched_at) FROM tracks WHERE status = 'downloaded' AND quality_searched_at IS NOT NULL "
        "AND COALESCE(probe_bitrate_kbps, bitrate, 0) < ?", (TOP_KBPS,)).fetchone()[0]
    next_due = (datetime.fromisoformat(waiting) + timedelta(days=recheck_days)).date().isoformat() if waiting and not due else None
    results = {r[0] or "unknown": r[1] for r in conn.execute(
        "SELECT quality_result, COUNT(*) FROM tracks WHERE status = 'downloaded' AND master_path IS NOT NULL "
        "AND COALESCE(probe_bitrate_kbps, bitrate, 0) < ? AND quality_searched_at IS NOT NULL GROUP BY 1", (TOP_KBPS,))}
    return {"songs": total, "top": sum(1 for s in eligible if s.kbps >= TOP_KBPS), "below_top": len(low), "due": len(due),
            "results": results,
            "waiting": len(low) - len(due), "held": total - len(eligible), "next_due": next_due,
            "queued_upgrades": conn.execute("SELECT COUNT(*) FROM upgrades").fetchone()[0]}


# ---- which uploads ----------------------------------------------------------------------------------------------------------

def _words(text: str) -> set[str]:
    return {w for w in REJECT_WORDS if re.search(rf"\b{re.escape(w)}\b", text.lower())}


def acceptable(song: Song, c: Candidate, excluded: set[str], known: set[str]) -> bool:
    """The cookie-free checks: could this upload be the same recording? (The audio fingerprint makes the final call later.)"""
    if c.video_id == song.video_id or c.video_id in excluded or c.video_id in known:
        return False
    if not c.seconds or abs(c.seconds - song.seconds) > LENGTH_TOLERANCE:
        return False
    have, want = set(ytsearch.normalize(c.title).split()), set(ytsearch.normalize(song.title).split())
    if not want or not want <= have:
        return False
    return not (_words(c.title) - _words(f"{song.original_title} {song.title}"))     # a live/remix/cover marker the song lacks


def shortlist(song: Song, candidates: list[Candidate], excluded: set[str], known: set[str]) -> list[Candidate]:
    ok = [c for c in candidates if acceptable(song, c, excluded, known)]
    ok.sort(key=lambda c: (not c.official, abs(c.seconds - song.seconds)))
    return ok[:CANDIDATES_PER_SONG]


# ---- the gentle search --------------------------------------------------------------------------------------------------------

def probe_kbps(video_id: str, cookies: Path) -> float:
    """The best audio bitrate YouTube offers this video to the Premium session (a lookup, nothing is downloaded)."""
    try:
        _, info = download._extract(video_id, cookies)
        fmt = download.select_format(info.get("formats") or [])
        return float(fmt["abr"]) if fmt else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _canary(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT video_id FROM tracks WHERE status = 'downloaded' AND master_path IS NOT NULL "
                       "AND COALESCE(probe_bitrate_kbps, bitrate, 0) >= ? ORDER BY video_id LIMIT 1", (TOP_KBPS,)).fetchone()
    return row["video_id"] if row else None


def search(conn: sqlite3.Connection, cfg: Config, *, limit: int | None = None,
           search_fn: Callable[[str, int], list[Candidate]] = ytsearch.candidates,
           probe_fn: Callable[[str, Path], float] = probe_kbps, sleep: Callable[[float], None] = time.sleep,
           now: datetime | None = None) -> SearchResult:
    """Search for better uploads of up to `limit` due songs. Writes the `upgrades` outbox and `quality_searched_at`."""
    out = SearchResult()
    if not check_cookies(cfg.cookies).usable:
        out.aborted = "your cookies are missing or expired: re-export them first (the lookups need your Premium session)"
        return out
    songs = due_songs(conn, cfg.quality_recheck_days, now)[: limit or cfg.quality_songs_per_run]
    if not songs:
        return out
    canary = _canary(conn)
    if canary:
        out.canary_before = probe_fn(canary, cfg.cookies)
        if out.canary_before < TOP_KBPS:
            out.aborted = f"your YouTube session is throttled right now (a top-quality song shows {out.canary_before:.0f} kbps): try later"
            return out
    excluded = {r[0] for r in conn.execute("SELECT video_id FROM excluded")}
    known = {r[0] for r in conn.execute("SELECT video_id FROM tracks")}
    results: list[tuple[Song, Found | None, str]] = []
    for song in songs:
        sleep(PAUSE_SECONDS)
        raw = search_fn(f"{song.artist.split(',')[0].strip()} {song.title}", 6)
        picks = shortlist(song, raw, excluded, known)
        hit = None
        for c in picks:
            sleep(PAUSE_SECONDS)
            kbps = probe_fn(c.video_id, cfg.cookies)
            if kbps >= TOP_KBPS and kbps > song.kbps:
                hit = Found(song.video_id, f"{song.artist} - {song.title}", c.video_id, c.title, c.channel, kbps,
                            song.channel, song.kbps)
                break
        code = "found" if hit else "no_results" if not raw else "no_match" if not picks else "matches_low"
        results.append((song, hit, code))
    if canary:
        sleep(PAUSE_SECONDS)
        out.canary_after = probe_fn(canary, cfg.cookies)
    healthy = not canary or (out.canary_after or 0) >= TOP_KBPS
    stamp = now_iso()
    for song, hit, code in results:
        if hit:                                   # a found upgrade is real even if the session degraded afterwards
            conn.execute("INSERT OR REPLACE INTO upgrades (new_id, old_id, title, new_channel, new_kbps, old_channel, old_kbps) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (hit.new_id, hit.old_id, hit.title, hit.new_channel, hit.new_kbps,
                                                          hit.old_channel, hit.old_kbps))
            out.found.append(hit)
        elif not healthy:
            continue                              # "nothing better" may only mean the session was throttled: search again later
        else:
            out.not_found += 1
        conn.execute("UPDATE tracks SET quality_searched_at = ?, quality_result = ? WHERE video_id = ?", (stamp, code, song.video_id))
        out.reasons[code] = out.reasons.get(code, 0) + 1
        out.searched += 1
    conn.commit()
    if not healthy:
        out.aborted = (f"your YouTube session got throttled during the search (the check song now shows "
                       f"{out.canary_after:.0f} kbps); songs without a result were not marked as searched")
    return out
