"""What `runsync youtube` does, as small functions that return facts (plain dicts) or raise.

The command only parses options and prints; the skills present the facts. Nothing here prints. Every write goes through
`ytclean.apply_plan`, which needs the id of the plan the owner confirmed.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import nullcontext

from . import snapshot, ytclean, ytsearch
from . import youtube as yt
from .config import Config
from .lock import run_lock
from .util import now_iso

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(value: str) -> str:
    """A YouTube video id, or PlanRefused: nothing else may reach yt-dlp or the API."""
    if not VIDEO_ID.match(value or ""):
        raise ytclean.PlanRefused(f"'{value}' is not a YouTube video id (11 letters, digits, - or _).")
    return value


def base_facts(cfg: Config, api: yt.Api) -> dict:
    return {"client_file": cfg.youtube_client.exists(), "logged_in": api.logged_in(), "budget": cfg.youtube_budget,
            "units_today": api.units_today()}


def search(query: str) -> dict:
    return {"query": query, "candidates": [ytsearch.as_dict(c) for c in ytsearch.candidates(query)]}


def replacements(conn: sqlite3.Connection) -> dict:
    """Lost songs the owner has not decided about yet, with search candidates for each (no quota)."""
    lost, nameless = ytsearch.lost_songs(conn)
    for song in lost:
        if song.in_library is None:
            song.candidates = ytsearch.candidates(ytsearch.query_for(song.title))
    return {"no_title": nameless, "decided": ytsearch.pick_counts(conn),
            "songs": [{"video_id": s.video_id, "ids": s.ids, "title": s.title, "reason": s.reason,
                       "in_library": s.in_library, "candidates": [ytsearch.as_dict(c) for c in s.candidates]}
                      for s in lost]}


def save_picks(conn: sqlite3.Connection, pick: str, skip: str) -> list[dict]:
    """Save the owner's answers (local, no quota). `pick` = "LOST:UPLOAD,..." and `skip` = "LOST,..."."""
    saved = []
    for item in (x.strip() for x in pick.split(",") if x.strip()):
        lost_id, _, upload = item.partition(":")
        if not (lost_id and upload):
            raise ytclean.PlanRefused("--pick needs LOSTID:UPLOADID.")
        video_id(lost_id), video_id(upload)
        saved.append({"lost": ytsearch.record_pick(conn, lost_id, upload, ytsearch.video_titles([upload])[upload]),
                      "pick": upload})
    for lost_id in (video_id(x.strip()) for x in skip.split(",") if x.strip()):
        saved.append({"lost": ytsearch.record_skip(conn, lost_id), "pick": None})
    return saved


def remove(conn: sqlite3.Connection, ids: str) -> list[str]:
    """Save "I want these songs gone" (local, no quota). The next --cleanup removes them from your playlists and lists the
    ones still liked for you to unlike by hand; the next sync then moves their files to .trash/ and forgets them."""
    wanted = [video_id(x.strip()) for x in ids.split(",") if x.strip()]
    for v in wanted:
        conn.execute("INSERT OR REPLACE INTO unwanted (video_id, decided_at) VALUES (?, ?)", (v, now_iso()))
    conn.commit()
    return wanted


def _items(r: ytclean.Applied) -> list[dict]:
    """What was really changed, one row per change (the skills list these)."""
    return [{"op": o.kind, "why": o.why, "playlist": r.playlists.get(o.playlist_id, o.playlist_id), "video_id": o.video_id,
             "title": o.title, "keep_id": o.keep_id, "keep_title": o.keep_title} for o in r.ops_done if o.kind != "create"]


def _applied(api: yt.Api, conn: sqlite3.Connection, r: ytclean.Applied, **counts) -> dict:
    """The result of a push: what ran, the read-back from YouTube, and what the database then forgot."""
    verify = ytclean.verify(api, r)
    return {**counts, "left": r.left, "stopped": r.stopped, "items": _items(r), "verify": verify,
            "forgotten": snapshot.settle_push(conn, r, verify["ok"])["purged"]}


def add(api: yt.Api, conn: sqlite3.Connection, cfg: Config, ids: list[str], to: str, create: bool, apply: bool,
        confirm: str, batch: int) -> dict:
    with run_lock(cfg.library) if apply else nullcontext():
        plan, skipped = ytclean.build_add_plan(api, conn, ids, to, create, ytsearch.video_titles(ids))
        facts = {"add": {"plan_id": ytclean.plan_id(plan), "playlist": next(iter(plan.playlists.values())),
                         "creates": any(o.kind == "create" for o in plan.ops), "cost": plan.cost,
                         "videos": [{"video_id": o.video_id, "title": o.title} for o in plan.ops if o.kind == "add"],
                         "skipped": skipped}}
        if apply:
            r = ytclean.apply_plan(api, conn, plan, confirm, batch)
            facts["applied"] = _applied(api, conn, r, added=r.added, created=r.created)
    return facts


def cleanup(api: yt.Api, conn: sqlite3.Connection, cfg: Config, apply: bool, confirm: str, batch: int,
            place_in: str = "") -> dict:
    with run_lock(cfg.library) if apply else nullcontext():
        target = None
        if place_in:
            found = ytclean.find_playlist(api, place_in)
            if found is None:
                raise ytclean.NoSuchPlaylist(f"No playlist called '{place_in}' for the replacements.")
            target = found[0]
        plan = ytclean.build_plan(api, conn, place_in=target)
        facts = {"cleanup": {**ytclean.summary(plan), "days": ytclean.days_needed(plan, cfg.youtube_budget), "batch": batch,
                             "items": [{"op": o.kind, "why": o.why, "playlist": plan.playlists[o.playlist_id],
                                        "video_id": o.video_id, "title": o.title, "keep_id": o.keep_id,
                                        "keep_title": o.keep_title} for o in plan.ops]}}
        if apply:
            r = ytclean.apply_plan(api, conn, plan, confirm, batch)
            facts["applied"] = _applied(api, conn, r, deleted=r.deleted, added=r.added)
    return facts


def undo(api: yt.Api, conn: sqlite3.Connection, cfg: Config) -> dict:
    with run_lock(cfg.library):
        r = ytclean.undo_last_run(api, conn)
    return {"undone": {"restored": r.restored, "removed": r.removed, "left": r.left, "stopped": r.stopped}}


def act(api: yt.Api, conn: sqlite3.Connection, cfg: Config, *, cleanup_: bool, apply: bool, confirm: str, batch: int,
        undo_: bool, add_: str, to: str, create: bool, status: bool = True, place_in: str = "") -> dict:
    """Run the one thing the owner asked for and return its facts. With nothing asked: the signed-in playlists."""
    if (cleanup_ or apply or undo_ or add_) and not api.logged_in():
        raise yt.AuthError(f"Not signed in to YouTube. {yt.LOGIN_HINT}")
    if add_:
        if not to:
            raise ytclean.PlanRefused("--add needs --to <playlist name or id>.")
        ids = [video_id(v.strip()) for v in add_.split(",") if v.strip()]
        facts = add(api, conn, cfg, ids, to, create, apply, confirm, batch)
    elif cleanup_ or apply:
        facts = cleanup(api, conn, cfg, apply, confirm, batch, place_in)
    elif undo_:
        facts = undo(api, conn, cfg)
    elif status and api.logged_in():
        facts = {"playlists": yt.my_playlists(api)}
    else:
        return {}
    facts["units_today"] = api.units_today()
    facts["picks"] = ytsearch.pick_counts(conn)
    return facts
