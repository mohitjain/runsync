# runsync: design

One tool: YouTube playlists → clean, tagged, best-quality music → a Shanling M0 Pro microSD card. The playlist is the
source of truth, the Mac library is the cache, the card is a disposable mirror. **Trust matters more than features.**

## Decisions

| Area | Decision |
|---|---|
| Source of truth | Selected YouTube playlists. Added → appears on the card. Gone from every selected playlist → leaves the card. |
| Interface | Claude Code skills present everything; commands only supply facts (`--json`) and gated writes. |
| Quality | Best stream kept untouched in `master/`. Player copy derived from it; never lossy → lossy. AAC is copied as is, Opus and AC-3 are decoded to FLAC. No MP3. |
| Source choice | Highest bitrate wins; within 10% prefers a natively playable format (AAC over Opus). Premium cookies expose AAC 258 kbps (anonymous: 130); some uploads only have ~129. |
| Names and tags | Files are `Title.m4a` (`Title (Artist)` only when two songs share a title). Clean title, artist, album, year, language, 500x500 cover, ReplayGain. Identity is the video id in `state.db`, not the file name. |
| Metadata order | YouTube fields → title parser → MusicBrainz → iTunes (India store) → `enrich.csv` filled by a web-search skill (answers need a source link and confidence ≥ 0.85) → `review.csv`. Corrections (per track or artist) live in `state.db` and always win. |
| Language | Hindi, English, Punjabi only. Your override, then the upload's words, then the lead artist's label. |
| Dedupe | Same recording found by audio fingerprint; never merges live, remix or cover with the original. Asks about doubtful pairs. Deletes for good only with `--confirm <plan id>`; keeps an alias so `--undo` works. |
| Card | Identified by volume UUID, never the Mac's own disk. Only files in the card's manifest are ever deleted; copy to a temp name, fsync, verify; 20% removal guard; one M3U8 per playlist; undo of the last sync. |
| YouTube API | Official Data API v3 over OAuth (Desktop client, PKCE; token owner-only, gitignored). Reads playlists (cookies are the fallback and still serve audio and Liked Music), mirrors Liked Music (read-only, through the cookies) into your **library list**, a playlist we can control, so a like adds the song and an unlike removes it, and runsync syncs from that list; removes repeats, merged duplicates, dead songs and songs you named from your playlists, puts chosen replacements in place of the dead entries, adds songs. Your replacement picks are saved in `state.db` until the push puts them in place of the dead entries, then forgotten. Daily quota 10,000 units; budget 8,000 (`youtube_budget`). |
| Database | A snapshot of YouTube from the last sync plus the changes still to push. After a verified push, dead rows (unavailable, merged, trashed, aliases, picks, logs) are deleted; no history. Unavailable with no replacement means delete. |
| Quality | About 1 song in 7 was uploaded without the 256 kbps tier (even official channels). `quality` looks for another upload of the same recording that has it, gently (cookies are needed to see the tier and are throttled when abused), queues it, and the normal plan, download and dedupe replace the old one. Top-quality songs are never searched; a miss is retried after a month. |
| Safety | Failure is never removal: a failed or empty fetch deletes nothing. Removed tracks go to `.trash/` for 14 days. Mass-delete guard (>20%). Every destructive or outward write: preview, then `--confirm <plan id>` of that exact plan, journal, undo. |
| Stack | Python 3.11, uv, yt-dlp (+ deno and yt-dlp-ejs), ffmpeg, chromaprint, mutagen, SQLite, typer + rich, pytest, ruff. |

## Library (`~/Music/RunSync`, or `RUNSYNC_HOME`)

`master/` best audio per video · `player/m4a/` and `player/flac/` device copies · `.trash/` · `state.db` · `enrich.csv`,
`review.csv` · `cache/covers/` · `config.yaml` (optional overrides) · `logs/`.

Modules: `fetch` (read playlists) · `download` · `resolve/` (metadata) · `dedupe` · `player` · `card` · `sync` (the pipeline) ·
`quality` (better uploads) · `youtube` (API client) · `ytclean` (plan, push, verify, undo) · `ytsearch` (lost songs, picks) · `ytflow` (what `runsync
youtube` does) · `snapshot` (keeps the database a snapshot) · `status` · `cli` (options and a few plain lines).

## Learned

- `--cookies-from-browser` is blocked on macOS. Export `cookies.txt` from an Incognito window (visit `youtube.com/robots.txt` first, then close it) or YouTube rotates the session within minutes.
- yt-dlp needs `deno` + `yt-dlp-ejs` to solve YouTube's JS challenge; without them every download fails.
- Playlist reads no longer need cookies (the API matched yt-dlp: Running 94, Refresh 225 distinct, though Refresh lists 354 entries). Cookies are still needed for Premium audio and for Liked Music (`LM`), which the API cannot read.
- A playlist sorted by date added refuses insert positions (`manualSortRequired`); adds go to the default place.
- Removed videos surface as "Video unavailable" and are permanent (`unavailable`). Private or deleted ones are never treated as removed.
- Downloading with yt-dlp is outside YouTube's terms even with Premium; that personal-use call is the owner's.

## Open items (need the card and player)

- Confirm the player model, then test one Opus, one M4A and one FLAC, 500x500 covers and a 150-track M3U8 on the real card.
- Card filesystem (exFAT above 32 GB) and M3U8 behaviour over 100 entries; playlist files sit next to the audio with relative names.
- Phase 5: `doctor` (yt-dlp, tools, cookie and YouTube-login validity, disk), launchd automation, resumable first import, backups.
- Later: lyrics (`.lrc` sidecars), a second device profile (Hindi and Punjabi only), BPM and tempo playlists.

## Working agreement

Tests before features. Everything destructive supports `--dry-run` and has a test. Never delete on a failed, empty or partial
fetch. Never overwrite higher-trust metadata. Commit only when asked. If a decision here is unclear, ask.
