import pytest

from runsync.retry import PermanentError, classify, run_with_retries


def test_classify():
    assert classify("ERROR: Private video. Sign in") == "permanent"
    assert classify("HTTP Error 503") == "transient"
    assert classify("timed out") == "transient"


def test_transient_retries_then_succeeds():
    calls, slept = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("HTTP Error 503")
        return "ok"

    assert run_with_retries(fn, sleep=slept.append) == "ok"
    assert slept == [5, 30]


def test_transient_gives_up_after_three_retries():
    slept = []
    with pytest.raises(RuntimeError):
        run_with_retries(lambda: (_ for _ in ()).throw(RuntimeError("timeout")), sleep=slept.append)
    assert slept == [5, 30, 120]


def test_permanent_not_retried():
    slept = []
    with pytest.raises(PermanentError):
        run_with_retries(lambda: (_ for _ in ()).throw(RuntimeError("Video unavailable")), sleep=slept.append)
    assert slept == []


def test_on_retry_called_with_attempt_and_delay():
    seen = []
    with pytest.raises(RuntimeError):
        run_with_retries(lambda: (_ for _ in ()).throw(RuntimeError("timeout")), sleep=lambda s: None,
                         on_retry=lambda a, d, e: seen.append((a, d)))
    assert seen == [(1, 5), (2, 30), (3, 120)]
