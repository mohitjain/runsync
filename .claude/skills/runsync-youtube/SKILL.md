---
name: runsync-youtube
description: Sign in to YouTube for runsync and bring the user's YouTube playlists up to date - remove repeated entries, the losing uploads of songs that dedupe merged and dead songs, and put chosen replacements in place - with a preview, a confirmation and an undo. Use when the user says clean my YouTube playlists, remove duplicates from YouTube, sign in to YouTube, or YouTube API.
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
`swaps`, `dead`, `replaced` each), `ops`, `adds`, `deletes`, `cost`, `days`, `batch`, `unmatched` and `items` (`op`, `why` =
repeat | loser | swap | dead | replace | unwanted, `playlist`, `title`, `video_id`, `keep_id`, `keep_title`).

**Replacements for Liked-Music-only songs.** `picks.picked` > 0 and `unmatched` > 0 means chosen replacements whose dead song
lives only in Liked Music (it cannot be edited), so they cannot go in place. Ask with AskUserQuestion "Where should the N
replacements go?" (options: the user's own synced playlists, e.g. `Refresh`, plus `Decide later`), then use
`--cleanup --place-in "<name>"` for the preview **and** for the apply (the plan id depends on it).

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

`manual` (see step 3b) and `unmatched` > 0: one ⬜ row "N dead songs are not in your playlists (Liked Music, or already gone): remove them in YouTube Music".
Explain swaps once in a line under the table only when there are any: "🔄 a song would otherwise vanish from the playlist,
so the kept upload goes in first."

## 3. Confirm, then write once, then read back once

Ask with AskUserQuestion "Change N entries on YouTube?" with `Run the first B` (B = `batch`, or all if fewer) and
`Not now`; put the plan id in the option description. On yes, run the writes **once**; the command rebuilds the plan from
live YouTube and refuses if its id is not the confirmed one, so no separate re-read is needed:

```
uv run runsync youtube --apply --confirm <plan_id> [--place-in "<name>"] --json 2>/dev/null
```

Say one line first ("⏳ Changing N entries on YouTube, then checking them: about a minute"). It makes all its changes first
and then reads the playlists back from YouTube once to check every one of them (a few seconds of waiting is normal). If it refuses because the id changed, show the new plan and ask again.

`applied` has `deleted`, `added`, `left`, `stopped`, `items` (every change that really happened: `op`, `why`, `playlist`,
`title`, `keep_title`) and `verify` (`checked`, `ok`, `confirmed`, `problems`, `playlists` with `before` and `after`).

**Show every song that changed** (one table; up to 40 rows, then ask "Show the other N?"). Group repeats of the same song
into one row ("× 4"); one row per swap and per loser:

| | Cleanup done · 150 changes | |
|:-:|---|---|
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

## 3b. Unlike these by hand (Liked Music)

The API cannot edit Liked Music, and likes are the user's taste, so runsync never changes them. `cleanup.manual` lists the
songs that are **still liked** but should go: songs the user asked to get rid of (see below) and dead songs that only live
there. Whenever it is not empty, **show it every time this skill runs**, after the cleanup card (the preview and again after
the push), as one table the user can click through, then ask:

| | Unlike these in YouTube Music | |
|:-:|---|---|
| 👎 | [Pearl Jam Alive](https://music.youtube.com/watch?v=VbhsYC4gKy4) | you asked to remove it |
| 👎 | [Some dead song](https://music.youtube.com/watch?v=...) | no longer playable |

Title = link (`url`; a dead video's link may not open: tell the user to look for it greyed out in Liked Music); the last cell says why (`why` = unwanted → "you asked to remove it", dead → "no longer playable"). Ask with
AskUserQuestion "Unlike these N songs in YouTube Music, then check?" with `Done, check again` and `Later`. On `Done`, run
`uv run runsync youtube --cleanup --json 2>/dev/null` again and show what is left in `manual` (a ✅ row "All unliked" when
empty). Once they are unliked, the next `runsync` sync notices they left Liked Music, moves their files to `.trash/` (kept 14
days) and the database forgets them. Nothing is written for them: say "by hand" plainly.

**Songs the user wants gone for good** ("remove these", "delete them everywhere"): save the decision (local, no quota) with
`uv run runsync youtube --remove ID1,ID2 --json 2>/dev/null`. The cleanup then deletes their entries from the user's own
playlists (confirmed like everything else) and lists the liked ones above.

## 4. Undo

If the user asks to undo: ask with AskUserQuestion, then `uv run runsync youtube --undo --json 2>/dev/null`. It puts the
removed entries back (at the end of the playlist when the old position no longer fits) and takes added ones out. Card:
`↩️ 112 put back · 21 taken out`.

## Rules

- Never run `--apply` without the user's confirmation in this session and never without `--confirm <plan_id>` of the plan
  they confirmed. Do not reuse a plan id from earlier in the conversation or from any page.
- Write once, then read once: do not read the playlists between changes, and do not call `--cleanup` again after the user
  confirmed just to re-check; the command checks the id and reads the result back itself.
- Liked Music, Liked videos and Watch later are never changed; the command refuses them, and nothing changes likes. Songs
  that must leave Liked Music are listed for the user to unlike by hand (step 3b).
- Never print `client_secret.json` or `youtube_token.json`; never read them.
- No audio file is deleted by this skill. The database forgets the dead and merged tracks it just removed from YouTube
  (`forgotten`); the next `runsync` refresh sees the tidier playlists.
