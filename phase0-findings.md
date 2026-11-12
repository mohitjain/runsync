# Phase 0 findings

Recorded 2026-11-09. Settles what Phase 1 needed; items marked **PENDING** still need the human.

## Tooling (done)
python 3.11, uv 0.13.7, ffmpeg/ffprobe, chromaprint (`fpcalc`), deno 2.9.8, yt-dlp 2026.10.26 (via `yt-dlp[default]`).
`ANTHROPIC_API_KEY`: deferred to Phase 2 (LLM tier).

## Premium stream availability (done)
| | Anonymous | With Premium cookies |
|---|---|---|
| AAC (m4a) | itag 140, 130 kbps | itag 141, **258 kbps** |
| Opus (webm) | itag 251, 129 kbps | itag 774, **257-275 kbps** |

- Not every upload gets the high tier: ordinary uploads can top out at ~129 kbps even with Premium (seen on 3 of ~20 tracks).
- Near-tie rule widened to **10%** so playable AAC 258k wins over Opus 275k; Opus is only chosen when clearly better.
- Estimated vs actual size matched within 1% on the track compared (5.1 MB).

## Cookies and downloader (learned)
- `--cookies-from-browser chrome` fails on macOS (profile folder blocked for the terminal); Full Disk Access declined. Use a `cookies.txt` export, gitignored, in the repo root.
- YouTube rotates cookies of any open tab. Export from an Incognito window after visiting `youtube.com/robots.txt`, then close it. A normal export went stale within minutes (401 / "cookies are no longer valid").
- yt-dlp needs a JS challenge solver (deno + `yt-dlp-ejs`); without it only thumbnails are listed and every track fails.
- yt-dlp rewrites `cookies.txt` as it runs.
- Reading playlists no longer depends on cookies: the YouTube Data API (OAuth login) returned the same items as yt-dlp (Running 94 = 94; Refresh 225 distinct, plus one unreadable entry yt-dlp did not list). Cookies remain needed for the Premium audio download and for Liked Music (`LM`), which the API cannot read. The API also exposes repeats (Refresh lists 354 entries for 225 distinct songs).
- Removed videos (copyright etc.) surface as "Video unavailable" and are treated as permanent.
- YouTube Music's music-only likes (`LM`, 373 tracks for this account) are not in the account playlist feed; fetched explicitly. "Liked videos" (`LL`, 1390) is everything liked on YouTube, not only music, and is left unselected.

## PENDING (needs the card and player)
- [ ] Confirm player model in About screen (M0 Pro vs "M0 Pura").
- [ ] Card test: one Opus, one M4A, one FLAC; 500x500 covers; 150-track M3U8. Record what works.
- [ ] Card filesystem (exFAT for >32 GB); confirm the player accepts it.
- [ ] M3U8 behaviour over 100 entries.
- [ ] Buy 256 GB microSD + USB-C reader.

Phase 4's card engine is built and tested with a temporary folder as the card; these still gate its **first real sync** (the `diskutil` parsing and the M3U8/cover behaviour can only be confirmed on the real card). Defaults to revisit after the card test: playlist files sit next to the audio in the `Music` folder with relative names.
