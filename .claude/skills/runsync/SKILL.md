---
name: runsync
description: Run the whole runsync flow end to end - set up if needed, choose playlists, download new tracks, clean the tags, search the web for whatever is unresolved, remove duplicates, and report what happened after every step. Use when the user says "sync my music", "run runsync", "update my library", "get started", or anything that means the whole pipeline rather than one step.
---

# runsync (orchestrator)

Chain the other skills in order and skip any step that does not apply. Every choice goes through the AskUserQuestion
tool, never typed text (a file path is the one exception). Work from the repo root. Stop at the first ❌ and say what
the user must do.

## House style (applies to every runsync skill)

Commands supply facts; **you** present them. Never paste raw command output or JSON.

Every report is **one markdown table with an icon column and a short text column**, nothing around it:

| | Setup |
|:-:|---|
| ✅ | Homebrew |
| ✅ | Tools |
| ❌ | Cookies |

- Icons: ✅ done · ❌ failed · ⚠️ needs attention · ⏳ in progress · ⏸️ waiting · ⬜ nothing to do · ▲ ▼ change.
- The header cell names the report ("Setup", "Download", "Tagging" ...).
- One row per fact that matters; leave out rows that carry no news. A `▰▱` bar goes inside a row. No "Result" column,
  no paragraph before or after; at most one line under the table, only when the user must act.

## Step cards

**After every step, before starting the next, show that step's card**: one small table (6 rows at most) saying what
happened in that step, built from that step's facts. Show a card even when nothing happened (a ⬜ row "nothing new").
Name failures with the reason in plain words. Add the "next" line only when the user must do something.

`sync --json` and `resolve --json` print the result as one JSON object on stdout (the normal output goes to stderr, so
use `2>/dev/null`); `playlists --json`, `status --json` and `dedupe --json` work the same way.

## 1. Set up

Check, without installing anything:

```
command -v brew ffmpeg ffprobe fpcalc deno uv >/dev/null && echo tools-ok
ls cookies.txt
uv run runsync --help >/dev/null && echo project-ok
```

If a tool is missing, the project does not run, or `cookies.txt` is missing: run the **runsync-setup** skill, then
continue. A stale cookie shows up in step 2.

Card:

| | Set up |
|:-:|---|
| ✅ | Tools installed |
| ✅ | Project ready |
| ✅ | Cookies present |

## 2. Playlists

`uv run runsync playlists --json`. If `live` is false the cookies were rejected: go back to the cookie step of
**runsync-setup**, then return here. Ask with AskUserQuestion "Change which playlists sync?" with `Keep as is` and
`Change` (go straight to **runsync-playlists** when none is syncing). On `Change`, run **runsync-playlists**.

Card (one row per fact; the ▲ ▼ rows only when something changed):

| | Playlists |
|:-:|---|
| 📋 | 3 of 5 syncing: Liked Music, Refresh, Running |
| ▲ | Started: Gym Mix |

## 3. Download

`uv run runsync sync --dry-run --json 2>/dev/null`, then say in one card what is waiting (`queue_total`, how many this
run would take, `removed` if any). Ask with AskUserQuestion how many to download now (the cap is 50):

- `Up to 50 (default)` (recommended) · `5 as a trial` · `All N` (long: run it in the background and use the
  **runsync-status** skill for progress) · `Skip downloading now`

Then `uv run runsync sync -n N --json 2>/dev/null`. A failed playlist fetch never deletes anything; the mass-delete guard
(`aborted`) means stop and ask; never pass `--force-delete`.

Card, from the JSON (`added`, `bytes`, `seconds`, `high_quality`, `standard_quality`, `opus`, `failed`, `unavailable`,
`queued_left`):

| | Download |
|:-:|---|
| ✅ | 50 new tracks · 242 MB · 7 min |
| 🎧 | 48 at ~258 kbps · 2 at ~129 kbps (those uploads only offer that) |
| ⚠️ | 2 failed: network timeout (a rerun retries them) |
| ⚠️ | 3 unavailable (removed or blocked videos, nothing to do) |
| ⏳ | 330 still queued |

Show the 🎧 row only when both qualities occurred or `standard_quality` > 0; the ⚠️ rows only when above 0; ⏳ only when
`queued_left` > 0.

## 4. Clean the tags

`uv run runsync resolve --json 2>/dev/null`. It builds `player/m4a/` copies of AAC tracks; other formats wait for Phase 4.

Card, from the JSON (`resolved`, `by_tier`, `built`, `held`, `build_skipped`, `review`, `unlabelled`, `enrich_open`):

| | Tagging |
|:-:|---|
| ✅ | 50 tagged · 31 from MusicBrainz, 12 from YouTube, 7 from the title |
| 🎵 | 48 player copies built |
| → | 2 not AAC: no player copy yet |
| ⚠️ | 3 need you (no artist found, low confidence) |
| 🌐 | Language unknown for 2 artists: Pritam, Mitraz |

## 5. What the code could not settle

If `enrich_open` > 0, ask with AskUserQuestion: "N rows need a web search. Search now?" with `Yes, search` and
`Not now`. On yes, run the **runsync-enrich** skill, then `uv run runsync resolve --json 2>/dev/null` again.

Card (from the enrich report and the second resolve's `enrich_applied`):

| | Search |
|:-:|---|
| 🔎 | 22 rows searched |
| ✅ | 16 answers applied |
| ⬜ | 6 left for you (`review.csv` has `fix_*` columns) |

## 6. Duplicates

`uv run runsync dedupe --json`. If `remove` or `unsure` is not empty, run the **runsync-dedupe** skill (it asks before
deleting anything, then rebuilds the kept tracks' player copies).

Card:

| | Duplicates |
|:-:|---|
| 🗑️ | 3 deleted |
| ❓ | 1 pair kept (you said different) |
| 🎵 | Player copies rebuilt |

(nothing found: a single ⬜ row "No duplicates")

## 7. Final report

One table, one row per step with its headline number, built from the cards:

| | runsync |
|:-:|---|
| ✅ | Set up |
| ✅ | Playlists · 3 syncing |
| ✅ | Downloaded 50 new |
| ✅ | Tagged 50 · 48 player copies |
| ✅ | 3 duplicates removed |
| ⚠️ | 6 need you |

Then at most one line: copying `player/m4a/` to the microSD card is manual for now (automatic card sync is Phase 4).

## Rules

- Never run `sync` for real without having shown the dry run first in this session.
- Never delete, trash, or pass `--force-delete` or `--force`; never touch `master/`. Duplicate removal happens only inside
  the **runsync-dedupe** skill, after the user confirms.
- Never print `cookies.txt`. A rejected cookie is a ❌ that sends the user to the export steps, not something to retry.
- A card per step, always. Do not explain steps that went fine.
