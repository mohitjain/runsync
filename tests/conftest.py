import shutil
import subprocess
from pathlib import Path

import pytest

from runsync import db

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def make_m4a(path: Path, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-b:a", "128k", str(path)], check=True)
    return path


def make_audio(path: Path, codec: str, seconds: float = 2.0) -> Path:
    """A short sine tone in any codec/container ffmpeg can write (libopus in .webm, ac3 in .m4a ...)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", codec, str(path)], check=True)
    return path


def add_track(conn, vid: str, master: Path | None = None, **cols) -> None:
    defaults = {"original_title": vid, "title": vid, "channel": "Chan", "status": "downloaded",
                "master_path": str(master) if master else None, "downloaded_at": "2026-01-01T00:00:00+00:00"}
    defaults.update(cols)
    names = ", ".join(["video_id", *defaults])
    conn.execute(f"INSERT INTO tracks ({names}) VALUES ({', '.join('?' * (len(defaults) + 1))})",
                 [vid, *defaults.values()])
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "state.db")


@pytest.fixture(autouse=True)
def no_google_network(monkeypatch):
    """No test may reach Google: the YouTube transport and token exchange fail loudly unless a test replaces them."""
    from runsync import youtube

    def blocked(*args, **kwargs):
        raise AssertionError("a test tried to contact Google; pass a fake transport")
    monkeypatch.setattr(youtube, "_http", blocked)
    monkeypatch.setattr(youtube, "_post_form", blocked)
