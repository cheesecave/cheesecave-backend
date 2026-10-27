# Cache layer

CheeseCave uses one cache interface for Redis/Valkey and bounded process-local
storage. A configured, reachable Redis is preferred. When Redis is unavailable
or disabled, explicitly immutable values can use the local fallback. A cache
failure must not turn a successful source read into an API error.

Implementation lives in [`cache.py`](../../src/kohakuhub/cache.py),
[`cache_memory.py`](../../src/kohakuhub/cache_memory.py), and
[`cache_monitor.py`](../../src/kohakuhub/cache_monitor.py). The Python package
name remains `kohakuhub` for compatibility.

## Backend selection

```text
Cache read
  -> configured Redis is available: read Redis
  -> otherwise, immutable=True and local fallback enabled: read local storage
  -> miss or caching unavailable: fetch from the source
```

The local store is a fallback, not a second copy of every Redis value. Successful
Redis reads and writes use Redis. A connection failure switches subsequent
operations to fallback without repeatedly waiting on a broken connection. One
probe can attempt recovery after the retry interval; a background maintenance
task also checks recovery while Redis is unavailable. Successful recovery clears
the local store and resumes Redis use. These state and cache decisions are per
process.

An explicit Redis URL opts into Redis when supplied through the environment
without an explicit `CACHE_ENABLED` value. The application does not discover or
scan unrelated Redis endpoints. With no Redis configuration, immutable caching
works locally by default.

The default `immutable=False` never uses local value storage. It can use Redis,
but falls through to the source when Redis is unavailable. Local caches have no
cross-process invalidation channel, so mutable values must not opt into local
fallback merely to improve their hit rate.

## Current integrations

These three business caches use the shared interface with `immutable=True` and
a one-hour TTL, subject to the configured TTL jitter:

| Namespace | Key identity | Cached value |
| --- | --- | --- |
| `commit-count` | LakeFS repository and resolved immutable commit head | Total commits after a complete history walk |
| `last-commit` | LakeFS repository, immutable commit and file path | Last commit touching the file, or a verified empty result |
| `manifest` | LakeFS repository and immutable commit | Name-only siblings JSON |

Key components are encoded as a JSON array after the namespace so punctuation
inside names or paths cannot merge different identities. Every read still checks
the reader's permissions before returning repository data. Branch names are
resolved to immutable heads before using persistent cache entries.

Incomplete history walks, lookup failures and timeouts are not cached. File
last-commit lookups retain their existing deadline, including cache coordination
and waiting for a LakeFS slot. Directory lookups are not added. In-flight file
and manifest tasks are shared and shielded from cancellation by one reader;
completed tasks are removed from their coordination dictionaries.

Manifest requests with blob metadata still read live `File` rows and share only
the active build. They are not retained in either backend: a file's blob ID can
be recorded after LakeFS creates the commit. Large name-only manifests remain
valid responses even when they exceed the local cache budget; they simply are
not retained. JSON construction and encoding/decoding of large cached strings
are moved off the event loop.

Other existing caches, such as upstream repository-source discovery, are not
automatically migrated or reported as entries in these namespaces.

## Configuration

Environment settings use `CHEESE_CAVE_` first, with `KOHAKU_HUB_` as the legacy
fallback. The table omits that prefix. TOML settings belong under `[cache]`.
Apply configuration changes by restarting the API processes.

| Environment suffix | TOML field | Default |
| --- | --- | --- |
| `CACHE_ENABLED` | `enabled` | `false`; controls Redis only |
| `CACHE_URL` | `url` | `redis://localhost:6379/0` |
| `CACHE_NAMESPACE` | `namespace` | `kh` |
| `CACHE_DEFAULT_TTL` | `default_ttl_seconds` | `300` seconds |
| `CACHE_JITTER_FRACTION` | `jitter_fraction` | `0.15` (approximately ±15%) |
| `CACHE_MAX_CONNECTIONS` | `max_connections` | `50` per process |
| `CACHE_SOCKET_TIMEOUT` | `socket_timeout_seconds` | `0.5` seconds |
| `CACHE_SOCKET_CONNECT_TIMEOUT` | `socket_connect_timeout_seconds` | `0.5` seconds |
| `CACHE_LOCAL_ENABLED` | `local_enabled` | `true` |
| `CACHE_LOCAL_MAX_ENTRIES` | `local_max_entries` | `10000` per process |
| `CACHE_LOCAL_MAX_BYTES` | `local_max_bytes` | `67108864` (64 MiB) per process |
| `CACHE_LOCAL_CLEANUP_INTERVAL` | `local_cleanup_interval_seconds` | `15` seconds |
| `CACHE_REDIS_RETRY_INTERVAL` | `redis_retry_interval_seconds` | `10` seconds |

For example, a Redis URL without credentials can be configured as:

```text
CHEESE_CAVE_CACHE_URL=redis://cache:6379/0
CHEESE_CAVE_CACHE_NAMESPACE=cheesecave
```

Setting `CHEESE_CAVE_CACHE_ENABLED=false` overrides the URL's implicit enablement
and leaves local fallback enabled. To bypass both backends, also set
`CHEESE_CAVE_CACHE_LOCAL_ENABLED=false`. Zero local entry or byte limits disable
local storage as well. Local limits must be nonnegative; cleanup and retry
intervals must be at least one second. Supplying only a URL in TOML does not
implicitly set `enabled=true`.

## Memory bounds and singleflight

Hit/miss counters count the initial cache lookup once. Internal singleflight
rechecks and peer polling do not add extra reads; coalesced requests are also
reported through the singleflight contention counter.

The local store has both an entry limit and an accounted-byte limit. Each entry
has a monotonic expiry time. Reads discard expired entries, capacity pressure
first reclaims expired entries and then evicts least-recently-used entries, and
the maintenance task sweeps expired entries even when there are no requests.
There are no per-entry timers or tasks that can retain evicted values. An entry
larger than the total byte budget is rejected without emptying useful entries.

Accounted bytes estimate retained Python key/value strings, the entry and its
scalar fields, plus an allowance for the dictionary node. This is not an exact
heap measurement or a limit on the process's resident memory. Limits apply
separately to each API process; adding workers multiplies the possible retained
local cache data. Redis memory limits and eviction policy belong to the Redis
server configuration and are not changed by this interface.

Cached values are serialized; decoded dictionaries and lists cannot modify the
stored value by reference. Cached value writes have a TTL, and the three integrations
above explicitly use 3600 seconds rather than the generic 300-second default.

`cache_get_or_fetch` coalesces requests for the same key with a process-local
lock, and with a short Redis lock when Redis is available. Active local lock
keys are capped at 4096 and removed after their last participant exits. At the
cap, new keys bypass local coordination instead of growing the lock registry.
Redis lock acquisition failures fall back immediately rather than polling a
dead connection. Lock release checks its ownership token so a slow reader
cannot delete a newer reader's lock. A source failure is propagated and never
stored. Namespace counters retain at most 64 named buckets plus an overflow
bucket, preventing metrics from becoming an unbounded key registry.

## Immutable and reserved mutable contracts

Immutable, content-addressed values are Mode A: their key contains the version,
so independent processes may safely retain the same answer. The explicit
`immutable=True` argument is the caller's assertion of this property; the cache
does not infer it from a namespace name.

Mode B helpers remain reserved infrastructure for future mutable integrations:

- `read_gen` and `bump_gen` access Redis generation counters and return zero
  when Redis is unavailable. They do not create local generation counters.
- There is currently no database `cache_gen` column or transaction-level
  generation maintenance in the repository/user/organization models. A counter
  API alone does not establish a complete generation-based consistency model.
- `cache_invalidate` and `cache_invalidate_prefix` clear matching local entries
  and attempt Redis invalidation. A future mutable integration must handle
  every write path, with invalidation after the authoritative database commit.

The existing Redis bootstrap coordinator checks `INFO server.run_id` during
initialization/reconnection and clears the reserved `MODE_B_PREFIXES` when it
detects a new server run. Recovery after an observed connection failure also
clears these reserved prefixes, because invalidations may have been missed
without a Redis restart. Immutable namespaces are excluded from these flushes.
This machinery is not a guarantee of cross-worker real-time mutable coherence;
future mutable consumers need explicit write, recovery and multi-worker tests.
The current three integrations rely only on immutable identities.

Negative cache entries distinguish a cache miss from a verified absent value:
`(False, None)` is a miss and `(True, None)` is a cached answer. Negative entries
have a short TTL (15 seconds by default). Mutable absence must not be marked
immutable, and an entity creation must invalidate any negative key that could
hide it. A verified empty history at an immutable commit remains an immutable
answer.

## Admin observations and actions

`GET /admin/api/cache/stats` requires Admin authentication and returns:

- `metrics`: namespace hit/miss/error/write/invalidation counters, singleflight
  contention and bootstrap-flush metadata.
- `backend`: active mode (`redis`, `memory`, or `disabled`), Redis configuration
  and availability, fallback state, status reason, retry time and process identity.
- `local`: current and peak entries/accounted bytes, limits, hits/misses, writes,
  evictions, expirations, rejected oversized entries, clears and namespace totals.
- `singleflight`: active local lock keys and their cap.
- `process`: current resident memory (RSS/working set) and its measurement source;
  unsupported or failed measurements are explicitly unknown.
- `history`: up to 120 observations of local entries, accounted bytes and process
  RSS. Normal observations use the cleanup interval; clearing the store also
  records an observation. The history is bounded and does not survive restart.
- `memory`: Redis server memory, configured maximum, eviction policy and evicted
  keys when reachable. This describes the server, including other clients and
  namespaces, not just this application's entries.

All counters, local cache measurements and history belong to the responding
process. `instance_id`, PID and `scope: "process"` identify that scope. Requests
balanced across workers can return different observations; the Admin panel
must not join them into one process history or describe them as a cluster total.

`POST /admin/api/cache/metrics/reset` resets in-process counters and local peaks
without deleting cached values. It works in local-only mode and accepts an
optional `{"instance_id": "..."}` body. The Admin panel supplies that identity.

`POST /admin/api/cache/local/clear` requires an `instance_id` body and clears only
the matching process's local entries. A different receiving worker returns 409;
refresh the panel and retry. This action does not flush Redis or other workers,
and active requests can repopulate the cache. Neither action resets business
data.

Entry counts and accounted bytes should plateau at their configured bounds;
expiry and eviction counts help explain changes. RSS includes every part of the
application and can stay elevated after a cache clear because of other workloads
or allocator behavior. These observations help investigate memory growth; they
do not establish or rule out a memory leak by themselves.

## Adding a cached read

1. Prove whether the value is immutable. Use an authoritative version in the
   key and check permissions before returning cached repository data.
2. Use a collision-free key and a distinct namespace. Call
   `cache_get_or_fetch(key, fetch, immutable=True, ttl=...)` for immutable data;
   do not introduce another persistent dictionary or call Redis directly.
3. Test cache hits, identity isolation, source failures, bounded local storage,
   invalidation, concurrent requests and relevant cancellation behavior. Verify
   both Redis use and no-Redis behavior; local-only tests must not require Redis.
4. For mutable data, leave `immutable=False` and implement the required database
   and invalidation protocol before enabling caching. Do not treat the reserved
   generation helpers as a finished protocol.

The cache foundation tests are in
[`test/kohakuhub/test_cache.py`](../../test/kohakuhub/test_cache.py). Existing
commit-history, tree and HF utility tests cover the three business integrations.
