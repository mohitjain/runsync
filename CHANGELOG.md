# Changelog

## Unreleased

### Phase 1: foundation
- Playlist discovery from the logged-in account (`runsync playlists`), plus `add-playlist`.
- Fetch, diff and mass-delete guard; removed tracks go to `.trash/` for 14 days.
- Best-quality download (Premium AAC ~258 kbps) with `.part` files, ffprobe check and atomic move.
- Retry/backoff, cookie-expiry and rotation detection, run lock, disk reconciliation.
- Per-track database record and event log; rotating run logs.
- Repo safety guard for cookies, databases, logs and secrets (hook + CI).
