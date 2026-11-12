"""Single-run lock so two syncs never download/write at the same time."""
from __future__ import annotations

import fcntl
import os
import subprocess
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


def is_running(library: Path) -> bool:
    """True while another runsync sync/resolve holds the lock. Never takes the lock itself."""
    path = Path(library) / ".sync.lock"
    if not path.exists():
        return False
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def running_command(library: Path) -> str | None:
    """Which runsync command holds the lock ("sync", "resolve", "dedupe"), read from the lock owner's command line.
    None when nothing runs; "unknown" when the owner cannot be identified."""
    if not is_running(library):
        return None
    try:
        pid = int((Path(library) / ".sync.lock").read_text().strip() or 0)
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=5, check=False).stdout.split()
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"
    for word in cmd:
        if word in ("sync", "resolve", "dedupe"):
            return word
    return "unknown"
