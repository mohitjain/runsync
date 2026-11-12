import json
import subprocess

import pytest
from conftest import make_m4a, needs_ffmpeg
from mutagen.mp4 import MP4

from runsync.player import FREEFORM, ascii_name, build_player_copy, safe_filename
from runsync.resolve import covers, gain
from runsync.resolve.model import TRUST

SOF0 = b"\xff\xd8\xff\xc0\x00\x11\x08\x01\xf4\x01\xf4\x03"
SOF2 = b"\xff\xd8\xff\xc2\x00\x11\x08\x01\xf4\x01\xf4\x03"


def test_baseline_vs_progressive_jpeg_detection():
    assert covers.is_baseline_jpeg(SOF0) and not covers.is_baseline_jpeg(SOF2)
    assert not covers.is_baseline_jpeg(b"not a jpeg")


def test_trust_order_is_what_the_plan_says():
    assert TRUST["override"] > TRUST["youtube"] > TRUST["musicbrainz"] > TRUST["itunes"] > TRUST["parser"]


def test_ascii_names():
    assert ascii_name("Beyoncé") == "Beyonce"
    assert ascii_name("Привет") is None
    assert safe_filename('Who: "Made" Who?') == "Who Made Who"
    assert safe_filename("Don't Cry", "AC/DC") == "Don't Cry (ACDC)"
    assert safe_filename("तुम", "x") is None


def test_ebur128_summary_parses():
    text = """  Integrated loudness:\n    I:         -9.3 LUFS\n    Threshold: -19.4 LUFS\n
  True peak:\n    Peak:       -0.5 dBFS\n"""
    g = gain.parse_ebur128(text)
    assert g["loudness_lufs"] == -9.3 and g["replaygain_gain_db"] == -8.7
    assert 0.94 < g["replaygain_peak"] < 0.95
    assert gain.parse_ebur128("nothing useful") is None


def test_cover_sources_are_tried_in_order_and_cached(tmp_path):
    seen = []

    def get(url):
        seen.append(url)
        if "coverartarchive" in url:
            raise OSError("404")
        return b"image"
    converted = []

    def fake_convert(raw, out, size=500):
        converted.append(raw)
        out.write_bytes(b"jpg")
        return True
    real = covers.to_square_jpeg
    covers.to_square_jpeg = fake_convert
    try:
        got = covers.fetch_cover("vid", tmp_path, "mbid", "https://itunes/art.jpg", get=get)
        assert got[1] == "itunes" and "coverartarchive" in seen[0]
        again = covers.fetch_cover("vid", tmp_path, "mbid", None, get=get)
        assert again[1] == "cache" and len(seen) == 2
    finally:
        covers.to_square_jpeg = real


@needs_ffmpeg
def test_cover_is_cropped_square_500_baseline(tmp_path):
    png = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=1",
                          "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                         capture_output=True, check=True).stdout
    out = tmp_path / "c.jpg"
    assert covers.to_square_jpeg(png, out)
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                                     capture_output=True, text=True, check=True).stdout)
    s = info["streams"][0]
    assert (s["width"], s["height"]) == (500, 500) and covers.is_baseline_jpeg(out.read_bytes())
    assert not covers.to_square_jpeg(b"garbage", tmp_path / "bad.jpg")


@needs_ffmpeg
def test_real_loudness_analysis(tmp_path):
    g = gain.analyse(make_m4a(tmp_path / "t.m4a", 3))
    assert g and -40 < g["loudness_lufs"] < 0 and g["replaygain_peak"] > 0


@needs_ffmpeg
def test_player_copy_has_tags_and_master_is_untouched(tmp_path):
    import hashlib
    master = make_m4a(tmp_path / "master" / "x.m4a")
    before = hashlib.sha256(master.read_bytes()).hexdigest()
    meta = {"artist": "Linkin Park, Sia", "title": "In Pieces", "album": "Minutes to Midnight", "year": 2007,
            "language": "english", "original_title": "In Pieces (Official)", "replaygain_gain_db": -3.2,
            "replaygain_peak": 0.9}
    out = build_player_copy(master, tmp_path / "player" / "m4a", "vid1", meta, None)
    tags = MP4(out["path"]).tags
    assert out["filename"] == "In Pieces.m4a"
    assert tags["\xa9nam"] == ["In Pieces"] and tags["aART"] == ["Linkin Park"] and tags["\xa9day"] == ["2007"]
    assert bytes(tags[FREEFORM + "VIDEO_ID"][0]) == b"vid1"
    assert bytes(tags[FREEFORM + "REPLAYGAIN_TRACK_GAIN"][0]) == b"-3.20 dB"
    assert hashlib.sha256(master.read_bytes()).hexdigest() == before
    assert not list((tmp_path / "player" / "m4a" / ".tmp").glob("*"))


@needs_ffmpeg
def test_filename_is_title_only_and_collisions_are_handled(tmp_path):
    master = make_m4a(tmp_path / "m.m4a")
    d = tmp_path / "p"
    a = build_player_copy(master, d, "v1", {"artist": "Avicii", "title": "Wake Me Up"}, None)
    assert a["filename"] == "Wake Me Up.m4a"
    again = build_player_copy(master, d, "v1", {"artist": "Avicii", "title": "Wake Me Up"}, None)
    assert again["path"] == a["path"]                      # same video: replaced in place
    dup = build_player_copy(master, d, "v2", {"artist": "avicii", "title": "Wake Me Up"}, None)
    assert "duplicate" in dup["skipped"]                   # same song from another video: not a second copy
    other = build_player_copy(master, d, "v3", {"artist": "Someone Else", "title": "Wake Me Up"}, None)
    assert other["filename"] == "Wake Me Up (Someone Else).m4a"   # different song, same title
    assert sorted(f.name for f in d.glob("*.m4a")) == ["Wake Me Up (Someone Else).m4a", "Wake Me Up.m4a"]


def test_non_m4a_and_unholdable_names_are_skipped_with_a_reason(tmp_path):
    (tmp_path / "x.webm").write_bytes(b"x")
    assert "FLAC" in build_player_copy(tmp_path / "x.webm", tmp_path / "p", "v", {"artist": "A", "title": "T"},
                                       None)["skipped"]          # not audio at all: it cannot be converted either
    (tmp_path / "y.m4a").write_bytes(b"x")
    assert "cannot hold" in build_player_copy(tmp_path / "y.m4a", tmp_path / "p", "v",
                                               {"artist": "A", "title": "Привет"}, None)["skipped"]


@needs_ffmpeg
def test_m4a_that_does_not_hold_aac_is_converted_to_flac_not_copied(tmp_path):
    ac3 = tmp_path / "x.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1",
                    "-c:a", "ac3", str(ac3)], check=True)
    out = build_player_copy(ac3, tmp_path / "p", "v1", {"artist": "A", "title": "T"}, None)
    assert out["format"] == "flac" and out["path"].parent == tmp_path / "flac"      # converted, never copied as m4a
    assert not list((tmp_path / "p").glob("*.m4a"))


def _cover(path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=500x500",
                    "-frames:v", "1", "-pix_fmt", "yuvj420p", str(path)], check=True)
    return path


@needs_ffmpeg
@pytest.mark.parametrize("codec,name", [("libopus", "o.webm"), ("ac3", "a.m4a")])
def test_non_aac_masters_become_tagged_flac_and_the_master_is_untouched(tmp_path, codec, name):
    import hashlib

    from conftest import make_audio
    from mutagen.flac import FLAC

    from runsync.download import probe
    master = make_audio(tmp_path / name, codec)
    before = hashlib.sha256(master.read_bytes()).hexdigest()
    meta = {"artist": "Avicii", "title": "Wake Me Up", "album": "True", "year": 2013, "language": "english",
            "original_title": "Avicii - Wake Me Up (Official Video)", "replaygain_gain_db": -9.5, "replaygain_peak": 0.98}
    out = build_player_copy(master, tmp_path / "m4a", "vid1", meta, _cover(tmp_path / "c.jpg"), flac_dir=tmp_path / "flac")
    assert out["format"] == "flac" and out["filename"] == "Wake Me Up.flac" and out["path"].exists()
    f = FLAC(out["path"])
    assert (f["title"], f["artist"], f["album"], f["date"], f["VIDEO_ID"], f["LANGUAGE"]) == (
        ["Wake Me Up"], ["Avicii"], ["True"], ["2013"], ["vid1"], ["english"])
    assert f["REPLAYGAIN_TRACK_GAIN"] == ["-9.50 dB"] and f["ORIGINAL_TITLE"]
    assert len(f.pictures) == 1 and f.pictures[0].data[:2] == b"\xff\xd8" and (f.pictures[0].width, f.pictures[0].height) == (500, 500)
    assert abs(probe(out["path"])["duration_s"] - 2.0) < 0.5 and probe(out["path"])["codec"] == "flac"
    assert hashlib.sha256(master.read_bytes()).hexdigest() == before
    assert not list((tmp_path / "flac" / ".tmp").glob("*"))


@needs_ffmpeg
def test_flac_and_m4a_copies_never_share_a_name_across_tracks(tmp_path):
    from conftest import make_audio
    aac = make_m4a(tmp_path / "a.m4a")
    opus = make_audio(tmp_path / "o.webm", "libopus")
    one = build_player_copy(aac, tmp_path / "m4a", "v1", {"artist": "A", "title": "Song"}, None, flac_dir=tmp_path / "flac")
    two = build_player_copy(opus, tmp_path / "m4a", "v2", {"artist": "B", "title": "Song"}, None, flac_dir=tmp_path / "flac")
    same = build_player_copy(opus, tmp_path / "m4a", "v3", {"artist": "A", "title": "Song"}, None, flac_dir=tmp_path / "flac")
    assert one["filename"] == "Song.m4a" and two["filename"] == "Song (B).flac"
    assert "duplicate" in same["skipped"]
