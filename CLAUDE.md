# runsync: notes for Claude Code

Read `PLAN.md` first: it holds the locked decisions and phase order. Work phase by phase and stop at each human checkpoint.

## Commands

```
uv sync
uv run pytest
uv run ruff check .
python3 scripts/check_sensitive.py --all
uv run runsync sync --dry-run
```

## Rules

- Never commit unless asked. Never commit or print `cookies.txt`, `*.db`, logs or keys.
- Safety first: a failed or empty fetch deletes nothing; failure is not removal; deletions go to `.trash/`. The one exception is `dedupe --apply`, which deletes duplicate files for good (the owner's choice) and only with `--confirm <plan id>` of the exact plan the owner confirmed (the command refuses anything else); it keeps an alias so `--undo` can bring a track back. Card writes (`card --apply`) are gated the same way (`--confirm <plan id>`), only touch files in the card's manifest, and never target any volume but the registered card.
- YouTube writes (`youtube --apply`, `--add --apply`) are gated like the card: preview, then `--confirm <plan id>` of the exact plan; adds come before deletes; `LM`, `LL` and `WL` are never written; every change is journalled for `--undo`. `client_secret.json` and `youtube_token.json` are never committed, printed or read. Tests must not reach Google (the conftest fixture blocks it).
- Anything destructive needs `--dry-run` and a test in `tests/test_run_sync.py`.
- Reporter hooks in `sync.py` own all user output; keep `sync.py` free of printing.
- Library lives in `~/Music/RunSync` (`RUNSYNC_HOME` overrides it); tests use a temp dir.
