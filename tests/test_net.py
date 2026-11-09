import json

from runsync.resolve.net import Http


def make(conn, responses, **kw):
    calls = []

    def fetch(url):
        calls.append(url)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return json.dumps(r).encode()
    sleeps: list[float] = []
    return Http(conn, fetch=fetch, sleep=sleeps.append, retry_delays=(0.1,), **kw), calls, sleeps


def test_second_identical_call_is_served_from_cache(conn):
    http, calls, _ = make(conn, [{"a": 1}])
    assert http.get_json("https://itunes.apple.com/search", {"term": "x"}) == {"a": 1}
    assert http.get_json("https://itunes.apple.com/search", {"term": "x"}) == {"a": 1}
    assert len(calls) == 1 and http.hits == 1 and http.misses == 1


def test_failure_returns_none_and_is_never_cached(conn):
    http, _, _ = make(conn, [OSError("down"), OSError("down"), {"ok": True}])
    assert http.get_json("https://itunes.apple.com/search", {"term": "x"}) is None
    assert http.failures == 1
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0
    assert http.get_json("https://itunes.apple.com/search", {"term": "x"}) == {"ok": True}  # tried again


def test_transient_error_is_retried_once_then_succeeds(conn):
    http, calls, _ = make(conn, [OSError("blip"), {"ok": 1}])
    assert http.get_json("https://itunes.apple.com/search", {"term": "y"}) == {"ok": 1}
    assert len(calls) == 2


def test_empty_answer_is_a_real_answer_and_cached(conn):
    http, calls, _ = make(conn, [{"results": []}])
    http.get_json("https://itunes.apple.com/search", {"term": "none"})
    http.get_json("https://itunes.apple.com/search", {"term": "none"})
    assert len(calls) == 1


def test_polite_delay_per_host(conn):
    http, _, sleeps = make(conn, [{}, {}], )
    http.clock = lambda: 100.0
    http.get_json("https://musicbrainz.org/ws/2/recording", {"q": "a"})
    http.get_json("https://musicbrainz.org/ws/2/recording", {"q": "b"})
    assert any(s >= 1.0 for s in sleeps)


def test_expired_cache_entry_is_refetched(conn):
    http, _, _ = make(conn, [{"v": 1}, {"v": 2}], cache_days=1)
    http.get_json("https://itunes.apple.com/search", {"term": "z"})
    conn.execute("UPDATE http_cache SET fetched_at = '2000-01-01T00:00:00+00:00'")
    assert http.get_json("https://itunes.apple.com/search", {"term": "z"}) == {"v": 2}
