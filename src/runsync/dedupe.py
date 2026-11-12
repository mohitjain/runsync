"""Dedupe: one file per real recording.

Two downloaded tracks are candidates when their lead artist, title and version markers match (a live or remix
version is never merged with the studio one). The audio decides: chromaprint fingerprints are compared at every
offset, so a music video with a long intro still matches its audio-only upload. Clear matches merge, clear misses
stay apart, and the band in between is left to the owner ("same song?"), whose answer is remembered.

Removal is permanent: the loser's master and player copy are deleted. An alias row keeps the merge visible and
undoable (undo queues the loser for download again)."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .db import log_event, update_track
from .diff import MassDeleteError
from .player import read_video_id
from .resolve.parse import channel_name, is_label, key

log = logging.getLogger("runsync.dedupe")

# Live and remastered uploads count as the same song as their studio version (the owner's rule), so these two
# markers are ignored when deciding who is a candidate, and such pairs merge without a fingerprint check.
IGNORED_MARKERS = frozenset({"live", "remaster"})
SAME_AT = 0.85        # fingerprint similarity at or above this: the same recording
MAX_OFFSET = 700      # fingerprint steps (~0.124 s each): up to ~87 s of intro/outro
MIN_OVERLAP = 300     # ~37 s of audio must overlap for a similarity to count


@dataclass
class Member:
    video_id: str
    artist: str
    title: str
    channel: str
    kbps: float
    codec: str
    seconds: float
    tier: int
    youtube_title: str = ""   # the upload's own title: what tells two identical-looking tracks apart
    artists: frozenset = frozenset()   # every credited artist, as comparison keys
    markers: frozenset = frozenset()   # version markers found for this upload (live, remaster, remix ...)

    @property
    def is_live(self) -> bool:
        return "live" in self.markers

    def label(self) -> str:
        return f"{self.artist} - {self.title}"

    def rank(self) -> tuple:
        # a copy the player can use today (AAC) first, then the studio version over a live one, then source
        # quality, then bitrate
        return (self.codec == "aac", not self.is_live, self.tier, self.kbps or 0)


@dataclass
class Group:
    winner: Member
    losers: list[tuple[Member, float | None]] = field(default_factory=list)   # (member, similarity to the winner)


@dataclass
class Unsure:
    a: Member
    b: Member
    similarity: float | None


@dataclass
class Plan:
    groups: list[Group] = field(default_factory=list)
    unsure: list[Unsure] = field(default_factory=list)

    @property
    def removals(self) -> int:
        return sum(len(g.losers) for g in self.groups)


# ---- fingerprints -----------------------------------------------------------------------------------------------

def fingerprint(path: Path) -> list[int] | None:
    try:
        out = subprocess.run(["fpcalc", "-raw", "-length", "600", str(path)], capture_output=True, text=True,
                             timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        log.warning("fpcalc failed for %s", path.name)
        return None
    line = next((x for x in out.splitlines() if x.startswith("FINGERPRINT=")), "")
    try:
        return [int(v) for v in line.split("=", 1)[1].split(",") if v]
    except (IndexError, ValueError):
        return None


def similarity(a: list[int], b: list[int]) -> float:
    """Best average bit agreement over all alignments with enough overlap (0.5 is chance, 1.0 identical)."""
    best = 0.0
    for k in range(-MAX_OFFSET, MAX_OFFSET + 1, 2):
        lo, hi = max(0, -k), min(len(a), len(b) - k)
        if hi - lo < MIN_OVERLAP:
            continue
        idx = range(lo, hi, 2)
        agree = sum(32 - (a[i] ^ b[i + k]).bit_count() for i in idx)
        best = max(best, agree / (32 * len(idx)))
    return best


def _cached_fingerprint(conn: sqlite3.Connection, video_id: str, path: Path, sha: str | None) -> list[int] | None:
    row = conn.execute("SELECT sha256, ints FROM fingerprints WHERE video_id = ?", (video_id,)).fetchone()
    if row and sha and row["sha256"] == sha:
        return [int(v) for v in row["ints"].split(",") if v]
    ints = fingerprint(path)
    if ints and sha:
        conn.execute("INSERT OR REPLACE INTO fingerprints (video_id, sha256, ints) VALUES (?,?,?)",
                     (video_id, sha, ",".join(map(str, ints))))
        conn.commit()
    return ints


# ---- planning -----------------------------------------------------------------------------------------------------

def _tier(channel: str, artist: str, has_music_fields: bool, title: str) -> int:
    """4 auto-generated Topic / art track, 3 the artist's own channel, 2 a label channel, 1 other, 0 lyric video."""
    if has_music_fields or (channel or "").endswith(" - Topic"):
        return 4
    if "lyric" in (title or "").lower():
        return 0
    if key(channel_name(channel)) == key(artist.split(",")[0]):
        return 3
    return 2 if is_label(channel) else 1


def _members(conn: sqlite3.Connection) -> list[tuple[Member, str | None, str]]:
    rows = conn.execute(
        "SELECT t.video_id, t.master_path, t.sha256, t.channel, t.yt_track, t.original_title, t.probe_duration_s, "
        "t.probe_bitrate_kbps, t.probe_codec, m.artist, m.title, m.versions_json FROM tracks t "
        "JOIN metadata m USING (video_id) WHERE t.status = 'downloaded' AND m.needs_review = 0 "
        "AND m.artist IS NOT NULL AND t.master_path IS NOT NULL ORDER BY t.video_id").fetchall()
    out = []
    for r in rows:
        if not Path(r["master_path"]).exists():
            continue
        m = Member(r["video_id"], r["artist"], r["title"], r["channel"] or "", r["probe_bitrate_kbps"] or 0,
                   r["probe_codec"] or "", r["probe_duration_s"] or 0,
                   _tier(r["channel"] or "", r["artist"], bool(r["yt_track"]), r["original_title"] or ""),
                   r["original_title"] or "",
                   frozenset(key(a) for a in r["artist"].split(",") if key(a)),
                   frozenset(json.loads(r["versions_json"] or "[]")))
        out.append((m, r["sha256"], key(r["title"])))
    return out


def _related(a: Member, b: Member) -> bool:
    """Same song title is already known. Related = they share a credited artist (whoever is listed first) and any
    version marker other than live/remaster agrees: a remix or cover is never merged with the original."""
    return bool(a.artists & b.artists) and (a.markers - IGNORED_MARKERS) == (b.markers - IGNORED_MARKERS)


def _decision(conn: sqlite3.Connection, a: str, b: str) -> bool | None:
    x, y = sorted((a, b))
    row = conn.execute("SELECT same FROM dedupe_decisions WHERE a = ? AND b = ?", (x, y)).fetchone()
    return None if row is None else bool(row["same"])


def build_plan(conn: sqlite3.Connection, fp_fn: Callable[[str, Path, str | None], list[int] | None] | None = None
               ) -> Plan:
    """Read-only apart from the fingerprint cache. fp_fn(video_id, path, sha) is injectable for tests."""
    fp_fn = fp_fn or (lambda vid, path, sha: _cached_fingerprint(conn, vid, path, sha))
    paths = {r["video_id"]: Path(r["master_path"]) for r in
             conn.execute("SELECT video_id, master_path FROM tracks WHERE master_path IS NOT NULL")}
    buckets: dict[str, list[tuple[Member, str | None]]] = {}
    for m, sha, ident in _members(conn):
        buckets.setdefault(ident, []).append((m, sha))

    plan = Plan()
    for items in buckets.values():
        if len(items) < 2:
            continue
        ms = {m.video_id: m for m, _ in items}
        sha = {m.video_id: s for m, s in items}
        parent = {v: v for v in ms}

        def find(v: str, parent=parent) -> str:
            while parent[v] != v:
                parent[v] = parent[parent[v]]
                v = parent[v]
            return v

        sims: dict[tuple[str, str], float | None] = {}
        undecided: list[tuple[str, str]] = []
        ids = sorted(ms)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if not _related(ms[a], ms[b]):
                    continue
                d = _decision(conn, a, b)
                if d is None and (ms[a].markers | ms[b].markers) & IGNORED_MARKERS:
                    d = True        # live / remastered version of the same song
                if d is True:
                    parent[find(a)] = find(b)
                elif d is None:
                    undecided.append((a, b))
        for a, b in undecided:
            fa, fb = fp_fn(a, paths[a], sha[a]), fp_fn(b, paths[b], sha[b])
            s = similarity(fa, fb) if fa and fb else None
            sims[(a, b)] = s
            if s is not None and s >= SAME_AT:
                parent[find(a)] = find(b)
        comps: dict[str, list[str]] = {}
        for v in ids:
            comps.setdefault(find(v), []).append(v)
        for members in comps.values():
            if len(members) > 1:
                winner = max((ms[v] for v in members), key=Member.rank)
                g = Group(winner)
                for v in members:
                    if v != winner.video_id:
                        g.losers.append((ms[v], sims.get(tuple(sorted((v, winner.video_id))))))
                plan.groups.append(g)
        for (a, b), s in sims.items():
            if find(a) == find(b):
                continue
            if s is None or s < SAME_AT:   # same title and artist but unclear audio: the owner decides, never silently kept
                plan.unsure.append(Unsure(ms[a], ms[b], s))
    plan.groups.sort(key=lambda g: g.winner.label().casefold())
    return plan


def plan_id(plan: Plan) -> str:
    """Names exactly which files a plan would delete. `--apply` only runs with the id of the plan the owner confirmed,
    so a plan that changed in between (or an unconfirmed run) deletes nothing."""
    pairs = sorted(f"{g.winner.video_id}>{m.video_id}" for g in plan.groups for m, _ in g.losers)
    return hashlib.sha256("\n".join(pairs).encode()).hexdigest()[:12]


def plan_to_dict(plan: Plan) -> dict:
    def mem(m: Member) -> dict:
        return {"id": m.video_id, "label": m.label(), "channel": m.channel, "kbps": round(m.kbps), "codec": m.codec,
                "seconds": round(m.seconds), "youtube_title": m.youtube_title}
    return {"plan_id": plan_id(plan), "remove": [{"keep": mem(g.winner), "remove": mem(m), "similarity": s}
                       for g in plan.groups for m, s in g.losers],
            "unsure": [{"a": mem(u.a), "b": mem(u.b), "similarity": u.similarity} for u in plan.unsure]}


# ---- acting -------------------------------------------------------------------------------------------------------

def record_decision(conn: sqlite3.Connection, a: str, b: str, same: bool) -> None:
    x, y = sorted((a, b))
    for v in (x, y):
        if not conn.execute("SELECT 1 FROM tracks WHERE video_id = ?", (v,)).fetchone():
            raise ValueError(f"no track {v}")
    conn.execute("INSERT OR REPLACE INTO dedupe_decisions (a, b, same, decided_at) VALUES (?,?,?,?)",
                 (x, y, int(same), datetime.now(UTC).isoformat(timespec="microseconds")))
    conn.commit()


def apply_plan(cfg: Config, conn: sqlite3.Connection, plan: Plan, force: bool = False) -> int:
    """Delete every loser's master and player copy for good, keep an alias. Returns how many were removed."""
    total = conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'downloaded'").fetchone()[0]
    if not force and total and plan.removals / total > cfg.mass_delete_threshold:
        raise MassDeleteError(f"{plan.removals} of {total} tracks ({plan.removals / total:.0%}) would be removed; "
                              f"over the {cfg.mass_delete_threshold:.0%} guard. Re-run with --force after checking.")
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    removed = 0
    for g in plan.groups:
        for m, sim in g.losers:
            row = conn.execute("SELECT master_path, player_path FROM tracks WHERE video_id = ?",
                               (m.video_id,)).fetchone()
            if not row or conn.execute("SELECT status FROM tracks WHERE video_id = ?",
                                       (g.winner.video_id,)).fetchone()["status"] != "downloaded":
                continue   # never delete a loser unless its winner is still a real file
            if row["master_path"]:
                Path(row["master_path"]).unlink(missing_ok=True)
            if row["player_path"]:
                pp = Path(row["player_path"])
                if pp.exists() and read_video_id(pp) == m.video_id:   # only a copy we made for this very track
                    pp.unlink()
            update_track(conn, m.video_id, status="merged", master_path=None, player_path=None, clean_filename=None,
                         player_format=None, player_size_bytes=None)
            conn.execute("INSERT OR REPLACE INTO aliases (video_id, winner_id, similarity, reason, merged_at) "
                         "VALUES (?,?,?,?,?)", (m.video_id, g.winner.video_id, sim,
                                                "same recording" if sim is not None else "same song (your answer, or a live/"
                                                "remaster version)", now))
            conn.execute("DELETE FROM fingerprints WHERE video_id = ?", (m.video_id,))
            log_event(conn, m.video_id, "merged", {"into": g.winner.video_id, "similarity": sim})
            log.info("merged %s into %s (similarity %s)", m.video_id, g.winner.video_id, sim)
            removed += 1
    conn.commit()
    return removed


def undo(conn: sqlite3.Connection, video_id: str) -> str:
    """Bring a merged track back: it is queued for download again and will not be merged into that winner again."""
    row = conn.execute("SELECT winner_id FROM aliases WHERE video_id = ?", (video_id,)).fetchone()
    if not row:
        raise ValueError(f"{video_id} is not a merged duplicate")
    record_decision(conn, video_id, row["winner_id"], same=False)
    conn.execute("DELETE FROM aliases WHERE video_id = ?", (video_id,))
    update_track(conn, video_id, status="pending", attempts=0, last_error=None)
    log_event(conn, video_id, "unmerged", {"was_into": row["winner_id"]})
    conn.commit()
    return row["winner_id"]


def winner_of(conn: sqlite3.Connection, video_id: str) -> str:
    """The track to use for this video id: itself, or the one that replaced it."""
    row = conn.execute("SELECT winner_id FROM aliases WHERE video_id = ?", (video_id,)).fetchone()
    return row["winner_id"] if row else video_id
