"""Best-audio selection, download to .part, ffprobe integrity check, atomic move into master/."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import yt_dlp

from .logs import YtdlpLogger

log = logging.getLogger("runsync.download")

NATIVE_CODECS = ("mp4a", "aac", "alac", "flac")


def audio_formats(formats: list[dict]) -> list[dict]:
    return [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
            and f.get("abr")]


def _is_native(f: dict) -> bool:
    return str(f.get("acodec", "")).startswith(NATIVE_CODECS)


def select_format(formats: list[dict], near_tie_ratio: float = 0.10) -> dict | None:
    """Highest bitrate wins; within near_tie_ratio of the best, prefer a natively playable codec."""
    cands = audio_formats(formats)
    if not cands:
        return None
    best = max(f["abr"] for f in cands)
    tied = [f for f in cands if f["abr"] >= best * (1 - near_tie_ratio)]
    native = [f for f in tied if _is_native(f)]
    pool = native or tied
    return max(pool, key=lambda f: f["abr"])


def probe(path: Path) -> dict | None:
    """ffprobe the file; None if unreadable/empty. Returns normalized facts."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels,bit_rate:format=duration,bit_rate,format_name",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True).stdout
        data = json.loads(out)
        stream = (data.get("streams") or [None])[0]
        fmt = data.get("format", {})
        duration = float(fmt.get("duration", 0))
        if not stream or duration <= 0:
            return None
        br = stream.get("bit_rate") or fmt.get("bit_rate")
        return {"codec": stream.get("codec_name"), "sample_rate": int(stream.get("sample_rate") or 0) or None,
                "channels": stream.get("channels"), "bitrate_kbps": round(int(br) / 1000, 1) if br else None,
                "duration_s": duration, "container": fmt.get("format_name")}
    except subprocess.CalledProcessError as exc:
        log.error("ffprobe failed for %s: %s", path.name, (exc.stderr or "").strip()[:300])
    except Exception:
        log.exception("ffprobe check errored for %s", path.name)
    return None


def probe_ok(path: Path) -> bool:
    return probe(path) is not None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_source_meta(info: dict) -> dict:
    """Pick the YouTube-side facts worth keeping, as tracks columns."""
    desc = info.get("description") or ""
    return {
        "original_title": info.get("title"), "channel": info.get("channel"), "channel_id": info.get("channel_id"),
        "uploader": info.get("uploader"), "upload_date": info.get("upload_date"),
        "duration_s": info.get("duration"), "description": desc[:4000] or None,
        "yt_tags": json.dumps(info.get("tags") or []), "yt_categories": json.dumps(info.get("categories") or []),
        "webpage_url": info.get("webpage_url"), "availability": info.get("availability"),
        "yt_track": info.get("track"), "yt_artist": _joined(info.get("artists")) or info.get("artist"),
        "yt_album": info.get("album"), "yt_release_year": info.get("release_year"),
    }


def _joined(v) -> str | None:
    return ", ".join(v) if isinstance(v, list) and v else None


def candidates_summary(formats: list[dict]) -> list[dict]:
    return [{"itag": f["format_id"], "codec": f.get("acodec"), "abr": f["abr"], "ext": f.get("ext"),
             "asr": f.get("asr"), "note": f.get("format_note")}
            for f in sorted(audio_formats(formats), key=lambda f: f["abr"], reverse=True)]


def file_facts(path: Path, youtube_duration: float | None) -> dict:
    """Facts measured on the actual file, as tracks columns."""
    p = probe(path) or {}
    drift = round(p["duration_s"] - youtube_duration, 2) if p.get("duration_s") and youtube_duration else None
    return {"master_filename": path.name, "file_ext": path.suffix.lstrip("."), "file_size_bytes": path.stat().st_size,
            "sha256": sha256_file(path), "probe_codec": p.get("codec"), "probe_bitrate_kbps": p.get("bitrate_kbps"),
            "probe_sample_rate": p.get("sample_rate"), "probe_channels": p.get("channels"),
            "probe_duration_s": p.get("duration_s"), "duration_drift_s": drift}


def _extract(video_id: str, cookies: Path | None) -> tuple[dict, dict]:
    """Extract info without downloading. Asking for bestaudio makes yt-dlp raise its REAL error
    (e.g. 'Video unavailable ... copyright') instead of a generic 'no formats'."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    base = {"quiet": True, "logger": YtdlpLogger()}
    if cookies:
        base["cookiefile"] = str(cookies)
    with yt_dlp.YoutubeDL(base | {"skip_download": True, "format": "bestaudio/best"}) as ydl:
        info = ydl.extract_info(url, download=False)
    return base, info


def probe_track(video_id: str, cookies: Path | None, near_tie_ratio: float = 0.10) -> dict:
    """What would we download? No file is fetched. Returns tracks-column values plus est_bytes."""
    _, info = _extract(video_id, cookies)
    fmt = select_format(info.get("formats") or [], near_tie_ratio)
    if fmt is None:
        raise RuntimeError("no audio formats available")
    est = fmt.get("filesize") or fmt.get("filesize_approx")
    if not est and info.get("duration"):
        est = int(fmt["abr"] * 1000 / 8 * info["duration"])
    cols = {**extract_source_meta(info), "itag": str(fmt["format_id"]), "codec": fmt.get("acodec"),
            "bitrate": fmt.get("abr"), "sample_rate": fmt.get("asr"), "channels": fmt.get("audio_channels"),
            "container": fmt.get("ext"), "format_note": fmt.get("format_note"), "stream_size_approx": est,
            "candidates_json": json.dumps(candidates_summary(info.get("formats") or [])),
            "used_cookies": int(bool(cookies)), "ytdlp_version": yt_dlp.version.__version__}
    log.info("%s probe: format %s %s @ %sk ~%.1f MB", video_id, fmt["format_id"], fmt.get("acodec"), fmt["abr"],
             (est or 0) / 1e6)
    return {"columns": cols, "est_bytes": est or 0, "codec": fmt.get("acodec"), "bitrate": fmt["abr"],
            "premium": fmt["abr"] >= 250}


def download_best(video_id: str, master: Path, cookies: Path | None, near_tie_ratio: float = 0.10) -> dict:
    """Returns {path, itag, codec, bitrate}. Raises on failure (caller classifies)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    base, info = _extract(video_id, cookies)
    cands = sorted(audio_formats(info.get("formats") or []), key=lambda f: f["abr"], reverse=True)
    log.debug("%s candidates: %s", video_id,
              "; ".join(f"{f['format_id']}={f.get('acodec')}@{f['abr']:.0f}k" for f in cands) or "none")
    fmt = select_format(info.get("formats") or [], near_tie_ratio)
    if fmt is None:
        raise RuntimeError("no audio formats available")
    log.info("%s title=%r chose format %s (%s @ %sk)", video_id, info.get("title"), fmt["format_id"],
             fmt.get("acodec"), fmt["abr"])
    t0 = time.monotonic()
    tmp = master / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(base | {"format": fmt["format_id"], "outtmpl": str(tmp / f"{video_id}.%(ext)s"),
                                  "part": True, "overwrites": True}) as ydl:
        ydl.download([url])
    got = [p for p in tmp.glob(f"{video_id}.*") if p.suffix != ".part"]
    if not got:
        raise RuntimeError("download produced no file")
    src = got[0]
    if not probe_ok(src):
        src.unlink(missing_ok=True)
        raise RuntimeError("integrity check failed (ffprobe)")
    dest = master / src.name
    os.replace(src, dest)
    log.info("%s saved %s (%.1f MB) in %.1fs", video_id, dest.name, dest.stat().st_size / 1e6,
             time.monotonic() - t0)
    seconds = round(time.monotonic() - t0, 1)
    meta = extract_source_meta(info)
    facts = file_facts(dest, info.get("duration"))
    if facts["duration_drift_s"] is not None and abs(facts["duration_drift_s"]) > 3:
        log.warning("%s duration drift %.1fs (file %.1fs vs YouTube %.1fs)", video_id, facts["duration_drift_s"],
                    facts["probe_duration_s"], info.get("duration"))
    columns = {
        **meta, **facts,
        "itag": str(fmt["format_id"]), "codec": fmt.get("acodec"), "bitrate": fmt.get("abr"),
        "sample_rate": fmt.get("asr"), "channels": fmt.get("audio_channels"), "container": fmt.get("ext"),
        "format_note": fmt.get("format_note"),
        "stream_size_approx": fmt.get("filesize") or fmt.get("filesize_approx"),
        "candidates_json": json.dumps(candidates_summary(info.get("formats") or [])),
        "used_cookies": int(bool(cookies)), "ytdlp_version": yt_dlp.version.__version__,
        "master_path": str(dest), "download_seconds": seconds,
    }
    return {"path": dest, "itag": columns["itag"], "codec": columns["codec"], "bitrate": columns["bitrate"],
            "columns": columns}
