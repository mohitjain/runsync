"""Single-run lock so two syncs never download/write at the same time."""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(Exception):
    pass


@contextmanager
def run_lock(library: Path):
    path = Path(library) / ".sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")  # noqa: SIM115 (held for the whole run)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        pid = f.read().strip() or "unknown"
        f.close()
        raise AlreadyRunning(f"another runsync sync is already running (pid {pid})") from None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
