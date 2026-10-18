"""The wait a client earns by failing: a few free mistakes, then a short wait that doubles."""

from __future__ import annotations

import pytest

from kohakuhub.auth.admin_throttle import AdminAuthThrottle, ThrottleParams

PARAMS = ThrottleParams(free=5, base_wait=1, max_wait=900, reset_after=1800)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def throttle(clock):
    return AdminAuthThrottle(clock=clock)


def fail(throttle, clock, key="a", times=1, wait_it_out=True):
    waits = []
    for _ in range(times):
        wait = throttle.record_failure(key, PARAMS)
        waits.append(wait)
        if wait and wait_it_out:
            clock.advance(wait)
    return waits


def test_the_first_five_failures_cost_nothing(throttle, clock):
    assert fail(throttle, clock, times=5) == [0, 0, 0, 0, 0]
    assert throttle.check("a") == 0


def test_from_the_sixth_failure_the_wait_doubles_up_to_the_cap(throttle, clock):
    waits = fail(throttle, clock, times=17)
    assert waits[:5] == [0] * 5
    assert waits[5:15] == [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
    assert waits[15:] == [900, 900]  # capped


def test_a_client_in_its_wait_is_told_how_long_and_the_wait_runs_out(throttle, clock):
    fail(throttle, clock, times=6, wait_it_out=False)  # the 6th starts a 1 s wait
    assert throttle.check("a") == 1
    clock.advance(0.4)
    assert throttle.check("a") == 1  # rounded up: 0.6 s left is still "1"
    clock.advance(0.7)
    assert throttle.check("a") == 0


def test_other_clients_are_not_affected(throttle, clock):
    fail(throttle, clock, key="attacker", times=9, wait_it_out=False)
    assert throttle.check("attacker") > 0
    assert throttle.check("administrator") == 0
    assert fail(throttle, clock, key="administrator", times=1) == [0]


def test_a_valid_token_resets_the_client(throttle, clock):
    fail(throttle, clock, times=8)
    throttle.record_success("a")
    assert throttle.check("a") == 0
    assert fail(throttle, clock, times=6)[:5] == [0] * 5  # five free ones again


def test_quiet_time_resets_the_count(throttle, clock):
    fail(throttle, clock, times=8)
    clock.advance(PARAMS.reset_after + 1)
    assert fail(throttle, clock, times=6)[:5] == [0] * 5


def test_success_of_an_unknown_client_is_harmless(throttle):
    throttle.record_success("never-seen")
    assert throttle.check("never-seen") == 0


def test_memory_is_bounded_and_the_oldest_client_is_forgotten(clock):
    throttle = AdminAuthThrottle(clock=clock, max_clients=3)
    for key in ("a", "b", "c"):
        fail(throttle, clock, key=key, times=7, wait_it_out=False)
    fail(throttle, clock, key="d", times=1)  # evicts the least recently used: "a"
    assert throttle.size() == 3
    assert throttle.check("a") == 0
    assert throttle.check("b") > 0


def test_touching_a_client_keeps_it_from_being_evicted(clock):
    throttle = AdminAuthThrottle(clock=clock, max_clients=2)
    fail(throttle, clock, key="a", times=7, wait_it_out=False)
    fail(throttle, clock, key="b", times=7, wait_it_out=False)
    throttle.check("a")  # a was just used
    fail(throttle, clock, key="c", times=1)
    assert throttle.check("a") > 0 and throttle.check("b") == 0


def test_the_global_alert_fires_once_a_minute_when_failures_are_many(throttle, clock):
    fired = [throttle.note_failure_for_alert(threshold=3) for _ in range(5)]
    assert fired == [False, False, False, True, False]  # fires on the 4th, then holds its tongue
    clock.advance(30)
    assert throttle.note_failure_for_alert(threshold=3) is False  # still inside the same minute
    clock.advance(61)
    # The old failures have aged out of the minute; a new burst over the threshold fires again.
    assert [throttle.note_failure_for_alert(threshold=3) for _ in range(4)] == [False, False, False, True]


def test_the_alert_can_be_turned_off(throttle):
    assert [throttle.note_failure_for_alert(threshold=0) for _ in range(100)] == [False] * 100


def test_reset_forgets_everything(throttle, clock):
    fail(throttle, clock, times=8, wait_it_out=False)
    throttle.reset()
    assert throttle.size() == 0 and throttle.check("a") == 0
