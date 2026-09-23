from rate_limit import SlidingWindowRateLimiter


# ── allow() -- check-and-record in one call ──────────────────────────────────

def test_allows_up_to_max_calls_within_window():
    limiter = SlidingWindowRateLimiter(max_calls=3, window_seconds=60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True


def test_rejects_call_beyond_max_calls_within_window():
    limiter = SlidingWindowRateLimiter(max_calls=2, window_seconds=60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_keys_are_independent():
    limiter = SlidingWindowRateLimiter(max_calls=1, window_seconds=60)
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is False


def test_old_hits_expire_out_of_the_window(monkeypatch):
    limiter = SlidingWindowRateLimiter(max_calls=1, window_seconds=10)
    fake_now = [1000.0]
    monkeypatch.setattr("rate_limit.time.monotonic", lambda: fake_now[0])

    assert limiter.allow("k") is True
    assert limiter.allow("k") is False  # still inside the 10s window

    fake_now[0] += 10.1  # past the window
    assert limiter.allow("k") is True


# ── check() / record() -- split for "only count some calls" limiters ───────

def test_check_does_not_record_a_hit():
    limiter = SlidingWindowRateLimiter(max_calls=1, window_seconds=60)
    assert limiter.check("k") is True
    assert limiter.check("k") is True  # unaffected by the previous check
    assert limiter.check("k") is True


def test_record_charges_a_hit_that_check_then_sees():
    limiter = SlidingWindowRateLimiter(max_calls=1, window_seconds=60)
    assert limiter.check("k") is True
    limiter.record("k")
    assert limiter.check("k") is False


def test_check_and_record_split_matches_allow_semantics(monkeypatch):
    # allow() and manual check()+record() should behave identically when
    # every call is recorded -- this pins that the two APIs agree.
    fake_now = [0.0]
    monkeypatch.setattr("rate_limit.time.monotonic", lambda: fake_now[0])
    a = SlidingWindowRateLimiter(max_calls=2, window_seconds=60)
    b = SlidingWindowRateLimiter(max_calls=2, window_seconds=60)

    results_a = []
    results_b = []
    for _ in range(4):
        results_a.append(a.allow("k"))
        ok = b.check("k")
        b.record("k")
        results_b.append(ok)
        fake_now[0] += 1

    assert results_a == results_b


# ── bounded memory ────────────────────────────────────────────────────────────

def test_stale_keys_are_evicted_after_enough_accesses(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr("rate_limit.time.monotonic", lambda: fake_now[0])
    limiter = SlidingWindowRateLimiter(max_calls=5, window_seconds=10)
    limiter._EVICT_EVERY = 2

    limiter.allow("stale-key")
    fake_now[0] += 100  # long past the 10s window
    limiter.allow("other-key-1")
    limiter.allow("other-key-2")  # this access triggers the sweep (every 2)

    assert "stale-key" not in limiter._hits


def test_key_count_never_exceeds_max_keys():
    limiter = SlidingWindowRateLimiter(max_calls=5, window_seconds=60, max_keys=50)
    for i in range(500):
        limiter.allow(f"key-{i}")
    assert len(limiter._hits) <= 50


def test_hard_cap_evicts_least_recently_touched_key_first(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr("rate_limit.time.monotonic", lambda: fake_now[0])
    limiter = SlidingWindowRateLimiter(max_calls=5, window_seconds=1000, max_keys=2)

    limiter.allow("oldest")
    fake_now[0] += 1
    limiter.allow("middle")
    fake_now[0] += 1
    limiter.allow("newest")  # pushes the table to 3 keys -> eviction fires

    assert "oldest" not in limiter._hits
    assert "middle" in limiter._hits
    assert "newest" in limiter._hits
