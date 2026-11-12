"""runsync CLI."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import db, fetch, ytclean, ytsearch
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
from .lock import AlreadyRunning, run_lock, running_command
from .logs import cookies_rotated, setup_logging
from .player import safe_filename
from .playlist_select import apply_selection, list_playlists
from .resolve.model import Candidate
from .resolve.net import Http
from .resolve.run import Reporter as ResolveReporter
from .resolve.run import ResolveResult, run_resolve
from .sync import Reporter, RunResult, run_sync

app = typer.Typer(help="Sync YouTube playlists to a music player.", no_args_is_help=True)
console = Console()


def _ctx() -> tuple[Config, object]:
    cfg = load_config()
    cfg.ensure_dirs()
    return cfg, db.connect(cfg.db_path)


def _interactive() -> bool:
    return sys.stdin.isatty()


def resolve_cookies(cfg: Config, anonymous: bool, interactive: bool) -> Path | None:
    """Return a cookie path, or None for anonymous (reduced quality)."""
    if anonymous:
        return None
    st = check_cookies(cfg.cookies)
    if st.usable:
        return cfg.cookies
    console.print(f"[yellow]Cookies unusable ({st.state}): {st.detail}[/]")
    console.print(f"Re-export cookies.txt from music.youtube.com (logged in) to [bold]{cfg.cookies}[/].")
    if interactive and not typer.confirm("Continue anonymously at reduced quality (~130 kbps)?", default=False):
        raise typer.Exit(1)
    console.print("[yellow]Falling back to anonymous download.[/]")
    return None


def _cookie_hint(force: bool = False) -> None:
    """If yt-dlp said the cookies were invalidated (or YouTube answered 401), give the exact fix."""
    if force or cookies_rotated():
        console.print(
            "\n[yellow]YouTube invalidated your cookies (they were rotated).[/] Re-export them:\n"
            "  1. Open a new Incognito window, log in to youtube.com (only tab open)\n"
            "  2. In that tab go to https://www.youtube.com/robots.txt\n"
            "  3. Export youtube.com cookies, save over [bold]cookies.txt[/], then close the window")


def _merge_remote(conn, cookies: Path | None, api=None) -> str:
    """Upsert account playlists from YouTube (the API when signed in, else cookies), keeping saved selection.
    Unselected ones that vanished are dropped. Returns the source used: "api" or "cookies"."""
    found, source = None, "cookies"
    if api is not None:
        try:
            found, source = fetch.api_account_playlists(api), "api"
        except yt.YouTubeError as exc:
            logging.getLogger("runsync.cli").warning("playlist list via API failed (%s); using cookies", exc)
    if found is None:
        if cookies is None:
            raise RuntimeError("no YouTube login and no usable cookies")
        found = fetch.fetch_account_playlists(cookies)
    for p in found:
        conn.execute("INSERT INTO playlists (id, title, url, kind) VALUES (?, ?, ?, 'account') "
                     "ON CONFLICT(id) DO UPDATE SET title = excluded.title", (p.id, p.title, p.url))
    ids = [p.id for p in found]
    if ids:  # never prune on an empty result
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM playlists WHERE kind = 'account' AND selected = 0 AND id NOT IN ({marks})", ids)
    conn.commit()
    return source


def _print_playlists(conn) -> None:
    rows = list_playlists(conn)
    _header("Playlists", f"{sum(r['synced'] for r in rows)} of {len(rows)} syncing")
    g = _grid()
    g.add_column(justify="right")
    g.add_column()
    g.add_column(overflow="ellipsis", no_wrap=True)
    g.add_column(justify="right")
    for r in rows:
        g.add_row(f"  [dim]{r['n']}[/]", "\u2705" if r["synced"] else "\u2b1c", f"[bold]{_esc(r['title'])}[/]",
                  str(r["tracks"]) if r["tracks"] else "[dim]-[/]")
    console.print(g)
    console.print("\n  [dim]\u2705 syncing   \u2b1c not syncing[/]")


@app.command()
def playlists(json_out: bool = typer.Option(False, "--json", help="The list as JSON (for the skill to present)."),
              sync_: str = typer.Option("", "--sync", help='Start syncing these: numbers or names, e.g. "1,3,Gym".'),
              stop: str = typer.Option("", "--stop", help="Stop syncing these: numbers or names.")) -> None:
    """Show your playlists (numbered; \u2705 syncing). With --sync / --stop: start or stop syncing some, by number or name."""
    cfg, conn = _ctx()
    logger = setup_logging(cfg, "playlists")
    if sync_ or stop:   # no refresh here: the numbers must match the list the user was just shown
        try:
            done = apply_selection(conn, sync_, stop)
        except ValueError as exc:
            console.print(f"[red]{exc}[/]. Nothing changed.")
            raise typer.Exit(2) from None
        if json_out:
            console.print_json(json.dumps({**done, "playlists": list_playlists(conn)}))
            return
        _print_playlists(conn)
        if done["leaving"]:
            console.print(f"\u26a0\ufe0f {done['leaving']} downloaded track(s) are only in the playlists you stopped; "
                          "the next sync moves them to .trash (kept 14 days). Preview: runsync sync --dry-run")
        return
    cookies = cfg.cookies if check_cookies(cfg.cookies).usable else None
    api = yt.open_api(cfg, conn)
    live, problem, source = False, "", "cookies"
    if cookies or api:
        try:
            source = _merge_remote(conn, cookies, api)
            live = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("playlist refresh failed: %s", exc)
            problem = "rejected" if ("401" in str(exc) or "sign in" in str(exc).lower() or cookies_rotated()) \
                else "failed"
    else:
        problem = "no cookies"

    rows = conn.execute("SELECT * FROM playlists ORDER BY title").fetchall()
    if json_out:   # facts only; the skill decides how to present them
        listed = [{**r, "source": "cookies" if source == "cookies" or r["id"] in fetch.API_UNREADABLE else "api"}
                  for r in list_playlists(conn)]
        console.print_json(json.dumps({"live": live, "problem": problem, "source": source, "playlists": listed}))
        return
    if problem:
        console.print("\u26a0\ufe0f [yellow]Could not refresh from YouTube; this is the saved list, not live.[/]")
        if problem == "rejected":
            _cookie_hint(force=True)
    if not rows:
        console.print("No playlists. Add cookies.txt, or use `runsync add-playlist <url>`.")
        raise typer.Exit(1)
    _print_playlists(conn)


@app.command("add-playlist")
def add_playlist(url: str) -> None:
    """Add a playlist by URL (for ones not in your account) and select it."""
    cfg, conn = _ctx()
    cookies = cfg.cookies if check_cookies(cfg.cookies).usable else None
    info = fetch.playlist_info(url, cookies)
    conn.execute("INSERT INTO playlists (id, title, url, kind, selected) VALUES (?, ?, ?, 'url', 1) "
                 "ON CONFLICT(id) DO UPDATE SET title = excluded.title, selected = 1",
                 (info.id, info.title, info.url))
    conn.commit()
    console.print(f"Added '{info.title}' ({info.id}).")


class CliReporter(Reporter):
    PREVIEW = 12

    def __init__(self, dry_run: bool) -> None:
        self.dry_run_mode = dry_run
        self.rows: list[tuple[str, int, int]] = []
        self.progress: Progress | None = None
        self.task = None
        self.results: list = []
        self.verb = "Downloading"
        self.est: list[tuple[str, int]] = []
        self.status = console.status("Fetching playlists...")
        self.status.start()

    def _stop_status(self):
        if self.status:
            self.status.stop()
            self.status = None

    def _out(self):
        return self.progress.console if self.progress else console

    def fetched(self, title, tracks, unreadable):
        self.rows.append((title, tracks, unreadable))

    def warn(self, msg):
        self._stop_status()
        console.print(f"[yellow]![/] {msg}", markup=True, highlight=False)

    def reconcile(self, files, missing, adopted, orphans):
        self._stop_status()
        self.disk = (files, missing, adopted, orphans)

    def diff(self, new, downloaded, pending, unavailable, removed):
        self._stop_status()
        _header("Playlists", f"{len(self.rows)} syncing")
        g = _grid()
        for title, n, u in self.rows:
            g.add_row(f"  {_esc(title)}", f"[bold]{n}[/]", f"[yellow]\u26a0\ufe0f {u} unreadable[/]" if u else "")
        console.print(g)
        console.print(f"  [dim]{sum(r[1] for r in self.rows)} tracks in total[/]")

        _header("Library")
        console.print("  " + _stats(("\u2705", downloaded, "!downloaded"), ("\u23f3", pending, "!queued"),
                                   ("\U0001f195", new, "new"), ("\u26a0\ufe0f", unavailable, "unavailable"),
                                   ("\U0001f5d1\ufe0f", removed, "to remove")))
        files, missing, adopted, orphans = getattr(self, "disk", (0, 0, 0, 0))
        notes = [f"{files} files in master/"]
        if missing:
            notes.append(f"[yellow]{missing} missing on disk, re-queued[/]")
        if adopted:
            notes.append(f"[green]{adopted} untracked adopted[/]")
        if orphans:
            notes.append(f"[yellow]{orphans} orphan files (not in DB, left alone)[/]")
        console.print("  [dim]" + " \u00b7 ".join(notes) + "[/]")

    def dry_run(self, queue_titles, queue_total, this_run, removed):
        if queue_titles:
            _header("Download queue", f"{queue_total} tracks \u00b7 this run takes {this_run}")
            g = _grid()
            for i, title in enumerate(queue_titles[: self.PREVIEW], 1):
                g.add_row(f"  [dim]{i:>2}[/]", _esc(_clip(title, 90)))
            console.print(g)
            if len(queue_titles) > self.PREVIEW:
                console.print(f"  [dim]\u2026 and {len(queue_titles) - self.PREVIEW} more[/]")
        else:
            console.print("\n\u2705 Nothing to download.")
        console.print("\n[dim]Dry run: nothing downloaded, nothing removed.[/]")

    def trashed(self, video_id):
        console.print(f"  [red]-[/] moved to trash: {video_id}")

    def download_start(self, n, total, video_id, title):
        self._stop_status()
        if self.progress is None:
            _header(f"{self.verb} {total} tracks")
            self.progress = Progress(SpinnerColumn(), BarColumn(bar_width=24), MofNCompleteColumn(),
                                     TimeElapsedColumn(), TextColumn("{task.description}"),
                                     console=console, transient=True)
            self.progress.start()
            self.task = self.progress.add_task("", total=total)
        self.progress.update(self.task, description=f"[dim]{_esc(_clip(title, 40))}[/]")
        self._title = title

    def download_retry(self, video_id, attempt, delay, reason):
        if self.progress:
            self.progress.update(self.task, description=f"[yellow]retry {attempt}/3 in {delay:.0f}s:[/] "
                                 f"[dim]{_esc(reason.splitlines()[0][:60])}[/]")

    def _line(self, mark: str, title: str, right: str) -> None:
        """One finished item, Homebrew-style: mark + name on the left, details right-aligned."""
        g = Table.grid(expand=True, padding=(0, 2, 0, 0))
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(f"{mark} {_esc(title)}", right)
        self._out().print(g)
        self.progress.advance(self.task)

    def download_ok(self, video_id, itag, codec, bitrate, size_bytes, seconds):
        kind = "AAC" if str(codec).startswith("mp4a") else str(codec).split(".")[0].upper()
        self.results.append(("ok", kind, size_bytes or 0))
        self._line("[green]✓[/]", self._title,
                   f"[{'green' if kind == 'AAC' else 'cyan'}]{kind:<4}[/] {bitrate:>4.0f}k  "
                   f"{(size_bytes or 0) / 1e6:>5.1f} MB  [bright_black]{seconds:>4.1f}s[/]")

    def download_unavailable(self, video_id, reason):
        self.results.append(("unavailable", "", 0))
        self._line("[yellow]–[/]", self._title, "[yellow]unavailable[/]")
        self._out().print(f"    [dim]{_esc(reason.splitlines()[0][:100])}[/]")

    def download_failed(self, video_id, reason):
        self.results.append(("failed", "", 0))
        self._line("[red]✗[/]", self._title, "[red]failed[/]")
        self._out().print(f"    [dim]{_esc(reason.splitlines()[0][:100])}[/]")

    def probe_ok(self, video_id, codec, bitrate, est_bytes, premium):
        kind = "AAC" if str(codec).startswith("mp4a") else str(codec).split(".")[0].upper()
        self.est.append((kind, est_bytes))
        tag = "[green]Premium[/]" if premium else "[bright_black]standard[/]"
        self._line("[green]✓[/]", self._title,
                   f"[{'green' if kind == 'AAC' else 'cyan'}]{kind:<4}[/] {bitrate:>4.0f}k  "
                   f"~{est_bytes / 1e6:>5.1f} MB  {tag}")

    def probe_failed(self, video_id, reason, permanent):
        mark, word = ("[yellow]–[/]", "[yellow]unavailable[/]") if permanent else ("[red]✗[/]", "[red]error[/]")
        self._line(mark, self._title, word)
        self._out().print(f"    [dim]{_esc(reason.splitlines()[0][:100])}[/]")

    def finish_downloads(self):
        self._stop_status()
        if self.progress:
            self.progress.stop()
            self.progress = None
        if self.est:
            total = sum(b for _, b in self.est)
            kinds = {k: sum(1 for x, _ in self.est if x == k) for k in dict.fromkeys(k for k, _ in self.est)}
            mix = ", ".join(f"{n} {k}" for k, n in kinds.items())
            _header(f"Estimate: {len(self.est)} tracks, ~{total / 1e6:,.0f} MB ({mix})")
            self.est = []
        if any(r[1] == "OPUS" for r in self.results):
            console.print("[dim]Note: OPUS masters are decoded to FLAC for the player copy (Phase 4).[/]")
        self.results = []


def _header(text: str, note: str = "") -> None:
    """Section headline: an accent bar, a bold title and an optional dim note."""
    console.print()
    console.print(f"[bold cyan]\u258d[/][bold]{text}[/]" + (f"  [dim]{note}[/]" if note else ""))


def _grid() -> Table:
    """Borderless, indented columns: the building block for every list in the CLI."""
    g = Table.grid(padding=(0, 2))
    g.pad_edge = False
    return g


def _list_table(*columns: tuple[str, str], expand: bool = False) -> Table:
    """Table for many rows: a single rule under a dim header, no boxes. columns = (name, 'left'|'right')."""
    t = Table(box=box.SIMPLE_HEAD, header_style="dim", show_edge=False, pad_edge=False, padding=(0, 2), expand=expand)
    for name, align in columns:
        t.add_column(name, justify=align, no_wrap=True, overflow="ellipsis")
    return t


def _stats(*items: tuple[str, int, str]) -> str:
    """'✅ 104 downloaded   ⏳ 380 queued': icon, count, label. A zero is left out unless the label starts with '!'."""
    return "   ".join(f"{icon} {n} {label.lstrip('!')}" for icon, n, label in items if n or label.startswith("!"))


def _esc(text: str) -> str:
    from rich.markup import escape
    return escape(text)


def _summary(res: RunResult) -> None:
    _header("Done")
    console.print("  " + _stats(("\u2705", len(res.added), "!added"), ("\U0001f5d1\ufe0f", len(res.removed), "removed"),
                               ("\u274c", len(res.failed), "failed"), ("\u26a0\ufe0f", len(res.unavailable), "unavailable")))
    if res.anonymous:
        console.print("  [yellow]\u26a0\ufe0f Anonymous run: reduced quality (no Premium cookies).[/]")
    if res.aborted:
        console.print(f"  [red]\u274c Stopped:[/] {_esc(res.aborted)}")


@app.command()
def sync(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; write nothing."),
         anonymous: bool = typer.Option(False, help="Skip cookies (reduced quality)."),
         force_delete: bool = typer.Option(False, help="Override the mass-delete guard."),
         verbose: bool = typer.Option(False, "--verbose", "-v", help="Also print debug logs to stderr."),
         probe: bool = typer.Option(False, "--probe", help="With --dry-run: ask YouTube each track's format, "
                                    "bitrate and size (no download). Checks as many tracks as --limit."),
         limit: int = typer.Option(None, "--limit", "-n", min=1, help="Max downloads this run (default: config)."),
         json_out: bool = typer.Option(False, "--json", help="Print the result as JSON on stdout (the normal "
                                       "output goes to stderr); for the skills to present.")
         ) -> None:
    """Fetch selected playlists, trash removed tracks, download new ones."""
    console.stderr = json_out   # --json: facts on stdout, the human view on stderr
    cfg, conn = _ctx()
    if limit is not None:
        cfg.max_downloads_per_run = limit
    logger = setup_logging(cfg, "sync", verbose)
    cookies = resolve_cookies(cfg, anonymous, _interactive())
    logger.info("cookie check: %s", check_cookies(cfg.cookies).state if not anonymous else "skipped (--anonymous)")

    if probe and not dry_run:
        console.print("[yellow]--probe only applies to --dry-run.[/]")
        raise typer.Exit(1)
    reporter = CliReporter(dry_run)
    if probe:
        reporter.verb = "Probing"
    needs_lock = (not dry_run) or probe
    try:
        with run_lock(cfg.library) if needs_lock else nullcontext():
            res = run_sync(cfg, conn, cookies, dry_run=dry_run, force_delete=force_delete,
                           log=logger.info, rep=reporter, probe=probe, api=yt.open_api(cfg, conn))
    except AlreadyRunning as exc:
        console.print(f"[red]{exc}[/]. Wait for it to finish, or stop it (Ctrl-C) and retry.")
        raise typer.Exit(3) from None
    finally:
        reporter.finish_downloads()
    logger.info("done: %s", res.summary())
    _cookie_hint()
    console.print(f"[dim]Log: {cfg.log_path}[/]")
    if not dry_run or res.aborted:
        _summary(res)
    if json_out:
        queued = conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'pending'").fetchone()[0]
        sys.stdout.write(json.dumps({**res.to_dict(), "dry_run": dry_run, "queued_left": queued}) + "\n")
    if res.aborted:
        raise typer.Exit(2)


ICONS = {   # one icon per outcome; the legend under the table lists only the ones that appeared
    "ok": ("\u2713", "green", "ok"),
    "named": ("\u2248", "green", "ok, file named 'Title (Artist)' because another song has the same title"),
    "noartist": ("\u2717", "yellow", "no artist found: needs you"),
    "low": ("!", "yellow", "low confidence: needs you"),
    "unconfirmed": ("?", "yellow", "artist guessed from the title only: needs you"),
    "letters": ("~", "yellow", "title has letters the player cannot show: needs you"),
    "dup": ("=", "dim", "same song as another track: not copied (Phase 3 decides)"),
    "notaac": ("\u2192", "dim", "not AAC: no player copy until Phase 4"),
    "other": ("\u2022", "yellow", "see the log"),
}


def _icon_for(reason: str) -> str:
    r = reason.lower()
    return ("noartist" if "no artist" in r else "low" if "low confidence" in r else
            "unconfirmed" if "not confirmed" in r else "letters" if "cannot show" in r or "cannot hold" in r else
            "dup" if r.startswith("duplicate") else "notaac" if "phase 4" in r else "other")


class ResolveCli(ResolveReporter):
    """One row per track, printed as a single table at the end: icon, before, artist, title, language."""

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.rows: dict[str, dict] = {}
        self.status = None

    def start(self, total: int) -> None:
        _header(f"{'Would resolve' if self.dry_run else 'Resolving'} {total} track{'s' if total != 1 else ''}")
        self.status = console.status("[dim]looking up tracks...[/]")
        self.status.start()

    def resolved(self, video_id: str, title: str, cand: Candidate, review: str | None,
                 languages: tuple[str, ...] = ()) -> None:
        self.rows[video_id] = {
            "before": title or "(no title)", "artist": ", ".join(cand.artists[:3]) or "-", "title": cand.title,
            "lang": "/".join(languages) or "-", "icon": _icon_for(review) if review else "ok"}
        if self.status:
            self.status.update(f"[dim]{len(self.rows)} done, last: {_esc(_clip(cand.title, 40))}[/]")

    def built(self, video_id: str, filename: str, size: int) -> None:
        row = self.rows.get(video_id)
        if row and filename != (safe_filename(row["title"]) or "") + ".m4a":
            row["icon"] = "named"

    def skipped(self, video_id: str, reason: str) -> None:
        row = self.rows.get(video_id)
        if row and row["icon"] == "ok":
            row["icon"] = _icon_for(reason)

    def warn(self, msg: str) -> None:
        console.print(f"[yellow]![/] {_esc(msg)}")

    def finish(self) -> None:
        if self.status:
            self.status.stop()
        if not self.rows:
            return
        t = _list_table(("", "left"), ("Before (YouTube title)", "left"), ("Artist", "left"), ("Title", "left"),
                        ("Lang", "left"), expand=True)
        for col, ratio in zip(t.columns[1:4], (4, 3, 3), strict=True):
            col.ratio = ratio
        for r in self.rows.values():
            sym, colour, _ = ICONS[r["icon"]]
            t.add_row(f"[{colour}]{sym}[/]", _esc(r["before"]), _esc(r["artist"]), _esc(r["title"]), r["lang"])
        console.print(t)
        used = list(dict.fromkeys(r["icon"] for r in self.rows.values()))
        console.print("  [dim]" + "   ".join(f"{ICONS[k][0]} {ICONS[k][2]}" for k in ICONS if k in used) + "[/]")


def _resolve_summary(res: ResolveResult, cfg: Config, dry_run: bool) -> None:
    _header("Summary", "preview, nothing written" if dry_run else "")
    tiers = ", ".join(f"{n} {t}" for t, n in sorted(res.by_tier.items())) or "none"
    ok = res.resolved - res.review
    console.print("  " + _stats(("\u2705", ok, "!resolved"), ("\u26a0\ufe0f", res.review, "need you"),
                               ("\u274c", res.lookups_failed, "lookups failed")))
    console.print(f"  [dim]{tiers} \u00b7 {res.lookups_fetched} looked up, {res.lookups_cached} from cache[/]")
    if not dry_run:
        console.print("  " + _stats(("\U0001f3b5", res.built, "!built"),
                                   ("\u23f8\ufe0f", res.held, "held for review"),
                                   ("\u2192", res.build_skipped, "skipped")))
    if res.unlabelled:
        names = ", ".join(sorted(res.unlabelled, key=lambda n: -res.unlabelled[n])[:6])
        more = f" +{len(res.unlabelled) - 6} more" if len(res.unlabelled) > 6 else ""
        console.print(f"  [dim]no language yet for {len(res.unlabelled)} artists: {names}{more}[/]")
    if res.imported_fixes:
        console.print(f"  \u2705 applied {res.imported_fixes} fix(es) from review.csv")
    if res.enrich_applied:
        console.print(f"  \u2705 applied {res.enrich_applied} search answer(s); run resolve again to use them")
    if res.enrich_open and not dry_run:
        console.print(f"\n\U0001f50e {res.enrich_open} row(s) wait for a web search: run the [bold]runsync-enrich[/] "
                      "skill in Claude Code, then resolve again.")
        console.print(f"  [dim]or fix them by hand in {cfg.review_path}[/]")


@app.command()
def resolve(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be resolved; write nothing."),
            offline: bool = typer.Option(False, "--offline", help="Skip MusicBrainz/iTunes/cover lookups."),
            no_build: bool = typer.Option(False, "--no-build", help="Resolve metadata only; no player copies."),
            redo: bool = typer.Option(False, "--redo", help="Re-resolve every track, not just new/unsure ones (your overrides still win)."),
            verbose: bool = typer.Option(False, "--verbose", "-v", help="Also print debug logs to stderr."),
            limit: int = typer.Option(None, "--limit", "-n", min=1, help="Max tracks this run."),
            json_out: bool = typer.Option(False, "--json", help="Print the result as JSON on stdout (the normal "
                                          "output goes to stderr); for the skills to present.")) -> None:
    """Clean titles into artist/title/album/year/language, then build tagged player copies."""
    console.stderr = json_out   # --json: facts on stdout, the human view on stderr
    cfg, conn = _ctx()
    logger = setup_logging(cfg, "resolve", verbose)
    http = None if (offline or not cfg.online) else Http(conn, cache_days=cfg.http_cache_days)
    rep = ResolveCli(dry_run)
    try:
        with run_lock(cfg.library) if not dry_run else nullcontext():
            res = run_resolve(cfg, conn, dry_run=dry_run, limit=limit, http=http, build=not no_build, redo=redo, rep=rep)
    except AlreadyRunning as exc:
        console.print(f"[red]{exc}[/]. Wait for it to finish, then retry.")
        raise typer.Exit(3) from None
    finally:
        rep.finish()
    logger.info("done: %s", res)
    _resolve_summary(res, cfg, dry_run)
    console.print(f"[dim]Log: {cfg.log_path}[/]")
    if json_out:
        sys.stdout.write(json.dumps({**res.to_dict(), "dry_run": dry_run}) + "\n")


def _print_dedupe(plan) -> None:
    """Each song once, its uploads indented under it; the upload's own title tells them apart."""
    def secs(m) -> str:
        return f"{int(m.seconds) // 60}:{int(m.seconds) % 60:02d}"

    def block(song: str, rows: list[tuple[str, str, object, str]]) -> None:
        console.print(f"\n  [bold]{_esc(song)}[/]")
        g = _grid()
        for icon, tag, m, note in rows:
            g.add_row(f"    {icon}", f"[bold]{tag}[/]" if tag else "", _esc(_clip(m.youtube_title or m.label(), 48)),
                      f"[dim]{_esc(_clip(m.channel, 18))}[/]", secs(m), f"[dim]{note}[/]")
        console.print(g)

    _header("Duplicates", f"{plan.removals} to delete \u00b7 {len(plan.unsure)} to decide")
    for g in plan.groups:
        block(g.winner.label(), [("\u2705", "", g.winner, "keep")]
              + [("\U0001f5d1\ufe0f", "", m, f"delete \u00b7 {s:.0%} match" if s is not None else "delete \u00b7 you said same")
                 for m, s in g.losers])
    for u in plan.unsure:
        note = f"{u.similarity:.0%} match" if u.similarity is not None else "could not compare"
        block(u.a.label(), [("\u2753", "A", u.a, ""), ("\u2753", "B", u.b, note)])
    console.print("\n  [dim]\u2705 keep   \U0001f5d1\ufe0f delete   \u2753 same song? you decide[/]")


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
    cfg, conn = _ctx()
    setup_logging(cfg, "dedupe")
    try:
        for text, verdict in ((same, True), (different, False)):
            for pair in filter(None, (x.strip() for x in text.split(";"))):   # "A,B;C,D" records several pairs
                a, b = (x.strip() for x in pair.split(","))
                record_decision(conn, a, b, verdict)
        if undo_id:
            winner = dedupe_undo(conn, undo_id)
            console.print(f"{undo_id} is back (it downloads on the next sync); it will not be merged into {winner} again.")
            return
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None
    plan = build_plan(conn)
    if json_out:
        console.print_json(json.dumps(plan_to_dict(plan)))
        return
    if not plan.groups and not plan.unsure:
        console.print("\u2705 No duplicates.")
        return
    _print_dedupe(plan)
    if not apply:
        if plan.removals:
            pid = plan_id(plan)
            console.print(f"{plan.removals} to delete. Nothing changed. To delete exactly these: "
                          f"[bold]runsync dedupe --apply --confirm {pid}[/]")
        return
    if confirm != plan_id(plan):   # the gate is in the command, not only in the skill's instructions
        console.print(f"[red]Not confirmed.[/] --apply needs --confirm {plan_id(plan)} (the id of the plan above); "
                      "a plan that changed since you confirmed is refused. Nothing deleted.")
        raise typer.Exit(2)
    try:
        with run_lock(cfg.library):
            n = apply_plan(cfg, conn, plan, force)
    except AlreadyRunning as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(3) from None
    except MassDeleteError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None
    console.print(f"Deleted {n} duplicate(s).")


def _minutes(m: float) -> str:
    m = max(1, round(m))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min" if m % 60 else f"{m // 60} h"


@app.command()
def status(json_out: bool = typer.Option(False, "--json", help="Facts as JSON (for the skill to present).")) -> None:
    """Progress at a glance: downloading, tagging or idle. Read-only, no network."""
    from datetime import UTC, datetime, timedelta
    cfg, conn = _ctx()
    n = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tracks GROUP BY status")}
    done, queued, gone = n.get("downloaded", 0), n.get("pending", 0), n.get("unavailable", 0)
    total = done + queued
    command = running_command(cfg.library)
    running = command is not None
    now = datetime.now(UTC)
    cut = (now - timedelta(minutes=10)).isoformat()
    rate = conn.execute("SELECT COUNT(*) FROM tracks WHERE downloaded_at > ?", (cut,)).fetchone()[0] / 10
    tag_rate = conn.execute("SELECT COUNT(*) FROM metadata WHERE resolved_at > ?", (cut,)).fetchone()[0] / 10
    last = conn.execute("SELECT MAX(downloaded_at) FROM tracks").fetchone()[0]
    quiet = (now - datetime.fromisoformat(last)).total_seconds() / 60 if last else 0
    untagged = conn.execute("SELECT COUNT(*) FROM tracks t LEFT JOIN metadata m USING (video_id) "
                            "WHERE t.status = 'downloaded' AND m.video_id IS NULL").fetchone()[0]
    need_you = conn.execute("SELECT COUNT(*) FROM metadata WHERE needs_review = 1").fetchone()[0]
    copies = conn.execute("SELECT COUNT(*) FROM tracks WHERE player_path IS NOT NULL").fetchone()[0]
    copies_target = conn.execute("SELECT COUNT(*) FROM tracks t JOIN metadata m USING (video_id) WHERE "
                                 "t.status = 'downloaded' AND m.needs_review = 0").fetchone()[0]
    activity = {"sync": "downloading", "resolve": "tagging", "dedupe": "deduping"}.get(command or "", "")
    if command == "unknown":
        activity = "working"
    if not activity:
        activity = "idle"
    tagged = done - untagged

    if json_out:
        console.print_json(json.dumps({
            "state": "running" if running else ("done" if not queued else "stopped"), "activity": activity,
            "downloaded": done, "queued": queued, "total": total, "unavailable": gone, "untagged": untagged,
            "tagged": tagged, "need_you": need_you, "player_copies": copies, "copies_target": copies_target,
            "per_min": round(rate, 1), "eta_min": round(queued / rate) if running and rate > 0 else None,
            "tag_per_min": round(tag_rate, 1),
            "tag_eta_min": round(untagged / tag_rate) if activity == "tagging" and tag_rate > 0 else None,
            "minutes_since_last_download": round(quiet, 1)}))
        return

    def bar(frac: float) -> str:
        full = round(frac * 24)
        on, off = "\u25b0" * full, "\u25b1" * (24 - full)
        return f"[green]{on}[/][dim]{off}[/]  [bold]{frac:.0%}[/]"

    if activity == "tagging" and not untagged and copies < copies_target:
        console.print("[bold yellow]\u23f3 Building player copies[/]")
        console.print(bar(copies / copies_target))
        console.print(f"{copies} of up to {copies_target} copies built")
        return
    if activity == "tagging":
        console.print("[bold yellow]\u23f3 Tagging[/]")
        console.print(bar(tagged / done if done else 1.0))
        console.print(f"{tagged} of {done} tracks tagged \u00b7 {untagged} left")
        if tag_rate > 0:
            console.print(f"[dim]\u2248 {tag_rate:.0f} per min \u00b7 about {_minutes(untagged / tag_rate)} left[/]")
        return
    if not queued:
        console.print("[bold green]\u2705 Everything is downloaded[/]")
    elif running:
        console.print("[bold yellow]\u23f3 Downloading[/]")
    else:
        console.print(f"[bold]\u23f8\ufe0f Stopped[/] [dim]\u00b7 {queued} waiting[/]")
    console.print(bar(done / total if total else 1.0))
    console.print(f"{done} of {total} tracks" + (f" [dim]\u00b7 {queued} left[/]" if queued else ""))
    if running and queued:
        if rate > 0:
            console.print(f"[dim]\u2248 {rate:.0f} per min \u00b7 about {_minutes(queued / rate)} left[/]")
        elif quiet >= 3:
            console.print(f"[yellow]no new download for {_minutes(quiet)}[/]")
    extras = [x for x in (f"\u26a0\ufe0f {gone} unavailable" if gone else "",
                          f"\U0001f3f7\ufe0f {untagged} to tag" if untagged else "") if x]
    if extras:
        console.print()
        console.print("   ".join(extras))


def _size(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _print_card(state: dict) -> None:
    s = state["state"]
    if s == "unregistered":
        _header("Card", "no card registered")
        if state["candidates"]:
            g = _grid()
            for v in state["candidates"]:
                g.add_row(f"  [bold]{_esc(v['name'])}[/]", v["fs"], f"{_size(v['free_bytes'])} free", f"[dim]{v['mount']}[/]")
            console.print(g)
            console.print("\n  [dim]Register yours with: runsync card --register <mount path>[/]")
        else:
            console.print("  \u23f8\ufe0f No card found. Insert it and run again.")
        return
    if s == "not_inserted":
        _header("Card", state["device"])
        console.print("  \u23f8\ufe0f Not inserted")
        return
    _header("Card", f"{state['device']} \u00b7 {state['fs']} \u00b7 {_size(state['free_bytes'])} free")
    console.print("  " + _stats(("\u2705", state["unchanged"], "!on the card already"), ("\u2795", state["adds"], "to add"),
                               ("\U0001f504", state["updates"], "to update"), ("\U0001f5d1\ufe0f", state["removes"], "to remove")))
    if state["adds"] or state["updates"]:
        console.print(f"  [dim]{_size(state['add_bytes'])} to copy[/]")
    for pl in state["playlists"]:
        console.print(f"  [dim]\U0001f4cb {_esc(pl['name'])} \u00b7 {pl['tracks']} tracks[/]")
    why = {"guard": "more than 20% of the card would be removed (needs --force)",
           "space": "not enough free space on the card",
           "conflict": f"files on the card have our names but are not ours: {', '.join(state.get('conflicts', [])[:3])}"}
    if state["blocked"]:
        console.print(f"  [red]\u274c Blocked: {why[state['blocked']]}[/]")
    elif state["adds"] or state["updates"] or state["removes"]:
        console.print(f"\n  To sync exactly this: [bold]runsync card --apply --confirm {state['plan_id']}[/]")
    else:
        console.print("\n  \u2705 The card is up to date.")


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
    volumes = candidate_volumes(cfg.library)
    try:
        if register:
            info = volume_info(Path(register))
            if info is None:
                raise ValueError(f"{register} is not a mounted volume macOS can identify")
            register_device(conn, info, name, cfg.library)
            console.print(f"\u2705 Registered {_esc(name)}: {_esc(info.name)} ({info.fs}). Only this card will be written.")
    except ValueError as exc:
        console.print(f"[red]{_esc(str(exc))}[/]")
        raise typer.Exit(2) from None
    state = card_state(conn, cfg.library, volumes)
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
                    console.print(f"\u21a9\ufe0f Undid the last sync: {r.removed} file(s) removed, {r.added} put back.")
                else:
                    if confirm != card_plan_id(plan):   # the gate is in the command, not only in the skill's instructions
                        console.print(f"[red]Not confirmed.[/] --apply needs --confirm {card_plan_id(plan)} (the id of the "
                                      "plan above); a plan that changed since you confirmed is refused. Nothing written.")
                        raise typer.Exit(2)
                    r = apply_card_plan(conn, dev["id"], mount, dev["subpath"], plan, force)
                    result = {"applied": {"added": r.added, "updated": r.updated, "removed": r.removed, "bytes": r.bytes,
                                          "playlists": r.playlists}}
                    console.print(f"\u2705 Synced: {r.added} added, {r.updated} updated, {r.removed} removed "
                                  f"({_size(r.bytes)} copied), {r.playlists} playlist file(s).")
        except AlreadyRunning as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(3) from None
        except (OSError, ValueError) as exc:
            console.print(f"[red]{_esc(str(exc))}[/] Nothing more was written.")
            raise typer.Exit(2) from None
        state = card_state(conn, cfg.library, candidate_volumes(cfg.library))
    elif apply or undo:
        console.print("[red]No registered card is inserted.[/] Nothing written.")
        raise typer.Exit(2)
    if eject and state.get("mount"):
        done = subprocess.run(["diskutil", "eject", state["mount"]], capture_output=True, text=True, check=False)
        result["ejected"] = done.returncode == 0
        console.print("\u23cf\ufe0f Ejected. Safe to remove the card." if done.returncode == 0
                      else f"[yellow]Could not eject: {_esc(done.stderr.strip()[:120])}[/]")
    if json_out:
        sys.stdout.write(json.dumps({**state, **result}) + "\n")
        return
    _print_card(state)


@app.command()
def youtube(login: bool = typer.Option(False, "--login", help="Sign in to YouTube in your browser (once)."),
            cleanup: bool = typer.Option(False, "--cleanup", help="Preview removing repeats and merged duplicates from "
                                         "your YouTube playlists (nothing changes)."),
            apply: bool = typer.Option(False, "--apply", help="Run the cleanup (needs --confirm)."),
            confirm: str = typer.Option("", "--confirm", help="With --apply: the plan id shown by the preview. Only that "
                                        "exact plan runs; any other is refused."),
            batch: int = typer.Option(ytclean.DEFAULT_BATCH, "--batch", help="With --apply: most operations per run."),
            undo: bool = typer.Option(False, "--undo", help="Reverse the last cleanup or add run."),
            search: str = typer.Option("", "--search", help="Search YouTube for a song (no quota); lists candidates."),
            replacements: bool = typer.Option(False, "--replacements", help="Search a replacement for each song YouTube "
                                              "no longer serves (no quota; nothing changes)."),
            add: str = typer.Option("", "--add", help="Video ids to add (comma separated); needs --to."),
            to: str = typer.Option("", "--to", help="With --add: your playlist's name or id."),
            create: bool = typer.Option(False, "--create", help="With --add: make the playlist (private) if it is missing."),
            json_out: bool = typer.Option(False, "--json", help="Print facts as JSON on stdout (for the skill).")) -> None:
    """Show the YouTube API login, today's quota and your playlists. --login signs in; --cleanup previews the cleanup."""
    console.stderr = json_out
    cfg, conn = _ctx()
    setup_logging(cfg, "youtube")
    api = yt.Api(cfg.youtube_token, cfg.youtube_client, conn, cfg.youtube_budget)
    facts: dict = {"client_file": cfg.youtube_client.exists(), "logged_in": api.logged_in(),
                   "budget": cfg.youtube_budget, "units_today": api.units_today()}

    def fail(message: str, code: int = 2):
        facts["error"] = message
        console.print(f"[red]{_esc(message)}[/]")
        if json_out:
            sys.stdout.write(json.dumps(facts) + "\n")
        raise typer.Exit(code)

    try:
        if login:
            if not cfg.youtube_client.exists():
                raise yt.AuthError(f"{cfg.youtube_client.name} is missing: create a Desktop OAuth client in Google Cloud "
                                   "and save its JSON in the repo folder.")
            yt.login(cfg.youtube_client, cfg.youtube_token,
                     announce=lambda m: console.print(f"\U0001f310 {_esc(m)}"))
            facts["logged_in"] = True
            console.print("\u2705 Signed in. The login is saved in youtube_token.json (owner-only, git-ignored).")
        if search:
            facts["search"] = {"query": search, "candidates": [ytsearch.as_dict(c) for c in ytsearch.candidates(search)]}
        if replacements:
            lost, nameless = ytsearch.lost_songs(conn)
            for song in lost:
                if song.in_library is None:
                    song.candidates = ytsearch.candidates(ytsearch.query_for(song.title))
            facts["replacements"] = {"no_title": nameless, "songs": [
                {"video_id": s_.video_id, "title": s_.title, "reason": s_.reason, "in_library": s_.in_library,
                 "candidates": [ytsearch.as_dict(c) for c in s_.candidates]} for s_ in lost]}
        if (cleanup or apply or undo or add) and not facts["logged_in"]:
            raise yt.AuthError(f"Not signed in to YouTube. {yt.LOGIN_HINT}")
        if add and not to:
            raise ytclean.PlanRefused("--add needs --to <playlist name or id>.")
        if apply and not (cleanup or add):
            cleanup = True
        if add:
            ids = [v.strip() for v in add.split(",") if v.strip()]
            with run_lock(cfg.library) if apply else nullcontext():
                aplan, skipped = ytclean.build_add_plan(api, conn, ids, to, create, ytsearch.video_titles(ids))
                facts["add"] = {"plan_id": ytclean.plan_id(aplan), "playlist": next(iter(aplan.playlists.values())),
                                "creates": any(o.kind == "create" for o in aplan.ops), "cost": aplan.cost,
                                "videos": [{"video_id": o.video_id, "title": o.title} for o in aplan.ops if o.kind == "add"],
                                "skipped": skipped}
                if apply:
                    r = ytclean.apply_plan(api, conn, aplan, confirm, batch)
                    facts["applied"] = {"added": r.added, "created": r.created, "left": r.left, "stopped": r.stopped}
                    console.print(f"\u2705 Added {r.added} song(s)" + (", playlist created" if r.created else "")
                                  + (f" ({_esc(r.stopped)})" if r.stopped else "."))
            facts["units_today"] = api.units_today()
        elif cleanup or undo:
            with run_lock(cfg.library) if (apply or undo) else nullcontext():
                if undo:
                    r = ytclean.undo_last_run(api, conn)
                    facts["undone"] = {"restored": r.restored, "removed": r.removed, "left": r.left, "stopped": r.stopped}
                    console.print(f"\u21a9\ufe0f Undid the last cleanup: {r.restored} put back, {r.removed} taken out"
                                  + (f", {r.left} left ({_esc(r.stopped)})" if r.left else "."))
                else:
                    plan = ytclean.build_plan(api, conn)
                    facts["cleanup"] = {**ytclean.summary(plan), "days": ytclean.days_needed(plan, cfg.youtube_budget),
                                        "batch": batch,
                                        "items": [{"op": o.kind, "why": o.why, "playlist": plan.playlists[o.playlist_id],
                                                   "video_id": o.video_id, "title": o.title, "keep_id": o.keep_id}
                                                  for o in plan.ops]}
                    if apply:
                        r = ytclean.apply_plan(api, conn, plan, confirm, batch)
                        facts["applied"] = {"deleted": r.deleted, "added": r.added, "left": r.left, "stopped": r.stopped}
                        console.print(f"\u2705 Cleaned: {r.deleted} removed, {r.added} added, {r.left} still to do"
                                      + (f" ({_esc(r.stopped)})" if r.stopped else "."))
            facts["units_today"] = api.units_today()
        elif facts["logged_in"]:
            facts["playlists"] = yt.my_playlists(api)
            facts["units_today"] = api.units_today()
    except AlreadyRunning as exc:
        fail(str(exc), 3)
    except (ytclean.PlanRefused, ytclean.NoSuchPlaylist) as exc:
        fail(str(exc))
    except yt.YouTubeError as exc:
        fail(str(exc))
    if json_out:
        sys.stdout.write(json.dumps(facts) + "\n")
        return
    if "add" in facts and "applied" not in facts:
        a = facts["add"]
        console.print(f"\u2795 Add plan {a['plan_id']}: {len(a['videos'])} song(s) to {_esc(a['playlist'])}"
                      f"{' (new)' if a['creates'] else ''}. Apply with: runsync youtube --add ... --to ... --apply --confirm {a['plan_id']}")
        return
    if "search" in facts or "replacements" in facts:
        for c in facts.get("search", {}).get("candidates", []):
            console.print(f"  {c['video_id']}  {_esc(c['title'])} \u00b7 {_esc(c['channel'])}")
        for song in facts.get("replacements", {}).get("songs", []):
            console.print(f"\U0001f3b5 {_esc(song['title'])}" + (" (already in your library)" if song["in_library"] else ""))
            for c in song["candidates"][:3]:
                console.print(f"    {c['video_id']}  {_esc(c['title'])} \u00b7 {_esc(c['channel'])}")
        return
    if "cleanup" in facts and "applied" not in facts and "undone" not in facts:
        c = facts["cleanup"]
        console.print(f"\U0001f9f9 Cleanup plan {c['plan_id']}: {c['deletes']} to remove, {c['adds']} to add "
                      f"({c['cost']} quota units, {c['days']} day(s)). Apply with: runsync youtube --apply --confirm {c['plan_id']}")
        return
    console.print(("\u2705 Signed in" if facts["logged_in"] else "\u26a0\ufe0f Not signed in (run: runsync youtube --login)")
                  + f" \u00b7 quota today {facts['units_today']} of {facts['budget']}")
    for p in facts.get("playlists", []):
        console.print(f"  \U0001f4cb {_esc(p['title'])} \u00b7 {p['count']}")


def _clip(text: str, width: int = 72) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
