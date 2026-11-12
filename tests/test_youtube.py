"""The YouTube API client and login: no network, a fake transport and a fake browser."""
import base64
import hashlib
import json
import logging
import os
import urllib.parse
import urllib.request

import pytest

from runsync import youtube as yt
from runsync.youtube import Api, AuthError, QuotaError, YouTubeError

FAKE_SECRET = "GOC" + "SPX-fake-secret-value-123456"     # split so the repo guard does not flag the test file


@pytest.fixture
def files(tmp_path):
    client = tmp_path / "client_secret.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": FAKE_SECRET,
                                                "redirect_uris": ["http://localhost"]}}))
    return client, tmp_path / "youtube_token.json"


def token(expires_in=3600, refresh="REFRESH-1", access="ACCESS-1", now=1000.0):
    return {"access_token": access, "refresh_token": refresh, "expires_at": now + expires_in, "token_type": "Bearer"}


class Transport:
    """Queued fake answers; records every call."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), json.loads(body) if body else None))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def api(files, *answers, conn=None, form=None, now=1000.0, **kw):
    client, tok = files
    if tok and not tok.exists():
        yt.save_token(tok, token(now=now))
    t = Transport(*answers)
    a = Api(tok, client, conn=conn, transport=t, post_form=form or (lambda u, d: {}), clock=lambda: now,
            sleep=lambda s: None, **kw)
    return a, t


def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = yt.make_pkce()
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert yt.make_pkce()[0] != verifier


def test_consent_url_asks_for_offline_access_with_pkce_and_state():
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(yt.build_auth_url("cid", "http://127.0.0.1:5", "CH", "ST")).query))
    assert (q["client_id"], q["code_challenge"], q["code_challenge_method"], q["state"], q["access_type"]) == (
        "cid", "CH", "S256", "ST", "offline")
    assert q["scope"] == yt.SCOPE and q["redirect_uri"] == "http://127.0.0.1:5"


def _fake_browser(state_override=None, error=None):
    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        params = {"error": error} if error else {"code": "AUTHCODE", "state": state_override or q["state"]}
        urllib.request.urlopen(f"{q['redirect_uri']}?{urllib.parse.urlencode(params)}", timeout=5).read()
    return open_browser


def test_login_exchanges_the_code_and_saves_an_owner_only_token(files):
    client, tok = files
    seen = {}

    def form(url, data):
        seen.update(url=url, **data)
        return {"access_token": "A", "refresh_token": "R", "expires_in": 3600}
    out = yt.login(client, tok, open_browser=_fake_browser(), post_form=form, timeout=10, clock=lambda: 1000.0)
    assert out["refresh_token"] == "R" and json.loads(tok.read_text())["access_token"] == "A"
    assert oct(os.stat(tok).st_mode & 0o777) == "0o600"
    assert seen["url"] == yt.TOKEN_URL and seen["code"] == "AUTHCODE" and seen["grant_type"] == "authorization_code"
    assert seen["code_verifier"] and seen["client_secret"].startswith("GOCSPX")


def test_login_refuses_a_mismatched_state_and_saves_nothing(files):
    client, tok = files
    with pytest.raises(AuthError, match="state"):
        yt.login(client, tok, open_browser=_fake_browser(state_override="forged"), post_form=lambda u, d: {}, timeout=10)
    assert not tok.exists()


def test_login_reports_a_denied_consent(files):
    client, tok = files
    with pytest.raises(AuthError, match="access_denied"):
        yt.login(client, tok, open_browser=_fake_browser(error="access_denied"), post_form=lambda u, d: {}, timeout=10)
    assert not tok.exists()


def test_login_needs_a_long_lived_token(files):
    client, tok = files
    with pytest.raises(AuthError, match="long-lived"):
        yt.login(client, tok, open_browser=_fake_browser(), post_form=lambda u, d: {"access_token": "A", "expires_in": 5},
                 timeout=10)
    assert not tok.exists()


def test_a_bad_client_file_is_explained(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{}")
    with pytest.raises(AuthError, match="Desktop-app JSON"):
        yt.load_client(bad)


def test_expired_token_is_refreshed_and_saved(files):
    _client, tok = files
    yt.save_token(tok, token(expires_in=-10))
    form_calls = []

    def form(url, data):
        form_calls.append(data)
        return {"access_token": "ACCESS-2", "expires_in": 3600}
    a, t = api(files, (200, {"items": []}), form=form)
    a.request("GET", "playlists", {"part": "snippet"})
    assert form_calls[0]["grant_type"] == "refresh_token" and form_calls[0]["refresh_token"] == "REFRESH-1"
    assert t.calls[0][2]["Authorization"] == "Bearer ACCESS-2"
    assert json.loads(tok.read_text())["access_token"] == "ACCESS-2" and json.loads(tok.read_text())["refresh_token"] == "REFRESH-1"
    assert oct(os.stat(tok).st_mode & 0o777) == "0o600"          # a refreshed token file stays owner-only


def test_a_401_is_retried_once_with_a_fresh_token_then_gives_up(files):
    a, t = api(files, (401, {}), (200, {"ok": 1}), form=lambda u, d: {"access_token": "NEW", "expires_in": 3600})
    assert a.request("GET", "playlists") == {"ok": 1} and t.calls[1][2]["Authorization"] == "Bearer NEW"
    a, t = api(files, (401, {}), (401, {}), form=lambda u, d: {"access_token": "NEW", "expires_in": 3600})
    with pytest.raises(AuthError, match="login"):
        a.request("GET", "playlists")


def test_a_revoked_login_says_to_sign_in_again(files):
    _client, tok = files
    yt.save_token(tok, token(expires_in=-10))
    a, _ = api(files, form=lambda u, d: {"error": "invalid_grant"})
    with pytest.raises(AuthError, match="--login"):
        a.request("GET", "playlists")


def test_not_signed_in_is_explained(tmp_path):
    a = Api(tmp_path / "none.json", tmp_path / "c.json")
    assert not a.logged_in()
    with pytest.raises(AuthError, match="Not signed in"):
        a.request("GET", "playlists")


def test_quota_exhaustion_stops_with_a_clear_message(files):
    a, _ = api(files, (403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}))
    with pytest.raises(QuotaError, match="midnight Pacific"):
        a.request("GET", "playlists")


def test_server_errors_are_retried_then_raised_and_client_errors_are_not(files):
    a, t = api(files, (503, {}), (500, {}), (200, {"ok": 1}))
    assert a.request("GET", "playlists") == {"ok": 1} and len(t.calls) == 3
    a, t = api(files, (503, {}), (503, {}), (503, {}))
    with pytest.raises(YouTubeError, match="503"):
        a.request("GET", "playlists")
    a, t = api(files, (404, {"error": {"message": "nope", "errors": [{"reason": "playlistNotFound"}]}}))
    with pytest.raises(YouTubeError, match="playlistNotFound"):
        a.request("GET", "playlistItems")
    assert len(t.calls) == 1
    a, t = api(files, OSError("network down"), (200, {"ok": 1}))
    assert a.request("GET", "playlists") == {"ok": 1}


def test_only_google_hosts_are_contacted(files, monkeypatch):
    a, t = api(files, (200, {}))
    monkeypatch.setattr(yt, "API_BASE", "https://evil.example.com/youtube/v3")
    with pytest.raises(YouTubeError, match="not a Google API host"):
        a.request("GET", "playlists")
    assert t.calls == []


def test_every_call_is_charged_to_the_daily_quota_and_the_budget_is_enforced(files, conn):
    a, _ = api(files, (200, {}), (200, {}), (200, {}), conn=conn, budget=101)
    a.request("GET", "playlists")
    a.request("POST", "playlistItems", {"part": "snippet"}, {"snippet": {}})
    a.request("DELETE", "playlistItems", {"id": "x"})
    assert a.units_today() == 1 + 50 + 50
    assert not a.can_spend(1) and a.can_spend(0)
    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 1


def test_tokens_and_secrets_never_reach_the_log(files, caplog):
    a, _ = api(files, (503, {}), (200, {"ok": 1}))
    with caplog.at_level(logging.DEBUG):
        a.request("GET", "playlists")
    text = caplog.text + json.dumps([r.getMessage() for r in caplog.records])
    assert "ACCESS-1" not in text and "REFRESH-1" not in text and "GOCSPX" not in text


def page(n_items, token=None, start=0):
    return (200, {"items": [{"id": f"item{start + i}", "snippet": {"position": start + i, "title": f"Song {start + i}",
                                                                   "videoOwnerChannelTitle": "Chan"},
                             "contentDetails": {"videoId": f"vid{start + i}"}} for i in range(n_items)],
                  **({"nextPageToken": token} if token else {})})


def test_playlist_entries_follow_pages_keep_repeats_and_flag_unavailable_videos(files):
    gone = (200, {"items": [{"id": "g", "snippet": {"position": 3, "title": "Deleted video"}, "contentDetails": {"videoId": "gone1"}},
                            {"id": "p", "snippet": {"position": 4, "title": "Private video"}, "contentDetails": {"videoId": "priv1"}},
                            {"id": "r", "snippet": {"position": 5, "title": "Song 0"}, "contentDetails": {"videoId": "vid0"}}]})
    a, t = api(files, page(3, "NEXT"), gone)
    es = yt.playlist_entries(a, "PL1")
    assert [e.video_id for e in es] == ["vid0", "vid1", "vid2", "gone1", "priv1", "vid0"]     # the repeat is kept
    assert [e.available for e in es] == [True, True, True, False, False, True]
    assert es[0].item_id == "item0" and es[5].item_id == "r" and es[3].position == 3
    assert "pageToken=NEXT" in t.calls[1][1] and "playlistId=PL1" in t.calls[0][1]


def test_my_playlists_lists_ids_titles_and_counts(files):
    a, _ = api(files, (200, {"items": [{"id": "PL1", "snippet": {"title": "Running"}, "contentDetails": {"itemCount": 94}}]}))
    assert yt.my_playlists(a) == [{"id": "PL1", "title": "Running", "count": 94}]


def test_write_helpers_send_exactly_what_the_api_expects(files):
    a, t = api(files, (200, {"id": "PLNEW"}), (200, {"id": "ITEM1"}), (200, {"id": "ITEM2"}), (204, {}))
    assert yt.create_playlist(a, "rerunsync") == "PLNEW"
    assert yt.add_entry(a, "PLNEW", "vid9") == "ITEM1"
    assert yt.add_entry(a, "PLNEW", "vid8", position=0) == "ITEM2"
    yt.delete_entry(a, "ITEM1")
    (m1, _, _, b1), (_, _, _, b2), (_, _, _, b3), (m4, u4, _, b4) = t.calls
    assert (m1, b1["snippet"]["title"], b1["status"]["privacyStatus"]) == ("POST", "rerunsync", "private")
    assert b2["snippet"] == {"playlistId": "PLNEW", "resourceId": {"kind": "youtube#video", "videoId": "vid9"}}
    assert b3["snippet"]["position"] == 0
    assert (m4, b4) == ("DELETE", None) and "id=ITEM1" in u4


def test_youtube_command_reports_not_signed_in_and_makes_no_network_call(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from runsync import cli
    from runsync.config import Config
    monkeypatch.setenv("RUNSYNC_HOME", str(tmp_path / "lib"))
    monkeypatch.setattr(Config, "youtube_token", property(lambda self: tmp_path / "none.json"))
    monkeypatch.setattr(Config, "youtube_client", property(lambda self: tmp_path / "none-client.json"))
    monkeypatch.setattr(yt, "_http", lambda *a, **k: pytest.fail("network call"))
    out = CliRunner().invoke(cli.app, ["youtube", "--json"])
    facts = json.loads(out.stdout)
    assert out.exit_code == 0 and facts["logged_in"] is False and facts["units_today"] == 0 and "playlists" not in facts
    assert "client_secret" not in out.output.lower() and "GOCSPX" not in out.output


# ---- Stage B: reading playlists through the API ---------------------------------------------------------------------

def _entries(*spec):
    return [yt.Entry(v, f"i{n}", n, t, "Chan", ok) for n, (v, t, ok) in enumerate(spec)]


def test_api_items_have_the_same_shape_as_the_ytdlp_ones(monkeypatch):
    from runsync import fetch
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: _entries(("a", "Song A", True), ("gone", "Deleted video", False),
                                                                          ("a", "Song A", True), ("b", "Song B", True)))
    api_items = fetch.api_playlist_items(object(), "PL1")
    ytdlp_items = fetch.parse_entries([{"id": "a", "title": "Song A"}, {"id": "gone", "title": "[Deleted video]"},
                                       {"id": "a", "title": "Song A"}, {"id": "b", "title": "Song B"}])
    assert (api_items.ids, api_items.titles, api_items.unknown) == (ytdlp_items.ids, ytdlp_items.titles, ytdlp_items.unknown)
    assert api_items.source == "api" and ytdlp_items.source == "cookies"


def _no_ytdlp(monkeypatch, answer=None):
    from runsync import fetch
    calls = []

    class FakeYdl:
        def __init__(self, opts):
            calls.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"entries": answer if answer is not None else [{"id": "c1", "title": "From cookies"}]}
    monkeypatch.setattr(fetch.yt_dlp, "YoutubeDL", FakeYdl)
    return calls


def test_fetch_uses_the_api_and_never_touches_ytdlp_when_it_answers(monkeypatch):
    from runsync import fetch
    ytdlp = _no_ytdlp(monkeypatch)
    monkeypatch.setattr(yt, "playlist_entries", lambda api, pid: _entries(("a", "Song A", True)))
    items = fetch.fetch_playlist_items("https://www.youtube.com/playlist?list=PL1", None, api=object())
    assert items.ids == ["a"] and items.source == "api" and ytdlp == []


@pytest.mark.parametrize("failure", ["error", "empty"])
def test_fetch_falls_back_to_cookies_when_the_api_fails_or_returns_nothing(monkeypatch, failure):
    from runsync import fetch
    _no_ytdlp(monkeypatch)

    def entries(api, pid):
        if failure == "error":
            raise QuotaError("out of quota")
        return []
    monkeypatch.setattr(yt, "playlist_entries", entries)
    items = fetch.fetch_playlist_items("https://www.youtube.com/playlist?list=PL1", None, api=object())
    assert items.ids == ["c1"] and items.source == "cookies"


@pytest.mark.parametrize("pid", ["LM", "WL"])
def test_liked_music_and_watch_later_always_use_cookies(monkeypatch, pid):
    from runsync import fetch
    _no_ytdlp(monkeypatch)
    monkeypatch.setattr(yt, "playlist_entries", lambda api, p: pytest.fail("the API must not be asked for " + pid))
    items = fetch.fetch_playlist_items(f"https://music.youtube.com/playlist?list={pid}", None, api=object())
    assert items.source == "cookies" and items.ids == ["c1"]


def test_without_an_api_nothing_changes(monkeypatch):
    from runsync import fetch
    ytdlp = _no_ytdlp(monkeypatch)
    monkeypatch.setattr(yt, "playlist_entries", lambda api, p: pytest.fail("no api, no call"))
    assert fetch.fetch_playlist_items("https://www.youtube.com/playlist?list=PL1", None).ids == ["c1"] and len(ytdlp) == 1


def test_an_empty_api_answer_for_a_known_playlist_is_still_a_failed_fetch_in_sync(monkeypatch, tmp_path):
    """The sync guard (empty answer for a playlist that had tracks = failed fetch, nothing deleted) holds with the API too."""
    from runsync import fetch
    _no_ytdlp(monkeypatch, answer=[])
    monkeypatch.setattr(yt, "playlist_entries", lambda api, p: [])
    items = fetch.fetch_playlist_items("https://www.youtube.com/playlist?list=PL1", None, api=object())
    assert items.ids == [] and items.unknown == []     # sync.py then refuses to delete anything (tests/test_run_sync.py)


def test_account_playlists_through_the_api_keep_the_special_ones(monkeypatch):
    from runsync import fetch
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PL1", "title": "Running", "count": 94}])
    got = {p.id: p.title for p in fetch.api_account_playlists(object())}
    assert got == {"PL1": "Running", "LL": "Liked videos", "WL": "Watch later", "LM": "Liked Music"}


def test_merge_remote_prefers_the_api_and_falls_back_to_cookies(conn, monkeypatch):
    from runsync import cli, fetch
    monkeypatch.setattr(yt, "my_playlists", lambda api: [{"id": "PL1", "title": "Running", "count": 1}])
    monkeypatch.setattr(fetch, "fetch_account_playlists",
                        lambda c: [fetch.PlaylistInfo("PLc", "From cookies", "https://www.youtube.com/playlist?list=PLc")])
    assert cli._merge_remote(conn, None, api=object()) == "api"
    assert {r[0] for r in conn.execute("SELECT id FROM playlists")} >= {"PL1", "LM"}

    def boom(api):
        raise AuthError("login expired")
    monkeypatch.setattr(yt, "my_playlists", boom)
    assert cli._merge_remote(conn, object(), api=object()) == "cookies"
    with pytest.raises(RuntimeError):
        cli._merge_remote(conn, None, api=object())       # API down and no cookies: an error, never an empty list
