"""Retry policy: transient errors retry with backoff; permanent errors never do."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
BACKOFF = (5, 30, 120)

PERMANENT_MARKERS = (
    "private video", "video unavailable", "this video has been removed", "video has been removed",
    "account associated with this video has been terminated", "not available in your country",
    "blocked it", "who has blocked", "copyright", "members-only", "join this channel", "deleted",
    "no longer available", "this video is not available",
)


class PermanentError(Exception):
    """Track can never be fetched (private/deleted/blocked)."""


def classify(message: str) -> str:
    low = message.lower()
    return "permanent" if any(m in low for m in PERMANENT_MARKERS) else "transient"


def run_with_retries(fn: Callable[[], T], sleep: Callable[[float], None] = time.sleep,
                     delays: tuple[float, ...] = BACKOFF,
                     on_retry: Callable[[int, float, Exception], None] | None = None) -> T:
    """Call fn; retry up to len(delays) times on transient errors. Permanent -> PermanentError."""
    attempt = 0
    while True:
        try:
            return fn()
        except PermanentError:
            raise
        except Exception as exc:
            if classify(str(exc)) == "permanent":
                raise PermanentError(str(exc)) from exc
            if attempt >= len(delays):
                raise
            if on_retry:
                on_retry(attempt + 1, delays[attempt], exc)
            sleep(delays[attempt])
            attempt += 1
