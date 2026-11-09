"""Per-artist language labels. Label an artist once; every track inherits it."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime

from .net import Http
from .parse import key
from .tiers import ITUNES_URL

GENERIC_GENRES = {"pop", "world", "worldwide", "soundtrack", "music", "new age"}   # say nothing about language
SOUTH_ASIA = {"IN", "PK", "BD", "LK", "NP", "AF", "BT", "MV"}   # a country here says nothing about Hindi/Punjabi/English


def text_language(*texts: str | None) -> str | None:
    """What the upload itself says: "Punjabi songs" / "Bollywood" in title, tags, channel or description.
    Only a clear answer counts: both languages mentioned (or neither) returns None."""
    t = " ".join(x for x in texts if x).lower()
    punjabi = bool(re.search(r"\bpunjabi\b|\bpunjab\b|\bbhangra\b", t))
    hindi = bool(re.search(r"\bbollywood\b|\bhindi\b", t))
    return "punjabi" if punjabi and not hindi else "hindi" if hindi and not punjabi else None


def genre_language(genre: str | None) -> list[str] | None:
    """iTunes genre -> language. A bare "Indian Pop" says nothing about Hindi vs Punjabi, so it is not used."""
    g = (genre or "").lower()
    if "punjabi" in g:
        return ["punjabi"]
    if any(w in g for w in ("bollywood", "hindi", "filmi")):
        return ["hindi"]
    return None


def get_artist(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM artists WHERE key = ?", (key(name),)).fetchone()
    return dict(row) if row else None


def save_artist(conn: sqlite3.Connection, name: str, languages: list[str], confidence: float, source: str,
                genre: str | None = None, country: str | None = None) -> None:
    conn.execute(
        "INSERT INTO artists (name, key, languages_json, confidence, source, country, genre, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET languages_json=excluded.languages_json, "
        "confidence=excluded.confidence, source=excluded.source, country=excluded.country, genre=excluded.genre, "
        "updated_at=excluded.updated_at",
        (name, key(name), json.dumps(languages), confidence, source, country, genre,
         datetime.now(UTC).isoformat(timespec="seconds")))


def itunes_artist_genre(http: Http, artist: str) -> str | None:
    """Most common iTunes genre among this artist's songs (Indian storefront)."""
    data = http.get_json(ITUNES_URL, {"term": artist, "media": "music", "entity": "song",
                                      "attribute": "artistTerm", "country": "IN", "limit": 10})
    genres = [r["primaryGenreName"] for r in (data or {}).get("results", [])
              if key(r.get("artistName", "")) == key(artist) and r.get("primaryGenreName")]
    return Counter(genres).most_common(1)[0][0] if genres else None   # ties: first seen (stable across runs)


def musicbrainz_country(http: Http, artist: str) -> str | None:
    data = http.get_json("https://musicbrainz.org/ws/2/artist", {"query": f'artist:"{artist}"', "fmt": "json",
                                                                 "limit": 3})
    for a in (data or {}).get("artists", []):
        if key(a.get("name", "")) == key(artist) and a.get("score", 0) >= 90:
            return a.get("country")
    return None


def label_artist(conn: sqlite3.Connection, name: str, http: Http | None, overrides: dict,
                 save: bool = True) -> tuple[list[str], float, str]:
    """(languages, confidence, source). Unknown -> ([], 0, 'unknown') and the artist goes to review.
    save=False (dry run) looks things up but stores no label."""
    def store(*args, **kwargs) -> None:
        if save:
            save_artist(*args, **kwargs)
    ov = (overrides.get("artists") or {})
    for n, spec in ov.items():
        if key(n) == key(name):
            langs = [str(x).lower() for x in (spec.get("languages") or [])]
            store(conn, name, langs, 1.0, "override")
            return langs, 1.0, "override"
    known = get_artist(conn, name)
    if known and known["source"] != "unknown":
        return json.loads(known["languages_json"] or "[]"), known["confidence"] or 0.0, known["source"]
    if not http:
        return [], 0.0, "unknown"
    genre = itunes_artist_genre(http, name)
    langs = genre_language(genre)
    if langs:
        store(conn, name, langs, 0.85, "itunes_genre", genre=genre)
        return langs, 0.85, "itunes_genre"
    country = musicbrainz_country(http, name)
    if country in SOUTH_ASIA:                     # South Asian but not Hindi/Punjabi-labelled: a human decides, once
        store(conn, name, [], 0.0, "unknown", genre=genre, country=country)
        return [], 0.0, "unknown"
    if country:                               # a known non-Indian artist: English
        store(conn, name, ["english"], 0.75, "musicbrainz_country", genre=genre, country=country)
        return ["english"], 0.75, "musicbrainz_country"
    if genre and genre.lower() not in GENERIC_GENRES and "indian" not in genre.lower():   # Rock, Metal, Dance...
        store(conn, name, ["english"], 0.7, "itunes_genre", genre=genre)
        return ["english"], 0.7, "itunes_genre"
    store(conn, name, [], 0.0, "unknown", genre=genre)
    return [], 0.0, "unknown"
