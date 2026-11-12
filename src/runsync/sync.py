"""Sync pipeline: fetch -> diff -> trash removed -> download new. Failure never deletes."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import download, fetch
from .config import Config
from .db import log_event, update_track
from .diff import MassDeleteError, check_mass_delete, compute_diff
from .retry import PermanentError, classify, run_with_retries

logger = logging.getLogger("runsync.sync")
STALE_THRESHOLD = 3  # same transient error on this many tracks -> yt-dlp looks stale


class Reporter:
    """Display hooks; the default does nothing. The CLI supplies a pretty one."""

    def fetched(self, title: str, tracks: int, unreadable: int) -> None: ...
    def warn(self, msg: str) -> None: ...
    def reconcile(self, files: int, missing: int, adopted: int, orphans: int) -> None: ...
    def diff(self, new: int, downloaded: int, pending: int, unavailable: int, removed: int) -> None: ...
    def dry_run(self, queue_titles: list[str], queue_total: int, this_run: int, removed: int) -> None: ...
    def trashed(self, video_id: str) -> None: ...
    def download_start(self, n: int, total: int, video_id: str, title: str) -> None: ...
    def download_ok(self, video_id: str, itag: str, codec: str, bitrate: float, size_bytes: int,
                    seconds: float) -> None: ...
    def download_unavailable(self, video_id: str, reason: str) -> None: ...
    def download_failed(self, video_id: str, reason: str) -> None: ...
    def download_retry(self, video_id: str, attempt: int, delay: float, reason: str) -> None: ...
    def probe_ok(self, video_id: str, codec: str, bitrate: float, est_bytes: int, premium: bool) -> None: ...
    def probe_failed(self, video_id: str, reason: str, permanent: bool) -> None: ...
    def finish_downloads(self) -> None: ...


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
    high_quality: int = 0                # >= 200 kbps (the Premium ~258 kbps tier)
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


def reconcile(cfg: Config, conn, apply: bool, log: Callable[[str], None] = print) -> dict:
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
            log_event(conn, r["video_id"], "file_missing", {"was": r["master_path"]})
    for p in adoptable:
        logger.info("adopting untracked file %s", p.name)
        if apply:
            facts = download.file_facts(p, None)
            update_track(conn, p.stem, status="downloaded", master_path=str(p), downloaded_at=now_iso(),
                         codec=facts["probe_codec"], **facts)
            log_event(conn, p.stem, "adopted", {"file": p.name})
    for p in real_orphans:
        logger.warning("orphan file in master/ not in DB: %s", p.name)
    if apply:
        conn.commit()
    return {"files": len(files), "missing": len(missing), "adopted": len(adoptable), "orphans": len(real_orphans)}


def _probe_queue(cfg, conn, queue, cookies, rep, log, sleep) -> None:
    """Ask YouTube what each queued track offers (no download). Caches stream facts; status unchanged."""
    for n, (vid, title) in enumerate(queue, 1):
        rep.download_start(n, len(queue), vid, title)
        try:
            got = download.probe_track(vid, cookies, cfg.near_tie_ratio)
            row = conn.execute("SELECT 1 FROM tracks WHERE video_id = ?", (vid,)).fetchone()
            if row:
                update_track(conn, vid, **got["columns"])
                log_event(conn, vid, "probed", {k: got["columns"][k] for k in ("itag", "codec", "bitrate",
                                                                                 "stream_size_approx")})
            rep.probe_ok(vid, got["codec"], got["bitrate"], got["est_bytes"], got["premium"])
        except Exception as exc:  # noqa: BLE001
            permanent = classify(str(exc)) == "permanent"
            logger.warning("probe failed for %s: %s", vid, str(exc)[:200])
            rep.probe_failed(vid, str(exc), permanent)
        conn.commit()
        if n < len(queue):
            sleep(min(cfg.pause_seconds, 1.0))
    rep.finish_downloads()


def backfill_file_facts(conn, log: Callable[[str], None] = print) -> int:
    """Fill file facts (size, hash, ffprobe) for downloaded tracks recorded before those columns existed."""
    rows = conn.execute("SELECT video_id, master_path, duration_s FROM tracks "
                        "WHERE status = 'downloaded' AND sha256 IS NULL AND master_path IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        path = Path(r["master_path"])
        if not path.exists():
            log(f"backfill: {r['video_id']} file missing at {path}")
            continue
        update_track(conn, r["video_id"], **download.file_facts(path, r["duration_s"]))
        log_event(conn, r["video_id"], "backfill_file_facts")
        n += 1
    conn.commit()
    if n:
        log(f"backfilled file facts for {n} tracks")
    return n


def clean_stale_tmp(cfg: Config, log: Callable[[str], None] = print) -> int:
    """master/.tmp only ever holds partial downloads from an interrupted run: remove them."""
    tmp = cfg.master / ".tmp"
    n = 0
    for p in tmp.glob("*") if tmp.exists() else []:
        if p.is_file():
            p.unlink()
            n += 1
    if n:
        log(f"removed {n} stale partial download file(s) from master/.tmp")
    return n


def purge_trash(cfg: Config, today: datetime | None = None) -> int:
    cutoff = (today or datetime.now()) - timedelta(days=cfg.trash_days)
    n = 0
    for p in cfg.trash.glob("*"):
        if p.is_file() and datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
            p.unlink()
            n += 1
    return n


def run_sync(cfg: Config, conn, cookies: Path | None, dry_run: bool = False, force_delete: bool = False,
             log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
             rep: Reporter | None = None, probe: bool = False) -> RunResult:
    rep = rep or Reporter()
    res = RunResult(anonymous=cookies is None)
    logger.info("sync start: dry_run=%s force_delete=%s cookies=%s", dry_run, force_delete,
                "yes" if cookies else "NO (anonymous)")
    cfg.ensure_dirs()
    playlists = conn.execute("SELECT * FROM playlists WHERE selected = 1").fetchall()
    if not playlists:
        res.aborted = "no playlists selected (run `runsync playlists select`)"
        return res

    # --- fetch -----------------------------------------------------------
    desired: set[str] = set()
    unknown: set[str] = set()
    titles: dict[str, str] = {}
    fetched: dict[str, fetch.PlaylistItems] = {}
    complete = True
    for pl in playlists:
        try:
            items = fetch.fetch_playlist_items(pl["url"], cookies)
        except Exception as exc:  # noqa: BLE001
            log(f"fetch failed for '{pl['title']}': {exc}")
            rep.warn(f"fetch failed for '{pl['title']}': {exc}")
            complete = False
            continue
        prev = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id = ?", (pl["id"],)).fetchone()[0]
        if not items.ids and not items.unknown and prev > 0:
            log(f"'{pl['title']}' came back empty but had {prev} tracks; treating as failed fetch")
            rep.warn(f"'{pl['title']}' came back empty but had {prev} tracks; treated as a failed fetch")
            complete = False
            continue
        fetched[pl["id"]] = items
        desired |= set(items.ids)
        unknown |= set(items.unknown)
        titles.update(items.titles)
        log(f"fetched '{pl['title']}': {len(items.ids)} tracks, {len(items.unknown)} unreadable")
        rep.fetched(pl["title"], len(items.ids), len(items.unknown))
    if not fetched:
        res.aborted = "no playlist could be fetched; nothing changed"
        return res
    ordered: list[str] = []
    seen: set[str] = set()
    for pl in playlists:
        for v in (fetched[pl["id"]].ids if pl["id"] in fetched else []):
            if v not in seen:
                seen.add(v)
                ordered.append(v)
    if not dry_run:
        clean_stale_tmp(cfg, log)
    rec = reconcile(cfg, conn, apply=not dry_run, log=log)
    rep.reconcile(**rec)
    if not complete:
        log("some playlists failed to fetch: removals are disabled this run")
        rep.warn("some playlists failed to fetch: removals are disabled this run")

    # --- diff ------------------------------------------------------------
    active = {r["video_id"] for r in conn.execute("SELECT video_id FROM tracks WHERE status != 'trashed'")}
    diff = compute_diff(desired, unknown, active, complete)
    try:
        if not force_delete:
            check_mass_delete(len(diff.removed), len(active), cfg.mass_delete_threshold)
    except MassDeleteError as exc:
        res.aborted = str(exc)
        return res
    log(f"diff: {len(diff.new)} new, {len(diff.present)} present, {len(diff.removed)} removed")
    status = {r["video_id"]: r["status"] for r in conn.execute("SELECT video_id, status FROM tracks")}
    wanted = desired | unknown
    n_dl = sum(1 for v in wanted if status.get(v) == "downloaded")
    n_pend = sum(1 for v in wanted if status.get(v) == "pending")
    n_unav = sum(1 for v in wanted if status.get(v) == "unavailable")
    rep.diff(len(diff.new), n_dl, n_pend, n_unav, len(diff.removed))

    if dry_run:
        res.removed = sorted(diff.removed)
        pend_rows = conn.execute("SELECT video_id, title FROM tracks WHERE status = 'pending' "
                                 "ORDER BY rowid").fetchall()
        queue = [(r["video_id"], r["title"] or r["video_id"]) for r in pend_rows]
        queue += [(v, titles.get(v) or v) for v in ordered if v in diff.new]
        this_run = min(len(queue), cfg.max_downloads_per_run)
        res.queue_total = len(queue)
        res.added = [v for v, _ in queue[:this_run]]
        log(f"dry-run: queue {len(queue)} ({len(diff.new)} new), this run would take {this_run}, "
            f"trash {len(diff.removed)}. Nothing written.")
        rep.dry_run([t for _, t in queue], len(queue), this_run, len(diff.removed))
        if probe:
            _probe_queue(cfg, conn, queue[:this_run], cookies, rep, log, sleep)
        return res

    # --- persist state ---------------------------------------------------
    for pid, items in fetched.items():
        conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))
        conn.executemany("INSERT OR IGNORE INTO playlist_items VALUES (?, ?, ?)",
                         [(pid, v, i) for i, v in enumerate(items.ids)])
        conn.execute("UPDATE playlists SET last_fetch_ok = 1, last_fetched_at = ? WHERE id = ?", (now_iso(), pid))
    ts = now_iso()
    for vid in [v for v in ordered if v in diff.new]:
        cur = conn.execute("INSERT OR IGNORE INTO tracks (video_id, title, first_seen_at, last_seen_at) "
                           "VALUES (?, ?, ?, ?)", (vid, titles.get(vid), ts, ts))
        if cur.rowcount:
            log_event(conn, vid, "discovered", {"title": titles.get(vid)})
    if diff.present:
        conn.executemany("UPDATE tracks SET last_seen_at = ? WHERE video_id = ?", [(ts, v) for v in diff.present])
    conn.commit()

    backfill_file_facts(conn, log)

    # --- trash removed ---------------------------------------------------
    for vid in sorted(diff.removed):
        row = conn.execute("SELECT master_path FROM tracks WHERE video_id = ?", (vid,)).fetchone()
        if row and row["master_path"] and Path(row["master_path"]).exists():
            src = Path(row["master_path"])
            shutil.move(str(src), cfg.trash / src.name)
            update_track(conn, vid, trash_path=str(cfg.trash / src.name))
        update_track(conn, vid, status="trashed", master_path=None, trashed_at=now_iso())
        log_event(conn, vid, "trashed", {"from": row["master_path"] if row else None})
        res.removed.append(vid)
        log(f"trashed {vid}")
        rep.trashed(vid)
    conn.commit()
    purged = purge_trash(cfg)
    if purged:
        log(f"purged {purged} files older than {cfg.trash_days} days from trash")

    # --- download --------------------------------------------------------
    todo = [r["video_id"] for r in conn.execute(
        "SELECT video_id FROM tracks WHERE status = 'pending' ORDER BY rowid")][: cfg.max_downloads_per_run]
    errors: dict[str, int] = {}
    for n, vid in enumerate(todo, 1):
        log(f"[{n}/{len(todo)}] downloading {vid}")
        res.names[vid] = titles.get(vid) or _db_title(conn, vid)
        rep.download_start(n, len(todo), vid, res.names[vid])
        conn.execute("UPDATE tracks SET attempts = attempts + 1, last_attempt_at = ? WHERE video_id = ?",
                     (now_iso(), vid))
        try:
            def _retry(attempt, delay, exc, v=vid):
                logger.warning("retry %d for %s in %ss after: %s", attempt, v, delay, str(exc)[:200])
                log_event(conn, v, "retry", {"attempt": attempt, "delay": delay, "reason": str(exc)[:200]})
                rep.download_retry(v, attempt, delay, str(exc))

            got = run_with_retries(lambda v=vid: download.download_best(v, cfg.master, cookies, cfg.near_tie_ratio),
                                   sleep=sleep, on_retry=_retry)
            update_track(conn, vid, status="downloaded", last_error=None, downloaded_at=now_iso(), **got["columns"])
            cols = got["columns"]
            log_event(conn, vid, "download_ok", {k: cols[k] for k in
                      ("itag", "codec", "bitrate", "file_size_bytes", "sha256", "download_seconds", "used_cookies")})
            res.added.append(vid)
            res.bytes_downloaded += cols["file_size_bytes"] or 0
            res.seconds_downloading += cols["download_seconds"] or 0
            res.opus += str(got["codec"]).startswith("opus")
            res.tracks.append({"id": vid, "title": res.names[vid], "codec": str(got["codec"]).split(".")[0],
                               "kbps": round(got["bitrate"] or 0), "bytes": cols["file_size_bytes"] or 0,
                               "seconds": round(cols["download_seconds"] or 0),
                               "quality": "high" if (got["bitrate"] or 0) >= 200 else "standard"})
            if (got["bitrate"] or 0) >= 200:
                res.high_quality += 1
            else:
                res.standard_quality += 1
            log(f"  ok: itag {got['itag']} {got['codec']} {got['bitrate']}k")
            rep.download_ok(vid, got["itag"], got["codec"], got["bitrate"], cols["file_size_bytes"],
                            cols["download_seconds"])
        except PermanentError as exc:
            logger.warning("permanent error for %s: %s", vid, exc)
            update_track(conn, vid, status="unavailable", last_error=str(exc)[:300])
            log_event(conn, vid, "unavailable", {"reason": str(exc)[:300]})
            res.unavailable.append(vid)
            res.unavailable_reasons[vid] = str(exc)[:200]
            log(f"  unavailable: {exc}")
            rep.download_unavailable(vid, str(exc))
        except Exception as exc:
            logger.exception("download failed for %s", vid)
            msg = str(exc)[:300]
            update_track(conn, vid, last_error=msg)
            log_event(conn, vid, "download_failed", {"reason": msg})
            res.failed.append((vid, msg))
            log(f"  failed (will retry next run): {msg}")
            rep.download_failed(vid, msg)
            errors[msg] = errors.get(msg, 0) + 1
            if errors[msg] >= STALE_THRESHOLD and not res.updated_ytdlp:
                res.updated_ytdlp = True
                log("same error on several tracks: updating yt-dlp once")
                subprocess.run(["uv", "pip", "install", "-U", "yt-dlp"], check=False)
                res.aborted = "yt-dlp updated; re-run `runsync sync` to retry"
                conn.commit()
                break
        conn.commit()
        if n < len(todo):
            sleep(cfg.pause_seconds)
    rep.finish_downloads()
    logger.info("sync end: %s", res.summary())
    for vid, msg in res.failed:
        logger.info("failed %s: %s", vid, msg)
    conn.execute("INSERT INTO runs (started_at, finished_at, dry_run, summary) VALUES (?, ?, 0, ?)",
                 (now_iso(), now_iso(), json.dumps({"summary": res.summary()})))
    conn.commit()
    return res
