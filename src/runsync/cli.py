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


def _cookie_hint() -> None:
    """If yt-dlp said the cookies were invalidated, give the exact fix."""
    if cookies_rotated():
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


@app.command()
def playlists() -> None:
    """Refresh playlists from YouTube, then pick which ones to sync (saved in the DB)."""
    cfg, conn = _ctx()
    cookies = cfg.cookies if check_cookies(cfg.cookies).usable else None
    if cookies:
        try:
            n = _merge_remote(conn, cookies)
            console.print(f"Refreshed from YouTube: {n} playlists.")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Could not refresh from YouTube ({exc}); showing saved list.[/]")
            _cookie_hint()
    else:
        console.print("[yellow]No valid cookies.txt; showing saved list (not refreshed).[/]")

    rows = conn.execute("SELECT * FROM playlists ORDER BY title").fetchall()
    if not rows:
        console.print("No playlists. Add cookies.txt, or use `runsync add-playlist <url>`.")
        raise typer.Exit(1)
    if not _interactive():
        for r in rows:
            console.print(f"{'[x]' if r['selected'] else '[ ]'} {r['title']}", markup=False)
        return

    import questionary
    chosen = questionary.checkbox(
        "Playlists to sync (space = toggle, enter = save)",
        choices=[questionary.Choice(r["title"], value=r["id"], checked=bool(r["selected"])) for r in rows]).ask()
    if chosen is None:
        console.print("Cancelled; selection unchanged.")
        raise typer.Exit(1)
    conn.execute("UPDATE playlists SET selected = 0")
    conn.executemany("UPDATE playlists SET selected = 1 WHERE id = ?", [(c,) for c in chosen])
    conn.commit()
    console.print(f"Saved: {len(chosen)} of {len(rows)} playlists selected.")


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


def _clip(text: str, width: int = 72) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
