import shutil
import subprocess

import pytest

from runsync import db
from runsync.config import Config
from runsync.sync import reconcile

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def make_audio(path):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", str(path)],
                   check=True)


@pytest.fixture
def env(tmp_path):
    cfg = Config(library=tmp_path)
    cfg.ensure_dirs()
    return cfg, db.connect(cfg.db_path)


def test_missing_file_requeued(env):
    cfg, conn = env
    conn.execute("INSERT INTO tracks (video_id, status, master_path) VALUES ('a', 'downloaded', ?)",
                 (str(cfg.master / "a.m4a"),))
    out = reconcile(cfg, conn, apply=True)
    assert out["missing"] == 1
    assert conn.execute("SELECT status, master_path FROM tracks").fetchone()[:] == ("pending", None)
    assert conn.execute("SELECT event FROM track_events").fetchone()[0] == "file_missing"


def test_dry_run_changes_nothing(env):
    cfg, conn = env
    conn.execute("INSERT INTO tracks (video_id, status, master_path) VALUES ('a', 'downloaded', '/nope/a.m4a')")
    assert reconcile(cfg, conn, apply=False)["missing"] == 1
    assert conn.execute("SELECT status FROM tracks").fetchone()[0] == "downloaded"


def test_orphan_not_in_db_left_alone(env):
    cfg, conn = env
    (cfg.master / "stray.m4a").write_bytes(b"x")
    out = reconcile(cfg, conn, apply=True)
    assert out["orphans"] == 1 and out["adopted"] == 0
    assert (cfg.master / "stray.m4a").exists()


@needs_ffmpeg
def test_valid_file_for_pending_track_adopted(env):
    cfg, conn = env
    conn.execute("INSERT INTO tracks (video_id, status) VALUES ('abc', 'pending')")
    make_audio(cfg.master / "abc.m4a")
    out = reconcile(cfg, conn, apply=True)
    assert out["adopted"] == 1 and out["orphans"] == 0
    row = conn.execute("SELECT status, sha256, file_size_bytes FROM tracks").fetchone()
    assert row["status"] == "downloaded" and len(row["sha256"]) == 64 and row["file_size_bytes"] > 0


def test_tmp_and_hidden_files_ignored(env):
    cfg, conn = env
    (cfg.master / ".tmp").mkdir()
    (cfg.master / ".DS_Store").write_bytes(b"x")
    assert reconcile(cfg, conn, apply=True) == {"files": 0, "missing": 0, "adopted": 0, "orphans": 0}
