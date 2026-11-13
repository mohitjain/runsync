"""Clean duplicates out of your YouTube playlists (the official API).

Four kinds of clutter: the same video listed again and again (repeats), the losing upload of a song that `dedupe`
merged (a loser), a song YouTube no longer serves (dead), and a dead song the owner picked a replacement for. Where the kept
upload or the replacement is missing from a playlist, the plan adds it first and then removes the old entry, so a song never
silently vanishes from a playlist. A dead song with no replacement is simply removed.

Same pattern as dedupe and the card: `build_plan` only reads, `apply_plan` needs the id of the exact plan the owner
confirmed, every change is journalled so `undo_last_run` can reverse it. Liked Music, Liked videos and Watch later are
never written. No printing here: the CLI and the skills present the results.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from . import youtube as yt
from .dedupe import winner_of
from .util import now_iso

NEVER_WRITE = {"LM", "LL", "WL"}      # the API cannot or must not touch these: likes are your taste, not clutter
WRITE_COST = yt.COST["POST"]           # an insert and a delete both cost 50 units
DEFAULT_BATCH = 100


class PlanRefused(Exception):
    """Applying was refused (wrong or missing plan id, or a forbidden playlist). Nothing was written."""


@dataclass(frozen=True)
class Op:
    kind: str            # "add" | "delete" | "create"
    playlist_id: str
    video_id: str
    item_id: str         # the playlist entry to delete ("" for an add)
    position: int
    why: str             # "repeat" | "loser" | "swap"
    title: str = ""
    keep_id: str = ""    # for loser/swap: the upload that stays (or is added)
    keep_title: str = ""


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)       # adds first, then deletes: any prefix is safe to run
    playlists: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)  # entries each playlist had when the plan was made
    unmatched: int = 0     # dead or replaced songs found in none of your playlists (Liked Music, or already gone)
    manual: list[dict] = field(default_factory=list)   # still liked: the owner unlikes these by hand (video_id, title, why, url)

    @property
    def adds(self) -> int:
        return sum(o.kind == "add" for o in self.ops)

    @property
    def deletes(self) -> int:
        return sum(o.kind == "delete" for o in self.ops)

    @property
    def cost(self) -> int:
        return len(self.ops) * WRITE_COST


def plan_id(plan: Plan) -> str:
    """Names exactly which playlist entries a plan would delete and which videos it would add."""
    lines = sorted(f"{o.kind}:{o.playlist_id}:{o.video_id}:{o.item_id}" for o in plan.ops)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def plan_playlist(pid: str, entries: list[yt.Entry], winner: Callable[[str], str],
                  title_of: Callable[[str], str] = lambda v: "", picks: dict[str, str] | None = None,
                  dead: set[str] | None = None, unwanted: set[str] | None = None) -> list[Op]:
    picks, dead, unwanted = picks or {}, dead or set(), unwanted or set()
    first: dict[str, yt.Entry] = {}
    repeats: list[yt.Entry] = []
    for e in entries:
        if e.video_id in first:
            repeats.append(e)
        else:
            first[e.video_id] = e
    present = set(first)
    adds: list[Op] = []
    deletes: list[Op] = []
    added: set[str] = set()
    for e in sorted(first.values(), key=lambda x: x.position):
        if e.video_id in picks:                        # a dead song with a chosen replacement: put it in place
            pick = picks[e.video_id]
            if pick not in present and pick not in added:
                adds.append(Op("add", pid, pick, "", e.position, "replace", title_of(pick), pick, title_of(pick)))
                added.add(pick)
            deletes.append(Op("delete", pid, e.video_id, e.item_id, e.position, "replace",
                              title_of(e.video_id) or e.title, pick, title_of(pick)))
            continue
        if e.video_id in unwanted:                     # the owner wants this song gone
            deletes.append(Op("delete", pid, e.video_id, e.item_id, e.position, "unwanted", title_of(e.video_id) or e.title))
            continue
        if e.video_id in dead or not e.available:      # dead and nothing to replace it with: delete it
            deletes.append(Op("delete", pid, e.video_id, e.item_id, e.position, "dead", title_of(e.video_id) or e.title))
            continue
        w = winner(e.video_id)
        if w == e.video_id:
            continue
        if w in present or w in added:
            deletes.append(Op("delete", pid, e.video_id, e.item_id, e.position, "loser", e.title, w, first[w].title if w in first else title_of(w)))
        else:
            adds.append(Op("add", pid, w, "", e.position, "swap", title_of(w), w, title_of(w)))
            deletes.append(Op("delete", pid, e.video_id, e.item_id, e.position, "swap", e.title, w, title_of(w)))
            added.add(w)
    deletes += [Op("delete", pid, e.video_id, e.item_id, e.position, "repeat", e.title, e.video_id, e.title) for e in repeats]
    return adds + deletes


def build_plan(api: yt.Api, conn: sqlite3.Connection, only: set[str] | None = None, place_in: str | None = None) -> Plan:
    """Read your owned, selected playlists live and work out what to clean. Writes nothing. `place_in` is a playlist id:
    replacements whose dead song is in none of your playlists (it lives in Liked Music) are added there."""
    owned = {p["id"]: p["title"] for p in yt.my_playlists(api)}
    selected = {r["id"] for r in conn.execute("SELECT id FROM playlists WHERE selected = 1")}
    plan = Plan()
    titles = {r["video_id"]: r["title"] or "" for r in conn.execute("SELECT video_id, title FROM tracks")}
    picks = {r["lost_id"]: r["pick_id"] for r in conn.execute(
        "SELECT lost_id, pick_id FROM replacement_picks WHERE status = 'picked' AND pick_id IS NOT NULL")}
    titles.update({r["pick_id"]: r["title"] or "" for r in conn.execute(
        "SELECT pick_id, title FROM replacement_picks WHERE pick_id IS NOT NULL")})
    dead = {r["video_id"] for r in conn.execute("SELECT video_id FROM tracks WHERE status = 'unavailable'")} - set(picks)
    unwanted = {r["video_id"] for r in conn.execute("SELECT video_id FROM unwanted")}
    present: dict[str, set[str]] = {}
    for pid, title in owned.items():
        if pid in NEVER_WRITE or pid not in selected or (only is not None and pid not in only):
            continue
        plan.playlists[pid] = title
        live = yt.playlist_entries(api, pid)
        plan.counts[pid] = len(live)
        present[pid] = {e.video_id for e in live}
        plan.ops += plan_playlist(pid, live, lambda v: winner_of(conn, v), lambda v: titles.get(v, ""), picks, dead, unwanted)
    placed = _place_replacements(plan, picks, titles, present, place_in)
    plan.ops.sort(key=lambda o: o.kind != "add")           # every add before any delete; sort is stable
    handled = {o.video_id for o in plan.ops if o.kind == "delete" and o.why in ("dead", "replace")}
    unmatched = (dead | set(picks)) - handled - placed
    plan.unmatched = len(unmatched)
    # Liked Music cannot be edited through the API: what is still liked there is for the owner to unlike by hand
    check = sorted(unwanted | unmatched | placed)
    liked = yt.ratings(api, check) if check else {}
    plan.manual = [{"video_id": v, "title": titles.get(v) or "(unavailable video: grey in Liked Music)", "why": "unwanted" if v in unwanted else "dead",
                    "url": f"https://music.youtube.com/watch?v={v}"} for v in check if liked.get(v) == "like"]
    return plan


def _place_replacements(plan: Plan, picks: dict[str, str], titles: dict[str, str], present: dict[str, set[str]],
                        place_in: str | None) -> set[str]:
    """Add the chosen replacements whose dead song is in none of your playlists to `place_in`. Returns the lost ids placed."""
    if not place_in:
        return set()
    if place_in not in present:
        raise NoSuchPlaylist("The playlist for replacements is not one of your synced playlists.")
    in_place = {o.video_id for o in plan.ops if o.kind == "delete" and o.why == "replace"}
    waiting = {lost: pick for lost, pick in picks.items() if lost not in in_place}
    for pick in dict.fromkeys(waiting.values()):
        if pick not in present[place_in] and not any(o.kind == "add" and o.video_id == pick and o.playlist_id == place_in
                                                    for o in plan.ops):
            plan.ops.append(Op("add", place_in, pick, "", 0, "replace", titles.get(pick, ""), pick, titles.get(pick, "")))
    return set(waiting)


class NoSuchPlaylist(Exception):
    pass


def find_playlist(api: yt.Api, name_or_id: str) -> tuple[str, str] | None:
    """One of your own playlists by id, exact name or a name part that matches exactly one. None when there is none."""
    mine = yt.my_playlists(api)
    want = name_or_id.strip().casefold()
    hit = [p for p in mine if p["id"] == name_or_id.strip()] or [p for p in mine if p["title"].casefold() == want] \
        or [p for p in mine if want and want in p["title"].casefold()]
    if len(hit) > 1:
        raise NoSuchPlaylist(f"'{name_or_id}' matches {len(hit)} playlists: {', '.join(p['title'] for p in hit)}")
    return (hit[0]["id"], hit[0]["title"]) if hit else None


def build_add_plan(api: yt.Api, conn: sqlite3.Connection, video_ids: list[str], playlist: str, create: bool = False,
                   titles: dict[str, str] | None = None) -> tuple[Plan, list[dict]]:
    """Add songs to one of your playlists (made first when it does not exist and `create` is set).
    Returns the plan and the ids skipped because the playlist already has them. Writes nothing."""
    titles = titles or {}
    found = find_playlist(api, playlist)
    plan, skipped = Plan(), []
    if found is None:
        if not create:
            raise NoSuchPlaylist(f"No playlist called '{playlist}'. Say to create it, or pick one of: "
                                 + ", ".join(p["title"] for p in yt.my_playlists(api)))
        pid, title, have = f"new:{playlist.strip()}", playlist.strip(), set()
        plan.ops.append(Op("create", pid, "", "", 0, "create", title))
        plan.counts[pid] = 0
    else:
        pid, title = found
        if pid in NEVER_WRITE:
            raise NoSuchPlaylist(f"'{title}' cannot be changed from here.")
        live = yt.playlist_entries(api, pid)
        have = {e.video_id for e in live}
        plan.counts[pid] = len(live)
    plan.playlists[pid] = title
    for v in dict.fromkeys(video_ids):
        if v in have:
            skipped.append({"video_id": v, "why": "already in the playlist"})
        else:
            plan.ops.append(Op("add", pid, v, "", 0, "add", titles.get(v, ""), v, titles.get(v, "")))
    return plan, skipped


def summary(plan: Plan) -> dict:
    per = {}
    for pid, title in plan.playlists.items():
        mine = [o for o in plan.ops if o.playlist_id == pid]
        per[pid] = {"id": pid, "title": title, "repeats": sum(o.why == "repeat" for o in mine),
                    "losers": sum(o.why == "loser" for o in mine), "swaps": sum(o.why == "swap" and o.kind == "add" for o in mine),
                    "dead": sum(o.why == "dead" for o in mine), "unwanted": sum(o.why == "unwanted" and o.kind == "delete" for o in mine),
                    "replaced": sum(o.why == "replace" and o.kind == "delete" for o in mine)}
    return {"plan_id": plan_id(plan), "playlists": list(per.values()), "ops": len(plan.ops), "adds": plan.adds,
            "deletes": plan.deletes, "cost": plan.cost, "unmatched": plan.unmatched,
            "manual": plan.manual}


def days_needed(plan: Plan, budget: int) -> int:
    return max(1, math.ceil(plan.cost / max(budget - 200, WRITE_COST))) if plan.ops else 0   # 200 units kept for reads


# ---- applying ---------------------------------------------------------------------------------------------------------

@dataclass
class Applied:
    run_id: int
    done: int = 0
    deleted: int = 0
    added: int = 0
    created: int = 0
    left: int = 0
    stopped: str = ""        # why it stopped early ("" = finished the batch)
    ops_done: list[Op] = field(default_factory=list)       # what really happened, with real playlist ids
    before: dict[str, int] = field(default_factory=dict)   # entries per playlist before the run
    playlists: dict[str, str] = field(default_factory=dict)
    live: dict[str, list[yt.Entry]] = field(default_factory=dict)   # the playlists as read back after the writes


def _journal(conn: sqlite3.Connection, run_id: int, op: str, o: Op, item_id: str) -> None:
    conn.execute("INSERT INTO youtube_journal (run_id, op, playlist_id, video_id, playlist_item_id, position) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (run_id, op, o.playlist_id, o.video_id, item_id, o.position))
    conn.commit()


def _select_playlist(conn: sqlite3.Connection, playlist_id: str, title: str) -> None:
    """A playlist made here is synced from now on."""
    conn.execute("INSERT INTO playlists (id, title, url, kind, selected) VALUES (?, ?, ?, 'account', 1) "
                 "ON CONFLICT(id) DO UPDATE SET selected = 1",
                 (playlist_id, title, f"https://www.youtube.com/playlist?list={playlist_id}"))
    conn.commit()


def _execute(api: yt.Api, conn: sqlite3.Connection, run_id: int, o: Op, made: dict[str, str], out: Applied) -> None:
    """Make one change on YouTube and note it in the journal."""
    if o.playlist_id in made:
        o = replace(o, playlist_id=made[o.playlist_id])
    if o.kind == "create":
        real = yt.create_playlist(api, o.title)
        made[o.playlist_id] = real
        o = replace(o, playlist_id=real)
        out.before[real], out.playlists[real] = 0, o.title
        _journal(conn, run_id, "create", o, "")
        _select_playlist(conn, real, o.title)
        out.created += 1
    elif o.kind == "add":
        _journal(conn, run_id, "add", o, yt.add_entry(api, o.playlist_id, o.video_id, position=o.position or None))
        out.added += 1
    else:
        yt.delete_entry(api, o.item_id)
        _journal(conn, run_id, "delete", o, o.item_id)
        out.deleted += 1
    out.ops_done.append(o)
    out.done += 1


def apply_plan(api: yt.Api, conn: sqlite3.Connection, plan: Plan, confirm: str, batch: int = DEFAULT_BATCH) -> Applied:
    """Run the first `batch` operations of the confirmed plan. Refuses unless `confirm` is this exact plan's id."""
    if not confirm or confirm != plan_id(plan):
        raise PlanRefused(f"Not confirmed: --apply needs --confirm {plan_id(plan)} (the id of the previewed plan). "
                          "A plan that changed since you confirmed it is refused. Nothing written.")
    if any(o.playlist_id in NEVER_WRITE for o in plan.ops):
        raise PlanRefused("A plan may not touch Liked Music, Liked videos or Watch later. Nothing written.")
    run_id = conn.execute("INSERT INTO youtube_runs (started_at, status, plan_id) VALUES (?, 'running', ?)",
                          (now_iso(), plan_id(plan))).lastrowid
    conn.commit()
    out = Applied(run_id=run_id, before=dict(plan.counts), playlists=dict(plan.playlists))
    made: dict[str, str] = {}
    status = "interrupted"            # what the run is recorded as if something unexpected ends it
    try:
        for o in plan.ops[:max(0, batch)]:
            if not api.can_spend(WRITE_COST):
                out.stopped = f"daily quota budget reached ({api.budget} units); the rest waits for tomorrow"
                break
            _execute(api, conn, run_id, o, made, out)
        status = "stopped" if out.stopped else "done"
    except yt.YouTubeError as exc:
        out.stopped, status = str(exc), "stopped"
    finally:
        out.left = len(plan.ops) - out.done
        conn.execute("UPDATE youtube_runs SET status = ?, finished_at = ? WHERE id = ?", (status, now_iso(), run_id))
        conn.commit()
    return out


def _problems(ops: list[Op], live: dict[str, list[yt.Entry]]) -> list[dict]:
    items = {pid: {e.item_id for e in es} for pid, es in live.items()}
    videos = {pid: Counter(e.video_id for e in es) for pid, es in live.items()}
    out = []
    for o in ops:
        if o.kind == "create":
            continue
        why = ""
        if o.kind == "add" and o.video_id not in videos[o.playlist_id]:
            why = "the song is not in the playlist"
        elif o.kind == "delete":
            if o.item_id in items[o.playlist_id]:
                why = "the entry is still in the playlist"
            elif o.why in ("loser", "swap", "replace") and o.keep_id not in videos[o.playlist_id]:
                why = "the kept upload is missing from the playlist"
            elif o.why == "repeat" and videos[o.playlist_id][o.video_id] < 1:
                why = "no copy of the song is left in the playlist"
        if why:
            out.append({"op": o.kind, "why": o.why, "playlist_id": o.playlist_id, "video_id": o.video_id,
                        "title": o.title, "problem": why})
    return out


def verify(api: yt.Api, applied: Applied, waits: tuple[float, ...] = (3, 8, 15),
          sleep: Callable[[float], None] | None = None) -> dict:
    """Read the touched playlists back from YouTube and check every change that was made. YouTube's lists lag for a few
    seconds after a write, so a mismatch is re-read a few times before it is reported."""
    ops = applied.ops_done
    if not ops:
        return {"checked": True, "ok": True, "confirmed": 0, "problems": [], "playlists": []}
    pids = sorted({o.playlist_id for o in ops})
    problems: list[dict] = []
    live: dict[str, list[yt.Entry]] = {}
    for attempt in range(len(waits) + 1):
        try:
            live = {pid: yt.playlist_entries(api, pid) for pid in pids}
        except yt.YouTubeError as exc:
            return {"checked": False, "ok": False, "reason": str(exc), "confirmed": 0, "problems": [], "playlists": []}
        problems = _problems(ops, live)
        expected = {pid: applied.before.get(pid, 0) + sum(o.kind == "add" for o in ops if o.playlist_id == pid)
                    - sum(o.kind == "delete" for o in ops if o.playlist_id == pid) for pid in pids}
        counts_ok = all(len(live[pid]) == expected[pid] for pid in pids)
        if (not problems and counts_ok) or attempt == len(waits):
            break
        (sleep or time.sleep)(waits[attempt])
    applied.live = live
    rows = [{"id": pid, "title": applied.playlists.get(pid, pid), "before": applied.before.get(pid, 0),
             "after": len(live[pid]), "expected": expected[pid]} for pid in pids]
    return {"checked": True, "ok": not problems and counts_ok, "confirmed": len(ops) - len(problems),
            "problems": problems, "playlists": rows}


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM youtube_runs WHERE status IN ('done', 'stopped', 'interrupted') AND "
                        "EXISTS (SELECT 1 FROM youtube_journal j WHERE j.run_id = youtube_runs.id) "
                        "ORDER BY id DESC LIMIT 1").fetchone()


@dataclass
class Undone:
    restored: int = 0      # deleted entries put back
    removed: int = 0       # added entries taken out again
    left: int = 0
    stopped: str = ""


def undo_last_run(api: yt.Api, conn: sqlite3.Connection) -> Undone:
    """Reverse the last cleanup run, newest operation first. Resumable: finished steps leave the journal."""
    run = last_run(conn)
    out = Undone()
    if run is None:
        return out
    rows = conn.execute("SELECT * FROM youtube_journal WHERE run_id = ? ORDER BY id DESC", (run["id"],)).fetchall()
    try:
        for r in rows:
            if not api.can_spend(WRITE_COST):
                out.stopped = f"daily quota budget reached ({api.budget} units); run the undo again tomorrow"
                break
            if r["op"] == "create":                           # the playlist stays (empty and private); only the note goes
                conn.execute("DELETE FROM youtube_journal WHERE id = ?", (r["id"],))
                conn.commit()
                continue
            if r["op"] == "delete":
                try:
                    yt.add_entry(api, r["playlist_id"], r["video_id"], position=r["position"])
                except yt.QuotaError:
                    raise
                except yt.AuthError:
                    raise
                except yt.YouTubeError:                      # the old position no longer fits: put it at the end
                    yt.add_entry(api, r["playlist_id"], r["video_id"])
                out.restored += 1
            else:
                yt.delete_entry(api, r["playlist_item_id"])
                out.removed += 1
            conn.execute("DELETE FROM youtube_journal WHERE id = ?", (r["id"],))
            conn.commit()
    except yt.YouTubeError as exc:
        out.stopped = str(exc)
    out.left = conn.execute("SELECT COUNT(*) FROM youtube_journal WHERE run_id = ?", (run["id"],)).fetchone()[0]
    if out.left == 0:
        conn.execute("UPDATE youtube_runs SET status = 'undone' WHERE id = ?", (run["id"],))
        conn.commit()
    return out
