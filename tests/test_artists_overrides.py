import pytest

from runsync.resolve.artists import (
    genre_language,
    get_artist,
    label_artist,
)
from runsync.resolve.overrides import (
    import_review_fixes,
    load_overrides,
    save_override,
    write_review,
)


class FakeHttp:
    def __init__(self, itunes=None, mb=None):
        self.itunes, self.mb, self.calls = itunes or [], mb or [], 0

    def get_json(self, base, params):
        self.calls += 1
        if "itunes" in base:
            return {"results": self.itunes}
        return {"artists": self.mb}


def test_genre_hints():
    assert genre_language("Punjabi Pop") == ["punjabi"] and genre_language("Bollywood") == ["hindi"]
    assert genre_language("Rock") is None


def test_artist_is_labelled_once_then_inherited(conn):
    http = FakeHttp(itunes=[{"artistName": "Diljit Dosanjh", "primaryGenreName": "Punjabi Pop"}])
    assert label_artist(conn, "Diljit Dosanjh", http, {})[0] == ["punjabi"]
    calls = http.calls
    assert label_artist(conn, "diljit dosanjh", http, {})[0] == ["punjabi"]   # case-insensitive, no new lookups
    assert http.calls == calls
    assert get_artist(conn, "Diljit Dosanjh")["source"] == "itunes_genre"


def test_override_label_beats_lookups(conn):
    http = FakeHttp(itunes=[{"artistName": "X", "primaryGenreName": "Rock"}])
    langs, conf, src = label_artist(conn, "X", http, {"artists": {"X": {"languages": ["Hindi", "Punjabi"]}}})
    assert (langs, conf, src) == (["hindi", "punjabi"], 1.0, "override") and http.calls == 0


def test_indian_artist_with_no_language_signal_goes_to_review(conn):
    http = FakeHttp(mb=[{"name": "Someone", "score": 100, "country": "IN"}])
    assert label_artist(conn, "Someone", http, {}) == ([], 0.0, "unknown")


def test_western_artist_defaults_to_english(conn):
    http = FakeHttp(itunes=[{"artistName": "Avicii", "primaryGenreName": "Dance"}])
    assert label_artist(conn, "Avicii", http, {})[0] == ["english"]


def test_no_network_means_unknown_not_a_guess(conn):
    assert label_artist(conn, "Anyone", None, {}) == ([], 0.0, "unknown")


def test_override_store_round_trips_and_human_beats_search(conn):
    assert save_override(conn, "track", "v1", {"artist": "Me"}, "human")
    assert not save_override(conn, "track", "v1", {"artist": "Them"}, "search", "https://e.com", 0.99)
    assert load_overrides(conn)["tracks"]["v1"]["artist"] == "Me"
    assert save_override(conn, "track", "v2", {"artist": "A"}, "search", "https://e.com", 0.9)
    assert not save_override(conn, "track", "v2", {"artist": "A"}, "search", "https://e.com", 0.9)   # unchanged
    assert save_override(conn, "track", "v2", {"artist": "B"}, "search", "https://e.com", 0.9)       # search may update search
    assert load_overrides(conn)["tracks"]["v2"]["artist"] == "B"
    assert load_overrides(conn)["artists"] == {}


def test_review_fixes_flow_into_overrides(tmp_path, conn):
    import csv
    review = tmp_path / "review.csv"
    write_review(review, [{"video_id": "v1", "reason": "x"}, {"video_id": "v2", "reason": "y"}])
    with review.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0]["fix_artist"], rows[0]["fix_title"] = "Fixed Artist", "Fixed, Title"   # the human edits one row
    with review.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    assert import_review_fixes(review, conn) == 1
    spec = load_overrides(conn)["tracks"]["v1"]
    assert (spec["artist"], spec["title"], spec["source"]) == ("Fixed Artist", "Fixed, Title", "human")


def test_text_language_reads_punjabi_and_bollywood_words():
    from runsync.resolve.artists import text_language
    assert text_language("Bapu Zimidar | Latest Punjabi Songs") == "punjabi"
    assert text_language("Full Song | Bollywood") == "hindi"
    assert text_language("Linkin Park - Crawling") is None


def test_bare_indian_pop_genre_does_not_label_hindi(conn):
    assert genre_language("Indian Pop") is None
    http = FakeHttp(itunes=[{"artistName": "Badshah", "primaryGenreName": "Indian Pop"}],
                    mb=[{"name": "Badshah", "score": 100, "country": "IN"}])
    assert label_artist(conn, "Badshah", http, {}, save=False)[0] == []    # human decides, once


def test_text_language_needs_a_clear_answer():
    from runsync.resolve.artists import text_language
    assert text_language("Latest Punjabi Songs", "bollywood") is None   # both: ambiguous
    assert text_language("Linkin Park") is None


def test_genre_ties_are_broken_the_same_way_every_run():
    from runsync.resolve.artists import itunes_artist_genre
    results = [{"artistName": "X", "primaryGenreName": g} for g in ("Pop", "Rock", "Pop", "Rock", "Jazz")]
    assert {itunes_artist_genre(FakeHttp(itunes=results), "X") for _ in range(20)} == {"Pop"}


def test_only_hindi_english_punjabi_are_accepted(conn):
    with pytest.raises(ValueError, match="tamil"):
        save_override(conn, "track", "v1", {"language": "tamil"})
    with pytest.raises(ValueError, match="french"):
        save_override(conn, "artist", "Someone", {"languages": ["hindi", "french"]})
    save_override(conn, "artist", "Someone", {"languages": ["english"]})
    assert load_overrides(conn)["artists"]["Someone"]["languages"] == ["english"]


def test_pakistani_artist_is_not_labelled_english(conn):
    http = FakeHttp(itunes=[{"artistName": "Atif Aslam", "primaryGenreName": "Indian Pop"}],
                    mb=[{"name": "Atif Aslam", "score": 100, "country": "PK"}])
    assert label_artist(conn, "Atif Aslam", http, {}, save=False)[0] == []
