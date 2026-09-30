"""Storage usage is kept up to date as repositories change (kohakuhub.usage).

Everything runs against the real database, LakeFS and bucket. Each check
compares the kept counters with an exact count made here from LakeFS and
the database.
"""

import asyncio
import importlib.util
import json
import random
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from peewee import fn

from test.kohakuhub.api.helpers import encode_ndjson
from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _live, lfs

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db_migrations"
    / "023_repository_usage_counters.py"
)
ORG = "acme-labs"


@pytest.fixture
def u(prepared_backend_test_state, monkeypatch):
    ns = type("U", (), {})()
    ns.cfg = _live("kohakuhub.config").cfg
    monkeypatch.setattr(ns.cfg.app, "repository_revert_enabled", True)
    monkeypatch.setattr(ns.cfg.app, "repository_reset_enabled", True)
    ns.db = _live("kohakuhub.db")
    ns.gc = _live("kohakuhub.lfs_gc")
    ns.usage = _live("kohakuhub.usage")
    ns.tasks = _live("kohakuhub.tasks")
    ns.records = _live("kohakuhub.api.commit.records")
    ns.quota = _live("kohakuhub.api.quota.util")
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.rest = _live("kohakuhub.lakefs_rest_client")
    ns.rest._singleton_client = None
    ns.db.BackgroundTask.delete().where(
        ns.db.BackgroundTask.kind.startswith("usage.")
    ).execute()
    yield ns
    ns.db.LfsObjectTombstone.delete().execute()
    ns.db.BackgroundTask.delete().where(
        ns.db.BackgroundTask.kind.startswith("usage.")
    ).execute()
    ns.rest._singleton_client = None


def _row(u, full_id):
    return u.db.Repository.get(u.db.Repository.full_id == full_id)


async def _exact(u, full_id):
    """(regular bytes on main, stored LFS bytes its history links), counted here."""
    row = _row(u, full_id)
    client = u.lakefs.get_lakefs_client()
    regular, after = 0, ""
    while True:
        page = await client.list_objects(
            repository=u.lakefs.resolve_lakefs_repo(row),
            ref="main",
            after=after,
            amount=1000,
        )
        regular += sum(
            o["size_bytes"]
            for o in page["results"]
            if u.gc.lfs_oid(o.get("physical_address")) is None
        )
        if not page["pagination"]["has_more"]:
            break
        after = page["pagination"]["next_offset"]
    gone = {t.sha256 for t in u.db.LfsObjectTombstone.select()}
    H = u.db.LFSObjectHistory
    linked = {
        h.sha256: h.size
        for h in H.select().where(H.repository == row)
        if len(h.sha256) == 64
    }
    return regular, sum(size for sha, size in linked.items() if sha not in gone)


def _kept(u, full_id):
    row = _row(u, full_id)
    assert row.used_bytes == row.main_regular_bytes + row.lfs_bytes
    return row.main_regular_bytes, row.lfs_bytes


async def _assert_exact(u, full_id):
    assert _kept(u, full_id) == await _exact(u, full_id)


async def _new(u, client, name, organization=None):
    body = {
        "type": "model",
        "name": name,
        **({"organization": organization} if organization else {}),
    }
    response = await client.post("/api/repos/create", json=body)
    assert response.status_code == 200, response.text
    repo = Repo(u, client, name)
    repo.id = f"{organization or 'owner'}/{name}"
    return repo


def _pending(u, kind):
    T = u.db.BackgroundTask
    return [
        json.loads(t.payload)
        for t in T.select()
        .where((T.kind == kind) & (T.status == u.tasks.QUEUED))
        .order_by(T.id)
    ]


def _sha(content):
    import hashlib

    return hashlib.sha256(content).hexdigest()


async def test_commits_keep_a_repository_usage_exact(u, owner_client):
    repo = await _new(u, owner_client, "usage-commits")
    assert _row(u, repo.id).main_counted_commit == await repo.head()
    assert _kept(u, repo.id) == (0, 0)

    weights = b"weights v1 " * 100
    await repo.commit(
        _file("config.json", "{}" * 50),
        _file("notes.md", "n" * 300),
        lfs("w.bin", weights),
    )
    assert _kept(u, repo.id) == (400, len(weights))
    await _assert_exact(u, repo.id)

    # Overwrite and delete regular files; replace and delete LFS files
    await repo.commit(
        _file("config.json", "{}"),
        _delete("notes.md"),
        lfs("w.bin", b"weights v2 " * 90),
    )
    assert _kept(u, repo.id) == (2, len(weights) + 990)  # both LFS versions stay stored
    await repo.commit(_delete("w.bin"), _delete("config.json"))
    assert _kept(u, repo.id) == (0, len(weights) + 990)
    # The same content again, under another path: stored once
    await repo.commit(lfs("again.bin", weights), lfs("twice.bin", weights))
    assert _kept(u, repo.id) == (0, len(weights) + 990)
    await _assert_exact(u, repo.id)
    assert not _pending(u, u.usage.RECOUNT_REPOSITORY_KIND)

    # Another branch: its LFS objects count, its regular files do not
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(
        _file("dev.txt", "d" * 70), lfs("dev.bin", b"dev only " * 30), branch="dev"
    )
    assert _kept(u, repo.id) == (0, len(weights) + 990 + 270)
    await _assert_exact(u, repo.id)

    # The same object in another repository counts there too
    other = await _new(u, owner_client, "usage-commits-other")
    await other.commit(lfs("copy.bin", weights))
    assert _kept(u, other.id) == (0, len(weights))


async def test_branch_operations_keep_it_exact(u, owner_client):
    repo = await _new(u, owner_client, "usage-branch-ops")
    first = await repo.commit(_file("a.txt", "a" * 100), lfs("a.bin", b"first " * 50))
    second = await repo.commit(
        _file("a.txt", "a" * 10),
        _file("b.txt", "b" * 40),
        lfs("a.bin", b"second " * 40),
    )
    await repo.commit(_delete("b.txt"), _file("c.txt", "c" * 25))
    await _assert_exact(u, repo.id)

    response = await owner_client.post(
        f"/api/models/{repo.id}/branch/main/revert", json={"ref": second}
    )
    assert response.status_code == 200, response.text
    await _assert_exact(u, repo.id)

    response = await owner_client.post(
        f"/api/models/{repo.id}/branch/main/reset", json={"ref": first, "force": True}
    )
    assert response.status_code == 200, response.text
    await _assert_exact(u, repo.id)

    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "feature", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(
        _file("feature.txt", "f" * 55), lfs("f.bin", b"feature " * 20), branch="feature"
    )
    response = await owner_client.post(
        f"/api/models/{repo.id}/merge/feature/into/main", json={}
    )
    assert response.status_code == 200, response.text
    await _assert_exact(u, repo.id)
    assert not _pending(u, u.usage.RECOUNT_REPOSITORY_KIND)


async def test_garbage_collection_takes_objects_out_and_back(
    u, owner_client, monkeypatch
):
    repo = await _new(u, owner_client, "usage-gc")
    other = await _new(u, owner_client, "usage-gc-other")
    content = b"collected " * 60
    await repo.commit(lfs("x.bin", content), _file("r.txt", "r" * 10))
    await other.commit(lfs("y.bin", content))
    sha = _sha(content)

    reason = {"value": None}
    monkeypatch.setattr(u.gc, "retention_reason", lambda s, **kw: reason["value"])
    assert u.gc.begin_delete(sha) is True
    assert _kept(u, repo.id) == (10, 0) and _kept(u, other.id) == (0, 0)
    u.gc.begin_delete(sha)  # tombstoned already: counted out once
    u.gc.finish_delete([sha])
    assert _kept(u, repo.id) == (10, 0)
    await _assert_exact(u, repo.id)

    # Uploaded again and claimed by a commit: counted back everywhere
    assert u.gc.claim_for_commit(sha, True) is True
    assert _kept(u, repo.id) == (10, len(content)) and _kept(u, other.id) == (
        0,
        len(content),
    )

    # A deletion that turns out to be relied on puts it back as well
    assert u.gc.begin_delete(sha) is True
    assert _kept(u, other.id) == (0, 0)
    reason["value"] = "head"
    assert u.gc.begin_delete(sha) is False
    assert _kept(u, other.id) == (0, len(content))
    await _assert_exact(u, repo.id)
    await _assert_exact(u, other.id)


async def test_a_revived_object_gone_again_is_counted_out(u, owner_client, monkeypatch):
    repo = await _new(u, owner_client, "usage-revive")
    content = b"revived " * 40
    await repo.commit(lfs("x.bin", content))
    sha = _sha(content)
    monkeypatch.setattr(u.gc, "retention_reason", lambda s, **kw: None)
    u.gc.begin_delete(sha)
    u.gc.finish_delete([sha])
    assert _kept(u, repo.id) == (0, 0)

    checks = iter([True, False])  # stored at the check, gone after the claim
    monkeypatch.setattr(
        u.records, "object_exists", lambda bucket, key: _async(next(checks))
    )
    with pytest.raises(u.records.OperationRefused):
        await u.records.claim_objects(
            {"x.bin": {"physical_address": f"s3://b/{u.gc.lfs_key(sha)}"}}, "test"
        )
    assert u.gc.tombstone_state(sha) == u.gc.DELETED
    await _assert_exact(u, repo.id)

    # Tombstoned again meanwhile (by a collection): counted out once
    monkeypatch.setattr(u.records, "claim_for_commit", lambda oid, stored: True)
    checks = iter([True, False])
    with pytest.raises(u.records.OperationRefused):
        await u.records.claim_objects(
            {"x.bin": {"physical_address": f"s3://b/{u.gc.lfs_key(sha)}"}}, "test"
        )
    await _assert_exact(u, repo.id)


async def _async(value):
    return value


async def test_a_main_the_count_does_not_follow_is_recounted(u, owner_client):
    repo = await _new(u, owner_client, "usage-chain")
    await repo.commit(_file("a.txt", "a" * 30))
    row = _row(u, repo.id)
    u.db.Repository.update(
        main_counted_commit="elsewhere", main_regular_bytes=999, used_bytes=999
    ).where(u.db.Repository.id == row.id).execute()
    await repo.commit(_file("b.txt", "b" * 5))
    assert _kept(u, repo.id) == (999, 0)  # not applied on a count that does not match
    assert _pending(u, u.usage.RECOUNT_REPOSITORY_KIND) == [{"repo_id": row.id}]

    await u.usage.recount_repository_task({"repo_id": row.id})
    await _assert_exact(u, repo.id)
    assert _row(u, repo.id).main_counted_commit == await repo.head()
    await repo.commit(_file("c.txt", "c"))
    assert _kept(u, repo.id) == (36, 0)  # counted again from there

    # A move it cannot read is recounted too
    u.db.BackgroundTask.delete().where(
        u.db.BackgroundTask.kind.startswith("usage.")
    ).execute()
    client = u.lakefs.get_lakefs_client()
    await u.records.count_main_move(client, repo.lakefs_repo, row, "0" * 64)
    assert _pending(u, u.usage.RECOUNT_REPOSITORY_KIND) == [{"repo_id": row.id}]
    # An initial commit has no parent to count from
    log = await client.log_commits(repository=repo.lakefs_repo, ref="main", amount=100)
    initial = log["results"][-1]["id"]
    await u.records.count_main_move(client, repo.lakefs_repo, row, initial)
    assert _kept(u, repo.id) == (36, 0)


async def test_a_recount_racing_a_change_tries_again_later(
    u, owner_client, monkeypatch
):
    repo = await _new(u, owner_client, "usage-busy")
    await repo.commit(_file("a.txt", "a" * 30))
    row = _row(u, repo.id)
    listed = u.usage._main_regular_bytes

    async def racing(repository):
        result = await listed(repository)
        await repo.commit(_file("b.txt", "b" * 7))  # lands while the recount lists
        return result

    monkeypatch.setattr(u.usage, "_main_regular_bytes", racing)
    assert await u.usage.recount_repository(row.id) is None
    monkeypatch.setattr(u.usage, "_main_regular_bytes", listed)
    assert _kept(u, repo.id) == (37, 0)  # the commit's change stands
    task = u.db.BackgroundTask.get(
        u.db.BackgroundTask.kind == u.usage.RECOUNT_REPOSITORY_KIND
    )
    assert task.run_after > u.db.utcnow()
    assert await u.usage.recount_repository(-1) is None  # a repository gone meanwhile


async def test_the_full_recount_sets_every_repository_and_reports_drift(
    u, owner_client
):
    testing = _live("kohakuhub.task_testing")
    repo = await _new(u, owner_client, "usage-drift")
    await repo.commit(_file("a.txt", "a" * 30), lfs("a.bin", b"drift " * 30))
    org_repo = await _new(u, owner_client, "usage-drift-org", ORG)
    await org_repo.commit(_file("o.txt", "o" * 12))
    R = u.db.Repository
    R.update(
        main_regular_bytes=1, lfs_bytes=2, used_bytes=3, main_counted_commit=None
    ).where(R.full_id.in_([repo.id, org_repo.id])).execute()
    u.db.User.update(public_used_bytes=0).where(u.db.User.username == ORG).execute()

    ctx = testing.RecordingContext(kind=u.usage.RECOUNT_KIND)
    await u.usage.recount({}, ctx)
    await _assert_exact(u, repo.id)
    await _assert_exact(u, org_repo.id)
    stats = ctx.checkpoint_state["stats"]
    assert stats["repositories"] == R.select().count()
    assert stats["drifted"] >= 2
    drifted = {d["repository"]: d for d in ctx.checkpoint_state["drift"]}
    assert drifted[f"model:{repo.id}"]["before"] == 3
    assert drifted[f"model:{repo.id}"]["after"] == _row(u, repo.id).used_bytes
    snapshot = u.db.User.get(u.db.User.username == ORG).public_used_bytes
    assert snapshot == u.usage.namespace_used(ORG, False) > 0

    # Interrupted anywhere and run again, it ends the same (the task contract)
    second = await _new(u, owner_client, "usage-drift-second")
    await second.commit(lfs("s.bin", b"second " * 9))
    ids = [_row(u, repo.id).id, _row(u, second.id).id]
    R.update(namespace="usage-ns").where(R.id.in_(ids)).execute()
    try:

        def corrupt():
            R.update(used_bytes=5, main_regular_bytes=5, lfs_bytes=0).where(
                R.id.in_(ids)
            ).execute()

        def counters():
            return [
                (r.main_regular_bytes, r.lfs_bytes, r.used_bytes)
                for r in R.select().where(R.id.in_(ids)).order_by(R.id)
            ]

        points = await testing.run_with_interruptions(
            u.usage.recount, {"namespace": "usage-ns"}, reset=corrupt, snapshot=counters
        )
        assert points > 4
    finally:
        R.update(namespace="owner").where(R.id.in_(ids)).execute()
    await _assert_exact(u, repo.id)
    await _assert_exact(u, second.id)

    # Cancelled, it stops
    ctx = testing.RecordingContext(kind=u.usage.RECOUNT_KIND)
    ctx.cancel_requested = True
    with pytest.raises(u.tasks.TaskCancelled):
        await u.usage.recount({}, ctx)


async def test_quotas_are_checked_against_the_summed_usage(u, owner_client):
    repo = await _new(u, owner_client, "usage-quota")
    await repo.commit(_file("a.txt", "a" * 1000))
    private = await _new(u, owner_client, "usage-quota-private")
    R = u.db.Repository
    R.update(private=True).where(R.full_id == private.id).execute()
    await private.commit(_file("p.txt", "p" * 300))
    used = u.usage.namespace_usage(["owner", "nobody"])
    assert used["nobody"] == {"private": 0, "public": 0}
    total = R.select().where(R.namespace == "owner")
    assert used["owner"]["public"] == sum(r.used_bytes for r in total if not r.private)
    assert (
        used["owner"]["private"] == sum(r.used_bytes for r in total if r.private) >= 300
    )

    U = u.db.User
    U.update(public_quota_bytes=used["owner"]["public"] + 10).where(
        U.username == "owner"
    ).execute()
    try:
        assert u.quota.check_quota("owner", 10, False) == (True, None)
        allowed, error = u.quota.check_quota("owner", 11, False)
        assert not allowed and "public" in error.lower()
        info = u.quota.get_storage_info("owner")
        assert info["public_used_bytes"] == used["owner"]["public"]
        assert info["public_available_bytes"] == 10
        repo_info = u.quota.get_repo_storage_info(_row(u, repo.id))
        assert repo_info["namespace_used_bytes"] == used["owner"]["public"]
        with pytest.raises(ValueError):
            u.quota.set_repo_quota(_row(u, repo.id), 11)
        u.quota.set_repo_quota(_row(u, repo.id), 10)
        assert _kept(u, repo.id) == (
            1000,
            0,
        )  # setting a quota leaves the counters alone

        # Making the private repository public counts it against the public quota
        response = await owner_client.put(
            f"/api/models/{private.id}/settings", json={"private": False}
        )
        assert response.status_code == 400, response.text
        assert (
            response.json()["detail"]["repo_size_bytes"]
            == _row(u, private.id).used_bytes
        )
    finally:
        U.update(public_quota_bytes=None).where(U.username == "owner").execute()
        R.update(quota_bytes=None).where(R.full_id == repo.id).execute()


async def test_moves_and_deletes_carry_the_usage(u, owner_client):
    repo = await _new(u, owner_client, "usage-move")
    await repo.commit(_file("a.txt", "a" * 500), lfs("m.bin", b"moved " * 30))
    size = _row(u, repo.id).used_bytes
    before = (
        u.usage.namespace_used("owner", False),
        u.usage.namespace_used(ORG, False),
    )
    response = await owner_client.post(
        "/api/repos/move",
        json={"fromRepo": repo.id, "toRepo": f"{ORG}/usage-move", "type": "model"},
    )
    assert response.status_code == 200, response.text
    assert _row(u, f"{ORG}/usage-move").used_bytes == size
    after = (u.usage.namespace_used("owner", False), u.usage.namespace_used(ORG, False))
    assert after == (before[0] - size, before[1] + size)

    # Deleting answers at once; the storage is purged in the background
    row = _row(u, f"{ORG}/usage-move")
    lakefs_repo = u.lakefs.resolve_lakefs_repo(row)
    response = await owner_client.request(
        "DELETE",
        "/api/repos/delete",
        json={"type": "model", "name": "usage-move", "organization": ORG},
    )
    assert response.status_code == 200, response.text
    assert u.usage.namespace_used(ORG, False) == before[1]
    purge = _pending(u, _live("kohakuhub.storage_cleanup").PURGE_KIND)
    assert {"lakefs_repo": lakefs_repo, "repo": f"model:{ORG}/usage-move"} in purge


async def test_recount_endpoints(u, owner_client, admin_client):
    repo = await _new(u, owner_client, "usage-endpoints")
    await repo.commit(_file("a.txt", "a" * 40))
    R = u.db.Repository
    R.update(used_bytes=7, main_regular_bytes=7).where(R.full_id == repo.id).execute()

    # A repository is recounted at once
    response = await owner_client.post(f"/api/quota/repo/model/{repo.id}/recalculate")
    assert response.status_code == 200, response.text
    assert response.json()["used_bytes"] == 40
    # A namespace is recounted in the background
    response = await owner_client.post("/api/quota/owner/recalculate")
    assert response.status_code == 200, response.text
    assert _pending(u, u.usage.RECOUNT_KIND) == [{"namespace": "owner"}]

    response = await admin_client.post("/admin/api/quota/owner/recalculate")
    assert response.status_code == 200, response.text
    assert response.json()["already_pending"] is True

    response = await admin_client.get("/admin/api/usage/recount")
    assert response.json()["task"] is None  # no site-wide recount yet
    response = await admin_client.post("/admin/api/usage/recount")
    body = response.json()
    assert body["already_pending"] is False
    response = await admin_client.post("/admin/api/repositories/recalculate-all")
    assert response.json() == {"task_id": None, "already_pending": True}
    status = (await admin_client.get("/admin/api/usage/recount")).json()
    assert (
        status["task"]["id"] == body["task_id"] and status["task"]["status"] == "queued"
    )
    assert status["interval_hours"] == 0

    task = u.db.BackgroundTask.get_by_id(body["task_id"])
    ctx = _live("kohakuhub.task_testing").RecordingContext(kind=u.usage.RECOUNT_KIND)
    await u.usage.recount({}, ctx)
    task.checkpoint = json.dumps(ctx.checkpoint_state)
    task.status, task.finished_at = u.tasks.SUCCEEDED, u.db.utcnow()
    task.save()
    status = (await admin_client.get("/admin/api/usage/recount")).json()["task"]
    assert status["stats"]["repositories"] == R.select().count()
    assert status["finished_at"].endswith("+00:00")

    # Over quota: a user by its summed usage, a repository by its own quota or its account's
    U = u.db.User
    R.update(quota_bytes=1).where(R.full_id == repo.id).execute()
    U.update(private_quota_bytes=1, public_quota_bytes=1).where(
        U.username == "owner"
    ).execute()
    try:
        overview = (await admin_client.get("/admin/api/quota/overview")).json()
    finally:
        U.update(private_quota_bytes=None, public_quota_bytes=None).where(
            U.username == "owner"
        ).execute()
        R.update(quota_bytes=None).where(R.full_id == repo.id).execute()
    over = next(
        item for item in overview["users_over_quota"] if item["username"] == "owner"
    )
    assert over["public_used"] == u.usage.namespace_used("owner", False)
    repos_over = {item["full_id"]: item for item in overview["repos_over_quota"]}
    assert repos_over[repo.id]["percentage"] == 4000.0
    assert "owner/demo-model" in repos_over  # inherits the account's quota of 1 byte
    assert (
        overview["system_storage"]["lfs_used"] == R.select(fn.SUM(R.lfs_bytes)).scalar()
    )
    owner_total = sum(u.usage.namespace_usage(["owner"])["owner"].values())
    assert {
        "username": "owner",
        "is_org": False,
        "total_bytes": owner_total,
    } in overview["top_consumers"]
    assert (
        overview["system_storage"]["private_used"] == u.usage.users_usage()["private"]
    )
    users = (
        await admin_client.get("/admin/api/users", params={"search": "owner"})
    ).json()["users"]
    listed = next(item for item in users if item["username"] == "owner")
    assert listed["public_used_bytes"] == u.usage.namespace_used("owner", False)


async def test_random_changes_leave_no_drift(u, owner_client):
    """Random commits across repositories (adds, overwrites, deletes, LFS
    objects shared between them), then a full recount: nothing had drifted."""
    rng = random.Random(23)
    repos = [await _new(u, owner_client, f"usage-random-{i}") for i in range(3)]
    blobs = [f"blob {i} ".encode() * rng.randint(5, 50) for i in range(12)]
    for step in range(40):
        repo = rng.choice(repos)
        choice = rng.random()
        path = f"f{rng.randint(0, 6)}"
        if choice < 0.45:
            await repo.commit(_file(f"{path}.txt", "x" * rng.randint(1, 200)))
        elif choice < 0.8:
            await repo.commit(lfs(f"{path}.bin", rng.choice(blobs)))
        else:
            head = (await owner_client.get(f"/api/models/{repo.id}/tree/main")).json()
            files = [f["path"] for f in head if f["type"] == "file"]
            if files:
                await repo.commit(_delete(rng.choice(files)))
    for repo in repos:
        await _assert_exact(u, repo.id)
    ctx = _live("kohakuhub.task_testing").RecordingContext(kind=u.usage.RECOUNT_KIND)
    await u.usage.recount({"namespace": "owner"}, ctx)
    drifted = {d["repository"] for d in ctx.checkpoint_state["drift"]}
    assert not drifted & {f"model:{repo.id}" for repo in repos}


def _migration():
    spec = importlib.util.spec_from_file_location("migration_023", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_migration_fills_the_counters_and_schedules_a_recount(
    u, owner_client
):
    repo = await _new(u, owner_client, "usage-migration")
    await repo.commit(_file("a.txt", "a" * 30), lfs("a.bin", b"migrated " * 30))
    migration = _migration()
    D = u.db
    assert migration.is_applied(D.db, u.cfg)
    postgres = u.cfg.app.db_backend == "postgres"
    with D.db.atomic() as transaction:
        D.db.execute_sql('DROP INDEX IF EXISTS "repository_namespace_private"')
        for column in ("main_regular_bytes", "lfs_bytes", "main_counted_commit"):
            D.db.execute_sql(f'ALTER TABLE "repository" DROP COLUMN "{column}"')
        D.db.execute_sql(
            (
                'UPDATE "repository" SET used_bytes = used_bytes + 5 WHERE full_id = %s'
                if postgres
                else 'UPDATE "repository" SET used_bytes = used_bytes + 5 WHERE full_id = ?'
            ),
            (repo.id,),
        )
        assert not migration.is_applied(D.db, u.cfg)
        assert migration.run() is True
        regular, lfs_bytes = await _exact(u, repo.id)
        assert _kept(u, repo.id) == (regular + 5, lfs_bytes)
        assert _row(u, repo.id).main_counted_commit is None
        assert _pending(u, u.usage.RECOUNT_KIND) == [{}]
        assert migration.run() is True  # applied: nothing again
        transaction.rollback()
    await _assert_exact(u, repo.id)

    # Never applied on a schema missing what came before
    with D.db.atomic() as transaction:
        D.db.execute_sql('DROP TABLE "lfs_gc_state"')
        assert not migration.is_applied(D.db, u.cfg)
        transaction.rollback()


def test_the_migration_reports_a_failure(u, monkeypatch):
    migration = _migration()
    monkeypatch.setattr(migration, "is_applied", lambda db, cfg: False)

    def broken(greatest):
        raise RuntimeError("boom")

    monkeypatch.setattr(migration, "_migrate", broken)
    assert migration.run() is False
    monkeypatch.setattr(
        migration, "should_skip_due_to_future_migrations", lambda *a: True
    )
    assert migration.run() is True


async def test_counting_without_row_locks(u, owner_client, monkeypatch):
    """SQLite has no FOR UPDATE (it serializes writers): counting works without it."""
    repo = await _new(u, owner_client, "usage-no-row-locks")
    monkeypatch.setattr(u.db.Repository._meta.database, "for_update", False)
    await repo.commit(_file("a.txt", "a" * 9), lfs("a.bin", b"unlocked " * 11))
    assert _kept(u, repo.id) == (9, 99)
    assert await u.usage.recount_repository(_row(u, repo.id).id) is not None
    await _assert_exact(u, repo.id)


async def test_recount_edges(u, owner_client, monkeypatch):
    repo = await _new(u, owner_client, "usage-edges")
    await repo.commit(*[_file(f"f{i}.txt", "e" * (i + 1)) for i in range(5)])
    row = _row(u, repo.id)
    # Listed page by page
    monkeypatch.setattr(u.usage, "LIST_PAGE", 2)
    u.db.Repository.update(main_regular_bytes=0, used_bytes=0).where(
        u.db.Repository.id == row.id
    ).execute()
    await u.usage.recount_repository(row.id)
    await _assert_exact(u, repo.id)

    # Without a main branch nothing regular counts; other LakeFS failures surface
    client = u.lakefs.get_lakefs_client()

    def failing(status):
        async def get_branch(**kwargs):
            request = httpx.Request("GET", "http://lakefs")
            raise httpx.HTTPStatusError(
                "x", request=request, response=httpx.Response(status, request=request)
            )

        return get_branch

    monkeypatch.setattr(client, "get_branch", failing(404))
    assert await u.usage._main_regular_bytes(row) == (None, 0)
    monkeypatch.setattr(client, "get_branch", failing(503))
    with pytest.raises(httpx.HTTPStatusError):
        await u.usage._main_regular_bytes(row)

    # Only sha256 objects are LFS objects; one no history links changes nothing
    before = _kept(u, repo.id)
    u.usage.object_gone("d41d8cd98f00b204e9800998ecf8427e")
    u.usage.object_back("f" * 64)
    assert _kept(u, repo.id) == before

    # A repository changing while the full recount reaches it is left to its own recount
    async def busy(repo_id):
        return None

    monkeypatch.setattr(u.usage, "recount_repository", busy)
    ctx = _live("kohakuhub.task_testing").RecordingContext(kind=u.usage.RECOUNT_KIND)
    await u.usage.recount({"namespace": "owner"}, ctx)
    stats = ctx.checkpoint_state["stats"]
    assert stats["busy"] == stats["repositories"] > 0


async def test_a_commit_stands_whatever_counting_it_does(u, owner_client, monkeypatch):
    repo = await _new(u, owner_client, "usage-commit-stands")

    def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(u.records.usage, "main_moved", broken)
    await repo.commit(_file("a.txt", "a"))  # answered 200
    row = _row(u, repo.id)
    assert _pending(u, u.usage.RECOUNT_REPOSITORY_KIND) == [{"repo_id": row.id}]
    monkeypatch.setattr(u.records.usage, "enqueue_repository_recount", broken)
    await repo.commit(_file("b.txt", "b"))  # still answered 200

    # An operation whose outcome is unknown recounts main, not another branch
    monkeypatch.undo()
    u.db.BackgroundTask.delete().where(
        u.db.BackgroundTask.kind.startswith("usage.")
    ).execute()
    u.records.outcome_unknown(row, "dev")
    assert not _pending(u, u.usage.RECOUNT_REPOSITORY_KIND)
    u.records.outcome_unknown(row, "main")
    assert _pending(u, u.usage.RECOUNT_REPOSITORY_KIND) == [{"repo_id": row.id}]


async def test_concurrent_commits_to_main(u, owner_client):
    """Commits racing on main (LakeFS refuses some: "predicate failed")."""
    repo = await _new(u, owner_client, "usage-concurrent")

    async def attempt(i):
        op = lfs(f"c{i}.bin", f"blob {i}".encode() * 9)
        repo.put(op["value"].pop("_content"))
        lines = [
            {"key": "header", "value": {"summary": "race"}},
            _file(f"c{i}.txt", "c" * (i + 1)),
            op,
        ]
        response = await owner_client.post(
            f"/api/models/{repo.id}/commit/main",
            content=encode_ndjson(lines),
            headers={"Content-Type": "application/x-ndjson"},
        )
        return response.status_code

    statuses = await asyncio.gather(*(attempt(i) for i in range(8)))
    assert 200 in statuses
    # A count that arrived out of order was left to a recount of the repository
    for payload in _pending(u, u.usage.RECOUNT_REPOSITORY_KIND):
        await u.usage.recount_repository_task(payload)
    await _assert_exact(u, repo.id)


async def test_a_recount_goes_on_past_a_repository_it_cannot_read(
    u, owner_client, monkeypatch
):
    repo = await _new(u, owner_client, "usage-unreadable")
    await repo.commit(_file("a.txt", "a" * 3))
    broken_id = _row(u, repo.id).id
    recount = u.usage.recount_repository

    async def flaky(repo_id):
        if repo_id == broken_id:
            raise RuntimeError("LakeFS 503")
        return await recount(repo_id)

    monkeypatch.setattr(u.usage, "recount_repository", flaky)
    ctx = _live("kohakuhub.task_testing").RecordingContext(kind=u.usage.RECOUNT_KIND)
    await u.usage.recount({"namespace": "owner"}, ctx)
    stats = ctx.checkpoint_state["stats"]
    assert stats["failed"] == 1 and stats["repositories"] > 1
    task = u.db.BackgroundTask.get(
        u.db.BackgroundTask.kind == u.usage.RECOUNT_REPOSITORY_KIND
    )
    assert (
        json.loads(task.payload) == {"repo_id": broken_id}
        and task.run_after > u.db.utcnow()
    )


def test_the_status_is_not_the_next_periodic_recount(u):
    T = u.db.BackgroundTask
    started = u.usage.enqueue_recount()
    T.update(status=u.tasks.SUCCEEDED, dedupe_key=None).where(T.id == started).execute()
    # A periodic recount queues its next occurrence when it starts
    u.tasks.enqueue(
        u.usage.RECOUNT_KIND,
        dedupe_key=u.tasks.periodic_key(u.usage.RECOUNT_KIND),
        run_after=u.db.utcnow() + timedelta(hours=6),
    )
    assert u.usage.recount_status()["task"]["id"] == started
    # A recount waiting to retry after a failed attempt is shown
    T.update(
        status=u.tasks.QUEUED,
        attempts=1,
        run_after=u.db.utcnow() + timedelta(minutes=1),
    ).where(T.id == started).execute()
    shown = u.usage.recount_status()["task"]
    assert (shown["id"], shown["status"]) == (started, "queued")
