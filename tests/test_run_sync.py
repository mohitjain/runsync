"""End-to-end tests of run_sync: real SQLite + real files, fake network (fetch/download)."""
import pytest

from runsync import db, download, fetch, sync
from runsync.config import Config
from runsync.fetch import PlaylistItems
from runsync.retry import PermanentError


class World:
    """Fake YouTube: playlists -> ids, plus per-video download behaviour."""

    def __init__(self, tmp_path, monkeypatch):
        self.cfg = Config(library=tmp_path / "lib")
        self.cfg.pause_seconds = 0  # so sleeps below are only retry backoffs
        self.cfg.ensure_dirs()
        self.conn = db.connect(self.cfg.db_path)
        self.playlists: dict[str, object] = {}  # url -> list[str] | list[str] unknown tuple | Exception
        self.unknown: dict[str, list[str]] = {}
        self.behaviour: dict[str, object] = {}  # video id -> Exception to raise
        self.downloads: list[str] = []
        self.sleeps: list[float] = []
        monkeypatch.setattr(fetch, "fetch_playlist_items", self._fetch)
        monkeypatch.setattr(download, "download_best", self._download)
        monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: None)  # never really update yt-dlp

    def add_playlist(self, pid, ids, unknown=()):
        self.conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES (?, ?, ?, 1)", (pid, pid, pid))
        self.conn.commit()
        self.playlists[pid] = list(ids)
        self.unknown[pid] = list(unknown)

    def _fetch(self, url, cookies):
        val = self.playlists[url]
        if isinstance(val, Exception):
            raise val
        return PlaylistItems(ids=list(val), titles={v: f"Title {v}" for v in val}, unknown=self.unknown.get(url, []))

    def _download(self, vid, master, cookies, near_tie_ratio=0.10):
        err = self.behaviour.get(vid)
        if err:
            raise err
        self.downloads.append(vid)
        path = master / f"{vid}.m4a"
        path.write_bytes(b"audio-" + vid.encode())
        cols = {"itag": "141", "codec": "mp4a.40.2", "bitrate": 258.0, "master_path": str(path),
                "file_size_bytes": path.stat().st_size, "sha256": "0" * 64, "download_seconds": 0.1,
                "used_cookies": 0}
        return {"path": path, "itag": "141", "codec": "mp4a.40.2", "bitrate": 258.0, "columns": cols}

    def run(self, **kw):
        return sync.run_sync(self.cfg, self.conn, None, log=lambda m: None, sleep=self.sleeps.append, **kw)

    def status(self, vid):
        row = self.conn.execute("SELECT status FROM tracks WHERE video_id = ?", (vid,)).fetchone()
        return row["status"] if row else None

    def master_files(self):
        return sorted(p.name for p in self.cfg.master.iterdir() if p.is_file())

    def trash_files(self):
        return sorted(p.name for p in self.cfg.trash.iterdir())


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def ids(n, prefix="t"):
    return [f"{prefix}{i}" for i in range(n)]


def test_downloads_everything_then_rerun_downloads_nothing(world):
    world.add_playlist("P", ids(3))
    res = world.run()
    assert sorted(res.added) == ids(3) and world.master_files() == ["t0.m4a", "t1.m4a", "t2.m4a"]
    world.downloads.clear()
    res2 = world.run()
    assert world.downloads == [] and res2.added == [] and not res2.aborted


def test_empty_fetch_deletes_nothing(world):
    world.add_playlist("P", ids(5))
    world.run()
    world.playlists["P"] = []  # YouTube returns an empty list
    res = world.run()
    assert res.aborted and world.trash_files() == []
    assert all(world.status(v) == "downloaded" for v in ids(5)) and len(world.master_files()) == 5


def test_failed_fetch_of_one_playlist_disables_removals(world):
    world.add_playlist("P1", ids(5))
    world.add_playlist("P2", ["z0"])
    world.run()
    world.playlists["P1"] = RuntimeError("HTTP 503")
    world.playlists["P2"] = ["z1"]  # z0 dropped, but the fetch was incomplete
    world.run()
    assert world.status("z0") == "downloaded" and world.trash_files() == []


def test_mass_delete_guard_aborts_then_force_overrides(world):
    world.add_playlist("P", ids(10))
    world.run()
    world.playlists["P"] = ids(5)  # 50% would be removed
    res = world.run()
    assert res.aborted and "guard" in res.aborted and world.trash_files() == []
    assert len(world.master_files()) == 10
    res = world.run(force_delete=True)
    assert len(res.removed) == 5 and len(world.trash_files()) == 5 and len(world.master_files()) == 5
    assert world.status("t9") == "trashed"


def test_track_removed_from_all_playlists_goes_to_trash(world):
    world.add_playlist("P", ids(10))
    world.run()
    world.playlists["P"] = ids(9)  # t9 dropped: 10% <= 20% guard
    res = world.run()
    assert res.removed == ["t9"] and world.trash_files() == ["t9.m4a"] and "t9.m4a" not in world.master_files()
    row = world.conn.execute("SELECT status, trashed_at, trash_path FROM tracks WHERE video_id='t9'").fetchone()
    assert row["status"] == "trashed" and row["trashed_at"] and row["trash_path"].endswith("t9.m4a")


def test_track_still_in_another_selected_playlist_stays(world):
    world.add_playlist("P1", ids(10))
    world.add_playlist("P2", ["t0", "t1"])
    world.run()
    world.playlists["P1"] = ids(10)[1:]  # t0 leaves P1 but P2 still has it
    res = world.run()
    assert res.removed == [] and world.status("t0") == "downloaded" and world.trash_files() == []


def test_unreadable_item_is_never_removed(world):
    world.add_playlist("P", ids(10))
    world.run()
    world.playlists["P"] = ids(9)
    world.unknown["P"] = ["t9"]  # now private/unreadable, not gone
    res = world.run()
    assert res.removed == [] and world.status("t9") == "downloaded"


def test_permanent_error_marks_unavailable_without_retries(world):
    world.add_playlist("P", ids(2))
    world.behaviour["t0"] = PermanentError("Video unavailable. Removed for copyright")
    res = world.run()
    assert res.unavailable == ["t0"] and world.status("t0") == "unavailable" and world.status("t1") == "downloaded"
    assert 5 not in world.sleeps and 30 not in world.sleeps  # permanent errors are never retried
    assert world.conn.execute("SELECT last_error FROM tracks WHERE video_id='t0'").fetchone()[0].startswith("Video")


def test_transient_error_retries_three_times_then_stays_pending(world):
    world.add_playlist("P", ids(1))
    world.behaviour["t0"] = RuntimeError("HTTP Error 503")
    res = world.run()
    assert world.status("t0") == "pending" and [v for v, _ in res.failed] == ["t0"]
    assert world.sleeps == [5, 30, 120]
    world.behaviour.clear()
    world.run()  # next run succeeds
    assert world.status("t0") == "downloaded"


def test_per_run_cap_respected_and_next_run_continues(world):
    world.cfg.max_downloads_per_run = 2
    world.add_playlist("P", ids(5))
    assert len(world.run().added) == 2
    assert len(world.run().added) == 2
    assert len(world.run().added) == 1
    assert len(world.master_files()) == 5


def test_dry_run_writes_nothing(world):
    world.add_playlist("P", ids(3))
    res = world.run(dry_run=True)
    assert res.added == ids(3)
    assert world.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    assert world.master_files() == [] and world.downloads == []


def test_same_error_on_several_tracks_triggers_ytdlp_update_and_stops(world):
    world.add_playlist("P", ids(6))
    for v in ids(6):
        world.behaviour[v] = RuntimeError("Sign in to confirm you're not a bot")
    res = world.run()
    assert res.updated_ytdlp and "re-run" in res.aborted
    assert len(res.failed) == 3  # stopped after the third identical failure, not all six


def test_stale_partial_downloads_are_cleaned_on_startup(world):
    world.add_playlist("P", ids(1))
    tmp = world.cfg.master / ".tmp"
    tmp.mkdir()
    (tmp / "t0.m4a.part").write_bytes(b"half a file")
    world.run()
    assert list(tmp.glob("*")) == [] and world.master_files() == ["t0.m4a"]


def test_interrupt_mid_download_leaves_master_clean_and_resumes(world, monkeypatch):
    world.add_playlist("P", ids(2))
    real = world._download

    def killed(vid, master, cookies, near_tie_ratio=0.10):
        tmp = master / ".tmp"
        tmp.mkdir(exist_ok=True)
        (tmp / f"{vid}.m4a.part").write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(download, "download_best", killed)
    with pytest.raises(KeyboardInterrupt):
        world.run()
    assert world.master_files() == []  # nothing corrupt landed in master/
    monkeypatch.setattr(download, "download_best", real)
    res = world.run()
    assert sorted(res.added) == ids(2) and world.master_files() == ["t0.m4a", "t1.m4a"]
    assert list((world.cfg.master / ".tmp").glob("*")) == []


def test_file_deleted_by_hand_is_redownloaded(world):
    world.add_playlist("P", ids(2))
    world.run()
    (world.cfg.master / "t1.m4a").unlink()
    world.downloads.clear()
    world.run()
    assert world.downloads == ["t1"] and "t1.m4a" in world.master_files()


def test_result_dict_has_the_facts_the_skills_present(world):
    world.add_playlist("P", ids(3))
    world.behaviour["t1"] = PermanentError("Video unavailable")
    d = world.run().to_dict()
    assert (d["added"], d["unavailable"], d["removed"], d["failed"]) == (2, 1, 0, [])
    assert d["high_quality"] == 2 and d["standard_quality"] == 0 and d["opus"] == 0
    assert d["bytes"] > 0 and d["aborted"] is None
    world.add_playlist("Q", ids(2, "q"))
    world.behaviour["q0"] = RuntimeError("boom")
    assert world.run().to_dict()["failed"] == [{"id": "q0", "reason": "boom"}]
