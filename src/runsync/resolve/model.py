"""Shared types for the resolver."""
from __future__ import annotations

from dataclasses import dataclass, field

# Higher number = more trusted. A new result never replaces a more trusted stored one.
TRUST = {"parser": 10, "itunes": 40, "musicbrainz": 60, "youtube": 70, "override": 100}

CONFIDENT = 0.85      # stop searching once a candidate reaches this
REVIEW_BELOW = 0.75   # below this the track goes to review.csv


@dataclass
class Candidate:
    tier: str                      # key of TRUST
    title: str
    artists: list[str] = field(default_factory=list)   # primary first
    album: str | None = None
    year: int | None = None
    confidence: float = 0.0
    versions: list[str] = field(default_factory=list)  # live, remix, remaster, ...
    hints: list[str] = field(default_factory=list)     # unclassified names from the title (singers, cast)
    alts: list[str] = field(default_factory=list)      # other possible readings of artist/title, for lookups
    note: str = ""
    extra: dict = field(default_factory=dict)         # mb ids, genre, artwork url

    @property
    def artist(self) -> str | None:
        return self.artists[0] if self.artists else None

    @property
    def trust(self) -> int:
        return TRUST[self.tier]
