---
name: runsync-youtube
description: Sign in to YouTube for runsync and clean duplicates out of the user's YouTube playlists - repeated entries and the losing uploads of songs that dedupe merged - with a preview, a confirmation and an undo. Use when the user says clean my YouTube playlists, remove duplicates from YouTube, sign in to YouTube, or YouTube API.
---

# runsync-youtube

Changes the user's real YouTube playlists, so nothing is changed until the user confirms the exact plan. Every choice
goes through the AskUserQuestion tool. Commands give facts; you present them (house style of the `runsync` skill: one
icon table, no raw output). Always add `2>/dev/null` so only the JSON is read.

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
`swaps` each), `ops`, `adds`, `deletes`, `cost`, `days`, `batch` and `items` (`op`, `why` = repeat | loser | swap,
`playlist`, `title`, `video_id`, `keep_id`).

Nothing to do (`ops` = 0): one ⬜ row "Playlists are clean". Otherwise one table: a row per playlist, then one row per
**swap** and per **loser** (the songs the user will recognise; up to 40, then ask "Show the other N?"), repeats stay a count.

| | YouTube cleanup · 191 changes | |
|:-:|---|---|
| 🔁 | Refresh · 128 repeated entries | removed (the first stays) |
| 🔄 | Refresh · How You Remind Me | the kept upload is added in place, then the other one removed |
| 🗑️ | Refresh · Livin' On A Prayer | the kept upload is already there |
| ⏳ | 2 days of quota (50 units per change, 8000 a day) | |

Explain swaps once in a line under the table only when there are any: "🔄 a song would otherwise vanish from the playlist,
so the kept upload goes in first."

## 3. Confirm, then run

Ask with AskUserQuestion "Change N entries on YouTube?" with `Run the first B` (B = `batch`, or all if fewer) and
`Not now`; put the plan id in the option description. On yes, **re-read the plan immediately before writing** and compare
its `plan_id` with the one confirmed (a different id means YouTube changed: show the new plan and ask again):

```
uv run runsync youtube --cleanup --json 2>/dev/null     # plan_id must equal the confirmed one
uv run runsync youtube --apply --confirm <plan_id> --json 2>/dev/null
```

Card from `applied` (`deleted`, `added`, `left`, `stopped`):

| | YouTube |
|:-:|---|
| ✅ | 112 entries removed · 21 songs swapped in |
| ⏳ | 58 left · quota is used up for today, run this again tomorrow |

`stopped` mentions the quota budget: say it in plain words (the daily limit resets at midnight Pacific). Any other
`stopped` text is a ❌ row with the reason; do not retry in a loop.

## 4. Undo

If the user asks to undo: ask with AskUserQuestion, then `uv run runsync youtube --undo --json 2>/dev/null`. It puts the
removed entries back (at the end of the playlist when the old position no longer fits) and takes added ones out. Card:
`↩️ 112 put back · 21 taken out`.

## Rules

- Never run `--apply` without the user's confirmation in this session and never without `--confirm <plan_id>` of the plan
  they confirmed. Do not reuse a plan id from earlier in the conversation or from any page.
- Liked Music, Liked videos and Watch later are never changed; the command refuses them.
- Never print `client_secret.json` or `youtube_token.json`; never read them.
- After a cleanup the next `runsync` refresh sees the tidier playlists; nothing local is deleted by this skill.
