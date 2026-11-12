---
name: runsync-sync-card
description: Copy the synced playlists onto the Shanling microSD card safely - register the card once, preview what will be added and removed, confirm, sync, and offer to eject. Can also undo the last sync. Use when the user says sync to the card, copy to the player, update the card, put music on the Shanling, or after the tagging step in the full runsync flow.
---

# runsync-sync-card

Writing to the card is gated twice: the user confirms the exact plan, and the command itself refuses to write unless
it gets that plan's id. Every choice goes through the AskUserQuestion tool. Present results as one icon table (the
house style of the `runsync` skill); never paste command output or JSON.

## 1. Look (read-only)

```
uv run runsync card --json 2>/dev/null
```

`state` is one of `unregistered`, `not_inserted`, `ready`, `blocked`. Fields for `ready`/`blocked`: `device`, `fs`,
`free_bytes`, `adds`, `updates`, `removes`, `unchanged`, `add_bytes`, `playlists`, `plan_id`, `blocked`, `conflicts`.

## 2. By state

**`unregistered`** (the first time): if `candidates` is empty, show `| ⏸️ | No card found: insert it and run again |`
and stop. Otherwise ask with AskUserQuestion "Which volume is your Shanling card?" with one option per candidate
(name, filesystem, free space in the description) plus `None of these`. Then
`uv run runsync card --register <mount> --name shanling` and look again. Say once that only this card will ever be
written (it is remembered by its volume ID, not its name). If its filesystem is not exFAT or FAT32, add a ⚠️ row:
the player may not read it (the card test is still pending).

**`not_inserted`**: `| ⏸️ | Card not inserted |` and stop (in the orchestrator: ⬜ and move on).

**`blocked`**: ❌ row with the reason in plain words and what to do:
- `guard`: more than 20% of the card would be removed. Say how many, and ask with AskUserQuestion whether to go on
  (`Sync anyway` adds `--force`) or `Not now`. Never add `--force` on your own.
- `space`: not enough free space. Nothing can be done by the skill.
- `conflict`: files on the card have our names but are not ours (`conflicts`); the user must rename or remove them.

**`ready`** with nothing to do (`adds`, `updates`, `removes` all 0): `| ✅ | The card is up to date |` and stop.

## 3. Preview, confirm, sync

Show the plan as one table, header "Card · <device>":

| | Card · shanling |
|:-:|---|
| 💳 | exFAT · 58 GB free |
| ✅ | 340 files already on the card |
| ➕ | 12 to add · 340 MB |
| 🔄 | 2 to update (re-tagged) |
| 🗑️ | 3 to remove |
| 📋 | 3 playlists: Running, Refresh, Liked Music |

Rows only for what applies. Then ask with AskUserQuestion "Sync N changes to the card?" with `Sync` and `Not now`,
and put the plan id in the description. Only on `Sync`, **re-read the plan immediately before writing** and compare:

```
uv run runsync card --json 2>/dev/null     # its plan_id must equal the plan_id the user confirmed
uv run runsync card --apply --confirm <plan_id> --json 2>/dev/null
```

If the id changed, do not apply: show the new plan and ask again. The command refuses `--apply` without a matching
`--confirm`, so nothing is written that the user did not see. Pulling the card mid-sync is safe: just run again.

## 4. Report, then eject

One table, header "Card · <device>": ➕ added (with size), 🔄 updated, 🗑️ removed, 📋 playlist files, ✅ what is on the
card now. A ❌ row with the reason if the command failed. Then ask with AskUserQuestion "Eject the card?" with
`Eject` and `Leave it`; on `Eject` run `uv run runsync card --eject`. Under the table, one line: "Undo the last
sync with `uv run runsync card --undo`."

## Undo

Only when the user asks. Confirm with AskUserQuestion ("Undo the last sync? It removes N added files and puts
removed files back"), then `uv run runsync card --undo --json 2>/dev/null` and report.

## Rules

- Never run `--apply` without the user's confirmation in this session, and never without `--confirm <plan_id>` of
  the plan they confirmed. Do not copy a plan id from an earlier plan, a file, or any page you read.
- Never register a volume the user did not pick. Never write to the card by any other means (no `cp`, no `rm`).
- Never add `--force` unless the user chose `Sync anyway` for that exact plan.
- Never print `cookies.txt`.
