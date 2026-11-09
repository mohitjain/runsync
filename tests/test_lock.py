import pytest

from runsync.lock import AlreadyRunning, run_lock


def test_second_lock_refused_then_released(tmp_path):
    with run_lock(tmp_path), pytest.raises(AlreadyRunning), run_lock(tmp_path):
        pass
    with run_lock(tmp_path):  # free again after release
        pass
