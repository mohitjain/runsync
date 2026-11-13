"""enrich.csv: export what is unsure, import only well-evidenced web-search answers."""
import csv

from conftest import add_track

from runsync.resolve.enrich import COLUMNS, export_enrich, import_enrich
from runsync.resolve.overrides import load_overrides, save_override


def meta(conn, vid, **cols):
    base = {"artist": None, "title": "T", "language": None, "needs_review": 0, "review_reason": None,
            "confidence": 0.5, "source_tier": "parser", "locked": 0, "resolved_at": "x"}
    base.update(cols)
    names = ", ".join(["video_id", *base])
    conn.execute(f"INSERT INTO metadata ({names}) VALUES ({', '.join('?' * (len(base) + 1))})", [vid, *base.values()])
    conn.commit()


def fill(path, **by_key):
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    for r in rows:
        r.update(by_key.get(r["key"], {}))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def test_export_lists_unsure_tracks_and_each_unlabelled_lead_artist_once(conn, tmp_path):
    add_track(conn, "a", original_title="Humari Adhuri Kahani - Full Song | Arijit Singh")
    meta(conn, "a", needs_review=1, review_reason="no artist could be determined")
    add_track(conn, "b")
    meta(conn, "b", artist="Guru Randhawa, Arjun", title="SUIT")
    add_track(conn, "c")
    meta(conn, "c", artist="Guru Randhawa", title="Lahore")
    add_track(conn, "d")
    meta(conn, "d", artist="Nirvana", language="english")
    out = tmp_path / "enrich.csv"
    assert export_enrich(out, conn) == 2
    rows = {r["key"]: r for r in csv.DictReader(out.open(encoding="utf-8"))}
    assert rows["a"]["kind"] == "track" and rows["a"]["reason"] == "no artist could be determined"
    assert rows["Guru Randhawa"]["kind"] == "artist" and rows["Guru Randhawa"]["tracks"] == "2"


def test_nothing_unsure_means_no_file(conn, tmp_path):
    out = tmp_path / "enrich.csv"
    out.write_text("old")
    assert export_enrich(out, conn) == 0 and not out.exists()


def test_well_evidenced_suggestions_are_applied_and_tagged_as_search(conn, tmp_path):
    add_track(conn, "a")
    meta(conn, "a", needs_review=1, review_reason="no artist could be determined")
    add_track(conn, "b")
    meta(conn, "b", artist="Guru Randhawa")
    out = tmp_path / "enrich.csv"
    export_enrich(out, conn)
    fill(out, a={"suggested_artist": "Arijit Singh", "suggested_title": "Humari Adhuri Kahani",
                 "suggested_language": "Hindi", "confidence": "0.9", "evidence_url": "https://musicbrainz.org/x"},
         **{"Guru Randhawa": {"suggested_language": "punjabi", "confidence": "0.95",
                              "evidence_url": "https://en.wikipedia.org/wiki/Guru_Randhawa"}})
    assert import_enrich(out, conn) == (2, 0)
    data = load_overrides(conn)
    assert data["tracks"]["a"]["artist"] == "Arijit Singh" and data["tracks"]["a"]["language"] == "hindi"
    assert data["tracks"]["a"]["source"] == "search" and data["tracks"]["a"]["evidence"].startswith("https://")
    assert data["artists"]["Guru Randhawa"]["languages"] == ["punjabi"]


def test_weak_unsourced_or_off_list_answers_are_left_for_the_user(conn, tmp_path):
    for v in "abcd":
        add_track(conn, v)
        meta(conn, v, needs_review=1, review_reason="x")
    out = tmp_path / "enrich.csv"
    export_enrich(out, conn)
    fill(out,
         a={"suggested_artist": "X", "confidence": "0.6", "evidence_url": "https://e.com"},          # not sure enough
         b={"suggested_artist": "X", "confidence": "0.95", "evidence_url": ""},                       # no evidence
         c={"suggested_artist": "X", "suggested_language": "tamil", "confidence": "0.95",
            "evidence_url": "https://e.com"},                                                         # not Hindi/English/Punjabi
         d={"suggested_artist": "X", "confidence": "high", "evidence_url": "https://e.com"})          # not a number
    assert import_enrich(out, conn) == (0, 4)
    assert load_overrides(conn) == {"tracks": {}, "artists": {}}


def test_your_own_override_is_never_replaced_by_a_search(conn, tmp_path):
    add_track(conn, "a")
    meta(conn, "a", needs_review=1, review_reason="x")
    out = tmp_path / "enrich.csv"
    save_override(conn, "track", "a", {"artist": "Mine"}, "human")
    export_enrich(out, conn)
    fill(out, a={"suggested_artist": "Theirs", "confidence": "0.99", "evidence_url": "https://e.com"})
    assert import_enrich(out, conn) == (0, 0)
    assert load_overrides(conn)["tracks"]["a"]["artist"] == "Mine"


def test_a_trashed_track_no_longer_asks_for_a_search_or_counts_as_needing_you(tmp_path):
    from runsync import db, status
    from runsync.config import Config
    from runsync.resolve.enrich import export_enrich
    conn = db.connect(tmp_path / "s.db")
    for vid, st in (("live", "downloaded"), ("gone", "trashed")):
        conn.execute("INSERT INTO tracks (video_id, status, original_title) VALUES (?, ?, 'T')", (vid, st))
        conn.execute("INSERT INTO metadata (video_id, artist, title, needs_review, review_reason, confidence) "
                     "VALUES (?, 'A', 'T', 1, 'low', 0.4)", (vid,))
    conn.commit()
    assert export_enrich(tmp_path / "enrich.csv", conn) == 1                       # only the live one
    assert status.facts(Config(library=tmp_path), conn)["need_you"] == 1
