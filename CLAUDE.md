# runsync: notes for Claude Code

Read `DESIGN.md` first: it holds the locked decisions and the open items. Stop at each human checkpoint.

## Commands

```
uv sync
uv run pytest
uv run ruff check .
python3 scripts/check_sensitive.py --all
uv run runsync sync --dry-run
```

## Rules

- **The database is a snapshot, not a history.** YouTube is the truth. `state.db` holds what YouTube had at the last sync plus the changes still waiting to be pushed (quota can run out). Flow: sync, work out the fixes (dedupe, web search, replacements, asking the owner), write them back to the YouTube playlists, verify, then delete what is dead from the database too. Nothing is kept "for history": no event logs, run logs, merged, trashed or unavailable rows, aliases or picks once YouTube is updated. A song that is unavailable and has no replacement is deleted (database, and the playlist entry after the owner confirms). Replacements go in place (added where the dead song was, the dead entry removed). Undo of a YouTube run is kept only until the next successful sync. Before adding a table or a column, say what deletes it once YouTube is in sync.
- Never commit unless asked. Never commit or print `cookies.txt`, `*.db`, logs or keys.
- Safety first: a failed or empty fetch deletes nothing; failure is not removal; deletions go to `.trash/`. The one exception is `dedupe --apply`, which deletes duplicate files for good (the owner's choice) and only with `--confirm <plan id>` of the exact plan the owner confirmed (the command refuses anything else); it keeps an alias so `--undo` can bring a track back. Card writes (`card --apply`) are gated the same way (`--confirm <plan id>`), only touch files in the card's manifest, and never target any volume but the registered card.
- YouTube writes (`youtube --apply`, `--add --apply`) are gated like the card: preview, then `--confirm <plan id>` of the exact plan; adds come before deletes; `LM`, `LL` and `WL` are never written, and nothing ever changes a like (songs that must leave Liked Music are listed with links for the owner to unlike by hand); every change is journalled for `--undo`. `client_secret.json` and `youtube_token.json` are never committed, printed or read. Tests must not reach Google (the conftest fixture blocks it).
- Anything destructive needs `--dry-run` and a test in `tests/test_run_sync.py`.
- Reporter hooks in `sync.py` own all user output; keep `sync.py` free of printing.
- Library lives in `~/Music/RunSync` (`RUNSYNC_HOME` overrides it); tests use a temp dir.
