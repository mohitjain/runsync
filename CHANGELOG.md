# Changelog

## Unreleased

### YouTube API
- `runsync youtube`: official YouTube Data API v3 over OAuth (installed-app flow with PKCE and a loopback redirect, stdlib only). The token is saved owner-only in `youtube_token.json` (gitignored and blocked by the guard); only Google hosts are contacted; tokens and the client secret are never logged; every call is counted against a daily quota ledger (budget 8,000 of 10,000 units).
- Playlists are read through the API when signed in (cookies are no longer needed for reading and are the fallback); Liked Music and Watch later stay on cookies. `playlists --json` reports the `source`.
- `youtube --cleanup` / `--apply --confirm ID` / `--undo`: removes repeated entries and the losing uploads of songs `dedupe` merged from your YouTube playlists. A song missing from a playlist is swapped (the kept upload is added before the loser is removed). Journalled and undoable; Liked Music, Liked videos and Watch later are never written. Merged tracks leaving a playlist no longer count as removals in `sync`.
- `youtube --search`, `--replacements`, `--add IDS --to PLAYLIST [--create]`: find replacements for unavailable songs (no quota), add songs to a playlist, and make the private `rerunsync` playlist, which is selected so the next refresh downloads it.
- After the writes the command reads the playlists back from YouTube once and checks every change (the entry is gone, the kept upload is there, counts match; it waits out YouTube's short lag). `applied.items` lists every song that changed and `applied.verify` the result; the skills show both.
- Skills `runsync-youtube`, `runsync-replace`, `runsync-add`, and a YouTube step in the `runsync` orchestrator.
- Tests can no longer reach Google (an autouse fixture blocks the transport).

### Phase 2a: metadata, language, covers
- `runsync resolve`: tiers YouTube fields, title parser, MusicBrainz, iTunes; confidence, trust order and a review queue (`review.csv`).
- `sync --json` and `resolve --json` list every track (title, size, quality; artist, album, year, language, source, file, cover, ReplayGain, review reason) and the orchestrator shows them in its Download and Tagging cards.
- Phase 4: FLAC player copies for Opus and AC-3 masters (`player/flac/`); `runsync card` + the `runsync-sync-card` skill write the selected playlists to a registered microSD card: identified by volume UUID (never the Mac's own disk), only manifest files are ever deleted, foreign files are never overwritten, temp-file + fsync + verify writes, resume after a pulled card, 20% removal guard, free-space check, one M3U8 per playlist, undo of the last sync, and `--apply` needs `--confirm <plan id>`.
- `sync --json` and `resolve --json` (facts on stdout, normal output on stderr); the skills present them: every report is one icon table, the `runsync` orchestrator shows a card after each step, and `runsync-enrich` reports answers with source links.
- `runsync status` + `runsync-status` skill: progress bar, rate and time left; schedulable with `/loop 5m /runsync-status`.
- `runsync dedupe` + `runsync-dedupe` skill (Phase 3): audio-fingerprint duplicate detection, best-source winner, permanent removal with `--apply`, aliases, `--undo`, mass-delete guard, and `--apply` only runs with `--confirm <plan id>` of the exact plan that was previewed.
- Skills are the interface: `runsync` (orchestrator), `runsync-setup`, `runsync-playlists`, `runsync-enrich`. The CLI is four commands (`playlists`, `add-playlist`, `sync`, `resolve`); the interactive playlist picker, `label` and `info` were removed because no skill could use them.
- Corrections (per track and per artist) live in `state.db` and always win; fixes typed into `review.csv` are imported automatically.
- `enrich.csv` + the `runsync-enrich` skill: unresolved tracks and unlabelled artists are searched on the web from a Claude Code session (no API key); only answers with a source link and confidence >= 0.85 are applied.
- Language is decided per track: your override, the upload's own words, then the lead artist's label. Only Hindi, English and Punjabi are accepted.
- Player copies are named `<Title>.m4a` (`Title (Artist)` only when two songs share a title; the same song twice is held for Phase 3) and only built from real AAC audio.
- `resolve --redo` re-resolves everything; renamed tracks lose their stale player copy.
- Per-artist language labels (Hindi/Punjabi/English), set once and inherited.
- Covers (Cover Art Archive, iTunes, YouTube) as 500x500 baseline JPEG; ReplayGain via ffmpeg.
- Tagged AAC copies in `player/m4a/` with title-only plain-letter names (`Title (Artist)` only to tell two songs apart); `master/` stays untouched.
- Cached lookups (`http_cache`), rate-limited; golden test set of 70 real titles.

### Phase 1: foundation
- Playlist discovery from the logged-in account (`runsync playlists`), plus `add-playlist`.
- Fetch, diff and mass-delete guard; removed tracks go to `.trash/` for 14 days.
- Best-quality download (Premium AAC ~258 kbps) with `.part` files, ffprobe check and atomic move.
- Retry/backoff, cookie-expiry and rotation detection, run lock, disk reconciliation.
- Per-track database record and event log; rotating run logs.
- Repo safety guard for cookies, databases, logs and secrets (hook + CI).
