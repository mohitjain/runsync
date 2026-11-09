"""Playlist discovery and flat item fetch via yt-dlp (library)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from .logs import YtdlpLogger

log = logging.getLogger("runsync.fetch")

UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}
LIBRARY_FEED = "https://www.youtube.com/feed/playlists"


@dataclass
class PlaylistInfo:
    id: str
    title: str
    url: str


@dataclass
class PlaylistItems:
    ids: list[str] = field(default_factory=list)        # available video ids, in order
    titles: dict[str, str] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)    # private/deleted: present but unreadable


def _opts(cookies: Path | None) -> dict:
    o: dict = {"quiet": True, "extract_flat": "in_playlist", "skip_download": True, "logger": YtdlpLogger(),
               "verbose": True}
    if cookies:
        o["cookiefile"] = str(cookies)
    return o


def fetch_account_playlists(cookies: Path) -> list[PlaylistInfo]:
    log.info("fetching account playlists from %s", LIBRARY_FEED)
    t0 = time.monotonic()
    with yt_dlp.YoutubeDL(_opts(cookies)) as ydl:
        info = ydl.extract_info(LIBRARY_FEED, download=False)
    log.info("account feed: %d entries in %.1fs", len(info.get("entries") or []), time.monotonic() - t0)
    out = []
    for e in info.get("entries") or []:
        pid = e.get("id")
        if not pid or pid.startswith("RD"):  # auto-generated mixes
            continue
        out.append(PlaylistInfo(pid, e.get("title") or pid, f"https://www.youtube.com/playlist?list={pid}"))
    # YouTube Music's music-only likes aren't in the feed; add them explicitly.
    out.append(PlaylistInfo("LM", "Liked Music", "https://music.youtube.com/playlist?list=LM"))
    return out


def playlist_info(url: str, cookies: Path | None) -> PlaylistInfo:
    with yt_dlp.YoutubeDL(_opts(cookies) | {"playlistend": 1}) as ydl:
        info = ydl.extract_info(url, download=False)
    pid = info.get("id")
    if not pid:
        raise ValueError(f"not a playlist: {url}")
    return PlaylistInfo(pid, info.get("title") or pid, f"https://www.youtube.com/playlist?list={pid}")


def parse_entries(entries: list[dict]) -> PlaylistItems:
    items = PlaylistItems()
    for e in entries:
        vid = (e or {}).get("id")
        title = ((e or {}).get("title") or "")
        if not vid:
            continue
        if title.strip().lower() in UNAVAILABLE_TITLES:
            items.unknown.append(vid)
        else:
            items.ids.append(vid)
            items.titles[vid] = title
    return items


def fetch_playlist_items(url: str, cookies: Path | None) -> PlaylistItems:
    log.info("fetching playlist %s (cookies=%s)", url, bool(cookies))
    t0 = time.monotonic()
    try:
        with yt_dlp.YoutubeDL(_opts(cookies)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        log.exception("playlist fetch failed: %s", url)
        raise
    items = parse_entries(info.get("entries") or [])
    log.info("playlist %s: %d ok, %d unreadable in %.1fs", url, len(items.ids), len(items.unknown),
             time.monotonic() - t0)
    if items.unknown:
        log.debug("unreadable ids: %s", ", ".join(items.unknown))
    return items
