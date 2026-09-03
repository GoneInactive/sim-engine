from exchange.rate_limit import TokenBucketLimiter


def test_burst_then_throttle():
    limiter = TokenBucketLimiter(rate_per_second=10, burst=5)
    results = [limiter.allow("k1", now=0.0) for _ in range(6)]
    assert results == [True, True, True, True, True, False]


def test_refills_over_time():
    limiter = TokenBucketLimiter(rate_per_second=10, burst=5)
    for _ in range(5):
        limiter.allow("k1", now=0.0)
    assert limiter.allow("k1", now=0.05) is False
    assert limiter.allow("k1", now=0.2) is True


def test_keys_are_independent():
    limiter = TokenBucketLimiter(rate_per_second=1, burst=1)
    assert limiter.allow("k1", now=0.0) is True
    assert limiter.allow("k2", now=0.0) is True
