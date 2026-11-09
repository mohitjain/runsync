"""runsync CLI."""
from __future__ import annotations

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

from . import db, fetch
from .config import Config, load_config
from .cookies import check_cookies
from .lock import AlreadyRunning, run_lock
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


def _merge_remote(conn, cookies: Path) -> int:
    """Upsert account playlists from YouTube, keeping saved selection. Unselected ones that vanished are dropped."""
    found = fetch.fetch_account_playlists(cookies)
    for p in found:
        conn.execute("INSERT INTO playlists (id, title, url, kind) VALUES (?, ?, ?, 'account') "
                     "ON CONFLICT(id) DO UPDATE SET title = excluded.title", (p.id, p.title, p.url))
    ids = [p.id for p in found]
    if ids:  # never prune on an empty result
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM playlists WHERE kind = 'account' AND selected = 0 AND id NOT IN ({marks})", ids)
    conn.commit()
    return len(found)


def _print_playlists(conn) -> None:
    rows = list_playlists(conn)
    t = Table(box=box.ROUNDED, header_style="bold cyan", border_style="bright_black")
    t.add_column("#", justify="right")
    t.add_column("")
    t.add_column("Playlist", style="bold")
    t.add_column("Tracks", justify="right")
    for r in rows:
        t.add_row(str(r["n"]), "\u2705" if r["synced"] else "\u2b1c", _esc(r["title"]), str(r["tracks"]) if r["tracks"] else "-")
    console.print(t)
    console.print("\u2705 syncing   \u2b1c not syncing")


@app.command()
def playlists(sync_: str = typer.Option("", "--sync", help='Start syncing these: numbers or names, e.g. "1,3,Gym".'),
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
        _print_playlists(conn)
        if done["leaving"]:
            console.print(f"\u26a0\ufe0f {done['leaving']} downloaded track(s) are only in the playlists you stopped; "
                          "the next sync moves them to .trash (kept 14 days). Preview: runsync sync --dry-run")
        return
    cookies = cfg.cookies if check_cookies(cfg.cookies).usable else None
    if cookies:
        try:
            n = _merge_remote(conn, cookies)
            console.print(f"Refreshed from YouTube: {n} playlists.")
        except Exception as exc:  # noqa: BLE001
            console.print("\u26a0\ufe0f [yellow]Could not refresh from YouTube; this is the saved list, not live.[/]")
            logger.warning("playlist refresh failed: %s", exc)
            _cookie_hint(force="401" in str(exc) or "sign in" in str(exc).lower())
    else:
        console.print("[yellow]No valid cookies.txt; showing saved list (not refreshed).[/]")

    rows = conn.execute("SELECT * FROM playlists ORDER BY title").fetchall()
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
        _header(f"Playlists ({len(self.rows)} selected)")
        t = Table(box=box.ROUNDED, header_style="bold cyan", border_style="bright_black", expand=False)
        t.add_column("Playlist", style="bold", min_width=18)
        t.add_column("Tracks", justify="right")
        t.add_column("Unreadable", justify="right")
        for title, n, u in self.rows:
            t.add_row(_esc(title), str(n), f"[yellow]{u}[/]" if u else "[bright_black]-[/]")
        t.add_section()
        t.add_row("[dim]Total[/]", f"[dim]{sum(r[1] for r in self.rows)}[/]",
                  f"[dim]{sum(r[2] for r in self.rows) or '-'}[/]")
        console.print(t)

        _header("Library status")
        c = Table(box=box.ROUNDED, header_style="bold", border_style="bright_black", expand=False)
        for name, style, w in (("Downloaded", "green", 12), ("Pending", "cyan", 9), ("New", "bold green", 6),
                               ("Unavailable", "yellow", 13), ("To remove", "red", 11)):
            c.add_column(name, justify="center", style=f"bold {style}".replace("bold bold", "bold"),
                         header_style=f"bold {style}".replace("bold bold", "bold"), min_width=w)
        c.add_row(str(downloaded), str(pending), str(new), str(unavailable), str(removed))
        console.print(c)
        files, missing, adopted, orphans = getattr(self, "disk", (0, 0, 0, 0))
        notes = [f"{files} files in master/"]
        if missing:
            notes.append(f"[yellow]{missing} missing on disk, re-queued[/]")
        if adopted:
            notes.append(f"[green]{adopted} untracked adopted[/]")
        if orphans:
            notes.append(f"[yellow]{orphans} orphan files (not in DB, left alone)[/]")
        console.print("[dim]Disk check:[/] " + ", ".join(notes))

    def dry_run(self, queue_titles, queue_total, this_run, removed):
        if queue_titles:
            _header(f"Download queue: {queue_total} tracks, this run takes {this_run}")
            t = Table(box=box.ROUNDED, header_style="bold green", border_style="bright_black", expand=False)
            t.add_column("#", justify="right", style="bright_black", min_width=3, no_wrap=True)
            t.add_column("Title", no_wrap=True, overflow="ellipsis", max_width=72)
            for i, title in enumerate(queue_titles[: self.PREVIEW], 1):
                t.add_row(str(i), _esc(title))
            if len(queue_titles) > self.PREVIEW:
                t.add_section()
                t.add_row("", f"[dim]... and {len(queue_titles) - self.PREVIEW} more[/]")
            console.print(t)
        else:
            console.print("[green]Nothing to download.[/]")
        console.print("[dim]Dry run: nothing downloaded, nothing removed.[/]")

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


def _header(text: str) -> None:
    console.print(f"[bold blue]==>[/] [bold]{text}[/]")


def _esc(text: str) -> str:
    from rich.markup import escape
    return escape(text)


def _summary(res: RunResult) -> None:
    ok = "[bold blue]==>[/] [bold]Done[/]  "
    body = (f"[green]{len(res.added)} added[/] · [red]{len(res.removed)} removed[/] · "
            f"[yellow]{len(res.failed)} failed[/] · [dim]{len(res.unavailable)} unavailable[/]")
    console.print(ok + body)
    if res.anonymous:
        console.print("[yellow]Warning:[/] anonymous run, reduced quality (no Premium cookies).")
    if res.aborted:
        console.print(f"[red]Stopped:[/] {_esc(res.aborted)}")


@app.command()
def sync(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; write nothing."),
         anonymous: bool = typer.Option(False, help="Skip cookies (reduced quality)."),
         force_delete: bool = typer.Option(False, help="Override the mass-delete guard."),
         verbose: bool = typer.Option(False, "--verbose", "-v", help="Also print debug logs to stderr."),
         probe: bool = typer.Option(False, "--probe", help="With --dry-run: ask YouTube each track's format, "
                                    "bitrate and size (no download). Checks as many tracks as --limit."),
         limit: int = typer.Option(None, "--limit", "-n", min=1, help="Max downloads this run (default: config).")
         ) -> None:
    """Fetch selected playlists, trash removed tracks, download new ones."""
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
                           log=logger.info, rep=reporter, probe=probe)
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
        t = Table(box=box.ROUNDED, expand=True)
        t.add_column("", width=1, no_wrap=True)
        t.add_column("Before (YouTube title)", ratio=4, overflow="ellipsis", no_wrap=True)
        t.add_column("Artist", ratio=3, overflow="ellipsis", no_wrap=True)
        t.add_column("Title", ratio=3, overflow="ellipsis", no_wrap=True)
        t.add_column("Lang", no_wrap=True)
        for r in self.rows.values():
            sym, colour, _ = ICONS[r["icon"]]
            t.add_row(f"[{colour}]{sym}[/]", _esc(r["before"]), _esc(r["artist"]), _esc(r["title"]), r["lang"])
        console.print(t)
        used = list(dict.fromkeys(r["icon"] for r in self.rows.values()))
        console.print("  ".join(f"[{ICONS[k][1]}]{ICONS[k][0]}[/] [dim]{ICONS[k][2]}[/]" for k in ICONS if k in used))
        console.print("[dim]Player files are named <Title>.m4a[/]")


def _resolve_summary(res: ResolveResult, cfg: Config, dry_run: bool) -> None:
    _header("Summary")
    tiers = ", ".join(f"{n} {t}" for t, n in sorted(res.by_tier.items())) or "none"
    t = Table(box=box.ROUNDED, show_header=False)
    t.add_row("Resolved", f"{res.resolved} of {res.considered}  [dim]({tiers})[/]")
    pct = f" ({100 * res.review / res.resolved:.0f}%)" if res.resolved else ""
    t.add_row("Needs review", f"{res.review}{pct}")
    if not dry_run:
        t.add_row("Player copies", f"{res.built} built, {res.held} held for review, {res.build_skipped} skipped")
        t.add_row("Covers", str(res.covers))
    t.add_row("Lookups", f"{res.lookups_fetched} fetched, {res.lookups_cached} cached, {res.lookups_failed} failed")
    if res.unlabelled:
        names = ", ".join(sorted(res.unlabelled, key=lambda n: -res.unlabelled[n])[:8])
        more = f" (+{len(res.unlabelled) - 8} more)" if len(res.unlabelled) > 8 else ""
        t.add_row("No language yet", f"{len(res.unlabelled)} artists: {names}{more}")
    console.print(t)
    if res.imported_fixes:
        console.print(f"Applied {res.imported_fixes} fix(es) from review.csv.")
    if res.enrich_applied:
        console.print(f"Applied {res.enrich_applied} search suggestion(s) from enrich.csv "
                      "(stored with their source link). Run resolve again to use them.")
    if res.enrich_open and not dry_run:
        console.print(f"{res.enrich_open} row(s) wait for a web search in [bold]{cfg.enrich_path}[/]: open Claude Code "
                      "in this repo and run the [bold]runsync-enrich[/] skill, then run [bold]runsync resolve[/] again.")
        console.print(f"[dim]Or fix tracks by hand in {cfg.review_path} (fix_* columns).[/]")


@app.command()
def resolve(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be resolved; write nothing."),
            offline: bool = typer.Option(False, "--offline", help="Skip MusicBrainz/iTunes/cover lookups."),
            no_build: bool = typer.Option(False, "--no-build", help="Resolve metadata only; no player copies."),
            redo: bool = typer.Option(False, "--redo", help="Re-resolve every track, not just new/unsure ones (your overrides still win)."),
            verbose: bool = typer.Option(False, "--verbose", "-v", help="Also print debug logs to stderr."),
            limit: int = typer.Option(None, "--limit", "-n", min=1, help="Max tracks this run.")) -> None:
    """Clean titles into artist/title/album/year/language, then build tagged player copies."""
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


def _clip(text: str, width: int = 72) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
