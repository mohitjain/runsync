"""runsync CLI.

Every command prints its facts as one JSON object with `--json` (that is what the skills read; the human lines then go
to stderr). Without `--json` it prints a few plain lines. Presentation is the skills' job, not this file's.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape

from . import db, ytclean, ytflow, ytsearch
from . import status as status_facts
from . import youtube as yt
from .card import (
    apply_card_plan,
    build_card_plan,
    candidate_volumes,
    card_plan_id,
    card_state,
    devices,
    register_device,
    undo_last_run,
    volume_info,
)
from .config import Config, load_config
from .cookies import check_cookies
from .dedupe import apply_plan, build_plan, plan_id, plan_to_dict, record_decision
from .dedupe import undo as dedupe_undo
from .diff import MassDeleteError
from .lock import AlreadyRunning, run_lock
from .logs import cookies_rotated, setup_logging
from .playlist_select import apply_selection, list_playlists, refresh_account_playlists
from .resolve.model import Candidate
from .resolve.net import Http
from .resolve.run import Reporter as ResolveReporter
from .resolve.run import ResolveResult, run_resolve
from .sync import Reporter, run_sync

app = typer.Typer(help="Sync YouTube playlists to a music player.", no_args_is_help=True, add_completion=False)
console = Console()


# ---- shared helpers ---------------------------------------------------------------------------------------------------

def _ctx() -> tuple[Config, sqlite3.Connection]:
    cfg = load_config()
    cfg.ensure_dirs()
    return cfg, db.connect(cfg.db_path)


def _esc(text: str) -> str:
    return escape(text)


def _say(text: str = "") -> None:
    console.print(text, highlight=False)


def _emit(facts: dict) -> None:
    """The facts, as the one JSON line on stdout that the skills read."""
    sys.stdout.write(json.dumps(facts) + "\n")


def _fail(message: str, code: int = 2, facts: dict | None = None, json_out: bool = False) -> NoReturn:
    _say(f"[red]{_esc(message)}[/]")
    if json_out and facts is not None:
        _emit({**facts, "error": message})
    raise typer.Exit(code)


def _size(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def resolve_cookies(cfg: Config, anonymous: bool, interactive: bool) -> Path | None:
    """Return a cookie path, or None for anonymous (reduced quality)."""
    if anonymous:
        return None
    st = check_cookies(cfg.cookies)
    if st.usable:
        return cfg.cookies
    _say(f"[yellow]Cookies unusable ({st.state}): {_esc(st.detail)}[/]")
    _say(f"Re-export cookies.txt from music.youtube.com (logged in) to [bold]{cfg.cookies}[/].")
    if interactive and not typer.confirm("Continue anonymously at reduced quality (~130 kbps)?", default=False):
        raise typer.Exit(1)
    _say("[yellow]Falling back to anonymous download.[/]")
    return None


def _cookie_hint(force: bool = False) -> None:
    """If yt-dlp said the cookies were invalidated (or YouTube answered 401), give the exact fix."""
    if force or cookies_rotated():
        _say("\n[yellow]YouTube invalidated your cookies (they were rotated).[/] Re-export them:\n"
             "  1. Open a new Incognito window, log in to youtube.com (only tab open)\n"
             "  2. In that tab go to https://www.youtube.com/robots.txt\n"
             "  3. Export youtube.com cookies, save over [bold]cookies.txt[/], then close the window")


# ---- playlists --------------------------------------------------------------------------------------------------------

def _print_playlists(conn) -> None:
    rows = list_playlists(conn)
    _say(f"[bold]Playlists[/] {sum(r['synced'] for r in rows)} of {len(rows)} syncing")
    for r in rows:
        _say(f"  {r['n']:>2} {'✅' if r['synced'] else '⬜'} {_esc(r['title'])} {r['tracks'] or ''}")


@app.command()
def playlists(json_out: bool = typer.Option(False, "--json", help="The list as JSON (for the skill to present)."),
              sync_: str = typer.Option("", "--sync", help='Start syncing these: numbers or names, e.g. "1,3,Gym".'),
              stop: str = typer.Option("", "--stop", help="Stop syncing these: numbers or names.")) -> None:
    """Show your playlists (numbered; ✅ syncing). With --sync / --stop: start or stop syncing some, by number or name."""
    console.stderr = json_out
    cfg, conn = _ctx()
    logger = setup_logging(cfg, "playlists")
    if sync_ or stop:   # no refresh here: the numbers must match the list the user was just shown
        try:
            done = apply_selection(conn, sync_, stop)
        except ValueError as exc:
            _fail(f"{exc}. Nothing changed.")
        if json_out:
            _emit({**done, "playlists": list_playlists(conn)})
            return
        _print_playlists(conn)
        if done["leaving"]:
            _say(f"⚠️ {done['leaving']} downloaded track(s) are only in the playlists you stopped; the next sync moves "
                 "them to .trash (kept 14 days). Preview: runsync sync --dry-run")
        return
    cookies = cfg.cookies if check_cookies(cfg.cookies).usable else None
    api = yt.open_api(cfg, conn)
    live, problem, source, gone = False, "", "cookies", []
    if cookies or api:
        try:
            source, gone = refresh_account_playlists(conn, cookies, api)
            live = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("playlist refresh failed: %s", exc)
            problem = "rejected" if ("401" in str(exc) or "sign in" in str(exc).lower() or cookies_rotated()) else "failed"
    else:
        problem = "no cookies"
    if json_out:   # facts only; the skill decides how to present them
        listed = [{**r, "source": "cookies" if source == "cookies" or r["id"] in ("LM", "WL") else "api"}
                  for r in list_playlists(conn)]
        _emit({"live": live, "problem": problem, "source": source, "gone": gone, "playlists": listed})
        return
    if problem:
        _say("⚠️ [yellow]Could not refresh from YouTube; this is the saved list, not live.[/]")
        if problem == "rejected":
            _cookie_hint(force=True)
    if not list_playlists(conn):
        _fail("No playlists. Add cookies.txt, or use `runsync add-playlist <url>`.", 1)
    _print_playlists(conn)
    for g in gone:
        _say(f"⚠️ [yellow]{_esc(g['title'])} no longer exists on YouTube[/]: stop syncing it with `runsync playlists --stop \"{_esc(g['title'])}\"`")


@app.command("add-playlist")
def add_playlist(url: str) -> None:
    """Add a playlist by URL (for ones not in your account) and select it."""
    from . import fetch
    cfg, conn = _ctx()
    cookies = cfg.cookies if check_cookies(cfg.cookies).usable else None
    info = fetch.playlist_info(url, cookies)
    conn.execute("INSERT INTO playlists (id, title, url, kind, selected) VALUES (?, ?, ?, 'url', 1) "
                 "ON CONFLICT(id) DO UPDATE SET title = excluded.title, selected = 1", (info.id, info.title, info.url))
    conn.commit()
    _say(f"Added '{_esc(info.title)}' ({info.id}).")


# ---- sync -------------------------------------------------------------------------------------------------------------

def _first_line(text: str, width: int = 100) -> str:
    return _esc((text.splitlines() or [""])[0][:width])


class _SyncProgress(Reporter):
    """One plain line per event while tracks download."""

    def warn(self, msg: str) -> None:
        _say(f"[yellow]![/] {_esc(msg)}")

    def download_start(self, n: int, total: int, video_id: str, title: str) -> None:
        _say(f"[{n}/{total}] {_esc(title)}")

    def download_ok(self, video_id, codec, bitrate, size_bytes, seconds) -> None:
        kind = "AAC" if str(codec).startswith("mp4a") else str(codec).split(".")[0].upper()
        _say(f"    ✅ {kind} {bitrate:.0f}k · {(size_bytes or 0) / 1e6:.1f} MB · {seconds:.0f}s")

    def download_unavailable(self, video_id: str, reason: str) -> None:
        _say(f"    [yellow]unavailable[/] {_first_line(reason)}")

    def download_failed(self, video_id: str, reason: str) -> None:
        _say(f"    [red]failed[/] {_first_line(reason)}")

    def download_retry(self, video_id: str, attempt: int, delay: float, reason: str) -> None:
        _say(f"    [yellow]retry {attempt}/3 in {delay:.0f}s[/] {_first_line(reason, 60)}")


@app.command()
def sync(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; write nothing."),
         anonymous: bool = typer.Option(False, help="Skip cookies (reduced quality)."),
         force_delete: bool = typer.Option(False, help="Override the mass-delete guard."),
         verbose: bool = typer.Option(False, "--verbose", "-v", help="Also print debug logs to stderr."),
         limit: int = typer.Option(None, "--limit", "-n", min=1, help="Max downloads this run (default: config)."),
         json_out: bool = typer.Option(False, "--json", help="Print the result as JSON on stdout (the normal "
                                       "output goes to stderr); for the skills to present.")) -> None:
    """Fetch selected playlists, trash removed tracks, download new ones."""
    console.stderr = json_out   # --json: facts on stdout, the human view on stderr
    cfg, conn = _ctx()
    if limit is not None:
        cfg.max_downloads_per_run = limit
    logger = setup_logging(cfg, "sync", verbose)
    cookies = resolve_cookies(cfg, anonymous, sys.stdin.isatty())
    logger.info("cookie check: %s", check_cookies(cfg.cookies).state if not anonymous else "skipped (--anonymous)")
    try:
        with run_lock(cfg.library) if not dry_run else nullcontext():
            res = run_sync(cfg, conn, cookies, dry_run=dry_run, force_delete=force_delete, rep=_SyncProgress(),
                           api=yt.open_api(cfg, conn))
    except AlreadyRunning as exc:
        _fail(f"{exc}. Wait for it to finish, or stop it (Ctrl-C) and retry.", 3)
    logger.info("done: %s", res.summary())
    _cookie_hint()
    if dry_run and not res.aborted:
        _say(f"Dry run: {res.queue_total} waiting, this run would download {len(res.added)} and remove "
             f"{len(res.removed)}. Nothing written.")
    else:
        _say(res.summary() + (f"\n[red]Stopped:[/] {_esc(res.aborted)}" if res.aborted else ""))
    _say(f"[dim]Log: {cfg.log_path}[/]")
    if json_out:
        queued = conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'pending'").fetchone()[0]
        _emit({**res.to_dict(), "dry_run": dry_run, "queued_left": queued})
    if res.aborted:
        raise typer.Exit(2)


# ---- resolve ----------------------------------------------------------------------------------------------------------

class _ResolveProgress(ResolveReporter):
    def start(self, total: int) -> None:
        _say(f"Resolving {total} track{'s' if total != 1 else ''}...")

    def resolved(self, video_id: str, title: str, cand: Candidate, review: str | None, languages=()) -> None:
        if review:
            _say(f"  ⚠️ {_esc(title or video_id)}: {_esc(review)}")

    def warn(self, msg: str) -> None:
        _say(f"[yellow]![/] {_esc(msg)}")


def _resolve_summary(res: ResolveResult, cfg: Config, dry_run: bool) -> None:
    _say(f"{'Would resolve' if dry_run else 'Resolved'} {res.resolved} ({res.review} need you)"
         + ("" if dry_run else f" · {res.built} player copies built, {res.held} held"))
    if res.enrich_open and not dry_run:
        _say(f"🔎 {res.enrich_open} row(s) wait for a web search: run the runsync-enrich skill, or fix them in {cfg.review_path}")


@app.command()
def resolve(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be resolved; write nothing."),
            offline: bool = typer.Option(False, "--offline", help="Skip MusicBrainz/iTunes/cover lookups."),
            no_build: bool = typer.Option(False, "--no-build", help="Resolve metadata only; no player copies."),
            redo: bool = typer.Option(False, "--redo", help="Re-resolve every track, not just new/unsure ones "
                                      "(your overrides still win)."),
            verbose: bool = typer.Option(False, "--verbose", "-v", help="Also print debug logs to stderr."),
            limit: int = typer.Option(None, "--limit", "-n", min=1, help="Max tracks this run."),
            json_out: bool = typer.Option(False, "--json", help="Print the result as JSON on stdout (the normal "
                                          "output goes to stderr); for the skills to present.")) -> None:
    """Clean titles into artist/title/album/year/language, then build tagged player copies."""
    console.stderr = json_out   # --json: facts on stdout, the human view on stderr
    cfg, conn = _ctx()
    logger = setup_logging(cfg, "resolve", verbose)
    http = None if (offline or not cfg.online) else Http(conn, cache_days=cfg.http_cache_days)
    try:
        with run_lock(cfg.library) if not dry_run else nullcontext():
            res = run_resolve(cfg, conn, dry_run=dry_run, limit=limit, http=http, build=not no_build, redo=redo,
                              rep=_ResolveProgress())
    except AlreadyRunning as exc:
        _fail(f"{exc}. Wait for it to finish, then retry.", 3)
    logger.info("done: %s", res)
    _resolve_summary(res, cfg, dry_run)
    if json_out:
        _emit({**res.to_dict(), "dry_run": dry_run})


# ---- dedupe -----------------------------------------------------------------------------------------------------------

def _print_dedupe(plan) -> None:
    _say(f"[bold]Duplicates[/] {plan.removals} to delete · {len(plan.unsure)} to decide")
    for g in plan.groups:
        _say(f"  ✅ keep   {_esc(g.winner.youtube_title or g.winner.label())}")
        for m, s in g.losers:
            _say(f"  🗑️ delete {_esc(m.youtube_title or m.label())} ({f'{s:.0%} match' if s is not None else 'you said same'})")
    for u in plan.unsure:
        _say(f"  ❓ same song? A: {_esc(u.a.youtube_title or u.a.label())}  B: {_esc(u.b.youtube_title or u.b.label())}")


@app.command()
def dedupe(apply: bool = typer.Option(False, "--apply", help="Delete the duplicates for good (default: only show them)."),
           json_out: bool = typer.Option(False, "--json", help="Print the plan as JSON (for the skill)."),
           same: str = typer.Option("", "--same", help="Record same-song pairs: ID1,ID2 (several: ID1,ID2;ID3,ID4)"),
           different: str = typer.Option("", "--different", help="Record different-song pairs: ID1,ID2 (several with ;)"),
           undo_id: str = typer.Option("", "--undo", help="Bring a merged track back (it downloads again): VIDEO_ID"),
           confirm: str = typer.Option("", "--confirm", help="With --apply: the plan id shown by the preview. Only that "
                                       "exact plan is deleted; any other plan is refused."),
           force: bool = typer.Option(False, "--force", help="Allow removing more than 20% of the library.")) -> None:
    """Find tracks that are the same recording uploaded twice; keep the best, delete the rest (--apply --confirm ID)."""
    console.stderr = json_out
    cfg, conn = _ctx()
    setup_logging(cfg, "dedupe")
    try:
        for text, verdict in ((same, True), (different, False)):
            for pair in filter(None, (x.strip() for x in text.split(";"))):   # "A,B;C,D" records several pairs
                a, b = (x.strip() for x in pair.split(","))
                record_decision(conn, a, b, verdict)
        if undo_id:
            winner = dedupe_undo(conn, undo_id)
            _say(f"{undo_id} is back (it downloads on the next sync); it will not be merged into {winner} again.")
            return
    except ValueError as exc:
        _fail(str(exc))
    plan = build_plan(conn)
    if json_out:
        _emit(plan_to_dict(plan))
        return
    if not plan.groups and not plan.unsure:
        _say("✅ No duplicates.")
        return
    _print_dedupe(plan)
    if not apply:
        if plan.removals:
            _say(f"{plan.removals} to delete. Nothing changed. To delete exactly these: "
                 f"[bold]runsync dedupe --apply --confirm {plan_id(plan)}[/]")
        return
    if confirm != plan_id(plan):   # the gate is in the command, not only in the skill's instructions
        _fail(f"Not confirmed. --apply needs --confirm {plan_id(plan)} (the id of the plan above); a plan that changed "
              "since you confirmed is refused. Nothing deleted.")
    try:
        with run_lock(cfg.library):
            n = apply_plan(cfg, conn, plan, force)
    except AlreadyRunning as exc:
        _fail(str(exc), 3)
    except MassDeleteError as exc:
        _fail(str(exc))
    _say(f"Deleted {n} duplicate(s).")


# ---- status -----------------------------------------------------------------------------------------------------------

@app.command()
def status(json_out: bool = typer.Option(False, "--json", help="Facts as JSON (for the skill to present).")) -> None:
    """Progress at a glance: downloading, tagging or idle. Read-only, no network."""
    cfg, conn = _ctx()
    f = status_facts.facts(cfg, conn)
    if json_out:
        _emit(f)
        return
    words = {"running": "⏳ " + f["activity"], "done": "✅ everything is downloaded", "stopped": f"⏸️ stopped, {f['queued']} waiting"}
    _say(f"{words[f['state']]} · {f['downloaded']} of {f['total']} tracks · {f['tagged']} tagged · "
         f"{f['player_copies']} player copies" + (f" · ⚠️ {f['unavailable']} unavailable" if f["unavailable"] else ""))


# ---- card -------------------------------------------------------------------------------------------------------------

def _print_card(state: dict) -> None:
    s = state["state"]
    if s == "unregistered":
        found = ", ".join(f"{_esc(v['name'])} ({v['mount']})" for v in state["candidates"]) or "none found"
        _say(f"[bold]Card[/] none registered. Volumes: {found}. Register yours: runsync card --register <mount path>")
        return
    if s == "not_inserted":
        _say(f"[bold]Card[/] {_esc(state['device'])}: ⏸️ not inserted")
        return
    _say(f"[bold]Card[/] {_esc(state['device'])} · {state['fs']} · {_size(state['free_bytes'])} free")
    _say(f"  ✅ {state['unchanged']} on the card · ➕ {state['adds']} to add · 🔄 {state['updates']} to update · "
         f"🗑️ {state['removes']} to remove")
    if state["blocked"]:
        why = {"guard": "more than 20% of the card would be removed (needs --force)",
               "space": "not enough free space on the card",
               "conflict": f"files on the card have our names but are not ours: {', '.join(state.get('conflicts', [])[:3])}"}
        _say(f"  [red]❌ Blocked: {_esc(why[state['blocked']])}[/]")
    elif state["adds"] or state["updates"] or state["removes"]:
        _say(f"  To sync exactly this: [bold]runsync card --apply --confirm {state['plan_id']}[/]")
    else:
        _say("  ✅ The card is up to date.")


@app.command()
def card(register: str = typer.Option("", "--register", help="Mount path of a card to register, e.g. /Volumes/SD"),
         name: str = typer.Option("shanling", "--name", help="Name for the card when registering."),
         apply: bool = typer.Option(False, "--apply", help="Copy and remove files on the card (needs --confirm)."),
         confirm: str = typer.Option("", "--confirm", help="With --apply: the plan id shown by the preview. Only that "
                                     "exact plan is applied; any other is refused."),
         force: bool = typer.Option(False, "--force", help="Allow removing more than 20% of the card."),
         undo: bool = typer.Option(False, "--undo", help="Reverse the last finished sync."),
         eject: bool = typer.Option(False, "--eject", help="Eject the card when done."),
         json_out: bool = typer.Option(False, "--json", help="Print facts as JSON on stdout (for the skill).")) -> None:
    """Show the card and what a sync would do (nothing changes). --apply --confirm ID syncs it."""
    console.stderr = json_out   # --json: facts on stdout, the human view on stderr
    cfg, conn = _ctx()
    setup_logging(cfg, "card")
    try:
        if register:
            info = volume_info(Path(register))
            if info is None:
                raise ValueError(f"{register} is not a mounted volume macOS can identify")
            register_device(conn, info, name, cfg.library)
            _say(f"✅ Registered {_esc(name)}: {_esc(info.name)} ({info.fs}). Only this card will be written.")
    except ValueError as exc:
        _fail(str(exc))
    state = card_state(conn, cfg.library, candidate_volumes(cfg.library))
    result: dict = {}
    if (apply or undo) and state["state"] in ("ready", "blocked"):
        dev = next(d for d in devices(conn) if d["id"] == state["device_id"])
        mount, free = Path(state["mount"]), state["free_bytes"]
        try:
            with run_lock(cfg.library):
                plan = build_card_plan(conn, dev["id"], mount, dev["subpath"], free, cfg.mass_delete_threshold)
                if undo:
                    r = undo_last_run(conn, dev["id"], mount, dev["subpath"], plan)
                    result = {"undone": {"restored": r.added, "removed": r.removed}}
                    _say(f"↩️ Undid the last sync: {r.removed} file(s) removed, {r.added} put back.")
                else:
                    if confirm != card_plan_id(plan):   # the gate is in the command, not only in the skill's instructions
                        _fail(f"Not confirmed. --apply needs --confirm {card_plan_id(plan)} (the id of the plan above); "
                              "a plan that changed since you confirmed is refused. Nothing written.")
                    r = apply_card_plan(conn, dev["id"], mount, dev["subpath"], plan, force)
                    result = {"applied": {"added": r.added, "updated": r.updated, "removed": r.removed, "bytes": r.bytes,
                                          "playlists": r.playlists}}
                    _say(f"✅ Synced: {r.added} added, {r.updated} updated, {r.removed} removed "
                         f"({_size(r.bytes)} copied), {r.playlists} playlist file(s).")
        except AlreadyRunning as exc:
            _fail(str(exc), 3)
        except (OSError, ValueError) as exc:
            _fail(f"{exc} Nothing more was written.")
        state = card_state(conn, cfg.library, candidate_volumes(cfg.library))
    elif apply or undo:
        _fail("No registered card is inserted. Nothing written.")
    if eject and state.get("mount"):
        done = subprocess.run(["diskutil", "eject", state["mount"]], capture_output=True, text=True, check=False)
        result["ejected"] = done.returncode == 0
        _say("⏏️ Ejected. Safe to remove the card." if done.returncode == 0
             else f"[yellow]Could not eject: {_esc(done.stderr.strip()[:120])}[/]")
    if json_out:
        _emit({**state, **result})
        return
    _print_card(state)


# ---- youtube ----------------------------------------------------------------------------------------------------------

def _say_youtube(facts: dict) -> None:
    """A few plain lines for a human; the skills read the JSON instead."""
    if "applied" in facts:
        a, v = facts["applied"], facts["applied"]["verify"]
        _say(f"✅ {a.get('deleted', 0)} removed, {a.get('added', 0)} added, {a['left']} still to do"
             + (f" ({_esc(a['stopped'])})" if a["stopped"] else "")
             + (" · checked on YouTube" if v["ok"] else " · [yellow]could not confirm every change on YouTube[/]"))
    elif "undone" in facts:
        u = facts["undone"]
        _say(f"↩️ Undid the last run: {u['restored']} put back, {u['removed']} taken out"
             + (f", {u['left']} left ({_esc(u['stopped'])})" if u["left"] else "."))
    elif "cleanup" in facts:
        c = facts["cleanup"]
        _say(f"🧹 Plan {c['plan_id']}: {c['deletes']} to remove, {c['adds']} to add ({c['cost']} quota units, {c['days']} day(s)). "
             f"Apply: runsync youtube --apply --confirm {c['plan_id']}")
        if c["manual"]:
            _say("👎 Unlike these by hand in YouTube Music (the API cannot edit Liked Music):")
            for m in c["manual"]:
                _say(f"   {_esc(m['title'] or m['video_id'])}  {m['url']}")
    elif "add" in facts:
        a = facts["add"]
        _say(f"➕ Plan {a['plan_id']}: {len(a['videos'])} song(s) to {_esc(a['playlist'])}{' (new)' if a['creates'] else ''}. "
             f"Apply: runsync youtube --add ... --to ... --apply --confirm {a['plan_id']}")
    for c in facts.get("search", {}).get("candidates", []):
        _say(f"  {c['video_id']}  {_esc(c['title'])} · {_esc(c['channel'])}")
    for song in facts.get("replacements", {}).get("songs", []):
        _say(f"🎵 {_esc(song['title'])}" + (" (already in your library)" if song["in_library"] else ""))
        for c in song["candidates"][:3]:
            _say(f"    {c['video_id']}  {_esc(c['title'])} · {_esc(c['channel'])}")
    if not any(k in facts for k in ("applied", "undone", "cleanup", "add", "search", "replacements")):
        _say(("✅ Signed in" if facts["logged_in"] else "⚠️ Not signed in (run: runsync youtube --login)")
             + f" · quota today {facts['units_today']} of {facts['budget']}")
        for p in facts.get("playlists", []):
            _say(f"  📋 {_esc(p['title'])} · {p['count']}")


@app.command()
def youtube(login: bool = typer.Option(False, "--login", help="Sign in to YouTube in your browser (once)."),
            cleanup: bool = typer.Option(False, "--cleanup", help="Preview bringing your YouTube playlists up to date: "
                                         "repeats, merged duplicates, dead songs, replacements (nothing changes)."),
            apply: bool = typer.Option(False, "--apply", help="Run the cleanup or the add (needs --confirm)."),
            confirm: str = typer.Option("", "--confirm", help="With --apply: the plan id shown by the preview. Only that "
                                        "exact plan runs; any other is refused."),
            batch: int = typer.Option(ytclean.DEFAULT_BATCH, "--batch", help="With --apply: most operations per run."),
            undo: bool = typer.Option(False, "--undo", help="Reverse the last cleanup or add run."),
            search: str = typer.Option("", "--search", help="Search YouTube for a song (no quota); lists candidates."),
            replacements: bool = typer.Option(False, "--replacements", help="Search a replacement for each song YouTube "
                                              "no longer serves (no quota; nothing changes)."),
            pick: str = typer.Option("", "--pick", help="Save your choice for a lost song: LOSTID:UPLOADID (several: "
                                     "comma separated). Local only, no quota; --cleanup then puts it in place."),
            skip: str = typer.Option("", "--skip", help="Save 'none of these' for lost songs (LOSTID, comma separated); "
                                     "they are deleted."),
            remove: str = typer.Option("", "--remove", help="Video ids you want gone for good (comma separated): saved here; "
                                       "--cleanup then removes them from your playlists and lists the liked ones for you to unlike."),
            add: str = typer.Option("", "--add", help="Video ids to add (comma separated); needs --to."),
            to: str = typer.Option("", "--to", help="With --add: your playlist's name or id."),
            create: bool = typer.Option(False, "--create", help="With --add: make the playlist (private) if it is missing."),
            json_out: bool = typer.Option(False, "--json", help="Print facts as JSON on stdout (for the skill).")) -> None:
    """Show the YouTube login, today's quota and your playlists. --login signs in; --cleanup previews the cleanup."""
    console.stderr = json_out
    cfg, conn = _ctx()
    setup_logging(cfg, "youtube")
    api = yt.Api(cfg.youtube_token, cfg.youtube_client, conn, cfg.youtube_budget)
    facts = ytflow.base_facts(cfg, api)
    try:
        if login:
            if not cfg.youtube_client.exists():
                raise yt.AuthError(f"{cfg.youtube_client.name} is missing: create a Desktop OAuth client in Google Cloud "
                                   "and save its JSON in the repo folder.")
            yt.login(cfg.youtube_client, cfg.youtube_token, announce=lambda m: _say(f"🌐 {_esc(m)}"))
            facts["logged_in"] = True
            _say("✅ Signed in. The login is saved in youtube_token.json (owner-only, git-ignored).")
        if search:
            facts["search"] = ytflow.search(search)
        if replacements:
            facts["replacements"] = ytflow.replacements(conn)
        if pick or skip:
            facts["saved"] = ytflow.save_picks(conn, pick, skip)
        if remove:
            facts["wanted_gone"] = ytflow.remove(conn, remove)
        facts["picks"] = ytsearch.pick_counts(conn)
        facts |= ytflow.act(api, conn, cfg, cleanup_=cleanup, apply=apply, confirm=confirm, batch=batch, undo_=undo,
                            add_=add, to=to, create=create,
                            status=not (search or replacements or pick or skip or remove or login))
    except AlreadyRunning as exc:
        _fail(str(exc), 3, facts, json_out)
    except (ytclean.PlanRefused, ytclean.NoSuchPlaylist, yt.YouTubeError) as exc:
        _fail(str(exc), 2, facts, json_out)
    if json_out:
        _emit(facts)
        return
    _say_youtube(facts)
