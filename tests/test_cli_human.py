"""What a person sees without --json: every command prints something sensible and exits cleanly."""
import pytest
from typer.testing import CliRunner

from runsync import cli, db
from runsync.config import load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNSYNC_HOME", str(tmp_path / "lib"))
    monkeypatch.setattr(cli.yt, "open_api", lambda cfg, conn=None: None)
    cfg = load_config()
    cfg.ensure_dirs()
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO playlists (id, title, url, selected) VALUES ('PL1', 'Running', 'u', 1), ('PL2', 'Gym', 'u', 0)")
    conn.execute("INSERT INTO tracks (video_id, title, status) VALUES ('a', 'A', 'downloaded'), ('b', 'B', 'pending')")
    conn.commit()
    return conn


def run(*args):
    out = CliRunner().invoke(cli.app, list(args))
    assert out.exception is None or isinstance(out.exception, SystemExit), repr(out.exception)
    return out


def test_status_says_where_things_stand_in_one_line(home):
    out = run("status")
    assert out.exit_code == 0 and "stopped, 1 waiting" in out.output and "1 of 2 tracks" in out.output
    home.execute("UPDATE tracks SET status = 'downloaded'")
    home.commit()
    assert "everything is downloaded" in run("status").output


def test_playlists_lists_them_numbered_with_what_syncs(home):
    out = run("playlists", "--stop", "2")        # numbered alphabetically: 1 Gym, 2 Running
    assert out.exit_code == 0 and "Running" in out.output and "Gym" in out.output
    assert "0 of 2 syncing" in out.output
    again = run("playlists", "--sync", "Gym")
    assert "1 of 2 syncing" in again.output


def test_playlists_without_any_login_says_it_is_the_saved_list(home):
    out = run("playlists")
    assert out.exit_code == 0 and "saved list, not live" in " ".join(out.output.split()) and "Running" in out.output


def test_playlists_with_nothing_saved_explains_what_to_do(home):
    home.execute("DELETE FROM playlists")
    home.commit()
    out = run("playlists")
    assert out.exit_code == 1 and "No playlists" in out.output


def test_card_without_a_registered_card_says_how_to_register(home):
    out = run("card")
    assert out.exit_code == 0 and "none registered" in " ".join(out.output.split()) and "--register" in out.output


def test_dedupe_with_nothing_to_do_says_so(home):
    assert "No duplicates" in run("dedupe").output


def test_youtube_not_signed_in_says_how_to_sign_in(home):
    out = run("youtube")
    assert out.exit_code == 0 and "Not signed in" in out.output and "--login" in out.output


def test_youtube_cleanup_when_not_signed_in_is_a_clear_error(home):
    out = run("youtube", "--cleanup")
    assert out.exit_code == 2 and "Not signed in" in out.output


def test_resolve_dry_run_reports_what_it_would_do(home):
    out = run("resolve", "--dry-run", "--offline")
    assert out.exit_code == 0 and "Would resolve" in out.output
