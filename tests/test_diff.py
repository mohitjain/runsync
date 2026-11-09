import pytest

from runsync.diff import MassDeleteError, check_mass_delete, compute_diff


def test_new_present_removed():
    d = compute_diff({"a", "b", "c"}, set(), {"b", "c", "d"}, True)
    assert d.new == {"a"} and d.present == {"b", "c"} and d.removed == {"d"}


def test_incomplete_fetch_removes_nothing():
    assert compute_diff({"a"}, set(), {"a", "b"}, False).removed == set()


def test_empty_desired_removes_nothing():
    assert compute_diff(set(), set(), {"a", "b"}, True).removed == set()


def test_unknown_is_never_removed():
    d = compute_diff({"a"}, {"b"}, {"a", "b"}, True)
    assert d.removed == set() and "b" in d.present


def test_guard_blocks_over_threshold():
    with pytest.raises(MassDeleteError):
        check_mass_delete(3, 10, 0.20)


def test_guard_allows_at_threshold():
    check_mass_delete(2, 10, 0.20)
