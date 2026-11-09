"""Cover art: Cover Art Archive -> iTunes -> YouTube thumbnail, normalised to a 500x500 baseline JPEG."""
from __future__ import annotations

import logging
import subprocess
import urllib.request
from pathlib import Path

from .net import USER_AGENT

log = logging.getLogger("runsync.covers")


def is_baseline_jpeg(data: bytes) -> bool:
    """True for a JPEG whose frame header is baseline (SOF0), not progressive (SOF2)."""
    if data[:2] != b"\xff\xd8":
        return False
    i = 2
    while i + 4 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xC0:
            return True
        if marker in (0xC1, 0xC2):
            return False
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return False


def download(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def to_square_jpeg(raw: bytes, out: Path, size: int = 500) -> bool:
    """Center-crop to a square, scale to size x size, write a baseline JPEG. Returns True on success."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0", "-vf",
         f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}", "-frames:v", "1", "-q:v", "3",
         "-pix_fmt", "yuvj420p", "-f", "mjpeg", str(tmp)],
        input=raw, capture_output=True, check=False, timeout=60)
    if r.returncode != 0 or not tmp.exists() or not is_baseline_jpeg(tmp.read_bytes()):
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(out)
    return True


def fetch_cover(video_id: str, out_dir: Path, mb_release_id: str | None, itunes_art: str | None,
                get=download) -> tuple[Path, str] | None:
    """Try each source in order; return (path, source) or None. Cached by video id."""
    out = out_dir / f"{video_id}.jpg"
    if out.exists():
        return out, "cache"
    sources: list[tuple[str, str]] = []
    if mb_release_id:
        sources.append(("coverartarchive", f"https://coverartarchive.org/release/{mb_release_id}/front-500"))
    if itunes_art:
        sources.append(("itunes", itunes_art))
    sources.append(("youtube", f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"))
    sources.append(("youtube", f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"))
    for name, url in sources:
        try:
            raw = get(url)
        except Exception as e:  # noqa: BLE001 (missing art is normal: try the next source)
            log.debug("cover %s failed (%s): %s", name, type(e).__name__, url)
            continue
        if to_square_jpeg(raw, out):
            return out, name
    return None
