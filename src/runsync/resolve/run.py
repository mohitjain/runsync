"""Resolve pipeline: metadata -> cover -> loudness -> player copy. Core logic only; output goes through Reporter."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..player import ascii_name, build_player_copy, read_video_id
from ..util import now_iso
from . import covers, gain
from .artists import label_artist, text_language
from .enrich import export_enrich, import_enrich
from .model import REVIEW_BELOW, TRUST, Candidate
from .net import Http
from .overrides import import_review_fixes, load_overrides, write_review
from .parse import key
from .tiers import parser_tier, resolve_candidates, sim

log = logging.getLogger("runsync.resolve")


class Reporter:
    """Hooks for output; the CLI subclasses this. Core code never prints."""
    def start(self, total: int) -> None: ...
    def resolved(self, video_id: str, title: str, cand: Candidate, review: str | None,
                 languages: tuple[str, ...] = ()) -> None: ...
    def built(self, video_id: str, filename: str, size: int) -> None: ...
    def skipped(self, video_id: str, reason: str) -> None: ...
    def warn(self, msg: str) -> None: ...


@dataclass
class ResolveResult:
    considered: int = 0
    resolved: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)
    review: int = 0
    kept: int = 0           # existing higher-trust result kept
    built: int = 0
    held: int = 0           # in review: no player copy yet
    build_skipped: int = 0
    covers: int = 0
    lookups_cached: int = 0
    lookups_fetched: int = 0
    lookups_failed: int = 0
    imported_fixes: int = 0
    enrich_applied: int = 0      # suggestions from enrich.csv written into the overrides table
    enrich_open: int = 0         # rows still waiting for a search
    unlabelled: dict[str, int] = field(default_factory=dict)   # artists with no language yet, and their track counts
    tracks: dict[str, dict] = field(default_factory=dict)      # per track: what was resolved and what was built

    def to_dict(self) -> dict:
        """Facts for the skills to present (see `runsync resolve --json`)."""
        return {"considered": self.considered, "resolved": self.resolved, "review": self.review,
                "by_tier": dict(self.by_tier), "kept": self.kept, "built": self.built, "held": self.held,
                "build_skipped": self.build_skipped, "covers": self.covers, "unlabelled": dict(self.unlabelled),
                "imported_fixes": self.imported_fixes, "enrich_applied": self.enrich_applied,
                "enrich_open": self.enrich_open, "lookups_fetched": self.lookups_fetched,
                "lookups_cached": self.lookups_cached, "lookups_failed": self.lookups_failed,
                "tracks": list(self.tracks.values())}


def _pick(cands: list[Candidate]) -> Candidate:
    best = max(cands, key=lambda c: (c.confidence, c.trust))
    for other in cands:  # fill gaps from candidates that agree on the title
        if other is not best and sim(other.title, best.title) >= 0.85:
            best.album = best.album or other.album
            best.year = best.year or other.year
            best.extra = {**other.extra, **best.extra}
    return best


def _apply_override(best: Candidate, spec: dict) -> Candidate:
    best.tier, best.confidence = "override", 1.0
    if spec.get("artist"):
        best.artists = [a.strip() for a in str(spec["artist"]).split(",") if a.strip()]
    for f in ("title", "album"):
        if spec.get(f):
            setattr(best, f, str(spec[f]))
    if spec.get("year"):
        best.year = int(spec["year"])
    return best


def _track_languages(conn, best: Candidate, row, spec: dict | None, http, overrides: dict, dry_run: bool) -> list[str]:
    """Most specific evidence first: your override, the upload's own words, then the artists' labels."""
    if spec and spec.get("language"):
        return [str(spec["language"]).lower()]
    here = text_language(row["original_title"], row["yt_tags"], row["channel"],
                                   (row["description"] or "")[:800])
    labels = [label_artist(conn, name, http, overrides, save=not dry_run)[0]   # label every credited artist
              for name in best.artists[:3]]                                       # so their own tracks inherit it
    if here:
        return [here]
    return labels[0][:1] if labels and labels[0] else []   # the lead artist decides; collaborators do not vote


_NOT_BUILT = {"file": None, "format": None, "size": None, "cover": None, "gain_db": None}


def _track_record(vid: str, original_title: str | None, cand: Candidate, languages: list[str], review: str | None) -> dict:
    """What this run decided for one track (the skills list it). Build facts are added later by _build_all."""
    return {"id": vid, "before": original_title or "", "artist": ", ".join(cand.artists[:3]), "title": cand.title,
            "album": cand.album, "year": cand.year, "language": "/".join(languages) or None, "tier": cand.tier,
            "confidence": round(cand.confidence, 2), "review": review, **_NOT_BUILT}


def _stored_record(r, review: str | None) -> dict:
    """The same record, from a metadata row (for tracks resolved in an earlier run)."""
    return {"id": r["video_id"], "before": r["original_title"] or "", "artist": r["artist"], "title": r["title"],
            "album": r["album"], "year": r["year"], "language": r["language"], "tier": r["source_tier"],
            "confidence": r["confidence"], "review": review, **_NOT_BUILT}


def _review_reason(cand: Candidate, languages: list[str], title_ok: bool) -> str | None:
    if not cand.artists:
        return "no artist could be determined"
    if cand.confidence < REVIEW_BELOW:
        return f"low confidence ({cand.confidence:.2f}, {cand.tier})"
    if not title_ok:
        return "title has letters the player cannot show: needs you"
    if cand.tier == "parser" and not languages:   # only our own guess, nothing confirms it: could be title/artist swapped
        return f"artist not confirmed by any source: {cand.artists[0]}"
    return None


def _store(conn: sqlite3.Connection, row, cand: Candidate, languages: list[str], review: str | None) -> None:
    artists = cand.artists[:3]
    conn.execute(
        "INSERT INTO metadata (video_id, artist, artists_json, album_artist, title, album, year, language, "
        "languages_json, versions_json, original_title, confidence, source_tier, locked, needs_review, review_reason, "
        "mb_recording_id, mb_release_id, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(video_id) DO UPDATE SET artist=excluded.artist, artists_json=excluded.artists_json, "
        "album_artist=excluded.album_artist, title=excluded.title, album=excluded.album, year=excluded.year, "
        "language=excluded.language, languages_json=excluded.languages_json, versions_json=excluded.versions_json, "
        "original_title=excluded.original_title, confidence=excluded.confidence, source_tier=excluded.source_tier, "
        "locked=excluded.locked, needs_review=excluded.needs_review, review_reason=excluded.review_reason, "
        "mb_recording_id=excluded.mb_recording_id, mb_release_id=excluded.mb_release_id, "
        "resolved_at=excluded.resolved_at",
        (row["video_id"], ", ".join(artists), json.dumps(artists), artists[0] if artists else None, cand.title,
         cand.album, cand.year, languages[0] if languages else None, json.dumps(languages),
         json.dumps(cand.versions), row["original_title"], cand.confidence, cand.tier,
         1 if cand.tier == "override" else 0, 1 if review else 0, review,
         cand.extra.get("mb_recording_id"), cand.extra.get("mb_release_id"), now_iso(precise=True)))


def _needs_resolving(r, overrides: dict, labelled: set[str], online: bool, redo: bool) -> bool:
    """New tracks, and old ones something changed for: an override or artist label arrived, or a review row can be retried."""
    if r["m_vid"] is None:
        return True
    has_override = r["video_id"] in overrides["tracks"]
    lead = key((r["m_artist"] or "").split(",")[0])
    newly_labelled = r["m_lang"] is None and not r["m_review"] and lead in labelled
    unlocked = not r["m_locked"]
    return (newly_labelled or (redo and unlocked) or (has_override and unlocked) or (bool(r["m_review"]) and online)
            or (has_override and r["m_tier"] != "override"))


def _select_rows(conn: sqlite3.Connection, overrides: dict, limit: int | None, online: bool, redo: bool = False):
    rows = conn.execute(
        "SELECT t.*, m.video_id AS m_vid, m.locked AS m_locked, m.needs_review AS m_review, m.source_tier AS m_tier, "
        "m.confidence AS m_conf, m.language AS m_lang, m.artist AS m_artist FROM tracks t "
        "LEFT JOIN metadata m USING (video_id) WHERE t.status = 'downloaded' ORDER BY t.downloaded_at, t.video_id").fetchall()
    labelled = {key(n) for n, spec in overrides["artists"].items() if (spec or {}).get("languages")}
    out = [r for r in rows if _needs_resolving(r, overrides, labelled, online, redo)]
    return out[:limit] if limit else out


def run_resolve(cfg: Config, conn: sqlite3.Connection, *, dry_run: bool = False, limit: int | None = None,
                http: Http | None = None, build: bool = True, redo: bool = False, rep: Reporter | None = None,
                cover_fn=covers.fetch_cover, analyse_fn=gain.analyse) -> ResolveResult:
    rep = rep or Reporter()
    res = ResolveResult()
    if not dry_run:
        cfg.ensure_dirs()
        res.imported_fixes = import_review_fixes(cfg.review_path, conn)
        res.enrich_applied, _ = import_enrich(cfg.enrich_path, conn)
    overrides = load_overrides(conn)
    rows = _select_rows(conn, overrides, limit, http is not None, redo)
    res.considered = len(rows)
    rep.start(len(rows))

    for row in rows:
        vid = row["video_id"]
        try:
            cands = resolve_candidates(row, http)
            best = _pick(cands)
            spec = overrides["tracks"].get(vid)
            if spec:
                best = _apply_override(best, spec)
            existing_locked = bool(row["m_locked"]) and not spec
            if existing_locked or (row["m_vid"] and not spec and row["m_tier"]
                                   and _trust(row["m_tier"]) > best.trust):
                res.kept += 1
                rep.skipped(vid, "kept the existing higher-trust result")
                continue
            languages = _track_languages(conn, best, row, spec, http, overrides, dry_run)
            title_ok = ascii_name(best.title) is not None
            review = _review_reason(best, languages, title_ok)
            if best.artists and not languages:
                res.unlabelled[best.artists[0]] = res.unlabelled.get(best.artists[0], 0) + 1
            if not dry_run:
                _store(conn, row, best, languages, review)
                conn.commit()
            res.resolved += 1
            res.by_tier[best.tier] = res.by_tier.get(best.tier, 0) + 1
            res.review += bool(review)
            rep.resolved(vid, row["original_title"] or "", best, review, tuple(languages))
            res.tracks[vid] = _track_record(vid, row["original_title"], best, languages, review)
        except Exception as e:  # one bad track never stops the run
            log.exception("resolve failed for %s", vid)
            rep.warn(f"{vid}: {type(e).__name__}: {e}")

    if http:
        res.lookups_cached, res.lookups_fetched, res.lookups_failed = http.hits, http.misses, http.failures
    if build and not dry_run:
        _build_all(cfg, conn, res, rep, cover_fn, analyse_fn, online=http is not None)
    if not dry_run:
        _write_review(cfg, conn)
        res.enrich_open = export_enrich(cfg.enrich_path, conn)
    return res


def _trust(tier: str) -> int:
    return TRUST.get(tier, 0)


def _ensure_cover(conn, cfg: Config, r, res: ResolveResult, cover_fn, online: bool) -> Path | None:
    """The cover to embed: the stored one, or (online) a fresh lookup that is then stored."""
    if r["cover_path"]:
        return Path(r["cover_path"])
    if not online:
        return None
    got = cover_fn(r["video_id"], cfg.cache / "covers", r["mb_release_id"], None)
    if not got:
        return None
    conn.execute("UPDATE metadata SET cover_path = ?, cover_source = ? WHERE video_id = ?", (str(got[0]), got[1], r["video_id"]))
    res.covers += 1
    return got[0]


def _ensure_gain(conn, r, master: Path, analyse_fn) -> None:
    if r["replaygain_gain_db"] is not None:
        return
    g = analyse_fn(master)
    if g:
        conn.execute("UPDATE metadata SET loudness_lufs=?, replaygain_gain_db=?, replaygain_peak=? WHERE video_id = ?",
                     (g["loudness_lufs"], g["replaygain_gain_db"], g["replaygain_peak"], r["video_id"]))


def _build_one(cfg: Config, conn, r, res: ResolveResult, rep, cover_fn, analyse_fn, online: bool) -> None:
    vid = r["video_id"]
    master = Path(r["master_path"] or "")
    if not master.exists():
        rep.skipped(vid, "master file missing")
        return
    cover = _ensure_cover(conn, cfg, r, res, cover_fn, online)
    _ensure_gain(conn, r, master, analyse_fn)
    meta = dict(conn.execute("SELECT * FROM metadata WHERE video_id = ?", (vid,)).fetchone())
    out = build_player_copy(master, cfg.player / "m4a", vid, meta, cover, flac_dir=cfg.player / "flac")
    if "skipped" in out:
        res.build_skipped += 1
        rep.skipped(vid, out["skipped"])
        return
    old = Path(r["player_path"]) if r["player_path"] else None
    if old and old != out["path"] and old.exists() and read_video_id(old) == vid:
        old.unlink()   # renamed after a re-resolve: remove our own stale derived copy, never a master
    conn.execute("UPDATE tracks SET clean_filename=?, player_path=?, player_format=?, player_size_bytes=?, "
                 "player_built_at=? WHERE video_id = ?",
                 (out["filename"], str(out["path"]), out["format"], out["size"], now_iso(precise=True), vid))
    conn.commit()
    res.built += 1
    rep.built(vid, out["filename"], out["size"])
    now = conn.execute("SELECT cover_source, replaygain_gain_db FROM metadata WHERE video_id = ?", (vid,)).fetchone()
    res.tracks.setdefault(vid, _stored_record(r, None)).update(
        {"file": out["filename"], "format": out["format"], "size": out["size"], "cover": now["cover_source"],
         "gain_db": round(now["replaygain_gain_db"], 2) if now["replaygain_gain_db"] is not None else None})


def _build_all(cfg: Config, conn, res: ResolveResult, rep, cover_fn, analyse_fn, online: bool) -> None:
    """Player copies for every resolved track that has none yet or was re-resolved since its copy was built."""
    rows = conn.execute(
        "SELECT t.video_id, t.master_path, t.player_path, m.* FROM tracks t JOIN metadata m USING (video_id) "
        "WHERE t.status = 'downloaded' AND (t.player_path IS NULL OR m.resolved_at > COALESCE(t.player_built_at, ''))"
    ).fetchall()
    for r in rows:
        if r["needs_review"]:
            res.held += 1
            res.tracks.setdefault(r["video_id"], _stored_record(r, r["review_reason"]))
            continue
        try:
            _build_one(cfg, conn, r, res, rep, cover_fn, analyse_fn, online)
        except Exception as e:
            log.exception("player build failed for %s", r["video_id"])
            rep.warn(f"{r['video_id']}: build failed: {type(e).__name__}: {e}")


def _write_review(cfg: Config, conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT m.video_id, t.original_title AS youtube_title, t.channel, m.review_reason AS reason, m.confidence, "
        "m.source_tier AS tier, m.artist, m.title, m.album, m.language FROM metadata m JOIN tracks t USING (video_id) "
        "WHERE m.needs_review = 1 ORDER BY m.confidence, m.video_id").fetchall()
    write_review(cfg.review_path, [dict(r) for r in rows])


__all__ = ["Reporter", "ResolveResult", "key", "parser_tier", "run_resolve"]
