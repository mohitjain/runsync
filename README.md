# runsync

Mirror your YouTube playlists to a microSD-based MP3 player (Shanling M0 Pro) with clean tags, cover art and no phone.
**The playlist is the source of truth**, the Mac library is the cache, the card is a disposable mirror.
Design and decisions: [DESIGN.md](DESIGN.md). Handling secrets: [SECURITY.md](SECURITY.md).

## Setup

macOS, then:

```
brew install python uv ffmpeg chromaprint deno
git clone https://github.com/mohitjain/runsync && cd runsync && uv sync
```

Open Claude Code in the folder and say **"set up runsync"**: it checks the tools and walks you through the cookie file
(`cookies.txt`, gitignored: it works like your login, never share it). Cookies give Premium quality (258 kbps) and
download the audio.

**YouTube login (optional, recommended):** playlists are then read through the official API, so stale cookies stop
mattering for reading, and runsync can tidy your playlists. Create a Google Cloud project with the YouTube Data API v3,
a consent screen (add yourself as a test user) and a **Desktop** OAuth client; save its JSON as `client_secret.json`
(gitignored), then run `uv run runsync youtube --login`.

## Use

Open Claude Code in this repo and say what you want:

| Say | Skill | Does |
|---|---|---|
| "sync my music" | `runsync` | the whole flow, a card after every step |
| "choose playlists" | `runsync-playlists` | pick which playlists to sync |
| "enrich my library" | `runsync-enrich` | web-searches what the code could not settle |
| "status" | `runsync-status` | progress; `/loop 5m /runsync-status` during a long download |
| "remove duplicates" | `runsync-dedupe` | the same song uploaded twice: keeps the best, asks about doubtful pairs |
| "sync to the card" | `runsync-sync-card` | registers the card once, previews, writes only the plan you confirm |
| "clean my YouTube playlists" | `runsync-youtube` | removes repeats and merged duplicates from your playlists, shows every song and checks the result |
| "replace the unavailable songs" | `runsync-replace` | finds new uploads of lost songs; your picks go to a private `rerunsync` playlist |
| "add this song to Running" | `runsync-add` | search, pick, confirm, add |

The skills drive `uv run runsync <command>` (`playlists`, `sync`, `resolve`, `status`, `dedupe`, `card`, `youtube`; most
take `--json`). Run `uv run runsync --help` for the rest.

## Safety

- A failed, empty or partial fetch never deletes anything; removed tracks go to `.trash/` for 14 days.
- Anything destructive or outward (dedupe delete, card sync, YouTube changes) shows a preview first and needs `--confirm <plan id>` of that exact plan; changes are journalled and undoable where possible.
- The card is identified by volume UUID; only files runsync put there are ever removed.
- Secrets (`cookies.txt`, `client_secret.json`, `youtube_token.json`, `*.db`, logs) are gitignored and blocked by a pre-commit and CI guard.

## Development

```
uv run pytest && uv run ruff check . && python3 scripts/check_sensitive.py --all
```

Library lives in `~/Music/RunSync` (`RUNSYNC_HOME` overrides it); tests use a temp dir and cannot reach Google.

## License

See [LICENSE](LICENSE). Downloading with yt-dlp is outside YouTube's terms even with Premium; using it is your decision.
