import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_sensitive.py"
ROOT = SCRIPT.parents[1]


def run(*files, cwd):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, files)], capture_output=True, text=True, cwd=cwd, check=False)


@pytest.mark.parametrize("name", ["cookies.txt", "yt-cookies-2026.txt", "state.db", "state.db-wal", "x.sqlite3",
                                  "state.db.bak-pre-v2", ".sync.lock", "logs/last_run.log", ".env", "a.m4a",
                                  "master/x"])
def test_sensitive_names_blocked(tmp_path, name):
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    assert run(name, cwd=tmp_path).returncode == 1


def test_renamed_cookie_file_caught_by_content(tmp_path):
    (tmp_path / "notes.txt").write_text("# Netscape HTTP Cookie File\n.youtube" + ".com\tTRUE\t/\tTRUE\t0\tSID\tv\n")
    assert run("notes.txt", cwd=tmp_path).returncode == 1


def test_renamed_sqlite_caught_by_content(tmp_path):
    (tmp_path / "data.dat").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    assert run("data.dat", cwd=tmp_path).returncode == 1


def test_api_key_caught(tmp_path):
    (tmp_path / "c.py").write_text("KEY = 'sk-ant-" + "a" * 30 + "'")
    assert run("c.py", cwd=tmp_path).returncode == 1


def test_normal_files_pass(tmp_path):
    (tmp_path / "ok.py").write_text("print('hi')")
    assert run("ok.py", cwd=tmp_path).returncode == 0


def test_repo_is_clean():
    """Nothing tracked or about-to-be-added in this repo may be sensitive."""
    assert run("--all", cwd=ROOT).returncode == 0


@pytest.mark.parametrize("path", ["cookies.txt", "state.db", "state.db-wal", "state.db-shm", "a.sqlite3",
                                  "state.db.bak-pre-v2", ".sync.lock", "logs/x.log", ".env", "master/a.m4a"])
def test_gitignore_covers(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert r.returncode == 0, f"{path} is not git-ignored"
