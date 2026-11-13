"""The official YouTube Data API v3: OAuth login, a small client, and a quota ledger. Standard library only.

Safety model
- Only Google hosts are ever contacted (allow-list); tokens and the client secret are never logged or printed.
- The saved login (youtube_token.json) is written with mode 0600 and is gitignored and blocked by the sensitive-file guard.
- Every call is charged to a daily quota ledger; callers can refuse a plan that would exceed the budget.
- Writes are gated elsewhere (plan id + --confirm); this module only knows how to read and, when asked, edit playlists.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

log = logging.getLogger("runsync.youtube")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube"
ALLOWED_HOSTS = {"www.googleapis.com", "oauth2.googleapis.com", "accounts.google.com"}
UNAVAILABLE_TITLES = {"deleted video", "private video"}
# Quota cost in units per call: reads are 1, any write is 50 (YouTube's published costs).
COST = {"GET": 1, "POST": 50, "PUT": 50, "DELETE": 50}
PACIFIC = ZoneInfo("America/Los_Angeles")   # the quota resets at midnight Pacific
LOGIN_HINT = "Run `runsync youtube --login` to sign in again."


class YouTubeError(Exception):
    """Anything the API or the login flow refused."""


class AuthError(YouTubeError):
    """No usable login: sign in again."""


class QuotaError(YouTubeError):
    """The daily quota is used up."""


# ---- OAuth login (installed-app flow with PKCE and a loopback redirect) --------------------------------------------

def load_client(path: Path) -> tuple[str, str]:
    """(client_id, client_secret) from the file Google lets you download for a Desktop app."""
    try:
        data = json.loads(Path(path).read_text())
        c = data.get("installed") or data["web"]
        return c["client_id"], c["client_secret"]
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise AuthError(f"cannot read the Google client file {path}: it should be the Desktop-app JSON you downloaded") from exc


def make_pkce() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
         "access_type": "offline", "prompt": "consent"}
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def save_token(path: Path, token: dict) -> None:
    """Owner-only file, written atomically. The tokens never appear in logs."""
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(token, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_token(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Google's API does not redirect. If anything ever does, stop: urllib would resend the Authorization header to the
    new host, which the host allow-list never checked."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _OPENER.open(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except ValueError:
            return {"error": f"http_{exc.code}"}


def _token_from(resp: dict, previous_refresh: str | None, now: float) -> dict:
    if "access_token" not in resp:
        why = resp.get("error_description") or resp.get("error") or "no access token"
        raise AuthError(f"Google refused the login ({why}). {LOGIN_HINT}")
    refresh = resp.get("refresh_token") or previous_refresh
    if not refresh:
        raise AuthError("Google did not give a long-lived login; try again and approve every permission.")
    return {"access_token": resp["access_token"], "refresh_token": refresh, "token_type": resp.get("token_type", "Bearer"),
            "expires_at": now + int(resp.get("expires_in", 3600)), "scope": resp.get("scope", SCOPE)}


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: ClassVar[dict] = {}

    def do_GET(self) -> None:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        type(self).result.update(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body style='font-family:sans-serif'><h2>runsync is signed in.</h2>"
                         b"<p>You can close this tab and go back to the terminal.</p></body></html>")

    def log_message(self, *args) -> None:   # never log the redirect (it carries the code)
        return


def login(client_path: Path, token_path: Path, open_browser: Callable[[str], object] = webbrowser.open,
          post_form: Callable[[str, dict], dict] | None = None, timeout: float = 300,
          clock: Callable[[], float] = time.time, announce: Callable[[str], None] = lambda url: None) -> dict:
    """Sign in once in the browser. Returns the saved token (never print it)."""
    post_form = post_form or _post_form
    client_id, client_secret = load_client(client_path)
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    handler = type("Catcher", (_Catcher,), {"result": {}})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = build_auth_url(client_id, redirect, challenge, state)
    announce(url)
    stop = threading.Event()

    def serve() -> None:
        end = time.monotonic() + timeout
        while not stop.is_set() and time.monotonic() < end and "code" not in handler.result \
                and "error" not in handler.result:
            server.handle_request()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        open_browser(url)
        t.join(timeout + 2)
    finally:
        stop.set()
        server.server_close()
    got = handler.result
    if "error" in got:
        raise AuthError(f"Google said: {got['error']}. {LOGIN_HINT}")
    if "code" not in got:
        raise AuthError(f"No answer from the browser within {int(timeout)} seconds. {LOGIN_HINT}")
    if not secrets.compare_digest(got.get("state", ""), state):
        raise AuthError("The login answer did not match this request (state check failed); nothing was saved.")
    resp = post_form(TOKEN_URL, {"code": got["code"], "client_id": client_id, "client_secret": client_secret,
                                 "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
    token = _token_from(resp, None, clock())
    save_token(token_path, token)
    return token


# ---- the client ------------------------------------------------------------------------------------------------

def _http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {}


def hours_until_reset(now: float | None = None) -> str:
    """\"in about 11 h\" until the next midnight Pacific, when Google resets the daily quota."""
    here = datetime.fromtimestamp(now, PACIFIC) if now else datetime.now(PACIFIC)
    reset = (here + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max(1, round((reset - here).total_seconds() / 3600))
    return f"in about {hours} h"


def quota_day(now: datetime | None = None) -> str:
    return (now or datetime.now(PACIFIC)).astimezone(PACIFIC).strftime("%Y-%m-%d")


class Api:
    """Authenticated calls to googleapis.com. `transport` and `post_form` are injectable for tests."""

    def __init__(self, token_path: Path, client_path: Path, conn: sqlite3.Connection | None = None,
                 budget: int = 8000, transport: Callable | None = None, post_form: Callable | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 retry_delays: tuple[float, ...] = (2, 10)) -> None:
        self.token_path, self.client_path, self.conn, self.budget = Path(token_path), Path(client_path), conn, budget
        self.transport, self.post_form = transport or _http, post_form or _post_form   # looked up at call time (tests patch them)
        self.clock, self.sleep, self.retry_delays = clock, sleep, retry_delays
        self._token: dict | None = None

    # -- auth
    def logged_in(self) -> bool:
        return bool((self._token or load_token(self.token_path) or {}).get("refresh_token"))

    def _access_token(self, force: bool = False) -> str:
        tok = self._token or load_token(self.token_path)
        if not tok or not tok.get("refresh_token"):
            raise AuthError(f"Not signed in to YouTube. {LOGIN_HINT}")
        if force or tok.get("expires_at", 0) - self.clock() < 60:
            client_id, client_secret = load_client(self.client_path)
            resp = self.post_form(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret,
                                              "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
            if resp.get("error") == "invalid_grant":
                raise AuthError(f"Your YouTube login expired or was revoked. {LOGIN_HINT}")
            tok = _token_from(resp, tok["refresh_token"], self.clock())
            save_token(self.token_path, tok)
        self._token = tok
        return tok["access_token"]

    # -- quota ledger
    def units_today(self) -> int:
        if not self.conn:
            return 0
        row = self.conn.execute("SELECT units FROM api_usage WHERE day = ?", (quota_day(),)).fetchone()
        return row["units"] if row else 0

    def can_spend(self, units: int) -> bool:
        return self.units_today() + units <= self.budget

    def _charge(self, units: int) -> None:
        if self.conn:
            self.conn.execute("INSERT INTO api_usage (day, units) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET "
                              "units = units + excluded.units", (quota_day(), units))
            self.conn.commit()

    # -- calls
    def request(self, method: str, resource: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = f"{API_BASE}/{resource}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        if urllib.parse.urlparse(url).hostname not in ALLOWED_HOSTS:
            raise YouTubeError("refusing to contact a host that is not a Google API host")
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self._access_token(force=refreshed)}", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            self._charge(COST.get(method, 1))
            try:
                status, payload = self.transport(method, url, headers, data)
            except OSError as exc:   # network down, timeout
                status, payload = 599, {"error": {"message": str(exc)}}
            if status < 300:
                return payload
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            reason = (err.get("errors") or [{}])[0].get("reason", "") if isinstance(err, dict) else ""
            if status == 401 and not refreshed:
                refreshed = True
                continue
            if status == 401:
                raise AuthError(f"YouTube rejected the login. {LOGIN_HINT}")
            if status == 403 and reason in ("quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded"):
                raise QuotaError("The YouTube API quota for today is used up. It resets at midnight Pacific time "
                                 f"({hours_until_reset(self.clock())}); nothing more was changed.")
            if status >= 500 and attempt < len(self.retry_delays):
                self.sleep(self.retry_delays[attempt])
                attempt += 1
                continue
            msg = err.get("message") if isinstance(err, dict) else None
            raise YouTubeError(f"YouTube answered {status}" + (f": {msg}" if msg else "") + (f" ({reason})" if reason else ""))


# ---- reading ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    video_id: str
    item_id: str        # the playlist entry's own id: what a delete needs
    position: int
    title: str
    owner: str
    available: bool


def my_playlists(api: Api) -> list[dict]:
    """[{id, title, count}] for the playlists you own."""
    out, token = [], None
    while True:
        params = {"part": "snippet,contentDetails", "mine": "true", "maxResults": 50}
        if token:
            params["pageToken"] = token
        d = api.request("GET", "playlists", params)
        out += [{"id": i["id"], "title": i["snippet"]["title"], "count": i["contentDetails"]["itemCount"]}
                for i in d.get("items", [])]
        token = d.get("nextPageToken")
        if not token:
            return out


def playlist_entries(api: Api, playlist_id: str) -> list[Entry]:
    """Every entry of a playlist, in order (repeats included). Deleted/private videos have available=False."""
    out, token = [], None
    while True:
        params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        d = api.request("GET", "playlistItems", params)
        for i in d.get("items", []):
            sn = i.get("snippet", {})
            vid = (i.get("contentDetails") or {}).get("videoId") or (sn.get("resourceId") or {}).get("videoId")
            if not vid:
                continue
            title = sn.get("title") or ""
            out.append(Entry(vid, i["id"], int(sn.get("position", len(out))), title,
                             sn.get("videoOwnerChannelTitle") or "", title.strip().lower() not in UNAVAILABLE_TITLES))
        token = d.get("nextPageToken")
        if not token:
            return out


# ---- writing (callers gate these with a previewed plan) -------------------------------------------------------------

def create_playlist(api: Api, title: str, private: bool = True) -> str:
    d = api.request("POST", "playlists", {"part": "snippet,status"},
                    {"snippet": {"title": title}, "status": {"privacyStatus": "private" if private else "unlisted"}})
    return d["id"]


def add_entry(api: Api, playlist_id: str, video_id: str, position: int | None = None) -> str:
    sn: dict = {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}
    if position is not None:
        sn["position"] = position
    try:
        return api.request("POST", "playlistItems", {"part": "snippet"}, {"snippet": sn})["id"]
    except YouTubeError as exc:
        if position is None or "manualSortRequired" not in str(exc):
            raise
        # The playlist is sorted automatically (for example by date added), so it has no positions: add at the default place.
        sn.pop("position")
        return api.request("POST", "playlistItems", {"part": "snippet"}, {"snippet": sn})["id"]


def delete_entry(api: Api, item_id: str) -> None:
    api.request("DELETE", "playlistItems", {"id": item_id})


def open_api(cfg, conn: sqlite3.Connection | None = None) -> Api | None:
    """An Api for this library when the owner has signed in, else None (callers then use cookies)."""
    api = Api(cfg.youtube_token, cfg.youtube_client, conn, cfg.youtube_budget)
    return api if api.logged_in() and Path(cfg.youtube_client).exists() else None
