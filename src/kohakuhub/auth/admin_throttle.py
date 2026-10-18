"""The wait a client earns by failing the admin token check.

A few mistakes cost nothing; after that every further failure starts a wait that doubles (the shape AWS Cognito uses
and OWASP's testing guide calls acceptable), up to a cap. It is counted per client, never globally: a global lock
would let anybody lock the real administrator out by failing on purpose.

State lives in this process (several workers each keep their own), bounded, behind a lock.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

MAX_CLIENTS = 10_000
ALERT_WINDOW_SECONDS = 60.0


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


class AdminAuthThrottle:
    def __init__(self, clock=time.monotonic, max_clients: int = MAX_CLIENTS):
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
            over = entry.failures - params.free
            wait = min(params.base_wait * 2 ** (over - 1), params.max_wait) if over > 0 else 0
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
