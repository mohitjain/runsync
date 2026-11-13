---
name: runsync-quality
description: Find better-sounding uploads for songs stuck below YouTube's top bitrate (about 128 kbps instead of 256) - show how many songs are top quality, search a few songs gently, and queue the better uploads for the next YouTube plan. Use when the user asks about audio quality, bitrate, or better versions of songs, or as a step of the full runsync flow.
---

# runsync-quality

About 1 song in 7 was uploaded without YouTube's 256 kbps tier (even official channels do that). This skill looks for another
upload of the same recording that has it. House style of the `runsync` skill: one icon table, no raw output. Use
`2>/dev/null` on every command. Every choice goes through the AskUserQuestion tool.

Rules of the feature: a song that is already top quality (256 kbps) is **never** searched; a song searched without luck is
searched again only after a month; the search needs the user's cookies, so it is gentle (a few songs per run, pauses, a
session check before and after) and it only **finds**: nothing is added or removed here.

## 1. Look (local, no network)

`uv run runsync quality --json 2>/dev/null`: `songs`, `top`, `below_top`, `due` (never searched, or last searched a month
ago), `waiting` (searched recently), `held` (no artist yet, skipped), `next_due` (date, when nothing is due),
`queued_upgrades` (found, waiting for the YouTube plan), `results` (why past searches ended, songs per code:
`found`, `matches_low` = matching uploads exist but all only ~128 kbps, so we very likely have the best that exists,
`no_match` = no upload passed the filters (same length and title), a better one might exist with a different cut,
`no_results` = the search returned nothing, `unknown` = searched before reasons were recorded).

| | Quality |
|:-:|---|
| 🎧 | 312 of 364 songs at the top tier (256 kbps) |
| ⬇️ | 52 below: 5 due a search, 47 searched recently |
| 📚 | 14 better uploads queued: the next YouTube plan adds them |
| ⬜ | 38 searched without luck: 17 matches are all 128k (likely the best that exists), 21 had no matching upload |

`due` = 0: say "⬜ nothing due" (and `next_due` when set) and stop. Not signed in with usable cookies: a ⏸️ row and send the user to
the cookie steps of **runsync-setup**.

## 2. Search (asks first: it uses the user's cookies)

Ask with AskUserQuestion "Look for better uploads of up to N songs now?" (N = `min(due, 5)`) with `Yes (about 2 minutes)` and
`Skip`. On yes say one line first ("⏳ Searching N songs, paced to protect your YouTube session: about 2 minutes"), then
`uv run runsync quality --search --json 2>/dev/null`. `search` has `searched`, `not_found`, `reasons` (this run, same codes), `found` (`song`, `old_channel`,
`old_kbps`, `new_channel`, `new_kbps`, `title`), `aborted`, `canary_before`, `canary_after`.

| | Quality search · 5 songs | |
|:-:|---|---|
| ⬆️ | Natural | ImagineDragons 128k → ImagineDragons (Lyrics) 258k |
| ⬆️ | Enna Sona | Sony Music India 128k → T-Series 258k |
| ⬜ | Bulleya | no better upload found (searched again in a month) |
| ✅ | Session check | healthy before and after |

- ⬆️ one row per `found`; ⬜ one row for `not_found` split by `reasons` ("N matches are all 128k: likely the best that exists;
  M had no matching upload"; both are searched again in a month).
- `aborted` non-empty: a ⏸️ row with the reason in plain words. Throttled session: "YouTube is limiting your session. Try later,
  or re-export the cookies." Songs without a result were **not** marked searched, so nothing is lost.
- Never say the old song is replaced yet: the upgrades are only queued.

## 3. What happens next

The found uploads are added to the library list by the next **runsync-youtube** plan (the user confirms it, with a note per
song: old channel and bitrate → new channel and bitrate). The normal Download and Tagging steps then fetch them, and
**runsync-dedupe** compares the audio: the same recording keeps the better copy (top bitrate wins), a doubtful pair is asked
about, and the old upload is removed after confirmation. One line under the table: "Added by the next YouTube plan."

## Rules

- Never search a song at the top tier; never raise the number of songs per run by yourself (it is in `config.yaml`:
  `quality_songs_per_run`, `quality_recheck_days`).
- Never print `cookies.txt`. A rejected or throttled session is a ⏸️ that sends the user to the cookie steps, not something to retry.
- This skill changes nothing on YouTube and deletes nothing.
