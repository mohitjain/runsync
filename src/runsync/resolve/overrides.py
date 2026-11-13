"""Corrections that always win (stored in state.db) and review.csv (what needs a human)."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from ..util import now_iso

LANGUAGES = ("hindi", "english", "punjabi")   # the only languages this library has
TRACK_FIELDS = ("artist", "title", "album", "year", "language")
REVIEW_COLUMNS = ["video_id", "youtube_title", "channel", "reason", "confidence", "tier", "artist", "title", "album",
                  "language", "fix_artist", "fix_title", "fix_album", "fix_language"]


def check_language(where: str, value) -> None:
    for lang in ([value] if isinstance(value, str) else list(value or [])):
        if str(lang).strip().lower() not in LANGUAGES:
            raise ValueError(f"{where}: language {lang!r} is not one of {list(LANGUAGES)}")


def load_overrides(conn: sqlite3.Connection) -> dict:
    """{"tracks": {video_id: spec}, "artists": {name: spec}}; spec also carries source/evidence/confidence."""
    out: dict = {"tracks": {}, "artists": {}}
    for r in conn.execute("SELECT kind, key, fields_json, source, evidence, confidence FROM overrides"):
        spec = json.loads(r["fields_json"])
        spec.update(source=r["source"], evidence=r["evidence"], confidence=r["confidence"])
        out["tracks" if r["kind"] == "track" else "artists"][r["key"]] = spec
    return out


def save_override(conn: sqlite3.Connection, kind: str, name: str, fields: dict, source: str = "human",
                  evidence: str | None = None, confidence: float | None = None) -> bool:
    """Store a correction. Returns True if something changed. A human entry is never replaced by a search answer.
    A human edit merges into the existing fields; a search answer replaces its earlier search answer."""
    where = f"{kind} {name}"
    if kind == "artist":
        check_language(where, fields.get("languages"))
    elif fields.get("language"):
        check_language(where, fields["language"])
    row = conn.execute("SELECT fields_json, source FROM overrides WHERE kind = ? AND key = ?", (kind, name)).fetchone()
    if row and row["source"] == "human" and source != "human":
        return False
    merged = {**json.loads(row["fields_json"]), **fields} if row and source == "human" else dict(fields)
    if row and json.loads(row["fields_json"]) == merged and row["source"] == source:
        return False
    conn.execute(
        "INSERT INTO overrides (kind, key, fields_json, source, evidence, confidence, updated_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET fields_json=excluded.fields_json, "
        "source=excluded.source, evidence=excluded.evidence, confidence=excluded.confidence, "
        "updated_at=excluded.updated_at",
        (kind, name, json.dumps(merged, ensure_ascii=False), source, evidence, confidence,
         now_iso(precise=True)))
    conn.commit()
    return True


def write_review(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in REVIEW_COLUMNS})
    tmp.replace(path)


def import_review_fixes(review: Path, conn: sqlite3.Connection) -> int:
    """Copy filled fix_* cells from review.csv into the overrides table. Returns the number of tracks updated."""
    if not review.exists():
        return 0
    n = 0
    with review.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            fixes = {k: (r.get(f"fix_{k}") or "").strip() for k in TRACK_FIELDS if k != "year"}
            fixes = {k: v for k, v in fixes.items() if v}
            if "language" in fixes:
                fixes["language"] = fixes["language"].lower()
            if fixes and r.get("video_id") and save_override(conn, "track", r["video_id"], fixes, "human"):
                n += 1
    return n
