---
name: runsync-setup
description: Set up runsync on a Mac from a fresh clone - install the system tools with Homebrew, run uv sync, guide the YouTube cookie export, and verify everything with a dry run. Use when the user says set up / install / get started with runsync, or when a runsync command fails because ffmpeg, deno, uv or cookies.txt is missing.
---

# runsync-setup

Take a fresh clone of this repo to a working `uv run runsync sync --dry-run`. Run the checks yourself with Bash,
install what is missing, and stop to ask the user only for the things you cannot do (logging in to YouTube).
Work from the repo root. Say what you are about to install before installing it.

## 1. Check the machine

```
uname -s            # must be Darwin (macOS). On anything else, stop and say runsync targets macOS.
command -v brew     # Homebrew
```

If `brew` is missing, do NOT run the Homebrew install script yourself. Tell the user to install Homebrew from
https://brew.sh (they paste the official one-liner into their own terminal), then continue once `command -v brew`
succeeds.

## 1b. Ask for the cookies first

The cookie export is the only step the user must do by hand, so ask for it now (section 4, steps 1-2) and install
while they do it. Copy and validate the file in section 4 once `uv sync` has finished.

## 2. Install the tools that are missing

Check each, then install only the missing ones in one `brew install` call:

| Command to check | Brew package |
|---|---|
| `python3 --version` (3.11 or newer) | `python` |
| `uv --version` | `uv` |
| `ffmpeg -version` and `ffprobe -version` | `ffmpeg` |
| `fpcalc -version` | `chromaprint` |
| `deno --version` | `deno` |

```
brew install python uv ffmpeg chromaprint deno      # minus anything already present
```

`deno` is required: without it yt-dlp cannot solve YouTube's JS challenge and every download fails with
"Requested format is not available".

## 3. Install the project

```
uv sync
uv run runsync --help          # must list playlists, sync, resolve, dedupe, card, youtube
```

## 4. Cookies (the only manual step)

Premium quality (258 kbps instead of ~130) needs a logged-in YouTube cookie file. Keep it simple:

1. Tell the user to download it:
   - Open a new **Incognito** window, log in to youtube.com (only tab open).
   - In that tab go to `https://www.youtube.com/robots.txt`.
   - Export the youtube.com cookies with the "Get cookies.txt LOCALLY" extension, then close the Incognito window.
   - Reason for the odd steps: YouTube rotates the cookies of any tab that stays open, so a normal export goes stale
     within minutes.
2. Ask: "Paste the path of the file you downloaded." Do not search the disk for it.
3. Copy it into the repo root as `cookies.txt` (`cp "<path>" ./cookies.txt`). Never open, print or paste its contents.
4. Confirm it is ignored by git, then check it:

   ```
   git check-ignore -v cookies.txt          # must print the .gitignore rule; if not, STOP and fix .gitignore
   uv run python -c "from pathlib import Path; from runsync.cookies import check_cookies as c; s = c(Path('cookies.txt')); print(s.state, s.detail)"
   ```
5. Tell the user, in these words or close to them: "Copied into the repo as `cookies.txt`. It is gitignored, so it
   will never be committed. It works as your YouTube login: don't share it, and you can delete the original in your
   Downloads folder."

`ok` here only means the file has login cookies that have not expired by date. YouTube can still have rotated them
on the server, which this check cannot see. The real test is `runsync playlists` in section 5, which logs in with
the cookies: if it prints "YouTube invalidated your cookies (they were rotated)" or shows no account playlists,
treat the cookies as bad even though this check said `ok`, and go back to step 1.

If the check says `missing`, `no_login` or `expired`, ask them to export again (step 1) and give the new path.
If the user declines, runsync still works anonymously at reduced quality; say so and continue.

## 4b. YouTube login (optional, recommended)

Cookies rotate and break; the official YouTube API does not. Signed in, runsync reads your playlists through the API
(cookies are then only needed to download audio) and can tidy your playlists. Ask with AskUserQuestion "Sign in to the
YouTube API now?" (`Yes, set it up` / `Later`). `Later` is fine: everything still works with cookies.

On yes: check `ls client_secret.json`. If it is missing, give the README steps ("YouTube login") in three short lines:
Google Cloud project, YouTube Data API v3 enabled, consent screen with the user as test user, a **Desktop** OAuth client;
save its JSON in the repo folder as `client_secret.json` (it is gitignored: never print it). Then tell the user to run
`! uv run runsync youtube --login` (it opens a browser), and confirm with `uv run runsync youtube --json 2>/dev/null`
(`logged_in` true). While the Google app is in testing mode the login can expire after 7 days: `--login` again.

## 5. Choose playlists

Run the `runsync-playlists` skill: it lists every playlist, asks which to start and stop syncing, and applies the
answers. Its first command (`runsync playlists`) also proves the cookies work, because it logs in to YouTube.
If it says the list could not be refreshed, go back to section 4.

## 6. Verify

```
uv run runsync sync --dry-run
uv run pytest -q               # optional: confirms the install
```

The dry run downloads and deletes nothing. Judge it only on install and cookie problems (a crash, or the
"cookies were rotated" message). Do NOT report problems with individual playlists (for example one that fails to
fetch): setup does not own them, and a fresh user has not chosen any yet. Then offer the next
steps, in order, and let the user choose:

1. `uv run runsync sync -n 5` : download five tracks as a trial.
2. `uv run runsync resolve --dry-run` : preview clean names.
3. `uv run runsync resolve` : tags, covers, player copies in `~/Music/RunSync/player/m4a/`.
4. The `runsync-enrich` skill for whatever resolve could not settle.

## Rules

- Install only into Homebrew and this project's `.venv`. Do not use sudo and do not edit shell profiles.
- Do not run `sync` for real, `resolve` for real, or touch `~/Music/RunSync` during setup; verification is dry-run only.
- Report at the end as ONE table with two columns, icon and step, nothing else (no Result column, no paragraphs
  before it), composed by you, never pasted from a command. Example:

  | | Setup |
  |:-:|---|
  | ✅ | Homebrew |
  | ✅ | Tools |
  | ✅ | Project installed |
  | ✅ | Cookies |
  | ⬜ | YouTube login (optional) |
  | ⏸️ | Choose playlists |

  Icons: ✅ done, ❌ failed, ⚠️ needs attention, ⏸️ waiting. Rows: Homebrew, Tools, Project installed,
  Cookies, YouTube login, Playlists. After the table, at most one line, and only if something is ❌ or ⚠️, saying what the user must do.
