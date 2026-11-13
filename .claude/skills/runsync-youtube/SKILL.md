---
name: runsync-youtube
description: Sign in to YouTube for runsync and bring the user's YouTube playlists up to date - mirror Liked Music into the library list, remove repeated entries, the losing uploads of songs that dedupe merged and dead songs, and put chosen replacements in place - with a preview, a confirmation and an undo. Use when the user says clean my YouTube playlists, remove duplicates from YouTube, sign in to YouTube, or YouTube API.
---

# runsync-youtube

Changes the user's real YouTube playlists, so nothing is changed until the user confirms the exact plan. Every choice
goes through the AskUserQuestion tool. Commands give facts; you present them (house style of the `runsync` skill: one
icon table, no raw output). Always add `2>/dev/null` so only the JSON is read.

The rule: YouTube is the truth, `state.db` is a snapshot of the last sync plus changes still to push. After a checked push
the database deletes what is dead; it keeps no history.

## 1. Signed in?

`uv run runsync youtube --json 2>/dev/null`: fields `logged_in`, `client_file`, `units_today`, `budget`, `playlists`.

- `client_file` false: one ❌ row "No Google client file" and one line: save the Desktop OAuth client JSON in the repo
  folder as `client_secret.json` (it is git-ignored and never printed). Stop.
- `logged_in` false: one ⏸️ row "Not signed in to YouTube" and one line: "Run `! uv run runsync youtube --login`, approve in the
  browser, then tell me." (The login opens a browser, so the user runs it themselves.) Stop.

| | YouTube |
|:-:|---|
| ✅ | Signed in · quota today 51 of 8000 |
| 📋 | Running · 94 · Refresh · 354 |

## 2. Look (reads only)

`uv run runsync youtube --cleanup --json 2>/dev/null`. `cleanup` has `plan_id`, `playlists` (`repeats`, `losers`,
`swaps`, `dead`, `replaced` each), `ops`, `adds`, `deletes`, `cost`, `days`, `batch`, `mirror` (`library`, `paused`, `liked`), `mirror_adds`, `mirror_removes` and `items` (`op`, `why` =
repeat | loser | swap | dead | replace | unwanted, `playlist`, `title`, `video_id`, `keep_id`, `keep_title`).

Nothing to do (`ops` = 0): one ⬜ row "Playlists are clean". Otherwise one table: a row per playlist, then one row per
**swap**, **loser**, **dead** song and **replacement** (the songs the user will recognise; up to 40, then ask "Show the other N?"),
repeats stay a count. A dead song with no replacement is **deleted**: the row says so, because it is the one permanent-looking
change (it can still be undone until the next sync).

| | YouTube cleanup · 191 changes | |
|:-:|---|---|
| 🔁 | Refresh · 128 repeated entries | removed (the first stays) |
| ⚰️ | Running · Paisa | no longer on YouTube, no replacement: removed |
| 🔎 | Running · Sweet Emotion | dead entry replaced by "Aerosmith - Sweet Emotion" |
| 🔄 | Refresh · How You Remind Me | the kept upload is added in place, then the other one removed |
| 🗑️ | Refresh · Livin' On A Prayer | the kept upload is already there |
| ⏳ | 2 days of quota (50 units per change, about 8000 a day) | |

Add the mirror rows of step 2b (`➕ N liked songs → <library>`, `➖ N unliked removed`). Explain swaps once in a line under the table only when there are any: "🔄 a song would otherwise vanish from the playlist,
so the kept upload goes in first."

## 2b. The library list (Liked Music mirrored into a playlist)

Liked Music cannot be edited through the API, a playlist can. So one playlist is the **library list**: Liked Music feeds it
(a new like is added, an unlike is removed) and runsync syncs from it. Dead songs, merged duplicates and songs the user
removed never enter it, so nothing is ever unliked by hand. `cleanup.mirror.library` is its name (`null` = not chosen yet).

**First run (`library` is null): ask, in this order, with AskUserQuestion.**
1. "Which playlist is your library? Everything you like goes there." Options: the user's own playlists (from
   `uv run runsync youtube --json 2>/dev/null` `playlists`, up to 3, the biggest first), plus `Create a new list` (ask its name,
   default `Library`). Existing: `uv run runsync youtube --library "<name>" --json 2>/dev/null`. New: preview with
   `--library "<name>" --create`, ask to confirm the plan id, then `--library "<name>" --create --apply --confirm <id>`.
2. If the preview then shows `mirror_adds` > 0: "N of your likes are not in the library yet. Add them?" with `Add them all
   (in daily batches)` and `Only new likes from now on`. The second runs `uv run runsync youtube --baseline --json 2>/dev/null`
   (local only; today's backlog is never added). Say how many days the first option takes (`days`).

**Every run.** Show the mirror in the cleanup card: `➕ N liked songs → <library>` (`mirror_adds`) and `➖ N unliked removed`
(`mirror_removes`). `mirror.paused` non-empty: a ⏸️ row "Liked Music mirror paused: <reason>" and, when it is about cookies,
send the user to the cookie steps of **runsync-setup**; nothing is written, nothing is lost. The mirror runs before the
download so new likes download in the same run.

**Stop syncing Liked Music once the library holds everything** (`mirror_adds` = 0 and not paused): offer once with
AskUserQuestion "Stop syncing Liked Music? Your library list now holds your likes." (`Stop` / `Keep`); `Stop` runs
`uv run runsync playlists --stop "Liked Music"`. Never offer it while `mirror_adds` > 0: songs only in Liked Music would leave
the library.

**Songs the user wants gone for good** ("remove these", "delete them everywhere"): `uv run runsync youtube --remove ID1,ID2
--json 2>/dev/null` (local, no quota). The cleanup then deletes their entries from the user's playlists and they are never
mirrored back; they leave the library on the next sync (files go to `.trash/` for 14 days).

## 3. Confirm, then write once, then read back once

Ask with AskUserQuestion "Change N entries on YouTube?" with `Run the first B` (B = `batch`, or all if fewer) and
`Not now`; put the plan id in the option description. On yes, run the writes **once**; the command rebuilds the plan from
live YouTube and refuses if its id is not the confirmed one, so no separate re-read is needed:

```
uv run runsync youtube --apply --confirm <plan_id> --json 2>/dev/null
```

Say one line first ("⏳ Changing N entries on YouTube, then checking them: about a minute"). It makes all its changes first
and then reads the playlists back from YouTube once to check every one of them (a few seconds of waiting is normal). If it refuses because the id changed, show the new plan and ask again.

`applied` has `deleted`, `added`, `left`, `stopped`, `items` (every change that really happened: `op`, `why`, `playlist`,
`title`, `keep_title`) and `verify` (`checked`, `ok`, `confirmed`, `problems`, `playlists` with `before` and `after`).

**Show every song that changed** (one table; up to 40 rows, then ask "Show the other N?"). Group repeats of the same song
into one row ("× 4"); one row per swap and per loser:

| | Cleanup done · 150 changes | |
|:-:|---|---|
| ➕ | Refresh · 161 liked songs | added from Liked Music (100 today, the rest tomorrow) |
| 🔁 | Refresh · Akhiyaan Gulaab | 4 repeated entries removed, 1 stays |
| ⚰️ | Running · Paisa | dead, no replacement: deleted |
| 🔎 | Running · Sweet Emotion | replaced in place by "Aerosmith - Sweet Emotion" |
| 🔄 | Refresh · Suit Full Video Song | replaced by "Suit (Audio)" |
| 🗑️ | Refresh · Lahore (Official Video) | duplicate removed, "Lahore" stays |
| ✅ | Checked on YouTube | 150 of 150 changes confirmed |
| ✅ | Refresh | 354 → 294 songs |
| ⏳ | 21 left | quota is used up for today, run this again after the reset |

- The ✅ "Checked" row comes from `verify`: `confirmed` of the number of changes. `checked` false: a ⚠️ row "Could not check
  (<reason in plain words>)", never a ✅. `ok` false: a ❌ row per `problems` entry with the song and what is wrong, and
  say the playlist may need a look.
- Playlist rows from `verify.playlists` (`title`, `before` → `after`).
- `forgotten` > 0: a ✅ row "N songs forgotten by the database" (dead and merged tracks that left YouTube are deleted from `state.db`; nothing is kept as history). `verify.ok` false: nothing is forgotten.
- `stopped` mentioning the quota: say it in plain words (the daily limit resets at midnight Pacific; the message names
  the hours left). Any other `stopped` text is a ❌ row with the reason; do not retry in a loop.

## 4. Undo

If the user asks to undo: ask with AskUserQuestion, then `uv run runsync youtube --undo --json 2>/dev/null`. It puts the
removed entries back (at the end of the playlist when the old position no longer fits) and takes added ones out. Card:
`↩️ 112 put back · 21 taken out`.

## Rules

- Never run `--apply` without the user's confirmation in this session and never without `--confirm <plan_id>` of the plan
  they confirmed. Do not reuse a plan id from earlier in the conversation or from any page.
- Write once, then read once: do not read the playlists between changes, and do not call `--cleanup` again after the user
  confirmed just to re-check; the command checks the id and reads the result back itself.
- Liked Music, Liked videos and Watch later are never changed; the command refuses them, and nothing ever changes a like.
  Liked Music is only read (through the cookies) and mirrored into the library list.
- Never print `client_secret.json` or `youtube_token.json`; never read them.
- No audio file is deleted by this skill. The database forgets the dead and merged tracks it just removed from YouTube
  (`forgotten`); the next `runsync` refresh sees the tidier playlists.
