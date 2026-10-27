"""Bounded, expiring storage for the process-local cache fallback.

Values are serialized strings: consumers decode on each read so cached mutable
objects cannot escape by reference. Operations are synchronous and must run on
the owning event-loop thread. No per-entry tasks, timers or secondary indexes
retain evicted keys. The lifecycle owner periodically calls :meth:`sweep` so
unread expired values are also released while traffic is idle.

``used_bytes`` accounts for Python key/value sizes, an entry and its scalar
fields, plus a fixed 96-byte allowance for an OrderedDict node/hash-table slot.
It is a bounded estimate of retained cache data, not process RSS or an exact
allocator measurement. Entry count independently bounds container overhead.
"""

import math
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Entry:
    payload: str
    expires_at: float
    size: int


_ENTRY_OVERHEAD = (
    sys.getsizeof(_Entry("", 0.0, 0))
    + sys.getsizeof(0.0)
    + sys.getsizeof(0)
    + 96
)


class BoundedMemoryCache:
    """TTL + LRU cache constrained by both entry count and estimated bytes."""

    def __init__(
        self,
        max_entries: int,
        max_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("Cache limits must be non-negative")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._used_bytes = 0
        self.reset_metrics()

    def _remove(self, key: str) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        self._used_bytes -= entry.size
        return True

    def get(self, key: str, *, record: bool = True) -> str | None:
        """Read/touch an entry; internal singleflight rechecks use record=False."""
        entry = self._entries.get(key)
        if entry is None:
            self.misses += int(record)
            return None
        if entry.expires_at <= self._clock():
            self._remove(key)
            self.expirations += 1
            self.misses += int(record)
            return None
        self._entries.move_to_end(key)
        self.hits += int(record)
        return entry.payload

    def set(self, key: str, payload: str, ttl: float) -> bool:
        if not math.isfinite(ttl) or ttl <= 0:
            self._remove(key)
            return False
        if self.max_entries == 0 or self.max_bytes == 0:
            return False
        size = sys.getsizeof(key) + sys.getsizeof(payload) + _ENTRY_OVERHEAD
        # Reject before touching any retained entry, including an older value
        # under this key. One huge result must not empty a useful cache.
        if size > self.max_bytes:
            self.rejected_oversize += 1
            return False
        self._remove(key)
        if (
            len(self._entries) >= self.max_entries
            or self._used_bytes + size > self.max_bytes
        ):
            self.sweep()
        while (
            len(self._entries) >= self.max_entries
            or self._used_bytes + size > self.max_bytes
        ):
            _, oldest = self._entries.popitem(last=False)
            self._used_bytes -= oldest.size
            self.evictions += 1
        self._entries[key] = _Entry(payload, self._clock() + ttl, size)
        self._used_bytes += size
        self.sets += 1
        self.peak_entries = max(self.peak_entries, len(self._entries))
        self.peak_bytes = max(self.peak_bytes, self._used_bytes)
        return True

    def delete(self, *keys: str) -> int:
        return sum(self._remove(key) for key in keys)

    def delete_prefix(self, prefix: str) -> int:
        keys = [key for key in self._entries if key.startswith(prefix)]
        return self.delete(*keys)

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        self._used_bytes = 0
        self.clears += 1
        return count

    def sweep(self) -> int:
        now = self._clock()
        expired = [
            key for key, entry in self._entries.items() if entry.expires_at <= now
        ]
        count = self.delete(*expired)
        self.expirations += count
        return count

    def reset_metrics(self) -> None:
        """Reset counters and peaks without discarding retained data."""
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.evictions = 0
        self.expirations = 0
        self.rejected_oversize = 0
        self.clears = 0
        self.peak_entries = len(self._entries)
        self.peak_bytes = self._used_bytes

    def snapshot(self) -> dict:
        self.sweep()
        namespaces: dict[str, dict] = {}
        for key, entry in self._entries.items():
            namespace = key.split(":", 1)[0] or "<unknown>"
            row = namespaces.setdefault(
                namespace, {"namespace": namespace, "entries": 0, "used_bytes": 0}
            )
            row["entries"] += 1
            row["used_bytes"] += entry.size
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "used_bytes": self._used_bytes,
            "max_bytes": self.max_bytes,
            "peak_entries": self.peak_entries,
            "peak_bytes": self.peak_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "rejected_oversize": self.rejected_oversize,
            "clears": self.clears,
            "namespaces": sorted(namespaces.values(), key=lambda row: row["namespace"]),
        }
