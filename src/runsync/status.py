"""Progress facts for `runsync status` (read-only, no network)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .config import Config
from .lock import running_command

ACTIVITY = {"sync": "downloading", "resolve": "tagging", "dedupe": "deduping", "unknown": "working"}


def _count(conn: sqlite3.Connection, sql: str, *args) -> int:
    return conn.execute(sql, args).fetchone()[0]


def facts(cfg: Config, conn: sqlite3.Connection) -> dict:
    n = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tracks GROUP BY status")}
    done, queued = n.get("downloaded", 0), n.get("pending", 0)
    command = running_command(cfg.library)
    running = command is not None
    now = datetime.now(UTC)
    cut = (now - timedelta(minutes=10)).isoformat()
    rate = _count(conn, "SELECT COUNT(*) FROM tracks WHERE downloaded_at > ?", cut) / 10
    tag_rate = _count(conn, "SELECT COUNT(*) FROM metadata WHERE resolved_at > ?", cut) / 10
    last = conn.execute("SELECT MAX(downloaded_at) FROM tracks").fetchone()[0]
    quiet = (now - datetime.fromisoformat(last)).total_seconds() / 60 if last else 0
    untagged = _count(conn, "SELECT COUNT(*) FROM tracks t LEFT JOIN metadata m USING (video_id) "
                            "WHERE t.status = 'downloaded' AND m.video_id IS NULL")
    activity = ACTIVITY.get(command or "", "idle")
    return {
        "state": "running" if running else ("done" if not queued else "stopped"), "activity": activity,
        "downloaded": done, "queued": queued, "total": done + queued, "unavailable": n.get("unavailable", 0),
        "untagged": untagged, "tagged": done - untagged,
        "need_you": _count(conn, "SELECT COUNT(*) FROM metadata m JOIN tracks t USING (video_id) "
                                 "WHERE t.status = 'downloaded' AND m.needs_review = 1"),
        "player_copies": _count(conn, "SELECT COUNT(*) FROM tracks WHERE player_path IS NOT NULL"),
        "copies_target": _count(conn, "SELECT COUNT(*) FROM tracks t JOIN metadata m USING (video_id) "
                                      "WHERE t.status = 'downloaded' AND m.needs_review = 0"),
        "per_min": round(rate, 1), "eta_min": round(queued / rate) if running and rate > 0 else None,
        "tag_per_min": round(tag_rate, 1),
        "tag_eta_min": round(untagged / tag_rate) if activity == "tagging" and tag_rate > 0 else None,
        "minutes_since_last_download": round(quiet, 1)}
