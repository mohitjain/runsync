---
name: runsync
description: Run the whole runsync flow end to end - set up if needed, choose playlists, download new tracks, clean the tags, search the web for whatever is unresolved, and report. Use when the user says "sync my music", "run runsync", "update my library", "get started", or anything that means the whole pipeline rather than one step.
---

# runsync (orchestrator)

Chain the other skills in order and skip any step that does not apply. Keep the output to icons and short tables
(✅ done, ❌ failed, ⚠️ needs attention, ⏸️ waiting, ⬜ skipped). Every choice goes through the AskUserQuestion tool,
never typed text (a file path is the one exception).

Work from the repo root. Stop at the first ❌ and say what the user must do.

## 1. Is it set up?

Check, without installing anything:

```
command -v brew ffmpeg ffprobe fpcalc deno uv >/dev/null && echo tools-ok
ls cookies.txt
uv run runsync --help >/dev/null && echo project-ok
```

If a tool is missing, the project does not run, or `cookies.txt` is missing: run the **runsync-setup** skill, then
continue. If all is present, move on (a stale cookie shows up in the next step).

## 2. Playlists

Run `uv run runsync playlists` and show the table. If it starts with "⚠️ Could not refresh", the cookies were
rejected: go back to the cookie step of **runsync-setup**, then return here.

If no playlist is ✅, or the user may want to change them, ask with AskUserQuestion: "Change which playlists sync?"
with options `Keep as is` and `Change`. On `Change`, run the **runsync-playlists** skill. With no ✅ playlist at all,
go straight to it.

## 3. Download

`uv run runsync sync --dry-run`, then show the one summary table it prints. If nothing is new and nothing is queued,
mark download ⬜ and go to step 4. Otherwise ask with AskUserQuestion how many to download now (the run caps at 50):

- `Up to 50 (default)` (recommended)
- `5 as a trial`
- `Skip downloading now`

Then run `uv run runsync sync` (add `-n 5` for the trial). If the dry run says removals were disabled or a playlist
failed to fetch, say so in one line and carry on; a failed fetch never deletes anything. If it reports the mass-delete
guard, stop and ask the user; never pass `--force-delete` on your own.

## 4. Clean the tags

`uv run runsync resolve` and show its table. It builds `player/m4a/` copies of AAC tracks; Opus and other formats
wait for Phase 4 and are listed as →.

## 5. What the code could not settle

If `~/Music/RunSync/enrich.csv` exists (resolve says "N row(s) wait for a web search"), ask with AskUserQuestion:
"N rows need a web search. Search now?" with `Yes, search` and `Not now`. On yes, run the **runsync-enrich** skill
and then `uv run runsync resolve` again. Tracks still unresolved after that are for the user: `review.csv` has `fix_*`
columns they can fill, and the next `resolve` applies them.

## 6. Report

End with one table, icon and step, nothing else (no Result column):

| | |
|---|---|
| ✅ | Set up |
| ✅ | Playlists (N syncing) |
| ✅ | Downloaded N new tracks |
| ✅ | Tagged N (player copies in `~/Music/RunSync/player/m4a/`) |
| ⚠️ | N need you (`review.csv`) |

Then at most one line: copying `player/m4a/` to the microSD card is manual for now (automatic card sync is Phase 4).

## Rules

- Never run `sync` for real without having shown the dry run first in this session.
- Never delete, trash, or pass `--force-delete`; never touch `master/`.
- Never print `cookies.txt`. A rejected cookie is a ❌ that sends the user to the export steps, not something to retry.
- Do not explain steps that went fine.
