from runsync.download import select_format


def f(fid, codec, abr):
    return {"format_id": fid, "acodec": codec, "vcodec": "none", "abr": abr}


def test_picks_highest_bitrate():
    assert select_format([f("140", "mp4a.40.2", 130), f("141", "mp4a.40.2", 258)])["format_id"] == "141"


def test_near_tie_prefers_native_aac():
    fmts = [f("774", "opus", 258), f("141", "mp4a.40.2", 256)]
    assert select_format(fmts)["format_id"] == "141"


def test_clear_opus_win_not_overridden():
    fmts = [f("774", "opus", 256), f("140", "mp4a.40.2", 130)]
    assert select_format(fmts)["format_id"] == "774"


def test_no_audio():
    assert select_format([{"format_id": "1", "vcodec": "avc1", "acodec": "none"}]) is None
