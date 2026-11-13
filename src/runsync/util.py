"""Small helpers shared by several modules."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path


def now_iso(precise: bool = False) -> str:
    """UTC timestamp. `precise` adds microseconds, for places where the order of two events inside one run matters."""
    return datetime.now(UTC).isoformat(timespec="microseconds" if precise else "seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
