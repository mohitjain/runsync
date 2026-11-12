"""Player copies: tagged, plainly named copies of masters. master/ is never modified."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .download import probe

FREEFORM = "----:com.apple.iTunes:"
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def ascii_name(s: str) -> str | None:
    """Accents folded to plain letters (é -> e). None when a letter cannot be folded, so the name would lose text."""
    folded = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    lost = [ch for ch in s if ord(ch) > 127 and unicodedata.category(ch).startswith("L")
            and not unicodedata.normalize("NFKD", ch).encode("ascii", "ignore")]
    return None if lost else folded


def safe_filename(title: str, artist: str | None = None) -> str | None:
    """`Title`, or `Title (Artist)` when the artist is needed to tell two songs apart. None if a letter cannot be kept."""
    t = ascii_name(title)
    a = ascii_name(artist) if artist else ""
    if t is None or a is None:
        return None
    name = ILLEGAL.sub("", f"{t} ({a})" if artist else t)
    return re.sub(r"\s+", " ", name).strip(" .")[:150] or None


def _free(value: str) -> list[MP4FreeForm]:
    return [MP4FreeForm(str(value).encode("utf-8"))]


def _read(path: Path, key: str) -> str | None:
    """A tag from a player copy (m4a or flac); None for a foreign or unreadable file."""
    try:
        if path.suffix.lower() == ".flac":
            names = {"\xa9ART": "artist", FREEFORM + "VIDEO_ID": "VIDEO_ID"}
            v = (FLAC(path).get(names.get(key, key)) or [None])[0]
            return v
        tags = MP4(path).tags or {}
        v = tags.get(key)
        if not v:
            return None
        return bytes(v[0]).decode() if key.startswith(FREEFORM) else str(v[0])
    except Exception:  # noqa: BLE001 (unreadable/foreign file: not ours)
        return None


def read_video_id(path: Path) -> str | None:
    return _read(path, FREEFORM + "VIDEO_ID")


def _existing(dirs: list[Path], base: str) -> Path | None:
    """The player copy already using this name, in any format folder."""
    for d in dirs:
        for ext in (".m4a", ".flac"):
            if (d / f"{base}{ext}").exists():
                return d / f"{base}{ext}"
    return None


class Skip(Exception):
    """This track gets no player copy; the message says why (shown to the owner)."""


def _pick_path(dest_dir: Path, video_id: str, meta: dict, ext: str, others: list[Path]) -> Path:
    """Title-only name. If another video already owns it: a different artist gets `Title (Artist)`, the same artist
    means it is a duplicate (run dedupe to keep one). `others` are the other format folders, so `Title.m4a` and
    `Title.flac` can never both exist for different tracks. Raises Skip when no honest name exists."""
    dirs = [dest_dir, *others]
    base = safe_filename(meta["title"])
    if base is None:
        raise Skip("title has letters the player file name cannot hold")
    owner = _existing(dirs, base)
    if owner is None or read_video_id(owner) == video_id:
        return dest_dir / f"{base}.{ext}"
    if (_read(owner, "\xa9ART") or "").casefold() == meta["artist"].casefold():
        raise Skip(f"duplicate of '{owner.name}' (same title and artist; run dedupe to keep one)")
    with_artist = safe_filename(meta["title"], meta["artist"])
    if with_artist is None:
        raise Skip("same title as another song and the artist name has letters the file name cannot hold")
    owner = _existing(dirs, with_artist)
    if owner is not None and read_video_id(owner) != video_id:
        raise Skip(f"duplicate of '{owner.name}' (same title and artist; run dedupe to keep one)")
    return dest_dir / f"{with_artist}.{ext}"


def _write_flac_tags(path: Path, video_id: str, meta: dict, cover: Path | None) -> None:
    f = FLAC(path)
    f.clear()   # whatever the source carried is not trusted
    f.clear_pictures()
    f["title"] = meta["title"]
    f["artist"] = meta["artist"]
    f["albumartist"] = meta.get("album_artist") or meta["artist"].split(",")[0]
    if meta.get("album"):
        f["album"] = meta["album"]
    if meta.get("year"):
        f["date"] = str(meta["year"])
    f["VIDEO_ID"] = video_id
    if meta.get("original_title"):
        f["ORIGINAL_TITLE"] = meta["original_title"]
    if meta.get("language"):
        f["LANGUAGE"] = meta["language"]
    if meta.get("replaygain_gain_db") is not None:
        f["REPLAYGAIN_TRACK_GAIN"] = f"{meta['replaygain_gain_db']:+.2f} dB"
        f["REPLAYGAIN_TRACK_PEAK"] = f"{meta['replaygain_peak']:.6f}"
    if cover and cover.exists():
        pic = Picture()
        pic.type, pic.mime, pic.desc = 3, "image/jpeg", "Cover"
        pic.width = pic.height = 500
        pic.depth = 24
        pic.data = cover.read_bytes()
        f.add_picture(pic)
    f.save()


def _build_flac(master: Path, flac_dir: Path, video_id: str, meta: dict, cover: Path | None, others: list[Path]) -> dict:
    """Decode a non-AAC master (Opus, AC-3 ...) to FLAC: lossless, so nothing is re-encoded lossy to lossy."""
    flac_dir.mkdir(parents=True, exist_ok=True)
    final = _pick_path(flac_dir, video_id, meta, "flac", others)
    tmp_dir = flac_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"{video_id}.flac"
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(master), "-map", "0:a:0",
                            "-vn", "-sample_fmt", "s16", "-c:a", "flac", "-compression_level", "8", str(tmp)],
                           capture_output=True, text=True, timeout=600, check=False)
        if r.returncode != 0 or not tmp.exists():
            raise Skip(f"could not convert to FLAC: {(r.stderr or '').strip()[-120:]}")
        _write_flac_tags(tmp, video_id, meta, cover)
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": final, "filename": final.name, "size": final.stat().st_size, "format": "flac"}


def _build_m4a(master: Path, dest_dir: Path, video_id: str, meta: dict, cover: Path | None, others: list[Path]) -> dict:
    """An AAC master is copied as it is, with clean tags: whatever YouTube embedded is not trusted."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = _pick_path(dest_dir, video_id, meta, "m4a", others)
    tmp_dir = dest_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"{video_id}.m4a"
    shutil.copyfile(master, tmp)
    try:
        f = MP4(tmp)
        f.delete()
        tags = f.tags if f.tags is not None else f.add_tags() or f.tags
        tags["\xa9nam"] = [meta["title"]]
        tags["\xa9ART"] = [meta["artist"]]
        tags["aART"] = [meta.get("album_artist") or meta["artist"].split(",")[0]]
        if meta.get("album"):
            tags["\xa9alb"] = [meta["album"]]
        if meta.get("year"):
            tags["\xa9day"] = [str(meta["year"])]
        if cover and cover.exists():
            tags["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
        tags[FREEFORM + "VIDEO_ID"] = _free(video_id)
        if meta.get("original_title"):
            tags[FREEFORM + "ORIGINAL_TITLE"] = _free(meta["original_title"])
        if meta.get("language"):
            tags[FREEFORM + "LANGUAGE"] = _free(meta["language"])
        if meta.get("replaygain_gain_db") is not None:
            tags[FREEFORM + "REPLAYGAIN_TRACK_GAIN"] = _free(f"{meta['replaygain_gain_db']:+.2f} dB")
            tags[FREEFORM + "REPLAYGAIN_TRACK_PEAK"] = _free(f"{meta['replaygain_peak']:.6f}")
        f.save()
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": final, "filename": final.name, "size": final.stat().st_size, "format": "m4a"}


def build_player_copy(master: Path, dest_dir: Path, video_id: str, meta: dict, cover: Path | None,
                      flac_dir: Path | None = None) -> dict:
    """Player copy with clean tags: AAC masters are copied as they are into dest_dir; anything else (Opus, AC-3 ...)
    is decoded to FLAC into flac_dir (default: the folder next to dest_dir).
    Returns {path, filename, size, format} or {skipped: reason}."""
    flac_dir = flac_dir or dest_dir.parent / "flac"
    try:
        if safe_filename(meta["title"]) is None:   # cheapest check first, whatever the audio is
            raise Skip("title has letters the player file name cannot hold")
        facts = probe(master) if master.exists() else None
        if master.suffix.lower() == ".m4a" and facts and facts["codec"] == "aac":
            return _build_m4a(master, dest_dir, video_id, meta, cover, [flac_dir])
        return _build_flac(master, flac_dir, video_id, meta, cover, [dest_dir])
    except Skip as why:
        return {"skipped": str(why)}
