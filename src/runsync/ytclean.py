"""Clean duplicates out of your YouTube playlists (the official API).

Two kinds of clutter: the same video listed again and again (repeats), and the losing upload of a song that `dedupe`
merged (a loser). Where the kept upload is missing from a playlist that holds a loser, the plan swaps: it adds the kept
upload first and then removes the loser, so a song never silently vanishes from a playlist.

Same pattern as dedupe and the card: `build_plan` only reads, `apply_plan` needs the id of the exact plan the owner
confirmed, every change is journalled so `undo_last_run` can reverse it. Liked Music, Liked videos and Watch later are
never written. No printing here: the CLI and the skills present the results.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import youtube as yt

NEVER_WRITE = {"LM", "LL", "WL"}      # the API cannot or must not touch these: likes are your taste, not clutter
WRITE_COST = yt.COST["POST"]           # an insert and a delete both cost 50 units
DEFAULT_BATCH = 100


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PlanRefused(Exception):
    """Applying was refused (wrong or missing plan id, or a forbidden playlist). Nothing was written."""


@dataclass(frozen=True)
class Op:
    kind: str            # "add" | "delete"
    playlist_id: str
    video_id: str
    item_id: str         # the playlist entry to delete ("" for an add)
    position: int
    why: str             # "repeat" | "loser" | "swap"
    title: str = ""
    keep_id: str = ""    # for loser/swap: the upload that stays (or is added)


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)       # adds first, then deletes: any prefix is safe to run
    playlists: dict[str, str] = field(default_factory=dict)

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


def resolve_winner(conn: sqlite3.Connection, video_id: str) -> str:
    """The upload that stands for this video: itself, or the one that replaced it (follows chains, never loops)."""
    seen = {video_id}
    cur = video_id
    while True:
        row = conn.execute("SELECT winner_id FROM aliases WHERE video_id = ?", (cur,)).fetchone()
        if not row or row["winner_id"] in seen:
            return cur
        cur = row["winner_id"]
        seen.add(cur)


def plan_playlist(pid: str, entries: list[yt.Entry], winner: Callable[[str], str],
                  title_of: Callable[[str], str] = lambda v: "") -> list[Op]:
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
        w = winner(e.video_id)
        if w == e.video_id:
            continue
        if w in present or w in added:
            deletes.append(Op("delete", pid, e.video_id, e.item_id, e.position, "loser", e.title, w))
        else:
            adds.append(Op("add", pid, w, "", e.position, "swap", title_of(w), w))
            deletes.append(Op("delete", pid, e.video_id, e.item_id, e.position, "swap", e.title, w))
            added.add(w)
    deletes += [Op("delete", pid, e.video_id, e.item_id, e.position, "repeat", e.title, e.video_id) for e in repeats]
    return adds + deletes


def build_plan(api: yt.Api, conn: sqlite3.Connection, only: set[str] | None = None) -> Plan:
    """Read your owned, selected playlists live and work out what to clean. Writes nothing."""
    owned = {p["id"]: p["title"] for p in yt.my_playlists(api)}
    selected = {r["id"] for r in conn.execute("SELECT id FROM playlists WHERE selected = 1")}
    plan = Plan()
    titles = {r["video_id"]: r["title"] or "" for r in conn.execute("SELECT video_id, title FROM tracks")}
    for pid, title in owned.items():
        if pid in NEVER_WRITE or pid not in selected or (only is not None and pid not in only):
            continue
        plan.playlists[pid] = title
        plan.ops += plan_playlist(pid, yt.playlist_entries(api, pid), lambda v: resolve_winner(conn, v),
                                  lambda v: titles.get(v, ""))
    plan.ops.sort(key=lambda o: o.kind != "add")           # every add before any delete; sort is stable
    return plan


def summary(plan: Plan) -> dict:
    per = {}
    for pid, title in plan.playlists.items():
        mine = [o for o in plan.ops if o.playlist_id == pid]
        per[pid] = {"id": pid, "title": title, "repeats": sum(o.why == "repeat" for o in mine),
                    "losers": sum(o.why == "loser" for o in mine), "swaps": sum(o.why == "swap" and o.kind == "add" for o in mine)}
    return {"plan_id": plan_id(plan), "playlists": list(per.values()), "ops": len(plan.ops), "adds": plan.adds,
            "deletes": plan.deletes, "cost": plan.cost}


def days_needed(plan: Plan, budget: int) -> int:
    return max(1, math.ceil(plan.cost / max(budget - 200, WRITE_COST))) if plan.ops else 0   # 200 units kept for reads


# ---- applying ---------------------------------------------------------------------------------------------------------

@dataclass
class Applied:
    run_id: int
    done: int = 0
    deleted: int = 0
    added: int = 0
    left: int = 0
    stopped: str = ""        # why it stopped early ("" = finished the batch)


def _journal(conn: sqlite3.Connection, run_id: int, op: str, o: Op, item_id: str) -> None:
    conn.execute("INSERT INTO youtube_journal (run_id, op, playlist_id, video_id, playlist_item_id, position) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (run_id, op, o.playlist_id, o.video_id, item_id, o.position))
    conn.commit()


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
    out = Applied(run_id=run_id)
    todo = plan.ops[:max(0, batch)]
    status = "done"
    try:
        for o in todo:
            if not api.can_spend(WRITE_COST):
                out.stopped = f"daily quota budget reached ({api.budget} units); the rest waits for tomorrow"
                break
            if o.kind == "add":
                new_id = yt.add_entry(api, o.playlist_id, o.video_id, position=o.position)
                _journal(conn, run_id, "add", o, new_id)
                out.added += 1
            else:
                yt.delete_entry(api, o.item_id)
                _journal(conn, run_id, "delete", o, o.item_id)
                out.deleted += 1
            out.done += 1
    except yt.YouTubeError as exc:
        out.stopped = str(exc)
        status = "stopped"
    out.left = len(plan.ops) - out.done
    if out.stopped:
        status = "stopped"
    conn.execute("UPDATE youtube_runs SET status = ?, finished_at = ? WHERE id = ?", (status, now_iso(), run_id))
    conn.commit()
    return out


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM youtube_runs WHERE status IN ('done', 'stopped') AND "
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
