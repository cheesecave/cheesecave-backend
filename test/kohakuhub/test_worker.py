"""Tests for the background task worker runtime."""

import asyncio
import json
import os
import signal
from datetime import timedelta

import pytest

from kohakuhub import tasks, worker as worker_module
from kohakuhub.db import BackgroundTask, BackgroundTaskEvent, BackgroundTaskLog, BackgroundWorker
from kohakuhub.worker import Worker
from test.kohakuhub.support.db import table_missing

pytestmark = pytest.mark.usefixtures("db_scope")


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(tasks, "_registry", {})
    return tasks._registry


def _worker(**options):
    defaults = dict(
        worker_id="w-test", concurrency=2, lease_seconds=3, poll_interval=0.05, shutdown_grace=2
    )
    return Worker(**(defaults | options))


async def _run_until(worker, predicate, timeout=5.0):
    """Run ``worker`` until ``predicate()`` holds, then stop it."""
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run(stop))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while not predicate():
            assert loop.time() < deadline, "worker did not reach the expected state"
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await runner


def _status(task_id):
    return BackgroundTask.get_by_id(task_id).status


def test_worker_defaults_come_from_config(monkeypatch):
    monkeypatch.setattr(worker_module.cfg.worker, "queues", ["bulk"])
    worker = Worker()

    assert worker.worker_id.count(":") == 2
    assert worker.concurrency == worker_module.cfg.worker.concurrency
    assert worker.queues == ["bulk"]
    assert Worker(queues=[]).queues == []


async def test_worker_runs_task_to_success():
    seen = []

    @tasks.task("test.ok")
    async def handler(payload):
        seen.append(payload)

    task_id = tasks.enqueue("test.ok", {"n": 1})
    await _run_until(_worker(), lambda: _status(task_id) == tasks.SUCCEEDED)

    assert seen == [{"n": 1}]


async def test_worker_retries_failures_and_fails_permanent_errors(monkeypatch):
    monkeypatch.setattr(tasks, "retry_delay", lambda attempts: 3600)

    @tasks.task("test.flaky")
    async def flaky(payload):
        raise RuntimeError("temporarily broken")

    @tasks.task("test.fatal")
    async def fatal(payload):
        raise tasks.PermanentTaskError("bad input")

    flaky_id = tasks.enqueue("test.flaky")
    fatal_id = tasks.enqueue("test.fatal")
    await _run_until(
        _worker(),
        lambda: BackgroundTask.get_by_id(flaky_id).last_error and _status(fatal_id) == tasks.FAILED,
    )

    flaky_row = BackgroundTask.get_by_id(flaky_id)
    assert flaky_row.status == tasks.QUEUED
    assert flaky_row.last_error == "RuntimeError: temporarily broken"
    assert BackgroundTask.get_by_id(fatal_id).last_error == "PermanentTaskError: bad input"


async def test_worker_times_out_slow_handlers():
    @tasks.task("test.slow", timeout=0.05, max_attempts=1)
    async def slow(payload):
        await asyncio.sleep(10)

    task_id = tasks.enqueue("test.slow")
    await _run_until(_worker(), lambda: _status(task_id) == tasks.FAILED)

    assert BackgroundTask.get_by_id(task_id).last_error == "Timed out after 0.05s"


async def test_worker_heartbeat_keeps_long_task_owned():
    # The handler outlives the lease, so only heartbeats keep it owned. The
    # lease leaves ~0.6s of slack for slow CI runners between renewals.
    @tasks.task("test.long")
    async def long_task(payload):
        await asyncio.sleep(2.0)

    task_id = tasks.enqueue("test.long")
    await _run_until(_worker(lease_seconds=0.9), lambda: _status(task_id) == tasks.SUCCEEDED)

    assert BackgroundTask.get_by_id(task_id).attempts == 1


async def test_worker_abandons_task_when_lease_is_lost():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @tasks.task("test.stolen")
    async def stolen(payload):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task_id = tasks.enqueue("test.stolen")

    async def steal():
        await started.wait()
        BackgroundTask.update(locked_by="someone-else").where(
            BackgroundTask.id == task_id
        ).execute()

    stealer = asyncio.create_task(steal())
    await _run_until(_worker(lease_seconds=0.15), cancelled.is_set)
    await stealer

    row = BackgroundTask.get_by_id(task_id)
    assert (row.status, row.locked_by) == (tasks.RUNNING, "someone-else")


async def test_worker_survives_heartbeat_errors(monkeypatch):
    calls = {"renew": 0}
    real_heartbeat = tasks.heartbeat

    def flaky_heartbeat(row, **kwargs):
        calls["renew"] += 1
        if calls["renew"] == 1:
            raise RuntimeError("db hiccup")
        return real_heartbeat(row, **kwargs)

    monkeypatch.setattr(tasks, "heartbeat", flaky_heartbeat)

    @tasks.task("test.hiccup")
    async def hiccup(payload):
        await asyncio.sleep(1.5)

    # First renewal (0.3s) fails, the second (0.6s) must land before the
    # 0.9s lease expires; the handler outlives the lease.
    task_id = tasks.enqueue("test.hiccup")
    await _run_until(_worker(lease_seconds=0.9), lambda: _status(task_id) == tasks.SUCCEEDED)

    assert calls["renew"] >= 2


async def test_worker_respects_concurrency_limit():
    release = asyncio.Event()
    active = {"now": 0, "max": 0}

    @tasks.task("test.blocking")
    async def blocking(payload):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await release.wait()
        active["now"] -= 1

    ids = [tasks.enqueue("test.blocking") for _ in range(3)]
    worker = _worker(concurrency=2)
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.3)
    assert active["max"] == 2
    assert sorted(_status(i) for i in ids) == [tasks.QUEUED, tasks.RUNNING, tasks.RUNNING]

    release.set()
    while any(_status(i) != tasks.SUCCEEDED for i in ids):
        await asyncio.sleep(0.02)
    stop.set()
    await runner


async def test_worker_drains_running_tasks_on_stop():
    started = asyncio.Event()

    @tasks.task("test.drain")
    async def drain(payload):
        started.set()
        await asyncio.sleep(0.2)

    task_id = tasks.enqueue("test.drain")
    stop = asyncio.Event()
    runner = asyncio.create_task(_worker().run(stop))
    await started.wait()
    stop.set()
    await runner

    assert _status(task_id) == tasks.SUCCEEDED


async def test_worker_starts_periodic_tasks():
    runs = []

    @tasks.task("test.tick", every=timedelta(hours=1))
    async def tick(payload):
        runs.append(1)

    await _run_until(_worker(), lambda: runs)

    pending = BackgroundTask.get(BackgroundTask.status == tasks.QUEUED)
    assert pending.dedupe_key == tasks.periodic_key("test.tick")


async def test_worker_survives_claim_and_record_errors(monkeypatch):
    @tasks.task("test.ok")
    async def handler(payload):
        return None

    task_id = tasks.enqueue("test.ok")
    real_claim, real_complete = tasks.claim_next, tasks.complete_task
    failures = {"claim": 1, "complete": 1}

    def flaky_claim(*args, **kwargs):
        if failures["claim"]:
            failures["claim"] -= 1
            raise RuntimeError("connection reset")
        return real_claim(*args, **kwargs)

    def flaky_complete(row):
        if failures["complete"]:
            failures["complete"] -= 1
            raise RuntimeError("connection reset")
        return real_complete(row)

    monkeypatch.setattr(tasks, "claim_next", flaky_claim)
    monkeypatch.setattr(tasks, "complete_task", flaky_complete)

    # The first outcome write fails; the task stays running until its lease
    # expires, then the worker claims and completes it again.
    await _run_until(_worker(lease_seconds=0.3), lambda: _status(task_id) == tasks.SUCCEEDED)

    assert BackgroundTask.get_by_id(task_id).attempts == 2


def test_reset_connection_logs_close_errors(monkeypatch):
    # Deliberate driver failure: close() cannot be made to raise on a real connection.
    def broken_close():
        raise RuntimeError("already gone")

    monkeypatch.setattr(worker_module.db, "close", broken_close)
    worker_module._reset_connection()  # does not raise


async def test_wait_for_schema_retries_until_table_exists(db_scope, monkeypatch):
    # The outage is a deliberate failure: a database that is down cannot be produced
    # in-process, so the first check raises through a targeted mock. The missing table
    # itself is real (table_missing) and is back once the block exits.
    real_table_exists = BackgroundWorker.table_exists
    outages = [RuntimeError("db down")]

    def table_exists():
        if outages:
            raise outages.pop()
        return real_table_exists()

    monkeypatch.setattr(BackgroundWorker, "table_exists", table_exists)
    stop = asyncio.Event()
    with table_missing(db_scope, BackgroundWorker):
        waiter = asyncio.create_task(_worker(poll_interval=0.01).wait_for_schema(stop))
        await asyncio.sleep(0.05)
        assert not waiter.done()
    assert await asyncio.wait_for(waiter, timeout=5) is True


async def test_wait_for_schema_returns_false_when_stopped(db_scope):
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)

    with table_missing(db_scope, BackgroundWorker):
        assert await _worker(poll_interval=10).wait_for_schema(stop) is False


async def test_serve_stops_on_sigterm():
    ran = []

    class FakeWorker:
        async def wait_for_schema(self, stop):
            return True

        async def run(self, stop):
            ran.append("run")
            os.kill(os.getpid(), signal.SIGTERM)
            await stop.wait()

    await worker_module.serve(FakeWorker())

    assert ran == ["run"]


async def test_serve_skips_run_when_stopped_before_schema():
    class FakeWorker:
        async def wait_for_schema(self, stop):
            return False

        async def run(self, stop):
            raise AssertionError("must not run")

    await worker_module.serve(FakeWorker())


def test_main_runs_serve(monkeypatch):
    calls = []

    async def fake_serve():
        calls.append("serve")

    monkeypatch.setattr(worker_module, "serve", fake_serve)
    worker_module.main()

    assert calls == ["serve"]


async def test_worker_restores_discarded_periodic_occurrence(monkeypatch):
    monkeypatch.setattr(worker_module, "PERIODIC_RESYNC_SECONDS", 0.1)

    @tasks.task("test.hourly", every=timedelta(hours=1))
    async def hourly(payload):
        return None

    def pending():
        return BackgroundTask.get_or_none(
            (BackgroundTask.status == tasks.QUEUED)
            & (BackgroundTask.dedupe_key == tasks.periodic_key("test.hourly"))
        )

    discarded = []

    def discard_then_wait_for_resync():
        row = pending()
        if row is not None and not discarded:
            tasks.request_cancel(row.id)  # e.g. an admin cancelled it
            discarded.append(row.id)
            return False
        return bool(discarded) and row is not None and row.id not in discarded

    await _run_until(_worker(), discard_then_wait_for_resync)


async def test_worker_survives_periodic_resync_errors(monkeypatch):
    monkeypatch.setattr(worker_module, "PERIODIC_RESYNC_SECONDS", 0.05)
    calls = []
    real_ensure = tasks.ensure_periodic_tasks

    def flaky_ensure():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("db hiccup")
        real_ensure()

    monkeypatch.setattr(tasks, "ensure_periodic_tasks", flaky_ensure)

    await _run_until(_worker(), lambda: len(calls) >= 3)


async def test_worker_fails_tasks_with_corrupt_payload():
    @tasks.task("test.corrupt", max_attempts=1)
    async def corrupt(payload):
        raise AssertionError("handler must not run")

    task_id = tasks.enqueue("test.corrupt")
    BackgroundTask.update(payload="{not json").where(BackgroundTask.id == task_id).execute()

    await _run_until(_worker(), lambda: _status(task_id) == tasks.FAILED)

    assert BackgroundTask.get_by_id(task_id).last_error.startswith("JSONDecodeError")


async def test_worker_releases_tasks_cancelled_at_shutdown():
    started = asyncio.Event()

    @tasks.task("test.interrupted", max_attempts=1)
    async def interrupted(payload):
        started.set()
        await asyncio.sleep(10)

    task_id = tasks.enqueue("test.interrupted")
    stop = asyncio.Event()
    runner = asyncio.create_task(_worker(shutdown_grace=0.1).run(stop))
    await started.wait()
    stop.set()
    await runner

    # Handed back without spending the attempt, so the next worker runs it.
    row = BackgroundTask.get_by_id(task_id)
    assert (row.status, row.attempts, row.locked_by) == (tasks.QUEUED, 0, None)
    assert row.run_after <= tasks.utcnow()


async def test_worker_keeps_handler_timeout_errors_distinct():
    @tasks.task("test.socket-timeout", max_attempts=1, timeout=30)
    async def socket_timeout(payload):
        raise asyncio.TimeoutError("read timed out")

    task_id = tasks.enqueue("test.socket-timeout")
    await _run_until(_worker(), lambda: _status(task_id) == tasks.FAILED)

    assert BackgroundTask.get_by_id(task_id).last_error == "TimeoutError: read timed out"


def _events(task_id):
    return [
        event.type
        for event in BackgroundTaskEvent.select()
        .where(BackgroundTaskEvent.task == task_id)
        .order_by(BackgroundTaskEvent.id)
    ]


def _logs(task_id):
    return [
        (entry.attempt, entry.level, entry.message)
        for entry in BackgroundTaskLog.select()
        .where(BackgroundTaskLog.task == task_id)
        .order_by(BackgroundTaskLog.id)
    ]


def test_worker_tick_is_bounded_by_the_lease():
    assert _worker(lease_seconds=60, flush_interval=5).tick == 5
    assert _worker(lease_seconds=3, flush_interval=5).tick == 1


async def test_worker_stores_progress_stages_and_logs_while_a_task_runs():
    seen = {}
    release = asyncio.Event()
    log = worker_module.get_logger("DEMO")

    @tasks.task("test.progress")
    async def handler(payload, ctx):
        ctx.stage("counting")
        log.info("starting on 3 items")
        for done in range(1, 4):
            ctx.progress(done, 3)
            await asyncio.sleep(0.05)
        await release.wait()

    task_id = tasks.enqueue("test.progress")

    def progressed():
        row = BackgroundTask.get_by_id(task_id)
        if row.progress_done == 3 and not release.is_set():
            seen["row"] = row
            seen["logs"] = _logs(task_id)
            release.set()
        return row.status == tasks.SUCCEEDED

    await _run_until(_worker(flush_interval=0.05), progressed)

    row = seen["row"]
    assert (row.status, row.progress_total, row.progress_stage) == (tasks.RUNNING, 3, "counting")
    assert row.progress_base_done == 1
    assert (1, "INFO", "starting on 3 items") in seen["logs"]  # stored before the end
    assert _events(task_id) == ["created", "claimed", "stage", "succeeded"]


async def test_worker_logs_the_traceback_of_a_failed_attempt():
    @tasks.task("test.broken", max_attempts=1)
    async def broken(payload):
        raise KeyError("lfs_oid")

    task_id = tasks.enqueue("test.broken")
    await _run_until(_worker(), lambda: _status(task_id) == tasks.FAILED)

    ((attempt, level, message),) = _logs(task_id)
    assert (attempt, level) == (1, "ERROR")
    assert message.startswith("Traceback") and "KeyError: 'lfs_oid'" in message


async def test_worker_logs_the_traceback_of_timeouts_and_permanent_errors():
    @tasks.task("test.slow", max_attempts=1, timeout=0.05)
    async def slow(payload):
        await asyncio.sleep(10)

    @tasks.task("test.fatal")
    async def fatal(payload):
        raise tasks.PermanentTaskError("bad input")

    slow_id = tasks.enqueue("test.slow")
    fatal_id = tasks.enqueue("test.fatal")
    await _run_until(
        _worker(),
        lambda: _status(slow_id) == tasks.FAILED and _status(fatal_id) == tasks.FAILED,
    )

    assert "TimeoutError" in _logs(slow_id)[0][2]
    assert "PermanentTaskError: bad input" in _logs(fatal_id)[0][2]


async def test_worker_lets_a_task_stop_itself_when_cancellation_is_requested():
    started = asyncio.Event()

    @tasks.task("test.cooperative")
    async def cooperative(payload, ctx):
        started.set()
        while not ctx.cancel_requested:
            await asyncio.sleep(0.02)
        raise tasks.TaskCancelled()

    task_id = tasks.enqueue("test.cooperative")

    async def cancel_when_started():
        await started.wait()
        tasks.request_cancel(task_id)

    canceller = asyncio.create_task(cancel_when_started())
    await _run_until(_worker(flush_interval=0.05), lambda: _status(task_id) == tasks.CANCELLED)
    await canceller

    assert _events(task_id)[-2:] == ["cancel_requested", "cancelled"]
    messages = [message for _, _, message in _logs(task_id)]
    assert any("Cancellation requested" in message for message in messages)
    assert messages[-1] == "Stopped early: cancellation was requested"


async def test_worker_interrupts_a_task_that_ignores_cancellation():
    started = asyncio.Event()
    interrupted = asyncio.Event()

    @tasks.task("test.stubborn")
    async def stubborn(payload):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            interrupted.set()
            raise

    task_id = tasks.enqueue("test.stubborn")

    async def cancel_when_started():
        await started.wait()
        tasks.request_cancel(task_id)

    canceller = asyncio.create_task(cancel_when_started())
    await _run_until(
        _worker(flush_interval=0.05, shutdown_grace=0.2),
        lambda: _status(task_id) == tasks.CANCELLED,
    )
    await canceller

    assert interrupted.is_set()
    assert BackgroundTask.get_by_id(task_id).attempts == 1  # not retried
    assert _logs(task_id)[-1][2] == "Interrupted: cancellation was requested"


async def test_worker_keeps_the_logs_of_an_attempt_that_lost_its_lease():
    started = asyncio.Event()

    @tasks.task("test.stolen-logs")
    async def stolen(payload):
        worker_module.get_logger("DEMO").info("did some work")
        started.set()
        await asyncio.sleep(10)

    task_id = tasks.enqueue("test.stolen-logs")

    async def steal():
        await started.wait()
        BackgroundTask.update(locked_by="someone-else").where(
            BackgroundTask.id == task_id
        ).execute()

    stealer = asyncio.create_task(steal())
    await _run_until(
        _worker(lease_seconds=3, flush_interval=0.05),
        lambda: (1, "INFO", "did some work") in _logs(task_id)
        and BackgroundTask.get_by_id(task_id).locked_by == "someone-else",
    )
    await stealer
    assert _status(task_id) == tasks.RUNNING  # the new owner's to finish


async def test_worker_logs_why_it_handed_a_task_back_at_shutdown():
    started = asyncio.Event()

    @tasks.task("test.handed-back")
    async def handed_back(payload):
        started.set()
        await asyncio.sleep(10)

    task_id = tasks.enqueue("test.handed-back")
    stop = asyncio.Event()
    runner = asyncio.create_task(_worker(shutdown_grace=0.1).run(stop))
    await started.wait()
    stop.set()
    await runner

    assert _logs(task_id)[-1][1:] == ("WARNING", "Interrupted: the worker is shutting down")
    assert _events(task_id)[-1] == "released"


async def test_worker_keeps_logs_and_progress_when_a_flush_fails(monkeypatch):
    real_write = tasks.write_logs
    calls = {"write": 0}

    def flaky_write(row, entries):
        calls["write"] += 1
        if calls["write"] == 1:
            raise RuntimeError("db hiccup")
        real_write(row, entries)

    monkeypatch.setattr(tasks, "write_logs", flaky_write)

    @tasks.task("test.flaky-flush")
    async def handler(payload, ctx):
        worker_module.get_logger("DEMO").info("first line")
        ctx.progress(5, 10)
        await asyncio.sleep(0.3)
        ctx.progress(10, 10)

    task_id = tasks.enqueue("test.flaky-flush")
    await _run_until(_worker(flush_interval=0.05), lambda: _status(task_id) == tasks.SUCCEEDED)

    assert calls["write"] >= 2
    assert _logs(task_id) == [(1, "INFO", "first line")]
    row = BackgroundTask.get_by_id(task_id)
    assert (row.progress_done, row.progress_base_done) == (10, 5)


def _roster(worker_id="w-test"):
    return BackgroundWorker.get_or_none(BackgroundWorker.id == worker_id)


def test_worker_name_is_the_hostname_with_an_optional_prefix(monkeypatch):
    monkeypatch.setattr(worker_module.socket, "gethostname", lambda: "3f9a1c2e7b10")
    monkeypatch.setattr(worker_module.cfg.worker, "name", "")
    assert Worker().name == "3f9a1c2e7b10"
    monkeypatch.setattr(worker_module.cfg.worker, "name", "gpu-box")
    assert Worker().name == "gpu-box-3f9a1c2e7b10"
    assert Worker(name="").name == "3f9a1c2e7b10"


async def test_worker_registers_heartbeats_and_signs_off(monkeypatch):
    monkeypatch.setattr(tasks, "WORKER_HEARTBEAT_SECONDS", 0.1)
    release = asyncio.Event()
    started = asyncio.Event()
    seen = {}

    @tasks.task("test.roster")
    async def handler(payload):
        started.set()
        await release.wait()

    @tasks.task("test.roster-fail", max_attempts=1)
    async def broken(payload):
        raise RuntimeError("boom")

    tasks.enqueue("test.roster")
    failing = tasks.enqueue("test.roster-fail")
    worker = _worker(queues=["default"], name="demo")

    def observe():
        row = _roster()
        if row is None or _status(failing) != tasks.FAILED:
            return False
        if "busy" not in seen and started.is_set():
            seen["busy"] = row.last_heartbeat_at
            return False
        if "busy" in seen and row.last_heartbeat_at > seen["busy"] and not release.is_set():
            seen["row"] = row
            release.set()
        return release.is_set() and row.succeeded == 1

    await _run_until(worker, observe)

    live = seen["row"]
    assert (live.state, live.failed) == ("running", 1)
    assert (live.name, live.pid, live.concurrency) == (worker.name, worker.pid, 2)
    assert json.loads(live.queues) == ["default"]
    assert tasks.worker_status(live, tasks.utcnow()) == "online"
    final = _roster()
    assert final.state == "stopped" and final.stopped_at is not None
    assert (final.succeeded, final.failed) == (1, 1)
    assert final.started_at == live.started_at  # heartbeats never reset it


async def test_worker_reports_draining_while_it_drains(monkeypatch):
    monkeypatch.setattr(tasks, "WORKER_HEARTBEAT_SECONDS", 0.05)
    started = asyncio.Event()
    states = []
    real_record = tasks.record_worker

    def record(worker_id, **fields):
        states.append(fields["state"])
        real_record(worker_id, **fields)

    monkeypatch.setattr(tasks, "record_worker", record)

    @tasks.task("test.slow-drain")
    async def slow(payload):
        started.set()
        await asyncio.sleep(0.3)

    tasks.enqueue("test.slow-drain")
    stop = asyncio.Event()
    runner = asyncio.create_task(_worker(shutdown_grace=2).run(stop))
    await started.wait()
    stop.set()
    await runner

    assert states[0] == "running" and states[-1] == "stopped"
    # Heartbeats continued during the drain, so the worker never looked lost.
    assert states.count("draining") >= 3


async def test_worker_keeps_running_when_the_roster_write_fails(monkeypatch):
    calls = {"n": 0}

    def broken(worker_id, **fields):
        calls["n"] += 1
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(tasks, "record_worker", broken)

    @tasks.task("test.roster-down")
    async def handler(payload):
        return None

    task_id = tasks.enqueue("test.roster-down")
    await _run_until(_worker(), lambda: _status(task_id) == tasks.SUCCEEDED)

    assert calls["n"] >= 2  # registered (failed), then signed off (failed)


async def test_worker_counts_only_failures_it_managed_to_record(monkeypatch):
    def unrecorded(row, error, **kwargs):
        raise RuntimeError("db down while recording the failure")

    monkeypatch.setattr(tasks, "fail_task", unrecorded)
    attempted = asyncio.Event()

    @tasks.task("test.unrecorded", max_attempts=1)
    async def broken(payload):
        attempted.set()
        raise KeyError("x")

    tasks.enqueue("test.unrecorded")
    worker = _worker()
    await _run_until(worker, attempted.is_set)

    assert worker.failed == 0


async def test_failing_log_writes_never_cost_the_lease(monkeypatch):
    def broken(row, entries):
        raise RuntimeError("log table unavailable")

    monkeypatch.setattr(tasks, "write_logs", broken)

    @tasks.task("test.chatty")
    async def chatty(payload, ctx):
        for step in range(20):
            worker_module.get_logger("DEMO").info(f"step {step}")
            ctx.progress(step + 1, 20)
            await asyncio.sleep(0.1)

    # The handler outlives the 0.9 s lease; only heartbeats keep it owned.
    task_id = tasks.enqueue("test.chatty")
    await _run_until(
        _worker(lease_seconds=0.9, flush_interval=0.1),
        lambda: _status(task_id) == tasks.SUCCEEDED,
    )

    row = BackgroundTask.get_by_id(task_id)
    assert row.attempts == 1
    assert row.progress_done == 20  # progress still reached the row
