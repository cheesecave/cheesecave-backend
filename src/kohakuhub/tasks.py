"""Durable background tasks stored in the application database.

A task is one ``BackgroundTask`` row. ``enqueue`` inserts it through the shared
Peewee ``db``, so calling it inside ``db.atomic()`` commits or rolls back
together with the caller's business writes. ``python -m kohakuhub.worker``
claims and runs tasks.

Delivery is at-least-once: a worker holds a lease on the task it runs and
renews it while the handler is alive. When a worker dies, its lease expires
and another worker claims the task again, so every handler must be idempotent.

Handlers share the event loop thread's database connection with the worker's
bookkeeping (as hub-api request handlers do), so never ``await`` inside
``db.atomic()``.

Every state change also writes a ``BackgroundTaskEvent`` in the same
transaction, so the timeline never disagrees with the row. A handler that
takes a second parameter receives a ``TaskContext`` for progress, stages,
checkpoints, cancellation and the publish fence; see
docs/development/background-tasks.md for the handler contract.
"""

import contextvars
import inspect
import json
import random
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from loguru import logger as loguru_logger
from peewee import PostgresqlDatabase

from kohakuhub.config import cfg
from kohakuhub.db import (
    BackgroundTask,
    BackgroundTaskEvent,
    BackgroundTaskLog,
    BackgroundWorker,
    utcnow,
)
from kohakuhub.logger import get_logger

logger = get_logger("TASKS")

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED)
FINISHED = (SUCCEEDED, FAILED, CANCELLED)

# heartbeat() outcomes
LEASE_OWNED = "owned"
LEASE_LOST = "lost"
LEASE_CANCEL = "cancel"

MAX_ERROR_LENGTH = 4000
MAX_STAGE_LENGTH = 255
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 3600
DEFAULT_STALL_AFTER = timedelta(minutes=10)
CLEANUP_KIND = "tasks.cleanup"
CLEANUP_BATCH = 1000
ON_FAILURE_SUFFIX = ".on_failure"
# Worker roster: a live worker refreshes its row this often; one silent for
# LOST_AFTER is reported lost. Rows are kept; the admin panel hides workers
# with no heartbeat for INACTIVE_AFTER unless asked.
WORKER_HEARTBEAT_SECONDS = 10.0
WORKER_LOST_AFTER = timedelta(seconds=30)
WORKER_INACTIVE_AFTER = timedelta(hours=24)
SCRATCH_PREFIX = "tmp/tasks/{task_id}/"
LOG_INSERT_BATCH = 500  # rows per INSERT, well under PostgreSQL's bind-parameter limit

TaskHandler = Callable[..., Awaitable[None]]


class PermanentTaskError(Exception):
    """Raised by a handler to fail its task without further retries."""


class TaskCancelled(Exception):
    """Raised by a handler that stopped early because cancellation was requested."""


class LeaseLost(Exception):
    """This worker no longer owns the task; it must not publish anything."""


@dataclass(frozen=True)
class TaskFailure:
    """What an ``on_failure`` hook learns about the task that ended."""

    task_id: int
    kind: str
    outcome: str  # failed or cancelled
    error: str | None
    scratch_prefix: str


FailureHandler = Callable[[dict[str, Any], TaskFailure], Awaitable[None]]


@dataclass(frozen=True)
class TaskSpec:
    kind: str
    handler: TaskHandler
    queue: str
    max_attempts: int
    timeout: float
    every: timedelta | None
    stall_after: timedelta | None = DEFAULT_STALL_AFTER
    on_failure: FailureHandler | None = None
    takes_context: bool = False


_registry: dict[str, TaskSpec] = {}


def _require_async(function, what: str) -> None:
    if not inspect.iscoroutinefunction(function):
        raise TypeError(f"{what} must be an async function")


def task(
    kind: str,
    *,
    queue: str = "default",
    max_attempts: int = 5,
    timeout: float = 3600.0,
    every: timedelta | None = None,
    stall_after: timedelta | None = DEFAULT_STALL_AFTER,
    on_failure: FailureHandler | None = None,
) -> Callable[[TaskHandler], TaskHandler]:
    """Register an async handler for ``kind``.

    Only registered kinds can be enqueued or claimed; rows never name code.
    ``every`` makes the kind periodic (see ``ensure_periodic_tasks``).
    A handler declared as ``handler(payload, ctx)`` receives a ``TaskContext``.
    ``stall_after`` flags a running task whose progress has not moved for
    that long (``None`` disables it). ``on_failure(payload, failure)`` runs
    once, as its own ``<kind>.on_failure`` task, when a task ends failed or
    cancelled; it must be re-runnable like any handler.
    """

    def decorator(handler: TaskHandler) -> TaskHandler:
        _require_async(handler, f"Task handler for {kind!r}")
        if on_failure is not None:
            _require_async(on_failure, f"on_failure hook for {kind!r}")
        follow_up = kind + ON_FAILURE_SUFFIX
        if kind in _registry or (on_failure is not None and follow_up in _registry):
            raise ValueError(f"Task kind already registered: {kind}")
        takes_context = len(inspect.signature(handler).parameters) >= 2
        _registry[kind] = TaskSpec(
            kind,
            handler,
            queue,
            max_attempts,
            timeout,
            every,
            stall_after,
            on_failure,
            takes_context,
        )
        if on_failure is not None:

            async def run_on_failure(payload: dict[str, Any]) -> None:
                await on_failure(payload["payload"], TaskFailure(**payload["failure"]))

            _registry[follow_up] = TaskSpec(
                follow_up, run_on_failure, queue, max_attempts, timeout, None, stall_after
            )
        return handler

    return decorator


def get_spec(kind: str) -> TaskSpec | None:
    return _registry.get(kind)


def periodic_key(kind: str) -> str:
    return f"periodic:{kind}"


def _database():
    return BackgroundTask._meta.database


def _record_event(
    task_id: int, type_: str, attempt: int, worker: str | None = None, **detail: Any
) -> None:
    BackgroundTaskEvent.insert(
        task=task_id,
        at=utcnow(),
        type=type_,
        attempt=attempt,
        worker=worker,
        detail=json.dumps(detail) if detail else None,
    ).execute()


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    priority: int = 0,
    queue: str | None = None,
) -> int | None:
    """Insert a task and return its id.

    Returns ``None`` when a pending task with the same ``dedupe_key`` already
    exists; that task will do the work. Keep payloads to IDs and small
    parameters: never credentials, presigned URLs, or file content.
    """
    spec = _registry.get(kind)
    if spec is None:
        raise ValueError(f"Unknown task kind: {kind}")
    if run_after is not None and run_after.tzinfo is not None:
        # Columns hold naive UTC; see kohakuhub.db.utcnow.
        run_after = run_after.astimezone(timezone.utc).replace(tzinfo=None)
    now = utcnow()
    with _database().atomic():
        rows = list(
            BackgroundTask.insert(
                kind=kind,
                queue=queue or spec.queue,
                payload=json.dumps(payload or {}),
                priority=priority,
                dedupe_key=dedupe_key,
                run_after=run_after or now,
                max_attempts=spec.max_attempts,
                stall_seconds=(int(spec.stall_after.total_seconds()) if spec.stall_after else None),
                created_at=now,
            )
            .on_conflict_ignore()
            .returning(BackgroundTask.id)
            .tuples()
            .execute()
        )
        if not rows:
            return None
        _record_event(rows[0][0], "created", 0)
        return rows[0][0]


def ensure_periodic_tasks() -> None:
    """Make sure every periodic kind has its pending occurrence."""
    for spec in _registry.values():
        if spec.every is not None:
            enqueue(spec.kind, dedupe_key=periodic_key(spec.kind))


def _select_candidate(now: datetime, queues: list[str] | None) -> BackgroundTask | None:
    T = BackgroundTask
    eligible = ((T.status == QUEUED) & (T.run_after <= now)) | (
        (T.status == RUNNING) & (T.locked_until < now)
    )
    condition = eligible & T.kind.in_(list(_registry))
    if queues:
        condition &= T.queue.in_(queues)
    query = T.select().where(condition).order_by(T.priority.desc(), T.run_after, T.id)
    if isinstance(T._meta.database, PostgresqlDatabase):
        # SQLite serializes writers, so it needs no row lock.
        query = query.for_update(skip_locked=True)
    return query.first()


def claim_next(
    worker_id: str, *, lease_seconds: int, queues: list[str] | None = None
) -> BackgroundTask | None:
    """Claim the next eligible task for ``worker_id``, or return ``None``.

    Eligible tasks are queued tasks that are due, and running tasks whose
    lease expired. Claiming releases the dedupe key and, for periodic kinds,
    schedules the next occurrence in the same transaction.
    """
    if not _registry:
        return None
    T = BackgroundTask
    now = utcnow()
    with T._meta.database.atomic():
        while (candidate := _select_candidate(now, queues)) is not None:
            if candidate.status == RUNNING and candidate.cancel_requested:
                # Its worker died before it could honour the request.
                _terminate(candidate, CANCELLED, now, None)
                continue
            if candidate.status == RUNNING and candidate.attempts >= candidate.max_attempts:
                # The worker died on its last attempt; don't loop on a task
                # that keeps killing workers.
                _terminate(candidate, FAILED, now, "lease expired on the final attempt")
                continue
            # Compare-and-set on the observed state: under SQLite a concurrent
            # worker may have claimed the same row since we read it.
            claimed = (
                T.update(
                    status=RUNNING,
                    attempts=T.attempts + 1,
                    locked_by=worker_id,
                    locked_until=now + timedelta(seconds=lease_seconds),
                    started_at=now,
                    dedupe_key=None,
                    progress_base_done=None,
                    progress_base_at=None,
                )
                .where(
                    (T.id == candidate.id)
                    & (T.status == candidate.status)
                    & (T.attempts == candidate.attempts)
                )
                .execute()
            )
            if not claimed:
                return None
            if candidate.status == RUNNING:
                _record_event(
                    candidate.id, "lease_expired", candidate.attempts, candidate.locked_by
                )
            _record_event(candidate.id, "claimed", candidate.attempts + 1, worker_id)
            spec = _registry[candidate.kind]
            if spec.every is not None:
                enqueue(spec.kind, dedupe_key=periodic_key(spec.kind), run_after=now + spec.every)
            return T.get_by_id(candidate.id)
    return None


def _owned(task_row: BackgroundTask):
    T = BackgroundTask
    return (
        (T.id == task_row.id)
        & (T.status == RUNNING)
        & (T.locked_by == task_row.locked_by)
        & (T.attempts == task_row.attempts)
    )


def _finish(task_row: BackgroundTask, status: str, now: datetime, error: str | None) -> int:
    return (
        BackgroundTask.update(
            status=status,
            finished_at=now,
            locked_by=None,
            locked_until=None,
            last_error=error,
        )
        .where(_owned(task_row))
        .execute()
    )


def _decoded_payload(task_row: BackgroundTask):
    try:
        return json.loads(task_row.payload)
    except ValueError:
        return task_row.payload


def _schedule_on_failure(task_row: BackgroundTask, outcome: str, error: str | None) -> None:
    spec = _registry.get(task_row.kind)
    if spec is None or spec.on_failure is None:
        return
    failure = TaskFailure(
        task_id=task_row.id,
        kind=task_row.kind,
        outcome=outcome,
        error=error,
        scratch_prefix=SCRATCH_PREFIX.format(task_id=task_row.id),
    )
    enqueue(
        task_row.kind + ON_FAILURE_SUFFIX,
        {"payload": _decoded_payload(task_row), "failure": asdict(failure)},
        dedupe_key=f"on_failure:{task_row.id}",
    )


def _terminate(task_row: BackgroundTask, status: str, now: datetime, error: str | None) -> bool:
    """End an owned task as failed or cancelled; schedule its ``on_failure``."""
    with _database().atomic():
        if not _finish(task_row, status, now, error):
            return False
        detail = {"error": error} if error else {}
        _record_event(task_row.id, status, task_row.attempts, task_row.locked_by, **detail)
        _schedule_on_failure(task_row, status, error)
        return True


def heartbeat(
    task_row: BackgroundTask, *, lease_seconds: float, progress: dict[str, Any] | None = None
) -> str:
    """Extend the lease and store pending progress in one fenced update.

    Returns ``LEASE_OWNED``, ``LEASE_CANCEL`` (still owned, but an admin asked
    to cancel) or ``LEASE_LOST`` (another worker owns the task now).
    """
    T = BackgroundTask
    until = utcnow() + timedelta(seconds=lease_seconds)
    if not T.update(locked_until=until, **(progress or {})).where(_owned(task_row)).execute():
        return LEASE_LOST
    cancel = T.select(T.cancel_requested).where(T.id == task_row.id).scalar()
    return LEASE_CANCEL if cancel else LEASE_OWNED


def record_stage(task_row: BackgroundTask, stage: str) -> bool:
    """Store the current stage and add it to the timeline, if still owned."""
    stage = stage[:MAX_STAGE_LENGTH]
    with _database().atomic():
        updated = (
            BackgroundTask.update(progress_stage=stage, progress_at=utcnow())
            .where(_owned(task_row))
            .execute()
        )
        if updated:
            _record_event(task_row.id, "stage", task_row.attempts, task_row.locked_by, stage=stage)
        return bool(updated)


def save_checkpoint(task_row: BackgroundTask, state: Any) -> bool:
    return bool(
        BackgroundTask.update(checkpoint=json.dumps(state)).where(_owned(task_row)).execute()
    )


def assert_owned(task_row: BackgroundTask) -> None:
    """Raise ``LeaseLost`` unless this worker still owns the task.

    Call it inside the transaction that publishes a handler's result: on
    PostgreSQL the row stays locked until that transaction ends, so no other
    worker can take the task over between the check and the commit.
    """
    query = BackgroundTask.select(BackgroundTask.id).where(_owned(task_row))
    if isinstance(_database(), PostgresqlDatabase):
        query = query.for_update()
    if query.first() is None:
        raise LeaseLost(f"Task {task_row.id} attempt {task_row.attempts} lost its lease")


def write_logs(task_row: BackgroundTask, entries: list[dict[str, Any]]) -> None:
    """Store captured log records. Not fenced: a worker that lost its lease
    still documents what it did, under its own attempt number."""
    rows = [{"task": task_row.id, "attempt": task_row.attempts, **entry} for entry in entries]
    for start in range(0, len(rows), LOG_INSERT_BATCH):
        BackgroundTaskLog.insert_many(rows[start : start + LOG_INSERT_BATCH]).execute()


def complete_task(task_row: BackgroundTask) -> bool:
    with _database().atomic():
        if not _finish(task_row, SUCCEEDED, utcnow(), None):
            return False
        _record_event(task_row.id, "succeeded", task_row.attempts, task_row.locked_by)
        return True


def release_task(task_row: BackgroundTask) -> bool:
    """Hand an interrupted task back to the queue without spending the attempt."""
    T = BackgroundTask
    with _database().atomic():
        released = (
            T.update(
                status=QUEUED,
                attempts=T.attempts - 1,
                run_after=utcnow(),
                locked_by=None,
                locked_until=None,
            )
            .where(_owned(task_row))
            .execute()
        )
        if released:
            _record_event(task_row.id, "released", task_row.attempts, task_row.locked_by)
        return bool(released)


def retry_delay(attempts: int) -> float:
    """Exponential backoff with up to 25% jitter."""
    delay = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
    return delay * random.uniform(1.0, 1.25)


def fail_task(task_row: BackgroundTask, error: str, *, permanent: bool = False) -> str | None:
    """Record a failed attempt; return the new status, or ``None`` if not owned."""
    error = error[:MAX_ERROR_LENGTH]
    now = utcnow()
    if permanent or task_row.attempts >= task_row.max_attempts:
        return FAILED if _terminate(task_row, FAILED, now, error) else None
    run_after = now + timedelta(seconds=retry_delay(task_row.attempts))
    with _database().atomic():
        requeued = (
            BackgroundTask.update(
                status=QUEUED,
                run_after=run_after,
                locked_by=None,
                locked_until=None,
                last_error=error,
            )
            .where(_owned(task_row))
            .execute()
        )
        if not requeued:
            return None
        _record_event(
            task_row.id,
            "retry_scheduled",
            task_row.attempts,
            task_row.locked_by,
            error=error,
            run_after=run_after.replace(tzinfo=timezone.utc).isoformat(),
        )
        return QUEUED


def cancel_running_task(task_row: BackgroundTask) -> bool:
    """Record that an owned task stopped because cancellation was requested."""
    return _terminate(task_row, CANCELLED, utcnow(), None)


def request_cancel(task_id: int) -> str | None:
    """Cancel a task from the admin panel.

    A queued task is cancelled at once and returns ``CANCELLED``. A running
    task is flagged; its worker stops it and records the outcome, and this
    returns ``RUNNING``. Returns ``None`` when there is nothing to cancel.
    """
    T = BackgroundTask
    now = utcnow()
    with _database().atomic():
        row = T.get_or_none(T.id == task_id)
        if row is None:
            return None
        if row.status == QUEUED:
            # Releasing the dedupe key lets new work (or a periodic kind's
            # next occurrence) be enqueued again.
            cancelled = (
                T.update(status=CANCELLED, finished_at=now, dedupe_key=None)
                .where((T.id == task_id) & (T.status == QUEUED))
                .execute()
            )
            if cancelled:
                _record_event(task_id, "cancelled", row.attempts, by="admin")
                _schedule_on_failure(row, CANCELLED, None)
                return CANCELLED
        elif row.status == RUNNING and not row.cancel_requested:
            flagged = (
                T.update(cancel_requested=True)
                .where((T.id == task_id) & (T.status == RUNNING))
                .execute()
            )
            if flagged:
                _record_event(task_id, "cancel_requested", row.attempts, row.locked_by)
                return RUNNING
    return None


def retry_failed_task(task_id: int) -> bool:
    """Requeue a failed or cancelled task with a fresh attempt budget."""
    T = BackgroundTask
    with _database().atomic():
        row = T.get_or_none((T.id == task_id) & T.status.in_([FAILED, CANCELLED]))
        if row is None:
            return False
        T.update(
            status=QUEUED,
            attempts=0,
            run_after=utcnow(),
            finished_at=None,
            cancel_requested=False,
        ).where(T.id == task_id).execute()
        _record_event(task_id, "retried", row.attempts, by="admin")
        return True


def discard_task(task_id: int) -> bool:
    """Delete a failed or cancelled task with its timeline and logs.

    Queued and running tasks are cancelled instead (``request_cancel``), so
    their ``on_failure`` hook gets to clean up.
    """
    T = BackgroundTask
    return bool(T.delete().where((T.id == task_id) & T.status.in_([FAILED, CANCELLED])).execute())


def estimate_eta_seconds(task_row: BackgroundTask, now: datetime) -> float | None:
    """Seconds left at the attempt's average rate so far, or ``None``.

    The rate is measured up to ``now``, not to the last report, so the ETA
    grows while a task stalls instead of freezing.
    """
    done, total = task_row.progress_done, task_row.progress_total
    base_done, base_at = task_row.progress_base_done, task_row.progress_base_at
    if task_row.status != RUNNING or None in (done, total, base_done, base_at):
        return None
    if done >= total:
        return 0.0
    elapsed = (now - base_at).total_seconds()
    if done <= base_done or elapsed <= 0:
        return None
    return (total - done) * elapsed / (done - base_done)


def is_stalled(task_row: BackgroundTask, now: datetime) -> bool:
    """A live task whose progress has not moved for its ``stall_seconds``."""
    if task_row.status != RUNNING or not task_row.stall_seconds or task_row.started_at is None:
        return False
    if task_row.locked_until is None or task_row.locked_until < now:
        return False  # not live: the lease-expired signal covers it
    moved = max(task_row.started_at, task_row.progress_at or task_row.started_at)
    return (now - moved).total_seconds() >= task_row.stall_seconds


def record_worker(
    worker_id: str,
    *,
    name: str,
    hostname: str,
    pid: int,
    queues: list[str],
    concurrency: int,
    state: str,
    succeeded: int,
    failed: int,
) -> None:
    """Register a worker or refresh its roster row (one upsert per heartbeat).

    Upserting rather than updating means a worker whose row went missing
    simply registers again.
    """
    now = utcnow()
    W = BackgroundWorker
    live = {
        "state": state,
        "succeeded": succeeded,
        "failed": failed,
        "last_heartbeat_at": now,
        "stopped_at": now if state == "stopped" else None,
    }
    W.insert(
        id=worker_id,
        name=name,
        hostname=hostname,
        pid=pid,
        queues=json.dumps(queues),
        concurrency=concurrency,
        started_at=now,
        **live,
    ).on_conflict(conflict_target=[W.id], update=live).execute()


def worker_status(worker: BackgroundWorker, now: datetime) -> str:
    """``online``, ``draining``, ``lost`` (no heartbeat for LOST_AFTER) or ``stopped``."""
    if worker.state == "stopped":
        return "stopped"
    if now - worker.last_heartbeat_at > WORKER_LOST_AFTER:
        return "lost"
    return "draining" if worker.state == "draining" else "online"


class TaskContext:
    """What a running handler can tell the system, and learn from it.

    ``progress`` only updates memory; the worker stores it with its next
    heartbeat. ``stage`` and ``checkpoint`` are written at once. Log records
    emitted while the handler runs are captured automatically.
    """

    def __init__(self, task_row: BackgroundTask, *, log_limit_bytes: int):
        self.task_id = task_row.id
        self.kind = task_row.kind
        self.attempt = task_row.attempts
        self.checkpoint_state = json.loads(task_row.checkpoint) if task_row.checkpoint else None
        self.cancel_requested = False
        self._row = task_row
        self._pending: dict[str, Any] = {}
        self._reported = False
        self._logs: list[dict[str, Any]] = []
        self._log_limit = log_limit_bytes
        self._log_bytes = 0
        self._log_truncated = False

    @property
    def scratch_prefix(self) -> str:
        """Deterministic scratch location, so the next attempt finds leftovers."""
        return SCRATCH_PREFIX.format(task_id=self.task_id)

    def progress(self, done: int, total: int | None = None) -> None:
        now = utcnow()
        self._pending.update(progress_done=done, progress_total=total, progress_at=now)
        if not self._reported:
            self._reported = True
            self._pending.update(progress_base_done=done, progress_base_at=now)

    def stage(self, name: str) -> None:
        record_stage(self._row, name)

    def checkpoint(self, state: Any) -> None:
        """Persist a JSON cursor that the next attempt receives as ``checkpoint_state``.

        Call it only after the work it covers is durable.
        """
        if not save_checkpoint(self._row, state):
            raise LeaseLost(f"Task {self.task_id} lost its lease before checkpointing")
        self.checkpoint_state = state

    def assert_owned(self) -> None:
        assert_owned(self._row)

    def log(self, level: str, message: str, at: datetime | None = None) -> None:
        if self._log_truncated:
            return
        # PostgreSQL text cannot hold NUL; one such record must not block the rest.
        message = message.replace("\x00", "\\x00")
        size = len(message.encode())
        if self._log_bytes + size > self._log_limit:
            self._log_truncated = True
            message = f"... log truncated: this attempt exceeded {self._log_limit} bytes"
            level = "WARNING"
        self._log_bytes += size
        self._logs.append({"at": at or utcnow(), "level": level, "message": message})

    def log_exception(self, error: BaseException) -> None:
        self.log("ERROR", "".join(traceback.format_exception(error)).rstrip())

    def take_logs(self) -> list[dict[str, Any]]:
        logs, self._logs = self._logs, []
        return logs

    def requeue_logs(self, logs: list[dict[str, Any]]) -> None:
        """Put back records a failed flush did not store, ahead of newer ones."""
        self._logs[:0] = logs

    def pending_progress(self) -> dict[str, Any]:
        return dict(self._pending)

    def clear_progress(self, stored: dict[str, Any]) -> None:
        """Forget what a heartbeat stored, keeping anything reported since."""
        for key, value in stored.items():
            if self._pending.get(key) == value:
                del self._pending[key]


current_context: contextvars.ContextVar[TaskContext | None] = contextvars.ContextVar(
    "kohakuhub_task_context", default=None
)
_capture_sink_id: int | None = None


def _capture(message) -> None:
    ctx = current_context.get()
    if ctx is not None:
        record = message.record
        # loguru's time is a datetime subclass that sqlite3 cannot bind; rebuild a plain one.
        at = datetime.fromtimestamp(record["time"].timestamp(), tz=timezone.utc).replace(tzinfo=None)
        ctx.log(record["level"].name, record["message"], at)


def install_log_capture() -> None:
    """Copy log records emitted inside a running handler into its task log.

    The worker sets ``current_context`` in the handler's asyncio task, and
    loguru calls sinks synchronously in the caller's context.
    """
    global _capture_sink_id
    if _capture_sink_id is None:
        _capture_sink_id = loguru_logger.add(
            _capture, level="INFO", filter=lambda record: current_context.get() is not None
        )


@task(CLEANUP_KIND, every=timedelta(hours=1), max_attempts=3)
async def cleanup_finished_tasks(payload: dict[str, Any], ctx: TaskContext) -> None:
    """Delete finished tasks (with their events and logs) past retention.

    Deletes in batches, so a large backlog shows progress and an interrupted
    run simply continues with what is left.
    """
    T = BackgroundTask
    now = utcnow()
    succeeded_before = now - timedelta(days=cfg.worker.succeeded_retention_days)
    failed_before = now - timedelta(days=cfg.worker.failed_retention_days)
    expired = ((T.status == SUCCEEDED) & (T.finished_at < succeeded_before)) | (
        T.status.in_([FAILED, CANCELLED]) & (T.finished_at < failed_before)
    )
    total = T.select().where(expired).count()
    deleted = 0
    while ids := [
        row_id for (row_id,) in T.select(T.id).where(expired).limit(CLEANUP_BATCH).tuples()
    ]:
        deleted += T.delete().where(T.id.in_(ids)).execute()
        ctx.progress(deleted, total)
    if deleted:
        logger.info(f"Deleted {deleted} finished background task(s)")
