---
name: runsync-dedupe
description: Find and delete duplicate tracks in the runsync library - the same song uploaded more than once - keeping the best one. Asks about pairs it is unsure of. Use when the user says dedupe, duplicates, remove duplicate songs, or after resolve in the full runsync flow.
---

# runsync-dedupe

Deleting is permanent (the loser's file is gone), so nothing is deleted until the user confirms. Every choice goes
through the AskUserQuestion tool. Report with icons and short tables only.

## 1. Look

```
uv run runsync dedupe --json
```

It compares audio fingerprints of tracks with the same artist, title and version (a live or remix version is never
a candidate) and prints `remove` (certain duplicates: keep one, remove one) and `unsure` pairs. Read-only.

If both lists are empty, say "✅ No duplicates" and stop.

## 2a. Many unsure pairs: ask once, in bulk

When there are more than 6 pairs in `unsure`, do not ask pair by pair first. Ask one AskUserQuestion about all of them
(show the count and the match range), with these options:

- `Decide the lowest few, same for the rest` (recommended): ask pair by pair only for pairs under 70% match, treat the
  others as the same song.
- `Same song for all N`
- `Keep both for all N`
- `Ask me about each`

Record the bulk answer with `--same` / `--different` for every pair it covers (loop over the JSON), then continue
with the pairs left to ask.

## 2. Unsure pairs: ask

For each pair in `unsure`, ask with AskUserQuestion (up to 4 questions per call; batch them): "Same song?" with
options:

- `Same song, keep the best` : description shows both uploads ("channel, length, kbps, N% match").
- `Different, keep both`

Both uploads usually have the same artist and title, so never show that name twice. Name them **A** and **B** by
their original YouTube title (`youtube_title`) with channel and length, for example:
"Avicii - Wake Me Up: A = \"Wake Me Up\" (Avicii, 4:07)  vs  B = \"Avicii - Wake Me Up (Official Video)\" (Avicii,
4:32). Same song?" Use the same A/B wording in the option descriptions. Then record each answer:

```
uv run runsync dedupe --same ID1,ID2         # same song (several pairs: --same "A,B;C,D")
uv run runsync dedupe --different ID1,ID2    # different, never asked again
```

## Presentation (you design it)

Never paste command output. From the JSON, one icon table per song, each upload on its own row, identified by its
own YouTube title so identical names can be told apart (A/B for the ones you are unsure about):

| | Guru Randhawa - Lahore | Channel | Length | |
|:-:|---|---|--:|---|
| ✅ | "Lahore" | Guru Randhawa | 3:17 | keep |
| 🗑️ | "Guru Randhawa: Lahore (Official Video)" | T-Series | 3:54 | 88% match |

| | Avicii - Wake Me Up (you decide) | Channel | Length | |
|:-:|---|---|--:|---|
| ❓ | **A** "Wake Me Up" | Avicii | 4:07 | |
| ❓ | **B** "Avicii - Wake Me Up (Official Video)" | Avicii | 4:32 | 75% match |

## 3. Show the plan, confirm, delete

```
uv run runsync dedupe
```

Present it as above (use `uv run runsync dedupe --json`). If something is marked for deletion, ask with AskUserQuestion:
"Delete N duplicate(s) for good?" with options `Delete N` and `Not now`. Only on `Delete N`:

```
uv run runsync dedupe --apply
```

If it says the mass-delete guard stopped it (more than 20% of the library), show the message and ask whether to
re-run with `--force`; never add `--force` on your own.

## 4. Rebuild the winners' player copies

A kept track may have had no player copy (it was held as a duplicate while the other upload's copy existed, and that
copy is now deleted). After `--apply`, always run:

```
uv run runsync resolve
```

It builds the missing copies and nothing else.

## 5. Report (you design it)

One icon table, header "Duplicates", rows only for what applies:

| | Duplicates |
|:-:|---|
| 🗑️ | 3 deleted |
| ❓ | 1 pair kept (you said different) |
| 🎵 | Player copies rebuilt |

Nothing found: a single row `| ⬜ | No duplicates |`. If anything failed, a ❌ row with the reason. Under the table, one
line: "A wrong merge can be undone: `uv run runsync dedupe --undo <video id>` queues it for download again."

## Rules

- Never run `--apply` without the user's confirmation in this session.
- Never delete anything by hand; use the command.
- Do not touch `state.db` or `master/` directly.
