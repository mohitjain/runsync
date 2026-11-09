"""Choosing playlists by number or name, without a prompt."""
import pytest
from conftest import add_track

from runsync.playlist_select import apply_selection, list_playlists, resolve_tokens


@pytest.fixture
def pl(conn):
    for pid, title, sel in [("p1", "Running", 1), ("p2", "Gym Mix", 0), ("p3", "Gym Classics", 0), ("p4", "Liked Music", 1)]:
        conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES (?,?,?,?)", (pid, title, "u", sel))
    conn.commit()
    return conn


def put(conn, pid, vid, status="downloaded"):
    if not conn.execute("SELECT 1 FROM tracks WHERE video_id = ?", (vid,)).fetchone():
        add_track(conn, vid, status=status)
    conn.execute("INSERT INTO playlist_items (playlist_id, video_id, position) VALUES (?,?,0)", (pid, vid))
    conn.commit()


def test_list_is_numbered_in_a_stable_order_with_sync_state(pl):
    rows = list_playlists(pl)
    assert [(r["n"], r["title"], r["synced"]) for r in rows] == [
        (1, "Gym Classics", False), (2, "Gym Mix", False), (3, "Liked Music", True), (4, "Running", True)]


def test_tokens_by_number_exact_name_or_unique_part(pl):
    rows = list_playlists(pl)
    assert resolve_tokens(rows, "1, 4") == ["p3", "p1"]
    assert resolve_tokens(rows, "gym mix") == ["p2"]
    assert resolve_tokens(rows, "run") == ["p1"]
    assert resolve_tokens(rows, "") == []


def test_unknown_or_ambiguous_tokens_are_refused(pl):
    rows = list_playlists(pl)
    for bad, msg in [("9", "no playlist number"), ("Zumba", "no playlist named"), ("gym", "more than one")]:
        with pytest.raises(ValueError, match=msg):
            resolve_tokens(rows, bad)


def test_apply_starts_and_stops_and_a_bad_token_changes_nothing(pl):
    out = apply_selection(pl, "Gym Mix", "Running")
    assert out["started"] == ["Gym Mix"] and out["stopped"] == ["Running"]
    assert {r["title"] for r in list_playlists(pl) if r["synced"]} == {"Gym Mix", "Liked Music"}
    with pytest.raises(ValueError):
        apply_selection(pl, "Gym Classics", "nope")
    assert {r["title"] for r in list_playlists(pl) if r["synced"]} == {"Gym Mix", "Liked Music"}   # unchanged
    with pytest.raises(ValueError, match="both"):
        apply_selection(pl, "Gym Classics", "Gym Classics")


def test_stopping_reports_only_tracks_no_other_synced_playlist_keeps(pl):
    put(pl, "p1", "only_running")
    put(pl, "p1", "both")
    put(pl, "p4", "both")
    put(pl, "p1", "pending_one", status="pending")       # not downloaded: nothing to trash
    assert apply_selection(pl, "", "Running")["leaving"] == 1
    put(pl, "p2", "only_running")                        # now also in a playlist we start in the same call
    pl.execute("UPDATE playlists SET selected = 1 WHERE id = 'p1'")
    pl.commit()
    assert apply_selection(pl, "Gym Mix", "Running")["leaving"] == 0
