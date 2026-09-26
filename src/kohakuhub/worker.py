"""Background task worker: ``python -m kohakuhub.worker``.

Claims rows from ``background_task`` and runs their registered handlers with
bounded concurrency. Delivery semantics live in ``kohakuhub.tasks``.
The worker never runs migrations; it waits until hub-api has created the table.
"""

import asyncio
import json
import os
import signal
import socket
import uuid

from kohakuhub import tasks
from kohakuhub import storage_cleanup  # noqa: F401 - registers the storage cleanup tasks
from kohakuhub.config import cfg
from kohakuhub.db import BackgroundTask, BackgroundWorker, db
from kohakuhub.lakefs_rest_client import close_lakefs_rest_client
from kohakuhub.logger import get_logger

logger = get_logger("WORKER")

# Re-create missing periodic occurrences (e.g. one an admin cancelled).
PERIODIC_RESYNC_SECONDS = 60.0


def _reset_connection() -> None:
    """Drop a possibly broken connection; Peewee reconnects on next use."""
    try:
        db.close()
    except Exception as e:
        logger.warning(f"Failed to close database connection: {e}")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


class Worker:
    def __init__(
        self,
        *,
        worker_id: str | None = None,
        concurrency: int | None = None,
        lease_seconds: float | None = None,
        poll_interval: float | None = None,
        shutdown_grace: float | None = None,
        queues: list[str] | None = None,
        flush_interval: float | None = None,
        log_limit_bytes: int | None = None,
        name: str | None = None,
    ):
        options = cfg.worker
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.worker_id = worker_id or f"{self.hostname}:{self.pid}:{uuid.uuid4().hex[:8]}"
        prefix = options.name if name is None else name
        # In Docker the hostname is the container id, which `docker ps` shows.
        self.name = f"{prefix}-{self.hostname}" if prefix else self.hostname
        self.concurrency = concurrency or options.concurrency
        self.lease_seconds = lease_seconds or options.lease_seconds
        self.poll_interval = poll_interval or options.poll_interval_seconds
        self.shutdown_grace = shutdown_grace or options.shutdown_grace_seconds
        self.queues = options.queues if queues is None else queues
        flush_interval = flush_interval or options.flush_interval_seconds
        # One tick stores progress and logs, renews the lease and polls for
        # cancellation, so it must also come often enough for the lease.
        self.tick = min(flush_interval, self.lease_seconds / 3)
        self.log_limit_bytes = log_limit_bytes or options.log_max_bytes_per_attempt
        self.succeeded = 0
        self.failed = 0
        self._running: set[asyncio.Task] = set()

    async def wait_for_schema(self, stop: asyncio.Event) -> bool:
        """Block until the task tables exist; ``False`` if stopped first."""
        while not stop.is_set():
            try:
                # background_worker is the last table the task migrations create.
                if BackgroundWorker.table_exists():
                    return True
                logger.info("Waiting for the background task tables (migrations run on hub-api)")
            except Exception as e:
                logger.warning(f"Database not ready: {e}")
                _reset_connection()
            await _sleep_or_stop(stop, self.poll_interval)
        return False

    async def run(self, stop: asyncio.Event) -> None:
        """Claim and run tasks until ``stop`` is set, then drain."""
        logger.info(
            f"Worker {self.worker_id} started "
            f"(concurrency={self.concurrency}, queues={self.queues or 'all'})"
        )
        tasks.install_log_capture()
        loop = asyncio.get_running_loop()
        next_resync = next_heartbeat = loop.time()
        try:
            while not stop.is_set():
                if loop.time() >= next_heartbeat:
                    self._report("running")
                    next_heartbeat = loop.time() + tasks.WORKER_HEARTBEAT_SECONDS
                if loop.time() >= next_resync:
                    self._resync_periodic()
                    next_resync = loop.time() + PERIODIC_RESYNC_SECONDS
                if len(self._running) < self.concurrency and (row := self._claim()) is not None:
                    self._start(row)
                    continue
                await self._wait(stop)
            self._report("draining")
            await self._drain()
        finally:
            self._report("stopped")
        logger.info(f"Worker {self.worker_id} stopped")

    def _report(self, state: str) -> None:
        """Refresh this worker's roster row; the next heartbeat retries on failure."""
        try:
            tasks.record_worker(
                self.worker_id,
                name=self.name,
                hostname=self.hostname,
                pid=self.pid,
                queues=self.queues,
                concurrency=self.concurrency,
                state=state,
                succeeded=self.succeeded,
                failed=self.failed,
            )
        except Exception as e:
            logger.warning(f"Failed to update the worker roster: {e}")
            _reset_connection()

    def _resync_periodic(self) -> None:
        try:
            tasks.ensure_periodic_tasks()
        except Exception as e:
            logger.exception("Failed to schedule periodic tasks", e)
            _reset_connection()

    def _claim(self) -> BackgroundTask | None:
        try:
            return tasks.claim_next(
                self.worker_id, lease_seconds=self.lease_seconds, queues=self.queues or None
            )
        except Exception as e:
            logger.exception("Failed to claim a task", e)
            _reset_connection()
            return None

    def _start(self, row: BackgroundTask) -> None:
        running = asyncio.create_task(self._execute(row))
        self._running.add(running)
        running.add_done_callback(self._running.discard)

    async def _wait(self, stop: asyncio.Event) -> None:
        """Sleep until stop, a handler finishing, or the next poll."""
        stop_waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait(
                {stop_waiter, *self._running},
                timeout=self.poll_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_waiter.cancel()

    async def _drain(self) -> None:
        if not self._running:
            return
        logger.info(f"Waiting up to {self.shutdown_grace}s for {len(self._running)} task(s)")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.shutdown_grace
        pending = set(self._running)
        # Keep heartbeating, so a long drain shows as draining rather than lost.
        while pending and (remaining := deadline - loop.time()) > 0:
            _, pending = await asyncio.wait(
                pending, timeout=min(remaining, tasks.WORKER_HEARTBEAT_SECONDS)
            )
            self._report("draining")
        # Cancelled tasks are released back to the queue (see _execute).
        for running in pending:
            running.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _execute(self, row: BackgroundTask) -> None:
        spec = tasks.get_spec(row.kind)
        ctx = tasks.TaskContext(row, log_limit_bytes=self.log_limit_bytes)
        loop = asyncio.get_running_loop()
        started = loop.time()
        run = asyncio.create_task(self._invoke(spec, row, ctx))
        heartbeat = asyncio.create_task(self._heartbeat(row, run, ctx))
        try:
            await run
        except asyncio.CancelledError:
            stopped_by = heartbeat.result() if heartbeat.done() else None
            if stopped_by == tasks.LEASE_CANCEL:
                ctx.log("WARNING", "Interrupted: cancellation was requested")
                self._flush(row, ctx)
                self._record(lambda: tasks.cancel_running_task(row))
            elif stopped_by == tasks.LEASE_LOST:
                logger.warning(f"Lost the lease on task {row.id} ({row.kind}); abandoned it")
                self._flush(row, ctx)
            else:
                # Worker shutdown: hand the task back instead of waiting out the lease.
                ctx.log("WARNING", "Interrupted: the worker is shutting down")
                self._flush(row, ctx)
                self._record(lambda: tasks.release_task(row))
                raise
        except asyncio.TimeoutError as e:
            ctx.log_exception(e)
            self._flush(row, ctx)
            if loop.time() - started >= spec.timeout:
                self._record_failure(row, f"Timed out after {spec.timeout}s")
            else:  # raised by the handler itself
                self._record_failure(row, f"TimeoutError: {e}")
        except tasks.TaskCancelled:
            ctx.log("WARNING", "Stopped early: cancellation was requested")
            self._flush(row, ctx)
            self._record(lambda: tasks.cancel_running_task(row))
        except tasks.PermanentTaskError as e:
            ctx.log_exception(e)
            self._flush(row, ctx)
            self._record_failure(row, f"PermanentTaskError: {e}", permanent=True)
        except Exception as e:
            ctx.log_exception(e)
            self._flush(row, ctx)
            self._record_failure(row, f"{type(e).__name__}: {e}")
        else:
            self._flush(row, ctx)
            if self._record(lambda: tasks.complete_task(row)):
                self.succeeded += 1
        finally:
            heartbeat.cancel()
            run.cancel()

    @staticmethod
    async def _invoke(spec: tasks.TaskSpec, row: BackgroundTask, ctx: tasks.TaskContext) -> None:
        # Set in the handler's own asyncio task, so only its log records are captured.
        tasks.current_context.set(ctx)
        # Decoding inside the task routes a corrupt payload through normal failure handling.
        payload = json.loads(row.payload)
        args = (payload, ctx) if spec.takes_context else (payload,)
        await asyncio.wait_for(spec.handler(*args), timeout=spec.timeout)

    def _flush(self, row: BackgroundTask, ctx: tasks.TaskContext) -> str | None:
        """Store captured logs and pending progress, renewing the lease.

        Returns the heartbeat outcome, or ``None`` if the database failed; what
        could not be stored is kept for the next flush. Logs and the heartbeat
        are written separately, so failing logs never cost the lease.
        """
        logs = ctx.take_logs()
        try:
            tasks.write_logs(row, logs)
        except Exception as e:
            logger.warning(f"Failed to store logs of task {row.id}: {e}")
            _reset_connection()
            ctx.requeue_logs(logs)
        progress = ctx.pending_progress()
        try:
            state = tasks.heartbeat(row, lease_seconds=self.lease_seconds, progress=progress)
        except Exception as e:
            logger.warning(f"Failed to store progress of task {row.id}: {e}")
            _reset_connection()
            return None
        ctx.clear_progress(progress)
        return state

    async def _heartbeat(
        self, row: BackgroundTask, run: asyncio.Task, ctx: tasks.TaskContext
    ) -> str:
        """Flush and renew every tick; stop the handler if the lease is lost,
        or if cancellation was requested and it did not stop on its own
        within ``shutdown_grace``. Returns why it stopped the handler."""
        loop = asyncio.get_running_loop()
        cancel_deadline = None
        while True:
            await asyncio.sleep(self.tick)
            state = self._flush(row, ctx)
            if state == tasks.LEASE_LOST:
                run.cancel()
                return state
            if state == tasks.LEASE_CANCEL:
                if cancel_deadline is None:
                    ctx.cancel_requested = True
                    cancel_deadline = loop.time() + self.shutdown_grace
                    ctx.log(
                        "WARNING",
                        f"Cancellation requested; interrupting in {self.shutdown_grace:g}s "
                        "unless the task stops first",
                    )
                elif loop.time() >= cancel_deadline:
                    run.cancel()
                    return state

    def _record_failure(self, row: BackgroundTask, error: str, *, permanent: bool = False) -> None:
        status = self._record(lambda: tasks.fail_task(row, error, permanent=permanent))
        if status is not None:
            self.failed += 1
        logger.warning(
            f"Task {row.id} ({row.kind}) attempt {row.attempts} failed -> {status}: {error}"
        )

    def _record(self, write):
        try:
            return write()
        except Exception as e:
            # The lease expires and another worker retries the task.
            logger.exception("Failed to record a task outcome", e)
            _reset_connection()
            return None


async def serve(worker: Worker | None = None) -> None:
    """Run a worker until SIGINT/SIGTERM."""
    worker = worker or Worker()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        if await worker.wait_for_schema(stop):
            await worker.run(stop)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        await close_lakefs_rest_client()
        _reset_connection()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    main()
