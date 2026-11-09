"""Golden set: real junk titles with hand-written correct tags. Every non-xfail entry must pass."""
from pathlib import Path

import pytest
import yaml

from runsync.resolve.model import REVIEW_BELOW
from runsync.resolve.run import _pick
from runsync.resolve.tiers import resolve_candidates

CASES = yaml.safe_load((Path(__file__).parent / "golden" / "titles.yaml").read_text())


def _row(case: dict) -> dict:
    yt = case.get("yt") or {}
    return {"original_title": case["raw"], "title": case["raw"], "channel": case.get("channel"),
            "yt_track": yt.get("track"), "yt_artist": yt.get("artist"), "yt_album": yt.get("album"),
            "yt_release_year": yt.get("year"), "probe_duration_s": None, "duration_s": None}


def _check(case: dict) -> list[str]:
    best = _pick(resolve_candidates(_row(case), None))
    exp, bad = case["expect"], []
    if "artists" in exp and best.artists != exp["artists"]:
        bad.append(f"artists {best.artists} != {exp['artists']}")
    for f in ("title", "album", "year"):
        if f in exp and getattr(best, f) != exp[f]:
            bad.append(f"{f} {getattr(best, f)!r} != {exp[f]!r}")
    if "versions" in exp and sorted(best.versions) != sorted(exp["versions"]):
        bad.append(f"versions {best.versions} != {exp['versions']}")
    if "review" in exp:
        review = (not best.artists) or best.confidence < REVIEW_BELOW
        if review != exp["review"]:
            bad.append(f"review {review} != {exp['review']} (confidence {best.confidence}, tier {best.tier})")
    return bad


@pytest.mark.parametrize("case", [c for c in CASES if "xfail" not in c], ids=lambda c: c["raw"][:50])
def test_golden(case):
    assert _check(case) == []


@pytest.mark.parametrize("case", [c for c in CASES if "xfail" in c], ids=lambda c: c["raw"][:50])
def test_known_limits_are_still_limits(case):
    """Documented offline limits. If one starts passing, move it out of xfail."""
    if not _check(case):
        pytest.fail("now passes offline: remove the xfail marker from golden/titles.yaml")


def test_golden_set_size():
    assert len(CASES) >= 50
