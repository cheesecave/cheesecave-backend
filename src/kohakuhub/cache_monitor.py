"""Bounded process-memory observations for the cache Admin panel.

The process working set includes the entire application and is distinct from
the cache's accounted-byte estimate. No peak-RSS substitute is returned when
the operating system cannot provide a current measurement.
"""

import ctypes
import os
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_rss() -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_uint32,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError("Current process working set unavailable")
    return int(counters.WorkingSetSize)


def read_process_rss() -> tuple[int | None, str | None]:
    """Read current resident memory, or explicitly report an unknown value."""
    try:
        if sys.platform == "win32":
            rss = _windows_rss()
            source = "windows-working-set"
        elif sys.platform.startswith("linux"):
            fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
            rss = int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
            source = "linux-proc-statm"
        else:
            return None, None
        if rss > 0:
            return rss, source
    except (OSError, ValueError, IndexError, AttributeError, OverflowError):
        # Metrics must not break the Admin endpoint or leak OS error details.
        pass
    return None, None


class ProcessMemoryMonitor:
    """Record at most 120 observations, with no task or timer of its own."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        rss_reader: Callable[[], tuple[int | None, str | None]] = read_process_rss,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._rss_reader = rss_reader
        self._history: deque[dict] = deque(maxlen=120)
        self._last_sample: float | None = None
        self._pid: int | None = None
        self._identity: dict = {}
        self._ensure_process()

    def _ensure_process(self) -> None:
        pid = os.getpid()
        if pid != self._pid:
            # A monitor constructed before fork must not attribute a child's
            # memory to its parent or keep the parent's observation history.
            self._pid = pid
            self._identity = {
                "instance_id": f"{pid}-{uuid.uuid4().hex}",
                "pid": pid,
                "scope": "process",
                "started_at_ms": int(self._wall_clock() * 1000),
            }
            self._history.clear()
            self._last_sample = None

    def identity(self) -> dict:
        self._ensure_process()
        return dict(self._identity)

    def sample(
        self,
        local_snapshot: dict,
        interval_seconds: float = 15,
        force: bool = False,
    ) -> dict:
        self._ensure_process()
        now = self._clock()
        rss, source = self._rss_reader()
        if (
            force
            or self._last_sample is None
            or now - self._last_sample >= max(0, interval_seconds)
        ):
            self._history.append(
                {
                    "timestamp_ms": int(self._wall_clock() * 1000),
                    "entries": local_snapshot["entries"],
                    "used_bytes": local_snapshot["used_bytes"],
                    "rss_bytes": rss,
                }
            )
            self._last_sample = now
        return {
            "process": {"rss_bytes": rss, "rss_source": source},
            "history": [dict(point) for point in self._history],
        }
