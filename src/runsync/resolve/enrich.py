"""enrich.csv: the hand-off to a Claude Code session that searches the web for what the code could not settle.

export_enrich() lists every unsure track and every artist without a language, with the evidence we have.
A session (the runsync-enrich skill) fills the suggested_* columns, an evidence_url and a confidence.
import_enrich() applies only well-evidenced suggestions into the overrides table, tagged source=search."""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from .overrides import LANGUAGES, save_override
from .parse import key

MIN_CONFIDENCE = 0.85
COLUMNS = ["kind", "key", "youtube_title", "channel", "tags", "description", "tracks", "current_artist",
           "current_title", "current_language", "reason",
           "suggested_artist", "suggested_title", "suggested_album", "suggested_year", "suggested_language",
           "confidence", "evidence_url", "note"]
TRACK_SUGGEST = ("artist", "title", "album", "year", "language")


def _row(kind: str, k: str, r: sqlite3.Row, **extra) -> dict:
    return {"kind": kind, "key": k, "youtube_title": r["original_title"], "channel": r["channel"],
            "tags": (r["yt_tags"] or "")[:200], "description": " ".join((r["description"] or "").split())[:400],
            "current_artist": r["artist"], "current_title": r["title"], "current_language": r["language"], **extra}


def export_enrich(path: Path, conn: sqlite3.Connection) -> int:
    """Write unsure tracks and unlabelled artists. Rows the session already answered are kept as they are."""
    sel = ("SELECT t.original_title, t.channel, t.yt_tags, t.description, m.* FROM metadata m "
           "JOIN tracks t USING (video_id) ")
    rows = [_row("track", r["video_id"], r, reason=r["review_reason"], tracks=1)
            for r in conn.execute(sel + "WHERE t.status = 'downloaded' AND m.needs_review = 1 "
                                      "ORDER BY m.confidence, m.video_id")]
    seen: dict[str, dict] = {}
    for r in conn.execute(sel + "WHERE t.status = 'downloaded' AND m.needs_review = 0 AND m.language IS NULL "
                          "AND m.artist IS NOT NULL "
                          "ORDER BY m.video_id"):
        lead = r["artist"].split(",")[0].strip()
        if key(lead) in seen:
            seen[key(lead)]["tracks"] += 1
            continue
        seen[key(lead)] = _row("artist", lead, r, reason="language unknown", tracks=1)
    rows += list(seen.values())
    if not rows:
        path.unlink(missing_ok=True)
        return 0
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") or "" for c in COLUMNS})
    tmp.replace(path)
    return len(rows)


def _confidence(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def import_enrich(path: Path, conn: sqlite3.Connection, min_confidence: float = MIN_CONFIDENCE) -> tuple[int, int]:
    """Apply suggestions into the overrides table. Returns (applied, left_for_you).
    Needs an evidence URL and enough confidence; a language must be one of the three; a human entry always wins."""
    if not path.exists():
        return 0, 0
    applied = left = 0
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sug = {k: (r.get(f"suggested_{k}") or "").strip() for k in TRACK_SUGGEST}
            sug = {k: v for k, v in sug.items() if v}
            if not sug:
                continue
            evidence = (r.get("evidence_url") or "").strip()
            conf = _confidence(r.get("confidence", ""))
            if (sug.get("language") and sug["language"].lower() not in LANGUAGES) or conf < min_confidence or \
                    not re.match(r"https?://", evidence):
                left += 1
                continue
            if r["kind"] == "artist":
                if "language" not in sug:
                    left += 1
                    continue
                done = save_override(conn, "artist", r["key"], {"languages": [sug["language"].lower()]},
                                     "search", evidence, conf)
            else:
                if "year" in sug and not sug["year"].isdigit():
                    sug.pop("year")
                if "language" in sug:
                    sug["language"] = sug["language"].lower()
                done = save_override(conn, "track", r["key"], sug, "search", evidence, conf)
            applied += 1 if done else 0
    return applied, left
