"""Resolver tiers: YouTube fields, title parser, MusicBrainz, iTunes. Each returns a Candidate with a confidence."""
from __future__ import annotations

import re
from collections.abc import Mapping
from difflib import SequenceMatcher

from .model import CONFIDENT, Candidate
from .net import Http
from .parse import (
    channel_name,
    clean_title,
    is_label,
    key,
    normalize,
    parse,
    split_names,
    tidy_case,
    versions_in,
)

MB_URL = "https://musicbrainz.org/ws/2/recording"
ITUNES_URL = "https://itunes.apple.com/search"
MAX_ARTISTS = 3


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, key(a), key(b)).ratio() if a and b else 0.0


def _strip_movie(title: str) -> tuple[str, str | None]:
    m = re.search(r"\s*[\(\[]\s*from\s+[\"']?([^\"'()\[\]]+?)[\"']?\s*[\)\]]", title, re.IGNORECASE)
    return (re.sub(r"\s+", " ", title[: m.start()] + title[m.end():]).strip(), m.group(1)) if m else (title, None)


def youtube_tier(row: Mapping) -> Candidate | None:
    """YouTube-supplied music fields (auto-generated "Topic" / art-track uploads). Best free source."""
    if not row["yt_track"]:
        return None
    names = split_names(row["yt_artist"] or "", strict=True)
    channel = row["channel"] or ""
    lead = channel_name(channel) if channel.endswith("- Topic") else None
    if len(names) > MAX_ARTISTS:  # a pile of credits (composers, lyricists...): keep only the lead
        names = [lead or names[0]]
    elif lead and key(lead) in {key(n) for n in names}:
        names = [lead] + [n for n in names if key(n) != key(lead)]
    if not names and channel and not is_label(channel):
        names = [channel_name(channel)]
    title_src, movie = _strip_movie(row["yt_track"])
    title, featured, year = clean_title(title_src)
    artists = (names + [f for f in featured if key(f) not in {key(n) for n in names}])[:MAX_ARTISTS]
    return Candidate(tier="youtube", title=title or row["yt_track"], artists=artists,
                     album=tidy_case(movie if "(from" in (row["yt_album"] or "").lower() else (row["yt_album"] or movie or "")) or None,
                     year=row["yt_release_year"] or year,
                     confidence=0.95 if artists else 0.6, versions=versions_in(row["yt_track"]),
                     note="YouTube music metadata")


def parser_tier(row: Mapping) -> Candidate:
    return parse(row["original_title"] or row["title"] or "", row["channel"])


def _artist_match(wanted: list[str], got: list[str]) -> float:
    if not wanted or not got:
        return 0.0
    return max(sim(w, g) for w in wanted for g in got)


def _score(title: str, wanted_artists: list[str], hints: list[str], got_title: str, got_artists: list[str],
           contexts: list[str], length_s: float | None, duration_s: float | None) -> float:
    """How sure are we that a lookup result is this upload? Needs a title match AND corroboration:
    the artist, a hinted singer, or a hinted film name appearing in the result's album/title."""
    got_title, movie = _strip_movie(got_title)
    if movie:
        contexts = [*contexts, movie]
    t = sim(title, got_title)
    if t < 0.85:
        return 0.0
    if wanted_artists:
        a = _artist_match(wanted_artists, got_artists)
        who = a if a >= 0.85 else 0.0
    else:
        h = _artist_match(hints, got_artists)
        film = any(len(key(x)) >= 5 and key(x) in key(c) for x in hints for c in contexts)
        who = 0.85 if h >= 0.85 else (0.8 if film else 0.0)
    if not who:
        return 0.0
    dur = 0.0
    if length_s and duration_s:
        gap = abs(length_s - duration_s)
        dur = 1.0 if gap <= 5 else 0.5 if gap <= 15 else 0.0 if gap <= 60 else -0.5   # YouTube cuts are often shorter
    return max(0.0, 0.5 * t + 0.4 * who + 0.1 * dur)


def _years(releases: list[dict]) -> tuple[int | None, str | None, str | None]:
    """(year, album, release id) from the earliest plain studio-album release."""
    best = None
    for r in releases or []:
        rg = r.get("release-group") or {}
        secondary = rg.get("secondary-types") or []
        date = r.get("date") or ""
        rank = (0 if rg.get("primary-type") == "Album" and not secondary else 1 if not secondary else 2,
                date or "9999")
        if best is None or rank < best[0]:
            best = (rank, date, r.get("title"), r.get("id"))
    if not best:
        return None, None, None
    year = int(best[1][:4]) if best[1][:4].isdigit() else None
    return year, best[2], best[3]


def musicbrainz_tier(http: Http, base: Candidate, duration_s: float | None) -> Candidate | None:
    """Search MusicBrainz for the parsed title; accept only a title match with corroborating artist/duration."""
    readings: list[tuple[str, list[str]]] = [(base.title, base.artists)]
    for alt in base.alts:  # the other reading of "A - B", or a film-name prefix
        readings.append((alt, base.artists if base.artists and key(alt) != key(base.artists[0]) else []))
        if base.artists and key(alt) == key(base.artists[0]):
            readings.append((alt, [base.title]))
    best: tuple[float, dict, str, list[str]] | None = None
    for title, artists in readings:
        query = f'recording:"{title}"' + (f' AND artist:"{artists[0]}"' if artists else "")
        data = http.get_json(MB_URL, {"query": query, "fmt": "json", "limit": 5})
        for rec in (data or {}).get("recordings", []):
            credit = [normalize(c.get("name") or (c.get("artist") or {}).get("name", ""))
                      for c in rec.get("artist-credit", []) if isinstance(c, dict)]
            contexts = [r.get("title", "") for r in rec.get("releases", [])]
            sc = _score(title, artists, base.hints + ([base.album] if base.album else []), normalize(rec.get("title", "")),
                        credit, contexts, (rec.get("length") or 0) / 1000 or None, duration_s)
            if sc > 0 and (best is None or sc > best[0]):
                best = (sc, rec, title, credit)
    if not best or best[0] < 0.7:
        return None
    sc, rec, _, credit = best
    year, album, release_id = _years(rec.get("releases", []))
    cand = Candidate(tier="musicbrainz", title=_strip_movie(normalize(rec["title"]))[0], artists=[c for c in credit if c][:MAX_ARTISTS],
                     album=album, year=year, confidence=min(0.95, sc), versions=base.versions,
                     note=f"MusicBrainz recording {rec['id']}")
    cand.extra = {"mb_recording_id": rec["id"], "mb_release_id": release_id}
    return cand


def itunes_tier(http: Http, base: Candidate, duration_s: float | None, country: str = "IN") -> Candidate | None:
    term = " ".join([*(base.artists[:1] or base.hints[:1]), base.title]).strip()
    data = http.get_json(ITUNES_URL, {"term": term, "media": "music", "entity": "song", "country": country,
                                      "limit": 5})
    best: tuple[float, dict] | None = None
    for r in (data or {}).get("results", []):
        sc = _score(base.title, base.artists, base.hints + ([base.album] if base.album else []),
                    normalize(r.get("trackName", "")), split_names(normalize(r.get("artistName", "")), amp=True),
                    [r.get("collectionName", ""), r.get("trackName", "")],
                    (r.get("trackTimeMillis") or 0) / 1000 or None, duration_s)
        if sc > 0 and (best is None or sc > best[0]):
            best = (sc, r)
    if not best or best[0] < 0.7:
        return None
    sc, r = best
    year = int(r["releaseDate"][:4]) if r.get("releaseDate", "")[:4].isdigit() else None
    track, movie = _strip_movie(normalize(r["trackName"]))
    cand = Candidate(tier="itunes", title=clean_title(track)[0] or track,
                     artists=split_names(normalize(r.get("artistName", "")), amp=True)[:MAX_ARTISTS],
                     album=r.get("collectionName") or movie, year=year, confidence=min(0.9, sc),
                     versions=base.versions, note="iTunes search")
    cand.extra = {"genre": r.get("primaryGenreName"), "artwork": (r.get("artworkUrl100") or "").replace(
        "100x100bb", "600x600bb")}
    return cand


def resolve_candidates(row: Mapping, http: Http | None) -> list[Candidate]:
    """Run tiers in order, stopping at the first confident result. Returns every candidate considered."""
    out: list[Candidate] = []
    yt = youtube_tier(row)
    if yt:
        out.append(yt)
        if yt.confidence >= CONFIDENT:
            return out
    base = parser_tier(row)
    if yt:  # the parser can still add hints, never overrides YouTube's fields
        base.artists = base.artists or yt.artists
    out.append(base)
    if base.confidence >= CONFIDENT and not http:
        return out
    if http:
        duration = row["probe_duration_s"] or row["duration_s"]
        mb = musicbrainz_tier(http, base, duration)
        if mb:
            out.append(mb)
            if mb.confidence >= CONFIDENT:
                return out
        it = itunes_tier(http, base, duration)
        if it:
            out.append(it)
    return out
