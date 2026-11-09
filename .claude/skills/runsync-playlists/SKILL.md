---
name: runsync-playlists
description: Choose which YouTube playlists runsync syncs - show every playlist with what is already syncing, ask which to start and which to stop, and apply the answers (numbers or names). Use when the user wants to pick, add, remove, start or stop syncing playlists, or after runsync-setup.
---

# runsync-playlists

Keep this short and visual. The user answers with numbers or names; you do the rest.

## 1. Show the list

```
uv run runsync playlists
```

It refreshes from YouTube and prints a numbered table: ✅ syncing, ⬜ not syncing. Show that table to the user as it is.

If the output starts with "⚠️ Could not refresh from YouTube", the list is the saved one, not live, and the cookies need
re-exporting (the command prints the steps). Say so in one line; continue with the saved list only if the user
agrees.

## 2. Ask with the AskUserQuestion tool (never as plain text)

Always ask with the AskUserQuestion tool, as multi-select questions, one per group that applies. Do not print the
questions in chat for the user to type answers to.

- "Which playlists do you want to START syncing?" (header `Start`): one option per ⬜ playlist.
- "Which playlists do you want to STOP syncing?" (header `Stop`): one option per ✅ playlist.
- Skip a question when its group is empty. Ask both in a single AskUserQuestion call.
- Option label = the playlist name exactly as in the table; description = its state and track count (for example
  "✅ syncing now · 94 tracks").
- Add a final option `None` to each question, so "no change" is a real choice.
- The tool takes at most 4 options per question and 4 questions per call. With more than 3 playlists in a group,
  split the group into several questions (`Start 1/2`, `Start 2/2`), each with its own `None`. With more than 12 in a
  group, show the numbered table and ask in plain text for numbers or names instead.
- The user can also choose "Other" and type numbers or names; accept those too.

A playlist that is not in the account can be added by URL: `uv run runsync add-playlist <url>`.

## 3. Apply

```
uv run runsync playlists --sync "<start answer>" --stop "<stop answer>"
```

Join the chosen playlist names with commas (names or numbers both work). Leave an option out when its answer is
`None`. Do not refresh between showing the list and applying (the numbers
must match what the user saw); the command already skips the refresh when `--sync` or `--stop` is given.

If it prints a red error (unknown number, unknown or ambiguous name, a playlist in both lists), nothing was changed:
show the message and ask again.

## 4. Confirm

Show the table the command prints (the new ✅/⬜ state). If it prints a ⚠️ line about tracks that will move to
`.trash`, repeat that line once and say it only happens at the next `sync` (kept 14 days), and that
`uv run runsync sync --dry-run` previews it. Then offer: `uv run runsync sync --dry-run`.

## Rules

- Never edit `state.db` directly; use the command.
- Stopping a playlist deletes nothing by itself. Do not run `sync` for real unless the user asks.
