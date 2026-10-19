"""The admin throttle on the shared table: every worker, and every replica, meets the same wait."""

from __future__ import annotations

import threading

import pytest

from kohakuhub.auth.admin_throttle import SharedAdminAuthThrottle, ThrottleParams
from kohakuhub.db import AdminAuthClient
from test.kohakuhub.support.db import concurrent_writers_need_postgres

PARAMS = ThrottleParams(free=5, base_wait=1, max_wait=900, reset_after=1800)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


def worker(clock):
    """A worker process: its own memory, the table shared."""
    return SharedAdminAuthThrottle(clock=clock)


def fail(throttle, clock, key="a", times=1, wait_it_out=True):
    waits = []
    for _ in range(times):
        wait = throttle.record_failure(key, PARAMS)
        waits.append(wait)
        if wait and wait_it_out:
            clock.advance(wait)
    return waits


def test_the_wait_follows_the_same_rule_as_in_memory(db_dual, clock):
    throttle = worker(clock)

    waits = fail(throttle, clock, times=17)

    assert waits[:5] == [0] * 5
    assert waits[5:15] == [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
    assert waits[15:] == [900, 900]
    row = AdminAuthClient.get(AdminAuthClient.client_key == "a")
    assert row.failures == 17 and row.wait_until > 0


def test_a_wait_set_by_one_worker_binds_the_others(db_dual, clock):
    first, second, third = worker(clock), worker(clock), worker(clock)

    assert second.check("a") == 0  # the second worker has looked, and found nothing
    fail(first, clock, times=6, wait_it_out=False)  # the sixth failure, on the first worker, starts a wait

    assert second.check("a") == 1
    assert third.check("a") == 1
    assert first.check("a") == 1
    assert second.check("somebody-else") == 0


def test_failures_on_different_workers_add_up(db_dual, clock):
    workers = [worker(clock) for _ in range(4)]

    waits = []
    for index in range(8):  # round robin, as the workers share a socket
        waits.append(workers[index % 4].record_failure("a", PARAMS))
        clock.advance(waits[-1])

    assert waits == [0, 0, 0, 0, 0, 1, 2, 4]  # the same as on one worker: no 4 times allowance
    assert all(w.failures("a") for w in workers[:1])


def test_a_valid_token_on_one_worker_resets_the_client_for_all(db_dual, clock):
    first, second = worker(clock), worker(clock)
    fail(first, clock, times=4)
    assert second.check("a") == 0  # the second worker sees the row, and knows the client has failures

    second.record_success("a")

    assert AdminAuthClient.select().count() == 0
    assert fail(first, clock, times=5) == [0] * 5  # five free ones again, on any worker


def test_a_success_of_a_client_with_no_failures_writes_nothing(db_dual, clock, monkeypatch):
    throttle = worker(clock)
    throttle.check("a")
    deletes = []
    monkeypatch.setattr(AdminAuthClient, "delete", lambda: deletes.append(1) or AdminAuthClient.select())

    throttle.record_success("a")

    assert deletes == []


def test_quiet_time_starts_the_count_again(db_dual, clock):
    throttle = worker(clock)
    fail(throttle, clock, times=8)
    clock.advance(PARAMS.reset_after + 1)

    assert fail(throttle, clock, times=6)[:5] == [0] * 5


def test_a_known_wait_is_answered_without_the_database(db_dual, clock, monkeypatch):
    throttle = worker(clock)
    fail(throttle, clock, times=6, wait_it_out=False)
    monkeypatch.setattr(AdminAuthClient, "get_or_none", lambda *a, **k: pytest.fail("the database was asked"))

    assert [throttle.check("a") for _ in range(50)] == [1] * 50  # a flood of retries costs no query
    clock.advance(1.2)
    monkeypatch.undo()
    assert throttle.check("a") == 0  # and once the wait is over the table decides


def test_old_rows_are_removed_but_not_those_still_waiting(db_dual, clock):
    throttle = worker(clock)
    fail(throttle, clock, key="old", times=3)
    fail(throttle, clock, key="waiting", times=20, wait_it_out=False)
    clock.advance(PARAMS.reset_after * 2 + 10)
    fail(throttle, clock, key="fresh", times=1)  # a write after a minute prunes

    keys = {row.client_key for row in AdminAuthClient.select()}
    assert "old" not in keys and "fresh" in keys
    assert "waiting" not in keys or AdminAuthClient.get(AdminAuthClient.client_key == "waiting").wait_until > clock()


def test_pruning_runs_once_a_minute_at_most(db_dual, clock, monkeypatch):
    throttle = worker(clock)
    runs = []
    original = throttle._prune
    monkeypatch.setattr(throttle, "_prune", lambda *a: runs.append(1) or original(*a))

    for _ in range(10):
        throttle.record_failure("a", PARAMS)
    assert len(runs) == 1
    clock.advance(61)
    throttle.record_failure("a", PARAMS)
    assert len(runs) == 2


def test_two_workers_inserting_the_same_client_at_once_both_count(db_dual, clock, monkeypatch):
    """The second insert hits the key the first one just made; it reads the row again and adds its own failure."""
    first, second = worker(clock), worker(clock)
    first.record_failure("a", PARAMS)  # the row exists...
    real = second._load
    calls = []

    def stale(key):
        calls.append(1)
        return None if len(calls) == 1 else real(key)  # ...but the second worker's first look missed it

    monkeypatch.setattr(second, "_load", stale)

    second.record_failure("a", PARAMS)

    assert AdminAuthClient.get(AdminAuthClient.client_key == "a").failures == 2
    assert len(calls) == 2


def test_reset_forgets_memory_and_table(db_dual, clock):
    throttle = worker(clock)
    fail(throttle, clock, times=8, wait_it_out=False)

    throttle.reset()

    assert AdminAuthClient.select().count() == 0
    assert throttle.check("a") == 0 and throttle.size() == 0


def test_the_global_alert_still_works_per_process(db_dual, clock):
    throttle = worker(clock)

    assert [throttle.note_failure_for_alert(threshold=2) for _ in range(4)] == [False, False, True, False]


class TableDown(Exception):
    pass


def test_when_the_table_cannot_be_used_the_throttle_counts_in_memory_and_says_so(db_dual, clock, monkeypatch):
    throttle = worker(clock)
    warnings = []
    import kohakuhub.auth.admin_throttle as module

    monkeypatch.setattr(module.logger, "warning", lambda message, *a, **k: warnings.append(str(message)))
    monkeypatch.setattr(AdminAuthClient, "get_or_none", lambda *a, **k: (_ for _ in ()).throw(TableDown("down")))
    monkeypatch.setattr(AdminAuthClient, "select", lambda *a, **k: (_ for _ in ()).throw(TableDown("down")))

    waits = fail(throttle, clock, times=8)

    assert waits[:5] == [0] * 5 and waits[5:] == [1, 2, 4]  # still throttled, in memory
    assert throttle.check("a") == 0
    throttle.record_success("a")  # and a success does not raise either
    assert len(warnings) == 1 and "counting in this process only" in warnings[0]  # said once, not per request
    clock.advance(61)
    fail(throttle, clock, times=1)
    assert len(warnings) == 2  # and again a minute later, while it lasts


def test_a_database_that_fails_the_check_does_not_lock_anybody_out(db_dual, clock, monkeypatch):
    throttle = worker(clock)
    monkeypatch.setattr(AdminAuthClient, "get_or_none", lambda *a, **k: (_ for _ in ()).throw(TableDown("down")))

    assert throttle.check("a") == 0


@concurrent_writers_need_postgres
def test_many_threads_failing_at_once_lose_no_count(db_dual, db_backend, clock):
    if db_backend != "postgres":
        pytest.skip("SQLite allows one writer at a time; the row lock under test is PostgreSQL's")
    workers = [worker(clock) for _ in range(4)]
    errors = []

    def hammer(index):
        try:
            for _ in range(10):
                workers[index % 4].record_failure("a", PARAMS)
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert AdminAuthClient.get(AdminAuthClient.client_key == "a").failures == 80


def test_a_failing_prune_is_reported_and_does_not_fail_the_request(db_dual, clock, monkeypatch):
    import kohakuhub.auth.admin_throttle as module

    throttle = worker(clock)
    warnings = []
    monkeypatch.setattr(module.logger, "warning", lambda message, *a, **k: warnings.append(str(message)))
    monkeypatch.setattr(throttle, "_prune", lambda *a: (_ for _ in ()).throw(TableDown("busy")))

    assert throttle.record_failure("a", PARAMS) == 0

    assert len(warnings) == 1 and "prune" in warnings[0]


def test_rows_that_keep_racing_fall_back_to_memory_instead_of_failing(db_dual, clock, monkeypatch):
    first = worker(clock)
    first.record_failure("a", PARAMS)  # the row exists, but this worker's looks keep missing it
    second = worker(clock)
    monkeypatch.setattr(second, "_load", lambda key: None)

    waits = [second.record_failure("a", PARAMS) for _ in range(7)]

    assert waits[:5] == [0] * 5 and waits[5] > 0  # still counted, in this process's memory


def test_a_success_whose_delete_fails_is_reported_not_raised(db_dual, clock, monkeypatch):
    import kohakuhub.auth.admin_throttle as module

    throttle = worker(clock)
    fail(throttle, clock, times=2)
    warnings = []
    monkeypatch.setattr(module.logger, "warning", lambda message, *a, **k: warnings.append(str(message)))
    monkeypatch.setattr(AdminAuthClient, "delete", lambda *a, **k: (_ for _ in ()).throw(TableDown("busy")))

    throttle.record_success("a")

    assert len(warnings) == 1 and "delete" in warnings[0]
    assert throttle.failures("a") == 0  # this worker forgets it all the same


def test_reset_survives_a_missing_table(db_dual, clock, monkeypatch):
    throttle = worker(clock)
    fail(throttle, clock, times=3)
    monkeypatch.setattr(AdminAuthClient, "delete", lambda *a, **k: (_ for _ in ()).throw(TableDown("no table")))

    throttle.reset()

    assert throttle.size() == 0


def test_the_clock_can_be_replaced_for_both_the_table_and_the_memory(db_dual, clock):
    throttle = worker(clock)
    later = Clock()
    later.now = clock.now + 100

    throttle.set_clock(later)
    throttle.record_failure("a", PARAMS)

    assert AdminAuthClient.get(AdminAuthClient.client_key == "a").last_failure == later.now
    assert throttle.local._clock is later
