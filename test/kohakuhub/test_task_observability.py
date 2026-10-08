"""Tests for the task timeline, progress, cancellation, logs and on_failure hooks."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from kohakuhub import tasks
from kohakuhub.db import BackgroundTask, BackgroundTaskEvent, BackgroundTaskLog, BackgroundWorker
from kohakuhub.logger import get_logger
from kohakuhub.task_testing import RecordingContext

pytestmark = pytest.mark.usefixtures("db_scope")

WORKER = "worker-a"
LEASE = 60


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(tasks, "_registry", {})
    return tasks._registry


def _register(kind="test.noop", **options):
    @tasks.task(kind, **options)
    async def handler(payload):
        return None

    return handler


def _events(task_id):
    return [
        (event.type, event.attempt, event.worker, json.loads(event.detail or "{}"))
        for event in BackgroundTaskEvent.select()
        .where(BackgroundTaskEvent.task == task_id)
        .order_by(BackgroundTaskEvent.id)
    ]


def _types(task_id):
    return [event[0] for event in _events(task_id)]


def _claim(lease=LEASE, worker=WORKER):
    return tasks.claim_next(worker, lease_seconds=lease)


def _expire(task_id):
    BackgroundTask.update(locked_until=tasks.utcnow() - timedelta(seconds=1)).where(
        BackgroundTask.id == task_id
    ).execute()


def _row(**fields):
    defaults = dict(
        status=tasks.RUNNING,
        started_at=None,
        locked_until=None,
        stall_seconds=None,
        progress_done=None,
        progress_total=None,
        progress_at=None,
        progress_base_done=None,
        progress_base_at=None,
    )
    return SimpleNamespace(**(defaults | fields))


# ----- timeline -----


def test_timeline_records_every_attempt_and_its_error(monkeypatch):
    monkeypatch.setattr(tasks, "retry_delay", lambda attempts: 0)
    _register("test.flaky", max_attempts=2)
    task_id = tasks.enqueue("test.flaky")

    assert tasks.fail_task(_claim(), "RuntimeError: first") == tasks.QUEUED
    assert tasks.fail_task(_claim(), "RuntimeError: second") == tasks.FAILED

    events = _events(task_id)
    assert [(e[0], e[1]) for e in events] == [
        ("created", 0),
        ("claimed", 1),
        ("retry_scheduled", 1),
        ("claimed", 2),
        ("failed", 2),
    ]
    assert events[1][2] == WORKER
    assert events[2][3]["error"] == "RuntimeError: first"
    assert events[2][3]["run_after"].endswith("+00:00")
    assert events[4][3] == {"error": "RuntimeError: second"}


def test_timeline_records_success_release_and_a_reclaimed_lease():
    _register("test.flow")
    task_id = tasks.enqueue("test.flow")
    assert tasks.release_task(_claim()) is True
    _claim(worker="worker-dead")
    _expire(task_id)
    assert tasks.complete_task(_claim()) is True

    assert _events(task_id) == [
        ("created", 0, None, {}),
        ("claimed", 1, WORKER, {}),
        ("released", 1, WORKER, {}),
        ("claimed", 1, "worker-dead", {}),
        ("lease_expired", 1, "worker-dead", {}),
        ("claimed", 2, WORKER, {}),
        ("succeeded", 2, WORKER, {}),
    ]


def test_timeline_records_a_task_that_kept_killing_workers():
    _register("test.crashy", max_attempts=1)
    task_id = tasks.enqueue("test.crashy")
    _claim(worker="worker-dead")
    _expire(task_id)

    assert _claim() is None
    assert _events(task_id)[-1] == (
        "failed",
        1,
        "worker-dead",
        {"error": "lease expired on the final attempt"},
    )


def test_deduplicated_enqueue_and_lost_writes_record_no_events():
    _register("test.dedupe")
    task_id = tasks.enqueue("test.dedupe", dedupe_key="k")
    assert tasks.enqueue("test.dedupe", dedupe_key="k") is None
    claimed = _claim()
    stale = BackgroundTask.get_by_id(task_id)
    stale.locked_by = "worker-other"

    assert tasks.complete_task(stale) is False
    assert tasks.release_task(stale) is False
    assert tasks.record_stage(stale, "nope") is False
    assert _types(task_id) == ["created", "claimed"]
    assert tasks.complete_task(claimed) is True


def test_enqueue_copies_the_kind_stall_threshold():
    _register("test.default")
    _register("test.slow", stall_after=timedelta(hours=2))
    _register("test.quiet", stall_after=None)

    stall = {
        kind: BackgroundTask.get_by_id(tasks.enqueue(kind)).stall_seconds
        for kind in ("test.default", "test.slow", "test.quiet")
    }

    assert stall == {"test.default": 600, "test.slow": 7200, "test.quiet": None}


# ----- cancellation -----


def test_request_cancel_cancels_a_queued_task_at_once():
    _register("test.cancel", every=timedelta(hours=1))
    task_id = tasks.enqueue("test.cancel", dedupe_key=tasks.periodic_key("test.cancel"))

    assert tasks.request_cancel(task_id) == tasks.CANCELLED

    row = BackgroundTask.get_by_id(task_id)
    assert (row.status, row.dedupe_key) == (tasks.CANCELLED, None)
    assert row.finished_at is not None
    assert _events(task_id)[-1] == ("cancelled", 0, None, {"by": "admin"})
    # The released dedupe key lets the periodic occurrence come back.
    tasks.ensure_periodic_tasks()
    assert BackgroundTask.select().where(BackgroundTask.status == tasks.QUEUED).count() == 1


def test_request_cancel_flags_a_running_task_once():
    _register("test.cancel")
    task_id = tasks.enqueue("test.cancel")
    claimed = _claim()

    assert tasks.request_cancel(task_id) == tasks.RUNNING
    assert tasks.request_cancel(task_id) is None  # already requested

    row = BackgroundTask.get_by_id(task_id)
    assert (row.status, row.cancel_requested) == (tasks.RUNNING, True)
    assert _events(task_id)[-1] == ("cancel_requested", 1, WORKER, {})

    assert tasks.cancel_running_task(claimed) is True
    assert BackgroundTask.get_by_id(task_id).status == tasks.CANCELLED
    assert _events(task_id)[-1] == ("cancelled", 1, WORKER, {})
    assert tasks.cancel_running_task(claimed) is False


def test_request_cancel_ignores_finished_and_missing_tasks(monkeypatch):
    _register("test.cancel")
    task_id = tasks.enqueue("test.cancel")
    tasks.complete_task(_claim())

    assert tasks.request_cancel(task_id) is None
    assert tasks.request_cancel(999_999) is None

    # A queued task claimed between the read and the update is left alone.
    other = tasks.enqueue("test.cancel")
    real_get = BackgroundTask.get_or_none

    def get_then_claim(*args, **kwargs):
        row = real_get(*args, **kwargs)
        _claim()
        return row

    # Targeted mock: the race needs another writer between the read and the update, which
    # one connection cannot interleave. The read and the claim are real SQL on real rows.
    monkeypatch.setattr(BackgroundTask, "get_or_none", get_then_claim)
    assert tasks.request_cancel(other) is None


def test_request_cancel_loses_a_race_with_the_task_finishing(monkeypatch):
    _register("test.cancel")
    task_id = tasks.enqueue("test.cancel")
    claimed = _claim()
    real_get = BackgroundTask.get_or_none

    def get_then_finish(*args, **kwargs):
        row = real_get(*args, **kwargs)
        tasks.complete_task(claimed)
        return row

    # Targeted mock for the same reason: the finish lands between the read and the update.
    monkeypatch.setattr(BackgroundTask, "get_or_none", get_then_finish)

    assert tasks.request_cancel(task_id) is None
    assert "cancel_requested" not in _types(task_id)


def test_claim_cancels_a_flagged_task_whose_worker_died():
    _register("test.cancel")
    task_id = tasks.enqueue("test.cancel")
    _claim(worker="worker-dead")
    tasks.request_cancel(task_id)
    _expire(task_id)

    assert _claim() is None
    assert BackgroundTask.get_by_id(task_id).status == tasks.CANCELLED
    assert _types(task_id)[-2:] == ["cancel_requested", "cancelled"]


def test_retry_clears_a_cancellation_and_records_it():
    _register("test.cancel")
    task_id = tasks.enqueue("test.cancel")
    claimed = _claim()
    tasks.request_cancel(task_id)
    tasks.cancel_running_task(claimed)

    assert tasks.retry_failed_task(task_id) is True

    row = BackgroundTask.get_by_id(task_id)
    assert (row.status, row.attempts, row.cancel_requested) == (tasks.QUEUED, 0, False)
    assert _events(task_id)[-1] == ("retried", 1, None, {"by": "admin"})


# ----- stage, checkpoint, fence, logs -----


def test_record_stage_updates_the_row_and_the_timeline():
    _register("test.stage")
    task_id = tasks.enqueue("test.stage")
    claimed = _claim()

    assert tasks.record_stage(claimed, "copying " + "x" * 400) is True

    row = BackgroundTask.get_by_id(task_id)
    assert len(row.progress_stage) == tasks.MAX_STAGE_LENGTH
    assert row.progress_at is not None
    event = _events(task_id)[-1]
    assert event[:3] == ("stage", 1, WORKER)
    assert event[3]["stage"] == row.progress_stage


def test_context_checkpoint_persists_and_seeds_the_next_attempt():
    _register("test.resume")
    task_id = tasks.enqueue("test.resume")
    ctx = tasks.TaskContext(_claim(), log_limit_bytes=1024)
    assert ctx.checkpoint_state is None

    ctx.checkpoint({"cursor": "b/2"})
    assert ctx.checkpoint_state == {"cursor": "b/2"}

    tasks.release_task(BackgroundTask.get_by_id(task_id))
    resumed = tasks.TaskContext(_claim(), log_limit_bytes=1024)
    assert resumed.checkpoint_state == {"cursor": "b/2"}
    assert resumed.scratch_prefix == f"tmp/tasks/{task_id}/"


def test_context_checkpoint_and_fence_refuse_a_lost_lease():
    _register("test.fence")
    task_id = tasks.enqueue("test.fence")
    ctx = tasks.TaskContext(_claim(), log_limit_bytes=1024)
    ctx.assert_owned()  # still ours
    ctx.stage("publishing")

    BackgroundTask.update(locked_by="worker-b").where(BackgroundTask.id == task_id).execute()

    with pytest.raises(tasks.LeaseLost):
        ctx.checkpoint({"cursor": 1})
    with pytest.raises(tasks.LeaseLost):
        ctx.assert_owned()
    assert BackgroundTask.get_by_id(task_id).checkpoint is None


def test_context_progress_keeps_the_attempt_base_and_newer_reports():
    _register("test.progress")
    ctx = tasks.TaskContext(_claim_new("test.progress"), log_limit_bytes=1024)

    ctx.progress(10, 100)
    first = ctx.pending_progress()
    assert first["progress_base_done"] == 10
    ctx.progress(20, 100)
    stored = ctx.pending_progress()
    ctx.progress(30, 100)  # reported while the heartbeat was storing ``stored``
    ctx.clear_progress(stored)

    pending = ctx.pending_progress()
    assert pending["progress_done"] == 30
    assert "progress_base_done" not in pending  # stored; the base is set once
    assert "progress_total" not in pending  # unchanged since it was stored


def _claim_new(kind):
    tasks.enqueue(kind)
    return _claim()


def test_context_log_caps_an_attempt_and_keeps_order_on_requeue():
    ctx = tasks.TaskContext(
        SimpleNamespace(id=1, kind="k", attempts=1, checkpoint=None), log_limit_bytes=10
    )
    ctx.log("INFO", "12345")
    ctx.log("INFO", "67890")
    ctx.log("INFO", "over the cap")
    ctx.log("INFO", "dropped")

    logs = ctx.take_logs()
    assert [entry["message"] for entry in logs[:2]] == ["12345", "67890"]
    assert logs[2]["level"] == "WARNING"
    assert "truncated" in logs[2]["message"]
    assert len(logs) == 3

    ctx._log_truncated = False
    ctx._log_limit = 10_000
    ctx.log("INFO", "newer")
    ctx.requeue_logs(logs[:1])
    assert [entry["message"] for entry in ctx.take_logs()] == ["12345", "newer"]
    assert ctx.take_logs() == []


def test_context_log_exception_keeps_the_traceback():
    ctx = tasks.TaskContext(
        SimpleNamespace(id=1, kind="k", attempts=1, checkpoint=None), log_limit_bytes=10_000
    )
    try:
        raise ValueError("bad row")
    except ValueError as error:
        ctx.log_exception(error)

    (entry,) = ctx.take_logs()
    assert entry["level"] == "ERROR"
    assert entry["message"].startswith("Traceback")
    assert "ValueError: bad row" in entry["message"]


def test_write_logs_tags_the_attempt_and_skips_empty_batches():
    _register("test.logs")
    task_id = tasks.enqueue("test.logs")
    claimed = _claim()
    tasks.write_logs(claimed, [])
    tasks.write_logs(claimed, [{"at": tasks.utcnow(), "level": "INFO", "message": "hello"}])

    (entry,) = BackgroundTaskLog.select().where(BackgroundTaskLog.task == task_id)
    assert (entry.attempt, entry.level, entry.message) == (1, "INFO", "hello")


def test_log_capture_copies_records_only_inside_a_task_context():
    tasks.install_log_capture()
    sink = tasks._capture_sink_id
    tasks.install_log_capture()  # idempotent
    assert tasks._capture_sink_id == sink

    ctx = tasks.TaskContext(
        SimpleNamespace(id=1, kind="k", attempts=1, checkpoint=None), log_limit_bytes=10_000
    )
    log = get_logger("TEST_CAPTURE")
    log.info("outside any task")
    token = tasks.current_context.set(ctx)
    try:
        log.info("inside the task")
        log.debug("below the capture level")
    finally:
        tasks.current_context.reset(token)
    log.info("outside again")

    messages = [(entry["level"], entry["message"]) for entry in ctx.take_logs()]
    assert messages == [("INFO", "inside the task")]
    assert ctx.take_logs() == []


def test_capture_sink_tolerates_a_record_without_a_context():
    tasks._capture(SimpleNamespace(record={}))  # filtered out normally; must not raise


# ----- ETA and stall -----


def test_estimate_eta_uses_the_attempt_rate_up_to_now():
    now = tasks.utcnow()
    base = dict(progress_base_done=100, progress_base_at=now - timedelta(seconds=10))

    assert tasks.estimate_eta_seconds(
        _row(progress_done=150, progress_total=300, **base), now
    ) == pytest.approx(30)
    assert tasks.estimate_eta_seconds(_row(progress_done=300, progress_total=300, **base), now) == 0
    # No work yet in this attempt, a future base, missing data, or not running.
    assert (
        tasks.estimate_eta_seconds(_row(progress_done=100, progress_total=300, **base), now) is None
    )
    assert (
        tasks.estimate_eta_seconds(
            _row(
                progress_done=150,
                progress_total=300,
                progress_base_done=100,
                progress_base_at=now + timedelta(seconds=1),
            ),
            now,
        )
        is None
    )
    assert tasks.estimate_eta_seconds(_row(progress_done=150, **base), now) is None
    assert (
        tasks.estimate_eta_seconds(
            _row(status=tasks.SUCCEEDED, progress_done=150, progress_total=300, **base), now
        )
        is None
    )


def test_is_stalled_needs_a_live_lease_and_no_recent_progress():
    now = tasks.utcnow()
    live = dict(
        stall_seconds=60,
        started_at=now - timedelta(minutes=10),
        locked_until=now + timedelta(seconds=30),
    )

    assert tasks.is_stalled(_row(**live), now) is True
    assert tasks.is_stalled(_row(progress_at=now - timedelta(seconds=90), **live), now) is True
    assert tasks.is_stalled(_row(progress_at=now - timedelta(seconds=5), **live), now) is False
    assert tasks.is_stalled(_row(**(live | {"stall_seconds": None})), now) is False
    assert tasks.is_stalled(_row(**(live | {"started_at": None})), now) is False
    assert tasks.is_stalled(_row(**(live | {"locked_until": None})), now) is False
    assert (
        tasks.is_stalled(_row(**(live | {"locked_until": now - timedelta(seconds=1)})), now)
        is False
    )
    assert tasks.is_stalled(_row(status=tasks.QUEUED, **live), now) is False


def test_claim_resets_the_progress_base_but_keeps_the_last_report():
    _register("test.base")
    task_id = tasks.enqueue("test.base")
    first = _claim()
    tasks.heartbeat(
        first,
        lease_seconds=LEASE,
        progress={
            "progress_done": 40,
            "progress_total": 100,
            "progress_base_done": 0,
            "progress_base_at": tasks.utcnow(),
        },
    )
    tasks.release_task(BackgroundTask.get_by_id(task_id))

    _claim()
    row = BackgroundTask.get_by_id(task_id)
    assert (row.progress_done, row.progress_total) == (40, 100)
    assert (row.progress_base_done, row.progress_base_at) == (None, None)


# ----- context detection and on_failure -----


def test_task_decorator_detects_handlers_that_take_a_context():
    @tasks.task("test.plain")
    async def plain(payload):
        return None

    @tasks.task("test.ctx")
    async def with_context(payload, ctx):
        return None

    assert tasks.get_spec("test.plain").takes_context is False
    assert tasks.get_spec("test.ctx").takes_context is True


def test_on_failure_registers_a_follow_up_kind_and_validates_the_hook():
    async def hook(payload, failure):
        return None

    _register("test.hooked", on_failure=hook, max_attempts=2, queue="bulk")
    follow_up = tasks.get_spec("test.hooked" + tasks.ON_FAILURE_SUFFIX)
    assert (follow_up.queue, follow_up.max_attempts, follow_up.on_failure) == ("bulk", 2, None)

    with pytest.raises(TypeError):
        _register("test.sync-hook", on_failure=lambda payload, failure: None)
    with pytest.raises(ValueError):
        _register("test.hooked", on_failure=hook)
    _register("test.taken" + tasks.ON_FAILURE_SUFFIX)
    with pytest.raises(ValueError):
        _register("test.taken", on_failure=hook)


async def test_on_failure_runs_once_for_each_terminal_outcome():
    calls = []

    async def hook(payload, failure):
        calls.append((payload, failure))

    _register("test.hooked", on_failure=hook, max_attempts=1)
    failed = tasks.enqueue("test.hooked", {"n": 1})
    tasks.fail_task(_claim(), "RuntimeError: broke")
    cancelled = tasks.enqueue("test.hooked", {"n": 2})
    tasks.request_cancel(cancelled)
    # Priority keeps the claims below away from the queued follow-ups.
    running = tasks.enqueue("test.hooked", {"n": 3}, priority=10)
    claimed = _claim()
    tasks.request_cancel(running)
    tasks.cancel_running_task(claimed)
    succeeded = tasks.enqueue("test.hooked", {"n": 4}, priority=10)
    tasks.complete_task(_claim())

    follow_ups = list(
        BackgroundTask.select()
        .where(BackgroundTask.kind == "test.hooked" + tasks.ON_FAILURE_SUFFIX)
        .order_by(BackgroundTask.id)
    )
    assert [row.dedupe_key for row in follow_ups] == [
        f"on_failure:{failed}",
        f"on_failure:{cancelled}",
        f"on_failure:{running}",
    ]
    for row in follow_ups:
        await tasks.get_spec(row.kind).handler(json.loads(row.payload))

    assert [(payload["n"], failure.outcome, failure.error) for payload, failure in calls] == [
        (1, tasks.FAILED, "RuntimeError: broke"),
        (2, tasks.CANCELLED, None),
        (3, tasks.CANCELLED, None),
    ]
    assert calls[0][1] == tasks.TaskFailure(
        task_id=failed,
        kind="test.hooked",
        outcome=tasks.FAILED,
        error="RuntimeError: broke",
        scratch_prefix=f"tmp/tasks/{failed}/",
    )
    assert succeeded not in {failure.task_id for _, failure in calls}


def test_on_failure_is_not_scheduled_for_retries_or_unhooked_kinds():
    async def hook(payload, failure):
        return None

    _register("test.hooked", on_failure=hook, max_attempts=2)
    _register("test.plain", max_attempts=1)
    tasks.enqueue("test.hooked")
    tasks.fail_task(_claim(), "retry me")
    tasks.enqueue("test.plain", priority=-1)
    BackgroundTask.update(run_after=tasks.utcnow() + timedelta(hours=1)).where(
        BackgroundTask.kind == "test.hooked"
    ).execute()
    tasks.fail_task(_claim(), "no hook")

    assert not BackgroundTask.select().where(BackgroundTask.kind.endswith(".on_failure")).exists()


def test_on_failure_passes_a_corrupt_payload_through_as_text():
    async def hook(payload, failure):
        return None

    _register("test.hooked", on_failure=hook, max_attempts=1)
    task_id = tasks.enqueue("test.hooked")
    BackgroundTask.update(payload="{not json").where(BackgroundTask.id == task_id).execute()
    tasks.fail_task(_claim(), "JSONDecodeError: bad")

    follow_up = BackgroundTask.get(BackgroundTask.kind == "test.hooked.on_failure")
    assert json.loads(follow_up.payload)["payload"] == "{not json"


# ----- worker roster -----


def _record(worker_id="w-1", **fields):
    defaults = dict(
        name="w-1-host",
        hostname="host",
        pid=7,
        queues=[],
        concurrency=4,
        state="running",
        succeeded=0,
        failed=0,
    )
    tasks.record_worker(worker_id, **(defaults | fields))
    return BackgroundWorker.get_by_id(worker_id)


def test_record_worker_registers_then_refreshes_without_resetting_the_start():
    BackgroundWorker.delete().execute()
    first = _record(queues=["bulk"])
    assert (first.state, first.stopped_at, json.loads(first.queues)) == ("running", None, ["bulk"])

    later = _record(state="draining", succeeded=5, failed=1)
    assert later.started_at == first.started_at
    assert later.last_heartbeat_at >= first.last_heartbeat_at
    assert (later.state, later.succeeded, later.failed) == ("draining", 5, 1)

    stopped = _record(state="stopped")
    assert stopped.stopped_at is not None
    assert BackgroundWorker.select().count() == 1
    BackgroundWorker.delete().execute()


def test_worker_status_derives_liveness_from_the_last_heartbeat():
    now = tasks.utcnow()

    def status(state, seconds_ago):
        row = SimpleNamespace(state=state, last_heartbeat_at=now - timedelta(seconds=seconds_ago))
        return tasks.worker_status(row, now)

    assert status("running", 5) == "online"
    assert status("draining", 5) == "draining"
    assert status("running", 31) == "lost"
    assert status("draining", 31) == "lost"
    assert status("stopped", 5) == "stopped"
    assert status("stopped", 3600) == "stopped"


async def test_cleanup_keeps_worker_history():
    BackgroundWorker.delete().execute()
    old = _record(state="stopped")
    BackgroundWorker.update(last_heartbeat_at=tasks.utcnow() - timedelta(days=90)).execute()

    await tasks.cleanup_finished_tasks({}, RecordingContext())
    assert BackgroundWorker.get_or_none(BackgroundWorker.id == old.id) is not None
    BackgroundWorker.delete().execute()


def test_write_logs_inserts_in_batches_and_keeps_nul_bytes_readable(monkeypatch):
    monkeypatch.setattr(tasks, "LOG_INSERT_BATCH", 2)
    _register("test.logs")
    task_id = tasks.enqueue("test.logs")
    claimed = _claim()
    ctx = tasks.TaskContext(claimed, log_limit_bytes=10_000)
    for i in range(4):
        ctx.log("INFO", f"line {i}")
    ctx.log("INFO", "binary\x00payload")

    tasks.write_logs(claimed, ctx.take_logs())

    messages = [
        row.message
        for row in BackgroundTaskLog.select()
        .where(BackgroundTaskLog.task == task_id)
        .order_by(BackgroundTaskLog.id)
    ]
    assert messages == ["line 0", "line 1", "line 2", "line 3", "binary\\x00payload"]
