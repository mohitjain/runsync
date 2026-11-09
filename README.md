# runsync

Personal music sync for runners: mirror your YouTube playlists to a microSD-based MP3 player (Shanling M0 Pro) with clean tags, cover art and no phone. **The playlist is the source of truth**, the Mac library is the cache, and devices are disposable mirrors.

See [PLAN.md](PLAN.md) for the build plan and locked decisions, [phase0-findings.md](phase0-findings.md) for what was verified, [CHANGELOG.md](CHANGELOG.md) for progress and [SECURITY.md](SECURITY.md) for handling cookies.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Pre-work (tooling, Premium format check, card test) | Tooling and Premium check done; card test pending ([findings](phase0-findings.md)) |
| 1 | Playlist discovery, fetch, diff, download to `master/` | Built and verified; awaiting the real-playlist checkpoint |
| 2 | Metadata, language, covers | Not started |
| 3 | Dedupe | Not started |
| 4 | Player build and safe sync to the card | Not started |
| 5 | Status, doctor, launchd automation, full import | Not started |

## Requirements

macOS, Python 3.11+, and:

```
brew install python uv ffmpeg chromaprint deno
```

`deno` plus the `yt-dlp-ejs` package (installed by `uv sync`) are needed so yt-dlp can solve YouTube's JS challenge; without them downloads fail with "Requested format is not available".

## Setup

```
uv sync
```

### Cookies (for YouTube Premium quality)

Without a login, YouTube exposes ~130 kbps AAC. Logged in with Premium, it also exposes ~258 kbps AAC, which the Shanling plays natively.

YouTube rotates cookies on any open browser tab, so export from a session that is never reused:

1. Open a new **Incognito** window and log in to youtube.com (only tab open).
2. In that tab go to `https://www.youtube.com/robots.txt`.
3. Export the `youtube.com` cookies with an extension such as "Get cookies.txt LOCALLY".
4. **Close the Incognito window.**
5. Save the file as `cookies.txt` in the repo root. It is **gitignored**; never commit or share it. It works as your login session.

When YouTube invalidates the session, `runsync` says so and prints these steps again.

If `cookies.txt` is missing or expired, `runsync` asks you to refresh it or continue anonymously at reduced quality. Non-interactive runs fall back automatically and note it in the log.

## Usage

```
uv run runsync playlists            # refresh from YouTube, tick the ones to sync, saved in the DB
uv run runsync add-playlist <url>   # add a playlist that isn't in your account

uv run runsync sync --dry-run                 # what would change (nothing downloaded or removed)
uv run runsync sync --dry-run --probe -n 20   # also ask YouTube each track's format, bitrate and size
uv run runsync sync -n 5                      # download at most 5 tracks this run
uv run runsync sync                           # fetch, trash removed tracks, download (default cap 50)
uv run runsync sync -v                        # also print debug logs
```

Only one `sync` runs at a time. Every run checks the database against the files in `master/` (missing files are re-queued, stray files are reported, nothing is deleted).

Only **selected** playlists are synced. A track leaves the library only when it is absent from every selected playlist.

## Library layout

Default location `~/Music/RunSync` (override with `RUNSYNC_HOME`):

```
master/          untouched best audio stream per video ID
.trash/          removed tracks, purged after 14 days
state.db         SQLite state
config.yaml      optional overrides (cookies path, caps, thresholds)
logs/last_run.log
```

## Safety rules

- A failed, empty or partial fetch never deletes anything.
- Private or deleted videos are treated as unavailable, never as removed.
- Mass-delete guard: stops if more than 20% of tracks would be removed (`--force-delete` to override).
- Removed tracks go to `.trash/` for 14 days.
- Downloads go to `.part` files and pass an `ffprobe` check before entering `master/`; leftover partials from a killed run are cleaned up at the next start.
- Retries: 3 attempts with 5s/30s/120s backoff, transient errors only.

## Development

```
uv run pytest
uv run ruff check .
python3 scripts/check_sensitive.py --all   # no cookies/databases/logs/secrets/audio in the repo
uv run pre-commit install                  # run the guard + ruff on every commit
```

CI runs the same guard, ruff and pytest on every PR. Logs are in `~/Music/RunSync/logs/` (`runsync.log`, `last_run.log`).

## License

Public domain ([Unlicense](LICENSE)): do whatever you want with it. No warranty.

## Terms

Downloading with yt-dlp is outside YouTube's terms of service, even with Premium. Personal use is the owner's call.
