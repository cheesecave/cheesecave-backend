"""Unit tests for the admin cache stats / metrics-reset routes.

These exercise the route functions directly (not through HTTP) so they
can run on any matrix without spinning up the full backend service. The
``verify_admin_token`` dependency is bypassed by calling the function
with the ``_admin=True`` keyword the FastAPI ``Depends`` would resolve
to in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import kohakuhub.api.admin.routers.cache as admin_cache


@pytest.mark.asyncio
async def test_get_cache_stats_returns_metrics_and_memory_snapshot():
    """Happy path: ``get_cache_stats`` proxies the in-process snapshot
    plus a Valkey memory probe into a single payload — ready to consume
    by the admin UI without further fan-out.
    """
    fake_metrics = {
        "configured_enabled": True,
        "client_initialized": True,
        "namespace": "kh",
        "hits": {"lakefs": 1},
        "misses": {},
        "errors": {},
        "set_count": {"lakefs": 1},
        "invalidate_count": {},
        "singleflight_contention": 0,
        "last_flush_run_id": "rid",
        "last_flush_at_ms": 1700000000000,
        "last_flushed_keys": 0,
    }
    fake_memory = {
        "available": True,
        "used_memory": 4194304,
        "used_memory_human": "4.00M",
        "maxmemory": 0,
        "maxmemory_policy": "allkeys-lfu",
        "evicted_keys": None,
    }

    with patch.object(admin_cache, "get_metrics_snapshot", return_value=fake_metrics), \
         patch.object(
             admin_cache, "get_memory_info", new=AsyncMock(return_value=fake_memory)
         ):
        result = await admin_cache.get_cache_stats(_admin=True)

    assert result["metrics"] == fake_metrics
    assert result["memory"] == fake_memory
    assert result["backend"]["scope"] == "process"
    assert result["local"]["used_bytes"] <= result["local"]["max_bytes"]
    assert len(result["history"]) <= 120


@pytest.mark.asyncio
async def test_reset_cache_metrics_succeeds_when_enabled():
    """When the cache is operational, the reset endpoint clears counters
    via the cache module's ``reset_metrics`` helper and returns a
    confirmation. We assert ``reset_metrics`` is invoked because that
    is the unit of behavior the route promises.
    """
    with patch.object(admin_cache, "reset_metrics") as reset:
        result = await admin_cache.reset_cache_metrics(_admin=True)
    assert result == {"reset": True}
    reset.assert_called_once()


@pytest.mark.asyncio
async def test_reset_and_clear_work_without_redis(monkeypatch):
    """Monitoring and maintenance are useful even without a Redis client."""
    from kohakuhub import cache

    await cache.close_cache()
    monkeypatch.setattr(cache.cfg.cache, "enabled", False)
    monkeypatch.setattr(cache.cfg.cache, "local_enabled", True)
    await cache.cache_set_json("test:immutable", {"value": 1}, immutable=True)
    snapshot = await admin_cache.get_cache_stats(_admin=True)
    assert snapshot["backend"]["mode"] == "memory"
    assert snapshot["process"]["rss_bytes"] is None or snapshot["process"]["rss_bytes"] > 0
    assert snapshot["local"]["entries"] == 1
    target = admin_cache.CacheProcessTarget(instance_id=snapshot["backend"]["instance_id"])
    assert await admin_cache.reset_cache_metrics(target=target, _admin=True) == {"reset": True}
    assert (await cache.cache_get_json("test:immutable", immutable=True))[0]
    cleared = await admin_cache.clear_local_cache(target=target, _admin=True)
    assert cleared == {"cleared": 1, "instance_id": target.instance_id, "scope": "process"}
    assert await cache.cache_get_json("test:immutable", immutable=True) == (False, None)
    await cache.close_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset_cache_metrics", "clear_local_cache"])
async def test_process_actions_reject_different_worker(action):
    target = admin_cache.CacheProcessTarget(instance_id="another-process")
    with pytest.raises(HTTPException) as exc:
        await getattr(admin_cache, action)(target=target, _admin=True)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_cache_routes_require_admin_auth():
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(admin_cache.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for method, path in [("GET", "/cache/stats"), ("POST", "/cache/metrics/reset"), ("POST", "/cache/local/clear")]:
            response = await client.request(method, path, json={"instance_id": "unknown"})
            assert response.status_code in (401, 403)


class TestProcessMemoryMonitor:
    @pytest.fixture
    def monitor(self):
        from kohakuhub.cache_monitor import ProcessMemoryMonitor

        monotonic = [10.0]
        wall = [1_700_000_000.0]
        rss = [1024]
        monitor = ProcessMemoryMonitor(
            clock=lambda: monotonic[0],
            wall_clock=lambda: wall[0],
            rss_reader=lambda: (rss[0], "test-current-rss"),
        )
        return monitor, monotonic, wall, rss

    def test_identity_is_stable_and_scoped_to_the_process(self, monitor):
        from kohakuhub.cache_monitor import ProcessMemoryMonitor

        tracker, _, _, _ = monitor
        identity = tracker.identity()
        assert identity["scope"] == "process"
        assert identity["pid"] > 0
        assert identity["started_at_ms"] == 1_700_000_000_000
        assert tracker.identity() == identity
        assert ProcessMemoryMonitor().identity()["instance_id"] != identity["instance_id"]

    def test_samples_are_rate_limited_but_rss_is_always_current(self, monitor):
        tracker, monotonic, wall, rss = monitor
        first = tracker.sample({"entries": 1, "used_bytes": 20})
        assert first["process"] == {"rss_bytes": 1024, "rss_source": "test-current-rss"}
        rss[0] = 4096
        for _ in range(200):
            result = tracker.sample({"entries": 2, "used_bytes": 40})
        assert result["process"]["rss_bytes"] == 4096
        assert len(result["history"]) == 1
        assert result["history"][0]["rss_bytes"] == 1024
        # Wall-clock changes do not let polling grow the history faster.
        wall[0] += 5000
        assert len(tracker.sample({"entries": 2, "used_bytes": 40})["history"]) == 1
        monotonic[0] += 15
        result = tracker.sample({"entries": 2, "used_bytes": 40})
        assert result["history"][-1] == {
            "timestamp_ms": int(wall[0] * 1000), "entries": 2,
            "used_bytes": 40, "rss_bytes": 4096,
        }
        assert len(result["history"]) == 2

    def test_forced_clear_samples_and_history_are_bounded(self, monitor):
        tracker, monotonic, wall, _ = monitor
        for index in range(200):
            monotonic[0] += 1
            wall[0] += 1
            result = tracker.sample({"entries": index, "used_bytes": index * 20}, force=True)
        assert len(result["history"]) == 120
        assert result["history"][0]["entries"] == 80
        assert result["history"][-1]["entries"] == 199
        result = tracker.sample({"entries": 0, "used_bytes": 0}, force=True)
        assert len(result["history"]) == 120
        assert result["history"][-1]["used_bytes"] == 0

    def test_process_change_replaces_identity_and_history(self, monitor, monkeypatch):
        from kohakuhub import cache_monitor

        tracker, _, wall, _ = monitor
        before = tracker.identity()
        tracker.sample({"entries": 1, "used_bytes": 20})
        monkeypatch.setattr(cache_monitor.os, "getpid", lambda: before["pid"] + 1)
        wall[0] += 10
        after = tracker.identity()
        assert after["instance_id"] != before["instance_id"]
        assert after["pid"] == before["pid"] + 1
        assert after["started_at_ms"] == 1_700_000_010_000
        assert len(tracker.sample({"entries": 0, "used_bytes": 0})["history"]) == 1

    def test_unavailable_rss_remains_unknown(self):
        from kohakuhub.cache_monitor import ProcessMemoryMonitor

        tracker = ProcessMemoryMonitor(rss_reader=lambda: (None, None))
        result = tracker.sample({"entries": 0, "used_bytes": 0}, interval_seconds=0)
        assert result["process"] == {"rss_bytes": None, "rss_source": None}
        assert result["history"][0]["rss_bytes"] is None

    def test_linux_reads_current_resident_pages(self, monkeypatch):
        from kohakuhub import cache_monitor

        monkeypatch.setattr(cache_monitor.sys, "platform", "linux")
        monkeypatch.setattr(cache_monitor.Path, "read_text", lambda *_args, **_kwargs: "99999 42 1 2 3")
        monkeypatch.setattr(cache_monitor.os, "sysconf", lambda _name: 4096, raising=False)
        assert cache_monitor.read_process_rss() == (42 * 4096, "linux-proc-statm")

    @pytest.mark.parametrize("contents", ["", "12 invalid", "12 -1", "12 0"])
    def test_invalid_linux_stat_is_unknown(self, monkeypatch, contents):
        from kohakuhub import cache_monitor

        monkeypatch.setattr(cache_monitor.sys, "platform", "linux")
        monkeypatch.setattr(cache_monitor.Path, "read_text", lambda *_args, **_kwargs: contents)
        monkeypatch.setattr(cache_monitor.os, "sysconf", lambda _name: 4096, raising=False)
        assert cache_monitor.read_process_rss() == (None, None)

    def test_inaccessible_os_metric_is_unknown(self, monkeypatch):
        from kohakuhub import cache_monitor

        def denied(*_args, **_kwargs):
            raise PermissionError("cannot access process memory")

        monkeypatch.setattr(cache_monitor.sys, "platform", "linux")
        monkeypatch.setattr(cache_monitor.Path, "read_text", denied)
        assert cache_monitor.read_process_rss() == (None, None)

    @pytest.mark.parametrize("success", [True, False])
    def test_windows_uses_current_working_set_not_peak(self, monkeypatch, success):
        from types import SimpleNamespace
        from kohakuhub import cache_monitor

        def current_process():
            return 123

        def process_info(handle, pointer, size):
            assert handle == 123
            assert size > 0
            pointer._obj.WorkingSetSize = 2048
            pointer._obj.PeakWorkingSetSize = 1_000_000
            return success

        kernel = SimpleNamespace(GetCurrentProcess=current_process)
        psapi = SimpleNamespace(GetProcessMemoryInfo=process_info)
        monkeypatch.setattr(cache_monitor.sys, "platform", "win32")
        monkeypatch.setattr(cache_monitor.ctypes, "WinDLL", lambda name, **_kwargs: kernel if name == "kernel32" else psapi, raising=False)
        expected = (2048, "windows-working-set") if success else (None, None)
        assert cache_monitor.read_process_rss() == expected

    def test_unsupported_platform_is_unknown(self, monkeypatch):
        from kohakuhub import cache_monitor

        monkeypatch.setattr(cache_monitor.sys, "platform", "unsupported")
        assert cache_monitor.read_process_rss() == (None, None)
