from smartsolar.backoff import Backoff


def test_the_delay_grows_up_to_the_maximum():
    backoff = Backoff(2.0, 60.0, rng=lambda: 1.0)
    assert [backoff.next() for _ in range(7)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_endless_retries_stay_at_the_maximum_without_overflowing():
    backoff = Backoff(2.0, 60.0, rng=lambda: 1.0)
    for _ in range(5000):  # 2.0**1024 overflowed after about 8.5 h of retries (issue #4)
        assert backoff.next() <= 60.0


def test_reset_starts_over():
    backoff = Backoff(2.0, 60.0, rng=lambda: 1.0)
    for _ in range(5000):
        backoff.next()
    backoff.reset()
    assert backoff.next() == 2.0
