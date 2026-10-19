"""What the admin Overview shows (issue cheesecave-admin#20): one cached, section-wise answer to
"is it running, what needs me, what is happening".

Every section is computed on its own, cached for its own time (``SECTION_TTL``), and fails on its own: a
section that raises reports ``{"error": ...}`` and the others are still returned. Counting and bucketing
happen in SQL (portable between SQLite and PostgreSQL), never row by row in Python.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from peewee import SQL, Cast, fn
from starlette.concurrency import run_in_threadpool

from kohakuhub import usage
from kohakuhub.api.admin.utils.health import overall_status, run_all_probes
from kohakuhub.api.misc import build_identity
from kohakuhub.db import BackgroundWorker, Commit, File, LFSObjectHistory, Repository, User
from kohakuhub.logger import get_logger
from kohakuhub.tasks import WORKER_INACTIVE_AFTER, worker_status

logger = get_logger("ADMIN")

SECTIONS = ("health", "workers", "queue", "build", "attention", "counts", "recent", "activity", "top")
RANGES = {"24h": ("hour", 24), "7d": ("day", 7), "30d": ("day", 30)}
SECTION_TTL = {  # seconds
    "health": 15,  # the probes touch PostgreSQL, S3 and LakeFS: many tabs must not multiply them
    "workers": 5,
    "stats": 5,  # the task statistics behind "queue" and "attention"
    "quota": 60,
    "lfs": 60,
    "counts": 30,
    "recent": 30,
    "activity": 300,
    "top": 300,
}
RECENT_LIMIT = 10
TOP_LIMIT = 5
MESSAGE_LIMIT = 60
HINT_NAMES = 3
SEVERITY_RANK = {"danger": 0, "warning": 1, "info": 2}


def _now() -> datetime:
    """Naive UTC, like every datetime the models return."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


# ---- cache --------------------------------------------------------------------------------------------


class Cache:
    """Values kept for a ttl; concurrent requests for one key share one computation (single flight).

    In process on purpose: each worker keeps its own copy, which is fine for a handful of administrators.
    ponytail: move to Valkey only if several workers' duplicate probes ever show up as load.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._entries: dict[Any, tuple[float, Any, str]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}

    def clear(self) -> None:
        self._entries.clear()

    def _fresh(self, key, ttl):
        entry = self._entries.get(key)
        if entry is not None and self.clock() - entry[0] < ttl:
            return entry[1], entry[2], True, int(self.clock() - entry[0])
        return None

    async def get(self, key, ttl: float, compute: Callable[[], Awaitable[Any]]):
        """``(value, computed_at_iso, hit, age_seconds)``. A failed computation is not cached."""
        if (fresh := self._fresh(key, ttl)) is not None:
            return fresh
        async with self._locks.setdefault(key, asyncio.Lock()):
            if (fresh := self._fresh(key, ttl)) is not None:
                return fresh  # another request computed it while this one waited
            value = await compute()
            at = _iso(_now())
            self._entries[key] = (self.clock(), value, at)
            return value, at, False, 0


cache = Cache()


# ---- sources (blocking database work runs in the thread pool) ------------------------------------------


def _bucket_keys(range_: str, now: datetime) -> tuple[str, list[str], list[str], datetime]:
    """``(bucket, keys, labels, since)``: the SQL group keys oldest first, their labels, and where the window starts."""
    bucket, size = RANGES[range_]
    if bucket == "hour":
        end = now.replace(minute=0, second=0, microsecond=0)
        starts = [end - timedelta(hours=size - 1 - i) for i in range(size)]
        return bucket, [s.strftime("%Y-%m-%d %H") for s in starts], [s.strftime("%H:00") for s in starts], starts[0]
    end = datetime.combine(now.date(), datetime.min.time())
    starts = [end - timedelta(days=size - 1 - i) for i in range(size)]
    return bucket, [s.strftime("%Y-%m-%d") for s in starts], [s.strftime("%m-%d") for s in starts], starts[0]


def _series(field, range_: str, now: datetime, *where) -> list[int]:
    """Rows created per bucket, zero-filled, grouped by the leading characters of the stored timestamp in SQL."""
    bucket, keys, _, since = _bucket_keys(range_, now)
    key = fn.SUBSTR(Cast(field, "TEXT"), 1, 13 if bucket == "hour" else 10).coerce(False)  # text, not the field's datetime
    rows = (
        field.model.select(key.alias("k"), fn.COUNT(1).alias("n"))
        .where(field >= since, *where)
        .group_by(SQL("1"))
        .tuples()
    )
    found = dict(rows)
    return [int(found.get(k, 0)) for k in keys]


def counts(range_: str, now: datetime) -> dict:
    """Totals as ``/stats/detailed`` has them, plus rows created in the range and per bucket."""
    users, repos, commits = User.created_at, Repository.created_at, Commit.created_at
    not_org = User.is_org == False  # noqa: E712 (a SQL expression)
    user_series = _series(users, range_, now, not_org)
    repo_series = _series(repos, range_, now)
    commit_series = _series(commits, range_, now)
    used = usage.users_usage()
    lfs_objects, lfs_size = LFSObjectHistory.select(fn.COUNT(1), fn.COALESCE(fn.SUM(LFSObjectHistory.size), 0)).scalar(as_tuple=True)
    return {
        "users": {"total": User.select().where(not_org).count(), "delta": sum(user_series), "series": user_series},
        "orgs": User.select().where(User.is_org == True).count(),  # noqa: E712
        "repos": {
            "total": Repository.select().count(),
            "private": Repository.select().where(Repository.private == True).count(),  # noqa: E712
            "delta": sum(repo_series),
            "series": repo_series,
        },
        "commits": {"total": Commit.select().count(), "delta": sum(commit_series), "series": commit_series},
        "storage": {"total": used["private"] + used["public"], "private": used["private"], "public": used["public"]},
        "lfs": {"objects": int(lfs_objects), "size": int(lfs_size)},
    }


def activity(range_: str, now: datetime) -> dict:
    bucket, _, labels, _ = _bucket_keys(range_, now)
    return {
        "range": range_,
        "bucket": bucket,
        "labels": labels,
        "commits": _series(Commit.created_at, range_, now),
        "repos": _series(Repository.created_at, range_, now),
        "users": _series(User.created_at, range_, now, User.is_org == False),  # noqa: E712
    }


def _shorten(message: str) -> str:
    return message if len(message) <= MESSAGE_LIMIT else message[: MESSAGE_LIMIT - 1] + "…"


def recent() -> dict:
    """The newest accounts, repositories and commits, mixed, newest first."""
    items = []
    for user in User.select().where(User.is_org == False).order_by(User.created_at.desc()).limit(RECENT_LIMIT):  # noqa: E712
        items.append((user.created_at, f"user:{user.id}", "user", user.username, "joined", "/users"))
    for repo in Repository.select().order_by(Repository.created_at.desc()).limit(RECENT_LIMIT):
        text = f"created {repo.repo_type} {repo.full_id}"
        items.append((repo.created_at, f"repo:{repo.id}", "repo", repo.namespace, text, "/repositories"))
    for commit in Commit.select(Commit, Repository).join(Repository).order_by(Commit.created_at.desc()).limit(RECENT_LIMIT):
        text = f"committed “{_shorten(commit.message)}” to {commit.repository.full_id}"
        items.append((commit.created_at, f"commit:{commit.id}", "commit", commit.username, text, "/commits"))
    items.sort(key=lambda item: item[0], reverse=True)
    return {
        "items": [
            {"id": i, "kind": kind, "actor": actor, "text": text, "at": _iso(at), "link": link}
            for at, i, kind, actor, text, link in items[:RECENT_LIMIT]
        ]
    }


def top() -> dict:
    """The repositories with the most commits, and with the most data on the main branch."""
    R = Repository
    by_commits = (
        R.select(R.repo_type, R.full_id, fn.COUNT(Commit.id).alias("n"))
        .join(Commit, on=(Commit.repository == R.id))
        .group_by(R.id)
        .order_by(fn.COUNT(Commit.id).desc(), R.id)
        .limit(TOP_LIMIT)
        .tuples()
    )
    by_size = (
        R.select(R.repo_type, R.full_id, fn.SUM(File.size).alias("n"))
        .join(File, on=(File.repository == R.id))
        .where(File.branch == "main", File.is_deleted == False)  # noqa: E712
        .group_by(R.id)
        .order_by(fn.SUM(File.size).desc(), R.id)
        .limit(TOP_LIMIT)
        .tuples()
    )
    return {
        "by_commits": [{"repo_type": t, "repo_full_id": f, "commits": int(n)} for t, f, n in by_commits],
        "by_size": [{"repo_type": t, "repo_full_id": f, "size_bytes": int(n)} for t, f, n in by_size],
    }


def workers(now: datetime) -> dict:
    """Workers by status, with the same left-out rule as the Tasks page (stopped, and silent for a day)."""
    W = BackgroundWorker
    found = {"online": 0, "draining": 0, "lost": 0}
    for row in W.select().where((W.state != "stopped") & (W.last_heartbeat_at >= now - WORKER_INACTIVE_AFTER)):
        found[worker_status(row, now)] += 1
    return found


def task_stats(now: datetime) -> dict:
    """The Tasks page's own 24 hour statistics (one place decides what counts as failed or backed up)."""
    from kohakuhub.api.admin.routers.tasks import build_task_stats  # the routers import this module

    return build_task_stats("24h", now)


def queue_from(stats: dict) -> dict:
    """Queue numbers and 24 hourly buckets from the 24 hour task statistics (96 quarter-hour buckets)."""
    backlog, summary, series = stats["backlog"], stats["summary"], stats["series"]
    step = len(series) // 24
    hourly = [
        {
            "ok": sum(b["succeeded"] for b in series[i * step : (i + 1) * step]),
            "failed": sum(b["failed"] for b in series[i * step : (i + 1) * step]),
        }
        for i in range(24)
    ]
    oldest = backlog["oldest_due_seconds"]
    return {
        "queued": backlog["due"] + backlog["scheduled"],
        "running": backlog["running"],
        "succeeded_24h": summary["succeeded"],
        "failed_24h": summary["failed"],
        "oldest_queued_age_s": None if oldest is None else int(oldest),
        "hourly": hourly,
    }


async def quota_overview() -> dict:
    from kohakuhub.api.admin.routers import quota  # the routers import this module

    return await quota.get_quota_overview(_admin=True)


def lfs_status() -> dict:
    from kohakuhub import storage_cleanup

    return storage_cleanup.lfs_reconciliation_status()


# ---- what needs attention (pure) -----------------------------------------------------------------------


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _wait(seconds: float) -> str:
    return f"{int(seconds)} s" if seconds < 60 else f"{int(seconds // 60)} min"


def _item(id_, severity, title, count, hint, path, query, label) -> dict:
    return {"id": id_, "severity": severity, "title": title, "count": count, "hint": hint, "link": {"path": path, "query": query, "label": label}}


def derive_attention(*, health, workers, stats, quota, lfs) -> list[dict]:
    """The things an operator should look at, worst first. A source that is None (it could not be read) adds nothing."""
    items = []
    if health:
        down = [d["name"] for d in health["dependencies"] if d["status"] == "down"]
        if down:
            title = f"{_plural(len(down), 'dependency', 'dependencies')} {'is' if len(down) == 1 else 'are'} down"
            items.append(_item("dependency", "danger", title, len(down), ", ".join(down), "/health", {}, "Health"))
    if workers and workers["lost"]:
        title = f"{_plural(workers['lost'], 'worker', 'workers')} lost {'its' if workers['lost'] == 1 else 'their'} heartbeat"
        items.append(_item("worker", "danger", title, workers["lost"], None, "/tasks", {}, "Workers"))
    if stats:
        failed = stats["summary"]["failed"]
        if failed:
            critical = stats["summary"]["failure_rate"] >= stats["thresholds"]["failure_rate_critical"]
            kinds = ", ".join(k["kind"] for k in stats["kinds"] if k["failed"]) or None
            title = f"{_plural(failed, 'task', 'tasks')} failed in the last 24 hours"
            items.append(_item("tasks", "danger" if critical else "warning", title, failed, kinds, "/tasks", {"status": "failed", "window": "24h"}, "Failed tasks"))
        oldest, thresholds = stats["backlog"]["oldest_due_seconds"], stats["thresholds"]
        if oldest is not None and oldest >= thresholds["backlog_warn_seconds"]:
            severity = "danger" if oldest >= thresholds["backlog_critical_seconds"] else "warning"
            items.append(_item("backlog", severity, "The task queue is backing up", stats["backlog"]["due"], f"the oldest has waited {_wait(oldest)}", "/tasks", {"status": "queued"}, "Queued tasks"))
    if quota:
        users = sorted(quota["users_over_quota"], key=lambda u: max(u["private_percentage"], u["public_percentage"]), reverse=True)
        repos = sorted(quota["repos_over_quota"], key=lambda r: r["percentage"], reverse=True)
        if users or repos:
            parts = ([_plural(len(users), "user", "users")] if users else []) + ([_plural(len(repos), "repository", "repositories")] if repos else [])
            total = len(users) + len(repos)
            title = f"{' and '.join(parts)} {'is' if total == 1 else 'are'} over their storage quota"
            if users:
                named = [f"{u['username']} {max(u['private_percentage'], u['public_percentage']):g}%" for u in users[:HINT_NAMES]]
                hint = ", ".join(named) + (f", +{len(users) - HINT_NAMES} more" if len(users) > HINT_NAMES else "")
                link = ("/users", {"filter": "over_quota"}, "Users over quota")
            else:
                named = [f"{r['full_id']} {r['percentage']:g}%" for r in repos[:HINT_NAMES]]
                hint = ", ".join(named) + (f", +{len(repos) - HINT_NAMES} more" if len(repos) > HINT_NAMES else "")
                link = ("/QuotaOverview", {}, "Quota Overview")
            items.append(_item("quota", "warning", title, total, hint, *link))
    if lfs and lfs["task"] and lfs["task"]["status"] == "failed":
        items.append(_item("lfs_drift", "info", "The last LFS reconciliation failed", 1, None, "/storage", {"tab": "lfs"}, "Storage"))
    items.sort(key=lambda i: (SEVERITY_RANK[i["severity"]], i["id"]))
    return items


# ---- sections ------------------------------------------------------------------------------------------


async def _probe_health() -> dict:
    probes = await run_all_probes()
    return {
        "status": overall_status(probes),
        "checked_at_ms": int(time.time() * 1000),
        "dependencies": [{k: p.get(k) for k in ("name", "status", "latency_ms", "detail")} for p in probes],
    }


def _threaded(fn_name: str, *args) -> Callable[[], Awaitable[Any]]:
    """A source that does blocking work: looked up by name at call time (so tests can swap it), run in the pool."""
    return lambda: run_in_threadpool(globals()[fn_name], *args)


async def _health():
    return await cache.get("health", SECTION_TTL["health"], _probe_health)


async def _workers():
    return await cache.get("workers", SECTION_TTL["workers"], _threaded("workers", _now()))


async def _stats():
    return await cache.get("stats", SECTION_TTL["stats"], _threaded("task_stats", _now()))


async def _quota():
    return await cache.get("quota", SECTION_TTL["quota"], quota_overview)


async def _lfs():
    return await cache.get("lfs", SECTION_TTL["lfs"], _threaded("lfs_status"))


async def _queue(range_):
    value, at, hit, age = await _stats()
    return queue_from(value), at, hit, age


async def _build(range_):
    async def compute():
        sha = build_identity()["git_sha"]
        return {"backend": sha[:7] if sha else "unknown"}

    return await cache.get("build", float("inf"), compute)


async def _attention(range_):
    async def quietly(source):
        try:
            return await source()
        except Exception as error:  # one unreadable source must not hide the others' findings
            logger.warning(f"Overview attention source failed: {error}")
            return None

    got = await asyncio.gather(*(quietly(s) for s in (_health, _workers, _stats, _quota, _lfs)))
    read = [g for g in got if g is not None]
    health, workers_, stats, quota, lfs = (g[0] if g else None for g in got)
    items = derive_attention(health=health, workers=workers_, stats=stats, quota=quota, lfs=lfs)
    return {"items": items}, _iso(_now()), all(g[2] for g in read), max((g[3] for g in read), default=0)


def _ranged(name, builder):
    async def source(range_):
        return await cache.get((name, range_), SECTION_TTL[name], _threaded(builder, range_, _now()))

    return source


def _unranged(name, builder):
    async def source(range_):
        return await cache.get(name, SECTION_TTL[name], _threaded(builder))

    return source


async def _health_section(range_):
    return await _health()


async def _workers_section(range_):
    return await _workers()


_SOURCES = {
    "health": _health_section,
    "workers": _workers_section,
    "queue": _queue,
    "build": _build,
    "attention": _attention,
    "counts": _ranged("counts", "counts"),
    "activity": _ranged("activity", "activity"),
    "recent": _unranged("recent", "recent"),
    "top": _unranged("top", "top"),
}


async def _section(name: str, range_: str) -> dict:
    try:
        value, at, hit, age = await _SOURCES[name](range_)
    except Exception as error:
        logger.warning(f"Overview section {name} failed: {error}")
        return {"generated_at": _iso(_now()), "error": {"code": "section_failed", "message": str(error)[:200]}}
    return {"generated_at": at, "cache": {"hit": hit, "age_s": age}, **value}


async def collect(sections, range_: str) -> dict:
    """``{name: section}`` for the known names asked for (in the order asked, once each); unknown names are ignored."""
    wanted = [name for name in dict.fromkeys(sections) if name in _SOURCES]
    return dict(zip(wanted, await asyncio.gather(*(_section(name, range_) for name in wanted))))
