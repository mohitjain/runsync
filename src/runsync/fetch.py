"""Playlist discovery and flat item fetch via yt-dlp (library)."""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from . import youtube as yt
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
    source: str = "cookies"                              # "api" when the YouTube API answered


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


API_UNREADABLE = {"LM", "WL"}   # the API cannot read YouTube Music likes or Watch later: those use cookies


def playlist_id(url: str) -> str:
    return (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("list") or [""])[0]


def api_account_playlists(api: yt.Api) -> list[PlaylistInfo]:
    """Your playlists through the API, plus the special ones the API does not list (same set the cookies give)."""
    out = [PlaylistInfo(p["id"], p["title"], f"https://www.youtube.com/playlist?list={p['id']}") for p in yt.my_playlists(api)]
    out.append(PlaylistInfo("LL", "Liked videos", "https://www.youtube.com/playlist?list=LL"))
    out.append(PlaylistInfo("WL", "Watch later", "https://www.youtube.com/playlist?list=WL"))
    out.append(PlaylistInfo("LM", "Liked Music", "https://music.youtube.com/playlist?list=LM"))
    return out


def api_playlist_items(api: yt.Api, pid: str) -> PlaylistItems:
    """The API's answer in the same shape yt-dlp gives (repeats kept, deleted/private ones in `unknown`)."""
    items = PlaylistItems(source="api")
    for e in yt.playlist_entries(api, pid):
        if e.available:
            items.ids.append(e.video_id)
            items.titles[e.video_id] = e.title
        else:
            items.unknown.append(e.video_id)
    return items


def fetch_playlist_items(url: str, cookies: Path | None, api: yt.Api | None = None) -> PlaylistItems:
    """Through the API when signed in (no cookies needed); any API problem or an empty answer falls back to yt-dlp."""
    pid = playlist_id(url)
    if api is not None and pid and pid not in API_UNREADABLE:
        try:
            items = api_playlist_items(api, pid)
            if items.ids or items.unknown:
                log.info("playlist %s via API: %d ok, %d unreadable", pid, len(items.ids), len(items.unknown))
                return items
            log.info("playlist %s: the API returned nothing; trying cookies", pid)
        except yt.YouTubeError as exc:
            log.warning("playlist %s: API failed (%s); falling back to cookies", pid, exc)
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
