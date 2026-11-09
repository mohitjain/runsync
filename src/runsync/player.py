"""Player copies: tagged, plainly named copies of masters. master/ is never modified."""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

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
    try:
        tags = MP4(path).tags or {}
        v = tags.get(key)
        if not v:
            return None
        return bytes(v[0]).decode() if key.startswith(FREEFORM) else str(v[0])
    except Exception:  # noqa: BLE001 (unreadable/foreign file: not ours)
        return None


def read_video_id(path: Path) -> str | None:
    return _read(path, FREEFORM + "VIDEO_ID")


def _pick_path(dest_dir: Path, video_id: str, meta: dict) -> Path | dict:
    """Title-only name. If another video already owns it: a different artist gets `Title (Artist)`,
    the same artist means it is a duplicate (Phase 3 decides which to keep)."""
    base = safe_filename(meta["title"])
    if base is None:
        return {"skipped": "title has letters the player file name cannot hold"}
    path = dest_dir / f"{base}.m4a"
    if not path.exists() or read_video_id(path) == video_id:
        return path
    if (_read(path, "\xa9ART") or "").casefold() == meta["artist"].casefold():
        return {"skipped": f"duplicate of '{path.name}' (same title and artist; deciding which to keep is Phase 3)"}
    with_artist = safe_filename(meta["title"], meta["artist"])
    if with_artist is None:
        return {"skipped": "same title as another song and the artist name has letters the file name cannot hold"}
    path = dest_dir / f"{with_artist}.m4a"
    if path.exists() and read_video_id(path) != video_id:
        return {"skipped": f"duplicate of '{path.name}' (same title and artist; deciding which to keep is Phase 3)"}
    return path


def build_player_copy(master: Path, dest_dir: Path, video_id: str, meta: dict, cover: Path | None) -> dict:
    """Copy an AAC/M4A master into dest_dir with clean tags. Returns {path, filename, size} or {skipped: reason}."""
    if master.suffix.lower() != ".m4a":
        return {"skipped": f"{master.suffix or 'unknown'} master: player copy for this format arrives in Phase 4"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = _pick_path(dest_dir, video_id, meta)
    if isinstance(final, dict):
        return final
    facts = probe(master)
    if not facts or facts["codec"] != "aac":   # an .m4a can hold AC-3/ALAC etc.; the player only takes AAC here
        return {"skipped": f"{(facts or {}).get('codec', 'unreadable')} audio in .m4a: needs conversion (Phase 4)"}
    tmp_dir = dest_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"{video_id}.m4a"
    shutil.copyfile(master, tmp)
    try:
        f = MP4(tmp)
        f.delete()  # start from clean tags: whatever YouTube embedded is not trusted
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
    return {"path": final, "filename": final.name, "size": final.stat().st_size}
