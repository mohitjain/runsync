"""Sync pipeline: fetch -> diff -> trash removed -> download new. Failure never deletes."""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import download, fetch, snapshot
from . import youtube as yt
from .config import Config
from .db import update_track
from .diff import Diff, MassDeleteError, check_mass_delete, compute_diff
from .retry import PermanentError, run_with_retries
from .util import now_iso

logger = logging.getLogger("runsync.sync")
STALE_THRESHOLD = 3     # same transient error on this many tracks -> yt-dlp looks stale
HIGH_KBPS = 200         # at or above this is the Premium ~258 kbps tier; below it the ~129 kbps uploads


class Reporter:
    """Progress hooks; the default does nothing. The CLI supplies one that prints."""

    def warn(self, msg: str) -> None: ...
    def download_start(self, n: int, total: int, video_id: str, title: str) -> None: ...
    def download_ok(self, video_id: str, codec: str, bitrate: float, size_bytes: int, seconds: float) -> None: ...
    def download_unavailable(self, video_id: str, reason: str) -> None: ...
    def download_failed(self, video_id: str, reason: str) -> None: ...
    def download_retry(self, video_id: str, attempt: int, delay: float, reason: str) -> None: ...


@dataclass
class RunResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    aborted: str | None = None
    anonymous: bool = False
    updated_ytdlp: bool = False
    queue_total: int = 0                 # dry run: how many tracks are waiting
    bytes_downloaded: int = 0
    seconds_downloading: float = 0.0
    high_quality: int = 0                # >= HIGH_KBPS (the Premium ~258 kbps tier)
    standard_quality: int = 0            # below that (~129 kbps uploads)
    opus: int = 0                        # masters that are not AAC (they get a FLAC player copy)
    tracks: list[dict] = field(default_factory=list)            # every download of this run, for the skills to list
    names: dict[str, str] = field(default_factory=dict)         # video id -> title, for every track attempted
    unavailable_reasons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Facts for the skills to present (see `runsync sync --json`)."""
        return {"added": len(self.added), "removed": len(self.removed), "unavailable": len(self.unavailable),
                "failed": [{"id": v, "title": self.names.get(v, v), "reason": r} for v, r in self.failed],
                "unavailable_tracks": [{"id": v, "title": self.names.get(v, v), "reason": self.unavailable_reasons.get(v, "")}
                                       for v in self.unavailable],
                "tracks": self.tracks, "aborted": self.aborted,
                "anonymous": self.anonymous, "queue_total": self.queue_total,
                "bytes": self.bytes_downloaded, "seconds": round(self.seconds_downloading),
                "high_quality": self.high_quality, "standard_quality": self.standard_quality, "opus": self.opus}

    def summary(self) -> str:
        s = (f"+{len(self.added)} added, -{len(self.removed)} removed, {len(self.failed)} failed, "
             f"{len(self.unavailable)} unavailable")
        if self.anonymous:
            s += " [ANONYMOUS: reduced quality, no Premium cookies]"
        if self.aborted:
            s += f" [ABORTED: {self.aborted}]"
        return s


def _db_title(conn, vid: str) -> str:
    row = conn.execute("SELECT title FROM tracks WHERE video_id = ?", (vid,)).fetchone()
    return (row["title"] if row and row["title"] else vid)


def reconcile(cfg: Config, conn, apply: bool) -> dict:
    """Compare DB with master/ on disk. Missing file -> back to pending; orphan file named after a pending
    video id -> adopted. Never deletes anything. apply=False only reports."""
    files = [p for p in cfg.master.iterdir() if p.is_file() and not p.name.startswith(".")] \
        if cfg.master.exists() else []
    rows = conn.execute("SELECT video_id, master_path FROM tracks WHERE status = 'downloaded'").fetchall()
    missing = [r for r in rows if not r["master_path"] or not Path(r["master_path"]).exists()]
    known = {Path(r["master_path"]).name for r in rows if r["master_path"]}
    orphans = [p for p in files if p.name not in known]
    pending_ids = {r["video_id"] for r in conn.execute("SELECT video_id FROM tracks WHERE status = 'pending'")}
    adoptable = [p for p in orphans if p.stem in pending_ids and download.probe_ok(p)]
    real_orphans = [p for p in orphans if p not in adoptable]
    for r in missing:
        logger.warning("file missing for %s (%s)%s", r["video_id"], r["master_path"],
                       "" if apply else " [dry-run]")
        if apply:
            update_track(conn, r["video_id"], status="pending", master_path=None)
    for p in adoptable:
        logger.info("adopting untracked file %s", p.name)
        if apply:
            facts = download.file_facts(p, None)
            update_track(conn, p.stem, status="downloaded", master_path=str(p), downloaded_at=now_iso(),
                         codec=facts["probe_codec"], **facts)
    for p in real_orphans:
        logger.warning("orphan file in master/ not in DB: %s", p.name)
    if apply:
        conn.commit()
    return {"files": len(files), "missing": len(missing), "adopted": len(adoptable), "orphans": len(real_orphans)}


def backfill_file_facts(conn) -> int:
    """Fill file facts (size, hash, ffprobe) for downloaded tracks recorded before those columns existed."""
    rows = conn.execute("SELECT video_id, master_path, duration_s FROM tracks "
                        "WHERE status = 'downloaded' AND sha256 IS NULL AND master_path IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        path = Path(r["master_path"])
        if not path.exists():
            logger.warning("backfill: %s file missing at %s", r["video_id"], path)
            continue
        update_track(conn, r["video_id"], **download.file_facts(path, r["duration_s"]))
        n += 1
    conn.commit()
    if n:
        logger.info("backfilled file facts for %d tracks", n)
    return n


def clean_stale_tmp(cfg: Config) -> int:
    """master/.tmp only ever holds partial downloads from an interrupted run: remove them."""
    tmp = cfg.master / ".tmp"
    n = 0
    for p in tmp.glob("*") if tmp.exists() else []:
        if p.is_file():
            p.unlink()
            n += 1
    if n:
        logger.info("removed %d stale partial download file(s) from master/.tmp", n)
    return n


def purge_trash(cfg: Config, today: datetime | None = None) -> int:
    cutoff = (today or datetime.now()) - timedelta(days=cfg.trash_days)
    n = 0
    for p in cfg.trash.glob("*"):
        if p.is_file() and datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
            p.unlink()
            n += 1
    return n


@dataclass
class Fetched:
    """What the selected playlists look like on YouTube right now."""
    desired: set[str] = field(default_factory=set)               # available video ids
    unknown: set[str] = field(default_factory=set)               # private/deleted: present but unreadable
    titles: dict[str, str] = field(default_factory=dict)
    by_playlist: dict[str, fetch.PlaylistItems] = field(default_factory=dict)
    ordered: list[str] = field(default_factory=list)             # distinct ids in playlist order
    complete: bool = True                                        # every selected playlist was read


def _fetch_all(conn, playlists, cookies: Path | None, api, rep: Reporter) -> Fetched:
    """Read every selected playlist. A failed or suspiciously empty read marks the picture incomplete (nothing is
    removed on an incomplete picture): failure is not removal."""
    got = Fetched()
    for pl in playlists:
        try:
            items = fetch.fetch_playlist_items(pl["url"], cookies, api)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch failed for '%s': %s", pl["title"], exc)
            rep.warn(f"fetch failed for '{pl['title']}': {exc}")
            got.complete = False
            continue
        prev = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id = ?", (pl["id"],)).fetchone()[0]
        if not items.ids and not items.unknown and prev > 0:
            logger.warning("'%s' came back empty but had %d tracks; treated as a failed fetch", pl["title"], prev)
            rep.warn(f"'{pl['title']}' came back empty but had {prev} tracks; treated as a failed fetch")
            got.complete = False
            continue
        got.by_playlist[pl["id"]] = items
        got.desired |= set(items.ids)
        got.unknown |= set(items.unknown)
        got.titles.update(items.titles)
        logger.info("fetched '%s': %d tracks, %d unreadable", pl["title"], len(items.ids), len(items.unknown))
    got.ordered = list(dict.fromkeys(v for pl in playlists if pl["id"] in got.by_playlist
                                     for v in got.by_playlist[pl["id"]].ids))
    return got


def _diff(cfg: Config, conn, got: Fetched, force_delete: bool) -> Diff:
    """What is new, present and removed. Raises MassDeleteError when too much would go."""
    active = {r["video_id"] for r in conn.execute("SELECT video_id FROM tracks WHERE status != 'trashed'")}
    diff = compute_diff(got.desired, got.unknown, active, got.complete)
    # A merged or unavailable track has no file. It leaves the database (see run_sync) when YouTube no longer lists it;
    # that is not a removal and never counts towards the mass-delete guard.
    ghosts = {r["video_id"] for r in conn.execute(
        "SELECT video_id FROM tracks WHERE status IN ('merged', 'unavailable') AND master_path IS NULL")}
    diff.removed -= ghosts
    if not force_delete:
        check_mass_delete(len(diff.removed), len(active), cfg.mass_delete_threshold)
    logger.info("diff: %d new, %d present, %d removed", len(diff.new), len(diff.present), len(diff.removed))
    return diff


def _persist_snapshot(conn, got: Fetched, diff: Diff) -> None:
    """The database now mirrors the playlists as just read."""
    for pid, items in got.by_playlist.items():
        conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))
        conn.executemany("INSERT OR IGNORE INTO playlist_items VALUES (?, ?, ?)",
                         [(pid, v, i) for i, v in enumerate(items.ids)])
        conn.execute("UPDATE playlists SET last_fetch_ok = 1, last_fetched_at = ? WHERE id = ?", (now_iso(), pid))
    ts = now_iso()
    for vid in (v for v in got.ordered if v in diff.new):
        conn.execute("INSERT OR IGNORE INTO tracks (video_id, title, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                     (vid, got.titles.get(vid), ts, ts))
    conn.executemany("UPDATE tracks SET last_seen_at = ? WHERE video_id = ?", [(ts, v) for v in diff.present])
    conn.commit()


def _trash_removed(cfg: Config, conn, removed: set[str], res: RunResult) -> None:
    """Tracks that left every selected playlist: the file goes to .trash/ (kept trash_days), the row is marked trashed."""
    for vid in sorted(removed):
        row = conn.execute("SELECT master_path FROM tracks WHERE video_id = ?", (vid,)).fetchone()
        if row and row["master_path"] and Path(row["master_path"]).exists():
            src = Path(row["master_path"])
            shutil.move(str(src), cfg.trash / src.name)
            update_track(conn, vid, trash_path=str(cfg.trash / src.name))
        update_track(conn, vid, status="trashed", master_path=None, trashed_at=now_iso())
        res.removed.append(vid)
        logger.info("trashed %s", vid)
    conn.commit()
    purged = purge_trash(cfg)
    if purged:
        logger.info("purged %d files older than %d days from trash", purged, cfg.trash_days)


def _download_one(cfg: Config, conn, vid: str, cookies: Path | None, res: RunResult, rep: Reporter,
                  sleep: Callable[[float], None]) -> None:
    """Download one track with retries and record the outcome. Raises only for unexpected failures (the caller counts them)."""
    def on_retry(attempt: int, delay: float, exc: Exception) -> None:
        logger.warning("retry %d for %s in %ss after: %s", attempt, vid, delay, str(exc)[:200])
        rep.download_retry(vid, attempt, delay, str(exc))

    got = run_with_retries(lambda: download.download_best(vid, cfg.master, cookies, cfg.near_tie_ratio),
                           sleep=sleep, on_retry=on_retry)
    cols = got["columns"]
    update_track(conn, vid, status="downloaded", last_error=None, downloaded_at=now_iso(), **cols)
    kbps = got["bitrate"] or 0
    res.added.append(vid)
    res.bytes_downloaded += cols["file_size_bytes"] or 0
    res.seconds_downloading += cols["download_seconds"] or 0
    res.opus += str(got["codec"]).startswith("opus")
    res.high_quality += kbps >= HIGH_KBPS
    res.standard_quality += kbps < HIGH_KBPS
    res.tracks.append({"id": vid, "title": res.names[vid], "codec": str(got["codec"]).split(".")[0], "kbps": round(kbps),
                       "bytes": cols["file_size_bytes"] or 0, "seconds": round(cols["download_seconds"] or 0),
                       "quality": "high" if kbps >= HIGH_KBPS else "standard"})
    logger.info("  ok: itag %s %s %sk", got["itag"], got["codec"], got["bitrate"])
    rep.download_ok(vid, got["codec"], got["bitrate"], cols["file_size_bytes"], cols["download_seconds"])


def _download_queue(cfg: Config, conn, cookies: Path | None, titles: dict[str, str], res: RunResult, rep: Reporter,
                    sleep: Callable[[float], None]) -> None:
    todo = [r["video_id"] for r in conn.execute(
        "SELECT video_id FROM tracks WHERE status = 'pending' ORDER BY rowid")][: cfg.max_downloads_per_run]
    errors: dict[str, int] = {}
    for n, vid in enumerate(todo, 1):
        logger.info("[%d/%d] downloading %s", n, len(todo), vid)
        res.names[vid] = titles.get(vid) or _db_title(conn, vid)
        rep.download_start(n, len(todo), vid, res.names[vid])
        conn.execute("UPDATE tracks SET attempts = attempts + 1, last_attempt_at = ? WHERE video_id = ?", (now_iso(), vid))
        try:
            _download_one(cfg, conn, vid, cookies, res, rep, sleep)
        except PermanentError as exc:
            logger.warning("unavailable %s: %s", vid, exc)
            update_track(conn, vid, status="unavailable", last_error=str(exc)[:300])
            res.unavailable.append(vid)
            res.unavailable_reasons[vid] = str(exc)[:200]
            rep.download_unavailable(vid, str(exc))
        except Exception as exc:
            logger.exception("download failed for %s (will retry next run)", vid)
            msg = str(exc)[:300]
            update_track(conn, vid, last_error=msg)
            res.failed.append((vid, msg))
            rep.download_failed(vid, msg)
            errors[msg] = errors.get(msg, 0) + 1
            if errors[msg] >= STALE_THRESHOLD and not res.updated_ytdlp:
                res.updated_ytdlp = True
                logger.warning("same error on several tracks: updating yt-dlp once")
                subprocess.run(["uv", "pip", "install", "-U", "yt-dlp"], check=False)
                res.aborted = "yt-dlp updated; re-run `runsync sync` to retry"
                conn.commit()
                break
        conn.commit()
        if n < len(todo):
            sleep(cfg.pause_seconds)


def run_sync(cfg: Config, conn, cookies: Path | None, dry_run: bool = False, force_delete: bool = False,
             sleep: Callable[[float], None] = time.sleep, rep: Reporter | None = None, api: yt.Api | None = None
             ) -> RunResult:
    rep = rep or Reporter()
    res = RunResult(anonymous=cookies is None)
    logger.info("sync start: dry_run=%s force_delete=%s cookies=%s", dry_run, force_delete,
                "yes" if cookies else "NO (anonymous)")
    cfg.ensure_dirs()
    playlists = conn.execute("SELECT * FROM playlists WHERE selected = 1").fetchall()
    if not playlists:
        res.aborted = "no playlists selected (choose some with `runsync playlists --sync`)"
        return res

    got = _fetch_all(conn, playlists, cookies, api, rep)
    if not got.by_playlist:
        res.aborted = "no playlist could be fetched; nothing changed"
        return res
    if not dry_run:
        clean_stale_tmp(cfg)
    reconcile(cfg, conn, apply=not dry_run)
    if not got.complete:
        logger.warning("some playlists failed to fetch: removals are disabled this run")
        rep.warn("some playlists failed to fetch: removals are disabled this run")
    try:
        diff = _diff(cfg, conn, got, force_delete)
    except MassDeleteError as exc:
        res.aborted = str(exc)
        return res

    if dry_run:     # report what would happen and write nothing
        queue = [r["video_id"] for r in conn.execute("SELECT video_id FROM tracks WHERE status = 'pending' ORDER BY rowid")]
        queue += [v for v in got.ordered if v in diff.new]
        res.removed = sorted(diff.removed)
        res.queue_total = len(queue)
        res.added = queue[:cfg.max_downloads_per_run]
        logger.info("dry-run: queue %d (%d new), this run would take %d, trash %d. Nothing written.",
                    len(queue), len(diff.new), len(res.added), len(diff.removed))
        return res

    _persist_snapshot(conn, got, diff)
    backfill_file_facts(conn)
    _trash_removed(cfg, conn, diff.removed, res)
    # keep the database a snapshot: forget what YouTube no longer lists
    snapshot.purge_trashed(conn)
    if got.complete:    # every selected playlist was read: YouTube is confirmed, so dead rows and the undo note can go
        forgotten = snapshot.purge_gone(conn, present=got.desired | got.unknown)
        if forgotten:
            logger.info("forgot %d merged or unavailable tracks that left your playlists", forgotten)
        snapshot.forget_pushed(conn, yt.quota_day())

    _download_queue(cfg, conn, cookies, got.titles, res, rep, sleep)
    logger.info("sync end: %s", res.summary())
    for vid, msg in res.failed:
        logger.info("failed %s: %s", vid, msg)
    conn.commit()
    return res
