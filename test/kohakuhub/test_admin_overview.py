"""The Overview sections: real SQL for what the database decides (counts, buckets, recent, top, workers, tasks)
on SQLite and PostgreSQL; plain unit tests for the cache, the attention rules and the orchestration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from kohakuhub import admin_overview as ov
from kohakuhub.db import BackgroundTask, BackgroundWorker, LFSObjectHistory
from test.kohakuhub.support.factories import make_commit, make_file, make_org, make_repo, make_user

NOW = datetime(2026, 10, 19, 14, 30, 5)  # naive UTC, like every datetime the models return


def ago(**delta):
    return NOW - timedelta(**delta)


# ---- counts and buckets -------------------------------------------------------------------------------


def seed_activity():
    alice = make_user("alice", created_at=ago(hours=2))
    make_user("bob", created_at=ago(days=3))
    make_user("old", created_at=ago(days=40))
    make_org("acme", admin=alice, created_at=ago(hours=1))  # organizations are not users
    r1 = make_repo(alice, "m1", created_at=ago(minutes=20), used_bytes=100)
    r2 = make_repo(alice, "d1", repo_type="dataset", private=True, created_at=ago(days=2), used_bytes=40)
    make_repo(alice, "ancient", created_at=ago(days=60), used_bytes=1)
    make_commit(r1, "c1", created_at=ago(minutes=5))
    make_commit(r1, "c2", created_at=ago(hours=3))
    make_commit(r2, "c3", created_at=ago(days=2, hours=1))
    make_commit(r2, "c4", created_at=ago(days=20))
    return alice, r1, r2


def test_counts_for_seven_days_have_totals_deltas_and_zero_filled_daily_series(db_dual):
    _, r1, _ = seed_activity()
    LFSObjectHistory.create(repository=r1, path_in_repo="a", sha256="x" * 64, size=7, commit_id="c1")
    LFSObjectHistory.create(repository=r1, path_in_repo="b", sha256="y" * 64, size=5, commit_id="c1")

    c = ov.counts("7d", NOW)

    assert c["users"]["total"] == 3  # alice, bob, old; the organization is not a user
    assert c["orgs"] == 1
    assert c["repos"] == {"total": 3, "private": 1, "delta": 2, "series": [0, 0, 0, 0, 1, 0, 1]}
    assert c["users"]["delta"] == 2 and len(c["users"]["series"]) == 7 and c["users"]["series"][-1] == 1
    assert c["commits"]["delta"] == 3
    assert c["commits"]["series"] == [0, 0, 0, 0, 1, 0, 2]
    assert sum(c["commits"]["series"]) == c["commits"]["delta"]
    assert c["storage"] == {"total": 140 + 1, "private": 40, "public": 101}
    assert c["lfs"] == {"objects": 2, "size": 12}


def test_counts_for_a_day_use_hourly_buckets_ending_at_the_current_hour(db_dual):
    seed_activity()
    c = ov.counts("24h", NOW)
    assert len(c["commits"]["series"]) == 24
    assert c["commits"]["series"][-1] == 1  # 14:25 is in the 14:00 bucket
    assert c["commits"]["series"][-4] == 1  # 11:30 is in the 11:00 bucket
    assert c["commits"]["delta"] == 2
    assert c["repos"]["series"][-1] == 1 and c["repos"]["delta"] == 1
    assert c["users"]["delta"] == 1  # alice, 2 hours ago


def test_counts_of_an_empty_site_are_zeros_not_errors(db_dual):
    c = ov.counts("30d", NOW)
    assert c["users"] == {"total": 0, "delta": 0, "series": [0] * 30}
    assert c["storage"] == {"total": 0, "private": 0, "public": 0}
    assert c["lfs"] == {"objects": 0, "size": 0}


def test_activity_labels_and_series_per_bucket(db_dual):
    seed_activity()
    day = ov.activity("7d", NOW)
    assert day["bucket"] == "day" and day["range"] == "7d"
    assert day["labels"][-1] == "10-19" and day["labels"][0] == "10-13" and len(day["labels"]) == 7
    assert day["commits"] == [0, 0, 0, 0, 1, 0, 2]
    assert day["repos"] == [0, 0, 0, 0, 1, 0, 1]
    assert day["users"][-1] == 1 and day["users"][-4] == 1

    hour = ov.activity("24h", NOW)
    assert hour["bucket"] == "hour" and hour["labels"][-1] == "14:00" and hour["labels"][0] == "15:00"
    assert hour["commits"][-1] == 1 and hour["commits"][-4] == 1 and sum(hour["commits"]) == 2


# ---- recent and top -----------------------------------------------------------------------------------


def test_recent_mixes_users_repositories_and_commits_newest_first_and_stops_at_ten(db_dual):
    alice, r1, _ = seed_activity()
    make_commit(r1, "long", created_at=ago(minutes=1), message="x" * 100, author=alice)
    items = ov.recent()["items"]
    assert len(items) == 10
    assert [i["at"] for i in items] == sorted((i["at"] for i in items), reverse=True)
    first = items[0]
    assert first["kind"] == "commit" and first["actor"] == "alice" and first["link"] == "/commits"
    assert first["text"] == "committed “" + "x" * 59 + "…” to alice/m1"
    assert first["at"].endswith("Z") and first["id"].startswith("commit:")
    kinds = {i["kind"] for i in items}
    assert {"user", "repo", "commit"} <= kinds
    joined = next(i for i in items if i["kind"] == "user")
    assert joined["text"] == "joined" and joined["link"] == "/users" and joined["id"].startswith("user:")
    created = next(i for i in items if i["id"] == f"repo:{r1.id}")
    assert created["text"] == "created model alice/m1" and created["actor"] == "alice" and created["link"] == "/repositories"
    assert all(i["actor"] != "acme" for i in items if i["kind"] == "user")  # organizations do not "join"


def test_top_repositories_by_commits_and_by_size_use_the_main_branch_files_that_still_exist(db_dual):
    alice, r1, r2 = seed_activity()
    make_commit(r1, "c5")
    make_file(r1, "a.bin", "a" * 64, size=900)
    make_file(r1, "gone.bin", "b" * 64, size=5000, is_deleted=True)
    make_file(r1, "side.bin", "c" * 64, size=7000, branch="dev")
    make_file(r2, "b.bin", "d" * 64, size=1000)
    t = ov.top()
    assert t["by_commits"][0] == {"repo_type": "model", "repo_full_id": "alice/m1", "commits": 3}
    assert [r["repo_full_id"] for r in t["by_commits"]] == ["alice/m1", "alice/d1"]
    assert t["by_size"] == [
        {"repo_type": "dataset", "repo_full_id": "alice/d1", "size_bytes": 1000},
        {"repo_type": "model", "repo_full_id": "alice/m1", "size_bytes": 900},
    ]


# ---- workers and tasks --------------------------------------------------------------------------------


def worker(wid, state, beat, **kw):
    return BackgroundWorker.create(
        id=wid, name=wid, hostname=wid, pid=1, concurrency=4, state=state, started_at=ago(days=3), last_heartbeat_at=beat, **kw
    )


def test_workers_are_counted_by_their_current_status_and_long_gone_ones_are_left_out(db_dual):
    worker("a", "running", ago(seconds=3))
    worker("b", "running", ago(seconds=4))
    worker("d", "draining", ago(seconds=2))
    worker("lost", "running", ago(minutes=5))
    worker("stopped", "stopped", ago(seconds=1), stopped_at=ago(seconds=1))
    worker("ancient", "running", ago(days=3))  # no heartbeat for days: hidden, as on the Tasks page
    assert ov.workers(NOW) == {"online": 2, "draining": 1, "lost": 1}


def task(kind, status, **kw):
    return BackgroundTask.create(kind=kind, status=status, **kw)


def test_the_queue_summarises_a_day_of_tasks_in_24_hourly_buckets(db_dual):
    task("repo.recalc", "succeeded", created_at=ago(hours=2, minutes=5), started_at=ago(hours=2), finished_at=ago(hours=2))
    task("repo.recalc", "succeeded", created_at=ago(minutes=30), started_at=ago(minutes=29), finished_at=ago(minutes=28))
    task("lfs.gc", "failed", created_at=ago(minutes=40), started_at=ago(minutes=39), finished_at=ago(minutes=38), last_error="boom")
    task("repo.recalc", "queued", run_after=ago(seconds=90))
    task("repo.recalc", "queued", run_after=NOW + timedelta(minutes=5))
    task("repo.recalc", "running", started_at=ago(seconds=3), locked_by="w", locked_until=NOW + timedelta(seconds=30))
    stats = ov.task_stats(NOW)
    q = ov.queue_from(stats)
    assert (q["queued"], q["running"], q["succeeded_24h"], q["failed_24h"]) == (2, 1, 2, 1)
    assert q["oldest_queued_age_s"] == 90
    assert len(q["hourly"]) == 24
    assert sum(b["ok"] for b in q["hourly"]) == 2 and sum(b["failed"] for b in q["hourly"]) == 1
    assert q["hourly"][-1] == {"ok": 1, "failed": 1}  # the last 60 minutes hold two of them
    assert q["hourly"][-2] == {"ok": 1, "failed": 0}  # the one two hours ago


def test_an_idle_queue_has_no_oldest_age(db_dual):
    q = ov.queue_from(ov.task_stats(NOW))
    assert q["oldest_queued_age_s"] is None and q["queued"] == 0 and q["failed_24h"] == 0


# ---- attention rules (pure) ---------------------------------------------------------------------------

OK_HEALTH = {"status": "ok", "dependencies": [{"name": "postgres", "status": "ok"}, {"name": "smtp", "status": "disabled"}]}
CALM_STATS = {
    "summary": {"failed": 0, "failure_rate": 0.0},
    "backlog": {"oldest_due_seconds": None, "due": 0},
    "thresholds": {"backlog_warn_seconds": 60, "backlog_critical_seconds": 300, "failure_rate_critical": 0.5},
    "kinds": [],
}
NO_QUOTA = {"users_over_quota": [], "repos_over_quota": []}


def attention(**over):
    args = {"health": OK_HEALTH, "workers": {"online": 3, "draining": 0, "lost": 0}, "stats": CALM_STATS, "quota": NO_QUOTA, "lfs": None}
    args.update(over)
    return ov.derive_attention(**args)


def test_a_calm_site_has_nothing_to_attend_to():
    assert attention() == []


def test_a_down_dependency_and_a_lost_worker_are_danger_and_say_which():
    health = {"status": "degraded", "dependencies": [{"name": "postgres", "status": "ok"}, {"name": "minio", "status": "down"}, {"name": "lakefs", "status": "down"}]}
    items = attention(health=health, workers={"online": 1, "draining": 0, "lost": 2})
    by_id = {i["id"]: i for i in items}
    assert by_id["dependency"]["severity"] == "danger" and by_id["dependency"]["count"] == 2
    assert by_id["dependency"]["title"] == "2 dependencies are down" and by_id["dependency"]["hint"] == "minio, lakefs"
    assert by_id["dependency"]["link"] == {"path": "/health", "query": {}, "label": "Health"}
    assert by_id["worker"]["title"] == "2 workers lost their heartbeat" and by_id["worker"]["link"]["path"] == "/tasks"
    one = attention(health={"status": "degraded", "dependencies": [{"name": "valkey", "status": "down"}]}, workers={"online": 1, "draining": 0, "lost": 1})
    assert {i["id"]: i["title"] for i in one} == {"dependency": "1 dependency is down", "worker": "1 worker lost its heartbeat"}


def test_failed_tasks_are_a_warning_until_the_failure_rate_is_critical():
    stats = {**CALM_STATS, "summary": {"failed": 1, "failure_rate": 0.2}, "kinds": [{"kind": "lfs.gc", "failed": 1}, {"kind": "repo.recalc", "failed": 0}]}
    (item,) = attention(stats=stats)
    assert item["id"] == "tasks" and item["severity"] == "warning" and item["count"] == 1
    assert item["title"] == "1 task failed in the last 24 hours" and item["hint"] == "lfs.gc"
    assert item["link"] == {"path": "/tasks", "query": {"status": "failed", "window": "24h"}, "label": "Failed tasks"}
    bad = {**stats, "summary": {"failed": 5, "failure_rate": 0.5}}
    (item,) = attention(stats=bad)
    assert item["severity"] == "danger" and item["title"] == "5 tasks failed in the last 24 hours"


def test_a_backing_up_queue_is_a_warning_then_danger_by_the_task_page_thresholds():
    def with_wait(seconds, due=4):
        return {**CALM_STATS, "backlog": {"oldest_due_seconds": seconds, "due": due}}

    assert attention(stats=with_wait(59)) == []
    (warn,) = attention(stats=with_wait(60))
    assert warn["id"] == "backlog" and warn["severity"] == "warning" and warn["count"] == 4
    assert warn["hint"] == "the oldest has waited 1 min" and warn["link"]["query"] == {"status": "queued"}
    (crit,) = attention(stats=with_wait(301))
    assert crit["severity"] == "danger" and crit["hint"] == "the oldest has waited 5 min"
    quick = {**with_wait(45), "thresholds": {**CALM_STATS["thresholds"], "backlog_warn_seconds": 30}}
    (short,) = attention(stats=quick)
    assert short["hint"] == "the oldest has waited 45 s"


def test_quota_pressure_names_the_worst_users_and_links_to_the_filtered_user_list():
    users = [{"username": n, "private_percentage": p, "public_percentage": 0} for n, p in [("a", 101), ("b", 150), ("c", 120), ("d", 110)]]
    (item,) = attention(quota={"users_over_quota": users, "repos_over_quota": [{"full_id": "o/r", "percentage": 130}]})
    assert item["id"] == "quota" and item["severity"] == "warning" and item["count"] == 5
    assert item["title"] == "4 users and 1 repository are over their storage quota"
    assert item["hint"] == "b 150%, c 120%, d 110%, +1 more"
    assert item["link"] == {"path": "/users", "query": {"filter": "over_quota"}, "label": "Users over quota"}
    (solo,) = attention(quota={"users_over_quota": users[:1], "repos_over_quota": []})
    assert solo["title"] == "1 user is over their storage quota" and solo["hint"] == "a 101%"


def test_only_repositories_over_quota_link_to_the_quota_page():
    repos = [{"full_id": "o/r", "percentage": 130}, {"full_id": "o/s", "percentage": 110}]
    (item,) = attention(quota={"users_over_quota": [], "repos_over_quota": repos})
    assert item["title"] == "2 repositories are over their storage quota" and item["hint"] == "o/r 130%, o/s 110%"
    assert item["link"] == {"path": "/QuotaOverview", "query": {}, "label": "Quota Overview"}


def test_a_failed_lfs_reconciliation_is_informational():
    (item,) = attention(lfs={"task": {"status": "failed"}})
    assert item["id"] == "lfs_drift" and item["severity"] == "info" and item["count"] == 1
    assert item["link"] == {"path": "/storage", "query": {"tab": "lfs"}, "label": "Storage"}
    assert attention(lfs={"task": {"status": "succeeded"}}) == []
    assert attention(lfs={"task": None}) == []


def test_items_are_ordered_danger_then_warning_then_info():
    health = {"status": "degraded", "dependencies": [{"name": "valkey", "status": "down"}]}
    stats = {**CALM_STATS, "summary": {"failed": 1, "failure_rate": 0.1}}
    items = attention(health=health, stats=stats, lfs={"task": {"status": "failed"}}, quota={"users_over_quota": [{"username": "a", "private_percentage": 101, "public_percentage": 0}], "repos_over_quota": []})
    assert [i["id"] for i in items] == ["dependency", "quota", "tasks", "lfs_drift"]


def test_a_missing_source_contributes_nothing_instead_of_breaking_the_list():
    assert attention(health=None, workers=None, stats=None, quota=None, lfs=None) == []


# ---- the cache ----------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


async def test_the_cache_serves_a_fresh_value_then_recomputes_after_the_ttl():
    clock = Clock()
    cache = ov.Cache(clock)
    calls = []

    async def compute():
        calls.append(1)
        return {"n": len(calls)}

    value, at, hit, age = await cache.get("k", 10, compute)
    assert (value, hit, age) == ({"n": 1}, False, 0) and at.endswith("Z")
    clock.t += 4
    value, at2, hit, age = await cache.get("k", 10, compute)
    assert (value, hit, age, at2) == ({"n": 1}, True, 4, at)
    clock.t += 7  # 11 s old, past the 10 s ttl
    value, _, hit, age = await cache.get("k", 10, compute)
    assert (value, hit, age) == ({"n": 2}, False, 0)
    assert len(calls) == 2


async def test_concurrent_requests_for_one_key_share_a_single_computation():
    cache = ov.Cache(Clock())
    started = []

    async def compute():
        started.append(1)
        await asyncio.sleep(0.05)
        return "shared"

    results = await asyncio.gather(*(cache.get("k", 10, compute) for _ in range(5)))
    assert len(started) == 1
    assert [r[0] for r in results] == ["shared"] * 5
    assert sorted(r[2] for r in results) == [False, True, True, True, True]


async def test_a_failed_computation_is_not_cached_and_does_not_hold_the_lock():
    cache = ov.Cache(Clock())
    outcomes = [RuntimeError("probe blew up"), "fine"]

    async def compute():
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    with pytest.raises(RuntimeError):
        await cache.get("k", 10, compute)
    value, _, hit, _ = await cache.get("k", 10, compute)
    assert (value, hit) == ("fine", False)


async def test_different_keys_do_not_share_values():
    cache = ov.Cache(Clock())

    async def one():
        return 1

    async def two():
        return 2

    assert (await cache.get("a", 10, one))[0] == 1
    assert (await cache.get("b", 10, two))[0] == 2


# ---- orchestration ------------------------------------------------------------------------------------


@pytest.fixture
def fake_sources(monkeypatch):
    """Every source replaced by a fixed value, a fresh cache, and a log of what was computed."""
    calls = []

    async def probes(timeout=2.0):
        calls.append("health")
        return [{"name": "postgres", "status": "ok", "latency_ms": 12, "detail": None, "version": "x", "endpoint": "secret://dsn"}, {"name": "smtp", "status": "disabled", "latency_ms": None, "detail": "off"}]

    monkeypatch.setattr(ov, "run_all_probes", probes)
    monkeypatch.setattr(ov, "cache", ov.Cache())

    def stub(name, value):
        def f(*a, **k):
            calls.append(name)
            if isinstance(value, Exception):
                raise value
            return value

        return f

    monkeypatch.setattr(ov, "workers", stub("workers", {"online": 3, "draining": 0, "lost": 0}))
    monkeypatch.setattr(ov, "task_stats", stub("task_stats", CALM_STATS | {"summary": {"failed": 0, "succeeded": 5, "failure_rate": 0}, "backlog": {"due": 0, "scheduled": 0, "running": 0, "oldest_due_seconds": None}, "series": [{"succeeded": 0, "failed": 0}] * 96}))
    monkeypatch.setattr(ov, "counts", stub("counts", {"users": {"total": 1}}))
    monkeypatch.setattr(ov, "activity", stub("activity", {"labels": []}))
    monkeypatch.setattr(ov, "recent", stub("recent", {"items": []}))
    monkeypatch.setattr(ov, "top", stub("top", {"by_commits": [], "by_size": []}))

    async def quota():
        calls.append("quota")
        return NO_QUOTA

    monkeypatch.setattr(ov, "quota_overview", quota)
    monkeypatch.setattr(ov, "lfs_status", stub("lfs", {"task": None}))
    monkeypatch.setattr(ov, "build_identity", lambda: {"git_sha": "0123456789abcdef", "build_time": None})
    return calls


async def test_every_section_carries_its_own_freshness_and_cache_state(fake_sources):
    out = await ov.collect(list(ov.SECTIONS), "7d")
    assert set(out) == set(ov.SECTIONS)
    for name, section in out.items():
        assert section["generated_at"].endswith("Z") and section["cache"] == {"hit": False, "age_s": 0}, name
    assert out["health"]["status"] == "ok" and out["health"]["checked_at_ms"] > 0
    assert out["health"]["dependencies"] == [
        {"name": "postgres", "status": "ok", "latency_ms": 12, "detail": None},
        {"name": "smtp", "status": "disabled", "latency_ms": None, "detail": "off"},
    ]  # nothing else leaks: no endpoint, no version
    assert out["build"] == {"backend": "0123456", "generated_at": out["build"]["generated_at"], "cache": {"hit": False, "age_s": 0}}
    assert out["attention"]["items"] == [] and out["workers"]["online"] == 3
    assert out["queue"]["queued"] == 0 and len(out["queue"]["hourly"]) == 24
    again = await ov.collect(["health", "counts"], "7d")
    assert again["health"]["cache"]["hit"] is True and again["counts"]["cache"]["hit"] is True


async def test_only_the_requested_sections_are_computed_and_unknown_names_are_ignored(fake_sources):
    out = await ov.collect(["counts", "nope"], "24h")
    assert list(out) == ["counts"]
    assert fake_sources == ["counts"]


async def test_attention_alone_pulls_its_sources_through_the_cache(fake_sources):
    await ov.collect(["attention"], "7d")
    assert set(fake_sources) == {"health", "workers", "task_stats", "quota", "lfs"}
    fake_sources.clear()
    await ov.collect(["health", "workers", "queue", "attention"], "7d")
    assert fake_sources == []  # all of it was cached a moment ago


async def test_the_range_is_part_of_the_cache_key_of_counts_and_activity(fake_sources):
    await ov.collect(["counts", "activity"], "7d")
    await ov.collect(["counts", "activity"], "30d")
    assert fake_sources.count("counts") == 2 and fake_sources.count("activity") == 2


async def test_a_failing_section_reports_its_error_and_leaves_the_others_alone(fake_sources, monkeypatch):
    monkeypatch.setattr(ov, "recent", lambda: (_ for _ in ()).throw(RuntimeError("db went away " + "x" * 400)))
    out = await ov.collect(["recent", "counts", "workers"], "7d")
    assert out["recent"]["error"]["code"] == "section_failed"
    assert out["recent"]["error"]["message"].startswith("db went away") and len(out["recent"]["error"]["message"]) <= 200
    assert "generated_at" in out["recent"] and "cache" not in out["recent"]
    assert "error" not in out["counts"] and "error" not in out["workers"]


async def test_a_failing_attention_source_is_skipped_not_fatal(fake_sources, monkeypatch):
    async def broken():
        raise RuntimeError("quota query failed")

    monkeypatch.setattr(ov, "quota_overview", broken)
    monkeypatch.setattr(ov, "lfs_status", lambda: (_ for _ in ()).throw(RuntimeError("lfs")))
    out = await ov.collect(["attention"], "7d")
    assert out["attention"]["items"] == [] and "error" not in out["attention"]


async def test_an_unknown_checkout_is_reported_as_unknown(fake_sources, monkeypatch):
    monkeypatch.setattr(ov, "build_identity", lambda: {"git_sha": None, "build_time": None})
    out = await ov.collect(["build"], "7d")
    assert out["build"]["backend"] == "unknown"


async def test_health_overall_status_is_degraded_if_anything_is_down_and_disabled_if_nothing_runs(fake_sources, monkeypatch):
    async def down(timeout=2.0):
        return [{"name": "postgres", "status": "down", "latency_ms": None, "detail": "refused"}]

    monkeypatch.setattr(ov, "run_all_probes", down)
    out = await ov.collect(["health"], "7d")
    assert out["health"]["status"] == "degraded"

    async def off(timeout=2.0):
        return [{"name": "smtp", "status": "disabled", "latency_ms": None, "detail": None}]

    monkeypatch.setattr(ov, "cache", ov.Cache())
    monkeypatch.setattr(ov, "run_all_probes", off)
    assert (await ov.collect(["health"], "7d"))["health"]["status"] == "disabled"


async def test_quota_and_lfs_sources_are_the_existing_admin_functions(monkeypatch):
    from kohakuhub.api.admin.routers import quota as quota_router
    from kohakuhub import storage_cleanup

    calls = []

    async def fake_quota(_admin=None):
        calls.append("quota")
        return {"users_over_quota": [], "repos_over_quota": []}

    monkeypatch.setattr(quota_router, "get_quota_overview", fake_quota)
    monkeypatch.setattr(storage_cleanup, "lfs_reconciliation_status", lambda: {"task": None})
    monkeypatch.setattr(ov, "cache", ov.Cache())
    assert await ov.quota_overview() == {"users_over_quota": [], "repos_over_quota": []}
    assert ov.lfs_status() == {"task": None}
    assert calls == ["quota"]
