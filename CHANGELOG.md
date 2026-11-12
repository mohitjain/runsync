# Changelog

## Unreleased

### Phase 2a: metadata, language, covers
- `runsync resolve`: tiers YouTube fields, title parser, MusicBrainz, iTunes; confidence, trust order and a review queue (`review.csv`).
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
