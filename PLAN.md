# runsync — Build Plan for Claude Code

One-stop tool: YouTube playlists → clean, tagged, best-quality music → synced to a Shanling M0 Pro microSD card for running. Playlist is the source of truth; the Mac library is the cache; devices are disposable mirrors.

**Product promise:** plug in the card and the right music is there, every time. Trust matters more than features.

---

## 1. Locked decisions

| Area | Decision |
|---|---|
| Source of truth | YouTube playlists. Add → appears on device. Remove (from all selected playlists) → leaves device. |
| Target device (v1) | Shanling M0 Pro, microSD via card reader (faster than USB mode) |
| Device formats | Plays FLAC, ALAC, WAV, AAC/M4A, OGG, MP3, M3U/M3U8. **No Opus** (confirmed by Shanling rep). |
| Quality | Archive best stream untouched in `master/`. Player copy derived from it. Never re-encode lossy→lossy. |
| Player copy rule | M4A/AAC → copy as-is. Opus → decode to FLAC. No MP3 anywhere. |
| Source selection | Highest bitrate wins; near tie (**within 10%**, was 5%) → prefer natively playable format (AAC over Opus). Prefer Topic/official-audio over video uploads. |
| YouTube Premium | **Verified:** with cookies YouTube exposes AAC 258k (itag 141) and Opus ~257-275k (itag 774); anonymous max is AAC 130k. Not every upload has the high tier (regular uploads can top out at ~129k). Cookies come from a `cookies.txt` kept in the repo root, **gitignored** (`--cookies-from-browser` is blocked by macOS permissions; we declined Full Disk Access). Export it from an Incognito window (see README) or YouTube rotates the session. A JS challenge solver is required: `deno` + the `yt-dlp-ejs` package (installed via `yt-dlp[default]`). |
| Naming | Plain-letter filenames, title only: `Title.ext` (`Title (Artist).ext` when two different songs share a title; same song twice is held as a duplicate for Phase 3). The uploaded title is kept in a custom tag. Identity = video ID in SQLite DB + custom tag, not filename. |
| Tags | Cleaned title, artist, album, year, album artist, language, square 500x500 baseline JPEG cover, ReplayGain (no loudnorm re-encode). |
| Metadata order | yt-dlp fields → title parsing → MusicBrainz + iTunes (`country=IN`) → LLM (description first, web search only if needed, DB-verified) → AcoustID → `review.csv`. `overrides.yaml` always wins. |
| Languages | English, Hindi, Punjabi only. |
| Language model | **Per artist**, not per song. Artist labelled once, tracks inherit. Occasional misses accepted. Multi-language artists store a list. |
| Dedupe | In v1. Wrong merge is worse than a leftover duplicate. Never merge live/remix/cover/acoustic/instrumental/slowed-reverb into original. Losers kept as aliases, reversible. |
| Retries | 3 retries per track per run, backoff 5s/30s/120s. Transient errors only. Permanent errors (private/deleted/blocked) → `unavailable`, keep existing file. |
| Safety | Wrong-volume protection (UUID), dry-run, abort on empty/failed fetch, mass-delete guard (>20%), trash folder 14 days, no deletes on failures. |
| YouTube API | Official Data API v3 over OAuth (Desktop client, PKCE, token owner-only and gitignored). Reads playlists (cookies are the fallback and still serve audio and Liked Music), cleans repeats and merged duplicates out of the user's playlists, adds songs, and puts replacements for unavailable songs into a private `rerunsync` playlist. Every write is previewed and gated by `--confirm <plan id>`, adds before deletes, journalled for undo, never touches `LM`/`LL`/`WL`; budget 8,000 of 10,000 units a day. |
| Lyrics | **Out of v1** (v2). Player reads sidecar `.lrc` only. |
| Multi-device | One library, many device profiles. v1 ships one profile; schema and engine are multi-device from day one. |
| Stack | Python 3.11+, uv, yt-dlp[default] (library, incl. yt-dlp-ejs), deno, ffmpeg, mutagen, SQLite, typer + rich + questionary (CLI), musicbrainzngs, pyacoustid/chromaprint, Anthropic API (fallback tier only). |

## 2. Parked (provision now, build later)

- **Wife's device**: Hindi + Punjabi only. Selection by playlist first, language filter as second condition.
- **Watch**: specific playlists, capacity-capped. Model unknown; transport depends on it (Apple Watch cannot be script-loaded).
- Lyrics, interactive review, running features (BPM, tempo playlists).

## 3. Architecture

**Library layout**
```
~/Music/RunSync/
  master/          # untouched best stream per video ID
  player/          # derived device-ready copies, cached per format
  .trash/          # removed tracks, purged after 14 days
  state.db         # SQLite: single source of state
  overrides.yaml   # human corrections, always win
  review.csv       # low-confidence tracks
  config.yaml      # playlists, devices, thresholds
  logs/last_run.log
```

**State DB (core tables)**
- `tracks` — video_id, status, source (itag, codec, bitrate), master_path, attempts, last_error, last_attempt_at
- `metadata` — video_id, artist, title, album, album_artist, year, language, confidence, source_tier
- `artists` — canonical name, aliases, languages[], confidence
- `playlists` / `playlist_items` — membership and order
- `aliases` — loser video_id → winner video_id (dedupe)
- `devices` / `device_tracks` — per-device manifest of what is on each device
- `runs` — change log entries

**Device profile (config schema)**
```yaml
devices:
  shanling:
    target: { volume_uuid: "<card UUID>", subpath: "Music" }
    formats: [flac, m4a]
    select: { playlists: all }      # future: languages, artists, tags
    capacity: { max_gb: null }
    transport: volume               # future: mtp, app
```

**Module boundaries**: fetch · download · resolve (metadata) · dedupe · build (player copies, M3U8) · sync (transport adapter) · status/doctor · cli.

---

## Phase 0 — Pre-work (human, ~1 hour)

**Goal:** remove unknowns before code depends on them.

- [ ] Buy a 256 GB microSD (SanDisk/Samsung, trusted seller) and a USB-C card reader
- [ ] Install: `brew install python uv ffmpeg chromaprint deno`
- [ ] Confirm player model in About screen (listing says M0 Pro; photo screen read "M0 Pura")
- [ ] **Card test** — put on the card: one Opus, one M4A, one FLAC; 500x500 embedded covers; a 150-track M3U8. Record what works.
- [ ] **Premium format check** — `yt-dlp -F` on a test track with cookies; note highest AAC/Opus bitrates exposed
- [ ] Create Anthropic API key, store in env var
- [ ] Pick the card filesystem (exFAT for >32 GB); confirm player accepts it, else format in-player

**Output:** `phase0-findings.md` — settles card filesystem, Premium stream availability, M3U8 behaviour (>100 entries).

**Gate:** do not start Phase 1 config until findings are recorded.

---

## Phase 1 — Foundation, fetch, diff, download

> **Status: built and verified on real data (20 tracks downloaded, kill-mid-run tested).** Awaiting the human checkpoint (one full real playlist + log review).
>
> **Changes from this spec made while building (decisions):**
> - **Playlist selection is one command:** `runsync playlists` refreshes from the logged-in account (incl. YouTube Music *Liked Music* `LM`, which is not in the account feed) and shows a checklist with saved selections; `runsync add-playlist <url>` for outside playlists. Selection is stored in the DB.
> - **`sync` flags:** `--dry-run`, `--probe` (with dry-run: ask YouTube each queued track's format/bitrate/size, no download), `--limit N`, `--force-delete`, `--anonymous`, `-v`.
> - **Run lock:** one `sync` at a time (`<library>/.sync.lock`); DB waits up to 30 s for a writer.
> - **Disk reconciliation on every run:** DB says downloaded but file missing → back to pending; untracked file named after a pending video id and passing ffprobe → adopted; other files reported as orphans, never deleted. Stale `master/.tmp` partials are removed at startup.
> - **Per-track record in SQLite:** source facts (title, channel, upload date, duration, description, tags, yt-dlp music fields), chosen stream, every candidate format, ffprobe facts, size, SHA-256, lifecycle timestamps and an append-only `track_events` table. Naming/player columns are reserved for Phases 2 and 4.
> - **Logging:** `<library>/logs/runsync.log` (rotating) and `last_run.log` (latest run), DEBUG level, yt-dlp output included, never cookie values.
> - **Cookie rotation** is detected from yt-dlp's warning and the CLI prints the Incognito re-export steps.
> - **Self-heal** (same error on 3 tracks): updates yt-dlp once, then stops and asks for a re-run (not an in-run retry).
> - **Politeness:** pause between downloads is 5 s (yt-dlp wiki advice for accounts).
> - **Repo safety:** `scripts/check_sensitive.py` blocks cookies/databases/logs/secrets/audio by name *and* content; runs as a pre-commit hook and in CI (`.github/workflows/ci.yml`).

> **Checkpoint run (2026-11-10, 85 tracks):** 107 downloaded, 0 failed, 0 corrupt, DB integrity ok, 7 unavailable (removed/blocked videos, correctly permanent). Log has only those expected errors.
>
> **Known follow-ups, to be fixed in one batch after the full library is downloaded:**
> 1. `select_format` can pick a non-playable codec (one AC-3 384k surround stream was chosen over AAC 258k). Restrict candidates to AAC/ALAC/FLAC/Opus/MP3 and re-fetch affected tracks.
> 2. yt-dlp's ANSI colour codes end up in `tracks.last_error`; strip them when storing.
> 3. 6 masters are Opus/webm (YouTube offered nothing better for the upload); they get no player copy until Phase 4 decodes them to FLAC.
> 4. About a quarter of uploads only offer ~129 kbps AAC (YouTube's tier for that upload, not a bug); consider flagging them in `status`.
> 5. More cases will be appended here as the rest of the library downloads.

**Goal:** playlists in, best-quality masters out, safely and resumably.

**Build**
1. Project skeleton (uv, ruff, pytest), CLI `runsync` with `--dry-run` on everything
2. Config loader, SQLite schema + migrations
3. Playlist fetch via flat-playlist → desired state; handle private/unavailable as `unknown`, not removed
4. Diff engine: new / still-present / removed-from-all-playlists (→ `.trash/`)
5. Downloader: best-audio selection rule, cookies from browser, `.part` temp files, `ffprobe` integrity check before moving into `master/`
6. Retry policy: 3 retries, backoff, transient-only; permanent errors → `unavailable`
7. Politeness: sequential downloads, pauses between tracks, per-run track cap (protects the Premium account)
8. Stale-downloader detection: same error across several tracks → update yt-dlp once, retry once

**Acceptance**
- Small playlist (≤20 tracks) fully downloads; rerun downloads nothing
- Killed mid-run → resumes cleanly, no corrupt files in `master/`
- Empty/failed fetch deletes nothing
- Unit tests for diff, retry classification, and the mass-delete guard

**Human checkpoint:** run on one real playlist; review the run log.

---

## Phase 2 — Metadata, language, covers

> **Status: Phase 2a built (deterministic tiers + web-search enrichment via the `runsync-enrich` skill). No LLM key is used.**
>
> **Decisions made while building 2a:**
> - `master/` is never modified. Resolved metadata lives in the DB (`metadata`, `artists`, `metadata_candidates`, `http_cache`); tags, cover and ReplayGain are embedded only in `player/m4a/` copies. Non-AAC masters get no copy until Phase 4.
> - Trust order: override > youtube > musicbrainz > itunes > parser. A result never replaces a higher-trust one. `locked` rows are never rewritten.
> - A lookup result is accepted only with a title match **plus** corroboration (the parsed artist, a singer named in the title, or a film name in the result's album). YouTube cuts are often shorter than album tracks, so duration is a small signal.
> - Many credits (>3) collapse to the lead artist (Topic channel name when present); `&` inside an artist name is kept ("Above & Beyond").
> - Standard library HTTP instead of `musicbrainzngs`; every lookup cached 90 days; failures are never cached.
> - Review queue is `metadata.needs_review` + `review.csv` instead of a separate table.
> - A title with letters that cannot go in a file name is held for review.

**Goal:** clean tags and covers with minimal human review.

**Build**
1. Title parser: strip junk (`Official Video`, `Lyrics`, `HD`, `4K`, `| Full Song`), pipe-segment classification (song / movie→album / artists), `ft.` normalization
2. Resolver tiers in order; each returns candidate + confidence; stop at first confident hit
3. LLM tier: input = title, channel, description, tags; output strict JSON; **LLM proposes, MusicBrainz/iTunes confirms**; no corroboration → review. Cache by video ID.
4. Cover art: Cover Art Archive → iTunes fallback; square 500x500 baseline JPEG
5. Artist table + per-artist language labelling; tracks inherit; new/ambiguous artists → review
6. Tag writer (mutagen) for FLAC/M4A; custom tags: video_id, original_title, language
7. Plain-letter filenames
8. `overrides.yaml` support (per track and per artist); `review.csv` generation
9. ReplayGain tags via ffmpeg analysis (non-destructive)
10. **Golden test set**: ~50 real junk titles with hand-written correct tags; runs on every change

**Acceptance**
- Golden set passes ≥ agreed threshold; regressions block merge
- ≤5% of tracks land in review on a real playlist
- Never overwrites a higher-trust tier or an override
- Artist table needs <30 manual labels per 1,000 tracks

**Human checkpoint:** you review `review.csv` once; confirm tag quality on 20 random tracks.

---

## Phase 3 — Dedupe

**Goal:** one copy per real song; zero wrong merges.

> **Status: built (`runsync dedupe` + the `runsync-dedupe` skill).** Decisions made while building:
> - The test is the audio, not metadata: lead artist + title + version markers pick the candidates (a live/remix/cover is never a candidate), then chromaprint fingerprints are compared at every offset. On the real library the true duplicates scored 0.75 to 0.94 and different songs about 0.52, so: >= 0.85 merge, anything below that is asked (nothing is silently kept apart: two uploads with the same title and a shared artist would collide on file names). Live and remastered uploads merge into the studio version without a check (owner's rule). The ~3 s duration rule was dropped: music videos run 7 to 37 s longer than the audio upload.
> - Removal is permanent (owner's choice, not `.trash/`): `dedupe` only previews; `--apply` deletes. An alias row keeps the merge visible and `--undo ID` queues the loser for download again; a >20% guard applies.
> - Winner: a copy the player can use today (AAC) over Opus, then source (Topic/art track > artist's own channel > label channel > other > lyric video), then bitrate. (Source first would have deleted the only playable copy of 3 songs.)
> - "Same song?" answers live in `dedupe_decisions` (state.db), not a YAML file.

**Build**
1. Level 1: same video in many playlists → one file, many references
2. Level 2: same song, different upload — match on canonical identity (MusicBrainz recording ID or normalized artist+title) + duration within ~3 s; AcoustID fingerprint verifies doubtful pairs
3. Level 3 guard: detect version markers (live, remix, cover, acoustic, instrumental, "from the movie", slowed/reverb) from title and MusicBrainz — never merge across versions
4. Winner ranking: Topic/official audio > label channel > music video > lyric video/random upload; ties → higher bitrate, then duration closest to album track
5. Pre-download metadata pass (save bandwidth) + post-download fingerprint pass
6. Losers recorded in `aliases` (never re-downloaded, playlists still resolve); merges reversible
7. Ambiguous pairs → "same song?" asked by the skill, answer stored in the database
8. Every merge logged: "merged 3 uploads of X → kept Topic upload"

**Acceptance**
- Test set of known duplicates merges; test set of live/remix/cover pairs never merges
- Undo restores the alias to a standalone track

**Open tuning (default in brackets):** remasters same song, keep higher quality [yes]; studio + live in same playlist [keep both].

---

## Phase 4 — Player build and safe sync

**Goal:** card always matches the playlists, without ever hurting the card.

> **Status: built and tested without a card (`runsync card` + the `runsync-sync-card` skill); the first real card sync is the human checkpoint.** Decisions made while building:
> - Opus and AC-3 masters are decoded to FLAC into `player/flac/` (lossless, so nothing is re-encoded lossy to lossy); file names are unique across both folders.
> - A card is identified by its volume UUID (read-only `diskutil info -plist`), registered once; the Mac's own disk and internal fixed volumes are never offered or accepted.
> - The manifest (`device_tracks`) is the only thing ever deleted from; a file with our name that is not ours is never overwritten (an identical one is adopted, a different one blocks the plan).
> - Writes go through `<name>.runsync-tmp` (fsync, size and checksum verified, then renamed); the next run finishes an interrupted one. Playlists are one M3U8 per selected playlist next to the audio (UTF-8, relative names), to be revisited after the card test.
> - `card --apply` needs `--confirm <plan id>` of the exact plan previewed (same gate as `dedupe --apply`); >20% removals and low free space block the plan; `--undo` reverses the last run (re-tagged updates are not reverted).
> - Tested with temporary folders standing in for the card; no disk image or test volume is ever created.

**Build**
1. Player-copy builder: M4A copy as-is; Opus → FLAC; embed tags + cover; cache per format profile
2. M3U8 per playlist: UTF-8, relative paths, only successfully downloaded tracks
3. Device profile engine (even for one device): selection, formats, capacity, per-device manifest
4. Transport adapter `volume`: identify card by volume UUID; refuse any other target; free-space check
5. Sync: copy new, remove only tracks absent from all of that device's selected playlists; exclude `.DS_Store`/`._*`; verify counts and sizes after copy
6. Resume after card pulled mid-sync; one-command undo from last manifest
7. Mass-delete guard (>20% → stop and ask); `--dry-run` shows adds/removes first

**Acceptance**
- Wrong volume → refuses
- Pull card mid-sync → next run completes cleanly
- Removing a song from one playlist does not remove it if another selected playlist has it
- Player plays everything; covers show; playlists load (per Phase 0 findings)

**Human checkpoint:** first real card sync, then a run on the track.

---

## Phase 4b — YouTube API (built)

Reading through the API, cleanup of repeats and merged duplicates in the user's YouTube playlists, adding songs, replacements for lost songs (`rerunsync`). Skills: `runsync-youtube`, `runsync-replace`, `runsync-add`. Acceptance: a one-operation real trial (one delete and one add, then `--undo`) before the first full cleanup.

## Phase 5 — Trust layer, automation, first import

**Goal:** you stop checking the card by hand.

**Build**
1. `runsync status` — one line: "412 tracks, 3 need review, last synced 6:40am"
2. Per-run change log: +added, −removed, failed (reason), unavailable, review-needed; macOS notification at end
3. `runsync doctor`: yt-dlp version + auto-update, ffmpeg, deno, cookie validity ("log in to YouTube again"), YouTube API login (a testing-mode login can expire after 7 days), API key, disk space, card detected
4. `launchd`: nightly `fetch` (download + tag + build); `sync` triggers only when the card mounts. Downloads never wait for the card.
5. First-import mode: resumable, progress shown, newest playlists first, spread over several nights with per-night cap
6. Git repo for config/overrides/DB schema; periodic backup of `master/`
7. README + runbook (what to do when the downloader breaks)

**Acceptance**
- Full import of your real library completes across nights without manual intervention
- Break the downloader deliberately → tool self-updates or reports a clear fix
- A week of normal use: zero silent deletions, <15 min maintenance

---

## v2 (after v1 is trusted)

- Lyrics: LRCLIB synced `.lrc` sidecars (same name as track, UTF-8), duration guard ±3 s, `no_lyrics` re-check after 30 days.
- Interactive terminal review (`runsync review`, one-key accept of LLM guess), `runsync retag`
- Non-song handling: hour-long mixes/DJ sets/podcasts flagged and skipped (optional chapter split)
- Second device profile (wife: Hindi + Punjabi), then watch adapter
- Opus path revisit if firmware adds support

## v3

BPM tags and tempo-based running playlists; loudness outlier cap; optional podcasts.

---

## Working agreement for Claude Code

1. Read this file first; update it when a decision changes.
2. Tests before features; golden set must stay green.
3. Everything destructive supports `--dry-run`; default to dry-run for new commands until verified.
4. Never delete on a failed, empty or partial fetch. Failure ≠ removal.
5. Never overwrite higher-trust metadata or `overrides.yaml`.
6. LLM calls only in the fallback tier, cached by video ID; report spend per run.
7. Small commits per task; end each phase with a short summary: what works, what is parked, what needs the human.
8. Stop at each human checkpoint and ask before moving to the next phase.
9. If a decision here is ambiguous, ask — don't guess.

## Success metrics

- ≤5% of tracks need manual review; review session <2 min
- Zero silent deletions; every deletion appears in the change log and is undoable
- Song added to a playlist is on the card by the next sync
- Monthly maintenance <15 min (kill criterion: >1 hour/month → simplify)

## Risks

| Risk | Mitigation |
|---|---|
| YouTube breaks the downloader | `doctor` + auto-update + clear error message |
| Premium account rate-limited/flagged | Sequential downloads, pauses, per-run caps, multi-night first import |
| Wrong merge or wrong delete | Reversible aliases, trash folder, mass-delete guard, dry-run |
| Bad tags on Hindi/Punjabi indie tracks | LLM + DB verification, review loop, overrides, artist table |
| Wrong card written | Volume-UUID check, free-space check |
| Fake/failing microSD | Buy from trusted seller; verify counts and sizes after sync |
| Terms of service | Downloading with yt-dlp is outside YouTube's terms even with Premium; personal-use call is the owner's |
