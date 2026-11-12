import pytest

from runsync.lock import AlreadyRunning, run_lock


def test_second_lock_refused_then_released(tmp_path):
    with run_lock(tmp_path), pytest.raises(AlreadyRunning), run_lock(tmp_path):
        pass
    with run_lock(tmp_path):  # free again after release
        pass


def test_is_running_sees_a_held_lock_without_taking_it(tmp_path):
    from runsync.lock import is_running, run_lock
    assert is_running(tmp_path) is False
    with run_lock(tmp_path):
        assert is_running(tmp_path) is True
    assert is_running(tmp_path) is False
    with run_lock(tmp_path):    # the check must not have left the lock held
        pass
