"""Redis/Valkey cache with a bounded fallback for immutable values.

Repository cache integrations use this shared entry point. Hand-rolled
``redis.asyncio`` calls would bypass:

- TTL jitter (stampede prevention)
- Two-level singleflight (cross-worker + intra-worker)
- Silent-degradation contract (a flaky Valkey must produce cache misses,
  never request errors)
- Namespace metrics (admin observability)
- Boot-time flush coordinator (Mode-B namespaces are wiped on every Valkey
  restart, regardless of API restarts; Mode-A namespaces survive)

See ``docs/development/cache.md`` for backend selection, local limits and
monitoring, plus the consistency requirements for future mutable integrations.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis

from kohakuhub.cache_memory import BoundedMemoryCache
from kohakuhub.cache_monitor import ProcessMemoryMonitor

from kohakuhub.config import cfg
from kohakuhub.logger import get_logger

logger = get_logger("CACHE")

# Sentinel used to encode "verified absent" without colliding with a real
# JSON-encoded ``null``. The negative cache lets us short-circuit repeated
# get_repository("ghost-namespace/ghost-repo") lookups without hitting the
# DB on every probe (anti-DoS for scrapers).
_NEGATIVE_SENTINEL = "__kh_neg_v1__"

# Mode-B namespace prefixes that are flushed on every Valkey restart.
#
# These hold either:
# - mutable state with write-through DEL (sess, tok, lakefs:branch, …) — a
#   write that lands while Valkey is down loses its DEL, and after RDB
#   restore the entry is silently stale. Flushing on boot bounds the
#   staleness window.
# - generation counters (repo:gen, org:gen, user:gen, list_gen) — the
#   counter's source of truth is the Postgres ``cache_gen`` column once
#   that lands. Flushing forces a fresh read from DB.
# - generation-keyed derived caches (repo:info, org:info, user:info,
#   uo:*, list:*) — old gen suffixes are unreachable anyway, but flushing
#   reclaims memory immediately rather than waiting for LFU.
# - negative cache (negative:*) — write-through DEL on entity creation
#   would be lost on a Valkey-down window.
#
# Mode-A namespaces (lakefs:commit, lakefs:stat, lakefs:list — keyed by
# commit_id) are intentionally NOT in this list. They are content-addressed
# and survive across restarts safely; that is the entire reason we accept
# the persistence cost.
MODE_B_PREFIXES: tuple[str, ...] = (
    "lakefs:branch:",
    "repo:gen:",
    "repo:info:",
    "org:gen:",
    "org:info:",
    "user:gen:",
    "user:info:",
    "uo:",
    "sess:",
    "tok:",
    "list_gen:",
    "list:",
    "negative:",
)

BOOTSTRAP_LOCK_KEY = "cache:bootstrap_lock"
BOOTSTRAP_RUN_ID_KEY = "cache:bootstrap_run_id"
BOOTSTRAP_LOCK_TTL_SECONDS = 30
BOOTSTRAP_WAIT_SLOP_SECONDS = 5
SINGLEFLIGHT_LOCK_TTL_SECONDS = 5
SINGLEFLIGHT_POLL_INTERVAL = 0.05
SCAN_BATCH_SIZE = 500


# ----------------------------------------------------------------------------
# Module state
# ----------------------------------------------------------------------------


@dataclass
class CacheMetrics:
    """In-process counters for the admin observability endpoint.

    Reset on process restart by design — these are operational signals, not
    business data.
    """

    hits: dict[str, int] = field(default_factory=dict)
    misses: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    set_count: dict[str, int] = field(default_factory=dict)
    invalidate_count: dict[str, int] = field(default_factory=dict)
    singleflight_contention: int = 0
    last_flush_run_id: str | None = None
    last_flush_at_ms: int | None = None
    last_flushed_keys: int = 0


_metrics = CacheMetrics()
_pool: aioredis.ConnectionPool | None = None
_client: aioredis.Redis | None = None
_local_locks: dict[str, tuple[asyncio.Lock, int]] = {}
_local: BoundedMemoryCache | None = None
_redis_available = False
_redis_reason = "not_initialized"
_next_redis_retry = 0.0
_probe_running = False
_needs_recovery_flush = False
_maintenance_task: asyncio.Task | None = None
_monitor = ProcessMemoryMonitor()


def _bump(counter: dict[str, int], key: str) -> None:
    # Metrics must not become an unbounded cache of caller-controlled namespaces.
    key = key[:64]
    if key not in counter and len(counter) >= 64:
        key = "<other>"
    counter[key] = counter.get(key, 0) + 1


def _ns(key: str) -> str:
    """Group metrics by the first ``:`` segment of the key."""
    head = key.split(":", 1)[0]
    return head or "<unknown>"


def _prefixed(key: str) -> str:
    """Apply the configured namespace prefix.

    Putting *every* key behind one prefix lets multiple deployments share a
    Valkey cluster without colliding, and lets tests use a per-session
    namespace to isolate from any leftover state.
    """
    return f"{cfg.cache.namespace}:{key}"


def _jittered_ttl(ttl: int) -> int:
    """Apply ±jitter to TTL to spread expiry across keys.

    Without this, a burst of writes that share a TTL all expire at the same
    instant — the classic synchronized-stampede pattern.
    """
    if cfg.cache.jitter_fraction <= 0 or ttl <= 1:
        return max(1, int(ttl))
    delta = ttl * cfg.cache.jitter_fraction
    return max(1, int(ttl + random.uniform(-delta, delta)))


@asynccontextmanager
async def _local_singleflight(key: str):
    """Keep active locks until their last waiter leaves; cap retained keys."""
    entry = _local_locks.get(key)
    if entry is None and len(_local_locks) >= 4096:
        yield
        return
    lock, users = entry or (asyncio.Lock(), 0)
    _local_locks[key] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        _, users = _local_locks[key]
        if users == 1:
            del _local_locks[key]
        else:
            _local_locks[key] = (lock, users - 1)


def _memory_cache() -> BoundedMemoryCache:
    global _local
    if _local is None:
        _local = BoundedMemoryCache(cfg.cache.local_max_entries, cfg.cache.local_max_bytes)
    return _local


def _local_enabled() -> bool:
    return cfg.cache.local_enabled and cfg.cache.local_max_entries > 0 and cfg.cache.local_max_bytes > 0


def _redis_failed(reason: str) -> None:
    global _redis_available, _redis_reason, _next_redis_retry, _needs_recovery_flush
    _redis_available = False
    _redis_reason = reason
    _needs_recovery_flush = True
    _next_redis_retry = time.monotonic() + cfg.cache.redis_retry_interval_seconds


async def _ready_client() -> aioredis.Redis | None:
    """Use a cooldown and one bounded probe, never poll a broken connection."""
    global _redis_available, _redis_reason, _probe_running, _next_redis_retry, _needs_recovery_flush
    if not cfg.cache.enabled or _client is None:
        return None
    if _redis_available:
        return _client
    if _probe_running or time.monotonic() < _next_redis_retry:
        return None
    _probe_running = True
    try:
        async def probe():
            await _client.ping()
            await _bootstrap_flush()
            # A connection outage can lose invalidations without a Redis restart.
            # Generation counters and their derived values must be discarded together.
            if _needs_recovery_flush:
                for prefix in MODE_B_PREFIXES:
                    await _scan_delete(_redis_pattern(_prefixed(prefix)))
        await asyncio.wait_for(probe(), timeout=max(1.0, cfg.cache.socket_timeout_seconds * 4))
        _redis_available = True
        _redis_reason = "connected"
        _next_redis_retry = 0
        _needs_recovery_flush = False
        if _local is not None:
            _local.clear()
        return _client
    except Exception:
        _redis_failed("connection_failed")
        return None
    finally:
        _probe_running = False


async def _maintain_cache() -> None:
    while True:
        await asyncio.sleep(cfg.cache.local_cleanup_interval_seconds)
        snapshot = _memory_cache().snapshot()  # Also releases unread expired values.
        _monitor.sample(snapshot, interval_seconds=cfg.cache.local_cleanup_interval_seconds)
        if not _redis_available:
            await _ready_client()


# ----------------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------------


async def init_cache() -> None:
    """Initialize the connection pool and run the boot-time flush coordinator.

    Safe to call multiple times; second call is a no-op. Failures here MUST
    NOT propagate — silent degradation is the contract.
    """
    global _pool, _client, _maintenance_task

    if _maintenance_task is None or _maintenance_task.done():
        _memory_cache()
        _maintenance_task = asyncio.create_task(_maintain_cache(), name="cache-maintenance")

    if _client is not None:
        return

    if not cfg.cache.enabled:
        return

    try:
        _pool = aioredis.BlockingConnectionPool.from_url(
            cfg.cache.url,
            max_connections=cfg.cache.max_connections,
            timeout=cfg.cache.socket_connect_timeout_seconds,
            socket_timeout=cfg.cache.socket_timeout_seconds,
            socket_connect_timeout=cfg.cache.socket_connect_timeout_seconds,
            decode_responses=True,
        )
        _client = aioredis.Redis(connection_pool=_pool)
    except Exception:
        _redis_failed("configuration_error")
        return
    await _ready_client()


async def close_cache() -> None:
    """Tear down the connection pool. Safe to call when never initialized."""
    global _pool, _client, _local, _maintenance_task, _redis_available, _redis_reason, _next_redis_retry, _probe_running, _needs_recovery_flush
    if _maintenance_task is not None:
        _maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await _maintenance_task
        _maintenance_task = None
    _local = None
    _redis_available = False
    _redis_reason = "not_initialized"
    _next_redis_retry = 0
    _probe_running = False
    _needs_recovery_flush = False
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            logger.warning("Failed to close the cache client")
        _client = None
    if _pool is not None:
        try:
            await _pool.aclose()
        except Exception:
            logger.warning("Failed to close the cache connection pool")
        _pool = None


def _get_client() -> aioredis.Redis | None:
    """Return the live client, or None if cache is disabled / not initialized."""
    return _client


def is_enabled() -> bool:
    """Legacy Redis configuration flag; runtime status is in get_runtime_snapshot."""
    return cfg.cache.enabled and _client is not None


# ----------------------------------------------------------------------------
# Boot-time flush coordinator
# ----------------------------------------------------------------------------


async def _bootstrap_flush() -> None:
    """Flush Mode-B namespaces if Valkey was restarted since last seen ``run_id``.

    ``run_id`` is a Valkey-internal value that changes on every Valkey
    process start. By recording the last-seen value (in Valkey itself, not
    on the API host) we get restart detection that is independent of API
    restarts — restarting the API against a long-running Valkey is a no-op
    for this routine, while a Valkey upgrade flushes Mode-B exactly once
    across all API workers.

    Only one worker performs the flush per detection event. Losers wait
    for the marker to update before returning, so they don't serve Mode-B
    reads against potentially-stale data.
    """
    client = _get_client()
    if client is None:
        return

    info = await client.info("server")
    run_id = info.get("run_id") if isinstance(info, dict) else None
    if not run_id:
        logger.warning(
            "Cache bootstrap: Valkey did not report run_id; "
            "skipping flush (Mode-B keys may be stale across restart)"
        )
        return

    # Namespace the coordinator sentinels too, so multiple deployments
    # sharing a Valkey run independent flush cycles instead of fighting
    # over the same key.
    run_id_key = _prefixed(BOOTSTRAP_RUN_ID_KEY)
    lock_key = _prefixed(BOOTSTRAP_LOCK_KEY)

    seen = await client.get(run_id_key)
    if seen == run_id:
        logger.info(
            f"Cache bootstrap: run_id unchanged ({_short(run_id)}); no flush needed"
        )
        return

    got_lock = bool(
        await client.set(lock_key, "1", nx=True, ex=BOOTSTRAP_LOCK_TTL_SECONDS)
    )
    if not got_lock:
        logger.info(
            f"Cache bootstrap: another worker is flushing; "
            f"waiting for run_id={_short(run_id)} marker..."
        )
        deadline = time.monotonic() + BOOTSTRAP_LOCK_TTL_SECONDS + BOOTSTRAP_WAIT_SLOP_SECONDS
        while time.monotonic() < deadline:
            seen = await client.get(run_id_key)
            if seen == run_id:
                logger.info("Cache bootstrap: peer flush completed")
                return
            await asyncio.sleep(0.1)
        logger.warning(
            "Cache bootstrap: timed out waiting for peer flush; proceeding anyway"
        )
        return

    logger.info(
        f"Cache bootstrap: detected new Valkey run_id ({_short(run_id)}); "
        f"flushing Mode-B namespaces..."
    )
    flushed = 0
    try:
        for prefix in MODE_B_PREFIXES:
            flushed += await _scan_delete(_redis_pattern(_prefixed(prefix)))
        await client.set(run_id_key, run_id)
        _metrics.last_flush_run_id = run_id
        _metrics.last_flush_at_ms = int(time.time() * 1000)
        _metrics.last_flushed_keys = flushed
        logger.info(
            f"Cache bootstrap: flushed {flushed} Mode-B keys "
            f"across {len(MODE_B_PREFIXES)} namespaces"
        )
    finally:
        try:
            await client.delete(lock_key)
        except Exception as e:
            logger.warning(f"Failed to release the bootstrap lock {lock_key} (it expires by itself): {e}")


def _short(s: str | None) -> str:
    if not s:
        return "<none>"
    return s[:12] + ("..." if len(s) > 12 else "")


# ----------------------------------------------------------------------------
# SCAN-based delete (never KEYS — it blocks the whole Valkey)
# ----------------------------------------------------------------------------


async def _scan_delete(pattern: str, batch: int = SCAN_BATCH_SIZE) -> int:
    """Delete all keys matching ``pattern``.

    Uses SCAN + DEL in batches. Never use KEYS in production: it does an
    O(N) blocking iteration over the whole keyspace.
    """
    client = _get_client()
    if client is None:
        return 0

    cursor = 0
    deleted = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=pattern, count=batch)
        if keys:
            deleted += await client.delete(*keys)
        if cursor == 0:
            break
    return deleted


# ----------------------------------------------------------------------------
# Public read/write API
# ----------------------------------------------------------------------------


async def _get_raw(key: str, *, immutable: bool = False, record_local: bool = True) -> tuple[bool, Any]:
    """Internal read helper (no metric counting).

    Used by ``cache_get_or_fetch`` to re-check inside the singleflight lock
    without double-counting metrics.
    """
    client = await _ready_client()
    raw = None
    if client is not None:
        try:
            raw = await client.get(_prefixed(key))
        except Exception:
            _bump(_metrics.errors, _ns(key))
            _redis_failed("read_failed")
            client = None
    if client is None and immutable and _local_enabled():
        raw = _memory_cache().get(key, record=record_local)
    if raw is None:
        return False, None
    if raw == _NEGATIVE_SENTINEL:
        return True, None
    try:
        value = await asyncio.to_thread(json.loads, raw) if len(raw) > 1024 * 1024 else json.loads(raw)
        return True, value
    except (json.JSONDecodeError, TypeError):
        _bump(_metrics.errors, _ns(key))
        return False, None


async def cache_get_json(key: str, *, immutable: bool = False) -> tuple[bool, Any]:
    """Read a cached JSON value.

    Returns ``(hit, value)``. ``hit=True, value=None`` means the negative
    sentinel was found (caller should treat as "verified absent"). With
    ``immutable=True``, an unavailable Redis uses the bounded local cache.
    Mutable reads and decode errors fall through to the source.
    """
    hit, value = await _get_raw(key, immutable=immutable)
    if hit:
        _bump(_metrics.hits, _ns(key))
    else:
        _bump(_metrics.misses, _ns(key))
    return hit, value


async def cache_set_json(key: str, value: Any, ttl: int | None = None, *, immutable: bool = False) -> bool:
    """Store ``value`` as JSON under ``key`` with a jittered TTL.

    Returns True on success, False on any failure (silent degradation).
    """
    actual_ttl = _jittered_ttl(
        ttl if ttl is not None else cfg.cache.default_ttl_seconds
    )
    try:
        payload = await asyncio.to_thread(json.dumps, value) if isinstance(value, str) and len(value) > 1024 * 1024 else json.dumps(value)
    except (TypeError, ValueError):
        _bump(_metrics.errors, _ns(key))
        return False
    return await _set_payload(key, payload, actual_ttl, immutable=immutable)


async def cache_set_negative(key: str, ttl: int = 15, *, immutable: bool = False) -> bool:
    """Store the negative sentinel — encodes "verified absent"."""
    return await _set_payload(key, _NEGATIVE_SENTINEL, _jittered_ttl(ttl), immutable=immutable)


async def _set_payload(key: str, payload: str, ttl: int, *, immutable: bool) -> bool:
    client = await _ready_client()
    if client is not None:
        try:
            await client.set(_prefixed(key), payload, ex=ttl)
            _bump(_metrics.set_count, _ns(key))
            return True
        except Exception:
            _bump(_metrics.errors, _ns(key))
            _redis_failed("write_failed")
    stored = immutable and _local_enabled() and _memory_cache().set(key, payload, ttl)
    if stored:
        _bump(_metrics.set_count, _ns(key))
    return bool(stored)


async def cache_invalidate(*keys: str) -> int:
    """Delete one or more keys. Returns count of keys actually removed."""
    local_removed = _memory_cache().delete(*keys)
    for key in keys:
        _bump(_metrics.invalidate_count, _ns(key))
    client = await _ready_client()
    if client is None or not keys:
        return local_removed
    try:
        n = await client.delete(*[_prefixed(k) for k in keys])
        return int(n) + local_removed
    except Exception:
        for k in keys:
            _bump(_metrics.errors, _ns(k))
        _redis_failed("invalidation_failed")
        return local_removed


def _redis_pattern(prefix: str) -> str:
    """Literal prefix for Redis glob matching (namespaces may contain punctuation)."""
    return "".join("\\" + char if char in "\\*?[]" else char for char in prefix) + "*"


async def cache_invalidate_prefix(prefix: str) -> int:
    """Delete all keys starting with ``prefix`` (SCAN-based).

    Use with care — a wide prefix sweeps many keys. Each Mode-B
    namespace flush goes through this path on Valkey restart.
    """
    local_removed = _memory_cache().delete_prefix(prefix)
    _bump(_metrics.invalidate_count, _ns(prefix))
    if await _ready_client() is None:
        return local_removed
    try:
        n = await _scan_delete(_redis_pattern(_prefixed(prefix)))
        return n + local_removed
    except Exception:
        _bump(_metrics.errors, _ns(prefix))
        _redis_failed("invalidation_failed")
        return local_removed


# ----------------------------------------------------------------------------
# Singleflight + get-or-fetch
# ----------------------------------------------------------------------------


async def cache_get_or_fetch(
    key: str,
    fetch: Callable[[], Awaitable[Any]],
    *,
    ttl: int | None = None,
    negative_ttl: int = 15,
    treat_none_as_negative: bool = False,
    immutable: bool = False,
) -> Any:
    """Read-through cache with two-level singleflight + silent degradation.

    Behavior:

    1. Cache hit → return value (None means negative-sentinel "verified absent").
    2. Cache miss → per-key in-process lock + Valkey ``SET NX EX`` lock; one
       awaiter calls ``fetch()``, others poll the cache for the resulting fill.
    3. ``fetch()`` raises → propagate (cache is not poisoned). Caller decides.
    4. Redis down → bounded local TTL/LRU for explicitly immutable values;
       mutable values continue to fetch from their source.

    Set ``treat_none_as_negative=True`` to cache "this entity does not exist"
    under ``negative_ttl``; subsequent lookups return ``None`` without invoking
    ``fetch()`` until the negative TTL expires or the key is invalidated.
    Critical: when the entity is later created, callers MUST invalidate this
    key explicitly — otherwise the negative result hides the new entity until
    TTL expiry.
    """
    hit, value = await _get_raw(key, immutable=immutable)
    if hit:
        _bump(_metrics.hits, _ns(key))
        return value
    _bump(_metrics.misses, _ns(key))

    async with _local_singleflight(key):
        # Re-check inside the lock: another coroutine in the same worker
        # may have populated the cache while we were waiting.
        hit, value = await _get_raw(key, immutable=immutable, record_local=False)
        if hit:
            _metrics.singleflight_contention += 1
            return value

        client = await _ready_client()
        cross_lock_key = _prefixed(f"sf:lock:{key}")
        lock_token = uuid.uuid4().hex
        got_cross_lock = False
        if client is not None:
            try:
                got_cross_lock = bool(
                    await client.set(
                        cross_lock_key,
                        lock_token,
                        nx=True,
                        ex=SINGLEFLIGHT_LOCK_TTL_SECONDS,
                    )
                )
            except Exception:
                _bump(_metrics.errors, _ns(key))
                _redis_failed("lock_failed")
                client = None

        if not got_cross_lock and client is not None:
            # Another worker is fetching — poll for the cache fill rather
            # than duplicating the round-trip to the source.
            _metrics.singleflight_contention += 1
            deadline = time.monotonic() + SINGLEFLIGHT_LOCK_TTL_SECONDS
            while time.monotonic() < deadline:
                hit, value = await _get_raw(key, immutable=immutable, record_local=False)
                if hit:
                    return value
                if not _redis_available:
                    break
                await asyncio.sleep(SINGLEFLIGHT_POLL_INTERVAL)
            # Timed out waiting — fall through and fetch ourselves rather
            # than blocking the request indefinitely.

        try:
            result = await fetch()
            if result is None and treat_none_as_negative:
                await cache_set_negative(key, ttl=negative_ttl, immutable=immutable)
            else:
                await cache_set_json(key, result, ttl=ttl, immutable=immutable)
            return result
        finally:
            if got_cross_lock and client is not None:
                try:
                    # A slow fetch must not delete a newer worker's expired/reacquired lock.
                    await client.eval(
                        "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
                        1, cross_lock_key, lock_token,
                    )
                except Exception:
                    _redis_failed("lock_release_failed")


# ----------------------------------------------------------------------------
# Generation counters
# ----------------------------------------------------------------------------


async def bump_gen(scope_key: str) -> int:
    """Atomically INCR a Valkey-side generation counter.

    Used to invalidate all derived caches whose key embeds the gen, without
    enumerating the derived keys. ``scope_key`` should be ``repo:gen:{id}``,
    ``user:gen:{id}``, etc. — the prefix matters: it must be in
    ``MODE_B_PREFIXES`` so a Valkey restart resets these counters and the
    SQL ``cache_gen`` column becomes the authoritative source.

    Returns the new value. Returns 0 when cache is unavailable; callers
    should still proceed (the gen-keyed cache will simply not be hit).
    """
    client = await _ready_client()
    if client is None:
        return 0
    try:
        return int(await client.incr(_prefixed(scope_key)))
    except Exception:
        _bump(_metrics.errors, _ns(scope_key))
        _redis_failed("generation_failed")
        return 0


async def read_gen(scope_key: str) -> int:
    """Read the current generation. Returns 0 when not yet bumped or cache off."""
    client = await _ready_client()
    if client is None:
        return 0
    try:
        v = await client.get(_prefixed(scope_key))
        return int(v) if v is not None else 0
    except Exception:
        _bump(_metrics.errors, _ns(scope_key))
        _redis_failed("generation_failed")
        return 0


# ----------------------------------------------------------------------------
# Observability
# ----------------------------------------------------------------------------


def get_metrics_snapshot() -> dict:
    """Return a snapshot of in-process cache metrics for the admin endpoint."""
    return {
        "configured_enabled": cfg.cache.enabled,
        "client_initialized": _client is not None,
        "namespace": cfg.cache.namespace,
        "hits": dict(_metrics.hits),
        "misses": dict(_metrics.misses),
        "errors": dict(_metrics.errors),
        "set_count": dict(_metrics.set_count),
        "invalidate_count": dict(_metrics.invalidate_count),
        "singleflight_contention": _metrics.singleflight_contention,
        "last_flush_run_id": _metrics.last_flush_run_id,
        "last_flush_at_ms": _metrics.last_flush_at_ms,
        "last_flushed_keys": _metrics.last_flushed_keys,
    }


def reset_metrics() -> None:
    """For tests: zero out all counters."""
    _metrics.hits.clear()
    _metrics.misses.clear()
    _metrics.errors.clear()
    _metrics.set_count.clear()
    _metrics.invalidate_count.clear()
    _metrics.singleflight_contention = 0
    _memory_cache().reset_metrics()


def get_runtime_snapshot(*, force_sample: bool = False) -> dict:
    local = _memory_cache().snapshot()
    local["cleanup_interval_seconds"] = cfg.cache.local_cleanup_interval_seconds
    available = bool(cfg.cache.enabled and _client is not None and _redis_available)
    fallback = _local_enabled()
    mode = "redis" if available else "memory" if fallback else "disabled"
    identity = _monitor.identity()
    sample = _monitor.sample(local, interval_seconds=cfg.cache.local_cleanup_interval_seconds, force=force_sample)
    return {
        "backend": {
            **identity,
            "mode": mode,
            "redis_configured": cfg.cache.enabled,
            "redis_available": available,
            "fallback_enabled": fallback,
            "reason": _redis_reason if cfg.cache.enabled else "redis_not_configured",
            "sampled_at_ms": int(time.time() * 1000),
            "next_retry_at_ms": int((time.time() + max(0, _next_redis_retry - time.monotonic())) * 1000)
                if cfg.cache.enabled and _client is not None and not available else None,
        },
        "local": local,
        "singleflight": {"active_keys": len(_local_locks), "max_keys": 4096},
        **sample,
    }


def clear_process_cache() -> int:
    count = _memory_cache().clear()
    get_runtime_snapshot(force_sample=True)
    return count


async def get_memory_info() -> dict:
    """Probe Valkey for memory + eviction info (for the admin endpoint)."""
    client = await _ready_client()
    if client is None:
        return {"available": False, "reason": "client not initialized"}
    try:
        info = await client.info("memory")
        statistics = await client.info("stats")
    except Exception:
        _redis_failed("memory_probe_failed")
        return {"available": False, "reason": "memory_probe_failed"}
    if not isinstance(info, dict):
        return {"available": False, "reason": "unexpected INFO response"}
    return {
        "available": True,
        "used_memory": info.get("used_memory"),
        "used_memory_human": info.get("used_memory_human"),
        "maxmemory": info.get("maxmemory"),
        "maxmemory_human": info.get("maxmemory_human"),
        "maxmemory_policy": info.get("maxmemory_policy"),
        "evicted_keys": statistics.get("evicted_keys") if isinstance(statistics, dict) else None,
    }
