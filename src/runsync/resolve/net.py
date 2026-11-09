"""Rate-limited, cached JSON lookups (MusicBrainz, iTunes). A failed call is never cached."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..retry import run_with_retries

log = logging.getLogger("runsync.net")
USER_AGENT = "runsync/0.1 (+https://github.com/mohitjain/runsync)"
MIN_INTERVAL = {"musicbrainz.org": 1.1, "itunes.apple.com": 3.0, "coverartarchive.org": 0.5}


def _urlopen(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class Http:
    """GET JSON with an on-disk (SQLite) cache and a polite per-host delay."""

    def __init__(self, conn: sqlite3.Connection, cache_days: int = 90, fetch: Callable[[str], bytes] = _urlopen,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 retry_delays: tuple[float, ...] = (2, 10)) -> None:
        self.conn, self.cache_days, self.fetch, self.sleep, self.clock = conn, cache_days, fetch, sleep, clock
        self.retry_delays = retry_delays
        self._last: dict[str, float] = {}
        self.hits = 0
        self.misses = 0
        self.failures = 0

    def get_json(self, base: str, params: dict) -> dict | None:
        url = f"{base}?{urllib.parse.urlencode(params)}"
        row = self.conn.execute("SELECT body, fetched_at FROM http_cache WHERE key = ?", (url,)).fetchone()
        if row and datetime.fromisoformat(row["fetched_at"]) > datetime.now(UTC) - timedelta(days=self.cache_days):
            self.hits += 1
            return json.loads(row["body"])
        host = urllib.parse.urlparse(base).hostname or ""
        wait = MIN_INTERVAL.get(host, 1.0) - (self.clock() - self._last.get(host, -1e9))
        if wait > 0:
            self.sleep(wait)
        self._last[host] = self.clock()

        def attempt() -> bytes:
            return self.fetch(url)

        try:
            body = run_with_retries(attempt, sleep=self.sleep, delays=self.retry_delays)
            data = json.loads(body)
        except Exception as e:  # noqa: BLE001 (network down, 5xx, bad JSON: answer unknown, cache nothing)
            self.failures += 1
            log.warning("lookup failed (%s): %s", type(e).__name__, url)
            return None
        self.misses += 1
        self.conn.execute("INSERT OR REPLACE INTO http_cache (key, body, fetched_at) VALUES (?, ?, ?)",
                          (url, json.dumps(data), datetime.now(UTC).isoformat(timespec="seconds")))
        self.conn.commit()
        return data
