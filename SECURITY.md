# Security

runsync handles a **logged-in YouTube session** (`cookies.txt`), which works like a password for your Google account.

## What must never be committed or shared

- `cookies.txt` (your session)
- `client_secret.json` (your Google OAuth client) and `youtube_token.json` (your saved YouTube login, owner-only; it can change your playlists)
- `state.db` and other SQLite files (your listening library)
- `logs/` (may contain video IDs and titles)
- API keys (`.env`, `ANTHROPIC_API_KEY`)

The repo enforces this three ways: `.gitignore`, a pre-commit hook (`scripts/check_sensitive.py`) that checks file names **and contents**, and the same guard in CI on every PR.

## If a secret was committed

1. Treat it as compromised. Log out of YouTube everywhere (this invalidates the exported cookies), revoke runsync at https://myaccount.google.com/permissions (this invalidates `youtube_token.json`), delete and recreate the OAuth client in Google Cloud, and/or rotate the key.
2. Remove it from history (`git filter-repo`), then force-push.
3. Do not rely on deleting the file in a later commit.

## Reporting a vulnerability

This is a personal project. Open a private security advisory on GitHub (Security tab → Report a vulnerability) rather than a public issue.
