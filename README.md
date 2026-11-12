# runsync

Personal music sync for runners: mirror your YouTube playlists to a microSD-based MP3 player (Shanling M0 Pro) with clean tags, cover art and no phone. **The playlist is the source of truth**, the Mac library is the cache, and devices are disposable mirrors.

See [PLAN.md](PLAN.md) for the build plan and locked decisions, [phase0-findings.md](phase0-findings.md) for what was verified, [CHANGELOG.md](CHANGELOG.md) for progress and [SECURITY.md](SECURITY.md) for handling cookies.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Pre-work (tooling, Premium format check, card test) | Tooling and Premium check done; card test pending ([findings](phase0-findings.md)) |
| 1 | Playlist discovery, fetch, diff, download to `master/` | Done; checkpoint passed on the real playlists (edge-case batch still to fix) |
| 2a | Metadata, language, covers, tagged player copies, web-search enrichment via a Claude Code skill (no API key) | Built; awaiting the review checkpoint |
| 3 | Dedupe | Done; run on the real library (96 duplicates removed) |
| 4 | Player build and safe sync to the card | Built and tested without a card; awaiting the first real card sync |
| 4b | YouTube API: read playlists without fresh cookies, clean duplicates out of them, add songs, replace lost ones | Built and tested; first real write pending your OK |
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

### YouTube login (optional, recommended)

The cookies above are still what downloads audio at Premium quality, but they rotate and break. Signing in to the official YouTube API removes that for **reading** your playlists, and lets runsync tidy and extend them.

1. In [Google Cloud](https://console.cloud.google.com) create a project, enable **YouTube Data API v3**, set up the consent screen (External, add yourself as a **test user**) and create an **OAuth client ID of type Desktop app**.
2. Save its JSON in the repo root as `client_secret.json` (**gitignored**, the guard blocks it).
3. Run `uv run runsync youtube --login`, approve in the browser. The login is saved in `youtube_token.json` (owner-only, **gitignored**). While the Google app is in testing mode the login may expire after 7 days; run `--login` again.

Playlists are then read through the API; Liked Music (`LM`) and Watch later still need the cookies. If the API fails for any reason runsync falls back to the cookies. Nothing is written to YouTube without a preview and `--confirm <plan id>`; quota use is tracked (10,000 units a day from Google; runsync stops at 8,000).

## Usage

The interface is Claude Code skills. Open Claude Code in this repo and say what you want:

| Say | Skill | Does |
|---|---|---|
| "sync my music" | `runsync` | the whole flow, asking only at choice points |
| "set up runsync" | `runsync-setup` | installs the tools, takes your cookie file, checks it |
| "choose playlists" | `runsync-playlists` | lists them (✅ syncing), asks which to start or stop |
| "enrich my library" | `runsync-enrich` | web-searches what the code could not settle |
| "status" / "how is it going" | `runsync-status` | progress at a glance; schedule it with `/loop 5m /runsync-status` during a long download |
| "sync to the card" | `runsync-sync-card` | registers your microSD card once, previews what changes, syncs only the confirmed plan, offers to eject |
| "remove duplicates" | `runsync-dedupe` | finds the same song uploaded twice, asks about doubtful pairs, deletes the extra |
| "clean my YouTube playlists" | `runsync-youtube` | signs in, previews repeats and merged duplicates in your YouTube playlists, removes them after you confirm (undoable) |
| "replace the unavailable songs" | `runsync-replace` | searches YouTube for the songs that were removed, you pick, they go into a private `rerunsync` playlist that the next refresh downloads |
| "add this song to Running" | `runsync-add` | searches, you pick the upload, confirm, added to the playlist |

The skills drive eight commands (most take `--json`, which they use to present results), which you can also run yourself:

```
uv run runsync playlists                       # numbered list: ✅ syncing, ⬜ not
uv run runsync playlists --sync "1,Gym" --stop 3   # start/stop by number or name
uv run runsync add-playlist <url>              # a playlist that isn't in your account

uv run runsync sync --dry-run                  # what would change (nothing downloaded or removed)
uv run runsync sync -n 5                       # download at most 5 tracks (default cap 50)
uv run runsync resolve --dry-run               # preview the clean artist/title for each track
uv run runsync resolve                         # tags, covers, ReplayGain, player copies
uv run runsync resolve --redo                  # re-resolve every track (your corrections still win)
uv run runsync status                          # progress bar, rate and time left
uv run runsync dedupe                          # show duplicates (nothing changes)
uv run runsync card                            # the registered card and what a sync would do (nothing changes)
uv run runsync card --apply --confirm ID       # sync exactly the previewed plan; --undo reverses the last sync, --eject when done
uv run runsync youtube                         # signed in? quota used today, your playlists
uv run runsync youtube --login                 # sign in once (opens your browser)
uv run runsync youtube --cleanup               # preview: repeats and merged duplicates in your YouTube playlists
uv run runsync youtube --apply --confirm ID    # run (the first --batch changes) exactly that plan; --undo reverses the last run
uv run runsync youtube --replacements          # search replacements for unavailable songs (no quota, nothing changes)
uv run runsync youtube --search "song artist"  # candidates for one song
uv run runsync youtube --add ID1,ID2 --to rerunsync --create   # preview adding songs (then --apply --confirm ID)
uv run runsync dedupe --apply --confirm ID     # delete exactly the previewed plan for good (the preview prints ID); --undo ID brings one back
```

`resolve` turns upload titles like `Humari Adhuri Kahani - Full Song | Arijit Singh | ...` into clean tags. It tries, in order: YouTube's own music fields, the title parser, MusicBrainz, iTunes (India store). It stops at the first confident answer and never lets a lower-trust result replace a higher one. Tracks it is not sure about go to `review.csv` in the library: fill the `fix_*` columns and run `resolve` again; fixes are stored in `state.db` and always win. What the code cannot settle (no artist, unknown language) is also written to `enrich.csv`, which the `runsync-enrich` skill fills from a web search; `resolve` applies only answers with a source link and confidence of at least 0.85. Languages are Hindi, English or Punjabi only, and a track inherits its artist's. `master/` is never touched: tagged copies with a 500x500 cover and ReplayGain go to `player/m4a/` (Opus/other formats wait for Phase 4).

### The flow

1. Playlists (once): choose which to sync.
2. `sync`: download new tracks to `master/`; removed ones go to `.trash/`.
3. `resolve`: clean artist/title/album/year/language, cover and ReplayGain; build `player/m4a/<Title>.m4a`.
4. Fix what is left: `review.csv` by hand, or the `runsync-enrich` skill.
5. `resolve` again: applies the fixes and rebuilds only the tracks that changed.
6. `dedupe`: same recording uploaded twice (compared by audio fingerprint, so a music video with an intro still matches) keeps the best source and deletes the other for good. A live, remix or cover version is never merged with the studio one. Pairs in the doubtful band are asked about and the answer is remembered.

Rules of the player copies: only real AAC audio is copied (Opus, AC-3 and other masters wait for Phase 4); a title with letters that cannot go in a file name is held for review; the same song from two videos is copied once and the other is held for Phase 3 (dedupe); a second song with the same title is named `Title (Artist)`. Language comes from your override, then what the upload says (script or words like "Punjabi"), then the lead artist; if nothing says, it stays blank.

Only one `sync` or `resolve` runs at a time. Every run checks the database against the files in `master/` (missing files are re-queued, stray files are reported, nothing is deleted).

Only **selected** playlists are synced. A track leaves the library only when it is absent from every selected playlist.

## Library layout

Default location `~/Music/RunSync` (override with `RUNSYNC_HOME`):

```
master/          untouched best audio stream per video ID
.trash/          removed tracks, purged after 14 days
state.db         SQLite state
player/m4a/      tagged AAC player copies (title-only plain-letter names), derived from master/
player/flac/     FLAC copies of the Opus and AC-3 masters
enrich.csv       rows waiting for the runsync-enrich skill (web search)
review.csv       tracks that need a human look
cache/covers/    downloaded cover art
config.yaml      optional overrides (cookies path, caps, thresholds)
logs/last_run.log
```

## Safety rules

- A failed, empty or partial fetch never deletes anything.
- Private or deleted videos are treated as unavailable, never as removed.
- Mass-delete guard: stops if more than 20% of tracks would be removed (`--force-delete` to override).
- Removed tracks go to `.trash/` for 14 days.
- Downloads go to `.part` files and pass an `ffprobe` check before entering `master/`; leftover partials from a killed run are cleaned up at the next start.
- YouTube writes (cleanup, adds) need a preview and `--confirm <plan id>` of that exact plan, add before they delete (a song never vanishes from a playlist), are journalled for `--undo`, never touch Liked Music, Liked videos or Watch later, and stop at 8,000 of Google's 10,000 daily quota units.
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
