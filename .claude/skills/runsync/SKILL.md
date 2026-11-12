---
name: runsync
description: Run the whole runsync flow end to end - set up if needed, choose playlists, download new tracks, clean the tags, search the web for whatever is unresolved, remove duplicates, sync the card, and report what happened after every step. Use when the user says "sync my music", "run runsync", "update my library", "get started", or anything that means the whole pipeline rather than one step.
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
- One row per fact that matters; leave out rows that carry no news. A `▰▱` bar goes inside a row. No "Result" column
  (the Download and Tagging cards are the exception: see Detail tables),
  no paragraph before or after; at most one line under the table, only when the user must act.

## Detail tables (Download, Tagging and YouTube)

The owner wants to see **every track**, not only totals. For the Download, Tagging and YouTube cards the single table gets one
row per track after the summary rows; it is still one table with an icon column first and the report named in the
header. Take the rows from the JSON `tracks` lists, in order. Show every track up to 40; beyond that show the first 40
and ask with AskUserQuestion "Show the other N?" (`Show all` / `Skip`) instead of one huge message.

## Step cards

**After every step, before starting the next, show that step's card**: one small table (6 rows at most) saying what
happened in that step, built from that step's facts. Show a card even when nothing happened (a ⬜ row "nothing new").
Name failures with the reason in plain words. Add the "next" line only when the user must do something.

`sync --json` and `resolve --json` print the result as one JSON object on stdout (the normal output goes to stderr, so
use `2>/dev/null`); `playlists --json`, `status --json`, `dedupe --json`, `card --json` and `youtube --json` work the same way.

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

`uv run runsync playlists --json`. `source` is `api` when the user is signed in to YouTube (no cookies needed to read
playlists) and `cookies` otherwise. If `live` is false neither worked (cookies rejected and no YouTube login): go back to the
cookie step of **runsync-setup**, then return here. Ask with AskUserQuestion "Change which playlists sync?" with `Keep as is` and
`Change` (go straight to **runsync-playlists** when none is syncing). On `Change`, run **runsync-playlists**.

Card (one row per fact; the ▲ ▼ rows only when something changed):

| | Playlists |
|:-:|---|
| 📋 | 3 of 5 syncing: Liked Music, Refresh, Running · read through the YouTube API |
| ▲ | Started: Gym Mix |

## 3. Download

`uv run runsync sync --dry-run --json 2>/dev/null`, then say in one card what is waiting (`queue_total`, how many this
run would take, `removed` if any). Ask with AskUserQuestion how many to download now (the cap is 50):

- `Up to 50 (default)` (recommended) · `5 as a trial` · `All N` (long: run it in the background and use the
  **runsync-status** skill for progress) · `Skip downloading now`

Then `uv run runsync sync -n N --json 2>/dev/null`. A failed playlist fetch never deletes anything; the mass-delete guard
(`aborted`) means stop and ask; never pass `--force-delete`.

Card, from the JSON (`tracks`, `added`, `bytes`, `seconds`, `failed`, `unavailable_tracks`, `queued_left`): summary rows,
then one row per track in `tracks` with its size and quality (`codec` and `kbps`), and the failed and unavailable ones:

| | Download · 3 new · 38 MB · 9 s | Size | Quality |
|:-:|---|--:|---|
| ✅ | Rehna Tu · A.R. Rahman, Prasoon Joshi | 13.2 MB | AAC 258 kbps |
| ✅ | Kaise Mujhe | 11.7 MB | AAC 257 kbps |
| ✅ | Shine | 5.6 MB | AAC 129 kbps (that upload only offers this) |
| ⚠️ | Some Song · network timeout (a rerun retries it) | | failed |
| ⚠️ | Another Song · video unavailable | | removed or blocked |
| ⏳ | 330 still queued | | |

Use the track `title` as given. Quality is `codec` + `kbps` (`high` is the Premium ~258 kbps tier, `standard` about 129).
Rows for failed or unavailable tracks only when there are any; ⏳ only when `queued_left` > 0. Nothing new: a single ⬜ row.

## 4. Clean the tags

`uv run runsync resolve --json 2>/dev/null`. AAC masters are copied to `player/m4a/`; Opus and AC-3 masters are converted to FLAC in `player/flac/`.

Card, from the JSON (`tracks`, `resolved`, `by_tier`, `built`, `held`, `review`, `unlabelled`, `enrich_open`): one summary
row, then one row per track in `tracks` showing what was written and what it became:

| | Tagging · 3 tracks | Album · year | Lang | Tagged from | Player copy |
|:-:|---|---|---|---|---|
| ✅ | A.R. Rahman, Prasoon Joshi - Rehna Tu | Delhi-6 · 2009 | hindi | YouTube 95% | Rehna Tu.m4a · 7.1 MB |
| ✅ | Imran Khan - Amplifier | Unforgettable - The Album · 2008 | english | MusicBrainz 95% | Amplifier.flac · 25.8 MB (converted from Opus) |
| 🧐 | Dil Ne Tumko Chun Liya Hai | - | - | held: no artist could be determined | none |

- ✅ a built track (`file` set); 🧐 a held track (`review` set): show its `review` reason in the "Tagged from" cell.
- "Tagged from" = `tier` (YouTube, MusicBrainz, iTunes, title, your fix/search answer for `override`) + `confidence` as a
  percent. `format` `flac` means a conversion: say so. Size from `size` in MB.
- One line under the table naming what every player copy carries: title, artist, album artist, album, year, language,
  the 500x500 cover (its source, e.g. `youtube`, `coverartarchive`), ReplayGain (`gain_db`) and the original YouTube title.
- Language unknown for some artists (`unlabelled`) and the other counts only as short rows above the track rows.

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

## 7. YouTube playlists

Optional and quick. `uv run runsync youtube --json 2>/dev/null`: not signed in (or no client file): one ⬜ row "Not signed
in to YouTube (optional)" and move on, never ask here. Signed in: `uv run runsync youtube --cleanup --json 2>/dev/null`;
`cleanup.ops` > 0: run the **runsync-youtube** skill (preview, confirm, apply); 0: a ⬜ row "Playlists are clean".

If the Download step found unavailable songs with a title on record, offer once with AskUserQuestion "Search replacements
for N lost songs?" (`Yes` / `Later`); yes runs the **runsync-replace** skill.

Card: the detail table of the sub-skill (every song removed, swapped or added, from `applied.items`), then the live check
from `applied.verify` ("Checked on YouTube: N of N changes confirmed", before → after counts per playlist). Never show a
✅ for a change the read-back did not confirm.

| | YouTube · 21 changes | |
|:-:|---|---|
| 🔁 | Refresh · Akhiyaan Gulaab | 4 repeated entries removed, 1 stays |
| 🔄 | Refresh · Suit Full Video Song | replaced by "Suit (Audio)" |
| ✅ | Checked on YouTube | 21 of 21 changes confirmed |
| ✅ | Refresh | 294 → 273 songs |
| 🔎 | rerunsync | 9 replacements added |

## 8. Card

If a card is registered and inserted, run the **runsync-sync-card** skill (it previews, asks, then writes only the
plan the user confirmed). Not registered: offer it once with AskUserQuestion ("Set up your card now?" `Yes` / `Later`).
Not inserted: one ⬜ row "Card not inserted" and move on.

Card:

| | Card |
|:-:|---|
| ➕ | 12 added · 340 MB |
| 🗑️ | 3 removed |
| 📋 | 3 playlist files |
| ✅ | 352 files on the card |

## 9. Final report

One table, one row per step with its headline number, built from the cards:

| | runsync |
|:-:|---|
| ✅ | Set up |
| ✅ | Playlists · 3 syncing |
| ✅ | Downloaded 50 new |
| ✅ | Tagged 50 · 48 player copies |
| ✅ | 3 duplicates removed |
| ✅ | YouTube playlists tidy |
| ✅ | Card · 352 files |
| ⚠️ | 6 need you |

Then at most one line, only if something needs the user (for example: the card was not inserted).

## Rules

- Never run `sync` for real without having shown the dry run first in this session.
- Never delete, trash, or pass `--force-delete` or `--force`; never touch `master/`. Duplicate removal happens only inside
  the **runsync-dedupe** skill, after the user confirms.
- Never print `cookies.txt`. A rejected cookie is a ❌ that sends the user to the export steps, not something to retry.
- YouTube writes happen once per confirmed plan, then the command reads the result back once; do not add reads in between.
- YouTube writes (cleanup, adds) happen only inside the **runsync-youtube**, **runsync-replace** and **runsync-add** skills,
  after the user confirms the exact plan. Never print `client_secret.json` or `youtube_token.json`.
- A card per step, always. Do not explain steps that went fine.
