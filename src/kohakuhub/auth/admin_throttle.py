"""The wait a client earns by failing the admin token check.

A few mistakes cost nothing; after that every further failure starts a wait that doubles (the shape AWS Cognito uses
and OWASP's testing guide calls acceptable), up to a cap. It is counted per client, never globally: a global lock
would let anybody lock the real administrator out by failing on purpose.

Two stores with one interface. ``AdminAuthThrottle`` keeps the state in the memory of the process (bounded, behind a
lock). ``SharedAdminAuthThrottle`` keeps it in the ``admin_auth_client`` table, so every worker process and every
replica meets the same wait; it keeps what it learned in a memory of its own, so a client that is waiting costs no
query for the length of the wait, and it falls back to counting in memory if the table cannot be used.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from peewee import IntegrityError

from kohakuhub.db import AdminAuthClient
from kohakuhub.logger import get_logger

logger = get_logger("ADMIN-AUTH")

MAX_CLIENTS = 10_000
ALERT_WINDOW_SECONDS = 60.0
PRUNE_EVERY_SECONDS = 60.0
FALLBACK_REPORT_EVERY_SECONDS = 60.0


@dataclass(frozen=True)
class ThrottleParams:
    free: int  # consecutive failures that cost nothing
    base_wait: float  # seconds of the first wait, doubled by each further failure
    max_wait: float  # the longest wait
    reset_after: float  # seconds without a failure after which the count starts again


@dataclass
class _Entry:
    failures: int = 0
    last_failure: float = 0.0
    wait_until: float = 0.0


def wait_for_failures(failures: int, params: ThrottleParams) -> float:
    """The wait a client has to serve after ``failures`` consecutive failures (0 while some are free)."""
    over = failures - params.free
    return min(params.base_wait * 2 ** (over - 1), params.max_wait) if over > 0 else 0


class AdminAuthThrottle:
    def __init__(self, clock=time.time, max_clients: int = MAX_CLIENTS):
        self._clock = clock
        self._max_clients = max_clients
        self._lock = threading.Lock()
        self._clients: OrderedDict[str, _Entry] = OrderedDict()
        self._recent_failures: deque[float] = deque()
        self._last_alert = -math.inf

    def check(self, key: str) -> int:
        """Seconds the client still has to wait (rounded up), 0 when it may try."""
        with self._lock:
            entry = self._clients.get(key)
            if entry is None:
                return 0
            self._clients.move_to_end(key)
            remaining = entry.wait_until - self._clock()
            return math.ceil(remaining) if remaining > 0 else 0

    def record_failure(self, key: str, params: ThrottleParams) -> int:
        """Count a failure; returns the wait it started (0 while the client still has free failures)."""
        with self._lock:
            now = self._clock()
            entry = self._clients.get(key)
            if entry is None:
                entry = self._clients[key] = _Entry()
                while len(self._clients) > self._max_clients:
                    self._clients.popitem(last=False)
            elif now - entry.last_failure > params.reset_after:
                entry.failures = 0
            self._clients.move_to_end(key)
            entry.failures += 1
            entry.last_failure = now
            wait = wait_for_failures(entry.failures, params)
            entry.wait_until = now + wait
            return int(wait) if float(wait).is_integer() else wait

    def record_success(self, key: str) -> None:
        with self._lock:
            self._clients.pop(key, None)

    def note_failure_for_alert(self, threshold: int) -> bool:
        """Count a failure for the global alert; True when it is time to say the failure rate is high.

        Informational only: the alert never blocks or delays anybody.
        """
        if threshold <= 0:
            return False
        with self._lock:
            now = self._clock()
            self._recent_failures.append(now)
            while self._recent_failures and now - self._recent_failures[0] > ALERT_WINDOW_SECONDS:
                self._recent_failures.popleft()
            if len(self._recent_failures) > threshold and now - self._last_alert >= ALERT_WINDOW_SECONDS:
                self._last_alert = now
                return True
            return False

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._clients

    def set_state(self, key: str, failures: int, last_failure: float, wait_until: float) -> None:
        """Remember what the shared table said about a client."""
        with self._lock:
            self._clients[key] = _Entry(failures, last_failure, wait_until)
            self._clients.move_to_end(key)
            while len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)

    def forget(self, key: str) -> None:
        with self._lock:
            self._clients.pop(key, None)

    def failures(self, key: str) -> int:
        """The client's current count of consecutive failures (for the log)."""
        with self._lock:
            entry = self._clients.get(key)
            return entry.failures if entry else 0

    def size(self) -> int:
        with self._lock:
            return len(self._clients)

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()
            self._recent_failures.clear()
            self._last_alert = -math.inf


class SharedAdminAuthThrottle:
    """The throttle on the ``admin_auth_client`` table (see the module docstring)."""

    def __init__(self, clock=time.time, max_clients: int = MAX_CLIENTS):
        self._clock = clock
        self.local = AdminAuthThrottle(clock=clock, max_clients=max_clients)
        self._last_prune = -math.inf
        self._last_report = -math.inf

    def set_clock(self, clock) -> None:
        self._clock = clock
        self.local._clock = clock

    # --- the table ------------------------------------------------------------------------------------------

    def _load(self, key: str):
        """The client's row, locked for the transaction on a database that has row locks."""
        query = AdminAuthClient.select().where(AdminAuthClient.client_key == key)
        if AdminAuthClient._meta.database.for_update:
            query = query.for_update()
        return query.first()

    def _report(self, what: str, error: Exception) -> None:
        now = self._clock()
        if now - self._last_report >= FALLBACK_REPORT_EVERY_SECONDS:
            self._last_report = now
            logger.warning(
                f"Admin token throttle: the admin_auth_client table could not be used ({what}: {error}); "
                "counting in this process only until it can"
            )

    def _prune(self, now: float, params: ThrottleParams) -> None:
        AdminAuthClient.delete().where(
            (AdminAuthClient.last_failure < now - 2 * params.reset_after)
            & (AdminAuthClient.wait_until < now)
        ).execute()

    def _maybe_prune(self, now: float, params: ThrottleParams) -> None:
        if now - self._last_prune < PRUNE_EVERY_SECONDS:
            return
        self._last_prune = now
        try:
            self._prune(now, params)
        except Exception as error:
            self._report("prune", error)

    # --- the interface of AdminAuthThrottle ---------------------------------------------------------------

    def check(self, key: str) -> int:
        wait = self.local.check(key)  # a wait this worker already knows costs no query
        if wait:
            return wait
        try:
            row = AdminAuthClient.get_or_none(AdminAuthClient.client_key == key)
        except Exception as error:
            self._report("read", error)
            return 0
        if row is None:
            self.local.forget(key)
            return 0
        self.local.set_state(key, row.failures, row.last_failure, row.wait_until)
        remaining = row.wait_until - self._clock()
        return math.ceil(remaining) if remaining > 0 else 0

    def record_failure(self, key: str, params: ThrottleParams) -> int:
        database = AdminAuthClient._meta.database
        for _ in range(3):
            now = self._clock()
            try:
                with database.atomic():
                    row = self._load(key)
                    quiet = row is not None and now - row.last_failure > params.reset_after
                    failures = 1 if row is None or quiet else row.failures + 1
                    wait = wait_for_failures(failures, params)
                    if row is None:
                        AdminAuthClient.create(
                            client_key=key, failures=failures, last_failure=now, wait_until=now + wait
                        )
                    else:
                        AdminAuthClient.update(
                            failures=failures, last_failure=now, wait_until=now + wait
                        ).where(AdminAuthClient.client_key == key).execute()
            except IntegrityError:
                continue  # another worker made the row between our look and our insert: look again
            except Exception as error:
                self._report("write", error)
                return self.local.record_failure(key, params)
            self.local.set_state(key, failures, now, now + wait)
            self._maybe_prune(now, params)
            return int(wait) if float(wait).is_integer() else wait
        return self.local.record_failure(key, params)  # the rows kept racing: do not fail the request over it

    def record_success(self, key: str) -> None:
        if self.local.has(key):  # the check of this very request found failures in the table
            try:
                AdminAuthClient.delete().where(AdminAuthClient.client_key == key).execute()
            except Exception as error:
                self._report("delete", error)
        self.local.record_success(key)

    def failures(self, key: str) -> int:
        return self.local.failures(key)

    def note_failure_for_alert(self, threshold: int) -> bool:
        return self.local.note_failure_for_alert(threshold)

    def size(self) -> int:
        return self.local.size()

    def reset(self) -> None:
        self.local.reset()
        self._last_prune = -math.inf
        self._last_report = -math.inf
        try:
            AdminAuthClient.delete().execute()
        except Exception:
            pass  # no table (a unit test without a database): nothing to forget

