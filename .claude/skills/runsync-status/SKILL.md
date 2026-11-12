---
name: runsync-status
description: Lightweight runsync progress check - how many tracks are downloaded and queued, whether a run is in progress, speed and time left, and what changed since the last check. Built to be scheduled (for example /loop 5m /runsync-status) during a long sync. Use when the user asks for status or progress.
---

# runsync-status

You are the presentation layer. The command only supplies facts; you decide what is worth saying and how it looks.
Never paste command output or JSON.

## Get the facts (one cheap local command, no network)

```
uv run runsync status --json
```

Fields: `state` (running | stopped | done), `activity` (downloading | tagging | deduping | working | idle),
`downloaded`, `queued`, `total`, `unavailable`, `untagged`, `tagged`, `need_you`, `player_copies`, `copies_target`,
`per_min`, `eta_min`, `tag_per_min`, `tag_eta_min`, `minutes_since_last_download`.

## Compose the message: one icon table (the house style of the `runsync` skill)

| | Download |
|:-:|---|
| ⏳ | Downloading · `▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱` 40% |
| 📦 | **189** of 477 tracks · 288 left |
| ⏱️ | ≈ 5 per min · about **55 min** left |
| ⚠️ | 14 unavailable (removed or blocked videos) |
| 🏷️ | 85 ready to tag |
| ▲ | +2 since the last check |

- First row: ⏳ Downloading (running), ✅ All downloaded (done) or ⏸️ Stopped (not running, queue left), with the bar
  and percent. Bar: 24 characters, `round(downloaded / total * 24)` of `▰`, the rest `▱`.
- 📦 always. ⏱️ only while running and `per_min` > 0 ("about 57 min", "about 1 h 20 min").
- ⚠️ only when `unavailable` is above 0 and rose, or when stalled: running and `minutes_since_last_download` >= 3
  ("No new download for N min: YouTube may be throttling or the cookies were rejected").
- 🏷️ only when `untagged` > 0.
- ▲ only from the second check, only when something changed.
- No text outside the table.

## When `activity` is `tagging`

Tagging has two stages, show the one that is running (header "Tagging"):

- `untagged` > 0: reading metadata. Bar = `tagged / downloaded`; "**140** of 460 tracks tagged · 320 left";
  ⏱️ from `tag_per_min` / `tag_eta_min`.
- `untagged` = 0 and `player_copies` < `copies_target`: building copies. Bar = `player_copies / copies_target`;
  "**192** of up to 417 player copies built" ("up to" because duplicates and held tracks get none).
- Rows after that: 🧐 `need_you` tracks need the user (only when > 0); no download rows.

| | Tagging |
|:-:|---|
| ⏳ | Building player copies · `▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱` 46% |
| ✅ | 460 of 460 tracks tagged |
| 🎵 | **192** of up to 417 player copies built |
| 🧐 | 42 need you so far |

When `activity` is `deduping` or `working`, one row: "⏳ Removing duplicates" / "⏳ A runsync job is running".

## Finished

`state` is not `running` (all stages finished):

- `done`: "✅ All N tracks downloaded." Next step in one line: tagging and duplicates (the `runsync` skill). Stop the
  schedule.
- `stopped` with a queue: "⏸️ Stopped with N waiting." Say why if you can tell (cap reached, cookies, an error in
  `~/Music/RunSync/logs/last_run.log`) and offer `uv run runsync sync`. Stop the schedule.

Stopping the schedule: if this runs under `/loop`, find the job running `/runsync-status` (CronList) and delete it
(CronDelete), or end a dynamic loop with its stop option. Tell the user it stopped.

## Rules

- Read-only: never start, stop or change a sync; never touch `state.db` or `master/`.
- Never print `cookies.txt`.
