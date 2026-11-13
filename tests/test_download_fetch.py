"""The yt-dlp seams: download and playlist discovery, with a fake yt-dlp (no network)."""
from typing import ClassVar

import pytest
from conftest import make_m4a, needs_ffmpeg

from runsync import download, fetch

FORMATS = [{"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none", "abr": 129.0, "ext": "m4a", "asr": 44100},
           {"format_id": "141", "acodec": "mp4a.40.2", "vcodec": "none", "abr": 258.0, "ext": "m4a", "asr": 44100},
           {"format_id": "18", "acodec": "mp4a.40.2", "vcodec": "avc1", "abr": 96.0, "ext": "mp4"}]       # has video: ignored
INFO = {"title": "Song", "duration": 2.0, "channel": "Chan", "formats": FORMATS}


class FakeYdl:
    """Stands in for yt_dlp.YoutubeDL: `download` writes `payload` where yt-dlp would have."""
    payload = "audio"
    seen: ClassVar[list] = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        FakeYdl.seen.append(self.opts["format"])
        out = self.opts["outtmpl"].replace("%(ext)s", "m4a")
        if FakeYdl.payload == "audio":
            make_m4a(__import__("pathlib").Path(out))
        elif FakeYdl.payload == "junk":
            __import__("pathlib").Path(out).write_bytes(b"this is not audio")


@pytest.fixture
def ydl(monkeypatch):
    FakeYdl.payload, FakeYdl.seen = "audio", []
    monkeypatch.setattr(download.yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(download, "_extract", lambda vid, cookies: ({"quiet": True}, INFO))
    return FakeYdl


@needs_ffmpeg
def test_download_best_takes_the_best_audio_stream_checks_the_file_and_reports_its_facts(tmp_path, ydl):
    got = download.download_best("abcdefghijk", tmp_path / "master", None)
    assert ydl.seen == ["141"] and got["itag"] == "141" and got["bitrate"] == 258.0         # the 258k audio, not the video
    cols = got["columns"]
    assert got["path"].parent == tmp_path / "master" and got["path"].exists()
    assert cols["master_path"] == str(got["path"]) and len(cols["sha256"]) == 64 and cols["probe_codec"] == "aac"
    assert cols["used_cookies"] == 0 and cols["file_size_bytes"] > 0 and cols["download_seconds"] >= 0
    assert not list((tmp_path / "master" / ".tmp").glob("*"))                                 # nothing left half-done


def test_a_download_that_is_not_audio_is_refused_and_removed(tmp_path, ydl):
    ydl.payload = "junk"
    with pytest.raises(RuntimeError, match="integrity check failed"):
        download.download_best("abcdefghijk", tmp_path / "master", None)
    assert not list((tmp_path / "master").glob("*.m4a")) and not list((tmp_path / "master" / ".tmp").glob("*"))


def test_a_download_that_produces_nothing_is_an_error(tmp_path, ydl):
    ydl.payload = "nothing"
    with pytest.raises(RuntimeError, match="no file"):
        download.download_best("abcdefghijk", tmp_path / "master", None)


def test_no_audio_formats_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "_extract", lambda vid, cookies: ({}, {"formats": [FORMATS[2]]}))
    with pytest.raises(RuntimeError, match="no audio formats"):
        download.download_best("abcdefghijk", tmp_path, None)


class ListingYdl:
    entries: ClassVar[list] = [{"id": "PLabc", "title": "Running"}, {"id": "RDmix", "title": "My Mix"}, {"id": "PLdef", "title": "Gym"},
               {"title": "no id"}]

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if "feed/playlists" in url:
            return {"entries": self.entries}
        return {"id": "PLabc", "title": "Running"}


def test_account_playlists_skip_auto_mixes_and_always_include_liked_music(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.yt_dlp, "YoutubeDL", ListingYdl)
    got = fetch.fetch_account_playlists(tmp_path / "cookies.txt")
    assert [(p.id, p.title) for p in got] == [("PLabc", "Running"), ("PLdef", "Gym"), ("LM", "Liked Music")]
    assert got[0].url == "https://www.youtube.com/playlist?list=PLabc"


def test_playlist_info_by_link_and_a_link_that_is_not_a_playlist(monkeypatch):
    monkeypatch.setattr(fetch.yt_dlp, "YoutubeDL", ListingYdl)
    assert fetch.playlist_info("https://www.youtube.com/playlist?list=PLabc", None).id == "PLabc"
    monkeypatch.setattr(ListingYdl, "extract_info", lambda self, url, download=False: {"title": "x"})
    with pytest.raises(ValueError, match="not a playlist"):
        fetch.playlist_info("https://example.com", None)
