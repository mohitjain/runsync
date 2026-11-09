"""Pure diff engine + mass-delete guard. Failure != removal."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Diff:
    new: set[str] = field(default_factory=set)
    present: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)


class MassDeleteError(Exception):
    pass


def compute_diff(desired: set[str], unknown: set[str], known_active: set[str], fetch_complete: bool) -> Diff:
    """desired: ids seen in selected playlists. unknown: ids that appeared but are private/unavailable
    (treated as present, never removed). known_active: ids we currently hold. If the fetch was not
    complete (any failure/empty), nothing is marked removed."""
    d = Diff()
    d.new = desired - known_active
    d.present = known_active & (desired | unknown)
    if fetch_complete and desired:
        d.removed = known_active - desired - unknown
    return d


def check_mass_delete(removed: int, total: int, threshold: float = 0.20) -> None:
    if total > 0 and removed / total > threshold:
        raise MassDeleteError(f"{removed} of {total} tracks ({removed / total:.0%}) would be removed; "
                              f"over the {threshold:.0%} guard. Re-run with --force-delete after checking.")
